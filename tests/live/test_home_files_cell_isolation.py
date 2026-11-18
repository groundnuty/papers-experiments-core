"""O63, a real Apptainer cell: it cannot read the key files a shard step staged into its dispatcher's home, nor the
operator's source tree, nor the dispatcher's /proc. Skipped unless `apptainer` is on the PATH and
`EXPCORE_LIVE_APPTAINER_IMAGE` names an agent image (a SIF with a POSIX `sh`, `ls` and `cat`: nothing else):

    EXPCORE_LIVE_APPTAINER_IMAGE=/path/to/agent.sif uv run pytest tests/live/test_home_files_cell_isolation.py

The test's trees go under `EXPCORE_LIVE_SCRATCH` (default: the working directory), never `/tmp` or `/dev/shm`, which a
cell binds its own directories over.

The cell runs as this user, as on HPC, with the command line the sandbox builds (`exec_argv`) and the three binds every
cell gets from the run's directory (`/work`, `/tmp`, `/dev/shm`; `sandbox.py`'s first binds), beside a dispatcher's
home on a stand-in tmpfs that `stage_home_files` filled from a tree of invented keys. It lists and reads the copy and
the source by their absolute paths and the dispatcher's `/proc/<pid>` entries, and every attempt must fail. Nothing
here is a real key."""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

IMAGE = os.environ.get("EXPCORE_LIVE_APPTAINER_IMAGE")
if shutil.which("apptainer") is None or not IMAGE:
    pytest.skip("apptainer or EXPCORE_LIVE_APPTAINER_IMAGE is missing: no real cell to run", allow_module_level=True)

from expcore_onedata.sandbox.apptainer.argv import Bind, exec_argv  # noqa: E402

SHARDS = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
KEY = ".zqsecrets/alpha-zq/key-zq"
SECRET = "sk-zq-live-0000"
CANARY = "canary-in-the-cells-own-work"


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_live_home_files", SHARDS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def private_dirs(path: Path, below: Path) -> None:
    """`path` and every directory between it and `below`, each 0700 whatever the umask (`mkdir(parents=True, mode=)`
    applies the mode to the leaf alone, and the product refuses a source tree whose directories are open to others)."""
    for directory in (*reversed(path.relative_to(below).parents), path.relative_to(below)):
        (below / directory).mkdir(exist_ok=True)
        (below / directory).chmod(0o700)


@pytest.fixture
def base():
    """A directory for the trees that is NOT under `/tmp` or `/dev/shm`: a cell binds its own directories over both, so
    a tree there would be hidden by the bind and the probes below would prove nothing about the host's file systems. It
    is under `EXPCORE_LIVE_SCRATCH` when that is set, else under the working directory, removed afterwards."""
    root = Path(os.environ.get("EXPCORE_LIVE_SCRATCH") or Path.cwd()).resolve()
    where = Path(tempfile.mkdtemp(prefix=".zq-home-files-", dir=root))
    where.chmod(0o700)
    assert not str(where).startswith(("/tmp/", "/dev/shm/")), "set EXPCORE_LIVE_SCRATCH to a directory outside /tmp"
    try:
        yield where
    finally:
        shutil.rmtree(where, ignore_errors=True)


def test_o63_a_real_cell_cannot_list_or_read_the_copy_the_source_or_the_dispatchers_proc(shards, base):
    """O63: the cell's attempts on the copy in the step's home, on the operator's source tree and on the dispatcher's
    /proc/<pid> (environ, cwd, mem, the directory itself) each fail, and nothing the cell prints holds the invented
    key, a name of the tree or either path. A positive control comes first: the same cell reads a canary file of its
    own /work and lists /, so a missing `ls` or `cat` (exit 127) cannot pass as a refusal."""
    private_dirs(base / "operator-keys", base)
    source = base / "operator-keys"
    private_dirs((source / KEY).parent, base)
    (source / KEY).write_text(SECRET + "\n")
    (source / KEY).chmod(0o600)
    home = base / "tmpfs" / "expcore-shard-fed-a.k3j9xq" / "home"
    private_dirs(home, base)
    assert shards.stage_home_files(shards.read_home_files(str(source)), home) == 1
    copy = home / KEY
    assert copy.read_text() == SECRET + "\n"  # the dispatcher's side holds it
    run = base / "tmpfs" / "root" / "run-1"
    binds = []
    for name, target in (("work", "/work"), ("tmp", "/tmp"), ("shm", "/dev/shm")):
        private_dirs(run / name, base)
        binds.append(Bind(run / name, target, True))
    (run / "work" / "canary").write_text(CANARY + "\n")
    dispatcher = os.getpid()
    attempts = [f"ls '{copy.parent}'", f"cat '{copy}'", f"ls '{home}'", f"ls '{source}'", f"cat '{source / KEY}'",
                f"ls '{base}'", f"ls /proc/{dispatcher}", f"cat /proc/{dispatcher}/environ",
                f"ls -l /proc/{dispatcher}/cwd", f"cat /proc/{dispatcher}/mem"]  # fmt: skip
    script = "cat /work/canary 2>&1; echo CAT:$?; ls / >/dev/null 2>&1; echo LS:$?; "
    # each probe's standard output is kept apart from its status (its errors are discarded: they name the path asked
    # for), so a probe that prints protected data and then fails cannot pass
    script += "; ".join(f'out=$({a} 2>/dev/null); echo "ATTEMPT:$?"; echo "OUT:$out"' for a in attempts)
    argv = exec_argv(["apptainer"], Path(IMAGE), binds, {}, ["sh", "-c", script])
    done = subprocess.run(argv, capture_output=True, text=True, timeout=180, check=False)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert CANARY in lines  # the control: the cell reads what it is given
    assert "CAT:0" in lines
    assert "LS:0" in lines
    codes = [int(line.removeprefix("ATTEMPT:")) for line in lines if line.startswith("ATTEMPT:")]
    assert len(codes) == len(attempts)
    assert all(code not in (0, 127) for code in codes), dict(zip(attempts, codes, strict=True))
    assert [line for line in lines if line.startswith("OUT:")] == ["OUT:"] * len(attempts)  # and printed nothing
    shown = done.stdout + done.stderr
    for word in (SECRET, "zq", str(base)):
        assert word not in shown
