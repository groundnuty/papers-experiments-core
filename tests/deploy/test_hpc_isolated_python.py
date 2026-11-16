"""Every Python the HPC driver launches runs isolated, and every step it builds names its working directory (O58, O57).

Seen live on 16 Nov 2026 (F2): a `down` launched from the login node's /tmp had the fault relay's `stop` import another
user's /tmp/inspect.py in place of the standard library's, since `python -m` puts the directory it was launched from
first on `sys.path`. The relay serves the federation's root, its secrets included, for the federation's whole life. A
step that inherits the login node's working directory starts in the node's shared /tmp instead (slurmstepd falls back
there when the directory does not exist on the node), so a step's first Python would be exposed the same way.

The relay here is a real process of this core's module, started and stopped through the driver's own runner, launched
from a directory that holds modules named like the standard library's and a package named like the core's, with more
of them in the federation's root and under the PYTHON* variables of the launching shell: it must never import one. The
driver's code is scanned so that no Python argv it builds lacks `-I`, and no `srun` or `sbatch` lacks `--chdir`; the
fake Slurm of hpc_fed_support refuses both too, in every test that drives it.

Nothing here reaches a cluster: the relay is a local process that runs no verb.
"""

import ast
import contextlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from hpc_fed_support import HPC, LAYOUT, hpc_on_path

DEPLOY = HPC.parent
PLANTED = ("inspect", "json", "uuid")  # modules of the standard library the relay imports as it starts
PACKAGE = "expcore_onedata"  # the core's own package, which `-m` would take from the first directory on sys.path
PLANT_STATUS = 86


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcrelay
        import hpcslurm

        modules.relay, modules.slurm = hpcrelay, hpcslurm
        yield modules


def plant(directory: Path, marks: Path) -> Path:
    """Fill `directory` with modules named like the standard library's and a package named like the core's, each of
    which leaves a mark in `marks` (`<directory>-<module>`) and exits with PLANT_STATUS when it is imported."""
    directory.mkdir(parents=True, exist_ok=True)

    def body(name: str) -> str:
        return f"open({str(marks / f'{directory.name}-{name}')!r}, 'w').close()\nraise SystemExit({PLANT_STATUS})\n"

    for name in PLANTED:
        (directory / f"{name}.py").write_text(body(name), encoding="utf-8")
    (directory / PACKAGE).mkdir(exist_ok=True)
    (directory / PACKAGE / "__init__.py").write_text(body(PACKAGE), encoding="utf-8")
    return directory


def private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


@contextlib.contextmanager
def relay(hpc, root: Path, environ: dict, log: Path):
    """The relay as `up` starts it (the real runner, the argv prefix of the verbs) and, after the block, ended."""
    runner = hpc.slurm.Runner()
    command = [sys.executable, "-I", "-B", str(HPC / "hpcfed.py"), "--layout", str(LAYOUT), "--root", str(root)]
    client = hpc.relay.start(
        runner, sys.executable, root, command, log, environ, record=lambda client: None, clock=time.monotonic,
        sleep=lambda seconds: time.sleep(0.2),
    )  # fmt: skip
    try:
        yield runner, client
    finally:
        runner.end_client(client, grace_s=2.0)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(client.pid, os.WNOHANG)


def stopped(hpc, runner, client, root: Path) -> bool:
    """`down`'s stop of the relay, and its process gone."""
    if not hpc.relay.stop(runner, sys.executable, root):
        return False
    deadline = time.monotonic() + 10
    while runner.client_alive(client) and time.monotonic() < deadline:
        time.sleep(0.1)
    return not runner.client_alive(client)


def base_environment(tmp_path: Path) -> dict:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)}


# -- the relay, a real process ------------------------------------------------------------------------------------


def test_o58_the_relay_never_imports_a_module_planted_where_it_is_launched_or_in_its_root(hpc, tmp_path, monkeypatch):
    """O58: launched from a directory that holds `inspect.py` (as the login node's /tmp did, F2), more modules named
    like the standard library's and a package named like the core's, with the same planted in the federation's root,
    the relay's `serve`, `alive` and `stop` import none of them: it beats, `stop` ends it, and nothing planted ran.
    The canary, the same `alive` without `-I`, does import one, so the plant is live."""
    marks = private(tmp_path / "marks")
    launch = plant(tmp_path / "launch", marks)
    root = plant(private(tmp_path / "root"), marks)
    canary = subprocess.run(
        [sys.executable, "-B", "-m", hpc.relay.RELAY, "alive", "--root", str(root)],
        cwd=launch, capture_output=True, timeout=120, check=False,
    )  # fmt: skip
    assert canary.returncode == PLANT_STATUS
    assert sorted(os.listdir(marks)) == [f"launch-{PACKAGE}"]
    (marks / f"launch-{PACKAGE}").unlink()
    monkeypatch.chdir(launch)
    with relay(hpc, root, base_environment(tmp_path), tmp_path / "relay.log") as (runner, client):
        assert stopped(hpc, runner, client, root)
    assert os.listdir(marks) == []


def test_o58_no_python_variable_of_the_launching_shell_reaches_the_relay(hpc, tmp_path, monkeypatch):
    """O58: `alive` and `stop` run in the operator's environment, and `serve` gets up's environment filtered to its
    allowlist. A PYTHONPATH that names planted modules, a PYTHONHOME that holds no Python, PYTHONINSPECT and a
    PYTHONSTARTUP change nothing (`-I` ignores every PYTHON* variable), and the relay's environment is PATH, HOME and
    LC_ALL alone."""
    marks = private(tmp_path / "marks")
    planted = plant(tmp_path / "planted", marks)
    root = private(tmp_path / "root")
    python = {
        "PYTHONPATH": str(planted),
        "PYTHONHOME": str(tmp_path / "no-python-here"),
        "PYTHONINSPECT": "1",
        "PYTHONSTARTUP": str(planted / "inspect.py"),
    }
    for name, value in python.items():
        monkeypatch.setenv(name, value)
    with relay(hpc, root, {**base_environment(tmp_path), **python}, tmp_path / "relay.log") as (runner, client):
        given = Path(f"/proc/{client.pid}/environ").read_bytes().split(b"\0")
        assert sorted(w.split(b"=", 1)[0].decode() for w in given if w) == ["HOME", "LC_ALL", "PATH"]
        assert stopped(hpc, runner, client, root)
    assert os.listdir(marks) == []


def test_o58_the_relay_works_in_the_federations_root_never_where_up_was_launched(hpc, tmp_path, monkeypatch):
    """O58: the relay, and every verb it runs (its children), work in the federation's root, a 0700 directory of
    ours, whatever directory `up` was launched from."""
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.chdir(launch)
    root = private(tmp_path / "root")
    with relay(hpc, root, base_environment(tmp_path), tmp_path / "relay.log") as (runner, client):
        assert Path(os.readlink(f"/proc/{client.pid}/cwd")) == root.resolve()
        assert stopped(hpc, runner, client, root)


@pytest.mark.parametrize("kind", ["group-readable", "open-to-all", "a-link"])
def test_o58_the_relay_never_starts_in_a_root_that_is_not_a_private_directory_of_ours(hpc, tmp_path, kind):
    """O58: a root open to others, or reached through a link, is refused before the relay is spawned."""
    target = private(tmp_path / "target")
    root = tmp_path / "root"
    if kind == "a-link":
        root.symlink_to(target)
    else:
        root.mkdir()
        root.chmod(0o750 if kind == "group-readable" else 0o777)
    spawned = []
    runner = SimpleNamespace(spawn=lambda *args, **kwargs: spawned.append(args), run=None, end_client=None)
    with pytest.raises(RuntimeError, match="not a private directory"):
        hpc.relay.start(
            runner, sys.executable, root, ["x"], tmp_path / "relay.log", base_environment(tmp_path),
            record=lambda client: None, clock=time.monotonic, sleep=lambda seconds: None,
        )  # fmt: skip
    assert spawned == []


def test_o58_the_verbs_argv_prefix_runs_isolated_from_a_planted_directory(tmp_path):
    """O58: the argv prefix the relay runs every verb through (`<python> -I -B hpcfed.py --layout L --root R`) finds
    its own modules with the script's directory off `sys.path`, and imports nothing from the directory it runs in: a
    verb on a root with no state file says so, and nothing planted ran."""
    marks = private(tmp_path / "marks")
    launch = plant(tmp_path / "launch", marks)
    root = private(tmp_path / "fed")
    done = subprocess.run(
        [sys.executable, "-I", "-B", str(HPC / "hpcfed.py"), "--layout", str(LAYOUT), "--root", str(root), "status",
         "aspen"],
        cwd=launch, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "LC_ALL": "C"},
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert done.returncode == 2, done.stderr
    assert "state could not be read" in done.stderr
    assert os.listdir(marks) == []


# -- every argv the driver builds, scanned -------------------------------------------------------------------------

INTERPRETER = re.compile(r"python[0-9.]*")  # a file name: python, python3, python3.11
NAMED_PYTHON = re.compile(r"(^|_)python$")  # a variable or attribute holding one: python, node_python, plan.python
LAUNCHERS = ("srun", "sbatch")  # Slurm's: each must name the working directory it starts in


def interpreter(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "executable":
        return isinstance(node.value, ast.Name) and node.value.id == "sys"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return INTERPRETER.fullmatch(node.value.rsplit("/", 1)[-1]) is not None
    name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
    return name is not None and NAMED_PYTHON.search(name) is not None


def python_findings(source: str) -> list[str]:
    """Each list or tuple display in Python source that names an interpreter not followed by `-I`, or starts a Slurm
    launch with no `--chdir=`: `<line>: <the display>`."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.List | ast.Tuple):
            continue
        words = node.elts
        for at, word in enumerate(words):
            after = words[at + 1] if at + 1 < len(words) else None
            if interpreter(word) and not (isinstance(after, ast.Constant) and after.value == "-I"):
                found.append(f"{node.lineno}: {ast.unparse(node)}")
        first = words[0] if words else None
        if isinstance(first, ast.Constant) and first.value in LAUNCHERS:
            chdir = [w for w in words if isinstance(w, ast.Constant) and str(w.value).startswith("--chdir=")]
            if not chdir:
                found.append(f"{node.lineno}: {ast.unparse(node)}")
    return found


SHELL_PYTHON = re.compile(r"""(?:^|[\s;|&(`]|\$\()(?:exec\s+)?("\$HPCOP_PY"|\$HPCOP_PY|python3(?:\.\d+)?)\s+(\S+)""")
SHELL_SLURM = re.compile(r"(?:^|[\s;|&(`]|\$\()(srun|sbatch)\s+-")


def shell_findings(source: str) -> list[str]:
    """Each command of a shell script that runs a Python without `-I`, or an `srun` or `sbatch` with no `--chdir=`,
    on its logical line (continuations joined, comments dropped): `<line>: <the command>`."""
    found, logical, start = [], "", 0
    for number, line in enumerate(source.splitlines(), 1):
        code = "" if line.lstrip().startswith("#") else re.split(r"\s#\s", line, maxsplit=1)[0]
        if not logical:
            start = number
        logical += code
        if code.endswith("\\"):
            logical = logical[:-1] + " "
            continue
        bad = [m for m in SHELL_PYTHON.finditer(logical) if m.group(2) != "-I"]
        bad += [m for m in SHELL_SLURM.finditer(logical) if "--chdir=" not in logical[m.start() :]]
        found += [f"{start}: {logical.strip()}" for _ in bad[:1]]
        logical = ""
    return found


def deploy_files(*patterns: str) -> list[Path]:
    return sorted(p for pattern in patterns for p in (DEPLOY / "hpc-federation").rglob(pattern))


def test_o58_the_scans_find_a_python_without_isolation_and_a_launch_that_names_no_working_directory():
    """O58, O57: the scans below catch what they guard against, so their silence on the driver means something."""
    python_bad = (
        '[python, "-B", "-m", "x"]\n[sys.executable, "x.py"]\n("python3.11", "-c", "y")\n[site.node_python, "z"]\n'
        '[plan.python, "-m", "expcore"]\n["srun", "--exact", "-N1"]\n["sbatch", "--wrap=x"]\n'
    )
    python_good = '[python, "-I", "-B", "-m", "x"]\n["srun", "--chdir=/", "x"]\n{"python", "x"}\n'
    shell_bad = (
        'python3 -c "x"\nx=$(python3.11 y.py)\n"$HPCOP_PY" --version\nexec $HPCOP_PY -B z.py\n'
        "srun --jobid=1 \\\n  -w n bash x.sh\nsbatch --wrap=x\n"
    )
    shell_good = (
        'python3 -I -c "x"\n"$HPCOP_PY" -I z.py  # python3 x\n# python3 y.py\n'
        'srun --jobid=1 \\\n  --chdir=/ bash x.sh\ndie "$HPCOP_PY cannot run"\nHPCOP_PY=${HPCOP_PY:-python3.11}\n'
    )
    assert len(python_findings(python_bad)) == 7
    assert python_findings(python_good) == []
    assert len(shell_findings(shell_bad)) == 6
    assert shell_findings(shell_good) == []


@pytest.mark.parametrize(
    "path",
    [*deploy_files("*.py"), *sorted((DEPLOY / "hpc-tests").glob("*.py"))],
    ids=lambda p: p.relative_to(DEPLOY).as_posix(),
)
def test_o58_every_python_argv_the_driver_builds_runs_isolated_and_every_slurm_launch_names_its_directory(path):
    """O58, O57: in the driver, its bundle, its run harness, shards.py and the test runner's tool, every argv display
    that names a Python (`sys.executable`, a `python*` file name, a variable named `python` or `*_python`) puts `-I`
    right after it, and every `srun` or `sbatch` argv carries `--chdir=`. A new one that misses either fails here."""
    assert python_findings(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("path", deploy_files("*.sh", "*.sbatch"), ids=lambda p: p.relative_to(DEPLOY).as_posix())
def test_o57_every_python_and_every_step_a_script_of_the_driver_launches_is_isolated_and_names_its_directory(path):
    """O57: every script of the bundle and the run harness runs each Python (`$HPCOP_PY`, `python3`, `python3.x`)
    with `-I`, inside a container too, and every `srun` it launches names its working directory (`--chdir=/`), so no
    step starts in the directory srun was called from, or in the node's shared /tmp when that does not exist there."""
    assert shell_findings(path.read_text(encoding="utf-8")) == []
