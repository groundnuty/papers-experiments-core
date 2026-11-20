"""C64: a lease's store holds one run, so its budget gate counts that run once and never stops for a second one.

The grant of a lease is one run's reservation and two admission probes (`lease_grant_usd`). The dispatcher assesses the
budget on every pass, and while the run is in flight that assessment used to ask whether ANOTHER run could start:
the run in flight was counted as reserved and again as the next run, which is more than the grant, so a stop was
recorded for a run the store can never start (a `budget_hold` and a `budget_hold_over` event, once per pass).
"""

import asyncio
import json
import threading
import time
from pathlib import Path

from campaign_support import make_fingerprint, make_manifest, make_schema
from scheduler_support import Factory, FakeExecutor, event_kinds, models_for, seed_store, settings_for

from expcore.campaign.elastic.config import lease_grant_usd
from expcore.campaign.executor import Endpoint
from expcore.campaign.gate import HOLDS_KEY, Budget
from expcore.campaign.manifest import Manifest, fingerprint_sha256
from expcore.campaign.run_lease import RunLease
from expcore.campaign.scheduler import Scheduler
from expcore.campaign.shard import usd_of
from expcore.campaign.shard_run import ShardStart

MICRO = 1_000_000


class HeldInFlight(FakeExecutor):
    """An executor whose run waits, in flight, until the dispatcher has assessed the budget with that run in flight."""

    def __init__(self) -> None:
        super().__init__()
        self.assessed_in_flight = threading.Event()

    async def __call__(self, request):
        deadline = time.monotonic() + 30
        while not self.assessed_in_flight.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return await super().__call__(request)


def leased_scheduler(root: Path, executor: HeldInFlight) -> Scheduler:
    """A scheduler over the one run of a lease whose grant is the one the ledger issues for the stub model."""
    schema = make_schema()
    plan = {"tasks": ("task-a",), "models": ("m1",), "seeds": (1,)}
    whole = make_manifest(schema, 7, **plan)
    fingerprint = make_fingerprint(schema, task_ids=("task-a",), model_keys=("m1",), seed_ids=(1,))
    models = models_for("m1")
    grant_micro = round(lease_grant_usd(models.resolve("m1"), executor.config.limits) * MICRO)
    lease = RunLease(whole.rows[0].key.text, 1, 0, grant_micro, fingerprint_sha256(fingerprint), "2099-01-01T00:00:00Z")
    seed_store(root, schema)
    return Scheduler(
        root,
        schema,
        Manifest(whole.order_seed, (whole.rows[0],)),
        fingerprint,
        executor,
        agent_config=executor.config,
        models=models,
        endpoints={"m1": Endpoint("m1", "unit-m1", "https://llm.example.invalid/m1")},
        settings=settings_for(budget=Budget(max_usd=usd_of(grant_micro)), unit_caps={"unit-m1": 1}),
        backend_factory=Factory(),
        shard=ShardStart(lease, {}, {"unit_caps": {"unit-m1": 1}, "budget": None}),
    )


def note_assessments_in_flight(scheduler: Scheduler, executor: HeldInFlight) -> list[list[str]]:
    """Wrap the budget's assessment: say, once it has returned, that one was made with a run in flight."""
    real, seen = scheduler.budget.assess, []

    def assess(in_flight, *args, **kwargs):
        state = real(in_flight, *args, **kwargs)
        if in_flight:
            seen.append(list(in_flight))
            executor.assessed_in_flight.set()
        return state

    scheduler.budget.assess = assess
    return seen


def test_c64_a_lease_whose_grant_covers_one_reservation_runs_its_run_with_no_hold_and_no_stop(tmp_path):
    """C64: the grant covers one reservation and not two (2.22 USD against 1.74 reserved); the dispatcher assesses
    while the run is in flight; the run is admitted and finishes, and no stop was recorded, begun or ended."""
    executor = HeldInFlight()
    with leased_scheduler(tmp_path, executor) as scheduler:
        assessed = note_assessments_in_flight(scheduler, executor)
        summary = scheduler.run()
        kinds = event_kinds(scheduler.store)
        stops = json.loads(scheduler.store.get_meta(HOLDS_KEY) or "{}")
    assert assessed, "the dispatcher never assessed the budget with the run in flight"
    assert summary.statuses == {"done": 1}
    assert len(executor.requests) == 1
    assert dict(summary.holds) == {}
    assert [kind for kind in kinds if kind.startswith("budget_hold")] == []
    assert stops == {}
