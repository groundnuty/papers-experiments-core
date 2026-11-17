"""G9 (O63): `shards.py collect` is atomic. It unpacks every shard into a private directory beside `--into`, makes every
check there, and only then moves the trees into `--into`, publishes the root archives, and writes
`archives.sha256.json` (write, fsync, rename) as the last act in the results directory and `--into`, printing its hash.
A refusal anywhere before that leaves `--into` exactly as it was found and writes no manifest, no root archive and no
marker. Every path and name here is invented."""

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest
from copy_back_support import MANIFEST, SCRIPT, make_plan, plan_file, sha_of


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_collect_atomic", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_collect_atomic"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_collect_atomic", None)


def _replace_archive(results: Path, label: str, members: dict[str, bytes]) -> None:
    """The shard's archive as a step could have left it: whole, matching its manifest, holding `members` (a name and the
    bytes of a file, written as the tar gives it, so that a name may lead out of the tree)."""
    tar_path, json_path = results / f"{label}.out.tar", results / f"{label}.out.json"
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    manifest = json.loads(json_path.read_text())
    manifest["sha256"], manifest["bytes"] = sha_of(tar_path), tar_path.stat().st_size
    json_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")


def _listing(directory: Path) -> list[str]:
    return sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*"))


def _untouched(plan, tmp_path, results_before, parent_before):
    assert _listing(plan.results) == results_before  # no manifest, no root archive, no private copy
    assert sorted(p.name for p in tmp_path.iterdir()) == parent_before  # no staging directory is left beside `into`
    assert not [s.root for s in plan.shards if (s.root / "evidence-collected.json").exists()]


REFUSED = {
    "a member that is not under out/": {"etc/passwd": b"x", "out/campaign.lock": b""},
    "a member that leads out of the tree": {"out/campaign.lock": b"", "out/../../escape": b"x"},
}


@pytest.mark.parametrize("what", list(REFUSED))
def test_a_refusal_inside_the_unpack_of_a_later_shard_leaves_the_new_into_absent_and_writes_nothing(
    shards, tmp_path, what
):
    """O63 (G9): shard 1 unpacks cleanly, shard 2's archive is refused while it is unpacked (it verified against its
    manifest, so no earlier check saw it): `--into` is never made, the first tree is gone with the private directory,
    the manifest is not written (it was, before its unpack loop) and no root archive or marker was published."""
    plan = make_plan(shards, tmp_path)
    _replace_archive(plan.results, "fed-b", REFUSED[what])
    results_before, parent_before = _listing(plan.results), sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(shards.ShardsError, match="fed-b"):
        shards.collect(plan, tmp_path / "merge")
    assert not (tmp_path / "merge").exists()
    _untouched(plan, tmp_path, results_before, parent_before)
    assert not (plan.results / MANIFEST).exists()


def test_a_refusal_inside_the_unpack_leaves_an_existing_empty_into_as_it_was(shards, tmp_path):
    """O63 (G9): `--into` made beforehand (0700, empty) is still there, still empty, still 0700, with no tree in it."""
    plan = make_plan(shards, tmp_path)
    _replace_archive(plan.results, "fed-b", REFUSED["a member that is not under out/"])
    into = tmp_path / "merge"
    into.mkdir(mode=0o700)
    before = into.stat()
    results_before, parent_before = _listing(plan.results), sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(shards.ShardsError, match="fed-b"):
        shards.collect(plan, into)
    assert into.is_dir()
    assert list(into.iterdir()) == []
    assert (into.stat().st_mode & 0o777, into.stat().st_uid) == (before.st_mode & 0o777, before.st_uid)
    _untouched(plan, tmp_path, results_before, parent_before)


def test_the_command_exits_2_after_a_refusal_in_the_unpack_and_prints_no_hash_and_no_merge_line(
    shards, tmp_path, capsys
):
    """O63 (G9): what an operator sees: exit 2, the refusal on stderr, nothing on stdout (no merge line to run) and no
    `archives manifest ... sha256` line to pull against."""
    make_plan(shards, tmp_path)
    _replace_archive(tmp_path / "results", "fed-b", REFUSED["a member that is not under out/"])
    code = shards.main(["collect", "--manifest", str(plan_file(tmp_path)), "--into", str(tmp_path / "merge")])
    out, err = capsys.readouterr()
    assert code == 2
    assert out == ""
    assert "fed-b" in err
    assert "sha256" not in err
    assert not (tmp_path / "results" / MANIFEST).exists()


def test_a_refusal_before_any_unpack_still_leaves_everything_as_found(shards, tmp_path):
    """O63 (G9): the earlier refusals (a shard not final, a root not whole) leave no more than they did."""
    plan = make_plan(shards, tmp_path)
    (plan.shards[0].root / "facts.json").unlink()
    results_before, parent_before = _listing(plan.results), sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(shards.ShardsError, match="fed-a"):
        shards.collect(plan, tmp_path / "merge")
    _untouched(plan, tmp_path, results_before, parent_before)


def test_a_successful_collect_moves_the_trees_in_whole_read_only_and_leaves_no_private_directory(shards, tmp_path):
    """O63 (G9): each shard's `out/` is in `--into` as it was unpacked, read-only; the private directory beside it is
    gone; the private copies of the archives were never in `--into`."""
    plan = make_plan(shards, tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    outs = shards.collect(plan, tmp_path / "merge")
    assert [o.relative_to(tmp_path / "merge").as_posix() for o in outs] == ["fed-a/out", "fed-b/out"]
    assert all((o / "campaign.lock").is_file() and not (o.stat().st_mode & 0o222) for o in outs)
    assert sorted(p.name for p in (tmp_path / "merge").iterdir()) == ["fed-a", "fed-b"]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([*before, "merge"])
    assert (tmp_path / "merge").stat().st_mode & 0o777 == 0o700


def test_the_manifest_is_written_last_after_the_trees_and_the_root_archives_and_before_the_markers(
    shards, tmp_path, monkeypatch
):
    """O63 (G9): when `archives.sha256.json` is written, `--into` holds every tree and the results directory every root
    archive; the markers in the roots (what unlocks `down`) come only after it, so a failure leaves `down` refusing. It
    is written through a draft that is fsynced and renamed (`_write_whole`)."""
    plan = make_plan(shards, tmp_path)
    seen = {}
    real = shards._write_whole

    def watching(path, write):
        if path.name == MANIFEST:
            seen["trees"] = sorted(p.name for p in (tmp_path / "merge").iterdir())
            seen["root_archives"] = sorted(p.name for p in plan.results.glob("*.root.*"))
            seen["markers"] = [s.label for s in plan.shards if (s.root / "evidence-collected.json").exists()]
        seen.setdefault("order", []).append(path.name)
        real(path, write)

    monkeypatch.setattr(shards, "_write_whole", watching)
    shards.collect(plan, tmp_path / "merge")
    assert seen["trees"] == ["fed-a", "fed-b"]
    assert seen["root_archives"] == ["fed-a.root.json", "fed-a.root.tar", "fed-b.root.json", "fed-b.root.tar"]
    assert seen["markers"] == []
    assert seen["order"][-1] == MANIFEST
    assert [s.label for s in plan.shards if (s.root / "evidence-collected.json").exists()] == ["fed-a", "fed-b"]


def test_a_manifest_that_cannot_be_written_puts_the_into_back_as_found_and_writes_no_marker(
    shards, tmp_path, monkeypatch
):
    """O63 (G9): the one failure after the trees are moved is the manifest's own write (a full disk): `--into` is
    made as it was found, no marker is written, and the refusal names the manifest."""
    plan = make_plan(shards, tmp_path)
    real = shards._write_whole

    def full_for_the_manifest(path, write):
        if path.name == MANIFEST:
            raise OSError(28, "No space left on device")
        real(path, write)

    monkeypatch.setattr(shards, "_write_whole", full_for_the_manifest)
    parent_before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(shards.ShardsError, match="manifest"):
        shards.collect(plan, tmp_path / "merge")
    assert not (tmp_path / "merge").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == parent_before
    assert not (plan.results / MANIFEST).exists()
    assert not [s.root for s in plan.shards if (s.root / "evidence-collected.json").exists()]


def test_the_hash_collect_prints_is_the_hash_of_the_manifest_it_wrote(shards, tmp_path, capsys):
    """O63 (G9): printed on stderr after the merge line, once everything else is done; equal to the file's SHA-256."""
    make_plan(shards, tmp_path)
    assert shards.main(["collect", "--manifest", str(plan_file(tmp_path)), "--into", str(tmp_path / "merge")]) == 0
    out, err = capsys.readouterr()
    written = (tmp_path / "results" / MANIFEST).read_bytes()
    assert f"{MANIFEST} sha256 {hashlib.sha256(written).hexdigest()}" in err
    assert "expcore" in out.splitlines()[0]  # the merge line, alone on stdout
