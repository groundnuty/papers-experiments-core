"""The layer boundary: expcore imports only the standard library and itself, expcore_onedata also expcore."""

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
STDLIB = set(sys.stdlib_module_names)
_DYNAMIC_IMPORTERS = {"import_module", "__import__"}


def _normalise(name: str) -> str:
    """Normalise a distribution name the way import names are compared here."""
    return name.lower().replace("-", "_")


def declared_dependencies(extra: str | None = None) -> set[str]:
    """Return the normalised names of the declared runtime dependencies, plus one optional extra."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    requirements = list(project.get("dependencies", []))
    if extra is not None:
        requirements += project.get("optional-dependencies", {}).get(extra, [])
    names = {re.match(r"[A-Za-z0-9_.\-]+", requirement) for requirement in requirements}
    return {_normalise(m.group()) for m in names if m}


def imported_roots(source: str) -> set[str]:
    """Return the top-level names an absolute import statement or a literal dynamic import pulls in."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            first = node.args[0] if node.args else None
            if name in _DYNAMIC_IMPORTERS and isinstance(first, ast.Constant) and isinstance(first.value, str):
                roots.add(first.value.split(".")[0])
    return roots


def violations(source: str, package: str) -> list[str]:
    """List the imports in source that the layer rules forbid for the given package."""
    allowed = STDLIB | {package}
    if package == "expcore":
        allowed |= declared_dependencies()
    elif package == "expcore_onedata":
        allowed |= {"expcore"} | declared_dependencies("onedata") | declared_dependencies()
    else:
        raise ValueError(f"unknown package {package!r}")
    return sorted(root for root in imported_roots(source) if _normalise(root) not in {_normalise(a) for a in allowed})


@pytest.mark.parametrize("package", ["expcore", "expcore_onedata"])
def test_every_module_respects_the_boundary(package):
    modules = sorted((SRC / package).rglob("*.py"))
    assert modules, f"no modules found under {SRC / package}"
    problems = {
        str(path.relative_to(ROOT)): found
        for path in modules
        if (found := violations(path.read_text(encoding="utf-8"), package))
    }
    assert not problems, f"forbidden imports: {problems}"


@pytest.mark.parametrize(
    "source",
    [
        "import expcore_onedata\n",
        "import expcore_onedata.sandbox as box\n",
        "from expcore_onedata import tokens\n",
        "from expcore_onedata.sandbox.gate import Gate\n",
        "def late():\n    import expcore_onedata\n",
        "import importlib\nimportlib.import_module('expcore_onedata.health')\n",
        "__import__('expcore_onedata')\n",
        "import requests\n",
    ],
)
def test_planted_import_fails_inside_expcore(source):
    assert violations(source, "expcore"), "the boundary test did not notice a planted import"


@pytest.mark.parametrize(
    "source",
    [
        "import os\nimport sys\n",
        "from collections import abc\n",
        "from __future__ import annotations\n",
        "import expcore.models\n",
        "from expcore.loop import run\n",
        "from . import sibling\n",
        "from .sibling import thing\n",
        "import importlib\nimportlib.import_module('json')\n",
    ],
)
def test_allowed_imports_inside_expcore(source):
    assert violations(source, "expcore") == []


def test_onedata_may_import_expcore_but_not_other_third_party_code():
    assert violations("from expcore.provenance import headers\nimport os\n", "expcore_onedata") == []
    assert violations("import requests\n", "expcore_onedata") == ["requests"]
