"""F02: a turn names the deployment that served it, as an optional field of version 3, `deployment_id`.

A front that spreads one model over several deployments names the one that answered in a response header
(`deployment_header` in the model entry). The adapter reads it on every reply and the record keeps it on the turn,
beside `system_fingerprint`. The field is optional: a turn whose reply named none has none (never a null), a record
written before it loads, writes back and replays to its own bytes, and a replay serves it as recorded.
"""

import dataclasses

import pytest
from loop_support import call, turn
from record_support import hand_made, record_run

from expcore.records.record import RunRecord, decode
from expcore.records.schema import SCHEMAS, RecordError, fields_of, optional_in, validate
from expcore.replay.replay import replay_record

V2, V3 = "expcore-record/2", "expcore-record/3"


def recorded(first="dep-1", second=None, **options) -> RunRecord:
    """A run of two turns: a tool round whose reply named `first`, and an answer that named `second` (None: none)."""
    script = [
        dataclasses.replace(turn(call("call-1", "echo")), deployment_id=first),
        dataclasses.replace(turn(text="done"), deployment_id=second),
    ]
    return record_run(script, {"echo": lambda args: "echoed"}, **options)


def test_f02_a_turn_that_names_a_deployment_writes_it_last_and_one_that_names_none_writes_no_key():
    record = recorded()
    first, second = record.transcript
    assert first["deployment_id"] == "dep-1"
    assert list(first)[-1] == "deployment_id"  # after every field the earlier merge points wrote
    assert "deployment_id" not in second  # absent, never a null
    text = record.dumps()
    assert validate(decode(text)) == V3
    assert RunRecord.from_json(decode(text)).dumps() == text


def test_f02_the_field_is_optional_in_version_three_and_in_no_earlier_version():
    (v3,) = (s for s in SCHEMAS if s.name == V3)
    assert v3.added["turn"] == ("deployment_id",)
    assert optional_in("turn", V3) == {"deployment_id"}
    assert "deployment_id" not in fields_of("turn", V2)
    assert list(fields_of("turn", V3))[-1] == "deployment_id"


def test_f02_a_record_of_version_two_is_built_without_it_and_one_that_holds_it_is_refused():
    record = recorded(record_options={"schema_version": V2})
    assert all("deployment_id" not in t for t in record.transcript)
    data = decode(record.dumps())
    assert validate(data) == V2
    data["transcript"][0]["deployment_id"] = "dep-1"
    with pytest.raises(RecordError, match=r"transcript\[0\]: unknown field\(s\) \['deployment_id'\]"):
        validate(data)


@pytest.mark.parametrize("bad", [None, "", " dep", "dep ", "x" * 129, 7, True, ["dep"], "a\nb"])
def test_f02_a_deployment_that_is_not_plain_printable_text_of_at_most_128_characters_is_refused(bad):
    data = decode(recorded().dumps())
    data["transcript"][0]["deployment_id"] = bad
    with pytest.raises(RecordError, match="deployment_id"):
        validate(data)


def test_f02_a_record_of_turns_that_name_deployments_replays_to_its_own_bytes_and_one_without_gains_none():
    """F02 with R03: the replay serves each turn's deployment as recorded, and fills none in where there is none."""
    with_ids = recorded("dep-1", "dep-2")
    assert [t["deployment_id"] for t in with_ids.transcript] == ["dep-1", "dep-2"]
    assert replay_record(with_ids).dumps() == with_ids.dumps()
    without = hand_made()
    assert "deployment_id" not in without.transcript[0]
    assert replay_record(recorded(None, None)).dumps() == recorded(None, None).dumps()
    assert all("deployment_id" not in t for t in recorded(None, None).transcript)
