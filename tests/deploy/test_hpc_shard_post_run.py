"""O63: a shard step's post-run script (deploy/hpc-federation/shards.py): a script of the study, declared in the shard
plan, that the step runs after its dispatcher exits 0 and before it archives, while the federation is up, and whose
non-zero exit, signal or timeout leaves the shard's results archived but not final, which `collect` and the core's
merge refuse. The dispatcher is a stand-in; every path and name is invented, and nothing runs srun."""

import argparse
import hashlib
import importlib.util
import json
import os
import signal
import sys
import tarfile
import threading
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
POST = "guards/post_run.py"
PASS = "raise SystemExit(0)\n"
HOME_NAMES = {"HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "APPTAINER_CONFIGDIR",
              "APPTAINER_CACHEDIR", "UV_CACHE_DIR", "PIP_CACHE_DIR", "PYTHONNOUSERSITE", "GIT_CONFIG_GLOBAL",
              "GIT_CONFIG_NOSYSTEM", "TMPDIR"}  # fmt: skip
SPEC = {"index": 1, "count": 2, "label": "fed-a"}


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_post_run", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_post_run"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_post_run", None)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def make_study(tmp_path: Path, post: str | None = PASS, guard: str | None = None) -> Path:
    study = tmp_path / "study"
    (study / "guards").mkdir(parents=True, exist_ok=True)
    study.chmod(0o755)  # whatever the umask: a declared guard's root must not be group or other writable
    (study / "study.toml").write_text("[study]\n")
    (study / "campaign.toml").write_text("[scheduler]\nworkers = 1\n")
    for name, text in (("post_run.py", post), ("pre_step.py", guard)):
        if text is not None:
            (study / "guards" / name).write_text(text)
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir(exist_ok=True)
    return study


def step_args(tmp_path, post=PASS, *, index=1, label="fed-a", guard=None, timeout=20.0, resume=False, digest=None,
              path=POST):  # fmt: skip
    """The step's arguments as its generated line gives them; `post=None` declares no post-run script."""
    study = make_study(tmp_path, post, guard)
    declared = {}
    if post is not None:
        declared = {"post_run": path, "post_run_sha256": digest or sha(post), "post_run_timeout": timeout}
    if guard is not None:
        declared |= {"pre_step": "guards/pre_step.py", "pre_step_sha256": sha(guard), "pre_step_timeout": timeout}
    return argparse.Namespace(study=study, index=index, count=2, label=label, tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=resume, archive_left=False,
                              snapshot_every=3600.0, **declared)  # fmt: skip


def dispatcher(status=0, then=None):
    seen = {}

    def run(argv, env, cwd):
        seen.update(argv=list(argv), cwd=Path(cwd), owner=json.loads((Path(cwd) / "step.json").read_text()))
        out = Path(argv[argv.index("run") + 1]) / "out"
        seen["restored"] = sorted(p.name for p in out.glob("*")) if out.is_dir() else []
        (out / "runs").mkdir(parents=True, exist_ok=True)
        (out / "runs" / "r1.json").write_text("{}")
        (out / "campaign.lock").write_text("")
        if then is not None:
            then()
        return status

    return run, seen


def run_step(shards, args, **kwargs):
    run, seen = dispatcher(kwargs.pop("status", 0), kwargs.pop("then", None))
    code = shards.step(args, runner=kwargs.pop("runner", run), core_found=lambda path: True)
    return code, seen


def manifest_of(tmp_path, label="fed-a") -> dict:
    return json.loads((tmp_path / "results" / f"{label}.out.json").read_text())


def names_in(tmp_path, label="fed-a") -> set[str]:
    with tarfile.open(tmp_path / "results" / f"{label}.out.tar") as tar:
        return set(tar.getnames())


def alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0] != "Z"
    except OSError:
        return False


def nothing_made(tmp_path: Path) -> None:
    assert not list((tmp_path / "tmpfs").iterdir())
    assert not list((tmp_path / "results").iterdir())


# -- a post-run script that passes ----------------------------------------------------------------------------------


def test_o63_a_post_run_script_that_exits_0_makes_the_results_final_and_is_recorded(shards, tmp_path, monkeypatch):
    """O63: the script runs after the dispatcher and passes: the shard's archive says `final` and carries the
    script's path, SHA-256, that it ran and its exit status; the archive holds no not-final mark; the step ends 0."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    code, seen = run_step(shards, step_args(tmp_path))
    assert code == 0
    record = {"path": POST, "sha256": sha(PASS), "ran": True, "exit_status": 0, "final": True}
    manifest = manifest_of(tmp_path)
    assert (manifest["final"], manifest["post_run"]) == (True, record)
    assert "out/NOT-FINAL.json" not in names_in(tmp_path)
    assert {"path": POST, "sha256": sha(PASS), "ran": False, "exit_status": None, "final": False} == (
        seen["owner"]["post_run"]
    )  # what the step's record said while the dispatcher ran: declared, not yet run


def test_o63_the_script_runs_after_the_dispatcher_before_the_archive_isolated_in_the_copy_with_its_arguments(
    shards, tmp_path, monkeypatch
):
    """O63: it runs after the dispatcher has exited 0 (the dispatcher's out/ is there) and before the archive is
    written, as the same interpreter with `-I -B`, in the study copy's root on the node's tmpfs, given the shard's
    label, the copy's out/ and the dispatcher's exit status, in the dispatcher's own environment (the allowlist, the
    step's home and TMPDIR; no canary of the account's), and the step's record says its dispatcher ended and the
    script is not yet run."""
    seen_file = tmp_path / "seen.json"
    archive_path = str(tmp_path / "results" / "fed-a.out.tar")
    post = (
        "import json, os, sys\n"
        "seen = {'isolated': sys.flags.isolated, 'bytecode': sys.flags.dont_write_bytecode, 'exe': sys.executable,\n"
        "        'argv': sys.argv, 'cwd': os.path.realpath(os.getcwd()), 'env': dict(os.environ),\n"
        f"        'out': sorted(os.listdir(sys.argv[2])), 'archived': os.path.exists({archive_path!r}),\n"
        "        'record': json.load(open('../step.json'))}\n"
        f"json.dump(seen, open({str(seen_file)!r}, 'w'))\n"
    )
    for name, value in {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "EXPCORE_FED_ROOT_PARENT": "/fed",
                        "EXPCORE_FED_ROOT": "/fed/root", "EXPCORE_CANARY": "x", "HOME": "/home/canary",
                        "PYTHONPATH": "/canary", "ANTHROPIC_API_KEY": "canary"}.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    args = step_args(tmp_path, post)
    (args.study / "json.py").write_text("raise SystemExit(99)\n")  # what `-I` keeps out of an import
    assert run_step(shards, args)[0] == 0
    found = json.loads(seen_file.read_text())
    assert (found["isolated"], found["bytecode"], found["exe"]) == (1, 1, sys.executable)
    cwd = Path(found["cwd"])
    assert cwd.name == "study"
    assert cwd.parent.parent == (tmp_path / "tmpfs").resolve()  # <tmpfs>/expcore-shard-<label>.<random>/study
    assert (found["argv"][1], found["argv"][3]) == ("fed-a", "0")
    assert Path(found["argv"][2]).resolve() == cwd / "out"
    assert "campaign.lock" in found["out"]
    assert found["archived"] is False
    assert set(found["env"]) == {"PATH", "LC_ALL", "EXPCORE_FED_ROOT_PARENT", "EXPCORE_FED_ROOT"} | HOME_NAMES
    assert Path(found["env"]["HOME"]).parent == cwd.parent  # the step's own directory, never the account's
    assert found["env"]["TMPDIR"] == str(cwd.parent / "tmp")
    assert (found["record"]["phase"], found["record"]["exit_status"]) == ("archiving", 0)
    assert found["record"]["post_run"]["ran"] is False


def test_o63_the_script_that_runs_is_the_one_hashed_at_the_start_never_the_file_read_again(
    shards, tmp_path, monkeypatch
):
    """O63: a script file replaced while the dispatcher runs is never the one that runs: the step runs the bytes it
    hashed before it began."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    args = step_args(tmp_path, PASS)

    def swapped():
        (args.study / POST).write_text("raise SystemExit(9)\n")

    run, _ = dispatcher(then=swapped)
    assert shards.step(args, runner=run, core_found=lambda path: True) == 0
    assert manifest_of(tmp_path)["final"] is True


# -- a post-run script that does not pass ---------------------------------------------------------------------------


SIGKILL = "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"


@pytest.mark.parametrize(
    ("text", "reason", "exit_status"),
    [("raise SystemExit(3)\n", "exited with status 3", 3), (SIGKILL, "killed by signal 9", -9)],
)
def test_o63_a_post_run_script_that_fails_leaves_the_results_archived_and_not_final(
    shards, tmp_path, monkeypatch, capfd, text, reason, exit_status
):
    """O63: a non-zero exit or a signal: the results are archived whole all the same (nothing is lost), the manifest
    says `final: false` with the reason and the script's record, the archive holds NOT-FINAL.json, and the step ends
    with POST_RUN_FAILED, not the dispatcher's 0."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    text = "import sys\nsys.stderr.write('owner check: a space is not as it was\\n')\n" + text
    code, _ = run_step(shards, step_args(tmp_path, text))
    assert code == shards.POST_RUN_FAILED == 74
    manifest = manifest_of(tmp_path)
    assert manifest["final"] is False
    assert manifest["lock"] is True
    assert manifest["post_run"]["ran"] is True
    assert (manifest["post_run"]["exit_status"], manifest["post_run"]["final"]) == (exit_status, False)
    assert reason in manifest["post_run"]["reason"]
    assert {"out/NOT-FINAL.json", "out/runs/r1.json", "out/campaign.lock"} <= names_in(tmp_path)
    with tarfile.open(tmp_path / "results" / "fed-a.out.tar") as tar:
        mark = json.loads(tar.extractfile("out/NOT-FINAL.json").read())
    assert mark["final"] is False
    assert reason in mark["reason"]
    said = capfd.readouterr().err
    assert "owner check: a space is not as it was" in said  # the script's own words reach the step's stderr
    assert "NOT final" in said
    assert not list((tmp_path / "tmpfs").iterdir())


def test_o63_a_post_run_script_that_overruns_its_bound_is_killed_and_the_results_are_not_final(
    shards, tmp_path, monkeypatch
):
    """O63: past `--post-run-timeout` the script and everything it started are killed, in about that time, and the
    results are archived, not final, with the bound in the reason."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    pids = tmp_path / "pids"
    text = (
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(60)\n"
    )
    began = time.monotonic()
    code, _ = run_step(shards, step_args(tmp_path, text, timeout=1.0))
    assert time.monotonic() - began < 30
    assert code == shards.POST_RUN_FAILED
    post = manifest_of(tmp_path)["post_run"]
    assert (post["ran"], post["exit_status"], post["final"]) == (True, None, False)
    assert "did not end within 1 s" in post["reason"]
    assert "out/NOT-FINAL.json" in names_in(tmp_path)
    for pid in map(int, pids.read_text().split()):
        for _ in range(50):
            if not alive(pid):
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"process {pid} of the script still runs")


def test_o63_a_step_told_to_stop_during_its_post_run_script_archives_the_results_not_final(
    shards, tmp_path, monkeypatch
):
    """O63: SIGTERM to the step during the script kills the script's group; the results, which must not be lost, are
    archived, not final."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    earlier = signal.signal(signal.SIGTERM, lambda *_: None)  # a SIGTERM that arrives too early is not the test's end
    timer = threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGTERM))
    try:
        timer.start()
        code, _ = run_step(shards, step_args(tmp_path, "import time\ntime.sleep(60)\n"))
    finally:
        timer.cancel()
        signal.signal(signal.SIGTERM, earlier)
    assert code == shards.POST_RUN_FAILED
    assert "killed by signal 9" in manifest_of(tmp_path)["post_run"]["reason"]
    assert "out/NOT-FINAL.json" in names_in(tmp_path)


def test_o63_a_dispatcher_that_did_not_exit_0_archives_its_results_not_final_without_running_the_script(
    shards, tmp_path, monkeypatch
):
    """O63: the script runs only after a dispatcher that exited 0. After another status it is not run (a marker it
    would write is absent), the results are archived not final with that reason, and the step ends with the
    dispatcher's own status."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    marker = tmp_path / "ran"
    code, _ = run_step(shards, step_args(tmp_path, f"open({str(marker)!r}, 'w').close()\n"), status=3)
    assert code == 3
    assert not marker.exists()
    manifest = manifest_of(tmp_path)
    assert (manifest["exit_status"], manifest["final"]) == (3, False)
    assert manifest["post_run"]["ran"] is False
    assert "the dispatcher exited 3" in manifest["post_run"]["reason"]
    assert "out/NOT-FINAL.json" in names_in(tmp_path)


def test_o63_the_step_record_holds_the_scripts_result_in_a_kept_out_and_archive_left_carries_it(
    shards, tmp_path, monkeypatch
):
    """O63: an out/ that could not be archived stays on the node with the step's record, which holds the post-run
    script's record (that it ran, its hash, its exit, whether final); `--archive-left` archives it as the step would
    have, final only when the script passed, marking it not final otherwise."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    for text, final in ((PASS, True), ("raise SystemExit(4)\n", False)):
        args = step_args(tmp_path, text)
        run, _ = dispatcher()
        with monkeypatch.context() as patch:
            patch.setattr(shards, "archive", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left")))
            assert shards.step(args, runner=run, core_found=lambda path: True) == shards.ARCHIVE_FAILED
        (kept,) = (tmp_path / "tmpfs").glob("expcore-shard-fed-a.*")
        owner = json.loads((kept / "step.json").read_text())
        assert (owner["post_run"]["ran"], owner["post_run"]["final"]) == (True, final)
        (kept / "step.json").write_text(json.dumps({**owner, "start": "0"}))  # the step ran in this process: gone
        left = argparse.Namespace(**{**vars(args), "archive_left": True})
        assert shards.archive_left(left) == 0
        manifest = manifest_of(tmp_path)
        assert manifest["final"] is final
        assert manifest["post_run"]["sha256"] == sha(text)
        assert ("out/NOT-FINAL.json" in names_in(tmp_path)) is (not final)
        for published in (tmp_path / "results").glob("fed-a.out.*"):  # the next round starts afresh
            published.unlink()


def test_o63_archive_left_of_a_step_killed_before_its_script_passed_archives_not_final(shards, tmp_path, monkeypatch):
    """O63: a step that ended (its dispatcher's out/ whole) before its post-run script passed leaves a record that
    declares the script and says it has not passed: `--archive-left` archives that out/ not final, never final."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    args = step_args(tmp_path, PASS)
    kept = tmp_path / "tmpfs" / "expcore-shard-fed-a.killed"
    (kept / "study" / "out" / "runs").mkdir(parents=True)
    (kept / "study" / "out" / "campaign.lock").write_text("")
    declared = {"path": POST, "sha256": sha(PASS), "ran": False, "exit_status": None, "final": False}
    record = {"shard": SPEC, "pid": os.getpid(), "start": "0", "root": "/fed/root", "phase": "archiving",
              "results": str(tmp_path / "results"), "base": None, "exit_status": 0, "post_run": declared}  # fmt: skip
    (kept / "step.json").write_text(json.dumps(record))
    assert shards.archive_left(argparse.Namespace(**{**vars(args), "archive_left": True})) == 0
    manifest = manifest_of(tmp_path)
    assert manifest["final"] is False
    assert "ended before its post-run script passed" in manifest["post_run"]["reason"]
    assert "out/NOT-FINAL.json" in names_in(tmp_path)


# -- refused at the start, before anything is made ------------------------------------------------------------------


@pytest.mark.parametrize("what", ["hash", "missing", "file link", "directory link", "directory", "no hash", "bad hash",
                                  "bad bound", "huge bound", "absolute", "climbs"])  # fmt: skip
def test_o63_a_post_run_script_that_is_not_as_declared_refuses_the_step_before_the_dispatcher_with_nothing_made(
    shards, tmp_path, monkeypatch, capsys, what
):
    """O63: a script that is not the file the plan hashed, is missing, a link or a directory, has no or a bad hash or
    bound, or whose path is absolute or climbs: the step is refused with 73, naming the script, before the dispatcher
    and before anything is made, so a campaign is never spent on a check that cannot run."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    args = step_args(tmp_path, PASS)
    outside = tmp_path / "outside.py"
    outside.write_text(PASS)
    if what == "hash":
        args.post_run_sha256 = sha("another\n")
    elif what == "missing":
        (args.study / POST).unlink()
    elif what == "file link":
        (args.study / POST).unlink()
        (args.study / POST).symlink_to(outside)
    elif what == "directory link":
        (args.study / "guards").rename(tmp_path / "guards-real")
        (args.study / "guards").symlink_to(tmp_path / "guards-real")
    elif what == "directory":
        (args.study / POST).unlink()
        (args.study / POST).mkdir()
    elif what == "no hash":
        args.post_run_sha256 = None
    elif what == "bad hash":
        args.post_run_sha256 = "abc"
    elif what == "bad bound":
        args.post_run_timeout = 0.0
    elif what == "huge bound":
        args.post_run_timeout = 1801.0
    elif what == "absolute":
        args.post_run = str(outside)
    else:
        args.post_run = "../outside.py"
    code, seen = run_step(shards, args)
    assert code == shards.PRE_STEP_REFUSED
    assert not seen
    nothing_made(tmp_path)
    assert "post-run script" in capsys.readouterr().err


def test_o63_the_post_run_script_is_checked_before_the_guard_runs(shards, tmp_path, monkeypatch):
    """O63: both scripts are read and hashed first: a post-run script that is not the plan's stops the step before a
    guard that would write a marker runs."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    marker = tmp_path / "ran"
    args = step_args(tmp_path, PASS, guard=f"open({str(marker)!r}, 'w').close()\n")
    args.post_run_sha256 = sha("another\n")
    code, seen = run_step(shards, args)
    assert code == shards.PRE_STEP_REFUSED
    assert not seen
    assert not marker.exists()
    nothing_made(tmp_path)


def test_o63_a_guard_and_a_post_run_script_each_run_in_their_turn_and_are_each_recorded(shards, tmp_path, monkeypatch):
    """O63: with both declared, the guard passes first, the dispatcher runs, then the post-run script; the manifest
    holds both records."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    code, seen = run_step(shards, step_args(tmp_path, PASS, guard=PASS))
    assert code == 0
    assert seen["owner"]["pre_step"]["exit_status"] == 0
    manifest = manifest_of(tmp_path)
    assert (manifest["pre_step"]["sha256"], manifest["final"]) == (sha(PASS), True)


def test_o63_a_step_with_no_post_run_script_declared_runs_as_before_and_records_none(shards, tmp_path, monkeypatch):
    """O63: no script declared: the step's record, its manifest and its archive hold no `post_run`, `final` or
    NOT-FINAL.json, and a dispatcher's non-zero exit ends the step with that status as ever."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    args = step_args(tmp_path, None)
    assert not any(name.startswith("post_run") for name in vars(args))
    code, seen = run_step(shards, args, status=3)
    assert code == 3
    assert "post_run" not in seen["owner"]
    manifest = manifest_of(tmp_path)
    assert not {"post_run", "final"} & set(manifest)
    assert "out/NOT-FINAL.json" not in names_in(tmp_path)


def test_o63_a_resume_of_a_not_final_archive_starts_from_it_without_the_mark_and_decides_afresh(
    shards, tmp_path, monkeypatch
):
    """O63: a shard archived not final is resumed like any other: its archive restores (the verified check does not
    refuse it) without NOT-FINAL.json in the copy the dispatcher sees; a script that passes this time makes the new
    archive final, with no mark."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    assert run_step(shards, step_args(tmp_path, "raise SystemExit(1)\n"))[0] == shards.POST_RUN_FAILED
    assert manifest_of(tmp_path)["final"] is False
    code, seen = run_step(shards, step_args(tmp_path, PASS, resume=True))
    assert code == 0
    assert "runs" in seen["restored"]
    assert "NOT-FINAL.json" not in seen["restored"]
    assert manifest_of(tmp_path)["final"] is True
    assert "out/NOT-FINAL.json" not in names_in(tmp_path)


# -- the shard plan: declaring and pinning it -----------------------------------------------------------------------


def manifest_file(tmp_path: Path, extra: str = "", post: str | None = PASS, guard: str | None = None) -> Path:
    """A shard plan of two federations; `extra` (top-level keys) goes before the `[[shards]]`."""
    make_study(tmp_path, post, guard)
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


def printed(shards, tmp_path, capsys, command, extra="", post=PASS, guard=None) -> list[str]:
    assert shards.main([command, "--manifest", str(manifest_file(tmp_path, extra, post, guard))]) == 0
    return capsys.readouterr().out.splitlines()


def test_o63_a_declared_post_run_script_is_pinned_in_the_plan_and_every_line_carries_it(shards, tmp_path, capsys):
    """O63: `plan` records the script's path, SHA-256 and bound; each line ends in the three post-run words after
    the guard's, and is otherwise the line with none declared."""
    base = printed(shards, tmp_path / "plain", capsys, "lines", post=None)
    declared = 'post_run = "guards/post_run.py"\npost_run_timeout_s = 90'
    plans = [json.loads(line) for line in printed(shards, tmp_path / "pinned", capsys, "plan", declared)]
    assert [p["post_run"] for p in plans] == [{"path": POST, "sha256": sha(PASS), "timeout_s": 90.0}] * 2
    assert all("pre_step" not in p for p in plans)
    words = printed(shards, tmp_path / "pinned", capsys, "lines", declared)
    tail = f" --post-run {POST} --post-run-sha256 {sha(PASS)} --post-run-timeout 90"
    for line, plain in zip(words, base, strict=True):
        assert line.endswith(tail)
        assert line[: -len(tail)].replace(str(tmp_path / "pinned"), "ROOT").split() == (
            plain.replace(str(tmp_path / "plain"), "ROOT").split()
        )


def test_o63_with_both_declared_the_line_carries_the_guard_then_the_post_run_script(shards, tmp_path, capsys):
    """O63: the guard's words, then the post-run words, each with its own hash and bound; the default bounds are 60 s
    and 300 s."""
    declared = 'pre_step = "guards/pre_step.py"\npost_run = "guards/post_run.py"'
    line = printed(shards, tmp_path, capsys, "lines", declared, guard="raise SystemExit(0)  # guard\n")[0]
    tail = line.split(" --pre-step ")[1]
    guard_hash = sha("raise SystemExit(0)  # guard\n")
    assert tail == (
        f"guards/pre_step.py --pre-step-sha256 {guard_hash} --pre-step-timeout 60 "
        f"--post-run {POST} --post-run-sha256 {sha(PASS)} --post-run-timeout 300"
    )


@pytest.mark.parametrize("value", ['"/abs/post_run.py"', '"../outside.py"', '"guards/../post_run.py"',
                                   '"./post_run.py"', '"guards/"', '""', "3", "true"])  # fmt: skip
def test_o63_a_post_run_path_that_leaves_the_study_or_is_not_plain_refuses_the_plan(shards, tmp_path, capsys, value):
    """O63: the same path rules as the guard's, at `plan` and `lines`, which print nothing."""
    path = manifest_file(tmp_path, f"post_run = {value}")
    for command in ("plan", "lines"):
        assert shards.main([command, "--manifest", str(path)]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "post_run" in captured.err


@pytest.mark.parametrize("what", ["file link", "directory link", "directory", "missing", "huge"])
def test_o63_a_post_run_script_that_is_a_link_or_no_file_refuses_the_plan(shards, tmp_path, capsys, what):
    """O63: a script that is a link (itself or on the way), a directory, missing or over 1 MiB refuses `plan` and
    `lines` with nothing printed, naming it."""
    path = manifest_file(tmp_path, f'post_run = "{POST}"')
    study = tmp_path / "study"
    (study / POST).unlink()
    if what == "file link":
        (study / "inside.py").write_text(PASS)
        (study / POST).symlink_to(study / "inside.py")
    elif what == "directory link":
        (study / "guards").rename(tmp_path / "guards-real")
        (study / "guards").symlink_to(tmp_path / "guards-real")
    elif what == "directory":
        (study / POST).mkdir()
    elif what == "huge":
        (study / POST).write_bytes(b"#" * ((1 << 20) + 1))
    for command in ("plan", "lines"):
        assert shards.main([command, "--manifest", str(path)]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert POST in captured.err


@pytest.mark.parametrize("extra", ["post_run_timeout_s = 30", f'post_run = "{POST}"\npost_run_timeout_s = 0',
                                   f'post_run = "{POST}"\npost_run_timeout_s = 1801',
                                   f'post_run = "{POST}"\npost_run_timeout_s = "9"'])  # fmt: skip
def test_o63_a_post_run_bound_without_a_script_or_out_of_range_refuses_the_manifest(shards, tmp_path, extra):
    """O63: `post_run_timeout_s` goes with `post_run` and is a number of seconds above 0 and at most 1800."""
    with pytest.raises(shards.ShardsError, match="post_run_timeout_s"):
        shards.load_plan(manifest_file(tmp_path, extra))


def test_o63_no_line_is_made_from_a_plan_that_has_not_pinned_its_post_run_script(shards, tmp_path):
    """O63: `step_argv` refuses a plan whose post-run script was not hashed (`pinned`)."""
    plan = shards.load_plan(manifest_file(tmp_path, f'post_run = "{POST}"'))
    with pytest.raises(shards.ShardsError, match="not pinned"):
        shards.step_argv(plan, plan.shards[0], shards.federation(plan.shards[0]), SCRIPT)


# -- collect and the merge refuse a shard that is not final ---------------------------------------------------------


def two_shards(shards, tmp_path, monkeypatch, texts, extra=f'post_run = "{POST}"', declared=(True, True)):
    """Run both shards' steps with the post-run script `texts[i]` each (None: its line carries no script, as one edited
    by hand), against a plan that declares `extra`; the plan."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    plan = shards.load_plan(manifest_file(tmp_path, extra))
    pinned = shards.pinned(plan)
    for index, (label, text, kept) in enumerate(zip(("fed-a", "fed-b"), texts, declared, strict=True), 1):
        args = step_args(tmp_path, text if text is not None else PASS, index=index, label=label)
        if not kept:
            for key in ("post_run", "post_run_sha256", "post_run_timeout"):
                delattr(args, key)
        run, _ = dispatcher()
        shards.step(args, runner=run, core_found=lambda path: True)
    assert (tmp_path / "study" / POST).read_text() == PASS  # what the plan hashed is what the study holds
    return pinned


def test_o63_collect_unpacks_shards_that_are_final_and_names_the_merge(shards, tmp_path, monkeypatch):
    """O63: every shard final, each having run the plan's script: collected as ever."""
    plan = two_shards(shards, tmp_path, monkeypatch, [PASS, PASS])
    outs = shards.collect(plan, tmp_path / "into")
    assert [o.parent.name for o in outs] == ["fed-a", "fed-b"]
    assert not any((o / "NOT-FINAL.json").exists() for o in outs)


def test_o63_collect_refuses_a_shard_that_is_not_final_naming_it_and_leaves_nothing_behind(
    shards, tmp_path, monkeypatch
):
    """O63: one shard's script did not pass: `collect` refuses the whole, naming that shard (its label) and the
    reason, and the directory to collect into is not left behind."""
    plan = two_shards(shards, tmp_path, monkeypatch, [PASS, "raise SystemExit(6)\n"])
    with pytest.raises(shards.ShardsError, match=r"shard fed-b: its results are not final .*exited with status 6"):
        shards.collect(plan, tmp_path / "into")
    assert not (tmp_path / "into").exists()


def test_o63_collect_refuses_a_not_final_shard_of_a_plan_that_declares_no_script(shards, tmp_path, monkeypatch):
    """O63: the manifest's `final: false` stands whatever the plan says: a plan with no post-run script still refuses
    a shard whose step ran one that did not pass."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    plan = shards.load_plan(manifest_file(tmp_path))
    for index, (label, text) in enumerate((("fed-a", PASS), ("fed-b", "raise SystemExit(1)\n")), 1):
        run, _ = dispatcher()
        shards.step(step_args(tmp_path, text, index=index, label=label), runner=run, core_found=lambda path: True)
    with pytest.raises(shards.ShardsError, match="shard fed-b: its results are not final"):
        shards.collect(plan, tmp_path / "into")


def test_o63_collect_refuses_a_shard_whose_step_did_not_run_the_plans_script_to_a_pass(shards, tmp_path, monkeypatch):
    """O63: a plan that declares the script refuses a shard whose manifest does not say it passed: a step whose line
    dropped the three words (a hand-edited line), or one that ran another script than the plan's."""
    plan = two_shards(shards, tmp_path, monkeypatch, [PASS, PASS], declared=(True, False))
    with pytest.raises(shards.ShardsError, match="shard fed-b: its step did not run the plan's post-run script"):
        shards.collect(plan, tmp_path / "into")
    assert not (tmp_path / "into").exists()


def test_o63_collect_refuses_an_unpacked_out_that_holds_the_mark(shards, tmp_path, monkeypatch):
    """O63: an archive whose manifest says final but whose out/ holds NOT-FINAL.json (edited by hand) is refused once
    unpacked, naming the shard, so the merge never sees it."""
    plan = two_shards(shards, tmp_path, monkeypatch, [PASS, PASS])
    work = tmp_path / "forge"
    work.mkdir()
    with tarfile.open(tmp_path / "results" / "fed-b.out.tar") as source:
        source.extractall(work, filter="data")
    (work / "out" / "NOT-FINAL.json").write_text("{}")
    tar_path = tmp_path / "results" / "fed-b.out.tar"
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tar:
        tar.add(work / "out", arcname="out")
    manifest = manifest_of(tmp_path, "fed-b")
    manifest["sha256"] = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    (tmp_path / "results" / "fed-b.out.json").write_text(json.dumps(manifest))
    with pytest.raises(shards.ShardsError, match="shard fed-b: its out/ is marked not final"):
        shards.collect(plan, tmp_path / "into")


def test_o63_the_constants_the_driver_and_the_core_share_are_the_same(shards):
    """O63: the not-final file's name in the driver (a stand-alone file, on the node) is the core's, which the merge
    refuses; the driver's statuses for a refused and a not-final step are apart from its others."""
    from expcore.campaign.shard import NOT_FINAL_FILE

    assert shards.NOT_FINAL == NOT_FINAL_FILE
    statuses = [shards.CORE_MISMATCH, shards.ARCHIVE_FAILED, shards.NOT_TIED, shards.PRE_STEP_REFUSED,
                shards.POST_RUN_FAILED]  # fmt: skip
    assert len(set(statuses)) == len(statuses)
