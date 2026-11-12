"""The SPARQL example platform (examples/sparql_platform) and its study, end to end, on the documented interfaces only.

The platform is a distribution of its own, `sparql-platform`, with an in-process RDF store (pyoxigraph). The core's
development dependencies install it editable, so `uv sync` brings pyoxigraph and this module always runs; the core
itself never imports either. The study runs in subprocesses, as `python -m expcore` would, so the platform's kind is
registered there and not in the process that runs the other tests. The package is built with stand-in wheels (the
core and the platform are installed editable here, and packing NumPy and SciPy again on every run costs minutes) and
checked with this interpreter (`make check-installed`); a real build that installs its wheels offline is what the
fresh-repo workflow does for the tiny study.
"""

import ast
import json
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sparql_platform import STORE_KIND, GraphStore, QueryLog, SparqlPlatform, SparqlTools, StoreHealth, StoreSnapshot
from study_support import HEAD, ROOT, clean_environment

from expcore.campaign.healthgate import Health
from expcore.errors import InfraError
from expcore.provenance.codestamp import write_stamp
from expcore.records.record import load_record
from expcore.study.config import load_study

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

PLATFORM = ROOT / "examples" / "sparql_platform"
STUDY = PLATFORM / "study"
RUNNER = ROOT / "tests" / "support" / "stand_in_runner.py"
PACKAGE = "orchard-study-package"
MAX_ROWS = 8  # the study's cap, MAX_ROWS in its plugin
STEPS = ("run", "check", "analyze", "report", "package")


@dataclass
class Orchard:
    """A stamped copy of the study, and what each step of the pipeline did to it."""

    root: Path
    codes: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def runs(self) -> Path:
        return self.root / "out" / "runs"

    @property
    def unpacked(self) -> Path:
        return self.root.parent / "unpacked" / PACKAGE

    def read(self, name: str):
        return json.loads((self.root / "out" / name).read_text(encoding="utf-8"))

    def records(self):
        found = [p for p in sorted(self.runs.glob("*/try*/*.json")) if p.name.count(".") == 2]
        return [load_record(p) for p in found]


def expcore(env: dict[str, str], cwd: Path, *argv: str) -> subprocess.CompletedProcess:
    """`python -m expcore ARGV` in a new process, with stand-in wheels for any package it builds."""
    command = [sys.executable, str(RUNNER), "-m", "expcore", *argv]
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)


@pytest.fixture(scope="module")
def orchard(tmp_path_factory, clean_environment) -> Orchard:
    """Copy and stamp the study, run every step of the pipeline, build the package, unpack it and check it."""
    base = tmp_path_factory.mktemp("orchard")
    root = Path(shutil.copytree(STUDY, base / "study", ignore=shutil.ignore_patterns("out", "__pycache__")))
    write_stamp(root, HEAD, load_study(root).scope)
    campaign = Orchard(root)
    targets = {"run": "study", "check": "study/out/runs", "analyze": "study", "report": "study"}
    for step in STEPS:
        argv = (step, "study", "--out", "dist") if step == "package" else (step, targets[step])
        done = expcore(clean_environment, base, *argv)
        campaign.codes[step], campaign.errors[step] = done.returncode, done.stderr
    if campaign.codes["package"] == 0:
        with tarfile.open(base / "dist" / f"{PACKAGE}.tar.gz") as archive:
            archive.extractall(base / "unpacked", filter="data")
        make = ["make", "-C", str(campaign.unpacked), "-f", "Makefile", "check-installed", f"PYTHON={sys.executable}"]
        done = subprocess.run(make, env=clean_environment, capture_output=True, text=True)
        campaign.codes["make"], campaign.errors["make"] = done.returncode, done.stdout + done.stderr
    return campaign


def test_every_step_succeeds_and_every_planned_run_finishes(orchard):
    assert orchard.codes == dict.fromkeys((*STEPS, "make"), 0), orchard.errors
    records = orchard.records()
    assert len(records) == 16  # four tasks, two conditions, two models, one seed: no attempt set aside
    assert {r.outcome["name"] for r in records} == {"finished"}


def test_the_replay_check_passes_and_records_the_platform_and_its_store_in_the_environment(orchard):
    check = json.loads((orchard.runs / "replay-check.json").read_text(encoding="utf-8"))
    assert check["n_ok"] == check["n_records"] == len(check["replayed"]) == 16
    assert not check["failures"]
    distributions = check["environment"]["distributions"]
    assert {"papers-experiments-core", "sparql-platform", "pyoxigraph"} <= set(distributions)
    assert check["environment"]["sources"]["sparql-platform"]["editable"] is True


def test_every_record_holds_the_stores_snapshot_and_the_read_only_tools_left_it_unchanged(orchard):
    for record in orchard.records():
        snapshot = record.instruments["rdf_store"]
        assert snapshot["ok"] is True
        assert snapshot["unchanged"] is True
        assert snapshot["start"] == snapshot["end"]
        assert snapshot["start"]["triples"] > 0
        assert snapshot["platform"]["distribution"] == "sparql-platform"
        assert snapshot["platform"]["store"].startswith("pyoxigraph ")


def test_the_study_records_a_failed_health_check_as_the_platforms_kind_not_the_cores_sandbox():
    assert load_study(STUDY).campaign.scheduler.health_kind == STORE_KIND


def test_the_conditions_differ_only_in_the_tools_the_model_was_shown(orchard):
    shown = {}
    for record in orchard.records():
        shown.setdefault(record.factors["exposure"], set()).add(tuple(t["name"] for t in record.tools))
    assert shown == {
        "query_only": {("sparql_query", "submit")},
        "query_and_schema": {("sparql_query", "list_predicates", "submit")},
    }


def test_the_platform_cuts_a_long_result_at_its_row_cap_and_says_so(orchard):
    cut = [(r, c) for r in orchard.records() for c in r.tool_calls if c["source_truncated"] is True]
    assert {(r.factors["task"], r.factors["exposure"]) for r, _ in cut} == {("thistledown-fruits", "query_only")}
    assert len(cut) == 2  # once per model
    for record, call in cut:
        lines = call["raw_text"].splitlines()
        assert len(lines) == 1 + MAX_ROWS + 1  # the header, the rows, the note
        assert lines[-1].startswith(f"(the first {MAX_ROWS} rows; more exist")
        assert not call["truncated"]  # the platform's cut, not the core's character cap
        assert record.instruments["rdf_store"]["served"]["truncated"] == 1


def test_an_update_a_service_query_and_a_syntax_error_reach_the_model_as_tool_errors(orchard):
    errors = {
        (r.factors["task"], r.factors["exposure"], r.factors["model"]): c["error_class"]
        for r in orchard.records()
        for c in r.tool_calls
        if c["status"] == "error"
    }
    assert errors == {
        ("marrowby-crew", "query_only", "beta-stub"): "ReadOnly",
        ("quillfen-count", "query_and_schema", "beta-stub"): "ServiceRefused",
        ("oldest-fruit", "query_and_schema", "alpha-stub"): "QuerySyntax",
    }


def test_the_analysis_scores_from_the_snapshot_and_keeps_the_exploratory_contrast_apart(orchard):
    scores = orchard.read("scores.json")
    assert '"unchanged": 1.0' in json.dumps(scores)
    analysis = orchard.read("analysis.json")
    labels = {c["name"]: c["exploratory"] for c in analysis["contrasts"]}
    assert labels == {
        "accuracy-guided-vs-bare": False,
        "queries-guided-vs-bare": False,
        "exposure-effect-on-rows": True,
    }
    report = (orchard.root / "out" / "report.md").read_text(encoding="utf-8")
    assert "exposure-effect-on-rows" in report.split("## Exploratory contrasts")[1]


def test_the_package_vendors_the_platform_and_its_store_and_its_check_regenerates_the_outputs(orchard):
    assert orchard.codes["make"] == 0, orchard.errors["make"]
    assert "every output regenerates from the records byte for byte" in orchard.errors["make"]
    package = json.loads((orchard.unpacked / "PACKAGE.json").read_text(encoding="utf-8"))
    assert {"sparql-platform", "pyoxigraph"} <= set(package["environment"]["distributions"])
    wheels = {w["name"] for w in package["wheels"]}
    assert {"papers-experiments-core", "sparql-platform", "pyoxigraph"} <= wheels
    assert (orchard.unpacked / "orchard.ttl").is_file()  # the graph the check's scorer loads again


# --- the platform's own pieces -------------------------------------------------------------------------------------


def _roots(path: Path) -> set[str]:
    roots = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_the_platform_imports_the_standard_library_the_core_and_its_store_and_the_study_only_the_platform():
    stdlib = set(sys.stdlib_module_names)
    modules = sorted((PLATFORM / "src" / "sparql_platform").glob("*.py"))
    assert modules
    for module in modules:
        assert _roots(module) - stdlib <= {"expcore", "pyoxigraph", "sparql_platform"}, module.name
    assert _roots(STUDY / "plugin.py") - stdlib == {"expcore", "sparql_platform"}


GRAPH = """@prefix o: <http://orchard.example/vocab#> .
o:a o:fruit "plum" . o:b o:fruit "pear" . o:c o:fruit "apple" . o:d o:fruit "plum" .
"""


@pytest.fixture
def graph(tmp_path) -> Path:
    path = tmp_path / "graph.ttl"
    path.write_text(GRAPH, encoding="utf-8")
    return path


def tools_over(graph: Path, max_rows: int = 2) -> SparqlTools:
    return SparqlTools(GraphStore.load(graph), QueryLog(), max_rows=max_rows)


def test_a_result_longer_than_the_cap_is_cut_and_marked_and_a_shorter_one_is_not(graph):
    tools = tools_over(graph)
    cut = tools.query({"query": "SELECT ?s WHERE { ?s ?p ?o } ORDER BY ?s"}, timeout_s=5)
    assert cut.source_truncated is True
    assert not cut.is_error
    assert cut.text.splitlines()[1:3] == ["<http://orchard.example/vocab#a>", "<http://orchard.example/vocab#b>"]
    whole = tools.query({"query": "SELECT ?f WHERE { ?s ?p ?f } GROUP BY ?f HAVING (COUNT(*) > 1)"}, timeout_s=5)
    assert whole.source_truncated is False
    assert whole.text == '?f\n"plum"'
    assert tools.query({"query": "ASK { ?s ?p 'pear' }"}, timeout_s=5).text == "true"
    assert tools.log.as_dict() == {"queries": 3, "rows_served": 3, "truncated": 1, "refused": 0}


def test_service_is_refused_anywhere_in_a_query_however_it_is_hidden_and_updates_are_refused(graph):
    """Telling the keyword from a string or a comment needs the parser's tokenizer; a guard that gets it wrong lets a
    query reach the network. So the word is refused anywhere, after decoding codepoint escapes, by design."""
    tools = tools_over(graph)
    hidden = (
        "SELECT * { SERVICE <http://elsewhere.example/q> { ?s ?p ?o } }",
        "select * { service <x:y> {} }",
        "SELECT * WHERE { #note\r SERVICE <http://elsewhere.example/q> { ?s ?p ?o } }",  # CR ends a comment
        "SELECT * WHERE { \\u0053ERVICE <http://elsewhere.example/q> { ?s ?p ?o } }",  # a codepoint escape
        "SELECT ?s WHERE { ?s ?p ?o FILTER(?o != 'service') }",  # the word in a literal: refused too
    )
    for remote in hidden:
        assert tools.query({"query": remote}, timeout_s=5).error_class == "ServiceRefused", remote
    update = tools.query({"query": "INSERT DATA { <x:a> <x:b> <x:c> }"}, timeout_s=5)
    assert update.is_error
    assert update.error_class == "ReadOnly"
    assert tools.query({"query": "SELEC ?x"}, timeout_s=5).error_class == "QuerySyntax"
    assert tools.query({}, timeout_s=5).error_class == "MissingQuery"
    assert tools.store.triples() == 4
    assert tools.log.refused == 8


def test_a_query_that_mints_blank_nodes_is_refused_because_its_labels_differ_on_every_run(graph):
    """A blank node a query creates gets a fresh label each time, so the tool's output would differ between runs."""
    tools = tools_over(graph)
    for minted in ("SELECT (BNODE() AS ?b) WHERE {}", "CONSTRUCT { [] <urn:p> <urn:o> } WHERE {}"):
        assert tools.query({"query": minted}, timeout_s=5).error_class == "BlankNodeResult", minted


@pytest.mark.parametrize("call", ["RAND()", "UUID()", "STRUUID()", "NOW()", "now ()"])
def test_a_function_whose_value_differs_on_every_call_is_refused(graph, call):
    """The tool's output must be the same each time a query runs, so random and clock functions are refused."""
    query = f"SELECT ({call} AS ?v) WHERE {{}}"
    assert tools_over(graph).query({"query": query}, timeout_s=5).error_class == "UnstableFunction"


def test_a_result_that_takes_longer_than_the_time_left_is_the_models_tool_error_not_the_stores_failure(graph):
    """Reading results past the deadline is a slow query, not a broken store: a tool error, never InfraError."""
    tools = tools_over(graph)
    late = tools.query({"query": "SELECT ?s WHERE { ?s ?p ?o }"}, timeout_s=-1)
    assert late.error_class == "QueryTimeout"


class FailingStore:
    """A store whose every read fails, as a store whose files went away would."""

    def query(self, text):
        raise OSError("gone")

    def triples(self):
        raise OSError("gone")

    def snapshot(self):
        raise OSError("gone")


def test_a_store_that_cannot_be_read_is_infrastructure_of_the_platforms_kind_never_a_tool_error(graph):
    tools = SparqlTools(FailingStore(), QueryLog(), max_rows=2)
    with pytest.raises(InfraError) as caught:
        tools.query({"query": "ASK { ?s ?p ?o }"}, timeout_s=5)
    assert caught.value.kind == STORE_KIND == "rdf_store"
    health = StoreHealth(GraphStore.load(graph))
    health.store = FailingStore()
    assert health() == Health(False, "rdf_store: the store could not be read (OSError)")
    with pytest.raises(InfraError):
        StoreSnapshot(FailingStore(), QueryLog()).start({})


def test_the_instrument_records_a_failed_end_reading_as_unknown_never_as_unchanged(graph):
    store = GraphStore.load(graph)
    instrument = StoreSnapshot(store, QueryLog())
    instrument.start({"run_key": "k", "factors": {}, "attempt": 1})
    instrument.store = FailingStore()
    snapshot = instrument.finish()
    assert snapshot["ok"] is False
    assert snapshot["end"] is None
    assert snapshot["unchanged"] is None
    assert snapshot["error"] == "OSError"
    assert snapshot["start"]["triples"] == 4


def test_the_platform_refuses_a_graph_with_blank_nodes_and_its_health_and_fingerprint_name_the_graph(graph, tmp_path):
    blank = tmp_path / "blank.ttl"
    blank.write_text('[] <http://x.example/p> "o" .\n', encoding="utf-8")
    with pytest.raises(ValueError, match="blank node"):
        GraphStore.load(blank)
    with pytest.raises(ValueError, match="not a graph file"):
        GraphStore.load(tmp_path / "graph.json")
    platform = SparqlPlatform(graph, max_rows=3)
    assert platform.health()() == Health(True, "the store answers and holds its 4 triples")
    assert platform.fingerprint() == {"platform": "sparql-platform", "max_rows": 3, "graph": platform.loaded}
    assert platform.loaded == GraphStore.load(graph).snapshot()  # the same file gives the same snapshot
