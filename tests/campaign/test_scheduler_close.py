"""Closing a scheduler waits for its workers before the store closes and the dispatch lock is released (C07, C11)."""

import asyncio
import threading
import time

import pytest
from lease_support import Probe
from scheduler_support import FakeExecutor, build, settings_for

from expcore.campaign.healthgate import Health
from expcore.campaign.lease import LeasePool
from expcore.campaign.lock import CampaignLocked

SMALL = {"tasks": ("task-a",), "seeds": (1,)}


class Blocked:
    """An executor behaviour that stays inside the attempt until it is let go; it finishes the run as usual then."""

    def __init__(self):
        self.started, self.release = threading.Event(), threading.Event()
        self.executor = FakeExecutor(default=self)

    async def __call__(self, request):
        self.started.set()
        await asyncio.to_thread(self.release.wait, 30)
        return await self.executor._run(request, "finish")


def in_thread(fn):
    thread = threading.Thread(target=fn, daemon=True)
    thread.start()
    return thread


class Slow:
    """A health check that takes a while to say the platform is up."""

    def __call__(self):
        time.sleep(0.15)
        return Health(True, "up")


@pytest.mark.parametrize("health", [None, Slow()], ids=["no health check", "a post-run health check in flight"])
def test_c07_a_second_dispatcher_cannot_take_the_lock_while_a_worker_is_mid_attempt_after_close_is_called(
    tmp_path, health
):
    """C07: close() signals the end and waits for the attempt in flight; then the store closes and the lock goes.
    The attempt ends as it would have: a finished result is not relabelled as infrastructure by the shutdown."""
    blocked = Blocked()
    first = build(tmp_path, executor=blocked.executor, health=health, plan=SMALL).open()
    runner = in_thread(first.run)
    assert blocked.started.wait(10)
    closer = in_thread(first.close)
    time.sleep(0.3)
    assert closer.is_alive()  # close is waiting for the worker, not abandoning it
    assert first.store is not None  # and the store is still open for the worker's result
    with pytest.raises(CampaignLocked, match="one dispatcher per store"):
        build(tmp_path, plan=SMALL).open()
    blocked.release.set()
    closer.join(10)
    runner.join(10)
    assert not closer.is_alive()
    assert first.store is None
    with build(tmp_path, plan=SMALL) as second:  # the lock was released once the worker had ended
        statuses = {
            r["status"]: r["n"] for r in second.store.q("SELECT status, COUNT(*) AS n FROM runs GROUP BY status")
        }
        assert statuses == {"done": 1, "pending": 2}  # the attempt in flight was finished and recorded, not cut off
        assert second.store.q("SELECT state FROM attempts")[0]["state"] == "done"


def test_c07_closing_a_scheduler_with_nothing_in_flight_returns_at_once_and_twice_is_harmless(tmp_path):
    """C07: with no worker and no loop there is nothing to wait for."""
    scheduler = build(tmp_path, plan=SMALL).open()
    began = time.monotonic()
    scheduler.close()
    scheduler.close()
    assert time.monotonic() - began < 20  # far below the 30 s hang it must not wait out
    with build(tmp_path, plan=SMALL):
        pass


def test_c07_a_run_waiting_for_a_lease_when_the_campaign_ends_gives_up_at_once_and_starts_nothing(tmp_path):
    """C07: a worker that waits for a key has started no attempt, so close does not wait out the lease timeout."""
    pool = LeasePool(tmp_path / "leases", lambda group: ["k1"], Probe(), owner="dispatcher", poll_s=0.01)
    busy = pool.try_acquire("g", "somebody-else")  # the only key stays busy
    executor = FakeExecutor()
    scheduler = build(
        tmp_path / "c", executor=executor, leases=pool, lease_group=lambda f: "g", settings=settings_for(), plan=SMALL
    ).open()
    runner = in_thread(scheduler.run)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not scheduler._inflight:
        time.sleep(0.01)
    time.sleep(0.2)  # the worker is inside the wait for a lease
    began = time.monotonic()
    scheduler.close()
    assert time.monotonic() - began < 20  # not the 600 s of the lease timeout
    runner.join(10)
    assert executor.requests == []
    busy.release()
    with build(tmp_path / "c", plan=SMALL) as again:
        assert again.store.q("SELECT COUNT(*) AS n FROM attempts")[0]["n"] == 0  # no attempt was used
        kinds = [r["detail"] for r in again.store.q("SELECT detail FROM events WHERE kind='run_unstarted'")]
        assert any("the campaign is ending" in detail for detail in kinds)


def test_c11_a_health_check_that_hangs_does_not_block_the_shutdown(tmp_path):
    """C11: the worker waiting on a hung pre-run check is let go when the campaign ends, and charges nothing."""
    release = threading.Event()
    calls = []

    def hangs():
        calls.append(1)
        release.wait(30)
        return Health(True, "late")

    scheduler = build(
        tmp_path, health=hangs, settings=settings_for(health_timeout_s=60), plan=SMALL, executor=FakeExecutor()
    ).open()
    runner = in_thread(scheduler.run)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not calls:
        time.sleep(0.01)
    assert calls  # the worker is inside the check
    began = time.monotonic()
    scheduler.close()
    assert time.monotonic() - began < 20  # not the 60 s of the health timeout
    runner.join(10)
    release.set()
    with build(tmp_path, plan=SMALL) as again:
        assert again.store.q("SELECT COUNT(*) AS n FROM attempts")[0]["n"] == 0
