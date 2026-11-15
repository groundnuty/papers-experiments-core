# Provenance: new
"""Admission to a federation with a fault injector: outage runs exclusive with every other run (O11).

A provider an outage takes down is down for every run on the federation, not only for the run that took it down. So when
platform.toml has `[faults]`, every run of the platform holds the federation's reader-writer lock, a `flock` on
`federation.lock` beside the fault journal, from before its sandbox and tool server start until after its post-run
check: shared for a run of a baseline condition, exclusive for a run whose condition uses an injector (its injection and
its clear included). The campaign takes it through the platform's run admission (`run_admission`, the study plugin's
`admission`), around the whole attempt, so no outage run can take the federation between a run's teardown and the
post-run check that judges it. An outage run never also takes it shared, so there is no upgrade and no deadlock: the
injector refuses to start under a shared admission, and takes the exclusive one itself when its run holds none. The tool
server and the sandbox (`SandboxPlatform.launch`) refuse to start without an admission (`require_admission`).

Waiting for the lock is admission, before the agent's loop starts, so it is outside the agent's time and charges it
nothing, as a lease taken before the run is. Taken inside a tool call instead, it is a `lease_queue` wait (`waits=`).
The wait is a poll of a non-blocking lock with a bound (`admission_timeout_s`), which a cancellation stops; one that
runs out while another run holds the federation (an outage run's heal, say), or while it stays closed for an outage
run's session drain (below), is `FederationBusy`, the hold `federation_busy`, which costs the waiting run no attempt.
Under a shared admission the journal must show no unsettled injection, checked while the lock is held, or the run is
refused as `injector_failure`. A held admission is a `HeldLock`: closed by its finalizer when its owner is dropped, and
closed in a forked child. An admission names the federation it holds (`Federation`: the lock file's resolved path and
the digest of platform.toml), and `require_admission` and the injector refuse one held for another federation, so a
run of one platform is never admitted by a lock of another.

`flock` is not fair, so a steady stream of shared holders could keep an outage run out for ever. A turnstile prevents
that: every admission, shared or exclusive, first takes `federation.intent` exclusively, then the main lock, and only
then lets the turnstile go. An outage run waiting for the main lock holds the turnstile, so runs that arrive after it
queue behind it, and it gets in once the runs that were on the federation at its arrival have finished.

**The session drain (`expcore_onedata.fault_drain`).** An exclusive admission carries a `SessionDrain`. The fault
injector arms it before it restores a provider it took down, and from then the federation admits nobody before
`[faults] session_drain_s` has passed since the later of the run's mount disconnecting and its REST gate stopping (the
sandbox reports both to it): a read of a file only the stopped provider holds leaves an on-demand sync job that a later
read of the same file would join and hang on. The last release of an armed admission (`Admission._release`) writes that
time to the journal's drain record (`federation.drain`, the wall clock, only ever later), and every admission checks the
record once it holds the lock: `acquire` waits for it, inside its bound, and `try_acquire` refuses. The drain is kept by
the federation, not by the process that began it, so it covers every way the federation is let go (the run's admission
at the end of its attempt, the injector's own, the health gate's recovery, a restore that holds no admission, an
interrupt), and a run that waits for it waits before its agent's clock starts, like any wait for the federation. A
shared admission, an exclusive run with no fault and an outage run whose provider never went down are armed by nobody
and write nothing. Admission is also what the next run's own attempt meets: any run, outage or baseline, is admitted
only after the record has passed.

The fault half of the platform's health check, which reads the holder record this module writes, is
`expcore_onedata.fault_gate.FaultGate`. Whoever takes the federation exclusively (`OUTAGE_RUN`, `INJECTOR` or the
gate's `RECOVERY`) first writes a holder record beside the lock, whole and fsynced (`fault_journal.HOLDER`): its pid and
boot id, its purpose, the target's label and the time it took the lock; it removes the record before it lets the lock
go, and a shared admission clears any record left behind, since no exclusive holder can then exist.

**The run's token steps (O35).** An admission also counts the steps of its run that use the run's tokens and have
begun but not ended (`token_step`, `Admission.open_steps`): the tool server's launch (`TOOL_SERVER_STEP`), up to the
end of its stop's re-verification of the plane, which examines its token, and the sandbox's (`SANDBOX_STEP`), from its
mint up to the end of its stop. Both launches note their own, and each worker thread of theirs that uses a token
(the tool server's checks, the sandbox's mint and start) is counted until the thread itself has ended, which a
cancellation of the launch does not hasten. The fault injector reads them at the start of a restore
or of its start's recovery, either of which may heal for up to `heal_timeout_s`: a step still running then could find
its token expired after the heal (`expcore_onedata.faults`, "The token's window"). A copy of the context (a worker
thread, a task) sees the same admission, and so the same count.

A tool outside a campaign (a smoke driver, a warm-up) holds the shared admission too, through
`expcore_onedata.tool_admission.shared_admission`, so no outage can overlap it. Whoever takes the lock exclusively
knows that no such tool holds it, and ends the record of any a dead one left open (`expcore_onedata.shared_holds`); a
wait for it that runs out says that tools outside a campaign hold the federation. An admission's `endings` are what
runs, last first, when the last share lets the lock go.

**The reset lock (`federation.reset`, `resetlock.py`, `resetgrant.py`).** Every shared admission (a campaign run's, a
tool's outside a campaign, the health check's) also holds `federation.reset` shared, recorded in `reset-holds.jsonl`,
for as long as it holds `federation.lock`. A shortcut reset (O51), which skips the block maps of files a planner's
reading showed empty and so needs no reader to place bytes during it, takes `federation.reset` exclusively: its run
gives up its own share of `federation.reset` (its agent has not started) and keeps its share of `federation.lock`, polls
for the exclusive hold within `[placement] reset_exclusive_wait_s`, and afterwards lets it go and takes its share back
within `admission_timeout_s`, each on a new descriptor: a flock is never converted in place (`LOCK_SH` to `LOCK_EX` on
one descriptor drops the old lock before it waits, and a failed non-blocking conversion has lost it). An exclusive
admission of `federation.lock` takes no reset lock: it already excludes every shared holder, every resetter included.

**The lock order, which every holder keeps:** `federation.intent`, then `federation.lock`, then (after the turnstile is
let go) `federation.reset`. Who waits for what, holding what:

- a shared admission waits for `federation.intent` holding nothing, for `federation.lock` (shared) holding the
  turnstile, and for `federation.reset` (shared) holding `federation.lock`;
- an exclusive one (an outage run, the injector, an `exclusive = true` run, the health gate's `RECOVERY`) waits for the
  turnstile holding nothing and for `federation.lock` (exclusive) holding the turnstile, and takes nothing more;
- a shortcut reset waits for `federation.reset` (exclusive) holding `federation.lock` shared and no share of
  `federation.reset`, and, its exclusive hold let go, for its share back holding the same.

No holder of `federation.reset` waits for `federation.lock` or the turnstile, and no holder of `federation.lock` waits
for the turnstile, so no wait closes a cycle. Two resetters cannot deadlock: each gives up its share before it waits, so
neither holds what the other waits for, one is granted, and the other's wait is bounded (past it, the full reads). The
windows the shortcut must not have: **an outage run never gets in during it**, since the resetting run's share of
`federation.lock`, which the exclusive admission conflicts with, is never let go (so it never lapses either); **no other
run's agent, no tool and no health check acts during it**, since each acts only under its share of `federation.reset`,
which conflicts with the exclusive hold; and nothing of the resetting run's own does, since the section seals its
admission (`seal`: granted only while no worker holds a share, and no worker takes one until it ends) and refuses its
infrastructure (`require_admission`, `require_reset_share`, `may_act`); a health check that meets the hold, or runs
in the resetting run's own context, is deferred, uncharged.

**No lock outlives its holder.** The kernel drops a dead process's flocks. Every lock file is opened `O_CLOEXEC`, so an
exec'd child never inherits one, and every held lock is a `HeldLock`, which the fork hook closes in a forked child
(`fault_journal`); no `subprocess` call of the package passes descriptors on (`pass_fds`, `close_fds=False`). The tool
server never holds a lock: the attempt holds it for it. The MCP client starts the server in a session of its own
(`start_new_session`), so it survives its parent's death; `PR_SET_PDEATHSIG` is not set, since the client gives no hook
to set it, it would reach only the direct child (a configured launcher such as `uv run` leaves the server a
grandchild), and it would fire with the forking thread rather than the process. Instead a shortcut reset that finds a
holder line whose process is gone takes no shortcut (`resetgrant.HOLDER_GONE`), and neither does any for
`resetgrant.DEAD_HOLDER_QUIET_S` after: a read it sent may still be placing bytes, and a server it started may still
run.
"""

import asyncio
import contextlib
import fcntl
import logging
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from expcore.campaign.attempt import RunAdmission
from expcore.campaign.factors import EXCLUSIVE_KEY, FactorSchema
from expcore.campaign.healthgate import DeferralBound
from expcore.config.loading import ConfigError, Section, load_toml
from expcore.errors import InfraError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.fault_deferral import heal_deferral_bound
from expcore_onedata.fault_drain import SessionDrain
from expcore_onedata.fault_journal import HeldLock, InjectionJournal, holder, make_directories
from expcore_onedata.fault_recovery import failure
from expcore_onedata.fault_settings import FaultSettings, load_faults
from expcore_onedata.resetlock import FEDERATION_RESET, ResetShare
from expcore_onedata.sandbox.kinds import FEDERATION_BUSY, PROVIDER_OUTAGE
from expcore_onedata.shared_holds import SharedHolds

log = logging.getLogger(__name__)

FEDERATION_LOCK = "federation.lock"
FEDERATION_INTENT = "federation.intent"  # the turnstile every admission passes before the main lock
OUTAGE_RUN, INJECTOR, RECOVERY = "outage_run", "injector", "recovery"  # why the federation is held exclusively
EXCLUSIVE_RUN = "exclusive_run"  # a run whose condition declares `exclusive = true`: a matched control, no fault
TOOL_SERVER_STEP, SANDBOX_STEP = "tool_server", "sandbox"  # the run's steps that use its tokens (`token_step`)
_ADMISSION: ContextVar["Admission | None"] = ContextVar("expcore_onedata_admission", default=None)
# locks kept for the life of the process: a drain that could be neither recorded nor waited out (`_release`)
_STRANDED: list[HeldLock] = []


class AdmissionRefused(ConfigError):
    """A run of a platform with a fault injector would start, or inject, without the admission its condition needs."""


class FederationBusy(InfraError):
    """The bounded wait for the federation ran out while another held it: the hold `federation_busy`, never charged."""

    def __init__(self, detail: str) -> None:
        super().__init__(FEDERATION_BUSY, detail)


@dataclass(frozen=True)
class Federation:
    """Which federation an admission is for: its lock file's resolved path and the digest of its platform.toml. Two
    configurations that share a lock file but differ are two federations, and so are two with one file but two locks."""

    lock_file: str
    config_sha256: str


def federation_of(settings: FaultSettings) -> Federation:
    """The identity of the federation `[faults]` describes."""
    return Federation(str((settings.journal / FEDERATION_LOCK).resolve()), settings.config_sha256)


class _Shares:
    """Who still needs an admission: its owner, and every worker handed a share that may outlive the owner's wait."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.count, self.owner_left = 1, False
        self.sealed = False  # a shortcut reset's section: no worker may take a share, and nothing of the run may act


class _Steps:
    """The run's steps that use its tokens and have begun, by name, each with how many have not ended yet."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running: dict[str, int] = {}


@dataclass(frozen=True)
class Admission:
    """The federation's lock as one run holds it, shared or exclusive, and which federation it is; held exclusively,
    the holder record it wrote beside the lock (`record`), which it removes before it lets the lock go.

    The owner's `close` gives up its share; the lock goes when the last share is given up, so a worker the owner
    handed a share (`retain`) keeps the federation held until it ends, even after the owner has left. `endings` are
    what to call, last first, just before the lock goes: a tool outside a campaign ends its record of the hold there
    (`expcore_onedata.tool_admission`), so the record ends with the lock, never before a worker that still holds it."""

    exclusive: bool
    lock: HeldLock
    federation: Federation
    journal: InjectionJournal | None = None
    record: Mapping[str, Any] | None = None
    shares: _Shares = field(default_factory=_Shares, compare=False, repr=False)
    drain: SessionDrain | None = field(default=None, compare=False, repr=False)  # exclusive only (`fault_drain`)
    steps: _Steps = field(default_factory=_Steps, compare=False, repr=False)
    endings: list[Callable[[], None]] = field(default_factory=list, compare=False, repr=False)
    reset: ResetShare | None = field(default=None, compare=False, repr=False)  # shared only: its federation.reset

    @property
    def held(self) -> bool:
        return self.lock.held

    def begin_step(self, name: str) -> Callable[[], None]:
        """Count a step of the run that uses its tokens (`TOOL_SERVER_STEP`, `SANDBOX_STEP`) as begun; return the call
        that counts it ended (only the first call counts)."""
        with self.steps.lock:
            self.steps.running[name] = self.steps.running.get(name, 0) + 1
        ended = [False]  # under the steps' lock: one end per begin, however often the call is made

        def end() -> None:
            with self.steps.lock:
                if not ended[0]:
                    ended[0] = True
                    self.steps.running[name] -= 1

        return end

    def open_steps(self) -> tuple[str, ...]:
        """The names of the run's steps that use its tokens and have begun but not ended, sorted."""
        with self.steps.lock:
            return tuple(sorted(name for name, count in self.steps.running.items() if count > 0))

    @property
    def may_act(self) -> bool:
        """Whether the holder may act on the federation: held, not sealed for its own shortcut reset (`seal`), and,
        shared, with its share of the reset lock held, not given up for that reset (`resetlock.py`)."""
        return self.held and not self.shares.sealed and (self.reset is None or self.reset.shared)

    def seal(self) -> bool:
        """For a shortcut reset's section (`resetgrant.py`): refuse every worker a share from now on, and return True,
        when the admission is held and its one share is its owner's (the owner has not left, and no worker holds a
        share); else False, sealing nothing. A worker that holds a share, the last one after the owner left included,
        may be reading, which the section must rule out."""
        with self.shares.lock:
            if not self.held or self.shares.owner_left or self.shares.count != 1 or self.shares.sealed:
                return False
            self.shares.sealed = True
            return True

    def unseal(self) -> None:
        """End the section's seal: workers may take shares again."""
        with self.shares.lock:
            self.shares.sealed = False

    def try_retain(self) -> Callable[[], None] | None:
        """Take one more share of the admission for a worker; return the call that gives it up (only the first call
        counts, in any thread), or None when there is no share to take: the admission is no longer held, or its last
        share is already given up and its release is under way, or a shortcut reset's section has sealed it (`seal`),
        so it cannot be kept for the worker (a tool reusing it refuses then)."""
        with self.shares.lock:
            if not self.held or self.shares.count == 0 or self.shares.sealed:
                return None
            self.shares.count += 1
        once = threading.Lock()  # never released: the first non-blocking acquire, in any thread, is the one that counts

        def give_up() -> None:
            if once.acquire(blocking=False):
                self._give_up()

        return give_up

    def retain(self) -> Callable[[], None]:
        """`try_retain`, with nothing to give up when there was no share to take."""
        return self.try_retain() or (lambda: None)

    def _give_up(self) -> None:
        with self.shares.lock:
            self.shares.count -= 1
            last = self.shares.count == 0
        if last:
            self._release()

    def mark(self, target: str) -> None:
        """Name in the holder record the target this exclusive admission takes down."""
        if self.journal is not None and self.record is not None and self.held:
            with contextlib.suppress(OSError):  # the record only names the holder: the lock is what holds
                self.journal.write_holder({**self.record, "target": target})

    def close(self) -> None:
        """Give up the owner's share; closing again is harmless. The last share lets the lock go (`_release`)."""
        with self.shares.lock:
            if self.shares.owner_left:
                return
            self.shares.owner_left = True
        self._give_up()

    def _release(self) -> None:
        """Let the locks go, the records first, so no record outlives a lock let go this way. An armed drain (exclusive
        only) is written to the federation's drain record first (`fault_drain`), so that no admission is granted inside
        it; when it cannot be written the drain is waited out here, the lock held. When that fails too (an interrupt of
        the wait, or anything else) the lock is not let go: it is kept for as long as this process lives (`_STRANDED`),
        with its holder record, and the failure is raised. The federation then stays closed until the process ends,
        which is the kernel's to settle, instead of opening inside the drain. Past the drain, the endings and the holder
        record go first, and the locks go whatever they do, an interrupt included: the reset lock (shared only), in
        whatever mode it is held, then the main lock, the reverse of the order they were taken in. A forked child's copy
        of the admission holds nothing (the fork hook closed its descriptors), so it ends and clears nothing of its
        parent's."""
        try:
            if self.drain is not None and self.held:
                self.drain.close_out()
        except BaseException:
            _STRANDED.append(self.lock)  # a held lock is closed by its finalizer once nothing refers to it
            raise
        owned = self.held
        try:
            if owned:
                for ending in reversed(self.endings):
                    try:
                        ending()
                    except Exception:  # the record of a hold only says who held it
                        log.exception("the end of a hold could not be recorded")
                if self.journal is not None and self.record is not None:
                    with contextlib.suppress(OSError):  # a record left behind is stale, and the next holder clears it
                        self.journal.clear_holder(self.record["token"])
        finally:
            try:
                if self.reset is not None:
                    self.reset.close(record=owned)
            finally:
                self.lock.close()


async def _close_off_the_loop(admission: Admission) -> None:
    """Give up the owner's share of an admission; one whose drain is armed lets go in a worker thread, shielded, so the
    event loop is never blocked by its record (or by the wait that stands in for it) and a cancellation leaves the lock
    held until the thread has ended."""
    if admission.drain is not None and admission.drain.armed:
        await asyncio.shield(asyncio.to_thread(admission.close))
    else:
        admission.close()


def current_admission() -> Admission | None:
    """The admission the running context holds, if it holds one that is still held."""
    admission = _ADMISSION.get()
    return admission if admission is not None and admission.held else None


def retain_admission() -> Callable[[], None]:
    """A share of the running context's admission for a worker that may outlive it (`Admission.retain`); a call that
    gives up nothing when the context holds none."""
    admission = current_admission()
    return admission.retain() if admission is not None else (lambda: None)


def begin_token_step(name: str) -> Callable[[], None]:
    """Count a step of the run that uses its tokens on the running context's admission (`Admission.begin_step`), and
    return the call that ends the count, once; nothing is counted without an admission, as on a platform without
    `[faults]`, which has no injector to read it. A worker thread a cancellation cannot stop takes a count of its own
    before it is dispatched and ends it as it ends, so the step outlives a launch that left without it."""
    admission = current_admission()
    return admission.begin_step(name) if admission is not None else (lambda: None)


@contextlib.contextmanager
def token_step(name: str) -> Iterator[None]:
    """For the body of a `with`, a step of the run that uses its tokens, counted from its start to its end however the
    body ends (`begin_token_step`)."""
    end = begin_token_step(name)
    try:
        yield
    finally:
        end()


def retain_to_act() -> Callable[[], None]:
    """A share of the running context's admission for work that acts on the federation and must not start without it
    (a sandbox's start, from its admission check until its start thread holds a share of its own): `retain_admission`,
    but `AdmissionRefused`, keeping no share, when the context holds an admission of which no share can be taken (it is
    being given up, or the run's shortcut section has sealed it) or which may not act (`require_reset_share`). Held,
    the share keeps any shortcut section of the run from beginning (`Admission.seal`). A call that gives up nothing
    when the context holds no admission."""
    admission = current_admission()
    if admission is None:
        return lambda: None
    share = admission.try_retain()
    if share is None:
        raise AdmissionRefused(
            "the run's admission is being given up, or its own shortcut reset holds the federation: nothing may start "
            "under it until the reset has ended"
        )
    try:
        require_reset_share(admission)
    except BaseException:
        share()
        raise
    return share


class FederationLock:
    """The reader-writer lock of one federation, on `federation.lock` in the fault journal's directory of `[faults]`;
    each admission it grants names that federation (`Federation`)."""

    def __init__(
        self,
        settings: FaultSettings,
        *,
        poll_s: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
    ) -> None:
        """`clock` and `sleep` are those of the session drain of the exclusive admissions it grants (the injector's own
        replace them when it arms the drain), and `wall` is the clock the drain record and its check are on, which every
        process shares; the waits for the lock itself always run on real time."""
        self.directory, self._poll_s = Path(settings.journal), poll_s
        self.federation = federation_of(settings)
        self._drain_s, self._clock, self._sleep, self._wall = settings.session_drain_s, clock, sleep, wall
        self._journal = InjectionJournal(self.directory)
        self._retake_s = settings.admission_timeout_s  # the bound of a shared admission's wait for its reset share back

    def drain_left(self) -> float:
        """The seconds the federation still admits nobody for, on the wall clock (0 when it is open): the journal's
        drain record, which an outage run's release writes. A record that cannot be read fails as InfraError of kind
        `injector_failure`, since an admission cannot tell it is safe to grant (O12: the type of an OS error only)."""
        try:
            until = self._journal.read_drain()
        except ValueError as error:
            raise failure(f"the federation's drain record cannot be read: {error}") from None
        except OSError as error:
            raise failure(f"the federation's drain record cannot be read ({type(error).__name__})") from None
        return 0.0 if until is None else max(0.0, until - self._wall())

    def hold_for(self, seconds: float) -> None:
        """Close the federation to every admission for `seconds` more, durably and never earlier than it is closed
        already: what a restore that holds no admission leaves, so that it is not let go inside the drain. A record that
        cannot be written fails as InfraError of kind `injector_failure`."""
        if seconds <= 0:
            return
        try:
            self._journal.raise_drain(self._wall() + seconds)
        except OSError as error:  # its text holds the journal's path: only its type is told (O12)
            raise failure(f"the federation's drain could not be recorded ({type(error).__name__})") from None

    def _await_drain(self, release: Callable[[], None], deadline: float | None, cancel: threading.Event | None) -> None:
        """With the lock held: wait until the drain record has passed, polling, inside the same bound and under the
        same cancellation as the wait for the lock; call `release` and raise when it cannot be waited out."""
        try:
            while True:
                left = self.drain_left()
                if left <= 0:
                    return
                if cancel is not None and cancel.is_set():
                    raise failure("the wait for the federation was cancelled")
                if deadline is not None and time.monotonic() >= deadline:
                    # the hold `federation_busy`, as a wait for the lock that runs out: the run did nothing (O11)
                    raise FederationBusy(
                        "the federation stayed closed past the bound of the wait: an outage run's sessions drain"
                    )
                pause = min(left, 0.5)
                time.sleep(pause if deadline is None else min(pause, max(0.0, deadline - time.monotonic())))
        except BaseException:
            release()
            raise

    def _try(self, name: str, mode: int) -> HeldLock | None:
        """Take one of the lock files in `mode` without waiting; None when another holds it in a conflicting one. The
        file is opened close-on-exec, so no exec'd child (the tool server, a sandbox command) inherits the lock, and
        the descriptor is a `HeldLock`, which the fork hook closes in a forked child."""
        make_directories(self.directory)
        descriptor = os.open(self.directory / name, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        except BaseException:
            os.close(descriptor)
            raise
        return HeldLock(descriptor)

    def _wait(self, name: str, mode: int, deadline: float | None, cancel: threading.Event | None) -> HeldLock:
        """Poll for one of the lock files until the deadline or the cancellation."""
        while True:
            held = self._try(name, mode)
            if held is not None:
                return held
            if cancel is not None and cancel.is_set():
                raise failure("the wait for the federation was cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                raise FederationBusy("the federation stayed held past the bound of the wait")
            time.sleep(self._poll_s)

    def _admit(
        self, main: HeldLock, exclusive: bool, purpose: str, target: str | None, reset: HeldLock | None = None
    ) -> Admission:
        """The admission of a lock just taken. Exclusive, it writes its holder record (or lets the lock go and raises
        InfraError of kind `injector_failure`); shared, it clears any record, which no holder can then own, and records
        its share of the reset lock (`reset`), or lets both locks go and raises when that cannot be recorded."""
        journal = self._journal
        if not exclusive:
            with contextlib.suppress(OSError):  # a stale record only misleads a diagnosis, which reads its holder
                journal.clear_holder(None)
            if reset is None:
                main.close()
                raise RuntimeError("a shared admission holds the reset lock shared")
            try:
                share = self._share(reset)
            except BaseException:
                try:
                    reset.close()
                finally:
                    main.close()
                raise
            return Admission(False, main, self.federation, reset=share)
        record = {"token": uuid.uuid4().hex, **holder(), "purpose": purpose, "target": target}
        record["since_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            journal.write_holder(record)
        except OSError as error:  # its text holds the journal's path: only its type is told (O12)
            main.close()
            raise failure(f"the federation's holder record could not be written ({type(error).__name__})") from None
        except BaseException:
            main.close()
            raise
        self._settle_tools()
        drain = SessionDrain(self._drain_s, clock=self._clock, sleep=self._sleep, journal=journal, wall=self._wall)
        return Admission(True, main, self.federation, journal, record, drain=drain)

    def _share(self, reset: HeldLock) -> ResetShare:
        """A shared admission's share of the reset lock just taken, recorded (`resetlock.ResetShare`)."""
        return ResetShare(
            self.directory,
            reset,
            try_take=lambda mode: self._try(FEDERATION_RESET, mode),
            busy=FederationBusy,
            retake_s=self._retake_s,
            poll_s=self._poll_s,
        )

    def _settle_tools(self) -> None:
        """Under the exclusive lock, no tool outside a campaign holds the federation shared: end the record of any that
        a dead one left open (`SharedHolds.settle`). It only says who held, so a record that cannot be read or written
        is told of and never keeps an outage run out."""
        try:
            SharedHolds(self.directory).settle(proven=True)
        except (OSError, ValueError):
            log.warning("the record of the tools' holds could not be settled", exc_info=True)

    def try_acquire(self, *, exclusive: bool, purpose: str = OUTAGE_RUN, target: str | None = None) -> Admission | None:
        """Pass the turnstile and take the lock without waiting; None when either is held against it, or while an outage
        run's sessions are draining (`drain_left`). `purpose` and `target` (a label) go into the holder record of an
        exclusive admission."""
        intent = self._try(FEDERATION_INTENT, fcntl.LOCK_EX)
        if intent is None:
            return None
        try:
            main = self._try(FEDERATION_LOCK, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        finally:
            intent.close()
        if main is None:
            return None
        try:
            if self.drain_left() > 0:  # an outage run's sessions are still draining: nobody is admitted yet
                main.close()
                return None
        except BaseException:
            main.close()
            raise
        reset = None
        if not exclusive:  # after the main lock and outside the turnstile: the order every holder keeps
            try:
                reset = self._try(FEDERATION_RESET, fcntl.LOCK_SH)
            except BaseException:
                main.close()
                raise
            if reset is None:  # a shortcut reset holds it exclusively
                main.close()
                return None
        return self._admit(main, exclusive, purpose, target, reset)

    def acquire(
        self,
        *,
        exclusive: bool,
        timeout_s: float | None = None,
        cancel: threading.Event | None = None,
        purpose: str = OUTAGE_RUN,
        target: str | None = None,
    ) -> Admission:
        """Pass the turnstile, then wait for the lock while holding it, polling, at most `timeout_s` in all and until
        `cancel` is set, and then, holding the lock, for the drain record to pass (`drain_left`), inside the same bound;
        raise `FederationBusy` (the hold `federation_busy`) when either wait ran out, and InfraError of kind
        `injector_failure` when it was cancelled or the holder record failed. `purpose` and `target` go into the holder
        record of an exclusive admission. Shared, it then takes its share of the reset lock within the same bound
        (`resetlock.py`): every shared holder does, the health check's too, whose first call reads a file of a study's
        space (`precampaign.content_type_check`), so a shortcut reset defers it, uncharged."""
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        try:
            intent = self._wait(FEDERATION_INTENT, fcntl.LOCK_EX, deadline, cancel)
            try:
                main = self._wait(FEDERATION_LOCK, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH, deadline, cancel)
            finally:
                intent.close()  # whoever waits behind it may now queue for the main lock
        except FederationBusy as busy:
            if exclusive:
                raise self._explained(busy) from None
            raise
        self._await_drain(main.close, deadline, cancel)
        if exclusive:
            return self._admit(main, exclusive, purpose, target)
        try:  # after the main lock and outside the turnstile: the order every holder keeps
            reset = self._wait(FEDERATION_RESET, fcntl.LOCK_SH, deadline, cancel)
        except BaseException:
            main.close()
            raise
        return self._admit(main, exclusive, purpose, target, reset)

    def _explained(self, busy: FederationBusy) -> FederationBusy:
        """An exclusive wait that ran out, with the tools outside a campaign that hold the federation shared named,
        when any do (their reasons are their own text and stay in the record)."""
        try:
            tools = SharedHolds(self.directory).waiting_on()
        except (OSError, ValueError):
            return busy  # the record only explains the wait
        return FederationBusy(f"{busy.detail}; {tools}") if tools else busy

    @contextlib.contextmanager
    def held(
        self,
        *,
        exclusive: bool,
        timeout_s: float | None = None,
        target: str | None = None,
        cancel: threading.Event | None = None,
        purpose: str = OUTAGE_RUN,
    ) -> Iterator[Admission]:
        """Hold the lock for the body of a `with`, as the running context's admission (the synchronous form)."""
        if current_admission() is not None:
            raise AdmissionRefused("this run already holds the federation's admission")
        admission = self.acquire(
            exclusive=exclusive, timeout_s=timeout_s, target=target, cancel=cancel, purpose=purpose
        )
        token = _ADMISSION.set(admission)
        try:
            if not exclusive or purpose == EXCLUSIVE_RUN:  # an outage run settles the journal itself, under its lock
                self._require_settled()
            # the record is read again after the journal was: a restore that cleared an injection without this lock (the
            # health gate's, of a stuck holder's) wrote its record first, so an admission that finds the journal
            # settled finds the record too, whatever it read before
            self._await_drain(lambda: None, None if timeout_s is None else time.monotonic() + timeout_s, cancel)
            yield admission
        finally:
            _ADMISSION.reset(token)
            admission.close()

    @contextlib.asynccontextmanager
    async def admitted(
        self, *, exclusive: bool, waits: Any = None, timeout_s: float | None = None, target: str | None = None
    ) -> AsyncIterator[Admission]:
        """Hold the lock for the body of an `async with`, as the running task's admission.

        The wait runs off the event loop and stops on cancellation; with `waits` (a timer or a call context) it is
        inside `waits.infrastructure_wait("lease_queue")`. Shared, the journal must hold no unsettled injection.
        """
        if current_admission() is not None:
            raise AdmissionRefused("this run already holds the federation's admission")
        cancel = threading.Event()
        waiting = asyncio.ensure_future(
            asyncio.to_thread(self.acquire, exclusive=exclusive, timeout_s=timeout_s, cancel=cancel, target=target)
        )
        try:
            if waits is None:
                admission = await asyncio.shield(waiting)
            else:
                async with waits.infrastructure_wait("lease_queue"):
                    admission = await asyncio.shield(waiting)
        except asyncio.CancelledError:
            cancel.set()  # the thread stops polling; an admission it took all the same is let go at once
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({waiting}, timeout=10 * self._poll_s + 1)
            if waiting.done() and not waiting.cancelled() and waiting.exception() is None:
                waiting.result().close()
            raise
        token = _ADMISSION.set(admission)
        try:
            if not exclusive:
                self._require_settled()
            deadline = None if timeout_s is None else time.monotonic() + timeout_s
            await asyncio.to_thread(self._await_drain, lambda: None, deadline, cancel)  # as `held`: after the journal
            yield admission
        finally:
            _ADMISSION.reset(token)
            await _close_off_the_loop(admission)

    def require_open(
        self, release: Callable[[], None], timeout_s: float | None, cancel: threading.Event | None = None
    ) -> None:
        """What every admission checks once it holds the lock, for a use of one already held (a tool reusing its
        context's, `expcore_onedata.tool_admission`): the journal holds no unsettled injection, and the drain record
        has passed, waited for at most `timeout_s` (`FederationBusy`, the hold `federation_busy`, when it runs out).
        `release` is called when either refuses."""
        try:
            self._require_settled()
        except BaseException:
            release()
            raise
        self._await_drain(release, None if timeout_s is None else time.monotonic() + timeout_s, cancel)

    def _require_settled(self) -> None:
        """Under the lock: refuse the run while the journal holds an unsettled injection, or cannot be read."""
        try:
            entries = InjectionJournal(self.directory).open_entries()
        except (OSError, ValueError) as error:
            raise failure(f"the injection journal cannot be read ({type(error).__name__})") from None
        if entries:
            targets = ", ".join(sorted({entry["target"] for entry in entries}))
            raise failure(f"an injection of {targets} is unsettled, so no run may start on the federation")


def fault_settings_of(config: PlatformConfig) -> FaultSettings | None:
    """`[faults]` of the configuration's platform.toml, validated; None when it has no such table."""
    if "faults" not in load_toml(config.file):
        return None
    return load_faults(config.file, config)


def require_same_federation(admission: Admission, federation: Federation) -> None:
    """Refuse an admission held for another federation than `federation`; the message names neither (O12)."""
    if admission.federation != federation:
        raise AdmissionRefused(
            "the admission held is for another federation (another lock file or another platform.toml) than the one "
            "this run acts on"
        )


class RunAdmit:
    """The campaign's run admission of a platform with `[faults]`: a callable the core enters around every attempt
    (`RunAdmission`), which also declares the longest the platform's health check may stay deferred, read from the same
    `[faults]` settings with no health object between (`declared_deferral_s`). The core compares it with the health
    check it is given and refuses the campaign when that check does not carry it (`fault_deferral`). A study that
    wraps the admission in a callable of its own passes `declared_deferral_s` on too, or only the health check's own
    declaration is read."""

    def __init__(self, admit: RunAdmission, settings: FaultSettings) -> None:
        self._admit, self._settings = admit, settings

    def __call__(
        self,
        treatment_kinds: tuple[str, ...],
        treatment_targets: Mapping[str, str],
        cancel: threading.Event,
        exclusive: bool,
    ) -> contextlib.AbstractContextManager[str]:
        return self._admit(treatment_kinds, treatment_targets, cancel, exclusive)

    def declared_deferral_s(self) -> DeferralBound | None:
        """The platform's declared longest deferral of its health check, as `PlatformHealth.max_deferral_s` answers it
        (`fault_deferral.heal_deferral_bound`); None with `heal = "off"`."""
        return heal_deferral_bound(self._settings)


def run_admission(config: PlatformConfig, schema: FactorSchema | None = None) -> RunAdmission | None:
    """The campaign's run admission for a platform with `[faults]` (`StudyPlugin.admission`), None without one. It
    also declares the platform's longest deferral (`RunAdmit.declared_deferral_s`), which the core holds the health
    check passed to the scheduler to.

    The core enters it before an attempt starts and leaves it after the attempt's post-run check: a run whose condition
    declares `provider_outage` holds the federation exclusively, with its target's label in the holder record, and so
    does a run whose condition declares `exclusive = true` (a matched control, named an exclusive run in the record);
    every other run shares it. It yields what it holds, `shared` or `exclusive`, from the admission it took. The wait is
    bounded by `admission_timeout_s`, ends with the campaign, and runs before the agent's clock starts; one that fails
    leaves the run unstarted.

    Given the study's `schema`, a condition that declares `exclusive = true` on a platform without `[faults]` is
    refused, naming the study file and the key: there is no federation lock to hold.
    """
    settings = fault_settings_of(config)
    if settings is None:
        exclusive = [name for name, value in (schema.exclusive if schema is not None else {}).items() if value]
        if schema is not None and exclusive:
            raise Section({}, schema.file, f"[conditions.{exclusive[0]}]").fail(
                EXCLUSIVE_KEY, f"needs a federation lock to hold, and {config.file} has no [faults] table"
            )
        return None
    lock = FederationLock(settings)

    @contextlib.contextmanager
    def admit(
        treatment_kinds: tuple[str, ...], treatment_targets: Mapping[str, str], cancel: threading.Event, exclusive: bool
    ) -> Iterator[str]:
        outage = PROVIDER_OUTAGE in treatment_kinds
        target = treatment_targets.get(PROVIDER_OUTAGE) if outage else None
        if outage and target is None:
            raise AdmissionRefused(
                f"a run whose condition declares {PROVIDER_OUTAGE!r} with no target is not admitted: the outage "
                "names the provider it takes down, and the federation is held for that provider's label"
            )
        purpose = OUTAGE_RUN if outage else EXCLUSIVE_RUN
        timeout_s = settings.admission_timeout_s
        with lock.held(
            exclusive=outage or exclusive, timeout_s=timeout_s, target=target, cancel=cancel, purpose=purpose
        ) as admission:
            yield "exclusive" if admission.exclusive else "shared"  # what it took, never what was asked

    return RunAdmit(admit, settings)


def require_admission(config: PlatformConfig) -> None:
    """Refuse to start a run's infrastructure on a platform with `[faults]` unless the run holds that federation."""
    settings = fault_settings_of(config)
    if settings is None:
        return
    admission = current_admission()
    if admission is None:
        raise AdmissionRefused(
            f"{config.file}: [faults]: a run's infrastructure starts only under the federation's admission "
            "(FederationLock.admitted, shared for a baseline run, exclusive for an outage run)"
        )
    require_same_federation(admission, federation_of(settings))
    require_reset_share(admission)


def require_reset_share(admission: Admission) -> None:
    """Refuse to act under an admission its own shortcut reset holds (`Admission.may_act`): sealed for the reset's
    section, or, shared, with its share of the reset lock given up for it or not taken back after it (`resetlock.py`).
    Nothing of the run may then reach the federation, since the reset counts on no reader placing bytes."""
    if not admission.may_act:
        raise AdmissionRefused(
            "the run's own shortcut reset holds the federation (its share of the reset lock is given up, or was not "
            "taken back after it): nothing of the run may act on the federation until the reset has ended"
        )
