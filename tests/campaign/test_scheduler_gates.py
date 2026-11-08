"""The gates around an attempt, through the scheduler: harness stops, health, budgets, units, identity, leases."""

import json
import os

import pytest
from campaign_support import NOW, make_schema
from lease_support import Clock, Probe
from scheduler_support import FakeExecutor, attempts_of, build, event_kinds, key_of, models_for, settings_for

from expcore.campaign.gate import Budget
from expcore.campaign.harness_stop import HarnessStopConfig
from expcore.campaign.healthgate import META as HEALTH_META
from expcore.campaign.healthgate import Health, parse
from expcore.campaign.lease import LeasePool
from expcore.campaign.store import Store
from expcore.config.loading import ConfigError

SMALL = {"tasks": ("task-a",), "seeds": (1,)}
SCHEMA = make_schema()


class Healthy:
    """A health check that fails for its first `down` calls and passes after; `raises` makes it raise instead."""

    def __init__(self, down=0, raises=False):
        self.down, self.raises, self.calls = down, raises, 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.down:
            if self.raises:
                raise OSError("cannot reach the platform")
            return Health(False, "platform unreachable")
        return Health(True, "fine")


def order(plan=SMALL, schema=SCHEMA):
    from campaign_support import make_manifest

    return [row.key.text for row in make_manifest(schema, **plan).rows]


# --- C10: harness errors stop a model, then all dispatch ----------------------------------------


def test_c10_three_harness_errors_of_a_kind_for_one_model_stop_its_dispatch(tmp_path):
    """C10: three harness errors for one model stop its dispatch; the rest of the plan stays pending."""
    plan = {"tasks": ("task-a", "task-b"), "seeds": (1, 2)}
    first = order(plan)[:3]
    executor = FakeExecutor({k: [("defect",)] for k in first})
    with build(tmp_path, executor=executor, plan=plan) as scheduler:
        summary = scheduler.run()
        assert summary.statuses == {"harness_error": 3, "pending": 9}
        assert executor.calls_of(first[0]) == 1
        assert len(executor.requests) == 3
        assert summary.holds["m1"].kind == "harness_errors"
        assert "dispatch_stopped" not in event_kinds(scheduler.store)  # the stop is its own event
        assert "harness_error_stop" in event_kinds(scheduler.store)


def test_c10_one_kind_across_three_models_stops_all_dispatch_and_records_it(tmp_path):
    """C10: a wider spread stops all dispatch, recorded as an event; the runs not yet started stay pending."""
    models = ("m1", "m2", "m3")
    plan = {"tasks": ("task-a",), "seeds": (1,), "models": models}
    broken = {key_of(SCHEMA, "mount-read", model=m): [("defect",)] for m in models}
    executor = FakeExecutor(broken)
    with build(tmp_path, executor=executor, plan=plan) as scheduler:
        summary = scheduler.run()
        assert summary.holds["all"].kind == "harness_errors"
        assert summary.statuses["harness_error"] == 3
        assert summary.statuses["pending"] > 0
        assert "harness_error_stop_all" in event_kinds(scheduler.store)


def test_c13_a_release_older_than_the_harness_stop_resumes_nothing_and_a_newer_one_resumes_once(tmp_path):
    """C13: a stale release file does not end a stop; one written after it does, once, and is used up."""
    plan = {"tasks": ("task-a", "task-b"), "seeds": (1, 2)}
    first = order(plan)[:3]
    release = tmp_path / "release.txt"
    release.write_text("m1\n", encoding="utf-8")
    os.utime(release, (1_000_000, 1_000_000))  # written long before the stop
    stop = HarnessStopConfig(release_file=release)
    with build(
        tmp_path / "c",
        executor=FakeExecutor({k: [("defect",)] for k in first}),
        plan=plan,
        settings=settings_for(harness_stop=stop),
    ) as scheduler:
        assert scheduler.run().holds["m1"].kind == "harness_errors"
        assert release.exists()  # the stale file ended nothing
        release.write_text("m1\n", encoding="utf-8")  # now newer than the stop
        summary = scheduler.run()
        assert not release.exists()  # used up
        assert summary.statuses.get("pending", 0) == 0  # dispatch resumed and finished the plan
        assert summary.statuses["done"] == 9


# --- C11: health -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raises", [False, True])
def test_c11_an_unhealthy_or_unreadable_check_holds_all_dispatch_for_one_attempt_per_outage(tmp_path, raises):
    """C11: a failed pre-run check, however it fails, charges its run one attempt and holds dispatch till one passes."""
    health = Healthy(down=3, raises=raises)
    executor = FakeExecutor()
    settings = settings_for(recheck_s=0.01, health_timeout_s=5)
    with build(tmp_path, executor=executor, settings=settings, health=health, plan=SMALL) as scheduler:
        summary = scheduler.run()
        first = order()[0]
        assert attempts_of(scheduler.store, first) == [("infra_fail", 1, 1), ("done", 0, 2)]
        assert summary.statuses == {"done": 3}
        [outage] = parse(scheduler.store.get_meta(HEALTH_META))
        assert (outage["how"], outage["checks"]) == ("check_passed", 3)  # two failed rechecks, then the passing one
        kinds = event_kinds(scheduler.store)
        assert kinds.count("health_gate_closed") == 1
        assert "health_gate_opened" in kinds
        assert executor.calls_of(first) == 1  # the run that found the outage was not executed for it


def test_c11_an_unreadable_gate_state_at_restart_opens_closed_and_blocks_dispatch_until_a_check_passes(tmp_path):
    """C11: the record of outages cannot be read, so the gate starts closed; the first passing check opens it."""
    from scheduler_support import seed_store

    seed_store(tmp_path, SCHEMA)
    store = Store(tmp_path / "state.sqlite", SCHEMA)
    store.put_meta(HEALTH_META, "{not json", NOW)
    store.close()
    health = Healthy(down=0)
    with build(tmp_path, health=health, settings=settings_for(recheck_s=0.01), plan=SMALL) as scheduler:
        assert scheduler.gate.closed
        assert scheduler.run().statuses == {"done": 3}
        assert "health_gate_record_unreadable" in event_kinds(scheduler.store)
        assert "health_gate_opened" in event_kinds(scheduler.store)


# --- C12 and C13: the budget ------------------------------------------------------------------------------------------


def test_c12_a_model_without_a_price_is_held_at_the_budget_gate_and_nothing_runs(tmp_path):
    """C12: a model whose price is unknown is held as if over budget; an executor is never called."""
    executor = FakeExecutor()
    settings = settings_for(budget=Budget(max_usd=1000.0), refuse_unpriced=False)
    unpriced = models_for("m1", price=None)
    with build(tmp_path, executor=executor, settings=settings, models=unpriced, plan=SMALL) as scheduler:
        summary = scheduler.run()
        assert executor.requests == []
        assert summary.holds["m1"].kind == "budget"
        assert "no price" in summary.holds["m1"].reason


def test_c12_a_study_run_with_an_unpriced_model_is_refused_at_the_start_unless_it_is_marked_free(tmp_path):
    """C12: refuse a study run with unpriced models unless they are explicitly marked free."""
    settings = settings_for(budget=Budget(max_usd=1000.0))
    with pytest.raises(ConfigError, match="no price for model"):
        build(tmp_path / "a", settings=settings, models=models_for("m1", price=None), plan=SMALL).open()
    with build(tmp_path / "b", settings=settings, models=models_for("m1", price=None, free=True), plan=SMALL) as free:
        assert free.run().statuses == {"done": 3}


def test_c12_and_c13_budgets_are_enforced_and_a_release_lets_the_campaign_go_on_by_its_step(tmp_path):
    """C12 and C13: runs stop being dispatched at the budget; a release written after the stop adds its step, once."""
    plan = {"tasks": ("task-a", "task-b"), "seeds": (1,)}  # six runs, each about 1.13 USD of output
    release = tmp_path / "release.txt"
    budget = Budget(max_usd=3.0, release_step_usd=3.5, release_file=release)
    executor = FakeExecutor(default="heavy")
    with build(tmp_path / "c", executor=executor, settings=settings_for(budget=budget), plan=plan) as scheduler:
        summary = scheduler.run()
        assert summary.statuses == {"done": 2, "pending": 4}  # a third would be expected to pass 3 USD
        assert summary.holds["m1"].kind == "budget"
        assert "over the ceiling of 3.00 USD" in summary.holds["m1"].reason
        release.write_text("ALL\n", encoding="utf-8")  # after the stop
        summary = scheduler.run()
        assert not release.exists()
        assert summary.statuses == {"done": 5, "pending": 1}  # the ceiling is now 6.50 USD
        assert "budget_released" in event_kinds(scheduler.store)
        assert summary.holds["m1"].kind == "budget"  # a new stop, which needs a release of its own


def test_c13_a_release_file_older_than_the_budget_stop_does_not_resume_dispatch(tmp_path):
    """C13: a stale or reused release file does not resume dispatch."""
    plan = {"tasks": ("task-a", "task-b"), "seeds": (1,)}
    release = tmp_path / "release.txt"
    release.write_text("ALL\n", encoding="utf-8")
    os.utime(release, (1_000_000, 1_000_000))
    budget = Budget(max_usd=3.0, release_step_usd=3.5, release_file=release)
    with build(
        tmp_path / "c", executor=FakeExecutor(default="heavy"), settings=settings_for(budget=budget), plan=plan
    ) as s:
        assert s.run().statuses == {"done": 2, "pending": 4}
        assert release.exists()


# --- C15: units ------------------------------------------------------------------------------------------------------


def test_c15_n_failures_pause_the_unit_and_one_success_clears_it(tmp_path):
    """C15: after the threshold of endpoint failures the unit is paused; the first success after the pause clears it."""
    flaky = key_of(SCHEMA, "mount-read")
    executor = FakeExecutor({flaky: [("infra", "endpoint")] * 2})
    settings = settings_for(unit_fail_threshold=2, unit_pause_s=0.05, max_attempts=5)
    with build(tmp_path, executor=executor, settings=settings, plan=SMALL) as scheduler:
        summary = scheduler.run()
        assert summary.statuses == {"done": 3}
        kinds = [k for k in event_kinds(scheduler.store) if k.startswith("unit_")]
        assert kinds == ["unit_paused", "unit_recovered"]  # paused at the second failure, cleared by a success
        assert scheduler.units.down("unit-m1") is False


def test_c15_a_paused_unit_holds_its_runs_and_leaves_other_units_alone(tmp_path):
    """C15: dispatch skips a paused unit and takes the runs of another one."""
    plan = {"tasks": ("task-a",), "seeds": (1,), "models": ("m1", "m2")}
    executor = FakeExecutor()
    with build(tmp_path, executor=executor, settings=settings_for(unit_pause_s=60), plan=plan) as scheduler:
        scheduler.units.failure("unit-m1", immediate=True)
        scheduler.run(max_dispatch=3)  # m2 has three runs, and they are the only ones that can start
        assert {r.identity.factors["model"] for r in executor.requests} == {"m2"}
        statuses = {r["f_model"]: r["status"] for r in scheduler.store.q("SELECT f_model, status FROM runs")}
        assert statuses == {"m1": "pending", "m2": "done"}
        row, waiting = scheduler.next_dispatch()
        assert row is None
        assert waiting  # the pause ends by itself
        assert scheduler.holds.current()["unit:unit-m1"].kind == "unit_paused"


# --- C16: served identity ----------------------------------------------------------------------


def test_c16_a_changed_served_identity_makes_the_run_infrastructure_and_it_is_run_again(tmp_path):
    """C16: a run whose endpoint served another fingerprint than the baseline is retried as infrastructure."""
    first, second, third = order()
    behaviours = {
        first: [("served", "model-x", "fp1")],
        second: [("served", "model-x", "fp2"), ("served", "model-x", "fp1")],
        third: [("served", "model-x", "fp1")],
    }
    with build(tmp_path, executor=FakeExecutor(behaviours), plan=SMALL) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        store = scheduler.store
        assert attempts_of(store, first) == [("done", 0, 1)]
        assert attempts_of(store, second) == [("infra_fail", 1, 1), ("done", 0, 2)]
        row = store.q("SELECT infra_kind, detail FROM attempts WHERE run_key=? ORDER BY id", (second,))[0]
        assert row["infra_kind"] == "endpoint"
        assert "served identity changed" in row["detail"]
        assert "served_identity_changed" in event_kinds(store)


# --- C19: leases -------------------------------------------------------------------------------


def lease_pool(tmp_path, probe, clock=None):
    clock = clock or Clock()
    return LeasePool(
        tmp_path / "leases", lambda group: ["k1", "k2"], probe, owner="dispatcher", clock=clock, quarantine_retry_s=60
    )


def test_c19_each_run_holds_a_lease_for_its_attempt_and_it_is_freed_when_the_resource_is_gone(tmp_path):
    """C19: a run gets a key from the platform's group, holds it during the attempt, and frees it once shown gone."""
    probe = Probe(gone_keys={"k1", "k2"})
    pool = lease_pool(tmp_path, probe)
    executor = FakeExecutor()
    with build(
        tmp_path / "c", executor=executor, leases=pool, lease_group=lambda factors: "g", plan=SMALL
    ) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        assert {r.lease.key for r in executor.requests} <= {"k1", "k2"}
        assert all(r.lease is not None and r.lease.group == "g" for r in executor.requests)
        assert pool.held_back() == 0
        assert pool.try_acquire("g") is not None  # nothing is left held


def test_c19_an_unproven_teardown_quarantines_and_with_every_key_quarantined_the_run_is_unstarted(tmp_path):
    """C19: a teardown that cannot be proven keeps its key; when every key is held the run uses no attempt."""
    probe, clock = Probe(), Clock()  # no resource is shown gone yet
    pool = lease_pool(tmp_path, probe, clock)
    executor = FakeExecutor()
    settings = settings_for(workers=3)
    with build(
        tmp_path / "c", executor=executor, leases=pool, lease_group=lambda f: "g", settings=settings, plan=SMALL
    ) as scheduler:
        summary = scheduler.run()
        assert pool.held_back() == 2
        done = [r.run_key for r in executor.requests]
        assert len(done) == 2
        left = [k for k in order() if k not in done]
        assert summary.statuses == {"done": 2, "pending": 1}
        assert attempts_of(scheduler.store, left[0]) == []  # unstarted: no attempt row, so no attempt used
        assert summary.holds["lease:g"].kind == "lease"
        assert scheduler._in_use() == 2  # quarantined leases count against the cap like running attempts
        # Once the teardown is shown, the keys are freed and the last run goes.
        probe.gone_keys.update({"k1", "k2"})
        clock.now += 61
        assert scheduler.run().statuses == {"done": 3}
        assert attempts_of(scheduler.store, left[0]) == [("done", 0, 1)]


def test_c19_a_run_left_unstarted_after_waiting_for_a_lease_says_how_long_it_waited(tmp_path):
    """C19: the wait before a timeout is in the run_unstarted event, so throughput analysis loses no queue time."""
    clock = Clock()
    pool = LeasePool(
        tmp_path / "leases", lambda group: ["k1"], Probe(), owner="dispatcher", clock=clock, sleep=clock.sleep
    )
    other = pool.try_acquire("g", "somebody-else")  # the only key is busy, and stays busy
    executor = FakeExecutor()
    settings = settings_for(lease_timeout_s=10.0)
    with build(
        tmp_path / "c", executor=executor, leases=pool, lease_group=lambda f: "g", settings=settings, plan=SMALL
    ) as scheduler:
        assert scheduler.run(max_dispatch=1).dispatched == 1
        rows = scheduler.store.q("SELECT detail FROM events WHERE kind='run_unstarted'")
        assert len(rows) == 1
        assert "after waiting 10.0 s" in rows[0]["detail"]
        assert executor.requests == []  # nothing started, no attempt used
    other.release()


def test_c19_a_quarantined_lease_counts_against_the_cap_of_its_unit(tmp_path):
    """C19: cap 1 and two keys: while the first run's sandbox is unproven, a second run on the unit does not start."""
    probe, clock = Probe(), Clock()
    pool = lease_pool(tmp_path, probe, clock)
    executor = FakeExecutor()
    settings = settings_for(workers=5, unit_caps={"unit-m1": 1})  # the global cap is not what limits here
    with build(
        tmp_path / "c", executor=executor, leases=pool, lease_group=lambda f: "g", settings=settings, plan=SMALL
    ) as scheduler:
        assert scheduler.run(max_dispatch=1).dispatched == 1
        assert pool.held_back() == 1  # one key is quarantined, the other is free
        assert pool.unusable() == {"k1"}
        row, waiting = scheduler.next_dispatch()
        assert row is None  # the unit is full: its quarantined sandbox may still be running
        assert waiting
        assert len(executor.requests) == 1
        assert scheduler._live_per_unit() == {"unit-m1": 1}
        probe.gone_keys.add("k1")  # the teardown is shown
        clock.now += 61
        row, _ = scheduler.next_dispatch()
        assert row is not None
        assert pool.held_back() == 0


def release_the_process(pool):
    """Drop the file locks of a pool whose process 'died': a kill frees them, and leaves every note as it was."""
    for lease, _, _ in list(pool._quarantined.values()):
        lease._handle.close()


def test_c19_a_resource_that_may_still_be_live_after_a_restart_counts_against_the_cap_of_its_unit(tmp_path):
    """C19: cap 1 and two keys; the dispatcher dies with the first run's sandbox unproven. After the restart that key
    is a dead holder's, held back, and its unit is still full, so the run does not start on the other key."""
    probe, clock = Probe(), Clock()
    first = LeasePool(
        tmp_path / "leases", lambda g: ["k1", "k2"], probe, owner="dispatcher-1", clock=clock, quarantine_retry_s=60
    )
    settings = settings_for(workers=5, unit_caps={"unit-m1": 1})
    with build(
        tmp_path / "c", executor=FakeExecutor(), leases=first, lease_group=lambda f: "g", settings=settings, plan=SMALL
    ) as scheduler:
        assert scheduler.run(max_dispatch=1).dispatched == 1
        assert first.held_back() == 1
        assert scheduler._live_per_unit() == {"unit-m1": 1}  # before the restart: the quarantined lease
    note = json.loads((tmp_path / "leases" / "k1.lock").read_text())
    assert note["unit"] == "unit-m1"  # the note records the unit, which is how the restart knows it
    release_the_process(first)
    after = Probe(gone_owners={"dispatcher-1"})  # the holder is dead; its sandbox is not shown gone
    second = LeasePool(
        tmp_path / "leases", lambda g: ["k1", "k2"], after, owner="dispatcher-2", clock=clock, quarantine_retry_s=60
    )
    executor = FakeExecutor()
    with build(
        tmp_path / "c", executor=executor, leases=second, lease_group=lambda f: "g", settings=settings, plan=SMALL
    ) as scheduler:
        assert second.held_back() == 0  # nothing is quarantined here: it is a dead holder's key now
        assert second.may_be_live() == ["unit-m1"]
        assert scheduler._live_per_unit() == {"unit-m1": 1}
        row, waiting = scheduler.next_dispatch()
        assert row is None  # key k2 is free, but the unit's cap of 1 is taken by the surviving resource
        assert waiting
        clock.now += 100  # past the retry period, still not shown gone: it goes on counting
        assert scheduler.next_dispatch()[0] is None
        assert scheduler._live_per_unit() == {"unit-m1": 1}
        assert executor.requests == []
        after.gone_keys.add("k1")  # the teardown is shown
        clock.now += 100
        row, _ = scheduler.next_dispatch()
        assert row is not None
        assert second.may_be_live() == []
        assert scheduler.run().statuses == {"done": 3}


def test_c19_a_dead_holders_key_whose_note_cannot_be_read_counts_against_every_unit(tmp_path):
    """C19: an unreadable note names no unit, so the key whose resource may be live fails safe and fills each cap."""
    directory = tmp_path / "leases"
    directory.mkdir()
    (directory / "k1.lock").write_text('{"owner": "holder-1", "uni', encoding="utf-8")
    pool = LeasePool(directory, lambda g: ["k1", "k2"], Probe(), owner="me", clock=Clock())
    settings = settings_for(workers=5, unit_caps={"unit-m1": 1})
    with build(
        tmp_path / "c", executor=FakeExecutor(), leases=pool, lease_group=lambda f: "g", settings=settings, plan=SMALL
    ) as scheduler:
        assert pool.may_be_live() == [None]
        assert scheduler.next_dispatch()[0] is None
