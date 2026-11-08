# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/replay.py:39-190
"""The three pieces that stand in for the outside world when the real loop is replayed from a record (R03).

- `RecordedBackend` serves each model turn, and the recorded failure of the last request, in order. It owns the
  conversation as a backend does, built from the recorded native messages, and checks against the record as it goes:
  that the conversation the request carries has the digest the record pinned for it (R06), that the settings of the
  request have theirs, and that every tool result the loop hands back is the text the model saw.
- `RecordedToolProvider` serves each tool result by call order and exact arguments. Calls the tool box answers itself
  (unknown tool, bad arguments, malformed arguments, skipped calls) never reach it.
- `ReplayClock` serves the recorded clock readings. Readings labelled `time_left` are not served: they were taken by the
  live backend's retry policy, and a recorded backend never retries.

A divergence found while the loop runs stops it through `Halt`, not as an outcome of the attempt.
"""

import asyncio
import copy
from collections.abc import Sequence
from typing import Any

from expcore.errors import ContextOverflow, InfraError, RequestAbandoned
from expcore.models.adapters import ModelTurn, ToolCall, ToolResult
from expcore.plugin import ToolOutput, ToolSpec
from expcore.records.build import info_of_route
from expcore.records.digest import canonical, digest
from expcore.records.record import RunRecord
from expcore.replay.divergence import stop

# The label under which `AgentTimer.time_left` logs its readings.
BACKEND_CLOCK_LABELS = frozenset({"time_left"})
# The error classes the tool box gives to calls it answers itself, which therefore never reach a provider.
TOOLBOX_ERROR_CLASSES = frozenset({"UnknownTool", "InvalidArguments"})


def exception_named(name: str) -> type[Exception]:
    """Return an exception class with the given name, for re-raising a failure that is recorded by class name only."""
    return type(name, (Exception,), {"__module__": __name__})


def strings_of(value: Any) -> set[str]:
    """Return every string inside a JSON value, keys excluded."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*(strings_of(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(strings_of(v) for v in value)) if value else set()
    return set()


class ReplayClock:
    """Serves the recorded clock readings one by one. Used as the loop's injected clock."""

    def __init__(self, readings: Sequence[Sequence[Any]]):
        self._values = [float(value) for label, value in readings if label not in BACKEND_CLOCK_LABELS]
        self._read = 0

    def __call__(self) -> float:
        """Return the next recorded reading; the loop reading more often than it did when recorded is a divergence."""
        if self._read >= len(self._values):
            stop("clock", f"the loop read the clock {self._read + 1} times; the record holds {len(self._values)}")
        value = self._values[self._read]
        self._read += 1
        return value

    @property
    def unused(self) -> int:
        """How many recorded readings the replay did not read."""
        return len(self._values) - self._read


class RecordedBackend:
    """A model backend that serves the turns of a record."""

    def __init__(self, record: RunRecord, cut: asyncio.Event):
        self._route = record.route
        self._turns = record.transcript
        self._failed = record.failed_request
        self._calls = record.tool_calls
        self._cut = cut
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

    async def step(self) -> ModelTurn:
        """Serve the next recorded turn; the request after the last turn is the recorded failure, if there is one."""
        k = self._served
        if k < len(self._turns):
            entry = self._turns[k]
            self._check(f"transcript[{k}]", entry.get("request_sha256"), entry.get("settings_sha256"))
            self._served += 1
            self._latest = entry.get("settings_sha256", self._latest)
            self._conversation.append(copy.deepcopy(entry["native"]))
            return self._model_turn(entry)
        if self._failed is not None and k == len(self._turns):
            self._check("failed_request", self._failed["request_sha256"], self._failed["settings_sha256"])
            self._served += 1
            self._latest = self._failed["settings_sha256"]
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
        )

    async def _raise_failure(self, failed: dict[str, Any]) -> None:
        """Fail the way the recorded request failed. A request the loop gave up on is not answered, and the replay's
        sleep is released so the loop's bound passes at once."""
        kind = failed["kind"]
        if kind == "cut":
            self._cut.set()
            await asyncio.Event().wait()
        if kind == "infra":
            raise InfraError(failed["infra_kind"], failed["detail"] or "")
        if kind == "context_overflow":
            raise ContextOverflow(failed["status"], failed["pattern"] or "")
        if kind == "abandoned":
            raise RequestAbandoned("read timeout")
        raise exception_named(failed["error_type"] or "Exception")()

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """Take the results the loop produced, check each against what the model saw, and extend the conversation."""
        k = self._served - 1
        entry = self._turns[k]
        logged = {c["call_id"]: (j, c) for j, c in enumerate(self._calls) if c["turn"] == k}
        carried = strings_of(entry["tool_messages"])
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
            if result.call.id not in carried or result.text not in carried:
                stop(f"transcript[{k}].tool_messages", "the recorded result messages do not hold what the loop gave")
        self._conversation += copy.deepcopy(entry["tool_messages"])

    def transcript(self) -> list[dict]:
        """Return a copy of the conversation built so far."""
        return copy.deepcopy(self._conversation)

    async def probe(self) -> None:
        """Nothing to probe."""

    async def aclose(self) -> None:
        """Nothing to release."""


def reaches_provider(call: dict[str, Any]) -> bool:
    """Tell whether a logged call was run by a tool provider, rather than answered by the tool box itself."""
    status = call["status"]
    return status in ("ok", "cut", "failed") or (status == "error" and call["error_class"] not in TOOLBOX_ERROR_CLASSES)


class RecordedToolProvider:
    """Serves the recorded results of the calls that reached a provider, by call order and exact arguments."""

    def __init__(self, record: RunRecord, cut: asyncio.Event):
        self._specs = [ToolSpec(**spec) for spec in record.tools]
        self._calls = [(j, c) for j, c in enumerate(record.tool_calls) if reaches_provider(c)]
        self._outcome = record.outcome
        self._cut = cut
        self._served = 0

    @property
    def expected_calls(self) -> int:
        """The number of calls the record shows reaching a provider."""
        return len(self._calls)

    @property
    def served(self) -> int:
        """The number of calls served so far."""
        return self._served

    def tools(self) -> list[ToolSpec]:
        """Return the recorded tool specs."""
        return list(self._specs)

    async def execute(self, name: str, arguments: dict[str, Any], *, timeout_s: float) -> ToolOutput:
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
        if status == "cut":
            self._cut.set()
            await asyncio.Event().wait()
        if status == "failed":
            self._raise_recorded_failure(j)
        if status == "ok":
            return ToolOutput(call["raw_text"], infra_wait_s=call["infra_wait_s"])
        return ToolOutput(self._error_message(call), True, call["error_class"], call["infra_wait_s"])

    def _raise_recorded_failure(self, j: int) -> None:
        """Raise what the provider raised: an infrastructure failure of the recorded kind, or a crash by class name."""
        outcome = self._outcome
        if outcome["name"] == "infrastructure":
            raise InfraError(outcome["infra_kind"], outcome["detail"])
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
