"""Shipped code parses on the oldest Python the distribution supports (K08).

CI runs the suite under that version, so there `compile` is the real check; under a newer interpreter the parse with
`feature_version` refuses most newer syntax as well.
"""

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLDEST = (3, 12)
SHIPPED = ("src", "examples", "scripts")


def shipped_python() -> list[Path]:
    return sorted(p for top in SHIPPED for p in (ROOT / top).rglob("*.py") if "__pycache__" not in p.parts)


def test_k08_the_distribution_and_the_linter_name_the_same_oldest_version():
    """K08: shipped code parses on the oldest supported Python, so the version the distribution accepts and the one
    the linter writes for are the same, and it is the one this test parses for."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["requires-python"] == ">={}.{}".format(*OLDEST)
    assert project["tool"]["ruff"]["target-version"] == "py{}{}".format(*OLDEST)


def test_k08_every_shipped_python_file_parses_as_the_oldest_supported_version():
    """K08: shipped code parses on the oldest supported Python: both packages, the example study and the scripts."""
    files = shipped_python()
    assert len(files) > 100  # the packages are there, so the pass is not over nothing
    for path in files:
        text = path.read_text(encoding="utf-8")
        ast.parse(text, filename=str(path), feature_version=OLDEST)
        compile(text, str(path), "exec", dont_inherit=True)
