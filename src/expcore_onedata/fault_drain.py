# Provenance: new
"""The session drain of an outage run: its exclusive admission is let go only after its sessions have ended (O11).

**Why.** During an outage an agent's read of a file only the stopped provider holds, through the local provider, leaves
an on-demand sync job on that provider's per-file synchronizer, and nothing calls it back. A later read of the same file
and range, from any session, joins the job and hangs. The job is cancelled only when the run's callers end: a REST read
when its HTTP request process dies (seconds after the client closes the socket, at most about 5 min after), a mount
(oneclient) at the file's release or about 300 s after oneclient's last connection closes (op-worker's
`fuse_session_grace_period_seconds`), plus about 15 s if the removal retries. Eviction, a transfer cancel, deleting the
file and `restart_link` cancel nothing; only a container restart does. (Source-read of op-worker f118ed9, not measured.)

**What.** `SessionDrain` is carried by an exclusive `Admission` (`expcore_onedata.admission`) and is armed by the
fault injector once a provider it took down is verified restored (`FaultInjector.clear`, and the recovery of a crashed
run's injection). Once armed, the admission's lock is let go only at the `until` of its plan: `[faults] session_drain_s`
(320 s by default) after the later of the run's mount disconnecting and its REST gate stopping. The wait is on the
admission's last release (`Admission._release`), so every way the federation is let go (the campaign's run admission,
the injector's own, the health gate's recovery) waits, the lock held and the holder record in place, so the health check
reads it as an outage run in progress and defers. An admission nothing armed (every shared admission, an exclusive
control, an outage run whose provider never went down) is let go at once.

**Where the times come from.** The sandbox (`DockerSandbox.session_ends`) records the moment its gate's stop was
confirmed and the moment its mount's sidecar was shown gone, and the sandbox platform (`SandboxPlatform.launch`)
reports each to the drain of the admission the run holds, as the launch ends. It begins its sandbox's kinds
(`SessionDrain.begin`) when the sandbox is built, so an end that was never reported is known to be missing.

**The deadline.** The drain is one deadline, `until`, not a duration, so whatever the restore took before it (a heal's
settle included) counts, and only the remainder is waited:

- every end known: `until = (the later end) + drain_s`; basis `session_end`;
- an end that is missing, a sandbox that reported none, or one whose stop was cut, failed or left the mount live:
  unknown, and unknown is never zero. `until = (the restore's start) + drain_s`, and never earlier than any known end
  plus `drain_s`; basis `restore_start`. A restore that never began reads as the moment the wait starts;
- a sandbox with no mount and no gate (a run with no data path): no session, no wait; basis `no_session`;
- `session_drain_s = 0`: no drain, declared; basis `off`.

**Order, in a run.** The gate's stop is in the sandbox's collection, the injector's restore (`clear`) follows, and the
mount's end is at the launch's exit, after it; the run's admission ends after that, and so after the post-run check. The
restore therefore cannot wait for a mount that is still connected: it records its plan with the mount unknown
(`deferred`) and the run's admission waits at its own end, with every end known. An injector that holds the federation
by an admission of its own (a run that holds none) has no sandbox of its run, so it waits in `clear`, before it lets go.

**Not covered.** A process killed between the restore and the admission's release drops the lock with the kernel, and no
wait follows it. A harness that kills its sandbox without `launch`'s stop reports no end, and the drain is then the
full wait from the restore's start.
"""

import dataclasses
import logging
import math
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

MOUNT, GATE = "mount", "gate"  # the kinds of session a sandbox's data path can end: its mount, its REST gate
OFF, SESSION_END, RESTORE_START, NO_SESSION = "off", "session_end", "restore_start", "no_session"
_KINDS = {MOUNT: "the mount's end", GATE: "the gate's stop"}
_MAX_PASSES = 3  # a sleep that returns before its time is slept again, at most this often


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

    `clock` and `sleep` are the drain's own and are replaced by `arm`: the injector arms it with its own, so a fake
    clock of a test is the one that decides, wherever the admission was made.
    """

    def __init__(
        self,
        drain_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        number = isinstance(drain_s, int | float) and not isinstance(drain_s, bool)
        if not number or not math.isfinite(drain_s) or drain_s < 0:
            raise ValueError("the drain must be a finite number of seconds, 0 or more")
        self.drain_s, self.clock, self.sleep = float(drain_s), clock, sleep
        self._lock = threading.Lock()
        self._armed = False
        self._restore_started: float | None = None
        self._tickets = 0
        self._pending: dict[str, int] = {}  # the sessions begun and not yet ended, by kind
        self._ends: dict[str, float] = {}  # the latest end of each kind
        self.last: dict[str, Any] | None = None  # the record of the latest wait

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
        """An outage was restored: from now this admission is let go only after the drain. `restore_started` is when
        the restore began, on `clock`; with none the wait counts from when it starts. A later arming moves the start
        later."""
        with self._lock:
            if clock is not None:
                self.clock = clock
            if sleep is not None:
                self.sleep = sleep
            self._armed = True
            if restore_started is not None:
                self._restore_started = restore_started

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

    def plan(self) -> DrainPlan:
        """When the drain ends, from what is known now (see the module docstring)."""
        with self._lock:
            ends, pending, tickets = dict(self._ends), dict(self._pending), self._tickets
            started = self._restore_started
        mount, gate = ends.get(MOUNT), ends.get(GATE)

        def plan(until: float | None, basis: str, why: str) -> DrainPlan:
            return DrainPlan(until, basis, why, self.drain_s, mount, gate, started)

        if self.drain_s == 0:
            return plan(None, OFF, "session_drain_s is 0: no drain was asked for")
        unknown = [f"{_KINDS[kind]} was not recorded" for kind in (MOUNT, GATE) if pending.get(kind, 0) > 0]
        if tickets == 0:
            unknown.append("no sandbox reported its sessions")
        if not unknown and not ends:
            return plan(None, NO_SESSION, "the run's sandbox had no mount and no REST gate")
        candidates = [end + self.drain_s for end in ends.values()]
        if unknown:
            candidates.append((self.clock() if started is None else started) + self.drain_s)
            why = "; ".join(unknown) + ": the drain runs in full from the restore's start"
            return plan(max(candidates), RESTORE_START, why)
        why = "the drain runs from the later of the mount's end and the gate's stop"
        return plan(max(candidates), SESSION_END, why)

    def wait(self) -> dict[str, Any]:
        """Wait until the drain has ended, if armed, and return its record: the plan's mapping and `waited_s`, the
        seconds waited on the drain's clock. Only the sleep's own interrupt escapes it, and then the lock is let go by
        whoever called (there is nothing else to hold it for). Calling it again is harmless."""
        started = self.clock()
        plan = self.plan()
        if not self.armed:
            return {**plan.record(), "armed": False, "waited_s": 0.0}
        for _ in range(_MAX_PASSES):
            left = None if plan.until is None else plan.until - self.clock()
            if left is None or left <= 0:
                break
            log.info("the federation is held %.0f s more, until the run's sessions have drained (%s)", left, plan.basis)
            self.sleep(left)
        record = {**plan.record(), "armed": True, "waited_s": max(0.0, self.clock() - started)}
        self.last = record
        return record
