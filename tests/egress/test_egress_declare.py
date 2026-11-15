"""Classification of a destination: the data gate, loopback, or outside, from the run's declaration."""

import pytest

from expcore.egress import GATE, LOOPBACK, OUTSIDE, Declaration, declaration

GATE_PORT = 41234
DECLARED = declaration([("127.0.0.1", GATE_PORT)], ("127.0.0.1", GATE_PORT), hosts=["provider-a.test"])


@pytest.mark.parametrize(
    ("address", "port", "expected"),
    [
        ("127.0.0.1", GATE_PORT, GATE),
        ("::ffff:127.0.0.1", GATE_PORT, GATE),
        ("127.0.0.1", 8091, LOOPBACK),
        ("127.5.6.7", 443, LOOPBACK),
        ("::1", 443, LOOPBACK),
        ("192.0.2.1", 443, OUTSIDE),
        ("::ffff:192.0.2.1", 443, OUTSIDE),
        ("2001:db8::1", 443, OUTSIDE),
        ("0.0.0.0", 443, OUTSIDE),
        ("10.1.2.3", GATE_PORT, OUTSIDE),
    ],
)
def test_each_destination_is_the_gate_loopback_or_outside(address, port, expected):
    """O52: the gate by address and port; loopback by the declared ranges; everything else, the unspecified address
    and the node's own addresses included, outside."""
    assert DECLARED.classify(address, port) == expected


def test_a_declared_range_is_the_only_loopback():
    """A run that declares a narrower loopback range classifies the rest of 127/8 as outside."""
    narrow = declaration([("127.0.0.1", GATE_PORT)], ("127.0.0.1", GATE_PORT), loopback=["127.0.0.1/32"])
    assert narrow.classify("127.0.0.2", 80) == OUTSIDE


def test_a_declaration_reads_back_from_its_record_unchanged():
    assert Declaration.from_json(DECLARED.to_json()) == DECLARED


GOOD = {"gate": [], "canary": ["127.0.0.1", 41234], "loopback": [], "hosts": [], "network_calls": False}


@pytest.mark.parametrize(
    "change",
    [
        {"gate": [["not-an-address", 1]]},
        {"gate": [["127.0.0.1", 0]]},
        {"canary": ["127.0.0.1"]},
        {"canary": ["127.0.0.1", "41234"]},
        {"loopback": ["127.0.0.1/8"]},
        {"network_calls": "yes"},
        {"canary": None},
    ],
)
def test_a_recorded_declaration_that_is_not_one_is_refused(change):
    """A bad address or port, a canary that is not one endpoint, a range with host bits, a non-boolean flag."""
    with pytest.raises(ValueError, match=r"."):
        Declaration.from_json({**GOOD, **change})


def test_a_recorded_declaration_without_its_canary_is_refused():
    with pytest.raises(ValueError, match="not a recorded egress declaration"):
        Declaration.from_json({k: v for k, v in GOOD.items() if k != "canary"})


def test_the_canary_is_its_endpoint_and_no_other():
    assert DECLARED.is_canary("::ffff:127.0.0.1", GATE_PORT)
    assert not DECLARED.is_canary("127.0.0.1", GATE_PORT + 1)


@pytest.mark.parametrize(
    ("host", "platform"),
    [("provider-a.test", True), ("PROVIDER-A.test.", True), ("localhost", True), ("127.0.0.1", True)],
)
def test_the_platforms_names_gate_and_loopback_are_platform_hosts(host, platform):
    assert DECLARED.is_platform_host(host) is platform


def test_any_other_host_is_not_the_platforms():
    assert not DECLARED.is_platform_host("pypi.org")
    assert not DECLARED.is_platform_host("192.0.2.1")
