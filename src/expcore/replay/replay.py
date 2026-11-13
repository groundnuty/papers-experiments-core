# Provenance:
#   source: www-2027-experiments @ 46a5828ddb3276ba9582e4b4a6c2ae0f217e9da7, harness/agent/replay.py, blob a0f49e7173040376dd0ee101d3eabfbb4924a4f7
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Offline replay of a recorded attempt: no model, no endpoint, no platform (R03).

Given a record, `replay_record` runs the real agent loop again with every model turn, every tool result and every clock
reading served from the record (`recorded.py`), builds a record from what the loop derived, and compares it with the
stored one section by section (`compare.py`). It re-derives the transcript, the tool-call log, the outcome with its caps,
the totals and the clock, byte for byte, and stops at the first divergence with a `ReplayDivergence` that names the path.

The divergence points, in the order they are met:

- before the run: the stored configuration does not have its hash; a prompt text does not have its hash; the tool box
  rebuilt from the recorded specs does not have the recorded manifest hash;
- while the loop runs: the conversation of a request, or the settings it sent, do not have the digest the record pinned
  for them (R06); a model step, a tool call or a clock reading beyond the record; a tool call with other arguments; a
  result the loop re-renders differently from the text the model saw; recorded result messages that do not hold it;
- after the loop: recorded model requests, tool calls or clock readings left unused; any section of the replayed record
  that differs from the stored one.

The loop's own watch is replayed too (`clock.py`). The recorded provider and backend enter the waits on infrastructure
the record shows, of the recorded kinds, and leave open the ones the loop closed; the replay's sleep ends exactly when
the next recorded reading is a `watch` reading, so a call is cut at the reading that cut it; and the cleanup after a
cut is answered from the label of the reading that ended it. So the figures the timer derives from the readings, the
seconds waited by kind, the cleanup time and whether it timed out, come out as recorded. Waits are replayed for tool
providers and for backends alike.

Replay does not re-execute tool calls: a tool's output is served from the record, not recomputed. Scores are not
replayed: they are recomputed by running the scorers on the record (`expcore.records.rescore`). A record of an older
schema version is replayed under the defaults then in force (`config_of`). The stack hash of a harness defect, which
comes from a live traceback, is not compared (see `compare.py`).

Decisions served during replay: the core makes none yet. A study that records a decision and serves it back checks it
offline through the `recompute` hook of the replay check (`check.py`, R05).
"""

import asyncio
from collections.abc import Collection, Sequence
from typing import Any

from expcore.config.models import Limits
from expcore.loop.agent_loop import run_agent
from expcore.loop.config import AgentConfig
from expcore.loop.toolbox import ToolBox
from expcore.plugin import ToolSpec
from expcore.prompt.templates import sha256_of
from expcore.records.build import PromptText, RunIdentity, _build_record
from expcore.records.digest import digest
from expcore.records.record import RunRecord
from expcore.records.recording import RecordingBackend
from expcore.records.redact import Redactor
from expcore.records.schema import SCHEMAS, RecordError, SchemaVersion, config_defaults
from expcore.replay.clock import EXECUTOR_CLOCK_LABELS, ReplayClock
from expcore.replay.compare import compare
from expcore.replay.divergence import Halt, ReplayDivergence
from expcore.replay.recorded import RecordedBackend, RecordedToolProvider, ReplayToolBox
from expcore.replay.rendering import check_rendering

__all__ = ["ReplayDivergence", "config_of", "replay_async", "replay_record"]


def as_record(record: RunRecord | dict[str, Any], schemas: Sequence[SchemaVersion] = SCHEMAS) -> RunRecord:
    """Accept a record or its decoded JSON; the latter is validated against its own schema version."""
    return record if isinstance(record, RunRecord) else RunRecord.from_json(record, tuple(schemas))


def config_of(record: RunRecord, schemas: Sequence[SchemaVersion] = SCHEMAS) -> AgentConfig:
    """Return the agent configuration a record ran under.

    A setting the record's schema version predates is given the value that was in force before the version added it;
    a setting the record holds is used as recorded. A configuration that cannot be rebuilt is a divergence.
    """
    merged = {**config_defaults(record.schema_version, schemas), **record.agent_config}
    merged.pop("terminal_tools", None)  # the tool box adds it to the record; it follows from the recorded tools
    try:
        limits = Limits(**merged.pop("limits"))
        return AgentConfig(limits, **merged)
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplayDivergence("agent_config", f"the recorded configuration cannot be rebuilt ({exc})") from None


def _check_inputs(record: RunRecord, box: ToolBox) -> None:
    """Check what the record pins about the run before it starts: the configuration, the prompts and the tools."""
    if digest(record.agent_config) != record.agent_config_sha256:
        raise ReplayDivergence("agent_config_sha256", "the stored configuration does not have the stored hash")
    for part in ("system", "user"):
        entry = record.prompt[part]
        if sha256_of(entry["text"]) != entry["sha256"]:
            raise ReplayDivergence(f"prompt.{part}.sha256", "the prompt text does not have the recorded hash")
    check_rendering(record, box.cap_chars)
    if box.manifest_sha256 != record.prompt["tools_sha256"]:
        raise ReplayDivergence("prompt.tools_sha256", "the tools rebuilt from the record do not have the recorded hash")


def _check_used(backend: RecordedBackend, provider: RecordedToolProvider, clock: ReplayClock) -> None:
    """Check that the replay used every recorded model request, tool call and clock reading."""
    if backend.served != backend.expected_steps:
        raise ReplayDivergence(
            "transcript", f"the replay used {backend.served} of {backend.expected_steps} model requests"
        )
    if provider.served != provider.expected_calls:
        raise ReplayDivergence(
            "tool_calls", f"the replay ran {provider.served} of {provider.expected_calls} tool calls"
        )
    if clock.unused:
        raise ReplayDivergence("clock", f"{clock.unused} recorded clock readings were not read")


def _cleanup_waiter_for(clock: ReplayClock):
    """Return the cleanup waiter of a replay: it answers as the record says, from the label of the reading that ended the
    cleanup, and waits for the cancelled tasks to stop unless the record says the deadline ran out."""

    async def waiter(tasks: Collection[asyncio.Future], _deadline_s: float) -> bool:
        timed_out = clock.cleanup_timed_out()
        if tasks and not timed_out:
            await asyncio.wait(tasks)
        return timed_out

    return waiter


def _prompt(entry: dict[str, Any]) -> PromptText:
    """Rebuild a prompt text from its record entry."""
    return PromptText(entry["text"], entry["template_sha256"], entry["slots"])


async def replay_async(record: RunRecord | dict[str, Any], *, schemas: Sequence[SchemaVersion] = SCHEMAS) -> RunRecord:
    """Replay a record through the real loop and return the record the replay built; raise ReplayDivergence if it differs.

    Must run on an event loop; `replay_record` does that for synchronous callers.
    """
    rec = as_record(record, schemas)
    config = config_of(rec, schemas)
    clock = ReplayClock(rec.clock)
    timer = config.timer(clock, cleanup_waiter=_cleanup_waiter_for(clock))
    backend, provider = RecordedBackend(rec, clock, timer.infrastructure_wait), RecordedToolProvider(rec, clock)
    try:
        box = ReplayToolBox([provider], cap_chars=config.limits.output_cap_chars, recorded=rec.tool_calls)
    except ValueError as exc:
        raise ReplayDivergence("tools", f"the recorded tools cannot be offered ({exc})") from None
    _check_inputs(rec, box)
    recording = RecordingBackend(backend)

    async def sleep_until_a_watch_is_due(_seconds: float) -> None:
        await clock.watch_due.wait()

    try:
        result = await run_agent(
            recording,
            box,
            rec.prompt["system"]["text"],
            rec.prompt["user"]["text"],
            config,
            timer=timer,
            clock=clock,
            sleep=sleep_until_a_watch_is_due,
        )
    except Halt as halt:
        raise halt.divergence from None
    _check_used(backend, provider, clock)
    # the readings an executor took before the loop started the clock are not the loop's: serve them back as recorded
    result.clock_readings[:0] = [(label, value) for label, value in rec.clock if label in EXECUTOR_CLOCK_LABELS]
    try:
        replayed = _build_record(
            result,
            recording,
            identity=RunIdentity(rec.run_key, rec.factors, rec.attempt),
            system=_prompt(rec.prompt["system"]),
            user=_prompt(rec.prompt["user"]),
            specs=[ToolSpec(**spec) for spec in rec.tools],
            code=rec.code,
            core_version=rec.core_version,
            instruments=rec.instruments,
            instrument_refusal=rec.instrument_refusal,  # carried over like the instruments; absent when absent
            reserved_capped=rec.totals.get("reserved_capped"),  # the campaign's, not the loop's: served as recorded
            reply_cap_unheld=rec.totals.get("reply_cap_unheld"),
            treatment_kinds=rec.treatment_kinds,
            treatment_targets=rec.treatment_targets,
            schema_version=rec.schema_version,  # the rule of the record's own version decides its treatment flag
            schemas=schemas,
            redactor=Redactor.none(),  # the record is redacted already; what the loop derives from it is too
        )
    except RecordError as exc:
        raise ReplayDivergence("transcript", str(exc)) from None
    for entry in replayed.tool_calls:  # the replay's own copy: a call served as recorded stays marked as such
        if entry["call_id"] in {c["call_id"] for c in rec.tool_calls if not c.get("rendering_verified", True)}:
            entry["rendering_verified"] = False
    compare(rec, replayed, schemas)
    return replayed


def replay_record(record: RunRecord | dict[str, Any], *, schemas: Sequence[SchemaVersion] = SCHEMAS) -> RunRecord:
    """Replay a record from synchronous code. Not for use inside a running event loop; await `replay_async` there."""
    return asyncio.run(replay_async(record, schemas=schemas))
