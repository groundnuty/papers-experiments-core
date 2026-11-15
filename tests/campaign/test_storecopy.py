"""Reading a finished store from a copy (R16): the store and its -wal, never its -shm, unchanged across the copy."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from campaign_crash import KILLED
from campaign_support import make_schema, open_store
from tree_support import read_only, tree_of

from expcore.campaign import storecopy
from expcore.campaign.resume import FINGERPRINT_KEY, read_fingerprint
from expcore.campaign.snapshot import SNAPSHOT_FILE, SnapshotError, StoreSnapshot, take_snapshot
from expcore.campaign.storecopy import COPY_ATTEMPTS, StoreChanged, copy_store

CRASH = Path(__file__).resolve().parents[1] / "support" / "campaign_crash.py"
DONE = "SELECT run_key, state FROM attempts"


@pytest.fixture
def killed(tmp_path) -> Path:
    """The out/ of a dispatcher killed once the store recorded an attempt's result: the store, a -wal holding what
    was never checkpointed into it, and a -shm."""
    out = tmp_path / "out"
    out.mkdir()
    store = out / "state.sqlite"
    done = subprocess.run(
        [sys.executable, str(CRASH), str(store), str(tmp_path / "run"), "after_metadata"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == KILLED, done.stderr
    assert sorted(p.name for p in out.iterdir()) == ["state.sqlite", "state.sqlite-shm", "state.sqlite-wal"]
    return store


def done_in(database: Path) -> list[tuple]:
    """The attempts a database file holds when it is opened alone, in a directory of its own; none without the table."""
    connection = sqlite3.connect(database)
    try:
        return [tuple(row) for row in connection.execute(DONE)]
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()


@pytest.fixture
def quick(monkeypatch):
    """No pause between attempts at a copy."""
    monkeypatch.setattr(storecopy, "COPY_PAUSE_S", 0.0)


def bump_on_copy(monkeypatch, store: Path, times: int | None) -> list[str]:
    """Make each copy of a file move the store's modification time on, as a writer would, the first `times` times
    (every time with None); return the list of the names copied, in order."""
    copied: list[str] = []
    real = storecopy._copy_file

    def copy_and_bump(source: Path, target: Path) -> str:
        digest = real(source, target)
        copied.append(source.name)
        if times is None or len(copied) <= times:
            now = store.stat().st_mtime_ns + 1_000_000_000
            os.utime(store, ns=(now, now))
        return digest

    monkeypatch.setattr(storecopy, "_copy_file", copy_and_bump)
    return copied


def test_r16_a_copy_holds_the_store_and_its_wal_and_never_its_shm(killed, tmp_path):
    """R16: the -wal holds committed transactions the store does not, so it is copied with it; the -shm is an index
    of the -wal shared between live connections, which SQLite rebuilds from the -wal, so it is never copied."""
    into = tmp_path / "copy"
    into.mkdir()
    copy = copy_store(killed, into)
    assert copy == into / killed.name
    assert sorted(p.name for p in into.iterdir()) == ["state.sqlite", "state.sqlite-wal"]
    for name in ("state.sqlite", "state.sqlite-wal"):
        assert (into / name).read_bytes() == (killed.parent / name).read_bytes()


def test_r16_a_killed_dispatchers_rows_in_the_wal_alone_are_read_through_the_copy_in_a_read_only_out(killed, tmp_path):
    """R16: what a killed dispatcher committed may be in its -wal alone. The copy, opened read-write in its own
    directory, replays the -wal, so the snapshot sees it; the read-only out/ keeps every byte and gains no file."""
    alone = tmp_path / "alone"
    alone.mkdir()
    (alone / killed.name).write_bytes(killed.read_bytes())
    assert done_in(alone / killed.name) == []  # the result is in the -wal alone
    before = tree_of(killed.parent)
    with read_only(killed.parent):
        take_snapshot(killed, tmp_path / SNAPSHOT_FILE)
    assert tree_of(killed.parent) == before
    snapshot = StoreSnapshot((tmp_path / SNAPSHOT_FILE).read_bytes(), make_schema())
    try:
        assert [row["state"] for row in snapshot.q(DONE)] == ["done"]
    finally:
        snapshot.close()


def test_r16_a_fingerprint_in_the_wal_alone_is_read_through_the_copy_in_a_read_only_out(tmp_path):
    """R16: a store whose fingerprint was committed and never checkpointed holds it in its -wal alone; read through
    the copy, the fingerprint is there, and the read-only out/ keeps every byte."""
    store = open_store(tmp_path / "live")
    store.db.execute("INSERT INTO meta(key,value,set_utc) VALUES(?,?,?)", (FINGERPRINT_KEY, '{"plan": 1}', "x"))
    out = tmp_path / "out"
    out.mkdir()
    for name in ("campaign.db", "campaign.db-wal"):  # what a kill leaves, its -shm aside
        (out / name).write_bytes((tmp_path / "live" / name).read_bytes())
    store.close()
    alone = tmp_path / "alone"
    alone.mkdir()
    (alone / "campaign.db").write_bytes((out / "campaign.db").read_bytes())
    connection = sqlite3.connect(alone / "campaign.db")
    try:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='meta'").fetchall() == []
    finally:
        connection.close()
    before = tree_of(out)
    with read_only(out):
        assert read_fingerprint(out / "campaign.db") == {"plan": 1}
    assert tree_of(out) == before


def test_r16_a_change_the_timestamps_cannot_show_is_caught_by_the_bytes(killed, tmp_path, monkeypatch, quick):
    """R16: a file system's timestamps may be too coarse to show a write between the copy of the store and that of
    its -wal; the bytes read again after the copy show it, so that copy is thrown away and made again."""
    monkeypatch.setattr(storecopy, "_identity", lambda path: ("the same", "whatever happens"))
    wal = killed.with_name(killed.name + "-wal")
    copied: list[str] = []
    real = storecopy._copy_file

    def copy_then_write(source: Path, target: Path) -> str:
        digest = real(source, target)
        copied.append(source.name)
        if len(copied) == 2:  # once, after the first copy of the -wal
            with open(wal, "ab") as appended:
                appended.write(b"a frame the copy did not see")
        return digest

    monkeypatch.setattr(storecopy, "_copy_file", copy_then_write)
    into = tmp_path / "copy"
    into.mkdir()
    copy_store(killed, into)
    assert copied == ["state.sqlite", "state.sqlite-wal"] * 2
    assert (into / wal.name).read_bytes() == wal.read_bytes()


def test_r16_a_store_that_changes_during_the_copy_is_copied_again(killed, tmp_path, monkeypatch, quick):
    """R16: the store and its -wal are stated before and after the copy; a change between is a copy that may mix two
    states, so it is thrown away and the copy made again."""
    copied = bump_on_copy(monkeypatch, killed, times=1)
    into = tmp_path / "copy"
    into.mkdir()
    copy_store(killed, into)
    assert copied == ["state.sqlite", "state.sqlite-wal"] * 2
    assert sorted(p.name for p in into.iterdir()) == ["state.sqlite", "state.sqlite-wal"]


def test_r16_a_store_that_keeps_changing_is_refused_naming_it_and_leaves_no_copy(killed, tmp_path, monkeypatch, quick):
    """R16: a store that changes during every attempt has a writer, so no copy of it is one state: refused, naming
    the store, after a bounded number of attempts, and nothing is left where the copy was made."""
    copied = bump_on_copy(monkeypatch, killed, times=None)
    into = tmp_path / "copy"
    into.mkdir()
    with pytest.raises(StoreChanged, match=str(killed)):
        copy_store(killed, into)
    assert len(copied) == 2 * COPY_ATTEMPTS
    assert list(into.iterdir()) == []


def test_r16_a_snapshot_of_a_store_that_keeps_changing_is_refused_as_a_snapshot_error(
    killed, tmp_path, monkeypatch, quick
):
    """R16: the snapshot's callers (check, merge, --after) refuse a SnapshotError, so a torn copy reaches none."""
    bump_on_copy(monkeypatch, killed, times=None)
    with pytest.raises(SnapshotError, match="changed while it was copied"):
        take_snapshot(killed, tmp_path / SNAPSHOT_FILE)
    assert not (tmp_path / SNAPSHOT_FILE).exists()
