"""The inclusion list of a replication package: only what it names goes in, and a list that names nothing fails."""

import pytest

from expcore.package.spec import PackageError, load_spec, pattern_regex, select


def write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def spec_with(root, include, name="pkg-one"):
    quoted = ", ".join(f'"{pattern}"' for pattern in include)
    (root / "package.toml").write_text(f'[package]\nname = "{name}"\ninclude = [{quoted}]\n', encoding="utf-8")
    return load_spec(root)


@pytest.fixture
def tree(tmp_path):
    write(
        tmp_path,
        {
            "study.toml": "",
            "notes.md": "",
            "plugin.py": "",
            "out/report.md": "",
            "out/export.json": "",
            "out/state.sqlite": "",
            "out/state.sqlite-wal": "",
            "out/campaign.lock": "",
            "out/leases/slot-1": "",
            "out/runs/aa/try1/aa.1.json": "",
            "out/runs/aa/try1/__pycache__/x.pyc": "",
            "out/runs/replay-check.json": "",
            "src/pkg/mod.py": "",
            "src/pkg/mod.pyc": "",
            "src/pkg/deep/more.py": "",
        },
    )
    return tmp_path


def test_only_the_files_the_patterns_select_go_in(tree):
    chosen = select(tree, spec_with(tree, ["study.toml", "out/*.md", "src/**/*.py"]))
    assert chosen == ["out/report.md", "src/pkg/deep/more.py", "src/pkg/mod.py", "study.toml"]


def test_a_directory_takes_every_file_below_it_but_never_caches_or_the_live_campaign(tree):
    assert select(tree, spec_with(tree, ["out"])) == [
        "out/export.json",
        "out/report.md",
        "out/runs/aa/try1/aa.1.json",
        "out/runs/replay-check.json",
    ]


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("*.md", "notes.md", True),
        ("*.md", "out/report.md", False),  # one star stays within one part
        ("**/*.md", "out/report.md", True),
        ("**/*.md", "notes.md", True),  # two stars match no part too
        ("out/r?port.md", "out/report.md", True),
        ("out", "out/runs/aa/try1/aa.1.json", True),
        ("out", "outside.md", False),
        ("o*t/[ab].md", "out/a.md", False),  # brackets are plain characters, not a class
    ],
)
def test_patterns(pattern, path, matches):
    assert bool(pattern_regex(pattern).fullmatch(path)) is matches


def test_k01_a_pattern_that_selects_nothing_is_refused_naming_it(tree):
    """K01: a list that names nothing would pass for any package, so a pattern that selects no file fails the build."""
    with pytest.raises(PackageError, match=r"'docs/\*\.md' selects no file"):
        select(tree, spec_with(tree, ["study.toml", "docs/*.md"]))
    with pytest.raises(PackageError, match="selects no file"):
        select(tree, spec_with(tree, ["out/state.sqlite"]))  # the live store is never selected, so this names nothing


def test_k01_an_empty_or_missing_list_is_refused(tmp_path):
    """K01: without an explicit inclusion list there is no package."""
    with pytest.raises(PackageError, match="not found"):
        load_spec(tmp_path)
    (tmp_path / "package.toml").write_text('[package]\nname = "p"\ninclude = []\n', encoding="utf-8")
    with pytest.raises(PackageError, match="non-empty list"):
        load_spec(tmp_path)


# The last but one is a single backslash once the test writes it into TOML.
@pytest.mark.parametrize("pattern", ["/etc/hosts", "../secret", "out/../x", "a//b", "./x", "a\\\\b", ""])
def test_a_pattern_that_leaves_the_study_or_is_malformed_is_refused(tmp_path, pattern):
    with pytest.raises(PackageError, match="include"):
        spec_with(tmp_path, [pattern])


@pytest.mark.parametrize("name", ["", "../up", "with space", ".hidden"])
def test_a_name_that_is_not_a_plain_file_name_is_refused(tmp_path, name):
    with pytest.raises(PackageError, match="name"):
        spec_with(tmp_path, ["x"], name=name)


def test_a_selected_symbolic_link_is_refused_and_an_unselected_one_is_ignored(tree):
    (tree / "link.md").symlink_to(tree / "notes.md")
    assert select(tree, spec_with(tree, ["notes.md"])) == ["notes.md"]
    with pytest.raises(PackageError, match="symbolic link"):
        select(tree, spec_with(tree, ["*.md"]))


def test_a_name_the_build_generates_cannot_be_selected(tree):
    write(tree, {"Makefile": "all:\n"})
    with pytest.raises(PackageError, match="Makefile is a name the build generates"):
        select(tree, spec_with(tree, ["Makefile"]))
