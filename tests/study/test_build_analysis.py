"""R17: a study's analysis may build its plugin without the platform, and then scores with the scorers the run had.

`build(context)` needs the study's platform (the rendered federation, the site's variables), which is gone when an HPC
batch is analysed after copy-back. A plugin module may also define `build_analysis(context)`: the analysis paths
(`analyse_study`, so `analyze`, `report`, `regenerate` and a package's check) call it, and fall back to `build` without
it. It is never called for a run. The campaign fingerprint records the run plugin's `scorer_digest`, and an analysis
built by `build_analysis` is refused when its scorers' digest is another one, or when a snapshot records none. With
`--rescore` a digest that is another one is let through and recorded, as the next section tests; a snapshot that
records none is refused with it too. A package's build and check pass no flag but honour the entry the recorded
analysis holds, as the last section tests (the packages themselves, in tests/package/test_rescored_package.py).

The example is copied, and its plugin gains both builders: each notes its call in a file the test names, `build` fails
when the test says the federation is gone, and `build_analysis` gives the scorers the test asks for.
"""

import ast
import dataclasses
import json
import re
import sqlite3
from pathlib import Path

import pytest
from build_analysis_support import GONE, LOG, SCORERS, edit_analysis, with_builders
from study_support import Campaign, clean_environment, cli, copy_campaign, finish, make_copy

from expcore.campaign.shard import ShardSpec
from expcore.cli import build_parser
from expcore.config.loading import ConfigError
from expcore.study import analysis_plugin, pipeline
from expcore.study.analysis_plugin import ScorersDiffer, load_analysis_plugin, require_run_scorers
from expcore.study.config import load_study
from expcore.study.pipeline import Refused, analyse_study, output_bytes, regenerate, run_study
from expcore.study.plugin import StudyContext, StudyPlugin, load_plugin

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

ROOT = Path(__file__).resolve().parents[2]
SCANNED = (ROOT / "src", ROOT / "deploy")  # every Python file the repository ships
DIGEST = re.compile(r"[0-9a-f]{64}")


def calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").split() if log.is_file() else []


@dataclasses.dataclass
class Built:
    campaign: Campaign
    run_calls: list[str]  # the builders `run` called
    finish_calls: list[str]  # the builders `check`, `analyze` and `report` called


@pytest.fixture(scope="module")
def built(tmp_path_factory, clean_environment) -> Built:
    """The example with both builders, run to the end through the command line; what each builder was asked."""
    base = tmp_path_factory.mktemp("build-analysis")
    campaign, log = Campaign(with_builders(base, "study")), base / "calls.log"
    with pytest.MonkeyPatch.context() as env:
        env.setenv(LOG, str(log))
        campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
        ran = calls(log)
        log.unlink()
        finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return Built(campaign, ran, calls(log))


@pytest.fixture
def copy(built, tmp_path) -> Campaign:
    return copy_campaign(built.campaign, tmp_path, "study")


# --- the analysis builds with build_analysis, and only the analysis --------------------------------------------------


def test_r17_a_run_calls_build_and_never_build_analysis(built):
    """R17: the run's own build serves a run; `build_analysis` is not asked, nor by the replay check."""
    assert built.run_calls
    assert set(built.run_calls) == {"build"}


def test_r17_analysis_calls_build_analysis_and_never_build(built):
    """R17: `check` builds nothing, and `analyze` and `report` each build the analysis plugin, not the run's."""
    assert built.finish_calls == ["build_analysis", "build_analysis"]


def test_r17_a_resume_a_shard_and_a_fingerprint_without_a_start_never_call_build_analysis(built, tmp_path, monkeypatch):
    """R17: every other way into the plugin (a resume of the finished campaign, a shard of a fresh one, the HPC shard
    driver's fingerprint) is a run path and builds with `build`."""
    log = tmp_path / "calls.log"
    monkeypatch.setenv(LOG, str(log))
    resumed = copy_campaign(built.campaign, tmp_path, "resumed")
    run_study(resumed.root)
    fresh = with_builders(tmp_path, "fresh", held=True)
    run_study(fresh, shard=ShardSpec(1, 2, "shard-1"))
    pipeline.campaign_fingerprint(fresh)
    assert calls(log)
    assert set(calls(log)) == {"build"}


WATCHED = {"load_analysis_plugin", "BUILD_ANALYSIS", "build_analysis"}


class References(ast.NodeVisitor):
    """Where a module of the core names the analysis loader, its constant or the builder's name: by function."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.found: set[str] = set()

    def note(self, name: str) -> None:
        if name in WATCHED:
            self.found.add(self.stack[-1] if self.stack else "<module>")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Name(self, node: ast.Name) -> None:
        self.note(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.note(node.attr)
        self.generic_visit(node)

    def visit_alias(self, node: ast.alias) -> None:
        self.note(node.name.rsplit(".", 1)[-1])

    def visit_Constant(self, node: ast.Constant) -> None:
        self.note(node.value if isinstance(node.value, str) else "")  # a docstring is never exactly the name


def test_r17_only_the_analysis_loads_build_analysis_and_only_the_analysis_loader_names_it():
    """R17: no Python file of the repository (the core, the Onedata layer, the deploy scripts) but the loader's own
    names the analysis loader, its constant or `"build_analysis"`, except `_analyse_loaded` (and the import it
    needs), so a run path that reached it would be a new reference here."""
    users: dict[str, set[str]] = {}
    for path in sorted(p for root in SCANNED for p in root.rglob("*.py")):
        seen = References()
        seen.visit(ast.parse(path.read_text(encoding="utf-8")))
        if seen.found:
            users[path.relative_to(ROOT).as_posix()] = seen.found
    assert users.pop("src/expcore/study/analysis_plugin.py", None) is not None
    assert users == {"src/expcore/study/pipeline.py": {"<module>", "_analyse_loaded"}}


def test_r17_an_analysis_builds_where_the_run_plugin_cannot_and_regenerates_what_it_wrote(
    built, copy, monkeypatch, tmp_path, clean_environment
):
    """R17: with the federation gone, `build` fails and `analyze`, `report` and `regenerate` still give the outputs the
    run's own build gave, since they need only the scorers."""
    log = tmp_path / "calls.log"
    monkeypatch.setenv(LOG, str(log))
    monkeypatch.setenv(GONE, "1")
    before = {name: (copy.root / "out" / name).read_bytes() for name in ("scores.json", "analysis.json", "report.md")}
    with pytest.raises(RuntimeError, match="the federation is gone"):
        load_plugin(StudyContext(load_study(copy.root), {}))
    for name in before:
        (copy.root / "out" / name).unlink()
    assert [cli(clean_environment, name, str(copy.root))[0] for name in ("analyze", "report")] == [0, 0]
    assert {name: (copy.root / "out" / name).read_bytes() for name in before} == before
    assert regenerate(copy.root) == []
    assert (calls(log).count("build"), calls(log).count("build_analysis")) == (1, 3)  # `build`: the failed load above


def test_r17_scores_and_analysis_are_byte_identical_whichever_builder_serves_them(copy, monkeypatch):
    """R17: one plugin file with both builders (a second would be another code identity): the analysis the
    `build_analysis` plugin gives and the one `build` gives agree in every output, byte for byte."""
    via_analysis = output_bytes(analyse_study(copy.root))
    monkeypatch.setattr(analysis_plugin, "BUILD_ANALYSIS", "no_such_builder")  # the module then defines none
    via_build = output_bytes(analyse_study(copy.root))
    assert via_analysis == via_build
    assert set(via_analysis) == {"export.json", "scores.json", "analysis.json", "report.md"}


# --- the guard: the analysis scores with the scorers the run had -----------------------------------------------------


@pytest.mark.parametrize("which", ["other", "extra", "none"])
def test_r17_a_build_analysis_with_other_scorers_is_refused_naming_them_and_nothing_is_written(
    which, copy, monkeypatch, clean_environment
):
    """R17: another version, an added scorer or none: the scorers' digest is not the campaign's, so `analyze` refuses
    before it scores, names the analysis plugin's scorers, never a digest or a version, and writes nothing."""
    monkeypatch.setenv(SCORERS, which)
    out = copy.root / "out"
    for name in ("scores.json", "analysis.json", "export.json", "report.md"):
        (out / name).unlink()
    status, errors = cli(clean_environment, "analyze", str(copy.root))
    assert status == 1
    assert errors.startswith("analyze refused: ")
    if which == "none":  # no scorer at all is the plugin's own refusal, before the comparison
        assert "at least one scorer" in errors
    else:
        assert "build_analysis" in errors
        assert "'tiny'" in errors
        assert "campaign" in errors
        assert (which == "extra") == ("'extra'" in errors)
        assert not DIGEST.search(errors)
        assert "'1'" not in errors
        assert "'2'" not in errors
    assert not any((out / name).exists() for name in ("scores.json", "analysis.json", "export.json", "report.md"))
    with pytest.raises(Refused if which != "none" else ConfigError):
        analyse_study(copy.root)


def test_r17_the_refusal_is_the_guards_alone_the_same_scorers_under_build_pass_unrefused(copy, monkeypatch):
    """R17: with the module's `build_analysis` out of the way, the other scorers are not what refuses: `build` gives the
    campaign's own, and a study without `build_analysis` keeps re-scoring after a scorer change (R13)."""
    monkeypatch.setenv(SCORERS, "other")
    with pytest.raises(Refused):
        analyse_study(copy.root)
    monkeypatch.setattr(analysis_plugin, "BUILD_ANALYSIS", "no_such_builder")
    analysis = analyse_study(copy.root)
    assert analysis.scorers == {"tiny": "1"}


def test_r17_a_module_without_build_analysis_is_loaded_by_its_build_as_before(tmp_path):
    """R17: the shipped example defines no `build_analysis`: its analysis plugin is the one `build` gives, unguarded."""
    plain = make_copy(tmp_path, "plain")
    context = StudyContext(load_study(plain), {})
    loaded = load_analysis_plugin(context)
    assert loaded.from_build_analysis is False
    assert loaded.plugin.scorer_digest() == load_plugin(context).scorer_digest()
    assert [type(s).__name__ for s in loaded.plugin.scorers] == ["TinyScorer"]


def test_r17_a_build_analysis_that_is_not_a_function_or_gives_no_plugin_is_refused_naming_it(tmp_path):
    """R17: the same refusals `build` gets, with the name of the function that failed."""
    cases = {
        "not a function": ("\nbuild_analysis = 3\n", "must define a function build_analysis(context)"),
        "not a plugin": ("\ndef build_analysis(context):\n    return 3\n", "build_analysis(context) returned int"),
    }
    for name, (extra, message) in cases.items():
        study = with_builders(tmp_path, name.replace(" ", "-"), extra)
        with pytest.raises(ConfigError, match=re.escape(message)):
            load_analysis_plugin(StudyContext(load_study(study), {}))


def test_r17_the_analysis_plugin_is_checked_for_its_scorers_alone(tmp_path):
    """R17: analysis reads only `scorers`, so `build_analysis` need give no prompt or tool hash and no backend factory;
    the scorers' names must still differ."""
    twice = "\ndef build_analysis(context):\n    return StudyPlugin(None, [TinyScorer(), TinyScorer()], {}, {})\n"
    study = with_builders(tmp_path, "twice", twice)
    with pytest.raises(ConfigError, match="at least one scorer, and their names must differ"):
        load_analysis_plugin(StudyContext(load_study(study), {}))
    bare = "\ndef build_analysis(context):\n    return StudyPlugin(None, [TinyScorer()], {}, {})\n"
    loaded = load_analysis_plugin(StudyContext(load_study(with_builders(tmp_path, "bare", bare)), {}))
    assert loaded.from_build_analysis
    assert loaded.plugin.prompts == {}
    assert loaded.plugin.tools == {}


# --- a store that records no scorer digest ---------------------------------------------------------------------------


def forget_scorer_digest(campaign: Campaign) -> None:
    """Make the store one whose fingerprint was edited or damaged: the core never writes one without the digest."""
    connection = sqlite3.connect(campaign.root / "out" / "state.sqlite")
    try:
        (text,) = connection.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
        held = json.loads(text)
        del held["scorer_digest"]
        connection.execute("UPDATE meta SET value=? WHERE key='fingerprint'", (json.dumps(held),))
        connection.commit()
    finally:
        connection.close()


def test_r17_a_store_that_records_no_scorer_digest_is_refused_for_a_build_analysis_and_not_for_a_build(
    copy, monkeypatch, clean_environment
):
    """R17: every fingerprint the core writes holds the digest, so a store without one was changed or is damaged, and
    the scorers of a `build_analysis` cannot be shown to be the run's: `analyze` refuses, with nothing written, even
    when they are the same. Built by `build`, which is never compared, the same store analyses as before."""
    forget_scorer_digest(copy)
    assert cli(clean_environment, "check", str(copy.runs))[0] == 0  # the check snapshots the store as it is
    for name in ("scores.json", "analysis.json", "export.json", "report.md"):
        (copy.root / "out" / name).unlink()
    for which in ("same", "other"):
        monkeypatch.setenv(SCORERS, which)
        status, errors = cli(clean_environment, "analyze", str(copy.root))
        assert status == 1
        assert "1 of 1 snapshot of the store records no valid scorer digest" in errors
        assert "build_analysis" in errors
        assert not DIGEST.search(errors)
        assert not (copy.root / "out" / "scores.json").exists()
        with pytest.raises(Refused):
            analyse_study(copy.root)
    monkeypatch.setattr(analysis_plugin, "BUILD_ANALYSIS", "no_such_builder")
    assert analyse_study(copy.root).scorers == {"tiny": "1"}


# --- the comparison itself, over what each snapshot of a store records -----------------------------------------------


class Scorer:
    def __init__(self, name: str, version: str):
        self.name, self.version = name, version


def digest_of(*scorers: Scorer) -> str:
    """The digest the run's plugin records for these scorers."""
    return StudyPlugin(None, list(scorers), {}, {}).scorer_digest()  # type: ignore[arg-type]


def test_r17_every_snapshot_is_compared_and_one_that_differs_refuses():
    """R17: a merged batch has a snapshot for each shard: all agree, or the analysis is refused naming the scorers."""
    mine = [Scorer("tiny", "1")]
    recorded = {"scorer_digest": digest_of(*mine)}
    assert require_run_scorers(mine, "plugin.py", [recorded]) is None
    assert require_run_scorers(mine, "plugin.py", [recorded, recorded]) is None
    with pytest.raises(ScorersDiffer) as refused:
        require_run_scorers(mine, "plugin.py", [recorded, {"scorer_digest": digest_of(Scorer("tiny", "2"))}])
    assert "'tiny'" in str(refused.value)
    assert "plugin.py" in str(refused.value)
    assert not DIGEST.search(str(refused.value))


def test_r17_a_snapshot_that_records_no_valid_digest_refuses_and_counts_them():
    """R17: no snapshot, no fingerprint, one without the digest, a malformed one (not 64 lower-case hex digits, not a
    string) are all a refusal, never a pass, even beside snapshots that agree; the message counts the snapshots and
    shows no digest."""
    mine = [Scorer("tiny", "1")]
    recorded = {"scorer_digest": digest_of(*mine)}
    with pytest.raises(ScorersDiffer, match="no snapshot of the store was read"):
        require_run_scorers(mine, "plugin.py", [])
    upper = {"scorer_digest": recorded["scorer_digest"].upper()}
    for blank in (None, {}, {"scorer_digest": "not a digest"}, {"scorer_digest": 7}, upper):
        with pytest.raises(ScorersDiffer, match=r"1 of 1 snapshot of the store records no valid scorer digest"):
            require_run_scorers(mine, "plugin.py", [blank])
        with pytest.raises(ScorersDiffer, match=r"1 of 2 snapshots of the store record no valid scorer digest") as two:
            require_run_scorers(mine, "plugin.py", [recorded, blank])
        assert not DIGEST.search(str(two.value))


def test_r17_another_digest_is_named_by_its_scorers_even_beside_a_snapshot_with_none():
    """R17: when one snapshot differs and another records none, the refusal that names the scorers comes first."""
    mine = [Scorer("tiny", "2")]
    other = {"scorer_digest": digest_of(Scorer("tiny", "1"))}
    with pytest.raises(ScorersDiffer, match="gives the scorers 'tiny'"):
        require_run_scorers(mine, "plugin.py", [None, other])


def test_r17_several_scorers_are_named_in_the_order_the_plugin_gives_them():
    """R17: the refusal names every scorer of the analysis plugin once, and says which builder gave them."""
    scorers = [Scorer("second", "1"), Scorer("first", "1")]
    with pytest.raises(ScorersDiffer) as refused:
        require_run_scorers(scorers, "plugin.py", [{"scorer_digest": "0" * 64}])
    assert "'second', 'first'" in str(refused.value)
    assert "build_analysis" in str(refused.value)


# --- --rescore: a scorer corrected since the run is allowed on purpose, and recorded ---------------------------------

OUTPUTS = ("export.json", "scores.json", "analysis.json", "report.md")  # what `analyze` and `report` write under out/


def recorded_digest(campaign: Campaign) -> str:
    """The run plugin's scorer digest, as the store's fingerprint records it."""
    return json.loads(campaign.sql("SELECT value FROM meta WHERE key='fingerprint'")[0]["value"])["scorer_digest"]


def written(campaign: Campaign) -> dict[str, bytes]:
    return {name: (campaign.root / "out" / name).read_bytes() for name in OUTPUTS}


def rescored(campaign: Campaign, env: dict[str, str]) -> tuple[str, str]:
    """Analyse and report a campaign whose `build_analysis` gives the corrected scorer, with `--rescore`; return the
    digest the run recorded and the one of the corrected scorers."""
    assert [cli(env, name, str(campaign.root), "--rescore")[0] for name in ("analyze", "report")] == [0, 0]
    return recorded_digest(campaign), digest_of(Scorer("tiny", "2"))


def test_r17_rescore_lets_a_corrected_scorer_through_and_records_both_digests_beside_r13s_scorer_versions(
    copy, monkeypatch, clean_environment
):
    """R17: with `--rescore` a `build_analysis` whose scorer changed since the run analyses; `scores.json` and
    `analysis.json["scorers"]` hold the corrected version, as R13's re-score does, and `analysis.json["rescore"]` says
    that the run recorded another digest and what it was."""
    monkeypatch.setenv(SCORERS, "other")
    old, new = rescored(copy, clean_environment)
    assert old == digest_of(Scorer("tiny", "1")) != new
    analysis = json.loads((copy.root / "out" / "analysis.json").read_text(encoding="utf-8"))
    assert analysis["scorers"] == {"tiny": "2"}
    assert analysis["rescore"] == {"run_scorer_digests": [old], "analysis_scorer_digest": new}
    scores = json.loads((copy.root / "out" / "scores.json").read_text(encoding="utf-8"))
    assert scores
    assert {held["tiny"]["version"] for held in scores.values()} == {"2"}


def test_r17_rescore_is_shown_in_the_reports_provenance_beside_the_scorers_line(copy, monkeypatch, clean_environment):
    """R17: the report's Provenance names the re-score, with both digests, right after R13's `- Scorers:` line, which
    holds the corrected version; the rest of the report is the report without it."""
    monkeypatch.setenv(SCORERS, "other")
    old, new = rescored(copy, clean_environment)
    lines = (copy.root / "out" / "report.md").read_text(encoding="utf-8").splitlines()
    at = lines.index("- Scorers: tiny `2`")
    assert lines[at + 1].startswith("- Re-scored (`--rescore`):")
    assert f"`{old}`" in lines[at + 1]
    assert f"`{new}`" in lines[at + 1]
    assert lines[at + 2].startswith("- Runs:")
    assert sum(line.startswith("- Re-scored") for line in lines) == 1


def test_r17_without_rescore_a_corrected_scorer_is_still_refused_and_the_refusal_names_the_flag(
    copy, monkeypatch, clean_environment
):
    """R17: no flag, no change: the refusal comes before any record is scored, writes nothing and shows no digest; it
    now says that `--rescore` is how to analyse with the corrected scorer on purpose."""
    monkeypatch.setenv(SCORERS, "other")
    for name in OUTPUTS:
        (copy.root / "out" / name).unlink()
    for command in ("analyze", "report"):
        status, errors = cli(clean_environment, command, str(copy.root))
        assert status == 1
        assert errors.startswith(f"{command} refused: ")
        assert "--rescore" in errors
        assert not DIGEST.search(errors)
    assert not any((copy.root / "out" / name).exists() for name in OUTPUTS)


def test_r17_rescore_with_no_recorded_digest_is_refused_whatever_the_scorers(copy, monkeypatch, clean_environment):
    """R17: the flag relaxes only a digest that differs: a store that records none cannot show the scorers to be
    corrected ones, so it is refused with the flag, for the same scorers and for others, writing nothing."""
    forget_scorer_digest(copy)
    assert cli(clean_environment, "check", str(copy.runs))[0] == 0
    for name in OUTPUTS:
        (copy.root / "out" / name).unlink()
    for which in ("same", "other"):
        monkeypatch.setenv(SCORERS, which)
        status, errors = cli(clean_environment, "analyze", str(copy.root), "--rescore")
        assert status == 1
        assert "1 of 1 snapshot of the store records no valid scorer digest" in errors
        assert not DIGEST.search(errors)
        assert not (copy.root / "out" / "scores.json").exists()
        with pytest.raises(Refused):
            analyse_study(copy.root, allow_rescore=True)


def test_r17_rescore_refuses_a_snapshot_with_no_digest_beside_one_that_differs_and_one_with_no_snapshot():
    """R17: with the flag, a merged batch whose snapshots differ and one records none is refused for the one that
    records none; a batch of no snapshot is refused; a blank, malformed or upper-case digest is no digest."""
    mine = [Scorer("tiny", "2")]
    other = {"scorer_digest": digest_of(Scorer("tiny", "1"))}
    with pytest.raises(ScorersDiffer, match=r"1 of 2 snapshots of the store record no valid scorer digest"):
        require_run_scorers(mine, "plugin.py", [None, other], rescore=True)
    with pytest.raises(ScorersDiffer, match="no snapshot of the store was read"):
        require_run_scorers(mine, "plugin.py", [], rescore=True)
    for blank in ({}, {"scorer_digest": "not a digest"}, {"scorer_digest": other["scorer_digest"].upper()}):
        with pytest.raises(ScorersDiffer, match="record no valid scorer digest"):
            require_run_scorers(mine, "plugin.py", [other, blank], rescore=True)


def test_r17_rescore_over_a_merged_batch_records_every_digest_the_snapshots_hold_once_and_in_order():
    """R17: each shard's snapshot is compared; the record lists the distinct digests the snapshots hold, sorted, one
    entry each, and the analysis plugin's own. Without the flag the same snapshots are refused."""
    mine = [Scorer("tiny", "3")]
    older, oldest = digest_of(Scorer("tiny", "2")), digest_of(Scorer("tiny", "1"))
    snapshots = [{"scorer_digest": older}, {"scorer_digest": oldest}, {"scorer_digest": older}]
    record = require_run_scorers(mine, "plugin.py", snapshots, rescore=True)
    assert record == {"run_scorer_digests": sorted({older, oldest}), "analysis_scorer_digest": digest_of(*mine)}
    with pytest.raises(ScorersDiffer):
        require_run_scorers(mine, "plugin.py", snapshots)


def test_r17_rescore_with_the_scorers_the_run_had_changes_no_byte_and_records_nothing(copy):
    """R17: nothing was re-scored, so nothing is recorded: with the same scorers the flag gives the bytes it gives
    without it, with no `rescore` entry and no report line, and the check against the run's digest returns none."""
    plain = output_bytes(analyse_study(copy.root))
    flagged = output_bytes(analyse_study(copy.root, allow_rescore=True))
    assert flagged == plain
    assert "rescore" not in json.loads(plain["analysis.json"])
    assert b"Re-scored" not in plain["report.md"]
    mine = [Scorer("tiny", "1")]
    assert require_run_scorers(mine, "plugin.py", [{"scorer_digest": digest_of(*mine)}], rescore=True) is None


def test_r17_rescore_with_a_module_without_build_analysis_is_as_it_was(copy, monkeypatch):
    """R17: a module that defines no `build_analysis` is built by `build` and never compared, so the flag changes
    nothing in what it analyses, however the environment asks for other scorers."""
    monkeypatch.setattr(analysis_plugin, "BUILD_ANALYSIS", "no_such_builder")
    monkeypatch.setenv(SCORERS, "other")
    plain = output_bytes(analyse_study(copy.root))
    assert output_bytes(analyse_study(copy.root, allow_rescore=True)) == plain
    assert "rescore" not in json.loads(plain["analysis.json"])


def test_r17_regenerate_with_rescore_derives_what_a_rescored_analysis_wrote_and_without_it_is_refused(
    copy, monkeypatch, clean_environment
):
    """R17: `regenerate` takes the flag as `analyze` does and compares byte for byte; without it a re-scored batch is
    refused as an analysis of it is."""
    monkeypatch.setenv(SCORERS, "other")
    rescored(copy, clean_environment)
    assert regenerate(copy.root, allow_rescore=True) == []
    with pytest.raises(Refused, match="--rescore"):
        regenerate(copy.root)


def test_r17_rescore_is_an_option_of_analyze_and_report_only():
    """R17: `analyze` and `report` take it (off by default); `run`, `check` and `merge` do not."""
    parser = build_parser()
    for command in ("analyze", "report"):
        assert parser.parse_args([command, "study"]).rescore is False
        assert parser.parse_args([command, "study", "--rescore"]).rescore is True
    for argv in (["run", "study", "--rescore"], ["check", "batch", "--rescore"], ["merge", "out", "s", "--rescore"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


# --- a package's build and check honour the re-score its shipped analysis.json records --------------------------------


def entry_of(old: str, mine: list[Scorer]) -> dict:
    """The `rescore` entry of an analysis that re-scored a store recording `old` with the scorers `mine`."""
    return {"run_scorer_digests": [old], "analysis_scorer_digest": digest_of(*mine)}


def test_r17_a_shipped_entry_equal_to_what_the_guard_records_lets_a_differing_digest_through_and_returns_it():
    """R17: the decision was recorded at analyze, so the entry the shipped analysis.json holds is honoured when it is
    exactly what the guard would record now, and the guard returns that same record, as `rescore=True` does."""
    mine, old = [Scorer("tiny", "2")], digest_of(Scorer("tiny", "1"))
    shipped = entry_of(old, mine)
    assert require_run_scorers(mine, "plugin.py", [{"scorer_digest": old}], shipped=shipped) == shipped
    assert require_run_scorers(mine, "plugin.py", [{"scorer_digest": old}], rescore=True) == shipped


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda e: {**e, "analysis_scorer_digest": "0" * 64}, id="another analysis digest"),
        pytest.param(lambda e: {**e, "run_scorer_digests": ["1" * 64]}, id="another run digest"),
        pytest.param(lambda e: {**e, "run_scorer_digests": [*e["run_scorer_digests"], "1" * 64]}, id="an extra digest"),
        pytest.param(lambda e: {**e, "run_scorer_digests": []}, id="no run digest"),
        pytest.param(lambda e: {**e, "note": "kept"}, id="an extra key"),
        pytest.param(lambda e: {"run_scorer_digests": e["run_scorer_digests"]}, id="no analysis digest"),
        pytest.param(lambda e: {**e, "run_scorer_digests": e["run_scorer_digests"][0]}, id="a digest, not a list"),
        pytest.param(lambda e: [e], id="a list"),
        pytest.param(lambda e: "rescored", id="a string"),
        pytest.param(lambda e: {}, id="an empty mapping"),
        pytest.param(lambda e: None, id="no entry"),
    ],
)
def test_r17_a_shipped_entry_that_is_not_exactly_the_record_is_refused_naming_the_scorers(mutate):
    """R17: a differing digest is honoured only for an entry equal to what the guard records now: another digest on
    either side, an extra, a missing or an extra key, a wrong type or no entry at all is the refusal of an analysis
    that was not asked to re-score. The message names the scorers and shows no digest."""
    mine, old = [Scorer("tiny", "2")], digest_of(Scorer("tiny", "1"))
    with pytest.raises(ScorersDiffer) as refused:
        require_run_scorers(mine, "plugin.py", [{"scorer_digest": old}], shipped=mutate(entry_of(old, mine)))
    assert "'tiny'" in str(refused.value)
    assert "build_analysis" in str(refused.value)
    assert not DIGEST.search(str(refused.value))


def test_r17_a_refusal_with_a_shipped_entry_says_the_entry_is_not_for_these_scorers_and_one_without_does_not():
    """R17: a package whose analysis.json records a re-score that is not this one is told so, which a package with no
    entry is not; the other says how to record one."""
    mine, old = [Scorer("tiny", "2")], digest_of(Scorer("tiny", "1"))
    wrong = entry_of(old, [Scorer("tiny", "3")])
    with pytest.raises(ScorersDiffer, match="records a re-score that is not of these scorers on this store"):
        require_run_scorers(mine, "plugin.py", [{"scorer_digest": old}], shipped=wrong)
    with pytest.raises(ScorersDiffer) as bare:
        require_run_scorers(mine, "plugin.py", [{"scorer_digest": old}])
    assert "records a re-score" not in str(bare.value)
    assert "--rescore" in str(bare.value)


def test_r17_a_matching_shipped_entry_relaxes_only_the_differing_digest_never_a_missing_one():
    """R17: honoured as the flag is: a snapshot that records no valid digest, or no snapshot, is refused even when the
    entry equals what the digests of the others give."""
    mine, old = [Scorer("tiny", "2")], digest_of(Scorer("tiny", "1"))
    shipped = entry_of(old, mine)
    with pytest.raises(ScorersDiffer, match=r"1 of 2 snapshots of the store record no valid scorer digest"):
        require_run_scorers(mine, "plugin.py", [{"scorer_digest": old}, {}], shipped=shipped)
    with pytest.raises(ScorersDiffer, match="no snapshot of the store was read"):
        require_run_scorers(mine, "plugin.py", [], shipped=shipped)


def test_r17_a_shipped_entry_beside_the_runs_own_scorers_records_nothing():
    """R17: with the scorers the run had there is no re-score, whatever the entry says: the guard returns none, so the
    outputs derive without the entry and the shipped one fails the byte comparison."""
    mine = [Scorer("tiny", "1")]
    own = digest_of(*mine)
    assert require_run_scorers(mine, "plugin.py", [{"scorer_digest": own}], shipped=entry_of(own, mine)) is None


def test_r17_the_entry_of_a_shipped_analysis_is_read_only_when_it_is_a_mapping_in_an_object():
    """R17: `rescore_entry` reads the key of an analysis.json given as bytes; a missing file, invalid JSON, a value
    that is not an object, no key or a value that is not an object under it give no entry."""
    entry = {"run_scorer_digests": ["a" * 64], "analysis_scorer_digest": "b" * 64}
    assert analysis_plugin.rescore_entry(json.dumps({"alpha": 0.05, "rescore": entry}).encode()) == entry
    for held in (None, b"", b"not json", b"[]", b'"x"', b"{}", b'{"rescore": 3}', b'{"rescore": [1]}', b"\xff\xfe"):
        assert analysis_plugin.rescore_entry(held) is None


def test_r17_regenerate_honours_the_re_score_the_batch_records_only_when_asked_to(copy, monkeypatch, clean_environment):
    """R17: `honour_shipped_rescore` is what a package's build and check pass: it derives what a re-scored analysis
    wrote, with the entry its analysis.json holds and no flag, while a plain `regenerate` and `analyze` still refuse."""
    monkeypatch.setenv(SCORERS, "other")
    rescored(copy, clean_environment)
    assert regenerate(copy.root, honour_shipped_rescore=True) == []
    with pytest.raises(Refused, match="--rescore"):
        regenerate(copy.root)
    status, errors = cli(clean_environment, "analyze", str(copy.root))  # analyze does not re-read the decision
    assert status == 1
    assert "--rescore" in errors


def test_r17_honouring_needs_the_entry_a_batch_with_none_is_refused_as_ever(copy, monkeypatch):
    """R17: the corrected scorers on a batch that nobody re-scored: no entry in analysis.json, so the refusal stands
    even when asked to honour one."""
    monkeypatch.setenv(SCORERS, "other")
    with pytest.raises(Refused, match="--rescore"):
        regenerate(copy.root, honour_shipped_rescore=True)
    with pytest.raises(Refused, match="--rescore"):
        analyse_study(copy.root, shipped_rescore=None)


def test_r17_an_entry_on_an_analysis_that_re_scored_nothing_fails_the_byte_comparison(copy):
    """R17: with the run's own scorers nothing is re-scored, so an entry in analysis.json cannot be honoured: the
    derived analysis has none and `regenerate` reports the file as differing."""
    mine = [Scorer("tiny", "1")]
    edit_analysis(copy.root, lambda data: data.update(rescore=entry_of(recorded_digest(copy), mine)))
    assert regenerate(copy.root, honour_shipped_rescore=True) == ["analysis.json: differs from what the records give"]


def test_r17_regenerate_compares_the_bytes_it_read_the_entry_from(copy, monkeypatch, clean_environment):
    """R17: analysis.json is read once, so the entry that is honoured and the bytes that are compared are the same:
    an entry changed to another digest is refused by the guard, not passed on to a comparison."""
    monkeypatch.setenv(SCORERS, "other")
    rescored(copy, clean_environment)
    edit_analysis(copy.root, lambda data: data["rescore"].update(run_scorer_digests=["1" * 64]))
    with pytest.raises(Refused, match="records a re-score that is not of these scorers on this store"):
        regenerate(copy.root, honour_shipped_rescore=True)
