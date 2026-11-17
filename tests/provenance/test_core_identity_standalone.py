# Provenance: new
"""`hatch_build.py` loads `core_identity.py` standalone, in an isolated build environment where `expcore` is not
importable, so the module may import nothing of the package (O81: a launch-time git read fix once imported
`gitobjects` there and broke every build of the core)."""

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[2] / "src" / "expcore" / "provenance" / "core_identity.py"


def test_core_identity_imports_nothing_of_the_package():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert not imported & {"expcore", "expcore_onedata"}, sorted(imported)


def test_core_identity_loads_standalone_in_an_isolated_interpreter():
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('_standalone_identity', {str(MODULE)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = module\n"  # as hatch_build.py does: dataclasses look the module up
        "spec.loader.exec_module(module)\n"
        "assert module.git_env()['GIT_OPTIONAL_LOCKS'] == '0'\n"
    )
    done = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_the_spec_loader_finds_the_module():
    assert importlib.util.spec_from_file_location("_x", MODULE) is not None
