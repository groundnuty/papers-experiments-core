"""O63: a shard step's pre-step guard (deploy/hpc-federation/shards.py): a script of the study, declared in the shard
plan, that the step runs first, before it makes any directory, under the step's own allowlist, with `-I` and the
study's root as its working directory, and whose non-zero exit, hash mismatch or timeout refuses the step with nothing
made. The dispatcher is a stand-in; every path and name is invented, and nothing runs srun."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
SPEC = {"index": 1, "count": 2, "label": "fed-a"}
GUARD = "guards/pre_step.py"
HOME_NAMES = {"HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "APPTAINER_CONFIGDIR",
              "APPTAINER_CACHEDIR", "UV_CACHE_DIR", "PIP_CACHE_DIR", "PYTHONNOUSERSITE", "GIT_CONFIG_GLOBAL",
              "GIT_CONFIG_NOSYSTEM"}  # fmt: skip
NO_HOME = "/dev/null/hpcop-home"


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_pre_step", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_pre_step"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_pre_step", None)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def make_study(tmp_path: Path, guard: str | None = "") -> Path:
    """A stamped study copy with its campaign.toml and, unless `guard` is None, its guard script."""
    study = tmp_path / "study"
    (study / "guards").mkdir(parents=True, exist_ok=True)
    study.chmod(0o755)  # whatever the umask: the step refuses a root that group or others can write
    (study / "study.toml").write_text("[study]\n")
    (study / "campaign.toml").write_text("[scheduler]\nworkers = 1\n")
    if guard is not None:
        (study / GUARD).write_text(guard)
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir(exist_ok=True)
    return study


def step_args(tmp_path: Path, guard: str | None = "", *, pre_step=GUARD, timeout=20.0, resume=False, digest=None):
    """The step's arguments as its generated line gives them: the guard, the SHA-256 the plan recorded for `guard`
    (or `digest`) and the bound; `guard=None` (or `pre_step=None`) declares none."""
    study = make_study(tmp_path, guard)
    declared = {} if pre_step is None else {"pre_step": pre_step, "pre_step_timeout": timeout,
                                            "pre_step_sha256": digest or sha(guard or "")}  # fmt: skip
    return argparse.Namespace(study=study, index=1, count=2, label="fed-a", tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=resume, archive_left=False,
                              snapshot_every=3600.0, **declared)  # fmt: skip


def dispatcher(status=0):
    seen = {}

    def run(argv, env, cwd):
        seen.update(argv=list(argv), cwd=Path(cwd), owner=json.loads((Path(cwd) / "step.json").read_text()))
        out = Path(argv[argv.index("run") + 1]) / "out"
        (out / "runs").mkdir(parents=True, exist_ok=True)
        (out / "campaign.lock").write_text("")
        return status

    return run, seen


def federation(tmp_path: Path, work_root: Path) -> Path:
    """A federation's root whose platform.toml declares the Apptainer sandbox (only the keys the step reads)."""
    root = tmp_path / "fed" / "root"
    root.mkdir(parents=True, exist_ok=True)
    (root / "platform.toml").write_text(f'[sandbox]\nbackend = "apptainer"\nwork_root = "{work_root}"\n')
    return root


def nothing_made(tmp_path: Path, work_root: Path | None = None) -> None:
    """No directory of the step: the tmpfs holds neither a step's directory nor the sandbox's work root, and the
    results hold neither a journal directory nor an archive."""
    assert not list((tmp_path / "tmpfs").iterdir())
    assert work_root is None or not work_root.exists()
    assert not list((tmp_path / "results").iterdir())


def run_step(shards, args, **kwargs):
    run, seen = dispatcher()
    code = shards.step(args, runner=kwargs.pop("runner", run), core_found=kwargs.pop("core_found", lambda p: True))
    return code, seen


# -- a guard that passes, and what it is told --------------------------------------------------------------------


def test_o63_a_guard_that_exits_0_lets_the_step_proceed_and_the_step_records_it(shards, tmp_path, monkeypatch):
    """O63: the guard runs, exits 0, and the step goes on to its dispatcher; its path, SHA-256 and exit status are in
    the step's record while it runs and in the archive's manifest, which is what comes back."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    code, seen = run_step(shards, step_args(tmp_path, "raise SystemExit(0)\n"))
    assert code == 0
    record = {"path": GUARD, "sha256": sha("raise SystemExit(0)\n"), "exit_status": 0}
    assert seen["owner"]["pre_step"] == record
    assert json.loads((tmp_path / "results" / "fed-a.out.json").read_text())["pre_step"] == record


def test_o63_the_guard_is_run_isolated_in_the_studys_root_under_the_steps_allowlist_with_the_label_and_campaign(
    shards, tmp_path, monkeypatch
):
    """O63: the same interpreter with `-I` (a `json.py` in the study's root is not imported, an environment variable
    that tunes Python is not read), the study's root as its working directory whatever the step's was, an environment
    of the allowlist and the step's own home alone (no canary, no HOME of the account), and as its arguments the shard's
    label and the stamped campaign.toml's path."""
    seen = tmp_path / "seen.json"
    guard = (
        "import json, os, sys\n"
        f"json.dump({{'isolated': sys.flags.isolated, 'bytecode': sys.flags.dont_write_bytecode, "
        f"'cwd': os.getcwd(), 'argv': sys.argv, 'env': dict(os.environ), 'exe': sys.executable, "
        f"'real': os.path.realpath(sys.argv[2]), 'campaign': open(sys.argv[2]).read()}}, open({str(seen)!r}, 'w'))\n"
    )
    args = step_args(tmp_path, guard)
    (args.study / "json.py").write_text("raise SystemExit(99)\n")  # what `-I` keeps out of an import
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    for name, value in {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "EXPCORE_FED_ROOT_PARENT": "/fed",
                        "EXPCORE_FED_ROOT": "/fed/root", "EXPCORE_CANARY": "x", "HOME": "/home/canary",
                        "PYTHONPATH": "/canary", "ANTHROPIC_API_KEY": "canary"}.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    assert run_step(shards, args)[0] == 0
    found = json.loads(seen.read_text())
    assert (found["isolated"], found["bytecode"]) == (1, 1)  # `-I -B`
    assert found["exe"] == sys.executable
    assert Path(found["cwd"]).resolve() == args.study.resolve()
    assert found["argv"][1] == "fed-a"  # campaign.toml is named through the root the step holds, never by its path
    assert re.fullmatch(r"/proc/self/fd/\d+/campaign\.toml", found["argv"][2])
    assert found["real"] == str((args.study / "campaign.toml").resolve())
    assert found["campaign"] == "[scheduler]\nworkers = 1\n"
    assert set(found["env"]) == {"PATH", "LC_ALL", "EXPCORE_FED_ROOT_PARENT", "EXPCORE_FED_ROOT"} | HOME_NAMES
    assert found["env"]["HOME"] == NO_HOME
    assert found["env"]["EXPCORE_FED_ROOT"] == "/fed/root"


def test_o63_the_guard_runs_the_bytes_the_step_hashed_never_the_file_read_again(shards, tmp_path, monkeypatch):
    """O63: a guard file replaced after its hash was checked is never the one that runs: the step runs the bytes it
    hashed (the script goes in on the interpreter's stdin)."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    args = step_args(tmp_path, "raise SystemExit(0)\n")
    real = shards.run_hook

    def swapping(source, *rest, **more):
        (args.study / GUARD).write_text("raise SystemExit(5)\n")  # the file changes between the check and the run
        return real(source, *rest, **more)

    monkeypatch.setattr(shards, "run_hook", swapping)
    assert run_step(shards, args)[0] == 0


# -- a guard that refuses -----------------------------------------------------------------------------------------


def test_o63_a_guard_that_exits_non_zero_refuses_the_step_with_nothing_made(shards, tmp_path, monkeypatch, capfd):
    """O63: a non-zero exit refuses the step (`PRE_STEP_REFUSED`), naming the guard and its exit status: the dispatcher
    never starts, and neither the sandbox's work root, the step's directory, the journals nor an archive is made; the
    core check, which comes after, never ran."""
    args = step_args(tmp_path, "import sys\nsys.stderr.write('study refusal: workers is 4\\n')\nraise SystemExit(7)\n")
    work_root = args.tmpfs / "agent"
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(federation(tmp_path, work_root)))
    checked = []
    code, seen = run_step(shards, args, core_found=lambda path: checked.append(path) or True)
    assert code == shards.PRE_STEP_REFUSED == 73
    assert not seen
    assert not checked
    nothing_made(tmp_path, work_root)
    said = capfd.readouterr().err
    assert GUARD in said
    assert "exit status 7" in said
    assert "study refusal: workers is 4" in said  # the guard's own words reach the step's stderr


def test_o63_a_refused_resume_removes_nothing_of_an_earlier_step_and_a_passing_one_does(shards, tmp_path, monkeypatch):
    """O63: a resume clears its shard's killed steps' directories, so a guard that refuses must run before that: the
    killed step's directory is still there; with a guard that passes, the same resume removes it."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    killed = tmp_path / "tmpfs" / "expcore-shard-fed-a.dead"
    (killed / "study" / "out").mkdir(parents=True)
    record = {"shard": SPEC, "pid": os.getpid(), "start": "0", "root": "/fed/root", "base": None,
              "results": str(tmp_path / "results"), "phase": "running"}  # fmt: skip
    (killed / "step.json").write_text(json.dumps(record))
    code, seen = run_step(shards, step_args(tmp_path, "raise SystemExit(1)\n", resume=True))
    assert code == shards.PRE_STEP_REFUSED
    assert not seen
    assert killed.is_dir()
    code, seen = run_step(shards, step_args(tmp_path, "raise SystemExit(0)\n", resume=True))
    assert code == 0
    assert seen
    assert not killed.exists()


def test_o63_a_guard_that_changed_since_the_plan_refuses_the_step_with_nothing_made(
    shards, tmp_path, monkeypatch, capsys
):
    """O63: the hash the plan recorded is the guard's: another file's is refused, naming the guard, before it runs
    (a guard that would write a marker leaves none) and before anything is made."""
    marker = tmp_path / "ran"
    guard = f"open({str(marker)!r}, 'w').close()\n"
    args = step_args(tmp_path, guard, digest=sha("another\n"))
    work_root = args.tmpfs / "agent"
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(federation(tmp_path, work_root)))
    code, seen = run_step(shards, args)
    assert code == shards.PRE_STEP_REFUSED
    assert not seen
    assert not marker.exists()
    nothing_made(tmp_path, work_root)
    said = capsys.readouterr().err
    assert GUARD in said
    assert "sha256" in said


def alive(pid: int) -> bool:
    """Whether a process of this host still runs (a zombie, which only waits to be reaped, does not)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
    except OSError:
        return False
    return state != "Z"


def test_o63_a_guard_that_overruns_its_bound_is_killed_and_refuses_the_step(shards, tmp_path, monkeypatch, capsys):
    """O63: a guard past `--pre-step-timeout` is killed with everything it started, and the step is refused naming the
    guard and the bound, in about that time, with nothing made."""
    pids = tmp_path / "pids"
    guard = (
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(60)\n"
    )
    args = step_args(tmp_path, guard, timeout=1.0)
    work_root = args.tmpfs / "agent"
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(federation(tmp_path, work_root)))
    began = time.monotonic()
    code, seen = run_step(shards, args)
    assert time.monotonic() - began < 30
    assert code == shards.PRE_STEP_REFUSED
    assert not seen
    nothing_made(tmp_path, work_root)
    said = capsys.readouterr().err
    assert GUARD in said
    assert "did not end within 1 s" in said
    for pid in map(int, pids.read_text().split()):  # the guard and its child: none is left running
        for _ in range(50):
            if not alive(pid):
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"process {pid} of the guard still runs")


def test_o63_a_guard_killed_by_a_signal_refuses_the_step_naming_the_signal(shards, tmp_path, monkeypatch, capsys):
    """O63: a guard that dies by a signal is a refusal, never a pass."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    code, seen = run_step(shards, step_args(tmp_path, "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"))
    assert code == shards.PRE_STEP_REFUSED
    assert not seen
    assert "signal 9" in capsys.readouterr().err
    nothing_made(tmp_path)


@pytest.mark.parametrize("case", ["group-writable", "world-writable", "not this user's", "a file", "missing"])
def test_o63_a_study_root_that_is_shared_or_not_ours_refuses_the_step_before_the_guard_runs(
    shards, tmp_path, monkeypatch, capsys, case
):
    """O63: the guard's working directory is the study's root, so a root another user could write to (group or other
    write bit, owned by another user), a file or no path at all refuses the step with nothing made, and the guard,
    which would write a marker, never runs."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    marker = tmp_path / "ran"
    args = step_args(tmp_path, f"open({str(marker)!r}, 'w').close()\n")
    if case == "group-writable":
        args.study.chmod(0o775)
    elif case == "world-writable":
        args.study.chmod(0o757)
    elif case == "not this user's":
        uid = os.getuid()
        monkeypatch.setattr(shards.os, "getuid", lambda: uid + 1)
    elif case == "a file":
        args.study = tmp_path / "study" / "study.toml"
    else:
        args.study = tmp_path / "no-study"
    code, seen = run_step(shards, args)
    assert code == shards.PRE_STEP_REFUSED
    assert not seen
    assert not marker.exists()
    nothing_made(tmp_path)
    assert GUARD in capsys.readouterr().err


@pytest.mark.parametrize("path", ["/abs/pre_step.py", "../outside.py", "guards/../pre_step.py", "./guards/pre_step.py",
                                  "guards//pre_step.py", "guards/", ""])  # fmt: skip
def test_o63_a_path_that_is_absolute_or_climbs_or_is_not_plain_refuses_the_step_with_nothing_made(
    shards, tmp_path, monkeypatch, path
):
    """O63: a line carrying a guard path that is absolute, has a `..`, `.` or empty part or names nothing is refused
    at the step too (a line typed by hand can carry anything), with nothing made."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    code, seen = run_step(shards, step_args(tmp_path, "", pre_step=path))
    assert code == shards.PRE_STEP_REFUSED
    assert not seen
    nothing_made(tmp_path)


@pytest.mark.parametrize("what", ["file link", "directory link", "directory", "missing", "campaign link",
                                  "campaign missing"])  # fmt: skip
def test_o63_a_guard_that_is_a_link_or_no_file_refuses_the_step_with_nothing_made(shards, tmp_path, monkeypatch, what):
    """O63: a guard reached through a link (as its last part or any part), a directory, a file that is not there, or a
    study whose campaign.toml is a link or missing: refused with nothing made, and the guard it would have run
    (a copy outside the study) never runs."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    marker = tmp_path / "ran"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "pre_step.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    args = step_args(tmp_path, None)
    guard_text = (outside / "pre_step.py").read_text()
    args.pre_step_sha256 = sha(guard_text)  # the hash of what the link points to: the check is not the hash's alone
    if what == "file link":
        (args.study / GUARD).symlink_to(outside / "pre_step.py")
    elif what == "directory link":
        (args.study / "guards").rmdir()
        (args.study / "guards").symlink_to(outside)
    elif what == "directory":
        (args.study / GUARD).mkdir()
    elif what in ("campaign link", "campaign missing"):
        (args.study / GUARD).write_text(guard_text)
        (args.study / "campaign.toml").unlink()
        if what == "campaign link":
            (args.study / "campaign.toml").symlink_to(outside / "pre_step.py")
    code, seen = run_step(shards, args)
    assert code == shards.PRE_STEP_REFUSED
    assert not seen
    assert not marker.exists()
    nothing_made(tmp_path)


@pytest.mark.parametrize("given", ["sha256", "timeout", "bad sha256", "bad timeout"])
def test_o63_a_guard_declared_by_halves_or_with_a_bad_hash_or_bound_refuses_the_step(
    shards, tmp_path, monkeypatch, given
):
    """O63: the hash and the bound belong to a declared guard, a path comes with its hash, the hash is 64 hex digits and
    the bound a number of seconds above 0 and at most 600: anything else is refused with nothing made."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    args = step_args(tmp_path, "")
    if given == "sha256":
        args = argparse.Namespace(**{**vars(args), "pre_step": None, "pre_step_timeout": None})
    elif given == "timeout":
        args = argparse.Namespace(**{**vars(args), "pre_step": None, "pre_step_sha256": None})
    elif given == "bad sha256":
        args.pre_step_sha256 = "abc"
    else:
        args.pre_step_timeout = 0.0
    code, seen = run_step(shards, args)
    assert code == shards.PRE_STEP_REFUSED
    assert not seen
    nothing_made(tmp_path)


def test_o63_a_path_without_its_hash_refuses_the_step(shards, tmp_path, monkeypatch):
    """O63: a guard with no hash recorded is never run: the hash is what the plan pinned."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    args = step_args(tmp_path, "")
    args.pre_step_sha256 = None
    code, seen = run_step(shards, args)
    assert code == shards.PRE_STEP_REFUSED
    assert not seen
    nothing_made(tmp_path)


# -- no guard declared --------------------------------------------------------------------------------------------


def test_o63_a_step_with_no_guard_declared_runs_as_before_and_records_none(shards, tmp_path, monkeypatch):
    """O63: no guard declared (as the step's arguments were before the feature): the step runs its dispatcher and
    its step record and its manifest hold no `pre_step`."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    args = step_args(tmp_path, None, pre_step=None)
    assert not any(name.startswith("pre_step") for name in vars(args))
    code, seen = run_step(shards, args)
    assert code == 0
    assert "pre_step" not in seen["owner"]
    assert "pre_step" not in json.loads((tmp_path / "results" / "fed-a.out.json").read_text())


def test_o63_archive_left_archives_a_kept_out_without_the_guard_and_carries_its_record(shards, tmp_path, monkeypatch):
    """O63: `--archive-left` starts no work and makes no work root: given the generated line's guard arguments it does
    not run the guard (a refusing guard cannot strand the only whole out/), and it archives with the record of the
    guard the step that kept the out/ had run."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    args = step_args(tmp_path, "raise SystemExit(0)\n")
    run, _ = dispatcher(status=3)
    with monkeypatch.context() as patch:
        patch.setattr(shards, "archive", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left")))
        assert shards.step(args, runner=run, core_found=lambda path: True) == shards.ARCHIVE_FAILED
    (kept,) = (tmp_path / "tmpfs").glob("expcore-shard-fed-a.*")
    owner = json.loads((kept / "step.json").read_text())
    (kept / "step.json").write_text(json.dumps({**owner, "start": "0"}))  # the step ran in this process: gone
    (args.study / GUARD).write_text("raise SystemExit(1)\n")  # a guard that would now refuse
    left_args = argparse.Namespace(**{**vars(args), "archive_left": True})
    assert shards.archive_left(left_args) == 0
    manifest = json.loads((tmp_path / "results" / "fed-a.out.json").read_text())
    assert manifest["pre_step"] == {"path": GUARD, "sha256": sha("raise SystemExit(0)\n"), "exit_status": 0}


# -- the shard plan: declaring and pinning it ---------------------------------------------------------------------


def manifest(tmp_path: Path, extra: str = "", guard: str | None = "raise SystemExit(0)\n") -> Path:
    """A shard plan of two federations; `extra` (top-level keys) goes before the `[[shards]]`."""
    make_study(tmp_path, guard)
    lines = [f'study = "{tmp_path / "study"}"', f'python = "{tmp_path / "venv" / "python"}"',
             f'core_src = "{tmp_path / "core" / "src"}"', f'results = "{tmp_path / "results"}"',
             f'tmpfs = "{tmp_path / "tmpfs"}"', 'step_path = "/usr/bin:/bin"', "cpus = 6",
             'memory = "18g"']  # fmt: skip
    for n, name in enumerate(("a", "b"), 1):
        root = tmp_path / "fed" / name
        root.mkdir(parents=True, exist_ok=True)
        state = {"format": "expcore-hpc-federation/1", "allocation": {"job": str(100 + n), "name": f"hpcop-x-{n}"},
                 "sandbox": {"placement": "het", "node": f"node-s{n}", "het_group": 1}, "phase": "linked"}  # fmt: skip
        (root / "hpc-federation.json").write_text(json.dumps(state))
        (root / "platform.toml").write_text('[space]\nname = "s"\ndata_manifest = "m.json"\n')
    if extra:
        lines.append(extra)
    for label, name in (("fed-a", "a"), ("fed-b", "b")):
        lines += ["[[shards]]", f'label = "{label}"', f'root = "{tmp_path / "fed" / name}"']
    path = tmp_path / "shards.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def lines_of(shards, tmp_path, capsys, command, extra="", guard="raise SystemExit(0)\n") -> list[str]:
    path = manifest(tmp_path, extra, guard)
    assert shards.main([command, "--manifest", str(path)]) == 0
    return capsys.readouterr().out.splitlines()


def test_o63_a_declared_guard_is_pinned_by_its_hash_in_the_plan_and_in_every_line(shards, tmp_path, capsys):
    """O63: `plan` records the guard's path, its SHA-256 and its bound for each shard, and each line carries the same
    three as the step's arguments; the line is otherwise the line with no guard, with nothing that skips the guard
    (not one more word)."""
    base = lines_of(shards, tmp_path / "plain", capsys, "lines", guard=None)
    declared = 'pre_step = "guards/pre_step.py"\npre_step_timeout_s = 30'
    plans = [json.loads(line) for line in lines_of(shards, tmp_path / "pinned", capsys, "plan", declared)]
    pin = sha("raise SystemExit(0)\n")
    assert [p["pre_step"] for p in plans] == [{"path": GUARD, "sha256": pin, "timeout_s": 30.0}] * 2
    words = lines_of(shards, tmp_path / "pinned", capsys, "lines", declared)
    assert len(words) == len(base) == 2
    for line, plain in zip(words, base, strict=True):
        tail = " --pre-step guards/pre_step.py --pre-step-sha256 " + pin + " --pre-step-timeout 30"
        assert line.endswith(tail)
        assert line[: -len(tail)].replace(str(tmp_path / "pinned"), "ROOT").split() == (
            plain.replace(str(tmp_path / "plain"), "ROOT").split()
        )


def test_o63_the_default_bound_is_60_seconds_and_a_plan_without_a_guard_prints_what_it_printed(
    shards, tmp_path, capsys
):
    """O63: a guard with no `pre_step_timeout_s` is bounded at 60 s on the line; with no guard the plan's records carry
    no `pre_step` key and the line carries no `--pre-step`."""
    declared = lines_of(shards, tmp_path / "a", capsys, "lines", 'pre_step = "guards/pre_step.py"')
    assert all(line.endswith("--pre-step-timeout 60") for line in declared)
    plans = [json.loads(line) for line in lines_of(shards, tmp_path / "b", capsys, "plan", guard=None)]
    assert all(set(p) == {"shard", "argv"} for p in plans)
    assert not any(word.startswith("--pre-step") for p in plans for word in p["argv"])


def test_o63_a_line_with_no_guard_is_byte_for_byte_the_line_it_was(shards, tmp_path):
    """O63: no guard declared: the step's line is the literal it was before the feature existed."""
    plan = shards.load_plan(manifest(tmp_path, guard=None))
    shard = plan.shards[0]
    line = shards.step_argv(plan, shard, shards.federation(shard), SCRIPT)
    homeless = [f"{k}={NO_HOME}{suffix}" for k, suffix in
                (("HOME", ""), ("XDG_CONFIG_HOME", "/.config"), ("XDG_CACHE_HOME", "/.cache"),
                 ("XDG_DATA_HOME", "/.local/share"), ("XDG_STATE_HOME", "/.local/state"),
                 ("APPTAINER_CONFIGDIR", "/.apptainer"), ("APPTAINER_CACHEDIR", "/.apptainer/cache"),
                 ("UV_CACHE_DIR", "/.cache/uv"), ("PIP_CACHE_DIR", "/.cache/pip"))]  # fmt: skip
    assert line == [
        "srun", "--jobid=101", "--het-group=1", "--exact", "-N1", "-n1", "-w", "node-s1", "-c", "6", "--mem=18g",
        "-J", "hpcop-x-1-shard-1", "--export=PATH", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LC_ALL=C",
        f"EXPCORE_FED_ROOT_PARENT={tmp_path / 'fed'}", f"EXPCORE_FED_ROOT={tmp_path / 'fed' / 'a'}", *homeless,
        "PYTHONNOUSERSITE=1", "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_NOSYSTEM=1",
        str(tmp_path / "venv" / "python"), "-I", str(SCRIPT), "step", "--study", str(tmp_path / "study"),
        "--index", "1", "--count", "2", "--label", "fed-a", "--tmpfs", str(tmp_path / "tmpfs"),
        "--results", str(tmp_path / "results"), "--core-src", str(tmp_path / "core" / "src"),
        "--snapshot-every", "900",
    ]  # fmt: skip


def test_o63_the_generated_line_runs_the_guard_first_even_before_the_core_check(shards, tmp_path, capsys, monkeypatch):
    """O63: run as typed (`main` on the generated line's own arguments), a line whose guard refuses ends with status
    73, never the core check's 97 that comes next when the guard passes; and a guard changed after the plan
    refuses it, naming the guard. The line is run from another directory with no guard-skipping option to give."""
    monkeypatch.chdir(tmp_path)
    declared = 'pre_step = "guards/pre_step.py"\npre_step_timeout_s = 20'
    ok = lines_of(shards, tmp_path / "ok", capsys, "lines", declared, "raise SystemExit(0)\n")[0]
    bad = lines_of(shards, tmp_path / "bad", capsys, "lines", declared, "raise SystemExit(3)\n")[0]
    changed = lines_of(shards, tmp_path / "changed", capsys, "lines", declared, "raise SystemExit(0)\n")[0]
    (tmp_path / "changed" / "study" / GUARD).write_text("raise SystemExit(0)  # edited since the plan\n")

    def typed(line: str) -> list[str]:
        words = line.split()
        return words[words.index("step") :]

    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    assert shards.main(typed(ok)) == shards.CORE_MISMATCH  # the guard passed: the core check, which fails, was next
    assert shards.main(typed(bad)) == shards.PRE_STEP_REFUSED
    capsys.readouterr()
    assert shards.main(typed(changed)) == shards.PRE_STEP_REFUSED
    assert "sha256" in capsys.readouterr().err
    assert not list((tmp_path / "ok" / "tmpfs").iterdir())


@pytest.mark.parametrize("value", ['"/abs/pre_step.py"', '"../outside.py"', '"guards/../pre_step.py"',
                                   '"./pre_step.py"', '"guards/"', '""', "3", "true",
                                   '["guards/pre_step.py"]'])  # fmt: skip
def test_o63_a_guard_path_that_leaves_the_study_or_is_not_a_plain_relative_path_refuses_the_plan(
    shards, tmp_path, capsys, value
):
    """O63: the plan's guard is a path relative to the study's root, with no `..`: refused when the manifest is read,
    by `plan` and `lines`, with nothing printed."""
    path = manifest(tmp_path, f"pre_step = {value}")
    for command in ("plan", "lines"):
        assert shards.main([command, "--manifest", str(path)]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "pre_step" in captured.err


@pytest.mark.parametrize("what", ["file link", "directory link", "outside link", "directory", "missing", "huge"])
def test_o63_a_guard_that_is_a_link_outside_the_study_or_no_file_refuses_the_plan_at_plan_time(
    shards, tmp_path, capsys, what
):
    """O63: a guard that is a link (as its last part or any part, in or out of the study), a directory, missing or over
    1 MiB refuses `plan` and `lines` with nothing printed, naming it."""
    path = manifest(tmp_path, f'pre_step = "{GUARD}"')
    study = tmp_path / "study"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "pre_step.py").write_text("raise SystemExit(0)\n")
    (study / GUARD).unlink()
    if what == "file link":
        (study / "inside.py").write_text("raise SystemExit(0)\n")
        (study / GUARD).symlink_to(study / "inside.py")
    elif what == "directory link":
        (study / "guards").rmdir()
        (study / "guards").symlink_to(outside)
    elif what == "outside link":
        (study / GUARD).symlink_to(outside / "pre_step.py")
    elif what == "directory":
        (study / GUARD).mkdir()
    elif what == "huge":
        (study / GUARD).write_bytes(b"#" * ((1 << 20) + 1))
    for command in ("plan", "lines"):
        assert shards.main([command, "--manifest", str(path)]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert GUARD in captured.err


@pytest.mark.parametrize("extra", ["pre_step_timeout_s = 30", 'pre_step = "guards/pre_step.py"\npre_step_timeout_s = 0',
                                   'pre_step = "guards/pre_step.py"\npre_step_timeout_s = 601',
                                   'pre_step = "guards/pre_step.py"\npre_step_timeout_s = "9"'])  # fmt: skip
def test_o63_a_bound_without_a_guard_or_out_of_range_refuses_the_manifest(shards, tmp_path, extra):
    """O63: `pre_step_timeout_s` goes with `pre_step` and is a number of seconds above 0 and at most 600."""
    with pytest.raises(shards.ShardsError, match="pre_step_timeout_s"):
        shards.load_plan(manifest(tmp_path, extra))


def test_o63_collect_does_not_read_the_guard(shards, tmp_path):
    """O63: the manifest of a plan that declares a guard loads with the guard's file gone (`collect` reads the
    manifest and the archives, never the guard): only `plan` and `lines` pin it."""
    path = manifest(tmp_path, f'pre_step = "{GUARD}"')
    (tmp_path / "study" / GUARD).unlink()
    plan = shards.load_plan(path)
    assert (plan.pre_step, plan.pre_step_sha256) == (GUARD, None)
    with pytest.raises(shards.ShardsError, match="does not exist"):
        shards.pinned(plan)
    with pytest.raises(shards.ShardsError, match="pinned"):  # a line is never made from a plan whose guard is not
        shards.step_argv(plan, plan.shards[0], shards.federation(plan.shards[0]), SCRIPT)


def test_o63_a_study_root_renamed_after_its_check_is_not_the_one_the_guard_runs_in(shards, tmp_path, monkeypatch):
    """O63: the step holds the root it checked: a directory put at the study's path after the check (the checked one
    renamed away) is neither the guard's working directory nor the place its campaign.toml is read from."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    seen = tmp_path / "seen.json"
    guard = (
        "import json, os, sys\n"
        f"json.dump({{'cwd': os.path.realpath(os.getcwd()), 'campaign': open(sys.argv[2]).read()}}, "
        f"open({str(seen)!r}, 'w'))\n"
    )
    args = step_args(tmp_path, guard)
    real = shards.run_hook

    def swapped(*more, **kwargs):
        args.study.rename(tmp_path / "study-moved")
        args.study.mkdir()
        args.study.chmod(0o755)
        (args.study / "campaign.toml").write_text("[scheduler]\nworkers = 99\n")
        return real(*more, **kwargs)

    monkeypatch.setattr(shards, "run_hook", swapped)
    run_step(shards, args)
    found = json.loads(seen.read_text())
    assert found["cwd"] == str((tmp_path / "study-moved").resolve())
    assert found["campaign"] == "[scheduler]\nworkers = 1\n"


def test_o63_a_guard_whose_leader_exits_as_the_bound_expires_still_has_everything_it_started_killed(
    shards, tmp_path, monkeypatch, capfd
):
    """O63: the bound runs out just as the guard's leader exits (here: the leader has exited and is not yet reaped when
    the step reports the timeout): the group is killed all the same, not only when the leader is found alive, so the
    helper the guard left running does not outlive the step that said it was killed."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    pids = tmp_path / "pids"
    guard = (
        "import subprocess, sys\n"
        "helper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pids)!r}, 'w').write(str(helper.pid))\n"
    )

    class Late(subprocess.Popen):
        def communicate(self, input=None, timeout=None):  # the bound expires after the leader has exited, unreaped
            self.stdin.write(input)
            self.stdin.close()
            os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOWAIT)
            raise subprocess.TimeoutExpired(self.args, timeout)

    monkeypatch.setattr(shards.subprocess, "Popen", Late)
    code, seen = run_step(shards, step_args(tmp_path, guard))
    assert code == shards.PRE_STEP_REFUSED
    assert not seen
    assert "did not end within" in capfd.readouterr().err
    helper = int(pids.read_text())
    for _ in range(50):
        if not alive(helper):
            break
        time.sleep(0.1)
    else:
        pytest.fail("the guard's helper still runs")
    nothing_made(tmp_path)


def test_o63_a_step_told_to_stop_while_its_guard_runs_kills_the_guard_and_makes_nothing(
    shards, tmp_path, monkeypatch, capfd
):
    """O63: SIGTERM to the step during the guard (a scancel) kills the guard's whole group, which lives in a session of
    its own, so it is not left to run out its bound; the step is refused, with nothing made."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    earlier = signal.signal(signal.SIGTERM, lambda *_: None)  # a SIGTERM that arrives too early is not the test's end
    timer = threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGTERM))
    try:
        timer.start()
        began = time.monotonic()
        code, seen = run_step(shards, step_args(tmp_path, "import time\ntime.sleep(60)\n"))
        assert time.monotonic() - began < 30
        assert signal.getsignal(signal.SIGTERM).__name__ == "<lambda>"  # the step's handlers are put back
    finally:
        timer.cancel()
        signal.signal(signal.SIGTERM, earlier)
    assert code == shards.PRE_STEP_REFUSED
    assert not seen
    assert "signal 9" in capfd.readouterr().err
    nothing_made(tmp_path)
