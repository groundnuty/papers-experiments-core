"""The HPC bundle's tools on a step's PATH (deploy/hpc-federation/bundle/lib.sh, ports.sh, node-init.sh): a step's
PATH is the driver's allowlist alone, and the first live zone step found no `ss` there. A script checks its tools up
front and dies naming each missing one, and every reading of the node's listeners fails when `ss` is missing or
fails: never an empty listing read as a node with no listener (live: a check whose `ss` was not on PATH counted no
listener, a false pass). The scripts run in plain bash on this host, over a PATH of stand-ins that lacks `ss`, or
whose `ss` fails, or prints listeners as the real one does."""

import os
import re
import shutil
import subprocess
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
