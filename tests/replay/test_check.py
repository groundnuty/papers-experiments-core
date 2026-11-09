"""The replay check (R04, R05, R08): what it stores, every case in which analysis refuses a batch, and the archives."""

import dataclasses
import hashlib
import json
import tarfile

import pytest
from record_support import CODE
from replay_scenarios import SCENARIOS

from expcore.records.record import RunRecord, decode
from expcore.records.schema import CURRENT
from expcore.replay.archive import ARCHIVE_GLOB, archive_batch, archived_records, archives_under
from expcore.replay.check import (
    CHECK_FILE,
    CHECK_SCHEMA,
    BatchNotVerified,
    HashedOnlyRecord,
    RecomputeHook,
    check_record,
    input_hashes,
    inputs_sha256,
    is_run_record,
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


def test_R04_a_refusal_names_the_command_that_makes_the_check_again_the_callers_own_when_it_has_one(tmp_path, checked):
    """R04: adds that a command line of its own is named in the refusal instead of this module's."""
    unchecked = tmp_path / "unchecked"
    unchecked.mkdir()
    by_default = refused(unchecked, "no replay-check.json")
    assert "python -m expcore.replay.check" in str(by_default)
    named = refused(unchecked, "no replay-check.json", rerun_command="python -m expcore check BATCH")
    assert str(named).endswith("run `python -m expcore check BATCH` first")
    first_record(checked).write_text("{}", encoding="utf-8")
    changed = refused(checked, "inputs changed", rerun_command="python -m expcore check BATCH")
    assert str(changed).endswith("run `python -m expcore check BATCH` first")


def test_R04_the_check_holds_the_code_identity_and_the_hash_of_every_input_by_relative_path(checked):
    """R04: the identity is the code stamp's, and each input is named by its path in the batch."""
    check = stored_check(checked)
    assert check["code"] == CODE
    assert check["record_schema"] == CURRENT == "expcore-record/3"
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
    data["tool_calls"][0]["arguments"] = {"x": "other"}
    text = json.dumps(data, indent=1, ensure_ascii=False)
    stage, error = check_record(text.encode("utf-8"))
    assert stage == "replay"
    assert "tool_calls[0].arguments" in error


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


def test_R04_a_run_record_cannot_be_hashed_only_so_a_check_cannot_replay_nothing_and_vouch_for_everything(batch):
    """R04 and R05: a pattern that names a record is refused when the check is made, and nothing is written."""
    for pattern in ("*", "*.json", "*.1.json"):
        with pytest.raises(HashedOnlyRecord, match=r"is a run record, so it must be replayed"):
            verify_batch(batch, code=CODE, hashed_only=[pattern])
    assert not (batch / CHECK_FILE).exists()


def test_R04_a_record_is_taken_for_one_by_its_name_or_by_its_schema_marker(tmp_path):
    root = tmp_path / "b"
    path = SCENARIOS["tools"][0]().write(root)
    assert is_run_record(path.name, path.read_bytes())  # by its name
    renamed = root / "renamed.json"
    renamed.write_bytes(path.read_bytes())
    assert is_run_record("renamed.json", renamed.read_bytes())  # by the marker it holds
    assert not is_run_record("manifest.json", b'{"cells": 3}')
    assert not is_run_record("campaign-snapshot.sqlite", b"SQLite format 3\x00\xff\xfe")


def test_R04_the_check_lists_the_records_it_replayed_and_not_the_ones_that_failed_or_were_hashed_only(batch):
    (batch / "manifest.json").write_text('{"cells": 3}', encoding="utf-8")
    (batch / "garbage.json").write_text("nope", encoding="utf-8")
    check = verify_batch(batch, code=CODE, hashed_only=["manifest.json"])
    assert [f["file"] for f in check["failures"]] == ["garbage.json"]
    assert "garbage.json" not in check["replayed"]
    assert "manifest.json" not in check["replayed"]
    assert sorted(check["replayed"]) == sorted(set(check["inputs"]) - {"manifest.json", "garbage.json"})


def test_R04_a_stored_check_that_calls_a_record_hashed_only_is_refused_whoever_made_it(checked):
    """R04: the check file edited so that no record is replayed any more (every input hashed, none replayed)."""
    check = stored_check(checked)
    check["hashed_only_patterns"] = ["*"]
    check["hashed_only"] = sorted(check["inputs"])
    check["replayed"] = []
    (checked / CHECK_FILE).write_text(json.dumps(check), encoding="utf-8")
    refused(checked, "is a run record, so it must be replayed")


def test_R04_the_lists_of_replayed_and_hashed_only_inputs_must_account_for_every_input_once(checked):
    check = stored_check(checked)
    check["replayed"] = check["replayed"][1:]
    (checked / CHECK_FILE).write_text(json.dumps(check), encoding="utf-8")
    refused(checked, "replayed and hashed-only inputs .* were altered")


def test_the_command_refuses_a_pattern_that_names_a_record_and_writes_no_check(batch, code_repo, capsys):
    assert run_cli(batch, code_repo, "--hashed-only", "*") == 1
    assert "is a run record, so it must be replayed" in capsys.readouterr().err
    assert not (batch / CHECK_FILE).exists()


def test_hidden_files_and_the_check_itself_are_not_inputs(checked):
    (checked / ".record.json.partial.tmp").write_text("x", encoding="utf-8")
    assert CHECK_FILE not in input_hashes(checked)
    assert require_current_check(checked, code=CODE)


# --- R05: decisions served during replay ------------------------------------------------------------------------------


def decisions(run_key_to_contradict=None, name="decisions", version="1"):
    """A hook that recomputes one decision per record, and contradicts the one of the named run."""

    def check(record: RunRecord) -> int:
        if record.run_key == run_key_to_contradict:
            raise ReplayDivergence("decision", "the recomputed decision differs from the recorded one")
        return 1

    return RecomputeHook(name, version, check)


def test_R05_a_recorded_decision_the_offline_recompute_contradicts_holds_the_batch(batch):
    """R05: the hook raises for a record whose decision it contradicts; that record fails at stage decision."""
    check = verify_batch(batch, code=CODE, recompute=[decisions("run-malformed")])
    assert [(f["stage"], f["hook"]) for f in check["failures"]] == [("decision", "decisions")]
    assert check["recompute"] == [{"name": "decisions", "version": "1", "records": 4, "decisions": 3, "failures": 1}]
    assert (check["n_records"], check["n_ok"]) == (4, 3)
    write_check(batch, check)
    refused(batch, "failed for 1 of 4")
    refused(batch, "recompute hook 'decisions' disagreed with 1 record", required_recompute=["decisions"])


def test_R05_a_recompute_that_agrees_with_every_record_passes_and_the_check_says_what_ran(batch):
    """R05: the check records the hook's name and version, the records it ran on and the decisions it recomputed."""
    seen = []

    def record_seen(record):
        seen.append(record.run_key)
        return 2

    check = verify_batch(batch, code=CODE, recompute=[RecomputeHook("seen", "3.1", record_seen)])
    assert sorted(seen) == sorted(f"run-{n}" for n in NAMES)
    assert check["recompute"] == [{"name": "seen", "version": "3.1", "records": 4, "decisions": 8, "failures": 0}]
    assert check["n_ok"] == 4


def test_R05_a_hook_without_a_name_or_a_version_or_with_a_name_used_twice_is_refused(batch):
    """R05: the check must be able to say which hooks ran."""
    nameless = RecomputeHook("", "1", lambda record: 1)
    with pytest.raises(TypeError, match="has a name"):
        verify_batch(batch, code=CODE, recompute=[nameless])
    with pytest.raises(TypeError, match="has a version"):
        verify_batch(batch, code=CODE, recompute=[RecomputeHook("a", "", lambda record: 1)])
    with pytest.raises(ValueError, match="named alike"):
        verify_batch(batch, code=CODE, recompute=[decisions(), decisions(version="2")])


def test_R04_a_check_made_without_a_required_recompute_is_refused(batch):
    """R04 and R05: the plain check passes on its own terms, and does not vouch for decisions it did not recompute."""
    write_check(batch, verify_batch(batch, code=CODE))
    assert require_current_check(batch, code=CODE)
    refused(
        batch, r"did not run the required recompute hook 'decisions' \(it ran: none\)", required_recompute=["decisions"]
    )


def test_R04_a_required_recompute_that_ran_and_agreed_is_accepted_and_one_that_ran_on_fewer_records_is_not(batch):
    """R04 and R05: the hook must have run on every record of the batch."""
    write_check(batch, verify_batch(batch, code=CODE, recompute=[decisions()]))
    assert require_current_check(batch, code=CODE, required_recompute=["decisions"])
    refused(batch, r"'other' \(it ran: \['decisions'\]\)", required_recompute=["decisions", "other"])
    check = stored_check(batch)
    check["recompute"][0]["records"] = 2
    write_check(batch, check)
    refused(batch, "'decisions' ran on 2 of 4 records", required_recompute=["decisions"])


def test_R08_an_archive_can_require_the_recompute_too(batch, tmp_path):
    """R08 and R05: an output is not archived on a check that lacks a required recompute."""
    write_check(batch, verify_batch(batch, code=CODE))
    with pytest.raises(BatchNotVerified, match="required recompute hook 'decisions'"):
        archive_batch(batch, tmp_path / "records.tar.gz", code=CODE, required_recompute=["decisions"])
    write_check(batch, verify_batch(batch, code=CODE, recompute=[decisions()]))
    assert archive_batch(batch, tmp_path / "records.tar.gz", code=CODE, required_recompute=["decisions"]).exists()


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


def test_R08_a_record_replaced_after_the_check_was_verified_is_refused_and_nothing_is_archived(
    checked, tmp_path, monkeypatch
):
    """R08: each record is read once and its bytes checked against the check; the archive holds those bytes."""
    import expcore.replay.archive as archive_module

    real = archive_module.require_current_check
    victim = first_record(checked)

    def verified_then_replaced(*args, **kwargs):
        result = real(*args, **kwargs)
        victim.write_bytes(victim.read_bytes() + b" ")  # swapped after the verification, before the archive is read
        return result

    monkeypatch.setattr(archive_module, "require_current_check", verified_then_replaced)
    target = tmp_path / "records.tar.gz"
    with pytest.raises(BatchNotVerified, match=f"{victim.name} changed after the check was verified"):
        archive_batch(checked, target, code=CODE)
    assert not target.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".records")]


def test_R08_a_record_added_after_the_check_was_verified_is_refused(checked, tmp_path, monkeypatch):
    """R08: the set of inputs must be the checked one when the archive is written."""
    import expcore.replay.archive as archive_module

    real = archive_module.require_current_check

    def verified_then_added(*args, **kwargs):
        result = real(*args, **kwargs)
        (checked / ("d" * 64 + ".1.json")).write_text("{}", encoding="utf-8")
        return result

    monkeypatch.setattr(archive_module, "require_current_check", verified_then_added)
    with pytest.raises(BatchNotVerified, match="inputs changed between the check and the archive"):
        archive_batch(checked, tmp_path / "records.tar.gz", code=CODE)


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


def test_R04_the_plain_command_is_refused_when_a_recompute_is_required(batch, code_repo, capsys):
    """R04 and R05: the command runs no recompute hook, so the check it makes cannot satisfy a requirement for one."""
    assert run_cli(batch, code_repo, "--require-recompute", "decisions") == 1
    assert "did not run the required recompute hook 'decisions'" in capsys.readouterr().err
    assert run_cli(batch, code_repo) == 0
    assert run_cli(batch, code_repo, "--require") == 0
    assert run_cli(batch, code_repo, "--require", "--require-recompute", "decisions") == 1


def test_R04_the_command_accepts_a_stored_check_that_ran_the_required_recompute(batch, code_repo):
    """R04 and R05: a study's own code runs the hook and stores the check; the command then confirms it is current."""
    from expcore.provenance.codestamp import CodeScope, version

    code = version(code_repo.path, CodeScope.of(["src"]))
    write_check(batch, verify_batch(batch, code=code, recompute=[decisions()]))
    assert run_cli(batch, code_repo, "--require", "--require-recompute", "decisions") == 0
    assert run_cli(batch, code_repo, "--require", "--require-recompute", "other") == 1
