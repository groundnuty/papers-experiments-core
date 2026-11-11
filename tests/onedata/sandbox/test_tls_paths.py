"""The sandbox's two clients of the federation take the platform's one `[tls]` setting: the data gate's upstream
connection (the agent's REST data path) and oneclient in the mount sidecar.

Each sandbox starts on the in-memory host (`FakeHost`), which keeps the arguments, mounts and environment each container
was given. The gate then runs in-process with the configuration file the sandbox wrote, its CA bundle read from where
the gate container's read-only mount puts it, against a TLS server on 127.0.0.1 whose certificate chains to a CA made in
the test (`tls_support`). Oneclient runs only in a container, so its half is checked by its arguments here and for real
in `tests/live/test_sandbox.py`.
"""

import contextlib
import hashlib
import http.client
import json
import socket
import ssl
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_host import FakeHost
from platform_support import write_platform
from sandbox_support import TOKEN, add_sandbox, fail_copies, make_spec, start_options
from tls_support import HAS_OPENSSL, TlsServer, make_federation_ca, swap_after_check

from expcore_onedata.config import load_platform
from expcore_onedata.sandbox import containers as C
from expcore_onedata.sandbox import gate
from expcore_onedata.sandbox.api import StartRefused
from expcore_onedata.sandbox.hostops import DockerHost
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.sandbox.sandbox import DockerSandbox

pytestmark = pytest.mark.skipif(not HAS_OPENSSL, reason="the openssl command makes the test CAs")

CA_NAME = "federation-ca.pem"  # the configured value of ca_file, relative to platform.toml
ATTRS = "/api/v3/oneprovider/data/f1"  # a read the gate passes: a file's attributes, answered with JSON
MODES = {
    "verify-default": None,
    "ca_file": {"ca_file": CA_NAME},
    "insecure_tls": {"insecure_tls": True},
}
# Each mode with the CA its bundle holds; the last is a ca_file of a CA the federation's certificate does not chain to,
# which must fail as the default does, so a ca_file that turned verification off would show.
CASES = {
    "verify-default": (None, "federation"),
    "ca_file": ({"ca_file": CA_NAME}, "federation"),
    "insecure_tls": ({"insecure_tls": True}, "federation"),
    "ca_file-of-another-ca": ({"ca_file": CA_NAME}, "stranger"),
}
REACHED = {"verify-default": False, "ca_file": True, "insecure_tls": True, "ca_file-of-another-ca": False}


@pytest.fixture(scope="module")
def federation(tmp_path_factory):
    return make_federation_ca(tmp_path_factory.mktemp("federation"))


@pytest.fixture(scope="module")
def stranger(tmp_path_factory):
    """Another CA, which the federation's certificate does not chain to."""
    return make_federation_ca(tmp_path_factory.mktemp("stranger"), name="unrelated-test-ca")


@pytest.fixture(scope="module")
def run_ca(tmp_path_factory):
    """The run's own CA and the gate's certificates for the configured hosts, which the agent trusts (not [tls])."""
    directory = tmp_path_factory.mktemp("run-tls")
    DockerHost().make_ca(directory, ["zone.test", "alpha.test"])
    return directory


def world_with(tmp_path, tls, federation):
    """A platform document with the sandbox table and `[tls]`, its bundle the CA of `federation`, and an in-memory
    host that knows the images."""

    def edit(data):
        add_sandbox(data)
        if tls is not None:
            data["tls"] = tls

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / CA_NAME).write_bytes(federation.ca.read_bytes())
    config = load_platform(write_platform(tmp_path, edit), require_secrets=False)
    s, host = config.sandbox, FakeHost()
    for ref in (s.agent, s.gate, s.oneclient):
        host.add_image(ref.name, ref.id)
    host.oneclient_images.add(s.oneclient.id)
    return SimpleNamespace(host=host, config=config, s=s, registry=Registry(), tmp_path=tmp_path)


def start(world, level):
    return DockerSandbox.start(world.host, world.s, make_spec(world, level), **start_options(world))


def mounts_of(container) -> dict[str, tuple[str, str]]:
    """A container's `-v` mounts: destination -> (source, options)."""
    out = {}
    for spec in container.options.get("-v", []):
        source, destination, *options = spec.split(":")
        out[destination] = (source, ":".join(options))
    return out


@contextlib.contextmanager
def gate_as_started(sbx, world, upstream, run_ca, log):
    """Run the gate in-process with the configuration the sandbox wrote for its container.

    Only what the test machine changes is replaced: each host's upstream address becomes the TLS server, the run's
    certificates and the log are local files, and a CA bundle is read from the host file the gate container's mount
    puts at the path the configuration names, which must be a read-only mount."""
    config = json.loads((sbx.dir / "gate.json").read_text(encoding="utf-8"))
    if "upstream_ca_file" in config:
        source, options = mounts_of(world.host.containers[sbx.names.gate])[config["upstream_ca_file"]]
        assert options == "ro"
        config["upstream_ca_file"] = source
    config["hosts"] = {
        name: {**host, "addr": "127.0.0.1", "port": upstream.server_port, "tls": True}
        for name, host in config["hosts"].items()
    }
    log.touch()
    config.update(token=TOKEN, log=str(log), tls_dir=str(run_ca), listen_host="127.0.0.1", listen_port=0)
    gate.CFG.clear()
    gate._CTX.clear()  # one client context per process and mode: a context of an earlier test must not be reused
    gate.CFG.update(config)
    httpd = gate.GateServer(("127.0.0.1", 0), gate.Handler)
    httpd.socket = gate._tls_context().wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_port
    finally:
        httpd.shutdown()
        httpd.server_close()
        gate.CFG.clear()
        gate._CTX.clear()


def read_through(port, run_ca, path=ATTRS) -> int:
    """The agent's read through the gate, trusting the run's CA as the agent does; return the answer's status."""
    context = ssl.create_default_context(cafile=str(run_ca / "ca.pem"))
    raw = socket.create_connection(("127.0.0.1", port), timeout=20)
    conn = http.client.HTTPConnection("alpha.test", timeout=20)
    conn.sock = context.wrap_socket(raw, server_hostname="alpha.test")
    try:
        conn.request("GET", path)
        answer = conn.getresponse()
        answer.read()
        return answer.status
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------------- the data gate


def the_run_copy(sbx, source: str) -> bytes:
    """The bytes of a mounted CA bundle, which must be the run's own read-only copy in its directory."""
    copy = Path(source)
    assert copy.parent == sbx.dir
    assert stat.S_IMODE(copy.stat().st_mode) == 0o444
    return copy.read_bytes()


@pytest.mark.parametrize("label", CASES)
def test_o05_the_gates_upstream_checks_the_federations_certificate_as_the_tls_setting_says(
    tmp_path, federation, stranger, run_ca, label
):
    """O05: the gate the sandbox configures refuses a certificate no default trusts (the agent gets a 502), and the
    same with a ca_file of an unrelated CA, and reaches the federation with its own CA as ca_file, read from a
    read-only mount, or with insecure_tls."""
    tls, ca = CASES[label]
    world = world_with(tmp_path, tls, {"federation": federation, "stranger": stranger}[ca])
    sbx = start(world, "via-gate")
    try:
        with TlsServer(federation) as upstream, gate_as_started(sbx, world, upstream, run_ca, tmp_path / "log") as port:
            status = read_through(port, run_ca)
            reached = list(upstream.seen)
    finally:
        sbx.stop()
    if REACHED[label]:
        assert (status, reached) == (200, [("GET", ATTRS)])
    else:
        assert (status, reached) == (502, [])


def test_o05_the_gate_is_given_the_tls_setting_and_the_runs_copy_read_only_and_the_agent_neither(tmp_path, federation):
    """O05: the gate's configuration and mounts carry the setting: the run's own read-only copy of the bundle, never
    the configured file, mounted read-only at the path its configuration names, verification off only with
    insecure_tls; the agent keeps trusting the run's CA alone."""
    seen = {}
    for label, tls in MODES.items():
        world = world_with(tmp_path / label, tls, federation)
        sbx = start(world, "via-gate")
        config = json.loads((sbx.dir / "gate.json").read_text(encoding="utf-8"))
        mounted = mounts_of(world.host.containers[sbx.names.gate]).get(C.GATE_CA_PATH)
        copy = the_run_copy(sbx, mounted[0]) if mounted else None
        seen[label] = (config["verify_ssl"], config.get("upstream_ca_file"), mounted and mounted[1], copy)
        agent = json.dumps(world.host.containers[sbx.names.agent].options, default=str)
        assert CA_NAME not in agent
        assert str(world.config.tls.ca_file) not in json.dumps(world.host.containers[sbx.names.gate].options)
        sbx.stop()
    assert seen == {
        "verify-default": (True, None, None, None),
        "ca_file": (True, C.GATE_CA_PATH, "ro", federation.ca.read_bytes()),
        "insecure_tls": (False, None, None, None),
    }


def test_o05_the_gate_trusts_the_verified_bytes_and_never_a_bundle_swapped_after_the_check(
    tmp_path, monkeypatch, federation, stranger, run_ca
):
    """O05: the bundle swapped for another CA's right after its check is not what the gate trusts: it refuses a
    server of the swapped-in CA (the agent gets a 502) and reaches the federation, whose CA was hashed."""
    world = world_with(tmp_path, MODES["ca_file"], federation)
    swap_after_check(monkeypatch, tmp_path / CA_NAME, stranger.ca.read_bytes())
    sbx = start(world, "via-gate")
    assert (tmp_path / CA_NAME).read_bytes() == stranger.ca.read_bytes()  # swapped once checked
    statuses = {}
    try:
        for name, server in (("impostor", stranger), ("federation", federation)):
            with TlsServer(server) as upstream, gate_as_started(sbx, world, upstream, run_ca, tmp_path / name) as port:
                statuses[name] = (read_through(port, run_ca), len(upstream.seen))
    finally:
        sbx.stop()
    assert statuses == {"impostor": (502, 0), "federation": (200, 1)}


# ---------------------------------------------------------------------------------------------------- oneclient


@pytest.mark.parametrize("label", CASES)
def test_o05_oneclient_is_given_the_tls_setting_and_the_bundle_read_only(tmp_path, federation, stranger, label):
    """O05: the sidecar's oneclient verifies by default; with ca_file, against the run's read-only copy of exactly the
    configured bundle, an unrelated CA's included, named to it by the OpenSSL setting SSL_CERT_FILE, with verification
    still on; and it runs `--insecure` only with insecure_tls."""
    tls, ca = CASES[label]
    bundle = {"federation": federation, "stranger": stranger}[ca].ca.read_bytes()
    world = world_with(tmp_path, tls, {"federation": federation, "stranger": stranger}[ca])
    sbx = start(world, "mounted")
    sidecar = world.host.containers[sbx.names.sidecar]
    command, mounts = sidecar.command, mounts_of(sidecar)
    binary = command.index(C.ONECLIENT_BIN)
    settings = command[command.index(C.SIDECAR_SHELL_NAME) + 1 : binary]  # what `env` sets and unsets for oneclient
    copy = the_run_copy(sbx, mounts[C.SIDECAR_CA_PATH][0]) if C.SIDECAR_CA_PATH in mounts else None
    sbx.stop()
    assert settings[:2] == ["-u", "ONECLIENT_INSECURE"]  # the image's own skip-verification flag is always removed
    if tls and "ca_file" in tls:
        assert settings[2:] == [f"SSL_CERT_FILE={C.SIDECAR_CA_PATH}"]
        assert mounts[C.SIDECAR_CA_PATH][1] == "ro"
        assert copy == bundle
    else:
        assert settings[2:] == []
        assert C.SIDECAR_CA_PATH not in mounts
    assert ("--insecure" in command) is (label == "insecure_tls")
    assert str((tmp_path / CA_NAME).resolve()) not in json.dumps(sidecar.options, default=str) + " ".join(command)


def test_o05_oneclient_is_given_the_verified_bytes_and_never_a_bundle_swapped_after_the_check(
    tmp_path, monkeypatch, federation, stranger
):
    """O05: the bundle swapped for another CA's right after its check is not what oneclient is given: its mount holds
    the bytes that were hashed."""
    world = world_with(tmp_path, MODES["ca_file"], federation)
    swap_after_check(monkeypatch, tmp_path / CA_NAME, stranger.ca.read_bytes())
    sbx = start(world, "mounted")
    sidecar = world.host.containers[sbx.names.sidecar]
    given = the_run_copy(sbx, mounts_of(sidecar)[C.SIDECAR_CA_PATH][0])
    sbx.stop()
    assert (tmp_path / CA_NAME).read_bytes() == stranger.ca.read_bytes()  # swapped once checked
    assert given == federation.ca.read_bytes()
    assert hashlib.sha256(given).hexdigest() == world.config.tls.ca_sha256


# ---------------------------------------------------------------------------------------------------- both


def test_o05_a_bundle_replaced_since_the_load_refuses_the_start_before_anything_exists(tmp_path, federation):
    """O05: a container is given the bundle only while it is the one the record names; a replaced one starts nothing."""
    world = world_with(tmp_path, MODES["ca_file"], federation)
    (tmp_path / CA_NAME).write_bytes(federation.ca.read_bytes() + federation.cert.read_bytes())
    for level in ("via-gate", "mounted"):
        with pytest.raises(StartRefused, match="changed since") as caught:
            start(world, level)
        assert CA_NAME not in str(caught.value)
        assert not world.host.containers
        assert not world.host.networks


RECORDED = {"verify-default": "verify", "ca_file": "ca_file", "insecure_tls": "insecure"}


@pytest.mark.parametrize("label", MODES)
@pytest.mark.parametrize("level", ["via-gate", "mounted", "isolated"])
def test_o12_the_run_record_names_the_tls_mode_and_a_bundle_by_its_sha256_alone(tmp_path, federation, label, level):
    """O12: the sandbox's metadata, the run's record of it, holds the mode on every data path and the bundle by its
    hash, never by its path or a line of it."""
    world = world_with(tmp_path, MODES[label], federation)
    sbx = start(world, level)
    meta = sbx.metadata()
    sbx.stop()
    expected = {"mode": RECORDED[label]}
    if label == "ca_file":
        expected["ca_sha256"] = hashlib.sha256(federation.ca.read_bytes()).hexdigest()
    assert meta["tls"] == expected
    text = json.dumps(meta, default=str)
    assert CA_NAME not in text
    assert str(tmp_path) not in text
    assert not any(line in text for line in federation.ca.read_text().splitlines() if "-----" not in line)


def test_o05_the_runs_copy_of_the_bundle_is_never_kept_with_the_evidence(tmp_path, federation):
    """O05: when collection fails and the run directory is kept as evidence, the run's copy of the CA bundle is
    removed first: the bundle is configuration, not evidence of the run."""

    world = world_with(tmp_path / "kept", MODES["ca_file"], federation)
    sbx = start(world, "via-gate")
    assert any(p.read_bytes() == federation.ca.read_bytes() for p in sbx.dir.glob("*.pem"))
    undo = fail_copies(world.host)
    with contextlib.suppress(Exception):
        sbx.collect(tmp_path / "e", outputs=["out.txt"])
    undo()
    sbx.stop()
    kept = world.s.work_root / "_kept_evidence" / sbx.sid
    assert kept.exists()
    assert not [p for p in kept.rglob("*") if p.is_file() and p.read_bytes() == federation.ca.read_bytes()]
