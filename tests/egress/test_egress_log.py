"""Reading the collector's log: intact only when every check holds, otherwise unknown with its reasons.

The reader knows nothing of the canary (the instrument checks it against the run's declaration), so these logs are
built without one.
"""

import json

import pytest
from egress_logs import NONCE, encode, rec
from egress_logs import lines as canary_lines
from egress_logs import log as canary_log

from expcore.egress import log as egress_log
from expcore.egress import read_log


def lines(records, **options):
    return canary_lines(records, canary=False, **options)


def log(records, **options):
    return canary_log(records, canary=False, **options)


RECORDS = [rec("connect", "127.0.0.1", 41234), rec("result", "127.0.0.1", 41234)]


def test_a_complete_log_of_this_run_is_intact_with_its_records_in_order():
    reading = read_log(log(RECORDS), NONCE)
    assert reading.intact
    assert reading.problems == ()
    assert list(reading.records) == RECORDS


def test_a_log_of_no_record_is_intact():
    """A run that connected nowhere has a start and an end line and nothing between: known, zero."""
    assert read_log(log([]), NONCE).intact


def damaged(kind: str) -> bytes:
    """A log damaged in one way."""
    out = lines(RECORDS)
    if kind == "cut_end":
        return encode(out[:-1])
    if kind == "cut_line":
        return encode(out)[:-7]
    if kind == "gap":
        del out[1]
    elif kind == "repeat":
        out[2]["seq"] = 1
    elif kind == "clock_back":
        out[2]["mono"] = 0.0
    elif kind == "after_end":
        out.append({**out[1], "seq": len(out), "mono": 999.0})
    elif kind == "bad_record":
        out[1]["rec"] = {**out[1]["rec"], "address": "not-an-address"}
    elif kind == "end_counts":
        out[-1]["records"] = 5
    elif kind == "no_start":
        out[0]["event"] = "record"
    return encode(out)


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("cut_end", egress_log.NOT_CLOSED),
        ("cut_line", egress_log.UNPARSEABLE),
        ("gap", egress_log.NOT_MONOTONIC),
        ("repeat", egress_log.NOT_MONOTONIC),
        ("clock_back", egress_log.NOT_MONOTONIC),
        ("after_end", egress_log.NOT_CLOSED),
        ("bad_record", egress_log.UNPARSEABLE),
        ("end_counts", egress_log.COUNTS_DIFFER),
        ("no_start", egress_log.NO_START),
    ],
)
def test_a_damaged_log_is_unknown_with_the_reason(kind, reason):
    """Cut, out of order, past its end, a record that is not one, an end that does not count what came before, no
    start line: each is unknown, never intact."""
    reading = read_log(damaged(kind), NONCE)
    assert not reading.intact
    assert reason in reading.problems


@pytest.mark.parametrize(
    ("data", "reason"), [(None, egress_log.MISSING), (b"", egress_log.EMPTY), (b"\xff\xfe\n", egress_log.UNPARSEABLE)]
)
def test_a_missing_empty_or_unreadable_log_is_unknown(data, reason):
    assert read_log(data, NONCE).problems == (reason,)


def test_a_log_of_another_run_is_unknown():
    """The nonce binds the log to the run that started its collector."""
    assert egress_log.NOT_THIS_RUN in read_log(log(RECORDS, nonce="ff" * 16), NONCE).problems


def test_a_log_past_the_size_limit_is_unknown_and_not_read():
    assert read_log(log(RECORDS), NONCE, max_bytes=10).problems == (egress_log.TOO_LARGE,)


@pytest.mark.parametrize(
    ("options", "reason"),
    [({"malformed": 1}, egress_log.MALFORMED), ({"unwritten": 2}, egress_log.UNWRITTEN)],
)
def test_a_log_that_held_something_other_than_a_record_or_lost_one_is_unknown(options, reason):
    """A malformed datagram (a forgery, say) or one past the collector's cap: what it hid cannot be known."""
    assert reason in read_log(log(RECORDS, **options), NONCE).problems


def test_a_record_that_says_its_sender_dropped_records_makes_the_log_unknown():
    reading = read_log(log([*RECORDS, rec("connect", "192.0.2.1", 443, dropped=1)]), NONCE)
    assert egress_log.DROPPED in reading.problems


def test_a_record_from_another_uid_than_the_collectors_makes_the_log_unknown():
    out = lines(RECORDS)
    out[1]["peer"]["uid"] = 4242
    assert egress_log.FOREIGN in read_log(encode(out), NONCE).problems


def test_the_records_before_the_first_damaged_line_are_kept_as_a_lower_bound():
    """An outside connect read before the damage is still there for the verdict to see."""
    out = lines([rec("connect", "192.0.2.1", 443), *RECORDS])
    out[2]["rec"] = "garbage"
    reading = read_log(encode(out), NONCE)
    assert not reading.intact
    assert [r["address"] for r in reading.records] == ["192.0.2.1"]


def test_a_line_that_is_not_an_object_is_unparseable():
    data = log(RECORDS).replace(b'{"event": "end"', json.dumps([1]).encode() + b"\n" + b'{"event": "end"', 1)
    assert egress_log.UNPARSEABLE in read_log(data, NONCE).problems
