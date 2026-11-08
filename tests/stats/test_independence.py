"""S09: the simulator and the power study import nothing of the pipeline they check."""

import ast
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
STDLIB = set(sys.stdlib_module_names)
PIPELINE = ("contrasts", "estimators", "pairing", "analysis", "prereg", "kat")


def imported_modules(source: str) -> set[str]:
    """Return every module an import statement names, with `from a import b` counted as `a` and as `a.b`."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level:
            raise AssertionError("relative imports hide the layering; use absolute names")
    return modules


def module_file(name: str) -> Path | None:
    """Return the source file of an expcore module or package, or None when the name is not one."""
    path = SRC.joinpath(*name.split("."))
    for candidate in (path.with_suffix(".py"), path / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def closure(name: str) -> tuple[set[str], set[str]]:
    """Return (expcore modules, third-party roots) reachable through a module's imports and its parent packages."""
    seen: set[str] = set()
    outside: set[str] = set()
    pending = [name]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        parts = current.split(".")
        pending.extend(".".join(parts[:i]) for i in range(1, len(parts)))  # importing a module runs its packages first
        source = module_file(current)
        assert source is not None, f"{current} is not a module of this package"
        for imported in imported_modules(source.read_text(encoding="utf-8")):
            root = imported.split(".")[0]
            if root == "expcore":
                if module_file(imported) is not None:
                    pending.append(imported)
            elif root not in STDLIB and root != "__future__":
                outside.add(root)
    return seen, outside


def test_s09_the_simulator_imports_numpy_and_the_row_shape_only():
    """S09: an import-graph test; the record simulator reaches no other part of the package."""
    internal, outside = closure("expcore.stats.sim")
    assert internal == {"expcore", "expcore.stats", "expcore.stats.sim", "expcore.stats.rows"}
    assert outside == {"numpy"}


def test_s09_the_power_study_imports_numpy_and_scipy_only():
    """S09: the power functions reach no module of the package apart from the empty package files."""
    internal, outside = closure("expcore.stats.power")
    assert internal == {"expcore", "expcore.stats", "expcore.stats.power"}
    assert outside == {"numpy", "scipy"}


def test_s09_the_row_shape_and_the_package_files_import_nothing_outside_the_standard_library():
    """S09: what both independent modules share is plain data, so the pipeline cannot reach them through it."""
    internal, outside = closure("expcore.stats.rows")
    assert internal == {"expcore", "expcore.stats", "expcore.stats.rows"}
    assert outside == set()


@pytest.mark.parametrize("module", ["expcore.stats.sim", "expcore.stats.power"])
def test_s09_no_pipeline_module_is_reachable(module):
    """S09: the named pipeline modules are absent from the closure, whatever the exact allow-list above says."""
    internal, _ = closure(module)
    assert not {f"expcore.stats.{name}" for name in PIPELINE} & internal


def test_s09_the_checker_notices_a_planted_pipeline_import(tmp_path, monkeypatch):
    """S09: the closure walk sees a pipeline import added to a simulator, directly and through the package file."""
    package = tmp_path / "expcore" / "stats"
    package.mkdir(parents=True)
    (tmp_path / "expcore" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "contrasts.py").write_text("import scipy\n")
    (package / "sim.py").write_text("import numpy\nfrom expcore.stats import contrasts\n")
    monkeypatch.setitem(globals(), "SRC", tmp_path)
    internal, outside = closure("expcore.stats.sim")
    assert "expcore.stats.contrasts" in internal
    assert "scipy" in outside
    (package / "sim.py").write_text("import numpy\n")
    (package / "__init__.py").write_text("from expcore.stats import contrasts\n")
    assert "expcore.stats.contrasts" in closure("expcore.stats.sim")[0]
