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
_IMPORT_MODULES = {"importlib", "builtins"}


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


def _dynamic_importers(tree: ast.AST) -> set[str]:
    """Return the plain names that stand for import_module or __import__, following aliases and assignments."""
    names = set(_DYNAMIC_IMPORTERS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in _IMPORT_MODULES:
            names.update(a.asname or a.name for a in node.names if a.name in _DYNAMIC_IMPORTERS)
    for _ in range(3):  # a few rounds follow chains such as `load = import_module; again = load`
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _is_importer(node.value, names):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _is_importer(node: ast.AST, names: set[str]) -> bool:
    """Tell whether an expression names the dynamic importer: a bound name or any `x.import_module` attribute."""
    if isinstance(node, ast.Name):
        return node.id in names
    return isinstance(node, ast.Attribute) and node.attr in _DYNAMIC_IMPORTERS


def _constant_string(node: ast.AST | None) -> str | None:
    """Return the value of a string constant, else None."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _module_argument(call: ast.Call) -> str | None:
    """Return the module a dynamic import names, string constants only, as a name whose first part is its root.

    A relative name (".x") is resolved against the package argument, positional or package=; with no usable package
    argument it cannot name another layer and is not reported.
    """
    keywords = {k.arg: k.value for k in call.keywords}
    name = _constant_string(call.args[0] if call.args else keywords.get("name"))
    if name is None or not name.startswith("."):
        return name
    package = _constant_string(call.args[1] if len(call.args) > 1 else keywords.get("package"))
    return package or None


def imported_roots(source: str) -> set[str]:
    """Return the top-level names an absolute import statement or a literal dynamic import pulls in."""
    tree = ast.parse(source)
    importers = _dynamic_importers(tree)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and _is_importer(node.func, importers):
            name = _module_argument(node)
            if name:
                roots.add(name.split(".")[0])
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
        "import importlib\nimportlib.import_module(name='expcore_onedata.health')\n",
        "__import__(name='requests')\n",
        "from importlib import import_module as load\nload('expcore_onedata')\n",
        "from importlib import import_module as load\nload(name='requests')\n",
        "import importlib as il\nil.import_module('expcore_onedata')\n",
        "import importlib as il\nil.import_module(name='requests.sessions')\n",
        "import builtins\nbuiltins.__import__('requests')\n",
        "import importlib\nload = importlib.import_module\nload('expcore_onedata')\n",
        "from importlib import import_module\nload = import_module\nagain = load\nagain(name='requests')\n",
        "from builtins import __import__ as imp\nimp('requests')\n",
        "import importlib\nimportlib.import_module('requests', package='expcore')\n",
        "import importlib\nimportlib.import_module('.x', 'expcore_onedata')\n",
        "import importlib\nimportlib.import_module('.x', package='expcore_onedata')\n",
        "import importlib\nimportlib.import_module(name='..x', package='expcore_onedata.sandbox')\n",
        "from importlib import import_module as load\nload('.x', 'requests')\n",
        "import importlib as il\nil.import_module('.x.y', package='requests.sessions')\n",
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
        "import importlib\nimportlib.import_module('.sibling', package='expcore')\n",
        "import importlib\nimportlib.import_module('.sibling', 'expcore.loop')\n",
        "import importlib\nimportlib.import_module('.sibling')\n",
        "import importlib\nimportlib.import_module('.sibling', package=name_from_somewhere)\n",
        "import importlib\nimportlib.import_module(name='json')\n",
        "from importlib import import_module as load\nload('expcore.loop')\n",
        "import importlib as il\nil.import_module(name='os.path')\n",
        "def import_module(x):\n    return x\nimport_module(1)\n",
    ],
)
def test_allowed_imports_inside_expcore(source):
    assert violations(source, "expcore") == []


def test_onedata_may_import_expcore_but_not_other_third_party_code():
    assert violations("from expcore.provenance import headers\nimport os\n", "expcore_onedata") == []
    assert violations("import requests\n", "expcore_onedata") == ["requests"]
