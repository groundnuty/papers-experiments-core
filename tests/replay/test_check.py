"""The replay check (R04, R05, R08): what it stores, every case in which analysis refuses a batch, and the archives."""

import dataclasses
import hashlib
import json
import tarfile

import pytest
from record_support import CODE
from replay_scenarios import SCENARIOS

from expcore.records.record import RunRecord, decode
from expcore.replay.archive import ARCHIVE_GLOB, archive_batch, archived_records, archives_under
from expcore.replay.check import (
    CHECK_FILE,
    CHECK_SCHEMA,
    BatchNotVerified,
    check_record,
    input_hashes,
    inputs_sha256,
    main,
    require_current_check,
    verify_batch,
    write_check,
)
from expcore.replay.divergence import ReplayDivergence

NAMES = ("tools", "malformed", "infra_turn_three", "tool_crash")


@pytest.fixture
def batch(tmp_path):
    """A batch of four recorded attempts, one of them in a subdirectory, with no check yet."""
    root = tmp_path / "batch"
    for name in NAMES:
        record = dataclasses.replace(SCENARIOS[name][0](), run_key=f"run-{name}")
        record.write(root / "shard-2" if name == "tool_crash" else root)
    return root


@pytest.fixture
def checked(batch):
    """The batch after a passing check is written."""
    write_check(batch, verify_batch(batch, code=CODE))
    return batch


def stored_check(batch):
    return json.loads((batch / CHECK_FILE).read_text(encoding="utf-8"))


def first_record(batch):
    return sorted(batch.glob("*.json"))[0]


def refused(batch, match, code=CODE, **kwargs):
    with pytest.raises(BatchNotVerified, match=match) as caught:
        require_current_check(batch, code=code, **kwargs)
    return caught.value


# --- what the check stores ------------------------------------------------------------------------------------------


def test_R04_a_batch_that_loads_round_trips_and_replays_passes_and_is_accepted(checked):
    """R04: every record of the batch, in subdirectories too, passes; analysis accepts the batch."""
    check = stored_check(checked)
    assert (check["schema"], check["n_records"], check["n_ok"], check["failures"]) == (CHECK_SCHEMA, 4, 4, [])
    assert require_current_check(checked, code=CODE)["inputs_sha256"] == check["inputs_sha256"]


def test_R04_the_check_holds_the_code_identity_and_the_hash_of_every_input_by_relative_path(checked):
    """R04: the identity is the code stamp's, and each input is named by its path in the batch."""
    check = stored_check(checked)
    assert check["code"] == CODE
    assert check["record_schema"] == "expcore-record/1"
    paths = sorted(check["inputs"])
    assert len(paths) == 4
    assert any(p.startswith("shard-2/") for p in paths)
    for rel in paths:
        assert check["inputs"][rel] == hashlib.sha256((checked / rel).read_bytes()).hexdigest()
    assert check["inputs_sha256"] == inputs_sha256(check["inputs"])
    assert input_hashes(checked) == check["inputs"]


def test_the_digest_of_the_inputs_does_not_depend_on_where_the_batch_lives(batch, tmp_path):
    before = inputs_sha256(input_hashes(batch))
    moved = tmp_path / "elsewhere" / "deeper"
    moved.parent.mkdir()
    batch.rename(moved)
    assert inputs_sha256(input_hashes(moved)) == before
    assert all(not p.startswith("/") for p in input_hashes(moved))


# --- every case in which analysis refuses ----------------------------------------------------------------------------


def test_R04_refuses_a_batch_that_has_no_check(batch):
    """R04: records never shown to replay are refused, and the message says what to run."""
    refused(batch, f"no {CHECK_FILE}.*python -m expcore.replay.check")


def test_R04_refuses_a_check_that_failed(batch):
    """R04: a record that does not replay fails the check, and a failed check is refused."""
    path = first_record(batch)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["tool_calls"] = []
    data["outcome"]["name"] = "turn_cap"
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    check = verify_batch(batch, code=CODE)
    assert check["n_ok"] == 3
    assert [f["stage"] for f in check["failures"]] == ["replay"]
    write_check(batch, check)
    refused(batch, "failed for 1 of 4 records")


def test_R04_refuses_inputs_that_changed_since_the_check(checked):
    """R04: a record rewritten after the check is refused, and the changed file is named."""
    path = first_record(checked)
    path.write_bytes(path.read_bytes() + b" ")
    refused(checked, f"inputs changed since the replay check \\(changed {path.name}")


def test_R04_refuses_an_input_added_or_removed_after_the_check(checked):
    """R04: a record added to the batch, or removed from it, invalidates the check."""
    first_record(checked).with_name("c" * 64 + ".1.json").write_text("{}", encoding="utf-8")
    refused(checked, "added")
    (checked / ("c" * 64 + ".1.json")).unlink()
    first_record(checked).unlink()
    refused(checked, "removed")


def test_R04_refuses_a_check_made_under_another_code_identity(checked):
    """R04: another commit, or the same commit with another tree hash, is another identity."""
    refused(checked, "made under code ccccccc, the code in use is eeeeeee", code={**CODE, "commit": "e" * 40})
    refused(checked, "made under code tree", code={**CODE, "tree_sha256": "f" * 64})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"commit": "unknown"}, "no known commit or tree hash"),
        ({"tree_sha256": None}, "no known commit or tree hash"),
        ({"stale": True}, "differs from the stamp"),
        ({"dirty": True, "why": "uncommitted changes"}, "has uncommitted changes"),
    ],
)
def test_R04_refuses_code_that_is_unknown_stale_or_dirty(checked, change, message):
    """R04: the code in use must be known, current and clean, unless dirty code is allowed."""
    refused(checked, f"the code in use .*{message}", code={**CODE, **change})


def test_dirty_code_is_accepted_only_when_the_caller_allows_it_and_the_identity_still_matches(checked):
    dirty = {**CODE, "dirty": True, "why": "uncommitted changes"}
    refused(checked, "has uncommitted changes", code=dirty)
    assert require_current_check(checked, code=dirty, allow_dirty=True)


def test_R04_refuses_a_check_made_under_dirty_code(batch):
    """R04: a check made on code with uncommitted changes names no commit, so it is refused later."""
    dirty = {**CODE, "dirty": True, "why": "uncommitted changes"}
    write_check(batch, verify_batch(batch, code=dirty))
    refused(batch, "the code the check was made under .*uncommitted")


def test_R04_refuses_a_check_of_another_schema(checked):
    """R04: a check of another check schema, or made under other record schemas, is refused."""
    check = stored_check(checked)
    write_check(checked, {**check, "schema": "replay-check/0"})
    refused(checked, "check schema 'replay-check/0'")
    write_check(checked, {**check, "record_schema": "expcore-record/0"})
    refused(checked, "record schema 'expcore-record/0'")


def test_R04_refuses_a_check_whose_list_of_inputs_was_edited(checked):
    """R04: removing an input from the stored list to make it match is detected."""
    check = stored_check(checked)
    dropped = sorted(check["inputs"])[0]
    del check["inputs"][dropped]
    write_check(checked, check)
    refused(checked, "list of inputs.*altered")


def test_a_check_file_that_is_not_json_is_refused_not_crashed_on(checked):
    (checked / CHECK_FILE).write_text("{not json", encoding="utf-8")
    refused(checked, "not readable")
    (checked / CHECK_FILE).write_text("[1]", encoding="utf-8")
    refused(checked, "not a check")


# --- the stages -------------------------------------------------------------------------------------------------------


def test_the_stages_a_record_can_fail_at_are_load_roundtrip_and_replay(batch):
    """A file that is not a record fails at load; bytes that load but do not write back fail at roundtrip."""
    (batch / "garbage.json").write_text("not json", encoding="utf-8")
    other = first_record(batch)
    data = json.loads(other.read_text(encoding="utf-8"))
    (batch / "reformatted.json").write_text(json.dumps(data, indent=4), encoding="utf-8")
    stages = {f["file"]: f["stage"] for f in verify_batch(batch, code=CODE)["failures"]}
    assert stages == {"garbage.json": "load", "reformatted.json": "roundtrip"}


def test_R04_check_record_reports_the_stage_and_a_message_that_names_the_path(batch):
    raw = first_record(batch).read_bytes()
    assert check_record(raw) is None
    data = decode(raw)
    data["transcript"][0]["tool_messages"] = []
    text = json.dumps(data, indent=1, ensure_ascii=False)
    stage, error = check_record(text.encode("utf-8"))
    assert stage == "replay"
    assert "transcript[0].tool_messages" in error


def test_a_failure_message_never_holds_a_secret_from_the_environment(batch, monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "sk-test-0123456789abcdefghij")
    (batch / "bad.json").write_text('{"schema_version": "sk-test-0123456789abcdefghij"}', encoding="utf-8")
    check = verify_batch(batch, code=CODE)
    assert "sk-test-0123456789abcdefghij" not in json.dumps(check)


# --- hashed-only inputs -----------------------------------------------------------------------------------------------


def test_a_file_the_analysis_reads_that_is_not_a_record_is_hashed_not_replayed(batch):
    """A manifest is named by pattern: it is not a record, and a change to it invalidates the check."""
    (batch / "manifest.json").write_text('{"cells": 3}', encoding="utf-8")
    unnamed = verify_batch(batch, code=CODE)  # without the pattern it is taken for a record, and fails to load
    assert (unnamed["n_records"], unnamed["n_ok"], unnamed["failures"][0]["stage"]) == (5, 4, "load")
    check = verify_batch(batch, code=CODE, hashed_only=["manifest.json"])
    assert (check["n_records"], check["n_ok"], check["hashed_only"]) == (4, 4, ["manifest.json"])
    write_check(batch, check)
    require_current_check(batch, code=CODE)
    (batch / "manifest.json").write_text('{"cells": 4}', encoding="utf-8")
    refused(batch, "changed manifest.json")


def test_hidden_files_and_the_check_itself_are_not_inputs(checked):
    (checked / ".record.json.partial.tmp").write_text("x", encoding="utf-8")
    assert CHECK_FILE not in input_hashes(checked)
    assert require_current_check(checked, code=CODE)


# --- R05: decisions served during replay ------------------------------------------------------------------------------


def test_R05_a_recorded_decision_the_offline_recompute_contradicts_holds_the_batch(batch):
    """R05: the hook raises for a record whose decision it contradicts; that record fails at stage decision."""

    def recompute(record: RunRecord) -> None:
        if record.run_key == "run-malformed":
            raise ReplayDivergence("decision", "the recomputed decision differs from the recorded one")

    check = verify_batch(batch, code=CODE, recompute=recompute)
    assert [f["stage"] for f in check["failures"]] == ["decision"]
    assert check["decisions_recomputed"] == 3
    write_check(batch, check)
    refused(batch, "failed for 1 of 4")


def test_R05_a_recompute_that_agrees_with_every_record_passes(batch):
    seen = []
    check = verify_batch(batch, code=CODE, recompute=lambda record: seen.append(record.run_key))
    assert sorted(seen) == sorted(f"run-{n}" for n in NAMES)
    assert (check["n_ok"], check["decisions_recomputed"]) == (4, 4)


# --- R08: archives ----------------------------------------------------------------------------------------------------


def test_R08_no_output_is_archived_without_its_replay_check(batch, tmp_path):
    """R08: archiving a batch with no check, or a check that is out of date, fails and leaves no archive."""
    target = tmp_path / "records.tar.gz"
    with pytest.raises(BatchNotVerified, match=r"no replay-check\.json"):
        archive_batch(batch, target, code=CODE)
    write_check(batch, verify_batch(batch, code=CODE))
    first_record(batch).write_bytes(b"{}")
    with pytest.raises(BatchNotVerified, match="inputs changed"):
        archive_batch(batch, target, code=CODE)
    assert not target.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".records")]


def test_R08_every_records_archive_is_collected_shards_included(checked, tmp_path):
    """R08: records-1.tar.gz and records-2.tar.gz, in any directory, are all found and their records all check."""
    shards = tmp_path / "out" / "deep"
    shards.mkdir(parents=True)
    archive_batch(checked, tmp_path / "out" / "records.tar.gz", code=CODE)
    archive_batch(checked, shards / "records-2.tar.gz", code=CODE)
    (tmp_path / "out" / "notes.tar.gz").write_bytes(b"not an archive of records")
    found = archives_under(tmp_path / "out")
    assert [p.name for p in found] == ["records-2.tar.gz", "records.tar.gz"]
    assert all(p.match(ARCHIVE_GLOB) for p in found)
    records = dict(archived_records(tmp_path / "out"))
    assert len(records) == 8
    assert all(check_record(raw) is None for raw in records.values())


def test_R08_an_archive_holds_the_inputs_and_the_check_that_covers_them(checked, tmp_path):
    """R08: the check travels with the records, so the archive can be verified where it lands."""
    target = archive_batch(checked, tmp_path / "records.tar.gz", code=CODE)
    with tarfile.open(target, "r:gz") as tar:
        names = tar.getnames()
    assert CHECK_FILE in names
    assert sorted(n for n in names if n != CHECK_FILE) == sorted(input_hashes(checked))
    assert all(name != CHECK_FILE for name, _ in archived_records(tmp_path))


# --- the command line -------------------------------------------------------------------------------------------------


@pytest.fixture
def code_repo(make_repo):
    repo = make_repo("code")
    repo.write("src/mod.py", "VALUE = 1\n")
    repo.commit("code")
    return repo


def run_cli(batch, repo, *extra):
    return main([str(batch), "--code-root", str(repo.path), "--root", "src", *extra])


def test_the_command_checks_a_batch_writes_the_check_and_exits_zero(batch, code_repo, capsys):
    assert run_cli(batch, code_repo) == 0
    assert "4 of 4 records passed" in capsys.readouterr().out
    assert stored_check(batch)["code"]["commit"] == code_repo.git("rev-parse", "HEAD")
    assert run_cli(batch, code_repo, "--require") == 0


def test_the_command_exits_one_and_lists_the_records_that_failed(batch, code_repo, capsys):
    (batch / "garbage.json").write_text("nope", encoding="utf-8")
    assert run_cli(batch, code_repo) == 1
    out = capsys.readouterr().out
    assert "FAIL garbage.json [load]" in out
    assert "4 of 5 records passed" in out


def test_the_command_refuses_dirty_code_unless_told_otherwise(batch, code_repo, capsys):
    code_repo.write("src/mod.py", "VALUE = 2\n")
    assert run_cli(batch, code_repo) == 1
    assert "uncommitted changes" in capsys.readouterr().err
    assert run_cli(batch, code_repo, "--allow-dirty") == 0


def test_the_command_refuses_a_check_after_the_code_changed(batch, code_repo, capsys):
    assert run_cli(batch, code_repo) == 0
    code_repo.write("src/mod.py", "VALUE = 3\n")
    code_repo.commit("change")
    assert run_cli(batch, code_repo, "--require") == 1
    assert "made under code" in capsys.readouterr().err


def test_the_command_needs_to_be_told_which_code_it_covers(batch, capsys):
    with pytest.raises(SystemExit) as caught:
        main([str(batch)])
    assert caught.value.code == 2
    assert "--root" in capsys.readouterr().err
