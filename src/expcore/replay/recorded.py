# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/replay.py:39-190
"""The three pieces that stand in for the outside world when the real loop is replayed from a record (R03).

- `RecordedBackend` serves each model turn, and the recorded failure of the last request, in order. It owns the
  conversation as a backend does, built from the recorded native messages, and checks against the record as it goes:
  that the conversation the request carries has the digest the record pinned for it (R06), that the settings of the
  request have theirs, and that every tool result the loop hands back is the text the model saw.
- `RecordedToolProvider` serves each tool result by call order and exact arguments. Calls the tool box answers itself
  (unknown tool, bad arguments, malformed arguments, skipped calls) never reach it.
- The recorded clock (`clock.py`) serves the recorded readings. Both pieces above walk the waits on infrastructure
  the record shows at the start of their call, through the context they are given, so the readings line up.

A divergence found while the loop runs stops it through `Halt`, not as an outcome of the attempt.
"""

import asyncio
import copy
import dataclasses
import re
from collections.abc import Callable, Collection, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from expcore.errors import ContextOverflow, InfraError, RequestAbandoned
from expcore.loop.toolbox import ToolBox, ToolRun
from expcore.models.adapters import ModelTurn, ToolCall, ToolResult
from expcore.plugin import NO_WAITS, CallContext, ToolOutput, ToolProvider, ToolSpec
from expcore.records.build import info_of_route
from expcore.records.digest import canonical, digest
from expcore.records.record import RunRecord
from expcore.replay.clock import ReplayClock, replay_waits
from expcore.replay.compare import first_difference
from expcore.replay.divergence import stop
from expcore.replay.native import malformed_flags, neutral_of, normalised

# The error classes the tool box gives to calls it answers itself, which therefore never reach a provider.
TOOLBOX_ERROR_CLASSES = frozenset({"UnknownTool", "InvalidArguments"})


def exception_named(name: str, *, provider_error: bool = False) -> type[Exception]:
    """Return an exception class with the given name, for re-raising a failure that is recorded by class name only.

    With `provider_error` it carries a status code, as an error the endpoint answered with does, which is what makes the
    loop take it for the attempt's own error and not for a crash in our code.
    """
    attributes: dict[str, Any] = {"__module__": __name__}
    if provider_error:
        attributes["status_code"] = 400
    return type(name, (Exception,), attributes)


# The fields of the object that carries a tool result in a native conversation: the call it answers, and its text.
RESULT_ID_FIELDS = ("tool_call_id", "tool_use_id")
RESULT_CONTENT_FIELD = "content"


def holds_result(value: Any, call_id: str, text: str) -> bool:
    """Tell whether some object inside a JSON value is the result of this call: its call-id field is the call's id and
    its content field is the text.

    A tool result is one object per call in every native conversation (`tool_call_id` and `content` in the
    chat-completions shape, `tool_use_id` and `content` in a block of the Messages API). The fields are compared by
    name, so a result whose id and text have traded places, or two results swapped between their calls, do not pass.
    """
    if isinstance(value, dict):
        if value.get(RESULT_CONTENT_FIELD) == text and any(value.get(f) == call_id for f in RESULT_ID_FIELDS):
            return True
        return any(holds_result(v, call_id, text) for v in value.values())
    if isinstance(value, list):
        return any(holds_result(v, call_id, text) for v in value)
    return False


_PARTS = {"tool_calls": "tool call", "text": "reply text", "reasoning": "reasoning"}  # of the reply as the loop read it


def _part_named(where: str) -> str:
    """The part of a recorded reply a difference is in, in plain words: `transcript[k].neutral.tool_calls[0]...` is a
    tool call."""
    found = re.match(r"transcript\[\d+\]\.neutral\.(\w+)", where)
    return _PARTS.get(found.group(1), "reply") if found else "reply"


class RecordedBackend:
    """A model backend that serves the turns of a record."""

    def __init__(
        self,
        record: RunRecord,
        clock: ReplayClock,
        waits: Callable[[str], AbstractAsyncContextManager[None]],
    ):
        self._route = record.route
        self._turns = record.transcript
        self._failed = record.failed_request
        self._calls = record.tool_calls
        self._outcome = record.outcome
        self._clock, self._waits = clock, waits
        self._served = 0
        self._conversation: list[dict[str, Any]] = []
        self._latest: str | None = None

    @property
    def output_limit(self) -> int | None:
        """The record does not keep the output limit as such; it is in the settings of each request."""
        return None

    @property
    def context_tokens(self) -> int | None:
        """The record does not keep the context window."""
        return None

    @property
    def expected_steps(self) -> int:
        """The number of model requests the record holds: its turns, and the request that failed."""
        return len(self._turns) + (self._failed is not None)

    @property
    def served(self) -> int:
        """The number of requests served so far."""
        return self._served

    def info(self) -> dict[str, Any]:
        """Describe the route as recorded, with the settings of the latest request served."""
        route = dict(self._route)
        if self._latest is not None:
            route["settings_sent_sha256"] = self._latest
        return copy.deepcopy(info_of_route(route))

    def begin(self, system: str, user: str, tools: list[dict]) -> None:
        """Start the conversation as every backend does: the system prompt when there is one, then the user prompt."""
        self._conversation = [{"role": "system", "content": system}] if system else []
        self._conversation.append({"role": "user", "content": user})

    def _check(self, path: str, request_sha256: str | None, settings_sha256: str | None) -> None:
        """Check the digests the record pinned for a request against the conversation and settings now."""
        if request_sha256 is not None and digest(self._conversation) != request_sha256:
            stop(
                f"{path}.request_sha256",
                "the conversation this request carries is not the one the record pinned (a recorded reply, reasoning "
                "or tool result was changed, or the replayed history differs)",
            )
        if settings_sha256 is not None:
            sets = self._route["settings"]
            if settings_sha256 not in sets or digest(sets[settings_sha256]) != settings_sha256:
                stop(f"{path}.settings_sha256", "the settings stored under this digest do not have it")

    def _check_neutral(self, k: int, entry: dict[str, Any]) -> None:
        """Check that the neutral turn is what the native message of the same turn stands for, by the parser of the
        backend that wrote it. The two are served to different places, so a record where they differ is not a run."""
        derived = neutral_of(self._route.get("backend"), entry["native"], malformed_flags(entry["neutral"]))
        if derived is None:
            stop(
                f"transcript[{k}].native",
                f"no parser for the native messages of backend {self._route.get('backend')!r}",
            )
        where = first_difference(normalised(entry["neutral"]), derived, f"transcript[{k}].neutral")
        if where is not None:
            stop(
                where,
                f"the recorded {_part_named(where)} does not match the model's raw reply in that turn "
                f"(transcript[{k}].native): the record was changed after it was written, or the backend now reads "
                "that reply differently",
            )

    async def step(self) -> ModelTurn:
        """Serve the next recorded turn; the request after the last turn is the recorded failure, if there is one."""
        k = self._served
        if k < len(self._turns):
            entry = self._turns[k]
            self._check_neutral(k, entry)
            self._check(f"transcript[{k}]", entry.get("request_sha256"), entry.get("settings_sha256"))
            await replay_waits(self._clock, self._waits, entry.get("waits_left_open", 0))
            self._served += 1
            self._latest = entry.get("settings_sha256", self._latest)
            self._conversation.append(copy.deepcopy(entry["native"]))
            return self._model_turn(entry)
        if self._failed is not None and k == len(self._turns):
            self._check("failed_request", self._failed["request_sha256"], self._failed["settings_sha256"])
            self._served += 1  # counted before the waits: a request that is cut is cut while it waits or hangs
            self._latest = self._failed["settings_sha256"]
            await replay_waits(self._clock, self._waits, 0)
            await self._raise_failure(self._failed)
        stop("transcript", f"the replay asked for model step {k + 1}; the record holds {self.expected_steps}")

    @staticmethod
    def _model_turn(entry: dict[str, Any]) -> ModelTurn:
        """Rebuild the model's reply from its neutral message and usage."""
        neutral, usage = entry["neutral"], entry["usage"]
        return ModelTurn(
            text=neutral["text"],
            tool_calls=[ToolCall(**c) for c in neutral["tool_calls"]],
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            stop_reason=entry["stop_reason"],
            latency_s=entry["latency_s"],
            reasoning=neutral["reasoning"],
            uncached_input_tokens=usage["uncached_input_tokens"],
            cache_creation_input_tokens=usage["cache_creation_input_tokens"],
            cache_read_input_tokens=usage["cache_read_input_tokens"],
            served_model=entry["served_model"],
            system_fingerprint=entry["system_fingerprint"],
            retries=tuple(copy.deepcopy(entry["retries"])),
            # absent in the record stays absent (None): a record of version 1 or 2, or an older writer of version 3
            usage_extra=dict(usage["extra"]) if "extra" in usage else None,
            usage_extra_cut=usage.get("extra_cut"),
        )

    async def _raise_failure(self, failed: dict[str, Any]) -> None:
        """Fail the way the recorded request failed. A request the loop gave up on is not answered: it waits until the
        loop's watch, which the recorded readings make look at the right moment, cuts it."""
        kind = failed["kind"]
        if kind == "cut":
            await asyncio.Event().wait()
        if kind == "infra":  # the target the failure named is the outcome's (none before version 3)
            target = self._outcome.get("infra_target") if self._outcome["name"] == "infrastructure" else None
            raise InfraError(failed["infra_kind"], failed["detail"] or "", target=target)
        if kind == "context_overflow":
            raise ContextOverflow(failed["status"], failed["pattern"] or "")
        if kind == "abandoned":
            raise RequestAbandoned("read timeout")
        recorded_as_error = self._outcome["name"] == "error"
        raise exception_named(failed["error_type"] or "Exception", provider_error=recorded_as_error)()

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """Take the results the loop produced, check each against what the model saw, and extend the conversation."""
        k = self._served - 1
        entry = self._turns[k]
        logged = {c["call_id"]: (j, c) for j, c in enumerate(self._calls) if c["turn"] == k}
        for result in results:
            found = logged.get(result.call.id)
            if found is None:
                stop("tool_calls", f"turn {k} has no recorded call {result.call.id!r}")
            j, recorded = found
            if result.text != recorded["text"]:
                stop(f"tool_calls[{j}].text", "the re-rendered result differs from what the model saw")
            if result.is_error != (recorded["status"] != "ok"):
                stop(
                    f"tool_calls[{j}].status", "the result is an error where the record says it was not, or the reverse"
                )
            if not holds_result(entry["tool_messages"], result.call.id, result.text):
                stop(
                    f"transcript[{k}].tool_messages",
                    f"the recorded message of call {result.call.id!r} does not hold the result the loop gave it",
                )
        self._conversation += copy.deepcopy(entry["tool_messages"])

    def transcript(self) -> list[dict]:
        """Return a copy of the conversation built so far."""
        return copy.deepcopy(self._conversation)

    async def probe(self) -> None:
        """Nothing to probe: a replay is never admitted, so it reports no usage."""

    async def aclose(self) -> None:
        """Nothing to release."""


class ReplayToolBox(ToolBox):
    """A tool box that serves, for a call whose rendering was redacted, the recorded shown text instead of rendering it.

    The cap and the error text are computed from the characters of the result, and redaction changed them: a secret
    and its replacement are not the same length. For such a call (`rendering_verified` false in the record) the box
    still has the provider serve it, so the call's identity and arguments are checked, but it returns the recorded
    shown text, raw text, its length, error class and truncation flag as they are. Every other call is rendered, and
    the cap is recomputed, as the loop did.

    The one provider of a replay serves every recorded call, so it cannot say which tools were a tool server's; the
    record does, in the names of `agent_config.tool_server_tools`, and the box is given them (L17).
    """

    def __init__(
        self,
        providers: Sequence[ToolProvider],
        *,
        cap_chars: int,
        recorded: Sequence[dict[str, Any]],
        tool_servers: Collection[str] = (),
    ):
        super().__init__(providers, cap_chars=cap_chars, tool_servers=tool_servers)
        self._unrendered = {c["call_id"]: c for c in recorded if not c.get("rendering_verified", True)}

    async def run(self, call: ToolCall, *, timeout_s: float, ctx: CallContext = NO_WAITS) -> ToolRun:
        """Run the call; for one whose rendering is not verifiable, put the recorded rendering in place of ours."""
        run = await super().run(call, timeout_s=timeout_s, ctx=ctx)
        recorded = self._unrendered.get(call.id)
        if recorded is None or not reaches_provider(recorded) or recorded["status"] not in ("ok", "error"):
            return run
        return dataclasses.replace(
            run,
            result=ToolResult(call, recorded["text"], run.result.is_error),
            raw_text=recorded["raw_text"],
            raw_chars=recorded["raw_chars"],
            truncated=recorded["truncated"],
            error_class=recorded["error_class"],
        )


def reaches_provider(call: dict[str, Any]) -> bool:
    """Tell whether a logged call was run by a tool provider, rather than answered by the tool box itself."""
    status = call["status"]
    return status in ("ok", "cut", "failed") or (status == "error" and call["error_class"] not in TOOLBOX_ERROR_CLASSES)


class RecordedToolProvider:
    """Serves the recorded results of the calls that reached a provider, by call order and exact arguments."""

    def __init__(self, record: RunRecord, clock: ReplayClock):
        self._specs = [ToolSpec(**spec) for spec in record.tools]
        self._calls = [(j, c) for j, c in enumerate(record.tool_calls) if reaches_provider(c)]
        self._outcome = record.outcome
        self._abandoned = record.totals["abandoned_threads"]
        self._clock = clock
        self._served = 0

    @property
    def expected_calls(self) -> int:
        """The number of calls the record shows reaching a provider."""
        return len(self._calls)

    @property
    def served(self) -> int:
        """The number of calls served so far."""
        return self._served

    def abandoned_threads(self) -> int:
        """Return the worker threads the recorded run left behind, which the loop result counts again."""
        return self._abandoned

    def tools(self) -> list[ToolSpec]:
        """Return the recorded tool specs."""
        return list(self._specs)

    async def execute(self, name: str, arguments: dict[str, Any], *, timeout_s: float, ctx: CallContext) -> ToolOutput:
        """Serve the next recorded call; a call beyond the record, or with other arguments, is a divergence."""
        i = self._served
        if i >= len(self._calls):
            stop("tool_calls", f"the replay ran tool call {i + 1} ({name}); the record holds {len(self._calls)}")
        j, call = self._calls[i]
        if call["name"] != name:
            stop(f"tool_calls[{j}].name", f"the replay called {name!r}, the record {call['name']!r}")
        if canonical(arguments) != canonical(call["arguments"]):
            stop(f"tool_calls[{j}].arguments", "the arguments differ from the recorded ones")
        self._served += 1
        status = call["status"]
        await replay_waits(self._clock, ctx.infrastructure_wait, call["waits_left_open"])
        if status == "cut":
            await asyncio.Event().wait()  # the loop's watch cuts it, at the recorded reading
        if status == "failed":
            self._raise_recorded_failure(j)
        if status == "ok":
            return ToolOutput(call["raw_text"], source_truncated=call["source_truncated"])
        return ToolOutput(self._error_message(call), True, call["error_class"], call["source_truncated"])

    def _raise_recorded_failure(self, j: int) -> None:
        """Raise what the provider raised: an infrastructure failure of the recorded kind, or a crash by class name."""
        outcome = self._outcome
        if outcome["name"] == "infrastructure":
            raise InfraError(outcome["infra_kind"], outcome["detail"], target=outcome.get("infra_target"))
        if outcome["name"] == "harness_defect":
            raise exception_named(outcome["defect"]["exception"])()
        stop(f"tool_calls[{j}].status", "a call is recorded as failed but the outcome records no failure of a tool")

    @staticmethod
    def _error_message(call: dict[str, Any]) -> str:
        """Recover the message of an error result: the logged raw text is the error class, a colon and the message."""
        error_class, raw = call["error_class"], call["raw_text"]
        prefix = f"{error_class}: "
        if raw.startswith(prefix):
            return raw[len(prefix) :]
        return "" if raw == error_class else raw
