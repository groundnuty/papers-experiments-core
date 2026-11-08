"""Endpoint health per unit: N failures pause it, and only a success clears the pause (C15)."""

import pytest
from campaign_support import NOW, planned_store

from expcore.campaign.units import META, UnitHealth


class Time:
    """The epoch time of the tests, moved by hand."""

    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def units_for(tmp_path, threshold=3, pause_s=100.0):
    store, _, _ = planned_store(tmp_path)
    moment = Time()
    return UnitHealth(store, fail_threshold=threshold, pause_s=pause_s, now=moment, stamp=lambda: NOW), moment, store


def events(store):
    return [r["kind"] for r in store.q("SELECT kind FROM events ORDER BY id")]


def test_c15_n_failures_pause_the_unit_and_one_success_clears_it(tmp_path):
    """C15: below the threshold the unit goes on; at it, the unit is paused; one success clears the pause."""
    units, moment, store = units_for(tmp_path, threshold=3)
    assert [units.failure("u") for _ in range(2)] == [False, False]
    assert units.ok("u")
    assert units.failure("u") is True
    assert not units.ok("u")
    assert units.down("u")
    moment.now += 101
    assert units.ok("u")  # after the pause a run may probe the unit
    assert units.down("u")  # but the outage window stays open until a success
    assert units.success("u") is True
    assert not units.down("u")
    assert units.paused_until("u") == 0.0
    assert events(store) == ["unit_paused", "unit_recovered"]


def test_c15_the_pause_is_cleared_only_by_a_success_so_one_failure_after_it_pauses_again(tmp_path):
    """C15: a unit that is down and fails again is paused again on that one failure; waiting never clears it."""
    units, moment, store = units_for(tmp_path, threshold=3)
    for _ in range(3):
        units.failure("u")
    moment.now += 101
    assert units.ok("u")
    assert units.failure("u") is True  # one failure is enough, as the unit is down
    assert not units.ok("u")
    assert events(store) == ["unit_paused", "unit_paused_again"]


def test_c15_a_success_resets_the_count_and_does_not_clear_other_units(tmp_path):
    """C15: failures are counted in a row per unit, and a success of one unit says nothing of another."""
    units, _, _ = units_for(tmp_path, threshold=2)
    units.failure("a")
    units.success("a")
    assert units.failure("a") is False  # the count started again
    units.failure("b")
    assert units.failure("b") is True
    assert units.ok("a")
    assert not units.ok("b")
    assert units.success("c") is False  # a unit never seen has nothing to clear


def test_c15_a_refused_key_pauses_the_unit_at_once(tmp_path):
    """C15: key rejection is not worth waiting for a second: the unit is paused on the first."""
    units, _, _ = units_for(tmp_path, threshold=5)
    assert units.failure("u", immediate=True, detail="HTTP 401") is True
    assert not units.ok("u")


def test_c15_the_state_survives_a_restart(tmp_path):
    """C15: a restart forgets neither a pause nor an open outage window."""
    units, moment, store = units_for(tmp_path, threshold=1)
    units.failure("u")
    again = UnitHealth(store, fail_threshold=1, pause_s=100.0, now=moment, stamp=lambda: NOW)
    assert not again.ok("u")
    assert again.down("u")
    assert again.status()["u"]["down"] is True


@pytest.mark.parametrize("garbage", ["{broken", "[1, 2]", '{"u": 3}', "null"])
def test_c15_a_record_that_cannot_be_read_pauses_every_unit_until_a_success_clears_it(tmp_path, garbage):
    """C15: malformed unit state fails safe: every unit, listed or not, is paused and down, and the event is logged."""
    _units, moment, store = units_for(tmp_path, pause_s=100.0)
    store.put_meta(META, garbage, NOW)
    again = UnitHealth(store, fail_threshold=3, pause_s=100.0, now=moment, stamp=lambda: NOW)
    assert "unit_record_unreadable" in events(store)
    for unit in ("u", "never-seen-before"):
        assert not again.ok(unit)
        assert again.down(unit)
        assert again.paused_until(unit) == moment.now + 100.0
    moment.now += 101  # the pause ends: one run may probe, as after any pause, but the unit is still down
    assert again.ok("u")
    assert again.down("u")
    assert again.failure("u") is True  # a unit that is down and fails again is paused again on that one failure
    assert not again.ok("u")
    moment.now += 101
    assert again.success("u") is True  # only a success clears it, and only that unit
    assert again.ok("u")
    assert not again.down("u")
    assert again.down("other")


def test_c15_the_pause_an_unreadable_record_caused_survives_a_restart_in_the_middle_of_it(tmp_path):
    """C15: the fail-safe state is written back, so a restart before any success does not read the units as healthy."""
    _units, moment, store = units_for(tmp_path, pause_s=100.0)
    store.put_meta(META, "{broken", NOW)
    UnitHealth(store, fail_threshold=3, pause_s=100.0, now=moment, stamp=lambda: NOW)
    moment.now += 500
    restarted = UnitHealth(store, fail_threshold=3, pause_s=100.0, now=moment, stamp=lambda: NOW)
    assert restarted.down("u")
    assert "unit_record_unreadable" in events(store)
    assert events(store).count("unit_record_unreadable") == 1  # the record is readable again, so it is said once
    assert restarted.success("u") is True
    assert not restarted.down("u")


def test_c15_the_limits_are_checked(tmp_path):
    """C15: a threshold or pause that cannot work is refused."""
    store, _, _ = planned_store(tmp_path)
    with pytest.raises(ValueError, match="fail_threshold"):
        UnitHealth(store, fail_threshold=0, pause_s=1.0, now=Time(), stamp=lambda: NOW)
    with pytest.raises(ValueError, match="pause_s"):
        UnitHealth(store, fail_threshold=1, pause_s=0, now=Time(), stamp=lambda: NOW)
