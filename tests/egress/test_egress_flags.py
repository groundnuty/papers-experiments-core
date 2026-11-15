"""One run's egress verdict: flagged for an outside connection, an unknown log or a static-scan hit; never clean when
its log is missing or damaged."""

import pytest
from egress_logs import NONCE, log, rec, record

from expcore.egress import LOG_UNKNOWN, NAMESPACE, OUTSIDE_FLAG, STATIC_SCAN, EgressLog, declaration, judge

DECLARED = declaration([("127.0.0.1", 41234)], ("127.0.0.1", 41234), hosts=["provider-a.test"])
GATE_ONLY = [rec("connect", "127.0.0.1", 41234), rec("result", "127.0.0.1", 41234)]


class Source:
    def __init__(self, data):
        self.data = data

    def egress_log(self):
        return self.data


def snapshot_of(data) -> dict:
    return EgressLog(Source(data), DECLARED, NONCE).finish()


def test_a_run_whose_intact_log_shows_only_the_gate_and_whose_code_has_no_hit_is_not_flagged():
    verdict = judge(record(instruments={NAMESPACE: snapshot_of(log(GATE_ONLY))}, cells=["print(1)"]), "cond-a")
    assert verdict.flags == ()
    assert not verdict.flagged


def test_an_outside_connection_flags_the_run():
    data = log([*GATE_ONLY, rec("connect", "192.0.2.1", 443)])
    assert judge(record(instruments={NAMESPACE: snapshot_of(data)}), "cond-a").flags == (OUTSIDE_FLAG,)


def test_a_static_scan_hit_flags_the_run():
    run = record(instruments={NAMESPACE: snapshot_of(log(GATE_ONLY))}, cells=["os.system('wget http://x')"])
    assert judge(run, "cond-a").flags == (STATIC_SCAN,)


@pytest.mark.parametrize(
    ("instruments", "reason"),
    [
        ({}, "not_recorded"),
        ({NAMESPACE: {"ok": False, "error": "OSError"}}, "instrument_failed"),
        ({NAMESPACE: {"version": 99}}, "not_a_snapshot"),
        ({NAMESPACE: "garbage"}, "not_a_snapshot"),
    ],
)
def test_a_run_without_a_readable_snapshot_is_flagged_as_unknown(instruments, reason):
    """No instrument, a failed one, or a snapshot this code cannot read: unknown, never clean."""
    verdict = judge(record(instruments=instruments), "cond-a")
    assert verdict.flags == (LOG_UNKNOWN,)
    assert verdict.unknown == (reason,)


@pytest.mark.parametrize(
    "data", [None, b"", log(GATE_ONLY, end=False), log(GATE_ONLY)[:-5], log(GATE_ONLY, malformed=1)]
)
def test_a_run_whose_log_is_missing_or_damaged_is_flagged_as_unknown_never_clean(data):
    verdict = judge(record(instruments={NAMESPACE: snapshot_of(data)}), "cond-a")
    assert verdict.log == "unknown"
    assert LOG_UNKNOWN in verdict.flags


def test_a_damaged_log_that_shows_an_outside_connection_says_both():
    data = log([rec("connect", "192.0.2.1", 443)], end=False)
    assert judge(record(instruments={NAMESPACE: snapshot_of(data)}), "cond-a").flags == (OUTSIDE_FLAG, LOG_UNKNOWN)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda s: s.update(counts={"gate": 1, "loopback": 0, "outside": 0}, outside=[{"attempts": 1}]),
        lambda s: s.update(log="intact", unknown=["not_closed"]),
        lambda s: s.update(counts=None),
        lambda s: s.update(declared={"gate": []}),
    ],
)
def test_a_snapshot_that_contradicts_itself_is_unknown(tamper):
    """An intact log whose counts disagree with its list, or that names a reason, or has no counts: not read."""
    snap = snapshot_of(log(GATE_ONLY))
    tamper(snap)
    assert judge(record(instruments={NAMESPACE: snap}), "cond-a").flags == (LOG_UNKNOWN,)


def test_a_run_without_a_snapshot_is_scanned_with_no_platform_names():
    """Its URLs to the provider flag too: nothing declared them the platform's."""
    run = record(cells=["get('https://provider-a.test/x')"])
    assert judge(run, "cond-a").flags == (LOG_UNKNOWN, STATIC_SCAN)
