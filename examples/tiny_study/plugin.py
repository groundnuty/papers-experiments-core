"""The plugin of the tiny study: the tools, the instrument, the scorer, the executor, and a stub platform's health and
resources.

The core calls `build(context)` once and expects a `StudyPlugin`. Everything here is the study's own code; the core
imports nothing from this file by name. Copy the file, change the names and the behaviour, keep the shape.
"""

import asyncio
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from expcore.campaign.executor import RunRequest, record_run
from expcore.campaign.healthgate import Health
from expcore.errors import InfraError
from expcore.models.adapters import ModelTurn, ToolCall
from expcore.plugin import CallContext, ToolOutput, ToolSpec
from expcore.prompt.templates import PromptTemplate
from expcore.records.instruments import finish_instruments, start_instruments
from expcore.records.record import RunRecord
from expcore.study.plugin import StudyContext, StudyPlugin, tools_sha256
from expcore.tools.function_provider import FunctionProvider

HERE = Path(__file__).resolve().parent

# The one fact each task asks for: the scorer's answer key. It is part of the code identity.
FACTS = {"lake": "deep", "river": "long", "hill": "green", "dune": "moving"}
HINTS = {  # a prompt slot may not be blank, so the basic level says something short
    "basic": "Be brief.",
    "extended": "The lookup tool also takes a `detail` argument; ask for the detail you need and one call is enough.",
}

# --- the tools ---------------------------------------------------------------------------------------------------


def lookup(arguments: dict[str, Any], *, timeout_s: float, ctx: CallContext) -> ToolOutput:
    """Look a topic up. A plain function runs in a worker thread, so it takes `timeout_s` and may take `ctx`."""
    with ctx.infrastructure_wait("toolserver_lifecycle"):  # the tool server's start-up is not the agent's time
        time.sleep(0)  # a real tool starts its server here
    topic = arguments.get("topic")
    if topic not in FACTS:
        return ToolOutput(f"no such topic: {topic!r}", is_error=True, error_class="UnknownTopic")
    return ToolOutput(f"The key fact about {topic} is: {FACTS[topic]}.", source_truncated=False)


class Shelf:
    """The stub platform's side of one run: it serves `lookup` and counts the facts it handed out.

    A real platform keeps such state itself (a server's log, a block map); the stub keeps it here, one shelf per run.
    """

    def __init__(self) -> None:
        self.served = 0

    def lookup(self, arguments: dict[str, Any], *, timeout_s: float, ctx: CallContext) -> ToolOutput:
        """Serve one lookup, and count it when it handed out a fact."""
        result = lookup(arguments, timeout_s=timeout_s, ctx=ctx)
        if not result.is_error:
            self.served += 1
        return result


def submit(arguments: dict[str, Any], *, timeout_s: float) -> str:
    """Take the answer. The spec marks this tool terminal, so a successful call ends the attempt."""
    return f"received: {arguments.get('answer')}"


def tool_specs(tools: str) -> list[ToolSpec]:
    """The tools of a `tools` level: `extended` adds an optional argument to `lookup`."""
    properties: dict[str, Any] = {"topic": {"type": "string"}}
    if tools == "extended":
        properties["detail"] = {"type": "string"}
    return [
        ToolSpec("lookup", "Look up the key fact about a topic.", {"type": "object", "properties": properties}),
        ToolSpec(
            "submit",
            "Give the final answer.",
            {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
            terminal=True,
        ),
    ]


def provider_for(tools: str, shelf: Shelf) -> FunctionProvider:
    """A fresh tool provider for one run (a provider holds the state of its calls, so runs never share one)."""
    lookup_spec, submit_spec = tool_specs(tools)
    return FunctionProvider([(lookup_spec, shelf.lookup), (submit_spec, submit)])


# --- the instrument -----------------------------------------------------------------------------------------------


class FactsServed:
    """An instrument: how many facts the platform handed out in one run, by the platform's own count.

    The executor makes a fresh one per run, since it holds that run's reading between `start` and `finish`, and runs
    can overlap. `start` takes the reading `finish` compares with; `identity` is the run's identity as a plain mapping
    (`run_key`, `factors`, `attempt`), which this count does not need. `finish` returns the snapshot that goes into
    `record.instruments["facts"]`, the only platform state a scorer reads. Both run outside the agent's time.
    """

    namespace = "facts"

    def __init__(self, shelf: Shelf) -> None:
        self.shelf = shelf
        self.before = 0

    def start(self, identity: Mapping[str, Any]) -> None:
        self.before = self.shelf.served

    def finish(self) -> dict[str, Any]:
        return {"ok": True, "served": self.shelf.served - self.before}


# --- the stub model's script --------------------------------------------------------------------------------------


def malformed() -> ModelTurn:
    """A reply whose tool call carries arguments that are not JSON; the loop reports it and the model goes on (L01)."""
    call = ToolCall("call_bad", "lookup", {}, "arguments are not valid JSON (Expecting value)", '{"topic":')
    return ModelTurn("", [call], 10, 5, "tool_calls", 0.0)


# The runs that do something other than finish with the right answer, by (condition, task). The right models are
# alpha-stub; beta-stub also answers wrongly on the runs in WRONG_FOR_BETA.
SPECIAL = {
    ("relay-down", "lake"): "infrastructure",  # the declared treatment: the tool server fails after the first lookup
    ("baseline", "river"): "malformed",  # one malformed call, then the model recovers
    ("baseline", "hill"): "endless",  # keeps looking things up: ends at the turn cap
}
WRONG_FOR_BETA = {("baseline", "lake"), ("baseline", "dune"), ("extended", "hill"), ("relay-down", "hill")}


def script_for(condition: str, task: str, model: str, tools: str) -> list[Any]:
    """The turns the stub plays for one run: a pure function of the run's identity, so a rerun plays the same."""
    detail = {"detail": "key"} if tools == "extended" else {}
    ask = [("lookup", {"topic": task, **detail})]
    kind = SPECIAL.get((condition, task), "finish")
    wrong = model == "beta-stub" and (condition, task) in WRONG_FOR_BETA
    answer = "unknown" if wrong else FACTS[task]
    if kind == "infrastructure":  # one completed turn first: a failure before the agent's first turn is never treatment
        return [ask, InfraError("knowledge", "the tool server went down after the first lookup")]
    if kind == "endless":
        return [ask] * 8
    done = [("submit", {"answer": answer})]
    if kind == "malformed":
        return [malformed(), ask, done]
    return [ask, *([ask] if tools == "basic" else []), done]  # the basic level looks twice, the extended once


# --- the scorer ---------------------------------------------------------------------------------------------------


class TinyScorer:
    """A pure function of a record: two axes, a verdict and named numeric outcomes (a missing one is None).

    A treatment attempt (under `relay-down`, the tool server failed after the agent's first turn) is scored like any
    other run: the failure is the condition's manipulation, so what the agent achieved under it is the measurement.
    It gave no answer, so its accuracy is 0, and its rounds and lookups are what it did before the failure. None means
    only that an outcome could not be measured: it would drop the run's pair from every contrast on that outcome, and a
    confirmatory contrast is refused for that, even one that declares `treatment_outcomes = "scored"`, past the number
    its preregistration allows in `max_unscored_treatment`.

    `served` is read from the instrument's snapshot, not from the tool calls: the platform's own count of the facts it
    handed out. A snapshot that failed (`"ok": false`) or is missing gives None, never 0.
    """

    name = "tiny"
    version = "1"

    def score(self, record: RunRecord) -> dict[str, Any]:
        calls = record.tool_calls
        outcome = record.outcome
        looked = [c for c in calls if c["name"] == "lookup"]
        if outcome["name"] == "infrastructure" and not outcome["treatment"]:
            # A failure that is not the condition's treatment is set aside and run again, and the core never scores
            # it; were it scored, nothing the agent did could be judged, so every outcome is unmeasured.
            return {
                "axes": {"agent": "no_answer", "platform": "failed"},
                "verdict": "not_determined",
                "outcomes": {"accuracy": None, "rounds": None, "lookups": None, "served": None},
            }
        failed = outcome["name"] == "infrastructure" or not (looked and all(c["status"] == "ok" for c in looked))
        submitted = [c for c in calls if c["name"] == "submit" and c["status"] == "ok"]
        correct = bool(submitted) and submitted[-1]["arguments"].get("answer") == FACTS[record.factors["task"]]
        facts = record.instruments.get(FactsServed.namespace, {})
        return {
            "axes": {"agent": "answered" if submitted else "no_answer", "platform": "failed" if failed else "served"},
            "verdict": "fulfilled" if correct else "violated",
            "outcomes": {
                "accuracy": 1.0 if correct else 0.0,  # a treatment attempt gave no answer: 0, never None
                "rounds": float(record.totals["rounds"]),
                "lookups": float(len(looked)),
                "served": float(facts["served"]) if facts.get("ok") is True else None,  # a failed reading: unmeasured
            },
        }


# --- the executor -------------------------------------------------------------------------------------------------


class TinyExecutor:
    """Runs one attempt: script the stub, render the prompt, start the instrument, run the loop, finish the instrument,
    write the record, return it."""

    def __init__(self, context: StudyContext) -> None:
        self.context = context
        self.template = PromptTemplate.from_file(HERE / "prompt.txt")

    async def __call__(self, request: RunRequest) -> RunRecord:
        study, factors = self.context.study, request.identity.factors
        condition = study.schema.condition_named(factors, request.run_key)
        backend = request.backend  # built by the scheduler from the admitted endpoint; never build one here
        if not callable(getattr(backend, "use", None)):  # the method this executor needs, whatever class serves it
            raise TypeError("the tiny study needs a scripted backend, one with use(script), as the stub has")
        backend.use(script_for(condition, factors["task"], factors["model"], factors["tools"]))
        user = self.template.render({"TASK": factors["task"], "HINT": HINTS[factors["tools"]]}, study_run=True)
        shelf = Shelf()
        instruments = [FactsServed(shelf)]  # fresh for every run: an instrument holds one run's reading
        identity = {"run_key": request.run_key, "factors": dict(factors), "attempt": request.identity.attempt}
        await asyncio.to_thread(start_instruments, instruments, identity)  # blocking I/O stays off the event loop
        record = await record_run(
            request,
            [provider_for(factors["tools"], shelf)],
            "You are a careful assistant.",
            user,
            study.campaign.agent,
            code=dict(self.context.code),
        )
        # The loop returns a record however the run ended, an infrastructure failure included, so the snapshot is
        # taken for every attempt that has a record. finish_instruments returns a new record: write that one.
        record = await asyncio.to_thread(finish_instruments, record, instruments)
        record.write(request.run_dir)  # the campaign checks, exports and analyses records that are on disk
        return record


# --- the platform's health and resources (a stub platform that is always fine) -----------------------------------


def healthy() -> Health:
    """The health check the scheduler asks before a run starts and after it ends."""
    return Health(True, "the stub platform is always up")


class StubProbe:
    """Answers what a platform would about a holder and its resources; here every holder and resource is gone."""

    def holder_gone(self, owner: str) -> bool | None:
        return True

    def resource_gone(self, key: str, note: Mapping[str, Any]) -> bool:
        return True


def lease_keys(group: str) -> Sequence[str]:
    """The keys of a lease group: two stand-ins for a platform's per-run principals."""
    return ["slot-1", "slot-2"]


def build(context: StudyContext) -> StudyPlugin:
    """What the core calls: assemble the study's executor, scorer, hashes and platform hooks."""
    study = context.study
    template = PromptTemplate.from_file(HERE / "prompt.txt")
    levels = {name: condition["tools"] for name, condition in study.schema.conditions.items()}
    return StudyPlugin(
        executor=TinyExecutor(context),
        scorers=[TinyScorer()],
        prompts={
            c: template.render({"TASK": "task", "HINT": HINTS[level]}).rendered_sha256 for c, level in levels.items()
        },
        tools={c: tools_sha256(tool_specs(level)) for c, level in levels.items()},
        health=healthy,
        probe=StubProbe(),
        lease_group=lambda factors: "stub-platform",
        lease_keys=lease_keys,
    )
