"""The HPC test runner end to end (deploy/hpc-tests), with the cluster faked: `ssh` runs the remote command in a local
shell, `srun` drops its options and runs the step's command (behind the real `/usr/bin/env -i`), Slurm answers from
files, and `uv` builds a small venv whose pytest is a stand-in. The boot, node.sh, the payload's and the results'
SHA-256, the self-check, the clean and the local report are the real ones, on a real tmpfs (the node root).
"""

import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import hpct_fakes as fakes
import pytest


def tmpfs_base():
    for base in ("/tmp", "/dev/shm"):
        probe = Path(base)
        if probe.is_dir() and os.access(base, os.W_OK):
            kind = subprocess.run(["stat", "-f", "-c", "%T", base], capture_output=True, text=True, check=False)
            if kind.stdout.strip() == "tmpfs":
                return base
    return None


BASE = tmpfs_base()
pytestmark = pytest.mark.skipif(BASE is None, reason="needs a writable tmpfs at /tmp or /dev/shm for the node root")


@pytest.fixture(scope="module")
def cli():
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(fakes.DEPLOY))
        import hpctestlib
        import hpctests

        yield SimpleNamespace(lib=hpctestlib, main=hpctests.main, module=hpctests)


def commit(repo: Path, files: dict) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-q", "-m", "x"],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    bin_dir, fake, home, repo = (tmp_path / n for n in ("bin", "fake", "home", "repo"))
    for path in (bin_dir, fake, home, repo):
        path.mkdir()
    fakes.slurm(bin_dir, fake)
    fakes.remote(bin_dir, fake)
    uv = fakes.uv(tmp_path / "uv", fake / "uv.jsonl")
    root = f"{BASE}/hpct-test-{secrets.token_hex(4)}"
    site = tmp_path / "site.env"
    site.write_text(
        f"HPCT_HOST=fake-login\nHPCT_NODE_ROOT={root}\nHPCT_UV={uv}\nHPCT_UV_VERSION=0.11.19\n"
        f"HPCT_OUT={tmp_path / 'out'}\n"
        f"HPCT_WORK={tmp_path}\nHPCT_STEP_PATH=/usr/bin:/bin:/opt/site-only/bin\nHPCT_ACCOUNT=acct-x\nHPCT_PARTITION=std-part\n"
    )
    site.chmod(0o600)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    sha = commit(repo, {"src/expcore/__init__.py": "", "src/expcore_onedata/__init__.py": "", "tests/test_x.py": ""})
    for key, value in fakes.environment(bin_dir, fake, home).items():
        monkeypatch.setenv(key, value)
    # What a login shell holds and a step must never see.
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
    monkeypatch.setenv("SLURM_JOB_ID", "1")
    monkeypatch.setenv("HPCT_SITE_SECRET", "x")
    node_root = Path(f"{root}-{os.getuid()}")
    yield SimpleNamespace(site=site, fake=fake, repo=repo, sha=sha, out=tmp_path / "out", node_root=node_root)
    shutil.rmtree(node_root, ignore_errors=True)


def allocate(cli, world):
    assert cli.main(["--site", str(world.site), "install"]) == 0
    assert cli.main(["--site", str(world.site), "submit"]) == 0
    now = int(time.time())
    start, end = (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) for t in (now - 60, now + 43200))
    (world.fake / "squeue").write_text(f"700|expcore-tests|RUNNING|{start}|{end}|16|30G\n")


def run(cli, world, *pytest_args):
    return cli.main(
        [
            "--site",
            str(world.site),
            "run",
            "--repo",
            str(world.repo),
            "--cores",
            "2",
            "--mem",
            "1G",
            "--time",
            "10",
            "--",
            *pytest_args,
        ]
    )


def only_run(world) -> Path:
    (out,) = world.out.iterdir()
    return out


def test_o64_a_selection_runs_at_the_commit_in_an_exact_step_and_its_results_come_back(cli, world, capsys):
    """O64: `run` ships the commit, runs pytest in an `--exact` step of our allocation behind the allowlist, brings
    back pytest's exit status, summary, `-ra` lines and JUnit XML, checked by SHA-256, and leaves no run directory on
    the node; nothing of the login shell's environment reaches the tests."""
    allocate(cli, world)
    assert run(cli, world, "tests", "-k", "not slow and x") == 0
    printed = capsys.readouterr().out
    assert "2 passed, 1 skipped in 0.50s" in printed
    assert "SKIPPED [1] tests/test_x.py:3: a stand-in skip" in printed
    assert "fake-login" not in printed
    out = only_run(world)
    assert (out / "results" / "junit.xml").exists()
    log = (out / "results" / "pytest.log").read_text()
    keys = set(log.split("env-keys: ", 1)[1].split("\n", 1)[0].split(","))
    assert not keys & {"SSH_AUTH_SOCK", "SLURM_JOB_ID", "HPCT_SITE_SECRET", "FAKE", "TZ"}
    assert {"PATH", "HOME", "TMPDIR"} <= keys
    assert '"tests", "-k", "not slow and x"' in log
    srun = (world.fake / "srun.argv").read_text().split("--\n")[0].splitlines()
    assert srun[:4] == ["--jobid=700", "--exact", "-N1", "-n1"]
    assert "--overlap" not in srun
    assert not (world.node_root / "runs").exists() or not any((world.node_root / "runs").iterdir())


def test_a_failing_selection_returns_pytest_s_own_status_and_names_the_failures(cli, world, capsys):
    allocate(cli, world)
    commit(world.repo, {"fake-outcome-fail": ""})
    assert run(cli, world, "tests") == 1
    printed = capsys.readouterr().out
    assert "FAILED tests/test_x.py::test_b - assert 0" in printed
    assert '"rc": 1' in (only_run(world) / "run.json").read_text()


def test_o64_a_venv_that_does_not_import_the_shipped_core_runs_no_test(cli, world, capsys):
    """O64: the self-check comes first: when the venv's isolated interpreter does not import the core from the
    shipped clone's src/, no test runs and the run exits 97."""
    allocate(cli, world)
    commit(world.repo, {"fake-wrong-core": ""})
    assert run(cli, world, "tests") == 97
    assert "SELF-CHECK FAILED" in capsys.readouterr().out
    assert not (only_run(world) / "results" / "pytest.log").exists()


def test_an_srun_client_that_does_not_return_after_its_step_is_ended_and_the_results_kept(cli, world, monkeypatch):
    allocate(cli, world)
    (world.fake / "hang").write_text("")
    monkeypatch.setattr(cli.module, "GRACE_S", 1.0)
    started = time.monotonic()
    assert run(cli, world, "tests") == 0
    assert time.monotonic() - started < 60


def test_only_committed_work_ships(cli, world, capsys):
    allocate(cli, world)
    (world.repo / "fake-outcome-fail").write_text("")  # untracked, not committed: not shipped
    (world.repo / "tests" / "test_x.py").write_text("changed\n")
    assert run(cli, world, "tests") == 0
    assert "uncommitted changes" in capsys.readouterr().out


def test_no_running_allocation_of_ours_is_exit_69_and_no_step(cli, world):
    assert cli.main(["--site", str(world.site), "install"]) == 0
    assert run(cli, world, "tests") == 69
    assert not (world.fake / "srun.argv").exists()


def test_a_site_env_others_can_read_is_refused(cli, world):
    world.site.chmod(0o644)
    assert cli.main(["--site", str(world.site), "jobs"]) == 64


def test_clean_removes_a_dead_run_s_directory_and_keeps_young_ones(cli, world, capsys):
    allocate(cli, world)
    runs = world.node_root / "runs"
    runs.mkdir(parents=True)
    dead, young = runs / "dead-run", runs / "young-run"
    for path in (dead, young):
        (path / "repo").mkdir(parents=True)
        (path / "pid").write_text("999999999\n")
    (dead / "repo").chmod(0o500)  # a tree a test left read-only
    old = time.time() - 600
    os.utime(dead, (old, old))
    assert cli.main(["--site", str(world.site), "clean", "--stale", "5"]) == 0
    assert "removed 1 run directories, kept 1 younger ones" in capsys.readouterr().out
    assert not dead.exists()
    assert young.exists()


def test_the_payload_must_arrive_intact(cli, world):
    root = f"{BASE}/hpct-test-{secrets.token_hex(4)}"
    command = cli.lib.boot_command(root, "r1", "0" * 64, "run")
    env = {"PATH": "/usr/bin:/bin"}
    try:
        out = subprocess.run(
            list(command), input=b"not the payload", capture_output=True, env=env, timeout=60, check=False
        )
        assert out.returncode == 72
        assert b"HPCT-FAIL boot: the payload arrived as sha256" in out.stdout
        assert not (Path(f"{root}-{os.getuid()}") / "runs" / "r1").exists()
    finally:
        shutil.rmtree(f"{root}-{os.getuid()}", ignore_errors=True)


def test_o64_the_boot_refuses_a_root_whose_filesystem_is_not_a_tmpfs_and_creates_nothing(cli):
    """O64: a step writes only to the node's tmpfs: a root under a /tmp that is not a tmpfs (on a site where /tmp is a
    disk) is refused before anything is made there."""
    if (
        subprocess.run(["stat", "-f", "-c", "%T", "/tmp"], capture_output=True, text=True, check=False).stdout.strip()
        == "tmpfs"
    ):
        pytest.skip("/tmp is a tmpfs here")
    root = f"/tmp/hpct-test-{secrets.token_hex(4)}"
    command = cli.lib.boot_command(root, "r1", "0" * 64, "run")
    out = subprocess.run(
        list(command), input=b"", capture_output=True, env={"PATH": "/usr/bin:/bin"}, timeout=60, check=False
    )
    assert b"HPCT-FAIL boot: the node root is not on a tmpfs" in out.stdout
    assert not Path(f"{root}-{os.getuid()}").exists()


# Every place a program of a step would otherwise read or write under the account's home, and where the step sends it.
UNDER_ROOT = ("HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_BIN_HOME",
              "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON_BIN_DIR", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR",
              "PIP_CACHE_DIR", "PYTHONUSERBASE", "APPTAINER_CACHEDIR", "APPTAINER_CONFIGDIR", "SINGULARITY_CACHEDIR",
              "SINGULARITY_CONFIGDIR")  # fmt: skip
TO_NULL = ("PIP_CONFIG_FILE", "GIT_CONFIG_GLOBAL")
SET_ONE = ("GIT_CONFIG_NOSYSTEM", "PYTHONNOUSERSITE")


def confined(env: dict, root: str, home: str) -> None:
    for key in UNDER_ROOT:
        assert env.get(key, "").startswith(root), (key, env.get(key))
    for key in TO_NULL:
        assert env.get(key) == "/dev/null", key
    for key in SET_ONE:
        assert env.get(key) == "1", key
    assert not [k for k, v in env.items() if home in v], "a value names the account's home"


def test_o64_a_step_reads_and_writes_nothing_of_the_account_s_home(cli, world):
    """O64: the account's home is shared by every agent of the account, and with HOME unset uv falls back to it (found
    live): in every uv call of a step and in pytest's environment, uv's cache, Python, Python links, tools and config,
    pip's cache and config, git's global and system config, Python's user site and Apptainer's cache go inside the
    run directory or the node root, or to /dev/null, no value names the account's home, and the Python is installed
    without a link in any bin directory (so neither ~/.local/share/uv/python nor ~/.local/bin is ever written)."""
    allocate(cli, world)
    assert run(cli, world, "tests") == 0
    root, home = f"{world.node_root}/", os.environ["HOME"]
    calls = fakes.calls(world.fake / "uv.jsonl")
    assert "--no-bin" in next(c for c in calls if c["args"][:2] == ["python", "install"])["args"]
    for call in calls:
        confined(call["env"], root, home)
        assert call["env"]["UV_NO_CONFIG"] == "1"
    log = (only_run(world) / "results" / "pytest.log").read_text()
    env = json.loads(log.split("env-json: ", 1)[1].split("\n", 1)[0])
    confined(env, root, home)
    assert env["HOME"].startswith(f"{root}runs/")


def test_o64_the_site_s_step_path_reaches_the_tests_through_the_payload_not_the_command_line(cli, world):
    """O64: HPCT_STEP_PATH is the tests' PATH on the node, carried in the payload; the step's command line has the
    boot's fixed PATH only."""
    allocate(cli, world)
    assert run(cli, world, "tests") == 0
    command = (world.fake / "srun.command").read_text().splitlines()
    assert command[:3] == ["/usr/bin/env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin"]
    assert "site-only" not in (world.fake / "srun.argv").read_text() + "\n".join(command)
    log = (only_run(world) / "results" / "pytest.log").read_text()
    assert "args: " in log


def test_o64_a_run_whose_pytest_exits_0_without_running_a_test_is_no_pass(cli, world, capsys):
    """O64: a zero from pytest counts as a pass only when its JUnit XML records a test; otherwise the run exits 5."""
    allocate(cli, world)
    commit(world.repo, {"fake-outcome-none": ""})
    assert run(cli, world, "tests") == 5
    assert "no test ran, which is not a pass" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [["--basetemp=/shared/x"], ["--junitxml", "/shared/j.xml"], ["-o", "cache_dir=/x"], ["-ocache_dir=/x"],
     ["--collect-only"], ["--co"], ["--version"], ["--log-file=/x"], ["-c", "/x.ini"]],
)  # fmt: skip
def test_o64_a_pytest_option_that_writes_elsewhere_or_runs_nothing_is_refused_before_any_step(cli, world, args):
    """O64: options that write outside the run (basetemp, JUnit, logs, ini overrides) or run no test are refused
    locally, before a step starts."""
    allocate(cli, world)
    assert run(cli, world, "tests", *args) == 64
    assert not (world.fake / "srun.argv").exists()


def test_the_boot_refuses_a_runs_directory_that_is_a_link(cli, world, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    world.node_root.mkdir(mode=0o700)
    (world.node_root / "runs").symlink_to(elsewhere)
    allocate(cli, world)
    assert run(cli, world, "tests") == 72
    assert list(elsewhere.iterdir()) == []
