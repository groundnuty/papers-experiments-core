"""O63: what a shard step leaves on the agent's node (deploy/hpc-federation/shards.py): the Apptainer sandbox's work
root it makes before the dispatcher, from the federation's platform.toml, and its own directory on the node's tmpfs,
which a resume or the federation's down removes once its step is shown gone, and never while it holds an out/ that could
not be archived. The dispatcher is a stand-in; every path and name is invented, and nothing runs srun."""

import argparse
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
SPEC = {"index": 1, "count": 2, "label": "fed-a"}


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_node", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_node"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_node", None)


def step_args(tmp_path, resume=False, archive_left=False):
    study = tmp_path / "study"
    study.mkdir(exist_ok=True)
    (study / "study.toml").write_text("[study]\n")
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir(exist_ok=True)
    return argparse.Namespace(study=study, index=1, count=2, label="fed-a", tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=resume,
                              archive_left=archive_left, snapshot_every=3600.0)  # fmt: skip


def dispatcher(status=0):
    seen = {}

    def run(argv, env, cwd):
        seen.update(argv=list(argv), cwd=Path(cwd), owner=json.loads((Path(cwd) / "step.json").read_text()))
        out = Path(argv[argv.index("run") + 1]) / "out"
        (out / "runs").mkdir(parents=True, exist_ok=True)
        (out / "runs" / "r1.json").write_text("{}")
        (out / "campaign.lock").write_text("")
        return status

    return run, seen


def federation(tmp_path, work_root, backend="apptainer") -> Path:
    """A federation's root whose platform.toml declares the sandbox (only the keys the step reads)."""
    root = tmp_path / "fed" / "root"
    root.mkdir(parents=True, exist_ok=True)
    (root / "platform.toml").write_text(f'[sandbox]\nbackend = "{backend}"\nwork_root = "{work_root}"\n')
    return root


def left(tmp_path, name, *, label="fed-a", pid=None, start="0", root="/fed/root", results=None, phase="running",
         failed=None, owner=True, base=None) -> Path:  # fmt: skip
    """A step's directory on the node's tmpfs, as `step` makes it: its owner record (by default a step killed while its
    dispatcher ran, whose process is gone: this one's pid with another start time), of this campaign (its federation's
    root and results directory) unless told otherwise, and, with `failed`, its archive-failed mark."""
    path = tmp_path / "tmpfs" / f"expcore-shard-{label}.{name}"
    (path / "study" / "out").mkdir(parents=True)
    (path / "study" / "out" / "campaign.lock").write_text("")
    (path / "study" / "out" / "runs").mkdir()
    if owner:
        record = {"shard": {**SPEC, "label": label}, "pid": pid or os.getpid(), "start": start, "root": root,
                  "results": str(results or tmp_path / "results"), "phase": phase, "base": base}  # fmt: skip
        (path / "step.json").write_text(json.dumps(record))
    if failed is not None:
        (path / "ARCHIVE-FAILED.json").write_text(
            json.dumps({"shard": {**SPEC, "label": label}, "exit_status": failed})
        )
    return path


# -- the sandbox's work root ---------------------------------------------------------------------------------------


def test_o63_the_step_makes_the_sandboxs_work_root_0700_before_the_dispatcher(shards, tmp_path, monkeypatch):
    """O63: the federation's platform.toml declares the Apptainer sandbox; its work_root, on the node's tmpfs, exists
    with mode 0700 when the dispatcher starts (the sandbox refuses a start without it), and stays for the next step."""
    args = step_args(tmp_path)
    work_root = args.tmpfs / "agent"
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(federation(tmp_path, work_root)))
    run, seen = dispatcher()

    def runner(argv, env, cwd):
        seen["mode"] = stat.S_IMODE(os.lstat(work_root).st_mode)
        return run(argv, env, cwd)

    assert shards.step(args, runner=runner, core_found=lambda path: True) == 0
    assert seen["mode"] == 0o700
    assert work_root.is_dir()


def test_o63_an_existing_work_root_that_is_this_users_and_0700_is_used(shards, tmp_path, monkeypatch):
    """O63: a work root a step made before (this user's, 0700) is used as it is, with what it holds."""
    args = step_args(tmp_path)
    work_root = args.tmpfs / "agent"
    work_root.mkdir(mode=0o700)
    (work_root / "_runs").mkdir()
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(federation(tmp_path, work_root)))
    run, seen = dispatcher()
    assert shards.step(args, runner=run, core_found=lambda path: True) == 0
    assert seen
    assert (work_root / "_runs").is_dir()


@pytest.mark.parametrize("case", ["link", "open to others", "not under --tmpfs", "not this user's", "no parent"])
def test_o63_a_work_root_that_is_unsafe_or_off_the_nodes_tmpfs_refuses_the_step_before_anything(
    shards, tmp_path, monkeypatch, case
):
    """O63: a link, a directory others can enter, one on the shared filesystem (outside --tmpfs), one of another user,
    or one whose parent is missing: refused, naming what is wrong and never the path, before the dispatcher, with no
    directory of the step made."""
    args = step_args(tmp_path)
    work_root = args.tmpfs / "agent"
    said = {"link": "a link", "open to others": "0700", "not under --tmpfs": "node's tmpfs",
            "not this user's": "not this user's", "no parent": "parent"}[case]  # fmt: skip
    if case == "link":
        (args.tmpfs / "elsewhere").mkdir(mode=0o700)
        work_root.symlink_to(args.tmpfs / "elsewhere")
    elif case == "open to others":
        work_root.mkdir()
        work_root.chmod(0o755)
    elif case == "not under --tmpfs":
        work_root = tmp_path / "shared" / "agent"
        work_root.parent.mkdir()
    elif case == "not this user's":
        work_root.mkdir(mode=0o700)
        uid = os.getuid()
        monkeypatch.setattr(shards.os, "getuid", lambda: uid + 1)
    else:
        work_root = args.tmpfs / "missing" / "agent"
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(federation(tmp_path, work_root)))
    run, seen = dispatcher()
    with pytest.raises(shards.ShardsError, match=said) as refused:
        shards.step(args, runner=run, core_found=lambda path: True)
    assert str(work_root) not in str(refused.value)
    assert not seen
    assert not list(args.tmpfs.glob("expcore-shard-*"))


@pytest.mark.parametrize("backend", ["docker", None])
def test_o63_without_the_apptainer_sandbox_no_work_root_is_made(shards, tmp_path, monkeypatch, backend):
    """O63: a platform file of the Docker backend, or none, makes the step make no work root."""
    args = step_args(tmp_path)
    if backend is not None:
        monkeypatch.setenv("EXPCORE_FED_ROOT", str(federation(tmp_path, args.tmpfs / "agent", backend)))
    else:
        monkeypatch.delenv("EXPCORE_FED_ROOT", raising=False)
    run, _ = dispatcher()
    assert shards.step(args, runner=run, core_found=lambda path: True) == 0
    assert not (args.tmpfs / "agent").exists()


# -- the step's directory on the node's tmpfs ----------------------------------------------------------------------


def test_o63_the_steps_directory_records_its_process_its_campaign_and_its_federation(shards, tmp_path, monkeypatch):
    """O63: a step's directory on the tmpfs records its pid and start time, its shard, its federation's root, its
    results directory and the archive it started from, its phase `running` while the dispatcher runs."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    run, seen = dispatcher()
    shards.step(step_args(tmp_path), runner=run, core_found=lambda path: True)
    owner = seen["owner"]
    assert (owner["pid"], owner["start"]) == (os.getpid(), shards._start_of(os.getpid()))
    assert (owner["shard"], owner["root"], owner["results"]) == (SPEC, "/fed/root", str(tmp_path / "results"))
    assert (owner["phase"], owner["base"]) == ("running", None)
    assert shards.alive(owner["pid"], owner["start"]) is True
    assert not seen["cwd"].exists()  # a step that ends removes it


def resume(shards, tmp_path, monkeypatch, root="/fed/root"):
    monkeypatch.setenv("EXPCORE_FED_ROOT", root)
    run, seen = dispatcher()
    code = shards.step(step_args(tmp_path, resume=True), runner=run, core_found=lambda path: True)
    return code, seen


def test_o63_a_resume_removes_its_shards_killed_steps_and_nothing_of_another_campaign(
    shards, tmp_path, monkeypatch, capsys
):
    """O63: a step killed while its dispatcher ran left its directory: the resume removes it once the step is shown
    gone; it keeps another shard's, one of the same label in another federation or campaign (the node's tmpfs is the
    node's), and one that names no step."""
    gone = left(tmp_path, "gone1")
    other_label = left(tmp_path, "gone2", label="fed-b")
    other_root = left(tmp_path, "gone3", root="/another/root")
    other_results = left(tmp_path, "gone4", results=tmp_path / "other-results")
    unnamed = left(tmp_path, "who", owner=False)
    code, seen = resume(shards, tmp_path, monkeypatch)
    assert (code, bool(seen)) == (0, True)
    assert not gone.exists()
    assert all(path.is_dir() for path in (other_label, other_root, other_results, unnamed))
    err = capsys.readouterr().err
    assert all(f"{path.name} is not a step of this shard of this campaign" in err
               for path in (other_root, other_results, unnamed))  # fmt: skip


def test_o63_a_resume_while_a_step_of_its_shard_still_runs_is_refused(shards, tmp_path, monkeypatch):
    """O63: a resume never runs beside a step of its shard that still runs on the node, nor removes its directory."""
    running = left(tmp_path, "live", start=shards._start_of(os.getpid()))
    with pytest.raises(shards.ShardsError, match="still runs on this node"):
        resume(shards, tmp_path, monkeypatch)
    assert running.is_dir()


def test_o63_a_dispatcher_that_outlived_its_killed_step_keeps_the_directory(shards, tmp_path, monkeypatch, capsys):
    """O63: the step's wrapper killed alone leaves its dispatcher running in the directory (its argv names it): the
    resume is refused and the sweep keeps it, until that process is gone too."""
    path = left(tmp_path, "orphan")
    survivor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", str(path / "study")])
    try:
        assert shards.naming(path) is True
        with pytest.raises(shards.ShardsError, match="still runs on this node"):
            resume(shards, tmp_path, monkeypatch)
        assert shards.main(["sweep", "--tmpfs", str(tmp_path / "tmpfs"), "--root", "/fed/root"]) == 0
        assert f"{path.name} (its step still runs" in capsys.readouterr().out
        assert path.is_dir()
    finally:
        survivor.kill()
        survivor.wait()
    assert shards.naming(path) is False
    assert resume(shards, tmp_path, monkeypatch)[0] == 0
    assert not path.exists()


@pytest.mark.parametrize("mark", [True, False], ids=["marked", "its mark never written"])
def test_o63_a_step_whose_dispatcher_ended_keeps_its_out_whatever_its_mark(shards, tmp_path, monkeypatch, capsys, mark):
    """O63: a step whose dispatcher ended (`archiving`) holds the only whole out/ until it is archived: the resume is
    refused, naming it, and the sweep keeps and names it, whether or not its archive-failed mark could be written (a
    full tmpfs); so does one whose record cannot be read."""
    ended = left(tmp_path, "ended", phase="archiving", failed=0 if mark else None)
    with pytest.raises(shards.ShardsError, match=f"was not archived and is kept on this node in {ended.name}"):
        resume(shards, tmp_path, monkeypatch)
    (ended.parent / "expcore-shard-fed-a.broken").mkdir()
    (ended.parent / "expcore-shard-fed-a.broken" / "step.json").write_text("{trunc")
    assert shards.main(["sweep", "--tmpfs", str(tmp_path / "tmpfs"), "--root", "/fed/root"]) == 0
    assert f"{ended.name} (its out/ was not archived" in capsys.readouterr().out
    assert ended.is_dir()
    assert (ended.parent / "expcore-shard-fed-a.broken").is_dir()


def test_o63_an_out_that_could_not_be_archived_is_kept_and_archived_by_archive_left(shards, tmp_path, monkeypatch):
    """O63: once its dispatcher ends the step records it (`archiving`, with its exit status) before anything else; an
    archive that fails then marks it too; a resume is refused while it is there; `step --archive-left` archives it as
    the step would have, with that status, and removes it; the resume then runs from that archive."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    run, _ = dispatcher(status=3)
    with monkeypatch.context() as patch:
        patch.setattr(shards, "archive", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left")))
        assert shards.step(step_args(tmp_path), runner=run, core_found=lambda path: True) == shards.ARCHIVE_FAILED
    (kept,) = (tmp_path / "tmpfs").glob("expcore-shard-fed-a.*")
    owner = json.loads((kept / "step.json").read_text())
    assert (owner["phase"], owner["exit_status"]) == ("archiving", 3)
    assert json.loads((kept / "ARCHIVE-FAILED.json").read_text())["exit_status"] == 3
    with pytest.raises(shards.ShardsError, match=f"was not archived and is kept on this node in {kept.name}"):
        resume(shards, tmp_path, monkeypatch)
    assert shards.archive_left(step_args(tmp_path, archive_left=True)) == 0
    assert not kept.exists()
    manifest = json.loads((tmp_path / "results" / "fed-a.out.json").read_text())
    assert (manifest["shard"], manifest["exit_status"], manifest["lock"]) == (SPEC, 3, True)
    resumed = {}

    def from_archive(argv, env, cwd):
        resumed["kept"] = (Path(argv[argv.index("run") + 1]) / "out" / "runs" / "r1.json").is_file()
        return 0

    assert shards.step(step_args(tmp_path, resume=True), runner=from_archive, core_found=lambda path: True) == 0
    assert resumed == {"kept": True}


def test_o63_archive_left_takes_only_this_campaigns_out_and_never_over_a_later_archive(shards, tmp_path, monkeypatch):
    """O63: `--archive-left` archives only an ended step of this shard of this campaign (its spec, root and results),
    and only while the published archive is the one that step started from: never another campaign's out/ under the
    same label, never an older out/ over a later archive."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    with pytest.raises(shards.ShardsError, match="no out/ of this shard of this campaign waits"):
        shards.archive_left(step_args(tmp_path, archive_left=True))
    foreign = left(tmp_path, "foreign", phase="archiving", root="/another/root")
    with pytest.raises(shards.ShardsError, match="no out/ of this shard of this campaign waits"):
        shards.archive_left(step_args(tmp_path, archive_left=True))
    assert foreign.is_dir()
    (tmp_path / "results" / "fed-a.out.json").write_text(json.dumps({"sha256": "a" * 64}))
    older = left(tmp_path, "older", phase="archiving", base="b" * 64)
    with pytest.raises(shards.ShardsError, match="an archive was published after the step"):
        shards.archive_left(step_args(tmp_path, archive_left=True))
    assert older.is_dir()


def test_o63_the_sweep_removes_this_federations_killed_steps_and_names_each_one_it_keeps(shards, tmp_path, capsys):
    """O63: down's sweep of the agent's node: a killed step's directory of this federation goes, whatever its shard;
    one of another federation and one that names no step stay unnamed; one still running and one whose out/ was not
    archived stay, named."""
    step_args(tmp_path)
    gone = left(tmp_path, "gone", label="fed-b")
    other = left(tmp_path, "other", root="/another/root")
    unnamed = left(tmp_path, "who", owner=False)
    running = left(tmp_path, "live", start=shards._start_of(os.getpid()))
    ended = left(tmp_path, "ended", phase="archiving", failed=1)
    assert shards.main(["sweep", "--tmpfs", str(tmp_path / "tmpfs"), "--root", "/fed/root"]) == 0
    printed = capsys.readouterr().out
    assert not gone.exists()
    assert all(path.is_dir() for path in (other, unnamed, running, ended))
    assert "1 step directory(ies) removed" in printed
    assert f"{ended.name} (its out/ was not archived" in printed
    assert f"{running.name} (its step still runs" in printed
    assert other.name not in printed


# -- the federation's platform file beside the study ---------------------------------------------------------------


def test_o63_each_step_copies_its_federations_platform_file_beside_an_unchanged_study_toml(
    shards, tmp_path, monkeypatch
):
    """O63: two shards on two federations: each step's copy of the study holds its own federation's platform.toml
    beside study.toml, as a regular file (never a link out of the study) of the root's bytes, mode 0600; study.toml is
    the study's own bytes in both, so its hash is every shard's."""
    seen = {}
    study_toml = None
    for label, index in (("fed-a", 1), ("fed-b", 2)):
        root = tmp_path / "fed" / label
        root.mkdir(parents=True)
        (root / "platform.toml").write_text(f'zone = {{ url = "https://zone-{label}.example.org" }}\n')
        monkeypatch.setenv("EXPCORE_FED_ROOT", str(root))
        args = step_args(tmp_path)
        args.label, args.index = label, index

        def run(argv, env, cwd, label=label):
            copy = Path(argv[argv.index("run") + 1])
            placed = copy / "platform.toml"
            seen[label] = (placed.is_symlink(), placed.read_bytes(), stat.S_IMODE(placed.stat().st_mode),
                           (copy / "study.toml").read_bytes())  # fmt: skip
            return 0

        shards.step(args, runner=run, core_found=lambda path: True)
        study_toml = (args.study / "study.toml").read_bytes()
    for label in ("fed-a", "fed-b"):
        linked, placed, mode, copied = seen[label]
        assert (linked, mode) == (False, 0o600)
        assert placed == (tmp_path / "fed" / label / "platform.toml").read_bytes()
        assert copied == study_toml
    assert seen["fed-a"][1] != seen["fed-b"][1]


def test_o63_a_study_that_holds_a_platform_file_of_its_own_is_refused(shards, tmp_path):
    """O63: the step places the federation's platform file; a study with one of its own is refused before anything."""
    args = step_args(tmp_path)
    (args.study / "platform.toml").write_text("")
    run, seen = dispatcher()
    with pytest.raises(shards.ShardsError, match=r"holds a platform\.toml of its own"):
        shards.step(args, runner=run, core_found=lambda path: True)
    assert not seen
