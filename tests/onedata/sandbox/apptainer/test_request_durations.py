"""O73: a run records how long the requests the gate relayed took (`infrastructure_facts.rest_request_durations`), from
the gate's log once it stopped: how many, how many ran over 10, 30 and 60 s by the connection each waited longest on,
and the longest; and never reads an unknown count as zero."""

import json

import pytest
from apptainer_support import make_settings, make_spec, start
from fake_node import FakeNode

from expcore_onedata.sandbox import gatelog

STOPPED = {
    "signalled": True,
    "exit_code": 0,
    "output_read": True,
    "log_write_failed": False,
    "teardown_incomplete": False,
}
DONE = json.dumps({"event": "teardown_complete", "connections": 1})
NO_BAND = {"requests": 0, "over_10s": 0, "over_30s": 0, "over_60s": 0}


def _line(**more) -> str:
    return json.dumps({"method": "GET", "status": 200, "body_bytes": 10, "body": "other", **more})


def _relayed(ms: int, conn: str = "reused", *, waited: str | None = None, **more) -> str:
    """A relayed request's line: the connection it ended on (`conn`) and the one it waited longest on (`waited`)."""
    return _line(
        upstream_conn=conn,
        duration_ms=ms,
        upstream_wait_ms=ms,
        upstream_wait_conn=waited or conn,
        upstream_wait_end="first_byte",
        **more,
    )


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


def test_o73_a_request_is_over_a_band_when_it_took_more_than_it_and_is_counted_by_its_connection():
    """O73: over 10, 30 and 60 s means strictly more (a request of exactly 60 s is over 30 s only); each count is by the
    connection, the requests total is the sum, and the longest is the largest duration."""
    lines = [
        *[_relayed(ms, "new") for ms in (9_999, 10_000, 10_001)],
        *[_relayed(ms, "reused") for ms in (30_000, 30_001, 60_000, 60_001, 125_000)],
        DONE,
    ]
    assert gatelog.request_durations(lines, STOPPED) == {
        "version": 1,
        "requests": 8,
        "by_connection": {
            "new": {"requests": 3, "over_10s": 1, "over_30s": 0, "over_60s": 0},
            "reused": {"requests": 5, "over_10s": 5, "over_30s": 4, "over_60s": 2},
        },
        "max_duration_ms": 125_000,
        "unknown": [],
    }


def test_o73_a_request_that_waited_longest_on_a_reused_connection_counts_as_reused_though_it_ended_on_a_new_one():
    """O73: a stall on a kept connection that was dropped and retried on a new one ends on `new` and waited on `reused`:
    the count is the connection it waited on."""
    lines = [_relayed(63_000, "new", waited="reused"), DONE]
    found = gatelog.request_durations(lines, STOPPED)
    assert found["by_connection"]["reused"] == {"requests": 1, "over_10s": 1, "over_30s": 1, "over_60s": 1}
    assert found["by_connection"]["new"] == NO_BAND


def test_o73_a_line_of_no_relayed_request_is_not_counted():
    """O73: a refusal, an error the parser made and a connection's events reached no upstream: not requests here."""
    lines = [
        _line(decision="deny", status=403),
        json.dumps({"event": "request_error", "status": 400}),
        json.dumps({"event": "conn_close", "ended": "eof", "in_request": False}),
        DONE,
    ]
    assert gatelog.request_durations(lines, STOPPED) == {
        "version": 1,
        "requests": 0,
        "by_connection": {"new": NO_BAND, "reused": NO_BAND},
        "max_duration_ms": None,
        "unknown": [],
    }


@pytest.mark.parametrize(
    ("lines", "stop", "reason"),
    [
        (None, STOPPED, gatelog.UNREADABLE),
        ([_relayed(5)], STOPPED, gatelog.GATE_TEARDOWN_UNCONFIRMED),  # no teardown_complete: lines may be missing
        ([_relayed(5), DONE], {"signalled": False}, gatelog.GATE_NOT_STOPPED),
        ([_relayed(5), DONE], {**STOPPED, "log_write_failed": True}, gatelog.GATE_LOG_WRITE_FAILED),
        ([_relayed(5), DONE, "{not json"], STOPPED, gatelog.GATE_LOG_MALFORMED),  # a log cut mid-line
        ([json.dumps({"event": "conn_close", "in_request": True}), DONE], STOPPED, gatelog.REQUEST_IN_FLIGHT),
        ([_line(upstream_conn="new", upstream_wait_conn="new"), DONE], STOPPED, gatelog.GATE_LOG_MALFORMED),
        ([_line(duration_ms=5), DONE], STOPPED, gatelog.GATE_LOG_MALFORMED),  # a duration with no relayed request
        ([_line(upstream_conn="new", duration_ms=5), DONE], STOPPED, gatelog.GATE_LOG_MALFORMED),  # no wait
        ([_relayed("5"), DONE], STOPPED, gatelog.GATE_LOG_MALFORMED),
        ([_relayed(-1), DONE], STOPPED, gatelog.GATE_LOG_MALFORMED),
        ([_relayed(True), DONE], STOPPED, gatelog.GATE_LOG_MALFORMED),
        ([_relayed(1.5), DONE], STOPPED, gatelog.GATE_LOG_MALFORMED),
        ([_relayed(5, "other"), DONE], STOPPED, gatelog.GATE_LOG_MALFORMED),
        ([_relayed(5, "new", waited="other"), DONE], STOPPED, gatelog.GATE_LOG_MALFORMED),
    ],
)
def test_o73_a_summary_that_cannot_be_shown_whole_is_unknown_and_never_zero(lines, stop, reason):
    """O73: a log that could not be read, a gate not shown stopped, a log it failed to write or cut, a line that is no
    duration, or an answer in flight at the stop make every count `None` with the reason: never a partial sum."""
    found = gatelog.request_durations(lines, stop)
    assert found["requests"] is None
    assert found["by_connection"] is None
    assert found["max_duration_ms"] is None
    assert reason in found["unknown"]
    assert found["version"] == 1


def test_o73_the_facts_carry_the_summary_once_the_gate_is_stopped_and_unknown_before(node, tmp_path):
    """O73: before the collection the summary is unknown (`gate_not_collected`), never zero; after it, the sum of the
    gate's lines, in the run's `infrastructure_facts`, on every run that has a gate."""
    box = start(node, make_settings(tmp_path), make_spec("via-gate"))
    try:
        before = box.metadata()["infrastructure_facts"]["rest_request_durations"]
        assert before == {
            "version": 1,
            "requests": None,
            "by_connection": None,
            "max_duration_ms": None,
            "unknown": ["gate_not_collected"],
        }
        with open(box.dir / "gate" / "requests.jsonl", "a", encoding="utf-8") as log:
            log.write(_relayed(12_000, "reused") + "\n" + _relayed(40, "new") + "\n" + _line() + "\n")
        box.collect(tmp_path / "out")
        assert box.metadata()["infrastructure_facts"]["rest_request_durations"] == {
            "version": 1,
            "requests": 2,
            "by_connection": {
                "new": {**NO_BAND, "requests": 1},
                "reused": {"requests": 1, "over_10s": 1, "over_30s": 0, "over_60s": 0},
            },
            "max_duration_ms": 12_000,
            "unknown": [],
        }
    finally:
        box.stop()


def test_o73_a_run_with_no_gate_records_no_summary(node, tmp_path):
    """O73: a run that reads no data through a gate has no durations to sum: no key, not a zero."""
    box = start(node, make_settings(tmp_path), make_spec("isolated"))
    try:
        box.collect(tmp_path / "out")
        assert "rest_request_durations" not in box.metadata()["infrastructure_facts"]
    finally:
        box.stop()
