# Provenance: new
"""The fault half of the platform's standard health check (O11).

`FaultGate` is the fault half of the platform's standard health check, and runs around the rest of it. Whenever the
journal holds an open injection and the federation can be taken exclusively (no run is on it), it recovers every
injection whose holder is gone, verifying each restore, before it considers the journal: so the first check of a
campaign settles what a crashed run left, and a later one settles an injection whose injector was dropped. Recovery
restores and so needs only the configuration's half of the sanction. It then takes the federation shared, as a run's
admission does, waiting at most `health_wait_s`, and holds it for the whole check, so no injection starts while the
check runs: under the shared lock it fails for an injection that is still open (recovery failed, or its lock is stuck)
and for a journal it cannot read, naming targets by their labels only, and otherwise answers as the rest of the check
(`inner`: the pre-campaign checks and the space). Keep `health_wait_s` below the campaign's `health_timeout_s`, or
the check counts as down before its wait can run out.

**A wait that runs out is told apart** by the holder record the exclusive holder keeps beside the lock
(`expcore_onedata.admission`). When the check's wait runs out it reads the journal and the record. An injection whose
restore failed (journalled `clear_failed`) fails the check, naming the target by its label, whoever still holds the
federation. A live holder, or none recorded, is contention, an outage run in progress, which is no fault of the
federation: the check answers deferred (`Health(..., deferred=True)`), which the core's gate treats as a wait that
charges no run. A recorded holder that is gone while the lock stays held is
stuck: the check recovers, verified, whatever the journal holds open, marks the record `stuck_since` (the first such
verdict's time) and fails, saying the lock is stuck. The record is never deleted for it, so every later timeout is stuck
too; only an acquisition of the lock, which proves it usable, clears it.

**It heals stuck pairs its own link check found (O35, `fault_gate_heal`).** Given the rest of the check's latest link
report (`links`) and the providers' services (`labels`), as `PlatformHealth` gives them, a check that holds no run's
admission and whose own all-pairs link check, in this call, found a stuck pair heals it with `[faults] heal`: it lets
its shared hold go, takes the federation exclusively (`RECOVERY`), waiting at most `health_wait_s` for every shared
holder and failing closed when one does not let go, runs the restore's heal with no provider restored, and answers
healthy only when its own check, run again under that admission, passes. A heal that left a pair failing keeps the
check failing, and none runs again until the gate's link check passes. A heal that runs past the campaign's
`health_timeout_s` reads as down in the core's gate, which then stays closed until a check after the heal passes.
"""

import contextlib
import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from expcore.campaign.healthgate import Health, HealthCheck, read_health
from expcore.errors import TARGET_LABEL, InfraError
from expcore_onedata.admission import (
    EXCLUSIVE_RUN,
    INJECTOR,
    OUTAGE_RUN,
    RECOVERY,
    FederationBusy,
    FederationLock,
    current_admission,
    require_same_federation,
)
from expcore_onedata.fault_gate_heal import GateHeal, GateHeals
from expcore_onedata.fault_journal import holder_alive
from expcore_onedata.fault_matrix import LinkMatrix
from expcore_onedata.fault_recovery import FaultDriver, Recovery
from expcore_onedata.fault_settings import FaultSettings

_PURPOSES = {
    OUTAGE_RUN: "an outage run",
    EXCLUSIVE_RUN: "an exclusive run",
    INJECTOR: "an injector",
    RECOVERY: "the recovery of the health check",
}
_STAMP = re.compile(r"[0-9T:+.-]{10,40}")
_WORD = re.compile(r"[A-Za-z_]{1,40}")


def _named(record: Mapping[str, Any] | None) -> str:
    """The holder a record names, in words: its purpose, the target's label and since when; never anything else."""
    if record is None:
        return "a run"
    who = _PURPOSES.get(str(record.get("purpose")), "a holder")
    target = record.get("target")
    since = record.get("since_utc")
    of = f" of {target}" if isinstance(target, str) and TARGET_LABEL.fullmatch(target) else ""
    at = f" since {since}" if isinstance(since, str) and _STAMP.fullmatch(since) else ""
    return f"{who}{of}{at}"


def _unsettled(entries: list[dict[str, Any]]) -> Health:
    """The failure of a journal with open injections: a failed restore by its target's label and how it failed."""
    failed = sorted({(entry["target"], entry["clear_failed"]) for entry in entries if "clear_failed" in entry})
    if failed:
        hows = ", ".join(f"{target} ({how if _WORD.fullmatch(how) else 'failed'})" for target, how in failed)
        return Health(False, f"the restore of {hows} failed: the federation is not in its baseline state")
    targets = ", ".join(sorted({entry["target"] for entry in entries}))
    return Health(False, f"an injection of {targets} is unsettled: the federation is not in its baseline state")


class FaultGate:
    """The fault half of the platform's standard health check, around the rest of it (see the module docstring)."""

    def __init__(
        self,
        settings: FaultSettings,
        driver: FaultDriver,
        inner: HealthCheck | None = None,
        *,
        labels: Mapping[str, str] | None = None,
        links: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
    ) -> None:
        """`links()` is the latest link report of `inner` (a `linkcheck.LinkReport` or a `LinkMatrix`; None before
        one), and `labels` maps each provider's label to its service: with both, a declared and sanctioned heal, the
        gate heals the stuck pairs its own check finds (see the module docstring)."""
        self._clock, self._sleep, self._drain_s = clock, sleep, settings.session_drain_s
        self._lock = FederationLock(settings, clock=clock, sleep=sleep, wall=wall)
        self._recovery = Recovery(settings, driver, clock=clock, sleep=sleep, labels=labels, wall=wall)
        self._journal = self._recovery.journal
        self._may_recover = settings.sanctioned is True
        self._wait_s = settings.health_wait_s
        self._inner = inner
        self._links = links
        self._heals = GateHeals(settings.journal)
        heals = links is not None and bool(labels) and settings.heal != "off" and self._may_recover
        self._healer = (
            GateHeal(settings, driver, self._recovery, labels or {}, self._lock.hold_for, self._heals)
            if heals
            else None
        )
        self._last_heal: dict[str, Any] | None = None

    @property
    def last_heal(self) -> dict[str, Any] | None:
        """The record of the latest heal this gate ran (`fault_gate_heal`), None before one."""
        return self._last_heal

    def __call__(self) -> Health:
        try:
            held = current_admission()
            if held is not None:  # a run's own post-run check, under its admission: no injection but its own can start,
                # and nothing is healed, since the run's tokens may still be in play
                require_same_federation(held, self._lock.federation)
                return self._judged()[0]
            if self._recovery.entries() and self._may_recover:
                exclusive = self._lock.try_acquire(exclusive=True, purpose=RECOVERY)
                if exclusive is not None:  # no run is on the federation: settle what is left
                    try:
                        if self._drain_s > 0 and exclusive.drain is not None:
                            # before anything is restored: a provider it brings back went down in a run it knows nothing
                            # of, and the release below publishes the drain whatever interrupts the recovery
                            exclusive.drain.owe(self._clock(), clock=self._clock, sleep=self._sleep)
                        self._recovery.recover()
                    finally:
                        exclusive.close()
            try:
                shared = self._lock.acquire(exclusive=False, timeout_s=self._wait_s)
            except FederationBusy:
                return self._busy()
            try:  # held shared for the whole check: no injection starts until it has answered
                verdict, stuck = self._judged()
            finally:
                shared.close()
            return verdict if stuck is None else self._heal(verdict, stuck)
        except InfraError as error:
            return Health(False, error.detail)

    def _checked(self) -> Health:
        """With the federation held: the journal, then the rest of the check."""
        entries = self._recovery.entries()
        if entries:
            return _unsettled(entries)
        return read_health(self._inner) if self._inner is not None else Health(True, "no injection is unsettled")

    def _report(self) -> Any:
        return None if self._links is None else self._links()

    def _judged(self) -> tuple[Health, LinkMatrix | None]:
        """With the federation held: the check (`_checked`), and the matrix of the link check it ran in this call when
        that found a stuck pair and the check failed; else None. A link check of this call that passed lets a later
        stuck pair be healed again (`GateHeals.passed`), while the federation is still held."""
        before = self._report()
        verdict = self._checked()
        report = self._report()
        if report is None or report is before:  # no link check ran in this call: nothing it found to act on
            return verdict, None
        matrix = report if isinstance(report, LinkMatrix) else LinkMatrix.from_report(report)
        if matrix.ok:
            with contextlib.suppress(OSError, ValueError):  # the latch then stays, and no heal runs: closed
                self._heals.passed()
            return verdict, None
        stuck = not verdict.ok and any(pair.stuck for pair in matrix.pairs)
        return verdict, matrix if stuck else None

    def _heal(self, verdict: Health, trigger: LinkMatrix) -> Health:
        """The check's own link check found a stuck pair, and the check holds no run's admission: heal it under an
        exclusive admission of the gate's own, then check again under it; fail closed whenever it cannot."""
        if self._healer is None:  # no heal declared, or no sanction, services or link report to heal by
            return verdict
        try:
            latched = self._heals.latched()
        except (OSError, ValueError) as error:
            why = f"the record of the fault gate's heals cannot be read ({type(error).__name__}), so no heal runs"
            return Health(False, f"{verdict.detail}; {why}")
        if latched:
            why = (
                "the fault gate has healed once since its link check last passed, and no heal runs again until a "
                "link check passes"
            )
            return Health(False, f"{verdict.detail}; {why}")
        try:
            exclusive = self._lock.acquire(exclusive=True, timeout_s=self._wait_s, purpose=RECOVERY)
        except FederationBusy:
            why = (
                f"the fault gate's heal did not run: the federation was not free of runs, tools and drains within "
                f"health_wait_s ({self._wait_s:g} s), so the gate stays closed"
            )
            return Health(False, f"{verdict.detail}; {why}")
        try:
            entries = self._recovery.entries()
            if entries:  # an injection opened since the check: its restore owns the links
                return _unsettled(entries)
            result, said = self._healer.run(trigger)
            if result is None:
                return Health(False, f"{verdict.detail}; {said}")
            self._last_heal = result.record
            if not result.healed:
                return Health(False, f"{verdict.detail}; {said}")
            # the gate opens only on its own check, run again under the exclusive admission, its link check passing
            again, _ = self._judged()
            latest = self._report()
            if again.ok and (latest is None or latest.ok is not True):
                return Health(False, f"{again.detail}; {said}; the gate's link check did not pass again after the heal")
            return again._replace(detail=f"{again.detail}; {said}")
        finally:
            exclusive.close()

    def _busy(self) -> Health:
        """The wait for the federation ran out: tell a failed restore, a stuck holder and an outage run on it apart."""
        entries = self._recovery.entries()
        if any("clear_failed" in entry for entry in entries):
            return _unsettled(entries)  # a restore failed: a fault of the federation, whoever still holds it
        draining = self._lock.drain_left()
        if draining > 0:  # nobody holds it: it is closed while an outage run's sessions drain (or a heal's settle
            # runs out), and that charges no run
            return Health(
                False,
                f"the federation is closed for {draining:.0f} s more while an outage run's sessions drain or a heal "
                "settles: the check waits for it",
                deferred=True,
            )
        record = self._journal.read_holder()
        if record is None or holder_alive(record) is not False:  # contention: a wait, which charges no run
            return Health(False, f"{_named(record)} holds the federation: the check waits for it", deferred=True)
        since = datetime.now(UTC).isoformat(timespec="seconds")
        marked = self._journal.mark_holder_stuck(record.get("token"), since) or record
        stuck = marked.get("stuck_since")
        at = f" since {stuck}" if isinstance(stuck, str) and _STAMP.fullmatch(stuck) else ""
        done = "its record stays until an acquisition of the lock proves it usable"
        if entries:  # it left something open: bring it back, verified, before anything else
            if not self._may_recover:
                return Health(
                    False, f"the federation's lock is stuck{at}: {_named(record)} is gone and left an injection open"
                )
            # the holder may have gone, and the lock with it, since the wait ran out: then the recovery runs under it
            exclusive = self._lock.try_acquire(exclusive=True, purpose=RECOVERY)
            try:
                if exclusive is not None and exclusive.drain is not None:
                    exclusive.drain.owe(self._clock(), clock=self._clock, sleep=self._sleep)
                elif exclusive is None and self._drain_s > 0:
                    # still held, by a holder that is gone: nobody else is admitted while it is, and the record is
                    # written first, so an admission that finds the journal settled after it finds the record
                    self._lock.hold_for(self._drain_s)
                self._recovery.recover()  # raises InfraError naming the target when one cannot be settled
            finally:
                if exclusive is not None:
                    exclusive.close()
            done = f"what it left open is recovered, and the federation closed for its drain; {done}"
        return Health(
            False, f"the federation's lock is stuck{at}: {_named(record)} is gone, yet the lock stays held; {done}"
        )
