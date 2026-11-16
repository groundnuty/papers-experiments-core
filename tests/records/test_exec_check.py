"""O55: each code cell's call keeps what its completion record showed, as an optional field of the call.

A code tool's exec is verified before its result reaches the model (`expcore_onedata.sandbox.completion`): its program
arrived whole, a record with the exec's nonce came after the interpreter exited, and its exit status is the one docker
reported. The tool hands those facts on (`ToolOutput.exec_check`), and the record keeps them on the call
(`tool_calls[i].exec_check`: `source_whole`, `exit_status`, `nonce_matched`), so a study's report can show that every
scored cell was verified. The field is optional in version 3: a call whose tool says nothing of it has none (never a
null), a record written before it loads, writes back and replays to its own bytes, and a replay writes it exactly as
the stored record holds it.
"""

import dataclasses

import pytest
from loop_support import FakeProvider
from record_support import CODE, record_run

from expcore.plugin import ToolOutput
from expcore.records.record import RunRecord, decode
from expcore.records.schema import SCHEMAS, RecordError, fields_of, optional_in, validate
from expcore.replay.check import check_record, verify_batch
from expcore.replay.replay import replay_record
from expcore.study import verified

V2, V3 = "expcore-record/2", "expcore-record/3"
CHECKED = {"source_whole": True, "exit_status": 0, "nonce_matched": True}
SCRIPT = [[("python", {"x": "print(1)"}), ("echo", {"x": "b"})], "done"]


def recorded(exec_check=CHECKED, **options) -> RunRecord:
    """A record of a run with a code-like call that reports its check and a call of another tool that reports none."""
    handlers = {
        "python": lambda args: ToolOutput("1\n", exec_check=exec_check),
        "echo": lambda args: "echoed",
    }
    return record_run(SCRIPT, handlers, **options)


def test_o55_a_calls_check_is_its_last_field_and_a_call_without_one_has_none():
    record = recorded()
    cell, other = record.tool_calls
    assert cell["exec_check"] == CHECKED
    assert list(cell)[-1] == "exec_check"  # after every field the earlier merge points wrote
    assert "exec_check" not in other  # absent, never a null
    text = record.dumps()
    assert validate(decode(text)) == V3
    assert RunRecord.from_json(decode(text)).dumps() == text


def test_o55_a_check_whose_facts_are_unknown_keeps_them_as_nulls():
    """O55: a cell the agent's own time limit ended has no completion record: nothing came with its nonce, and what
    arrived and how it exited are unknown."""
    unknown = {"source_whole": None, "exit_status": None, "nonce_matched": False}
    record = recorded(unknown)
    assert record.tool_calls[0]["exec_check"] == unknown
    assert validate(decode(record.dumps())) == V3


def test_o55_the_field_is_optional_in_version_three_and_in_no_earlier_version():
    (v3,) = (s for s in SCHEMAS if s.name == V3)
    assert "exec_check" in v3.added["tool_call"]
    assert "exec_check" in optional_in("tool_call", V3)
    assert set(v3.optional["exec_check"]) == {"source_whole", "exit_status", "nonce_matched"}
    assert "exec_check" not in fields_of("tool_call", V2)


def test_o55_a_record_of_version_two_is_built_without_it_and_one_that_holds_it_is_refused():
    record = recorded(record_options={"schema_version": V2})
    assert all("exec_check" not in call for call in record.tool_calls)
    data = decode(record.dumps())
    assert validate(data) == V2
    data["tool_calls"][0]["exec_check"] = CHECKED
    with pytest.raises(RecordError, match=r"tool_calls\[0\]: unknown field\(s\) \['exec_check'\]"):
        validate(data)


@pytest.mark.parametrize(
    ("bad", "where"),
    [
        (None, r"tool_calls\[0\]\.exec_check: expected dict"),
        ({**CHECKED, "verified": True}, r"tool_calls\[0\]\.exec_check: unknown field\(s\) \['verified'\]"),
        ({**CHECKED, "exit_status": "0"}, r"tool_calls\[0\]\.exec_check\.exit_status"),
        ({**CHECKED, "source_whole": 1}, r"tool_calls\[0\]\.exec_check\.source_whole"),
        ({**CHECKED, "nonce_matched": None}, r"tool_calls\[0\]\.exec_check\.nonce_matched"),
    ],
    ids=["null", "unknown-key", "status-text", "whole-number", "nonce-null"],
)
def test_o55_a_present_check_is_an_object_of_the_three_facts_each_of_its_type(bad, where):
    data = decode(recorded().dumps())
    data["tool_calls"][0]["exec_check"] = bad
    with pytest.raises(RecordError, match=where):
        validate(data)


def test_o55_a_record_with_checks_replays_to_its_own_bytes_and_one_without_gains_none():
    """O55 with R03: the replay serves each call's check as recorded, so the replayed record holds it where the stored
    one does, and nowhere else."""
    with_check = recorded()
    assert replay_record(with_check).dumps() == with_check.dumps()
    without = record_run(SCRIPT, {"python": lambda args: "1\n", "echo": lambda args: "echoed"})
    assert all("exec_check" not in call for call in without.tool_calls)
    assert replay_record(without).dumps() == without.dumps()


def test_o55_the_replay_check_and_the_analysis_accept_records_with_and_without_checks(tmp_path):
    """O55: the replay check passes over a batch that holds both, and the analysis, which reads only verified bytes,
    parses both (`study.verified`). The export reads the campaign store, never a record, so it is untouched."""
    batch = tmp_path / "batch"
    records = [
        dataclasses.replace(recorded(), run_key="with-check"),
        dataclasses.replace(record_run(SCRIPT, {"python": lambda a: "1\n", "echo": lambda a: "e"}), run_key="without"),
    ]
    for record in records:
        record.write(batch)
        raw = (batch / record.filename).read_bytes()
        assert check_record(raw) is None
        where = f"{record.filename.split('.')[0]}/try-1/{record.filename}"  # where a campaign's batch keeps it
        assert verified._parse(where, raw).dumps().encode("utf-8") == raw
    check = verify_batch(batch, code=CODE)
    assert (check["n_records"], check["n_ok"], check["failures"]) == (2, 2, [])


def test_o55_the_provider_refuses_a_check_that_is_not_an_object():
    with pytest.raises(ValueError, match="exec_check"):
        ToolOutput("x", exec_check=["not", "an", "object"])


def test_o55_a_fake_provider_hands_the_check_through_the_loop_unchanged():
    provider = FakeProvider({"python": lambda args: ToolOutput("", exec_check=CHECKED)})
    record = record_run([[("python", {"x": "pass"})], "done"], {}, providers=[provider])
    assert record.tool_calls[0]["exec_check"] == CHECKED
