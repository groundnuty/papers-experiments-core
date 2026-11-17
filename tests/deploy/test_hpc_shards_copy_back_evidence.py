"""O63: what a shard keeps beside its archive, copied back with it. A shard's spend journals (a new file per step:
what a killed step spent, which `--resume` counts as `lost_in_kill`), its archive manifest `<label>.out.json` and the
newest snapshot of its `out/` that is newer than the verified archive (a step resumed from the archive and killed took
it: it holds runs the archive lacks) are listed by `shards.py collect` in `archives.sha256.json` and checked by
`verify-copy`, because the site's scratch is cleared after the pull. Snapshots that are not newer than the archive are
resume material and not listed. Every path and name here is invented."""

import importlib.util
import json
import os
import shutil
import sys

import pytest
from copy_back_support import (
    JOURNALS,
    KEYS,
    MANIFEST,
    SCRIPT,
    archive_sha,
    archived,
    ended_run,
    live_out,
    make_plan,
    plan_file,
    relative,
    resumed_and_killed,
    sha_of,
    step_shard,
    take_snapshot,
    with_newer_snapshot,
)

from expcore.campaign import out_snapshot


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_copy_back_evidence", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_copy_back_evidence"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_copy_back_evidence", None)


def _evidence(plan):
    return json.loads((plan.results / MANIFEST).read_text(encoding="utf-8"))["evidence"]


def _names(plan):
    return [entry["name"] for entry in _evidence(plan)]


def _journal_names(label):
    return [f"{label}/journals/{name}" for name in JOURNALS[label]]


# ------------------------------------------------------------------------------------------------ collect's evidence
def test_the_manifest_lists_each_shards_journals_and_archive_manifest_with_their_hashes(shards, tmp_path):
    """O63: every file under `<label>/journals/` and `<label>.out.json`, with its relative name, size and SHA-256, the
    names sorted, taken from the files and never from a step's claim."""
    plan = make_plan(shards, tmp_path)
    shards.collect(plan, tmp_path / "merge")
    wanted = sorted(["fed-a.out.json", "fed-b.out.json", *_journal_names("fed-a"), *_journal_names("fed-b")])
    assert _evidence(plan) == [
        {"name": name, "bytes": (plan.results / name).stat().st_size, "sha256": sha_of(plan.results / name)}
        for name in wanted
    ]


def test_the_manifest_with_its_evidence_is_the_same_for_every_collect_of_unchanged_results(shards, tmp_path):
    """O63: no timestamp and no directory in it; a journal that grows between two collects changes it, as it must."""
    plan = make_plan(shards, tmp_path)
    shards.collect(plan, tmp_path / "first")
    first = (plan.results / MANIFEST).read_bytes()
    shards.collect(plan, tmp_path / "second")
    assert (plan.results / MANIFEST).read_bytes() == first
    assert str(tmp_path) not in first.decode()
    journal = plan.results / "fed-a" / "journals" / next(iter(JOURNALS["fed-a"]))
    journal.write_bytes(journal.read_bytes() + b'{"spent": 9}\n')
    shards.collect(plan, tmp_path / "third")
    assert (plan.results / MANIFEST).read_bytes() != first


def test_a_shard_with_no_journals_directory_lists_its_archive_manifest_alone(shards, tmp_path):
    """O63: no journal is not an error; the archive manifest is listed whatever else is there."""
    plan = shards.load_plan(plan_file(tmp_path))
    archived(shards, tmp_path, "fed-a", 1, journals=False)
    archived(shards, tmp_path, "fed-b", 2)
    shards.collect(plan, tmp_path / "merge")
    assert _names(plan) == sorted(["fed-a.out.json", "fed-b.out.json", *_journal_names("fed-b")])


def test_a_journal_in_a_subdirectory_is_listed_by_its_relative_name(shards, tmp_path):
    """O63: every file under journals/ is evidence, wherever it lies."""
    plan = make_plan(shards, tmp_path)
    (plan.results / "fed-a" / "journals" / "old").mkdir()
    (plan.results / "fed-a" / "journals" / "old" / "one.jsonl").write_text("{}\n")
    shards.collect(plan, tmp_path / "merge")
    assert "fed-a/journals/old/one.jsonl" in _names(plan)


@pytest.mark.parametrize("kind", ["file link", "directory link", "directory of links", "journals is a link"])
def test_a_link_among_the_journals_refuses_the_collect_whole_naming_the_shard(shards, tmp_path, kind):
    """O63: a link is never followed to be hashed, so the pull could not reproduce it: the collect is refused, naming
    the shard, with no manifest and no directory left."""
    plan = make_plan(shards, tmp_path)
    journals = plan.results / "fed-a" / "journals"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.jsonl").write_text("{}\n")
    if kind == "file link":
        (journals / "link.jsonl").symlink_to(elsewhere / "x.jsonl")
    elif kind == "directory link":
        (journals / "dir").symlink_to(elsewhere)
    elif kind == "directory of links":
        (journals / "sub").mkdir()
        (journals / "sub" / "link.jsonl").symlink_to(elsewhere / "x.jsonl")
    else:
        shutil.rmtree(journals)
        journals.symlink_to(elsewhere)
    with pytest.raises(shards.ShardsError, match="fed-a"):
        shards.collect(plan, tmp_path / "merge")
    assert not (plan.results / MANIFEST).exists()
    assert not (tmp_path / "merge").exists()


def test_an_archive_manifest_that_no_longer_names_the_verified_archive_refuses_the_collect(
    shards, tmp_path, monkeypatch
):
    """O63: the `.out.json` listed is the one of the archive hashed. A step that published another between the two
    reads (collect run while a step ends) would have the manifest vouch for a pair that never existed."""
    plan = make_plan(shards, tmp_path)
    real = shards.verified

    def verified_then_replaced(results, shard, scratch):
        found = real(results, shard, scratch)
        own = results / f"{shard.label}.out.json"
        own.write_text(json.dumps(json.loads(own.read_text()) | {"sha256": "0" * 64}))
        return found

    monkeypatch.setattr(shards, "verified", verified_then_replaced)
    with pytest.raises(shards.ShardsError, match="fed-a"):
        shards.collect(plan, tmp_path / "merge")
    assert not (plan.results / MANIFEST).exists()


# ------------------------------------------------------------------------------------------------ the snapshots
def test_a_snapshot_newer_than_the_verified_archive_is_listed_with_every_file_and_warned_of(shards, tmp_path, capsys):
    """O63: its `facts.base` is the archive's SHA-256, so a step started from the archive and was killed: the snapshot
    holds a run the merge will not. Every file of it (store, runs/, its manifest) is listed with its hash, and the
    collect says so, naming the shard and the snapshot (it does not refuse: the merge is of the archive)."""
    plan, snapshot = with_newer_snapshot(shards, tmp_path)
    shards.collect(plan, tmp_path / "merge")
    seen = capsys.readouterr().err
    assert "fed-a" in seen
    assert snapshot.name in seen
    assert "newer than its archive" in seen
    assert "fed-b" not in seen
    files = sorted(relative(path, plan.results) for path in snapshot.rglob("*") if path.is_file())
    assert files
    assert f"fed-a/snapshots/{snapshot.name}/snapshot.json" in files
    assert f"fed-a/snapshots/{snapshot.name}/state.sqlite" in files
    assert any("/runs/" in name for name in files)
    listed = [entry for entry in _evidence(plan) if "/snapshots/" in entry["name"]]
    assert listed == [
        {"name": name, "bytes": (plan.results / name).stat().st_size, "sha256": sha_of(plan.results / name)}
        for name in files
    ]


def test_a_snapshot_taken_before_the_archive_is_resume_material_and_not_listed(shards, tmp_path, capsys):
    """O63: a snapshot that names no archive (the shard's first step) or an older one was taken before the archive and
    holds nothing it lacks: not listed, and nothing said."""
    plan = make_plan(shards, tmp_path)
    store, out = live_out(tmp_path, "fed-a")
    ended_run(store, out, KEYS[0])
    take_snapshot(tmp_path, store, out, "fed-a", None)  # the first step's own: its base is none
    take_snapshot(
        tmp_path, store, out, "fed-b", "0" * 64, index=2, stamp="20261116T150100Z"
    )  # one of an earlier archive
    store.close()
    shards.collect(plan, tmp_path / "merge")
    assert not [name for name in _names(plan) if "/snapshots/" in name]
    assert "newer" not in capsys.readouterr().err


def test_only_the_newest_snapshot_that_verifies_is_listed(shards, tmp_path):
    """O63: the one `--resume` would restore (`restore_newest`, with the archive named as the base): of two that name
    the archive, the newer, and its older sibling is left out."""
    plan = make_plan(shards, tmp_path)
    store, out = live_out(tmp_path, "fed-a")
    base = archive_sha(tmp_path, "fed-a")
    ended_run(store, out, KEYS[0])
    older = take_snapshot(tmp_path, store, out, "fed-a", base)
    ended_run(store, out, KEYS[1])
    newer = take_snapshot(tmp_path, store, out, "fed-a", base, stamp="20261116T150100Z")
    store.close()
    shards.collect(plan, tmp_path / "merge")
    snapshots = {name.split("/")[2] for name in _names(plan) if "/snapshots/" in name}
    assert snapshots == {newer.name} != {older.name}


def test_a_newest_snapshot_that_does_not_verify_falls_back_to_the_older_one_and_says_so(shards, tmp_path, capsys):
    """O63: a snapshot a resume would not use (a file changed since it was taken) is never listed as the evidence."""
    plan = make_plan(shards, tmp_path)
    store, out = live_out(tmp_path, "fed-a")
    base = archive_sha(tmp_path, "fed-a")
    ended_run(store, out, KEYS[0])
    older = take_snapshot(tmp_path, store, out, "fed-a", base)
    ended_run(store, out, KEYS[1])
    newer = take_snapshot(tmp_path, store, out, "fed-a", base, stamp="20261116T150100Z")
    store.close()
    record = next(newer.rglob("record.json"))
    record.write_text('{"changed": true}')
    shards.collect(plan, tmp_path / "merge")
    snapshots = {name.split("/")[2] for name in _names(plan) if "/snapshots/" in name}
    assert snapshots == {older.name}
    assert f"snapshot {newer.name} is not used" in capsys.readouterr().err


def test_a_snapshot_of_another_shard_refuses_the_collect_naming_it_as_a_resume_does(shards, tmp_path):
    """O63: `--resume` refuses it (O62), so `collect` picks the same and does the same."""
    plan = make_plan(shards, tmp_path)
    store, out = live_out(tmp_path, "fed-a")
    ended_run(store, out, KEYS[0])
    facts = {"shard": {"index": 2, "count": 2, "label": "fed-b"}, "base": archive_sha(tmp_path, "fed-a")}
    out_snapshot.take(
        out, tmp_path / "results" / "fed-a" / "snapshots", scratch=tmp_path / "scratch", facts=facts, live=True, now="x"
    )
    store.close()
    with pytest.raises(shards.ShardsError, match=r"fed-a.*snap-000001"):
        shards.collect(plan, tmp_path / "merge")
    assert not (plan.results / MANIFEST).exists()


def test_the_check_of_a_snapshot_leaves_nothing_in_the_directory_collected_into(shards, tmp_path):
    """O63: the snapshot is read through a restore into a private directory under the target, which is removed: the
    merge, run there, sees the shards' directories and nothing else."""
    plan, _ = with_newer_snapshot(shards, tmp_path)
    shards.collect(plan, tmp_path / "merge")
    assert sorted(os.listdir(tmp_path / "merge")) == ["fed-a", "fed-b"]


def test_a_collect_refused_after_a_snapshot_was_read_leaves_nothing_behind(shards, tmp_path, monkeypatch):
    """O63: the manifest cannot be written (the results directory is full) once the snapshot has been read through its
    restore: refused whole, and the target, with that private directory, is gone."""
    plan, _ = with_newer_snapshot(shards, tmp_path)

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shards, "_write_whole", full)
    with pytest.raises(shards.ShardsError, match="manifest"):
        shards.collect(plan, tmp_path / "merge")
    assert not (tmp_path / "merge").exists()


def _snapshot_names(plan):
    return sorted({name.split("/")[2] for name in _names(plan) if "/snapshots/" in name})


def test_a_shard_whose_step_ended_normally_lists_no_snapshot_though_its_step_took_them(shards, tmp_path, capsys):
    """O63: through the real step: the snapshots it took name no archive (it started from none), so they are older than
    the archive it wrote and hold nothing it lacks. Its journal and archive manifest are listed."""
    plan = shards.load_plan(plan_file(tmp_path))
    archived(shards, tmp_path, "fed-b", 2, journals=False)  # the plan's other shard, not the one stepped here
    assert step_shard(shards, tmp_path, resume=False) == 0
    assert (tmp_path / "results" / "fed-a" / "snapshots" / "snap-000001").is_dir()  # the last, taken after the exit
    capsys.readouterr()
    shards.collect(plan, tmp_path / "merge")
    assert _snapshot_names(plan) == []
    assert len([name for name in _names(plan) if name.startswith("fed-a/journals/")]) == 1
    assert "fed-a.out.json" in _names(plan)
    assert "newer than its archive" not in capsys.readouterr().err


def test_a_step_resumed_from_the_archive_and_killed_leaves_a_snapshot_that_collect_lists(shards, tmp_path, capsys):
    """O63: through the real step: its snapshots name the archive it resumed from, which is still the shard's archive
    (nothing newer was written), so the newest holds the run the archive lacks: listed, with both steps' journals, and
    the collect warns. The first step's snapshot, older than the archive, is still not."""
    plan = resumed_and_killed(shards, tmp_path)
    capsys.readouterr()
    shards.collect(plan, tmp_path / "merge")
    assert _snapshot_names(plan) == ["snap-000002"]
    assert len([name for name in _names(plan) if name.startswith("fed-a/journals/")]) == 2
    seen = capsys.readouterr().err
    assert "snap-000002" in seen
    assert "newer than its archive" in seen
