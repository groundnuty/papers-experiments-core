"""Progressive ingest: files of a dataset arrive during a run on their schedule, each verified, every id recorded, and
the dataset restored to its declared baseline afterwards without losing what the study keeps (O27)."""

import json
import threading
import time

import pytest

from expcore.errors import InfraError, is_treatable
from expcore_onedata.ingest import NAMESPACE, ProgressiveIngest
from expcore_onedata.ingestplan import load_ingest_plan
from expcore_onedata.sandbox.kinds import INGEST_FAILURE
from expcore_onedata.testing import BETA_ID, PROVIDER, SPACE_NAME, with_placement, write_platform

DAY = bytes((i * 31 + i // 97) % 256 for i in range(10240))  # the dataset's last day, which a run sees arrive
KEPT = b"an earlier day, never touched\n" * 20
NOTE = b"a new note the owner adds\n" * 15
DAY_PATH, KEPT_PATH, NOTE_PATH = "records/day-13.csv", "records/day-12.csv", "records/note.txt"


def two_providers(data):
    with_placement(data)
    data["support"]["providers"] = ["alpha", "beta"]


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, two_providers)


@pytest.fixture
def fake(fake):
    """The placement fixture's federation with beta supporting the space too, and the dataset's baseline: two days of
    records, the data provider holding both."""
    fake.add_provider(BETA_ID, "beta.test")
    fake.add_file(KEPT_PATH, KEPT)
    fake.add_file(DAY_PATH, DAY)
    return fake


def write_plan(tmp_path, files):
    """A plan over a staging directory holding the day and the note; `files` are its [[ingest.files]] rows."""
    staging = tmp_path / "staged"
    staging.mkdir(exist_ok=True)
    (staging / "day-13.csv").write_bytes(DAY)
    (staging / "note.txt").write_bytes(NOTE)
    rows = "".join(
        "\n[[ingest.files]]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in row.items())
        for row in files
    )
    path = tmp_path / "ingest.toml"
    path.write_text('[ingest]\nstaging = "staged"\n' + rows, encoding="utf-8")
    return path


DAY_ROW = {"label": "day-13", "path": DAY_PATH, "source": "day-13.csv", "at_s": 300, "baseline": "complete"}
NOTE_ROW = {"label": "note", "path": NOTE_PATH, "source": "note.txt", "at_s": 100, "baseline": "absent"}


@pytest.fixture
def make(provisioner, config, tmp_path):
    """Build an instrument over a plan of the given rows; every instrument a test builds shares the journal and the
    provisioner's change log, as one study's runs do."""
    built = []

    def build(*rows):
        plan = load_ingest_plan(write_plan(tmp_path, rows), config)
        ingest = ProgressiveIngest(provisioner, plan, journal=tmp_path / "journal.jsonl", max_wait_s=0.001)
        built.append(ingest)
        return ingest

    yield build
    for ingest in built:  # no schedule outlives its test
        ingest.stop()


def until(predicate, timeout_s=10.0):
    """Wait, in real time and bounded, for the schedule's thread to reach a state."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "the schedule did not reach the expected state"
        time.sleep(0.001)


def run_to(ingest, ticker, t0, seconds, progress):
    """Move the clock, which only the parked schedule reads, to `seconds` after the run's start, and wait until the
    schedule has carried out `progress` steps."""
    ticker.now = max(ticker.now, t0 + seconds)
    until(lambda: ingest.progress >= progress)


def content(fake, path):
    return bytes(fake.nodes[fake.id_of(path)].content) if fake.exists(path) else None


def seen_by(fake, provider_id, path):
    node = fake.nodes[fake.id_of(path)]
    return fake._visible(node, provider_id)


# -- the schedule ------------------------------------------------------------------------------------------------------


def test_o27_files_arrive_on_their_schedule_and_not_before(make, fake, ticker):
    """O27: at the start the planned files are absent (the day removed, the note never there); at 100 s the note
    arrives whole and the day is still absent; at 300 s the day arrives. Each write is recorded after its due time,
    in order, with how late it was."""
    ingest = make(DAY_ROW, NOTE_ROW)
    ingest.start({"run_key": "r1"})
    t0 = ticker.now
    assert (content(fake, DAY_PATH), content(fake, NOTE_PATH)) == (None, None)
    assert content(fake, KEPT_PATH) == KEPT
    run_to(ingest, ticker, t0, 99, 0)
    assert ingest.progress == 0
    assert content(fake, NOTE_PATH) is None
    run_to(ingest, ticker, t0, 100, 1)
    assert (content(fake, NOTE_PATH), content(fake, DAY_PATH)) == (NOTE, None)
    run_to(ingest, ticker, t0, 300, 2)
    assert content(fake, DAY_PATH) == DAY
    snapshot = ingest.finish()
    written = [s for s in snapshot["steps"] if s["step"] == "written"]
    assert [(s["label"], s["due_s"]) for s in written] == [("note", 100.0), ("day-13", 300.0)]
    assert all(s["t_rel_s"] >= s["due_s"] and s["late_s"] >= 0 for s in written)
    assert [s["label"] for s in snapshot["steps"] if s["step"] == "planned"] == ["note", "day-13"]
    assert snapshot["ok"] is True
    assert snapshot["anchor"] == "instrument_start"
    assert {label: f["state"] for label, f in snapshot["files"].items()} == {"day-13": "arrived", "note": "arrived"}
    ingest.raise_if_failed()


def test_o27_a_file_in_parts_is_missing_then_partial_then_whole(make, fake, ticker):
    """O27: a file written in four parts over 300 s is absent before its time, then a quarter, a half, three quarters
    and finally whole, each part on its own schedule and each verified as the prefix the plan hashed."""
    ingest = make({**DAY_ROW, "at_s": 100, "parts": 4, "over_s": 300})
    ingest.start({})
    t0 = ticker.now
    assert content(fake, DAY_PATH) is None
    for part, due in enumerate((100, 200, 300, 400), start=1):
        run_to(ingest, ticker, t0, due, part)
        assert content(fake, DAY_PATH) == DAY[: len(DAY) * part // 4]
    snapshot = ingest.finish()
    confirmed = [s for s in snapshot["steps"] if s["step"] == "confirmed"]
    assert [(s["part"], s["bytes"]) for s in confirmed] == [(1, 2560), (2, 5120), (3, 7680), (4, 10240)]
    assert snapshot["files"]["day-13"]["state"] == "arrived"


def test_o27_each_arrival_is_verified_on_every_provider_that_must_see_it(make, fake, ticker):
    """O27: a second provider sees a new file only after 6 s; the arrival is confirmed once the data provider's block
    map covers it and the second provider shows it at its size, and the reading says how long that took."""
    fake.visibility_delay_s = 6.0
    ingest = make(NOTE_ROW)
    ingest.start({})
    t0 = ticker.now
    run_to(ingest, ticker, t0, 100, 1)
    assert seen_by(fake, BETA_ID, NOTE_PATH)
    steps = ingest.finish()["steps"]
    written, confirmed = (next(s for s in steps if s["step"] == kind) for kind in ("written", "confirmed"))
    assert confirmed["holder_covered"] is True
    assert confirmed["visible_s"]["providers[1]"] > 0  # it waited for the second provider
    assert confirmed["t_rel_s"] - written["t_rel_s"] >= 6.0
    assert "providers[0]" not in confirmed["visible_s"]  # the data provider wrote it


# -- failures ----------------------------------------------------------------------------------------------------------


def test_o27_an_arrival_another_provider_never_shows_is_an_ingest_failure_and_nothing_later_is_skipped_silently(
    make, fake, ticker
):
    """O27: the second provider never shows the note. Past the visibility bound the arrival fails as an ingest
    failure; the day that was due later is not written and is recorded as abandoned, not dropped; the attempt is set
    aside through `raise_if_failed`."""
    ingest = make(NOTE_ROW, DAY_ROW)
    ingest.start({})
    t0 = ticker.now
    fake.visibility_delay_by_provider[BETA_ID] = 10**9  # from here on, nothing new shows on the second provider
    ticker.now = t0 + 100
    until(lambda: ingest.failure is not None)
    snapshot = ingest.finish()
    assert snapshot["ok"] is False
    assert snapshot["failure"]["kind"] == INGEST_FAILURE
    assert (snapshot["failure"]["label"], snapshot["failure"]["error"]) == ("note", "NotVisible")
    assert [(s["label"], s["step"]) for s in snapshot["steps"] if s["step"] in ("failed", "abandoned")] == [
        ("note", "failed"),
        ("day-13", "abandoned"),
    ]
    with pytest.raises(InfraError) as raised:
        ingest.raise_if_failed()
    assert raised.value.kind == INGEST_FAILURE


def test_o27_a_refused_write_is_an_ingest_failure_and_the_kind_is_never_a_treatment(make, fake, ticker):
    """O27: the platform answers a write with a 500. The arrival fails as `ingest_failure`, which no condition may
    declare as its treatment, and the dataset is still restored after the run."""
    ingest = make(DAY_ROW)
    ingest.start({})
    t0 = ticker.now
    fake.statuses = [["/content", 1, 500]]
    ticker.now = t0 + 300
    until(lambda: ingest.failure is not None)
    snapshot = ingest.finish()
    assert (snapshot["failure"]["kind"], snapshot["failure"]["error"]) == (INGEST_FAILURE, "PlatformError")
    with pytest.raises(InfraError, match="ingest_failure"):
        ingest.raise_if_failed()
    assert is_treatable(INGEST_FAILURE) is False
    assert snapshot["restore"]["ok"] is True
    assert content(fake, DAY_PATH) == DAY


def test_o27_a_file_at_a_planned_path_that_is_neither_the_ingests_nor_its_source_is_refused_and_kept(
    make, fake, tmp_path
):
    """O27 with O09: the day in the space differs from its staged copy and the ingest did not make it, so removing it
    could lose data. The start refuses as an ingest failure and sends no delete; the file keeps its content."""
    fake.nodes[fake.id_of(DAY_PATH)].content[5] ^= 0xFF
    changed = content(fake, DAY_PATH)
    ingest = make(DAY_ROW)
    with pytest.raises(InfraError) as raised:
        ingest.start({})
    assert raised.value.kind == INGEST_FAILURE
    assert not [call for call in fake.provider_calls if call[1] == "DELETE"]
    assert content(fake, DAY_PATH) == changed


def test_o27_a_staged_source_changed_after_the_plan_was_loaded_refuses_the_start_and_removes_nothing(
    make, fake, tmp_path
):
    """O27: the copy the study keeps is checked against the plan's hash before anything is removed."""
    ingest = make(DAY_ROW)
    (tmp_path / "staged" / "day-13.csv").write_bytes(DAY[:-1] + b"?")
    with pytest.raises(InfraError, match="staged source"):
        ingest.start({})
    assert content(fake, DAY_PATH) == DAY
    assert not [call for call in fake.provider_calls if call[1] == "DELETE"]


def test_o27_a_part_still_being_written_past_the_bound_is_an_ingest_failure_and_the_restore_waits(
    provisioner, config, fake, ticker, tmp_path, monkeypatch
):
    """O27: a write that does not return within the stop's bound is never abandoned in silence: the stop reports it,
    the restore refuses to race it, and the attempt is an ingest failure."""
    plan = load_ingest_plan(write_plan(tmp_path, [NOTE_ROW]), config)
    ingest = ProgressiveIngest(provisioner, plan, journal=tmp_path / "j.jsonl", max_wait_s=0.001, join_timeout_s=0.05)
    entered, release = threading.Event(), threading.Event()
    real = provisioner.upload

    def held(*args, **kwargs):
        entered.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(provisioner, "upload", held)
    ingest.start({})
    ticker.now += 100
    until(entered.is_set)
    snapshot = ingest.finish()
    assert snapshot["restore"]["ok"] is False
    assert snapshot["failure"]["phase"] == "stop"
    with pytest.raises(InfraError, match="ingest_failure"):
        ingest.raise_if_failed()
    release.set()
    assert ingest.stop() is True


# -- the restore -------------------------------------------------------------------------------------------------------


def test_o27_the_restore_leaves_the_declared_baseline_and_deletes_nothing_the_study_keeps(make, fake, ticker, tmp_path):
    """O27: after the run the day is back whole (its baseline is complete), the note is gone again (its baseline is
    absent), the earlier day was never touched, the staged copies are unchanged, and every provider sees the day."""
    kept_id = fake.id_of(KEPT_PATH)
    ingest = make(DAY_ROW, NOTE_ROW)
    ingest.start({})
    t0 = ticker.now
    run_to(ingest, ticker, t0, 300, 2)
    snapshot = ingest.finish()
    assert snapshot["restore"]["ok"] is True
    assert (content(fake, DAY_PATH), content(fake, NOTE_PATH)) == (DAY, None)
    assert (fake.id_of(KEPT_PATH), content(fake, KEPT_PATH)) == (kept_id, KEPT)
    assert (tmp_path / "staged" / "day-13.csv").read_bytes() == DAY
    assert (tmp_path / "staged" / "note.txt").read_bytes() == NOTE
    assert seen_by(fake, BETA_ID, DAY_PATH)
    assert sorted(n.name for n in fake.nodes.values() if n.parent == fake.id_of("records")) == [
        "day-12.csv",
        "day-13.csv",
    ]


def test_o27_a_run_that_ends_before_an_arrival_restores_the_file_from_its_staged_copy(make, fake, ticker):
    """O27: the run ends at 50 s, before either file was due. That is the manipulation, not a failure: both are
    recorded as not due, and the day, removed for the run, is written back from the copy the study keeps."""
    ingest = make(DAY_ROW, NOTE_ROW)
    ingest.start({})
    ticker.now += 50
    snapshot = ingest.finish()
    assert snapshot["ok"] is True
    assert snapshot["failure"] is None
    assert [s["step"] for s in snapshot["steps"] if s["step"] not in ("planned",)] == ["not_due", "not_due"]
    assert {label: f["state"] for label, f in snapshot["files"].items()} == {
        "day-13": "not_arrived",
        "note": "not_arrived",
    }
    assert (content(fake, DAY_PATH), content(fake, NOTE_PATH)) == (DAY, None)
    ingest.raise_if_failed()


def test_o27_a_crash_mid_plan_ends_at_the_baseline_after_the_next_start(make, fake, ticker):
    """O27: a run stops with the day half written and nothing restored, as a killed harness would leave it. The next
    run's start finishes the day from its staged copy (into the file the ingest made), checks it, and then removes it
    for its own start state; its finish leaves the day whole again."""
    crashed = make({**DAY_ROW, "at_s": 100, "parts": 4, "over_s": 300})
    crashed.start({})
    run_to(crashed, ticker, ticker.now, 200, 2)
    assert crashed.stop() is True  # the schedule ends; no restore runs, as after a crash
    half = fake.id_of(DAY_PATH)
    assert content(fake, DAY_PATH) == DAY[:5120]
    following = make({**DAY_ROW, "at_s": 100, "parts": 4, "over_s": 300})
    following.start({})
    assert content(fake, DAY_PATH) is None
    snapshot = following.finish()
    assert content(fake, DAY_PATH) == DAY
    recovered = [e for e in snapshot["ids"] if e["phase"] == "start"]
    assert [(e["event"], e["old_id"], e["new_id"]) for e in recovered] == [
        ("restored", half, half),
        ("removed", half, None),
    ]
    assert snapshot["start"]["restore"]["day-13"]["action"] == "resumed"


def test_o27_a_crash_after_the_start_removed_a_file_is_recovered_from_the_journal(make, fake, ticker):
    """O27: a run stops right after its start removed the day, before anything arrived, and nothing restores it. The
    next start finds the path empty, reads the day's last id from the journal, checks every provider answers it absent
    before the name is written again, and restores the day; the event names the removed id as the old one."""
    baseline = fake.id_of(DAY_PATH)
    crashed = make(DAY_ROW)
    crashed.start({})
    assert crashed.stop() is True
    assert content(fake, DAY_PATH) is None
    following = make(DAY_ROW)
    following.start({})
    snapshot = following.finish()
    restored = next(e for e in snapshot["ids"] if e["phase"] == "start" and e["event"] == "restored")
    assert (restored["old_id"], restored["action"]) == (baseline, "created")
    assert content(fake, DAY_PATH) == DAY


# -- what the record says ----------------------------------------------------------------------------------------------


def test_o27_every_file_id_the_ingest_removes_creates_or_restores_is_recorded_with_its_label(
    make, fake, ticker, tmp_path
):
    """O27: the baseline's id is recorded as removed, the arrival's as created in place of it, and the restore's as
    restored; each event has the label, the old and new ids and its clock. The journal keeps the same, so the ids of
    an attempt whose record was lost can still be mapped to files."""
    baseline = fake.id_of(DAY_PATH)
    ingest = make(DAY_ROW)
    ingest.start({})
    run_to(ingest, ticker, ticker.now, 300, 1)
    arrived = fake.id_of(DAY_PATH)
    snapshot = ingest.finish()
    events = [(e["label"], e["event"], e["phase"], e["old_id"], e["new_id"]) for e in snapshot["ids"]]
    assert events == [
        ("day-13", "restored", "start", baseline, baseline),
        ("day-13", "removed", "start", baseline, None),
        ("day-13", "created", "run", baseline, arrived),
        ("day-13", "restored", "restore", arrived, arrived),
    ]
    assert all({"clock", "t_rel_s", "utc"} <= set(e) for e in snapshot["ids"])
    ids = snapshot["files"]["day-13"]
    assert (ids["baseline_id"], ids["arrived_id"], ids["restored_id"]) == (baseline, arrived, arrived)
    journal = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    in_journal = [(j["label"], j["event"], j["phase"], j["old_id"], j["new_id"]) for j in journal if j["entry"] == "id"]
    assert in_journal == events


def test_o27_the_record_names_providers_by_position_and_files_by_label(make, fake, ticker, tmp_path):
    """O27 with O12: nothing in the snapshot names a provider, the space, a path or the staging directory."""
    ingest = make(DAY_ROW, NOTE_ROW)
    ingest.start({})
    run_to(ingest, ticker, ticker.now, 300, 2)
    text = json.dumps(ingest.finish())
    for private in ("alpha", "beta", SPACE_NAME, "records/", ".csv", "note.txt", str(tmp_path), PROVIDER):
        assert private not in text
    assert '"providers[1]"' in text
    assert NAMESPACE == "progressive_ingest"
    assert ingest.namespace == NAMESPACE


def test_o27_an_empty_plan_records_the_same_steps(make, fake, ticker):
    """O27: a sham control with no files starts, finishes and records the instrument's steps, all empty."""
    ingest = make()
    ingest.start({})
    snapshot = ingest.finish()
    assert (snapshot["ok"], snapshot["steps"], snapshot["ids"], snapshot["files"]) == (True, [], [], {})
    assert snapshot["restore"]["ok"] is True
    assert snapshot["plan"]["files"] == []
