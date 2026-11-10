"""The example study as the installed core carries it: `python -m expcore example DEST` writes it out."""

import json
import shutil
import tomllib
from pathlib import Path

import pytest

from expcore.cli import main
from expcore.study import example
from expcore.study.example import DocsPlace, ExampleError, example_source, readme_text, write_example

ROOT = Path(__file__).resolve().parents[2]


def test_the_example_is_written_into_a_new_directory_with_every_file_and_without_what_its_runs_left(
    tmp_path, monkeypatch
):
    shipped = sorted(p.name for p in example_source().iterdir() if p.is_file())
    assert {"plugin.py", "package.toml", "study.toml"} <= set(shipped)
    source = Path(shutil.copytree(example_source(), tmp_path / "source", ignore=shutil.ignore_patterns("out")))
    for leftover in ("out/runs/x.json", "__pycache__/plugin.cpython-312.pyc"):  # what running it in place leaves
        (source / leftover).parent.mkdir(parents=True, exist_ok=True)
        (source / leftover).write_text("x", encoding="utf-8")
    monkeypatch.setattr(example, "example_source", lambda: source)
    written = write_example(tmp_path / "study")
    assert sorted(p.relative_to(tmp_path / "study").as_posix() for p in written) == shipped
    assert not (tmp_path / "study" / "out").exists()
    assert not (tmp_path / "study" / "__pycache__").exists()
    assert (tmp_path / "study" / "plugin.py").read_bytes() == (source / "plugin.py").read_bytes()


class InstalledCore:
    """A stand-in for the core's installed distribution: its version and its `direct_url.json`, if any."""

    def __init__(self, version: str, direct_url: dict | None = None):
        self.version = version
        self.direct_url = direct_url

    def read_text(self, name: str) -> str | None:
        return json.dumps(self.direct_url) if name == "direct_url.json" and self.direct_url is not None else None


COMMIT = "0123456789abcdef0123456789abcdef01234567"
WEB_DOCS = f"https://github.com/example-owner/example-core/blob/{COMMIT}/docs/"


def test_the_readme_written_out_points_at_the_installed_cores_docs_and_leads_with_clean_commands(tmp_path):
    source = (example_source() / "README.md").read_text(encoding="utf-8")
    assert "](../../docs/interfaces.md)" in source  # in the checkout, the links are relative
    write_example(tmp_path / "study")
    readme = (tmp_path / "study" / "README.md").read_text(encoding="utf-8")
    assert "../../docs" not in readme
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    # an editable install records no repository URL and no commit: the pages are named by path, at the version's tag
    assert f"`docs/interfaces.md` (in the core's repository, at tag v{version})" in readme
    assert f"section 8 (`docs/study-guide.md` in the core's repository, at tag v{version})" in readme
    first_block = readme.split("```")[1]
    assert "uv run python -m expcore run my-study" in first_block
    assert "--allow-dirty" not in first_block


def test_a_core_installed_from_a_github_repository_gets_links_to_its_docs_at_that_commit_and_nothing_else_changes():
    source = (example_source() / "README.md").read_text(encoding="utf-8")
    readme = readme_text(source, DocsPlace(WEB_DOCS, f"at commit {COMMIT}"))
    assert f"]({WEB_DOCS}interfaces.md)" in readme
    assert f"]({WEB_DOCS}study-guide.md#8-ship-a-replication-package)" in readme
    assert readme.replace(WEB_DOCS, "../../docs/") == source


def test_the_docs_are_found_at_the_commit_the_core_was_installed_from_else_its_tag_else_the_default_branch(
    monkeypatch,
):
    def git(url: str) -> dict:
        return {"url": url, "vcs_info": {"vcs": "git", "commit_id": COMMIT}}

    installs = {  # name: (the URL it was installed from, the web docs found for it)
        "github": ("https://github.com/example-owner/example-core.git", WEB_DOCS),
        "credentials": ("https://x-token@github.com/example-owner/example-core/", WEB_DOCS),
        "local clone": ("file:///srv/clones/example-core", None),
        "other host": ("https://git.example.invalid/example-core.git", None),
        "plain http": ("http://github.com/example-owner/example-core.git", None),
    }
    for name, (url, web) in installs.items():
        dist = InstalledCore("0.1.0", git(url))
        monkeypatch.setattr(example.metadata, "distribution", lambda _name, dist=dist: dist)
        assert example.docs_place() == DocsPlace(web, f"at commit {COMMIT}"), name
    for dist, where in (
        (InstalledCore("0.1.0+local.7"), "at tag v0.1.0"),  # from an index, or an editable checkout
        (InstalledCore("0.2.0", {"vcs_info": {"commit_id": "not a commit"}}), "at tag v0.2.0"),
    ):
        monkeypatch.setattr(example.metadata, "distribution", lambda _name, dist=dist: dist)
        assert example.docs_place() == DocsPlace(None, where)

    def missing(name):
        raise example.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(example.metadata, "distribution", missing)
    assert example.docs_place() == DocsPlace(None, "on its default branch")


def test_the_example_is_never_written_over_a_directory_that_holds_anything(tmp_path):
    (tmp_path / "study").mkdir()
    (tmp_path / "study" / "mine.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ExampleError, match="not an empty directory"):
        write_example(tmp_path / "study")
    assert main(["example", str(tmp_path / "study")]) == 1
    assert (tmp_path / "study" / "mine.txt").read_text(encoding="utf-8") == "keep"


def test_the_command_writes_the_example_and_says_how_to_run_it(tmp_path, capsys):
    assert main(["example", str(tmp_path / "fresh")]) == 0
    assert f"python -m expcore run {tmp_path / 'fresh'}" in capsys.readouterr().out
    assert (tmp_path / "fresh" / "study.toml").is_file()


def test_the_wheel_carries_the_example_and_the_public_token_hashes_and_leaves_out_ignored_files():
    """An installed core has no checkout to read from, so the wheel itself must carry both (the build config)."""
    wheel = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"]["build"]["targets"][
        "wheel"
    ]
    assert "examples/tiny_study" in wheel["only-include"]
    assert wheel["sources"]["examples"] == "expcore/examples"  # where example_source looks in an installed core
    assert wheel["force-include"]["tests/data/study_tokens.sha256"] == "expcore/provenance/study_tokens.sha256"
    assert "out/" in (ROOT / "examples" / "tiny_study" / ".gitignore").read_text(encoding="utf-8").split()
