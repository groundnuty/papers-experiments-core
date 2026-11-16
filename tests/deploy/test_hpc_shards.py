"""O56: the shard driver of deploy/hpc-federation (one shard per HPC federation), checked without a cluster: the
manifest, the login node's step line, the step on its node with a stand-in dispatcher, and the archives the merge
takes. Every path, name and job here is invented, and nothing runs srun."""

import argparse
import importlib.util
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards", None)


def _manifest(tmp_path, **changes):
    roots = [tmp_path / "fed" / name for name in ("a", "b")]
    for n, root in enumerate(roots, 1):
        root.mkdir(parents=True)
        state = {"format": "expcore-hpc-federation/1", "allocation": {"job": str(100 + n), "name": f"hpcop-x-{n}"},
                 "sandbox": {"placement": "het", "node": f"node-s{n}", "het_group": 1}, "phase": "linked",
                 "data": {"ingested": {}}}  # fmt: skip
        (root / "platform.toml").write_text('[space]\nname = "s"\ndata_manifest = "m.json"\n')  # its data declared
        if changes.get("placement") == "separate":
            state["sandbox"] = {"placement": "separate", "node": f"node-s{n}", "job": str(200 + n),
                                "name": f"hpcop-sbx-{n}"}  # fmt: skip
        elif changes.get("placement") == "none":
            del state["sandbox"]
        (root / "hpc-federation.json").write_text(json.dumps(state))
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
    labels = changes.get("labels", ("fed-a", "fed-b"))
    for label, root in zip(labels, roots, strict=False):
        lines += ["[[shards]]", f'label = "{label}"', f'root = "{root}"']
    path = tmp_path / "shards.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_the_manifest_gives_one_shard_per_federation_in_order(shards, tmp_path):
    """O56: shard i is the i-th federation; each label is checked by the core's rule."""
    plan = shards.load_plan(_manifest(tmp_path))
    assert [(s.index, s.count, s.label) for s in plan.shards] == [(1, 2, "fed-a"), (2, 2, "fed-b")]


@pytest.mark.parametrize("labels", [("fed-a",), ("fed-a", "fed-a"), ("Fed.A", "fed-b")])
def test_a_manifest_with_one_shard_a_label_twice_or_a_host_like_label_is_refused(shards, tmp_path, labels):
    with pytest.raises(shards.ShardsError):
        shards.load_plan(_manifest(tmp_path, labels=labels))


def test_the_step_line_is_an_exact_step_on_the_sandbox_node_with_the_allowlist_alone(shards, tmp_path):
    """O56: `--exact`, never `--overlap`; the federation's job and sandbox node; an environment of PATH, LC_ALL and the
    federation's root alone; the dispatcher's shard and label."""
    plan = shards.load_plan(_manifest(tmp_path))
    shard = plan.shards[1]
    line = shards.step_argv(plan, shard, shards.federation(shard), SCRIPT)
    assert line[:4] == ["srun", "--jobid=102", "--het-group=1", "--exact"]
    assert "--overlap" not in line
    assert line[line.index("-w") + 1] == "node-s2"
    at = line.index("-i") + 1
    homeless = [f"{k}={v}" for k, v in shards.home_env(shards.NO_HOME).items()]
    env = line[at : at + 4 + len(homeless)]
    assert [e.split("=")[0] for e in env[:4]] == ["PATH", "LC_ALL", "EXPCORE_FED_ROOT_PARENT", "EXPCORE_FED_ROOT"]
    assert env[3] == f"EXPCORE_FED_ROOT={shard.root}"
    assert env[4:] == homeless
    assert line[at + len(env) : at + len(env) + 3] == [plan.python, "-I", str(SCRIPT)]
    assert line[line.index("--index") + 1 : line.index("--index") + 2] == ["2"]
    assert line[line.index("--label") + 1] == "fed-b"


def test_o57_the_step_line_sends_no_home_and_the_dispatcher_one_in_the_steps_own_directory(shards, tmp_path):
    """O57: no tool of a shard step may fall back to the account's home, which every job and agent of the account
    shares: the step's line points HOME, the XDG base directories, Apptainer's, uv's and pip's directories under a path
    nobody can make, with Python's user site off and no git configuration; the dispatcher (the agent's cells) gets every
    one of them in the step's own private directory on the node's tmpfs."""
    plan = shards.load_plan(_manifest(tmp_path))
    line = shards.step_argv(plan, plan.shards[0], shards.federation(plan.shards[0]), SCRIPT)
    words = dict(w.split("=", 1) for w in line[line.index("-i") + 1 :] if "=" in w and w.split("=")[0].isupper())
    names = ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "APPTAINER_CONFIGDIR",
             "APPTAINER_CACHEDIR", "UV_CACHE_DIR", "PIP_CACHE_DIR")  # fmt: skip
    assert all(words[name].startswith("/dev/null/") for name in names)
    flags = ("PYTHONNOUSERSITE", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")
    assert [words[name] for name in flags] == ["1", "/dev/null", "1"]
    run, seen = _dispatcher()
    seen_dirs = {}

    def looking(argv, env, cwd):  # the caches are made by their tools, inside the home made here
        seen_dirs.update({name: Path(env[name]).is_dir() for name in names if not name.endswith("_CACHE_DIR")})
        return run(argv, env, cwd)

    assert shards.step(_step_args(tmp_path), runner=looking, core_found=lambda path: True) == 0
    home = Path(seen["env"]["HOME"])
    assert home.parent.parent == tmp_path / "tmpfs"  # <tmpfs>/expcore-shard-<label>.<random>/home
    assert all(seen["env"][name] == str(home) or seen["env"][name].startswith(f"{home}/") for name in names)
    assert (seen["env"]["PYTHONNOUSERSITE"], seen["env"]["GIT_CONFIG_GLOBAL"]) == ("1", "/dev/null")
    assert seen_dirs
    assert all(seen_dirs.values())  # made, private to the step, before the dispatcher starts
    assert hpc_home_env("/h") == shards.home_env("/h")
    assert hpc_launch_no_home() == shards.NO_HOME


def _deploy_module(name: str):
    """A module of deploy/hpc-federation loaded by its path, as this file loads shards.py, its imports beside it."""
    spec = importlib.util.spec_from_file_location(f"hpc_{name}_for_shards", SCRIPT.parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def hpc_home_env(home: str) -> dict:
    """The launcher's own home_env (hpcslurm.py), which shards.py keeps a copy of."""
    return _deploy_module("hpcslurm").home_env(home)


def hpc_launch_no_home() -> str:
    """hpclaunch.NO_HOME, read from its source: the module imports the driver's others by their plain names."""
    text = (SCRIPT.parent / "hpclaunch.py").read_text(encoding="utf-8")
    return text.split('NO_HOME = "', 1)[1].split('"', 1)[0]


def test_a_federation_whose_state_names_no_held_job_is_refused(shards, tmp_path):
    plan = shards.load_plan(_manifest(tmp_path))
    (plan.shards[0].root / "hpc-federation.json").write_text(json.dumps({"format": "expcore-hpc-federation/1"}))
    with pytest.raises(shards.ShardsError, match="no held job"):
        shards.federation(plan.shards[0])


def _step_args(tmp_path, index=1, label="fed-a", resume=False):
    study = tmp_path / "study"
    study.mkdir(exist_ok=True)
    (study / "study.toml").write_text("[study]\n")
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir(exist_ok=True)
    return argparse.Namespace(study=study, index=index, count=2, label=label, tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=resume)  # fmt: skip


def _dispatcher(status=0, lock=True):
    seen = {}

    def run(argv, env, cwd):
        seen.update(argv=list(argv), env=dict(env), cwd=cwd)
        out = Path(argv[argv.index("run") + 1]) / "out"
        (out / "runs").mkdir(parents=True, exist_ok=True)
        (out / "runs" / "r1.json").write_text("{}")
        if lock:
            (out / "campaign.lock").write_text("")
        return status

    return run, seen


def test_the_step_runs_the_shard_on_a_tmpfs_copy_and_archives_its_out_whole(shards, tmp_path):
    """O56, #305: the dispatcher runs the shard on a copy on the node's tmpfs, with HOME and TMPDIR there; its `out/`
    comes back whole, lock included, and the tmpfs copy is gone."""
    run, seen = _dispatcher(status=3)
    args = _step_args(tmp_path)
    assert shards.step(args, runner=run, core_found=lambda path: True) == 3
    assert seen["argv"][-4:] == ["--shard", "1/2", "--shard-label", "fed-a"]
    assert seen["env"]["HOME"].startswith(str(tmp_path / "tmpfs"))
    assert not list((tmp_path / "tmpfs").iterdir())
    manifest = json.loads((tmp_path / "results" / "fed-a.out.json").read_text())
    assert manifest["lock"] is True
    assert manifest["exit_status"] == 3
    with tarfile.open(tmp_path / "results" / "fed-a.out.tar") as tar:
        assert {"out/campaign.lock", "out/runs/r1.json"} <= set(tar.getnames())


def test_a_step_whose_interpreter_imports_another_core_runs_nothing(shards, tmp_path):
    """O56: lane H's trap: a step that would run an installed core instead of the one given stops first."""
    run, seen = _dispatcher()
    assert shards.step(_step_args(tmp_path), runner=run, core_found=lambda path: False) == shards.CORE_MISMATCH
    assert not seen


def test_the_core_check_compares_where_the_core_was_imported_from(shards, tmp_path):
    inside = tmp_path / "src" / "expcore" / "__init__.py"
    assert shards.imports_core_from(tmp_path / "src", lambda: str(inside))
    assert not shards.imports_core_from(tmp_path / "elsewhere", lambda: str(inside))


def test_a_resumed_step_starts_from_its_last_archive(shards, tmp_path):
    """O56: `--resume` unpacks the shard's verified archive into its copy before the dispatcher starts."""
    run, _ = _dispatcher()
    shards.step(_step_args(tmp_path), runner=run, core_found=lambda path: True)
    found = {}

    def resumed(argv, env, cwd):
        out = Path(argv[argv.index("run") + 1]) / "out"
        found["kept"] = (out / "runs" / "r1.json").is_file()
        return 0

    shards.step(_step_args(tmp_path, resume=True), runner=resumed, core_found=lambda path: True)
    assert found["kept"]


def test_the_snapshot_hook_runs_once_more_after_the_dispatcher_and_never_ends_the_run(shards, tmp_path):
    """O56: the hook of limit 1 runs after the dispatcher exits, before the archive; one that raises is reported."""
    taken = []

    def take(out):
        taken.append(out.name)
        raise OSError("disk full")

    run, _ = _dispatcher()
    hook = shards.Snapshots(take, every_s=3600)
    assert shards.step(_step_args(tmp_path), runner=run, core_found=lambda path: True, snapshots=hook) == 0
    assert taken == ["out"]
    assert (tmp_path / "results" / "fed-a.out.tar").is_file()


def _archived(shards, tmp_path, label, index, lock=True):
    out = tmp_path / "src-out" / label / "out"
    (out / "runs").mkdir(parents=True)
    (out / "runs" / "r.json").write_text(json.dumps({"label": label}))
    if lock:
        (out / "campaign.lock").write_text("")
    (tmp_path / "results").mkdir(exist_ok=True)
    shards.archive(out, tmp_path / "results", {"index": index, "count": 2, "label": label}, 0)


def test_collect_verifies_and_unpacks_every_shard_read_only_and_names_the_merge(shards, tmp_path):
    """O56, #305: each shard's `out/` whole under `<into>/<label>/out`, read-only, and the merge's command line."""
    plan = shards.load_plan(_manifest(tmp_path))
    for shard in plan.shards:
        _archived(shards, tmp_path, shard.label, shard.index)
    outs = shards.collect(plan, tmp_path / "merge")
    assert [o.relative_to(tmp_path / "merge").as_posix() for o in outs] == ["fed-a/out", "fed-b/out"]
    assert (outs[0] / "campaign.lock").is_file()
    assert not os.access(outs[0] / "runs" / "r.json", os.W_OK)
    line = shards.merge_argv(plan, outs)
    assert line[1:4] == ["-m", "expcore", "merge"]
    assert line[5:] == [str(o) for o in outs]


def test_collect_refuses_a_shard_without_its_lock(shards, tmp_path):
    plan = shards.load_plan(_manifest(tmp_path))
    _archived(shards, tmp_path, "fed-a", 1)
    _archived(shards, tmp_path, "fed-b", 2, lock=False)
    with pytest.raises(shards.ShardsError, match=r"fed-b.*campaign\.lock"):
        shards.collect(plan, tmp_path / "merge")
    assert not (tmp_path / "merge").exists()


def test_collect_refuses_an_archive_that_is_not_the_one_its_manifest_names(shards, tmp_path):
    plan = shards.load_plan(_manifest(tmp_path))
    for shard in plan.shards:
        _archived(shards, tmp_path, shard.label, shard.index)
    with open(tmp_path / "results" / "fed-b.out.tar", "ab") as handle:
        handle.write(b"\0" * 512)
    with pytest.raises(shards.ShardsError, match=r"fed-b.*not the one"):
        shards.collect(plan, tmp_path / "merge")


def test_collect_refuses_another_shard_s_archive_under_a_label(shards, tmp_path):
    plan = shards.load_plan(_manifest(tmp_path))
    _archived(shards, tmp_path, "fed-a", 1)
    _archived(shards, tmp_path, "fed-b", 1)
    with pytest.raises(shards.ShardsError, match="another shard"):
        shards.collect(plan, tmp_path / "merge")


def test_an_archive_that_cannot_be_written_keeps_the_shard_s_out_on_the_node(shards, tmp_path, monkeypatch):
    """O56: the tmpfs copy holds the only whole `out/`: a failed archive never removes it."""
    run, _ = _dispatcher()

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shards, "archive", full)
    assert shards.step(_step_args(tmp_path), runner=run, core_found=lambda path: True) == shards.ARCHIVE_FAILED
    kept = list((tmp_path / "tmpfs").iterdir())
    assert len(kept) == 1
    assert (kept[0] / "study" / "out" / "campaign.lock").is_file()


def test_a_fresh_start_of_a_shard_that_has_results_is_refused(shards, tmp_path):
    """O56: a repeated launch line never replaces a finished shard's archive; only `--resume` continues it."""
    run, _ = _dispatcher()
    shards.step(_step_args(tmp_path), runner=run, core_found=lambda path: True)
    before = (tmp_path / "results" / "fed-a.out.tar").read_bytes()
    again, seen = _dispatcher()
    assert shards.step(_step_args(tmp_path), runner=again, core_found=lambda path: True) == 2
    assert not seen
    assert (tmp_path / "results" / "fed-a.out.tar").read_bytes() == before


def test_collect_unpacks_the_private_copy_it_verified_and_leaves_none_behind(shards, tmp_path):
    """O56: what is unpacked is a copy whose bytes were hashed, never the original read a second time."""
    plan = shards.load_plan(_manifest(tmp_path))
    for shard in plan.shards:
        _archived(shards, tmp_path, shard.label, shard.index)
    copy, _ = shards.verified(plan.results, plan.shards[0], tmp_path)
    assert copy.parent == tmp_path
    assert copy.read_bytes() == (plan.results / "fed-a.out.tar").read_bytes()
    copy.unlink()
    shards.collect(plan, tmp_path / "merge")
    assert not list((tmp_path / "merge").glob(".*.tar"))


def test_a_separate_sandbox_s_step_goes_to_its_own_held_job(shards, tmp_path):
    """O56: `placement = "separate"`: the sandbox's own job, no het group, its name in the step's."""
    plan = shards.load_plan(_manifest(tmp_path, placement="separate"))
    line = shards.step_argv(plan, plan.shards[0], shards.federation(plan.shards[0]), SCRIPT)
    assert line[:3] == ["srun", "--jobid=201", "--exact"]
    assert not [a for a in line if a.startswith("--het-group")]
    assert line[line.index("-J") + 1] == "hpcop-sbx-1-shard-1"


def test_a_het_sandbox_without_its_group_is_refused(shards, tmp_path):
    """O56: a step of a heterogeneous job with no `--het-group` is refused by the site's Slurm, and one in group 0
    would land on the services' node shape: the agent's needs its own group, above 0."""
    plan = shards.load_plan(_manifest(tmp_path))
    state_file = plan.shards[0].root / "hpc-federation.json"
    state = json.loads(state_file.read_text())
    del state["sandbox"]["het_group"]
    state_file.write_text(json.dumps(state))
    with pytest.raises(shards.ShardsError, match="het_group"):
        shards.federation(plan.shards[0])


def test_a_state_with_no_sandbox_record_takes_the_manifest_s_node_in_the_federation_s_job(shards, tmp_path):
    path = _manifest(tmp_path, placement="none")
    path.write_text(
        path.read_text().replace(
            'root = "' + str(tmp_path / "fed" / "a") + '"',
            'root = "' + str(tmp_path / "fed" / "a") + '"\nsandbox_node = "node-m1"',
        )
    )
    plan = shards.load_plan(path)
    found = shards.federation(plan.shards[0])
    assert (found["job"], found["node"], found["het_group"]) == ("101", "node-m1", None)
    with pytest.raises(shards.ShardsError, match="no sandbox node"):
        shards.federation(plan.shards[1])


def test_a_manifest_node_that_is_not_the_state_s_is_refused(shards, tmp_path):
    path = _manifest(tmp_path)
    path.write_text(
        path.read_text().replace(
            'root = "' + str(tmp_path / "fed" / "a") + '"',
            'root = "' + str(tmp_path / "fed" / "a") + '"\nsandbox_node = "node-other"',
        )
    )
    plan = shards.load_plan(path)
    with pytest.raises(shards.ShardsError, match="not the one its state file names"):
        shards.federation(plan.shards[0])


def test_a_state_whose_job_holds_the_sandbox_role_names_its_node(shards, tmp_path):
    """O56: `up` places the agent's node as the `sandbox` role of the federation's own job (`services.sandbox`)."""
    plan = shards.load_plan(_manifest(tmp_path, placement="none"))
    state_file = plan.shards[0].root / "hpc-federation.json"
    state = json.loads(state_file.read_text())
    state["services"] = {"sandbox": {"kind": "sandbox", "node": "node-r1"}}
    state_file.write_text(json.dumps(state))
    found = shards.federation(plan.shards[0])
    assert (found["job"], found["node"], found["het_group"]) == ("101", "node-r1", None)


def test_a_sandbox_record_without_its_node_is_refused_whatever_else_names_one(shards, tmp_path):
    """O56: a separate job's record with no node never takes the node of the federation's own job."""
    plan = shards.load_plan(_manifest(tmp_path, placement="separate"))
    state_file = plan.shards[0].root / "hpc-federation.json"
    state = json.loads(state_file.read_text())
    del state["sandbox"]["node"]
    state["services"] = {"sandbox": {"kind": "sandbox", "node": "node-r1"}}
    state_file.write_text(json.dumps(state))
    with pytest.raises(shards.ShardsError, match="no sandbox node"):
        shards.federation(plan.shards[0])
