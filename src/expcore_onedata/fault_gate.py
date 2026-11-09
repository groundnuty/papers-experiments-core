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
charges no run and leaves a finished run's outcome standing. A recorded holder that is gone while the lock stays held is
stuck: the check recovers, verified, whatever the journal holds open (or, with nothing open, only clears the record) and
fails, saying the lock is stuck.
"""

import re
from collections.abc import Mapping
from typing import Any

from expcore.campaign.healthgate import Health, HealthCheck, read_health
from expcore.errors import TARGET_LABEL, InfraError
from expcore_onedata.admission import INJECTOR, OUTAGE_RUN, RECOVERY, FederationBusy, FederationLock
from expcore_onedata.fault_journal import holder_alive
from expcore_onedata.fault_recovery import FaultDriver, Recovery
from expcore_onedata.fault_settings import FaultSettings

_PURPOSES = {OUTAGE_RUN: "an outage run", INJECTOR: "an injector", RECOVERY: "the recovery of the health check"}
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

    def __init__(self, settings: FaultSettings, driver: FaultDriver, inner: HealthCheck | None = None) -> None:
        self._lock = FederationLock(settings)
        self._recovery = Recovery(settings, driver)
        self._journal = self._recovery.journal
        self._may_recover = settings.sanctioned is True
        self._wait_s = settings.health_wait_s
        self._inner = inner

    def __call__(self) -> Health:
        try:
            if self._recovery.entries() and self._may_recover:
                exclusive = self._lock.try_acquire(exclusive=True, purpose=RECOVERY)
                if exclusive is not None:  # no run is on the federation: settle what is left
                    try:
                        self._recovery.recover()
                    finally:
                        exclusive.close()
            try:
                shared = self._lock.acquire(exclusive=False, timeout_s=self._wait_s)
            except FederationBusy:
                return self._busy()
            try:  # held shared for the whole check: no injection starts until it has answered
                entries = self._recovery.entries()
                if entries:
                    return _unsettled(entries)
                return (
                    read_health(self._inner) if self._inner is not None else Health(True, "no injection is unsettled")
                )
            finally:
                shared.close()
        except InfraError as error:
            return Health(False, error.detail)

    def _busy(self) -> Health:
        """The wait for the federation ran out: tell a failed restore, a stuck holder and an outage run on it apart."""
        entries = self._recovery.entries()
        if any("clear_failed" in entry for entry in entries):
            return _unsettled(entries)  # a restore failed: a fault of the federation, whoever still holds it
        record = self._journal.read_holder()
        if record is None or holder_alive(record) is not False:  # contention: a wait, which charges no run
            return Health(False, f"{_named(record)} holds the federation: the check waits for it", deferred=True)
        done = "its record is cleared"
        if entries:  # it left something open: bring it back, verified, before anything else
            if not self._may_recover:
                return Health(
                    False, f"the federation's lock is stuck: {_named(record)} is gone and left an injection open"
                )
            self._recovery.recover()  # raises InfraError naming the target when one cannot be settled
            done = "what it left open is recovered and its record cleared"
        self._journal.clear_holder(record.get("token"))
        return Health(
            False, f"the federation's lock is stuck: {_named(record)} is gone, yet the lock stays held; {done}"
        )
