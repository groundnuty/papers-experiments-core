"""The plugin of the orchard study, a small study on the SPARQL platform.

The platform (`sparql_platform`, an installed distribution named in `[study] requires`) gives the store, the read-only
query tools, the instrument and the health check. The study gives the graph, the questions, the answer key, the
terminal `submit` tool, the stub model's scripts, the scorer and the executor.
"""

import asyncio
from pathlib import Path
from typing import Any

from sparql_platform import NAMESPACE, QUERY_TOOL, SCHEMA_TOOL, SparqlPlatform

from expcore.campaign.executor import RunRequest, record_run
from expcore.plugin import ToolSpec
from expcore.prompt.templates import PromptTemplate
from expcore.records.instruments import finish_instruments, start_instruments
from expcore.records.record import RunRecord
from expcore.study.plugin import StudyContext, StudyPlugin, tools_sha256
from expcore.tools.function_provider import FunctionProvider

HERE = Path(__file__).resolve().parent
MAX_ROWS = 8  # the platform's cap on the rows of one result

# --- the questions and the answer key (part of the code identity) -------------------------------------------------

QUESTIONS = {
    "marrowby-crew": "Which crew tends the grove labelled Marrowby?",
    "oldest-fruit": "What fruit does the oldest tree bear?",
    "quillfen-count": "How many trees grow in the grove labelled Quillfen?",
    "thistledown-fruits": "How many different fruits grow in the grove labelled Thistledown?",
}
KEY = {"marrowby-crew": "Finch crew", "oldest-fruit": "quince", "quillfen-count": "7", "thistledown-fruits": "4"}
HINTS = {
    "query_only": "Only the query tool is available.",
    "query_and_schema": "The list_predicates tool shows which predicates the graph uses.",
}
SYSTEM = "You are a careful assistant."

# --- the study's own tool: the answer ------------------------------------------------------------------------------

SUBMIT = ToolSpec(
    "submit",
    "Give the final answer.",
    {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
    terminal=True,
)


def submit(arguments: dict[str, Any], *, timeout_s: float) -> str:
    """Take the answer. The spec marks the tool terminal, so a successful call ends the attempt."""
    return f"received: {arguments.get('answer')}"


def schema_shown(exposure: str) -> bool:
    return exposure == "query_and_schema"


def tool_manifest(exposure: str) -> list[ToolSpec]:
    """What the model sees under an exposure level, in the order the providers show it: the platform's, then submit."""
    return [*SparqlPlatform.tool_specs(schema=schema_shown(exposure)), SUBMIT]


# --- the stub model's scripts --------------------------------------------------------------------------------------

PREFIXES = "PREFIX o: <http://orchard.example/vocab#> PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
RIGHT = {
    "marrowby-crew": 'SELECT ?crew WHERE { ?g rdfs:label "Marrowby" ; o:tendedBy ?c . ?c rdfs:label ?crew }',
    "oldest-fruit": "SELECT ?fruit ?year WHERE { ?t o:fruit ?fruit ; o:planted ?year } ORDER BY ?year LIMIT 1",
    "quillfen-count": 'SELECT (COUNT(?t) AS ?trees) WHERE { ?g rdfs:label "Quillfen" . ?t o:grove ?g }',
    "thistledown-fruits": (
        "SELECT (COUNT(DISTINCT ?fruit) AS ?fruits) "
        'WHERE { ?g rdfs:label "Thistledown" . ?t o:grove ?g ; o:fruit ?fruit }'
    ),
}
# Without the schema tool the agent first guesses a predicate the graph does not use (no rows), or, for the last task,
# lists every tree, which the platform cuts at MAX_ROWS.
FIRST_TRY = {
    "marrowby-crew": 'SELECT ?crew WHERE { ?g o:name "Marrowby" ; o:crew ?crew }',
    "oldest-fruit": "SELECT ?fruit WHERE { ?t o:age ?age ; o:fruit ?fruit } ORDER BY DESC(?age) LIMIT 1",
    "quillfen-count": 'SELECT (COUNT(?t) AS ?trees) WHERE { ?t o:in "Quillfen" }',
    "thistledown-fruits": 'SELECT ?t ?fruit WHERE { ?g rdfs:label "Thistledown" . ?t o:grove ?g ; o:fruit ?fruit }',
}
EXPLORE = "SELECT DISTINCT ?p WHERE { ?s ?p ?o } ORDER BY ?p"
UPDATE = "INSERT DATA { <http://orchard.example/grove/marrowby> o:tendedBy <http://orchard.example/crew/wren> }"
REMOTE = "SELECT * WHERE { SERVICE <http://elsewhere.example/sparql> { ?s ?p ?o } }"
BROKEN = "SELEC ?fruit WHERE { ?t o:fruit ?fruit }"

# What some runs do first, by (condition, task, model): an update and a SERVICE call, both refused, and a syntax error.
DETOUR = {
    ("bare", "marrowby-crew", "beta-stub"): UPDATE,
    ("guided", "quillfen-count", "beta-stub"): REMOTE,
    ("guided", "oldest-fruit", "alpha-stub"): BROKEN,
}
WRONG = {("bare", "oldest-fruit", "beta-stub"): "plum", ("guided", "thistledown-fruits", "beta-stub"): "3"}


def ask(query: str) -> list[tuple[str, dict[str, Any]]]:
    return [(QUERY_TOOL, {"query": PREFIXES + query})]


def script_for(condition: str, task: str, model: str) -> list[Any]:
    """The turns the stub plays for one run: a pure function of the run's identity, so a rerun plays the same."""
    detour = DETOUR.get((condition, task, model))
    turns = [ask(detour)] if detour else []
    if condition == "guided":
        turns += [[(SCHEMA_TOOL, {})], ask(RIGHT[task])]
    elif task == "thistledown-fruits":
        turns += [ask(EXPLORE), ask(FIRST_TRY[task]), ask(RIGHT[task])]
    else:
        turns += [ask(FIRST_TRY[task]), ask(EXPLORE), ask(RIGHT[task])]
    answer = WRONG.get((condition, task, model), KEY[task])
    return [*turns, [("submit", {"answer": answer})]]


# --- the scorer ----------------------------------------------------------------------------------------------------


class OrchardScorer:
    """A pure function of a record: whether the answer was right, how many queries and rounds it took, and what the
    platform's snapshot says it served and whether the store stayed as it was. A snapshot that failed or is missing
    leaves those outcomes None, never 0."""

    name = "orchard"
    version = "1"

    def score(self, record: RunRecord) -> dict[str, Any]:
        outcome = record.outcome
        unmeasured = {"accuracy": None, "queries": None, "rounds": None, "rows_served": None, "unchanged": None}
        if outcome["name"] == "infrastructure" and not outcome["treatment"]:
            return {
                "axes": {"agent": "no_answer", "platform": "failed"},
                "verdict": "not_determined",
                "outcomes": unmeasured,
            }
        calls = record.tool_calls
        submitted = [c for c in calls if c["name"] == "submit" and c["status"] == "ok"]
        answer = submitted[-1]["arguments"].get("answer") if submitted else None
        correct = answer == KEY[record.factors["task"]]
        snapshot = record.instruments.get(NAMESPACE, {})
        read = snapshot.get("ok") is True
        return {
            "axes": {"agent": "answered" if submitted else "no_answer", "platform": "read" if read else "unread"},
            "verdict": "fulfilled" if correct else "violated",
            "outcomes": {
                "accuracy": 1.0 if correct else 0.0,
                "queries": float(sum(1 for c in calls if c["name"] == QUERY_TOOL)),
                "rounds": float(record.totals["rounds"]),
                "rows_served": float(snapshot["served"]["rows_served"]) if read else None,
                "unchanged": float(snapshot["unchanged"]) if read else None,
            },
        }


# --- the executor --------------------------------------------------------------------------------------------------


class OrchardExecutor:
    """Runs one attempt: script the stub, render the prompt, open the run's platform session, start its instrument,
    run the loop, finish the instrument, write the record, return it."""

    def __init__(self, context: StudyContext, platform: SparqlPlatform) -> None:
        self.context, self.platform = context, platform
        self.template = PromptTemplate.from_file(HERE / "prompt.txt")

    async def __call__(self, request: RunRequest) -> RunRecord:
        study, factors = self.context.study, request.identity.factors
        condition = study.schema.condition_named(factors, request.run_key)
        backend = request.backend  # built by the scheduler; never build one here
        if not callable(getattr(backend, "use", None)):
            raise TypeError("the orchard study needs a scripted backend, one with use(script), as the stub has")
        backend.use(script_for(condition, factors["task"], factors["model"]))
        exposure = factors["exposure"]
        user = self.template.render({"QUESTION": QUESTIONS[factors["task"]], "HINT": HINTS[exposure]}, study_run=True)
        session = self.platform.session()  # one per run: its log and its instrument are this run's
        instruments = [session.instrument()]
        identity = {"run_key": request.run_key, "factors": dict(factors), "attempt": request.identity.attempt}
        await asyncio.to_thread(start_instruments, instruments, identity)
        providers = [session.provider(schema=schema_shown(exposure)), FunctionProvider([(SUBMIT, submit)])]
        record = await record_run(request, providers, SYSTEM, user, study.campaign.agent, code=dict(self.context.code))
        record = await asyncio.to_thread(finish_instruments, record, instruments)
        record.write(request.run_dir)
        return record


def build(context: StudyContext) -> StudyPlugin:
    """What the core calls: load the graph into the platform, and assemble the executor, scorer, hashes and health."""
    platform = SparqlPlatform(HERE / "orchard.ttl", max_rows=MAX_ROWS)
    template = PromptTemplate.from_file(HERE / "prompt.txt")
    levels = {name: condition["exposure"] for name, condition in context.study.schema.conditions.items()}
    return StudyPlugin(
        executor=OrchardExecutor(context, platform),
        scorers=[OrchardScorer()],
        prompts={
            c: template.render({"QUESTION": "question", "HINT": HINTS[level]}).rendered_sha256
            for c, level in levels.items()
        },
        tools={c: tools_sha256(tool_manifest(level)) for c, level in levels.items()},
        health=platform.health(),
        platform=platform.fingerprint(),
    )
