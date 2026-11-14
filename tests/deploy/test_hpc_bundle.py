"""The HPC bundle as a whole (deploy/hpc-federation): what it runs on, its batch jobs, and its port shim."""

import ast
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

HPC = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation"
BUNDLE = HPC / "bundle"
OLDEST_ON_A_NODE = (3, 8)  # the guard runs on the Onedata images' Python 3.8; the other helpers on the nodes' 3.9+
GCC = shutil.which("gcc")
needs_gcc = pytest.mark.skipif(GCC is None, reason="no gcc on this host")


def test_every_python_file_of_the_bundle_parses_on_the_images_python():
    """The linter writes for the distribution's Python, the bundle runs on older ones: each file parses as 3.8."""
    files = sorted(HPC.rglob("*.py"))
    assert len(files) >= 7
    for path in files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=OLDEST_ON_A_NODE)


@pytest.mark.parametrize("job", ["zone.sbatch", "provider.sbatch"])
def test_every_batch_job_has_a_time_limit_and_is_never_requeued(job):
    """A requeued service would come back empty, as a new identity, and an unbounded one stays exposed."""
    directives = [
        line for line in (BUNDLE / job).read_text(encoding="utf-8").splitlines() if line.startswith("#SBATCH")
    ]
    assert "#SBATCH --no-requeue" in directives
    assert any(re.fullmatch(r"#SBATCH --time=\d+:\d\d:\d\d", line) for line in directives)


@pytest.mark.parametrize("job", ["zone.sbatch", "provider.sbatch"])
def test_a_service_that_ends_without_a_teardown_fails_its_job(job, tmp_path):
    """The batch job's state says how its service ended: success only for a stop teardown.sh asked for (its marker in
    the work directory), and otherwise the service's own status, or 1 when it exited 0 by itself. Run here as the
    job's own last lines, after `wait` gave the service's status."""
    text = (BUNDLE / job).read_text(encoding="utf-8")
    tail = text[text.index('status=0; wait "$svc" || status=$?') :]
    say = 'say() { printf "%s\\n" "$*"; }\n'

    def end(status: int, requested: bool) -> int:
        work = tmp_path / f"w{status}{requested}"
        work.mkdir()
        if requested:
            (work / "stop-requested").touch()
        script = say + f'W="{work}"\nsvc=x\nwait() {{ return {status}; }}\n' + tail
        return subprocess.run(["bash", "-c", script], capture_output=True, timeout=30, check=False).returncode

    assert end(143, requested=True) == 0
    assert end(0, requested=True) == 0
    assert end(0, requested=False) == 1
    assert end(137, requested=False) == 137
    teardown = (BUNDLE / "teardown.sh").read_text(encoding="utf-8")
    assert teardown.index('then : > "$W/stop-requested"; fi') < teardown.index('signal_ours "$pid" "$start" TERM')


def test_the_shim_and_the_port_list_name_the_same_public_ports():
    """The shim compiles its public ports in; ports.sh names them for the live check. They must agree."""
    shim = (BUNDLE / "portshim.c").read_text(encoding="utf-8")
    block = shim.split("hpcop-public-ports: begin", 1)[1].split("hpcop-public-ports: end", 1)[0]
    in_shim = sorted(int(n) for n in re.findall(r"^\s*(\d+),", block, re.M))
    listed = re.search(r"^HPCOP_PUBLIC_PORTS='([\d ]+)'$", (BUNDLE / "ports.sh").read_text(encoding="utf-8"), re.M)
    assert listed is not None
    assert in_shim == sorted(int(n) for n in listed.group(1).split()) == [6665, 8443]


@needs_gcc
def test_the_port_shim_compiles_without_a_warning(tmp_path):
    """build-rootfs.sh compiles it inside the image; here with the host's gcc, every warning an error."""
    out = tmp_path / "portshim.so"
    argv = [GCC, "-shared", "-fPIC", "-O2", "-fno-stack-protector", "-Wall", "-Wextra", "-Werror", "-o", str(out)]
    result = subprocess.run(
        [*argv, str(BUNDLE / "portshim.c")], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    assert out.stat().st_size > 0


@pytest.fixture
def short_dir():
    """A directory with a short path. The shim tells programs apart by /proc/self/exe, read into a 64-byte buffer
    that refuses a cut path, and the unit test checks that it reads its own name from there: a binary under a long
    pytest directory would fall back to argv[0] and fail that check."""
    path = Path(tempfile.mkdtemp(prefix="su"))
    if len(str(path / "shim_unit")) >= 60:
        pytest.skip(f"the temporary directory's path is too long for the shim's name check: {path}")
    yield path
    shutil.rmtree(path)


@needs_gcc
def test_the_port_shims_decisions_pass_its_unit_tests(short_dir):
    """The shim's bind, listen, connect and owner-check decisions and its file parsing, on canned tables."""
    files = {
        "HPCOP_PEERS_FILE": "peers.test",
        "HPCOP_ERLANG_ENV": "erlang-env.test",
        "HPCOP_PROC_NET_TCP": "tcp.test",
        "HPCOP_PROC_NET_TCP6": "tcp6.test",
        "HPCOP_UNPRIV_START_FILE": "unpriv.test",
    }
    defines = [f'-D{name}="{short_dir / file}"' for name, file in files.items()]
    binary = short_dir / "shim_unit"
    argv = [GCC, "-O1", "-Wall", "-Wextra", "-Werror", *defines, "-DHPCOP_TEST_UID=1000", "-o", str(binary)]
    built = subprocess.run([*argv, str(HPC / "tests" / "shim_unit.c")], capture_output=True, text=True, timeout=120)
    assert built.returncode == 0, built.stderr
    run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60, check=False)
    assert run.returncode == 0, run.stdout[-3000:]
    assert run.stdout.rstrip().endswith("PASS: 0 failure(s)")
