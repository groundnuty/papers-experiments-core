"""The scheduler kernel: manifest order, the order of an attempt, and the regressions it carries."""

import subprocess
import sys
from pathlib import Path

import pytest
from campaign_support import make_manifest, make_schema, treatment_schema
from scheduler_crash import KILLED, PLAN
from scheduler_support import Factory, FakeExecutor, attempts_of, build, event_kinds, key_of, settings_for

from expcore.campaign.scheduler import CampaignLocked
from expcore.errors import InfraError
from expcore.models.adapters import ProbeReply

CRASH = Path(__file__).resolve().parents[1] / "support" / "scheduler_crash.py"
SMALL = {"tasks": ("task-a",), "seeds": (1,)}  # three runs, one per condition


def keys(schema=None, plan=SMALL):
    return [row.key.text for row in make_manifest(schema or make_schema(), **plan).rows]


def test_the_pending_runs_are_dispatched_in_the_manifests_seeded_order_and_all_finish(tmp_path):
    """The scheduler runs the store's pending runs in the manifest's order, each through the executor, to done."""
    executor = FakeExecutor()
    with build(tmp_path, executor=executor, plan=SMALL) as scheduler:
        summary = scheduler.run()
        assert [r.run_key for r in executor.requests] == keys()
        assert summary.statuses == {"done": 3}
        assert summary.dispatched == summary.attempts == 3
        assert all(attempts_of(scheduler.store, k) == [("done", 0, 1)] for k in keys())


def test_each_request_carries_what_the_study_needs_and_the_backend_is_closed(tmp_path):
    """The request has the key, the attempt number, the run directory, the endpoint and an unstarted timer."""
    executor, factory = FakeExecutor(), Factory()
    with build(tmp_path, executor=executor, factory=factory, plan=SMALL) as scheduler:
        scheduler.run(max_dispatch=1)
        [request] = executor.requests
        assert request.identity.attempt == 1
        assert request.run_dir.is_dir()
        assert request.endpoint.unit == "unit-m1"
        assert request.treatment_kinds == ()
        assert request.lease is None
        assert request.backend.closed
        assert len(factory.built) == 2  # one for admission, one for the run


def test_c07_a_second_dispatcher_on_the_same_store_exits(tmp_path):
    """C07: while one dispatcher has the store open, a second refuses to start, and the first is not disturbed."""
    with build(tmp_path, plan=SMALL) as first:
        with pytest.raises(CampaignLocked, match="one dispatcher per store"):
            build(tmp_path, plan=SMALL).open()
        assert first.run().statuses == {"done": 3}
    with build(tmp_path, plan=SMALL):  # the lock was released with the first
        pass


def test_c14_the_endpoint_the_campaign_admitted_is_the_one_every_run_uses(tmp_path):
    """C14: an override of the base URL is the URL admission checked, the request carries and the record names."""
    url = "https://override.example.invalid/v1"
    executor, factory = FakeExecutor(), Factory()
    with build(tmp_path, executor=executor, factory=factory, base_urls={"m1": url}, plan=SMALL) as scheduler:
        scheduler.run()
    assert {call[2] for call in factory.calls} == {url}  # admission and all three runs
    assert {r.endpoint.base_url for r in executor.requests} == {url}
    assert {record.route["base_url"] for record in executor.records} == {url}


def test_c14_a_model_whose_endpoint_is_not_admitted_is_held_and_no_run_starts(tmp_path):
    """C14 and B07: a model that fails its one-token completion is not dispatched, and the hold says why."""
    factory = Factory(refuse={"m1": InfraError("endpoint", "down")})
    executor = FakeExecutor()
    with build(tmp_path, executor=executor, factory=factory, plan=SMALL) as scheduler:
        summary = scheduler.run()
        assert executor.requests == []
        assert summary.holds["m1"].kind == "not_admitted"
        assert "one-token completion failed" in summary.holds["m1"].reason
        assert summary.statuses == {"pending": 3}
        assert "model_not_admitted" in event_kinds(scheduler.store)


def test_b07_a_model_whose_endpoint_omits_usage_is_held_as_not_admitted_and_no_run_starts(tmp_path):
    """B07: the token budget binds a run through the counts its replies report, so a model whose one-token completion
    reports none is not dispatched, budget or none, and the hold says why."""

    class Silent(Factory):
        def __call__(self, spec, **options):
            backend = super().__call__(spec, **options)

            async def probe():
                return ProbeReply(1, None, None)

            backend.probe = probe
            return backend

    executor = FakeExecutor()
    with build(tmp_path, executor=executor, factory=Silent(), plan=SMALL) as scheduler:
        summary = scheduler.run()
        assert executor.requests == []
        assert summary.holds["m1"].kind == "not_admitted"
        assert "reported no input and no output token count" in summary.holds["m1"].reason
        assert "model_not_admitted" in event_kinds(scheduler.store)


def test_c03_an_infrastructure_failure_is_run_again_and_charged_until_the_limit(tmp_path):
    """C03: an infrastructure attempt is charged and the run goes again, as the same run from the same row and seed."""
    schema = make_schema()
    retry, doomed = key_of(schema, "mount-read"), key_of(schema, "api-none")
    behaviours = {
        retry: [("infra", "sandbox")],
        doomed: [("infra", "sandbox"), ("raise_infra", "sandbox"), ("infra", "sandbox")],
    }
    executor = FakeExecutor(behaviours)
    with build(tmp_path, executor=executor, settings=settings_for(max_attempts=2), plan=SMALL) as scheduler:
        summary = scheduler.run()
        assert attempts_of(scheduler.store, retry) == [("infra_fail", 1, 1), ("done", 0, 2)]
        assert attempts_of(scheduler.store, doomed) == [("infra_fail", 1, 1), ("infra_fail", 1, 2)]
        assert summary.statuses == {"done": 2, "infra_exhausted": 1}
        assert [r.identity.attempt for r in executor.requests if r.run_key == retry] == [1, 2]
        assert executor.calls_of(doomed) == 2  # the limit was two: no third attempt


def test_a_treatment_failure_is_final_and_scored_and_never_run_again_nor_a_unit_failure(tmp_path):
    """The treatment rule: infrastructure of a declared kind under that condition is a scored final attempt."""
    schema = treatment_schema(("endpoint",), "api-none")
    treated = key_of(schema, "api-none")
    executor = FakeExecutor({treated: [("infra_after_turn", "endpoint")]})
    settings = settings_for(unit_fail_threshold=1, identity_kind="knowledge")  # one counted failure pauses at once
    with build(tmp_path, schema=schema, executor=executor, settings=settings, plan=SMALL) as scheduler:
        summary = scheduler.run()
        store = scheduler.store
        assert summary.statuses == {"done": 3}
        assert attempts_of(store, treated) == [("done", 0, 1)]
        row = store.q("SELECT treatment, termination, infra_kind FROM attempts WHERE run_key=?", (treated,))[0]
        assert (row["treatment"], row["termination"], row["infra_kind"]) == (1, "infrastructure", "endpoint")
        assert executor.calls_of(treated) == 1  # scored, never run again
        request = next(r for r in executor.requests if r.run_key == treated)
        assert request.treatment_kinds == ("endpoint",)
        assert not [e for e in event_kinds(store) if e.startswith("unit_")]  # the outage is the manipulation
        assert scheduler.units.status() == {}  # no failure was counted, so the unit has no state at all


def test_c01_a_treatment_kind_failure_before_the_first_turn_is_set_aside_and_run_again(tmp_path):
    """C01: the same declared kind, failing before the model's first turn, is not the manipulation: the attempt is set
    aside and charged like any infrastructure failure, and the run goes again and finishes."""
    schema = treatment_schema(("endpoint",), "api-none")
    early = key_of(schema, "api-none")
    executor = FakeExecutor({early: [("infra", "endpoint")]})
    settings = settings_for(unit_fail_threshold=5, identity_kind="knowledge")
    with build(tmp_path, schema=schema, executor=executor, settings=settings, plan=SMALL) as scheduler:
        summary = scheduler.run()
        store = scheduler.store
        assert summary.statuses == {"done": 3}
        assert attempts_of(store, early) == [("infra_fail", 1, 1), ("done", 0, 2)]
        first = store.q("SELECT treatment, infra_kind FROM attempts WHERE run_key=? ORDER BY id", (early,))[0]
        assert (first["treatment"], first["infra_kind"]) == (0, "endpoint")
        assert executor.calls_of(early) == 2
        assert executor.records[0].outcome["treatment"] is False  # the record agrees with the store


def test_the_health_and_identity_kinds_may_not_be_a_treatment_kind(tmp_path):
    """A failed health check or a changed endpoint must never be scored as a condition's treatment: refused at start."""
    schema = treatment_schema(("sandbox",), "api-none")
    with pytest.raises(ValueError, match="health_kind 'sandbox' is the treatment of condition 'api-none'"):
        build(tmp_path, schema=schema)
    with pytest.raises(ValueError, match="identity_kind 'endpoint'"):
        build(
            tmp_path, schema=treatment_schema(("endpoint",), "api-none"), settings=settings_for(health_kind="knowledge")
        )


def test_c17_an_unexpected_worker_exception_stops_dispatch_loudly_and_leaves_the_run_for_recovery(tmp_path):
    """C17: a raising worker stops dispatch and leaves its run for recovery, which runs it again under its number."""

    def disk_full(point, run_key):
        if point == "before_marker":
            raise OSError("no space left on device")

    first = keys()[0]
    executor = FakeExecutor()
    with build(tmp_path, executor=executor, plan=SMALL, crash_hook=disk_full) as scheduler:
        with pytest.raises(RuntimeError, match="stopped by an unexpected error"):
            scheduler.run()
        assert [r.run_key for r in executor.requests] == [first]  # nothing else was dispatched
        assert attempts_of(scheduler.store, first) == [("running", 0, 1)]
        assert "dispatch_stopped_by_error" in event_kinds(scheduler.store)
    again = FakeExecutor()
    with build(tmp_path, executor=again, plan=SMALL) as scheduler:
        assert scheduler.recovery.interrupted == 1
        assert scheduler.run().statuses == {"done": 3}
        assert attempts_of(scheduler.store, first) == [("interrupted", 0, 1), ("done", 0, 1)]


def test_c10_a_harness_defect_is_neither_infrastructure_nor_an_outcome_and_runs_again_after_the_fix(tmp_path):
    """C10: a crash in our code is set aside uncharged; the next start sets the run pending again from the same row."""
    first = keys()[0]
    with build(tmp_path, executor=FakeExecutor({first: [("defect",)]}), plan=SMALL) as scheduler:
        assert scheduler.run().statuses == {"done": 2, "harness_error": 1}
        assert attempts_of(scheduler.store, first) == [("harness_error", 0, 1)]
        assert "harness_error" in event_kinds(scheduler.store)
    after_the_fix = FakeExecutor()
    with build(tmp_path, executor=after_the_fix, plan=SMALL) as scheduler:
        assert scheduler.recovery.requeued == 1
        assert scheduler.run().statuses == {"done": 3}
        assert attempts_of(scheduler.store, first) == [("harness_error", 0, 1), ("done", 0, 1)]
    assert [r.run_key for r in after_the_fix.requests] == [first]  # only the run that failed, with its own seed
    assert after_the_fix.requests[0].identity.factors["seed"] == 1


@pytest.mark.parametrize("point", ["before_marker", "after_marker", "after_metadata"])
def test_c04_a_campaign_killed_at_any_point_of_an_attempt_resumes_cleanly(tmp_path, point):
    """C04: kill the dispatcher in the middle of the first attempt; the next one settles it and finishes the plan."""
    done = subprocess.run([sys.executable, str(CRASH), str(tmp_path), point], capture_output=True, text=True)
    assert done.returncode == KILLED, done.stderr
    first = keys(plan=PLAN)[0]
    executor = FakeExecutor()
    with build(tmp_path, executor=executor, plan=PLAN) as scheduler:
        recovery = scheduler.recovery
        scheduler.run()
        states = attempts_of(scheduler.store, first)
        statuses = {r["status"] for r in scheduler.store.q("SELECT status FROM runs")}
    assert statuses == {"done"}
    expected = {
        "before_marker": ([("interrupted", 0, 1), ("done", 0, 1)], (0, 1), 3),
        "after_marker": ([("done", 0, 1)], (1, 0), 2),  # committed from its marker: never run again
        "after_metadata": ([("done", 0, 1)], (0, 0), 2),  # nothing to repair
    }[point]
    assert states == expected[0]
    assert (recovery.committed, recovery.interrupted) == expected[1]
    assert len(executor.requests) == expected[2]
