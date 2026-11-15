"""Reading a finished store from a copy (R16): the store and its -wal, never its -shm, unchanged across the copy."""

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from campaign_crash import KILLED
from campaign_support import make_schema, open_store
from tree_support import read_only, tree_of

from expcore.campaign import storecopy
from expcore.campaign.lock import filesystem_type
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


@pytest.fixture
def killed_on_tmpfs(tmp_path) -> Iterator[Path]:
    """A killed dispatcher's store (see `killed`) on tmpfs, where file times move only on the kernel's coarse tick,
    as on the HPC nodes and temporary directories shards run in; skipped where no writable tmpfs exists."""
    shm = Path("/dev/shm")
    if filesystem_type(shm) != "tmpfs" or not os.access(shm, os.W_OK):
        pytest.skip("no writable tmpfs at /dev/shm on this host, so the coarse-tick case cannot be shown here")
    base = Path(tempfile.mkdtemp(prefix="expcore-r16-", dir=shm))
    try:
        out = base / "out"
        out.mkdir()
        store = out / "state.sqlite"
        done = subprocess.run(
            [sys.executable, str(CRASH), str(store), str(tmp_path / "run"), "after_metadata"],
            capture_output=True,
            text=True,
        )
        assert done.returncode == KILLED, done.stderr
        yield store
    finally:
        shutil.rmtree(base)


def rewrite_a_page_in_place(store: Path) -> None:
    """Commit as a writer could within one tick of the clock: change bytes of the store in place, then put its
    modification time back, so its size, time and inode are what they were and only its contents differ."""
    was = os.stat(store)
    with open(store, "r+b") as handle:
        handle.seek(was.st_size - 16)
        old = handle.read(16)
        handle.seek(was.st_size - 16)
        handle.write(bytes(byte ^ 0xFF for byte in old))
    os.utime(store, ns=(was.st_atime_ns, was.st_mtime_ns))
    now = os.stat(store)
    assert (now.st_size, now.st_mtime_ns, now.st_ino) == (was.st_size, was.st_mtime_ns, was.st_ino)


def write_during_copies(monkeypatch, store: Path, *, when: str, times: int | None) -> list[str]:
    """Rewrite a page of the store in place just before (`when="before"`) or just after (`"after"`) each of its
    copies, the first `times` times (every time with None); return the names copied, in order."""
    copied: list[str] = []
    real = storecopy._copy_file
    rewrites = 0

    def copy_with_writer(source: Path, target: Path) -> str:
        nonlocal rewrites
        due = source == store and (times is None or rewrites < times)
        if due and when == "before":
            rewrite_a_page_in_place(store)
        digest = real(source, target)
        if due and when == "after":
            rewrite_a_page_in_place(store)
        rewrites += due
        copied.append(source.name)
        return digest

    monkeypatch.setattr(storecopy, "_copy_file", copy_with_writer)
    return copied


def test_r16_on_tmpfs_a_writer_that_keeps_size_time_and_inode_during_every_copy_is_refused(
    killed_on_tmpfs, tmp_path, monkeypatch, quick
):
    """R16: a commit during every copy that leaves the store's size, modification time and inode as they were is
    seen in its contents, which the signature before and after the copy hashes: the copy is never accepted, and
    after the bounded attempts it is refused, naming the store, with nothing left where it was made."""
    copied = write_during_copies(monkeypatch, killed_on_tmpfs, when="after", times=None)
    into = tmp_path / "copy"
    into.mkdir()
    with pytest.raises(StoreChanged, match=f"{killed_on_tmpfs}: it changed while it was copied"):
        copy_store(killed_on_tmpfs, into)
    assert copied == ["state.sqlite", "state.sqlite-wal"] * COPY_ATTEMPTS
    assert list(into.iterdir()) == []


@pytest.mark.parametrize("when", ["before", "after"])
def test_r16_on_tmpfs_a_page_rewritten_once_around_the_copy_is_copied_again_never_torn(
    killed_on_tmpfs, tmp_path, monkeypatch, quick, when
):
    """R16: the bytes copied must equal the contents hashed both before and after the copy, so a store rewritten in
    place just before its copy is read (after the signature was taken) or just after, size, time and inode kept, is
    copied again; what is accepted is one state of both files."""
    copied = write_during_copies(monkeypatch, killed_on_tmpfs, when=when, times=1)
    into = tmp_path / "copy"
    into.mkdir()
    copy_store(killed_on_tmpfs, into)
    assert copied == ["state.sqlite", "state.sqlite-wal"] * 2
    for name in ("state.sqlite", "state.sqlite-wal"):
        assert (into / name).read_bytes() == (killed_on_tmpfs.parent / name).read_bytes()


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
