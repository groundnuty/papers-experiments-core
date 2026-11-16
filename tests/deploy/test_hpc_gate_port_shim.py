"""O61: the cell's connect-side 443 mapper (deploy/hpc-federation/bundle/gateport.c) sends a connect() to 127.0.0.1:443
to the gate's port, and touches no other connection.

The published REST client dials port 443 only, and on a batch node the gate cannot bind below 1024, so the cell loads
this shim: a TCP connect to 127.0.0.1:443, and to no IPv6 address, goes to the gate's port, which the shim reads from
the cell's /etc/expcore/gate.json and, without that file, from EXPCORE_GATE_PORT. It is built with the host's gcc and
every warning an error, as the run builds it inside the cell's image, and used by a small Python client (the published
client's own path: `socket.connect`). Three witnesses tell what a connect really reached: a listener of the test's that
takes it; a stand-in for the next `connect()` in the preload list (deploy/hpc-federation/tests/fake_next_connect.c),
which records the destination the shim passes on and fails, so no packet leaves; and the egress shim (egresslog.c,
loaded after this one), which logs what it is asked to connect to. Nothing leaves the machine.
"""

import fcntl
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HPC = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation"
SHIM = HPC / "bundle" / "gateport.c"
EGRESS = HPC / "bundle" / "egresslog.c"
FAKE = HPC / "tests" / "fake_next_connect.c"
PROBE = HPC / "tests" / "gateport_probe.c"
GCC = shutil.which("gcc")
FLAGS = [
    "-shared",
    "-fPIC",
    "-O2",
    "-fno-stack-protector",
    "-Wall",
    "-Wextra",
    "-Werror",
]  # build-rootfs.sh's, sandbox.sh's
ENV = "EXPCORE_GATE_PORT"
CONFIG = "/etc/expcore/gate.json"
ENETDOWN = 100  # what the stand-in's connect() fails with

pytestmark = pytest.mark.skipif(GCC is None, reason="no gcc on this host")


def _ipv6() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
    except OSError:
        return False
    return True


def _unix() -> bool:
    try:
        socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM).close()
    except OSError:
        return False
    return True


needs_ipv6 = pytest.mark.skipif(not _ipv6(), reason="this host has no usable ::1")
needs_unix = pytest.mark.skipif(not _unix(), reason="this host does not permit Unix sockets (a sandbox)")


def _compile(argv: list[str]) -> None:
    built = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    assert built.returncode == 0, built.stderr


@pytest.fixture(scope="module")
def built():
    """The shims, compiled once. `gateport` reads its description of the gate from <root>/gate.json (the tests' build);
    `shipped` is the build the run makes, with /etc/expcore/gate.json. `egress` has its socket path in the same
    directory."""
    root = Path(tempfile.mkdtemp(prefix="gp"))  # short: a Unix socket's path is at most 107 bytes
    sock = root / "eg.sock"
    _compile(
        [
            GCC,
            *FLAGS,
            f'-DEXPCORE_GATE_CONFIG_FILE="{root / "gate.json"}"',
            "-o",
            str(root / "gateport.so"),
            str(SHIM),
            "-ldl",
        ]
    )
    _compile([GCC, *FLAGS, "-o", str(root / "shipped.so"), str(SHIM), "-ldl"])
    _compile([GCC, *FLAGS, f'-DHPCOP_EGRESS_SOCKET="{sock}"', "-o", str(root / "egresslog.so"), str(EGRESS)])
    _compile([GCC, "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(root / "fake.so"), str(FAKE)])
    _compile([GCC, "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(root / "probe"), str(PROBE)])
    yield {
        "root": root,
        "gateport": root / "gateport.so",
        "shipped": root / "shipped.so",
        "egress": root / "egresslog.so",
        "fake": root / "fake.so",
        "probe": root / "probe",
        "sock": sock,
        "file": root / "gate.json",
    }
    shutil.rmtree(root)


@pytest.fixture
def gate_file(built):
    """The cell's /etc/expcore/gate.json as the tests' build reads it: `write(text or object)`; absent until written."""
    path = built["file"]

    def clean():
        if path.is_dir():
            path.rmdir()
        path.unlink(missing_ok=True)

    def write(content):
        clean()
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")

    clean()
    yield write
    clean()


def description(port, **more) -> dict:
    """What the cell's /etc/expcore/gate.json says."""
    return {"address": "127.0.0.1", "port": port, "names": ["zone.test", "alpha.test"], **more}


CLIENT = r"""
import json, socket, sys
steps = json.loads(sys.argv[1])
out = []
for kind, host, port in steps:
    result = {"kind": kind, "host": host, "port": port}
    family = socket.AF_UNIX if kind == "unix" else socket.AF_INET6 if ":" in host else socket.AF_INET
    protocol = int(kind[5:]) if kind.startswith("proto") else 0
    try:
        s = socket.socket(family, socket.SOCK_DGRAM if kind == "udp" else socket.SOCK_STREAM, protocol)
    except OSError as e:
        result["no_socket"] = e.errno
        out.append(result)
        continue
    s.settimeout(5)
    try:
        if kind == "mapped":
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        if kind == "nonblocking":
            s.setblocking(False)
            result["errno"] = s.connect_ex((host, port))
        else:
            s.connect(host if kind == "unix" else (host, port))
            result["peer"] = s.getpeername()[1]
            if kind == "send":
                s.sendall(b"hello")
    except OSError as e:
        result["errno"] = e.errno
    s.close()
    out.append(result)
print(json.dumps(out))
"""


def run_client(steps, preload, env=None, stderr=None):
    """Run the client under LD_PRELOAD=`preload` with `env` added; (its results by step, its stderr). With `stderr`, a
    file descriptor of the test's, the client's stderr goes there and is not captured (its text is then "")."""
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LD_PRELOAD": ":".join(str(p) for p in preload)}
    done = subprocess.run(
        [sys.executable, "-I", "-c", CLIENT, json.dumps(steps)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if stderr is None else stderr,
        text=True,
        timeout=60,
        env={**base, **(env or {})},
        check=False,
    )
    assert done.returncode == 0, (done.returncode, done.stderr)
    return json.loads(done.stdout), done.stderr or ""


def listener(family=socket.AF_INET, host="127.0.0.1") -> socket.socket:
    s = socket.socket(family)
    s.bind((host, 0))
    s.listen(8)
    return s


def taken(server: socket.socket, wait=0.5) -> list[bytes]:
    """What a listener received: each connection it holds, with the bytes the client sent (b"" if none)."""
    got = []
    server.settimeout(wait)
    while True:
        try:
            conn, _ = server.accept()
        except OSError:  # TimeoutError: nothing more is queued
            return got
        conn.settimeout(0.3)
        try:
            got.append(conn.recv(16))
        except OSError:
            got.append(b"")
        conn.close()


def port_of(server) -> int:
    return server.getsockname()[1]


def via_the_stand_in(built, tmp_path, steps, env=None):
    """The client under this shim with the stand-in as the next connect(): (results, stderr, destinations it saw)."""
    log = tmp_path / "connects.log"
    results, stderr = run_client(
        steps, [built["gateport"], built["fake"]], {"FAKE_CONNECT_LOG": str(log), **(env or {})}
    )
    seen = [tuple(line.split()) for line in log.read_text().splitlines()] if log.exists() else []
    return results, stderr, seen


# ----------------------------------------------------------------------------------------------------------- build


def test_the_shim_compiles_without_a_warning(tmp_path):
    """The run builds it in the cell's image with the image's gcc, as the other shims: every warning an error."""
    _compile([GCC, *FLAGS, "-o", str(tmp_path / "gateport.so"), str(SHIM), "-ldl"])
    assert (tmp_path / "gateport.so").stat().st_size > 0


@pytest.mark.skipif(shutil.which("nm") is None, reason="no nm on this host")
def test_the_shim_exports_connect_and_nothing_else(built):
    """It interposes one call; no other symbol of a process can come from it."""
    shown = subprocess.run(
        ["nm", "-D", "--defined-only", str(built["gateport"])], capture_output=True, text=True, check=True
    )
    assert [line.split()[-1] for line in shown.stdout.splitlines()] == ["connect"]


def test_the_shipped_build_reads_the_cells_description_of_the_gate_at_its_fixed_path(built):
    assert CONFIG.encode() in built["shipped"].read_bytes()
    assert str(built["file"]).encode() not in built["shipped"].read_bytes()


@pytest.mark.skipif(Path(CONFIG).exists(), reason="this host has a /etc/expcore/gate.json of its own")
def test_the_shipped_build_maps_by_the_environment_when_there_is_no_description(built):
    gate = listener()
    [result], stderr = run_client([["send", "127.0.0.1", 443]], [built["shipped"]], {ENV: str(port_of(gate))})
    assert (result["peer"], stderr) == (port_of(gate), "")
    assert taken(gate) == [b"hello"]


# ------------------------------------------------------------------------------------------------------- redirects


def test_a_connect_to_loopback_443_lands_on_the_gates_port(built):
    """O61: the published client's one connect, to 127.0.0.1:443, reaches the listener at the gate's port."""
    gate = listener()
    [result], _ = run_client([["send", "127.0.0.1", 443]], [built["gateport"]], {ENV: str(port_of(gate))})
    assert result["peer"] == port_of(gate)
    assert taken(gate) == [b"hello"]


@needs_ipv6
def test_a_connect_to_ipv6_loopback_443_is_not_rewritten_and_reaches_no_listener_at_the_gates_port(built):
    """O61: the gate listens on 127.0.0.1 only, so [::1]:443 rewritten to [::1]:<gate port> would reach whatever other
    socket holds that address, another user's on a shared node. It goes through as asked: a listener at the gate's port
    on [::1], standing for that stranger, is never reached."""
    stranger = listener(socket.AF_INET6, "::1")
    [result], _ = run_client([["send", "::1", 443]], [built["gateport"]], {ENV: str(port_of(stranger))})
    assert result.get("peer") in (None, 443)  # refused, unless the host has a listener of its own on [::1]:443
    assert taken(stranger) == []


def test_every_connect_of_a_process_to_443_is_mapped_not_only_the_first(built):
    gate = listener()
    results, _ = run_client([["send", "127.0.0.1", 443]] * 4, [built["gateport"]], {ENV: str(port_of(gate))})
    assert [r["peer"] for r in results] == [port_of(gate)] * 4
    assert taken(gate) == [b"hello"] * 4


def test_the_port_comes_from_the_cells_file_when_the_environment_is_cleared(built, gate_file):
    """O61: the agent's code can clear its environment; the file is where the cell's description of the gate is."""
    gate = listener()
    gate_file(description(port_of(gate)))
    [result], stderr = run_client([["send", "127.0.0.1", 443]], [built["gateport"]])
    assert (result["peer"], stderr) == (port_of(gate), "")
    assert taken(gate) == [b"hello"]


def test_the_file_decides_when_the_environment_names_another_port(built, gate_file):
    """The file is the source to rely on: an environment that says otherwise sends no connect of the cell elsewhere."""
    gate, other = listener(), listener()
    gate_file(description(port_of(gate)))
    [result], _ = run_client([["send", "127.0.0.1", 443]], [built["gateport"]], {ENV: str(port_of(other))})
    assert result["peer"] == port_of(gate)
    assert taken(gate) == [b"hello"]
    assert taken(other) == []


def test_a_file_with_other_keys_nested_values_escapes_and_whitespace_is_read(built, gate_file):
    gate = listener()
    gate_file(
        '{\n  "names": ["zone.test", "al\\u0070ha.test", {"k": [1, -2.5e3, true, false, null, "q\\"\\\\"], "e": {}}],\n'
        f'  "p\\u006frt" : {port_of(gate)} ,\n  "address":"127.0.0.1"\n}}\n'
    )
    [result], stderr = run_client([["send", "127.0.0.1", 443]], [built["gateport"]])
    assert (result["peer"], stderr) == (port_of(gate), "")


def test_the_rewritten_connect_goes_to_the_next_connect_with_the_gates_port_and_its_result_is_the_next_ones(
    built, tmp_path
):
    """The shim calls the next connect() in the lookup order, not the kernel, and returns what it returns."""
    steps = [["tcp", "127.0.0.1", 443], ["nonblocking", "127.0.0.1", 443]]
    results, _, seen = via_the_stand_in(built, tmp_path, steps, {ENV: "34567"})
    assert seen == [("tcp", "inet", "127.0.0.1", "34567")] * 2
    assert [r["errno"] for r in results] == [ENETDOWN, ENETDOWN]


# ---------------------------------------------------------------------------------------------------- left alone


def test_a_connect_to_another_loopback_port_is_untouched(built):
    gate, other = listener(), listener()
    [result], _ = run_client([["send", "127.0.0.1", port_of(other)]], [built["gateport"]], {ENV: str(port_of(gate))})
    assert result["peer"] == port_of(other)
    assert taken(other) == [b"hello"]
    assert taken(gate) == []


def test_a_connect_to_443_on_another_loopback_address_is_untouched(built):
    """Only 127.0.0.1 itself: 127.0.0.2 is loopback too, and is no name of the cell's."""
    gate = listener(host="0.0.0.0")
    [result], _ = run_client([["send", "127.0.0.2", 443]], [built["gateport"]], {ENV: str(port_of(gate))})
    assert result.get("peer") in (None, 443)  # refused, unless the host has a listener of its own on 443
    assert taken(gate) == []


def test_a_udp_connect_to_loopback_443_is_untouched(built):
    """TCP only: a UDP socket's destination is not rewritten (its peer is still 443)."""
    gate = listener()
    [result], _ = run_client([["udp", "127.0.0.1", 443]], [built["gateport"]], {ENV: str(port_of(gate))})
    assert result["peer"] == 443
    assert taken(gate) == []


def test_every_other_destination_reaches_the_next_connect_unchanged(built, tmp_path):
    """O61: the destinations the shim must not touch reach the next connect() as asked: the same address and port.
    (No packet leaves: the stand-in fails every call.) The result is always the next connect()'s."""
    steps = [
        ["tcp", "127.0.0.1", 80],
        ["tcp", "127.0.0.1", 444],
        ["tcp", "127.0.0.1", 8443],
        ["tcp", "127.0.0.2", 443],
        ["tcp", "192.0.2.1", 443],
        ["tcp", "10.255.255.254", 443],
        ["udp", "127.0.0.1", 443],
        ["unix", "/nonexistent-expcore-test/sock", 0],
    ]
    results, stderr, seen = via_the_stand_in(built, tmp_path, steps, {ENV: "34567"})
    assert stderr == ""
    assert seen == [
        ("tcp", "inet", "127.0.0.1", "80"),
        ("tcp", "inet", "127.0.0.1", "444"),
        ("tcp", "inet", "127.0.0.1", "8443"),
        ("tcp", "inet", "127.0.0.2", "443"),
        ("tcp", "inet", "192.0.2.1", "443"),
        ("tcp", "inet", "10.255.255.254", "443"),
        ("udp", "inet", "127.0.0.1", "443"),
        ("tcp", "unix", "-", "0"),
    ]
    assert [r["errno"] for r in results] == [ENETDOWN] * len(steps)


@needs_ipv6
def test_every_other_ipv6_destination_reaches_the_next_connect_unchanged(built, tmp_path):
    """O61: no IPv6 destination is rewritten: [::1]:443 and [::ffff:127.0.0.1]:443 reach the next connect() as asked,
    with the rest. The mapper rewrites 127.0.0.1:443 and nothing else."""
    steps = [
        ["tcp", "::1", 443],
        ["tcp", "::1", 80],
        ["tcp", "::2", 443],
        ["mapped", "::ffff:127.0.0.1", 443],
        ["udp", "::1", 443],
    ]
    results, _, seen = via_the_stand_in(built, tmp_path, steps, {ENV: "34567"})
    assert seen == [
        ("tcp", "inet6", "::1", "443"),
        ("tcp", "inet6", "::1", "80"),
        ("tcp", "inet6", "::2", "443"),
        ("tcp", "inet6", "::ffff:127.0.0.1", "443"),
        ("udp", "inet6", "::1", "443"),
    ]
    assert [r["errno"] for r in results] == [ENETDOWN] * len(steps)


@pytest.mark.parametrize(("name", "protocol"), [("sctp", 132), ("mptcp", 262)])
def test_a_stream_socket_that_is_not_tcp_is_untouched(built, tmp_path, name, protocol):
    """O61: SOCK_STREAM is also SCTP and MPTCP; only TCP is the REST client's, and a plain TCP socket given by its
    protocol number is still mapped (the control)."""
    steps = [[f"proto{protocol}", "127.0.0.1", 443], ["proto6", "127.0.0.1", 443]]
    results, _, seen = via_the_stand_in(built, tmp_path, steps, {ENV: "34567"})
    if "no_socket" in results[0]:
        pytest.skip(f"this host has no {name} sockets (errno {results[0]['no_socket']})")
    assert seen == [("tcp", "inet", "127.0.0.1", "443"), ("tcp", "inet", "127.0.0.1", "34567")]


def test_a_socket_address_shorter_than_its_family_needs_is_never_read_past_its_length(built):
    """O61: the shim reads nothing of the address before its length allows: every short length, at the end of the last
    readable page, comes back EINVAL from the kernel, and the process lives."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LD_PRELOAD": str(built["gateport"]), ENV: "34567"}
    done = subprocess.run(
        [str(built["probe"]), "short"], capture_output=True, text=True, timeout=60, env=env, check=False
    )
    assert done.returncode == 0, (done.returncode, done.stderr)
    lines = [line.split() for line in done.stdout.splitlines()]
    assert len(lines) == 16 + 24
    assert {(r[2], r[3]) for r in lines} == {("-1", "22")}  # EINVAL, every one


@needs_ipv6
def test_the_short_ipv6_address_the_kernel_accepts_goes_through_unchanged(built, tmp_path):
    """O61: [::1]:443 as the 24-byte address (no scope id), at the end of the last readable page, is read for its family
    only and reaches the next connect() as it was given."""
    log = tmp_path / "connects.log"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LD_PRELOAD": f"{built['gateport']}:{built['fake']}",
        "FAKE_CONNECT_LOG": str(log),
        ENV: "34567",
    }
    done = subprocess.run(
        [str(built["probe"]), "v6short"], capture_output=True, text=True, timeout=60, env=env, check=False
    )
    assert done.returncode == 0, (done.returncode, done.stderr)
    assert done.stdout.split() == ["-1", "0"]  # the stand-in's failure, no peer
    assert [tuple(line.split()) for line in log.read_text().splitlines()] == [("tcp", "inet6", "::1", "443")]


# --------------------------------------------------------------------------------------- a variable or file unusable


def test_without_a_file_or_the_variable_nothing_is_redirected_and_nothing_is_said(built):
    """Loaded where the cell's launcher set no port (a process that dropped the environment): inert, and silent."""
    gate, other = listener(), listener()
    steps = [["send", "127.0.0.1", 443], ["send", "127.0.0.1", port_of(other)]]
    results, stderr = run_client(steps, [built["gateport"]])
    assert results[1]["peer"] == port_of(other)
    assert taken(gate) == []
    assert stderr == ""


@pytest.mark.parametrize(
    "value",
    [
        "",
        "abc",
        "443x",
        " 8080",
        "8080 ",
        "+8080",
        "-1",
        "0",
        "65536",
        "99999999999999999999",
        "0x1F90",
        "80.80",
        "\uff18\uff10\uff18\uff10",
    ],
)
def test_a_malformed_variable_leaves_every_connection_alone_and_says_so_once(built, value):
    """O61: a port is decimal digits, 1 to 65535: anything else and the shim redirects nothing, and one line on stderr
    says why however many connects the process makes."""
    gate, other = listener(), listener()
    steps = [["send", "127.0.0.1", 443]] * 3 + [["send", "127.0.0.1", port_of(other)]]
    results, stderr = run_client(steps, [built["gateport"]], {ENV: value})
    assert results[-1]["peer"] == port_of(other)  # the process's other connections are untouched
    assert taken(gate) == []
    lines = stderr.splitlines()
    assert len(lines) == 1, stderr
    assert ENV in lines[0], stderr


def test_a_valid_variable_says_nothing(built):
    gate = listener()
    _, stderr = run_client([["send", "127.0.0.1", 443]], [built["gateport"]], {ENV: str(port_of(gate))})
    assert stderr == ""
    assert taken(gate) == [b"hello"]


UNUSABLE_FILES = {
    "empty": "",
    "not an object": "[]",
    "truncated": '{"address": "127.0.0.1", "port": 8080',
    "no port": '{"address": "127.0.0.1"}',
    "no address": '{"port": 8080}',
    "port as a string": '{"address": "127.0.0.1", "port": "8080"}',
    "port zero": '{"address": "127.0.0.1", "port": 0}',
    "port too large": '{"address": "127.0.0.1", "port": 65536}',
    "port with a fraction": '{"address": "127.0.0.1", "port": 8080.5}',
    "port with an exponent": '{"address": "127.0.0.1", "port": 1e3}',
    "negative port": '{"address": "127.0.0.1", "port": -8080}',
    "port with a leading zero": '{"address": "127.0.0.1", "port": 08080}',
    "port null": '{"address": "127.0.0.1", "port": null}',
    "another address": '{"address": "10.0.0.1", "port": 8080}',
    "address that only starts right": '{"address": "127.0.0.10", "port": 8080}',
    "address not a string": '{"address": 127, "port": 8080}',
    "port twice": '{"address": "127.0.0.1", "port": 8080, "port": 8081}',
    "address twice": '{"address": "127.0.0.1", "address": "127.0.0.1", "port": 8080}',
    "trailing comma": '{"address": "127.0.0.1", "port": 8080,}',
    "trailing text": '{"address": "127.0.0.1", "port": 8080} x',
    "bad value elsewhere": '{"address": "127.0.0.1", "port": 8080, "names": [1, }',
    "bad escape": '{"address": "127.0.0.1", "port": 8080, "names": ["a\\q"]}',
    "too deep": '{"address": "127.0.0.1", "port": 8080, "n": ' + "[" * 40 + "]" * 40 + "}",
    "too large": json.dumps(description(8080, names=["x" * 100] * 50)),
}


@pytest.mark.parametrize("content", UNUSABLE_FILES.values(), ids=UNUSABLE_FILES.keys())
def test_an_unusable_file_leaves_every_connection_alone_says_so_once_and_the_environment_is_not_consulted(
    built, gate_file, content
):
    """A file that exists decides, so one that cannot be used does not hand the decision to the environment: the process
    redirects nothing, even with a valid port in its environment, and one line on stderr names the file."""
    gate = listener()
    gate_file(content)
    results, stderr = run_client([["send", "127.0.0.1", 443]] * 3, [built["gateport"]], {ENV: str(port_of(gate))})
    assert taken(gate) == []
    assert [r.get("peer") for r in results] == [None] * 3 or all(r.get("peer") == 443 for r in results)
    lines = stderr.splitlines()
    assert len(lines) == 1, stderr
    assert "gate.json" in lines[0], stderr
    assert ENV not in lines[0], stderr


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_a_path_that_is_no_regular_file_leaves_every_connection_alone_and_says_so_once(built, gate_file, kind):
    """A directory opens and does not read; a FIFO nothing writes would block an open for ever, and must not hold the
    process that loads the shim (the client has a timeout of its own, so a hang is a failure here)."""
    gate = listener()
    gate_file("{}")
    built["file"].unlink()
    if kind == "directory":
        built["file"].mkdir()
    else:
        os.mkfifo(built["file"])
    _, stderr = run_client([["send", "127.0.0.1", 443]], [built["gateport"]], {ENV: str(port_of(gate))})
    assert taken(gate) == []
    assert len(stderr.splitlines()) == 1, stderr
    assert "gate.json" in stderr


def test_a_stderr_pipe_nobody_reads_does_not_kill_the_process_that_loads_the_shim(built):
    """O61: the line is written in a constructor, before main(): a pipe whose reader has gone must not end the process
    with SIGPIPE (run_client asserts the exit status 0)."""
    read, write = os.pipe()
    os.close(read)
    try:
        results, _ = run_client([["udp", "127.0.0.1", 443]], [built["gateport"]], {ENV: "not-a-port"}, stderr=write)
    finally:
        os.close(write)
    assert results[0]["peer"] == 443


def test_a_stderr_pipe_that_is_full_does_not_hold_the_process_that_loads_the_shim(built):
    """O61: a write to a full pipe blocks, and a process that waited for its reader before main() would hang: the
    client has 60 s, and finishes at once."""
    read, write = os.pipe()
    flags = fcntl.fcntl(write, fcntl.F_GETFL)
    fcntl.fcntl(write, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    try:
        while True:  # fill it
            try:
                os.write(write, b"x" * 4096)
            except BlockingIOError:
                break
        fcntl.fcntl(write, fcntl.F_SETFL, flags)  # the client's stderr is this same description: blocking again
        results, _ = run_client([["udp", "127.0.0.1", 443]], [built["gateport"]], {ENV: "not-a-port"}, stderr=write)
    finally:
        os.close(write)
        os.close(read)
    assert results[0]["peer"] == 443


def test_a_stderr_file_gets_the_line_appended_and_keeps_its_content(built, tmp_path):
    """O61: a regular file is written through the program's own descriptor, with its O_APPEND and offset: what the
    program had written there before is not overwritten."""
    log = tmp_path / "stderr.log"
    log.write_text("keep\n", encoding="utf-8")
    fd = os.open(log, os.O_WRONLY | os.O_APPEND)
    try:
        run_client([["udp", "127.0.0.1", 443]], [built["gateport"]], {ENV: "not-a-port"}, stderr=fd)
    finally:
        os.close(fd)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, lines
    assert lines[0] == "keep"
    assert ENV in lines[1]


# ----------------------------------------------------------------------------- with the egress shim (O52, order)


def egress_records(receiver) -> list[tuple[str, int]]:
    """The `connect` records the egress shim sent to its receiver, as address and port, in order."""
    receiver.settimeout(0.5)
    out = []
    while True:
        try:
            line = receiver.recv(4096).decode()
        except OSError:
            return out
        fields = line.split()
        if fields[1] == "connect":
            out.append((fields[7], int(fields[8])))


@pytest.fixture
def receiver(built):
    """The egress shim's records are datagrams to a socket path compiled into it: bind one for this test."""
    built["sock"].unlink(missing_ok=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(str(built["sock"]))
    yield s
    s.close()
    built["sock"].unlink(missing_ok=True)


@needs_unix
def test_before_the_egress_shim_the_egress_log_shows_the_gates_port(built, receiver):
    """O52: the egress shim logs what it is asked to connect to, so loaded after this one it records the gate's
    port, the destination of the connect, and a canary connect to the gate matches the log; a destination this shim
    left alone is logged as it was asked for."""
    gate = listener()
    steps = [["send", "127.0.0.1", 443], ["send", "127.0.0.2", 443]]
    results, _ = run_client(steps, [built["gateport"], built["egress"]], {ENV: str(port_of(gate))})
    assert results[0]["peer"] == port_of(gate)
    assert taken(gate) == [b"hello"]
    assert egress_records(receiver) == [("127.0.0.1", port_of(gate)), ("127.0.0.2", 443)]


@needs_unix
def test_loaded_after_the_egress_shim_this_one_is_never_reached(built, receiver):
    """The order is a requirement: the egress shim goes straight to the kernel, so a mapper listed after it is
    hidden. The connect then goes to 443 as asked, is logged as that, and nothing reaches the gate."""
    gate = listener()
    results, _ = run_client(
        [["send", "127.0.0.1", 443]], [built["egress"], built["gateport"]], {ENV: str(port_of(gate))}
    )
    assert results[0].get("peer") in (None, 443)
    assert taken(gate) == []
    assert egress_records(receiver) == [("127.0.0.1", 443)]
