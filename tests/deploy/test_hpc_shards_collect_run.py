"""O63: `shards.py collect --run` (collect, then merge at once) does not merge a campaign that a snapshot is ahead of.
A shard resumed from its archive and killed leaves a snapshot with runs the archive lacks: plain `collect` warns, lists
the snapshot for the copy back and prints the merge line, which leaves those runs out for the operator to run
knowingly; `collect --run` refuses before any merge, naming the shard and the snapshot, because a merge that silently
omitted them would analyse an incomplete campaign. `merge` has no flag to merge a shard short, so there is none to
reuse. Every path and name here is invented."""

import importlib.util
import json
import subprocess
import sys

import pytest
from copy_back_support import (
    KEYS,
    MANIFEST,
    SCRIPT,
    archive_sha,
    archived,
    ended_run,
    live_out,
    make_plan,
    plan_file,
    resumed_and_killed,
    step_shard,
    take_snapshot,
)


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_collect_run", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_collect_run"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_collect_run", None)


@pytest.fixture
def no_merge(shards, monkeypatch):
    """`subprocess.run` of the module, which is what runs the merge: recorded, never run."""
    calls = []

    def recorded(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(shards.subprocess, "run", recorded)
    return calls


def _collect(shards, tmp_path, *flags):
    return shards.main(["collect", "--manifest", str(plan_file(tmp_path)), "--into", str(tmp_path / "merge"), *flags])


def test_collect_alone_after_a_resumed_step_was_killed_warns_lists_the_snapshot_and_prints_the_merge_line(
    shards, tmp_path, capsys, no_merge
):
    """O63: exit 0, the merge line alone on stdout, a warning naming the shard and the snapshot on stderr, and the
    snapshot's files in the manifest the operator pulls. Nothing is merged."""
    plan = resumed_and_killed(shards, tmp_path)
    capsys.readouterr()
    assert _collect(shards, tmp_path) == 0
    seen = capsys.readouterr()
    assert len(seen.out.strip().splitlines()) == 1
    assert "merge" in seen.out
    assert "fed-a" in seen.err
    assert "snap-000002" in seen.err
    assert "newer than its archive" in seen.err
    listed = json.loads((plan.results / MANIFEST).read_text(encoding="utf-8"))["evidence"]
    assert any(entry["name"].startswith("fed-a/snapshots/snap-000002/") for entry in listed)
    assert no_merge == []


def test_collect_run_after_a_resumed_step_was_killed_refuses_before_any_merge(shards, tmp_path, capsys, no_merge):
    """O63: exit 2, naming the shard and the snapshot and what to do (resume the shard, or merge without it by running
    `collect` alone and the line it prints). No merge runs, no manifest is written and no directory is left."""
    plan = resumed_and_killed(shards, tmp_path)
    capsys.readouterr()
    assert _collect(shards, tmp_path, "--run") == 2
    seen = capsys.readouterr()
    assert "fed-a" in seen.err
    assert "snap-000002" in seen.err
    assert "resume" in seen.err
    assert "without --run" in seen.err
    assert seen.out == ""
    assert no_merge == []
    assert not (plan.results / MANIFEST).exists()
    assert not (tmp_path / "merge").exists()


def test_collect_run_names_every_shard_a_snapshot_is_ahead_of(shards, tmp_path, no_merge):
    """O63: both shards, in one refusal."""
    plan = make_plan(shards, tmp_path)
    for index, label in enumerate(("fed-a", "fed-b"), 1):
        store, out = live_out(tmp_path, label)
        ended_run(store, out, KEYS[0])
        take_snapshot(tmp_path, store, out, label, archive_sha(tmp_path, label), index=index)
        store.close()
    with pytest.raises(shards.ShardsError) as refused:
        shards.collect(plan, tmp_path / "merge", refuse_newer=True)
    assert "fed-a" in str(refused.value)
    assert "fed-b" in str(refused.value)
    assert str(refused.value).count("snap-000001") == 2


def test_collect_run_still_merges_when_every_snapshot_is_older_than_its_archive(shards, tmp_path, no_merge):
    """O63: a step that ended took snapshots, and none is ahead of its archive: the merge runs, in the target."""
    plan = shards.load_plan(plan_file(tmp_path))
    archived(shards, tmp_path, "fed-b", 2, journals=False)
    assert step_shard(shards, tmp_path, resume=False) == 0
    assert (plan.results / "fed-a" / "snapshots").is_dir()
    assert _collect(shards, tmp_path, "--run") == 0
    assert len(no_merge) == 1
    argv, kwargs = no_merge[0]
    assert argv[1:4] == ["-I", "-B", "-m"]
    assert "merge" in argv
    assert kwargs["cwd"] == tmp_path / "merge"
    assert (plan.results / MANIFEST).is_file()
