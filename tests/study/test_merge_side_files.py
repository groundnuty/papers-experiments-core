"""R15 (#304): `merge` and the check of a merged batch settle the side files a kill left, as `check` does.

The shards are the example study with side files (`side_file_support.declare_side_files`), run for real as shards 1 and
2 of 2. A kill after a record that names a side file and before its rename leaves the file under its temporary name
(`temporary_name`); each test plants that state, or a variation of it, in a copy of a finished shard or of a merged
batch, and asks `merge` or `check` what they make of it. Before #304 both refused it as a side file that is missing,
though `check` run on the shard first would have settled it.
"""

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from shard_support import IDENTITY, declaring, make_study, merge_into
from side_file_support import SIDE, changed_log, declare_side_files, side_files, stored_check
from study_support import Campaign, clean_environment, cli, copy_campaign
from tree_support import read_only, tree_of

from expcore.replay.side_file_placement import temporary_name

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


@pytest.fixture(scope="module")
def base(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("merge-side-files")


def side_study(base: Path, name: str) -> Campaign:
    """A copy of the example with side files, whose unit caps are those two shards share (`make_study`)."""
    return Campaign(declare_side_files(make_study(base, name)))


def side_shard(base: Path, name: str, env: dict[str, str], index: int) -> Campaign:
    """Run shard `index` of 2 of the study with side files, as `run --shard` does."""
    shard = side_study(base, name)
    with pytest.MonkeyPatch.context() as patch:
        declaring(patch, IDENTITY)
        code, errors = cli(env, "run", str(shard.root), "--shard", f"{index}/2", "--shard-label", f"shard-{index}")
    assert code == 0, errors
    assert side_files(shard.runs), "the shard's runs wrote no side file"
    return shard


@pytest.fixture(scope="module")
def shard1(base, clean_environment) -> Campaign:
    return side_shard(base, "shard-one", clean_environment, 1)


@pytest.fixture(scope="module")
def shard2(base, clean_environment) -> Campaign:
    return side_shard(base, "shard-two", clean_environment, 2)


@pytest.fixture(scope="module")
def merged(base, clean_environment, shard1, shard2) -> Campaign:
    """The two shards merged, as they finished, into a copy of the study with side files."""
    target = side_study(base, "merged")
    code, errors = merge_into(clean_environment, target, shard1, shard2)
    assert code == 0, errors
    return target


def make_writable(root: Path) -> None:
    """Give the owner write access to every directory below `root`: a batch merged from a read-only shard keeps the
    shard's directory modes, which a removal of the test's directory would stop at."""
    for path in [root, *root.rglob("*")]:
        if path.is_dir() and not path.is_symlink():
            path.chmod(path.stat().st_mode | 0o700)


@pytest.fixture
def target(tmp_path) -> Iterator[Campaign]:
    """A study to merge into, with nothing merged yet."""
    campaign = side_study(tmp_path, "target")
    yield campaign
    make_writable(campaign.root)


def leave_under_temporary_name(batch_of: Campaign) -> tuple[Path, Path]:
    """Leave the first side file of a finished campaign under its temporary name, as a kill between its record and
    its rename does; return where it lay and where it lies now."""
    placed = side_files(batch_of.runs)[0]
    temporary = placed.with_name(temporary_name(SIDE, 1))
    placed.rename(temporary)
    return placed, temporary


def merge_error(env, target: Campaign, *shards: Campaign) -> str:
    """Merge, expect a refusal that leaves no batch and no staging directory, and return what it said."""
    code, errors = merge_into(env, target, *shards)
    assert code == 1, errors
    assert errors.splitlines()[-1].startswith("merge refused: "), errors
    out = target.root / "out"
    assert not out.exists(), "a refused merge wrote the batch"
    assert not list(out.parent.glob(f".{out.name}.merge-*")), "a refused merge left its staging directory"
    return errors


def has_temporary_file(batch: Path) -> bool:
    return any(batch.rglob(f".{SIDE}.tmp-*"))


# --- the shard being merged ------------------------------------------------------------------------------------------


def test_r15_merge_finishes_the_rename_a_kill_left_in_a_shard_and_the_merged_batch_passes_check(
    clean_environment, shard1, shard2, target, tmp_path
):
    """R15 (#304): a shard whose attempt was killed after its record and before the rename of its side file is merged,
    not refused as missing a file: the rename is finished in the shard (the record names the file's SHA-256), said on
    stderr with the shard's name, the merged batch holds the file under its declared name, and `check` passes it. A
    shard with nothing to settle is not written to."""
    killed = copy_campaign(shard1, tmp_path, "killed")
    placed, temporary = leave_under_temporary_name(killed)
    kept = temporary.read_bytes()
    untouched = tree_of(shard2.root / "out")
    code, errors = merge_into(clean_environment, target, killed, shard2)
    assert code == 0, errors
    shown = temporary.relative_to(killed.runs).as_posix()
    assert f"merge: shard 1 (shard-1): finished {shown}: " in errors
    assert placed.read_bytes() == kept
    assert not temporary.exists()
    assert tree_of(shard2.root / "out") == untouched
    in_batch = target.runs / placed.relative_to(killed.runs)
    assert in_batch.read_bytes() == kept
    assert not has_temporary_file(target.runs)
    code, errors = cli(clean_environment, "check", str(target.runs))
    assert code == 0, errors
    assert stored_check(target.runs)["failures"] == []


def test_r15_merge_reports_what_a_read_only_shard_cannot_settle_with_the_fix_and_writes_nothing(
    clean_environment, shard1, shard2, target, tmp_path
):
    """R15 (#304): a shard whose attempt directory is read-only (a copy fetched from another host) is never written
    to. The merge is refused, and the refusal names the file under its temporary name and the fix (make the shard's
    out/ writable and merge again), not only a file that is missing; the shard stays byte for byte, and the merge that
    follows the fix passes."""
    killed = copy_campaign(shard1, tmp_path, "killed")
    placed, temporary = leave_under_temporary_name(killed)
    before = tree_of(killed.root / "out")
    with read_only(temporary.parent):
        errors = merge_error(clean_environment, target, killed, shard2)
        assert tree_of(killed.root / "out") == before
    shown = temporary.relative_to(killed.runs).as_posix()
    assert "shard 1 (shard-1)" in errors
    assert f"merge: shard 1 (shard-1): left {shown}: its directory is read-only" in errors
    assert "make the shard's out/ writable" in errors
    assert "merge again" in errors
    assert temporary.is_file()
    assert not placed.exists()
    code, errors = merge_into(clean_environment, target, killed, shard2)
    assert code == 0, errors
    assert placed.is_file()
    assert (target.runs / placed.relative_to(killed.runs)).is_file()


def test_r15_merge_does_not_blame_a_read_only_shard_for_a_failure_it_did_not_cause(
    clean_environment, shard1, shard2, target, tmp_path
):
    """R15 (#304): a read-only shard whose temporary file is redundant (the file is in place with the same SHA-256) is
    merged as it is, not refused for what it cannot settle: nothing the replay check reads is wrong."""
    copy = copy_campaign(shard1, tmp_path, "redundant")
    placed = side_files(copy.runs)[0]
    temporary = placed.with_name(temporary_name(SIDE, 1))
    shutil.copyfile(placed, temporary)
    before = tree_of(copy.root / "out")
    with read_only(temporary.parent):
        code, errors = merge_into(clean_environment, target, copy, shard2)
        assert tree_of(copy.root / "out") == before
    assert code == 0, errors
    assert f"left {temporary.relative_to(copy.runs).as_posix()}: its directory is read-only" in errors


def test_r15_merge_still_refuses_a_temporary_file_that_is_not_the_one_the_record_names(
    clean_environment, shard1, shard2, target, tmp_path
):
    """R15 (#304): the settling puts a file in place only for the hash its record names, so a temporary file of other
    bytes is never renamed: the shard fails the replay check as before, with the side file missing, and the file is
    left where it lies, as the evidence it is."""
    killed = copy_campaign(shard1, tmp_path, "killed")
    placed, temporary = leave_under_temporary_name(killed)
    changed_log(temporary)
    wrong = temporary.read_bytes()
    errors = merge_error(clean_environment, target, killed, shard2)
    assert "shard 1 (shard-1)" in errors
    assert "fail the replay check" in errors
    assert "[side_file]" in errors
    assert "but no such file lies beside it" in errors
    assert not placed.exists()
    assert temporary.read_bytes() == wrong


def test_r15_a_refused_merge_leaves_the_renames_it_finished_in_an_earlier_shard(
    clean_environment, shard1, shard2, target, tmp_path
):
    """R15 (#304): a merge refused on shard 2 has already settled shard 1, which it replays first. The rename is what
    `check` would have made, so it stays, and it is the only change: nothing is written to OUT."""
    killed = copy_campaign(shard1, tmp_path, "killed")
    placed, temporary = leave_under_temporary_name(killed)
    bad = copy_campaign(shard2, tmp_path, "bad")
    _, wrong = leave_under_temporary_name(bad)
    changed_log(wrong)
    errors = merge_error(clean_environment, target, killed, bad)
    assert "shard 2 (shard-2)" in errors
    assert "fail the replay check" in errors
    assert placed.is_file()
    assert not temporary.exists()
    assert wrong.is_file()


def test_r15_merge_removes_nothing_from_a_shard_it_cannot_prove_has_no_writer(
    clean_environment, shard1, shard2, target, tmp_path
):
    """R15 (#304): a temporary file that no record names (a kill before the record) is a partial file; removing it
    needs the campaign's lock, which a merge, a reader of the shard, does not take. It stays in the shard as it is,
    the merge passes (a dot-name is never read), and `check` of the merged batch passes as well."""
    stray = copy_campaign(shard1, tmp_path, "stray")
    beside = side_files(stray.runs)[0].parent.parent / "try9"
    beside.mkdir()
    partial = beside / temporary_name(SIDE, 9)
    partial.write_bytes(b"killed before its record was written")
    code, errors = merge_into(clean_environment, target, stray, shard2)
    assert code == 0, errors
    assert "removed" not in errors
    assert partial.read_bytes() == b"killed before its record was written"
    code, errors = cli(clean_environment, "check", str(target.runs))
    assert code == 0, errors


# --- the merged batch ------------------------------------------------------------------------------------------------


def test_r15_check_of_a_merged_batch_finishes_the_rename_a_record_names_and_passes(clean_environment, merged, tmp_path):
    """R15 (#304): a merged batch holding a side file under its temporary name, which a record beside it names, is
    settled by `check` like any batch, not refused as missing: the rename is finished, said on stderr, the check
    passes and hashes the file. No lock file is made in the merged batch's out/."""
    copy = copy_campaign(merged, tmp_path, "merged-copy")
    placed, temporary = leave_under_temporary_name(copy)
    kept = temporary.read_bytes()
    code, errors = cli(clean_environment, "check", str(copy.runs))
    assert code == 0, errors
    assert f"check: finished {temporary.relative_to(copy.runs).as_posix()}: " in errors
    assert placed.read_bytes() == kept
    assert not temporary.exists()
    assert stored_check(copy.runs)["failures"] == []
    assert placed.relative_to(copy.runs).as_posix() in stored_check(copy.runs)["hashed_only"]
    assert not (copy.root / "out" / "campaign.lock").exists()


def test_r15_check_of_a_merged_batch_reports_what_a_read_only_directory_cannot_settle(
    clean_environment, merged, tmp_path
):
    """R15 (#304): in a directory that cannot be written the check says what it left and how to settle it, and fails
    the side file as before; the directory is not written to."""
    copy = copy_campaign(merged, tmp_path, "merged-copy")
    placed, temporary = leave_under_temporary_name(copy)
    with read_only(temporary.parent):
        code, errors = cli(clean_environment, "check", str(copy.runs))
        assert temporary.is_file()
        assert not placed.exists()
    assert code == 1, errors
    assert f"check: left {temporary.relative_to(copy.runs).as_posix()}: its directory is read-only" in errors
    assert "make the directory writable and run the command again" in errors


def test_r15_check_of_a_merged_batch_still_refuses_a_temporary_file_of_other_bytes_and_removes_nothing(
    clean_environment, merged, tmp_path
):
    """R15 (#304): a merged batch has no lock to prove that no attempt writes in it, so its check finishes only a
    rename a record names. A temporary file of other bytes is not renamed (the side file stays missing and the check
    fails), and a partial one no record names is left where it lies."""
    copy = copy_campaign(merged, tmp_path, "merged-copy")
    placed, temporary = leave_under_temporary_name(copy)
    changed_log(temporary)
    partial = side_files(copy.runs)[0].parent.parent / "try9" / temporary_name(SIDE, 9)
    partial.parent.mkdir()
    partial.write_bytes(b"killed before its record was written")
    code, errors = cli(clean_environment, "check", str(copy.runs))
    assert code == 1, errors
    assert not placed.exists()
    assert temporary.is_file()
    assert partial.is_file()
    [failure] = [f for f in stored_check(copy.runs)["failures"] if f["stage"] == "side_file"]
    assert "but no such file lies beside it" in failure["error"]
