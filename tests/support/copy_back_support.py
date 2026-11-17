"""Helpers of the copy-back tests of `deploy/hpc-federation/shards.py` (O63): a plan of two shards with their archives,
their spend journals and their archive manifests in a results directory, and the pull of that directory to the
operator's host. Every path and name here is invented."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from campaign_support import NOW, finished, make_manifest, make_schema
from root_evidence_support import make_root

from expcore.campaign import out_snapshot
from expcore.campaign.markers import write_marker
from expcore.campaign.store import Store

SCHEMA = make_schema()
ROWS = make_manifest(SCHEMA).rows
KEYS = [row.key.text for row in ROWS]
SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
MANIFEST = "archives.sha256.json"
LABELS = ("fed-a", "fed-b")
JOURNALS = {  # a step's journal is a file of its own: a new one each time the shard is started
    "fed-a": {
        "20261116T100000Z-111-aaaa0001.jsonl": b'{"spent": 1}\n{"spent": 2}\n',
        "20261116T130000Z-222-bbbb0002.jsonl": b'{"spent": 3}\n',
    },
    "fed-b": {"20261116T100500Z-333-cccc0003.jsonl": b'{"spent": 4}\n'},
}


def plan_file(tmp_path, labels=LABELS):
    lines = [
        f'study = "{tmp_path / "study"}"',
        f'python = "{tmp_path / "venv" / "python"}"',
        f'core_src = "{tmp_path / "core" / "src"}"',
        f'results = "{tmp_path / "results"}"',
        f'tmpfs = "{tmp_path / "tmpfs"}"',
        'step_path = "/usr/bin:/bin"',
        "cpus = 6",
        'memory = "18g"',
    ]
    for label in labels:
        lines += ["[[shards]]", f'label = "{label}"', f'root = "{tmp_path / "fed" / label}"']
        if not (tmp_path / "fed" / label).exists():  # the federation's root, with what a collect archives from it
            make_root(tmp_path / "fed" / label)
    path = tmp_path / "shards.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def archived(shards, tmp_path, label, index, count=2, lock=True, journals=True):
    """A shard's archive and its manifest in `<tmp>/results`, and (by default) the journals of its steps."""
    out = tmp_path / "src-out" / label / "out"
    (out / "runs").mkdir(parents=True)
    (out / "runs" / "r.json").write_text(json.dumps({"label": label, "pad": label * 200}))
    if lock:
        (out / "campaign.lock").write_text("")
    (tmp_path / "results").mkdir(exist_ok=True)
    shards.archive(out, tmp_path / "results", {"index": index, "count": count, "label": label}, 0)
    if journals:
        directory = tmp_path / "results" / label / "journals"
        directory.mkdir(parents=True, exist_ok=True)
        for name, content in JOURNALS.get(label, {}).items():
            (directory / name).write_bytes(content)


def make_plan(shards, tmp_path):
    """A plan of two shards, each with its archive and journals in `<tmp>/results`."""
    plan = shards.load_plan(plan_file(tmp_path))
    for shard in plan.shards:
        archived(shards, tmp_path, shard.label, shard.index)
    return plan


def sha_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pulled(shards, tmp_path):
    """What an operator holds after `collect` and a pull of the results directory (`rsync -a results/ local/`):
    `(manifest, local dir, the hash collect printed)`."""
    plan = make_plan(shards, tmp_path)
    shards.collect(plan, tmp_path / "merge")
    return pull(plan, tmp_path)


def pull(plan, tmp_path):
    local = tmp_path / "local"
    shutil.copytree(plan.results, local, symlinks=True)
    return local / MANIFEST, local, sha_of(plan.results / MANIFEST)


def verify(shards, manifest, local, hash_given):
    return shards.main(["verify-copy", str(manifest), str(local), "--manifest-sha256", hash_given])


# ---- a shard's live out/ and the core's own snapshots of it
def live_out(tmp_path, label):
    """A shard's out/ with a real store, as a dispatcher leaves it (the core's own schema and markers)."""
    out = tmp_path / "live" / label / "out"
    (out / "runs").mkdir(parents=True)
    (out / "campaign.lock").write_text("")
    store = Store(out / "state.sqlite", SCHEMA)
    store.sync_manifest(ROWS)
    return store, out


def ended_run(store, out, key):
    """One attempt, begun and ended: its record and its marker in its directory, its row finished."""
    digest = store.q("SELECT digest FROM runs WHERE run_key=?", (key,))[0]["digest"]
    run_dir = out / "runs" / digest / "try1"
    run_dir.mkdir(parents=True)
    begun = store.begin_attempt(key, NOW, 4321, str(run_dir))
    (run_dir / "record.json").write_text(json.dumps({"run": key}))
    result = finished(input_tokens=1000, output_tokens=200)
    write_marker(run_dir, key, begun.id, result)
    store.finish_attempt(begun.id, result, 3)


def take_snapshot(tmp_path, store, out, label, base, index=1, count=2, stamp="20261116T150000Z"):
    """The core's own snapshot of `out` into the shard's snapshots directory, naming the archive it started from."""
    facts = {"shard": {"index": index, "count": count, "label": label}, "base": base}
    into = tmp_path / "results" / label / "snapshots"
    return out_snapshot.take(out, into, scratch=tmp_path / "scratch", facts=facts, live=True, now=stamp)


def archive_sha(tmp_path, label):
    return json.loads((tmp_path / "results" / f"{label}.out.json").read_text(encoding="utf-8"))["sha256"]


def relative(path, root):
    return path.relative_to(root).as_posix()


def with_newer_snapshot(shards, tmp_path, label="fed-a", index=1):
    """Both shards archived; `label` was resumed from its archive and killed after a run it took a snapshot of."""
    plan = make_plan(shards, tmp_path)
    store, out = live_out(tmp_path, label)
    ended_run(store, out, KEYS[0])
    snapshot = take_snapshot(tmp_path, store, out, label, archive_sha(tmp_path, label), index=index)
    store.close()
    return plan, snapshot


class Killed(BaseException):
    """The step killed outright: its dispatcher never returns, and nothing is archived."""


def step_shard(shards, tmp_path, resume, killed=False):
    """One real `shards.step` of a one-shard plan, with a stand-in dispatcher that does what the core's does here:
    its spend journal (a file of its own), a store with a run ended in it, the lock. `killed` ends it as a kill outright
    would (no archive; the step's own last snapshot is taken as it unwinds)."""
    study = tmp_path / "study"
    study.mkdir(exist_ok=True)
    (study / "study.toml").write_text("[study]\n")
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir(exist_ok=True)
    args = argparse.Namespace(
        study=study,
        index=1,
        count=2,
        label="fed-a",
        tmpfs=tmp_path / "tmpfs",
        results=tmp_path / "results",
        core_src=tmp_path,
        resume=resume,
        snapshot_every=3600.0,
    )

    def dispatcher(argv, env, cwd):
        out = Path(argv[argv.index("run") + 1]) / "out"
        Path(argv[argv.index("--spend-journal") + 1]).write_text('{"spent": 1}\n')
        (out / "runs").mkdir(parents=True, exist_ok=True)
        (out / "campaign.lock").write_text("")
        store = Store(out / "state.sqlite", SCHEMA)
        store.sync_manifest(ROWS)
        try:
            ended_run(store, out, KEYS[1 if resume else 0])
        finally:
            store.close()
        if killed:
            raise Killed
        return 0

    return shards.step(args, runner=dispatcher, core_found=lambda path: True)


def resumed_and_killed(shards, tmp_path):
    """The plan of two shards in the state this is all about: `fed-b` archived, and `fed-a` stepped to its archive, then
    resumed from it and killed outright after a run it took a snapshot of, so that its newest snapshot names the archive
    it holds and the archive lacks that run. Through the real `shards.step`."""
    plan = shards.load_plan(plan_file(tmp_path))
    archived(shards, tmp_path, "fed-b", 2, journals=False)
    assert step_shard(shards, tmp_path, resume=False) == 0
    with pytest.raises(Killed):
        step_shard(shards, tmp_path, resume=True, killed=True)
    return plan
