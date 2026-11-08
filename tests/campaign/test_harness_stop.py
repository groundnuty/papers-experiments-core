"""Harness errors are neither infrastructure nor outcomes: they requeue, and enough of a kind stop dispatch (C10)."""

import os

import pytest
from campaign_support import LATER, NOW, defect, planned_store, run_one

from expcore.campaign.harness_stop import (
    ALL,
    KEY,
    HarnessStopConfig,
    HarnessStops,
    kind_of,
    kind_of_detail,
)
from expcore.campaign.markers import AttemptResult
from expcore.campaign.recovery import recover
from expcore.campaign.release import epoch_of
from expcore.loop.outcomes import HarnessDefect, Outcome

MODELS = ("m1", "m2", "m3")


def error_of(kind="ValueError", ended=LATER):
    outcome = Outcome("harness_defect", "boom", defect=HarnessDefect(kind, "c" * 64))
    return AttemptResult(outcome, ended, 1.0)


def keys_by_model(store):
    out = {}
    for row in store.q("SELECT run_key, f_model FROM runs ORDER BY seq"):
        out.setdefault(row["f_model"], []).append(row["run_key"])
    return out


def fail(store, stops, model, index, kind="ValueError", ended=LATER, now=LATER):
    """Make the model's index-th run end as a harness error and tell the stops, as the scheduler does."""
    run_one(store, keys_by_model(store)[model][index], error_of(kind, ended))
    return stops.note(model, kind, now)


def setup(tmp_path, **config):
    store, _, _ = planned_store(tmp_path, models=MODELS, seeds=(1, 2, 3))
    return store, HarnessStops(store, HarnessStopConfig(**config))


def test_c10_three_harness_errors_of_a_kind_stop_that_model_and_no_other(tmp_path):
    """C10: three harness errors for one model stop its dispatch; the others go on."""
    store, stops = setup(tmp_path)
    assert fail(store, stops, "m1", 0) == []
    assert fail(store, stops, "m1", 1) == []
    assert stops.stopped() == (frozenset(), False)
    [reason] = fail(store, stops, "m1", 2)
    assert "3 harness errors of kind ValueError on m1" in reason
    assert stops.stopped() == (frozenset({"m1"}), False)
    event = store.q("SELECT kind, model FROM events WHERE kind='harness_error_stop'")[0]
    assert (event["kind"], event["model"]) == ("harness_error_stop", "m1")


def test_c10_errors_of_different_kinds_do_not_pool(tmp_path):
    """C10: the kind is the exception class, so two different bugs do not add up to one stop."""
    store, stops = setup(tmp_path)
    for index, kind in enumerate(("ValueError", "KeyError", "TypeError")):
        assert fail(store, stops, "m1", index, kind) == []
    assert stops.stopped() == (frozenset(), False)


def test_c10_one_kind_on_a_wider_spread_of_models_stops_all_dispatch_and_records_the_event(tmp_path):
    """C10: a wider spread stops all dispatch, recorded as an event naming the models."""
    store, stops = setup(tmp_path)
    fail(store, stops, "m1", 0)
    fail(store, stops, "m2", 0)
    assert stops.stopped() == (frozenset(), False)
    [reason] = fail(store, stops, "m3", 0)
    assert "kind ValueError on 3 models (m1, m2, m3)" in reason
    models, everything = stops.stopped()
    assert models == frozenset()
    assert everything
    assert store.q("SELECT kind FROM events WHERE kind='harness_error_stop_all'")


def test_c10_errors_outside_the_window_do_not_count_and_a_stop_that_fired_stays(tmp_path):
    """C10: the window is read from store times; a stop once fired is never ended by the window moving on."""
    store, stops = setup(tmp_path, window_s=3600)
    old = "2025-12-31T00:00:00Z"
    fail(store, stops, "m1", 0, ended=old)
    fail(store, stops, "m1", 1, ended=old)
    assert fail(store, stops, "m1", 2) == []  # two are a day old: one counts
    fail(store, stops, "m2", 0)
    fail(store, stops, "m2", 1)
    assert fail(store, stops, "m2", 2)  # three inside the hour
    assert stops.note("m1", "ValueError", "2030-01-01T00:00:00Z") == []  # years later the stop still holds
    assert stops.stopped()[0] == frozenset({"m2"})


def test_c10_a_record_of_stops_that_cannot_be_read_stops_everything(tmp_path):
    """C10: fail closed: nothing says what was stopped, so everything is."""
    store, stops = setup(tmp_path)
    store.put_meta(KEY, "{not json", NOW)
    models, everything = stops.stopped()
    assert models == frozenset()
    assert everything
    assert stops.status()["unreadable"]
    assert stops.note("m1", "ValueError", LATER) == []  # it does not mend the record by itself
    assert stops.stopped()[1]


def test_c10_a_release_written_after_the_stop_ends_it_once_and_the_old_errors_stop_counting(tmp_path):
    """C10: a model's name in the release file ends that model's stop; the errors up to then no longer count."""
    path = tmp_path / "release"
    store, stops = setup(tmp_path, release_file=path)
    for index in range(3):
        fail(store, stops, "m1", index)
    path.write_text("m1\n", encoding="utf-8")
    os.utime(path, (epoch_of(LATER) + 5, epoch_of(LATER) + 5))
    released = stops.take_releases("2026-01-01T00:20:00Z")
    assert set(released) == {"m1"}
    assert not path.exists()
    assert stops.stopped() == (frozenset(), False)
    # The three old errors are cleared: two more are not enough, and a third inside the window stops it again.
    later = "2026-01-01T00:21:00Z"
    assert fail(store, stops, "m1", 3, ended=later, now=later) == []
    assert fail(store, stops, "m1", 4, ended=later, now=later) == []
    assert fail(store, stops, "m1", 5, ended=later, now=later)
    assert stops.stopped()[0] == frozenset({"m1"})


def test_c10_a_release_older_than_its_stop_or_for_a_stop_not_in_force_ends_nothing(tmp_path):
    """C10: a stale file does not end the stop, and a name with no stop in force is left in the file."""
    path = tmp_path / "release"
    store, stops = setup(tmp_path, release_file=path)
    for index in range(3):
        fail(store, stops, "m1", index)
    path.write_text("m1\nm2\n", encoding="utf-8")
    os.utime(path, (epoch_of(LATER) - 3600, epoch_of(LATER) - 3600))
    assert stops.take_releases("2026-01-01T00:20:00Z") == {}
    assert stops.stopped()[0] == frozenset({"m1"})
    assert path.exists()
    os.utime(path, (epoch_of(LATER) + 5, epoch_of(LATER) + 5))
    assert set(stops.take_releases("2026-01-01T00:20:00Z")) == {"m1"}
    assert path.read_text(encoding="utf-8").split() == ["m2"]  # m2 has no stop: its line stays


def test_c10_the_word_all_ends_the_all_stop(tmp_path):
    """C10: ALL ends the stop on all dispatch, and it is recorded as an event."""
    path = tmp_path / "release"
    store, stops = setup(tmp_path, release_file=path)
    for model in MODELS:
        fail(store, stops, model, 0)
    assert stops.stopped()[1]
    path.write_text(f"{ALL}\n", encoding="utf-8")
    os.utime(path, (epoch_of(LATER) + 5, epoch_of(LATER) + 5))
    assert set(stops.take_releases("2026-01-01T00:20:00Z")) == {ALL}
    assert stops.stopped() == (frozenset(), False)
    assert store.q("SELECT kind FROM events WHERE kind='harness_error_released_all'")


def test_c10_without_a_release_file_nothing_is_released(tmp_path):
    """C10: a campaign configured with no release file ends its stops only by an operator editing the store."""
    store, stops = setup(tmp_path)
    for index in range(3):
        fail(store, stops, "m1", index)
    assert stops.take_releases(LATER) == {}
    assert stops.stopped()[0] == frozenset({"m1"})


def test_c10_a_harness_error_requeues_from_the_same_row_and_keeps_its_failed_attempt(tmp_path):
    """C10: the run goes back to pending from the same row at the next open; the failed attempt stays."""
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    assert run_one(store, key, defect()) == "harness_error"
    assert recover(store, LATER, 3).requeued == 1
    assert store.q("SELECT status FROM runs WHERE run_key=?", (key,))[0]["status"] == "pending"
    assert [r["state"] for r in store.q("SELECT state FROM attempts WHERE run_key=?", (key,))] == ["harness_error"]


def test_c10_the_kind_is_the_exception_class_of_the_defect_and_of_the_stored_detail(tmp_path):
    """C10: the outcome and the detail the store writes name the same kind."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, manifest.rows[0].key.text, error_of("KeyError"))
    detail = store.q("SELECT detail FROM attempts")[0]["detail"]
    assert kind_of_detail(detail) == "KeyError"
    assert kind_of(error_of("KeyError").outcome) == "KeyError"
    assert kind_of_detail(None) == kind_of_detail("") == "unknown"


def test_c10_the_limits_are_checked():
    """C10: limits that cannot work are refused."""
    with pytest.raises(ValueError, match="at least 1"):
        HarnessStopConfig(stop_after=0)
    with pytest.raises(ValueError, match="window_s"):
        HarnessStopConfig(window_s=0)


def stored_errors_without_notes(store, model, count, start=0, kind="ValueError", ended=LATER):
    """Make runs end as harness errors without telling the stops: what a result committed from its marker leaves."""
    for index in range(start, start + count):
        run_one(store, keys_by_model(store)[model][index], error_of(kind, ended))


def test_c10_the_stops_the_stored_errors_imply_are_derived_when_the_record_lacks_them(tmp_path):
    """C10: three same-kind errors in the store and no record of a stop: reconcile fires it once, with its event."""
    store, stops = setup(tmp_path)
    stored_errors_without_notes(store, "m1", 3)
    assert stops.stopped() == (frozenset(), False)
    [reason] = stops.reconcile("2026-01-01T00:30:00Z")
    assert "3 harness errors of kind ValueError on m1" in reason
    assert stops.stopped() == (frozenset({"m1"}), False)
    assert stops.reconcile("2026-01-01T00:31:00Z") == []  # again changes nothing
    events = store.q("SELECT detail FROM events WHERE kind='harness_error_stop'")
    assert len(events) == 1
    assert "derived at the start from the stored attempts" in events[0]["detail"]
    assert stops.load()["models"]["m1"]["utc"] == LATER  # the stop began when the third error ended, not at the start


def test_c10_the_derivation_stops_all_dispatch_for_one_kind_on_a_wide_spread_and_not_for_two_models(tmp_path):
    """C10: the all-stop is derived from the stored errors too; two models are below its limit."""
    store, stops = setup(tmp_path)
    stored_errors_without_notes(store, "m1", 1)
    stored_errors_without_notes(store, "m2", 1)
    assert stops.reconcile(LATER) == []
    stored_errors_without_notes(store, "m3", 1)
    [reason] = stops.reconcile(LATER)
    assert "kind ValueError on 3 models" in reason
    assert stops.stopped()[1]


def test_c10_the_derivation_counts_only_what_a_stop_would_have_counted(tmp_path):
    """C10: errors outside the window, of other kinds, or too few stop nothing."""
    store, stops = setup(tmp_path, window_s=3600.0)
    stored_errors_without_notes(store, "m1", 1, ended="2026-01-01T00:00:00Z")
    stored_errors_without_notes(store, "m1", 1, start=1, ended="2026-01-01T05:00:00Z")  # two hours past the first
    stored_errors_without_notes(store, "m1", 1, start=2, ended="2026-01-01T05:10:00Z", kind="KeyError")
    assert stops.reconcile("2026-01-01T06:00:00Z") == []
    assert stops.stopped() == (frozenset(), False)


def test_c10_the_derivation_leaves_a_released_stop_released(tmp_path):
    """C10: errors up to a release stop counting, so a restart after it does not stop the model again."""
    path = tmp_path / "release"
    store, stops = setup(tmp_path, release_file=path)
    for index in range(3):
        fail(store, stops, "m1", index)
    path.write_text("m1\n", encoding="utf-8")
    os.utime(path, (epoch_of(LATER) + 5, epoch_of(LATER) + 5))
    assert set(stops.take_releases("2026-01-01T00:20:00Z")) == {"m1"}
    assert stops.reconcile("2026-01-01T00:25:00Z") == []
    assert stops.stopped() == (frozenset(), False)
    stored_errors_without_notes(store, "m1", 2, start=3, ended="2026-01-01T00:22:00Z")  # two after the release: too few
    assert stops.reconcile("2026-01-01T00:25:00Z") == []


def test_c10_the_derivation_does_not_mend_an_unreadable_record(tmp_path):
    """C10: a record nobody can read stays all-stopped; reconcile does not overwrite it."""
    store, stops = setup(tmp_path)
    stored_errors_without_notes(store, "m1", 3)
    store.put_meta(KEY, "{broken", NOW)
    assert stops.reconcile(LATER) == []
    assert stops.stopped()[1]
    assert store.get_meta(KEY) == "{broken"


def test_c10_the_derivation_replays_by_completion_time_so_a_later_error_cannot_fall_into_an_earlier_window(tmp_path):
    """C10: attempts that began in one order and ended in another: errors ending at 10 h, 1 h and 2 h with a 6 h
    window are not three errors of one window, so no stop is invented."""
    store, stops = setup(tmp_path, window_s=6 * 3600.0)
    for index, ended in enumerate(("2026-01-01T10:00:00Z", "2026-01-01T01:00:00Z", "2026-01-01T02:00:00Z")):
        stored_errors_without_notes(store, "m1", 1, start=index, ended=ended)
    assert stops.reconcile("2026-01-01T11:00:00Z") == []
    assert stops.stopped() == (frozenset(), False)


def test_c10_the_derivation_fires_the_stop_at_the_third_error_to_end_whatever_order_the_attempts_began_in(tmp_path):
    """C10: three errors ending within the window stop the model, and the stop begins when the last of them ended."""
    store, stops = setup(tmp_path, window_s=6 * 3600.0)
    for index, ended in enumerate(("2026-01-01T03:00:00Z", "2026-01-01T01:00:00Z", "2026-01-01T02:00:00Z")):
        stored_errors_without_notes(store, "m1", 1, start=index, ended=ended)
    [reason] = stops.reconcile("2026-01-01T11:00:00Z")
    assert "3 harness errors of kind ValueError on m1" in reason
    assert stops.load()["models"]["m1"]["utc"] == "2026-01-01T03:00:00Z"


def test_c13_a_release_written_earlier_in_the_second_than_the_stop_does_not_end_it(tmp_path):
    """C13: the stop is dated at full precision: a release at .200 of the second does not pass for a stop at .800."""
    path = tmp_path / "release"
    store, stops = setup(tmp_path, release_file=path)
    for index in range(2):
        fail(store, stops, "m1", index)
    run_one(store, keys_by_model(store)["m1"][2], error_of())
    second = "2026-01-01T00:10:05Z"
    stops.note("m1", "ValueError", second, exact="2026-01-01T00:10:05.800000Z")
    assert stops.load()["models"]["m1"]["epoch"] == pytest.approx(epoch_of(second) + 0.8)
    path.write_text("m1\n", encoding="utf-8")
    os.utime(path, (epoch_of(second) + 0.2, epoch_of(second) + 0.2))
    assert stops.take_releases("2026-01-01T00:10:06Z") == {}  # older than the stop, though in the same second
    assert path.exists()
    assert stops.stopped()[0] == frozenset({"m1"})
    os.utime(path, (epoch_of(second) + 0.9, epoch_of(second) + 0.9))
    assert set(stops.take_releases("2026-01-01T00:10:06Z")) == {"m1"}


def test_c13_a_stop_derived_at_the_start_is_dated_at_the_end_of_the_second_its_error_ended_in(tmp_path):
    """C13: the error's time is known to the second, so the derived stop is the latest it can have been."""
    path = tmp_path / "release"
    store, stops = setup(tmp_path, release_file=path)
    stored_errors_without_notes(store, "m1", 3)
    stops.reconcile("2026-01-01T00:30:00Z")
    assert stops.load()["models"]["m1"]["epoch"] == pytest.approx(epoch_of(LATER) + 1.0)
    path.write_text("m1\n", encoding="utf-8")
    os.utime(path, (epoch_of(LATER) + 0.5, epoch_of(LATER) + 0.5))
    assert stops.take_releases("2026-01-01T00:30:01Z") == {}
