# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/campaign/scheduler.py:2174-2349
"""One attempt of one run, from the gates to the store (the worker side of the scheduler).

The order is fixed, and each step is decided by what a kill at that point must leave behind (C04):

1. the **health gate**: a held run stays pending and charges nothing; the run that finds an outage is its one attempt;
2. a **lease**, when the platform declares resources: with none to be had (every key quarantined, or none declared) the
   run is left unstarted and uses no attempt, because no attempt row exists yet (C19);
3. **the gates again**: a lease can take a long wait, in which the health gate may have closed, a harness stop or a
   unit's pause or the budget may have begun, or the campaign may have been told to end. If any is closed now the
   lease is released cleanly (nothing was launched, so there is no teardown to prove) and the run stays pending with no
   attempt used (C11, C17);
3a. the platform's **run admission**, when it has one (`Parts.admission`): it holds a shared resource for the run, may
   wait for it before anything starts (outside the agent's clock), and is left only after the post-run check and the
   store's record, so what the post-run check sees is what the run left; refused (`InfraError`), the run is left
   unstarted and uses no attempt. Its wait can be long too, so once admitted the run **looks at the gates again**, as
   after its lease: if one is closed now the admission is let go and the run stays pending, uncharged;
4. the backend, from the endpoint the campaign admitted (C14), and the attempt's timer;
5. the **'running' row** in the store, then the study's `RunExecutor`, which calls `record_attempt`; with a budget,
   under the attempt's spend guard, which tops up each send its token cap cannot account for (`topup`, C26);
6. the post-run health check, an instrument's refusal and the served-identity check, which can turn the result into
   infrastructure (C11, C31, C16);
7. the **outcome marker**, then the **store's record** of the result, in this order, with the unit's and the
   harness-error bookkeeping in the same transaction as the result (C04, C15, C10), so a kill cannot keep the result and
   lose the stop or the failure it caused; the lease ends after it. A result committed from its marker at the next
   start has no bookkeeping of its own: the scheduler derives the stops and the units from the stored attempts then.

An exception in the executor is the study's: `InfraError` is infrastructure, anything else is a harness defect, whose
redacted traceback goes to the campaign's local log (`defect_log`) and nowhere else; the request carries that log, so a
crash the loop catches inside `record_run` is logged too, redacted with the secrets the executor passed it. An
exception in the steps around it is our own bookkeeping failing, and propagates: the scheduler stops dispatch loudly
and leaves the 'running' row for recovery (C17).
"""

import asyncio
import contextlib
import dataclasses
import json
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expcore.campaign.attempts import classify, unverified
from expcore.campaign.defect_log import DefectLog
from expcore.campaign.executor import (
    BackendFactory,
    Endpoint,
    RunExecutor,
    RunRequest,
    failure_result,
    result_from_record,
    sent_no_request,
)
from expcore.campaign.factors import FactorSchema
from expcore.campaign.harness_stop import HarnessStops, kind_of
from expcore.campaign.healthgate import DEFER, FOUND, HOLD, HealthGate
from expcore.campaign.holds import Hold, HoldRegistry
from expcore.campaign.identity import ServedIdentities
from expcore.campaign.lease import Lease, LeaseCancelled, LeasePool, LeaseRefused
from expcore.campaign.markers import AttemptResult, write_marker
from expcore.campaign.settings import SchedulerConfig
from expcore.campaign.store import Store
from expcore.campaign.topup import AttemptSpend, Ledger
from expcore.campaign.units import UnitHealth
from expcore.config.models import ModelTable
from expcore.errors import InfraError
from expcore.loop.config import AgentConfig
from expcore.loop.outcomes import Outcome
from expcore.models import spend
from expcore.records.build import RunIdentity, redactor_for

LEASE_WAIT_NOTED_S = 0.5  # a wait for a lease longer than this is an event
# A platform's run admission: given the run's treatment kinds, its treatment targets, the campaign's end and whether its
# condition declares `exclusive = true`, a context manager that holds what the run needs from before the attempt until
# after its post-run check (`Parts.admission`), and yields what it holds: "shared" or "exclusive".
RunAdmission = Callable[[tuple[str, ...], Mapping[str, str], threading.Event, bool], AbstractContextManager[str]]
HELD_MODES = ("shared", "exclusive")  # what a run admission may yield


@dataclass
class Parts:
    """Everything an attempt needs, handed over by the scheduler once."""

    store: Store
    schema: FactorSchema
    settings: SchedulerConfig
    executor: RunExecutor
    agent_config: AgentConfig
    models: ModelTable
    endpoints: Mapping[str, Endpoint]
    backend_factory: BackendFactory
    root: Path
    stamp: Callable[[], str]
    monotonic: Callable[[], float]
    units: UnitHealth
    stops: HarnessStops
    identities: ServedIdentities
    holds: HoldRegistry
    recheck: Callable[[Any], Hold | None] = lambda row: None  # why a run that holds its lease may not start after all
    stop: threading.Event = field(default_factory=threading.Event)  # set when the campaign is ending
    exact_stamp: Callable[[], str] | None = None  # now at full precision, for the moment a stop begins
    health: HealthGate | None = None
    leases: LeasePool | None = None
    lease_group: Callable[[Mapping[str, Any]], str | None] = lambda factors: None
    admission: RunAdmission | None = None  # the platform's hold on a shared resource, around the attempt (step 3a)
    crash_hook: Callable[[str, str], None] = lambda point, run_key: None
    ledger: Ledger | None = None  # with a budget: the top-ups of the sends a run's token cap cannot account for (C26)
    # with a budget: whether the assessment that admitted the run reserved it by the reply maximum (C27); None: unknown
    reserved_capped: Callable[[str], bool | None] = lambda run_key: None
    # with a budget: whether the store has taken the model off the reply maximum now (C27); None: no budget, no maximum
    reply_cap_unheld: Callable[[str], bool | None] = lambda model: None


class AttemptRunner:
    """Runs attempts; safe to call from several worker threads, because all its state is in the store and its parts."""

    def __init__(self, parts: Parts):
        self.p = parts

    def treatment_kinds(self, factors: Mapping[str, Any], run_key: str) -> tuple[str, ...]:
        """Return the infrastructure kinds the run's condition declares as its treatment."""
        return self.p.schema.treatment_kinds[self.p.schema.condition_named(factors, run_key)]

    def treatment_targets(self, factors: Mapping[str, Any], run_key: str) -> dict[str, str]:
        """Return the target the run's condition declares for any of its treatment kinds."""
        return self.p.schema.treatment_targets[self.p.schema.condition_named(factors, run_key)]

    def work(self, row: Any) -> float:
        """Run one attempt of the row's run, end to end. Return the seconds before the run may start again."""
        p = self.p
        key, factors = row["run_key"], json.loads(row["factors"])
        if p.health is not None:
            pre = p.health.before_run(key)
            if pre.verdict == HOLD:
                return 0.0  # nothing is tried, charged or recorded; the gate holds dispatch
            if pre.verdict == DEFER:  # the platform is held on purpose: a wait, so nothing is tried or charged
                p.store.event(p.stamp(), "run_deferred", factors["model"], f"{key}: {pre.detail}; no attempt used")
                return p.settings.recheck_s
            if pre.verdict == FOUND:
                return self._charge_outage(row, factors, pre.detail)
        timer = p.agent_config.timer(p.monotonic)
        lease, granted = self._lease(row, factors, timer)
        if not granted:
            return p.settings.retry_delay_s  # left unstarted: no attempt row exists, so none is used
        launched = False
        try:
            hold = p.recheck(row)
            if hold is not None:
                self._leave_unstarted(row, factors, hold)
                return 0.0  # the hold decides when it may start again
            with self._admitted(row, factors) as admitted:
                if admitted is None:
                    return p.settings.retry_delay_s  # left unstarted: no attempt row exists, so none is used
                hold = p.recheck(row) if p.admission is not None else None  # the wait may have been long: look again
                if hold is not None:
                    self._leave_unstarted(row, factors, hold, waited_for="the platform's admission")
                    return 0.0  # leaving the block lets the admission go; the hold decides when it may start
                launched = True
                return self._attempt(row, factors, timer, lease, admitted)
        finally:
            if lease is not None and p.leases is not None:
                if launched:
                    p.leases.finish(lease)
                else:
                    lease.release()  # nothing ran under it: a clean release, with no teardown to prove

    # --- the steps ------------------------------------------------------------------------------------------------

    def _lease(self, row: Any, factors: Mapping[str, Any], timer: Any) -> tuple[Lease | None, bool]:
        """Take the lease the run needs. Return (the lease or None when it needs none, whether the run may start)."""
        p = self.p
        group = p.lease_group(factors) if p.leases is not None else None
        if group is None or p.leases is None:
            return None, True
        key, model = row["run_key"], factors["model"]
        try:
            lease = asyncio.run(
                p.leases.acquire_under(
                    timer,
                    group,
                    run_key=key,
                    timeout_s=p.settings.lease_timeout_s,
                    cancel=p.stop,
                    unit=p.endpoints[model].unit,
                )
            )
        except LeaseCancelled as refusal:  # the campaign is ending: not a hold on the group, just a run not started
            p.store.event(p.stamp(), "run_unstarted", model, f"{key}: no lease, the campaign is ending: {refusal}")
            return None, False
        except LeaseRefused as refusal:
            p.holds.note(f"lease:{group}", Hold("lease", f"{type(refusal).__name__}: {refusal}", temporary=True))
            waited = f"after waiting {refusal.wait_s:.1f} s "
            p.store.event(p.stamp(), "run_unstarted", model, f"{key}: no lease {waited}and no attempt used: {refusal}")
            return None, False
        p.holds.note(f"lease:{group}", None)
        if lease.wait_s >= LEASE_WAIT_NOTED_S:
            p.store.event(p.stamp(), "lease_waited", model, f"{key}: waited {lease.wait_s:.1f} s for lease {lease.key}")
        return lease, True

    @contextlib.contextmanager
    def _admitted(self, row: Any, factors: Mapping[str, Any]) -> Iterator[str | None]:
        """Hold the platform's run admission for the body, if it has one (step 3a); yield what the run holds of it
        ("shared", "exclusive", or "none" with no admission), as the platform says it admitted it, or None when the run
        may not start. An admission that yields anything else is a defect of the platform, and raises."""
        p = self.p
        if p.admission is None:
            yield "none"
            return
        key = row["run_key"]
        kinds, targets = self.treatment_kinds(factors, key), self.treatment_targets(factors, key)
        exclusive = p.schema.exclusive[p.schema.condition_named(factors, key)]
        with contextlib.ExitStack() as held:
            try:
                mode = held.enter_context(p.admission(kinds, targets, p.stop, exclusive))
            except InfraError as refusal:  # never admitted: nothing started, so no attempt is used
                text = f"{key}: not admitted by the platform, no attempt used: {refusal.detail}"
                p.store.event(p.stamp(), "run_unstarted", factors["model"], text)
                yield None
                return
            if mode not in HELD_MODES:
                raise ValueError(f"{key}: the run admission yielded {mode!r}, not one of {', '.join(HELD_MODES)}")
            yield mode

    def _leave_unstarted(
        self, row: Any, factors: Mapping[str, Any], hold: Hold, *, waited_for: str = "its lease"
    ) -> None:
        """Say that a run which got its lease (or its admission) was held after all: nothing started, no attempt used
        (C11, C17)."""
        key = row["run_key"]
        released = "lease" if waited_for == "its lease" else "admission"
        self.p.store.event(
            self.p.stamp(),
            "run_unstarted",
            factors["model"],
            f"{key}: a {hold.kind} hold began while it waited for {waited_for}; the {released} is released, no attempt "
            f"used: {hold.reason}",
        )

    def _begin(
        self, row: Any, lease: Lease | None = None, *, admission: str = "none", reserved_capped: bool | None = None
    ) -> tuple[Any, Path]:
        """Insert the 'running' row, with a directory of its own for this try, the lease it runs under, what it holds
        of the run admission, and the bound the budget gate reserved the run (C27; None when no gate decided it)."""
        p = self.p
        tries = len(p.store.q("SELECT 1 FROM attempts WHERE run_key=?", (row["run_key"],))) + 1
        run_dir = p.root / "runs" / row["digest"] / f"try{tries}"
        run_dir.mkdir(parents=True, exist_ok=True)
        begun = p.store.begin_attempt(
            row["run_key"],
            p.stamp(),
            os.getpid(),
            str(run_dir),
            lease_wait_s=None if lease is None else lease.wait_s,
            lease_outcome="none" if lease is None else "granted",
            reserved_capped=reserved_capped,
            admission=admission,
        )
        return begun, run_dir

    def _charge_outage(self, row: Any, factors: Mapping[str, Any], detail: str) -> float:
        """The run found an outage with its pre-run check: it is the outage's one attempt, and nothing was run."""
        p = self.p
        key = row["run_key"]
        begun, run_dir = self._begin(row, reserved_capped=p.reserved_capped(key))
        outcome = Outcome("infrastructure", f"pre-run health check failed: {detail}", infra_kind=p.settings.health_kind)
        result = AttemptResult(  # no request was sent: known to be no turn, so it costs 0 at any price (C24)
            outcome,
            p.stamp(),
            0.0,
            input_tokens=0,
            output_tokens=0,
            treatment_kinds=self.treatment_kinds(factors, key),
            treatment_targets=self.treatment_targets(factors, key),
            turn_usage=(),
        )
        self._finish(begun, run_dir, key, result)
        return p.settings.retry_delay_s

    def _attempt(
        self, row: Any, factors: Mapping[str, Any], timer: Any, lease: Lease | None, admission: str = "none"
    ) -> float:
        """Run the attempt the lease (or none) holds the resources for, record its result and do the bookkeeping."""
        p = self.p
        key, model = row["run_key"], factors["model"]
        endpoint, kinds = p.endpoints[model], self.treatment_kinds(factors, key)
        targets = self.treatment_targets(factors, key)
        backend = p.backend_factory(
            p.models.resolve(model),
            seed=factors["seed"],
            limits=p.agent_config.limits,
            base_url=endpoint.base_url,
            time_left=timer.time_left,
        )
        # The decision the run was admitted under, never the model's status read now (C27). The row keeps it as it is,
        # so an attempt whose usage turns out unknown is charged that bound; the guard takes unknown as yes, which
        # checks more, never less.
        decided = p.reserved_capped(key)
        begun, run_dir = self._begin(row, lease, admission=admission, reserved_capped=decided)
        guard = None
        if p.ledger is not None:
            guard = AttemptSpend(p.ledger, begun.id, key, model, reserved_capped=True if decided is None else decided)
        defects = DefectLog(p.root, key, begun.number, p.stamp, lambda: redactor_for(backend, models=p.models))
        request = RunRequest(
            RunIdentity(key, factors, begun.number),
            run_dir,
            endpoint,
            backend,
            timer,
            p.monotonic,
            kinds,
            lease,
            treatment_targets=targets,
            models=p.models,
            defect_log=defects,
            reserved_capped=decided,  # the row's value, so the record and the store say one thing (C27)
            reply_cap_unheld=lambda: p.reply_cap_unheld(model),
        )

        async def go() -> Any:
            try:  # every request of the attempt, in any task or thread it starts, goes through its guard
                with contextlib.nullcontext() if guard is None else spend.watching(guard):
                    return await p.executor(request)
            finally:
                await backend.aclose()

        started = p.monotonic()
        record = None
        try:
            record = asyncio.run(go())
            result = result_from_record(record, ended_utc=p.stamp())
        except Exception as error:  # the executor's: infrastructure or a defect of ours, classified by what it is
            result = failure_result(
                error,
                ended_utc=p.stamp(),
                wall_s=p.monotonic() - started,
                treatment_kinds=kinds,
                treatment_targets=targets,
                sent_nothing=sent_no_request(error, backend),
            )
            if result.outcome.defect is not None:  # the attempt keeps the class and the stack hash; the log the rest
                defects(error, result.outcome.defect)
        result, moved = self._judge(result, record, model, key)
        mix = None if record is None else p.identities.deployment_mix(model, record.transcript)  # B18

        def bookkeeping() -> None:  # in the transaction of the result: both stand, or neither does
            self._bookkeeping(model, endpoint, key, result, kinds, targets, moved)
            if mix is not None:
                p.store.record_deployments(begun.id, mix)

        status = self._finish(begun, run_dir, key, result, bookkeeping)
        defects.raise_failure()  # only now, with the record, the marker and the store written: a loud stop (C17)
        return p.settings.retry_delay_s if status == "pending" else 0.0

    def _judge(self, result: AttemptResult, record: Any, model: str, key: str) -> tuple[AttemptResult, bool]:
        """Apply the post-run health check (C11) and the served-identity check (C16) to a result."""
        p = self.p
        if p.health is not None:
            health, outage = p.health.after_run()
            if health.deferred:  # it verified nothing: no result is scored on it, treatment included (C11)
                result = unverified(result, health.detail)
                text = f"{key}: {health.detail}; nothing is scored on it ({result.outcome.infra_kind or 'set aside'})"
                p.store.event(p.stamp(), "post_run_check_deferred", model, text)
            elif not health.ok and outage is not None:
                result = dataclasses.replace(result, outage_kind=p.settings.health_kind)  # the outage's, set aside
                p.health.note_discard(outage, result.input_tokens, result.output_tokens)
            elif not health.ok and not result.outcome.set_aside:
                text = f"post-run health check failed: {health.detail}"
                result = dataclasses.replace(
                    result, outcome=Outcome("infrastructure", text, infra_kind=p.settings.health_kind)
                )
            elif not health.ok:
                p.store.event(p.stamp(), "post_run_check_failed", model, f"{key}: {health.detail}; the outcome stands")
        if record is None:
            return result, False
        result = self._judge_instruments(result, record, model, key)
        change = p.identities.check(model, record.transcript, p.stamp(), run_key=key)
        if change is None:
            return result, False
        p.store.event(p.stamp(), "served_identity_changed", model, f"{key}: {json.dumps(change, sort_keys=True)}")
        if result.outcome.set_aside:
            return result, True  # already set aside: the event and the unit's failure are what is left to do
        text = "the endpoint's served identity changed during the run"
        outcome = Outcome("infrastructure", text, infra_kind=p.settings.identity_kind)
        return dataclasses.replace(result, outcome=outcome), True

    def _judge_instruments(self, result: AttemptResult, record: Any, model: str, key: str) -> AttemptResult:
        """Apply the refusal an instrument's post-run check put in the record (C31): the third way a finished attempt is
        set aside here, after the health check, which decides first, and before the endpoint's identity.

        The refusal applies to an attempt the store would score, which is what `classify` says: a cap or an answer, or
        a fault the condition declares as its treatment, whether or not an outage flag is on it (the treatment rule is
        checked first, C01). Such an attempt becomes infrastructure of the instrument's kind, a kind no condition can
        have as its treatment, so it is never scored: it is run again like any infrastructure failure. The record keeps
        what the loop saw; the result is what the store decides on, so it no longer claims the treatment the record
        flagged. A result that still carries an `outage_kind` is then classified as an attempt an outage overlapped,
        `outage_discarded` and not charged, as the health check's outcome would be; any other is `infra_fail` and
        charged. Either way it is not scored and its run is tried again.

        An attempt the store would not score, one set aside by the health check, an outage, or its own infrastructure
        failure or defect, keeps its outcome and the refusal is noted.
        """
        p, refusal = self.p, record.instrument_refusal
        if refusal is None:
            return result
        who = f"instrument {refusal['namespace']!r} refused the attempt"
        text = f"{key}: {who}: {refusal['infra_kind']}: {refusal['detail']}"
        scored = classify(result, result.treatment_kinds, result.treatment_targets).state == "done"
        if not scored and (result.outage_kind is not None or result.outcome.set_aside):
            p.store.event(p.stamp(), "instrument_refused", model, f"{text}; the outcome stands")
            return result
        p.store.event(p.stamp(), "instrument_refused", model, text)
        detail = f"{who} after the run: {refusal['detail']}"
        outcome = Outcome("infrastructure", detail, infra_kind=refusal["infra_kind"])
        return dataclasses.replace(result, outcome=outcome, recorded_treatment=False)

    def _finish(
        self, begun: Any, run_dir: Path, key: str, result: AttemptResult, also: Callable[[], None] | None = None
    ) -> str:
        """Write the marker, then the store's record, `also` in its transaction; a kill between is recovery's (C04)."""
        p = self.p
        p.crash_hook("before_marker", key)
        write_marker(run_dir, key, begun.id, result)
        p.crash_hook("after_marker", key)
        status = p.store.finish_attempt(begun.id, result, p.settings.max_attempts, also=also)
        p.crash_hook("after_metadata", key)
        return status

    def _bookkeeping(
        self,
        model: str,
        endpoint: Endpoint,
        key: str,
        result: AttemptResult,
        kinds: tuple[str, ...],
        targets: Mapping[str, str],
        moved: bool,
    ) -> None:
        """Count the unit's failure or success (C15) and the harness error (C10), as the result's cause says."""
        p = self.p
        classified = classify(result, kinds, targets)
        if classified.treatment:
            return  # the outage is the manipulation: it neither pauses the unit nor clears its pause
        outcome = result.outcome
        if moved or outcome.infra_kind in ("endpoint", "key_rejected"):
            p.units.failure(
                endpoint.unit, immediate=outcome.infra_kind == "key_rejected", detail=f"{key}: {outcome.detail}"
            )
        elif classified.state == "done":
            p.units.success(endpoint.unit)
        if classified.state == "harness_error":
            defect = outcome.defect
            p.store.event(p.stamp(), "harness_error", model, f"{key}: {defect.exception if defect else 'unknown'}")
            now = p.stamp()  # records the stop it fires, if any, as an event
            p.stops.note(model, kind_of(outcome), now, exact=p.exact_stamp() if p.exact_stamp else now)
