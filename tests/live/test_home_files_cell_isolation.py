"""O63, a real Apptainer cell: it cannot read the key files a shard step staged into its dispatcher's home, nor the
operator's source tree, nor the dispatcher's /proc. Skipped unless `apptainer` is on the PATH and
`EXPCORE_LIVE_APPTAINER_IMAGE` names an agent image (a SIF with a POSIX `sh`, `ls` and `cat`):

    EXPCORE_LIVE_APPTAINER_IMAGE=/path/to/agent.sif uv run pytest tests/live/test_home_files_cell_isolation.py

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
from pathlib import Path

import pytest

IMAGE = os.environ.get("EXPCORE_LIVE_APPTAINER_IMAGE")
if shutil.which("apptainer") is None or not IMAGE:
    pytest.skip("apptainer or EXPCORE_LIVE_APPTAINER_IMAGE is missing: no real cell to run", allow_module_level=True)

from expcore_onedata.sandbox.apptainer.argv import Bind, cell_command, exec_argv  # noqa: E402

SHARDS = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
KEY = ".zqsecrets/alpha-zq/key-zq"
SECRET = "sk-zq-live-0000"


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


def test_o63_a_real_cell_cannot_list_or_read_the_copy_the_source_or_the_dispatchers_proc(shards, tmp_path):
    """O63: the cell's attempts on the copy in the step's home, on the operator's source tree and on the dispatcher's
    /proc/<pid> (environ, cwd, mem, the directory itself) each fail, and nothing the cell prints holds the invented
    key, a name of the tree or either path."""
    source = tmp_path / "operator-keys"
    (source / KEY).parent.mkdir(parents=True, mode=0o700)
    (source / KEY).write_text(SECRET + "\n")
    (source / KEY).chmod(0o600)
    home = tmp_path / "tmpfs" / "expcore-shard-fed-a.k3j9xq" / "home"
    home.mkdir(parents=True, mode=0o700)
    assert shards.stage_home_files(shards.read_home_files(str(source)), home) == 1
    copy = home / KEY
    assert copy.read_text() == SECRET + "\n"  # the dispatcher's side holds it
    run = tmp_path / "tmpfs" / "root" / "run-1"
    binds = []
    for name, target in (("work", "/work"), ("tmp", "/tmp"), ("shm", "/dev/shm")):
        (run / name).mkdir(parents=True, mode=0o700)
        binds.append(Bind(run / name, target, True))
    dispatcher = os.getpid()
    attempts = [f"ls '{copy.parent}'", f"cat '{copy}'", f"ls '{home}'", f"ls '{source}'", f"cat '{source / KEY}'",
                f"ls '{tmp_path}'", f"ls /proc/{dispatcher}", f"cat /proc/{dispatcher}/environ",
                f"ls -l /proc/{dispatcher}/cwd", f"cat /proc/{dispatcher}/mem"]  # fmt: skip
    script = "; ".join(f"{a} >/dev/null 2>&1; echo $?" for a in attempts)
    argv = exec_argv(["apptainer"], Path(IMAGE), binds, {}, cell_command(60.0, ["sh", "-c", script]))
    done = subprocess.run(argv, capture_output=True, text=True, timeout=180, check=False)
    assert done.returncode == 0, done.stderr
    codes = [int(line) for line in done.stdout.split()]
    assert len(codes) == len(attempts)
    assert all(code != 0 for code in codes), dict(zip(attempts, codes, strict=True))
    shown = done.stdout + done.stderr
    for word in (SECRET, "zq", str(tmp_path)):
        assert word not in shown
