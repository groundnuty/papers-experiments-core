# Provenance: new
"""A shared admission's share of `federation.reset`, the lock a shortcut reset takes exclusively (O11, O51).

Every shared admission of a federation with `[faults]` (a campaign run's, through `admission.run_admission`, and a
tool's outside a campaign, through `tool_admission.shared_admission`) holds `federation.reset` shared beside its share
of `federation.lock`, for as long as it holds the admission. A reset that skips the per-file reads of files a planner's
reading showed empty (`resetgrant.py`) needs to know that no reader can place bytes while it runs, and an on-demand
read leaves no transfer record to find: so it takes `federation.reset` exclusively, which no shared admission can hold
alongside. This module is that share, as one admission holds it: `ResetShare`.

**Its states.** `shared` (its share held), `given_up` (let go for the admission's own shortcut reset: the run's agent
has not started, so it is safe), `exclusive` (the lock taken exclusively, on a descriptor of its own) and `closed` (the
admission let go). A flock is never converted in place: `LOCK_SH` to `LOCK_EX` on one descriptor is not atomic on Linux
(the kernel drops the old lock before it waits for the new one, so a waiter may slip in between, and a non-blocking
conversion that fails has lost the lock it had). Every change goes through `given_up`: the descriptor of the old mode is
closed, and a new one is opened for the next. The owner's share of `federation.lock` is never touched here, so it never
lapses: an outage run, which needs that lock exclusively, stays out for the whole reset.

**Who held it.** Each hold of the share writes a line to `reset-holds.jsonl` beside the fault journal, in the format of
the tools' record (`shared_holds.SharedHolds`, `name=RESET_HOLDS`): the process as the lease pool names a holder (boot
id, pid and start time), whether it is a campaign attempt, and when it began; and a second line when it is let go. A
shortcut reset whose wait runs out names the live holders from it, and the gone ones as `holder_gone`. A hold that
cannot be recorded is refused (`injector_failure`): the record is what tells a reset that a holder died, and a hold
nobody can see is not one. An end that cannot be recorded is told of and leaves the line open; the next exclusive
holder ends it.

**Descriptors.** Every lock file is opened with `O_CLOEXEC` (`admission.FederationLock._try`), so an exec'd child (the
tool server, a sandbox command) never inherits one, and a held lock is a `fault_journal.HeldLock`, which the fork hook
closes in a forked child: a lock never outlives the process that took it.
"""

import fcntl
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from expcore.campaign import liveness
from expcore_onedata.fault_journal import HeldLock
from expcore_onedata.fault_recovery import failure
from expcore_onedata.shared_holds import SharedHolds

log = logging.getLogger(__name__)

FEDERATION_RESET = "federation.reset"  # beside federation.lock: shared by every shared admission, exclusive for a reset
RESET_HOLDS = "reset-holds.jsonl"  # who holds federation.reset shared, in the format of shared-holds.jsonl
SHARED, GIVEN_UP, EXCLUSIVE, CLOSED = "shared", "given_up", "exclusive", "closed"
IN_CAMPAIGN, OUTSIDE_CAMPAIGN = "a campaign attempt", "outside a campaign"  # the `reason` of a hold's line

TryTake = Callable[[int], HeldLock | None]  # take federation.reset in a mode (LOCK_SH, LOCK_EX) at once, or None
Busy = Callable[[str], Exception]  # the error of a bounded wait that ran out, from its detail


class ResetShare:
    """One shared admission's share of `federation.reset` (see the module docstring). Its methods are safe across
    threads: the owner's reset changes its state while a worker may let the admission go (`close`)."""

    def __init__(
        self,
        directory: Path,
        held: HeldLock,
        *,
        try_take: TryTake,
        busy: Busy,
        retake_s: float | None,
        poll_s: float,
    ) -> None:
        """Record the share just taken (`held`); raise InfraError of kind `injector_failure`, the share not closed, when
        it cannot be recorded. `retake_s` bounds the wait to take the share back after a reset (None: no bound)."""
        self._holds = SharedHolds(Path(directory), name=RESET_HOLDS)
        self._try_take, self._busy, self._retake_s, self._poll_s = try_take, busy, retake_s, poll_s
        self._lock = threading.Lock()
        self._held: HeldLock | None = held
        self._state = SHARED
        self._epoch = 0  # counts the exclusive holds, so a grant names the one it took (`exclusive_epoch`)
        self._token: str | None = self._begin()

    @property
    def state(self) -> str:
        return self._state

    @property
    def shared(self) -> bool:
        """Whether the share is held: the admission may act on the federation."""
        return self._state == SHARED and self._held is not None and self._held.held

    def exclusive_epoch(self) -> int | None:
        """The number of the exclusive hold in force, None when the lock is not held exclusively now."""
        with self._lock:
            live = self._state == EXCLUSIVE and self._held is not None and self._held.held
            return self._epoch if live else None

    def holders(self) -> SharedHolds:
        """The record of every shared admission's hold of `federation.reset` in this federation."""
        return self._holds

    def _begin(self) -> str:
        reason = IN_CAMPAIGN if liveness.current() is not None else OUTSIDE_CAMPAIGN
        try:
            return self._holds.begin(reason)
        except OSError as error:  # its text holds the journal's path: only its type is told (O12)
            raise failure(f"the share of the reset lock could not be recorded ({type(error).__name__})") from None

    def _end(self, token: str | None) -> None:
        if token is None:
            return
        try:
            self._holds.end(token)
        except OSError:  # the line stays open; the next exclusive holder, which proves it unheld, ends it
            log.warning("the end of a share of the reset lock could not be recorded", exc_info=True)

    def give_up(self) -> None:
        """Let the share go for the owner's own shortcut reset (`shared` to `given_up`). Raise RuntimeError unless it is
        held: a reset gives up only a share it has."""
        with self._lock:
            if self._state != SHARED or self._held is None:
                raise RuntimeError(f"a share of the reset lock in state {self._state!r} cannot be given up")
            held, token = self._held, self._token
            self._held, self._token, self._state = None, None, GIVEN_UP
        try:
            held.close()
        finally:
            self._end(token)

    def try_exclusive(self) -> bool:
        """Take the lock exclusively, at once, on a descriptor of its own (`given_up` to `exclusive`); False when a
        shared admission holds it, or the admission has let go."""
        with self._lock:
            if self._state != GIVEN_UP:
                return False
            held = self._try_take(fcntl.LOCK_EX)
            if held is None:
                return False
            self._held, self._state = held, EXCLUSIVE
            self._epoch += 1
            return True

    def end_exclusive(self) -> None:
        """Let the exclusive hold go (`exclusive` to `given_up`); nothing when it is not held so."""
        with self._lock:
            if self._state != EXCLUSIVE:
                return
            held, self._held, self._state = self._held, None, GIVEN_UP
        if held is not None:
            held.close()

    def take_shared(self) -> None:
        """Take the share back after a reset (`given_up` to `shared`), on a new descriptor, waiting while another reset
        holds the lock exclusively, within `retake_s`; record the hold. Raise the `busy` error when the wait runs out,
        the share still given up, so the run's agent cannot start (`admission.require_admission` refuses). A share the
        admission let go meanwhile takes nothing."""
        deadline = None if self._retake_s is None else time.monotonic() + self._retake_s
        while True:
            with self._lock:
                if self._state == CLOSED:
                    return
                if self._state != GIVEN_UP:
                    raise RuntimeError(f"a share of the reset lock in state {self._state!r} cannot be taken back")
                held = self._try_take(fcntl.LOCK_SH)
                if held is not None:
                    self._held, self._state = held, SHARED
                    self._token = self._begin()
                    return
            if deadline is not None and time.monotonic() >= deadline:
                raise self._busy(
                    "the share of the reset lock could not be taken back after a shortcut reset: another reset held "
                    "it exclusively past the bound of the wait"
                )
            time.sleep(self._poll_s)

    def close(self, *, record: bool) -> None:
        """Let go whatever the share holds, for good (`closed`), and end its record when `record` (a forked child's
        copy, whose descriptor the fork hook closed, records nothing of its parent's)."""
        with self._lock:
            held, token = self._held, self._token
            self._held, self._token, self._state = None, None, CLOSED
        try:
            if held is not None:
                held.close()
        finally:
            if record:
                self._end(token)


__all__ = [
    "CLOSED",
    "EXCLUSIVE",
    "FEDERATION_RESET",
    "GIVEN_UP",
    "IN_CAMPAIGN",
    "OUTSIDE_CAMPAIGN",
    "RESET_HOLDS",
    "SHARED",
    "ResetShare",
]
