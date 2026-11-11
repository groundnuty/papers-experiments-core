"""R14: the replay check's side files on a batch of its own: what a check records with and without a declaration, the
pairing of a directory that holds several records, a side file named like a record, and the analysis's refusal of a
check made under another declaration than the one in use."""

import dataclasses
import hashlib
import json

import pytest
from record_support import CODE
from replay_scenarios import SCENARIOS

from expcore.records.record import RunRecord, decode
from expcore.replay.check import CHECK_FILE, BatchNotVerified, require_current_check, verify_batch, write_check
from expcore.replay.side_files import SideFile

NAMES = ("tools", "malformed", "infra_turn_three", "tool_crash")
LOGGED = SideFile("ranges_log.json", "instruments.ranges.side_sha256")
KEYS_WITHOUT_A_DECLARATION = [
    "schema",
    "record_schema",
    "checked_at",
    "code",
    "hashed_only_patterns",
    "n_records",
    "n_ok",
    "failures",
    "hashed_only",
    "replayed",
    "recompute",
    "inputs",
    "inputs_sha256",
]


@pytest.fixture
def batch(tmp_path):
    """Four recorded attempts, three of them in one directory and one alone in `shard-2`, with no check yet."""
    root = tmp_path / "batch"
    for name in NAMES:
        record = dataclasses.replace(SCENARIOS[name][0](), run_key=f"run-{name}")
        record.write(root / "shard-2" if name == "tool_crash" else root)
    return root


def lone_record(batch):
    """The one record of `shard-2`."""
    [path] = (batch / "shard-2").glob("*.json")
    return path


def name_side_file(path, data: bytes) -> None:
    """Write a side file beside a record and rewrite the record to name it by its SHA-256."""
    (path.parent / LOGGED.pattern).write_bytes(data)
    record = RunRecord.from_json(decode(path.read_bytes()))
    instruments = {**record.instruments, "ranges": {"ok": True, "side_sha256": hashlib.sha256(data).hexdigest()}}
    dataclasses.replace(record, instruments=instruments).write(path.parent, environ={})


def test_r14_a_check_without_a_declaration_records_exactly_what_it_recorded_before(batch):
    """R14: without side files the check holds the same keys in the same order and the same patterns, and a declaration
    that is empty writes the same bytes as none: a study that declares none is checked as before."""
    made = verify_batch(batch, code=CODE, hashed_only=["*attempt-marker.json"])
    assert list(made) == KEYS_WITHOUT_A_DECLARATION
    assert made["hashed_only_patterns"] == ["*attempt-marker.json"]
    empty = verify_batch(batch, code=CODE, hashed_only=["*attempt-marker.json"], side_files=())
    empty["checked_at"] = made["checked_at"]
    assert json.dumps(empty, indent=1) == json.dumps(made, indent=1)


def test_r14_the_hashed_only_patterns_a_caller_gives_are_recorded_as_given_with_the_side_files_after_them(batch):
    """R14: a caller's patterns are recorded exactly as given, a repeated one included, with no declaration; a
    declaration only appends the patterns of its side files that the caller did not give already."""
    made = verify_batch(batch, code=CODE, hashed_only=["*attempt-marker.json", "*attempt-marker.json"])
    assert made["hashed_only_patterns"] == ["*attempt-marker.json", "*attempt-marker.json"]
    given = ["*attempt-marker.json", "*attempt-marker.json", LOGGED.pattern]
    made = verify_batch(batch, code=CODE, hashed_only=given, side_files=[LOGGED])
    assert made["hashed_only_patterns"] == [*given, f"*/{LOGGED.pattern}"]


def test_r14_a_side_file_named_like_a_json_file_is_hashed_and_paired_never_taken_for_a_record(batch):
    """R14: a declared name ending in `.json` is a side file, hashed and not replayed, and pairs with its record;
    without the declaration the same file fails the check as a record that does not load."""
    name_side_file(lone_record(batch), b'{"ranges": [[0, 512]]}')
    without = verify_batch(batch, code=CODE)
    assert [(f["file"], f["stage"]) for f in without["failures"]] == [(f"shard-2/{LOGGED.pattern}", "load")]
    made = verify_batch(batch, code=CODE, side_files=[LOGGED])
    assert (made["n_records"], made["n_ok"], made["failures"]) == (4, 4, [])
    assert made["hashed_only"] == [f"shard-2/{LOGGED.pattern}"]
    assert made["side_files"] == [{"pattern": LOGGED.pattern, "hash_field": LOGGED.hash_field}]


def test_r14_a_side_file_in_a_directory_of_several_records_cannot_be_paired_and_is_refused(batch):
    """R14: the pairing is by directory, so a side file beside three records fails each of them, never one at random."""
    (batch / LOGGED.pattern).write_bytes(b'{"ranges": []}')
    made = verify_batch(batch, code=CODE, side_files=[LOGGED])
    errors = {f["file"]: f["error"] for f in made["failures"] if f["stage"] == "side_file"}
    assert len(errors) == 3
    assert set(errors.values()) == {
        f"3 records share its directory, so {LOGGED.pattern} cannot be paired with one of them"
    }
    assert (made["n_records"], made["n_ok"]) == (4, 1)


def test_r14_an_orphan_side_file_fails_the_check_without_counting_as_a_record(batch):
    """R14: an orphan fails against its own path; the records still count as they are, and the check does not pass."""
    (batch / "leftover").mkdir()
    (batch / "leftover" / LOGGED.pattern).write_bytes(b'{"ranges": []}')
    made = verify_batch(batch, code=CODE, side_files=[LOGGED])
    assert [f["file"] for f in made["failures"]] == [f"leftover/{LOGGED.pattern}"]
    assert (made["n_records"], made["n_ok"], len(made["replayed"])) == (4, 4, 4)
    write_check(batch, made)
    with pytest.raises(BatchNotVerified, match="the replay check failed"):
        require_current_check(batch, code=CODE, side_files=[LOGGED])


def test_r14_the_analysis_refuses_a_check_made_under_another_declaration_than_the_one_in_use(batch):
    """R14: a check made under one declaration does not vouch for the batch under another, nor under none, and a check
    made under none does not vouch for it under one; a caller that declares nothing compares nothing."""
    name_side_file(lone_record(batch), b'{"ranges": [[0, 512]]}')
    write_check(batch, verify_batch(batch, code=CODE, side_files=[LOGGED]))
    assert require_current_check(batch, code=CODE, side_files=[LOGGED])["side_files"]
    assert require_current_check(batch, code=CODE)["side_files"]
    for other in ([], [SideFile(LOGGED.pattern, "instruments.other.sha256")], [LOGGED, SideFile("more.gz", "a.b")]):
        with pytest.raises(BatchNotVerified, match="another declaration of side files"):
            require_current_check(batch, code=CODE, side_files=other)
    (batch / "shard-2" / LOGGED.pattern).unlink()  # a check without the declaration would take it for a record
    write_check(batch, verify_batch(batch, code=CODE))
    with pytest.raises(BatchNotVerified, match="another declaration of side files"):
        require_current_check(batch, code=CODE, side_files=[LOGGED])


@pytest.mark.parametrize(
    "recorded",
    ["ranges_log.json", [{"pattern": "ranges_log.json"}], [{"pattern": 1, "hash_field": "a"}]],
    ids=["not-a-list", "a-key-missing", "not-text"],
)
def test_r14_a_declaration_altered_in_the_check_file_is_refused(batch, recorded):
    """R14: the declaration a check records must be a list of patterns with their fields, or the check is refused."""
    made = verify_batch(batch, code=CODE)
    made["side_files"] = recorded
    write_check(batch, made)
    for declared in (None, [LOGGED]):
        with pytest.raises(BatchNotVerified, match=f"declaration of side files in {CHECK_FILE} was altered"):
            require_current_check(batch, code=CODE, side_files=declared)
