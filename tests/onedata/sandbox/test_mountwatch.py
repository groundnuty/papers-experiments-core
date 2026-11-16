"""O60: the host's mount watcher, with real inotify on a directory of the test's: its log as the core reads it.

The watcher in the process is given a `/proc` in memory for its proofs; the one run as its own process reads the real
`/proc` and the real mount table, which hold no FUSE mount of the run's, so its log is read as unproven.
"""

import os
import signal
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path

import pytest
from fake_proc import UID, FakeProc, P, cell_process, host_process, table

from expcore.mountguard import log as guard_log
from expcore.mountguard import read_log, snapshot, verdict_of
from expcore_onedata.sandbox import mountwatch as MW

NONCE = "0123456789abcdef0123456789abcdef"
FUSE = "0:99999"  # devices no real table of the test host holds


def real_table() -> str:
    with open("/proc/self/mountinfo", encoding="utf-8") as stream:
        return stream.read()


@pytest.fixture
def run(tmp_path):
    """A run's guard: the token file in its directory, the mount point, the watcher's log path."""
    token_dir, mount = tmp_path / "token", tmp_path / "mount"
    token_dir.mkdir(mode=0o700)
    mount.mkdir()
    token = token_dir / "token"
    token.write_text("secret-token-value")
    return Namespace(
        log=str(tmp_path / "watch.jsonl"), nonce=NONCE, token=str(token), mountpoint=str(mount),
        oneclient="99:1099", protect=[str(token_dir)], poll_s=0.01,
    )  # fmt: skip


def watcher(run, proc: FakeProc) -> MW.Watcher:
    mount = os.path.realpath(run.mountpoint)
    mountinfo = real_table() + f"9999 1 {FUSE} / {mount} rw - fuse.oneclient oneclient rw,user_id={UID}\n"
    own = f"Name:\tx\nUid:\t{UID}\t{UID}\t{UID}\t{UID}\nNSpid:\t7\n"
    return MW.Watcher(run, proc, own, mountinfo)


def cell(pid: int) -> P:
    mounts = table(("0:99997", "/", "/", "overlay"), ("0:99998", "/", "/proc", "proc"), (FUSE, "/", "/data", "fuse.x"))
    return cell_process(pid, mountinfo=mounts)


def finish(w: MW.Watcher, run) -> bytes:
    w.close()
    return Path(run.log).read_bytes()


def test_a_run_that_never_touches_the_token_reads_clean(run, capsys):
    proc = FakeProc().add(host_process(99))
    w = watcher(run, proc)
    w.tick()
    assert not w.armed
    os.unlink(run.token)  # the host's removal once the mount is up: the canary
    w.tick()
    assert w.armed
    assert MW.ARMED in capsys.readouterr().out
    proc.add(cell(500))
    w.tick()
    del proc.procs[500]  # the cell ended before the window closed
    reading = read_log(finish(w, run), NONCE)
    assert reading.problems == ()
    assert reading.isolation_problems == ()
    assert reading.accesses == {"token": 0, "dir": 0}
    assert verdict_of(snapshot(reading)) == "clean"


def test_a_listing_of_the_token_directory_in_the_window_flags_the_run(run):
    proc = FakeProc().add(host_process(99), cell(500))
    w = watcher(run, proc)
    os.unlink(run.token)
    w.tick()
    os.listdir(os.path.dirname(run.token))
    w.tick()
    reading = read_log(finish(w, run), NONCE)
    assert reading.intact
    assert reading.accesses["dir"] >= 1
    assert verdict_of(snapshot(reading)) == "flagged"


def test_a_file_made_in_the_token_directory_in_the_window_flags_the_run(run):
    proc = FakeProc().add(host_process(99), cell(500))
    w = watcher(run, proc)
    os.unlink(run.token)
    w.tick()
    Path(run.token).write_text("x")
    reading = read_log(finish(w, run), NONCE)
    assert reading.accesses["dir"] >= 1


def test_a_tracer_on_oneclient_in_the_window_flags_the_run(run):
    proc = FakeProc().add(host_process(99), cell(500))
    w = watcher(run, proc)
    os.unlink(run.token)
    w.tick()
    proc.procs[99].tracer = 4321
    w.tick()
    reading = read_log(finish(w, run), NONCE)
    assert reading.traced == 1
    assert verdict_of(snapshot(reading)) == "flagged"


def test_a_watcher_stopped_before_the_canary_is_unknown(run):
    reading = read_log(finish(watcher(run, FakeProc().add(host_process(99))), run), NONCE)
    assert guard_log.NO_CANARY in reading.problems
    assert guard_log.NOT_ARMED in reading.problems


def test_a_run_whose_cells_were_never_caught_is_unproven(run):
    w = watcher(run, FakeProc().add(host_process(99)))
    os.unlink(run.token)
    w.tick()
    reading = read_log(finish(w, run), NONCE)
    assert reading.intact
    assert reading.isolation_problems == (guard_log.NO_PROOF,)
    assert verdict_of(snapshot(reading)) == "unknown"


def test_a_cell_alive_when_the_window_closes_makes_the_run_unknown(run):
    proc = FakeProc().add(host_process(99), cell(500))
    w = watcher(run, proc)
    os.unlink(run.token)
    w.tick()
    reading = read_log(finish(w, run), NONCE)  # the cell is still in the table
    assert "cell_alive_at_close" in reading.isolation_problems


def test_an_existing_log_is_refused(run):
    Path(run.log).write_text("")
    with pytest.raises(FileExistsError):
        watcher(run, FakeProc().add(host_process(99)))


def test_a_watcher_that_cannot_read_its_own_namespace_refuses_to_start(run):
    with pytest.raises(SystemExit):
        MW.Watcher(run, FakeProc(), "Name:\tx\n", real_table())
    assert not os.path.exists(run.log)


def test_the_log_names_no_path_and_no_token(run):
    proc = FakeProc().add(host_process(99), cell(500))
    w = watcher(run, proc)
    os.unlink(run.token)
    w.tick()
    Path(os.path.dirname(run.token), "named-by-the-agent").write_text("x")
    data = finish(w, run)
    assert b"secret-token-value" not in data
    assert b"named-by-the-agent" not in data
    assert os.path.dirname(run.token).encode() not in data
    assert oct(os.stat(run.log).st_mode & 0o777) == "0o600"


def _start_time(pid: int) -> str:
    with open(f"/proc/{pid}/stat", encoding="utf-8") as stream:
        return stream.read().rpartition(")")[2].split()[19]


def _wait_for(path: str, line: str, until: float) -> None:
    while time.monotonic() < until:
        if os.path.exists(path) and line in Path(path).read_text().splitlines():
            return
        time.sleep(0.05)
    raise AssertionError(f"the watcher did not print {line!r}")


def test_the_watcher_as_its_own_process_writes_a_log_the_core_reads(run, tmp_path):
    """The real process on the real /proc: armed by the token's removal, an access counted, stopped by SIGTERM. The
    host has no FUSE mount of the run's, so the log is intact and the isolation unproven (`mount_unseen`)."""
    stand_in = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
    out = tmp_path / "watch.stdout"
    try:
        argv = [sys.executable, "-I", "-m", "expcore_onedata.sandbox.mountwatch", "--log", run.log, "--nonce", NONCE,
                "--token", run.token, "--mountpoint", run.mountpoint, "--oneclient",
                f"{stand_in.pid}:{_start_time(stand_in.pid)}", "--protect", run.protect[0],
                "--poll-s", "0.2"]  # fmt: skip
        with open(out, "wb") as stdout:
            watch = subprocess.Popen(argv, stdout=stdout, stderr=subprocess.PIPE)
        until = time.monotonic() + 30
        _wait_for(str(out), MW.READY, until)
        os.unlink(run.token)
        _wait_for(str(out), MW.ARMED, until)
        os.listdir(os.path.dirname(run.token))
        time.sleep(0.5)
        watch.send_signal(signal.SIGTERM)
        assert watch.wait(30) == 0, watch.stderr.read()
    finally:
        stand_in.kill()
        stand_in.wait()
    reading = read_log(Path(run.log).read_bytes(), NONCE)
    assert reading.problems == ()
    assert reading.accesses["dir"] >= 1
    assert guard_log.MOUNT_UNSEEN in reading.isolation_problems
    assert verdict_of(snapshot(reading)) == "flagged"
