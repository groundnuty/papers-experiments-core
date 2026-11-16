"""The HPC bundle's tools on a step's PATH (deploy/hpc-federation/bundle/lib.sh, ports.sh, node-init.sh): a step's
PATH is the driver's allowlist alone, and the first live zone step found no `ss` there. A script checks its tools up
front and dies naming each missing one, and every reading of the node's listeners fails when `ss` is missing or
fails: never an empty listing read as a node with no listener (live: a check whose `ss` was not on PATH counted no
listener, a false pass). The scripts run in plain bash on this host, over a PATH of stand-ins that lacks `ss`, or
whose `ss` fails, or prints listeners as the real one does."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "bundle"
# What sourcing lib.sh and the port functions call, linked from this host; never ss.
TOOLS = ("dirname", "awk", "sort", "grep", "od", "tr", "head", "cat", "sed", "cut", "id", "hostname", "mktemp", "stat")
# One line as `ss -Hlntuep` prints it: a TCP listener on 9100, of uid 1000.
LISTENER = 'tcp LISTEN 0 4096 127.0.0.1:9100 0.0.0.0:* users:(("beam.smp",pid=7,fd=9)) uid:1000 ino:1 sk:1\n'


def bin_dir(where: Path, ss: str | None) -> Path:
    """A PATH directory of this host's tools, linked, with `ss` absent (None) or a stand-in script."""
    where.mkdir()
    for tool in TOOLS:
        found = shutil.which(tool)
        assert found, f"this host has no {tool}"
        (where / tool).symlink_to(found)
    if ss is not None:
        (where / "ss").write_text(f"#!{shutil.which('bash')}\n{ss}\n", encoding="utf-8")
        (where / "ss").chmod(0o755)
    return where


def run(path: Path, tmp: Path, snippet: str) -> subprocess.CompletedProcess:
    """`snippet` in a fresh bash that sources lib.sh, with PATH the directory alone and nothing else of this host's."""
    env = {"PATH": str(path), "TMPDIR": str(tmp), "LC_ALL": "C"}
    script = f'source "{BUNDLE}/lib.sh"; {snippet}'
    return subprocess.run(
        [shutil.which("bash"), "-c", script], capture_output=True, text=True, timeout=60, check=False, env=env
    )


@pytest.fixture
def no_ss(tmp_path):
    return bin_dir(tmp_path / "bin", None)


@pytest.fixture
def failing_ss(tmp_path):
    return bin_dir(tmp_path / "bin", "echo 'ss: cannot open netlink socket' >&2; exit 1")


@pytest.fixture
def working_ss(tmp_path):
    return bin_dir(tmp_path / "bin", f"printf '%s' '{LISTENER}'")


def test_need_tools_dies_naming_every_missing_tool_and_the_setting_that_holds_them(tmp_path, no_ss):
    """O57: a script's up-front check names every missing tool and HPC_STEP_PATH, and passes when all are there."""
    done = run(no_ss, tmp_path, "need_tools awk ss no-such-tool-here; echo reached")
    assert done.returncode == 1
    assert "reached" not in done.stdout
    assert re.search(r"hpcop: ERROR: not found on PATH \(.*\): ss no-such-tool-here; HPC_STEP_PATH", done.stderr)
    assert run(no_ss, tmp_path, "need_tools awk sort; echo reached").stdout == "reached\n"


@pytest.mark.parametrize("ss", ["no_ss", "failing_ss"])
def test_no_reading_of_the_listeners_is_empty_when_ss_is_missing_or_fails(tmp_path, ss, request):
    """O57: listeners and used_ports fail, as `out=$(...) || ...` sees them, instead of printing nothing."""
    path = request.getfixturevalue(ss)
    for function in ("listeners", "used_ports"):
        done = run(path, tmp_path, f"if out=$({function}); then echo 'read: [$out]'; else echo failed; fi")
        assert done.stdout == "failed\n", function
        assert "ss" in done.stderr


@pytest.mark.parametrize("ss", ["no_ss", "failing_ss"])
def test_the_port_check_before_a_start_fails_closed_when_ss_is_missing_or_fails(tmp_path, ss, request):
    """O57: ports_free never says the ports are free from a listing it could not read."""
    done = run(request.getfixturevalue(ss), tmp_path, "ports_free oz; echo reached")
    assert done.returncode == 1
    assert "ok: ports free" not in done.stderr
    assert "reached" not in done.stdout


@pytest.mark.parametrize("ss", ["no_ss", "failing_ss"])
def test_no_epmd_port_is_picked_blind_when_ss_is_missing_or_fails(tmp_path, ss, request):
    """O57: configure.sh's `EPMD=$(pick_epmd_port)` (a command substitution clears -e) stops the script instead of
    going on with a port picked from no listing."""
    done = run(request.getfixturevalue(ss), tmp_path, 'EPMD=$(pick_epmd_port); echo "picked $EPMD"')
    assert done.returncode == 1
    assert "picked" not in done.stdout


@pytest.mark.parametrize("ss", ["no_ss", "failing_ss"])
def test_the_live_listener_check_is_a_fail_when_ss_is_missing_or_fails(tmp_path, ss, request):
    """O57: check-defaults.sh --live's listener check reports a FAIL, never a node with no listener to check."""
    done = run(request.getfixturevalue(ss), tmp_path, "live_listeners_ok oz 1000 21300 && echo passed || echo failed")
    assert done.stdout == "failed\n"
    assert "FAIL: this node's listeners could not be listed (ss missing or failing)" in done.stderr


def test_a_listing_that_ss_gave_is_read_as_before(tmp_path, working_ss):
    """O57: with a working ss the readings are the listing's: its port is used, and the start's port check passes."""
    assert run(working_ss, tmp_path, "used_ports").stdout == "9100\n"
    done = run(working_ss, tmp_path, "ports_free oz && echo passed")
    assert done.stdout == "passed\n"
    assert "ok: ports free" in done.stderr


def test_o57_home_in_points_every_home_a_tool_uses_into_the_work_directory_made_private(tmp_path):
    """O57: a service's scripts never fall back to the account's home: home_in makes `<work>/home` and the directories
    under it (0700) and exports HOME, the XDG base directories, Apptainer's configuration and cache, uv's and pip's
    caches there, Python's user site off and no git configuration, for every child (apptainer among them)."""
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    names = ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "APPTAINER_CONFIGDIR",
             "APPTAINER_CACHEDIR", "UV_CACHE_DIR", "PIP_CACHE_DIR", "PYTHONNOUSERSITE", "GIT_CONFIG_GLOBAL",
             "GIT_CONFIG_NOSYSTEM")  # fmt: skip
    shown = " ".join(f'"{name}=${{{name}}}"' for name in names)
    linked = bin_dir(tmp_path / "more", None)
    (linked / "install").symlink_to(shutil.which("install"))
    (linked / "env").symlink_to(shutil.which("env"))
    done = run(linked, tmp_path, f'home_in "{work}"; env -i {shown} env')  # what a child of the script sees
    assert done.returncode == 0, done.stderr
    env = dict(line.split("=", 1) for line in done.stdout.splitlines())
    home = work / "home"
    assert env["HOME"] == str(home)
    for name in names[1:9]:
        assert env[name].startswith(f"{home}/"), name
    assert (env["PYTHONNOUSERSITE"], env["GIT_CONFIG_GLOBAL"], env["GIT_CONFIG_NOSYSTEM"]) == ("1", "/dev/null", "1")
    for name in names[:7]:
        path = Path(env[name])
        assert path.is_dir(), name
        assert not path.is_symlink(), name
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, name


def test_o57_home_at_points_the_home_into_the_work_directory_and_makes_nothing(tmp_path):
    """O57, O58: a step that writes nothing (status.sh) points the same variables into the work directory and makes
    nothing there: the directory is ours and 0700, so no one else can make its home."""
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    shown = 'printf "%s %s\\n" "$HOME" "$APPTAINER_CACHEDIR"'
    done = run(bin_dir(tmp_path / "bin", None), tmp_path, f'home_at "{work}"; {shown}')
    assert done.stdout == f"{work}/home {work}/home/.apptainer/cache\n"
    assert list(work.iterdir()) == []


# Every script the driver runs as a step, or as the service step's own body, that knows its work directory.
STEP_SCRIPTS = ("zone.sbatch", "provider.sbatch", "teardown.sh", "status.sh", "exec.sh", "restart.sh", "zone-export.sh",
                "mint-registration-token.sh", "deliver-token.sh")  # fmt: skip
# A command that may read HOME: Apptainer, curl (its ~/.curlrc, through lib.sh's http), Python, a child script.
READS_HOME = re.compile(r'apptainer |curl |\bhttp |\bcb_status |\brun_service |"\$HPCOP_PY"|bash "\$(B|HPCOP_BUNDLE)/')


@pytest.mark.parametrize("script", STEP_SCRIPTS)
def test_o57_every_step_script_points_its_home_into_its_work_directory_before_any_tool(script):
    """O57: a step's allowlist names no home at all (below /dev/null); each script that knows its work directory points
    every home a tool reads into it (home_in) once it learns it and before any command that may read one, so no tool of
    a step, Apptainer and curl included, reads or writes the account's home."""
    code = [line for line in (BUNDLE / script).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]  # fmt: skip
    learnt = next(i for i, line in enumerate(code) if line.startswith("W=$("))
    pointed = next(i for i, line in enumerate(code) if line.startswith(('home_in "$W"', 'home_at "$W"')))
    if script == "status.sh":  # read-only (O58): it points the home there and makes nothing
        assert code[pointed].startswith('home_at "$W"')
    reads = [i for i, line in enumerate(code) if i > learnt and READS_HOME.search(line)]
    assert learnt < pointed, script
    assert all(pointed < i for i in reads), (script, [code[i] for i in reads if i < pointed])


def test_o57_the_service_itself_starts_with_its_home_in_its_work_directory():
    """O57: run_service, which becomes the service through Apptainer, points every home into the work directory before
    its apptainer exec, whichever script runs it."""
    text = (BUNDLE / "lib.sh").read_text(encoding="utf-8")
    body = text[text.index("run_service() {") :]
    body = body[: body.index("\n}\n")]
    assert body.index('home_in "$W"') < body.index("run -- apptainer exec")


def leftovers(kind: str, tmpfs: Path) -> subprocess.CompletedProcess:
    """bundle/leftovers.py as down runs it on a node, here on a directory of this host's."""
    argv = [sys.executable, "-I", "-B", str(BUNDLE / "leftovers.py"), kind, str(tmpfs)]
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)


def test_o57_leftovers_counts_the_work_directories_of_ours_of_one_kind_and_nothing_else(tmp_path):
    """O57: down's node check counts the directories named as make_workdir names them, of the kind asked, directly in
    the tmpfs: another kind, another name, a file or a directory deeper down is no work directory of that kind."""
    tmpfs = tmp_path / "tmpfs"
    tmpfs.mkdir()
    assert json.loads(leftovers("oz", tmpfs).stdout) == {"count": 0, "kind": "oz"}
    for name in ("hpcop-oz.abc123", "hpcop-oz.XYZ789", "hpcop-op.abc123", "hpcop-oz.short", "other"):
        (tmpfs / name).mkdir()
    (tmpfs / "other" / "hpcop-oz.deeper").mkdir()
    assert json.loads(leftovers("oz", tmpfs).stdout) == {"count": 2, "kind": "oz"}
    assert json.loads(leftovers("op", tmpfs).stdout) == {"count": 1, "kind": "op"}


def test_o57_leftovers_gives_no_count_for_a_tmpfs_that_is_a_link_or_cannot_be_read(tmp_path):
    """O57: a tmpfs that is a link is not where the bundle made its work directories (lib.sh replaces a TMPDIR that is
    a link), and one that cannot be read shows nothing: no count is printed, so down keeps the service unverified."""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "linked").symlink_to(real)
    for tmpfs in (tmp_path / "linked", tmp_path / "missing"):
        done = leftovers("oz", tmpfs)
        assert done.returncode != 0, tmpfs
        assert done.stdout == "", tmpfs
    assert leftovers("xx", real).returncode == 2


def test_node_init_names_a_missing_ss_before_it_makes_anything(tmp_path, no_ss):
    """O57: node-init.sh checks its tools up front: without ss it dies naming it, before its sweep or a work
    directory, so a step fails in its first second saying why."""
    holder = tmp_path / "holder"
    holder.mkdir()
    env = {"PATH": str(no_ss), "TMPDIR": str(holder), "LC_ALL": "C"}
    done = subprocess.run(
        [shutil.which("bash"), str(BUNDLE / "node-init.sh"), "oz", str(tmp_path / "onezone.tar")],
        capture_output=True, text=True, timeout=60, check=False, env=env,
    )  # fmt: skip
    assert done.returncode == 1
    assert re.search(r"not found on PATH \(.*\): .*\bss\b", done.stderr)
    assert "hidepid" not in done.stderr  # it stopped before its first check of the node
    assert os.listdir(holder) == []
