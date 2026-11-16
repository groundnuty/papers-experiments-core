"""The snapshot of the campaign store: a consistent read-only copy the replay check hashes, and a view of its bytes."""

import os
import sqlite3

import pytest
from campaign_support import NOW, make_schema, planned_store, run_one
from tree_support import added, read_only, tree_of, without

from expcore.campaign.export import export_run_state
from expcore.campaign.markers import AttemptResult
from expcore.campaign.scheduler import STATE_FILE
from expcore.campaign.snapshot import (
    SNAPSHOT_FILE,
    AttemptsRunning,
    SnapshotError,
    StoreSnapshot,
    rebase_run_dirs,
    take_snapshot,
)
from expcore.loop.outcomes import Outcome
from expcore.package.spec import never_selected

WAL_HEADER, ROLLBACK_HEADER = b"\x02\x02", b"\x01\x01"  # a database file's bytes 18 and 19: its write and read versions


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


@pytest.fixture
def finished_out(settled, tmp_path):
    """A study's out/ as a finished campaign leaves it: the store closed under its own name, and an empty batch."""
    store, _ = settled
    store.close()
    out = tmp_path / "out"
    (out / "runs").mkdir(parents=True)
    os.replace(store.path, out / STATE_FILE)
    assert sorted(p.name for p in tmp_path.glob(f"{store.path.name}*")) == []  # the close left no side file
    return out


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
        assert take_snapshot(store.path, tmp_path / SNAPSHOT_FILE, in_place=True) == 0
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


def test_r04_a_snapshot_from_before_the_admission_column_exports_it_as_unknown(settled, tmp_path):
    """R04: a campaign recorded before attempts kept their admission mode is still analysed: a snapshot is read as it
    was written, never migrated, so its attempts' admission is None (unknown) in the export, never a mode."""
    store, _ = settled
    destination = tmp_path / SNAPSHOT_FILE
    take_snapshot(store.path, destination)
    old = sqlite3.connect(destination)
    old.execute("ALTER TABLE attempts DROP COLUMN admission")
    old.commit()
    old.close()
    data = export_run_state(StoreSnapshot(destination.read_bytes(), make_schema()))
    assert data["analysed_attempts"]
    assert all(entry["admission"] is None for entry in data["analysed_attempts"])


# --- R16: a read of a finished or sealed store writes nothing beside it ----------------------------------------------


def test_r16_a_finished_store_stays_in_wal_mode_and_its_snapshot_is_one_rollback_journal_file(finished_out):
    """R16: the campaign keeps its store in WAL mode, closed too (bytes 18 and 19 at 2), so an open of it in place may
    make a -shm and a -wal. What is sealed is the snapshot, which says rollback journal (both at 1): read in place or
    as bytes, it never needs a side file."""
    store = finished_out / STATE_FILE
    assert store.read_bytes()[18:20] == WAL_HEADER
    take_snapshot(store, finished_out / "runs" / SNAPSHOT_FILE)
    assert (finished_out / "runs" / SNAPSHOT_FILE).read_bytes()[18:20] == ROLLBACK_HEADER


def test_r16_snapshotting_a_finished_store_writes_only_the_snapshot_into_the_batch(finished_out):
    """R16: the check copies the store into the batch it then hashes; the batch gains the snapshot and nothing else,
    no scratch copy and no side file of either database."""
    batch = finished_out / "runs"
    take_snapshot(finished_out / STATE_FILE, batch / SNAPSHOT_FILE)
    assert list(tree_of(batch)) == [SNAPSHOT_FILE]


def test_r16_reading_the_sealed_snapshot_leaves_the_batch_byte_for_byte_as_it_was(finished_out):
    """R16: the analysis reads the snapshot's bytes once and opens them in memory; the batch, the snapshot with it,
    keeps every byte and gains no file."""
    batch = finished_out / "runs"
    take_snapshot(finished_out / STATE_FILE, batch / SNAPSHOT_FILE)
    sealed = tree_of(batch)
    snapshot = StoreSnapshot((batch / SNAPSHOT_FILE).read_bytes(), make_schema())
    try:
        assert export_run_state(snapshot)["analysed_attempts"]
    finally:
        snapshot.close()
    assert tree_of(batch) == sealed


def test_r16_a_snapshot_of_a_finished_store_in_a_read_only_out_leaves_it_byte_for_byte(finished_out):
    """R16: a finished store is read from a copy in a directory of the reader's own, so its out/ may be read-only (a
    shard's fetched from another host) and keeps every byte: no -shm or -wal is made beside the store."""
    before = tree_of(finished_out)
    with read_only(finished_out):
        take_snapshot(finished_out / STATE_FILE, finished_out / "runs" / SNAPSHOT_FILE)
    assert without(tree_of(finished_out), "runs/") == without(before, "runs/")


def test_r16_in_place_backs_the_store_itself_up_over_a_read_only_connection(finished_out):
    """R16: `in_place`, which `check --partial` uses for a store a dispatcher holds (no copy of a store being written
    is one state), reads the store itself over a read-only connection rather than a copy: it leaves the -shm and
    -wal a live store has beside it anyway, never selected for a package. (That the backup sees one committed state
    under a writer is shown above, with a write transaction open.)"""
    before = tree_of(finished_out)
    take_snapshot(finished_out / STATE_FILE, finished_out / "runs" / SNAPSHOT_FILE, in_place=True)
    new = added(without(before, "runs/"), without(tree_of(finished_out), "runs/"))
    assert new == [f"{STATE_FILE}-shm", f"{STATE_FILE}-wal"]
    assert all(never_selected(f"out/{name}") for name in new)


def test_o62_a_store_copied_back_has_each_attempts_directory_under_its_new_runs(tmp_path):
    """O62: a snapshot's `<digest>/try<N>` and an archive's absolute paths of another node both become the runs
    directory the store now has, so a resume's recovery reads each running attempt's marker where it is."""
    store, _, manifest = planned_store(tmp_path)
    keys = [row.key.text for row in manifest.rows]
    run_one(store, keys[0], finished(), run_dir=str(tmp_path / "old-node" / f"{0:064d}" / "try1"))
    store.begin_attempt(keys[1], NOW, 4321, str(tmp_path / "old-node" / f"{1:064d}" / "try2"))
    destination = tmp_path / SNAPSHOT_FILE
    take_snapshot(store.path, destination, allow_running=True, in_place=True)
    store.close()
    for copy in (destination, store.path):
        assert rebase_run_dirs(copy, tmp_path / "new" / "runs") == 2
        with sqlite3.connect(copy) as connection:
            named = sorted(r[0] for r in connection.execute("SELECT run_dir FROM attempts"))
        new = tmp_path / "new" / "runs"
        assert named == [str(new / f"{0:064d}" / "try1"), str(new / f"{1:064d}" / "try2")]
