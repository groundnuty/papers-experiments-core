"""Re-scoring end to end (R13): scores are derived from the records alone, with no model and no platform.

The pipeline keeps no score that it reads back. The records on disk hold an empty `scores` section (a record is never
written after its replay check, since that would change its hash), and `out/scores.json` is written by `analyze` and
read by nothing. `analyze` scores every verified record in memory with the plugin's scorers, so it is the way to
re-score: there is no separate command. These tests run it with the model backends and the platform made unusable.

The campaign is built once per module; each test works on a copy of it.
"""

import json
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from study_support import HEAD, Campaign, clean_environment, cli, copy_campaign, finish, make_copy, table

from expcore.provenance.codestamp import write_stamp
from expcore.records.record import load_record
from expcore.study.config import load_study

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

DERIVED = ("scores.json", "analysis.json", "export.json", "report.md")  # what `analyze` and `report` write under out/
ANALYZED = ("scores.json", "analysis.json", "export.json")  # what `analyze` alone must have written
BUMPED = "tools-effect-on-rounds"  # the only contrast that reads the outcome the new scorer changes
FIRST = 'version = "1"'
SECOND = 'version = "2"'
ROUNDS = 'float(record.totals["rounds"]),'
TWICE = '2 * float(record.totals["rounds"]),'  # version 2 counts a round per call and per reply


class Unavailable(AssertionError):
    """Raised by anything a re-scoring must not reach: a model backend, the scheduler, a lease on the platform."""


def refuse(*args: Any, **kwargs: Any) -> None:
    raise Unavailable("re-scoring reached a model or the platform")


# (module, name) of everything that makes a model call or touches the platform: a backend is built, a run is executed,
# the scheduler dispatches, a lease is taken. The tool functions and the plugin's health check are reachable only
# through these.
TRIPWIRES = [
    ("expcore.models.factory", "build_backend"),
    ("expcore.campaign.executor", "build_backend"),
    ("expcore.campaign.executor", "record_run"),
    ("expcore.models.adapters", "StubAdapter"),
    ("expcore.models.adapters", "OpenAICompatAdapter"),
    ("expcore.models.adapters", "AnthropicAdapter"),
    ("expcore.study.pipeline", "Scheduler"),
    ("expcore.study.pipeline", "LeasePool"),
    ("socket", "create_connection"),
]


@contextmanager
def offline() -> Iterator[None]:
    """No model backend, no scheduler, no lease and no network connection for the duration of the block."""
    with pytest.MonkeyPatch.context() as patch:
        for module, name in TRIPWIRES:
            patch.setattr(f"{module}.{name}", refuse)
        patch.setattr(socket.socket, "connect", refuse)
        yield


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory, clean_environment) -> Campaign:
    """The example, run to the end through the command line: run, check, analyze, report."""
    campaign = Campaign(make_copy(tmp_path_factory.mktemp("rescoring"), "study"))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


def out_file(campaign: Campaign, name: str) -> Path:
    return campaign.root / "out" / name


def rounds_cells(report: str) -> list[str]:
    return table(report, "## Exploratory contrasts")[BUMPED]


def interval(cell: str) -> list[float]:
    return [float(end) for end in cell.strip("[]").split(", ")]


def test_r13_the_scores_are_kept_in_one_file_that_nothing_reads_back_and_never_in_a_record(pipeline):
    """R13: the only stored scores are in scores.json; the records hold none, so re-scoring starts from the records."""
    out = pipeline.root / "out"
    holding = sorted(p.name for p in out.rglob("*") if p.is_file() and b'"verdict"' in p.read_bytes())
    assert holding == ["scores.json"]
    records = [p for p in pipeline.runs.rglob("*.json") if p.name not in ("attempt-marker.json", "replay-check.json")]
    assert len(records) == 22
    assert {json.dumps(load_record(p).scores) for p in records} == {"{}"}


def test_r13_the_tripwires_stop_a_command_that_would_run_a_model_or_use_the_platform(pipeline, tmp_path):
    """R13: the offline block is live: `run` and each of the things it would use raise when asked for."""
    fresh = Campaign(make_copy(tmp_path, "fresh"))
    with offline():
        with pytest.raises(Unavailable):
            cli({}, "run", str(fresh.root))
        for module, name in TRIPWIRES:
            target = __import__(module, fromlist=[name])
            with pytest.raises(Unavailable):
                getattr(target, name)()


def test_r13_without_a_stored_score_analyze_and_report_derive_them_from_the_records_with_no_model_or_platform(
    pipeline, clean_environment, tmp_path
):
    """R13: with every output removed and the backends and the platform unusable, the report is the same."""
    copy = copy_campaign(pipeline, tmp_path, "rescored")
    before = {name: out_file(copy, name).read_bytes() for name in DERIVED}
    for name in DERIVED:
        out_file(copy, name).unlink()
    with offline():
        analyze, _ = cli(clean_environment, "analyze", str(copy.root))
        after_analyze = {name: out_file(copy, name).read_bytes() for name in ANALYZED if out_file(copy, name).exists()}
        report, _ = cli(clean_environment, "report", str(copy.root))
    assert (analyze, report) == (0, 0)
    assert after_analyze == {name: before[name] for name in ANALYZED}  # analyze wrote them, before report ran
    assert {name: out_file(copy, name).read_bytes() for name in DERIVED} == before  # scores.json and the report too
    assert len(json.loads(before["scores.json"])) == 22


@pytest.fixture(scope="module")
def bumped(pipeline, tmp_path_factory, clean_environment) -> dict[str, Any]:
    """A copy whose scorer is version 2, which counts twice the rounds; re-stamped, re-checked, analysed, reported."""
    copy = copy_campaign(pipeline, tmp_path_factory.mktemp("bumped"), "study")
    plugin = copy.root / "plugin.py"
    source = plugin.read_text(encoding="utf-8")
    assert source.count(FIRST) == 1
    assert source.count(ROUNDS) == 1
    plugin.write_text(source.replace(FIRST, SECOND).replace(ROUNDS, TWICE), encoding="utf-8")
    write_stamp(copy.root, HEAD, load_study(copy.root).scope)  # the scorer is code the identity covers
    steps = {"check": str(copy.runs), "analyze": str(copy.root), "report": str(copy.root)}
    with offline():
        stale = cli(clean_environment, "analyze", str(copy.root))  # refused: the check is of the old scorer's code
        codes, analysed = {}, {}
        for name, target in steps.items():
            codes[name] = cli(clean_environment, name, target)[0]
            if name == "analyze":  # read what analyze wrote before report can write it again
                analysed = {n: out_file(copy, n).read_bytes() for n in ANALYZED if out_file(copy, n).exists()}
    return {"campaign": copy, "stale": stale, "codes": codes, "analysed": analysed}


def test_r13_a_new_scorer_version_needs_a_new_replay_check_before_the_scores_are_derived_again(bumped):
    """R13: the scorer is part of the code identity, so analyze refuses until the check is made again, offline."""
    code, errors = bumped["stale"]
    assert code == 1
    assert "the check was made under code tree" in errors
    assert f"python -m expcore check {bumped['campaign'].runs}" in errors
    assert bumped["codes"] == {"check": 0, "analyze": 0, "report": 0}


def test_r13_a_new_scorer_version_changes_one_outcome_in_the_known_way_and_is_recorded_with_every_score(
    pipeline, bumped
):
    """R13: version 2 doubles the rounds of every scored run and nothing else; a run with no rounds still has none."""
    old = json.loads(out_file(pipeline, "scores.json").read_text(encoding="utf-8"))
    assert set(bumped["analysed"]) == set(ANALYZED)
    new = json.loads(bumped["analysed"]["scores.json"].decode("utf-8"))
    assert old.keys() == new.keys()
    assert len(new) == 22
    for key, held in new.items():
        assert (old[key]["tiny"]["version"], held["tiny"]["version"]) == ("1", "2")
        was, now = old[key]["tiny"]["result"], held["tiny"]["result"]
        doubled = None if was["outcomes"]["rounds"] is None else 2 * was["outcomes"]["rounds"]
        assert now == {**was, "outcomes": {**was["outcomes"], "rounds": doubled}}
    assert any(held["tiny"]["result"]["outcomes"]["rounds"] for held in new.values())


def test_r13_after_a_new_scorer_version_the_report_changes_in_that_outcomes_contrast_and_in_what_records_the_change(
    pipeline, bumped
):
    """R13: the report differs in the row of the contrast on that outcome, the scorer line and the code identity."""
    campaign = bumped["campaign"]
    old, new = pipeline.report.splitlines(), out_file(campaign, "report.md").read_text(encoding="utf-8").splitlines()
    assert len(old) == len(new)
    changed = [(a, b) for a, b in zip(old, new, strict=True) if a != b]
    prefixes = ("- Code:", "- Scorers:", f"| {BUMPED} |")
    assert [next(p for p in prefixes if a.startswith(p)) for a, _ in changed] == list(prefixes)
    (code_old, code_new), (scorers_old, scorers_new), _ = changed
    stamps = [json.loads((c.root / ".code-stamp.json").read_text(encoding="utf-8")) for c in (pipeline, campaign)]
    assert stamps[0]["tree_sha256"] != stamps[1]["tree_sha256"]
    assert stamps[0]["head"] == stamps[1]["head"]
    assert code_old.replace(stamps[0]["tree_sha256"], stamps[1]["tree_sha256"]) == code_new  # the tree, nothing else
    assert (scorers_old, scorers_new) == ("- Scorers: tiny `1`", "- Scorers: tiny `2`")
    before, after = rounds_cells(pipeline.report), rounds_cells(out_file(campaign, "report.md").read_text("utf-8"))
    assert float(after[3]) == 2 * float(before[3])  # the estimate; the interval follows
    assert interval(after[4]) == [2 * end for end in interval(before[4])]
    assert [c for i, c in enumerate(after) if i not in (3, 4)] == [c for i, c in enumerate(before) if i not in (3, 4)]
