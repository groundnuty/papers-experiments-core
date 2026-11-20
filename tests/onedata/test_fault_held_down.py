"""The registry of what a run's own injection holds down (`fault_owner.hold`, `release`, `held_down`, `held_reader`),
and the injector that keeps it: held once the target is seen down, keyed by the attempt, released on every path
(O97)."""

import asyncio
import gc
import threading

import pytest
from fault_support import OUTAGE_TARGET, FakeServices, injector_for, write_faults

from expcore.campaign import liveness
from expcore.errors import InfraError
from expcore_onedata import fault_owner

KEY = "task=task-a|model=m1|seed=1"
OTHER_KEY = "task=task-b|model=m1|seed=1"
RUN = {"run_key": KEY, "attempt": 1}
OUTSIDE = None  # no attempt: a script beside a campaign


@pytest.fixture(autouse=True)
def empty_registry():
    fault_owner.reset_held()
    yield
    fault_owner.reset_held()


@pytest.fixture
def campaign():
    """A campaign registered in this process: attempts 5 and 6 run."""
    rows = {5: "running", 6: "running"}
    key = liveness.register(rows.get)
    yield key
    liveness.unregister(key)


def token(campaign_key, attempt_id):
    return fault_owner.attempt_of(
        {"process": liveness.this_process(), "campaign": campaign_key, "attempt_id": attempt_id}
    )


def test_o97_a_run_with_no_injection_holds_nothing_down():
    """O97: nothing registered reads as the empty set, for any key: no injection in effect."""
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == frozenset()


def test_o97_a_hold_is_the_runs_alone():
    """O97: the label held for one run key is not held for another's."""
    fault_owner.hold(KEY, "providers[1]", attempt=OUTSIDE)
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == {"providers[1]"}
    assert fault_owner.held_down(OTHER_KEY, attempt=OUTSIDE) == frozenset()


def test_o97_a_run_can_hold_two_targets_and_release_one():
    """O97: two labels of one run, and releasing one leaves the other."""
    fault_owner.hold(KEY, "providers[1]", attempt=OUTSIDE)
    fault_owner.hold(KEY, "providers[2]", attempt=OUTSIDE)
    fault_owner.release(KEY, "providers[1]", attempt=OUTSIDE)
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == {"providers[2]"}


def test_o97_a_release_with_no_label_drops_every_label_of_the_run():
    """O97: `release(run_key)` ends the run's holds, and only that run's."""
    fault_owner.hold(KEY, "providers[1]", attempt=OUTSIDE)
    fault_owner.hold(OTHER_KEY, "providers[2]", attempt=OUTSIDE)
    fault_owner.release(KEY, attempt=OUTSIDE)
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == frozenset()
    assert fault_owner.held_down(OTHER_KEY, attempt=OUTSIDE) == {"providers[2]"}


def test_o97_a_release_of_what_is_not_held_is_harmless():
    """O97: a clear that comes with nothing held releases nothing and raises nothing."""
    fault_owner.release(KEY, attempt=OUTSIDE)
    fault_owner.release(KEY, "providers[1]", attempt=OUTSIDE)
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == frozenset()


def test_o97_a_hold_is_seen_from_the_deadlines_timer_thread():
    """O97: a thread that never ran the hold (the cell timer's) reads it: the registry is not a ContextVar."""
    fault_owner.hold(KEY, "providers[1]", attempt=OUTSIDE)
    seen: list[frozenset[str]] = []
    thread = threading.Thread(target=lambda: seen.append(fault_owner.held_down(KEY, attempt=OUTSIDE)))
    thread.start()
    thread.join()
    assert seen == [frozenset({"providers[1]"})]


def test_o97_a_hold_whose_injector_is_gone_reads_as_released():
    """O97: an injector dropped without a clear holds nothing down any more (its hold is weak)."""

    class Injector:
        pass

    owner = Injector()
    fault_owner.hold(KEY, "providers[1]", owner, attempt=OUTSIDE)
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == {"providers[1]"}
    del owner
    gc.collect()
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == frozenset()


def test_o97_a_key_that_is_not_text_holds_nothing():
    """O97: a run key that is not a string reads as the empty set, never an error in the deadline's thread."""
    assert fault_owner.held_down(None, attempt=OUTSIDE) == frozenset()  # type: ignore[arg-type]


def test_o97_a_hold_of_one_attempt_is_never_read_for_another_of_the_same_run_key(campaign):
    """O97: attempt 5 left its hold; attempt 6 and a script read nothing, though the run key is the same."""
    fault_owner.hold(KEY, "providers[1]", attempt=token(campaign, 5))
    assert fault_owner.held_down(KEY, attempt=token(campaign, 5)) == {"providers[1]"}
    assert fault_owner.held_down(KEY, attempt=token(campaign, 6)) == frozenset()
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == frozenset()


def test_o97_outside_a_campaign_the_run_key_alone_must_match():
    """O97: with no attempt on either side, the hold and the read meet on the run key."""
    fault_owner.hold(KEY, "providers[1]", attempt=OUTSIDE)
    assert fault_owner.attempt_key() is OUTSIDE
    assert fault_owner.held_reader(KEY)() == {"providers[1]"}
    assert fault_owner.held_reader(OTHER_KEY)() == frozenset()


def test_o97_the_reader_takes_its_attempt_where_it_is_made_and_keeps_it_for_the_timers_thread(campaign):
    """O97: made inside attempt 5, the reader reads attempt 5's holds from a thread with no context, later, and reads
    nothing of attempt 6's."""
    with liveness.running(campaign, 5):
        reader = fault_owner.held_reader(KEY)
    fault_owner.hold(KEY, "providers[1]", attempt=token(campaign, 5))
    fault_owner.hold(KEY, "providers[2]", attempt=token(campaign, 6))
    seen: list[frozenset[str]] = []
    thread = threading.Thread(target=lambda: seen.append(reader()))
    thread.start()
    thread.join()
    assert seen == [frozenset({"providers[1]"})]


def test_o97_the_end_of_the_attempt_lets_its_hold_go(campaign):
    """O97: a hold made inside an attempt that never reached its clear is released when the attempt ends."""
    with liveness.running(campaign, 5):
        fault_owner.hold(KEY, "providers[1]", attempt=token(campaign, 5))
        assert fault_owner.held_down(KEY, attempt=token(campaign, 5)) == {"providers[1]"}
    assert fault_owner.held_down(KEY, attempt=token(campaign, 5)) == frozenset()


def test_o97_the_target_is_held_down_once_its_injection_is_verified(tmp_path):
    """O97: after `start` has seen the target down, the run's key holds exactly that target's label."""
    injector = injector_for(write_faults(tmp_path), FakeServices())
    injector.start(RUN)
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == {OUTAGE_TARGET}
    injector.finish()


def test_o97_an_injector_inside_an_attempt_holds_under_that_attempts_token(tmp_path, campaign):
    """O97: the hold is the attempt's: attempt 6, running while attempt 5's entry is still registered (5 never reached
    its clear), reads nothing of the target attempt 5's injector holds down."""
    injector = injector_for(write_faults(tmp_path), FakeServices())
    with liveness.running(campaign, 5):
        injector.start(RUN)
        reader = fault_owner.held_reader(KEY)
        assert reader() == {OUTAGE_TARGET}
        with liveness.running(campaign, 6):
            retry = fault_owner.held_reader(KEY)
        assert retry() == frozenset()
        assert reader() == {OUTAGE_TARGET}
    injector.finish()


def test_o97_an_attempt_that_ends_with_its_injection_open_releases_the_hold(tmp_path, campaign):
    """O97: the end of the attempt is the last resort: no clear ran, and the hold is gone with the attempt."""
    injector = injector_for(write_faults(tmp_path), FakeServices())
    with liveness.running(campaign, 5):
        injector.start(RUN)
        reader = fault_owner.held_reader(KEY)
        assert reader() == {OUTAGE_TARGET}
    assert reader() == frozenset()
    injector.finish()


def test_o97_a_start_that_does_not_see_the_target_down_holds_nothing(tmp_path):
    """O97: a stop that leaves the target running is no injection in effect: nothing is held."""
    injector = injector_for(write_faults(tmp_path), FakeServices(stop_noop=True))
    with pytest.raises(InfraError):
        injector.start(RUN)
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == frozenset()


def test_o97_a_start_that_fails_after_it_registered_its_hold_releases_it(tmp_path, monkeypatch):
    """O97: the hold is registered, then the start raises: the exit releases it, the target is not held down for a run
    that never began."""
    real = fault_owner.hold

    def hold_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("the start failed after its hold")

    monkeypatch.setattr(fault_owner, "hold", hold_then_fail)
    injector = injector_for(write_faults(tmp_path), FakeServices())
    with pytest.raises(RuntimeError, match="after its hold"):
        injector.start(RUN)
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == frozenset()
    injector.clear()


def test_o97_a_clear_releases_the_hold(tmp_path):
    """O97: once the clear has run, the run holds nothing down."""
    injector = injector_for(write_faults(tmp_path), FakeServices())
    injector.start(RUN)
    assert injector.clear() is True
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == frozenset()


def test_o97_the_hold_is_gone_before_the_restore_begins(tmp_path):
    """O97: a cell that dies while the target is being brought back is not the agent's: the hold is released first."""
    services = FakeServices(serves_after=1)
    seen: list[frozenset[str]] = []
    services.on_serves = lambda service: seen.append(fault_owner.held_down(KEY, attempt=OUTSIDE))
    injector = injector_for(write_faults(tmp_path), services)
    injector.start(RUN)
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == {OUTAGE_TARGET}
    injector.clear()
    assert seen
    assert all(held == frozenset() for held in seen)


def test_o97_a_clear_that_fails_to_restore_still_holds_nothing(tmp_path):
    """O97: a restore that fails leaves the target down, and the run is not read as holding it down on purpose."""
    services = FakeServices()
    injector = injector_for(write_faults(tmp_path), services)
    injector.start(RUN)
    services.knobs["start_fails"] = True
    injector.clear()
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == frozenset()


def test_o97_a_prepared_block_holds_only_after_its_inject(tmp_path):
    """O97: the hold follows `inject()` of a prepared block, never the baseline between its halves."""
    injector = injector_for(write_faults(tmp_path), FakeServices())
    with injector.prepared(RUN) as block:
        assert fault_owner.held_down(KEY, attempt=OUTSIDE) == frozenset()
        block.inject()
        assert fault_owner.held_down(KEY, attempt=OUTSIDE) == {OUTAGE_TARGET}
    injector.clear()
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == frozenset()


def test_o97_a_second_run_of_the_injector_holds_for_its_own_key_only(tmp_path):
    """O97: after a clear, a new start holds the new run's key and the earlier run's stays released."""
    injector = injector_for(write_faults(tmp_path), FakeServices())
    injector.start(RUN)
    injector.clear()
    injector.start({"run_key": OTHER_KEY, "attempt": 1})
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == frozenset()
    assert fault_owner.held_down(OTHER_KEY, attempt=OUTSIDE) == {OUTAGE_TARGET}
    injector.finish()


def test_o97_a_body_that_raises_in_running_releases_the_hold_before_the_clear(tmp_path):
    """O97: the exit of `running` releases at once, in its own thread, then clears."""
    services = FakeServices(serves_after=1)
    seen: list[frozenset[str]] = []
    services.on_serves = lambda service: seen.append(fault_owner.held_down(KEY, attempt=OUTSIDE))
    injector = injector_for(write_faults(tmp_path), services)

    async def body():
        async with injector.running(RUN):
            assert fault_owner.held_down(KEY, attempt=OUTSIDE) == {OUTAGE_TARGET}
            raise RuntimeError("the run failed")

    with pytest.raises(RuntimeError, match="the run failed"):
        asyncio.run(body())
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == frozenset()
    assert all(held == frozenset() for held in seen)


def test_o97_a_cancelled_running_releases_the_hold(tmp_path):
    """O97: a cancellation inside the body leaves no hold, whatever the clear's thread does after."""
    injector = injector_for(write_faults(tmp_path), FakeServices())

    async def body():
        async with injector.running(RUN):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(body())
    assert fault_owner.held_down(KEY, attempt=OUTSIDE) == frozenset()
