"""C40: the repository's own executors declare their secrets before they start an instrument.

A failed start's diagnostic is shown in `export.json` and the report only for an attempt whose executor called
`RunRequest.declare_secrets` (a call with no argument says the run has none beyond the environment's), since the core
cannot know a secret that only a later `record_run` names. An executor of the example studies, or one the test suite
supplies as a study, that started an instrument without declaring would have every diagnostic of its failed starts
withheld: this scan keeps each of them to the order the guides show.

The scan reads the source: a function that takes `request` and mentions `start_instruments` (calling it, or handing it
to a thread) must call `request.declare_secrets` on an earlier line. A helper that takes no `request` is covered by the
`__call__` that hands it its instruments.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_FILES = sorted(
    [
        *(ROOT / "examples").rglob("plugin.py"),
        *(ROOT / "tests" / "support").glob("*_plugin.py"),
        *(ROOT / "src").rglob("*.py"),
    ]
)


def starters(path: Path) -> list[ast.AsyncFunctionDef | ast.FunctionDef]:
    """The functions of a file that take a `request` and mention `start_instruments`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        takes_request = "request" in {a.arg for a in node.args.args}
        if takes_request and any(isinstance(n, ast.Name) and n.id == "start_instruments" for n in ast.walk(node)):
            found.append(node)
    return found


def declare_lines(function: ast.AsyncFunctionDef | ast.FunctionDef) -> list[int]:
    return [
        n.lineno
        for n in ast.walk(function)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "declare_secrets"
    ]


def start_lines(function: ast.AsyncFunctionDef | ast.FunctionDef) -> list[int]:
    return [n.lineno for n in ast.walk(function) if isinstance(n, ast.Name) and n.id == "start_instruments"]


def test_c40_the_scan_finds_the_executors_it_is_about():
    """C40: the scan is not empty: the example studies and the suite's plugins that start instruments are in it."""
    names = {path.name for path in EXECUTOR_FILES if starters(path)}
    assert {"plugin.py", "faults_shard_plugin.py", "hpc_rehearsal_plugin.py"} <= names


@pytest.mark.parametrize(
    "path", [p for p in EXECUTOR_FILES if starters(p)], ids=lambda p: p.relative_to(ROOT).as_posix()
)
def test_c40_an_executor_declares_its_secrets_before_it_starts_an_instrument(path):
    """C40: `request.declare_secrets(...)` comes before the first mention of `start_instruments` in every executor."""
    for function in starters(path):
        declared, started = declare_lines(function), start_lines(function)
        assert declared, f"{path.name}:{function.name} starts instruments and never calls request.declare_secrets"
        assert min(declared) < min(started), f"{path.name}:{function.name} declares after it starts an instrument"


def test_c40_the_scan_sees_an_executor_that_declares_late_or_never():
    """C40: the scan would fail an executor that declares after the start, or not at all."""
    late = ast.parse("async def run(request):\n    start_instruments([])\n    request.declare_secrets()\n").body[0]
    never = ast.parse("async def run(request):\n    start_instruments([])\n").body[0]
    assert min(declare_lines(late)) > min(start_lines(late))
    assert declare_lines(never) == []
