"""The side files a study declares in study.toml (`[[study.side_files]]`): read when the study loads, validated with
errors that name the file and the key, and part of the campaign fingerprint through study.toml's hash."""

import shutil
from pathlib import Path

import pytest

from expcore.config.loading import ConfigError
from expcore.replay.side_files import SideFile
from expcore.study.config import load_study, read_study_identity
from expcore.study.pipeline import fingerprint_of
from expcore.study.plugin import StudyContext, load_plugin

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
DECLARED = '[[study.side_files]]\npattern = "{pattern}"\nhash_field = "{field}"\n'


@pytest.fixture
def study_dir(tmp_path) -> Path:
    copy = tmp_path / "study"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    return copy


def declare(study: Path, text: str) -> None:
    """Append a declaration to the study's study.toml."""
    path = study / "study.toml"
    path.write_text(path.read_text(encoding="utf-8") + "\n" + text, encoding="utf-8")


def test_r14_a_declaration_is_read_when_the_study_loads_and_by_the_checks_reading_of_study_toml(study_dir):
    """R14: both readings of study.toml, the whole study and the `[study]` table the replay check reads, hold the
    declared side files in the order the file gives; a study that declares none holds none."""
    assert load_study(study_dir).side_files == read_study_identity(study_dir).side_files == ()
    declare(study_dir, DECLARED.format(pattern="ranges_log.json.gz", field="instruments.ranges.side_sha256"))
    declare(study_dir, DECLARED.format(pattern="second_log.txt", field="instruments.second.sha256"))
    expected = (
        SideFile("ranges_log.json.gz", "instruments.ranges.side_sha256"),
        SideFile("second_log.txt", "instruments.second.sha256"),
    )
    assert load_study(study_dir).side_files == expected
    assert read_study_identity(study_dir).side_files == expected


@pytest.mark.parametrize(
    ("text", "fragments"),
    [
        ('[[study.side_files]]\nhash_field = "instruments.ranges.sha256"\n', ["key 'pattern'", "is required"]),
        ('[[study.side_files]]\npattern = "ranges.gz"\n', ["key 'hash_field'", "is required"]),
        (
            '[[study.side_files]]\npattern = "ranges.gz"\nhash_field = "instruments.ranges.sha256"\nglob = "x"\n',
            ["key 'glob'", "is not a known key"],
        ),
        (DECLARED.format(pattern="runs/ranges.gz", field="instruments.ranges.sha256"), ["key 'pattern'", "not a path"]),
        (DECLARED.format(pattern="*.json.gz", field="instruments.ranges.sha256"), ["key 'pattern'", "not a glob"]),
        (DECLARED.format(pattern="ranges[0].gz", field="instruments.ranges.sha256"), ["key 'pattern'", "not a glob"]),
        (DECLARED.format(pattern=".ranges.gz", field="instruments.ranges.sha256"), ["key 'pattern'", "a dot"]),
        (
            DECLARED.format(pattern="replay-check.json", field="instruments.ranges.sha256"),
            ["key 'pattern'", "the replay check writes"],
        ),
        (
            DECLARED.format(pattern="campaign-snapshot.sqlite", field="instruments.ranges.sha256"),
            ["key 'pattern'", "the replay check writes"],
        ),
        (
            DECLARED.format(pattern="attempt-marker.json", field="instruments.ranges.sha256"),
            ["key 'pattern'", "the replay check writes"],
        ),
        (
            DECLARED.format(pattern=f"{'ab' * 32}.1.json", field="instruments.ranges.sha256"),
            ["key 'pattern'", "the replay check writes"],
        ),
        (DECLARED.format(pattern="ranges.gz", field="instruments..sha256"), ["key 'hash_field'", "joined by dots"]),
        (DECLARED.format(pattern="ranges.gz", field="instruments ranges"), ["key 'hash_field'", "joined by dots"]),
        (
            DECLARED.format(pattern="ranges.gz", field="instruments.ranges.sha256")
            + DECLARED.format(pattern="ranges.gz", field="instruments.other.sha256"),
            ["number 2", "key 'pattern'", "an earlier"],
        ),
        (
            DECLARED.format(pattern="ranges.gz", field="instruments.ranges.sha256")
            + DECLARED.format(pattern="other.gz", field="instruments.ranges.sha256"),
            ["number 2", "key 'hash_field'", "an earlier"],
        ),
    ],
    ids=[
        "no-pattern",
        "no-field",
        "unknown-key",
        "a-path",
        "a-star",
        "a-bracket",
        "a-dot-file",
        "the-check",
        "the-snapshot",
        "a-marker",
        "a-record-name",
        "an-empty-part",
        "a-space",
        "a-pattern-twice",
        "a-field-twice",
    ],
)
def test_r14_a_declaration_that_cannot_be_paired_is_refused_naming_the_file_and_the_key(study_dir, text, fragments):
    """R14: a missing or unknown key, a pattern that is a path, a glob, a dot file or a name the campaign or the check
    writes, a field that is not record keys joined by dots, and a pattern or field declared twice are refused when the
    study loads, by both readings of study.toml, naming the file and the key."""
    declare(study_dir, text)
    for read in (load_study, read_study_identity):
        with pytest.raises(ConfigError) as caught:
            read(study_dir)
        message = str(caught.value)
        for fragment in ("study.toml", "[[study.side_files]]", *fragments):
            assert fragment in message, message


def test_r14_side_files_that_are_not_tables_are_refused_naming_the_key(study_dir):
    """R14: `side_files` must be an array of tables; anything else is refused naming the file and the key."""
    path = study_dir / "study.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace('plugin = "plugin.py"', 'plugin = "plugin.py"\nside_files = ["x"]'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"study\.toml: \[study\]: key 'side_files' must be an array of tables"):
        load_study(study_dir)


def test_r14_a_declaration_changes_the_campaign_fingerprint_so_a_resume_under_another_is_refused(study_dir):
    """R14: the declaration is in study.toml, whose hash is in the campaign fingerprint (C06), so a campaign started
    under one declaration is not resumed under another."""
    code = {"commit": "a" * 40, "tree_sha256": "b" * 64, "dirty": False}

    def fingerprint() -> dict:
        study = load_study(study_dir)
        return fingerprint_of(study, load_plugin(StudyContext(study, code)))

    before = fingerprint()
    declare(study_dir, DECLARED.format(pattern="ranges_log.json.gz", field="instruments.ranges.side_sha256"))
    after = fingerprint()
    assert before != after
    assert before["configs"]["study.toml"] != after["configs"]["study.toml"]
