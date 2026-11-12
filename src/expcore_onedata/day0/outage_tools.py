# Provenance: new
"""The outage check's knowledge plane: the agent's own tools, called through MCP as an agent calls them (O28, #108).

`outage_agent` starts the condition's tool server the way the sandbox does (`SandboxPlatform.tool_server`: the leased
slot's tool-server user and the token minted for it, examined and checked against the condition's rights), and this
module calls its tools through the MCP client (`McpToolProvider.execute`): `get_file_distribution`,
`list_space_providers`, `schedule_file_replication` to the agent's local provider, and `get_transfer` until the
transfer ends or the bound. Nothing here asks Onezone or a provider directly.

**Arguments** come from each tool's own input schema: a property is filled by what its name says it is (a transfer, a
file, a space, a provider), and a tool whose required arguments cannot all be filled is not called and is unmeasured,
with the reason. **Each call** records its outcome (`served`, `refused` for an answer the tool marked an error, `no
answer` for a server that is gone, `blocked` for no answer within the bound), its seconds on the check's clock against
the bound, whether it timed out, its attempts (a read-only tool is called up to `READ_ATTEMPTS` times in all while it
answers an error; a schedule only once), the error's wording and the answer's text, both scrubbed of every name and
id the check knows (O12) and cut to `TEXT_LIMIT`. A distribution is read as the provider's REST distribution when the
answer is one, and a transfer's state as its REST record; an answer of another shape is kept as text, unparsed.
"""

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from expcore.errors import InfraError
from expcore_onedata.consistency import ENDED_STATES, transfer_state
from expcore_onedata.day0.outage_cells import ROLES, Roles
from expcore_onedata.placement import held_bytes, provider_blocks

KNOWLEDGE_TOOLS = ("get_file_distribution", "list_space_providers", "schedule_file_replication", "get_transfer")
NEEDS = {
    "get_file_distribution": ("file",),
    "list_space_providers": ("space",),
    "schedule_file_replication": ("file", "provider"),
    "get_transfer": ("transfer",),
}
READ_ONLY = ("get_file_distribution", "list_space_providers", "get_transfer")
READ_ATTEMPTS = 3
TEXT_LIMIT = 400


def kind_of(name: str) -> str | None:
    """What a tool's argument is, from its name: a transfer, a file, a space or a provider; None when it says none."""
    word = name.lower()
    for kind in ("transfer", "file", "space", "provider"):
        if kind in word:
            return kind
    return None


def arguments(name: str, schema: Mapping[str, Any] | None, values: Mapping[str, str]) -> dict[str, str] | str:
    """The arguments of a call to `name` from its input schema and the values of each kind; the reason when its
    required arguments cannot all be filled, or a kind it needs has no argument."""
    properties = (schema or {}).get("properties") or {}
    required = list((schema or {}).get("required") or [])
    filled = {prop: values[kind] for prop in properties if (kind := kind_of(prop)) in NEEDS[name] and kind in values}
    unfilled = [prop for prop in required if prop not in filled]
    if unfilled:
        return f"its input schema requires {len(unfilled)} argument(s) the check cannot fill"
    lacking = [kind for kind in NEEDS[name] if kind not in {kind_of(prop) for prop in filled}]
    if lacking:
        return f"its input schema has no argument for the {' and the '.join(lacking)}"
    return filled


class Scrubber:
    """Replaces every name and id the check knows in a text with what the report calls it (O12), longest first."""

    def __init__(self, pairs: Mapping[str, str]) -> None:
        self._pairs = sorted(((k, v) for k, v in pairs.items() if k), key=lambda pair: -len(pair[0]))

    def add(self, needle: str, label: str) -> None:
        if needle:
            self._pairs = sorted([*self._pairs, (needle, label)], key=lambda pair: -len(pair[0]))

    def __call__(self, text: str) -> str:
        for needle, label in self._pairs:
            text = text.replace(needle, label)
        return text[:TEXT_LIMIT]


def _json(text: str) -> Any:
    """A tool's answer as JSON, unwrapped from the `{"result": ...}` an MCP server's structured content puts around a
    plain return value (itself JSON text, when the tool returned that); None when it is no JSON."""
    try:
        answer = json.loads(text)
    except ValueError:
        return None
    while isinstance(answer, Mapping) and set(answer) == {"result"}:
        inner = answer["result"]
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except ValueError:
                return inner
        answer = inner
    return answer


def transfer_id(answer: Any) -> str | None:
    """A transfer's id in a scheduling tool's answer: the value of the first key that names a transfer's id."""
    if isinstance(answer, Mapping):
        for key, value in answer.items():
            if isinstance(value, str) and value and "transfer" in key.lower() and key.lower().endswith("id"):
                return value
        for value in answer.values():
            found = transfer_id(value)
            if found:
                return found
    return None


class Tools:
    """The knowledge tools of one tool server, called within `bound_s` and recorded (see the module docstring)."""

    def __init__(
        self,
        provider: Any,
        roles: Roles,
        space_id: str,
        scrub: Scrubber,
        *,
        bound_s: float,
        poll_s: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._provider, self._roles, self._space_id, self._scrub = provider, roles, space_id, scrub
        self.bound_s, self._poll_s, self._clock, self._sleep = bound_s, poll_s, clock, sleep
        self._specs = {spec.name: spec for spec in provider.tools()}
        self.transfers: dict[str, str] = {}  # a file's key -> the id of the transfer its schedule answered

    def missing(self) -> list[str]:
        """The knowledge tools this server does not have."""
        return [name for name in KNOWLEDGE_TOOLS if name not in self._specs]

    async def _once(self, name: str, args: Mapping[str, str]) -> tuple[dict[str, Any], str | None]:
        """One call: its facts, and the answer's raw text when it served (for parsing before it is scrubbed)."""
        began = self._clock()
        try:
            out = await asyncio.wait_for(
                self._provider.execute(name, dict(args), timeout_s=self.bound_s), timeout=self.bound_s
            )
        except TimeoutError:
            seconds = round(self._clock() - began, 3)
            return {"outcome": "blocked", "timed_out": True, "seconds": seconds}, None
        except InfraError as error:  # the server closed or failed: no answer from the tool at all
            seconds = round(self._clock() - began, 3)
            return {
                "outcome": "no answer",
                "timed_out": False,
                "seconds": seconds,
                "error": self._scrub(error.detail),
            }, None
        seconds = round(self._clock() - began, 3)
        if out.is_error:
            facts = {"outcome": "refused", "timed_out": False, "seconds": seconds, "error": self._scrub(out.text)}
            return {**facts, "error_class": out.error_class}, None
        return {"outcome": "served", "timed_out": False, "seconds": seconds, "text": self._scrub(out.text)}, out.text

    async def call(self, name: str, values: Mapping[str, str]) -> tuple[dict[str, Any], str | None]:
        """Call a tool, a read-only one again while it answers an error; its facts and the served answer's text."""
        base: dict[str, Any] = {"tool": name, "bound_s": self.bound_s}
        spec = self._specs.get(name)
        if spec is None:
            return {**base, "outcome": None, "reason": "the condition's tool server has no such tool"}, None
        args = arguments(name, spec.input_schema, values)
        if isinstance(args, str):
            return {**base, "outcome": None, "reason": args}, None
        errors: list[str] = []
        attempts = 0
        while True:
            attempts += 1
            facts, raw = await self._once(name, args)
            if facts["outcome"] in ("refused", "no answer"):
                errors.append(facts["error"])
            retry = name in READ_ONLY and facts["outcome"] == "refused" and attempts < READ_ATTEMPTS
            if not retry:
                return {**base, **facts, "attempts": attempts, "errors": errors}, raw
            await asyncio.to_thread(self._sleep, self._poll_s)

    # -- the four tools -----------------------------------------------------------------------------------------
    async def distribution(self, key: str, file_id: str) -> dict[str, Any]:
        """`get_file_distribution` of a file: who holds how much, by role, when the answer is a REST distribution."""
        facts, raw = await self.call("get_file_distribution", {"file": file_id})
        if raw is not None:
            facts["providers"] = self._held(_json(raw))
        return {**facts, "file": key}

    def _held(self, answer: Any) -> dict[str, Any] | None:
        if not isinstance(answer, Mapping):
            return None
        rows = {}
        for role in ROLES:
            try:
                held = held_bytes(provider_blocks(answer, self._roles.ids[role]))
                rows[self._roles.labels[role]] = {"listed": True, "held_bytes": held}
            except ValueError:  # not in the answer, or unreadable: unknown, never zero
                rows[self._roles.labels[role]] = {"listed": False, "held_bytes": None}
        return rows

    async def providers(self) -> dict[str, Any]:
        """`list_space_providers` of the space: which of the three providers its answer names."""
        facts, raw = await self.call("list_space_providers", {"space": self._space_id})
        if raw is not None:
            facts["listed"] = {self._roles.labels[role]: self._roles.ids[role] in raw for role in ROLES}
        return facts

    async def replicate(self, key: str, file_id: str) -> dict[str, Any]:
        """`schedule_file_replication` of a file to the local provider, then `get_transfer` until it ends or the
        bound."""
        scheduled, raw = await self.call(
            "schedule_file_replication", {"file": file_id, "provider": self._roles.ids["local"]}
        )
        found = transfer_id(_json(raw)) if raw is not None else None
        if raw is not None and found is None:
            scheduled["transfer_id_read"] = False
        if found is None:
            return {"file": key, "schedule_file_replication": scheduled, "get_transfer": None}
        self.transfers[key] = found
        self._scrub.add(found, "transfer")
        return {"file": key, "schedule_file_replication": scheduled, "get_transfer": await self.follow(found)}

    async def follow(self, transfer: str) -> dict[str, Any]:
        """Call `get_transfer` every poll until the transfer has ended or the bound, keeping the states it went
        through. A call that fails is classified by its own timing (its `seconds` and outcome), never by the seconds
        since the schedule; a blocked call ends the following."""
        began, trail, calls = self._clock(), [], []
        last: dict[str, Any] = {}
        while True:
            last, raw = await self.call("get_transfer", {"transfer": transfer})
            calls.append(last)
            record = _json(raw) if raw is not None else None
            state = transfer_state(record) if isinstance(record, Mapping) else None
            shown = state or last["outcome"]
            if not trail or trail[-1][1] != shown:
                trail.append([round(self._clock() - began, 3), shown])
            if state in ENDED_STATES or last["outcome"] in ("blocked", "no answer", None):
                break
            if self._clock() - began >= self.bound_s:
                break
            await asyncio.to_thread(self._sleep, self._poll_s)
        ended = bool(trail) and trail[-1][1] in ENDED_STATES
        facts = {"seconds": round(self._clock() - began, 3), "bound_s": self.bound_s, "states": trail, "ended": ended}
        facts["calls"] = len(calls)
        if ended or last["outcome"] == "served":
            return {**facts, "outcome": "served" if ended else "blocked", "last_call": last}
        if last["outcome"] is None:  # not called: unmeasured, named by its tool and why
            facts.update(tool="get_transfer", reason=last.get("reason"))
        return {**facts, "outcome": last["outcome"], "last_call": last}

    async def transfer_now(self, key: str) -> dict[str, Any] | None:
        """One `get_transfer` of the transfer scheduled for a file, after the restore; None when none was."""
        transfer = self.transfers.get(key)
        if transfer is None:
            return None
        facts, raw = await self.call("get_transfer", {"transfer": transfer})
        record = _json(raw) if raw is not None else None
        if isinstance(record, Mapping):
            facts["state"] = transfer_state(record)
        return facts


def baseline_gaps(rows: Mapping[str, Any]) -> list[str]:
    """The tools that answered with everything up and did not serve, by name: a tool that fails then says nothing
    about an outage. A cell that was not measured (outcome None, or no `get_transfer` to follow) is a gap, not this."""
    wrong = []
    for name, row in rows.items():
        cells: Sequence[Any] = (
            [row] if "outcome" in row else [row.get("schedule_file_replication"), row.get("get_transfer")]
        )
        answered = [cell for cell in cells if isinstance(cell, Mapping) and cell.get("outcome") is not None]
        if any(cell.get("outcome") != "served" for cell in answered):
            wrong.append(name)
    return wrong


__all__ = ["KNOWLEDGE_TOOLS", "Scrubber", "Tools", "arguments", "baseline_gaps", "kind_of", "transfer_id"]
