"""The merge-point fixtures of record schema 3: a record written at any earlier merge point still validates, loads,
writes back and replays byte for byte under the newest code (R01, R02, R03).

Until v0.1.0 is tagged, `expcore-record/3` is extended by several changes, and live runs write version-3 records before
each one lands. The rule (docs/interfaces.md, the bullet "Extending version 3 before v0.1.0" of "The run record's
schema") is that every extension is strictly additive: a new field is optional, an absent field means "not recorded",
and no existing field changes its meaning or its validation. Two things hold the rule.

Structural tests over the schema itself, whatever the fixtures happen to hold. A `failed_request`, an outcome's
`defect` and the malformed events of the totals are objects a fixture may hold none of, and each is a section of
`SECTIONS` (`failed_request`, `defect`, `malformed_event`), so its keys are versioned like any other. Every field
version 3 adds is listed in its `optional`. And the fields version 3 had at its first merge point, frozen in
`tests/data/record-v3-fields.json`, keep their types, their order and whether they are required, while every field not
in that file is optional: so a required key added to a section, whether or not the version lists it in `added`, fails
here. The objects outside `SECTIONS` are held otherwise. Every record holds a `code` identity and an `agent_config`
with its `limits`, so the fixtures catch a required key added to them; and every key of a retry entry may be left out
(`RETRY_FIELDS`), so a new one is additive as it stands.

And the fixtures: the directory `tests/data/record-v3/` holds one per merge point, numbered in the order they merged,
each written by the code of that merge. A branch that extends version 3 adds the next fixture, written by its own code,
and never edits an earlier one: a change that is not additive makes the earlier fixtures fail here, for the shapes they
hold.
"""

import copy
import dataclasses
import json
import re
from pathlib import Path

import pytest

from expcore.records.injections import FAULT_NAMESPACE, injected_targets
from expcore.records.record import RunRecord, decode, load_record
from expcore.records.schema import (
    RETRY_FIELDS,
    SCHEMAS,
    SECTIONS,
    RecordError,
    SchemaVersion,
    fields_of,
    optional_in,
    reply_count_rule,
    target_rule,
    validate,
)
from expcore.replay.check import check_record
from expcore.replay.replay import replay_record

DATA = Path(__file__).resolve().parents[1] / "data"
DIRECTORY = DATA / "record-v3"
FIXTURES = sorted(DIRECTORY.glob("*.json"))
NAME = re.compile(r"(\d\d)-[a-z0-9]+(?:-[a-z0-9]+)*\.json")
V3 = "expcore-record/3"
# Version 3 as its first merge point defined it: each section's fields in file order with their types, and the
# fields it marked optional. Frozen like the fixtures: an extension adds optional fields, and never edits this file.
FIRST_FIELDS = json.loads((DATA / "record-v3-fields.json").read_text(encoding="utf-8"))


def required_additions(version: SchemaVersion) -> dict[str, list[str]]:
    """The fields a version adds that it does not mark optional, by section: the ones a record written before it
    would lack. Empty for a version whose additions are all optional."""
    required = {
        section: sorted(set(names) - set(version.optional.get(section, ()))) for section, names in version.added.items()
    }
    return {section: names for section, names in required.items() if names}


def departures(version: str, first: dict, schemas: tuple[SchemaVersion, ...] = SCHEMAS) -> list[str]:
    """What a version's sections, as the code now defines them, change in the fields of its first merge point: a field
    removed, retyped, moved, or made required or optional, and a new field that is required. Empty when every change
    since is an optional field added."""
    found = []
    for section, fields in first["sections"].items():
        now = fields_of(section, version, schemas) if section in SECTIONS else {}
        types = {name: " | ".join(t.__name__ for t in held) for name, held in now.items()}
        was_optional, is_optional = set(first["optional"].get(section, ())), optional_in(section, version, schemas)
        for name in fields:
            if name not in types:
                found.append(f"{section}.{name} is removed")
            elif types[name] != fields[name]:
                found.append(f"{section}.{name} is retyped")
            elif (name in was_optional) != (name in is_optional):
                found.append(f"{section}.{name} changed whether it is required")
        if [name for name in types if name in fields] != [name for name in fields if name in types]:
            found.append(f"{section}: its fields are in another order")
    for section in SECTIONS:
        known, is_optional = first["sections"].get(section, {}), optional_in(section, version, schemas)
        found += [
            f"{section}.{name} is a new required field"
            for name in fields_of(section, version, schemas)
            if name not in known and name not in is_optional
        ]
    return found


def test_R02_every_field_version_three_adds_is_optional_in_every_section_whatever_the_fixtures_hold():
    """R02: until v0.1.0 an extension of version 3 is additive, so no section of it may gain a required field. This
    holds over the schema itself, so a required field in a section the fixtures leave empty (a null `failed_request`,
    an outcome's defect, the malformed events of the totals, each a section) is caught too, though every fixture
    would still validate: a live version-3 record of that shape would fail to load."""
    (v3,) = (s for s in SCHEMAS if s.name == V3)
    assert required_additions(v3) == {}


def test_R02_the_fields_of_version_three_at_its_first_merge_point_keep_their_types_order_and_whether_required():
    """R02: a field added to a section without listing it in `added` is required in every version, the older ones too,
    and a field retyped, removed or made required changes the validation of records already written. Each is caught
    over the schema itself, in every section, whatever the fixtures hold; an optional field added is not."""
    assert departures(V3, FIRST_FIELDS) == []


def test_R02_the_structural_check_names_the_required_field_a_version_adds_to_a_section_with_no_fixture():
    """R02: the check is not vacuous: a version that adds a field to `failed_request` or to an outcome's `defect` and
    leaves it required is named, where one that marks it optional, or adds nothing there, is not."""
    required = SchemaVersion("expcore-record/3", added={"failed_request": ("pattern",), "usage": ("extra",)})
    assert required_additions(required) == {"failed_request": ["pattern"], "usage": ["extra"]}
    mixed = SchemaVersion(
        "expcore-record/3",
        added={"failed_request": ("pattern",), "usage": ("extra",)},
        optional={"usage": ("extra",)},
    )
    assert required_additions(mixed) == {"failed_request": ["pattern"]}
    optional = SchemaVersion("expcore-record/3", added={"usage": ("extra",)}, optional={"usage": ("extra",)})
    assert required_additions(optional) == {}
    assert required_additions(SchemaVersion("expcore-record/3")) == {}
    defect = SchemaVersion("expcore-record/3", added={"defect": ("exception",)})
    assert required_additions(defect) == {"defect": ["exception"]}


@pytest.mark.parametrize(("section", "key"), [("defect", {"frame": (str,)}), ("malformed_event", {"column": (int,)})])
def test_R02_the_frozen_fields_name_a_required_key_added_to_a_section_the_fixtures_leave_empty(
    monkeypatch, section, key
):
    """R02: the frozen fields are not vacuous: a key added to an outcome's defect or to a malformed event, and listed in
    no version's `added`, is required in every version and is named, though no fixture holds either object and every
    one still validates."""
    monkeypatch.setitem(SECTIONS, section, {**SECTIONS[section], **key})
    (name,) = key
    assert departures(V3, FIRST_FIELDS) == [f"{section}.{name} is a new required field"]
    for path in FIXTURES:
        validate(decode(path.read_bytes()))


def test_R02_the_frozen_fields_pass_an_optional_field_added_and_name_a_field_retyped_or_made_optional(monkeypatch):
    """R02: an extension that adds an optional field departs from nothing; one that retypes a field of the first merge
    point, or makes a required one optional, is named."""
    monkeypatch.setitem(SECTIONS, "defect", {**SECTIONS["defect"], "frame": (str,)})
    v3 = SCHEMAS[2]
    added = {**v3.added, "defect": ("frame",)}
    extended = (*SCHEMAS[:2], dataclasses.replace(v3, added=added, optional={**v3.optional, "defect": ("frame",)}))
    assert departures(V3, FIRST_FIELDS, extended) == []
    required = (*SCHEMAS[:2], dataclasses.replace(v3, added=added))
    assert departures(V3, FIRST_FIELDS, required) == ["defect.frame is a new required field"]
    monkeypatch.undo()
    retyped = {**FIRST_FIELDS["sections"], "defect": {"exception": "int", "stack_sha256": "str"}}
    assert departures(V3, {**FIRST_FIELDS, "sections": retyped}) == ["defect.exception is retyped"]
    loosened = {**FIRST_FIELDS["optional"], "defect": ["exception"]}
    assert departures(V3, {**FIRST_FIELDS, "optional": loosened}) == ["defect.exception changed whether it is required"]


def test_R02_the_fixtures_are_numbered_from_one_in_merge_order_with_none_missing():
    """R02: the first merge point has its fixture, and a later one cannot be added out of order or replace one."""
    assert FIXTURES, f"no fixture in {DIRECTORY}: the merge point of version 3's first extension has one"
    matches = [NAME.fullmatch(path.name) for path in FIXTURES]
    assert all(matches), f"fixture names are NN-topic.json: {[p.name for p in FIXTURES if not NAME.fullmatch(p.name)]}"
    assert [int(m.group(1)) for m in matches if m] == list(range(1, len(FIXTURES) + 1))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_R01_every_merge_point_fixture_is_version_three_validates_and_writes_back_byte_for_byte(path):
    """R01: a record written at an earlier merge point of version 3 is still valid under the newest schema, loads, and
    writing it back gives its bytes: an extension that added a required field or changed a check fails here."""
    stored = path.read_bytes()
    assert validate(decode(stored)) == "expcore-record/3" == SCHEMAS[2].name
    record = load_record(path)
    assert record.dumps().encode("utf-8") == stored
    assert RunRecord.from_json(decode(stored)).dumps() == stored.decode("utf-8")


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_R03_every_merge_point_fixture_replays_byte_for_byte_under_the_newest_code(path):
    """R03: the replay re-runs the loop on the record and writes the same bytes, and the check stages pass: a field the
    older writer left out is not filled in by the newer code, as a null or as a zero."""
    stored = path.read_bytes()
    replayed = replay_record(load_record(path))
    assert replayed.schema_version == "expcore-record/3"
    assert replayed.dumps().encode("utf-8") == stored
    assert check_record(stored) is None


def test_R02_the_first_fixture_pins_the_extra_usage_fields_present_absent_reported_empty_and_cut():
    """R02: the first merge point added `usage.extra` and `usage.extra_cut`, both optional: the fixture has a turn
    with fields, a turn whose backend reported none (both absent), a turn that reported an empty set, and a cut."""
    first = [p for p in FIXTURES if p.name.startswith("01-")]
    assert [p.name for p in first] == ["01-usage-extra.json"]
    usages = [turn["usage"] for turn in load_record(first[0]).transcript]
    assert [u.get("extra") for u in usages] == [
        {"completion_tokens_details.reasoning_tokens": 7, "used_credits": 1.5},
        None,
        {},
        {"used_credits": 0.25},
    ]
    assert [u.get("extra_cut") for u in usages] == [0, None, 0, 3]
    assert "extra" not in usages[1]
    assert "extra_cut" not in usages[1]


def test_C26_the_second_fixture_pins_a_billed_reply_that_became_no_turn_as_incomplete_usage():
    """C26 and R03: version 3 is the first under the reply-count rule, which changes what a version-3 record writes, so
    the second fixture was written by the code that applies it: a tool round, then a billed reply the adapter counted
    and could not read. The backend counted one reply more than the record kept turns, the totals say the usage is
    incomplete, and the record replays to those bytes."""
    second = [p for p in FIXTURES if p.name.startswith("02-")]
    assert [p.name for p in second] == ["02-billed-unread-reply.json"]
    record = load_record(second[0])
    assert reply_count_rule(record.schema_version)
    assert record.route["usage_seen"]["responses"] == len(record.transcript) + 1 == 2
    assert record.failed_request["kind"] == "infra"
    assert record.totals["usage_incomplete"] is True


def test_C01_the_third_fixture_pins_a_targeted_fault_with_the_injectors_reading_and_its_verified_restore():
    """C01 and R02: the third merge point added the target of a fault, `outcome.infra_target` and `treatment_targets`,
    both optional, with version 3's target rule. Its fixture, written by that code on the record tests' fake clock, is
    a run whose tool server failed naming the declared target after one completed turn, with the fault injector's
    reading of that target injected, cleared and verified in this attempt: it is treatment, not set aside, and both
    fields come last in their sections. The earlier fixtures hold neither field."""
    third = [p for p in FIXTURES if p.name.startswith("03-")]
    assert [p.name for p in third] == ["03-fault-target.json"]
    record = load_record(third[0])
    assert target_rule(record.schema_version)
    assert len(record.transcript) == 1
    assert record.treatment_kinds == ["knowledge"]
    assert record.treatment_targets == {"knowledge": "servers[1]"}
    assert (record.outcome["infra_kind"], record.outcome["infra_target"]) == ("knowledge", "servers[1]")
    assert (record.outcome["treatment"], record.set_aside) == (True, False)
    reading = record.instruments[FAULT_NAMESPACE]
    assert injected_targets(record.instruments, record.run_key, record.attempt) == {"servers[1]"}
    assert reading["injections"] == [{"target": "servers[1]", "injected": True, "cleared": True, "verified": True}]
    assert [step["step"] for step in reading["steps"]] == ["sanctioned", "injected", "cleared", "verified"]
    assert reading["steps"][-1]["identity_changed"] is True
    data = decode(third[0].read_bytes())
    assert list(data)[-1] == "treatment_targets"
    assert list(data["outcome"])[-1] == "infra_target"
    for earlier in FIXTURES[:2]:
        held = decode(earlier.read_bytes())
        assert "treatment_targets" not in held
        assert "infra_target" not in held["outcome"]


def test_R02_the_target_fields_are_absent_when_there_is_none_never_null_or_empty():
    """R02 and R10: a version-3 record that holds `treatment_targets` as an empty table, or `infra_target` as null,
    would not write back to its own bytes, since the writer leaves both out when there is nothing to say, so both are
    refused. The third fixture with both fields taken out is a record of a condition that declares the kind with no
    target, which the kind and the first turn alone decide: still valid, still treatment."""
    data = decode(FIXTURES[0].read_bytes())
    empty = {**copy.deepcopy(data), "treatment_targets": {}}
    with pytest.raises(RecordError, match="treatment_targets: a condition that declares no target"):
        validate(empty)
    null = copy.deepcopy(data)
    null["outcome"]["infra_target"] = None
    with pytest.raises(RecordError, match=r"outcome\.infra_target: expected str"):
        validate(null)
    third = decode(next(p for p in FIXTURES if p.name.startswith("03-")).read_bytes())
    untargeted = {k: v for k, v in copy.deepcopy(third).items() if k != "treatment_targets"}
    del untargeted["outcome"]["infra_target"]
    assert validate(untargeted) == V3
    assert untargeted["outcome"]["treatment"] is True


def test_c31_the_fourth_fixture_pins_an_instruments_refusal_after_the_run_and_the_first_turn_reading_in_the_clock():
    """C31 and R03: the fourth merge point added the optional `instrument_refusal` of a record, and a clock reading the
    executor takes for an instrument's first-turn hook before the loop starts its clock. The fixture was written by the
    code of that merge: a finished attempt whose instrument then refused it with a kind no condition can declare. It
    replays to its own bytes, with the executor's reading and the refusal served back as recorded, and it holds none
    of the target fields of the third merge point, which the condition did not declare."""
    fourth = [p for p in FIXTURES if p.name.startswith("04-")]
    assert [p.name for p in fourth] == ["04-instrument-refusal.json"]
    record = load_record(fourth[0])
    assert record.outcome["name"] == "finished"  # what the loop saw; the refusal came after the run
    assert record.instrument_refusal == {
        "namespace": "manipulation",
        "infra_kind": "tool_plane_integrity",
        "detail": "the arriving files were not in their declared start state",
    }
    assert record.set_aside is True
    assert [label for label, _ in record.clock][:2] == ["instrument_first_turn", "start"]
    assert record.instruments["manipulation"]["start_state_clock_s"] == record.clock[0][1]
    data = decode(fourth[0].read_bytes())
    assert list(data)[-1] == "instrument_refusal"
    assert "treatment_targets" not in data
    assert "infra_target" not in data["outcome"]
    for earlier in FIXTURES[:3]:  # earlier records stay without it
        assert "instrument_refusal" not in decode(earlier.read_bytes())


@pytest.mark.parametrize(
    "entry",
    [{}, dict.fromkeys(RETRY_FIELDS)],
    ids=["no-key", "every-key-null"],
)
def test_R02_a_retry_entry_of_version_three_needs_no_key_and_takes_any_value(entry):
    """R02: retry entries are checked by a key list rather than a section, so neither the frozen fields nor the
    fixtures (whose turns hold no retry) would see a later branch make a retry key required or typed: a version 3
    record whose retry entry holds no key, or every key null (a connection error writes no status), stays valid."""
    record = decode(FIXTURES[0].read_bytes())
    record["transcript"][0]["retries"] = [entry]
    assert validate(record) == V3


def test_c27_the_fifth_fixture_pins_whether_the_attempt_was_held_to_the_reservation_and_the_first_four_hold_neither():
    """C27 and R03: the fifth merge point added `totals.reserved_capped` and `totals.reply_cap_unheld`, both optional
    booleans after the last of the totals. Its fixture, written by the code of that merge on the record tests' fake
    clock, is a run the budget gate reserved by the reply maximum and whose model the campaign had since taken off it.
    It validates and replays to its own bytes (the parametrized tests above); the four fixtures before it hold neither
    field, and a record without them gains nothing."""
    fifth = [p for p in FIXTURES if p.name.startswith("05-")]
    assert [p.name for p in fifth] == ["05-reserved-capped.json"]
    record = load_record(fifth[0])
    assert (record.totals["reserved_capped"], record.totals["reply_cap_unheld"]) == (True, True)
    assert list(record.totals)[-2:] == ["reserved_capped", "reply_cap_unheld"]
    assert replay_record(record).totals == record.totals
    for earlier in FIXTURES[:4]:
        held = load_record(earlier)
        assert "reserved_capped" not in held.totals
        assert "reply_cap_unheld" not in held.totals
        assert replay_record(held).dumps().encode("utf-8") == earlier.read_bytes()


def test_l17_the_sixth_fixture_pins_a_declared_tool_server_call_bound_and_each_started_call_elapsed_time():
    """L17 and R03: the sixth merge point added `agent_config.tool_server_call_bound_s`, which a study that declares no
    bound leaves out, with the names of the tools it reached beside it (`tool_server_tools`), and the optional
    `elapsed_s` of a tool call that started. Its fixture, written by the code of that merge on the record tests' fake
    clock, declares a bound of 60 s over two tools: a code-like `echo`, which the bound does not reach, and a tool
    server's `search`. It holds a call that took 10 s, a call to the tool server that outlasted the bound and failed
    as `knowledge` infrastructure, and a call answered as skipped, which has no elapsed time. It validates and replays
    to its own bytes (the parametrized tests above); the five fixtures before it hold none of these, and a record
    without them gains nothing. (The bound was first merged as `tool_call_bound_s`, capping every call, and renamed
    before any release.)"""
    sixth = [p for p in FIXTURES if p.name.startswith("06-")]
    assert [p.name for p in sixth] == ["06-tool-call-bound.json"]
    record = load_record(sixth[0])
    assert record.agent_config["tool_server_call_bound_s"] == 60.0
    assert list(record.agent_config)[-3:] == ["tool_server_call_bound_s", "terminal_tools", "tool_server_tools"]
    assert record.agent_config["tool_server_tools"] == ["search"]  # the bound did not reach `echo`
    assert "tool_call_bound_s" not in record.agent_config
    assert [(c["status"], c.get("elapsed_s")) for c in record.tool_calls] == [
        ("ok", 10.0),
        ("failed", 60.0),
        ("skipped", None),
    ]
    assert "elapsed_s" not in record.tool_calls[2]
    assert (record.outcome["name"], record.outcome["infra_kind"]) == ("infrastructure", "knowledge")
    assert "within 60 s" in record.outcome["detail"]
    assert replay_record(record).dumps().encode("utf-8") == sixth[0].read_bytes()
    for earlier in FIXTURES[:5]:
        held = load_record(earlier)
        assert "tool_server_call_bound_s" not in held.agent_config
        assert "tool_server_tools" not in held.agent_config
        assert all("elapsed_s" not in call for call in held.tool_calls)
        assert replay_record(held).dumps().encode("utf-8") == earlier.read_bytes()
