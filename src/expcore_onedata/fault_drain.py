# Provenance: new
"""The session drain of an outage run: no run is admitted to the federation until its sessions have ended (O11).

**Why.** During an outage an agent's read of a file only the stopped provider holds, through the local provider, leaves
an on-demand sync job on that provider's per-file synchronizer, and nothing calls it back. A later read of the same file
and range, from any session, joins the job and hangs. The job is cancelled only when the run's callers end: a REST read
when its HTTP request process dies (seconds after the client closes the socket, at most about 5 min after), a mount
(oneclient) at the file's release or about 300 s after oneclient's last connection closes (op-worker's
`fuse_session_grace_period_seconds`), plus about 15 s if the removal retries. Eviction, a transfer cancel, deleting the
file and `restart_link` cancel nothing; only a container restart does. (Source-read of op-worker f118ed9, not measured.)

**What.** `SessionDrain` is carried by an exclusive `Admission` (`expcore_onedata.admission`). The fault injector
arms it before it restores a provider it took down (`FaultInjector.clear`; `disarm` takes it back when the provider
never went down), and `owe` arms it before a recovery brings back a provider an earlier run left down. Both come
before the restore, so nothing between the journal's clear and the restore's report can leave the federation open.
Once armed, the federation admits no run before the drain has ended: the plan's `until` is `[faults] session_drain_s`
(320 s by default) after the later of the run's mount disconnecting and its REST gate stopping.

**How it is kept.** When an armed admission is let go, `SessionDrain.publish` writes `until`, on the wall clock, to the
journal's drain record (`InjectionJournal.raise_drain`, `federation.drain`), which only ever moves later; and every
admission, shared or exclusive, checks that record once it holds the lock (`FederationLock.acquire` waits for it,
`try_acquire` refuses). So the drain is kept by the federation itself and not by the process that began it: an
interrupt, a restore that holds no admission and a recovery in another process cannot reopen it. An injector that
holds the federation by an admission of its own also waits the drain out in `clear`, so its snapshot records the wait
(`SessionDrain.wait`). When the record cannot be written, the release waits in process, the lock held, instead of going
(`close_out`). An admission nothing armed (every shared admission, an exclusive control, an outage run whose provider
never went down) writes nothing and is let go at once.

**Where the times come from.** The sandbox (`DockerSandbox.session_ends`) records the moment its gate's stop was
confirmed and the moment its mount's sidecar was shown gone, and the sandbox platform (`SandboxPlatform.launch`) reports
each to the drain of the admission the run holds, as the launch ends. It begins its sandbox's kinds
(`SessionDrain.begin`) when the sandbox is built, so an end that was never reported is known to be missing.

**The deadline.** The drain is one deadline, `until`, not a duration, so whatever the restore took before it (a heal's
settle included) counts, and only the remainder is waited:

- every end known: `until = (the later end) + drain_s`; basis `session_end`;
- an end that is missing, a sandbox that reported none, or one whose stop was cut, failed or left the mount live:
  unknown, and unknown is never zero. `until = (the restore's start) + drain_s`, and never earlier than any known end
  plus `drain_s`; basis `restore_start`. A restore that never began reads as the moment the plan is made;
- sessions owed to an earlier run (`owe`: a recovery restored a provider that a run left down): unknown whatever the
  current run's sandbox says, so `until` is never earlier than the recovery's start plus `drain_s`; basis
  `restore_start`;
- a sandbox with no mount and no gate (a run with no data path): no session, no wait; basis `no_session`;
- `session_drain_s = 0`: no drain, declared; basis `off`.

**Order, in a run.** The gate's stop is in the sandbox's collection, the injector's restore (`clear`) follows, and the
mount's end is at the launch's exit, after it; the run's admission ends after that, and so after the post-run check. The
restore therefore cannot wait for a mount that is still connected: it records its plan with the mount unknown
(`deferred`), and the run's admission publishes the drain when it ends, with every end known. An injector that holds the
federation by an admission of its own (a run that holds none) has no sandbox of its run, so it waits in `clear`.

**Not covered.** A process killed before its admission is let go publishes nothing, and the lock goes with the kernel:
the next start finds the injection settled, or recovers it, in which case the recovery owes the full drain from its own
start. A crashed run's orphaned sandbox keeps its mount connected until the next sandbox start reaps it, which is after
the drain such a recovery owes.
"""

import dataclasses
import logging
import math
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the journal imports nothing of this module: the import is for the annotation only
    from expcore_onedata.fault_journal import InjectionJournal

log = logging.getLogger(__name__)

MOUNT, GATE = "mount", "gate"  # the kinds of session a sandbox's data path can end: its mount, its REST gate
OFF, SESSION_END, RESTORE_START, NO_SESSION = "off", "session_end", "restore_start", "no_session"
_KINDS = {MOUNT: "the mount's end", GATE: "the gate's stop"}


@dataclasses.dataclass(frozen=True)
class DrainPlan:
    """When the drain ends and on what it rests: `until` on the drain's clock (None: nothing to wait for), `basis` (one
    of `off`, `session_end`, `restore_start`, `no_session`), why in words, and the times used (None: not recorded)."""

    until: float | None
    basis: str
    why: str
    drain_s: float
    mount_end: float | None
    gate_stop: float | None
    restore_started: float | None

    def record(self) -> dict[str, Any]:
        """The plan as a plain mapping, its times on the drain's clock."""
        return dataclasses.asdict(self)


class SessionTicket:
    """One sandbox's sessions: it ends each kind once, and a repeat of an end is ignored."""

    def __init__(self, drain: "SessionDrain", kinds: tuple[str, ...]) -> None:
        self._drain, self._open = drain, set(kinds)
        self._lock = threading.Lock()

    def ended(self, kind: str, at: float | None = None) -> None:
        """Note that this sandbox's `kind` of session ended at `at` (now, on the drain's clock, by default)."""
        with self._lock:
            if kind not in self._open:
                return
            self._open.discard(kind)
        self._drain._ended(kind, self._drain.clock() if at is None else at)


class SessionDrain:
    """The drain of one exclusive admission (see the module docstring); safe to use from several threads.

    `clock` and `sleep` are the drain's own and are replaced by `arm` and `owe`: the injector arms it with its own, so a
    fake clock of a test is the one that decides, wherever the admission was made. `journal` and `wall` are where the
    drain is published (`publish`): with no journal a release can only wait in process.
    """

    def __init__(
        self,
        drain_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        journal: "InjectionJournal | None" = None,
        wall: Callable[[], float] = time.time,
    ) -> None:
        number = isinstance(drain_s, int | float) and not isinstance(drain_s, bool)
        if not number or not math.isfinite(drain_s) or drain_s < 0:
            raise ValueError("the drain must be a finite number of seconds, 0 or more")
        self.drain_s, self.clock, self.sleep = float(drain_s), clock, sleep
        self._journal, self._wall = journal, wall
        self._lock = threading.Lock()
        self._armed = False
        self._restore_started: float | None = None
        self._owed: float | None = None  # the start of a recovery whose run's sessions are unknown (`owe`)
        self._tickets = 0
        self._pending: dict[str, int] = {}  # the sessions begun and not yet ended, by kind
        self._ends: dict[str, float] = {}  # the latest end of each kind
        self.last: dict[str, Any] | None = None  # the record of the latest wait or publication

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed

    def arm(
        self,
        restore_started: float | None = None,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        """An outage was restored: from now this admission is let go only into a drain. `restore_started` is when the
        restore began, on `clock`; with none the wait counts from when the plan is made. A later arming moves the start
        later."""
        with self._lock:
            self._set(clock, sleep)
            self._armed = True
            if restore_started is not None:
                self._restore_started = (
                    restore_started if self._restore_started is None else max(self._restore_started, restore_started)
                )

    def disarm(self) -> None:
        """Take back an arming made before a restore that then found nothing to restore (the provider never went down).
        Nothing owed to an earlier run is ever taken back."""
        with self._lock:
            if self._owed is None:
                self._armed = False
                self._restore_started = None

    def owe(
        self,
        since: float,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        """A recovery restored a provider that an earlier run left down, from `since` on `clock`: that run's sessions
        are unknown, whatever this admission's own sandbox says of its own, so the drain is never over before `since`
        plus `drain_s`. The sandbox's reports cannot remove what is owed, and a second recovery owes its own drain on
        top of the first's: the latest start counts."""
        with self._lock:
            self._set(clock, sleep)
            self._armed = True
            self._owed = since if self._owed is None else max(self._owed, since)

    def _set(self, clock: Callable[[], float] | None, sleep: Callable[[float], None] | None) -> None:
        if clock is not None:
            self.clock = clock
        if sleep is not None:
            self.sleep = sleep

    def begin(self, *kinds: str) -> SessionTicket:
        """A sandbox starts sessions of these kinds (none, for one with no data path); whoever ends them says so on the
        ticket. Until it has, those ends are unknown."""
        unknown = [kind for kind in kinds if kind not in _KINDS]
        if unknown:
            raise ValueError(f"not a kind of session: {', '.join(unknown)}")
        distinct = tuple(dict.fromkeys(kinds))
        with self._lock:
            self._tickets += 1
            for kind in distinct:
                self._pending[kind] = self._pending.get(kind, 0) + 1
        return SessionTicket(self, distinct)

    def _ended(self, kind: str, at: float) -> None:
        with self._lock:
            self._pending[kind] = max(0, self._pending.get(kind, 0) - 1)
            self._ends[kind] = max(self._ends.get(kind, at), at)

    def plan(self, now: float | None = None) -> DrainPlan:
        """When the drain ends, from what is known now (see the module docstring). `now` is the moment a restore that
        never began is taken to have started: the clock's, by default."""
        with self._lock:
            ends, pending, tickets = dict(self._ends), dict(self._pending), self._tickets
            started, owed = self._restore_started, self._owed
        mount, gate = ends.get(MOUNT), ends.get(GATE)

        def plan(until: float | None, basis: str, why: str) -> DrainPlan:
            return DrainPlan(until, basis, why, self.drain_s, mount, gate, started)

        if self.drain_s == 0:
            return plan(None, OFF, "session_drain_s is 0: no drain was asked for")
        unknown = [f"{_KINDS[kind]} was not recorded" for kind in (MOUNT, GATE) if pending.get(kind, 0) > 0]
        if tickets == 0:
            unknown.append("no sandbox reported its sessions")
        if owed is not None:
            unknown.append("the sessions of the run a recovery restored are unknown")
        if not unknown and not ends:
            return plan(None, NO_SESSION, "the run's sandbox had no mount and no REST gate")
        candidates = [end + self.drain_s for end in ends.values()]
        if unknown:
            if started is not None:
                candidates.append(started + self.drain_s)
            elif owed is None:  # a restore that never began: taken to begin when the plan is made
                candidates.append((self.clock() if now is None else now) + self.drain_s)
            if owed is not None:
                candidates.append(owed + self.drain_s)
            why = "; ".join(unknown) + ": the drain runs in full from the restore's start"
            return plan(max(candidates), RESTORE_START, why)
        why = "the drain runs from the later of the mount's end and the gate's stop"
        return plan(max(candidates), SESSION_END, why)

    def wait(self) -> dict[str, Any]:
        """Wait until the drain has ended, if armed, and return its record: the plan's mapping and `waited_s`, the
        seconds waited on the drain's clock. It returns only once the clock has reached `until`, planned afresh before
        each sleep so that an end or a debt noted meanwhile counts: a sleep that comes back early is slept again, and
        the one exception is a clock that did not move across a sleep, which no wait can outlast (a fake whose sleep
        does nothing). Only the sleep's own interrupt escapes it. Calling it again is harmless."""
        started = self.clock()
        plan = self.plan(started)
        if not self.armed:
            return {**plan.record(), "armed": False, "waited_s": 0.0}
        last: float | None = None
        while plan.until is not None:
            now = self.clock()
            left = plan.until - now
            if left <= 0 or (last is not None and now <= last):
                break
            last = now
            log.info("the federation is held %.0f s more, until the run's sessions have drained (%s)", left, plan.basis)
            self.sleep(left)
            plan = self.plan(started)
        record = {**plan.record(), "armed": True, "waited_s": max(0.0, self.clock() - started)}
        self.last = record
        return record

    def publish(self) -> dict[str, Any]:
        """Write the drain's end to the federation's drain record, so that no admission is granted before it, and return
        the plan's record with `published`, the wall-clock time written (None when nothing needed writing: not armed, or
        nothing to wait for). Raises what the journal raises when it cannot write."""
        plan = self.plan()
        record: dict[str, Any] = {**plan.record(), "armed": self.armed, "waited_s": 0.0, "published": None}
        if not record["armed"] or plan.until is None or self._journal is None:
            return record
        # the monotonic reading first: a pause before the wall clock's makes the deadline later, never earlier
        now = self.clock()
        until_wall = self._wall() + (plan.until - now)
        self._journal.raise_drain(until_wall)
        record["published"] = until_wall
        self.last = record
        return record

    def close_out(self) -> dict[str, Any]:
        """What an admission does when it is let go: publish the drain, or, when it cannot be published (no journal, or
        the journal cannot be written), wait it out in process so that the lock is never let go inside it."""
        if self._journal is None:
            return self.wait()
        try:
            return self.publish()
        except (OSError, ValueError) as error:  # the record cannot be written, or an existing one cannot be read
            log.error("the drain could not be recorded (%s): the admission waits it out", type(error).__name__)
            return self.wait()
