"""C41: the scheduler itself holds a lease to its campaign, its attempt limit and its grant, whoever builds the start.

`study.lease_start` makes the settings of a lease and refuses first, before a store is made. These tests build the
scheduler and its `ShardStart` by hand, as a caller that does not come through the pipeline would, and expect the
scheduler to refuse a lease that does not fit, before it opens the store and pins anything.
"""

import dataclasses
import sqlite3
from pathlib import Path

import pytest
from campaign_support import make_fingerprint, make_manifest, make_schema
from scheduler_support import Factory, FakeExecutor, models_for, seed_store, settings_for

from expcore.campaign.executor import Endpoint
from expcore.campaign.gate import Budget
from expcore.campaign.manifest import Manifest, fingerprint_sha256
from expcore.campaign.run_lease import LeaseError, RunLease
from expcore.campaign.scheduler import Scheduler
from expcore.campaign.shard import usd_of
from expcore.campaign.shard_run import ShardStart

GRANT_MICRO = 40_000_000  # the stub models' one run reserves a few dollars at most at these prices and caps
GRANT = 40.0


def lease_for(fingerprint, manifest, **changes) -> RunLease:
    facts = {
        "run_key": manifest.rows[0].key.text,
        "lease_id": 1,
        "attempt_base": 0,
        "grant_micro_usd": GRANT_MICRO,
        "campaign_sha256": fingerprint_sha256(fingerprint),
        "expires_utc": "2099-01-01T00:00:00Z",
    }
    return RunLease(**(facts | changes))


def scheduler_of(root: Path, *, lease_changes=None, **settings) -> Scheduler:
    """A scheduler over the one run of a lease, built by hand: its start and its settings are the caller's."""
    schema = make_schema()
    plan = {"tasks": ("task-a",), "models": ("m1",), "seeds": (1,)}
    whole = make_manifest(schema, 7, **plan)
    fingerprint = make_fingerprint(schema, task_ids=("task-a",), model_keys=("m1",), seed_ids=(1,))
    lease = lease_for(fingerprint, whole, **(lease_changes or {}))
    seed_store(root, schema)
    executor = FakeExecutor()
    return Scheduler(
        root,
        schema,
        Manifest(whole.order_seed, (whole.rows[0],)),
        fingerprint,
        executor,
        agent_config=executor.config,
        models=models_for("m1"),
        endpoints={"m1": Endpoint("m1", "unit-m1", "https://llm.example.invalid/m1")},
        settings=dataclasses.replace(settings_for(), **settings),
        backend_factory=Factory(),
        shard=ShardStart(lease, {}, {"unit_caps": {"unit-m1": 1}, "budget": None}),
    )


def pinned(root: Path) -> list:
    """What the store holds of a shard or a lease: nothing, when the scheduler refused before it opened the store."""
    connection = sqlite3.connect(root / "state.sqlite")
    try:
        return connection.execute("SELECT key FROM meta WHERE key LIKE 'shard%' OR key LIKE 'lease%'").fetchall()
    finally:
        connection.close()


def test_c41_a_scheduler_of_a_lease_held_to_its_grant_and_campaign_opens(tmp_path):
    """C41: the settings `lease_start` makes (the grant as the budget) and the campaign's own hash open the store, and
    the lease is pinned; this is the control of the refusals below."""
    with scheduler_of(tmp_path, budget=Budget(max_usd=GRANT)):
        assert sorted(key for (key,) in pinned(tmp_path)) == [
            "lease_attempt_base",
            "shard_shares",
            "shard_spec",
            "shard_started_utc",
        ]


def test_c41_a_scheduler_refuses_a_lease_issued_for_another_campaign_before_it_opens_the_store(tmp_path):
    """C41: a campaign that has the leased run key and another fingerprint (a scorer changed, say) has another hash than
    the lease's, so the run is not executed and nothing is pinned."""
    with pytest.raises(LeaseError, match="campaign"):
        scheduler_of(tmp_path, budget=Budget(max_usd=GRANT), lease_changes={"campaign_sha256": "ab" * 32}).open()
    assert pinned(tmp_path) == []


def test_c41_a_scheduler_with_no_budget_does_not_run_a_lease_with_a_grant(tmp_path):
    """C41: settings with no budget leave the gate off, and a priced run could cost more than the grant: refused."""
    with pytest.raises(LeaseError, match="grant"):
        scheduler_of(tmp_path).open()
    assert pinned(tmp_path) == []


@pytest.mark.parametrize(
    "budget",
    [
        Budget(max_usd=GRANT * 2),
        Budget(max_usd=GRANT + 0.000001),  # one micro-dollar above the grant
        Budget(max_usd=GRANT, release_step_usd=1.0, release_file=Path("release.txt")),
    ],
)
def test_c41_a_scheduler_refuses_a_budget_that_can_be_raised_past_the_grant(tmp_path, budget):
    """C41: a ceiling above the grant, or one a release can lift, is not the grant."""
    with pytest.raises(LeaseError, match="grant"):
        scheduler_of(tmp_path, budget=budget).open()
    assert pinned(tmp_path) == []


def test_c41_a_scheduler_refuses_a_lease_whose_run_has_no_attempt_left(tmp_path):
    """C41: a base at the attempt limit (3 here) would run an attempt past it before anything could exhaust the run:
    refused before the store is opened."""
    with pytest.raises(LeaseError, match="no attempt left"):
        scheduler_of(tmp_path, budget=Budget(max_usd=GRANT), lease_changes={"attempt_base": 3}).open()
    assert pinned(tmp_path) == []


def test_c41_a_scheduler_refuses_a_ceiling_a_fraction_of_a_micro_dollar_above_the_grant(tmp_path):
    """C41: a grant of 10 micro-dollars is a ceiling of 0.00001 dollars and no more: the gate would admit a run that
    reserves 10.5 micro-dollars under a ceiling of 0.0000109, so that ceiling is refused, and 0.00001 itself opens."""
    with pytest.raises(LeaseError, match="grant"):
        scheduler_of(tmp_path, budget=Budget(max_usd=0.0000109), lease_changes={"grant_micro_usd": 10}).open()
    assert pinned(tmp_path) == []
    with scheduler_of(tmp_path, budget=Budget(max_usd=0.00001), lease_changes={"grant_micro_usd": 10}):
        assert "shard_spec" in {key for (key,) in pinned(tmp_path)}


def test_c41_a_scheduler_opens_the_ceiling_lease_start_makes_for_a_grant_too_large_for_a_float_to_hold_exactly(
    tmp_path,
):
    """C41: the ceiling `lease_start` makes is the dollars of the grant, the nearest float to the exact amount; for a
    grant of 2**53 - 1 micro-dollars that float is one micro-dollar higher, and the scheduler must not refuse the
    settings that were made for it."""
    grant = 2**53 - 1
    lease_dollars = usd_of(grant)
    assert round(lease_dollars * 1_000_000) == grant + 1
    with scheduler_of(tmp_path, budget=Budget(max_usd=lease_dollars), lease_changes={"grant_micro_usd": grant}):
        assert "shard_spec" in {key for (key,) in pinned(tmp_path)}
