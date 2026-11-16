"""O35: the link-check record, `link-check.json`: the report of one all-pairs link check, written whole or not at all.

`up` has always written it (`deploy/compose-federation/fedlinks.py`); the restore's and the heal's link checks write it
through the same function now, so every writer produces one format and no reader of it (an operator's pre-check that
wants a check under an hour old) sees a torn file.
"""

import errno
import fcntl
import json
import os
import threading
from pathlib import Path

import pytest

from expcore_onedata import linkrecord
from expcore_onedata.linkcheck import REMEDY, LinkReport, PairResult
from expcore_onedata.linkrecord import LOCK, RECORD, discard_record, record_text, recording, write_record


def passing(pairs: int = 2, *, started: float = 10.0, ended: float = 12.5) -> LinkReport:
    results = tuple(PairResult(f"providers[{i}]", "providers[9]", True, 0.25, 65536) for i in range(pairs))
    return LinkReport(True, "every pair passed", results, started, ended)


def failing(pairs: int = 2, *, started: float = 20.0, ended: float = 55.0) -> LinkReport:
    results = tuple(
        PairResult(
            f"providers[{i}]",
            "providers[9]",
            False,
            30.0,
            reason=f"no transfer from providers[9] to providers[{i}] within 30 s",
            remedy=REMEDY.format(reader=f"providers[{i}]"),
        )
        for i in range(pairs)
    )
    return LinkReport(False, "a provider link is stuck or untested", results, started, ended)


def directory_names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def test_o35_the_record_is_named_for_the_file_up_writes():
    """O35: one name for every writer, the one `up`'s facts sit beside."""
    assert RECORD == "link-check.json"


def test_o35_the_record_text_is_the_reports_json_exactly_as_up_has_always_written_it():
    """O35: indent 2, sorted keys, a final newline: the bytes `up` wrote before the writer was shared, so a reader of an
    older record reads a new one."""
    report = passing()
    assert record_text(report) == json.dumps(report.to_json(), indent=2, sort_keys=True) + "\n"
    assert record_text(report).startswith('{\n  "detail": "every pair passed",\n  "ok": true,\n')
    assert record_text(report).endswith("}\n")


def test_o35_a_written_record_is_the_report_whole_and_leaves_nothing_beside_it(tmp_path):
    """O35: the record holds the verdict, each pair, the transfers and the check's window on the host's epoch clock."""
    path = tmp_path / RECORD
    write_record(passing(started=10.0, ended=12.5), path)
    assert json.loads(path.read_text(encoding="utf-8")) == passing(started=10.0, ended=12.5).to_json()
    assert json.loads(path.read_text(encoding="utf-8"))["window"] == {"start_epoch_s": 10.0, "end_epoch_s": 12.5}
    assert directory_names(tmp_path) == [RECORD]


def test_o35_a_failing_report_is_written_as_the_failing_verdict_it_is(tmp_path):
    """O35: the record never says more than the check found: a failing report is written failing, naming the pair."""
    path = tmp_path / RECORD
    write_record(failing(), path)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["ok"] is False
    assert [pair["ok"] for pair in record["pairs"]] == [False, False]
    assert "no transfer from providers[9] to providers[0]" in record["pairs"][0]["reason"]


def test_o35_a_rewrite_replaces_the_old_record_in_one_rename_of_a_complete_draft(tmp_path, monkeypatch):
    """O35: the draft is a sibling of the record, complete and fsynced before it replaces the old one, and the old one
    is whole until then, so a reader sees the old record or the new, never a mixture."""
    path = tmp_path / RECORD
    write_record(passing(), path)
    seen: list[tuple[Path, Path]] = []
    synced: list[int] = []
    real_replace, real_fsync = os.replace, os.fsync

    def replace(source, destination):
        seen.append((Path(source), Path(destination)))
        assert json.loads(Path(source).read_text(encoding="utf-8")) == failing().to_json()  # the draft is complete
        assert json.loads(Path(destination).read_text(encoding="utf-8")) == passing().to_json()  # the old one, whole
        assert len(synced) >= 1  # the draft's data was flushed to disk before the rename
        real_replace(source, destination)

    def fsync(descriptor):
        synced.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "fsync", fsync)
    write_record(failing(), path)
    ((draft, destination),) = seen
    assert destination == path
    assert draft.parent == path.parent
    assert draft != path
    assert len(synced) >= 2  # the draft, then the directory that holds the rename
    assert json.loads(path.read_text(encoding="utf-8")) == failing().to_json()
    assert directory_names(tmp_path) == [RECORD]


def test_o35_a_concurrent_reader_never_reads_a_torn_record(tmp_path):
    """O35: a reader that reads the record in a loop while another writes it again and again always gets one complete
    report, passing or failing, never a prefix of one."""
    path = tmp_path / RECORD
    sizes = {True: 300, False: 200}
    reports = {True: passing(sizes[True]), False: failing(sizes[False])}
    write_record(reports[True], path)
    stop = threading.Event()
    problems: list[str] = []
    reads = [0]

    def read() -> None:
        while not stop.is_set():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                problems.append(f"{type(error).__name__}: {error}")
                return
            if len(record["pairs"]) != sizes[record["ok"]]:
                problems.append("a record whose verdict and pairs disagree")
                return
            reads[0] += 1
            stop.wait(0.001)  # a read at least every millisecond, never a busy loop

    reader = threading.Thread(target=read)
    reader.start()
    try:
        for number in range(60):
            write_record(reports[number % 2 == 1], path)
    finally:
        stop.set()
        reader.join(30)
    assert problems == []
    assert reads[0] > 0
    assert directory_names(tmp_path) == [RECORD]


def test_o35_a_write_that_fails_leaves_the_old_record_whole_and_no_draft(tmp_path, monkeypatch):
    """O35: a disk that cannot take the draft raises, and what was on disk before is still the whole old record: no
    half-written file, no stray draft."""
    path = tmp_path / RECORD
    write_record(passing(), path)

    def full(descriptor):
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(OSError, match="no space"):
        write_record(failing(), path)
    monkeypatch.undo()
    assert json.loads(path.read_text(encoding="utf-8")) == passing().to_json()
    assert directory_names(tmp_path) == [RECORD]


def test_o35_two_writers_never_share_a_draft(tmp_path, monkeypatch):
    """O35: the draft's name is the writer's own, so a second process writing at the same time cannot overwrite the
    first one's draft before it is renamed."""
    path = tmp_path / RECORD
    names: list[str] = []
    real_replace = os.replace

    def replace(source, destination):
        names.append(Path(source).name)
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    write_record(passing(), path)
    write_record(passing(), path)
    assert len(names) == 2
    assert len(set(names)) == 2


def test_o35_the_record_has_the_mode_a_plain_write_gives_it(tmp_path):
    """O35: `up`'s record was written with the process's umask, and is still: the draft is not owner-only, since the
    record holds only labels, seconds, bytes and transfer ids."""
    path = tmp_path / RECORD
    previous = os.umask(0o022)
    try:
        write_record(passing(), path)
    finally:
        os.umask(previous)
    assert path.stat().st_mode & 0o777 == 0o644


def test_o35_a_record_is_discarded_so_that_no_older_verdict_stands(tmp_path):
    """O35: removing the record is how a check that cannot be recorded keeps a passing verdict that is older than it
    from standing on disk; discarding what is not there is no error."""
    path = tmp_path / RECORD
    write_record(passing(), path)
    discard_record(path)
    assert not path.exists()
    discard_record(path)
    assert directory_names(tmp_path) == []


def test_o35_a_record_the_directory_will_not_let_go_is_emptied_in_place_and_dated_to_the_epoch(tmp_path, monkeypatch):
    """O35: when the directory refuses the removal (a read-only directory, with the record's own file still writable),
    the old pass must not stay readable: it is emptied in place, which is no record (it is not JSON), and dated to the
    epoch, so that a reader that goes by the file's time finds it old too."""
    path = tmp_path / RECORD
    write_record(passing(), path)

    def refuse(self, missing_ok=False):
        raise PermissionError(errno.EACCES, "read-only directory")

    monkeypatch.setattr(Path, "unlink", refuse)
    discard_record(path)
    monkeypatch.undo()
    assert path.exists()
    assert path.read_bytes() == b""
    assert path.stat().st_mtime == 0
    with pytest.raises(json.JSONDecodeError):
        json.loads(path.read_text(encoding="utf-8"))


def test_o35_a_record_that_can_be_neither_removed_nor_emptied_raises_for_the_caller_to_say(tmp_path, monkeypatch):
    """O35: the one case in which an old record this process cannot change stays: discarding it raises `OSError`, which
    the probe logs."""
    path = tmp_path / RECORD
    write_record(passing(), path)

    def refuse(*args, **kwargs):
        raise PermissionError(errno.EACCES, "read-only")

    monkeypatch.setattr(Path, "unlink", refuse)
    monkeypatch.setattr(linkrecord, "open", refuse, raising=False)
    with pytest.raises(PermissionError):
        discard_record(path)
    monkeypatch.undo()
    assert json.loads(path.read_text(encoding="utf-8")) == passing().to_json()


def test_o35_a_check_that_starts_while_another_is_under_way_waits_for_it(tmp_path):
    """O35: `recording` holds the record's lock exclusively, so one check at a time makes the record: the second waits
    until the first has let go, and holds it itself then."""
    path = tmp_path / RECORD
    inside, release, second_in = threading.Event(), threading.Event(), threading.Event()
    order: list[str] = []

    def first() -> None:
        with recording(path) as held:
            assert held
            order.append("first in")
            inside.set()
            release.wait(30)
            order.append("first out")

    def second() -> None:
        with recording(path) as held:
            assert held
            order.append("second in")
            second_in.set()

    holder = threading.Thread(target=first)
    holder.start()
    assert inside.wait(30)
    waiter = threading.Thread(target=second)
    waiter.start()
    assert not second_in.wait(0.3)  # it waits while the first is inside
    release.set()
    assert second_in.wait(30)
    holder.join(30)
    waiter.join(30)
    assert order == ["first in", "first out", "second in"]
    assert LOCK in directory_names(tmp_path)


def test_o35_a_lock_file_another_user_made_read_only_is_still_taken(tmp_path):
    """O35: `flock` needs no write permission on a local filesystem, so a lock file that only its owner can write (0644,
    or here 0444) is opened read-only by a process that cannot write it, and taken by every process that can read it."""
    lock = tmp_path / LOCK
    lock.write_bytes(b"")
    lock.chmod(0o444)
    with recording(tmp_path / RECORD) as held:
        assert held is True


def test_o35_a_lock_file_made_under_a_strict_umask_is_readable_to_every_process_that_may_take_part(tmp_path):
    """O35: the lock file's owner makes it 0644 whatever its umask, so a process of another user that opens it
    read-only can lock it and is ordered with the owner's checks, never run unserialized beside them."""
    previous = os.umask(0o077)
    try:
        with recording(tmp_path / RECORD) as held:
            assert held is True
    finally:
        os.umask(previous)
    assert (tmp_path / LOCK).stat().st_mode & 0o777 == 0o644


def test_o35_a_lock_that_cannot_be_taken_is_said_by_a_false_and_never_raises(tmp_path):
    """O35: a directory that cannot hold the lock file is no reason to skip a check: the caller is told it runs
    unserialized."""
    with recording(tmp_path / "missing" / RECORD) as held:
        assert held is False


def test_o35_the_lock_is_taken_where_an_exclusive_flock_needs_a_write_descriptor(tmp_path, nfs_flock_rule):
    """O35: NFS implements `flock` as a POSIX lock, so an exclusive one on a read-only descriptor fails there with EBADF
    (live, on an HPC federation's NFSv4 root, `up` said the record's lock could not be taken and no check left a
    record). The lock file is opened for writing, so the lock is taken, and it excludes another taker."""
    path = tmp_path / RECORD
    with recording(path) as held:
        assert held is True
        other = os.open(tmp_path / LOCK, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(other)
    assert (tmp_path / LOCK).stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("code", [errno.EACCES, errno.EROFS, errno.EPERM])
def test_o35_a_lock_file_that_cannot_be_written_is_opened_read_only_and_taken_where_flock_allows_it(
    tmp_path, refuse_lock_writes, code
):
    """O35: a lock file this process may read but not write (another user's 0644 file, a read-only mount, an immutable
    file) is opened read-only after the write is refused, and locked: on a local filesystem `flock` needs no write
    permission."""
    (tmp_path / LOCK).write_bytes(b"")
    tried = refuse_lock_writes(code)
    with recording(tmp_path / RECORD) as held:
        assert held is True
    assert tried == [os.O_RDWR, os.O_RDONLY]


@pytest.mark.parametrize("code", [errno.EACCES, errno.EROFS, errno.EPERM])
def test_o35_a_lock_file_that_cannot_be_written_is_not_taken_where_flock_needs_a_write_descriptor(
    tmp_path, nfs_flock_rule, refuse_lock_writes, code
):
    """O35: where an exclusive `flock` needs a write descriptor (NFS) and the lock file cannot be written, the lock
    cannot be taken: `recording` says so with a False and raises nothing, so the caller publishes no verdict."""
    (tmp_path / LOCK).write_bytes(b"")
    tried = refuse_lock_writes(code)
    with recording(tmp_path / RECORD) as held:
        assert held is False
    assert tried == [os.O_RDWR, os.O_RDONLY]


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EMFILE, errno.ENOENT])
def test_o35_a_failure_to_open_the_lock_that_is_no_refusal_to_write_is_not_tried_read_only(
    tmp_path, refuse_lock_writes, code
):
    """O35: only a refusal to write (EACCES, EROFS, EPERM) sends the lock to a read-only open; any other failure to
    open the lock file is a lock that cannot be taken, said by a False."""
    tried = refuse_lock_writes(code)
    with recording(tmp_path / RECORD) as held:
        assert held is False
    assert tried == [os.O_RDWR]


def test_o35_the_module_names_what_up_and_the_platforms_probe_import():
    """O35: the writer's whole interface: the writer, its lock, and where the federation's record lies (its root)."""
    assert set(linkrecord.__all__) == {
        "LOCK",
        "RECORD",
        "ROOT_FILE",
        "discard_record",
        "federation_root",
        "record_path",
        "record_text",
        "recording",
        "write_record",
    }


def test_o35_the_record_lies_in_the_parent_of_the_journal_whichever_file_names_it():
    """O35: the federation's root is its journal's parent, and the record and the lock are in it."""
    journal = Path("/federation/fault-journal")
    assert linkrecord.federation_root(journal) == Path("/federation")
    assert linkrecord.record_path(journal) == Path("/federation") / RECORD
