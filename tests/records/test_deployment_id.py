"""B20: a turn names the deployment that served it, as an optional field of version 3, `deployment_id`.

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
    """B20 with R03: the replay serves each turn's deployment as recorded, and fills none in where there is none."""
    with_ids = recorded("dep-1", "dep-2")
    assert [t["deployment_id"] for t in with_ids.transcript] == ["dep-1", "dep-2"]
    assert replay_record(with_ids).dumps() == with_ids.dumps()
    without = hand_made()
    assert "deployment_id" not in without.transcript[0]
    assert replay_record(recorded(None, None)).dumps() == recorded(None, None).dumps()
    assert all("deployment_id" not in t for t in recorded(None, None).transcript)


def run_of(*ids) -> RunRecord:
    """A run with one turn per entry, each naming that deployment (None: its reply named none); the last answers."""
    script = []
    for index, name in enumerate(ids):
        base = turn(call(f"call-{index}", "echo")) if index < len(ids) - 1 else turn(text="done")
        script.append(dataclasses.replace(base, deployment_id=name))
    return record_run(script, {"echo": lambda args: "echoed"})


@pytest.mark.parametrize(
    ("ids", "switched"),
    [
        (("dep-1", None, "dep-1"), True),  # a front that overflowed to another service for one turn
        ((None, "dep-1"), True),
        (("dep-1", "dep-2"), True),
        ((None, None, None), False),  # a model that is never served through a front
        (("dep-1", "dep-1", "dep-1"), False),
        (("dep-1",), False),
    ],
)
def test_b20_a_record_says_its_turns_switched_deployment_when_they_show_more_than_one_and_a_turn_without_the_id_counts(
    ids, switched
):
    """B20: the totals' optional `deployment_switched` is written (true) when the turns show more than one deployment,
    the turns without the id among turns with it counting as one more, and left out otherwise; it is the last of the
    totals, validates, and replays to the record's own bytes."""
    record = run_of(*ids)
    text = record.dumps()
    if switched:
        assert record.totals["deployment_switched"] is True
        assert list(record.totals)[-1] == "deployment_switched"
    else:
        assert "deployment_switched" not in record.totals  # absent, never false
    assert validate(decode(text)) == V3
    assert replay_record(record).dumps() == text


def run_of_fingerprints(*fingerprints, ids=None) -> RunRecord:
    """A run with one turn per fingerprint (None: its reply carried none), each naming the matching entry of `ids`."""
    script = []
    for index, fingerprint in enumerate(fingerprints):
        base = turn(call(f"call-{index}", "echo")) if index < len(fingerprints) - 1 else turn(text="done")
        name = None if ids is None else ids[index]
        script.append(dataclasses.replace(base, system_fingerprint=fingerprint, deployment_id=name))
    return record_run(script, {"echo": lambda args: "echoed"})


@pytest.mark.parametrize(
    ("fingerprints", "ids", "switched"),
    [
        (("fp-a", "fp-a", "fp-b"), None, True),  # an endpoint that routes per request and sends no deployment header
        (("fp-a", None, "fp-a"), None, True),  # a reply with none among replies with one counts as one more
        ((None, None), None, False),  # an endpoint that reports no fingerprint at all
        (("fp-a", "fp-a"), None, False),
        (("fp-a", "fp-a"), ("dep-x", "dep-x"), False),  # a fleet attempt: one deployment, one fingerprint
        (("fp-a | fp-b",), None, True),  # one reply spliced from two deployments carries two fingerprints
    ],
)
def test_b20_a_record_says_its_turns_switched_when_their_fingerprints_show_more_than_one(fingerprints, ids, switched):
    """B20: the deployment id is not the only evidence: replies under more than one system fingerprint (a reply with
    none counting as one more only when other replies carry one) set the same `deployment_switched`, which is written
    only when true, validates, and replays to the record's own bytes."""
    record = run_of_fingerprints(*fingerprints, ids=ids)
    text = record.dumps()
    if switched:
        assert record.totals["deployment_switched"] is True
        assert list(record.totals)[-1] == "deployment_switched"
    else:
        assert "deployment_switched" not in record.totals  # absent, never false
    assert validate(decode(text)) == V3
    assert replay_record(record).dumps() == text


def test_b20_a_record_written_before_the_fingerprint_rule_replays_to_its_own_bytes():
    """B20: a record whose replies show two fingerprints and whose totals have no `deployment_switched` (written before
    the fingerprint rule) is replayed with the flag served as recorded, never filled in: no divergence at the totals."""
    record = run_of_fingerprints("fp-a", "fp-b")
    assert record.totals["deployment_switched"] is True
    older = dataclasses.replace(record, totals={k: v for k, v in record.totals.items() if k != "deployment_switched"})
    assert replay_record(older).dumps() == older.dumps()


def test_b20_deployment_switched_is_optional_in_version_three_and_unknown_to_version_two():
    assert "deployment_switched" in optional_in("totals", V3)
    assert "deployment_switched" not in fields_of("totals", V2)
    older = recorded(record_options={"schema_version": V2})
    assert "deployment_switched" not in older.totals  # a version that predates it is built without it
    data = decode(older.dumps())
    data["totals"]["deployment_switched"] = True
    with pytest.raises(RecordError, match=r"totals: unknown field\(s\) \['deployment_switched'\]"):
        validate(data)
    body = decode(run_of("dep-1", None).dumps())
    body["totals"]["deployment_switched"] = "yes"
    with pytest.raises(RecordError, match="deployment_switched"):
        validate(body)
