"""O97: the root half of the Docker diagnosis reads a survivor's `/proc` entry as the root user, through the directory
it opened and only while that is the process listed (`procsnap.py`).

The agent runs as another user, so the harness reads what its survivors wait on through the host's privilege. The helper
snapshots each listed process in one call and answers any single read the snapshot did not hold. A pid that ended and
was given to another process (on the agent's node that could be the data gate) is never read: the start tick is
checked as the entry is opened, and every read goes through that open directory. These tests run the helper's own code
on a made-up `/proc` tree and on a real process of this user."""

import base64
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from proc_tree import START, make_tree, stat_line

from expcore_onedata.sandbox import procsnap


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A `/proc` of one process, 31 (`proc_tree.make_tree`), the helper pointed at it."""
    root = make_tree(tmp_path)
    monkeypatch.setattr(procsnap, "PROC_ROOT", str(root))
    return root


def run(request: dict) -> dict:
    out = io.StringIO()
    status = procsnap.main([], io.StringIO(json.dumps(request)), out)
    assert status == 0
    return json.loads(out.getvalue())


def target(pid: int = 31, start: str = START) -> dict:
    return {"pid": pid, "start": start}


def test_o97_a_snapshot_holds_what_the_diagnosis_reads_of_each_thread_and_fd(tree):
    """O97: one call answers for a process: its stat and mountinfo, its cwd, its threads' stat, wchan and syscall, and
    each fd's link and fdinfo, each file once, as the root user."""
    [one] = run({"targets": [target()], "snapshot": True})["results"]
    shot = one["snapshot"]
    assert one["status"] == "ok"
    assert shot["listings"] == {"task": ["31", "32"], "fd": ["3", "4"]}
    assert shot["text"]["task/32/syscall"].startswith("0 0x3 ")
    assert shot["text"]["task/32/wchan"] == "request_wait_answer"
    assert shot["text"]["fdinfo/3"].count("mnt_id:\t60") == 1
    assert shot["links"] == {"cwd": "/work", "fd/3": "/data/space-a/file", "fd/4": "/work/out"}
    assert "stat" in shot["text"]
    assert "mountinfo" in shot["text"]
    assert not any(name in shot["text"] for name in ("cmdline", "environ", "maps", "mem"))


def test_o97_a_pid_that_now_holds_another_process_is_not_read(tree):
    """O97: the listing named start tick 4242; the entry now shows another start, so it is `replaced` and nothing of it
    is read, whatever the request asked, a read of its memory included."""
    (tree / "31" / "stat").write_text(stat_line(31, "S", start="9999"))
    answer = run(
        {
            "targets": [target()],
            "snapshot": True,
            "reads": [{"pid": 31, "start": START, "kind": "read_at", "rel": "mem", "offset": 16, "n": 64}],
        }
    )
    assert answer["results"][0] == {"pid": 31, "status": "replaced"}
    assert answer["reads"] == [{"ok": False, "err": "replaced"}]


def test_o97_a_process_that_is_gone_and_one_that_cannot_be_read_are_told_apart(tree):
    """O97: ENOENT on the entry is `gone`; a stat that cannot be parsed is `unreadable`: unknown is never gone."""
    (tree / "40").mkdir()
    (tree / "40" / "stat").write_text("40 (python")  # cut off
    results = run({"targets": [target(77), target(40)], "snapshot": True})["results"]
    assert {r["pid"]: r["status"] for r in results} == {77: "gone", 40: "unreadable"}


def test_o97_a_read_past_the_snapshot_is_answered_alone_and_the_memory_at_an_offset_comes_back_whole(tree):
    """O97: the path argument a blocked call named is read from the process's memory at the pointer it gave: `read_at`
    answers the bytes (base64), at most `READ_CAP`, and `maps` is a plain read made last."""
    reads = [
        {"pid": 31, "start": START, "kind": "read_at", "rel": "mem", "offset": 16, "n": 20},
        {"pid": 31, "start": START, "kind": "read", "rel": "maps"},
        {"pid": 31, "start": START, "kind": "readlink", "rel": "fd/3"},
        {"pid": 31, "start": START, "kind": "listdir", "rel": "task"},
    ]
    answer = run({"targets": [], "snapshot": False, "reads": reads})["reads"]
    assert base64.b64decode(answer[0]["b64"]) == b"/data/space-a/other\0"
    assert "7f00-7f10" in answer[1]["text"]
    assert answer[2]["link"] == "/data/space-a/file"
    assert answer[3]["list"] == ["31", "32"]


@pytest.mark.parametrize(
    "rel", ["cmdline", "environ", "../1/stat", "/etc/passwd", "task/../../1/stat", "fd/../cwd/x", "stat/", "root", ""]
)
def test_o97_only_the_files_the_diagnosis_names_are_read(tree, rel):
    """O97: the helper runs as root and is given a relative name by the harness: it reads only a short list of `/proc`
    entries, never a command line or an environment, and no name that leaves the process's directory."""
    answer = run({"targets": [], "reads": [{"pid": 31, "start": START, "kind": "read", "rel": rel}]})["reads"]
    assert answer == [{"ok": False, "err": "refused"}]


def test_o97_an_oversize_read_and_a_cut_listing_are_said_not_made_up(tree):
    """O97: a `read_at` of more than `READ_CAP` bytes is refused; a process with more fds than the snapshot reads says
    its listing was cut, and the fds past the cut are not in the snapshot."""
    (tree / "31" / "fd" / "5").symlink_to("/work/more")
    (tree / "31" / "fdinfo" / "5").write_text("pos:\t0\nmnt_id:\t61\n")
    answer = run(
        {
            "targets": [],
            "reads": [{"pid": 31, "start": START, "kind": "read_at", "rel": "mem", "offset": 0, "n": 10**6}],
        }
    )
    assert answer["reads"] == [{"ok": False, "err": "refused"}]
    old = procsnap.FD_CAP
    procsnap.FD_CAP = 2
    try:
        shot = run({"targets": [target()], "snapshot": True})["results"][0]["snapshot"]
    finally:
        procsnap.FD_CAP = old
    assert shot["cut"] == ["fd"]
    assert "fd/5" not in shot["links"]
    assert "fd/3" in shot["links"]


def test_o97_a_request_that_is_not_one_is_refused_with_exit_2_and_nothing_read(tree):
    """O97: the exit code is 2 for a request that cannot be read, as the signal helper's is."""
    for text in ("not json", json.dumps({"targets": "x"}), json.dumps({"targets": [{"pid": True, "start": "1"}]})):
        out, err = io.StringIO(), io.StringIO()
        assert procsnap.main([], io.StringIO(text), out, err) == 2
        assert out.getvalue() == ""


def test_o97_the_program_reads_a_real_process_of_this_user_as_a_subprocess_would(monkeypatch):
    """O97: the helper's source, run as `PYTHON -I -B -c SOURCE` as the harness runs it (here as this user), reads a
    live process: its threads and fds, with the start tick it was listed at."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not Path(f"/proc/{child.pid}/task").exists():
            time.sleep(0.01)
        text = Path(f"/proc/{child.pid}/stat").read_text()
        start = text[text.rfind(")") + 2 :].split()[19]
        done = subprocess.run(
            [sys.executable, "-I", "-B", "-c", Path(procsnap.__file__).read_text(encoding="utf-8")],
            input=json.dumps({"targets": [target(child.pid, start)], "snapshot": True}),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert done.returncode == 0, done.stderr
        [one] = json.loads(done.stdout)["results"]
        assert one["status"] == "ok"
        assert str(child.pid) in one["snapshot"]["listings"]["task"]
        assert one["snapshot"]["links"]["cwd"] == os.getcwd()
    finally:
        child.kill()
        child.wait()
