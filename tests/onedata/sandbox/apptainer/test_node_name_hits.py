"""O73: a run whose federation is reached by declared names records how many node names and addresses the gate saw in
the answers it relayed (`infrastructure_facts.rest_node_name_hits`), from the gate's log once it stopped, and never
reads an unknown count as zero; the list of names reaches the gate as its own configuration, never a cell."""

import json
from pathlib import Path

import pytest
from apptainer_support import make_settings, make_spec, start
from fake_node import OWN_FQDN, OWN_HOST, FakeNode

from expcore_onedata.addresses import build
from expcore_onedata.sandbox import gatelog
from expcore_onedata.sandbox.api import DataPathMode, SandboxSpec
from expcore_onedata.sandbox.apptainer import gaterun
from expcore_onedata.sandbox.apptainer.settings import GATE_OWN_KEYS
from expcore_onedata.tls import TlsSettings
from expcore_onedata.tokens import Secret

NODE_A, NODE_B = "cn-r3c07-n12.cluster.example.org", "cn-r3c07-n13.cluster.example.org"
NAMES = (NODE_A, "cn-r3c07-n12", "198.51.100.21", NODE_B, "cn-r3c07-n13", "198.51.100.22")
STOPPED = {
    "signalled": True,
    "exit_code": 0,
    "output_read": True,
    "log_write_failed": False,
    "teardown_incomplete": False,
}


def _line(**more) -> str:
    return json.dumps({"method": "GET", "status": 200, "body_bytes": 10, "body": "other", **more})


DONE = json.dumps({"event": "teardown_complete", "connections": 1})


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


def _spec(names=NAMES) -> SandboxSpec:
    base = make_spec("via-gate")
    return SandboxSpec(
        run_key=base.run_key, mode=DataPathMode.REST, token=Secret("tok-0123456789"), zone=base.zone,
        provider=base.provider, node_names=tuple(names),
    )  # fmt: skip


def test_o73_the_hits_are_the_sum_of_the_lines_of_a_gate_that_stopped_whole():
    """O73: the count is the sum of the request lines' `node_name_hits`, one channel, a version, and no unknown."""
    lines = [_line(node_name_hits=2), _line(), _line(node_name_hits=1), DONE]
    assert gatelog.node_name_hits(lines, STOPPED) == {"version": 1, "rest_answers": 3, "unknown": []}
    assert gatelog.node_name_hits([_line(), DONE], STOPPED) == {"version": 1, "rest_answers": 0, "unknown": []}


@pytest.mark.parametrize(
    ("lines", "stop", "reason"),
    [
        (None, STOPPED, gatelog.UNREADABLE),
        (
            [_line(node_name_hits=1)],
            STOPPED,
            gatelog.GATE_TEARDOWN_UNCONFIRMED,
        ),  # no teardown_complete: lines may be missing
        ([_line(node_name_hits=1), DONE], {"signalled": False}, gatelog.GATE_NOT_STOPPED),
        ([_line(node_name_hits=1), DONE], {**STOPPED, "log_write_failed": True}, gatelog.GATE_LOG_WRITE_FAILED),
        ([_line(node_name_hits="2"), DONE], STOPPED, gatelog.GATE_LOG_MALFORMED),
        ([_line(node_name_hits=-1), DONE], STOPPED, gatelog.GATE_LOG_MALFORMED),
        ([_line(node_name_hits=True), DONE], STOPPED, gatelog.GATE_LOG_MALFORMED),
        (["{not json", DONE], STOPPED, gatelog.GATE_LOG_MALFORMED),
        ([json.dumps({"event": "conn_close", "in_request": True}), DONE], STOPPED, gatelog.REQUEST_IN_FLIGHT),
    ],
)
def test_o73_a_count_that_cannot_be_shown_whole_is_unknown_and_never_zero(lines, stop, reason):
    """O73: a log that could not be read, a gate not shown stopped, a log it failed to write, a line that is no count or
    one answer in flight at the stop make the count `None` with the reason: never a partial sum taken for the whole."""
    found = gatelog.node_name_hits(lines, stop)
    assert found["rest_answers"] is None
    assert reason in found["unknown"]
    assert found["version"] == 1


def test_o73_the_gates_configuration_carries_the_names_when_given_and_a_gate_option_cannot_set_it():
    """O73: `scan_names` is the sandbox's own key of the gate's configuration, set only when there are names to scan."""
    base = {
        "run_key": "r", "tls": TlsSettings(), "served": {}, "listing": "paged", "directory": Path("/x"),
        "tls_dir": Path("/y"), "token_file": Path("/z"), "port": 1, "upstream_ca": None, "options": {},
        "upstream_read_timeout_s": 14400.0,
    }  # fmt: skip
    assert gaterun.config(**base, scan_names=NAMES)["scan_names"] == list(NAMES)
    assert "scan_names" not in gaterun.config(**base, scan_names=())
    assert "scan_names" in GATE_OWN_KEYS
    with pytest.raises(Exception, match="scan_names"):
        gaterun.config(**{**base, "options": {"scan_names": ["x"]}}, scan_names=NAMES)


def test_o73_the_address_map_gives_the_real_names_and_addresses_and_never_a_declared_name():
    """O73: the list is the map's real side: each node's names and address, lower case, once, no declared name."""
    found = build(
        {
            "zone.layout.test": ("198.51.100.21", [NODE_A, "cn-r3c07-n12"]),
            "alpha.layout.test": ("198.51.100.22", [NODE_B.upper(), "cn-r3c07-n13"]),
        }
    )
    assert sorted(found.real_hosts()) == sorted(NAMES)
    assert not [h for h in found.real_hosts() if h.endswith(".test")]


def test_o73_the_platform_gives_a_run_the_names_of_its_map_and_a_platform_without_one_none(tmp_path, monkeypatch):
    """O73: the spec a run starts with carries the map's real names (not in its repr); a platform with no `[addresses]`,
    a Compose federation's, carries none."""
    from types import SimpleNamespace

    from expcore_onedata.config import load_platform
    from expcore_onedata.sandbox.provider import SandboxPlatform
    from expcore_onedata.testing import ADMIN_ENV, write_platform

    monkeypatch.setenv(ADMIN_ENV, "admin-token-0123456789")
    found = build({"alpha.test": ("198.51.100.22", [NODE_B]), "zone.test": ("198.51.100.21", [NODE_A])})
    (tmp_path / "map.json").write_text(json.dumps(found.to_document()), encoding="utf-8")
    (tmp_path / "map.json").chmod(0o600)
    (tmp_path / "plain").mkdir()
    mapped = load_platform(write_platform(tmp_path, lambda d: d.update(addresses={"file": "map.json"})))
    plain = load_platform(write_platform(tmp_path / "plain"))

    def spec_of(config):
        stub = SimpleNamespace(
            _s=SimpleNamespace(mode_of=lambda level: DataPathMode.REST, listing_of=lambda level: None), _config=config
        )
        return SandboxPlatform._spec(stub, "run-key-1", "via-gate", Secret("tok-0123456789"), None)

    assert sorted(spec_of(mapped).node_names) == sorted(["198.51.100.21", "198.51.100.22", NODE_A, NODE_B])
    assert NODE_A not in repr(spec_of(mapped))
    assert spec_of(plain).node_names == ()


def test_o73_the_gate_is_given_the_names_and_a_cell_is_given_none_of_them(node, tmp_path):
    """O73: the gate's own configuration holds the list, the federation's nodes' names and addresses and then the agent
    node's own names (`own_names`); no exec's argument, environment or bound file of a cell holds a name, an address or
    the port."""
    box = start(node, make_settings(tmp_path), _spec())
    try:
        box.exec("print(1)", 30)
        assert node.gates[-1].config["scan_names"] == [*NAMES, OWN_HOST, OWN_FQDN]
        for done in node.execs:
            seen = [" ".join(done.argv), json.dumps(done.env)]
            for source, _ in done.binds.values():
                path = Path(source)
                files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
                seen += [p.read_bytes().decode("utf-8", "replace") for p in files]
            for word in (*NAMES, OWN_HOST, OWN_FQDN):
                assert not [text for text in seen if word in text], word
    finally:
        box.stop()


def test_o73_the_facts_carry_the_count_once_the_gate_is_stopped_and_unknown_before(node, tmp_path):
    """O73: before the collection the count is unknown (`gate_not_collected`), never zero; after it, the sum of the
    gate's lines, beside the version, in the run's `infrastructure_facts`."""
    box = start(node, make_settings(tmp_path), _spec())
    try:
        before = box.metadata()["infrastructure_facts"]["rest_node_name_hits"]
        assert before == {"version": 1, "rest_answers": None, "unknown": ["gate_not_collected"]}
        with open(box.dir / "gate" / "requests.jsonl", "a", encoding="utf-8") as log:
            log.write(_line(node_name_hits=2) + "\n" + _line(node_name_hits=1) + "\n" + _line() + "\n")
        box.collect(tmp_path / "out")
        assert box.metadata()["infrastructure_facts"]["rest_node_name_hits"] == {
            "version": 1,
            "rest_answers": 3,
            "unknown": [],
        }
    finally:
        box.stop()


def test_o73_a_run_given_no_names_records_no_count(node, tmp_path):
    """O73: a Compose federation's, or any run whose platform has no map: no list, no scan, no key (not a zero)."""
    box = start(node, make_settings(tmp_path), make_spec("via-gate"))
    try:
        box.collect(tmp_path / "out")
        assert "rest_node_name_hits" not in box.metadata()["infrastructure_facts"]
        assert "scan_names" not in node.gates[-1].config
    finally:
        box.stop()
