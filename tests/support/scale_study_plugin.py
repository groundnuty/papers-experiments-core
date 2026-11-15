"""The plugin of the scale study (`shard_scale_support`), copied into every copy of that study as its `plugin.py`.

Four stub models answer one fact per task in two arms, `plain` and `guided`. Whether a run answers rightly, and how many
lookups it makes first, is a pure function of the run's task, arm, model and seed (a SHA-256 of them), so every driver
that runs a run plays the same script and the records of two drivers score alike.

Two faults are planted from the driver's environment, read when an attempt starts, never at `build`: so they are no
part of the campaign fingerprint, and one shard of a campaign can meet them while its other shards do not, as one host
of several would.

- `SCALE_INFRA_RUNS`: a JSON list of run keys whose first attempt fails before the agent's first turn, as a platform
  whose tool server is down fails it (kind `knowledge`, which no condition declares, so it is never the scheduler's own
  health kind): the attempt is set aside and the run is tried again;
- `SCALE_HEAVY`: when set, every model turn reports a large prompt, so the runs of that driver cost far more than the
  others, and its share of the budget runs out first.

The stub platform has no leases: a lease pool is each host's own, under its `out/`, and the tiny study's rehearsals
exercise it; here it would only add to every run's time.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from expcore.campaign.executor import RunRequest, record_run
from expcore.campaign.healthgate import Health
from expcore.errors import InfraError
from expcore.models.adapters import ModelTurn, ToolCall
from expcore.plugin import CallContext, ToolOutput, ToolSpec
from expcore.prompt.templates import PromptTemplate
from expcore.records.record import RunRecord
from expcore.study.plugin import StudyContext, StudyPlugin, tools_sha256
from expcore.tools.function_provider import FunctionProvider

HERE = Path(__file__).resolve().parent
INFRA_RUNS = "SCALE_INFRA_RUNS"
HEAVY = "SCALE_HEAVY"
HEAVY_PROMPT_TOKENS = 6000
SKILL = {
    "alpha-stub": 10,
    "beta-stub": 0,
    "gamma-stub": -5,
    "delta-stub": 5,
}  # the models answer rightly this much more
HINTS = {"plain": "Be brief.", "guided": "Look the fact up once, then submit it exactly as the lookup gives it."}


def fact_of(task: str) -> str:
    """The one fact a task asks for: the scorer's answer key."""
    return f"fact-of-{task}"


def lookup(arguments: dict[str, Any], *, timeout_s: float, ctx: CallContext) -> ToolOutput:
    """Look a task's fact up."""
    topic = arguments.get("topic")
    if not isinstance(topic, str) or not topic:
        return ToolOutput(f"no such topic: {topic!r}", is_error=True, error_class="UnknownTopic")
    return ToolOutput(f"The key fact about {topic} is: {fact_of(topic)}.", source_truncated=False)


def submit(arguments: dict[str, Any], *, timeout_s: float) -> str:
    """Take the answer; the spec marks this tool terminal."""
    return f"received: {arguments.get('answer')}"


TOOL_SPECS = [
    ToolSpec(
        "lookup", "Look up the key fact about a topic.", {"type": "object", "properties": {"topic": {"type": "string"}}}
    ),
    ToolSpec(
        "submit",
        "Give the final answer.",
        {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
        terminal=True,
    ),
]


def draw(*parts: Any) -> int:
    """A number from 0 to 99, a pure function of the parts."""
    text = json.dumps(list(parts), separators=(",", ":"))
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16) % 100


def script_for(arm: str, task: str, model: str, seed: int, *, heavy: bool) -> list[Any]:
    """The turns the stub plays for one run: one or two lookups, then the answer, right or wrong."""
    chance = 55 + SKILL[model] + (15 if arm == "guided" else 0)
    right = draw("answer", arm, task, model, seed) < chance
    looks = 1 if arm == "guided" or draw("looks", task, model, seed) < 50 else 2
    answer = fact_of(task) if right else "unknown"
    turns: list[Any] = [[("lookup", {"topic": task})] for _ in range(looks)] + [[("submit", {"answer": answer})]]
    if not heavy:
        return turns
    return [
        ModelTurn("", [ToolCall(f"call_{n}", *turn[0])], HEAVY_PROMPT_TOKENS, 20, "tool_calls", 0.0)
        for n, turn in enumerate(turns)
    ]


def infra_runs() -> frozenset[str]:
    """The run keys whose first attempt this driver's platform fails, from its environment."""
    return frozenset(json.loads(os.environ.get(INFRA_RUNS, "[]")))


class ScaleScorer:
    """A pure function of a record: the answer's accuracy and the rounds it took."""

    name = "scale"
    version = "1"

    def score(self, record: RunRecord) -> dict[str, Any]:
        submitted = [c for c in record.tool_calls if c["name"] == "submit" and c["status"] == "ok"]
        correct = bool(submitted) and submitted[-1]["arguments"].get("answer") == fact_of(record.factors["task"])
        return {
            "axes": {"agent": "answered" if submitted else "no_answer"},
            "verdict": "fulfilled" if correct else "violated",
            "outcomes": {"accuracy": 1.0 if correct else 0.0, "rounds": float(record.totals["rounds"])},
        }


class ScaleExecutor:
    """Runs one attempt: the stub's script, the prompt, the loop, the record."""

    def __init__(self, context: StudyContext) -> None:
        self.context = context
        self.template = PromptTemplate.from_file(HERE / "prompt.txt")

    async def __call__(self, request: RunRequest) -> RunRecord:
        study, factors = self.context.study, request.identity.factors
        backend = request.backend
        arm, task = factors["arm"], factors["task"]
        script = script_for(arm, task, factors["model"], factors["seed"], heavy=bool(os.environ.get(HEAVY)))
        if request.identity.attempt == 1 and request.run_key in infra_runs():
            script = [InfraError("knowledge", "the tool server of this host is down")]  # before the first turn
        backend.use(script)
        slots = {"TASK": task, "REGION": study.tasks[task].attributes["region"], "HINT": HINTS[arm]}
        user = self.template.render(slots, study_run=True)
        provider = FunctionProvider([(TOOL_SPECS[0], lookup), (TOOL_SPECS[1], submit)])
        record = await record_run(
            request,
            [provider],
            "You are a careful assistant.",
            user,
            study.campaign.agent,
            code=dict(self.context.code),
        )
        record.write(request.run_dir)
        return record


def healthy() -> Health:
    return Health(True, "the stub platform is always up")


def build(context: StudyContext) -> StudyPlugin:
    study = context.study
    template = PromptTemplate.from_file(HERE / "prompt.txt")
    stand_ins = {"TASK": "task", "REGION": "region"}
    arms = {name: condition["arm"] for name, condition in study.schema.conditions.items()}
    return StudyPlugin(
        executor=ScaleExecutor(context),
        scorers=[ScaleScorer()],
        prompts={c: template.render({**stand_ins, "HINT": HINTS[arm]}).rendered_sha256 for c, arm in arms.items()},
        tools=dict.fromkeys(arms, tools_sha256(TOOL_SPECS)),
        health=healthy,
    )
