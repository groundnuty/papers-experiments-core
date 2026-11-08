"""The golden record: version 1 of the record format is frozen from its first pushed commit (R01, R02).

`tests/data/golden-record-v1.json` is a record of a synthetic run with invented names: tool calls of every outcome, a
result the data source cut, a call the loop skipped, a secret redacted from the prompt and from a result. It must
load, write back byte for byte, and replay. If a change to the format makes this test fail, the change is to the
shape that ships: it needs a new schema version in `expcore.records.schema.SCHEMAS`, not an edit of version 1, and
the golden file stays as it is. The file was made by running the stub backend through the loop with an environment
variable named GOLDEN_API_KEY set to an invented key, and writing `record.dumps()`.
"""

from pathlib import Path

from expcore.records.record import RunRecord, decode, load_record
from expcore.records.schema import SCHEMAS
from expcore.replay.check import check_record
from expcore.replay.replay import replay_record

GOLDEN = Path(__file__).resolve().parents[1] / "data" / "golden-record-v1.json"


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
