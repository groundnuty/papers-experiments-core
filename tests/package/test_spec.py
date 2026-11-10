"""The inclusion list of a replication package: only what it names goes in, and a list that names nothing fails."""

import pytest

from expcore.campaign.defect_log import write_defect
from expcore.loop.outcomes import harness_defect_of
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


def test_l08_the_harness_defect_log_is_never_selected_whatever_the_patterns_say(tree):
    """L08: the redacted tracebacks stay on the author's machine: "out", "out/logs" or "**/*.log" never take them."""
    try:
        raise ValueError("a crash")
    except ValueError as error:
        defect = harness_defect_of(error)
        log = write_defect(tree / "out", error, defect, stamp="T", run_key="k", attempt=1, redactor=Plain)
    assert log.is_file()
    assert (log.parent / ".gitignore").is_file()
    for pattern in ("out", "out/**"):
        assert not [path for path in select(tree, spec_with(tree, [pattern])) if path.startswith("out/logs")]
    for pattern in ("out/logs", "**/*.log", "out/logs/.gitignore"):  # a pattern that names only the logs names nothing
        with pytest.raises(PackageError, match="selects no file"):
            select(tree, spec_with(tree, ["study.toml", pattern]))


class Plain:
    """A redactor that changes nothing."""

    def text(self, value):
        return value


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
    write(tree, {"SHA256SUMS": "x\n"})
    with pytest.raises(PackageError, match="SHA256SUMS is a name"):
        select(tree, spec_with(tree, ["SHA256SUMS"]))


def test_k10_no_file_under_wheels_can_come_from_the_study(tree):
    """K10: the wheels the package's `make check` installs are the ones the build vendors, so a study's own file under
    wheels/ is refused, wherever the pattern that selects it points."""
    write(tree, {"wheels/requirements.txt": "evil==1\n", "wheels/evil-1-py3-none-any.whl": "x"})
    for pattern in ("wheels", "wheels/requirements.txt", "**/*.whl"):
        with pytest.raises(PackageError, match=r"wheels/.* is a name"):
            select(tree, spec_with(tree, [pattern]))


@pytest.mark.parametrize("name", ["GNUmakefile", "makefile", "Makefile"])
def test_k07_no_file_make_reads_at_the_package_root_can_come_from_the_study(tree, name):
    """K07: `make check` must run the package's own check, so a file GNU Make would read before or instead of the
    generated Makefile (GNUmakefile, makefile) is refused like the Makefile itself; one in a subdirectory is not."""
    write(tree, {name: "check:\n\ttrue\n", f"sub/{name}": "all:\n"})
    with pytest.raises(PackageError, match=f"{name} is a name"):
        select(tree, spec_with(tree, [name]))
    assert select(tree, spec_with(tree, [f"sub/{name}"])) == [f"sub/{name}"]


def opaque_spec(root, entries: str, include=("study.toml", "notes.md")):
    quoted = ", ".join(f'"{pattern}"' for pattern in include)
    text = f'[package]\nname = "pkg-one"\ninclude = [{quoted}]\n\n{entries}'
    (root / "package.toml").write_text(text, encoding="utf-8")
    return load_spec(root)


def test_k03_an_opaque_entry_attests_one_included_file_by_its_hash_with_a_reason(tree):
    """K03: the only way past the scan is an explicit attestation: a path, its SHA-256 and why it was let through."""
    entry = f'[[opaque]]\npath = "notes.md"\nsha256 = "{"ab" * 32}"\nreason = "read by hand: a cover page"\n'
    spec = opaque_spec(tree, entry)
    assert {path: (o.sha256, o.reason) for path, o in spec.opaque.items()} == {
        "notes.md": ("ab" * 32, "read by hand: a cover page")
    }
    assert select(tree, spec) == ["notes.md", "study.toml"]


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        ('[[opaque]]\npath = "other.pdf"\nsha256 = "{h}"\nreason = "r"\n', "does not include"),
        ('[[opaque]]\npath = "notes.md"\nsha256 = "ABC"\nreason = "r"\n', "sha256"),
        ('[[opaque]]\npath = "notes.md"\nsha256 = "{h}"\nreason = " "\n', "reason"),
        ('[[opaque]]\npath = "notes.md"\nsha256 = "{h}"\n', "reason"),
        ('[[opaque]]\npath = "*.md"\nsha256 = "{h}"\nreason = "r"\n', "one file"),
        ('[[opaque]]\npath = "notes.md"\nsha256 = "{h}"\nreason = "r"\n' * 2, "twice"),
    ],
)
def test_k03_an_opaque_entry_that_is_not_one_included_file_with_a_hash_and_a_reason_is_refused(tree, entry, why):
    """K03: an attestation names exactly one file the package includes, its full SHA-256 and a reason."""
    with pytest.raises(PackageError, match=why):
        select(tree, opaque_spec(tree, entry.format(h="ab" * 32)))
