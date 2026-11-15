"""The agent's egress shim (deploy/hpc-federation/bundle/egresslog.c) and its collector (egress-collector.py).

Built with the host's gcc and run against a small test program (deploy/hpc-federation/tests/egress_probe.c) that
connects to a loopback listener of its own and, non-blocking, to 192.0.2.1:9 (TEST-NET-1, RFC 5737: never routed, so
no real outside traffic), and sends UDP to loopback. The collector writes the log the core's `expcore.egress` reads.
"""

import hashlib
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HPC = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation"
SHIM = HPC / "bundle" / "egresslog.c"
COLLECTOR = HPC / "bundle" / "egress-collector.py"
PROBE = HPC / "tests" / "egress_probe.c"
GCC = shutil.which("gcc")
PAYLOAD = b"PAYLOAD-7f3a-NEVER-LOGGED"  # egress_probe.c sends it in every datagram
NONCE = "0123456789abcdef0123456789abcdef"
READY = b"egress collector ready"


def _unix_sockets() -> bool:
    try:
        socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM).close()
    except OSError:
        return False
    return True


needs_gcc = pytest.mark.skipif(GCC is None, reason="no gcc on this host")
needs_unix = pytest.mark.skipif(not _unix_sockets(), reason="this host does not permit Unix sockets (a sandbox)")


def _short_dir() -> Path:
    """A private directory with a short path: a Unix socket's path is at most 107 bytes."""
    path = Path(tempfile.mkdtemp(prefix="eg"))
    if len(str(path / "s" / "sock")) >= 100:
        shutil.rmtree(path)
        pytest.skip(f"the temporary directory's path is too long for a Unix socket: {path}")
    return path


def _compile(argv: list[str]) -> None:
    built = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    assert built.returncode == 0, built.stderr


def _start_collector(sock_dir: Path, log: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-I", str(COLLECTOR), str(sock_dir), str(log), NONCE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ready, _, _ = select.select([proc.stdout], [], [], 30)
    line = proc.stdout.readline() if ready else b""
    if READY not in line:
        proc.kill()
        raise AssertionError(f"the collector did not start: {line!r} {proc.stderr.read()!r}")
    return proc


def _stop(proc: subprocess.Popen) -> int:
    proc.send_signal(signal.SIGTERM)
    return proc.wait(timeout=30)


def _lines(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture(scope="module")
def run():
    """Build the shim and the probe, run the probe under the shim with a collector, then stop the collector."""
    if GCC is None or not _unix_sockets():
        pytest.skip("needs gcc and Unix sockets")
    root = _short_dir()
    sock_dir = root / "s"
    sock_dir.mkdir(mode=0o700)
    shim, probe = root / "egresslog.so", root / "egress_probe"
    flags = ["-O2", "-Wall", "-Wextra", "-Werror"]
    _compile(
        [GCC, "-shared", "-fPIC", *flags, f'-DHPCOP_EGRESS_SOCKET="{sock_dir / "sock"}"', "-o", str(shim), str(SHIM)]
    )
    _compile([GCC, *flags, "-o", str(probe), str(PROBE)])
    log = root / "egress.jsonl"
    collector = _start_collector(sock_dir, log)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LD_PRELOAD": str(shim)}
    probed = subprocess.run([str(probe), "all"], capture_output=True, text=True, timeout=60, env=env, check=False)
    status = _stop(collector)
    out = dict(line.split(" ", 1) for line in probed.stdout.splitlines())
    yield {"root": root, "probe": probe, "shim": shim, "env": env, "out": out, "log": log, "status": status}
    shutil.rmtree(root)


def _records(run: dict, op: str) -> list[dict]:
    return [line["rec"] for line in _lines(run["log"]) if line["event"] == "record" and line["rec"]["op"] == op]


@needs_gcc
def test_the_egress_shim_compiles_without_a_warning(tmp_path):
    """The run builds it inside the cell's image with the image's gcc; here with the host's, every warning an error."""
    out = tmp_path / "egresslog.so"
    _compile(
        [
            GCC,
            "-shared",
            "-fPIC",
            "-O2",
            "-fno-stack-protector",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-o",
            str(out),
            str(SHIM),
        ]
    )
    assert out.stat().st_size > 0


@needs_gcc
@needs_unix
def test_a_connect_to_loopback_is_logged_with_its_destination_pid_and_result(run):
    """The attempt goes in before the call and its result after it: 0 for a listener that took it."""
    port = int(run["out"]["listener"])
    attempts = [r for r in _records(run, "connect") if r["address"] == "127.0.0.1" and r["port"] == port]
    results = [r for r in _records(run, "result") if r["address"] == "127.0.0.1" and r["port"] == port]
    assert run["out"]["connect-loopback"] == "0 0"
    assert len(attempts) == 1
    assert attempts[0]["family"] == "inet"
    assert attempts[0]["type"] == "tcp"
    assert attempts[0]["pid"] == int(run["out"]["pid"])
    assert attempts[0]["result"] is None
    assert [r["result"] for r in results] == [0]


@needs_gcc
@needs_unix
def test_a_connect_to_an_outside_address_is_logged_whatever_it_returns(run):
    """O52: TEST-NET-1 is never routed: the non-blocking attempt is logged before the call, and its result after it
    (an errno that depends on the host's network, so not asserted here)."""
    attempts = [r for r in _records(run, "connect") if r["address"] == "192.0.2.1"]
    results = [r for r in _records(run, "result") if r["address"] == "192.0.2.1"]
    assert [(r["port"], r["type"], r["family"]) for r in attempts] == [(9, "tcp", "inet")]
    assert len(results) == 1
    assert isinstance(results[0]["result"], int)


@needs_gcc
@needs_unix
def test_an_ipv6_connect_with_the_short_address_length_the_kernel_accepts_is_logged(run):
    """A 24-byte IPv6 address (no scope id) is one the kernel takes: the shim logs it rather than passing it by."""
    if run["out"]["connect-short-v6"].startswith("skipped"):
        pytest.skip("no IPv6 sockets on this host")
    attempts = [r for r in _records(run, "connect") if r["family"] == "inet6"]
    assert [(r["address"], r["port"]) for r in attempts] == [("::1", 9)]


@needs_gcc
@needs_unix
def test_a_udp_send_is_logged_once_for_each_new_address(run):
    """Two sendto()s to one address give one record; a sendmsg() to another address gives its own."""
    first, second = (int(p) for p in run["out"]["udp"].split())
    sent = [(r["op"], r["address"], r["port"], r["type"]) for r in _records(run, "sendto") + _records(run, "sendmsg")]
    assert sorted(sent) == sorted([("sendto", "127.0.0.1", first, "udp"), ("sendmsg", "127.0.0.1", second, "udp")])


@needs_gcc
@needs_unix
def test_no_payload_reaches_the_log(run):
    """Only addresses, ports, the socket type, the pid, the descriptor, the time and the result: never data."""
    assert run["out"]["sent"] == "3"
    assert PAYLOAD not in run["log"].read_bytes()


@needs_gcc
@needs_unix
def test_the_log_is_sequenced_from_its_nonce_and_closed_by_its_end_line(run):
    """The collector numbers every line from 0, opens with the run's nonce and closes with the counts it wrote."""
    lines = _lines(run["log"])
    assert run["status"] == 0
    assert lines[0]["event"] == "start"
    assert lines[0]["nonce"] == NONCE
    assert lines[0]["format"] == "hpcop-egress/1"
    assert lines[0]["uid"] == os.getuid()
    assert [line["seq"] for line in lines] == list(range(len(lines)))
    assert lines[-1]["event"] == "end"
    assert lines[-1]["records"] == len(lines) - 2
    assert (lines[-1]["malformed"], lines[-1]["unwritten"]) == (0, 0)
    assert all(line["peer"]["uid"] == os.getuid() for line in lines[1:-1])


@needs_gcc
@needs_unix
def test_a_connect_the_shim_cannot_log_is_refused(run):
    """With no collector listening, a connect is refused with EACCES: through the shim, unlogged is unreached."""
    probed = subprocess.run([str(run["probe"]), "connect"], capture_output=True, text=True, timeout=60, env=run["env"])
    out = dict(line.split(" ", 1) for line in probed.stdout.splitlines())
    assert out["connect-loopback"] == f"-1 {13}"  # EACCES


@needs_unix
def test_a_malformed_datagram_is_kept_as_its_size_and_hash_only():
    """A datagram that is no record (the agent's own, say) is never written as it came: its length and SHA-256."""
    root = _short_dir()
    try:
        sock_dir = root / "s"
        sock_dir.mkdir(mode=0o700)
        log = root / "egress.jsonl"
        collector = _start_collector(sock_dir, log)
        junk = b"hpcop-egress/1 connect 1 3 1.000000000 tcp inet not-an-address 9 - 0 " + PAYLOAD + b"\n"
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sender.sendto(junk, str(sock_dir / "sock"))
        sender.close()
        assert _stop(collector) == 0
        lines = _lines(log)
        bad = [line for line in lines if line["event"] == "malformed"]
        assert [(b["bytes"], b["sha256"]) for b in bad] == [(len(junk), hashlib.sha256(junk).hexdigest())]
        assert lines[-1]["malformed"] == 1
        assert PAYLOAD not in log.read_bytes()
    finally:
        shutil.rmtree(root)


@pytest.mark.parametrize("problem", ["log_exists", "socket_dir_open"])
def test_the_collector_refuses_a_log_it_did_not_create_or_a_socket_directory_others_can_enter(problem, tmp_path):
    """A log that already exists may hold an earlier run's lines; a socket directory open to others lets them in."""
    sock_dir = tmp_path / "s"
    sock_dir.mkdir(mode=0o700)
    log = tmp_path / "egress.jsonl"
    if problem == "log_exists":
        log.write_text("", encoding="utf-8")
    else:
        sock_dir.chmod(0o755)
    done = subprocess.run(
        [sys.executable, "-I", str(COLLECTOR), str(sock_dir), str(log), NONCE], capture_output=True, timeout=30
    )
    assert done.returncode == 2
    assert READY not in done.stdout
    assert (b"already exists" if problem == "log_exists" else b"0700") in done.stderr


@needs_gcc
@needs_unix
def test_the_cores_reader_reads_the_real_collectors_log_as_intact_and_classifies_it(run):
    """The core's `expcore.egress` reads what this collector wrote: the probe's first connect, to its own listener,
    declared as the gate and the canary; its UDP receivers are loopback, TEST-NET-1 is outside."""
    from expcore.egress import EgressLog, declaration

    class Source:
        def egress_log(self) -> bytes:
            return run["log"].read_bytes()

    listener = ("127.0.0.1", int(run["out"]["listener"]))
    snap = EgressLog(Source(), declaration([listener], listener), NONCE).finish()
    v6 = 0 if run["out"]["connect-short-v6"].startswith("skipped") else 1  # [::1]:9, where the host has IPv6
    assert (snap["log"], snap["unknown"], snap["canary"]) == ("intact", [], True)
    assert snap["counts"] == {"gate": 0, "loopback": 2 + v6, "outside": 1}
    assert [(d["address"], d["port"]) for d in snap["outside"]] == [("192.0.2.1", 9)]
