"""The changelog's list of guaranteed regressions agrees with the tests on this tree, in both directions.

A test backs a regression ID when it is a function whose name starts with `test`, in a file `tests/**/test_*.py`
outside `tests/support/`, and its docstring starts with the ID (`B01: ...`). Module docstrings do not count.
"""

import ast
import collections
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
ID = re.compile(r"([A-Z]\d{2})\b")
ROW = re.compile(r"^\| ([A-Z]\d{2}) \| (.+) \| (.+) \|$")


def backed_ids() -> dict[str, set[str]]:
    """Every regression ID with a test on the tree, and the test files that hold one."""
    found: dict[str, set[str]] = collections.defaultdict(set)
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        if "support" in path.relative_to(ROOT / "tests").parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test"):
                match = ID.match(ast.get_docstring(node) or "")
                if match:
                    found[match.group(1)].add(path.relative_to(ROOT).as_posix())
    return found


def release(version: str) -> str:
    """The changelog entry of one version: from its heading to the next one."""
    text = CHANGELOG.read_text(encoding="utf-8")
    start = text.index(f"\n## {version}\n")
    following = text.find("\n## ", start + 1)
    return text[start : following if following != -1 else len(text)]


def listed(version: str) -> dict[str, list[str]]:
    """The guaranteed IDs of a release with the test files each row names."""
    rows: dict[str, list[str]] = {}
    for line in release(version).split("### Regressions guaranteed", 1)[1].splitlines():
        match = ROW.match(line)
        if match:
            assert match.group(1) not in rows, f"{match.group(1)} is listed twice"
            rows[match.group(1)] = re.findall(r"`([^`]+)`", match.group(3))
    return rows


def current_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def test_the_changelog_has_an_entry_for_the_version_the_distribution_carries():
    assert f"\n## {current_version()}\n" in CHANGELOG.read_text(encoding="utf-8")


def test_every_id_the_changelog_lists_has_a_test_whose_docstring_starts_with_it_in_each_file_named():
    rows, backed = listed(current_version()), backed_ids()
    assert rows, "the release lists no guaranteed regression"
    for regression, files in rows.items():
        assert files, f"{regression} names no test file"
        for name in files:
            assert (ROOT / name).is_file(), f"{regression}: {name} does not exist"
            assert name in backed.get(regression, set()), f"{regression}: {name} has no test whose docstring starts so"


def test_every_id_that_has_such_a_test_on_the_tree_is_in_the_changelog():
    missing = sorted(set(backed_ids()) - set(listed(current_version())))
    assert not missing, f"tested on this tree but missing from the changelog: {', '.join(missing)}"


def test_every_listed_id_is_a_regression_of_the_list_with_its_title():
    titles = dict(
        re.findall(r"^\*\*([A-Z]\d{2})\. (.+?)\*\*", (ROOT / "docs" / "regressions.md").read_text("utf-8"), re.M)
    )
    for line in release(current_version()).splitlines():
        match = ROW.match(line)
        if match:
            assert match.group(1) in titles, f"{match.group(1)} is not in docs/regressions.md"
            assert match.group(2) == titles[match.group(1)].rstrip(".").replace("|", "\\|")


def test_every_id_in_the_regression_list_is_guaranteed_or_named_as_not_in_this_release():
    """Every ID docs/regressions.md defines is either in the release's table of guaranteed regressions or named, in
    parentheses, on its "Not in this release" line, so no regression is silently left out of a release."""
    defined = re.findall(r"^\*\*([A-Z]\d{2})\. ", (ROOT / "docs" / "regressions.md").read_text(encoding="utf-8"), re.M)
    assert defined, "no regression IDs found in docs/regressions.md"
    entry = release(current_version())
    not_in = next((line for line in entry.splitlines() if line.startswith("- Not in this release:")), "")
    deferred = set(re.findall(r"\b([A-Z]\d{2})\b", not_in))
    unaccounted = [i for i in defined if i not in listed(current_version()) and i not in deferred]
    assert not unaccounted, f"neither guaranteed nor named as not in this release: {', '.join(unaccounted)}"
    both = sorted(set(listed(current_version())) & deferred)
    assert not both, f"both guaranteed and named as not in this release: {', '.join(both)}"
