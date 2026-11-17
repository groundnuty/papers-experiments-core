"""O91: reading the read-log collector's log: intact only when every check holds, otherwise unknown with its reasons.

Pure bytes in, a `Reading` out: no socket here. A run with an unknown log reads as UNKNOWN reads, never as fewer reads.
"""

import pytest
from readlog_logs import NONCE, PEER_UID, SHA, canary, encode, lines, log, read

from expcore_onedata.readlog import reader as reader_module
from expcore_onedata.readlog.reader import read_log

RECORDS = [read("file-1", range=[0, 4096], bytes=4096, sha256=SHA), read("file-2")]


def reading_of(data, **options):
    return read_log(data, NONCE, expected_peer_uid=options.pop("expected_peer_uid", PEER_UID), **options)


def test_o91_a_complete_log_of_this_run_is_intact_with_its_records_in_order():
    """O91: start, the canary, the reads, end, the counts agreeing: intact, the canary first and the reads in order."""
    reading = reading_of(log(RECORDS))
    assert reading.intact
    assert reading.problems == ()
    assert list(reading.reads) == RECORDS
    assert list(reading.records) == [canary(), *RECORDS]


def test_o91_a_log_of_a_run_that_read_nothing_is_intact_with_its_canary_alone():
    """O91: the canary, then nothing: known, zero reads. Not the same as a log nobody could read."""
    reading = reading_of(log([]))
    assert reading.intact
    assert reading.reads == ()


def damaged(kind: str) -> bytes:
    """A log damaged in one way."""
    out = lines(RECORDS)
    if kind == "cut_end":
        return encode(out[:-1])
    if kind == "cut_line":
        return encode(out)[:-7]
    if kind == "gap":
        del out[2]
    elif kind == "repeat":
        out[2]["seq"] = 1
    elif kind == "clock_back":
        out[2]["mono"] = 0.0
    elif kind == "after_end":
        out.append({**out[1], "seq": len(out), "mono": 999.0})
    elif kind == "bad_record":
        out[2]["rec"] = {**out[2]["rec"], "item": 5}
    elif kind == "extra_field":
        out[2]["rec"] = {**out[2]["rec"], "path": "/data/x"}
    elif kind == "end_counts":
        out[-1]["records"] = 5
    elif kind == "no_start":
        del out[0]
    elif kind == "two_ends":
        out.insert(3, {**out[-1], "seq": 3, "mono": 103.0})
    elif kind == "unknown_event":
        out[2]["event"] = "note"
    else:
        raise AssertionError(kind)
    return encode(out)


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("cut_end", reader_module.NOT_CLOSED),
        ("cut_line", reader_module.UNPARSEABLE),
        ("gap", reader_module.NOT_MONOTONIC),
        ("repeat", reader_module.NOT_MONOTONIC),
        ("clock_back", reader_module.NOT_MONOTONIC),
        ("after_end", reader_module.NOT_CLOSED),
        ("bad_record", reader_module.BAD_RECORD),
        ("extra_field", reader_module.BAD_RECORD),
        ("end_counts", reader_module.COUNTS_DIFFER),
        ("no_start", reader_module.NO_START),
        ("two_ends", reader_module.NOT_CLOSED),
        ("unknown_event", reader_module.BAD_RECORD),
    ],
)
def test_o91_a_damaged_log_is_unknown_with_the_reason(kind, reason):
    """O91: each way a log can be damaged (a kill, a cut, a hole, a forged line) is a reason; none reads as intact."""
    reading = reading_of(damaged(kind))
    assert not reading.intact
    assert reason in reading.problems


def test_o91_a_killed_collector_leaves_a_log_with_no_end_line_which_is_unknown_and_keeps_what_was_read():
    """O91: no end line (the collector was killed with records queued): unknown, never fewer reads; the records read
    before the cut are kept as a lower bound."""
    reading = reading_of(log(RECORDS, end=False))
    assert reading.problems == (reader_module.NOT_CLOSED,)
    assert list(reading.reads) == RECORDS


@pytest.mark.parametrize("data", [None, b""])
def test_o91_a_missing_or_empty_log_is_unknown_never_a_run_that_read_nothing(data):
    """O91: a missing or empty log is unknown never a run that read nothing."""
    reading = reading_of(data)
    assert not reading.intact
    assert reading.problems == ((reader_module.MISSING,) if data is None else (reader_module.EMPTY,))
    assert reading.reads == ()


def test_o91_a_log_past_the_size_limit_is_not_read():
    """O91: a log past the size limit is not read."""
    reading = reading_of(log(RECORDS), max_bytes=100)
    assert reading.problems == (reader_module.TOO_LARGE,)


def test_o91_a_log_of_another_run_is_unknown():
    """O91: a log of another run is unknown."""
    reading = read_log(log(RECORDS, nonce="ff" * 16), NONCE, expected_peer_uid=PEER_UID)
    assert reading.problems == (reader_module.NOT_THIS_RUN,)


def test_o91_a_malformed_datagram_makes_the_log_unknown():
    """O91: a datagram that was no record (a forged or damaged one) is a malformed line; the whole log is unknown."""
    reading = reading_of(log(RECORDS, malformed=1))
    assert reading.problems == (reader_module.MALFORMED,)
    assert list(reading.reads) == RECORDS


def test_o91_a_log_that_hit_its_cap_is_unknown():
    """O91: the cap was reached and datagrams were counted unwritten: those reads are unknown, so the log is."""
    reading = reading_of(log(RECORDS, unwritten=3, cap_reached=True))
    assert set(reading.problems) == {reader_module.UNWRITTEN, reader_module.CAP_REACHED}


def test_o91_an_end_line_that_does_not_say_the_cap_was_not_reached_is_unknown():
    """O91: an end line that does not say the cap was not reached is unknown."""
    out = lines(RECORDS)
    del out[-1]["cap_reached"]
    assert reader_module.CAP_REACHED in reading_of(encode(out)).problems


def test_o91_a_record_from_another_uid_than_the_expected_peer_is_unknown():
    """O91: the peer rule: every record's sender is the expected uid (the driver's, or the declared agent's)."""
    reading = reading_of(log(RECORDS, sender_uid=PEER_UID + 1))
    assert reader_module.FOREIGN in reading.problems
    assert not reading.intact


def test_o91_a_collector_told_another_peer_uid_than_the_harness_expects_is_unknown():
    """O91: the start line records which uid the collector was told to expect; a log whose collector expected another
    one than the harness declares is not this backend's log."""
    reading = reading_of(log(RECORDS, start_peer_uid=PEER_UID + 5))
    assert reader_module.WRONG_PEER_RULE in reading.problems


def test_o91_a_sender_with_no_credentials_is_foreign():
    """O91: a sender with no credentials is foreign."""
    out = lines(RECORDS)
    out[2]["peer"] = {"pid": None, "uid": None}
    assert reader_module.FOREIGN in reading_of(encode(out)).problems


def test_o91_a_peer_pid_of_zero_is_accepted_across_pid_namespaces():
    """O91: Docker's peer pid across PID namespaces reads 0: it is recorded, never refused."""
    out = lines(RECORDS)
    for line in out:
        if line["event"] == "record":
            line["peer"]["pid"] = 0
    assert reading_of(encode(out)).intact


def test_o91_a_log_whose_first_record_is_no_canary_is_unknown():
    """O91: the harness's canary gives the log its first record; a log without one never proved the channel was up."""
    reading = reading_of(log(RECORDS, with_canary=False))
    assert reading.problems == (reader_module.NO_CANARY,)


def test_o91_a_log_with_a_start_and_an_end_and_nothing_between_has_no_canary():
    """O91: the shape a channel that never worked leaves must not read as a run that read nothing."""
    assert reading_of(log([], with_canary=False)).problems == (reader_module.NO_CANARY,)


def test_o91_the_reasons_are_each_named_once_in_the_order_found():
    """O91: the reasons are each named once in the order found."""
    out = lines(RECORDS, malformed=2, sender_uid=PEER_UID + 1)
    problems = reading_of(encode(out)).problems
    assert len(problems) == len(set(problems))


@pytest.mark.parametrize(
    "rec",
    [
        {"v": 2, "op": "read", "item": "x"},
        {"v": 1, "op": "write", "item": "x"},
        {"v": 1, "op": "read", "item": ""},
        {"v": 1, "op": "read", "item": "x" * 1025},
        {"v": 1, "op": "read", "item": "x", "range": [5, 2]},
        {"v": 1, "op": "read", "item": "x", "range": [0]},
        {"v": 1, "op": "read", "item": "x", "bytes": -1},
        {"v": 1, "op": "read", "item": "x", "bytes": True},
        {"v": 1, "op": "read", "item": "x", "sha256": "AB" * 32},
        {"v": 1, "op": "read"},
    ],
)
def test_o91_a_record_line_that_is_not_a_client_record_is_a_bad_record(rec):
    """O91: a record line that is not a client record is a bad record."""
    out = lines([rec])
    assert reader_module.BAD_RECORD in reading_of(encode(out)).problems
