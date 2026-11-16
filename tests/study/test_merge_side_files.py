"""R15 (#304): `merge` and the check of a merged batch settle the side files a kill left, as `check` does, and the
merge never writes into a shard.

The shards are the example study with side files (`side_file_support.declare_side_files`), run for real as shards 1 and
2 of 2. A kill after a record that names a side file and before its rename leaves the file under its temporary name
(`temporary_name`); each test plants that state, or a variation of it, in a copy of a finished shard or of a merged
batch, and asks `merge` or `check` what they make of it. Before #304 both refused it as a side file that is missing,
though `check` run on the shard first would have settled it. The merge settles in a private copy of the shard's
`runs/`, so every test of it also holds the shard's `out/` byte for byte, directory modes and listing included.
"""

import contextlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from shard_support import IDENTITY, declaring, make_study, merge_into
from side_file_support import SIDE, changed_log, declare_side_files, side_files, stored_check
from study_support import Campaign, clean_environment, cli, copy_campaign
from tree_support import tree_of

from expcore.replay.side_file_placement import Settled, temporary_name
from expcore.study import merge as merge_module

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


@pytest.fixture
def target(tmp_path) -> Campaign:
    """A study to merge into, with nothing merged yet."""
    return side_study(tmp_path, "target")


def state_of(root: Path) -> tuple[dict[str, str], list[str]]:
    """What a directory holds: the SHA-256 of every file's bytes, and every entry's path and mode, directories too."""
    entries = [root, *root.rglob("*")]
    listing = [
        f"{path.relative_to(root).as_posix()}{'/' if path.is_dir() else ''} {stat.S_IMODE(path.lstat().st_mode):o}"
        for path in entries
    ]
    return tree_of(root), sorted(listing)


@contextlib.contextmanager
def sealed(root: Path) -> Iterator[Path]:
    """Make every directory of `root` read-only (mode 555, no entry can be made, renamed or removed in it), as a shard
    fetched read-only from another host is, and restore the modes on exit."""
    directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in directories}
    for path in directories:
        path.chmod(0o555)
    try:
        assert not os.access(root, os.W_OK), "the test runs with a user that ignores directory modes"
        yield root
    finally:
        for path in sorted(modes, key=lambda p: len(p.parts)):
            path.chmod(modes[path])


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


def test_r15_merge_finishes_the_rename_in_its_own_copy_and_leaves_the_shard_byte_for_byte(
    clean_environment, shard1, shard2, target, tmp_path
):
    """R15 (#304): a shard whose attempt was killed after its record and before the rename of its side file is merged,
    not refused as missing a file: the merged batch holds the file under its declared name, said on stderr with the
    shard's name, and `check` passes it. The shard's `out/` is byte for byte what it was (every file's bytes, every
    path and mode), the file still under its temporary name, and so is the other shard's; no staging directory stays."""
    killed = copy_campaign(shard1, tmp_path, "killed")
    placed, temporary = leave_under_temporary_name(killed)
    kept = temporary.read_bytes()
    before = state_of(killed.root / "out"), state_of(shard2.root / "out")
    code, errors = merge_into(clean_environment, target, killed, shard2)
    assert code == 0, errors
    assert (state_of(killed.root / "out"), state_of(shard2.root / "out")) == before
    assert temporary.read_bytes() == kept
    assert not placed.exists()
    shown = temporary.relative_to(killed.runs).as_posix()
    assert f"merge: shard 1 (shard-1): finished {shown}: " in errors
    in_batch = target.runs / placed.relative_to(killed.runs)
    assert in_batch.read_bytes() == kept
    assert not has_temporary_file(target.runs)
    assert sorted(p.name for p in (target.root / "out").iterdir()) == ["merge.json", "runs", "shards"]
    assert not list(target.root.glob(".*merge-*"))
    code, errors = cli(clean_environment, "check", str(target.runs))
    assert code == 0, errors
    assert stored_check(target.runs)["failures"] == []


def test_r15_merge_of_a_sealed_shard_settles_in_its_copy_and_writes_nothing_into_it(
    clean_environment, shard1, shard2, target, tmp_path
):
    """R15 (#304): a shard whose every directory is read-only (a copy fetched from another host) is merged all the
    same: the leftover is settled in the merge's copy, the shard is byte for byte as it was, directory modes included,
    and the merged batch is writable, not sealed like the shard."""
    killed = copy_campaign(shard1, tmp_path, "killed")
    placed, temporary = leave_under_temporary_name(killed)
    kept = temporary.read_bytes()
    with sealed(killed.root / "out"):
        before = state_of(killed.root / "out")
        code, errors = merge_into(clean_environment, target, killed, shard2)
        assert state_of(killed.root / "out") == before
    assert code == 0, errors
    assert temporary.is_file()
    assert not placed.exists()
    in_batch = target.runs / placed.relative_to(killed.runs)
    assert in_batch.read_bytes() == kept
    assert all(os.access(path, os.W_OK) for path in [target.runs, *target.runs.rglob("*")] if path.is_dir())
    code, errors = cli(clean_environment, "check", str(target.runs))
    assert code == 0, errors


def test_r15_merge_drops_a_redundant_copy_from_its_copy_and_keeps_the_shards(
    clean_environment, shard1, shard2, target, tmp_path
):
    """R15 (#304): a temporary file whose bytes are those of the file in place refuses nothing; it is not carried into
    the merged batch, and the shard, which is read-only here, keeps it."""
    copy = copy_campaign(shard1, tmp_path, "redundant")
    placed = side_files(copy.runs)[0]
    temporary = placed.with_name(temporary_name(SIDE, 1))
    temporary.write_bytes(placed.read_bytes())
    with sealed(copy.root / "out"):
        before = state_of(copy.root / "out")
        code, errors = merge_into(clean_environment, target, copy, shard2)
        assert state_of(copy.root / "out") == before
    assert code == 0, errors
    shown = temporary.relative_to(copy.runs).as_posix()
    assert f"merge: shard 1 (shard-1): removed {shown}: " in errors
    assert not has_temporary_file(target.runs)
    assert (target.runs / placed.relative_to(copy.runs)).read_bytes() == placed.read_bytes()


def test_r15_merge_still_refuses_a_temporary_file_that_is_not_the_one_the_record_names(
    clean_environment, shard1, shard2, target, tmp_path
):
    """R15 (#304): the settling puts a file in place only for the hash its record names, so a temporary file of other
    bytes is never renamed: the shard fails the replay check as before, with the side file missing, and the shard,
    wrong file included, is byte for byte what it was."""
    killed = copy_campaign(shard1, tmp_path, "killed")
    _, temporary = leave_under_temporary_name(killed)
    changed_log(temporary)
    before = state_of(killed.root / "out")
    errors = merge_error(clean_environment, target, killed, shard2)
    assert "shard 1 (shard-1)" in errors
    assert "fail the replay check" in errors
    assert "[side_file]" in errors
    assert "but no such file lies beside it" in errors
    assert state_of(killed.root / "out") == before


def test_r15_merge_leaves_a_partial_file_in_the_shard_and_out_of_the_merged_batch(
    clean_environment, shard1, shard2, target, tmp_path
):
    """R15 (#304): a temporary file that no record names (a kill before the record) is a partial file. Removing it
    from the shard is not the merge's to do and no attempt can be writing in the merge's own copy, so the copy loses
    it, said on stderr, and the shard keeps it as the evidence it is. The merged batch passes `check`."""
    stray = copy_campaign(shard1, tmp_path, "stray")
    beside = side_files(stray.runs)[0].parent.parent / "try9"
    beside.mkdir()
    partial = beside / temporary_name(SIDE, 9)
    partial.write_bytes(b"killed before its record was written")
    before = state_of(stray.root / "out")
    code, errors = merge_into(clean_environment, target, stray, shard2)
    assert code == 0, errors
    assert state_of(stray.root / "out") == before
    assert f"merge: shard 1 (shard-1): removed {partial.relative_to(stray.runs).as_posix()}: " in errors
    assert not has_temporary_file(target.runs)
    code, errors = cli(clean_environment, "check", str(target.runs))
    assert code == 0, errors


def test_r15_a_refused_merge_writes_into_no_shard_and_nothing_to_out(
    clean_environment, shard1, shard2, target, tmp_path
):
    """R15 (#304): a merge refused on shard 2 has replayed shard 1 first, in its copy: shard 1 keeps its leftover as it
    was, so neither shard's `out/` has changed by a byte, and no batch or staging directory is left."""
    killed = copy_campaign(shard1, tmp_path, "killed")
    leave_under_temporary_name(killed)
    bad = copy_campaign(shard2, tmp_path, "bad")
    _, wrong = leave_under_temporary_name(bad)
    changed_log(wrong)
    before = state_of(killed.root / "out"), state_of(bad.root / "out")
    errors = merge_error(clean_environment, target, killed, bad)
    assert "shard 2 (shard-2)" in errors
    assert "fail the replay check" in errors
    assert (state_of(killed.root / "out"), state_of(bad.root / "out")) == before


def test_r15_merge_refuses_what_its_own_staging_directory_cannot_settle(
    clean_environment, shard1, shard2, target, monkeypatch
):
    """R15 (#304): a settle that returns a write it could not make (the directory the merge copies into, beside OUT,
    cannot be written) refuses the merge, naming the file and the reason, rather than replaying a copy that lacks it."""
    stuck = Settled("ab/try1/.extent_ranges.json.gz.tmp-1", "finished", "the record names it", left=True)
    monkeypatch.setattr(merge_module, "settle_side_files", lambda *args, **kwargs: [stuck])
    errors = merge_error(clean_environment, target, shard1, shard2)
    assert "shard 1 (shard-1): 1 side file(s) that a kill left under a temporary name cannot be settled" in errors
    assert "ab/try1/.extent_ranges.json.gz.tmp-1" in errors
    assert "staging directory" in errors


def test_r15_merge_replays_a_directory_named_like_a_shard_s_own_check_file(
    clean_environment, shard1, shard2, target, tmp_path
):
    """R15 (#304): only a shard's own `replay-check.json` and snapshot files are left out of the copy the merge
    replays. A directory of one of those names is replayed, so a malformed record in it still refuses the shard, as it
    did when the shard was replayed in place."""
    trap = copy_campaign(shard1, tmp_path, "trap")
    hidden = trap.runs / "replay-check.json"
    hidden.mkdir()
    (hidden / f"{'0' * 64}.1.json").write_text("not a record", encoding="utf-8")
    before = state_of(trap.root / "out")
    errors = merge_error(clean_environment, target, trap, shard2)
    assert "shard 1 (shard-1)" in errors
    assert "fail the replay check" in errors
    assert state_of(trap.root / "out") == before


def test_r15_merge_refuses_an_out_inside_a_shard_whatever_the_path_to_it(
    clean_environment, shard1, shard2, target, tmp_path
):
    """R15 (#304): the merge writes a staging directory and the batch beside OUT, so an OUT that lies in a shard's
    out/, by its own path or through a link, is refused before anything is made: no directory is added to the shard."""
    inside = copy_campaign(shard1, tmp_path, "inside")
    link = tmp_path / "link-to-out"
    link.symlink_to(inside.root / "out", target_is_directory=True)
    before = state_of(inside.root / "out")
    for out in (inside.root / "out" / "merged", inside.root / "out" / "new" / "merged", link / "merged"):
        shards = [str(inside.root / "out"), str(shard2.root / "out")]
        code, errors = cli(clean_environment, "merge", str(out), *shards, "--study", str(target.root))
        assert code == 1, errors
        assert errors.splitlines()[-1].startswith("merge refused: "), errors
        assert "the out/ of a shard" in errors
        assert state_of(inside.root / "out") == before


# --- the merged batch ------------------------------------------------------------------------------------------------


def test_r15_check_of_a_merged_batch_finishes_the_rename_a_record_names_and_passes(clean_environment, merged, tmp_path):
    """R15 (#304): a merged batch holding a side file under its temporary name, which a record beside it names, is
    settled by `check` like any batch, not refused as missing: the rename is finished, said on stderr, the check
    passes and hashes the file. It touches nothing outside the batch's `runs/` (merge.json and the snapshots of the
    shards are what they were), and makes no lock file in the merged batch's out/."""
    copy = copy_campaign(merged, tmp_path, "merged-copy")
    placed, temporary = leave_under_temporary_name(copy)
    kept = temporary.read_bytes()
    outside = tree_of(copy.root / "out" / "shards"), (copy.root / "out" / "merge.json").read_bytes()
    code, errors = cli(clean_environment, "check", str(copy.runs))
    assert code == 0, errors
    assert f"check: finished {temporary.relative_to(copy.runs).as_posix()}: " in errors
    assert placed.read_bytes() == kept
    assert not temporary.exists()
    assert stored_check(copy.runs)["failures"] == []
    assert placed.relative_to(copy.runs).as_posix() in stored_check(copy.runs)["hashed_only"]
    assert (tree_of(copy.root / "out" / "shards"), (copy.root / "out" / "merge.json").read_bytes()) == outside
    assert not (copy.root / "out" / "campaign.lock").exists()


def test_r15_check_of_a_merged_batch_reports_what_a_read_only_directory_cannot_settle(
    clean_environment, merged, tmp_path
):
    """R15 (#304): in a directory that cannot be written the check says what it left and how to settle it, and fails
    the side file as before; the directory is not written to."""
    copy = copy_campaign(merged, tmp_path, "merged-copy")
    placed, temporary = leave_under_temporary_name(copy)
    with sealed(temporary.parent):
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
