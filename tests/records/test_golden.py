"""The golden records: version 1 of the record format is frozen from its first pushed commit (R01, R02).

`tests/data/golden-record-v2.json` is the same for version 2, which adds no field but changes a rule: an infrastructure
outcome is flagged as treatment only after the model completed a turn. `tests/data/golden-record-v3.json` is the same
for version 3, which adds the target of a fault (`treatment_targets`, `outcome.infra_target`) and the target rule: a
declared target is treatment only for its own failure while the fault injector's reading shows it injected. Each file
loads, writes back byte for byte, and replays, under the rule of its own version.

`tests/data/golden-record-v1.json` is a record of a synthetic run with invented names: tool calls of every outcome, a
result the data source cut, a call the loop skipped, a secret redacted from the prompt and from a result. It must
load, write back byte for byte, and replay. If a change to the format makes this test fail, the change is to the
shape that ships: it needs a new schema version in `expcore.records.schema.SCHEMAS`, not an edit of version 1, and
the golden file stays as it is. The file was made by running the stub backend through the loop with an environment
variable named GOLDEN_API_KEY set to an invented key, and writing `record.dumps()`.
"""

from pathlib import Path

from expcore.records.injections import FAULT_NAMESPACE
from expcore.records.record import RunRecord, decode, load_record
from expcore.records.schema import SCHEMAS, first_turn_rule, target_rule
from expcore.replay.check import check_record
from expcore.replay.replay import replay_record

GOLDEN = Path(__file__).resolve().parents[1] / "data" / "golden-record-v1.json"
GOLDEN_V2 = Path(__file__).resolve().parents[1] / "data" / "golden-record-v2.json"
GOLDEN_V3 = Path(__file__).resolve().parents[1] / "data" / "golden-record-v3.json"


def test_R01_the_golden_record_of_version_one_loads_and_writes_back_byte_for_byte():
    """R01: any later change to the version-1 shape fails here."""
    stored = GOLDEN.read_bytes()
    record = load_record(GOLDEN)
    assert record.schema_version == "expcore-record/1" == SCHEMAS[0].name
    assert record.dumps().encode("utf-8") == stored
    assert RunRecord.from_json(decode(stored)).dumps() == stored.decode("utf-8")


def test_R02_the_golden_record_holds_exactly_the_fields_of_version_one():
    """R02: the top-level fields, in the order the file writes them."""
    assert list(decode(GOLDEN.read_bytes())) == [
        "schema_version",
        "run_key",
        "factors",
        "attempt",
        "treatment_kinds",
        "route",
        "prompt",
        "tools",
        "agent_config",
        "agent_config_sha256",
        "code",
        "core_version",
        "transcript",
        "failed_request",
        "tool_calls",
        "clock",
        "outcome",
        "totals",
        "instruments",
        "scores",
        "redacted_paths",
    ]


def test_R03_the_golden_record_passes_the_replay_check_stages_and_replays():
    """R03: it loads, writes back, and replays through the loop, with the redacted call served as recorded."""
    assert check_record(GOLDEN.read_bytes()) is None
    replayed = replay_record(load_record(GOLDEN))
    assert replayed.outcome["name"] == "finished"
    assert [c["rendering_verified"] for c in replayed.tool_calls].count(False) == 1


def test_R12_the_golden_record_holds_no_secret_and_lists_where_one_was_redacted():
    """R12: the invented key is in no field, and the redacted paths say where it was."""
    text = GOLDEN.read_text(encoding="utf-8")
    assert "golden-key-invented" not in text
    assert load_record(GOLDEN).redacted_paths


def test_R01_the_golden_record_of_version_two_loads_and_writes_back_byte_for_byte():
    """R01: version 2 is frozen as version 1 is, beside it: its bytes load and come back as they were."""
    stored = GOLDEN_V2.read_bytes()
    record = load_record(GOLDEN_V2)
    assert record.schema_version == "expcore-record/2" == SCHEMAS[1].name
    assert record.dumps().encode("utf-8") == stored
    assert RunRecord.from_json(decode(stored)).dumps() == stored.decode("utf-8")


def test_R02_the_two_golden_records_hold_the_same_fields_in_the_same_order():
    """R02: version 2 added no field, only the first-turn rule."""
    assert list(decode(GOLDEN_V2.read_bytes())) == list(decode(GOLDEN.read_bytes()))


def test_C01_each_golden_record_replays_under_the_rule_of_its_own_version():
    """C01: version 1 has no first-turn rule and version 2 has one. The version-2 golden is a treatment failure after
    two completed turns and replays as treatment; the version-1 golden is not an infrastructure failure at all and
    replays as it was written. Both replayed records keep their own version."""
    assert (first_turn_rule("expcore-record/1"), first_turn_rule("expcore-record/2")) == (False, True)
    v2 = load_record(GOLDEN_V2)
    assert len(v2.transcript) == 2
    assert (v2.outcome["infra_kind"], v2.outcome["treatment"], v2.set_aside) == ("knowledge", True, False)
    replayed = replay_record(v2)
    assert (replayed.schema_version, replayed.outcome["treatment"]) == ("expcore-record/2", True)
    assert replayed.dumps().encode("utf-8") == GOLDEN_V2.read_bytes()
    first = replay_record(load_record(GOLDEN))
    assert (first.schema_version, first.outcome["treatment"]) == ("expcore-record/1", False)


def test_R03_the_version_two_golden_passes_the_replay_check_stages():
    """R03: it loads, writes back and replays through the loop."""
    assert check_record(GOLDEN_V2.read_bytes()) is None


def test_R01_the_golden_record_of_version_three_loads_and_writes_back_byte_for_byte():
    """R01: version 3 is frozen beside the others: its bytes load and come back as they were."""
    stored = GOLDEN_V3.read_bytes()
    record = load_record(GOLDEN_V3)
    assert record.schema_version == "expcore-record/3" == SCHEMAS[2].name
    assert record.dumps().encode("utf-8") == stored
    assert RunRecord.from_json(decode(stored)).dumps() == stored.decode("utf-8")


def test_R02_version_three_adds_exactly_the_declared_targets_and_the_outcome_target():
    """R02: the version-3 golden holds the fields of version 2 in their order, then the declared targets; its outcome
    holds version 2's fields and then the target."""
    v2, v3 = decode(GOLDEN_V2.read_bytes()), decode(GOLDEN_V3.read_bytes())
    assert list(v3) == [*v2, "treatment_targets"]
    assert list(v3["outcome"]) == [*v2["outcome"], "infra_target"]


def test_C01_the_version_three_golden_is_a_targeted_treatment_and_replays_as_one():
    """C01: a failure naming the declared target, after two completed turns, with the injector's reading of it: it is
    treatment, and replay, served the reading from the record, agrees byte for byte."""
    assert (target_rule("expcore-record/2"), target_rule("expcore-record/3")) == (False, True)
    v3 = load_record(GOLDEN_V3)
    assert len(v3.transcript) == 2
    assert v3.treatment_targets == {"knowledge": "servers[1]"}
    assert (v3.outcome["infra_target"], v3.outcome["treatment"], v3.set_aside) == ("servers[1]", True, False)
    assert v3.instruments[FAULT_NAMESPACE]["injections"][0]["target"] == "servers[1]"
    replayed = replay_record(v3)
    assert (replayed.schema_version, replayed.outcome["treatment"]) == ("expcore-record/3", True)
    assert replayed.dumps().encode("utf-8") == GOLDEN_V3.read_bytes()


def test_R03_the_version_three_golden_passes_the_replay_check_stages():
    """R03: it loads, writes back and replays through the loop."""
    assert check_record(GOLDEN_V3.read_bytes()) is None


SECRET_SHAPED = {  # values a checking environment may hold: every form the redaction rules recognise, none in a golden
    "TOYLAB_TOKEN": "amber-line-one\ncobalt-line-two",
    "OTHER_API_KEY": "abcd/efgh-4471",
    "THIRD_SECRET": "quartz'pebble\"7781-amber",
}


def test_R12_the_golden_records_check_and_replay_whatever_secrets_the_checking_environment_holds(monkeypatch):
    """R12: a record written under earlier redaction rules checks and replays under the current ones: the write-back
    redacts nothing and the replay redacts nothing, so only a secret the record really holds can stop it."""
    for name, value in SECRET_SHAPED.items():
        monkeypatch.setenv(name, value)
    for golden in (GOLDEN, GOLDEN_V2, GOLDEN_V3):
        assert check_record(golden.read_bytes()) is None


def test_R12_a_secret_of_the_checking_environment_held_by_a_record_is_reported_as_a_leak_not_a_divergence(
    monkeypatch,
):
    """R12: the golden record's user prompt stands in for a value a record should not hold. With it named as a secret
    by the checking environment, the check stops at its own `secret` stage, naming where, never at the write-back;
    without it the record checks clean, and its replay is the same either way."""
    raw = GOLDEN_V2.read_bytes()
    assert check_record(raw) is None
    monkeypatch.setenv("TOYLAB_TOKEN", "do the task")
    stage, error = check_record(raw)
    assert stage == "secret"
    assert "prompt" in error
    assert "do the task" not in error
    assert replay_record(load_record(GOLDEN_V2)).outcome == load_record(GOLDEN_V2).outcome
