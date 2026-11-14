"""R15: placing a declared side file beside its record, safely (#248).

The helper (`stage_side_file`, then `StagedSideFile.commit` once the record is written) is tested with a stand-in for
the sandbox's `collect`; the recovery rule (`settle_side_files`, which `check` runs first) on batches that a kill
left in each state it can leave one in: after the temporary file, after the record, after the rename. Whatever the
state, `check` must pass what the study did not break and must not pass a wrong file.
"""

import dataclasses
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from record_support import CODE
from replay_scenarios import SCENARIOS

from expcore.records.record import RunRecord
from expcore.replay import side_files as side_module
from expcore.replay.check import verify_batch
from expcore.replay.side_file_placement import (
    PlacementError,
    UndeclaredSideFile,
    settle_side_files,
    stage_side_file,
    temporary_name,
)
from expcore.replay.side_files import SideFile, SideFileTooLarge

LOGGED = SideFile("extent_ranges.json.gz", "instruments.extent.side_sha256")
SECOND = SideFile("surplus.bin", "instruments.surplus.side_sha256")
DECLARED = [LOGGED, SECOND]
CONTENT = b"byte ranges the agent read " * 40
ATTEMPT = 1


class FakeSandbox:
    """What `DockerSandbox.collect(dest, outputs=...)` does for the helper: copies each named file `work` holds to
    `dest/work/<name>`, writes its metadata beside them, and says which outputs it copied. It keeps its calls."""

    def __init__(self, work: dict[str, bytes]) -> None:
        self.work = work
        self.calls: list[tuple[Path, list[str]]] = []

    def collect(self, dest_dir, outputs=()):
        dest = Path(dest_dir)
        self.calls.append((dest, list(outputs)))
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "metadata.json").write_text("{}", encoding="utf-8")
        got = {}
        for rel in outputs:
            got[rel] = rel in self.work
            if got[rel]:
                (dest / "work" / rel).parent.mkdir(parents=True, exist_ok=True)
                (dest / "work" / rel).write_bytes(self.work[rel])
        return {"outputs": got, "deaths": []}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_for(run_key: str, **named: str) -> RunRecord:
    """A record that replays, of one attempt, whose `extent` and `surplus` instruments hold the hashes given."""
    record = dataclasses.replace(SCENARIOS["tools"][0](), run_key=run_key, attempt=ATTEMPT)
    instruments = dict(record.instruments)
    for namespace, sha in named.items():
        instruments[namespace] = {"ok": True, "side_sha256": sha}
    return dataclasses.replace(record, instruments=instruments)


@pytest.fixture
def batch(tmp_path):
    """A batch of one attempt that is complete (its own directory), where `try2` of another run is yet to be made."""
    root = tmp_path / "runs"
    record_for("run-finished").write(root / "aaaa" / "try1")
    return root


@pytest.fixture
def run_dir(batch):
    return batch / "bbbb" / "try1"


@pytest.fixture
def request_of(run_dir):
    return SimpleNamespace(run_dir=run_dir, identity=SimpleNamespace(run_key="run-killed", attempt=ATTEMPT))


def stage(request_of, work=None, name=LOGGED.pattern, **options):
    sandbox = FakeSandbox({LOGGED.pattern: CONTENT} if work is None else work)
    return sandbox, stage_side_file(sandbox, request_of, DECLARED, name, **options)


def write_record(request_of, sha: str) -> Path:
    return record_for(request_of.identity.run_key, extent=sha).write(request_of.run_dir)


def failures(batch, declared=DECLARED) -> list[tuple[str, str]]:
    made = verify_batch(batch, code=CODE, side_files=declared)
    return [(f["file"], f["error"]) for f in made["failures"]]


def temporary(run_dir) -> Path:
    return run_dir / temporary_name(LOGGED.pattern, ATTEMPT)


# --- the helper -------------------------------------------------------------------------------------------------------


def test_r15_the_helper_collects_through_the_sandbox_into_a_scratch_directory_outside_the_batch(request_of, batch):
    """R15: the file comes from `sandbox.collect(dest, outputs=[...])`, so the copy keeps its guarantees (O19), into a
    directory that is not in the batch: the collection's metadata and its `work/` copy would fail the check there."""
    sandbox, staged = stage(request_of)
    [(dest, outputs)] = sandbox.calls
    assert outputs == [LOGGED.pattern]
    assert batch not in dest.parents
    assert request_of.run_dir not in (dest, *dest.parents)
    assert not dest.exists()  # the scratch directory is removed once the file is staged
    assert staged is not None
    assert sorted(p.name for p in request_of.run_dir.iterdir()) == [temporary_name(LOGGED.pattern, ATTEMPT)]


def test_r15_the_helper_writes_the_file_under_a_dot_name_the_check_ignores_and_returns_its_sha256(request_of, batch):
    """R15: the staged file is `.<pattern>.tmp-<attempt>`, byte for byte the collected file, and the SHA-256 it
    returns is the one the record names; no file of the declared name exists until the rename."""
    _, staged = stage(request_of)
    assert staged is not None
    assert staged.sha256 == digest(CONTENT)
    assert staged.size == len(CONTENT)
    assert temporary(request_of.run_dir).read_bytes() == CONTENT
    assert not (request_of.run_dir / LOGGED.pattern).exists()
    assert failures(batch) == []  # a dot-name is never read by the check


def test_r15_the_helper_takes_the_file_from_another_path_of_work_when_given_one(request_of):
    """R15: `source` names where the file is in /work when that is not its declared name; it lands under the name."""
    sandbox, staged = stage(request_of, work={"out/ranges.gz": CONTENT}, source="out/ranges.gz")
    assert sandbox.calls[0][1] == ["out/ranges.gz"]
    assert staged is not None
    assert staged.declaration == LOGGED
    assert temporary(request_of.run_dir).read_bytes() == CONTENT


@pytest.mark.parametrize("name", ["other.json.gz", "extent_ranges.json.gz.tmp-1", "*", "dir/extent_ranges.json.gz"])
def test_r15_the_helper_refuses_a_name_the_declaration_does_not_hold(request_of, name):
    """R15: only a declared pattern is placed; the sandbox is not asked and nothing is written."""
    sandbox = FakeSandbox({LOGGED.pattern: CONTENT})
    with pytest.raises(UndeclaredSideFile, match="declares no side file named"):
        stage_side_file(sandbox, request_of, DECLARED, name)
    assert sandbox.calls == []
    assert not request_of.run_dir.exists()


@pytest.mark.parametrize("source", ["/etc/passwd", "../outside", "a/../../b", "", "."])
def test_r15_the_helper_refuses_a_source_that_leaves_the_working_directory(request_of, source):
    """R15: a source is a relative path inside /work, as `collect` requires, refused before the sandbox is asked."""
    sandbox = FakeSandbox({})
    with pytest.raises(ValueError, match="relative path inside the working directory"):
        stage_side_file(sandbox, request_of, DECLARED, LOGGED.pattern, source=source)
    assert sandbox.calls == []


def test_r15_the_helper_refuses_a_file_over_the_cap_and_leaves_nothing_behind(request_of, monkeypatch):
    """R15: a collected file of more than MAX_SIDE_FILE_BYTES is refused, naming the cap, before a byte is staged."""
    monkeypatch.setattr(side_module, "MAX_SIDE_FILE_BYTES", len(CONTENT) - 1)
    with pytest.raises(SideFileTooLarge, match="MAX_SIDE_FILE_BYTES"):
        stage(request_of)
    assert not request_of.run_dir.exists() or list(request_of.run_dir.iterdir()) == []


def test_r15_the_helper_takes_a_file_of_exactly_the_cap(request_of, monkeypatch):
    """R15: the cap is a maximum: a file of exactly MAX_SIDE_FILE_BYTES is placed."""
    monkeypatch.setattr(side_module, "MAX_SIDE_FILE_BYTES", len(CONTENT))
    _, staged = stage(request_of)
    assert staged is not None
    assert staged.size == len(CONTENT)


def test_r15_the_helper_stops_at_the_cap_while_copying_whatever_the_file_claimed_to_hold(request_of, monkeypatch):
    """R15: the size the copy actually holds is what is refused, so a file that grew after it was measured is too."""
    monkeypatch.setattr(side_module, "MAX_SIDE_FILE_BYTES", len(CONTENT) - 1)
    monkeypatch.setattr("expcore.replay.side_file_placement._size_of", lambda _descriptor: 0)
    with pytest.raises(SideFileTooLarge):
        stage(request_of)
    assert list(request_of.run_dir.glob(".*")) == []


def test_r15_a_file_the_run_did_not_write_is_no_side_file(request_of):
    """R15: the sandbox copied nothing (the file is absent, or the agent made it a link, which the collection refuses
    and records): the run made no side file, the helper returns None, and nothing is written."""
    sandbox, staged = stage(request_of, work={})
    assert staged is None
    assert len(sandbox.calls) == 1
    assert not request_of.run_dir.exists() or list(request_of.run_dir.iterdir()) == []


def test_r15_commit_refuses_to_rename_before_a_record_names_the_file(request_of):
    """R15: the order is the temporary file, then the record with its hash, then the rename: a commit with no record,
    or with one that names another hash, renames nothing."""
    _, staged = stage(request_of)
    assert staged is not None
    with pytest.raises(PlacementError, match="no record"):
        staged.commit()
    write_record(request_of, digest(b"another file"))
    with pytest.raises(PlacementError, match="names"):
        staged.commit()
    assert temporary(request_of.run_dir).exists()
    assert not (request_of.run_dir / LOGGED.pattern).exists()


def test_r15_commit_renames_the_file_into_place_once_the_record_names_it(request_of, batch):
    """R15: after the record, the rename puts the file at its declared name, and the check passes."""
    _, staged = stage(request_of)
    assert staged is not None
    write_record(request_of, staged.sha256)
    placed = staged.commit()
    assert placed == request_of.run_dir / LOGGED.pattern
    assert placed.read_bytes() == CONTENT
    assert not temporary(request_of.run_dir).exists()
    assert failures(batch) == []


def test_r15_commit_succeeds_when_a_check_renamed_the_file_before_it_looked(request_of):
    """R15: a check that ran during the attempt may have finished the rename: the commit then finds the file in place
    with the hash and succeeds, leaving no temporary copy."""
    _, staged = stage(request_of)
    assert staged is not None
    write_record(request_of, staged.sha256)
    os.replace(temporary(request_of.run_dir), request_of.run_dir / LOGGED.pattern)
    assert staged.commit() == request_of.run_dir / LOGGED.pattern
    assert not temporary(request_of.run_dir).exists()


def test_r15_commit_succeeds_when_a_check_renames_the_file_between_its_look_and_its_rename(request_of, monkeypatch):
    """R15: a check that finishes the rename after the commit saw the temporary file and before it renamed it makes
    the commit's rename fail on a missing source: the commit finds the file in place with the hash and succeeds."""
    _, staged = stage(request_of)
    assert staged is not None
    write_record(request_of, staged.sha256)
    real = os.replace

    def raced(source, target):
        real(source, target)  # the check's rename, first
        raise FileNotFoundError(source)

    monkeypatch.setattr("expcore.replay.side_file_placement.os.replace", raced)
    assert staged.commit() == request_of.run_dir / LOGGED.pattern
    assert (request_of.run_dir / LOGGED.pattern).read_bytes() == CONTENT


def test_r15_commit_never_overwrites_a_file_already_at_the_declared_name(request_of):
    """R15: a file that lies at the declared name with other bytes than the record names is kept, and the commit
    refuses, so the check's pairing refuses the file instead of the commit turning the discrepancy into a pass."""
    _, staged = stage(request_of)
    assert staged is not None
    write_record(request_of, staged.sha256)
    (request_of.run_dir / LOGGED.pattern).write_bytes(b"another file")
    with pytest.raises(PlacementError, match="not overwritten"):
        staged.commit()
    assert (request_of.run_dir / LOGGED.pattern).read_bytes() == b"another file"


def test_r15_commit_reports_a_file_that_is_neither_staged_nor_in_place_as_lost(request_of):
    """R15: with the temporary file gone and nothing at the declared name, the commit says the file is lost."""
    _, staged = stage(request_of)
    assert staged is not None
    write_record(request_of, staged.sha256)
    temporary(request_of.run_dir).unlink()
    with pytest.raises(PlacementError, match="lost"):
        staged.commit()


def test_r15_discard_removes_the_staged_file_and_only_that(request_of):
    """R15: an executor that fails before it writes the record removes what it staged; a second call does nothing."""
    _, staged = stage(request_of)
    assert staged is not None
    staged.discard()
    staged.discard()
    assert list(request_of.run_dir.iterdir()) == []


# --- a kill at each step ----------------------------------------------------------------------------------------------


def test_r15_a_kill_after_the_temporary_file_leaves_no_state_the_check_refuses(request_of, batch):
    """R15: killed after the temporary file and before the record, the dot-name is never read, so the check passes as
    it stands; `check` then removes the file, which no record names, and a second pass finds nothing to do."""
    _, staged = stage(request_of)
    assert staged is not None
    assert failures(batch) == []
    [settled] = settle_side_files(batch, DECLARED, remove_unnamed=True)
    assert (settled.action, settled.path) == ("removed", temporary(request_of.run_dir).relative_to(batch).as_posix())
    assert not temporary(request_of.run_dir).exists()
    assert failures(batch) == []
    assert settle_side_files(batch, DECLARED, remove_unnamed=True) == []


def test_r15_a_kill_after_the_record_is_finished_by_the_check_and_not_refused(request_of, batch):
    """R15: killed after the record that names the file and before the rename, the check as it stands refuses a hash
    with no file, which the study did not cause; `check` first finishes the rename, since the file's hash is the one
    the record names, then hashes and pairs the file like any other."""
    _, staged = stage(request_of)
    assert staged is not None
    write_record(request_of, staged.sha256)
    [(_, error)] = failures(batch)
    assert "but no such file lies beside it" in error
    [settled] = settle_side_files(batch, DECLARED)
    assert settled.action == "finished"
    assert (request_of.run_dir / LOGGED.pattern).read_bytes() == CONTENT
    assert not temporary(request_of.run_dir).exists()
    made = verify_batch(batch, code=CODE, side_files=DECLARED)
    assert made["failures"] == []
    assert made["n_ok"] == made["n_records"] == 2
    placed = (request_of.run_dir / LOGGED.pattern).relative_to(batch).as_posix()
    assert made["hashed_only"] == [placed]
    assert made["inputs"][placed] == digest(CONTENT)
    assert settle_side_files(batch, DECLARED) == []


def test_r15_a_kill_after_the_rename_is_a_complete_attempt(request_of, batch):
    """R15: killed after the rename, nothing is left to settle and the check passes."""
    _, staged = stage(request_of)
    assert staged is not None
    write_record(request_of, staged.sha256)
    staged.commit()
    assert settle_side_files(batch, DECLARED) == []
    assert failures(batch) == []


def test_r15_a_temporary_file_that_is_not_the_one_a_record_names_is_never_renamed(request_of, batch):
    """R15: the check never accepts a wrong file: a temporary file whose hash is not the record's is removed, never
    put in place, and the record's hash with no file is refused as it must be."""
    _, staged = stage(request_of)
    assert staged is not None
    write_record(request_of, staged.sha256)
    temporary(request_of.run_dir).write_bytes(CONTENT[:-1] + b"X")  # not what the record names
    [settled] = settle_side_files(batch, DECLARED, remove_unnamed=True)
    assert settled.action == "removed"
    assert not (request_of.run_dir / LOGGED.pattern).exists()
    [(file, error)] = failures(batch)
    assert file.endswith(".json")
    assert "but no such file lies beside it" in error


def test_r15_a_temporary_file_with_no_record_beside_it_is_removed_only_when_asked(request_of, batch):
    """R15: a check made while the campaign runs cannot tell a kill from an attempt that has staged its file and has
    not written its record yet, so by default nothing is removed; the caller that holds the campaign's lock asks."""
    _, staged = stage(request_of)
    assert staged is not None
    assert settle_side_files(batch, DECLARED) == []
    assert temporary(request_of.run_dir).exists()
    [settled] = settle_side_files(batch, DECLARED, remove_unnamed=True)
    assert settled.action == "removed"
    assert not temporary(request_of.run_dir).exists()


def test_r15_a_file_being_staged_is_not_removed_when_its_record_lands_during_the_check(request_of, batch):
    """R15: a check that hashes the temporary file while the attempt is still writing it holds a hash of a prefix; when
    the attempt then writes the record that names the whole file, the check must not take the mismatch for a file no
    record names and delete it. By default nothing is removed, and the attempt's own commit succeeds."""
    _, staged = stage(request_of)
    assert staged is not None
    whole = temporary(request_of.run_dir).read_bytes()
    temporary(request_of.run_dir).write_bytes(whole[:10])  # what the check read: a prefix
    write_record(request_of, staged.sha256)  # the attempt's record, naming the whole file
    assert settle_side_files(batch, DECLARED) == []
    temporary(request_of.run_dir).write_bytes(whole)  # the attempt's write finishes
    assert staged.commit() == request_of.run_dir / LOGGED.pattern
    assert failures(batch) == []


def test_r15_a_rename_a_record_names_is_finished_without_asking_to_remove_anything(request_of, batch):
    """R15: a rename whose record names the file is finished whatever the caller allows removing, so a check made
    while the campaign runs finishes it too: the attempt's own commit, if it comes after, finds the file in place."""
    _, staged = stage(request_of)
    assert staged is not None
    write_record(request_of, staged.sha256)
    [settled] = settle_side_files(batch, DECLARED)
    assert settled.action == "finished"
    assert staged.commit() == request_of.run_dir / LOGGED.pattern
    assert failures(batch) == []


def test_r15_a_temporary_file_beside_a_record_that_names_another_hash_is_removed_when_asked(request_of, batch):
    """R15: a record that is there and does not name the file leaves it unnamed: removed, when removal is asked for."""
    _, staged = stage(request_of)
    assert staged is not None
    write_record(request_of, digest(b"another file"))
    assert settle_side_files(batch, DECLARED) == []
    [settled] = settle_side_files(batch, DECLARED, remove_unnamed=True)
    assert settled.action == "removed"


def test_r15_a_file_in_place_with_the_named_hash_makes_the_temporary_copy_redundant(request_of, batch):
    """R15: a rename that happened and left its source behind (a crash before the directory reached the disk) leaves
    two copies of one file: the temporary one goes."""
    _, staged = stage(request_of)
    assert staged is not None
    write_record(request_of, staged.sha256)
    (request_of.run_dir / LOGGED.pattern).write_bytes(CONTENT)
    [settled] = settle_side_files(batch, DECLARED)
    assert settled.action == "removed"
    assert failures(batch) == []


def test_r15_a_file_in_place_with_another_hash_is_never_replaced(request_of, batch):
    """R15: a file already at the declared name is not overwritten by the recovery, whatever it holds: the check
    refuses it as changed, and the temporary copy stays for whoever looks."""
    _, staged = stage(request_of)
    assert staged is not None
    write_record(request_of, staged.sha256)
    (request_of.run_dir / LOGGED.pattern).write_bytes(b"changed after the run")
    assert settle_side_files(batch, DECLARED) == []
    assert (request_of.run_dir / LOGGED.pattern).read_bytes() == b"changed after the run"
    assert temporary(request_of.run_dir).exists()
    [(_, error)] = failures(batch)
    assert "does not have the SHA-256 the record names" in error


def test_r15_a_directory_of_several_records_is_never_settled_by_guessing(request_of, batch):
    """R15: with two records in the directory, one of which names the file, it cannot be paired with one of them, so it
    is not renamed (and not removed, being named), and the check refuses the directory as for any side file there."""
    _, staged = stage(request_of)
    assert staged is not None
    write_record(request_of, staged.sha256)
    record_for("run-another", extent=staged.sha256).write(request_of.run_dir)
    assert settle_side_files(batch, DECLARED) == []
    assert temporary(request_of.run_dir).exists()


def test_r15_a_record_that_does_not_load_names_nothing(request_of, batch):
    """R15: a record that is not JSON names no file; the temporary file beside it is removed and the check refuses the
    record, as it does without any side file."""
    _, staged = stage(request_of)
    assert staged is not None
    (request_of.run_dir / record_for("run-killed").filename).write_text("not json", encoding="utf-8")
    [settled] = settle_side_files(batch, DECLARED, remove_unnamed=True)
    assert settled.action == "removed"


def test_r15_the_recovery_touches_only_the_temporary_names_of_declared_side_files(batch, run_dir):
    """R15: other dot-files (a record's own interrupted write, another file's temporary), a temporary file of an
    undeclared name, one without an attempt number, and a link named like a temporary file are left as they are."""
    run_dir.mkdir(parents=True)
    outside = run_dir.parent / "outside.bin"
    outside.write_bytes(b"not a side file")
    left = {
        ".record.json.abc.tmp": b"a record's interrupted write",
        f".{LOGGED.pattern}.tmp-x": b"no attempt number",
        f".{LOGGED.pattern}.tmp-": b"no attempt number",
        ".undeclared.bin.tmp-1": b"an undeclared name",
        f".{LOGGED.pattern}.bak": b"another suffix",
    }
    for name, data in left.items():
        (run_dir / name).write_bytes(data)
    (run_dir / temporary_name(LOGGED.pattern, 3)).symlink_to(outside)
    assert settle_side_files(batch, DECLARED) == []
    assert {p.name: p.read_bytes() for p in run_dir.iterdir() if not p.is_symlink()} == left
    assert (run_dir / temporary_name(LOGGED.pattern, 3)).is_symlink()
    assert outside.read_bytes() == b"not a side file"


def test_r15_the_recovery_settles_each_declared_side_file_of_a_directory_on_its_own(request_of, batch):
    """R15: two declared files staged for one record: each temporary file is judged against its own field."""
    sandbox = FakeSandbox({LOGGED.pattern: CONTENT, SECOND.pattern: b"the second one"})
    first = stage_side_file(sandbox, request_of, DECLARED, LOGGED.pattern)
    second = stage_side_file(sandbox, request_of, DECLARED, SECOND.pattern)
    assert first is not None
    assert second is not None
    record_for(request_of.identity.run_key, extent=first.sha256, surplus=digest(b"another")).write(request_of.run_dir)
    settled = {s.path.rsplit("/", 1)[1]: s.action for s in settle_side_files(batch, DECLARED, remove_unnamed=True)}
    assert settled == {
        temporary_name(LOGGED.pattern, ATTEMPT): "finished",
        temporary_name(SECOND.pattern, ATTEMPT): "removed",
    }


def test_r15_a_batch_that_declares_no_side_file_is_left_alone(request_of, batch):
    """R15: with no declaration there is nothing to settle, and no file is read or touched."""
    _, staged = stage(request_of)
    assert staged is not None
    assert settle_side_files(batch, []) == []
    assert temporary(request_of.run_dir).exists()


def test_r15_the_recorded_name_of_a_temporary_file_is_fixed(request_of):
    """R15: the rule is stated by name in the docs, so the name is part of the interface."""
    assert temporary_name("extent_ranges.json.gz", 3) == ".extent_ranges.json.gz.tmp-3"
