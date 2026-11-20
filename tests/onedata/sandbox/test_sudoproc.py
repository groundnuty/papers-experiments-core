"""O97: the reader of a survivor's `/proc` through the host's privilege is the diagnosis's `ProcReader` (`stuckread`):
names relative to `/proc`, `listdir`, `read`, `readlink` and `read_at`, each answering None where the file cannot be
shown and raising nothing.

The diagnosis has a few seconds and a survivor holds up to dozens of files, so a `sudo cat` for each would not fit: one
call to the root half (`procsnap.py`) reads every listed process whole, and the reader serves from that. A read past it
costs one more call, a few at most. A process that was not listed, one that is gone or was replaced, a name off the
helper's list, a call that fails and a budget that is spent all answer None: the diagnosis reads that as unknown, and
unknown never becomes the agent's."""

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
    """O97: the listing, a thread's syscall, an fd's link and its fdinfo, the mount table, the root and the working
    directory come from one call to the root half, which is run as `PYTHON -I -B -c SOURCE` under sudo with the listed
    (pid, start) and nothing else."""
    reader = reader_of(host)
    assert reader.listdir("31/task") == ["31", "32"]
    assert reader.read("31/task/32/syscall", 4096).startswith(b"0 0x3 ")
    assert reader.read("31/task/32/wchan", 4096) == b"request_wait_answer"
    assert reader.readlink("31/task/32/fd/3") == "/data/space-a/file"
    assert b"mnt_id:\t60" in reader.read("31/task/32/fdinfo/3", 4096)
    assert b"fuse.oneclient" in reader.read("31/task/32/mountinfo", 1 << 18)
    assert reader.readlink("31/task/32/cwd") == "/work"
    assert reader.readlink("31/task/32/root") == "/"
    assert reader.listdir("31/task/32/fd") == ["3", "4"]
    [call] = host.calls
    assert call["sudo"] is True
    assert call["argv"][:4] == [sys.executable, "-I", "-B", "-c"]
    assert call["argv"][4] == sudoproc.PROGRAM
    assert call["request"] == {"targets": [{"pid": 31, "start": START}], "snapshot": True, "reads": []}


@pytest.mark.parametrize("form", ["/proc/31/stat", "31/stat", Path("/proc/31/stat")])
def test_o97_a_name_is_taken_as_the_proc_path_of_a_listed_process_in_any_of_its_forms(host, form):
    """O97: the reads name a process as `<pid>/...` relative to `/proc`, as `/proc/<pid>/...`, or as a `Path`."""
    assert reader_of(host).read(form, 4096).startswith(b"31 (python) Z ")


def test_o97_a_read_is_cut_to_its_limit(host):
    """O97: `read(name, limit)` returns at most `limit` bytes, as the node's own reader does."""
    assert reader_of(host).read("31/task/32/syscall", 3) == b"0 0"


def test_o97_the_memory_at_a_pointer_is_one_more_call_and_comes_back_as_bytes(host):
    """O97: a path argument is read from the thread's memory at the pointer the blocked call gave: the one read that is
    not in the snapshot, a call of its own, and bytes."""
    reader = reader_of(host)
    assert reader.read_at("31/task/32/mem", 16, 20) == b"/data/space-a/other\0"
    assert len(host.calls) == 2
    assert host.calls[1]["request"]["targets"] == []
    assert host.calls[1]["request"]["snapshot"] is False
    assert host.calls[1]["request"]["reads"][0]["kind"] == "read_at"


def test_o97_maps_is_a_read_of_its_own_made_when_it_is_asked_for(host):
    """O97: `maps` can wait on a survivor's memory lock, so it is not in the snapshot and is read, last, only when the
    diagnosis asks: the snapshot did not wait on it. The answer is kept: asked again, it is not a second call."""
    reader = reader_of(host)
    assert len(host.calls) == 1
    assert b"7f00-7f10" in reader.read("31/task/32/maps", 1 << 22)
    assert len(host.calls) == 2
    assert b"7f00-7f10" in reader.read("31/task/32/maps", 1 << 22)
    assert len(host.calls) == 2


def test_o97_a_process_that_was_not_listed_is_never_read(host):
    """O97: a pid that is not one of the listed (pid, start) pairs answers None without a call: the reader is a window
    on the survivors, never on the host."""
    reader = reader_of(host)
    assert reader.read("1/stat", 4096) is None
    assert reader.listdir("1/task") is None
    assert reader.read_at("1/task/1/mem", 0, 16) is None
    assert len(host.calls) == 1


@pytest.mark.parametrize(
    "name",
    [
        "31/cmdline",
        "31/environ",
        "31/../1/stat",
        "/etc/passwd",
        "stat",
        "31/task/32/cmdline",
        "31/mountinfo",
        "31/maps",
    ],
)
def test_o97_a_name_off_the_helpers_list_answers_none_without_a_call(host, name):
    """O97: no command line, no environment, nothing that leaves the process's directory, and none of the process-level
    entries the diagnosis does not read (a zombie leader's are empty, so it reads them below a thread)."""
    reader = reader_of(host)
    assert reader.read(name, 4096) is None
    assert len(host.calls) == 1


def test_o97_a_process_that_was_replaced_or_is_gone_answers_none_and_reads_nothing_of_it(host, tmp_path):
    """O97: the start tick of 31 is now another process's: it is `replaced`, so no read of it answers (the root half
    opened and checked it before reading anything); a pid that never was is gone. Both come from the snapshot."""
    (tmp_path / "proc" / "31" / "stat").write_text(stat_line(31, "S", start="9999"))
    reader = reader_of(host, [*LEFT, {"pid": 77, "start": "1"}])
    assert reader.read("31/task/32/syscall", 4096) is None
    assert reader.listdir("77/task") is None
    assert len(host.calls) == 1


def test_o97_a_file_the_snapshot_could_not_read_answers_none_and_is_not_asked_again(host, tmp_path):
    """O97: `syscall` of a thread that cannot be read is None, from the snapshot, with no second call (the same answer
    would come back)."""
    (tmp_path / "proc" / "31" / "task" / "32" / "syscall").unlink()
    assert reader_of(host).read("31/task/32/syscall", 4096) is None
    assert len(host.calls) == 1


@pytest.mark.parametrize(
    "failure",
    [
        CmdResult(1, "", "sudo: a password is required"),
        CmdResult(0, "not json", ""),
        CmdResult(0, '{"results": []}', ""),
    ],
)
def test_o97_a_call_that_fails_or_answers_wrongly_makes_every_read_none(tmp_path, monkeypatch, failure):
    """O97: sudo refused, an answer that is not JSON, or one that does not answer for each listed process: no read
    answers, so the diagnosis is unknown for every survivor."""
    reader = reader_of(Host(make_tree(tmp_path), monkeypatch, fail=failure))
    assert reader.read("31/stat", 4096) is None
    assert reader.listdir("31/task") is None
    assert reader.readlink("31/task/32/cwd") is None


def test_o97_a_host_that_raises_makes_every_read_none(tmp_path, monkeypatch):
    """O97: a host whose command raises (a timeout, a daemon) has read nothing."""
    host = Host(make_tree(tmp_path), monkeypatch)
    host.run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the host failed"))
    assert reader_of(host).read("31/stat", 4096) is None


def test_o97_a_spent_budget_answers_none_and_runs_no_call(host):
    """O97: the diagnosis is given seconds; a read past the snapshot with none left is not made."""
    clock = iter([0.0, 0.1, 99.0, 99.0]).__next__
    reader = reader_of(host, timeout_s=5.0, clock=clock)
    assert reader.read_at("31/task/32/mem", 16, 20) is None
    assert len(host.calls) == 1


def test_o97_the_calls_past_the_snapshot_are_few(host):
    """O97: a diagnosis that keeps asking past the snapshot is stopped at `MISSES` calls, not left to run a sudo call
    for each of a survivor's files."""
    reader = reader_of(host)
    for _ in range(sudoproc.MISSES):
        assert reader.read_at("31/task/32/mem", 16, 4) == b"/dat"
    assert reader.read_at("31/task/32/mem", 16, 4) is None
    assert len(host.calls) == 1 + sudoproc.MISSES


def test_o97_a_listing_the_snapshot_cut_is_said_cut_and_its_unread_entries_are_one_more_call(host):
    """O97: a process with more fds than the snapshot reads has the whole listing, the first of them read, and a read of
    one past the cut is one more call that answers for it, never a guess."""
    old = procsnap.FD_CAP
    procsnap.FD_CAP = 1
    try:
        reader = reader_of(host)
    finally:
        procsnap.FD_CAP = old
    assert reader.listdir("31/task/32/fd") == ["3", "4"]
    assert reader.readlink("31/task/32/fd/3") == "/data/space-a/file"
    assert reader.readlink("31/task/32/fd/4") == "/work/out"  # one call of its own
    assert len(host.calls) == 2
