"""O60: the host's mount watcher, with real inotify on a directory of the test's: its log as the core reads it.

The watcher in the process is given a `/proc` in memory for its proofs; the one run as its own process reads the real
`/proc` and the real mount table, which hold no FUSE mount of the run's, so its log is read as unproven.
"""

import json
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
from expcore_onedata.sandbox import mountproof as MP
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
    """O60: armed by the host's removal of the token, a cell proved, nothing touched: the core reads it clean."""
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
    assert reading.accesses == {"token_file": 0, "token_dir": 0}
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
    assert reading.accesses["token_dir"] >= 1
    assert verdict_of(snapshot(reading)) == "flagged"


def test_a_file_made_in_the_token_directory_in_the_window_flags_the_run(run):
    proc = FakeProc().add(host_process(99), cell(500))
    w = watcher(run, proc)
    os.unlink(run.token)
    w.tick()
    Path(run.token).write_text("x")
    reading = read_log(finish(w, run), NONCE)
    assert reading.accesses["token_dir"] >= 1


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


def host_table(run) -> str:
    """The mount table the in-process watcher is given (`watcher`): the real one and the run's mount."""
    mount = os.path.realpath(run.mountpoint)
    return real_table() + f"9999 1 {FUSE} / {mount} rw - fuse.oneclient oneclient rw,user_id={UID}\n"


def proofs_of(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.decode().splitlines() if json.loads(line)["event"] == "proof"]


def test_a_failed_cell_proof_in_the_log_carries_what_failed_it_and_names_no_path(run):
    """O60: the line gains `offending` and `process`; the run's paths are not in it, and the core reads it as before."""
    host_proc = next(iter(MP.proc_devs(real_table())))
    bound = table(
        ("0:99997", "/", "/", "overlay"), (host_proc, "/", "/hostproc", "proc"), (FUSE, "/", "/data", "fuse.x")
    )
    w = watcher(run, FakeProc().add(host_process(99), cell_process(500, mountinfo=bound)))
    os.unlink(run.token)
    w.tick()
    data = finish(w, run)
    (line,) = [p for p in proofs_of(data) if p["label"] == "cell"]
    assert line["problems"] == ["host_proc_in_cell"]
    assert [(e["problem"], e["fstype"], e["point"]) for e in line["offending"]] == [
        ("host_proc_in_cell", "proc", "other")
    ]
    assert line["offending_total"] == 1
    assert set(line["process"]) == {"root", "mnt_ns", "root_mount"}
    assert set(line["cell"]) == {"pid", "start_time", "nspid_depth"}
    assert run.mountpoint.encode() not in data
    assert os.path.dirname(run.token).encode() not in data
    assert "host_proc_in_cell" in read_log(data, NONCE).isolation_problems


def test_the_watcher_reads_the_hosts_root_mount_and_its_own_mount_namespace_at_its_start(run):
    proc = FakeProc(own_mnt_ns="mnt:[4026531841]").add(host_process(99))
    w = watcher(run, proc)
    assert w.host.mnt_ns == "mnt:[4026531841]"
    assert w.host.root_mount == MP.host_mounts(host_table(run))[0]
    assert w.host.entries == MP.host_mounts(host_table(run))[1]


def test_a_watcher_that_cannot_read_its_own_mount_namespace_still_starts(run):
    assert watcher(run, FakeProc().add(host_process(99))).host.mnt_ns is None


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
    assert reading.accesses["token_dir"] >= 1
    assert guard_log.MOUNT_UNSEEN in reading.isolation_problems
    assert verdict_of(snapshot(reading)) == "flagged"


def test_o60_the_drivers_read_of_oneclients_log_in_the_window_flags_nothing(tmp_path, run):
    """O60: a study reads oneclient's log during the run (`mount_log()`, before the agent's first turn, say): that read
    is the driver's, of the log directory, which is protected from every cell but which the watcher does not watch
    (it watches the token file and its directory alone). So it is no `token_access`, no `process_access` and leaves the
    isolation proven: the run reads clean, with no access counted."""
    log_dir = tmp_path / "oneclient-log"
    log_dir.mkdir(mode=0o700)
    (log_dir / "oneclient.INFO").write_text("I1116 10:00:00.000000 1 logging.cc:80] Forced proxy IO: 1\n")
    run.protect = [*run.protect, str(log_dir)]  # as the host mount protects it (`hostmount.Layout.protected`)
    proc = FakeProc().add(host_process(99))
    w = watcher(run, proc)
    os.unlink(run.token)
    w.tick()
    assert w.armed
    proc.add(cell(500))
    w.tick()
    for _ in range(3):  # the driver reads it, listing the directory first, during the window
        assert "Forced proxy IO" in (log_dir / "oneclient.INFO").read_text()
        os.listdir(log_dir)
        w.tick()
    del proc.procs[500]
    reading = read_log(finish(w, run), NONCE)
    assert reading.problems == ()
    assert reading.isolation_problems == ()
    assert reading.accesses == {"token_file": 0, "token_dir": 0}
    assert reading.traced == 0
    assert verdict_of(snapshot(reading)) == "clean"
