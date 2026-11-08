# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/replay.py:262-330, 392-454
"""Compare a replayed record with the stored one, section by section, and name the first place that differs.

Everything the loop derives is compared: the prompts and tools, the configuration, the route, the transcript, the
failed request, the tool-call log, the clock readings, the outcome and the totals. Identity, the code stamp, the
instrument outputs and the scores are carried over from the record, not derived, and are not compared.

A record of an older schema version is compared without the fields that later versions added, and with the agent
configuration the defaults then in force. Two things are excused for every record: the clock readings labelled
`time_left` (a recorded backend does not retry), and the stack hash of a harness defect, which is computed from a live
traceback and cannot be reproduced from a recording; the exception class is still compared.
"""

from collections.abc import Sequence
from typing import Any

from expcore.records.digest import plain
from expcore.records.record import RunRecord
from expcore.records.schema import SCHEMAS, SchemaVersion, added_after, config_defaults
from expcore.replay.clock import BACKEND_CLOCK_LABELS
from expcore.replay.divergence import ReplayDivergence

# The sections compared, in the order the first difference is looked for.
COMPARED = (
    "prompt",
    "tools",
    "agent_config",
    "route",
    "transcript",
    "failed_request",
    "tool_calls",
    "clock",
    "outcome",
    "totals",
)
# Sections inside other sections that a later schema version may have added fields to.
_INNER = {"turn": ("neutral", "usage"), "prompt": ("system", "user")}


def first_difference(a: Any, b: Any, path: str = "") -> str | None:
    """Return the path of the first place two JSON values differ, or None when they are equal."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in [*a, *(k for k in b if k not in a)]:
            if key not in a or key not in b:
                return f"{path}.{key}"
            found = first_difference(a[key], b[key], f"{path}.{key}")
            if found is not None:
                return found
        return None
    if isinstance(a, list) and isinstance(b, list):
        for i, (x, y) in enumerate(zip(a, b, strict=False)):  # the lengths are compared below
            found = first_difference(x, y, f"{path}[{i}]")
            if found is not None:
                return found
        return None if len(a) == len(b) else f"{path} (length {len(a)} vs {len(b)})"
    return None if a == b and type(a) is type(b) else path


def _without(value: dict[str, Any], names: frozenset[str]) -> dict[str, Any]:
    """Return a copy of a mapping without some keys."""
    return {k: v for k, v in value.items() if k not in names}


def _strip_turn(turn: dict[str, Any], version: str, schemas: Sequence[SchemaVersion]) -> dict[str, Any]:
    """Drop from a turn the fields later versions added, in the turn and in its neutral message and usage."""
    out = _without(turn, added_after(version, "turn", schemas))
    for inner in _INNER["turn"]:
        if inner in out:
            out[inner] = _without(out[inner], added_after(version, inner, schemas))
    return out


def _strip(name: str, value: Any, version: str, schemas: Sequence[SchemaVersion]) -> Any:
    """Drop from one section of a replayed record the fields that the stored record's version predates."""
    if name == "transcript":
        return [_strip_turn(t, version, schemas) for t in value]
    if name == "tool_calls":
        return [_without(c, added_after(version, "tool_call", schemas)) for c in value]
    if name == "tools":
        return [_without(t, added_after(version, "tool", schemas)) for t in value]
    if name == "prompt":
        out = _without(value, added_after(version, "prompt", schemas))
        for part in _INNER["prompt"]:
            out[part] = _without(out[part], added_after(version, "prompt_part", schemas))
        return out
    if name in ("outcome", "totals") or (name == "failed_request" and value is not None):
        return _without(value, added_after(version, name, schemas))
    return value


def _blank_defect_stack(outcome: dict[str, Any]) -> dict[str, Any]:
    """Return the outcome with the stack hash of a harness defect blanked: it cannot be reproduced offline."""
    if outcome.get("defect") is None:
        return outcome
    return {**outcome, "defect": {**outcome["defect"], "stack_sha256": None}}


def section(record: RunRecord, name: str, *, replayed: bool, schemas: Sequence[SchemaVersion]) -> Any:
    """Return one section of a record as plain values, ready to compare with the same section of its counterpart."""
    value = plain(getattr(record, name))
    if name == "clock" and not replayed:
        return [r for r in value if r[0] not in BACKEND_CLOCK_LABELS]
    if name == "agent_config" and not replayed:
        return {**config_defaults(record.schema_version, schemas), **value}
    if name == "outcome":
        return _blank_defect_stack(value)
    return value


def compare(recorded: RunRecord, replayed: RunRecord, schemas: Sequence[SchemaVersion] = SCHEMAS) -> None:
    """Raise ReplayDivergence at the first section of the replayed record that differs from the stored one."""
    for name in COMPARED:
        stored = section(recorded, name, replayed=False, schemas=schemas)
        again = _strip(name, section(replayed, name, replayed=True, schemas=schemas), recorded.schema_version, schemas)
        where = first_difference(stored, again, name)
        if where is not None:
            raise ReplayDivergence(where, "the replayed record differs from the stored one")
