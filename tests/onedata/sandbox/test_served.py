"""O20: the bytes a run's agent read through its data path, counted once from the stopped gate's log; unknown, not 0.

The count is a pure function of the gate's log lines and of what the gate's stop found (`gatelog.served`); the sandbox
takes it when its collection has stopped the gate, and the platform's instrument records it. Every line here is one
the gate writes, with invented values; the sandbox runs over the in-memory host.
"""

import json

import pytest
from sandbox_support import COMPLETE_LEVEL, make_spec, start_options

from expcore.records.instruments import finish_instruments, start_instruments
from expcore_onedata.sandbox import gatelog
from expcore_onedata.sandbox.sandbox import DockerSandbox
from expcore_onedata.sandbox.served import NAMESPACE, ServedBytes

STOPPED = {"signalled": True, "exit_code": 0, "log_write_failed": False, "teardown_incomplete": False}
ANSWERS = [  # a run's answers, as the gate logs them
    {"method": "GET", "tag": "content", "status": 200, "body": "content", "body_bytes": 1000},
    {"method": "GET", "tag": "content", "status": 206, "body": "content", "body_bytes": 24},
    {"method": "GET", "tag": "content", "status": 502, "body": "content", "body_bytes": 40, "cut": True},
    {"method": "GET", "tag": "children", "status": 200, "body": "listing", "body_bytes": 300, "complete_listing": True},
    {"method": "GET", "tag": "attrs", "status": 200, "body": "other", "body_bytes": 90},
    {"method": "GET", "tag": "children", "status": 404, "body": "other", "body_bytes": 50},
    {"method": "PUT", "decision": "deny", "status": 403, "body": "gate", "body_bytes": 70},
    {"event": "request_error", "status": 400, "body": "gate", "body_bytes": 11},
    {"event": "request_error", "status": None, "reason": "request headers not received in time"},
    {"event": "conn_close", "conn": "1-1", "after_request": 3, "ended": "eof"},
    {"event": "conn_close", "conn": "1-2", "after_request": 4, "ended": "teardown", "in_request": False},
    {"event": "connection_error", "error_origin": "client"},
]


def log(*records) -> list[str]:
    return [json.dumps(r) for r in records]


def test_o20_the_bytes_served_add_up_each_answer_by_what_its_body_was():
    """O20: the platform's file content (a cut transfer's part included), its listings and its other answers make the
    bytes served; the gate's own answers are counted apart, and lines that served nothing count nothing."""
    got = gatelog.served(log(*ANSWERS), STOPPED)
    assert got == {
        "served_bytes": 1000 + 24 + 40 + 300 + 90 + 50,
        "content_bytes": 1064,
        "listing_bytes": 300,
        "other_bytes": 140,
        "gate_answer_bytes": 81,
        "unknown": [],
    }


@pytest.mark.parametrize(
    ("lines", "stop", "reason"),
    [
        (None, STOPPED, "gate_log_unreadable"),
        (log(*ANSWERS), None, "gate_not_stopped"),
        (log(*ANSWERS), {**STOPPED, "signalled": False}, "gate_not_stopped"),
        (log(*ANSWERS), {k: v for k, v in STOPPED.items() if k != "exit_code"}, "gate_not_stopped"),
        (log(*ANSWERS), {**STOPPED, "log_write_failed": True}, "gate_log_write_failed"),
        (log(*ANSWERS), {k: v for k, v in STOPPED.items() if k != "teardown_incomplete"}, "gate_teardown_incomplete"),
        (
            log(*ANSWERS, {"event": "conn_close", "ended": "teardown", "in_request": True}),
            STOPPED,
            "request_in_flight_at_stop",
        ),
        ([*log(*ANSWERS), '{"method": "GET", "tag": "content", "body_by'], STOPPED, "gate_log_malformed"),
        (log(*ANSWERS, {"method": "GET", "tag": "attrs", "status": 200}), STOPPED, "gate_log_malformed"),
        (log(*ANSWERS, {"method": "GET", "status": 200, "body_bytes": 5}), STOPPED, "gate_log_malformed"),
        (log(*ANSWERS, {"method": "GET", "body": "content", "body_bytes": -1}), STOPPED, "gate_log_malformed"),
        (log(*ANSWERS, ["not", "an", "object"]), STOPPED, "gate_log_malformed"),
    ],
    ids=[
        "unreadable",
        "no-stop",
        "not-signalled",
        "not-exited",
        "log-write-failed",
        "teardown-unknown",
        "in-flight",
        "cut-line",
        "no-body-bytes",
        "no-body-kind",
        "negative",
        "not-an-object",
    ],
)
def test_o20_a_count_that_may_be_missing_a_line_is_unknown_never_zero(lines, stop, reason):
    """O20: a log that may lack a line, or holds one that does not say what it served, makes every count None with the
    reason, never a partial sum taken for the whole."""
    got = gatelog.served(lines, stop)
    assert got["unknown"] == [reason]
    assert all(got[field] is None for field in gatelog.SERVED_FIELDS)


def start(world, level):
    return DockerSandbox.start(world.host, world.s, make_spec(world, level), **start_options(world))


def write_log(sbx, *records) -> None:
    (sbx.dir / "gatelog" / "requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), "utf-8")


@pytest.mark.parametrize(("level", "listing"), [(COMPLETE_LEVEL, "complete"), ("via-gate", "paged")])
def test_o20_a_rest_runs_count_is_taken_once_by_the_collection_and_the_instrument_records_it(
    world, tmp_path, level, listing
):
    """O20: before its collection a REST run's count is unknown, since its gate still runs; the collection stops the
    gate and counts its log once, the metadata and the instrument's snapshot carry that count with the data path and
    its listing mode, and it stays the same after the sandbox and its log are gone."""
    sbx = start(world, level)
    instrument = ServedBytes(sbx)
    start_instruments([instrument], {"run_key": "run-key-1", "factors": {}, "attempt": 1})
    assert sbx.served_bytes()["unknown"] == ["gate_not_collected"]
    assert sbx.served_bytes()["served_bytes"] is None
    write_log(sbx, *ANSWERS)
    sbx.collect(tmp_path / "evidence")
    write_log(sbx)  # the log changing after the collection does not change the count
    expected = {"data_path": "rest", "listing": listing, **gatelog.served(log(*ANSWERS), STOPPED)}
    assert sbx.served_bytes() == expected
    assert json.loads((tmp_path / "evidence" / "metadata.json").read_text())["served_bytes"] == expected
    snapshot = instrument.finish()
    assert snapshot == {"version": 1, **expected}
    sbx.stop()
    assert sbx.served_bytes() == expected
    assert NAMESPACE == "served_bytes"


def test_o20_a_second_collection_keeps_the_count_the_first_took(world, tmp_path):
    """O20: the count is taken once. A second collection stops a gate that is already gone (on a real host its stop
    then fails), so counting again could only lose what the first collection counted; it keeps that count."""
    sbx = start(world, "via-gate")
    write_log(sbx, *ANSWERS)
    sbx.collect(tmp_path / "first")
    counted = sbx.served_bytes()
    write_log(sbx)  # a recount of this log would give zero
    sbx.collect(tmp_path / "second")
    assert sbx.served_bytes() == counted
    assert counted["served_bytes"] == 1504
    sbx.stop()


def test_o20_the_instrument_puts_the_count_in_the_record_where_a_scorer_reads_it(world, tmp_path):
    """O20: through the core's instrument hooks the count lands in the record under its namespace, as a plain JSON
    object, so a scorer and replay read the recorded value and nothing measures it again."""
    from record_support import hand_made

    sbx = start(world, COMPLETE_LEVEL)
    instrument = ServedBytes(sbx)
    start_instruments([instrument], {"run_key": "run-key-1", "factors": {}, "attempt": 1})
    write_log(sbx, *ANSWERS)
    sbx.collect(tmp_path / "evidence")
    record = finish_instruments(hand_made(), [instrument])
    assert record.instruments[NAMESPACE]["served_bytes"] == 1504
    assert record.instruments[NAMESPACE]["listing"] == "complete"
    sbx.stop()


def test_o20_a_mounts_reads_are_unknown_and_no_data_path_read_nothing(world):
    """O20: a mount's reads go through oneclient, which no gate sees, so its count is unknown and never zero; a run with
    no data path had nothing to read through, so its count is zero."""
    mounted = start(world, "mounted")
    got = mounted.served_bytes()
    assert (got["data_path"], got["listing"], got["unknown"]) == ("mount", None, ["mount_not_through_gate"])
    assert all(got[field] is None for field in gatelog.SERVED_FIELDS)
    assert ServedBytes(mounted).finish()["served_bytes"] is None
    mounted.stop()
    isolated = start(world, "isolated")
    got = isolated.served_bytes()
    assert (got["data_path"], got["unknown"]) == ("none", [])
    assert all(got[field] == 0 for field in gatelog.SERVED_FIELDS)
    isolated.stop()


def test_o20_a_gate_whose_stop_left_a_request_unanswered_or_a_close_line_unwritten_is_unknown(world, tmp_path):
    """O20: the stop of the gate decides whether its log is whole: a request still being answered when it stopped, or a
    teardown that says it could not write every close line, makes the count unknown."""
    sbx = start(world, "via-gate")
    write_log(sbx, *ANSWERS, {"event": "conn_close", "conn": "1-9", "ended": "teardown", "in_request": True})
    sbx.collect(tmp_path / "first")
    assert sbx.served_bytes()["unknown"] == ["request_in_flight_at_stop"]
    sbx.stop()
    other = start(world, "via-gate")
    world.host.containers[other.names.gate].logs = "gate ready\nGATE TEARDOWN INCOMPLETE\n"
    write_log(other, *ANSWERS)
    other.collect(tmp_path / "second")
    assert other.served_bytes()["unknown"] == ["gate_teardown_incomplete"]
    assert other.metadata()["gate_stop"]["teardown_incomplete"] is True
    other.stop()


def test_o20_a_gate_log_that_is_not_json_is_unknown_and_the_metadata_still_written(world, tmp_path):
    """O20: a line cut short (a gate killed mid-write) leaves the count unknown, and the collection still writes the
    metadata, with the gate's summary unknown rather than a crash."""
    sbx = start(world, "via-gate")
    (sbx.dir / "gatelog" / "requests.jsonl").write_text('{"method": "GET", "body_by\n', encoding="utf-8")
    sbx.collect(tmp_path / "evidence")
    meta = json.loads((tmp_path / "evidence" / "metadata.json").read_text())
    assert meta["served_bytes"]["unknown"] == ["gate_log_malformed"]
    assert meta["gate"]["unavailable"] == "the gate log could not be parsed"
    sbx.stop()
