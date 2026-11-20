"""O97: the reader of a survivor's `/proc` through the host's privilege answers the diagnosis's four reads (`listdir`,
`read`, `readlink`, `read_at`) from one snapshot, never from a second sudo call per file, and never reads what it was
not given.

The diagnosis has a few seconds and a survivor holds up to dozens of files, so a `sudo cat` for each would not fit: one
call to the root half (`procsnap.py`) reads every listed process whole, and the reader serves from that. A read past it
costs one more call, a few at most. A process that is gone or was replaced, a file that is not on the helper's list, a
call that fails and a budget that is spent all raise `OSError`: the diagnosis reads that as unknown, and unknown never
becomes the agent's."""

import errno
import io
import json
import sys
from pathlib import Path

import pytest
from proc_tree import START, make_tree, stat_line

from expcore_onedata.sandbox import procsnap, sudoproc
from expcore_onedata.sandbox.hostops import CmdResult


class Host:
    """The host: `run` is the root half run in-process on a made-up tree, and every call is kept."""

    def __init__(self, root: Path, monkeypatch, *, fail: CmdResult | None = None) -> None:
        monkeypatch.setattr(procsnap, "PROC_ROOT", str(root))
        self.calls: list[dict] = []
        self.fail = fail

    def run(self, argv, timeout_s, *, sudo=False, input=None):
        self.calls.append({"argv": list(argv), "timeout_s": timeout_s, "sudo": sudo, "request": json.loads(input)})
        if self.fail is not None:
            return self.fail
        out, err = io.StringIO(), io.StringIO()
        return CmdResult(procsnap.main([], io.StringIO(input), out, err), out.getvalue(), err.getvalue())


LEFT = [{"pid": 31, "start": START, "state": "D", "comm": "python"}]


@pytest.fixture
def host(tmp_path, monkeypatch):
    return Host(make_tree(tmp_path), monkeypatch)


def reader_of(host, left=LEFT, timeout_s=5.0, clock=None):
    return sudoproc.SudoProcReader.snapshot(host, left, timeout_s, **({"clock": clock} if clock else {}))


def test_o97_one_sudo_call_answers_every_read_of_the_snapshot(host):
    """O97: the listing, a thread's syscall, an fd's link and its fdinfo and the mountinfo come from one call to the
    root half, which is run as `PYTHON -I -B -c SOURCE` under sudo with the listed (pid, start) and nothing else."""
    reader = reader_of(host)
    assert reader.listdir("/proc/31/task") == ["31", "32"]
    assert reader.read("/proc/31/task/32/syscall").startswith("0 0x3 ")
    assert reader.read("/proc/31/task/32/wchan") == "request_wait_answer"
    assert reader.readlink("/proc/31/fd/3") == "/data/space-a/file"
    assert "mnt_id:\t60" in reader.read("/proc/31/fdinfo/3")
    assert "fuse.oneclient" in reader.read("/proc/31/mountinfo")
    assert reader.readlink("/proc/31/cwd") == "/work"
    [call] = host.calls
    assert call["sudo"] is True
    assert call["argv"][:4] == [sys.executable, "-I", "-B", "-c"]
    assert call["argv"][4] == sudoproc.PROGRAM
    assert call["request"] == {"targets": [{"pid": 31, "start": START}], "snapshot": True, "reads": []}


@pytest.mark.parametrize("form", ["/proc/31/stat", "31/stat", Path("/proc/31/stat")])
def test_o97_a_path_is_taken_as_the_proc_path_of_a_listed_process_in_any_of_its_forms(host, form):
    """O97: the reads name a process as `/proc/<pid>/...`, relative to `/proc`, or as a `Path`."""
    assert reader_of(host).read(form).startswith("31 (python) Z ")


def test_o97_the_memory_at_a_pointer_is_one_more_call_and_comes_back_as_bytes(host):
    """O97: a path argument is read from the process's memory at the pointer the blocked call gave: the one read that
    is not in the snapshot, a call of its own, and bytes."""
    reader = reader_of(host)
    assert reader.read_at("/proc/31/mem", 16, 20) == b"/data/space-a/other\0"
    assert len(host.calls) == 2
    assert host.calls[1]["request"]["targets"] == []
    assert host.calls[1]["request"]["snapshot"] is False
    assert host.calls[1]["request"]["reads"][0]["kind"] == "read_at"


def test_o97_maps_is_a_read_of_its_own_made_when_it_is_asked_for(host):
    """O97: `maps` can wait on a survivor's memory lock, so it is not in the snapshot and is read, last, only when the
    diagnosis asks: the snapshot did not wait on it."""
    reader = reader_of(host)
    assert len(host.calls) == 1
    assert "7f00-7f10" in reader.read("/proc/31/maps")
    assert len(host.calls) == 2


def test_o97_a_process_that_was_not_listed_is_never_read(host):
    """O97: a pid that is not one of the listed (pid, start) pairs is refused without a call: the reader is a window on
    the survivors, never on the host."""
    reader = reader_of(host)
    with pytest.raises(PermissionError):
        reader.read("/proc/1/stat")
    with pytest.raises(PermissionError):
        reader.listdir("/proc/1/task")
    assert len(host.calls) == 1


@pytest.mark.parametrize("path", ["/proc/31/cmdline", "/proc/31/environ", "/proc/31/../1/stat", "/etc/passwd", "stat"])
def test_o97_a_name_off_the_helpers_list_is_refused_without_a_call(host, path):
    """O97: no command line, no environment and nothing that leaves the process's directory."""
    reader = reader_of(host)
    with pytest.raises(PermissionError):
        reader.read(path)
    assert len(host.calls) == 1


def test_o97_a_process_that_was_replaced_or_is_gone_raises_file_not_found_and_reads_nothing_of_it(host, tmp_path):
    """O97: the start tick of 31 is now another process's: it is `replaced`, so no read of it answers (the root half
    opened and checked it before reading anything); a pid that never was is gone."""
    (tmp_path / "proc" / "31" / "stat").write_text(stat_line(31, "S", start="9999"))
    left = [*LEFT, {"pid": 77, "start": "1"}]
    reader = reader_of(host, left)
    with pytest.raises(FileNotFoundError):
        reader.read("/proc/31/task/32/syscall")
    with pytest.raises(FileNotFoundError):
        reader.listdir("/proc/77/task")
    assert len(host.calls) == 1  # both answered by the snapshot's statuses, no call per read


def test_o97_a_file_the_snapshot_could_not_read_raises_its_error_and_is_not_asked_again(host, tmp_path):
    """O97: `syscall` of a thread that cannot be read is an `OSError` of that errno, from the snapshot, with no second
    call (the same answer would come back)."""
    (tmp_path / "proc" / "31" / "task" / "32" / "syscall").unlink()
    reader = reader_of(host)
    with pytest.raises(FileNotFoundError):
        reader.read("/proc/31/task/32/syscall")
    assert len(host.calls) == 1


@pytest.mark.parametrize(
    "failure",
    [
        CmdResult(1, "", "sudo: a password is required"),
        CmdResult(0, "not json", ""),
        CmdResult(0, '{"results": []}', ""),
    ],
)
def test_o97_a_call_that_fails_or_answers_wrongly_makes_every_read_an_error(tmp_path, monkeypatch, failure):
    """O97: sudo refused, an answer that is not JSON, or one that does not answer for each listed process: no read
    answers, so the diagnosis is unknown for every survivor."""
    host = Host(make_tree(tmp_path), monkeypatch, fail=failure)
    reader = reader_of(host)
    for read in (lambda: reader.read("/proc/31/stat"), lambda: reader.listdir("/proc/31/task")):
        with pytest.raises(OSError, match="Errno 5") as raised:
            read()
        assert raised.value.errno == errno.EIO


def test_o97_a_spent_budget_raises_a_timeout_and_runs_no_call(host):
    """O97: the diagnosis is given seconds; a read past the snapshot with none left is not made."""
    clock = iter([0.0, 0.1, 99.0, 99.0]).__next__
    reader = reader_of(host, timeout_s=5.0, clock=clock)
    with pytest.raises(TimeoutError):
        reader.read_at("/proc/31/mem", 16, 20)
    assert len(host.calls) == 1


def test_o97_the_calls_past_the_snapshot_are_few(host):
    """O97: a diagnosis that keeps asking past the snapshot is stopped at `MISSES` calls, not left to run a sudo call
    for each of a survivor's files."""
    reader = reader_of(host)
    for _ in range(sudoproc.MISSES):
        reader.read_at("/proc/31/mem", 16, 4)
    with pytest.raises(TimeoutError):
        reader.read_at("/proc/31/mem", 16, 4)
    assert len(host.calls) == 1 + sudoproc.MISSES


def test_o97_a_listing_the_snapshot_cut_is_said_cut_and_its_unread_entries_are_not_invented(host, tmp_path):
    """O97: a process with more fds than the snapshot reads has the listing, the first of them read, and a read of one
    past the cut is one more call that answers for it, never a guess."""
    old = procsnap.FD_CAP
    procsnap.FD_CAP = 1
    try:
        reader = reader_of(host)
    finally:
        procsnap.FD_CAP = old
    assert reader.listdir("/proc/31/fd") == ["3", "4"]
    assert reader.readlink("/proc/31/fd/3") == "/data/space-a/file"
    assert reader.readlink("/proc/31/fd/4") == "/work/out"  # one call of its own
    assert len(host.calls) == 2
