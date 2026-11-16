"""O61: with `peer_uid_check` on, the data gate serves a connection only from a socket of its own uid, and refuses every
other before it reads or writes a byte; with the flag off (Docker's gate, on a network of its own) nothing changes.

The gate runs in-process, as in test_gate.py: a TLS listener on a loopback port and clients that speak TLS to it
with the run's test CA. A request the gate answers by itself (a write, which it refuses with 403) tells a served
connection from a refused one, so no upstream is needed. The kernel's real tables attribute these connections to this
process's uid; another uid's case is a check built for a different one, which reads the same real row and finds it is
not that uid's. Host names are invented (`.test`); nothing leaves the machine.
"""

import contextlib
import http.client
import json
import os
import shutil
import socket
import ssl
import threading
import time
from pathlib import Path

import pytest

from expcore_onedata.sandbox import gate, gatepeer
from expcore_onedata.sandbox.hostops import DockerHost

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="the openssl command makes the run CA")

ZONE = "zone.test"
FILE = "/api/v3/oneprovider/data/f1/content"
MINE = os.getuid()
needs_proc = pytest.mark.skipif(not os.access(gatepeer.PROC_NET_TCP, os.R_OK), reason="no readable /proc/net/tcp")


def peer():
    """The gate's own copy of gatepeer, which it loads by path: the one its check is made of."""
    return gate._peer_module()


def other_uid() -> int:
    return next(u for u in range(MINE + 1, MINE + 10) if u not in (0, gatepeer.overflow_uid()))


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    directory = tmp_path_factory.mktemp("tls")
    DockerHost().make_ca(directory, [ZONE])
    return directory


@pytest.fixture
def world(tls, tmp_path, monkeypatch):
    """A gate with its owner check as the configuration says (`world.start(flag)`), and the log it writes."""
    log = tmp_path / "requests.jsonl"
    log.touch()
    monkeypatch.setattr(gate, "_PEER_CHECK", None)
    gate.CFG.clear()
    gate.CFG.update(
        run_key="run-test-1",
        token="tok-gate-0123456789",
        log=str(log),
        tls_dir=str(tls),
        hosts={ZONE: {"role": "onezone", "addr": "127.0.0.1", "port": 1, "tls": False}},
    )
    started = []

    def start(flag=None):
        if flag is not None:
            gate.CFG["peer_uid_check"] = flag
        gate._start_peer_check()
        httpd = gate.GateServer(("127.0.0.1", 0), gate.Handler)
        httpd.socket = gate._tls_context().wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return httpd

    yield type("World", (), {"start": staticmethod(start), "log": log, "tls": tls})
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()
    gate.CFG.clear()


def lines(world) -> list[dict]:
    return [json.loads(line) for line in world.log.read_text().splitlines() if line.strip()]


def refusals(world) -> list[dict]:
    return [r for r in lines(world) if r.get("event") == "peer_refused"]


def wait_for_refusals(world, count: int) -> list[dict]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        found = refusals(world)
        if len(found) >= count:
            return found
        time.sleep(0.05)
    raise AssertionError(f"{count} refusal(s) not logged in 10 s: {lines(world)}")


def write_answer(world, port) -> int:
    """The status of the gate's own answer to a write, over TLS: it proves the connection was served."""
    context = ssl.create_default_context(cafile=str(world.tls / "ca.pem"))
    raw = socket.create_connection(("127.0.0.1", port), timeout=20)
    conn = http.client.HTTPConnection(ZONE, timeout=20)
    conn.sock = context.wrap_socket(raw, server_hostname=ZONE)
    conn.request("PUT", FILE, body=b"x")
    answer = conn.getresponse()
    answer.read()
    conn.close()
    return answer.status


def bytes_from(port, send=b"") -> bytes:
    """Everything a raw client receives from a listener before it closes: a refused connection gives none."""
    got = b""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        raw.settimeout(10)
        if send:
            with contextlib.suppress(OSError):  # the gate may have reset it already
                raw.sendall(send)
        try:
            while chunk := raw.recv(4096):
                got += chunk
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass
    return got


# ------------------------------------------------------------------------------------------------------ flag off


def test_with_the_flag_off_the_gate_has_no_check_and_serves_as_before(world):
    """O61: absent or false, `peer_uid_check` leaves the gate as it was: no check, no table read, every peer served."""
    for flag in (None, False):
        httpd = world.start(flag)
        assert gate._PEER_CHECK is None
        assert write_answer(world, httpd.server_port) == 403
    assert refusals(world) == []


def test_with_the_flag_off_a_connection_is_passed_on_untouched(world):
    """O61: `verify_request` answers as the base server does, without reading the connection or any table."""
    httpd = world.start()
    untouched = object()  # no getpeername(): reading it would raise
    assert httpd.verify_request(untouched, ("127.0.0.1", 1)) is True


# ------------------------------------------------------------------------------------------------------- flag on


@needs_proc
def test_with_the_flag_on_a_peer_of_the_gates_own_uid_is_served(world):
    """O61: this process is the gate and the client: the kernel's row for the client is ours, so it is served."""
    httpd = world.start(True)
    assert isinstance(gate._PEER_CHECK, peer().OwnerCheck)
    assert write_answer(world, httpd.server_port) == 403
    assert refusals(world) == []


@needs_proc
def test_with_the_flag_on_a_peer_of_another_uid_is_refused_and_never_served_a_byte(world):
    """O61: the row is the client's and is not the uid the check wants, so the connection is closed unread and
    unwritten, and one `peer_refused` line says why and whose it was."""
    httpd = world.start(True)
    gate._PEER_CHECK = peer().OwnerCheck(other_uid())
    hello = b"\x16\x03\x01\x00\x05hello"  # the start of a TLS record: a gate that read it would answer with an alert
    assert bytes_from(httpd.server_port, hello) == b""
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        write_answer(world, httpd.server_port)
    found = wait_for_refusals(world, 2)
    assert {r["reason"] for r in found} == {"other_uid"}
    assert {r["peer_uid"] for r in found} == {MINE}
    assert all(r["listen"] == httpd.server_port and r["peer"].startswith("127.0.0.1:") for r in found)
    assert [r for r in lines(world) if r.get("event") != "peer_refused"] == []  # no request, no connection error


@needs_proc
def test_a_refusal_does_not_end_the_accept_loop(world):
    """O61: after a refused connection the next one is judged on its own."""
    httpd = world.start(True)
    ours = gate._PEER_CHECK
    gate._PEER_CHECK = peer().OwnerCheck(other_uid())
    assert bytes_from(httpd.server_port) == b""
    gate._PEER_CHECK = ours
    assert write_answer(world, httpd.server_port) == 403


def test_with_the_flag_on_a_peer_no_table_lists_is_refused(world, tmp_path):
    """O61: a peer that cannot be attributed is refused, not given the benefit of the doubt."""
    tcp, tcp6 = tmp_path / "tcp", tmp_path / "tcp6"
    tcp.write_text("  sl  local_address rem_address   st\n")
    tcp6.write_text("  sl  local_address rem_address   st\n")
    httpd = world.start(True)
    gate._PEER_CHECK = peer().OwnerCheck(MINE, tables=(str(tcp), str(tcp6)))
    assert bytes_from(httpd.server_port) == b""
    [found] = wait_for_refusals(world, 1)
    assert (found["reason"], found["peer_uid"]) == ("not_found", None)


def test_a_check_that_fails_refuses_the_connection_and_says_so(world):
    """O61: a fault in the check is a refusal, logged with its error: never a connection served unchecked."""

    class Broken:
        def decide(self, conn):
            raise RuntimeError("the check broke")

    httpd = world.start(True)
    gate._PEER_CHECK = Broken()
    assert bytes_from(httpd.server_port) == b""
    [found] = wait_for_refusals(world, 1)
    assert found["reason"] == "check_error"
    assert "the check broke" in found["error"]


# ------------------------------------------------------------------------------------------------------- start


@pytest.mark.parametrize("value", ["true", "yes", 1, 0, None, [], {}])
def test_a_flag_that_is_not_true_or_false_refuses_the_start(world, value):
    """O61: never a value the gate would have to guess at (a string "false" is truthy)."""
    gate.CFG["peer_uid_check"] = value
    with pytest.raises(SystemExit, match="peer_uid_check"):
        gate._start_peer_check()
    assert gate._PEER_CHECK is None


def test_a_gate_run_as_a_uid_the_tables_show_for_nobody_refuses_to_start_with_the_check_on(world, monkeypatch):
    """O61: root's sockets cannot be told from ownerless ones, so the gate does not claim to check them."""
    monkeypatch.setattr(os, "getuid", lambda: 0)
    gate.CFG["peer_uid_check"] = True
    with pytest.raises(SystemExit, match="no owner"):
        gate._start_peer_check()
    assert gate._PEER_CHECK is None


def test_a_gate_that_cannot_read_the_tables_refuses_to_start_with_the_check_on(world, monkeypatch, tmp_path):
    monkeypatch.setattr(peer(), "PROC_NET_TCP", str(tmp_path / "absent"))
    gate.CFG["peer_uid_check"] = True
    with pytest.raises(SystemExit, match="cannot be read"):
        gate._start_peer_check()
    assert gate._PEER_CHECK is None


# ------------------------------------------------------------------------------------------------- tcp forwards


@pytest.fixture
def forward(world):
    """A plain TCP echo server and a gate forward (`tcp_forwards`) to it; returns the forward's port."""
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen(8)

    def serve():
        while True:
            try:
                c, _ = echo.accept()
            except OSError:
                return
            with c:
                data = c.recv(4096)
                c.sendall(b"echo:" + data)

    threading.Thread(target=serve, daemon=True).start()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    gate.CFG["peer_uid_check"] = True
    gate._start_peer_check()
    gate._tcp_forward(port, "127.0.0.1", echo.getsockname()[1])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:  # the forward binds in the calling thread; its accept loop starts after
        try:
            assert (
                bytes_from(port, b"ready") == b"echo:ready"
            )  # held open until answered: a peer that closed at once is refused
            break
        except OSError:
            time.sleep(0.05)
    yield port
    echo.close()


@needs_proc
def test_a_tcp_forward_serves_a_peer_of_the_gates_uid_under_the_check(world, forward):
    """O61: the knowledge-plane forward, which accepts on its own socket, is checked as the HTTPS listener is."""
    assert bytes_from(forward, b"ping") == b"echo:ping"
    assert refusals(world) == []


@needs_proc
def test_a_tcp_forward_refuses_a_peer_of_another_uid_before_any_byte(world, forward):
    gate._PEER_CHECK = peer().OwnerCheck(other_uid())
    assert bytes_from(forward, b"ping") == b""
    found = wait_for_refusals(world, 1)
    assert {r["reason"] for r in found} == {"other_uid"}
    assert {r["listen"] for r in found} == {forward}


LOAD_THE_GATE = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("gate", sys.argv[1] + "/gate.py")
gate = importlib.util.module_from_spec(spec)
sys.modules["gate"] = gate
spec.loader.exec_module(gate)
gate.CFG.update(json.loads(sys.argv[2]))
gate._start_peer_check()
print("started", gate._PEER_CHECK is not None)
"""


@pytest.fixture
def three_files(tmp_path):
    """A gate directory as the HPC steps copy it today: gate.py, gatepolicy.py and gateboundary.py, nothing else."""
    directory = tmp_path / "gate-dir"
    directory.mkdir()
    for name in ("gate.py", "gatepolicy.py", "gateboundary.py"):
        shutil.copy(Path(gate.__file__).parent / name, directory / name)
    return directory


def run_gate_directory(directory: Path, config: dict):
    import subprocess
    import sys

    return subprocess.run(
        [sys.executable, "-I", "-c", LOAD_THE_GATE, str(directory), json.dumps(config)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_gate_directory_of_the_three_files_still_starts_with_the_check_off(three_files):
    """O61: gatepeer.py is loaded on demand, so a directory copied before it existed works while the flag is off."""
    done = run_gate_directory(three_files, {})
    assert (done.returncode, done.stdout.strip()) == (0, "started False"), done.stderr
    done = run_gate_directory(three_files, {"peer_uid_check": False})
    assert (done.returncode, done.stdout.strip()) == (0, "started False"), done.stderr


def test_a_gate_directory_without_gatepeer_refuses_to_start_with_the_check_on(three_files):
    """O61: a gate asked to check its peers that cannot is not left to serve them unchecked."""
    done = run_gate_directory(three_files, {"peer_uid_check": True})
    assert done.returncode != 0
    assert "gatepeer.py" in done.stderr


def test_a_gate_directory_with_gatepeer_starts_with_the_check_on(three_files):
    shutil.copy(Path(gate.__file__).parent / "gatepeer.py", three_files / "gatepeer.py")
    done = run_gate_directory(three_files, {"peer_uid_check": True})
    if "no owner" in done.stderr or "cannot be read" in done.stderr:
        pytest.skip("this host cannot run the check (uid 0, or no /proc/net/tcp)")
    assert (done.returncode, done.stdout.strip()) == (0, "started True"), done.stderr
