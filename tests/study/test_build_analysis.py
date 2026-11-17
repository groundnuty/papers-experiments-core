"""R17: a study's analysis may build its plugin without the platform, and then scores with the scorers the run had.

`build(context)` needs the study's platform (the rendered federation, the site's variables), which is gone when an HPC
batch is analysed after copy-back. A plugin module may also define `build_analysis(context)`: the analysis paths
(`analyse_study`, so `analyze`, `report`, `regenerate` and a package's check) call it, and fall back to `build` without
it. It is never called for a run. The campaign fingerprint records the run plugin's `scorer_digest`, and an analysis
built by `build_analysis` is refused when its scorers' digest is another one.

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
from study_support import HEAD, Campaign, clean_environment, cli, copy_campaign, finish, make_copy

from expcore.campaign.shard import ShardSpec
from expcore.config.loading import ConfigError
from expcore.provenance.codestamp import write_stamp
from expcore.study import analysis_plugin, pipeline
from expcore.study.analysis_plugin import ScorersDiffer, load_analysis_plugin, scorer_notes
from expcore.study.config import load_study
from expcore.study.pipeline import Refused, analyse_study, output_bytes, regenerate, run_study
from expcore.study.plugin import StudyContext, StudyPlugin, load_plugin

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

SRC = Path(__file__).resolve().parents[2] / "src" / "expcore"
LOG = "TINY_BUILD_LOG"  # the file each builder notes its call in
GONE = "TINY_NO_FEDERATION"  # "1": `build` fails, as it does where the federation no longer exists
SCORERS = "TINY_ANALYSIS_SCORERS"  # which scorers `build_analysis` gives: same, other, extra or none
DIGEST = re.compile(r"[0-9a-f]{64}")

# What the copy's plugin gains: the run's `build` keeps its body under another name and wraps it.
BOTH_BUILDERS = """

# --- the test's two builders ---------------------------------------------------------------------------------------
import os

_RUN_BUILD = build


def _note(name):
    path = os.environ.get("TINY_BUILD_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(name + "\\n")


def build(context):
    _note("build")
    if os.environ.get("TINY_NO_FEDERATION") == "1":
        raise RuntimeError("the federation is gone")
    return _RUN_BUILD(context)


class OtherScorer(TinyScorer):
    version = "2"


class ExtraScorer(TinyScorer):
    name = "extra"


def build_analysis(context):
    _note("build_analysis")
    chosen = {
        "same": [TinyScorer()],
        "other": [OtherScorer()],
        "extra": [TinyScorer(), ExtraScorer()],
        "none": [],
    }[os.environ.get("TINY_ANALYSIS_SCORERS", "same")]
    return StudyPlugin(executor=None, scorers=chosen, prompts={}, tools={})
"""


def with_builders(base: Path, name: str, extra: str = BOTH_BUILDERS, *, held: bool = False) -> Path:
    """A stamped copy of the example whose plugin gains `extra`; the scorer is code, so the stamp is made after. With
    `held`, a budget too small for any run, so a start opens the store, pins its fingerprint and returns, and room for
    two shards (a model's cap is at least the number of shards)."""
    copy = make_copy(base, name)
    if held:
        text = (copy / "campaign.toml").read_text(encoding="utf-8").replace("max_usd = 5.0", "max_usd = 0.00001")
        text = text.replace("alpha-stub = 1", "alpha-stub = 2").replace("beta-stub = 1", "beta-stub = 2")
        (copy / "campaign.toml").write_text(text, encoding="utf-8")
    plugin = copy / "plugin.py"
    plugin.write_text(plugin.read_text(encoding="utf-8") + extra, encoding="utf-8")
    write_stamp(copy, HEAD, load_study(copy).scope)
    return copy


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
    """R17: no module of the core but the loader's own names the analysis loader, its constant or `"build_analysis"`,
    except `_analyse_loaded` (and the import it needs), so a run path that reached it would be a new reference here."""
    users: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        seen = References()
        seen.visit(ast.parse(path.read_text(encoding="utf-8")))
        if seen.found:
            users[path.relative_to(SRC).as_posix()] = seen.found
    assert users.pop("study/analysis_plugin.py", None) is not None
    assert users == {"study/pipeline.py": {"<module>", "_analyse_loaded"}}


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
    assert analysis.notes == ()
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
    """Make the store one from before the fingerprint held the scorers' digest."""
    connection = sqlite3.connect(campaign.root / "out" / "state.sqlite")
    try:
        (text,) = connection.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
        held = json.loads(text)
        del held["scorer_digest"]
        connection.execute("UPDATE meta SET value=? WHERE key='fingerprint'", (json.dumps(held),))
        connection.commit()
    finally:
        connection.close()


def test_r17_a_store_without_the_digest_skips_the_comparison_and_the_analysis_says_so(
    copy, monkeypatch, clean_environment, capsys
):
    """R17: an old store has nothing to compare with: the analysis goes on, its notes say the check was skipped (on
    stderr from `analyze` and `report`, in no output file), and an analysis under another scorer is then not refused."""
    forget_scorer_digest(copy)
    assert cli(clean_environment, "check", str(copy.runs))[0] == 0  # the check snapshots the store as it is
    monkeypatch.setenv(SCORERS, "other")
    analysis = analyse_study(copy.root)
    assert len(analysis.notes) == 1
    assert "records no scorer digest" in analysis.notes[0]
    assert "build_analysis" in analysis.notes[0]
    assert not DIGEST.search(analysis.notes[0])
    status, errors = cli(clean_environment, "analyze", str(copy.root))
    assert status == 0
    assert "analyze: " + analysis.notes[0] in errors
    written = "".join(
        (copy.root / "out" / name).read_text(encoding="utf-8") for name in ("analysis.json", "scores.json")
    )
    assert "records no scorer digest" not in written


# --- the comparison itself, over what each snapshot of a store records -----------------------------------------------


class Scorer:
    def __init__(self, name: str, version: str):
        self.name, self.version = name, version


def digest_of(*scorers: Scorer) -> str:
    """The digest the run's plugin records for these scorers."""
    return StudyPlugin(None, list(scorers), {}, {}).scorer_digest()  # type: ignore[arg-type]


def test_r17_every_snapshot_that_records_a_digest_is_compared_and_one_that_differs_refuses():
    """R17: a merged batch has a snapshot for each shard: all agree, or the analysis is refused naming the scorers."""
    mine = [Scorer("tiny", "1")]
    recorded = {"scorer_digest": digest_of(*mine)}
    assert scorer_notes(mine, "plugin.py", [recorded, recorded]) == ()
    with pytest.raises(ScorersDiffer) as refused:
        scorer_notes(mine, "plugin.py", [recorded, {"scorer_digest": digest_of(Scorer("tiny", "2"))}])
    assert "'tiny'" in str(refused.value)
    assert "plugin.py" in str(refused.value)
    assert not DIGEST.search(str(refused.value))


def test_r17_a_snapshot_that_records_none_is_said_and_never_makes_the_others_unchecked():
    """R17: no fingerprint, one without the digest, or a malformed one is a snapshot with nothing to compare; the
    rest are compared as they are, and the note counts the ones that could not be."""
    mine = [Scorer("tiny", "1")]
    recorded = {"scorer_digest": digest_of(*mine)}
    (note,) = scorer_notes(mine, "plugin.py", [])  # no snapshot read at all: nothing was compared, and it says so
    assert "no snapshot" in note
    for blank in (None, {}, {"scorer_digest": "not a digest"}, {"scorer_digest": 7}):
        (note,) = scorer_notes(mine, "plugin.py", [blank])
        assert "1 of 1 snapshot" in note
        (note,) = scorer_notes(mine, "plugin.py", [recorded, blank])
        assert "1 of 2 snapshots" in note
        with pytest.raises(ScorersDiffer):
            scorer_notes([Scorer("tiny", "2")], "plugin.py", [recorded, blank])


def test_r17_several_scorers_are_named_in_the_order_the_plugin_gives_them():
    """R17: the refusal names every scorer of the analysis plugin once, and says which builder gave them."""
    scorers = [Scorer("second", "1"), Scorer("first", "1")]
    with pytest.raises(ScorersDiffer) as refused:
        scorer_notes(scorers, "plugin.py", [{"scorer_digest": "0" * 64}])
    assert "'second', 'first'" in str(refused.value)
    assert "build_analysis" in str(refused.value)
