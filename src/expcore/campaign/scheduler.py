# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/scheduler.py, blob ab7e21172a589eaee0f70b47eb5f285fc21abeca
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""The scheduler kernel: dispatch the store's pending runs, in the manifest's seeded order, on one host.

The imported campaign loop was one 3,000-line class that interleaved a study's holds, hosts, memory watchdogs and
options with the dispatch. This is the generic kernel and nothing else:

- **open** (`Scheduler.open`): take the one-dispatcher lock (C07), open the store for a resume and settle what a kill
  left (C04, C06), derive the stops and the units' failures from the stored attempts (C10, C15), restore the gates,
  admit each model's endpoint once (C14), sweep what dead lease holders left (C19).
- **dispatch** (`Scheduler.run`): walk the pending runs in manifest order; a run starts when no hold applies to it and
  no cap is full. Every reason it may not is a `Hold` on a subject (all runs, a model, a unit), written to the event log
  when it changes (`holds`): a harness-error stop (C10), a closed health gate (C11), a budget (C12), an unadmitted model,
  a unit's pause (C15). With `on_hold`, each change is also said as one line as it happens, with when dispatch resumes,
  so a command line is not silent through a pause.
- **a worker per run** (`attempt.AttemptRunner`): the gates, a lease, the study's `RunExecutor` through
  `record_attempt`, the marker, the store (C04, C16, C19). An exception in that bookkeeping stops dispatch loudly and
  leaves the run for recovery (C17).

There are no study names, layouts or options here, and no slots: a **unit** is any key the platform names for what runs
share (an endpoint, a provider), and caps count runs in flight per unit, plus leases held back by quarantine and the
sandboxes of reserved processes beside the campaign. A key whose resource may still be live (a quarantined lease, a
key held back for a dead holder, before and after a restart) counts against the cap of the unit its run belonged to
(C19); a quarantined lease also against the global one.
"""

import asyncio
import contextlib
import datetime as dt
import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expcore.campaign import reply_cap
from expcore.campaign.attempt import AttemptRunner, Parts, RunAdmission
from expcore.campaign.defect_log import log_path
from expcore.campaign.executor import BackendFactory, Endpoint, RunExecutor, default_backend_factory
from expcore.campaign.factors import EXCLUSIVE_KEY, FactorSchema
from expcore.campaign.gate import BudgetGate
from expcore.campaign.harness_stop import HarnessStops
from expcore.campaign.healthgate import HealthCheck, HealthGate
from expcore.campaign.holds import ALL, Hold, HoldRegistry
from expcore.campaign.identity import ServedIdentities
from expcore.campaign.lease import LeasePool
from expcore.campaign.lock import CampaignLocked, DispatchLock
from expcore.campaign.manifest import Manifest
from expcore.campaign.probe_spend import MeteredProbes
from expcore.campaign.recovery import Recovery
from expcore.campaign.reservation import Reservations
from expcore.campaign.resume import open_campaign
from expcore.campaign.settings import SchedulerConfig
from expcore.campaign.shard_run import ShardStart
from expcore.campaign.store import Store, column_for
from expcore.campaign.topup import Ledger
from expcore.campaign.units import UnitHealth, history_from_attempts
from expcore.config.loading import Section
from expcore.config.models import ModelTable
from expcore.loop.config import AgentConfig
from expcore.models.admission import admit_reading, max_tokens_ignored, reply_cap_unshown

__all__ = ["CampaignLocked", "RunSummary", "Scheduler", "iso", "utc_now"]

STATE_FILE = "state.sqlite"


def utc_now() -> dt.datetime:
    """Return the current time, timezone-aware, in UTC."""
    return dt.datetime.now(dt.UTC)


def iso(moment: dt.datetime) -> str:
    """Return a time the way the store writes timestamps: ISO 8601 UTC, whole seconds, Z suffix."""
    return moment.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class RunSummary:
    """How a call to `run` ended: the workers it started (`dispatched`, what `max_dispatch` bounds), the holds still
    in force, the runs by status, the local log of harness defects' tracebacks when there is one (`defect_log`), and
    the attempts it began: a worker that got no lease, was not admitted by the platform (`Parts.admission`), or found a
    gate closed once it had either, began none."""

    dispatched: int
    holds: Mapping[str, Hold] = field(default_factory=dict)
    statuses: Mapping[str, int] = field(default_factory=dict)
    harness_log: Path | None = None
    attempts: int = 0


class Scheduler:
    """The dispatcher of one campaign store. Use it as a context manager: `with Scheduler(...) as scheduler`."""

    def __init__(
        self,
        root: Path | str,
        schema: FactorSchema,
        manifest: Manifest,
        fingerprint: dict,
        executor: RunExecutor,
        *,
        agent_config: AgentConfig,
        models: ModelTable,
        endpoints: Mapping[str, Endpoint],
        settings: SchedulerConfig | None = None,
        health: HealthCheck | None = None,
        leases: LeasePool | None = None,
        lease_group: Callable[[Mapping[str, Any]], str | None] = lambda factors: None,
        admission: RunAdmission | None = None,
        reservations: Reservations | None = None,
        backend_factory: BackendFactory = default_backend_factory,
        clock: Callable[[], dt.datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        crash_hook: Callable[[str, str], None] = lambda point, run_key: None,
        on_hold: Callable[[str], None] | None = None,
        shard: ShardStart | None = None,
    ):
        self.root, self.schema, self.manifest, self.fingerprint = Path(root), schema, manifest, fingerprint
        self.shard = shard  # a shard's start: `manifest` is then its part of the plan, `settings` its shares (#154)
        self.on_hold = on_hold  # takes one line per hold that begins, changes or ends; never one per pass
        self.executor, self.agent_config, self.models = executor, agent_config, models
        self.endpoints, self.settings = dict(endpoints), settings or SchedulerConfig()
        self.health_check, self.leases, self.lease_group = health, leases, lease_group
        self.admission = admission
        self.reservations, self.backend_factory = reservations, backend_factory
        self.clock, self.monotonic, self.crash_hook = clock, monotonic, crash_hook
        self.plan_models = sorted({row.key.factors["model"] for row in manifest.rows})
        missing = [m for m in self.plan_models if m not in self.endpoints]
        if missing:
            raise ValueError(f"the plan runs model(s) {', '.join(missing)} that have no admitted endpoint")
        self.settings.check_kinds(schema)
        exclusive = [name for name, value in schema.exclusive.items() if value]
        if exclusive and admission is None:  # nothing would hold the platform exclusively: refuse, never pretend
            raise Section({}, schema.file, f"[conditions.{exclusive[0]}]").fail(
                EXCLUSIVE_KEY,
                "declares an exclusive run, but the platform gives the campaign no run admission to hold exclusively",
            )
        self.store: Store | None = None
        self.recovery: Recovery | None = None
        self.not_admitted: dict[str, str] = {}
        self.stop = threading.Event()
        self._lock = DispatchLock(self.root)
        self._cv = threading.Condition()
        self._inflight: dict[str, tuple[str, str]] = {}  # run key -> (model, unit)
        self._assess = (
            threading.Lock()
        )  # the budget is assessed by the dispatcher and by the workers' recheck, one at a time
        self._not_before: dict[str, float] = {}  # run key -> monotonic time before which it does not start again
        # run key -> whether the assessment that admitted the run reserved it by the reply maximum (C27)
        self._reserved_capped: dict[str, bool] = {}
        self._workers: list[threading.Thread] = []
        self._run_thread: threading.Thread | None = None  # the thread inside `run`, while there is one
        self._fatal: BaseException | None = None

    # --- lifecycle ------------------------------------------------------------------------------------------------

    def stamp(self) -> str:
        """Return the current time as the store writes it."""
        return iso(self.clock())

    def exact_stamp(self) -> str:
        """Return the current time at full precision, microseconds included: the moment a stop begins is dated with it."""
        return self.clock().astimezone(dt.UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def open(self) -> "Scheduler":
        """Take the lock, open the store for a resume, restore the gates and admit the endpoints."""
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock.acquire()
        try:
            self._open_store()
        except BaseException:
            self.close()
            raise
        return self

    def _open_store(self) -> None:
        cfg = self.settings
        if cfg.budget is not None and cfg.refuse_unpriced:
            self.models.require_priced(self.plan_models)
        if cfg.budget is not None:  # the reservation bounds the request past the token cap by the window (C25)
            self.models.require_context_window(self.plan_models)
        self.store, self.recovery = open_campaign(
            self.root / STATE_FILE,
            self.schema,
            self.manifest,
            self.fingerprint,
            now=self.stamp(),
            max_attempts=cfg.max_attempts,
            allow_growth=cfg.allow_growth,
            shard=self.shard,
        )
        store = self.store
        self.holds = HoldRegistry(store, self.stamp, None if self.on_hold is None else self._announce)
        self.units = UnitHealth(
            store,
            fail_threshold=cfg.unit_fail_threshold,
            pause_s=cfg.unit_pause_s,
            now=lambda: self.clock().timestamp(),
            stamp=self.stamp,
        )
        self.stops = HarnessStops(store, cfg.harness_stop)
        self.reconcile_accounting()
        self.identities = ServedIdentities(store, self.stamp, {m: self.models.resolve(m) for m in self.plan_models})
        self.budget, self.ledger = None, None
        paying = None  # with a budget: the ledger admission's probes are paid from (C26)
        if cfg.budget is not None:
            specs = {m: self.models.resolve(m) for m in self.plan_models}
            # No model is shown held to R yet; an attempt whose usage is unknown is charged the bound its row records
            # it was reserved at dispatch, so the probes find the room the campaign really has (C27).
            early = BudgetGate(store, specs, cfg.budget, self.agent_config.limits)
            early.take_releases(self.stamp())  # a release given for a stopped campaign makes room for its probes
            paying = self._ledger(early)
        held = self._admit_models(paying)  # before the gate: which models are held to the reply maximum (C27)
        if cfg.budget is not None:
            self.budget = BudgetGate(store, specs, cfg.budget, self.agent_config.limits, reply_capped=held)
            self.ledger = self._ledger(self.budget)
        else:
            store.event(self.stamp(), "budget_gate_off", None, "no budget in this campaign: runs are not held for cost")
        self.gate = None
        if self.health_check is not None:
            self.gate = HealthGate(
                store,
                self.health_check,
                self.stamp,
                recheck_s=cfg.recheck_s,
                timeout_s=cfg.health_timeout_s,
                max_defer_s=cfg.max_defer_s,
            )
            self.gate.load()
        self._sweep_leases()
        self.runner = AttemptRunner(
            Parts(
                store=store,
                schema=self.schema,
                settings=cfg,
                executor=self.executor,
                agent_config=self.agent_config,
                models=self.models,
                endpoints=self.endpoints,
                backend_factory=self.backend_factory,
                root=self.root,
                stamp=self.stamp,
                monotonic=self.monotonic,
                units=self.units,
                stops=self.stops,
                identities=self.identities,
                holds=self.holds,
                recheck=self.recheck,
                stop=self.stop,
                exact_stamp=self.exact_stamp,
                health=self.gate,
                leases=self.leases,
                lease_group=self.lease_group,
                admission=self.admission,
                crash_hook=self.crash_hook,
                ledger=self.ledger,
                reserved_capped=self._admitted_capped,
                reply_cap_unheld=self._reply_cap_unheld,
            )
        )

    def _ledger(self, gate: BudgetGate) -> Ledger:
        """A ledger over the gate: a top-up is decided under the dispatcher's locks, in its order, against the runs in
        flight (C26)."""
        assert self.store

        def in_flight() -> list[str]:  # read while the locks are held
            return [m for m, _ in self._inflight.values()]

        def live() -> frozenset[str]:  # the runs a worker is working on; read while the locks are held (C17)
            return frozenset(self._inflight)

        return Ledger(gate, self.store, self.stamp, (self._cv, self._assess), in_flight, live)

    def reconcile_accounting(self) -> None:
        """Derive the harness-error stops and the units' failures from the stored attempts (C04, C10, C15).

        A result committed from its marker at this start never went through the bookkeeping of the process that made
        it, and a record can be older than the attempts. So neither is trusted alone: what the attempts say is added.
        """
        assert self.store
        self.stops.reconcile(self.stamp())
        self.units.reconcile(
            history_from_attempts(
                self.store, lambda model: self.endpoints[model].unit if model in self.endpoints else None
            )
        )

    def _admit_models(self, paying: Ledger | None) -> frozenset[str]:
        """Admit each model's endpoint once (C14). The endpoint checked here is the one every run of the model uses.

        With a budget, a model of the core's own factory is also probed for the reply maximum; return the models the
        probe showed held to it, and mark in the store those it did not (C27). A one-token completion answered with
        more than one output token shows the endpoint ignores max_tokens: the model is marked at once, and the probe of
        the reply maximum is not sent.

        With a budget (`paying`), every send of a probe is paid from it (C26): reserved one request's bound before it
        goes out, charged in the store, and settled to what an answered reply's counts cost. A probe that does not fit
        is not sent, and its model is not admitted, its reason saying the budget is spent. The stub sends nothing an
        endpoint could bill, so its probes are not paid for; a model without a price is not probed at all, since what
        a probe of it costs is unknown, and the budget gate holds it (C12).
        """
        assert self.store
        held = set()
        for model in self.plan_models:
            endpoint, spec = self.endpoints[model], self.models.resolve(model)
            pays = paying if spec.backend != "stub" else None  # the ledger this model's probes are paid from
            if pays is not None and pays.probe_bound(model) is None:
                continue
            backend = self.backend_factory(
                spec, seed=0, limits=self.agent_config.limits, base_url=endpoint.base_url, time_left=None
            )
            metered = None if pays is None else MeteredProbes(backend, pays, model)
            probed = reply_cap.probed(spec, self.backend_factory, self.settings.budget is not None)

            async def check(
                backend: Any = backend, probing: Any = metered or backend, probed: bool = probed
            ) -> tuple[str | None, str | None, str | None]:
                try:
                    reason, reply = await admit_reading(probing)
                    ignored = max_tokens_ignored(reply)  # the one-token reply went past its max_tokens of 1 (C27)
                    asked = reason is None and probed and ignored is None
                    return reason, ignored, await reply_cap_unshown(probing) if asked else None
                finally:
                    await backend.aclose()

            reason, ignored, unshown = asyncio.run(check())
            if ignored is not None and self.settings.budget is not None:  # at once: no later probe can be refused first
                detail = f"{model}: {ignored}; its runs are reserved by its context window"
                reply_cap.mark_unheld(self.store, model, self.stamp(), "reply_cap_not_shown", detail)
            if metered is not None and metered.refusal is not None:  # not sent: no reply says anything of the model
                reason = f"the campaign's budget is spent: {metered.refusal}"
            # a model that declares accepted fingerprints is held to them by its probe's reply (B18): read from what the
            # one-token completion already noted, so nothing more is sent, and only for a model nothing refused before
            reason = reason or self.identities.admit(model, backend, self.stamp())
            if reason is not None:
                self.not_admitted[model] = reason
                self.store.event(self.stamp(), "model_not_admitted", model, f"{model}: {reason}")
            elif probed and unshown is None and ignored is None:
                held.add(model)
            elif probed and unshown is not None:
                detail = f"{model}: {unshown}; its runs are reserved by its context window"
                reply_cap.mark_unheld(self.store, model, self.stamp(), "reply_cap_not_shown", detail)
        return frozenset(held)

    def _sweep_leases(self) -> None:
        """Look for what dead lease holders left (C19); the keys still held back are said to the event log."""
        assert self.store
        if self.leases is None:
            return
        groups = {g for row in self.manifest.rows if (g := self.lease_group(row.key.factors)) is not None}
        held_back = self.leases.sweep_dead_holders(groups)
        if held_back:
            self.store.event(
                self.stamp(),
                "leases_held_back",
                None,
                f"{', '.join(held_back)}: a dead holder's resource is not shown gone; not granted until it is",
            )

    def close(self) -> None:
        """Signal the end, wait for the workers, then stop the gate's thread, close the store and release the lock.

        A worker is never cut off: it ends its current attempt (a run waiting for a lease or a health check gives up at
        once, since it has started nothing), so its result is recorded before the store closes. A run is unfinished
        only if the process dies, and the next open settles that (C04). The wait is the attempt's own: a post-run health
        check in it is bounded by `health_timeout_s`. The lock is released last: while any worker can still write, no
        other dispatcher can start on the store (C07).
        """
        self.stop.set()
        self._wake()
        self._wait_for_workers()
        gate = getattr(self, "gate", None)
        if gate is not None:
            gate.join()
        if self.store is not None:
            self.store.close()
            self.store = None
        self._lock.release()

    def _wait_for_workers(self) -> None:
        """Wait for the dispatch loop, if another thread is inside `run`, and for every worker to end."""
        me = threading.current_thread()
        with self._cv:
            looping = self._run_thread
        if looping is not None and looping is not me:
            looping.join()  # it starts no worker once `stop` is set, and joins those it started
        with self._cv:
            workers = list(self._workers)
        for worker in workers:
            if worker is not me:
                worker.join()

    def __enter__(self) -> "Scheduler":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- dispatch -------------------------------------------------------------------------------------------------

    def _in_use(self) -> int:
        """Count what the global cap counts: runs in flight, leases held back by quarantine, reserved sandboxes."""
        held = self.leases.held_back() if self.leases is not None else 0
        reserved = self.reservations.reserved_count() if self.reservations is not None else 0
        return len(self._inflight) + held + reserved

    def _live_per_unit(self) -> dict[str | None, int]:
        """Count the keys whose resource may still be live by the unit of the run that held each (None: unknown)."""
        counts: dict[str | None, int] = {}
        if self.leases is not None:
            for unit in self.leases.may_be_live():
                counts[unit] = counts.get(unit, 0) + 1
        return counts

    def _assessed(self, flying: list[str], now: str, live: frozenset[str]) -> Any:
        """Assess the budget for the models of the runs in flight (None without a budget). `live` are the keys of the
        runs a worker is working on, read under `_cv` by the caller: a running attempt of any other run lost its worker
        to a crash in our own bookkeeping, and is charged its bound (C17)."""
        if self.budget is None:
            return None
        with self._assess:
            return self.budget.assess(flying, now, exact=self.exact_stamp(), live=live)

    def recheck(self, row: Any) -> Hold | None:
        """Tell why a run that holds its lease may not start after all, or None: the gates dispatch looked at, again.

        A worker calls it once its lease is in hand, because the wait for a lease can be long. The run is not counted
        among the runs in flight for the budget, as at dispatch, so its own bound is reserved once. A run it lets start
        keeps whether this assessment reserved it by the reply maximum, for its spend guard (C27).
        """
        if self.stop.is_set():
            return Hold("shutdown", "the campaign is ending", temporary=True)
        key, model = row["run_key"], row[column_for("model")]
        with self._cv:
            others = [m for k, (m, _) in self._inflight.items() if k != key]
            live = frozenset(self._inflight)
        stopped, stopped_all = self.stops.stopped()
        budget = self._assessed(others, self.stamp(), live)
        hold = (
            self._all_hold(stopped_all)
            or self._model_hold(model, stopped, budget)
            or self._unit_hold(self.endpoints[model].unit)
        )
        if hold is None and budget is not None:
            with self._cv:
                self._reserved_capped[key] = model in budget.capped
        return hold

    def _admitted_capped(self, run_key: str) -> bool | None:
        """Tell whether the assessment that admitted the run reserved it by the reply maximum; None when none did, as
        without a budget (C27). The attempt's row records it as it is; its spend guard takes None as yes."""
        with self._cv:
            return self._reserved_capped.get(run_key)

    def _reply_cap_unheld(self, model: str) -> bool | None:
        """Tell whether the store has taken the model off the reply maximum; None without a budget, which has none to
        hold (C27). It reads the store as it is now, so an attempt's record gets the mark as the loop ended."""
        if self.budget is None:
            return None
        assert self.store
        return reply_cap.is_unheld(reply_cap.unheld(self.store), model)

    def _all_hold(self, stopped_all: bool) -> Hold | None:
        """The hold that applies to every run, if any: a stop on all dispatch, or a closed health gate."""
        if stopped_all:
            return Hold("harness_errors", "dispatch of every model is stopped by harness errors", temporary=False)
        if self.gate is not None and self.gate.blocking:  # a deferral past its bound: it may never end by itself
            why = "the health gate is closed: the platform's check stayed deferred past max_defer_s"
            return Hold("health_gate", why, temporary=False)
        if self.gate is not None and self.gate.closed:
            return Hold("health_gate", "the health gate is closed: no run starts until a check passes", temporary=True)
        return None

    def _model_hold(self, model: str, stopped: frozenset[str], budget: Any) -> Hold | None:
        """The hold that applies to one model's runs, if any."""
        if model in stopped:
            return Hold("harness_errors", f"dispatch of {model} is stopped by harness errors", temporary=False)
        if model in self.not_admitted:
            return Hold("not_admitted", self.not_admitted[model], temporary=False)
        reason = None if budget is None else budget.reason(model)
        return None if reason is None else Hold("budget", reason, temporary=False)

    def _lease_hold(self, factors: Mapping[str, Any]) -> tuple[str, Hold | None] | None:
        """The hold on runs that need a lease when every key of their group is quarantined or held back (C19).

        Return None for a run that needs no lease, else the hold's subject and the hold (None when a key is usable).
        Such a run would be refused at the lease, so it is not started: no attempt is used, and no event is written for
        every pass. The hold ends when a teardown is proven (`settle_quarantines` runs on every pass).
        """
        group = self.lease_group(factors) if self.leases is not None else None
        if group is None or self.leases is None:
            return None
        keys = self.leases.keys_of(group)
        if keys and not set(keys) <= self.leases.unusable():
            return f"lease:{group}", None
        why = "the platform declares no key" if not keys else "every key is quarantined or held back"
        return f"lease:{group}", Hold("lease", f"group {group}: {why}", temporary=False)

    def _unit_hold(self, unit: str) -> Hold | None:
        """The hold on a unit: its pause (C15)."""
        if self.units.ok(unit):
            return None
        until = iso(dt.datetime.fromtimestamp(self.units.paused_until(unit), dt.UTC))
        reason = f"{unit} is paused after repeated failures; only a success clears it"
        return Hold("unit_paused", reason, temporary=True, until=until)

    def next_dispatch(self) -> tuple[Any | None, bool]:
        """Return the first pending run, in manifest order, that can start now, and whether any run is only waiting.

        "Only waiting" means something will end by itself: a run's delay, a unit's pause, a closed gate, a full cap.
        A pending run that is held by a stop, a budget or an unadmitted model is not waiting: someone has to look.
        """
        store = self.store
        assert store is not None
        now, mono = self.stamp(), self.monotonic()
        self.stops.take_releases(now)
        if self.leases is not None:
            self.leases.settle_quarantines()
        stopped, stopped_all = self.stops.stopped()
        budget = self._assessed([m for m, _ in self._inflight.values()], now, frozenset(self._inflight))
        in_use = self._in_use()
        held_in_unit = self._live_per_unit()
        waiting = False
        all_hold = self._all_hold(stopped_all)
        self.holds.note(ALL, all_hold)
        for row in store.pending():
            key, model = row["run_key"], row[column_for("model")]
            if key in self._inflight:
                continue
            if all_hold is not None and not all_hold.temporary:
                continue  # nothing starts until someone looks: no run is waiting for its time
            if self._not_before.get(key, 0.0) > mono:
                waiting = True
                continue
            unit = self.endpoints[model].unit
            model_hold, unit_hold = self._model_hold(model, stopped, budget), self._unit_hold(unit)
            self.holds.note(model, model_hold)
            self.holds.note(f"unit:{unit}", unit_hold)
            leased = self._lease_hold(json.loads(row["factors"]))
            if leased is not None:
                self.holds.note(*leased)
            hold = all_hold or model_hold or unit_hold or (leased[1] if leased else None)
            if hold is not None:
                waiting = waiting or hold.temporary
                continue
            cap = self.settings.unit_caps.get(unit)
            running = sum(1 for _, u in self._inflight.values() if u == unit)
            running += held_in_unit.get(unit, 0) + held_in_unit.get(None, 0)  # a key of unknown unit counts for all
            full = cap is not None and running >= cap
            if in_use >= self.settings.workers or full:
                waiting = True
                continue
            if budget is not None:  # the decision a run begun before its recheck (an outage's) records (C27)
                self._reserved_capped[key] = model in budget.capped
            return row, waiting
        return None, waiting

    def _work(self, row: Any) -> None:
        """One worker: run the attempt; an unexpected exception stops dispatch loudly and leaves the run (C17)."""
        key = row["run_key"]
        try:
            delay = self.runner.work(row)
            if delay:
                self._not_before[key] = self.monotonic() + delay
        except Exception as error:
            # Unexpected (disk full, a database error, an executor that built the wrong record): stop dispatching
            # loudly. A 'running' attempt is left as it is; the next start settles it and runs the run again.
            self._fatal = error
            self.stop.set()
            with contextlib.suppress(Exception):
                assert self.store
                self.store.event(self.stamp(), "dispatch_stopped_by_error", None, f"{key}: {type(error).__name__}")
        finally:
            with self._cv:
                self._inflight.pop(key, None)
                self._reserved_capped.pop(key, None)
                self._cv.notify_all()

    def run(self, max_dispatch: int | None = None) -> RunSummary:
        """Dispatch until no run is pending, or only held runs are, or `stop` is set; join the runs in flight.

        With `exit_when_blocked` it returns as soon as nothing is in flight and every pending run is held by a hold
        that will not end by itself; without it, it waits for someone to release. Raises RuntimeError if a worker
        failed unexpectedly (C17), after the runs in flight have ended.
        """
        store = self.store
        assert store is not None, "call open() first"
        if self.gate is not None:
            self.gate.start(self.stop, on_open=self._wake)
        dispatched = 0
        before = store.q("SELECT COALESCE(MAX(id), 0) AS last FROM attempts")[0]["last"]  # the rows this call begins
        with self._cv:
            self._run_thread = threading.current_thread()
        try:
            while not self.stop.is_set():
                with self._cv:
                    row, waiting = self.next_dispatch()
                    if row is None:
                        if not self._inflight and (
                            not store.pending() or (self.settings.exit_when_blocked and not waiting)
                        ):
                            break
                        self._cv.wait(timeout=self.settings.poll_interval_s)
                        continue
                    model = row[column_for("model")]
                    self._inflight[row["run_key"]] = (model, self.endpoints[model].unit)
                worker = threading.Thread(target=self._work, args=(row,), daemon=True, name=f"run-{row['seq']}")
                with self._cv:
                    self._workers.append(worker)
                worker.start()
                dispatched += 1
                if max_dispatch is not None and dispatched >= max_dispatch:
                    break
        finally:
            with self._cv:
                workers = list(self._workers)
            for worker in workers:
                worker.join()
            with self._cv:
                self._workers.clear()
                self._run_thread = None
        if self._fatal is not None:
            raise RuntimeError(f"campaign stopped by an unexpected error: {self._fatal!r}") from self._fatal
        statuses = {r["status"]: r["n"] for r in store.q("SELECT status, COUNT(*) AS n FROM runs GROUP BY status")}
        log = log_path(self.root)
        begun = store.q("SELECT COUNT(*) AS n FROM attempts WHERE id > ?", (before,))[0]["n"]
        return RunSummary(dispatched, self.holds.current(), statuses, log if log.is_file() else None, begun)

    def _announce(self, subject: str, before: Hold | None, after: Hold | None) -> None:
        """Say, once, that a hold began, changed or ended, with its reason and when dispatch resumes."""
        assert self.on_hold is not None
        if after is None:
            kind = before.kind if before is not None else "a"
            self.on_hold(f"hold on {subject} ended ({kind}); dispatch of its runs resumes")
            return
        self.on_hold(f"hold on {subject} ({after.kind}): {after.reason}; {self._resumes(subject, after)}")

    def _resumes(self, subject: str, hold: Hold) -> str:
        """When dispatch held by `hold` resumes, as far as the scheduler knows."""
        if hold.kind == "unit_paused" and hold.until is not None:
            return f"dispatch on it resumes at {hold.until}, when one run may probe it"
        if hold.kind == "health_gate" and hold.temporary:
            return f"dispatch resumes when a health check passes (checked every {self.settings.recheck_s:g} s)"
        if hold.kind == "health_gate":  # blocking: a deferral past its bound, whose holder may never let go
            return (
                "it may not end by itself: dispatch resumes only when a health check passes (checked every "
                f"{self.settings.recheck_s:g} s), so someone should look at what holds the platform"
            )
        if hold.temporary:
            return "dispatch resumes when it ends by itself"
        return "it does not end by itself: its runs stay pending until someone looks"

    def _wake(self) -> None:
        with self._cv:
            self._cv.notify_all()
