"""Snapshots of a live campaign's out/, verified and swapped in by rename, and their restore (O62)."""

import json
import sqlite3

import pytest
from campaign_support import NOW, finished, make_manifest, make_schema
from scheduler_support import build

from expcore.campaign import out_snapshot
from expcore.campaign.markers import MARKER_NAME, read_marker, write_marker
from expcore.campaign.out_snapshot import OutSnapshotError, restore, snapshots, take
from expcore.campaign.recovery import recover
from expcore.campaign.snapshot import take_snapshot
from expcore.campaign.store import Store

FACTS = {"shard": {"index": 1, "count": 2, "label": "fed-a"}, "base": None}


class Killed(BaseException):
    """A kill outright: nothing after it runs, no handler of `Exception` sees it."""


def kill_at(point):
    def crash(reached):
        if reached == point:
            raise Killed(point)

    return crash


@pytest.fixture
def live(tmp_path):
    """A live out/: a store with the plan's runs, as a dispatcher holds it, and its runs/."""
    out = tmp_path / "out"
    (out / "runs").mkdir(parents=True)
    store = Store(out / "state.sqlite", make_schema())
    store.sync_manifest(make_manifest(make_schema()).rows)
    yield out, store
    store.close()


def keys_of(store):
    return [r["run_key"] for r in store.q("SELECT run_key FROM runs ORDER BY seq")]


def begin(out, store, key):
    """Begin an attempt as the runner does: its directory first, then its row."""
    digest = store.q("SELECT digest FROM runs WHERE run_key=?", (key,))[0]["digest"]
    tries = len(store.q("SELECT 1 FROM attempts WHERE run_key=?", (key,))) + 1
    run_dir = out / "runs" / digest / f"try{tries}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return store.begin_attempt(key, NOW, 4321, str(run_dir)), run_dir


def end(store, key, begun, run_dir):
    """End an attempt as the runner does: its record, its marker, then its row."""
    (run_dir / "record.json").write_text(json.dumps({"run": key}))
    write_marker(run_dir, key, begun.id, finished())
    store.finish_attempt(begun.id, finished(), 3)


def one_done(out, store, key):
    begun, run_dir = begin(out, store, key)
    end(store, key, begun, run_dir)
    return begun, run_dir


def snap(out, into, tmp_path, crash=out_snapshot._never):
    return take(out, into, scratch=tmp_path / "scratch", facts=FACTS, live=True, now=NOW, crash=crash)


def test_o62_a_snapshot_restores_the_store_and_runs_with_directories_under_the_new_out(live, tmp_path):
    """O62: the snapshot's store and runs/ come back byte for byte, its manifest last, its attempts' directories
    pointed at the out/ they are restored to."""
    out, store = live
    keys = keys_of(store)
    one_done(out, store, keys[0])
    made = snap(out, tmp_path / "snaps", tmp_path)
    assert made.name == "snap-000001"
    assert snapshots(tmp_path / "snaps") == [made]
    manifest = restore(made, tmp_path / "new" / "out")
    assert manifest["facts"] == FACTS
    assert manifest["campaign"] is None
    new = tmp_path / "new" / "out"
    restored = [p.relative_to(new).as_posix() for p in sorted(new.rglob("*")) if p.is_file()]
    marked = "leases/ROLLED_BACK"  # every key of the restored lease directory needs its proof (C19)
    assert marked in restored
    assert sorted(manifest["files"]) == [n for n in restored if not n.startswith("state.sqlite") and n != marked]
    with sqlite3.connect(new / "state.sqlite") as copy:
        (run_dir,) = copy.execute("SELECT run_dir FROM attempts").fetchone()
    assert run_dir.startswith(str(new / "runs"))
    assert (new / "runs").exists()


def test_o62_the_lease_notes_the_logs_and_the_lock_come_back_and_the_live_store_files_do_not(live, tmp_path):
    """O62: a lease's note is recovery state (a key whose holder died goes to nobody until its resource is shown gone,
    C19), so it comes back with the rest of out/; the live store's -wal and -shm stand for nothing: its backup does."""
    out, _store = live
    (out / "leases").mkdir()
    (out / "leases" / "slot-1.lock").write_text('{"holder": 4321, "unit": "unit-m1"}')
    (out / "logs").mkdir()
    (out / "logs" / "harness-errors.log").write_text("a traceback\n")
    (out / "campaign.lock").write_text("")
    made = snap(out, tmp_path / "snaps", tmp_path)
    names = set(json.loads((made / "snapshot.json").read_text())["files"])
    assert {"leases/slot-1.lock", "logs/harness-errors.log", "campaign.lock"} <= names
    assert not [name for name in names if name.startswith("state.sqlite")]
    new = tmp_path / "new" / "out"
    restore(made, new)
    assert (new / "leases" / "slot-1.lock").read_text() == '{"holder": 4321, "unit": "unit-m1"}'
    assert (new / "campaign.lock").is_file()


def test_o62_the_store_is_copied_first_so_a_run_that_ends_between_the_copies_is_recovered(live, tmp_path):
    """O62: a run that ends after the store's backup and before runs/ is copied is running in the store copy with its
    marker in runs/: the resume's recovery commits it from the marker, so nothing names a record runs/ lacks."""
    out, store = live
    key = keys_of(store)[0]
    begun, run_dir = begin(out, store, key)

    def ends_between(point):
        if point == "backup":
            end(store, key, begun, run_dir)

    made = snap(out, tmp_path / "snaps", tmp_path, crash=ends_between)
    restore(made, tmp_path / "new" / "out")
    resumed = Store(tmp_path / "new" / "out" / "state.sqlite", make_schema())
    assert resumed.q("SELECT state FROM attempts")[0]["state"] == "running"
    assert recover(resumed, NOW, 3).committed == 1
    assert resumed.q("SELECT state FROM attempts")[0]["state"] == "done"
    resumed.close()


def test_o62_runs_copied_before_the_store_would_name_a_record_runs_lacks(live, tmp_path):
    """O62: the order matters. Copied the other way, a run that ends between the copies is done in the store copy and
    has no marker in the runs/ copy: recovery never visits a done row, so its record would be gone for good. The
    check `take` makes on its own copies (every ended attempt's marker present) finds exactly that."""
    out, store = live
    key = keys_of(store)[0]
    begun, run_dir = begin(out, store, key)
    every = {f"{d.parent.name}/{d.name}" for d in (out / "runs").glob("*/try*")}  # nothing left out
    files = out_snapshot._copy_out(out, tmp_path / "runs-first", every, out_snapshot._never)
    end(store, key, begun, run_dir)
    take_snapshot(out / "state.sqlite", tmp_path / "store-after", allow_running=True, in_place=True)
    ended, _, _ = out_snapshot._attempts(tmp_path / "store-after")
    (done,) = ended
    assert done in every
    assert f"runs/{done}/{MARKER_NAME}" not in files


def test_o62_a_run_directory_no_row_of_the_copy_names_is_left_out(live, tmp_path):
    """O62: an attempt that began after the backup left a directory, and a marker, that no row of the store copy
    names. The restored store gives that run's next attempt the same directory and the same attempt id, so a stale
    marker there would read as the next attempt's: the snapshot leaves the directory out."""
    out, store = live
    key = keys_of(store)[0]
    later = {}

    def begins_between(point):
        if point == "backup":
            later["begun"], later["dir"] = begin(out, store, key)
            write_marker(later["dir"], key, later["begun"].id, finished())

    made = snap(out, tmp_path / "snaps", tmp_path, crash=begins_between)
    assert not any("try1" in name for name in json.loads((made / "snapshot.json").read_text())["files"])
    new = tmp_path / "new" / "out"
    restore(made, new)
    resumed = Store(new / "state.sqlite", make_schema())
    begun, run_dir = begin(new, resumed, key)
    assert (begun.id, run_dir.name) == (later["begun"].id, later["dir"].name)
    assert read_marker(run_dir, key, begun.id) is None
    resumed.close()


@pytest.mark.parametrize("point", ["backup", "files", "verified", "rename"])
def test_o62_a_kill_while_a_snapshot_is_taken_leaves_the_last_good_one(live, tmp_path, point):
    """O62: killed mid-backup, mid-copy of runs/, after the check or as the rename is made, a snapshot never
    replaces the last good one: its `.part` directory is never restored, and the next take removes it."""
    out, store = live
    keys = keys_of(store)
    one_done(out, store, keys[0])
    good = snap(out, tmp_path / "snaps", tmp_path)
    one_done(out, store, keys[1])
    with pytest.raises(Killed):
        snap(out, tmp_path / "snaps", tmp_path, crash=kill_at(point))
    assert snapshots(tmp_path / "snaps") == [good]
    assert len(restore(good, tmp_path / "new" / "out")["files"]) == 2  # one run's record and marker
    after = snap(out, tmp_path / "snaps", tmp_path)
    assert not list((tmp_path / "snaps").glob(".*part"))
    assert snapshots(tmp_path / "snaps") == [after, good]


def test_o62_a_kill_after_the_rename_keeps_both_and_the_new_one_is_newest(live, tmp_path):
    """O62: once renamed, the new snapshot is whole; a kill before the older ones are removed leaves both."""
    out, store = live
    keys = keys_of(store)
    one_done(out, store, keys[0])
    good = snap(out, tmp_path / "snaps", tmp_path)
    with pytest.raises(Killed):
        snap(out, tmp_path / "snaps", tmp_path, crash=kill_at("renamed"))
    newest, older = snapshots(tmp_path / "snaps")
    assert older == good
    assert newest.name == "snap-000002"
    restore(newest, tmp_path / "new" / "out")


def test_o62_the_newest_two_are_kept(live, tmp_path):
    """O62: the previous good snapshot stays until a newer one is in place, and no more than two stay."""
    out, _store = live
    made = [snap(out, tmp_path / "snaps", tmp_path) for _ in range(3)]
    assert snapshots(tmp_path / "snaps") == [made[2], made[1]]


@pytest.mark.parametrize("damage", ["changed", "gone", "added", "manifest", "store"])
def test_o62_a_damaged_snapshot_is_refused_and_leaves_nothing(live, tmp_path, damage):
    """O62: a file changed, gone or added, a manifest that cannot be read, or a store whose bytes changed, fails the
    restore naming the snapshot, and nothing is left where it was restored to."""
    out, store = live
    one_done(out, store, keys_of(store)[0])
    made = snap(out, tmp_path / "snaps", tmp_path)
    record = next(made.rglob("record.json"))
    if damage == "changed":
        record.write_text("{}")
    elif damage == "gone":
        record.unlink()
    elif damage == "added":
        (record.parent / "extra.json").write_text("{}")
    elif damage == "manifest":
        (made / "snapshot.json").write_text("{")
    else:
        with open(made / "state.sqlite", "r+b") as handle:
            handle.seek(200)
            handle.write(b"\xff" * 64)
    with pytest.raises(OutSnapshotError, match="snap-000001"):
        restore(made, tmp_path / "new" / "out")
    assert not (tmp_path / "new" / "out").exists()


def test_o62_a_snapshot_whose_manifest_leaves_out_an_ended_attempts_marker_is_never_restored(live, tmp_path):
    """O62 (live slot 5's resume-line): a snapshot's files are its manifest's, and its store names an ended attempt
    whose marker is in neither (a manifest made otherwise than by `take`): the restore fails, naming the snapshot,
    and leaves nothing, since the restored store would name a record runs/ does not hold."""
    out, store = live
    one_done(out, store, keys_of(store)[0])
    made = snap(out, tmp_path / "snaps", tmp_path)
    marker = next(made.rglob(MARKER_NAME))
    manifest = json.loads((made / "snapshot.json").read_text())
    del manifest["files"][marker.relative_to(made).as_posix()]
    (made / "snapshot.json").write_text(json.dumps(manifest))
    marker.unlink()
    with pytest.raises(OutSnapshotError, match=r"snap-000001: its store names 1 ended attempt\(s\) whose marker"):
        restore(made, tmp_path / "new" / "out")
    assert not (tmp_path / "new" / "out").exists()


def test_o62_a_take_that_fails_leaves_no_part_and_the_snapshots_as_they_were(live, tmp_path):
    """O62: a take that fails (no store yet, say) is reported by the caller; nothing of it stays."""
    out, _store = live
    good = snap(out, tmp_path / "snaps", tmp_path)
    (out / "state.sqlite").rename(out / "moved.sqlite")
    with pytest.raises(Exception, match="no campaign store"):
        snap(out, tmp_path / "snaps", tmp_path)
    assert snapshots(tmp_path / "snaps") == [good]
    assert not list((tmp_path / "snaps").glob(".*"))


def test_o62_a_run_end_shows_in_the_count_of_ended_attempts(live, tmp_path):
    """O62: what the snapshot thread polls to take one at a run's end."""
    out, store = live
    assert out_snapshot.ended_attempts(out) == 0
    one_done(out, store, keys_of(store)[0])
    assert out_snapshot.ended_attempts(out) == 1
    assert out_snapshot.ended_attempts(tmp_path / "nowhere") is None


def test_o62_a_restored_snapshot_resumes_under_the_dispatcher_and_counts_a_lost_start_once(tmp_path):
    """O62: the real dispatcher opens a restored snapshot (its fingerprint checked, its recovery run), counts the
    attempts of a start whose store never reached a snapshot as lost, and runs the campaign to its end."""
    journals = tmp_path / "journals"
    journals.mkdir()
    root = tmp_path / "out"
    with build(root, spend_journal=journals / "j1.jsonl") as scheduler:
        scheduler.run(max_dispatch=2)
    made = take(root, tmp_path / "snaps", scratch=tmp_path / "scratch", facts=FACTS, live=False, now=NOW)
    with build(root, spend_journal=journals / "j2.jsonl") as scheduler:  # the start a kill will lose
        scheduler.run(max_dispatch=2)
    restore(made, tmp_path / "new" / "out")
    with build(tmp_path / "new" / "out", spend_journal=journals / "j3.jsonl") as scheduler:
        assert scheduler.lost_counted == {"journals": 2, "attempts": 2, "charges_usd": {}}
        scheduler.run()
        statuses = {row["status"] for row in scheduler.store.q("SELECT status FROM runs")}
    assert statuses == {"done"}


@pytest.mark.parametrize("text", ["null", "[]", '{"format": "expcore-out-snapshot/1"}', '"text"'])
def test_o62_a_manifest_that_is_no_object_is_a_snapshot_error(live, tmp_path, text):
    """O62: whatever a damaged manifest holds, it is refused as a snapshot that does not verify, so the caller goes on
    to the one before."""
    out, _store = live
    made = snap(out, tmp_path / "snaps", tmp_path)
    (made / "snapshot.json").write_text(text)
    with pytest.raises(OutSnapshotError, match="snap-000001"):
        restore(made, tmp_path / "new" / "out")
