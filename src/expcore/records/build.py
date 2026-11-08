# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/harness/runner.py:413-431
"""Turn a finished attempt into a record.

`record_attempt` runs one attempt through the loop with the backend wrapped for recording and returns the record.
`build_record` is the same step for a caller that ran the loop itself. The record is built the same way whatever ended
the attempt, so an infrastructure failure writes a partial record: the turns so far, the request that failed, the usage
seen, and no score (R09). Replay builds its own record through this function, so the two are compared like with like.

Exception text is stable (R07): a class name and, only for the core's own exceptions, their fixed fields. The text a
library puts in an exception differs between interpreter versions and would stop a record replaying elsewhere.
"""

import asyncio
import dataclasses
import time
import warnings
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from expcore.loop.agent_loop import run_agent
from expcore.loop.config import AgentConfig
from expcore.loop.outcomes import Outcome
from expcore.loop.records import LoopResult, TurnRecord
from expcore.loop.timer import AgentTimer
from expcore.loop.toolbox import ToolBox
from expcore.models.adapters import ModelBackend
from expcore.models.route import sdk_version
from expcore.plugin import ToolProvider, ToolSpec
from expcore.prompt.templates import RenderedPrompt, sha256_of
from expcore.records.digest import digest, plain
from expcore.records.record import RunRecord
from expcore.records.recording import Captured, RecordingBackend
from expcore.records.redact import Redactor
from expcore.records.schema import CURRENT, RecordError
from expcore.records.seal import seal

CORE_DISTRIBUTION = "papers-experiments-core"
CONTEXT_OVERFLOW = "ContextOverflow"
INFRA_PREFIX = "infrastructure: "


@dataclass(frozen=True)
class RunIdentity:
    """Who the attempt is: the run key, the factor values it was derived from, and the attempt number."""

    run_key: str
    factors: Mapping[str, Any]
    attempt: int


@dataclass(frozen=True)
class PromptText:
    """A prompt as given to the model, with the template hash and slot values when it was rendered from a template."""

    text: str
    template_sha256: str | None = None
    slots: Mapping[str, str] | None = None

    @classmethod
    def of(cls, prompt: "str | RenderedPrompt | PromptText") -> "PromptText":
        """Accept a plain string, a rendered template or a prompt text."""
        if isinstance(prompt, PromptText):
            return prompt
        if isinstance(prompt, RenderedPrompt):
            return cls(prompt.text, prompt.template_sha256, dict(prompt.slots))
        return cls(prompt)

    def entry(self) -> dict[str, Any]:
        """Return the prompt as the record keeps it."""
        slots = None if self.slots is None else dict(self.slots)
        return {
            "text": self.text,
            "sha256": sha256_of(self.text),
            "template_sha256": self.template_sha256,
            "slots": slots,
        }


def stable_detail(outcome: Outcome) -> str:
    """Return the outcome's detail line without library exception text (R07).

    Caps, malformed endings and infrastructure details are written by the core and kept. For an error the model
    endpoint or a library raised, and for a defect, the detail is the exception class; the message is not stored. A
    context overflow keeps its detail, which holds only the status and the matched pattern.
    """
    if outcome.name == "error":
        return outcome.detail if outcome.error_type == CONTEXT_OVERFLOW else str(outcome.error_type)
    if outcome.name == "harness_defect" and outcome.defect is not None:
        return outcome.defect.exception
    return outcome.detail


def stable_call_detail(status: str, detail: str) -> str:
    """Return a tool call's detail line without library exception text: a crash keeps its class name only (R07)."""
    if status == "failed" and not detail.startswith(INFRA_PREFIX):
        return detail.split(":", 1)[0]
    return detail


def outcome_entry(outcome: Outcome) -> dict[str, Any]:
    """Return the outcome as the record keeps it."""
    defect = outcome.defect
    return {
        "name": outcome.name,
        "detail": stable_detail(outcome),
        "infra_kind": outcome.infra_kind,
        "error_type": outcome.error_type,
        "defect": None if defect is None else {"exception": defect.exception, "stack_sha256": defect.stack_sha256},
    }


def tool_entry(spec: ToolSpec) -> dict[str, Any]:
    """Return a tool spec as the record keeps it: as the provider gave it, before the cap sentence is added."""
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": plain(spec.input_schema),
        "terminal": spec.terminal,
    }


def call_entry(call: Any) -> dict[str, Any]:
    """Return a tool-call log entry as the record keeps it."""
    entry = plain(dataclasses.asdict(call))
    entry["detail"] = stable_call_detail(entry["status"], entry["detail"])
    return entry


def turn_entry(index: int, captured: Captured, settings_sha256: str, loop_turn: TurnRecord) -> dict[str, Any]:
    """Return one model turn as the record keeps it: the neutral and the native message, and what surrounded them."""
    turn = captured.turn
    return {
        "index": index,
        "neutral": {
            "text": turn.text,
            "reasoning": turn.reasoning,
            "tool_calls": [plain(dataclasses.asdict(c)) for c in turn.tool_calls],
        },
        "native": captured.native,
        "tool_messages": captured.tool_messages,
        "request_sha256": "",  # filled in by `seal`, over the redacted conversation
        "settings_sha256": settings_sha256,
        "stop_reason": turn.stop_reason,
        "usage": {
            "input_tokens": turn.input_tokens,
            "output_tokens": turn.output_tokens,
            "uncached_input_tokens": turn.uncached_input_tokens,
            "cache_creation_input_tokens": turn.cache_creation_input_tokens,
            "cache_read_input_tokens": turn.cache_read_input_tokens,
        },
        "latency_s": turn.latency_s,
        "served_model": turn.served_model,
        "system_fingerprint": turn.system_fingerprint,
        "retries": plain(list(turn.retries)),
        "waits_left_open": loop_turn.waits_left_open,
        "infra_wait_by_kind": plain(loop_turn.infra_wait_by_kind),
    }


def route_entry(info: Mapping[str, Any], settings: Mapping[str, Any]) -> dict[str, Any]:
    """Return the route section: the backend's description with each distinct settings set kept once, by digest.

    `info["settings_sent"]` (the settings of the latest request) is replaced by the digest of that set. Every turn
    names its own set by digest, so a setting that changes in the middle of a run stays visible.
    """
    rest = {k: v for k, v in plain(dict(info)).items() if k != "settings_sent"}
    latest = plain(info.get("settings_sent", {}))
    sets = {**plain(dict(settings))}
    sets.setdefault(digest(latest), latest)
    return {**rest, "settings_sent_sha256": digest(latest), "settings": sets}


def info_of_route(route: Mapping[str, Any]) -> dict[str, Any]:
    """Return what `backend.info()` returned for a route section: the inverse of `route_entry`."""
    rest = {k: v for k, v in route.items() if k not in ("settings", "settings_sent_sha256")}
    return {**rest, "settings_sent": route["settings"].get(route["settings_sent_sha256"], {})}


def expected_conversation(system: str, user: str, captured: Sequence[Captured]) -> list[dict[str, Any]]:
    """Return the conversation the pieces add up to: system (when given), user, then each turn and its results."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": user})
    for c in captured:
        messages += [c.native, *c.tool_messages]
    return messages


def build_record(
    result: LoopResult,
    recording: RecordingBackend,
    *,
    identity: RunIdentity,
    system: "str | RenderedPrompt | PromptText",
    user: "str | RenderedPrompt | PromptText",
    specs: Sequence[ToolSpec],
    code: Mapping[str, Any],
    core_version: str | None = None,
    instruments: Mapping[str, Any] | None = None,
    redactor: Redactor | None = None,
    secrets: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> RunRecord:
    """Build the record of a finished attempt from the loop's result and the recording wrapper it ran through.

    Raises RecordError when the pieces do not add up: a different number of replies than the loop counted, or a
    conversation that is not the one the pieces make, which means the backend's history is not the plain
    system, user, assistant, tool-results sequence the replay serves.

    Secrets are redacted here, before any digest is taken: the digests the record pins (prompt texts, the
    conversation each request carried, the settings each sent, the configuration) are those of the redacted
    data, which is what is stored and replayed, and `redacted_paths` lists where something was replaced.
    The secrets are the environment's (`environ`, the process's by default), the registered ones, and
    `secrets` for this record. A replay passes `Redactor.none()`: what it reads is redacted already.
    """
    sys_prompt, user_prompt = PromptText.of(system), PromptText.of(user)
    captured = recording.captured
    if len(captured) != len(result.turns):
        raise RecordError(f"the loop counted {len(result.turns)} replies but the recording holds {len(captured)}")
    if expected_conversation(sys_prompt.text, user_prompt.text, captured) != result.transcript:
        raise RecordError(
            "the backend's conversation is not the system and user prompts followed by the recorded turns"
        )
    sets: dict[str, Any] = {}
    turns = []
    for index, c in enumerate(captured):
        turns.append(turn_entry(index, c, digest(c.settings), result.turns[index]))
        sets[digest(c.settings)] = c.settings
    failed = None
    if recording.failure is not None:
        failed = dict(recording.failure)
        sets[failed["settings_sha256"]] = recording.failure_settings
    config = plain(result.config)
    fields = {
        "run_key": identity.run_key,
        "factors": plain(dict(identity.factors)),
        "attempt": identity.attempt,
        "route": route_entry(result.backend_info, sets),
        "prompt": {
            "system": sys_prompt.entry(),
            "user": user_prompt.entry(),
            "tools_sha256": result.tool_manifest_sha256,
        },
        "tools": [tool_entry(s) for s in specs],
        "agent_config": config,
        "agent_config_sha256": result.config_sha256,
        "code": plain(dict(code)),
        "core_version": core_version,
        "transcript": turns,
        "failed_request": failed,
        "tool_calls": [call_entry(c) for c in result.tool_calls],
        "clock": [[label, value] for label, value in result.clock_readings],
        "outcome": outcome_entry(result.outcome),
        "totals": {
            "rounds": result.rounds,
            "malformed_calls": result.malformed_calls,
            "malformed_events": plain(result.malformed_events),
            "tokens_used": result.tokens_used,
            "usage_incomplete": result.usage_incomplete,
            "agent_s": result.agent_s,
            "elapsed_s": result.elapsed_s,
            "infra_wait_s": result.infra_wait_s,
            "infra_wait_by_kind": plain(result.infra_wait_by_kind),
            "cleanup_s": result.cleanup_s,
            "cleanup_timed_out": result.cleanup_timed_out,
            "abandoned_threads": result.abandoned_threads,
            "final_text": result.final_text,
        },
        "instruments": plain(dict(instruments or {})),
        "scores": {},
    }
    redactor = redactor if redactor is not None else Redactor.from_environment(environ, secrets)
    redacted, paths = redactor.structure(fields)
    if paths:
        warnings.warn(
            f"record of attempt {identity.attempt}: redacted secrets at {len(paths)} place(s)",
            RuntimeWarning,
            stacklevel=2,
        )
    return RunRecord(**seal(redacted), redacted_paths=paths, schema_version=CURRENT)


async def record_attempt(
    backend: ModelBackend,
    providers: Sequence[ToolProvider],
    system: "str | RenderedPrompt | PromptText",
    user: "str | RenderedPrompt | PromptText",
    config: AgentConfig,
    identity: RunIdentity,
    *,
    code: Mapping[str, Any],
    core_version: str | None = None,
    timer: AgentTimer | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    secrets: Iterable[str] = (),
) -> RunRecord:
    """Run one attempt through the loop, recording it, and return its record.

    The tool box takes its cap from the configuration, so the record's configuration describes what the model saw.
    `code` is the code identity, for example `expcore.provenance.codestamp.version(root, scope)`, and `core_version`
    defaults to the installed version of the core. The caller builds the backend (passing it `timer.time_left` when
    it uses the loop's time) and closes it.

    Secrets are redacted from the record (R12): the environment's, the ones registered with
    `expcore.records.redact.register_secret` (a platform that mints a token registers it), and `secrets`.
    """
    recording = RecordingBackend(backend)
    box = ToolBox(list(providers), cap_chars=config.limits.output_cap_chars)
    specs = [spec for provider in providers for spec in provider.tools()]
    prompts = (PromptText.of(system), PromptText.of(user))
    result = await run_agent(
        recording, box, prompts[0].text, prompts[1].text, config, timer=timer, clock=clock, sleep=sleep
    )
    return build_record(
        result,
        recording,
        identity=identity,
        system=prompts[0],
        user=prompts[1],
        specs=specs,
        code=code,
        core_version=core_version if core_version is not None else sdk_version(CORE_DISTRIBUTION),
        secrets=secrets,
    )
