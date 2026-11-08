"""Kill safety: a process killed at each point of an attempt resumes cleanly (C04), and the outcome marker."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from campaign_crash import KILLED
from campaign_support import (
    LATER,
    NOW,
    defect,
    finished,
    infra,
    make_manifest,
    make_schema,
    open_store,
    planned_store,
    run_one,
)

from expcore.campaign.markers import MARKER_NAME, AttemptResult, MarkerError, read_marker, write_marker
from expcore.campaign.recovery import Recovery, recover
from expcore.loop.outcomes import OUTCOMES, HarnessDefect, Outcome

CRASH = Path(__file__).resolve().parents[1] / "support" / "campaign_crash.py"


def kill_at(tmp_path: Path, point: str):
    """Run a child that dies at `point`; return the store opened afterwards, the run key and the run directory."""
    database, run_dir = tmp_path / "campaign.db", tmp_path / "run"
    done = subprocess.run(
        [sys.executable, str(CRASH), str(database), str(run_dir), point], capture_output=True, text=True
    )
    assert done.returncode == KILLED, done.stderr
    return open_store(tmp_path), make_manifest(make_schema()).rows[0].key.text, run_dir


def attempts(store):
    return [(r["state"], r["charged"], r["attempt"]) for r in store.q("SELECT * FROM attempts ORDER BY id")]


def status(store, key):
    return store.q("SELECT status FROM runs WHERE run_key=?", (key,))[0]["status"]


def test_c04_a_kill_before_the_marker_leaves_an_interrupted_uncharged_attempt_and_a_run_to_redo(tmp_path):
    """C04: a kill after the attempt began and before its marker: interrupted, never charged, run again."""
    store, key, run_dir = kill_at(tmp_path, "before_marker")
    assert attempts(store) == [("running", 0, 1)]
    assert not (run_dir / MARKER_NAME).exists()
    assert recover(store, LATER, 3) == Recovery(committed=0, interrupted=1, requeued=0)
    assert attempts(store) == [("interrupted", 0, 1)]
    assert store.charged_attempts(key) == 0
    assert status(store, key) == "pending"
    assert store.q("SELECT usage_complete FROM attempts")[0][0] == 0  # nobody knows what it used
    redo = store.begin_attempt(key, LATER, 1, str(run_dir))
    assert redo.number == 1  # the same attempt number, as no fault was observed


def test_c04_a_kill_after_the_marker_commits_the_marked_result_and_never_runs_it_again(tmp_path):
    """C04: an outcome marker written before the kill is committed, never re-run."""
    store, key, run_dir = kill_at(tmp_path, "after_marker")
    assert attempts(store) == [("running", 0, 1)]
    assert (run_dir / MARKER_NAME).is_file()
    assert recover(store, LATER, 3) == Recovery(committed=1, interrupted=0, requeued=0)
    assert attempts(store) == [("done", 0, 1)]
    assert status(store, key) == "done"
    row = store.q("SELECT * FROM attempts")[0]
    assert (row["input_tokens"], row["output_tokens"], row["termination"], row["wall_s"]) == (10, 5, "finished", 12.5)
    assert key not in [r["run_key"] for r in store.pending()]
    assert store.q("SELECT kind FROM events")[0]["kind"] == "recovered_committed"


def test_c04_a_kill_after_the_metadata_write_leaves_nothing_to_repair(tmp_path):
    """C04: a kill after the store recorded the result: the result stands and recovery does nothing."""
    store, key, _ = kill_at(tmp_path, "after_metadata")
    assert attempts(store) == [("done", 0, 1)]
    assert status(store, key) == "done"
    assert recover(store, LATER, 3) == Recovery(0, 0, 0)
    assert attempts(store) == [("done", 0, 1)]
    assert status(store, key) == "done"


@pytest.mark.parametrize("point", ["before_marker", "after_marker", "after_metadata"])
def test_c04_recovery_is_safe_to_repeat_after_a_kill_of_its_own(tmp_path, point):
    """C04: a second recovery finds nothing left to settle."""
    store, key, _ = kill_at(tmp_path, point)
    recover(store, LATER, 3)
    snapshot = (attempts(store), status(store, key))
    assert recover(store, LATER, 3) == Recovery(0, 0, 0)
    assert (attempts(store), status(store, key)) == snapshot


def test_c04_a_marked_infrastructure_failure_is_charged_when_committed_from_its_marker(tmp_path):
    """C04: a marker's result is committed by the same rules as a live finish, including the charge."""
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    begun = store.begin_attempt(key, NOW, 1, str(tmp_path / "run"))
    write_marker(tmp_path / "run", key, begun.id, infra("sandbox"))
    assert recover(store, LATER, 3).committed == 1
    assert (attempts(store), status(store, key)) == ([("infra_fail", 1, 1)], "pending")
    assert store.charged_attempts(key) == 1


def test_c04_a_marker_left_by_an_earlier_attempt_does_not_end_this_one(tmp_path):
    """C04: the marker names the attempt it ended, so a stale one in a reused directory is ignored."""
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    run_dir = tmp_path / "run"
    first = store.begin_attempt(key, NOW, 1, str(run_dir))
    write_marker(run_dir, key, first.id, infra("endpoint"))
    store.finish_attempt(first.id, infra("endpoint"), 3)
    second = store.begin_attempt(key, LATER, 1, str(run_dir))  # the same directory; the old marker is still there
    assert read_marker(run_dir, key, second.id) is None
    assert recover(store, LATER, 3) == Recovery(0, 1, 0)
    assert attempts(store) == [("infra_fail", 1, 1), ("interrupted", 0, 2)]


def test_c04_a_marker_that_cannot_be_trusted_stops_recovery_instead_of_guessing(tmp_path):
    """C04: neither losing a finished attempt nor running it twice is acceptable, so recovery fails loudly."""
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    run_dir = tmp_path / "run"
    store.begin_attempt(key, NOW, 1, str(run_dir))
    run_dir.mkdir()
    (run_dir / MARKER_NAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(MarkerError, match="not a valid marker"):
        recover(store, LATER, 3)
    assert attempts(store) == [("running", 0, 1)]


def test_c04_an_attempt_with_no_run_directory_is_interrupted(tmp_path):
    """C04: with nowhere to look for a marker the attempt cannot have finished."""
    store, _, manifest = planned_store(tmp_path)
    store.begin_attempt(manifest.rows[0].key.text, NOW, 1, "")
    assert recover(store, LATER, 3).interrupted == 1


def test_a_harness_error_run_goes_back_to_pending_from_the_same_row_with_its_attempt_kept(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    assert run_one(store, key, defect()) == "harness_error"
    assert key not in [r["run_key"] for r in store.pending()]
    assert recover(store, LATER, 3) == Recovery(0, 0, 1)
    assert status(store, key) == "pending"
    assert attempts(store) == [("harness_error", 0, 1)]
    assert store.q("SELECT detail FROM attempts")[0]["detail"].startswith("ValueError aaaaaaaaaaaa: boom")
    assert store.q("SELECT kind FROM events")[0]["kind"] == "harness_error_requeued"


# --- the marker file -----------------------------


def every_result() -> list[AttemptResult]:
    extra = {
        "infrastructure": {"infra_kind": "knowledge"},
        "error": {"error_type": "ContextOverflow"},
        "harness_defect": {"defect": HarnessDefect("KeyError", "c" * 64)},
    }
    return [AttemptResult(Outcome(name, "d", **extra.get(name, {})), LATER, 2.5) for name in OUTCOMES]


@pytest.mark.parametrize("result", every_result(), ids=lambda r: r.outcome.name)
def test_a_marker_reads_back_the_result_it_was_written_from(tmp_path, result):
    write_marker(tmp_path / "run", "k", 7, result)
    assert read_marker(tmp_path / "run", "k", 7) == result


def test_a_marker_keeps_usage_and_an_outage_kind(tmp_path):
    result = AttemptResult(
        Outcome("finished"),
        LATER,
        9.0,
        outage_kind="sandbox",
        input_tokens=5,
        cache_read_tokens=3,
        usage_complete=False,
    )
    write_marker(tmp_path, "k", 1, result)
    assert read_marker(tmp_path, "k", 1) == result


def test_writing_a_marker_leaves_no_temporary_file_and_replaces_an_older_marker(tmp_path):
    write_marker(tmp_path, "k", 1, finished())
    write_marker(tmp_path, "k", 2, infra())
    assert [p.name for p in tmp_path.iterdir()] == [MARKER_NAME]
    assert read_marker(tmp_path, "k", 1) is None
    assert read_marker(tmp_path, "k", 2).outcome.name == "infrastructure"


def test_no_marker_reads_as_none(tmp_path):
    assert read_marker(tmp_path / "absent", "k", 1) is None


def test_a_marker_for_another_run_reads_as_none(tmp_path):
    write_marker(tmp_path, "k", 1, finished())
    assert read_marker(tmp_path, "other", 1) is None


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda body: body.update(schema=2), "unknown marker schema 2"),
        (lambda body: body["result"].update(surprise=1), "unknown result fields: surprise"),
        (lambda body: body["result"]["outcome"].update(name="won"), "unknown outcome 'won'"),
        (lambda body: body["result"].pop("wall_s"), "not a valid marker"),
    ],
)
def test_a_marker_of_another_schema_or_with_impossible_content_is_refused(tmp_path, edit, message):
    path = write_marker(tmp_path, "k", 1, finished())
    body = json.loads(path.read_text(encoding="utf-8"))
    edit(body)
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(MarkerError, match=message):
        read_marker(tmp_path, "k", 1)
