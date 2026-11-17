"""O73: the gate's scan for node names holds the agent node's own host name too, in its one-label and fully qualified
forms, read from the node the run is on and never written as a literal, so a leak of it through an answer is counted. It
never holds the declared cell name (`sandbox`), which is the allowed one, nor `localhost`, nor a name the federation
declares: counting those would be false hits. A run with no names to scan scans nothing."""

import json

import pytest
from apptainer_support import make_settings, make_spec, start
from fake_node import OWN_FQDN, OWN_HOST, FakeNode

from expcore_onedata.sandbox import cellhost, gate
from expcore_onedata.sandbox.api import DataPathMode, SandboxSpec
from expcore_onedata.sandbox.apptainer import loopback
from expcore_onedata.sandbox.apptainer.node import LocalNode
from expcore_onedata.tokens import Secret

NODE_A = "cn-r3c07-n12.cluster.example.org"
SERVICES = (NODE_A, "cn-r3c07-n12", "198.51.100.21")


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


def _spec(names=SERVICES) -> SandboxSpec:
    base = make_spec("via-gate")
    return SandboxSpec(
        run_key=base.run_key, mode=DataPathMode.REST, token=Secret("tok-0123456789"), zone=base.zone,
        provider=base.provider, node_names=tuple(names),
    )  # fmt: skip


def _scanned(node: FakeNode, tmp_path) -> list[str]:
    """The list the sandbox gave the gate of a run on `node`."""
    box = start(node, make_settings(tmp_path), _spec())
    try:
        return node.gates[-1].config["scan_names"]
    finally:
        box.stop()


def test_o73_an_fqdn_gives_its_one_label_form_and_a_one_label_name_only_itself():
    """O73: a fully qualified name is scanned whole and by its first label, since a label followed by a dot is no match
    of the whole; a name of one label, which holds no domain, is scanned as it is."""
    assert cellhost.scan_names((), ("CN-Own-N01.Cluster.Example.Invalid",)) == (
        "cn-own-n01.cluster.example.invalid",
        "cn-own-n01",
    )
    assert cellhost.scan_names((), ("cn-own-n01",)) == ("cn-own-n01",)


def test_o73_the_federation_s_names_stay_as_they_are_and_the_node_s_follow_once():
    """O73: the services' names come first and unchanged, the agent node's after them, none twice in any case."""
    given = ("198.51.100.7", "CN-A.example.invalid")
    found = cellhost.scan_names(given, ("cn-own-n01", "cn-a.example.invalid", "CN-OWN-N01.cluster.example.invalid"))
    assert found == (*given, "cn-own-n01", "cn-own-n01.cluster.example.invalid", "cn-a")


@pytest.mark.parametrize(
    ("own", "kept"),
    [
        (("sandbox",), ()),
        (("sandbox.cluster.example.invalid",), ("sandbox.cluster.example.invalid",)),
        (("localhost", "localhost.localdomain"), ()),
        (("", "  "), ()),
        (("198.51.100.5",), ("198.51.100.5",)),
    ],
)
def test_o73_the_declared_name_localhost_and_blanks_are_never_scanned_and_an_address_is_not_split(own, kept):
    """O73: `sandbox` is the allowed name and `localhost` is nobody's, so each would count false hits; an address is
    scanned whole, not by its first group."""
    assert cellhost.scan_names((), own) == kept


def test_o73_a_name_the_federation_declares_is_never_scanned():
    """O73: a node named as one of the federation's declared names (what the agent is meant to see) is no leak."""
    found = cellhost.scan_names((), ("alpha.layout.test", "zone"), declared=("alpha.layout.test", "ZONE"))
    assert found == ()


def test_o73_the_local_node_reads_its_host_name_and_its_fqdn_from_the_system(monkeypatch):
    """O73: the node is asked for its own names, both as the system says them; a lookup that fails is skipped."""
    monkeypatch.setattr(loopback.socket, "gethostname", lambda: "CN-Own-N01")
    monkeypatch.setattr(loopback.socket, "getfqdn", lambda: "cn-own-n01.cluster.example.invalid")
    assert LocalNode().own_names() == ("CN-Own-N01", "cn-own-n01.cluster.example.invalid")

    def broken():
        raise OSError("no resolver")

    monkeypatch.setattr(loopback.socket, "getfqdn", broken)
    assert LocalNode().own_names() == ("CN-Own-N01",)


def test_o73_the_gate_scans_for_the_agent_node_after_the_federation_s_nodes(node, tmp_path):
    """O73: the sandbox asks its node for its names and gives the gate the federation's list and then the node's, in
    both forms; the gate's configuration is the only place they go."""
    assert _scanned(node, tmp_path) == [*SERVICES, OWN_HOST, OWN_FQDN]


def test_o73_the_cell_name_is_never_in_the_gate_s_list_even_for_a_node_that_is_named_so(node, tmp_path):
    """O73: a node whose own host name is the declared one adds its fully qualified form alone."""
    node.own_hosts = ("sandbox", "sandbox.cluster.example.invalid")
    found = _scanned(node, tmp_path)
    assert found == [*SERVICES, "sandbox.cluster.example.invalid"]
    assert "sandbox" not in found


def test_o73_a_name_the_run_serves_is_not_scanned_for_even_if_the_node_has_it(node, tmp_path):
    """O73: the zone's and the provider's declared names are what the agent sees; a node of the same name is no leak."""
    base = make_spec("via-gate")
    node.own_hosts = (base.provider.host,)
    assert _scanned(node, tmp_path) == list(SERVICES)


def test_o73_a_run_with_no_names_to_scan_asks_the_node_for_none(node, tmp_path):
    """O73: a federation whose names are the real ones (Compose's) has no list and no scan; the node is not asked."""
    node.own_hosts = RuntimeError("never asked")
    box = start(node, make_settings(tmp_path), make_spec("via-gate"))
    try:
        assert "scan_names" not in node.gates[-1].config
    finally:
        box.stop()


def test_o73_an_answer_with_the_agent_node_s_name_is_counted_and_one_with_the_cell_name_is_not(node, tmp_path):
    """O73: the list the sandbox gave the gate, run through the gate's own scan: the agent node's real name, in either
    form and any case, is one hit wherever an answer carries it; the declared `sandbox` is none; a service's node is
    still counted."""
    names = _scanned(node, tmp_path)
    gate.CFG.clear()
    gate.CFG["scan_names"] = names
    try:
        for text in (OWN_HOST, OWN_FQDN, OWN_FQDN.upper(), f"https://{OWN_FQDN}:8443/x"):
            assert gate._node_name_hits(json.dumps({"domain": text}).encode()) == 1, text
        assert gate._node_name_hits(json.dumps({"host": "sandbox"}).encode()) == 0
        assert gate._node_name_hits(json.dumps({"host": "sandbox", "domain": OWN_FQDN}).encode()) == 1
        assert gate._node_name_hits(json.dumps({"host": NODE_A}).encode()) == 1
        assert gate._redact_node_names(f"GET /{OWN_FQDN}") == f"GET /{gate.NODE_MARK}"
        assert gate._redact_node_names("GET /sandbox") == "GET /sandbox"
    finally:
        gate.CFG.clear()


def test_o73_the_node_s_names_are_in_the_spec_of_no_run_and_in_no_record(node, tmp_path):
    """O73: nothing a record or the spec's repr holds carries the agent node's name: it is read at the start and goes to
    the gate's configuration alone."""
    spec = _spec()
    box = start(node, make_settings(tmp_path), spec)
    try:
        box.exec("print(1)", 30)
        meta = json.dumps(box.metadata())
        assert OWN_HOST not in meta
        assert OWN_HOST not in repr(spec)
    finally:
        box.stop()
