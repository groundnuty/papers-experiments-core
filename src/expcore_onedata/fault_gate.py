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
federation; a heal's own marker (`clear_failed` with `healing`, its latest line) is no failed restore yet, but a heal
under way. A live holder, or none recorded, is contention, an outage run in progress, its heal included, which is no
fault of the federation: the check answers deferred and charging no one (`Health(..., deferred=True, charge=False)`),
which the core's gate treats as a wait that charges no run, and, once it outlasts `max_defer_s` (as an outage run
with a heal can), as a blocking hold that still charges no run. A recorded holder that is gone while the lock stays
held is
stuck: the check recovers, verified, whatever the journal holds open, marks the record `stuck_since` (the first such
verdict's time) and fails, saying the lock is stuck. The record is never deleted for it, so every later timeout is stuck
too; only an acquisition of the lock, which proves it usable, clears it.

**It heals stuck pairs its own link check found (O35, `fault_gate_heal`).** Given the link report the rest of the check
made in the calling thread (`links`), a way to make it check the links again (`relink`) and the providers' services
(`labels`), as `PlatformHealth` gives them, a check that holds no run's admission and whose own all-pairs link check, in
this call, found a stuck pair heals it with `[faults] heal`: it lets its shared hold go, takes the federation
exclusively (`RECOVERY`), waiting at most `health_wait_s` for every shared holder and for the drain record, and failing
closed, uncharged, as the hold `federation_busy` when either outlasts it, and hands the admission to the restore's heal,
with no provider restored, **in the background** (`_heal_on`). The check that began it, and every check while it runs,
answers the uncharged deferral (`Health(..., deferred=True, charge=False)`) at once, so a heal longer than the
campaign's `health_timeout_s` never reads as down and a pair it mends charges no run; past the heal's own bound
(`heal_timeout_s` and `heal_settle_s` since it began) a heal still running closes the gate, uncharged. That bound, after
a session drain and with the core's waits between and around them (a recheck and the check that starts the heal, and
a recheck and the check that makes the link check afresh after it), is the longest the gate declares it may defer
(`max_deferral_s`), and the core refuses a campaign whose `max_defer_s` is shorter at its start, so a heal never turns
into a blocking hold that stops the campaign. A check that found the pair too and meets a heal under way (this gate's,
or another check's holding the federation) waits the same way, never taking the latch the heal set for a closed
episode, and so does another process's check that meets a heal's container restart journalled `clear_failed` during
its settle. Whatever the heal did, the next check makes the link
check afresh (`relink`), so no pass from before the heal's end opens the gate: a heal that left a pair failing leaves it
to the next check, which finds it stuck and fails closed, and none runs again until the gate's link check passes. A
check under a run's admission keeps a share of it until it ends, so one that outlives its caller's wait still holds the
federation.
"""

import contextlib
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from expcore.campaign.healthgate import DeferralBound, Health, HealthCheck, read_health
from expcore.errors import TARGET_LABEL, InfraError
from expcore_onedata.admission import (
    EXCLUSIVE_RUN,
    INJECTOR,
    OUTAGE_RUN,
    RECOVERY,
    Admission,
    FederationBusy,
    FederationLock,
    current_admission,
    require_same_federation,
)
from expcore_onedata.fault_gate_heal import GateHeal, GateHeals
from expcore_onedata.fault_journal import holder_alive
from expcore_onedata.fault_matrix import LinkMatrix
from expcore_onedata.fault_recovery import FaultDriver, Recovery
from expcore_onedata.fault_settings import FaultSettings, heal_deferral_s, heal_wait_s

_PURPOSES = {
    OUTAGE_RUN: "an outage run",
    EXCLUSIVE_RUN: "an exclusive run",
    INJECTOR: "an injector",
    RECOVERY: "the recovery of the health check",
}
log = logging.getLogger(__name__)
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


def _held_for(record: Mapping[str, Any]) -> float | None:
    """The seconds since a holder record's `since_utc`, on the wall clock; None when it holds no readable time."""
    since = record.get("since_utc")
    try:
        taken = datetime.fromisoformat(since) if isinstance(since, str) else None
    except ValueError:
        return None
    if taken is None or taken.tzinfo is None:
        return None
    return (datetime.now(UTC) - taken).total_seconds()


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
        relink: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
    ) -> None:
        """`links()` takes the link report `inner` made in the calling thread since it was last taken (a
        `linkcheck.LinkReport` or a `LinkMatrix`; None when it made none), so a call acts only on a report its own
        check made, never on one another call's made; `relink()` makes `inner`'s next call run its link check again,
        whatever its last verdict; `labels` maps each provider's label to its service. With all three, a declared and
        sanctioned heal, the gate heals the stuck pairs its own check finds (see the module docstring)."""
        self._clock, self._sleep, self._drain_s = clock, sleep, settings.session_drain_s
        self._lock = FederationLock(settings, clock=clock, sleep=sleep, wall=wall)
        self._recovery = Recovery(settings, driver, clock=clock, sleep=sleep, labels=labels, wall=wall)
        self._journal = self._recovery.journal
        self._may_recover = settings.sanctioned is True
        self._wait_s = settings.health_wait_s
        self._inner = inner
        self._links, self._relink = links, relink
        self._heals = GateHeals(settings.journal)
        declared = settings.heal != "off" and self._may_recover
        heals = declared and links is not None and relink is not None and bool(labels)
        self._healer = (
            GateHeal(settings, driver, self._recovery, labels or {}, self._lock.hold_for, self._heals)
            if heals
            else None
        )
        self._last_heal: dict[str, Any] | None = None
        self._heal_guard = threading.Lock()  # the heal running in the background, and when it began
        self._heal_thread: threading.Thread | None = None
        self._heal_began = 0.0
        self._heal_bound_s = heal_wait_s(settings)  # the longest a heal defers the gate
        self._settings = settings

    def max_deferral_s(self) -> DeferralBound | None:
        """The longest this gate's checks may stay deferred, for the core to hold the campaign's `max_defer_s` to at
        its start (`healthgate.deferral_bound`); None with the heal off. With a declared heal the first deferral can be
        the session drain of an outage run (`session_drain_s`), and the waits that follow it are these, whether this
        gate heals or another process's does: one wait between two checks and the check that starts the heal (the
        heal begins at a check, so it begins up to `recheck_s` plus the poll and `health_timeout_s` after the drain
        ends), the heal's whole bound (`heal_wait_s`: `heal_timeout_s` and `heal_settle_s`), and then, since the last
        deferred answer of the heal can come just before it ends, one more wait between two checks and the check that
        makes the link check afresh, which a call that meets it waits for, uncharged (`PlatformHealth._regular`). The
        platform's own seconds are the drain and the heal's bound (`heal_deferral_s`); the waits between and around
        them are the core's settings, which it adds (`DeferralBound.rechecks`, `checks`): two rechecks and two checks
        with a drain. With no drain the first deferral is the check that starts the heal, so one recheck and one check
        (the pair after the heal) are added.

        **What it assumes and leaves out.** The heal ends within its own bound (`heal_timeout_s`, and the settle's hold
        `heal_settle_s` after its last restart; a heal that overruns it closes the gate, and the hold it leaves can
        defer a check after that, past the total). Every check, whole (its waits for the federation and its link
        check), ends within the campaign's `health_timeout_s`, the check's own contract (`docs/interfaces.md`,
        HealthCheck; the link check too, `linkcheck`: keep it above `visible_timeout_s + timeout_s + evict_timeout_s`).
        The core gives up on a check that outlives it, reads it as down and throws its answer away, but the check's
        thread runs on and can still start the heal later than the bound counts. And three waits are not in it: a
        deferral that began before the drain (an outage run still running, another worker's check deferred on it) or
        for another reason (a run on an ingest baseline, which `PlatformHealth` checks first) counts from its first
        answer, and so does a heal that waits for another holder of the federation (the hold `federation_busy`), so
        each adds to its age."""
        settings = self._settings
        if settings.heal == "off":
            return None
        drained = settings.session_drain_s > 0
        first = (
            f"session_drain_s ({settings.session_drain_s:g} s) after an outage run, then one wait between two checks "
            "(the core's recheck_s and poll_interval_s) and the check that starts the heal (its health_timeout_s), "
            "then "
            if drained
            else "the check that starts the heal, then "
        )
        detail = (
            f'[faults] heal = "{settings.heal}": {first}heal_timeout_s ({settings.heal_timeout_s:g} s) and '
            f"heal_settle_s ({settings.heal_settle_s:g} s), then another wait between two checks and the check that "
            "makes the link check afresh after the heal (its health_timeout_s), each check assumed to end within its "
            "health_timeout_s and the heal within its own bound, and no deferral begun earlier or for another reason, "
            "nor a wait for another holder of the federation, counted; lower heal_timeout_s or session_drain_s, or set "
            'heal = "off", to shorten it'
        )
        return DeferralBound(
            heal_deferral_s(settings), detail, rechecks=2 if drained else 1, checks=2 if drained else 1
        )

    @property
    def last_heal(self) -> dict[str, Any] | None:
        """The record of the latest heal this gate ran (`fault_gate_heal`), None before one."""
        return self._last_heal

    def wait_for_heal(self, timeout_s: float | None = None) -> bool:
        """Wait, at most `timeout_s`, for the heal this gate runs in the background to end; whether none runs now (for
        a caller that must not leave one running: a shutdown, a test)."""
        with self._heal_guard:
            thread = self._heal_thread
        if thread is not None:
            thread.join(timeout_s)
        return thread is None or not thread.is_alive()

    def _healing(self) -> Health | None:
        """While this gate's heal runs: the uncharged deferral, within the heal's own bound (`heal_timeout_s` and
        `heal_settle_s` since it began), and past it a closed gate, still uncharged, so a hung heal never defers for
        ever; None when no heal runs."""
        with self._heal_guard:
            thread, began = self._heal_thread, self._heal_began
        if thread is None or not thread.is_alive():
            return None
        waited, bound = self._clock() - began, self._heal_bound_s
        if waited <= bound:
            detail = f"the fault gate is healing stuck pairs ({waited:.0f} s of at most {bound:g} s): the check waits"
            return Health(False, detail, deferred=True, charge=False)
        detail = (
            f"the fault gate's heal has run {waited:.0f} s, past its bound of {bound:g} s (heal_timeout_s and "
            "heal_settle_s): the gate stays closed until a check after the heal passes"
        )
        return Health(False, detail, deferred=False, charge=False)

    def __call__(self) -> Health:
        try:
            held = current_admission()
            if held is not None:  # a run's own post-run check, under its admission: no injection but its own can start,
                # and nothing is healed, since the run's tokens may still be in play
                require_same_federation(held, self._lock.federation)
                return self._held_check(held)
            healing = self._healing()  # a heal runs: a wait that charges no run, answered at once
            if healing is not None:
                return healing
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
                verdict, made = self._judged()
            finally:
                shared.close()
            stuck = made is not None and not verdict.ok and any(pair.stuck for pair in made.pairs)
            return self._heal(verdict, made) if stuck and made is not None else verdict
        except InfraError as error:
            return Health(False, error.detail)

    def _held_check(self, held: Admission) -> Health:
        """The check under the caller's admission, a share of it kept until the check ends: a check that outlives its
        caller's wait (the core gives up on it, and the run lets its admission go) still holds the federation while it
        reads, so no heal's admission is granted under it. An admission already being let go fails it closed."""
        give_up = held.try_retain()
        if give_up is None:
            return Health(
                False, "the run's admission was let go before its check could hold it, so nothing was checked"
            )
        try:
            return self._judged()[0]
        finally:
            give_up()

    def _checked(self) -> Health:
        """With the federation held: the journal, then the rest of the check."""
        entries = self._recovery.entries()
        if entries:
            return _unsettled(entries)
        return read_health(self._inner) if self._inner is not None else Health(True, "no injection is unsettled")

    def _taken(self) -> Any:
        return None if self._links is None else self._links()

    def _judged(self) -> tuple[Health, LinkMatrix | None]:
        """With the federation held: the check (`_checked`), and the matrix of the link check it made in this call, in
        this thread (None when it made none: a report another call made is never this call's). A link check of this
        call that passed lets a later stuck pair be healed again (`GateHeals.passed`), while the federation is held."""
        self._taken()  # whatever this thread's earlier calls left untaken is not this call's
        verdict = self._checked()
        report = self._taken()
        if report is None:
            return verdict, None
        matrix = report if isinstance(report, LinkMatrix) else LinkMatrix.from_report(report)
        if matrix.ok:
            with contextlib.suppress(OSError, ValueError):  # the latch then stays, and no heal runs: closed
                self._heals.passed()
        return verdict, matrix

    def _latched(self, verdict: Health) -> Health | None:
        """The answer when no heal may run (a heal has begun since the gate's link check last passed, or the record
        cannot be read), else None."""
        try:
            latched = self._heals.latched()
        except (OSError, ValueError) as error:
            why = f"the record of the fault gate's heals cannot be read ({type(error).__name__}), so no heal runs"
            return verdict._replace(detail=f"{verdict.detail}; {why}")
        if not latched:
            return None
        why = "the fault gate has healed once since its link check last passed, and no heal runs again until a link "
        return verdict._replace(detail=f"{verdict.detail}; {why}check passes")

    def _heal(self, verdict: Health, trigger: LinkMatrix) -> Health:
        """The check's own link check found a stuck pair, and the check holds no run's admission: take an exclusive
        admission of the gate's own and hand it to a heal in the background, answering the uncharged deferral, so the
        heal never runs past the core's wait for this check (`health_timeout_s`) and no run is charged for it; fail
        closed whenever it cannot begin."""
        if self._healer is None or self._relink is None:  # no heal declared, or no sanction, services or reports
            return verdict
        refused = self._refused(verdict)  # read first, so a latched gate waits for nobody
        if refused is not None:
            return refused
        try:  # waits, inside the bound, for every shared holder and then for the drain record, as any admission does
            exclusive = self._lock.acquire(exclusive=True, timeout_s=self._wait_s, purpose=RECOVERY)
        except FederationBusy as busy:
            healing = self._healing() or self._healing_elsewhere()
            if healing is not None:  # another check's heal began meanwhile: its wait, uncharged
                return healing
            # the hold `federation_busy`: the gate closes (a pair is stuck) and the campaign holds, charging no run for
            # a wait the run did nothing to cause; the next check of the closed gate tries again
            why = (
                f"the fault gate's heal did not run ({busy.kind}): the federation was not free of runs, tools and "
                f"drains within health_wait_s ({self._wait_s:g} s), so the gate stays closed"
            )
            return verdict._replace(detail=f"{verdict.detail}; {why}", deferred=False, charge=False)
        handed = False
        try:
            entries = self._recovery.entries()
            if entries:  # an injection opened since the check: its restore owns the links
                return _unsettled(entries)
            refused = self._latched(verdict)  # again, under the admission: another check may have healed meanwhile
            if refused is not None:
                return refused
            with self._heal_guard:
                self._heal_began = self._clock()
                thread = threading.Thread(
                    target=self._heal_on, args=(exclusive, trigger), name="fault-gate-heal", daemon=True
                )
                self._heal_thread = thread
                thread.start()  # it owns the admission from here, and lets it go when the heal has ended
            handed = True
        finally:
            if not handed:
                exclusive.close()
        why = (
            f"the fault gate has begun healing the stuck pairs its link check found (for at most "
            f"{self._heal_bound_s:g} s): the check waits for it"
        )
        return Health(False, f"{verdict.detail}; {why}", deferred=True, charge=False)

    def _refused(self, verdict: Health) -> Health | None:
        """Before this check's heal takes the federation: a heal that runs (this gate's, or another check's that holds
        the federation) answers its uncharged wait, never the latch it set; with no heal running, a latch set since the
        gate's link check last passed refuses the heal, charged as any stuck link is; None when the heal may go on.
        A heal running is asked about before the latch and again after it, and the latch read once more, so a heal that
        began, or one that ended with a pass, between the readings is never taken for a closed episode."""
        waiting = self._healing() or self._healing_elsewhere()
        if waiting is not None:
            return waiting
        if self._latched(verdict) is None:
            return None
        waiting = self._healing() or self._healing_elsewhere()
        if waiting is not None:
            return waiting
        return self._latched(verdict)

    def _healing_elsewhere(self) -> Health | None:
        """`_recovering` of the holder record as it stands now."""
        return self._recovering(self._journal.read_holder())

    def _recovering(self, record: Mapping[str, Any] | None) -> Health | None:
        """While another health check, in this process or another, holds the federation for its recovery or heal (its
        holder record, alive): the uncharged deferral within the heal's bound (`heal_timeout_s` and `heal_settle_s`
        since the record's `since_utc`), and past it, or when the record does not say since when, a closed gate,
        still uncharged, so a hung heal of a live process never defers for ever; None when no such holder holds it."""
        if record is None or record.get("purpose") != RECOVERY or holder_alive(record) is False:
            return None
        held_s, bound = _held_for(record), self._heal_bound_s
        if held_s is not None and held_s <= bound:
            detail = f"{_named(record)} holds the federation: the check waits for it"
            return Health(False, detail, deferred=True, charge=False)
        detail = (
            f"{_named(record)} has held the federation past the heal's bound of {bound:g} s (heal_timeout_s and "
            "heal_settle_s), or its record does not say since when: the gate stays closed until a check after it passes"
        )
        return Health(False, detail, deferred=False, charge=False)

    def _heal_on(self, exclusive: Admission, trigger: LinkMatrix) -> None:
        """The heal, in the background, under the exclusive admission it was handed, which it lets go at its end. A
        heal that mended every pair makes the gate's own link check afresh under the admission (`relink`), and a pass
        lifts the latch. Whatever the heal did, the rest of the check's last link report is then expired before the
        admission goes (`relink`), so the next check checks the links afresh and no pass made before the end of the
        heal, by any check, opens the gate: after a heal that failed it finds the pairs still stuck and fails closed,
        the latch keeping a second heal out."""
        try:
            result, said = self._healer.run(trigger) if self._healer is not None else (None, "no heal is declared")
            if result is not None:
                self._last_heal = result.record
            if result is not None and result.healed and self._relink is not None:
                self._relink()
                self._judged()  # under the admission: a pass lifts the latch
            else:
                log.warning("the fault gate's heal left the gate closed: %s", said)
        except Exception as error:  # the next check finds the pairs as they are, and fails closed on them
            log.warning("the fault gate's heal raised %s; the gate stays closed", type(error).__name__)
        finally:
            try:
                if self._relink is not None:
                    self._relink()  # the next check checks the links afresh, whatever any check found before
            finally:
                exclusive.close()

    def _busy(self) -> Health:
        """The wait for the federation ran out: tell a failed restore, a stuck holder and an outage run on it apart."""
        entries = self._recovery.entries()
        record = self._journal.read_holder()  # read once: who holds the federation, as every branch below judges it
        recovering = self._recovering(record)  # a health check's recovery or heal: its bounded wait, or None
        failed = [entry for entry in entries if "clear_failed" in entry and not entry.get("healing")]
        if failed and recovering is not None and all(entry.get("heal") is True for entry in failed):
            # only a heal's own container restarts failed, and the health check that heals still holds the
            # federation for their settle: a heal not over yet, a wait that charges no run (closed past its bound)
            return recovering
        if failed:
            return _unsettled(entries)  # a restore failed: a fault of the federation, whoever still holds it
        if recovering is not None:
            # before the drain record, which a heal that outlived its bound may still be extending with a late settle
            return recovering
        draining = self._lock.drain_left()
        if draining > 0:  # nobody holds it: it is closed while an outage run's sessions drain (or a heal's settle
            # runs out), and that charges no run
            return Health(
                False,
                f"the federation is closed for {draining:.0f} s more while an outage run's sessions drain or a heal "
                "settles: the check waits for it",
                deferred=True,
                charge=False,  # nor once it outlasts the gate's bound: the drain says nothing about the waiting run
            )
        if record is None or holder_alive(record) is not False:
            # contention, a heal under way included (its marker is no failed restore yet): a wait that charges no run,
            # nor the one that finds it once it outlasts the gate's bound, as an outage run with a heal can
            detail = f"{_named(record)} holds the federation: the check waits for it"
            return Health(False, detail, deferred=True, charge=False)
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
