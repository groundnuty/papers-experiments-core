"""O69: on the Apptainer backend the agent's cell sees the names the layout declares, on 443, never a node's name or its
port: the gate serves and certifies `[sandbox.federation]`'s names and dials the URLs' hosts and ports behind them."""

import dataclasses
import json
from pathlib import Path

import pytest
from apptainer_support import PROVIDER, ZONE, make_settings, make_spec, start
from fake_node import FakeNode

from expcore_onedata.sandbox.api import Endpoint, SandboxError, StartRefused
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.apptainer import gaterun
from expcore_onedata.sandbox.settings import FederationNetwork

DECLARED_ZONE, DECLARED_PROVIDER = "zone.layout.test", "alpha.layout.test"  # invented, as a layout declares them
NAMES = FederationNetwork(None, DECLARED_PROVIDER, DECLARED_ZONE)
URL_HOSTS = (ZONE.host, PROVIDER.host)  # the URLs' hosts, standing in for the nodes' names: never the cell's to see
URL_PORT = str(ZONE.port)  # 8443, the nodes' public port: never the cell's either


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


@pytest.fixture
def named(tmp_path):
    return dataclasses.replace(make_settings(tmp_path), federation=NAMES)


def _bound_text(cell) -> str:
    """The text of every file bound into a cell, each bound file and every file under each bound directory."""
    parts = []
    for source, _ in cell.binds.values():
        path = Path(source)
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        parts += [p.read_bytes().decode("utf-8", "replace") for p in files]
    return "\n".join(parts)


def _bound(cell, target: str) -> str:
    return Path(cell.binds[target][0]).read_text(encoding="utf-8")


def test_o69_the_cell_is_given_the_declared_names_on_the_loopback_and_never_a_url_s_host_or_port(node, named):
    """O69: `ONEDATA_ONEZONE_HOST`, `/etc/hosts` and `/etc/expcore/gate.json` hold the declared names, the names
    resolve to the loopback, where the cell dials them on 443, and no exec's argv, environment or bound file holds a
    URL's host, nor its environment, hosts or gate file the URLs' port."""
    box = start(node, named)
    try:
        box.exec("print(1)", 30)
        cell = node.execs[-1]
        assert cell.env["ONEDATA_ONEZONE_HOST"] == DECLARED_ZONE
        assert _bound(cell, "/etc/hosts") == AV.hosts_text([DECLARED_ZONE, DECLARED_PROVIDER])
        assert f"127.0.0.1 {DECLARED_ZONE} {DECLARED_PROVIDER}\n" in _bound(cell, "/etc/hosts")
        gate_file = json.loads(_bound(cell, AV.GATE_INFO))
        assert gate_file == {"address": "127.0.0.1", "port": node.gates[-1].config["listen_port"],
                             "names": [DECLARED_ZONE, DECLARED_PROVIDER]}  # fmt: skip
        assert node.execs  # the canary, the read-only probe and the agent's cell
        for done in node.execs:
            seen = "\n".join([" ".join(done.argv), json.dumps(done.env), _bound_text(done)])
            for host in URL_HOSTS:
                assert host not in seen
            assert URL_PORT not in json.dumps(done.env)
            for target in ("/etc/hosts", AV.GATE_INFO):
                assert target not in done.binds or URL_PORT not in _bound(done, target)
    finally:
        box.stop()


def test_o69_the_gate_serves_and_certifies_the_declared_names_and_dials_the_urls_behind_them(node, named):
    """O69: the gate's configuration serves the declared names, each dialling its URL's host and port as `[tls]` says;
    the run's CA issues leaves for the declared names alone; the run's metadata says the names were declared, and
    never which."""
    box = start(node, named)
    try:
        hosts = node.gates[-1].config["hosts"]
        assert hosts == {
            DECLARED_ZONE: {"role": "onezone", "addr": ZONE.host, "port": ZONE.port, "tls": True},
            DECLARED_PROVIDER: {"role": "oneprovider", "addr": PROVIDER.host, "port": PROVIDER.port, "tls": True},
        }
        leaves = sorted(p.name for p in (box.dir / "tls").glob("*.pem") if p.name != "ca.pem")
        assert leaves == sorted([f"{DECLARED_ZONE}.pem", f"{DECLARED_PROVIDER}.pem"])
        gate = box.metadata()["gate"]
        assert gate["names"] == "declared"
        assert DECLARED_ZONE not in json.dumps(gate)
    finally:
        box.stop()


def test_o69_the_run_s_egress_declaration_names_the_declared_hosts(node, named):
    """O69: the names a URL in the agent's code may name, as its record keeps them, are the declared ones."""
    box = start(node, named)
    try:
        declared = box.egress_declaration()
        assert declared.hosts == tuple(sorted([DECLARED_ZONE, DECLARED_PROVIDER]))
        assert not set(URL_HOSTS) & set(declared.hosts)
    finally:
        box.stop()


def test_o69_without_the_table_the_cell_is_given_the_urls_hosts_as_before(node, tmp_path):
    """O69: a platform that declares no names serves the agent the URLs' hosts, as it always has."""
    box = start(node, make_settings(tmp_path))
    try:
        box.exec("print(1)", 30)
        assert node.execs[-1].env["ONEDATA_ONEZONE_HOST"] == ZONE.host
        assert set(node.gates[-1].config["hosts"]) == {ZONE.host, PROVIDER.host}
        assert box.metadata()["gate"]["names"] == "urls"
    finally:
        box.stop()


def test_o69_declared_names_without_the_zone_s_refuse_a_rest_start_before_anything_exists(node, tmp_path):
    """O69: the gate serves the zone too, so names that lack the zone's (settings made in code) refuse the start, and
    no gate is given the URLs' hosts in their place."""
    settings = dataclasses.replace(make_settings(tmp_path), federation=FederationNetwork(None, DECLARED_PROVIDER))
    with pytest.raises(StartRefused, match="names no zone"):
        start(node, settings)
    assert not node.spawned
    assert not node.gates


def test_o69_declared_names_in_capitals_are_served_in_lower_case(node, tmp_path):
    """O69: the gate matches Host and SNI in lower case, so names given in capitals (settings made in code) are served,
    certified and given to the cell in lower case."""
    capitals = FederationNetwork(None, DECLARED_PROVIDER.upper(), DECLARED_ZONE.title())
    box = start(node, dataclasses.replace(make_settings(tmp_path), federation=capitals))
    try:
        box.exec("print(1)", 30)
        assert set(node.gates[-1].config["hosts"]) == {DECLARED_ZONE, DECLARED_PROVIDER}
        assert node.execs[-1].env["ONEDATA_ONEZONE_HOST"] == DECLARED_ZONE
    finally:
        box.stop()


def test_o69_without_the_table_a_zone_on_its_provider_s_host_starts_as_before(node, tmp_path):
    """O69: with no names declared, a zone whose URL has the provider's host on another port is given to the cell by
    its URL's host, as before; the change of names never makes such a platform refuse its start."""
    spec = dataclasses.replace(make_spec(), zone=Endpoint(PROVIDER.host, 9443, True))
    box = start(node, make_settings(tmp_path), spec)
    try:
        box.exec("print(1)", 30)
        assert node.execs[-1].env["ONEDATA_ONEZONE_HOST"] == PROVIDER.host
    finally:
        box.stop()


def test_o69_one_name_for_the_zone_and_the_provider_is_refused():
    """O69: the gate serves one host by a name, so two roles under one declared name never collapse into one."""
    with pytest.raises(SandboxError, match="one name"):
        gaterun.hosts(ZONE, PROVIDER, (DECLARED_ZONE, DECLARED_ZONE.upper()))
    assert list(gaterun.hosts(ZONE, PROVIDER, (DECLARED_ZONE, DECLARED_PROVIDER))) == [DECLARED_ZONE, DECLARED_PROVIDER]
