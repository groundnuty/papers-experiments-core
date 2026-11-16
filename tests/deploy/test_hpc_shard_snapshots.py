"""O62: a shard step's out/ snapshotted while it runs, restored by `--resume` after a kill outright, and the spend the
kill took out of the store counted once. The dispatcher is a stand-in that drives the core's own store, journal and
recovery; nothing runs srun, and every path, name and job here is invented."""

import argparse
import importlib.util
import json
import sqlite3
import sys
import tarfile
import threading
import time
from pathlib import Path

import pytest
from campaign_support import NOW, finished, make_manifest, make_schema
from scheduler_support import models_for

from expcore.campaign.attempt_bounds import bound_usd
from expcore.campaign.export import export_run_state
from expcore.campaign.gate import Budget, BudgetGate
from expcore.campaign.lost_spend import Pricing, open_journal
from expcore.campaign.markers import write_marker
from expcore.campaign.recovery import recover
from expcore.campaign.store import Store
from expcore.config.models import Limits
from expcore.config.prices import Price

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
SCHEMA, CAMPAIGN = make_schema(), "c" * 64
MANIFEST = make_manifest(SCHEMA)
KEYS = [row.key.text for row in MANIFEST.rows]
SPEC = models_for("m1", price=Price(3.0, 15.0), context_tokens=16_000).resolve("m1")
LIMITS = Limits(
    max_tool_rounds=10, time_cap_s=300.0, token_budget=100_000, output_cap_chars=200, max_output_tokens=4096
)
BOUND = bound_usd(SPEC, LIMITS, reply_capped=False)
MEASURED = (1000 * 3.0 + 200 * 15.0) / 1e6


class Killed(BaseException):
    """The step killed outright: no last snapshot, no archive."""


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_snap", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_snap"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_snap", None)


def step_args(tmp_path, resume=False, label="fed-a"):
    study = tmp_path / "study"
    study.mkdir(exist_ok=True)
    (study / "study.toml").write_text("[study]\n")
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir(exist_ok=True)
    return argparse.Namespace(study=study, index=1, count=2, label=label, tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=resume,
                              snapshot_every=3600.0)  # fmt: skip


def hook_for(shards, args):
    """The step's own snapshots, taken when the stand-in asks (`take_now`), and none after a kill outright."""
    live = threading.Event()
    live.set()
    facts = {"shard": {"index": args.index, "count": args.count, "label": args.label},
             "base": shards.archive_named(args.results, args.label)}  # fmt: skip
    scratch = args.tmpfs / "snapshot-scratch"
    take = shards.taker(args.results / args.label / "snapshots", scratch, facts, live)

    class Hook(shards.Snapshots):
        killed = False

        def take_now(self, out):
            self._once(out)

        def stop(self, out):
            live.clear()
            if self.killed:
                self._stop.set()
                return
            super().stop(out)

    return Hook(take, every_s=3600.0)


def dispatcher(hook, script, label="fed-a"):
    """A stand-in dispatcher: opens the store under out/ as `expcore run` does (recovery, then this start's journal
    counted against it), runs `script(store, out, counted)` and closes."""
    seen = {}

    def run(argv, env, cwd):
        out = Path(argv[argv.index("run") + 1]) / "out"
        journal = Path(argv[argv.index("--spend-journal") + 1])
        seen["journal"], seen["argv"] = journal, list(argv)
        (out / "runs").mkdir(parents=True, exist_ok=True)
        (out / "campaign.lock").write_text("")
        store = Store(out / "state.sqlite", SCHEMA)
        store.sync_manifest(MANIFEST.rows)
        try:
            recover(store, NOW, 3)
            counted = open_journal(store, journal, CAMPAIGN, label, NOW, Pricing({"m1": SPEC}, LIMITS))
            seen["counted"] = counted
            return script(store, out, counted)
        except Killed:
            hook.killed = True
            raise
        finally:
            if store.spend_journal is not None:
                store.spend_journal.close()
            store.close()

    return run, seen


def begin(store, out, key):
    digest = store.q("SELECT digest FROM runs WHERE run_key=?", (key,))[0]["digest"]
    tries = len(store.q("SELECT 1 FROM attempts WHERE run_key=?", (key,))) + 1
    run_dir = out / "runs" / digest / f"try{tries}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return store.begin_attempt(key, NOW, 4321, str(run_dir)), run_dir


def end(store, key, begun, run_dir):
    result = finished(input_tokens=1000, output_tokens=200)
    (run_dir / "record.json").write_text(json.dumps({"run": key}))
    write_marker(run_dir, key, begun.id, result)
    store.finish_attempt(begun.id, result, 3)


def killed_step(shards, tmp_path, script, resume=False):
    """Run one step that its stand-in kills outright; what the stand-in saw."""
    args = step_args(tmp_path, resume=resume)
    hook = hook_for(shards, args)
    run, seen = dispatcher(hook, lambda store, out, counted: script(store, out, counted, hook))
    with pytest.raises(Killed):
        shards.step(args, runner=run, core_found=lambda path: True, snapshots=hook)
    return seen


def first_life(store, out, counted, hook):
    """Attempt 1 begins; a snapshot; attempt 2 begins and ends, attempt 3 begins; the node dies."""
    begin(store, out, KEYS[0])
    hook.take_now(out)
    begun, run_dir = begin(store, out, KEYS[1])
    end(store, KEYS[1], begun, run_dir)
    begin(store, out, KEYS[2])
    raise Killed


def test_o62_a_killed_shard_resumes_from_its_snapshot_and_its_lost_spend_is_counted_once(shards, tmp_path):
    """O62: a step killed outright leaves its snapshot and its journal; `--resume` restores the snapshot and counts
    the attempts begun after it, the ended one at its measured cost and the one in flight at its bound; a second kill
    and resume counts nothing twice; the shard's budget, export and report carry it."""
    killed_step(shards, tmp_path, first_life)
    assert not (tmp_path / "results" / "fed-a.out.tar").exists()
    assert [p.name for p in (tmp_path / "results" / "fed-a" / "snapshots").iterdir()] == ["snap-000001"]

    def second_life(store, out, counted, hook):
        assert (counted["journals"], counted["attempts"]) == (1, 2)
        assert store.q("SELECT state FROM attempts")[0]["state"] == "interrupted"  # in flight at the snapshot
        hook.take_now(out)
        raise Killed

    killed_step(shards, tmp_path, second_life, resume=True)
    found = {}

    def third_life(store, out, counted):
        found["counted"] = counted
        found["export"] = export_run_state(store)
        gate = BudgetGate(store, {"m1": SPEC}, Budget(max_usd=10.0), LIMITS)
        found["spent"] = gate.assess([], NOW, live=set()).spent_usd
        return 0

    args = step_args(tmp_path, resume=True)
    run, _ = dispatcher(None, third_life)
    assert shards.step(args, runner=run, core_found=lambda path: True) == 0
    assert found["counted"] == {"journals": 1, "attempts": 0, "charges_usd": {}}
    lost = [(e["run_key"], e["ended"]) for e in found["export"]["lost_in_kill"]]
    assert lost == [(KEYS[1], True), (KEYS[2], False)]
    assert found["spent"] == pytest.approx(BOUND + MEASURED + BOUND)  # the interrupted one, and the two lost
    assert (tmp_path / "results" / "fed-a.out.tar").is_file()


def test_o62_a_damaged_newest_snapshot_falls_back_to_the_previous_good_one(shards, tmp_path, capsys):
    """O62: the newest snapshot fails its check: the one before it is restored, and the newest is named."""

    def two_snapshots(store, out, counted, hook):
        begin(store, out, KEYS[0])
        hook.take_now(out)
        begun, run_dir = begin(store, out, KEYS[1])
        end(store, KEYS[1], begun, run_dir)
        hook.take_now(out)
        raise Killed

    killed_step(shards, tmp_path, two_snapshots)
    newest = tmp_path / "results" / "fed-a" / "snapshots" / "snap-000002"
    next(newest.rglob("record.json")).write_text("{}")
    found = {}

    def resumed(store, out, counted):
        found["ended"] = store.q("SELECT COUNT(*) AS n FROM attempts WHERE state='done'")[0]["n"]
        found["counted"] = counted["attempts"]
        return 0

    run, _ = dispatcher(None, resumed)
    shards.step(step_args(tmp_path, resume=True), runner=run, core_found=lambda path: True)
    assert found == {"ended": 0, "counted": 1}  # snapshot 1 lacks the run that ended: its spend is lost, once
    assert "snapshot snap-000002 is not used" in capsys.readouterr().err


def test_o62_an_archive_newer_than_every_snapshot_is_what_resumes(shards, tmp_path, capsys):
    """O62: a step that ended and archived took its snapshots before the archive, which they do not name: the next
    `--resume` starts from the archive."""

    def ends(store, out, counted):
        begun, run_dir = begin(store, out, KEYS[0])
        end(store, KEYS[0], begun, run_dir)
        return 0

    run, _ = dispatcher(None, ends)
    assert shards.step(step_args(tmp_path), runner=run, core_found=lambda path: True) == 0
    assert (tmp_path / "results" / "fed-a" / "snapshots" / "snap-000001").is_dir()  # the last one, after the exit
    found = {}

    def resumed(store, out, counted):
        found["dir"] = store.q("SELECT run_dir FROM attempts")[0]["run_dir"]
        found["out"] = out
        return 0

    run, _ = dispatcher(None, resumed)
    shards.step(step_args(tmp_path, resume=True), runner=run, core_found=lambda path: True)
    assert "resumes from its archive" in capsys.readouterr().err
    assert found["dir"].startswith(str(found["out"] / "runs"))  # pointed at this step's copy, not the last node's


def test_o62_a_snapshot_newer_than_the_archive_is_what_resumes(shards, tmp_path, capsys):
    """O62: a step resumed from the archive and killed took snapshots that name that archive: they are newer."""
    run, _ = dispatcher(None, lambda store, out, counted: 0)
    shards.step(step_args(tmp_path), runner=run, core_found=lambda path: True)

    def later(store, out, counted, hook):
        begun, run_dir = begin(store, out, KEYS[3])
        end(store, KEYS[3], begun, run_dir)
        hook.take_now(out)
        raise Killed

    killed_step(shards, tmp_path, later, resume=True)
    capsys.readouterr()
    found = {}
    run, _ = dispatcher(None, lambda store, out, counted: found.update(n=len(store.q("SELECT 1 FROM attempts"))) or 0)
    shards.step(step_args(tmp_path, resume=True), runner=run, core_found=lambda path: True)
    assert "resumes from snapshot snap-000002" in capsys.readouterr().err
    assert found["n"] == 1


def test_o62_a_start_without_resume_of_a_shard_with_a_snapshot_is_refused(shards, tmp_path):
    """O62: a repeated launch line never throws away a killed step's snapshot."""
    killed_step(shards, tmp_path, first_life)
    run, seen = dispatcher(None, lambda store, out, counted: 0)
    assert shards.step(step_args(tmp_path), runner=run, core_found=lambda path: True) == 2
    assert not seen


def test_o62_a_snapshot_of_another_shard_is_refused_naming_it(shards, tmp_path):
    """O62: a snapshot whose manifest names another shard is never restored into this one."""
    killed_step(shards, tmp_path, first_life)
    manifest = tmp_path / "results" / "fed-a" / "snapshots" / "snap-000001" / "snapshot.json"
    data = json.loads(manifest.read_text())
    data["facts"]["shard"]["label"] = "fed-b"
    manifest.write_text(json.dumps(data))
    run, seen = dispatcher(None, lambda store, out, counted: 0)
    with pytest.raises(shards.ShardsError, match="snap-000001 is of shard"):
        shards.step(step_args(tmp_path, resume=True), runner=run, core_found=lambda path: True)
    assert not seen


def test_o62_a_shard_that_ends_normally_archives_exactly_what_its_dispatcher_left(shards, tmp_path):
    """O62: the snapshots live beside the archive, and the last one, after the dispatcher exits, reads a private copy
    of the store: the archive holds the dispatcher's files and nothing else, as before snapshots."""

    def ends(store, out, counted):
        begun, run_dir = begin(store, out, KEYS[0])
        end(store, KEYS[0], begun, run_dir)
        return 0

    for where, hook in (("with", None), ("without", "none")):
        base = tmp_path / where
        base.mkdir()
        args = step_args(base)
        run, _ = dispatcher(None, ends)
        snapshots = shards.Snapshots(lambda out: None, every_s=3600.0) if hook else None
        shards.step(args, runner=run, core_found=lambda path: True, snapshots=snapshots)
    members = {}
    for where in ("with", "without"):
        with tarfile.open(tmp_path / where / "results" / "fed-a.out.tar") as tar:
            members[where] = sorted(tar.getnames())
    assert members["with"] == members["without"]
    assert not [name for name in members["with"] if name.endswith(("-wal", "-shm"))]
    assert (tmp_path / "with" / "results" / "fed-a" / "snapshots" / "snap-000001").is_dir()


def test_o62_a_store_a_read_left_with_its_shm_as_the_dispatcher_exited_is_settled(shards, tmp_path):
    """O62: a snapshot's read in place that overlapped the dispatcher's exit leaves the store's -shm and -wal; before
    the archive, the step closes the store as the dispatcher would have, so neither is archived and nothing is lost."""
    out = tmp_path / "out"
    out.mkdir()
    store = Store(out / "state.sqlite", SCHEMA)
    store.sync_manifest(MANIFEST.rows)
    store.close()
    reader = sqlite3.connect(f"file:{out / 'state.sqlite'}?mode=ro", uri=True)
    reader.execute("SELECT COUNT(*) FROM runs").fetchone()
    reader.close()
    assert (out / "state.sqlite-shm").exists()
    shards.settle_store(out)
    assert sorted(p.name for p in out.iterdir()) == ["state.sqlite"]
    with sqlite3.connect(out / "state.sqlite") as check:
        assert check.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == len(KEYS)


def test_o62_each_step_journals_to_a_new_file_named_to_the_dispatcher(shards, tmp_path):
    """O62: the dispatcher is given `--spend-journal`, a new file under `<results>/<label>/journals/` per step,
    before its shard options."""
    run, seen = dispatcher(None, lambda store, out, counted: 0)
    shards.step(step_args(tmp_path), runner=run, core_found=lambda path: True)
    first = seen["journal"]
    assert first.parent == tmp_path / "results" / "fed-a" / "journals"
    assert first.suffix == ".jsonl"
    assert seen["argv"][-4:] == ["--shard", "1/2", "--shard-label", "fed-a"]
    run, again = dispatcher(None, lambda store, out, counted: 0)
    shards.step(step_args(tmp_path, resume=True), runner=run, core_found=lambda path: True)
    assert again["journal"] != first
    assert first.is_file()


def test_o62_a_run_end_takes_a_snapshot_before_the_clock_would(shards, tmp_path):
    """O62: the snapshot thread takes one when the count of ended attempts changes, long before `every_s`."""
    taken = threading.Event()
    readings = iter([0, 0, 1])
    hook = shards.Snapshots(lambda out: taken.set(), every_s=3600.0, progress=lambda: next(readings, 1), poll_s=0.01)
    hook.start(tmp_path)
    try:
        assert taken.wait(5.0)
    finally:
        hook._stop.set()
        hook._thread.join()


def test_o62_the_manifest_sets_how_often_a_step_snapshots(shards, tmp_path):
    """O62: `snapshot_every_s` is optional, at least 60, and reaches the step's line."""
    roots = [tmp_path / "fed" / name for name in ("a", "b")]
    lines = [f'study = "{tmp_path / "study"}"', f'python = "{tmp_path / "py"}"', f'core_src = "{tmp_path}"',
             f'results = "{tmp_path / "results"}"', f'tmpfs = "{tmp_path / "tmpfs"}"', 'step_path = "/usr/bin"',
             "cpus = 6", 'memory = "18g"', "snapshot_every_s = 600"]  # fmt: skip
    for label, root in zip(("fed-a", "fed-b"), roots, strict=True):
        root.mkdir(parents=True)
        state = {"format": "expcore-hpc-federation/1", "allocation": {"job": "101", "name": "hpcop-x"},
                 "sandbox": {"placement": "het", "node": "node-s1", "het_group": 1}, "phase": "linked"}  # fmt: skip
        (root / "hpc-federation.json").write_text(json.dumps(state))
        lines += ["[[shards]]", f'label = "{label}"', f'root = "{root}"']
    (tmp_path / "shards.toml").write_text("\n".join(lines) + "\n")
    plan = shards.load_plan(tmp_path / "shards.toml")
    line = shards.step_argv(plan, plan.shards[0], shards.federation(plan.shards[0]), SCRIPT)
    assert line[line.index("--snapshot-every") + 1] == "600"
    (tmp_path / "shards.toml").write_text("\n".join(lines).replace("= 600", "= 5") + "\n")
    with pytest.raises(shards.ShardsError, match="at least 60"):
        shards.load_plan(tmp_path / "shards.toml")


def test_o62_time_is_not_slept_on(shards):
    """The thread waits on its stop event, never on a sleep, so a stop is at once."""
    hook = shards.Snapshots(lambda out: None, every_s=3600.0)
    began = time.monotonic()
    hook.start(Path("/nonexistent"))
    hook.stop(Path("/nonexistent"))
    assert time.monotonic() - began < 5.0


def two_snapshots(store, out, counted, hook):
    begin(store, out, KEYS[0])
    hook.take_now(out)
    begun, run_dir = begin(store, out, KEYS[1])
    end(store, KEYS[1], begun, run_dir)
    hook.take_now(out)
    raise Killed


@pytest.mark.parametrize("text", ["null", "[]"])
def test_o62_a_newest_manifest_that_is_no_object_falls_back_too(shards, tmp_path, text):
    """O62: a newest snapshot whose manifest is JSON but no object is passed over like any damaged one."""
    killed_step(shards, tmp_path, two_snapshots)
    (tmp_path / "results" / "fed-a" / "snapshots" / "snap-000002" / "snapshot.json").write_text(text)
    found = {}
    run, _ = dispatcher(None, lambda store, out, counted: found.update(n=len(store.q("SELECT 1 FROM attempts"))) or 0)
    shards.step(step_args(tmp_path, resume=True), runner=run, core_found=lambda path: True)
    assert found["n"] == 1  # snapshot 1: the one attempt that had begun


def test_o62_a_shard_whose_every_snapshot_fails_and_that_has_no_archive_is_refused(shards, tmp_path):
    """O62: dropping a killed shard's results for a fresh start is never a resume: it is refused, naming why."""
    killed_step(shards, tmp_path, two_snapshots)
    for snapshot in (tmp_path / "results" / "fed-a" / "snapshots").iterdir():
        (snapshot / "snapshot.json").write_text("{")
    run, seen = dispatcher(None, lambda store, out, counted: 0)
    with pytest.raises(shards.ShardsError, match="no snapshot of its 2 verifies and it has no archive"):
        shards.step(step_args(tmp_path, resume=True), runner=run, core_found=lambda path: True)
    assert not seen
