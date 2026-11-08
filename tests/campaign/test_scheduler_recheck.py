"""A run that has just got its lease is held after all when a gate has closed while it waited (C11, C17)."""

import json
from types import SimpleNamespace

import pytest
from lease_support import Clock, Probe
from scheduler_support import FakeExecutor, attempts_of, build, settings_for

from expcore.campaign.harness_stop import KEY as STOPS_KEY
from expcore.campaign.healthgate import Health
from expcore.campaign.lease import LeasePool

SMALL = {"tasks": ("task-a",), "seeds": (1,)}
UNIT = "unit-m1"


class Up:
    """A health check that passes."""

    def __call__(self):
        return Health(True, "fine")


class SlowPool(LeasePool):
    """A lease pool that runs a hook once a lease is in hand: what happens while a run waits for its key."""

    after_acquire = None

    async def acquire_under(self, waits, group, **kwargs):
        lease = await super().acquire_under(waits, group, **kwargs)
        if self.after_acquire is not None:
            self.after_acquire()
        return lease


def slow_pool(tmp_path, probe, clock):
    return SlowPool(
        tmp_path / "leases", lambda group: ["k1", "k2"], probe, owner="dispatcher", clock=clock, sleep=clock.sleep
    )


def close_the_gate(scheduler):
    scheduler.gate._close("another-run", "the platform went down")


def stop_the_model(scheduler):
    state = {"models": {"m1": {"kind": "ValueError", "utc": "x", "epoch": 1.0, "why": "3 harness errors"}}}
    scheduler.store.put_meta(STOPS_KEY, json.dumps({**state, "all": None, "cleared": {}}), "2026-01-01T00:00:00Z")


def pause_the_unit(scheduler):
    scheduler.units.failure(UNIT, immediate=True, detail="HTTP 503")


def exhaust_the_budget(scheduler):
    scheduler.budget = SimpleNamespace(
        assess=lambda flying, now, exact=None: SimpleNamespace(reason=lambda model: "over its ceiling")
    )


def end_the_campaign(scheduler):
    scheduler.stop.set()


CLOSINGS = [
    pytest.param(close_the_gate, "health_gate", id="health gate"),
    pytest.param(stop_the_model, "harness_errors", id="harness stop"),
    pytest.param(pause_the_unit, "unit_paused", id="unit pause"),
    pytest.param(exhaust_the_budget, "budget", id="budget"),
    pytest.param(end_the_campaign, "shutdown", id="shutdown"),
]


@pytest.mark.parametrize(("closing", "kind"), CLOSINGS)
def test_c11_a_gate_closed_or_a_shutdown_while_the_run_waited_for_its_lease_releases_it_and_uses_no_attempt(
    tmp_path, closing, kind
):
    """C11 and C17: after the lease is in hand the gates are looked at again; one closed means the lease goes back
    cleanly (no teardown to prove, nothing launched) and the run stays pending with no attempt used."""
    probe, clock = Probe(), Clock()
    pool = slow_pool(tmp_path, probe, clock)
    executor = FakeExecutor()
    with build(
        tmp_path / "c",
        executor=executor,
        leases=pool,
        lease_group=lambda f: "g",
        health=Up(),
        settings=settings_for(),
        plan=SMALL,
    ) as scheduler:
        pool.after_acquire = lambda: closing(scheduler)
        scheduler.run(max_dispatch=1)
        first = scheduler.store.q("SELECT run_key FROM runs ORDER BY seq")[0]["run_key"]
        assert executor.requests == []  # nothing was started
        assert attempts_of(scheduler.store, first) == []  # no attempt row, so no attempt used
        assert scheduler.store.q("SELECT COUNT(*) AS n FROM attempts")[0]["n"] == 0
        assert scheduler.store.q("SELECT status, COUNT(*) AS n FROM runs GROUP BY status")[0]["status"] == "pending"
        details = [r["detail"] for r in scheduler.store.q("SELECT detail FROM events WHERE kind='run_unstarted'")]
        assert len(details) == 1
        assert f"a {kind} hold" in details[0]
        assert "lease is released" in details[0]
        assert probe.asked == []  # released as unused: the probe was not asked to show a teardown
        assert pool.held_back() == 0
        assert pool.unusable() == set()
        again = [pool.try_acquire("g", "later-a"), pool.try_acquire("g", "later-b")]
        assert all(lease is not None for lease in again)  # both keys are free


def test_c11_when_no_gate_has_closed_the_run_starts_and_the_lease_is_finished_as_before(tmp_path):
    """C11: the recheck changes nothing for a run that is still free to start."""
    probe, clock = Probe(gone_keys={"k1", "k2"}), Clock()
    pool = slow_pool(tmp_path, probe, clock)
    executor = FakeExecutor()
    with build(
        tmp_path / "c", executor=executor, leases=pool, lease_group=lambda f: "g", health=Up(), plan=SMALL
    ) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        assert len(executor.requests) == 3
        assert probe.asked  # a run that started has its teardown looked at


def test_c11_the_run_that_was_held_after_its_lease_starts_once_the_hold_ends(tmp_path):
    """C11: nothing was used up, so when the gate opens again the same run is dispatched and finishes."""
    probe, clock = Probe(gone_keys={"k1", "k2"}), Clock()
    pool = slow_pool(tmp_path, probe, clock)
    executor = FakeExecutor()
    with build(
        tmp_path / "c", executor=executor, leases=pool, lease_group=lambda f: "g", health=Up(), plan=SMALL
    ) as scheduler:
        pool.after_acquire = lambda: close_the_gate(scheduler)
        scheduler.run(max_dispatch=1)
        pool.after_acquire = None
        assert scheduler.gate.poll() is True  # the dispatcher's own check passes: the gate opens
        assert scheduler.run().statuses == {"done": 3}
        assert [attempts_of(scheduler.store, r["run_key"]) for r in scheduler.store.q("SELECT run_key FROM runs")] == [
            [("done", 0, 1)]
        ] * 3
