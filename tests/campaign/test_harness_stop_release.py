"""A harness-error stop ends only by the release line that names its serial, whatever the file's time says (C13)."""

import json
import logging
import os
import re

import pytest
from campaign_support import LATER, planned_store, run_one

from expcore.campaign import release
from expcore.campaign.harness_stop import ALL, KEY, HarnessStopConfig, HarnessStops
from expcore.campaign.markers import AttemptResult
from expcore.campaign.release import epoch_of
from expcore.campaign.snapshot import take_snapshot
from expcore.campaign.store import Store
from expcore.loop.outcomes import HarnessDefect, Outcome

MODELS = ("m1", "m2", "m3")
SERIAL = re.compile(r"[0-9]+\.[a-z2-7]{8}")
RELEASED_AT = "2026-01-01T00:20:00Z"
AFTER = "2026-01-01T00:21:00Z"  # errors after the release count again
LOG = "expcore.campaign.release"


def error_of(ended=LATER):
    return AttemptResult(Outcome("harness_defect", "boom", defect=HarnessDefect("ValueError", "c" * 64)), ended, 1.0)


def keys_of(store, model):
    return [r["run_key"] for r in store.q("SELECT run_key FROM runs WHERE f_model=? ORDER BY seq", (model,))]


def fail(store, stops, model, index, ended=LATER, exact=None):
    """Make the model's index-th run end as a harness error and tell the stops, as the scheduler does."""
    run_one(store, keys_of(store, model)[index], error_of(ended))
    return stops.note(model, "ValueError", ended, exact=exact)


def stop_m1(store, stops, start=0, ended=LATER, exact=None):
    """Stop m1 with three harness errors; return the stop's serial."""
    for index in range(start, start + 3):
        fail(store, stops, "m1", index, ended, exact)
    return stops.status()["serials"]["m1"]


def setup(tmp_path, release_file=True):
    store, _, _ = planned_store(tmp_path, models=MODELS, seeds=(1, 2, 3))
    path = tmp_path / "release" if release_file else None
    return store, HarnessStops(store, HarnessStopConfig(release_file=path)), path


def reopen(store, path, cfg):
    """A restart: the store closed and opened again from its file, and the stops read from it."""
    schema = store.schema
    store.close()
    again = Store(path, schema)
    return again, HarnessStops(again, cfg)


def test_c13_each_stop_is_given_a_serial_shown_in_its_event_its_hold_and_the_status(tmp_path):
    """C13: the model's stop and the all-stop each get a serial of their own, and the hold says the line to write."""
    store, stops, _ = setup(tmp_path)
    serial = stop_m1(store, stops)
    assert SERIAL.fullmatch(serial)
    [event] = store.q("SELECT detail FROM events WHERE kind='harness_error_stop'")
    assert f"dispatch of m1 stops (stop {serial})" in event["detail"]
    models, every = stops.hold_reasons()
    assert models["m1"] == (
        f"dispatch of m1 is stopped by harness errors (stop {serial}; to end it, write the line 'm1 {serial}' in "
        "the harness stop's release file)"
    )
    assert every is None
    fail(store, stops, "m2", 0)
    fail(store, stops, "m3", 0)
    every_serial = stops.status()["serials"][ALL]
    assert every_serial.split(".")[0] == str(int(serial.split(".")[0]) + 1)
    assert f"write the line 'ALL {every_serial}'" in stops.hold_reasons()[1]
    assert f"(stop {every_serial})" in store.q("SELECT detail FROM events WHERE kind='harness_error_stop_all'")[0][0]


def test_c13_without_a_release_file_the_hold_names_its_serial_and_no_line(tmp_path):
    """C13: a campaign without a release file is told the stop, not a line nothing would read."""
    store, stops, _ = setup(tmp_path, release_file=False)
    serial = stop_m1(store, stops)
    assert stops.hold_reasons()[0]["m1"] == f"dispatch of m1 is stopped by harness errors (stop {serial})"


@pytest.mark.parametrize("offset", [-3600.0, -0.0015, 0.0, 1.0], ids=["behind", "a-tick-behind", "same-tick", "ahead"])
def test_c13_the_stops_own_line_ends_it_once_whatever_time_the_file_carries(tmp_path, offset):
    """C13: a file stamped by a coarse clock (a tick before the stop it follows), by a server behind or ahead, or in
    the stop's own tick: its time changes nothing, and the stop's own line ends it once."""
    store, stops, path = setup(tmp_path)
    serial = stop_m1(store, stops, exact="2026-01-01T00:10:00.800000Z")
    stopped_at = stops.load()["models"]["m1"]["epoch"]
    assert stopped_at == pytest.approx(epoch_of(LATER) + 0.8)  # dated at full precision, for display
    path.write_text(f"m1 {serial}\n", encoding="utf-8")
    os.utime(path, (stopped_at + offset, stopped_at + offset))
    assert set(stops.take_releases(RELEASED_AT)) == {"m1"}
    assert not path.exists()
    assert stops.stopped() == (frozenset(), False)
    assert stops.take_releases(RELEASED_AT) == {}


@pytest.mark.parametrize("ahead", [0.0, 1.0, 3600.0])
def test_c13_a_file_written_before_the_stop_ends_nothing_even_stamped_after_it(tmp_path, ahead, caplog):
    """C13: a bare name or the line of an earlier stop, written before this stop by a writer whose clock is ahead,
    ends nothing: it stays, and the warning gives the line that ends the stop in force."""
    store, stops, path = setup(tmp_path)
    first = stop_m1(store, stops)
    path.write_text(f"m1 {first}\n", encoding="utf-8")
    assert set(stops.take_releases(RELEASED_AT)) == {"m1"}
    path.write_text(f"m1\nm1 {first}\n", encoding="utf-8")  # written now, before the next stop
    second = stop_m1(store, stops, start=3, ended=AFTER, exact="2026-01-01T00:21:00.500000Z")
    assert second != first
    stamp = stops.load()["models"]["m1"]["epoch"] + ahead
    os.utime(path, (stamp, stamp))
    with caplog.at_level(logging.WARNING, logger=LOG):
        assert stops.take_releases(AFTER) == {}
    assert stops.stopped()[0] == frozenset({"m1"})
    assert path.read_text(encoding="utf-8").splitlines() == ["m1", f"m1 {first}"]
    warned = [r.getMessage() for r in caplog.records]
    assert len(warned) == 2
    assert all(f"the line 'm1 {second}' ends it" in text for text in warned)
    assert any("'<name> <serial>'" in text for text in warned)


def test_c13_a_refused_line_is_recorded_once_however_many_passes_read_it(tmp_path):
    """C13: the dispatcher reads the file at every pass; the refusal is one event, not one per pass."""
    store, stops, path = setup(tmp_path)
    stop_m1(store, stops)
    path.write_text("m1\n", encoding="utf-8")
    for _ in range(4):
        assert stops.take_releases(RELEASED_AT) == {}
    assert len(store.q("SELECT 1 FROM events WHERE kind='harness_error_release_refused'")) == 1


def test_c13_a_restart_keeps_the_serial_and_a_later_stop_never_gets_one_given_before(tmp_path):
    """C13: the serial is in the store, the count goes on across a restart, and an old stop's line ends no new stop."""
    store, stops, path = setup(tmp_path)
    first = stop_m1(store, stops)
    store, stops = reopen(store, tmp_path / "campaign.db", stops.cfg)
    assert stops.reconcile(RELEASED_AT) == []
    assert stops.status()["serials"]["m1"] == first
    path.write_text(f"m1 {first}\n", encoding="utf-8")
    assert set(stops.take_releases(RELEASED_AT)) == {"m1"}
    store, stops = reopen(store, tmp_path / "campaign.db", stops.cfg)
    second = stop_m1(store, stops, start=3, ended=AFTER)
    assert int(second.split(".")[0]) == int(first.split(".")[0]) + 1
    path.write_text(f"m1 {first}\n", encoding="utf-8")
    assert stops.take_releases(AFTER) == {}
    path.write_text(f"m1 {second}\n", encoding="utf-8")
    assert set(stops.take_releases(AFTER)) == {"m1"}


def test_c13_a_stop_recorded_before_serials_is_given_one_at_the_start_and_a_bare_name_is_refused(tmp_path, caplog):
    """C13: a store from before serials: its stop is given a serial at the start, kept across a restart; the bare name,
    the old form, ends nothing and is refused with the line to write, which then ends the stop once."""
    store, stops, path = setup(tmp_path)
    for model in MODELS:  # m1 stopped, and the all-stop, both recorded as before serials
        fail(store, stops, model, 0)
    stop_m1(store, stops, start=1)
    old = stops.load()
    del old["models"]["m1"]["serial"], old["all"]["serial"]
    store.put_meta(KEY, json.dumps(old), LATER)
    store, stops = reopen(store, tmp_path / "campaign.db", stops.cfg)
    assert stops.reconcile(RELEASED_AT) == []
    numbered = store.q("SELECT model, detail FROM events WHERE kind='harness_error_stop_numbered' ORDER BY id")
    serial, every = stops.status()["serials"]["m1"], stops.status()["serials"][ALL]
    assert [(e["model"], e["detail"]) for e in numbered] == [
        ("m1", f"the stop on m1, recorded without a serial, is stop {serial}"),
        (None, f"the all-stop, recorded without a serial, is stop {every}"),
    ]
    assert f"write the line 'm1 {serial}'" in stops.hold_reasons()[0]["m1"]
    path.write_text("m1\nALL\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=LOG):
        assert stops.take_releases(RELEASED_AT) == {}
    assert any("'<name> <serial>'" in r.getMessage() and f"'m1 {serial}'" in r.getMessage() for r in caplog.records)
    store, stops = reopen(store, tmp_path / "campaign.db", stops.cfg)
    assert stops.reconcile(RELEASED_AT) == []
    assert stops.status()["serials"] == {"m1": serial, ALL: every}  # numbered once, never again
    path.write_text(f"m1 {serial}\nALL {every}\n", encoding="utf-8")
    assert set(stops.take_releases(RELEASED_AT)) == {"m1", ALL}
    assert stops.stopped() == (frozenset(), False)


def test_c13_a_stop_lost_in_a_restore_gives_its_line_to_no_later_stop(tmp_path, caplog):
    """C13: a store copied back from a snapshot taken before a stop gives the next stop the same count, but its random
    part differs: the lost stop's exact line, written before the restore and still in the file, is refused and named;
    the new stop's own line ends it once."""
    store, stops, path = setup(tmp_path)
    live = tmp_path / "campaign.db"
    take_snapshot(live, tmp_path / "snapshot.db", in_place=True)
    lost = stop_m1(store, stops)
    path.write_text(f"m1 {lost}\n", encoding="utf-8")  # the person releases the stop they saw; the host dies first
    schema = store.schema
    store.close()
    for leftover in tmp_path.glob("campaign.db*"):
        leftover.unlink()
    (tmp_path / "snapshot.db").rename(live)  # the restore: the store as the snapshot holds it
    store = Store(live, schema)
    stops = HarnessStops(store, stops.cfg)
    assert stops.load()["models"] == {}  # the copy has no stop
    new = stop_m1(store, stops)
    assert new.split(".")[0] == lost.split(".")[0]  # the count came back with the copy
    assert new != lost
    with caplog.at_level(logging.WARNING, logger=LOG):
        assert stops.take_releases(RELEASED_AT) == {}
    assert stops.stopped()[0] == frozenset({"m1"})
    [warned] = [r.getMessage() for r in caplog.records]
    assert f"the release line 'm1 {lost}' ends nothing" in warned
    assert f"the line 'm1 {new}' ends it" in warned
    with path.open("a", encoding="utf-8") as file:
        file.write(f"m1 {new}\n")
    assert set(stops.take_releases(RELEASED_AT)) == {"m1"}
    assert path.read_text(encoding="utf-8").splitlines() == [f"m1 {lost}"]
    assert stops.take_releases(RELEASED_AT) == {}


def test_c13_a_line_used_up_for_a_stop_that_changed_as_the_file_was_read_ends_nothing(tmp_path, monkeypatch):
    """C13: the stop is ended only if the one in force when the release is recorded carries the serial matched."""
    store, stops, path = setup(tmp_path)
    serial = stop_m1(store, stops)
    path.write_text(f"m1 {serial}\n", encoding="utf-8")
    real = release.take_listed

    def racing(*args):
        taken = real(*args)
        state = stops.load()  # another writer ended the stop and a new one began, with a serial of its own
        state["models"]["m1"]["serial"] = release.next_serial(store, LATER)
        store.put_meta(KEY, json.dumps(state), LATER)
        return taken

    monkeypatch.setattr(release, "take_listed", racing)
    assert stops.take_releases(RELEASED_AT) == {}
    assert stops.stopped()[0] == frozenset({"m1"})
    assert not store.q("SELECT 1 FROM events WHERE kind='harness_error_released'")


def test_c13_a_stop_derived_at_the_start_is_released_by_its_serial_before_the_end_of_its_second(tmp_path):
    """C13: a derived stop is dated at the end of its error's second, for display; a file stamped earlier in that
    second still ends it, by its line."""
    store, stops, path = setup(tmp_path)
    for index in range(3):
        run_one(store, keys_of(store, "m1")[index], error_of())
    stops.reconcile("2026-01-01T00:30:00Z")
    stop = stops.load()["models"]["m1"]
    assert stop["epoch"] == pytest.approx(epoch_of(LATER) + 1.0)
    path.write_text(f"m1 {stop['serial']}\n", encoding="utf-8")
    os.utime(path, (epoch_of(LATER) + 0.5, epoch_of(LATER) + 0.5))
    assert set(stops.take_releases("2026-01-01T00:30:01Z")) == {"m1"}
