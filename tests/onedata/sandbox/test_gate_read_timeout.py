"""O85: the gate's upstream read timeout is declared, `[sandbox.gate] upstream_read_timeout_s`, not a constant the agent
cannot see. It decides when an agent's REST read through the gate fails while the platform hangs, so it is validated at
load, written into the gate's configuration on the Docker backend (the Apptainer backend's half is in
`apptainer/test_gate_read_timeout.py`), used by the gate for every upstream read, and recorded in each REST run's
`infrastructure_facts`. A platform that does not declare it gets the constant it always had (14,400 s)."""

import http.client
import json
import socket
import ssl
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from fake_host import FakeHost
from sandbox_support import add_sandbox, make_spec, sandbox_data, start_options

from expcore.config.loading import ConfigError, Section
from expcore_onedata.config import load_platform
from expcore_onedata.sandbox import gate, gatepolicy
from expcore_onedata.sandbox.apptainer.settings import GATE_OWN_KEYS
from expcore_onedata.sandbox.hostops import DockerHost
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.sandbox.sandbox import DockerSandbox
from expcore_onedata.sandbox.settings import load_sandbox
from expcore_onedata.testing import write_platform

KEY = "upstream_read_timeout_s"
FACT = "gate_upstream_read_timeout_s"  # the run's `infrastructure_facts` key
SERVED = "depot.layout.test"  # the name the gate serves and its certificate holds (invented: resolves nowhere)
FILE = "/api/v3/oneprovider/data/f1/content"


def _gated_world(tmp_path, gate_table):
    """The sandbox `world` of the conftest, with `[sandbox.gate]` as `gate_table` (no table when None)."""

    def edit(data):
        add_sandbox(data)
        if gate_table is not None:
            data["sandbox"]["gate"] = gate_table

    config = load_platform(write_platform(tmp_path, edit), require_secrets=False)
    s, host = config.sandbox, FakeHost()
    for ref in (s.agent, s.gate, s.oneclient):
        host.add_image(ref.name, ref.id)
    host.oneclient_images.add(s.oneclient.id)
    return SimpleNamespace(host=host, config=config, s=s, registry=Registry(), tmp_path=tmp_path)


def _start(world, level="via-gate"):
    return DockerSandbox.start(world.host, world.s, make_spec(world, level), **start_options(world))


def _load(tmp_path, **changes):
    """`load_sandbox` over the default Docker `[sandbox]` document with `changes` made to it."""
    document = sandbox_data() | changes
    return load_sandbox(Section(document, tmp_path / "platform.toml", "[sandbox]"), tmp_path)


# ---------------------------------------------------------------------------------------------------- the setting


def test_o85_the_default_is_the_constant_the_gate_always_had():
    """O85: a platform that declares nothing gets 14,400 s, which is the gate's own constant: one number, defined once
    (`gatepolicy`), so the settings' default and the gate's cannot drift."""
    assert gatepolicy.UPSTREAM_READ_TIMEOUT_S == gate.READ_TIMEOUT == 14400


def test_o85_a_platform_without_the_table_has_the_default_and_a_declared_value_is_read_as_a_float(tmp_path):
    """O85: no `[sandbox.gate]` is the default; a declared whole number is one float, so `3600` and `3600.0` are one
    setting everywhere it is carried."""
    assert _load(tmp_path).gate_upstream_read_timeout_s == 14400.0
    for written in (3600, 3600.0):
        loaded = _load(tmp_path, gate={KEY: written})
        assert loaded.gate_upstream_read_timeout_s == 3600.0
        assert isinstance(loaded.gate_upstream_read_timeout_s, float)
    assert _load(tmp_path, gate={}).gate_upstream_read_timeout_s == 14400.0


@pytest.mark.parametrize(
    "bad",
    [0, -5, 0.5, 86401, True, "600", None, [600], float("nan"), float("inf")],
    ids=["zero", "negative", "under-a-second", "over-a-day", "bool", "string", "none", "list", "nan", "inf"],
)
def test_o85_a_bad_value_is_refused_at_load_naming_the_key_and_not_the_value(tmp_path, bad):
    """O85: a timeout that is not a number of seconds from 1 to 86,400 is refused when the platform loads, by the key's
    name; the message never holds the value."""
    with pytest.raises(ConfigError, match=KEY):
        _load(tmp_path, gate={KEY: bad})


@pytest.mark.parametrize("bad", [86401, -73, 0.25])
def test_o85_the_refusal_never_holds_the_value_it_refuses(tmp_path, bad):
    """O85, O12: a configured value is never echoed: the message names the key and the bounds, not what was written."""
    with pytest.raises(ConfigError, match=KEY) as refused:
        _load(tmp_path, gate={KEY: bad})
    assert str(bad) not in str(refused.value)


def test_o85_a_table_with_another_key_is_refused_and_so_is_one_without_a_rest_data_path(tmp_path):
    """O85: `[sandbox.gate]` takes this key alone, and no gate runs without a REST level, so a declaration there
    would be a setting nothing reads: refused, never ignored."""
    with pytest.raises(ConfigError, match="not_a_gate_key"):
        _load(tmp_path, gate={"not_a_gate_key": 1})
    only_mount = {"mounted": "mount", "isolated": "none"}
    with pytest.raises(ConfigError, match=KEY):
        _load(tmp_path, gate={KEY: 600}, data_paths=only_mount)
    assert _load(tmp_path, data_paths=only_mount).gate_upstream_read_timeout_s == 14400.0


def test_o85_the_sandbox_owns_the_gate_key_so_a_gate_option_cannot_shadow_it():
    """O85: on the Apptainer backend `gate_options` may not set the key the declared value is written to."""
    assert KEY in GATE_OWN_KEYS


# ---------------------------------------------------------------------------------------------------- the Docker gate


@pytest.mark.parametrize(
    ("table", "effective"), [(None, 14400.0), ({KEY: 14400}, 14400.0), ({KEY: 900}, 900.0), ({KEY: 2.5}, 2.5)]
)
def test_o85_the_docker_gate_is_given_the_effective_value_and_the_run_records_it(tmp_path, table, effective):
    """O85: the gate's configuration file carries the effective value (the default when none is declared), and the
    REST run's `infrastructure_facts` says which value its gate was given; a run with no gate has no such fact."""
    world = _gated_world(tmp_path, table)
    sbx = _start(world)
    try:
        written = json.loads((sbx.dir / "gate.json").read_text(encoding="utf-8"))
        assert written[KEY] == effective
        assert sbx.metadata()["infrastructure_facts"][FACT] == effective
    finally:
        sbx.stop()
    isolated = _start(world, "isolated")
    try:
        assert FACT not in isolated.metadata().get("infrastructure_facts", {})
    finally:
        isolated.stop()


# ---------------------------------------------------------------------------------------------------- the gate itself


class _Stalled:
    """A plain-HTTP upstream on the loopback that accepts connections and never answers."""

    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.held = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            self.held.append(connection)

    def close(self):
        self.listener.close()
        for connection in self.held:
            connection.close()


@pytest.fixture
def stalled():
    upstream = _Stalled()
    yield upstream
    upstream.close()


def _start_gate(tmp_path, upstream, **config):
    """The gate in-process, its one host a plain-HTTP upstream; returns the server and the directory of its TLS."""
    tls = tmp_path / "gate-tls"
    DockerHost().make_ca(tls, [SERVED])
    log = tmp_path / "requests.jsonl"
    log.touch()
    gate.CFG.clear()
    gate._CTX.clear()
    gate.CFG.update(
        run_key="run-test-1", token="tok-gate-0123456789", log=str(log), tls_dir=str(tls), verify_ssl=True,
        hosts={SERVED: {"role": "oneprovider", "addr": "127.0.0.1", "port": upstream.port, "tls": False}},
        listen_host="127.0.0.1", listen_port=0, **config,
    )  # fmt: skip
    httpd = gate.GateServer(("127.0.0.1", 0), gate.Handler)
    httpd.socket = gate._tls_context().wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, tls, log


@pytest.fixture
def clean_gate():
    yield
    gate.CFG.clear()
    gate._CTX.clear()


def test_o85_the_gate_waits_for_an_upstream_answer_as_long_as_it_was_declared(tmp_path, stalled, clean_gate):
    """O85: with `upstream_read_timeout_s` in its configuration the gate gives a hung upstream read up at that time:
    the agent gets a 502 `badGateway` and the request's line says the gate's own timer ended it, as it always does
    for this timeout."""
    httpd, tls, log = _start_gate(tmp_path, stalled, **{KEY: 0.5})
    try:
        context = ssl.create_default_context(cafile=str(tls / "ca.pem"))
        raw = socket.create_connection(("127.0.0.1", httpd.server_port), timeout=20)
        conn = http.client.HTTPConnection(SERVED, timeout=20)
        conn.sock = context.wrap_socket(raw, server_hostname=SERVED)
        began = time.monotonic()
        conn.request("GET", FILE)
        answer = conn.getresponse()
        answer.read()
        waited = time.monotonic() - began
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert answer.status == 502
    assert 0.4 <= waited < 15  # the declared half second, plus the gate's own work; never the default's four hours
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    failed = [line for line in lines if line.get("status") == 502]
    assert [line["error_origin"] for line in failed] == ["gate"]


def test_o85_without_the_key_the_gate_reads_with_the_constant_it_always_had(tmp_path, stalled, clean_gate):
    """O85: a configuration that has no `upstream_read_timeout_s` (an older one, or the HPC rehearsal's hand-written
    one) gives every upstream socket `READ_TIMEOUT`; one that names it gives that value."""
    host = {"role": "oneprovider", "addr": "127.0.0.1", "port": stalled.port, "tls": False}
    gate.CFG.clear()
    connection = gate._upstream_conn(host)
    try:
        assert connection.sock.gettimeout() == gate.READ_TIMEOUT == 14400
    finally:
        connection.close()
    gate.CFG[KEY] = 7.5
    connection = gate._upstream_conn(host)
    try:
        assert connection.sock.gettimeout() == 7.5
    finally:
        connection.close()


@pytest.mark.parametrize(
    "bad",
    [0, -1, "600", True, None, float("nan"), float("inf")],
    ids=["zero", "negative", "string", "bool", "none", "nan", "inf"],
)
def test_o85_a_gate_given_a_timeout_that_is_no_positive_number_refuses_to_start(tmp_path, monkeypatch, clean_gate, bad):
    """O85: the gate never guesses at its timeout: a configuration whose value is no positive finite number ends the
    start, before any listener, as a listing mode it does not know does."""
    token = tmp_path / "token"
    token.write_text("tok-gate-0123456789", encoding="utf-8")
    config = tmp_path / "gate.json"
    config.write_text(json.dumps({"token_file": str(token), KEY: bad}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["gate.py", str(config)])
    gate.CFG.clear()
    with pytest.raises(SystemExit, match="read timeout"):
        gate.main()
