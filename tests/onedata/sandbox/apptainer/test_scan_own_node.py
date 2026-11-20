"""O73: the gate's scan for node names holds the agent node's own host name too, in its one-label and fully qualified
forms, read from the node the run is on and never written as a literal, so a leak of it through an answer is counted. It
never holds the declared cell name (`sandbox`), which is the allowed one, nor `localhost`, nor a name the federation
declares: counting those would be false hits. A run with no names to scan scans nothing."""

import json
import threading
import time

import pytest
from apptainer_support import make_settings, make_spec, start
from fake_node import OWN_FQDN, OWN_HOST, FakeNode

from expcore_onedata.sandbox import cellhost, gate
from expcore_onedata.sandbox.api import DataPathMode, SandboxSpec, StartRefused
from expcore_onedata.sandbox.apptainer import loopback
from expcore_onedata.sandbox.apptainer.node import LocalNode
from expcore_onedata.sandbox.cellhost import OwnNames
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
    assert found == (*given, "cn-own-n01", "cn-a", "cn-own-n01.cluster.example.invalid")


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


def _system(monkeypatch, host, answer):
    """The node's own system: `gethostname` says `host`; `gethostbyaddr`, asked about that host name, answers the
    `(primary name, aliases, addresses)` of `answer`, or raises it."""
    monkeypatch.setattr(loopback.socket, "gethostname", lambda: host)

    def lookup(name):
        assert name == host, "the lookup is of the node's own host name"
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(loopback.socket, "gethostbyaddr", lookup)


def test_o73_the_local_node_reads_its_host_name_and_every_name_its_resolver_gives(monkeypatch):
    """O73: the node is asked for its own names: its host name, then the resolver's primary name and each alias, as the
    system says them, none twice in any case. A node of several dotted names has each one scanned (`getfqdn` gave only
    the first)."""
    _system(
        monkeypatch,
        "CN-Own-N01",
        ("cn-own-n01.cluster.example.invalid", ["cn-own-n01.ib.example.invalid", "cn-own-n01", ""], ["198.51.100.9"]),
    )
    assert LocalNode().own_names() == OwnNames(
        ("CN-Own-N01", "cn-own-n01.cluster.example.invalid", "cn-own-n01.ib.example.invalid")
    )


@pytest.mark.parametrize("error", [OSError("no resolver"), TimeoutError("slow"), ValueError("odd")])
def test_o73_a_resolver_that_fails_is_unread_by_its_reason_and_the_host_name_stands_alone(monkeypatch, error):
    """O73: whatever a lookup raised, the reason recorded is `fqdn_failed`, never the exception's text."""
    _system(monkeypatch, "CN-Own-N01", error)
    assert LocalNode().own_names() == OwnNames(("CN-Own-N01",), ("fqdn_failed",))
    _system(monkeypatch, "CN-Own-N01", loopback.socket.herror(1, "Unknown host"))
    assert LocalNode().own_names() == OwnNames(("CN-Own-N01",), ("fqdn_failed",))


@pytest.mark.parametrize(
    ("answer", "names"),
    [
        (("cn-own-n01", [], ["198.51.100.9"]), ("cn-own-n01",)),
        (("cn-own-n01", ["cn-own-n01-ib"], []), ("cn-own-n01", "cn-own-n01-ib")),
        (("198.51.100.9", [], []), ("cn-own-n01", "198.51.100.9")),
    ],
)
def test_o73_the_names_of_an_answer_with_no_domain_are_read_as_they_are(monkeypatch, answer, names):
    """O73: a resolver that gives the host name no dotted name (a hosts file with the short name alone) answers what it
    has, without an error, as `getfqdn` did; that the scan then holds a one-label name is said by the run's facts
    (`fqdn_unqualified`, below), where the names the scan keeps are known."""
    _system(monkeypatch, "cn-own-n01", answer)
    assert LocalNode().own_names() == OwnNames(names)


def test_o73_a_host_name_that_is_already_qualified_leaves_nothing_unread(monkeypatch):
    """O73: a kernel host name that holds its domain is a qualified name: nothing is unread and the name stands once."""
    name = "cn-own-n01.cluster.example.invalid"
    _system(monkeypatch, name, (name, [], []))
    assert LocalNode().own_names() == OwnNames((name,))


@pytest.mark.parametrize("host", ["", None])
def test_o73_a_host_name_that_cannot_be_read_is_unread_and_the_resolver_is_not_asked(monkeypatch, host):
    """O73: with no host name there is nothing to look up: both forms are unread, by reason, and no lookup is made."""

    def broken():
        if host is None:
            raise OSError("no host name")
        return host

    monkeypatch.setattr(loopback.socket, "gethostname", broken)

    def never(_name):
        raise AssertionError("the resolver was asked with no host name")

    monkeypatch.setattr(loopback.socket, "gethostbyaddr", never)
    assert LocalNode().own_names() == OwnNames((), ("host_name_failed", "fqdn_failed"))


def test_o73_the_resolver_s_bound_is_short_and_finite():
    """O73: the production bound on the lookup is a positive number of seconds that a start can afford."""
    assert 0 < loopback.FQDN_TIMEOUT_S <= 10


def test_o73_a_resolver_that_hangs_costs_the_bound_and_leaves_the_host_name_alone(monkeypatch):
    """O73: the lookup runs in a thread of its own with a short bound: a resolver that never answers delays a start by
    that bound and no more, the host name is scanned alone, and the reason, not the name, says what was not read."""
    release = threading.Event()
    monkeypatch.setattr(loopback.socket, "gethostname", lambda: "cn-own-n01")
    monkeypatch.setattr(
        loopback.socket, "gethostbyaddr", lambda _name: release.wait(30) and ("never.answered.invalid", [], [])
    )
    monkeypatch.setattr(loopback, "FQDN_TIMEOUT_S", 0.2)
    try:
        began = time.monotonic()
        found = LocalNode().own_names()
        assert time.monotonic() - began < 5
        assert found == OwnNames(("cn-own-n01",), ("fqdn_timeout",))
    finally:
        release.set()


def test_o73_a_lookup_that_answers_in_time_is_read_whole_and_a_thread_that_ends_is_not_left_behind(monkeypatch):
    """O73: the bound costs a quick lookup nothing: its answer is read, nothing is unread, no thread of ours stays. The
    threads are compared by identity and the lookup's own name, never by a total count: any other thread of the process
    may start or end meanwhile (a count once went down by one on CI)."""
    _system(monkeypatch, "cn-own-n01", ("cn-own-n01.cluster.example.invalid", [], []))
    before = set(threading.enumerate())  # a lookup thread an earlier test left ending is in it, and not this call's
    assert loopback.own_names(timeout_s=5) == OwnNames(("cn-own-n01", "cn-own-n01.cluster.example.invalid"))
    left = [t for t in threading.enumerate() if t not in before and t.name == "own-fqdn-lookup"]
    assert left == []  # it answered in time, so its thread ended before the call returned: none is left behind


@pytest.mark.parametrize(
    ("name", "qualified"),
    [("cn-own-n01.cluster.example.invalid", True), ("cn-own-n01", False), ("", False), ("198.51.100.9", False),
     ("2001:db8::1", False)],
)  # fmt: skip
def test_o73_a_qualified_name_holds_a_domain_and_is_no_address(name, qualified):
    """O73: what makes a node's name read as a fully qualified one."""
    assert cellhost.qualified(name) is qualified


@pytest.mark.parametrize("name", ["localhost4", "localhost4.localdomain4", "localhost6.localdomain6", "ip6-localhost"])
def test_o73_the_loopback_aliases_of_a_hosts_file_are_nobody_s_name(name):
    """O73: the aliases a stock hosts file gives the loopback addresses would count false hits, as `localhost` would."""
    assert cellhost.scan_names((), (name,)) == ()


def test_o73_the_gate_scans_for_the_agent_node_after_the_federation_s_nodes(node, tmp_path):
    """O73: the sandbox asks its node for its names and gives the gate the federation's list and then the node's, in
    both forms; the gate's configuration is the only place they go."""
    assert _scanned(node, tmp_path) == [*SERVICES, OWN_HOST, OWN_FQDN]


def test_o73_a_name_the_run_serves_is_not_scanned_for_even_if_the_node_has_it(node, tmp_path):
    """O73: the zone's and the provider's declared names are what the agent sees; a node of the same name is no leak."""
    base = make_spec("via-gate")
    node.own_hosts = (base.provider.host,)
    assert _scanned(node, tmp_path) == list(SERVICES)


def test_o73_a_form_the_node_could_not_read_is_in_the_run_s_facts_by_its_reason_alone(node, tmp_path):
    """O73: a scanning run records how many forms of the agent node's names it scanned and the reasons any could not be
    read (`own_node_scan`), never a name; read whole, the reasons are none."""
    box = start(node, make_settings(tmp_path), _spec())
    try:
        assert box.metadata()["infrastructure_facts"]["own_node_scan"] == {"version": 1, "forms": 2, "unread": []}
    finally:
        box.stop()
    node.own_hosts, node.own_unread = (OWN_HOST,), ("fqdn_timeout",)
    box = start(node, make_settings(tmp_path), _spec())
    try:
        facts = box.metadata()["infrastructure_facts"]
        assert facts["own_node_scan"] == {"version": 1, "forms": 1, "unread": ["fqdn_timeout"]}
        assert OWN_HOST not in json.dumps(box.metadata())
        box._meta["infrastructure_facts"] = {"backend": "apptainer"}  # a record of the facts made again later
        assert box.metadata()["infrastructure_facts"]["own_node_scan"]["unread"] == ["fqdn_timeout"]
    finally:
        box.stop()


def test_o73_a_run_with_no_names_to_scan_asks_the_node_for_none(node, tmp_path):
    """O73: a federation whose names are the real ones (Compose's) has no list and no scan; the node is not asked."""
    node.own_hosts = RuntimeError("never asked")
    box = start(node, make_settings(tmp_path), make_spec("via-gate"))
    try:
        assert "scan_names" not in node.gates[-1].config
        assert "own_node_scan" not in box.metadata()["infrastructure_facts"]
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


def test_o73_the_node_is_asked_once_for_its_names_however_the_start_goes(node, tmp_path):
    """O73: one lookup serves the refusal check and the gate's list (a lookup is bounded, never repeated)."""
    _scanned(node, tmp_path)
    assert node.own_asked == 1


@pytest.mark.parametrize(
    ("own", "unread"),
    [
        ((OWN_HOST,), ["fqdn_unqualified"]),
        ((OWN_HOST, "198.51.100.9"), ["fqdn_unqualified"]),
        (("localhost", "localhost.localdomain", "localhost4.localdomain4", OWN_HOST), ["fqdn_unqualified"]),
        ((OWN_HOST, "ip6-localhost", "localhost6.localdomain6"), ["fqdn_unqualified"]),
        ((OWN_HOST, make_spec("via-gate").provider.host), ["fqdn_unqualified"]),
        ((OWN_HOST, OWN_FQDN), []),
        ((OWN_FQDN,), []),
        ((OWN_HOST, "localhost.localdomain", OWN_FQDN), []),
    ],
)
def test_o73_the_facts_say_when_no_name_the_scan_keeps_has_a_domain(node, tmp_path, own, unread):
    """O73: `fqdn_unqualified` is decided over the names the scan keeps: a loopback alias with a domain, a name the
    federation declares or an address is no qualified name of the node, so a run whose only dotted name is one of those
    scans a one-label form that never matches the node's name followed by its domain, and the facts say so."""
    node.own_hosts = own
    box = start(node, make_settings(tmp_path), _spec())
    try:
        assert box.metadata()["infrastructure_facts"]["own_node_scan"]["unread"] == unread
    finally:
        box.stop()


def test_o73_a_reason_the_node_gave_is_not_joined_by_another(node, tmp_path):
    """O73: a lookup that timed out says `fqdn_timeout` alone: the host name's missing domain is that reason's."""
    node.own_hosts, node.own_unread = (OWN_HOST,), ("fqdn_timeout",)
    box = start(node, make_settings(tmp_path), _spec())
    try:
        assert box.metadata()["infrastructure_facts"]["own_node_scan"]["unread"] == ["fqdn_timeout"]
    finally:
        box.stop()


def test_o73_a_node_whose_resolver_gives_no_qualified_name_says_so_in_the_run_s_facts(node, tmp_path, monkeypatch):
    """O73: end to end, a node that reads its own system and finds no dotted name scans its one label form and records
    `fqdn_unqualified`: the gate's miss of `<label>.<domain>` is no longer a silent one."""
    _system(monkeypatch, "cn-own-n01", ("cn-own-n01", [], []))
    node.own_names = loopback.own_names  # this node reads the system, as `LocalNode` does
    box = start(node, make_settings(tmp_path), _spec())
    try:
        assert node.gates[-1].config["scan_names"] == [*SERVICES, "cn-own-n01"]
        own = box.metadata()["infrastructure_facts"]["own_node_scan"]
        assert own == {"version": 1, "forms": 1, "unread": ["fqdn_unqualified"]}
    finally:
        box.stop()


def test_o73_each_dotted_name_the_resolver_gives_is_counted_in_an_answer(node, tmp_path, monkeypatch):
    """O73: a node of two dotted names (its cluster name and its fabric name) has a leak of either one counted; with
    the first alone, as `getfqdn` gave, a leak of the other counted 0."""
    _system(
        monkeypatch,
        "cn-own-n01",
        ("cn-own-n01.cluster.example.invalid", ["cn-own-n01.ib.example.invalid"], ["198.51.100.9"]),
    )
    node.own_names = loopback.own_names
    names = _scanned(node, tmp_path)
    gate.CFG.clear()
    gate.CFG["scan_names"] = names
    try:
        for leak in (
            "cn-own-n01.cluster.example.invalid",
            "https://cn-own-n01.ib.example.invalid:8443/x",
            "cn-own-n01",
        ):
            assert gate._node_name_hits(json.dumps({"domain": leak}).encode()) == 1, leak
    finally:
        gate.CFG.clear()


@pytest.mark.parametrize(
    "own",
    [
        ("sandbox",),
        ("Sandbox.cluster.example.invalid",),
        ("cn-own-n01", " sandbox.cluster.example.invalid "),
        ("cn-own-n01.cluster.example.invalid", "SANDBOX"),
    ],
)
def test_o73_an_agent_node_named_as_the_cell_is_refused_before_anything_is_created(node, tmp_path, own):
    """O73: a node whose host name, or the first label of whose fully qualified name, is the declared one would show the
    cell its own real name and have it counted by nobody: the start is refused before the first directory is made or
    anything ran, and the message names no node."""
    node.own_hosts = own
    settings = make_settings(tmp_path)
    with pytest.raises(StartRefused, match="agent node") as refused:
        start(node, settings, _spec())
    for word in ("cn-own-n01", "cluster.example.invalid"):
        assert word not in str(refused.value)
    assert node.execs == []
    assert node.gates == []
    assert list(settings.work_root.iterdir()) == []
