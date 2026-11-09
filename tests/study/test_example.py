"""The example study as the installed core carries it: `python -m expcore example DEST` writes it out."""

import tomllib
from pathlib import Path

import pytest

from expcore.cli import main
from expcore.study.example import ExampleError, example_source, write_example

ROOT = Path(__file__).resolve().parents[2]


def test_the_example_is_written_into_a_new_directory_with_every_file_and_without_what_its_runs_left(tmp_path):
    source = example_source()
    (source / "out").mkdir(exist_ok=True)  # what running the example in place leaves (git ignores it)
    leftover = source / "out" / "left-by-a-run.txt"
    leftover.write_text("x", encoding="utf-8")
    try:
        written = write_example(tmp_path / "study")
    finally:
        leftover.unlink()
    names = sorted(p.relative_to(tmp_path / "study").as_posix() for p in written)
    expected = sorted(p.name for p in source.iterdir() if p.is_file())
    assert names == expected
    assert "plugin.py" in names
    assert "package.toml" in names
    assert not (tmp_path / "study" / "out").exists()
    assert (tmp_path / "study" / "plugin.py").read_bytes() == (source / "plugin.py").read_bytes()


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
