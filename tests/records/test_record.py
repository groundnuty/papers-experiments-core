"""The record file: atomic write, byte-for-byte read-back, what an older schema never receives (R01, R10)."""

import copy
import json
import os

import pytest
from record_support import hand_made, record_run

from expcore.records.record import RunRecord, decode, load_record, record_filename
from expcore.records.schema import RecordError, SchemaVersion

V1 = SchemaVersion("expcore-record/1")
V2 = SchemaVersion(
    "expcore-record/2",
    added={"record": ("instruments",), "turn": ("request_sha256",)},
    config_before={"cleanup_deadline_s": 60.0},
)
TABLE = (V1, V2)


def older_record() -> RunRecord:
    """A record of the older version of TABLE: the fields version 2 added are left out, as they were then."""
    base = hand_made(schema_version="expcore-record/1")
    turn = {k: v for k, v in base.transcript[0].items() if k != "request_sha256"}
    return RunRecord(**{**base.__dict__, "transcript": [turn]})


def test_the_file_is_named_by_the_digest_of_the_run_key_and_the_attempt():
    record = hand_made()
    name = record.filename
    digest, attempt, suffix = name.split(".")
    assert (len(digest), attempt, suffix) == (64, "1", "json")
    assert name == record_filename(record.run_key, 1)
    assert record_filename("other", 1) != name
    with pytest.raises(RecordError, match="non-negative integer"):
        record_filename("k", -1)


def test_R01_a_written_record_is_read_back_to_the_same_bytes(tmp_path):
    """R01: load and dump of a record is byte-identical."""
    path = hand_made().write(tmp_path)
    stored = path.read_bytes()
    loaded = load_record(path)
    assert loaded == hand_made()
    assert loaded.dumps().encode("utf-8") == stored
    assert not stored.endswith(b"\n")


def test_R01_a_record_of_a_real_run_writes_back_to_the_same_bytes(tmp_path):
    """R01: the same holds for a record the loop produced, with tool calls and a cut."""
    record = record_run([[("echo", {"x": "a" * 500})], "done"], {"echo": lambda a: a["x"]})
    stored = record.write(tmp_path).read_bytes()
    assert any(call["truncated"] for call in record.tool_calls)
    assert load_record(tmp_path / record.filename).dumps().encode("utf-8") == stored


def test_R01_unicode_and_an_unpaired_surrogate_survive_the_round_trip(tmp_path):
    """R01: non-ASCII text is written as it is; an unpaired surrogate as its escape; both read back the same."""
    record = hand_made(totals={**hand_made().totals, "final_text": "café ☃ \ud800 end"})
    stored = record.write(tmp_path).read_bytes()
    assert b"\\ud800" in stored
    assert "café".encode() in stored
    assert load_record(tmp_path / record.filename).dumps().encode("utf-8") == stored


def test_R01_the_write_is_atomic_no_temporary_file_is_left_and_a_failed_write_keeps_the_old_record(
    tmp_path, monkeypatch
):
    """R01: a temporary file, an fsync, then a rename; a failure before the rename keeps the old record, no litter."""
    first = hand_made()
    path = first.write(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    second = hand_made(totals={**first.totals, "final_text": "second"})
    second.write(tmp_path)
    assert synced, "the temporary file is fsynced before the rename"
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        first.write(tmp_path)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


def test_R01_a_field_a_later_schema_added_is_never_written_into_an_older_record():
    """R01: the older record is written without the later field, and reads back to the same bytes."""
    record = older_record()
    assert "instruments" not in record.to_json(TABLE)
    text = record.dumps(TABLE)
    assert "instruments" not in json.loads(text)
    assert "request_sha256" not in text
    assert RunRecord.from_json(decode(text), TABLE).dumps(TABLE) == text


def test_R01_a_later_field_that_holds_data_cannot_be_written_into_an_older_record():
    """R01: data in a field the record's version predates is an error, never silently dropped."""
    record = RunRecord(**{**older_record().__dict__, "instruments": {"probe_a": {"x": 1}}})
    with pytest.raises(RecordError, match="predates the field"):
        record.dumps(TABLE)


def test_R02_a_record_with_an_unknown_field_fails_load_from_a_file(tmp_path):
    """R02: the file's name is in the message."""
    text = json.loads(hand_made().dumps())
    text["extra"] = 1
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(text), encoding="utf-8")
    with pytest.raises(RecordError, match=r"bad\.json: .*unknown field"):
        load_record(path)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xff\xfe", "not a JSON document"),
        (b'{"a": 1,', "not a JSON document"),
        (b'{"a": 1, "a": 2}', "duplicate key"),
        (b'{"a": NaN}', "NaN is not a JSON number"),
        (b"[1]", "a record is a JSON object"),
    ],
)
def test_R02_text_that_is_not_one_clean_record_is_refused(raw, message):
    """R02: bad bytes, truncated JSON, a repeated key (the last would hide the first) and NaN are refused."""
    with pytest.raises(RecordError, match=message):
        RunRecord.from_json(decode(raw))


def test_R10_a_missing_counter_serialises_as_null_never_zero():
    """R10: usage a reply did not carry is null in the file."""
    record = record_run([[("echo", {})]], {"echo": lambda a: "x"})
    usage = json.loads(record.dumps())["transcript"][0]["usage"]
    assert usage["uncached_input_tokens"] is None
    assert usage["cache_read_input_tokens"] is None
    assert isinstance(usage["input_tokens"], int)


def test_a_record_is_immutable_and_with_instrument_returns_a_new_one():
    record = hand_made()
    with pytest.raises(AttributeError):
        record.attempt = 2  # type: ignore[misc]
    bigger = record.with_instrument("probe_a", {"bytes": 12})
    assert record.instruments == {}
    assert bigger.instruments == {"probe_a": {"bytes": 12}}
    assert RunRecord.from_json(decode(bigger.dumps())) == bigger


def test_writing_a_record_with_a_non_finite_number_is_refused():
    record = hand_made(totals={**hand_made().totals, "agent_s": float("inf")})
    with pytest.raises(RecordError, match="agent_s"):
        record.dumps()


def test_the_dumped_record_is_independent_of_the_dictionaries_the_caller_keeps():
    record = hand_made()
    before = record.dumps()
    mutated = copy.deepcopy(record.to_json())
    mutated["factors"]["task"] = "other"
    assert record.dumps() == before
