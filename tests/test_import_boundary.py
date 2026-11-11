"""The layer boundary: expcore imports only the standard library and itself, expcore_onedata also expcore.

The platform layer's testing package (`expcore_onedata.testing`) is held to the same rule, which keeps pytest and every
test tree out of it, and no runtime path may import it: the core, the platform layer's other modules, the CLI and the
shipped example.
"""

import ast
import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
EXAMPLES = ROOT / "examples"
TESTING = "expcore_onedata.testing"
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


# -- the testing package is never part of a runtime path --------------------------------------------------------------


def _dynamic_module(call: ast.Call) -> str | None:
    """Return the full dotted name a literal dynamic import names, a relative one resolved against its package."""
    keywords = {k.arg: k.value for k in call.keywords}
    name = _constant_string(call.args[0] if call.args else keywords.get("name"))
    if name is None or not name.startswith("."):
        return name
    package = _constant_string(call.args[1] if len(call.args) > 1 else keywords.get("package"))
    if not package:
        return None
    try:
        return importlib.util.resolve_name(name, package)
    except ImportError:  # more dots than the package has levels: it cannot name a module
        return None


def _from_base(node: ast.ImportFrom, package: str) -> str | None:
    """Return the dotted module a `from ... import` statement reads, a relative one resolved against `package`."""
    if node.level == 0:
        return node.module
    parts = package.split(".") if package else []
    kept = parts[: len(parts) - (node.level - 1)] if node.level - 1 <= len(parts) else []
    if not kept:
        return None
    return ".".join([*kept, *([node.module] if node.module else [])])


def imported_modules(source: str, package: str) -> set[str]:
    """Return every dotted module name an import statement or a literal dynamic import can load.

    `package` is the dotted package of the file the source is in, which relative imports are resolved against. For
    `from M import a` both `M` and `M.a` are returned, since `a` may be a submodule.
    """
    tree = ast.parse(source)
    importers = _dynamic_importers(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _from_base(node, package)
            if base:
                found.add(base)
                found.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call) and _is_importer(node.func, importers):
            name = _dynamic_module(node)
            if name:
                found.add(name)
    return found


def imports_testing(source: str, package: str) -> bool:
    """Tell whether the source can load `expcore_onedata.testing` or anything below it."""
    return any(name == TESTING or name.startswith(f"{TESTING}.") for name in imported_modules(source, package))


def runtime_files() -> list[tuple[Path, str]]:
    """Every Python file a runtime path can load, with its package: all of src/ but the testing package, and the
    example study the distribution ships."""
    files = [
        (path, ".".join(path.relative_to(SRC).parts[:-1]))
        for path in sorted(SRC.rglob("*.py"))
        if path.relative_to(SRC).parts[:2] != ("expcore_onedata", "testing")
    ]
    return files + [(path, "") for path in sorted((EXAMPLES / "tiny_study").rglob("*.py"))]


def test_no_runtime_module_imports_the_testing_package():
    files = runtime_files()
    assert len(files) > 100, "the runtime modules were not found"
    offenders = [
        str(path.relative_to(ROOT)) for path, package in files if imports_testing(path.read_text("utf-8"), package)
    ]
    assert not offenders, f"imported from a runtime path: {offenders}"


def test_importing_every_runtime_module_loads_no_testing_module():
    """The check above reads the source; this one runs it: whatever the runtime modules import, by whatever means, the
    testing package is not among the loaded modules."""
    names = sorted(
        ".".join(path.relative_to(SRC).with_suffix("").parts).removesuffix(".__init__")
        for path, _ in runtime_files()
        if path.is_relative_to(SRC) and path.name != "__main__.py"
    )
    program = (
        "import importlib, sys\n"
        f"for name in {names!r}:\n    importlib.import_module(name)\n"
        f"print(sorted(m for m in sys.modules if m == {TESTING!r} or m.startswith({TESTING + '.'!r})))\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", program], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip() == "[]", f"loaded by importing the runtime modules: {done.stdout.strip()}"


@pytest.mark.parametrize(
    ("source", "package"),
    [
        ("import expcore_onedata.testing\n", ""),
        ("import expcore_onedata.testing.fake_data as fake\n", ""),
        ("from expcore_onedata.testing import FakeData\n", ""),
        ("from expcore_onedata.testing.world import Ticker\n", ""),
        ("from expcore_onedata import testing\n", ""),
        ("from expcore_onedata import health, testing as fakes\n", ""),
        ("def late():\n    from expcore_onedata.testing import FakeData\n", ""),
        ("from . import testing\n", "expcore_onedata"),
        ("from .testing import Ticker\n", "expcore_onedata"),
        ("from .testing.world import Ticker\n", "expcore_onedata"),
        ("from .. import testing\n", "expcore_onedata.sandbox"),
        ("from ..testing import Ticker\n", "expcore_onedata.sandbox"),
        ("import importlib\nimportlib.import_module('expcore_onedata.testing')\n", ""),
        ("import importlib\nimportlib.import_module(name='expcore_onedata.testing.world')\n", ""),
        ("import importlib\nimportlib.import_module('.testing', 'expcore_onedata')\n", ""),
        ("import importlib\nimportlib.import_module('..testing', package='expcore_onedata.sandbox')\n", ""),
        ("__import__('expcore_onedata.testing')\n", ""),
        ("from importlib import import_module as load\nload('expcore_onedata.testing')\n", ""),
        ("import importlib as il\nil.import_module(name='expcore_onedata.testing.fake_data')\n", ""),
    ],
)
def test_planted_import_of_the_testing_package_is_found(source, package):
    assert imports_testing(source, package), "the boundary test did not notice a planted import of the testing package"


@pytest.mark.parametrize(
    ("source", "package"),
    [
        ("import expcore_onedata\n", ""),
        ("from expcore_onedata import tokens\n", ""),
        ("from expcore_onedata.sandbox import gate\n", ""),
        ("import expcore_onedata.testing_helpers\n", ""),
        ("from . import tokens\n", "expcore_onedata"),
        ("from .testing_notes import text\n", "expcore_onedata"),
        ("from .. import gate\n", "expcore_onedata.sandbox"),
        ("import importlib\nimportlib.import_module('expcore_onedata.health')\n", ""),
        ("import importlib\nimportlib.import_module('.health', 'expcore_onedata')\n", ""),
        ("NAME = 'expcore_onedata.testing'\n", ""),
    ],
)
def test_other_imports_are_not_taken_for_the_testing_package(source, package):
    assert not imports_testing(source, package)


def test_the_testing_package_imports_no_pytest_and_no_test_tree():
    modules = sorted((SRC / "expcore_onedata" / "testing").rglob("*.py"))
    assert len(modules) > 5, "the testing package was not found"
    forbidden = {"pytest", "_pytest", "tests", "conftest"}
    found = {
        str(path.relative_to(ROOT)): hit
        for path in modules
        if (hit := imported_roots(path.read_text(encoding="utf-8")) & forbidden)
    }
    assert not found, f"the testing package imports test machinery: {found}"
    for source, root in [
        ("import pytest\n", "pytest"),
        ("from tests.support import x\n", "tests"),
        ("from fake_data import X\n", "fake_data"),
    ]:
        assert violations(source, "expcore_onedata") == [root]
