"""Which run a lease takes: the declared set first, then the eligible model with the most remaining expected time,
then that model's first pending run in the seeded order."""

import pytest
from campaign_support import make_manifest, make_schema
from elastic_support import SITE, assert_invariants, completion, finish, join, leased, make, plan

from expcore.campaign.elastic import LedgerRun, Return, SiteReading, Wait, ledger_runs

CORE_FIRST = {"api-none": 0, "mount-read": 1}


def test_c41_the_earliest_set_with_a_pending_run_is_filled_first_and_never_skipped(tmp_path):
    """C41: every run of the core set is leased before any of the next; while the core set's model is at its cap the
    federation waits, though the next set has runs pending."""
    runs = plan(tasks=3, sets=CORE_FIRST)
    ledger, clock = make(tmp_path, runs=runs, caps={"m1": 2})
    for label in ("fed-1", "fed-2", "fed-3"):
        join(ledger, clock, label)
    first = [leased(ledger, label) for label in ("fed-1", "fed-2")]
    assert {lease.condition for lease in first} == {"api-none"}
    waiting = ledger.issue("fed-3")
    assert isinstance(waiting, Wait)
    assert "cap" in waiting.reason
    finish(ledger, first[0])
    third = leased(ledger, "fed-1")
    assert third.condition == "api-none"
    finish(ledger, first[1])
    finish(ledger, third)
    assert leased(ledger, "fed-2").condition == "mount-read"
    assert_invariants(ledger)


def test_c41_a_returned_run_reopens_its_set(tmp_path):
    """C41: set progress counts leases, not results: a core run returned after the next set has begun is leased
    before any further run of the next set."""
    runs = plan(tasks=1, sets=CORE_FIRST)
    ledger, clock = make(tmp_path, runs=runs)
    for label in ("fed-1", "fed-2", "fed-3"):
        join(ledger, clock, label)
    core = leased(ledger, "fed-1")
    assert core.condition == "api-none"
    later = leased(ledger, "fed-2")
    assert later.condition == "mount-read"
    ledger.observe_site(SITE, SiteReading(returns=(Return(core.lease_id, spend_usd=0.0),)))
    again = leased(ledger, "fed-3")
    assert (again.run_key, again.condition) == (core.run_key, "api-none")
    assert_invariants(ledger)


def test_c41_a_run_expired_in_an_earlier_set_reopens_it(tmp_path):
    """C41: an expired lease puts its run back, so its set is the current one again."""
    runs = plan(tasks=1, sets=CORE_FIRST)
    ledger, clock = make(tmp_path, runs=runs)
    for label in ("fed-1", "fed-2", "fed-3"):
        join(ledger, clock, label)
    core = leased(ledger, "fed-1")
    later = leased(ledger, "fed-2")
    ledger.observe_site(SITE, SiteReading(heartbeats={later.lease_id: 1}))
    clock.advance(200)
    ledger.observe_site(SITE, SiteReading(heartbeats={later.lease_id: 2}))
    clock.advance(101)
    ledger.observe_site(SITE, SiteReading(heartbeats={later.lease_id: 3}))
    assert ledger.lease(core.lease_id).state == "expired"
    assert leased(ledger, "fed-3").run_key == core.run_key


def test_c41_the_model_with_the_most_remaining_expected_time_is_leased_first(tmp_path):
    """C41: a model's remaining time is its pending runs' estimates plus what is left of its runs in flight; the
    model with the most gets the idle federation, and time spent in flight lowers it."""
    runs = plan(models=("m1", "m2"), tasks=3, conditions=("api-none",))[:5]  # m1 has 3 runs, m2 has 2
    ledger, clock = make(tmp_path, runs=runs)
    for label in ("fed-1", "fed-2", "fed-3"):
        join(ledger, clock, label)
    assert leased(ledger, "fed-1").model == "m1"  # 1800 s against 1200 s
    assert leased(ledger, "fed-2").model == "m1"  # 1200 s pending and 600 s in flight, against 1200 s
    clock.advance(500)
    assert leased(ledger, "fed-3").model == "m2"  # 600 s pending and twice 100 s in flight, against 1200 s


def test_c41_a_model_at_its_cap_gives_way_to_the_next(tmp_path):
    """C41: the most lagging model at its cap is passed over for the next one, which is not a later set's."""
    runs = plan(models=("m1", "m2"), tasks=3, conditions=("api-none",))[:5]
    ledger, clock = make(tmp_path, runs=runs, caps={"m1": 1})
    for label in ("fed-1", "fed-2", "fed-3"):
        join(ledger, clock, label)
    assert [leased(ledger, label).model for label in ("fed-1", "fed-2", "fed-3")] == ["m1", "m2", "m2"]
    assert_invariants(ledger)


def test_c41_within_a_model_the_first_pending_run_in_the_seeded_order_is_taken_whatever_its_condition(tmp_path):
    """C41: the run is the model's lowest seq, so conditions interleave as the manifest's seeded order does."""
    runs = plan(tasks=2)
    ledger, clock = make(tmp_path, runs=runs)
    labels = ("fed-1", "fed-2", "fed-3", "fed-4")
    for label in labels:
        join(ledger, clock, label)
    assert [leased(ledger, label).run_key for label in labels] == [run.run_key for run in runs]


def test_c41_estimates_start_at_the_prior_counted_as_three_runs_and_take_each_counted_lease(tmp_path):
    """C41: a cell's expected time is the prior until a lease of it counts, then the mean of the prior's three
    pseudo-runs and the counted lease times, the federation's reset included; a superseded lease is not counted."""
    ledger, clock = make(tmp_path, runs=plan(tasks=2, conditions=("api-none",)), prior_run_s=600.0)
    join(ledger, clock, "fed-1")
    assert ledger.estimate_s("m1", "api-none") == 600.0
    lease = leased(ledger, "fed-1")
    clock.advance(200)
    ledger.observe_site(SITE, SiteReading(completions=(completion(lease),), heartbeats={}))
    assert ledger.estimate_s("m1", "api-none") == 600.0  # read, not yet verified
    clock.advance(1000)
    ledger.verify_archive(lease.lease_id, completion(lease).archive_sha256)
    assert ledger.estimate_s("m1", "api-none") == pytest.approx((3 * 600.0 + 200.0) / 4)


def test_c41_ledger_runs_takes_each_runs_model_condition_and_declared_set_from_the_manifest():
    """C41: the ledger's runs come from the manifest in its seeded order; a run is in the first declared set whose
    filters it matches, and a run no set matches is refused."""
    schema = make_schema()
    manifest = make_manifest(schema, models=("m1", "m2"))
    sets = ({"condition": ["api-none"], "model": ["m1"]}, {})
    runs = ledger_runs(manifest, schema, sets)
    assert [run.run_key for run in runs] == [row.key.text for row in manifest.rows]
    assert [run.seq for run in runs] == [row.seq for row in manifest.rows]
    for run, row in zip(runs, manifest.rows, strict=True):
        condition = schema.condition_of(row.key)
        assert (run.model, run.condition) == (row.key.factors["model"], condition)
        assert run.set_index == (0 if (condition, run.model) == ("api-none", "m1") else 1)
    assert all(isinstance(run, LedgerRun) for run in runs)
    with pytest.raises(ValueError, match="no declared set"):
        ledger_runs(manifest, schema, ({"condition": ["api-none"]},))
