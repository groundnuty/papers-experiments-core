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
  a unit's pause (C15).
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

from expcore.campaign.attempt import AttemptRunner, Parts, RunAdmission
from expcore.campaign.executor import BackendFactory, Endpoint, RunExecutor, default_backend_factory
from expcore.campaign.factors import FactorSchema
from expcore.campaign.gate import BudgetGate
from expcore.campaign.harness_stop import HarnessStops
from expcore.campaign.healthgate import HealthCheck, HealthGate
from expcore.campaign.holds import ALL, Hold, HoldRegistry
from expcore.campaign.identity import ServedIdentities
from expcore.campaign.lease import LeasePool
from expcore.campaign.lock import CampaignLocked, DispatchLock
from expcore.campaign.manifest import Manifest
from expcore.campaign.recovery import Recovery
from expcore.campaign.reservation import Reservations
from expcore.campaign.resume import open_campaign
from expcore.campaign.settings import SchedulerConfig
from expcore.campaign.store import Store, column_for
from expcore.campaign.units import UnitHealth, history_from_attempts
from expcore.config.models import ModelTable
from expcore.loop.config import AgentConfig
from expcore.models.admission import admit

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
    """How a call to `run` ended: runs dispatched, the holds still in force, and the runs by status."""

    dispatched: int
    holds: Mapping[str, Hold] = field(default_factory=dict)
    statuses: Mapping[str, int] = field(default_factory=dict)


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
    ):
        self.root, self.schema, self.manifest, self.fingerprint = Path(root), schema, manifest, fingerprint
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
        self.store, self.recovery = open_campaign(
            self.root / STATE_FILE,
            self.schema,
            self.manifest,
            self.fingerprint,
            now=self.stamp(),
            max_attempts=cfg.max_attempts,
            allow_growth=cfg.allow_growth,
        )
        store = self.store
        self.holds = HoldRegistry(store, self.stamp)
        self.units = UnitHealth(
            store,
            fail_threshold=cfg.unit_fail_threshold,
            pause_s=cfg.unit_pause_s,
            now=lambda: self.clock().timestamp(),
            stamp=self.stamp,
        )
        self.stops = HarnessStops(store, cfg.harness_stop)
        self.reconcile_accounting()
        self.identities = ServedIdentities(store, self.stamp)
        self.budget = None
        if cfg.budget is not None:
            specs = {m: self.models.resolve(m) for m in self.plan_models}
            self.budget = BudgetGate(store, specs, cfg.budget, self.agent_config.limits.token_budget)
        else:
            store.event(self.stamp(), "budget_gate_off", None, "no budget in this campaign: runs are not held for cost")
        self.gate = None
        if self.health_check is not None:
            self.gate = HealthGate(
                store, self.health_check, self.stamp, recheck_s=cfg.recheck_s, timeout_s=cfg.health_timeout_s
            )
            self.gate.load()
        self._admit_models()
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
            )
        )

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

    def _admit_models(self) -> None:
        """Admit each model's endpoint once (C14). The endpoint checked here is the one every run of the model uses."""
        assert self.store
        for model in self.plan_models:
            endpoint = self.endpoints[model]
            backend = self.backend_factory(
                self.models.resolve(model),
                seed=0,
                limits=self.agent_config.limits,
                base_url=endpoint.base_url,
                time_left=None,
            )

            async def check(backend: Any = backend) -> str | None:
                try:
                    return await admit(backend)
                finally:
                    await backend.aclose()

            reason = asyncio.run(check())
            if reason is not None:
                self.not_admitted[model] = reason
                self.store.event(self.stamp(), "model_not_admitted", model, f"{model}: {reason}")

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

    def _assessed(self, flying: list[str], now: str) -> Any:
        """Assess the budget for the models of the runs in flight (None without a budget)."""
        if self.budget is None:
            return None
        with self._assess:
            return self.budget.assess(flying, now, exact=self.exact_stamp())

    def recheck(self, row: Any) -> Hold | None:
        """Tell why a run that holds its lease may not start after all, or None: the gates dispatch looked at, again.

        A worker calls it once its lease is in hand, because the wait for a lease can be long. The run is not counted
        among the runs in flight for the budget, as at dispatch, so its own bound is reserved once.
        """
        if self.stop.is_set():
            return Hold("shutdown", "the campaign is ending", temporary=True)
        key, model = row["run_key"], row[column_for("model")]
        with self._cv:
            others = [m for k, (m, _) in self._inflight.items() if k != key]
        stopped, stopped_all = self.stops.stopped()
        budget = self._assessed(others, self.stamp())
        return (
            self._all_hold(stopped_all)
            or self._model_hold(model, stopped, budget)
            or self._unit_hold(self.endpoints[model].unit)
        )

    def _all_hold(self, stopped_all: bool) -> Hold | None:
        """The hold that applies to every run, if any: a stop on all dispatch, or a closed health gate."""
        if stopped_all:
            return Hold("harness_errors", "dispatch of every model is stopped by harness errors", temporary=False)
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
        return Hold(
            "unit_paused", f"{unit} is paused after repeated failures; only a success clears it", temporary=True
        )

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
        budget = self._assessed([m for m, _ in self._inflight.values()], now)
        in_use = self._in_use()
        held_in_unit = self._live_per_unit()
        waiting = False
        all_hold = self._all_hold(stopped_all)
        self.holds.note(ALL, all_hold)
        for row in store.pending():
            key, model = row["run_key"], row[column_for("model")]
            if key in self._inflight:
                continue
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
        return RunSummary(dispatched, self.holds.current(), statuses)

    def _wake(self) -> None:
        with self._cv:
            self._cv.notify_all()
