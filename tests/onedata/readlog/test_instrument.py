"""O91: the `read_log` instrument: its snapshot holds intact or unknown with reasons, counts and the log's hash.

An unknown log is never zero reads: `counts` is None, `records` is only what the readable part shows, and the
snapshot says so. The hash is of the log's bytes as the sandbox read them, the file the study declares as a side file.
"""

import hashlib
import json

import pytest
from readlog_logs import NONCE, PEER_UID, SHA, encode, lines, log, read

from expcore.plugin import Instrument
from expcore.records.instruments import check_instruments
from expcore.replay import side_files as side_module
from expcore_onedata.readlog import instrument as instrument_module
from expcore_onedata.readlog import paths, reader
from expcore_onedata.readlog.instrument import ReadLog, snapshot

READS = [read("file-1", range=[0, 4096], bytes=4096, sha256=SHA), read("file-2"), read("file-1")]


class Source:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    def read_log(self):
        self.calls += 1
        return self.data


def finished(data, **options):
    source = Source(data)
    made = ReadLog(source, options.pop("nonce", NONCE), options.pop("expected_peer_uid", PEER_UID)).finish()
    return made, source


def test_o91_an_intact_log_gives_exact_counts_and_the_hash_of_its_bytes():
    """O91: an intact log gives exact counts and the hash of its bytes."""
    data = log(READS)
    made, source = finished(data)
    assert made == {
        "version": 1,
        "log": "intact",
        "unknown": [],
        "canary": True,
        "counts": {"reads": 3, "canaries": 1},
        "records": 3,
        "side_sha256": hashlib.sha256(data).hexdigest(),
        "side_bytes": len(data),
    }
    assert source.calls == 1
    json.dumps(made)  # the record holds it as JSON


def test_o91_a_log_of_a_run_that_read_nothing_is_intact_with_zero_reads():
    """O91: a log of a run that read nothing is intact with zero reads."""
    made, _ = finished(log([]))
    assert made["log"] == "intact"
    assert made["counts"] == {"reads": 0, "canaries": 1}
    assert made["records"] == 0


def test_o91_a_log_with_no_end_line_is_unknown_with_no_counts_and_still_has_its_hash():
    """O91: unknown is never zero: `counts` is None, `records` a lower bound, and the log is still the side file, so
    that what was read can be looked at."""
    data = log(READS, end=False)
    made, _ = finished(data)
    assert made["log"] == "unknown"
    assert made["unknown"] == [reader.NOT_CLOSED]
    assert made["counts"] is None
    assert made["records"] == 3
    assert made["canary"] is True
    assert made["side_sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("data", [None, b""])
def test_o91_a_missing_or_empty_log_is_unknown_has_no_side_file_when_missing_and_is_never_zero(data):
    """O91: a missing or empty log is unknown has no side file when missing and is never zero."""
    made, _ = finished(data)
    assert made["log"] == "unknown"
    assert made["counts"] is None
    assert made["records"] == 0
    assert made["canary"] is False
    assert made["unknown"] == [reader.MISSING if data is None else reader.EMPTY]
    assert made["side_sha256"] == (None if data is None else hashlib.sha256(b"").hexdigest())
    assert made["side_bytes"] == (None if data is None else 0)


def test_o91_a_log_with_no_canary_is_unknown_and_says_the_canary_was_missing():
    """O91: a log with no canary is unknown and says the canary was missing."""
    made, _ = finished(log(READS, with_canary=False))
    assert made["log"] == "unknown"
    assert made["canary"] is False
    assert made["unknown"] == [reader.NO_CANARY]
    assert made["counts"] is None


def test_o91_a_log_of_another_run_or_another_peer_rule_is_unknown():
    """O91: a log of another run or another peer rule is unknown."""
    assert finished(log(READS), nonce="ee" * 16)[0]["unknown"] == [reader.NOT_THIS_RUN]
    assert finished(log(READS), expected_peer_uid=PEER_UID + 1)[0]["unknown"] == [
        reader.WRONG_PEER_RULE,
        reader.FOREIGN,
    ]


def test_o91_every_reason_the_reader_finds_is_listed_once():
    """O91: every reason the reader finds is listed once."""
    out = lines(READS, malformed=2, unwritten=4, cap_reached=True, sender_uid=PEER_UID + 1)
    made, _ = finished(encode(out))
    assert made["log"] == "unknown"
    assert len(made["unknown"]) == len(set(made["unknown"]))
    assert {reader.MALFORMED, reader.UNWRITTEN, reader.CAP_REACHED, reader.FOREIGN} <= set(made["unknown"])


def test_o91_a_log_too_large_to_be_a_side_file_is_unknown_and_has_no_side_hash(monkeypatch):
    """O91: a file over the side-file cap cannot be staged, so the record could not name it: unknown, with no hash."""
    data = log(READS)
    monkeypatch.setattr(side_module, "MAX_SIDE_FILE_BYTES", len(data) - 1)
    made, _ = finished(data)
    assert made["log"] == "unknown"
    assert instrument_module.SIDE_TOO_LARGE in made["unknown"]
    assert made["side_sha256"] is None
    assert made["counts"] is None


def test_o91_the_collectors_cap_is_below_the_side_file_cap():
    """O91: a log the collector stops at its cap is always stageable."""
    assert paths.MAX_LOG_BYTES + paths.END_RESERVE_BYTES < side_module.MAX_SIDE_FILE_BYTES


def test_c61_the_side_file_takes_a_log_as_large_as_the_collector_may_write():
    """C61: a log of exactly the collector's cap, 256 MiB, is stageable: the snapshot names it as the side file, with no
    `side_file_too_large`. The length is what the decision reads (hashing 256 MiB would cost seconds for nothing), so a
    one-byte `bytes` that reports the cap's length stands for the log."""

    class FullLog(bytes):
        def __len__(self):
            return paths.MAX_LOG_BYTES

    made = snapshot(reader.Reading((), ()), FullLog(b"x"))
    assert paths.MAX_LOG_BYTES == 256 * 1024 * 1024
    assert instrument_module.SIDE_TOO_LARGE not in made["unknown"]
    assert made["side_bytes"] == paths.MAX_LOG_BYTES
    assert made["side_sha256"] is not None


def test_c61_the_side_file_cap_is_272_mib_the_collectors_cap_and_the_headroom_the_two_had():
    """C61: the side-file cap is raised with the collector's: 16 MiB above the collector's 256 MiB, as 64 MiB was above
    48 MiB, and above the longest log the collector can write with its reserve."""
    assert side_module.MAX_SIDE_FILE_BYTES == paths.MAX_LOG_BYTES + 16 * 1024 * 1024 == 272 * 1024 * 1024
    assert paths.MAX_LOG_BYTES + paths.END_RESERVE_BYTES < side_module.MAX_SIDE_FILE_BYTES


def test_o91_the_snapshot_of_a_reading_is_a_function_of_the_reading_and_the_bytes():
    """O91: the snapshot of a reading is a function of the reading and the bytes."""
    data = log(READS)
    reading = reader.read_log(data, NONCE, expected_peer_uid=PEER_UID)
    assert snapshot(reading, data) == finished(data)[0]


def test_o91_the_instrument_is_a_core_instrument_in_its_own_namespace():
    """O91: the instrument is a core instrument in its own namespace."""
    instrument = ReadLog(Source(None), NONCE, PEER_UID)
    assert instrument.namespace == "read_log" == paths.NAMESPACE
    assert isinstance(instrument, Instrument)
    check_instruments([instrument])
    assert instrument.start({"run_key": "r"}) is None


@pytest.mark.parametrize(
    ("nonce", "uid"),
    [("", PEER_UID), (None, PEER_UID), (NONCE, -1), (NONCE, "1000"), (NONCE, True), (NONCE, 2**33)],
)
def test_o91_the_instrument_refuses_a_nonce_or_uid_that_is_not_one(nonce, uid):
    """O91: the instrument refuses a nonce or uid that is not one."""
    with pytest.raises((TypeError, ValueError)):
        ReadLog(Source(None), nonce, uid)


def test_o91_the_instrument_refuses_a_source_that_cannot_read_a_log():
    """O91: the instrument refuses a source that cannot read a log."""
    with pytest.raises(TypeError, match="read_log"):
        ReadLog(object(), NONCE, PEER_UID)


def test_o91_a_source_that_raises_is_not_hidden():
    """O91: a source that raises is not hidden."""

    class Broken:
        def read_log(self):
            raise OSError("gone")

    with pytest.raises(OSError, match="gone"):
        ReadLog(Broken(), NONCE, PEER_UID).finish()


def test_o91_a_source_that_returns_text_is_refused():
    """O91: a source that returns text is refused."""
    with pytest.raises(TypeError, match="bytes"):
        finished("a string, not bytes")


def test_o91_the_study_declares_the_side_file_by_the_names_in_paths():
    """O91: the study declares the side file by the names in paths."""
    assert paths.SIDE_HASH_FIELD == "instruments.read_log.side_sha256"
    snap = finished(log(READS))[0]
    record = {"instruments": {paths.NAMESPACE: snap}}
    assert side_module.named_hash(record, paths.SIDE_HASH_FIELD) == snap["side_sha256"]
