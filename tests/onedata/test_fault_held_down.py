"""The registry of what a run's own injection holds down (`fault_owner.hold`, `release`, `held_down`), and the injector
that keeps it: held once the target is seen down, released before any restore (O97)."""

import gc
import threading

import pytest
from fault_support import OUTAGE_TARGET, FakeServices, injector_for, write_faults

from expcore.errors import InfraError
from expcore_onedata import fault_owner

KEY = "task=task-a|model=m1|seed=1"
OTHER_KEY = "task=task-b|model=m1|seed=1"
RUN = {"run_key": KEY, "attempt": 1}


@pytest.fixture(autouse=True)
def empty_registry():
    fault_owner.reset_held()
    yield
    fault_owner.reset_held()


def test_o97_a_run_with_no_injection_holds_nothing_down():
    """O97: nothing registered reads as the empty set, for any key: no injection in effect."""
    assert fault_owner.held_down(KEY) == frozenset()


def test_o97_a_hold_is_the_runs_alone():
    """O97: the label held for one run key is not held for another's."""
    fault_owner.hold(KEY, "providers[1]")
    assert fault_owner.held_down(KEY) == {"providers[1]"}
    assert fault_owner.held_down(OTHER_KEY) == frozenset()


def test_o97_a_run_can_hold_two_targets_and_release_one():
    """O97: two labels of one run, and releasing one leaves the other."""
    fault_owner.hold(KEY, "providers[1]")
    fault_owner.hold(KEY, "providers[2]")
    fault_owner.release(KEY, "providers[1]")
    assert fault_owner.held_down(KEY) == {"providers[2]"}


def test_o97_a_release_with_no_label_drops_every_label_of_the_run():
    """O97: `release(run_key)` ends the run's holds, and only that run's."""
    fault_owner.hold(KEY, "providers[1]")
    fault_owner.hold(OTHER_KEY, "providers[2]")
    fault_owner.release(KEY)
    assert fault_owner.held_down(KEY) == frozenset()
    assert fault_owner.held_down(OTHER_KEY) == {"providers[2]"}


def test_o97_a_release_of_what_is_not_held_is_harmless():
    """O97: a clear that comes with nothing held releases nothing and raises nothing."""
    fault_owner.release(KEY)
    fault_owner.release(KEY, "providers[1]")
    assert fault_owner.held_down(KEY) == frozenset()


def test_o97_a_hold_is_seen_from_the_deadlines_timer_thread():
    """O97: a thread that never ran the hold (the cell timer's) reads it: the registry is not a ContextVar."""
    fault_owner.hold(KEY, "providers[1]")
    seen: list[frozenset[str]] = []
    thread = threading.Thread(target=lambda: seen.append(fault_owner.held_down(KEY)))
    thread.start()
    thread.join()
    assert seen == [frozenset({"providers[1]"})]


def test_o97_a_hold_whose_injector_is_gone_reads_as_released():
    """O97: an injector dropped without a clear holds nothing down any more (its hold is weak)."""

    class Injector:
        pass

    owner = Injector()
    fault_owner.hold(KEY, "providers[1]", owner)
    assert fault_owner.held_down(KEY) == {"providers[1]"}
    del owner
    gc.collect()
    assert fault_owner.held_down(KEY) == frozenset()


def test_o97_a_key_that_is_not_text_holds_nothing():
    """O97: a run key that is not a string reads as the empty set, never an error in the deadline's thread."""
    assert fault_owner.held_down(None) == frozenset()  # type: ignore[arg-type]


def test_o97_the_target_is_held_down_once_its_injection_is_verified(tmp_path):
    """O97: after `start` has seen the target down, the run's key holds exactly that target's label."""
    injector = injector_for(write_faults(tmp_path), FakeServices())
    injector.start(RUN)
    assert fault_owner.held_down(KEY) == {OUTAGE_TARGET}
    injector.finish()


def test_o97_a_start_that_does_not_see_the_target_down_holds_nothing(tmp_path):
    """O97: a stop that leaves the target running is no injection in effect: nothing is held."""
    injector = injector_for(write_faults(tmp_path), FakeServices(stop_noop=True))
    with pytest.raises(InfraError):
        injector.start(RUN)
    assert fault_owner.held_down(KEY) == frozenset()


def test_o97_a_clear_releases_the_hold(tmp_path):
    """O97: once the clear has run, the run holds nothing down."""
    injector = injector_for(write_faults(tmp_path), FakeServices())
    injector.start(RUN)
    assert injector.clear() is True
    assert fault_owner.held_down(KEY) == frozenset()


def test_o97_the_hold_is_gone_before_the_restore_begins(tmp_path):
    """O97: a cell that dies while the target is being brought back is not the agent's: the hold is released first."""
    services = FakeServices(serves_after=1)
    seen: list[frozenset[str]] = []
    services.on_serves = lambda service: seen.append(fault_owner.held_down(KEY))
    injector = injector_for(write_faults(tmp_path), services)
    injector.start(RUN)
    assert fault_owner.held_down(KEY) == {OUTAGE_TARGET}
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
    assert fault_owner.held_down(KEY) == frozenset()


def test_o97_a_prepared_block_holds_only_after_its_inject(tmp_path):
    """O97: the hold follows `inject()` of a prepared block, never the baseline between its halves."""
    injector = injector_for(write_faults(tmp_path), FakeServices())
    with injector.prepared(RUN) as block:
        assert fault_owner.held_down(KEY) == frozenset()
        block.inject()
        assert fault_owner.held_down(KEY) == {OUTAGE_TARGET}
    injector.clear()
    assert fault_owner.held_down(KEY) == frozenset()


def test_o97_a_second_run_of_the_injector_holds_for_its_own_key_only(tmp_path):
    """O97: after a clear, a new start holds the new run's key and the earlier run's stays released."""
    injector = injector_for(write_faults(tmp_path), FakeServices())
    injector.start(RUN)
    injector.clear()
    injector.start({"run_key": OTHER_KEY, "attempt": 1})
    assert fault_owner.held_down(KEY) == frozenset()
    assert fault_owner.held_down(OTHER_KEY) == {OUTAGE_TARGET}
    injector.finish()
