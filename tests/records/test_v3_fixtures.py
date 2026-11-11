"""The merge-point fixtures of record schema 3: a record written at any earlier merge point still validates, loads,
writes back and replays byte for byte under the newest code (R01, R02, R03).

Until v0.1.0 is tagged, `expcore-record/3` is extended by several changes, and live runs write version-3 records before
each one lands. The rule (docs/interfaces.md, "Extending record schema 3 before v0.1.0") is that every extension is
strictly additive: a new field is optional, an absent field means "not recorded", and no existing field changes its
meaning or its validation. `tests/data/record-v3/` holds one fixture per merge point, numbered in the order they
merged, each written by the code of that merge. A branch that extends version 3 adds the next fixture, written by its
own code, and never edits an earlier one: an extension that is not additive makes the earlier fixtures fail here.
"""

import re
from pathlib import Path

import pytest

from expcore.records.record import RunRecord, decode, load_record
from expcore.records.schema import SCHEMAS, validate
from expcore.replay.check import check_record
from expcore.replay.replay import replay_record

DIRECTORY = Path(__file__).resolve().parents[1] / "data" / "record-v3"
FIXTURES = sorted(DIRECTORY.glob("*.json"))
NAME = re.compile(r"(\d\d)-[a-z0-9]+(?:-[a-z0-9]+)*\.json")


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
