"""The snapshot of the campaign store: a consistent read-only copy the replay check hashes, and a view of its bytes."""

import sqlite3

import pytest
from campaign_support import NOW, make_schema, planned_store, run_one

from expcore.campaign.export import export_run_state
from expcore.campaign.markers import AttemptResult
from expcore.campaign.snapshot import SNAPSHOT_FILE, AttemptsRunning, SnapshotError, StoreSnapshot, take_snapshot
from expcore.loop.outcomes import Outcome


def finished() -> AttemptResult:
    return AttemptResult(Outcome("finished"), NOW, 1.0)


@pytest.fixture
def settled(tmp_path):
    """A store whose first two runs are done and which has nothing running."""
    store, _, manifest = planned_store(tmp_path)
    keys = [row.key.text for row in manifest.rows]
    run_one(store, keys[0], finished())
    run_one(store, keys[1], finished())
    return store, keys


def test_a_snapshot_is_a_copy_whose_export_equals_the_stores(settled, tmp_path):
    store, _ = settled
    destination = tmp_path / SNAPSHOT_FILE
    assert take_snapshot(store.path, destination) == 0
    snapshot = StoreSnapshot(destination.read_bytes(), make_schema())
    assert export_run_state(snapshot) == export_run_state(store)


def test_taking_a_snapshot_leaves_the_store_as_it_was_and_no_scratch_file_behind(settled, tmp_path):
    store, _ = settled
    store.close()
    before = store.path.read_bytes()
    take_snapshot(store.path, tmp_path / SNAPSHOT_FILE)
    assert store.path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir() if "snapshot" in p.name) == [SNAPSHOT_FILE]


def test_the_copy_has_the_stores_schema_and_is_one_self_contained_file(settled, tmp_path):
    """The copy is made page by page, so it has the store's tables, generated columns included, on any SQLite; a store
    in WAL mode (as the campaign's is) gives a copy that is not, which needs no -wal or -shm file beside it."""
    store, _ = settled
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    destination = tmp_path / SNAPSHOT_FILE
    take_snapshot(store.path, destination)
    schema = "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    copy = sqlite3.connect(destination)
    try:
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert copy.execute(schema).fetchall() == [tuple(row) for row in store.db.execute(schema)]
    finally:
        copy.close()
    assert sorted(p.name for p in tmp_path.iterdir() if "snapshot" in p.name) == [SNAPSHOT_FILE]


def test_the_copy_names_each_attempts_directory_within_the_batch_and_keeps_no_byte_of_the_path_it_ran_under(tmp_path):
    """The snapshot is hashed, analysed and shipped in a replication package, so it holds no path of the machine,
    not even in the free space that keeps old versions of updated rows; the store keeps its absolute paths."""
    store, _, manifest = planned_store(tmp_path)
    keys = [row.key.text for row in manifest.rows]
    for number, key in enumerate(keys[:3]):
        run_one(store, key, finished(), run_dir=str(tmp_path / "private-place" / f"{number:064d}" / "try1"))
    destination = tmp_path / SNAPSHOT_FILE
    take_snapshot(store.path, destination)
    assert b"private-place" not in destination.read_bytes()
    snapshot = StoreSnapshot(destination.read_bytes(), make_schema())
    named = sorted(r["run_dir"] for r in snapshot.q("SELECT run_dir FROM attempts"))
    assert named == [f"{number:064d}/try1" for number in range(3)]
    assert all("private-place" in r["run_dir"] for r in store.q("SELECT run_dir FROM attempts"))


def test_the_snapshot_is_one_consistent_state_whatever_a_writer_has_open(settled, tmp_path):
    """A transaction another connection has not committed is not in the copy; the copy is not blocked by it."""
    store, keys = settled
    store.db.execute("BEGIN IMMEDIATE")
    store.db.execute("INSERT INTO attempts(run_key, attempt, state) VALUES (?, 1, 'running')", (keys[2],))
    try:
        assert take_snapshot(store.path, tmp_path / SNAPSHOT_FILE) == 0
    finally:
        store.db.execute("ROLLBACK")


def test_attempts_still_running_are_refused_and_leave_no_snapshot_unless_a_partial_one_is_asked_for(settled, tmp_path):
    store, keys = settled
    store.begin_attempt(keys[2], NOW, 1, "")
    destination = tmp_path / SNAPSHOT_FILE
    with pytest.raises(AttemptsRunning, match="1 attempt") as caught:
        take_snapshot(store.path, destination)
    assert caught.value.count == 1
    assert not destination.exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []  # no scratch copy either
    assert take_snapshot(store.path, destination, allow_running=True) == 1
    assert destination.exists()


def test_a_missing_store_is_refused_naming_it(tmp_path):
    with pytest.raises(SnapshotError, match="no campaign store here"):
        take_snapshot(tmp_path / "state.sqlite", tmp_path / SNAPSHOT_FILE)


def test_a_view_of_a_snapshot_cannot_be_written(settled, tmp_path):
    store, _ = settled
    take_snapshot(store.path, tmp_path / SNAPSHOT_FILE)
    snapshot = StoreSnapshot((tmp_path / SNAPSHOT_FILE).read_bytes(), make_schema())
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        snapshot.db.execute("UPDATE attempts SET treatment = 1")
    assert snapshot.q("SELECT COUNT(*) FROM attempts WHERE treatment = 1")[0][0] == 0


def test_bytes_that_are_not_a_store_for_this_schema_are_refused(settled, tmp_path):
    store, _ = settled
    take_snapshot(store.path, tmp_path / SNAPSHOT_FILE)
    with pytest.raises(SnapshotError, match="not a campaign store"):
        StoreSnapshot(b"this is not a database", make_schema())
    other = make_schema().__class__({"gate": ["open"]}, ["task", "gate", "model", "seed"], {"only": {"gate": "open"}})
    with pytest.raises(SnapshotError, match="factor columns"):
        StoreSnapshot((tmp_path / SNAPSHOT_FILE).read_bytes(), other)
