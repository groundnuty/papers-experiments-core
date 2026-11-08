# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/record.py:181-185, 307-339, 355-362
"""The record schema: its versions, the fields each version added, and the validation that refuses everything else.

A record names its schema version. A version is readable when it is in `SCHEMAS`; a field is known to a record when
the record's version, or an earlier one, introduced it. Anything else is refused at load, whether it is an unknown
version, an unknown field, a missing field or a field added by a later version than the record's own (R02).

Version 1 is frozen from its first pushed commit (see the table below). To change the record format, append a
`SchemaVersion` that lists, per section, the fields it adds; give the settings it
adds to the agent configuration the value that was in force before (`config_before`), so an older record replays
under the rule it ran under; and bump nothing else. Older records keep loading, are written back as the bytes they
were read from (R01), and are replayed with the defaults of their time.

Every section is a table of field name to the types the field may hold, in the order the file writes them. Numbers are
finite: a count the run did not obtain is `None`, never 0 and never NaN (R10).
"""

import dataclasses
import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from expcore.config.models import Limits
from expcore.errors import infra_kinds
from expcore.loop.config import AgentConfig
from expcore.loop.outcomes import HarnessDefect, Outcome
from expcore.loop.records import TOOL_STATUSES
from expcore.plugin import WAIT_KINDS

SCHEMA_PREFIX = "expcore-record/"
NONE = type(None)
NUM = (int, float)
_NAMESPACE = re.compile(r"[a-z][a-z0-9_]*")

# The fields of each section of a record, with the types each may hold. The order is the order of the file.
SECTIONS: dict[str, dict[str, tuple[type, ...]]] = {
    "record": {
        "run_key": (str,),
        "factors": (dict,),
        "attempt": (int,),
        "treatment_kinds": (list,),
        "route": (dict,),
        "prompt": (dict,),
        "tools": (list,),
        "agent_config": (dict,),
        "agent_config_sha256": (str,),
        "code": (dict,),
        "core_version": (str, NONE),
        "transcript": (list,),
        "failed_request": (dict, NONE),
        "tool_calls": (list,),
        "clock": (list,),
        "outcome": (dict,),
        "totals": (dict,),
        "instruments": (dict,),
        "scores": (dict,),
        "redacted_paths": (list,),
    },
    "prompt": {"system": (dict,), "user": (dict,), "tools_sha256": (str,)},
    "prompt_part": {"text": (str,), "sha256": (str,), "template_sha256": (str, NONE), "slots": (dict, NONE)},
    "tool": {"name": (str,), "description": (str,), "input_schema": (dict,), "terminal": (bool,)},
    "turn": {
        "index": (int,),
        "neutral": (dict,),
        "native": (dict,),
        "tool_messages": (list,),
        "request_sha256": (str,),
        "settings_sha256": (str,),
        "stop_reason": (str, NONE),
        "usage": (dict,),
        "latency_s": (float, int),
        "served_model": (str, NONE),
        "system_fingerprint": (str, NONE),
        "retries": (list,),
        "waits_left_open": (int,),
        "infra_wait_by_kind": (dict,),
    },
    "neutral": {"text": (str,), "reasoning": (str, NONE), "tool_calls": (list,)},
    "neutral_call": {
        "id": (str,),
        "name": (str,),
        "arguments": (dict,),
        "parse_error": (str, NONE),
        "raw_arguments": (str, NONE),
    },
    "usage": {
        "input_tokens": (int, NONE),
        "output_tokens": (int, NONE),
        "uncached_input_tokens": (int, NONE),
        "cache_creation_input_tokens": (int, NONE),
        "cache_read_input_tokens": (int, NONE),
    },
    "tool_call": {
        "turn": (int,),
        "round": (int,),
        "call_id": (str,),
        "name": (str,),
        "arguments": (dict,),
        "status": (str,),
        "text": (str,),
        "raw_text": (str,),
        "raw_chars": (int,),
        "truncated": (bool,),
        "error_class": (str, NONE),
        "raw_arguments": (str, NONE),
        "infra_wait_s": NUM,
        "started_s": (*NUM, NONE),
        "ended_s": (*NUM, NONE),
        "detail": (str,),
        "source_truncated": (bool, NONE),
        "waits_left_open": (int,),
        "terminal": (bool,),
        "infra_wait_by_kind": (dict,),
        "rendering_verified": (bool,),
    },
    "failed_request": {
        "kind": (str,),
        "infra_kind": (str, NONE),
        "detail": (str, NONE),
        "status": (int, NONE),
        "pattern": (str, NONE),
        "error_type": (str, NONE),
        "settings_sha256": (str,),
        "request_sha256": (str,),
    },
    "outcome": {
        "name": (str,),
        "detail": (str,),
        "infra_kind": (str, NONE),
        "error_type": (str, NONE),
        "defect": (dict, NONE),
        "treatment": (bool,),
    },
    "totals": {
        "rounds": (int,),
        "malformed_calls": (int,),
        "malformed_events": (list,),
        "tokens_used": (int,),
        "usage_incomplete": (bool,),
        "agent_s": NUM,
        "elapsed_s": NUM,
        "infra_wait_s": NUM,
        "infra_wait_by_kind": (dict,),
        "cleanup_s": NUM,
        "cleanup_timed_out": (bool,),
        "abandoned_threads": (int,),
        "final_text": (str, NONE),
    },
}
FAILURE_KINDS = ("infra", "context_overflow", "abandoned", "error", "cut")
# The fields of a retry event (`expcore.models.retry.Retrier`) and of a code identity (`codestamp.version`).
RETRY_FIELDS = ("retry", "kind", "error", "status", "retry_after_s", "wait_s", "message")
CODE_FIELDS = ("commit", "dirty", "source", "stale", "why", "tree_sha256")


class RecordError(ValueError):
    """A record is not valid: the message names the place and what is wrong."""


@dataclass(frozen=True)
class SchemaVersion:
    """One version of the record format.

    `added` maps a section name to the fields this version introduced into it. `config_before` maps a setting this
    version added to the agent configuration to the value that was in force before it existed.
    """

    name: str
    added: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    config_before: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Refuse a name outside the schema family and a table that lists a section or field the format lacks."""
        if not self.name.startswith(SCHEMA_PREFIX) or not self.name[len(SCHEMA_PREFIX) :].isdigit():
            raise ValueError(f"schema version {self.name!r} must look like {SCHEMA_PREFIX}N")
        for section, names in self.added.items():
            known = SECTIONS.get(section)
            unknown = [n for n in names if known is None or n not in known]
            if unknown:
                raise ValueError(f"{self.name} lists fields the {section!r} section does not have: {unknown}")


# The table of versions. Version 1 is frozen from its first pushed commit: nothing was published before it, so
# its shape is the one that ships, and a change to it breaks every record written since. After that push, a
# change to the format is a new version in this table that lists what it added, never an edit of version 1.
# `tests/data/golden-record-v1.json` is a record of version 1 that must keep loading and writing back byte for byte.
SCHEMAS: tuple[SchemaVersion, ...] = (SchemaVersion(f"{SCHEMA_PREFIX}1"),)
CURRENT = SCHEMAS[-1].name


def names(schemas: Sequence[SchemaVersion] = SCHEMAS) -> tuple[str, ...]:
    """Return the readable version names, oldest first."""
    return tuple(s.name for s in schemas)


def position(version: str, schemas: Sequence[SchemaVersion] = SCHEMAS) -> int:
    """Return the index of a readable version; raise RecordError naming the readable ones otherwise."""
    readable = names(schemas)
    if version not in readable:
        raise RecordError(f"unsupported record schema {version!r} (readable: {', '.join(readable)})")
    return readable.index(version)


def added_after(version: str, section: str, schemas: Sequence[SchemaVersion] = SCHEMAS) -> frozenset[str]:
    """Return the fields of a section that versions later than `version` introduced."""
    later = schemas[position(version, schemas) + 1 :]
    return frozenset(name for s in later for name in s.added.get(section, ()))


def fields_of(section: str, version: str, schemas: Sequence[SchemaVersion] = SCHEMAS) -> dict[str, tuple[type, ...]]:
    """Return the fields a record of `version` holds in a section, in file order."""
    later = added_after(version, section, schemas)
    return {name: types for name, types in SECTIONS[section].items() if name not in later}


def config_defaults(version: str, schemas: Sequence[SchemaVersion] = SCHEMAS) -> dict[str, Any]:
    """Return the agent-configuration settings a record of `version` predates, with the values then in force."""
    later = schemas[position(version, schemas) + 1 :]
    return {name: value for s in later for name, value in s.config_before.items()}


def _fits(value: Any, types: tuple[type, ...]) -> bool:
    """Tell whether a value has one of the types: no bool for a number, and floats are finite."""
    if isinstance(value, bool):
        return bool in types
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return isinstance(value, types)


def check_section(
    section: str, data: Any, version: str, where: str, schemas: Sequence[SchemaVersion] = SCHEMAS
) -> None:
    """Refuse a mapping whose keys are not exactly the section's fields for this version, or whose values mistype."""
    if not isinstance(data, dict):
        raise RecordError(f"{where}: expected an object, got {type(data).__name__}")
    expected = fields_of(section, version, schemas)
    later = added_after(version, section, schemas)
    unknown = sorted(k for k in data if k not in expected)
    if unknown:
        late = [k for k in unknown if k in later]
        note = f" ({', '.join(late)} added by a later schema than {version})" if late else ""
        raise RecordError(f"{where}: unknown field(s) {unknown}{note}")
    missing = [k for k in expected if k not in data]
    if missing:
        raise RecordError(f"{where}: missing field(s) {missing}")
    for name, types in expected.items():
        if not _fits(data[name], types):
            raise RecordError(
                f"{where}.{name}: expected {' or '.join(t.__name__ for t in types)}, got {type(data[name]).__name__}"
            )


def _check_list(section: str, items: Any, version: str, where: str, schemas: Sequence[SchemaVersion]) -> None:
    """Check every element of a list as a section."""
    for i, item in enumerate(items):
        check_section(section, item, version, f"{where}[{i}]", schemas)


def _check_keys(where: str, data: Any, allowed: Collection[str], required: Collection[str] | None = None) -> None:
    """Refuse an object with a key outside `allowed`, or without one of `required` (by default, all of `allowed`)."""
    if not isinstance(data, dict):
        raise RecordError(f"{where}: expected an object, got {type(data).__name__}")
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise RecordError(f"{where}: unknown field(s) {unknown}")
    missing = sorted(set(allowed if required is None else required) - set(data))
    if missing:
        raise RecordError(f"{where}: missing field(s) {missing}")


def _check_waited(waited: Any, where: str) -> None:
    """Check seconds waited per kind: only kinds of the closed list, each a finite number of seconds, not negative.

    A wait that was declared and lasted no time is 0.0 seconds, and valid.
    """
    bad = [k for k, v in waited.items() if k not in WAIT_KINDS or not _fits(v, NUM) or v < 0]
    if bad:
        raise RecordError(f"{where}: seconds waited are given per kind of {tuple(WAIT_KINDS)}, not {sorted(bad)}")


def _check_turn(turn: dict, version: str, where: str, schemas: Sequence[SchemaVersion]) -> None:
    """Check the parts of one turn entry."""
    _check_waited(turn["infra_wait_by_kind"], f"{where}.infra_wait_by_kind")
    check_section("neutral", turn["neutral"], version, f"{where}.neutral", schemas)
    _check_list("neutral_call", turn["neutral"]["tool_calls"], version, f"{where}.neutral.tool_calls", schemas)
    check_section("usage", turn["usage"], version, f"{where}.usage", schemas)
    if turn["native"].get("role") != "assistant":
        raise RecordError(f"{where}.native: the native message of a turn must have the role 'assistant'")
    for i, retry in enumerate(turn["retries"]):
        _check_keys(f"{where}.retries[{i}]", retry, RETRY_FIELDS, required=())


def _check_outcome(outcome: dict, treatment_kinds: Any, completed_turns: int, where: str) -> None:
    """Build the outcome through the loop's own vocabulary check, so a name or kind outside it is refused here too.

    The `treatment` flag must be what the outcome, the record's `treatment_kinds` and its transcript make it: true
    exactly when the outcome is infrastructure of a kind in the list that can be a treatment and the transcript holds
    at least one completed turn. A record whose flag says otherwise contradicts itself (R02).
    """
    defect = outcome["defect"]
    if defect is not None:
        _check_keys(f"{where}.defect", defect, ("exception", "stack_sha256"))
    try:
        held = None
        if defect is not None:
            held = HarnessDefect(defect["exception"], defect["stack_sha256"])
        built = Outcome(outcome["name"], outcome["detail"], outcome["infra_kind"], outcome["error_type"], held)
    except (KeyError, TypeError, ValueError) as exc:
        raise RecordError(f"{where}: {type(exc).__name__}: {exc}") from None
    expected = built.is_treatment(treatment_kinds, completed_turns)
    if outcome["treatment"] is not expected:
        raise RecordError(
            f"{where}.treatment: is {outcome['treatment']} but the outcome, treatment_kinds and the {completed_turns} "
            f"completed turn(s) make it {expected}"
        )


def _check_treatment_kinds(kinds: Any) -> None:
    """Check the infrastructure kinds the condition declared as treatment: distinct, sorted, from the registry."""
    if not isinstance(kinds, list) or any(not isinstance(k, str) for k in kinds):
        raise RecordError("treatment_kinds: a list of infrastructure kinds")
    unknown = [k for k in kinds if k not in infra_kinds()]
    if unknown:
        raise RecordError(
            f"treatment_kinds: unknown infrastructure kind {unknown[0]!r}; known: {', '.join(infra_kinds())}"
        )
    if kinds != sorted(set(kinds)):
        raise RecordError("treatment_kinds: the kinds are written sorted, each once")


def _check_configuration(config: Any, version: str, schemas: Sequence[SchemaVersion]) -> None:
    """Check the agent configuration against the loop's own fields, less the settings a later version added."""
    later = set(config_defaults(version, schemas))
    allowed = {f.name for f in dataclasses.fields(AgentConfig)} | {"terminal_tools"}
    _check_keys("agent_config", config, allowed - later)
    _check_keys("agent_config.limits", config["limits"], {f.name for f in dataclasses.fields(Limits)})
    tools = config["terminal_tools"]
    if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
        raise RecordError("agent_config.terminal_tools: a list of tool names")


def _check_route(route: Any) -> None:
    """Check the parts of the route that the record itself adds: the settings sets and the digest of the latest."""
    sets = route.get("settings")
    if not isinstance(sets, dict) or any(not isinstance(v, dict) for v in sets.values()):
        raise RecordError("route.settings: the settings of each request, an object per digest")
    if not isinstance(route.get("settings_sent_sha256"), str):
        raise RecordError("route.settings_sent_sha256: the digest of the settings of the latest request")


def validate(data: Any, schemas: Sequence[SchemaVersion] = SCHEMAS) -> str:
    """Check a decoded record against its own schema version, section by section; return the version.

    Raises RecordError naming the first place that is wrong. An unknown version, an unknown or missing field, a field
    a later schema added, a value of the wrong type, an outcome outside the vocabulary and a tool-call status outside
    the closed list are all refused.
    """
    if not isinstance(data, dict):
        raise RecordError("a record is a JSON object")
    version = data.get("schema_version")
    if not isinstance(version, str):
        raise RecordError(f"unsupported record schema {version!r} (readable: {', '.join(names(schemas))})")
    position(version, schemas)
    body = {k: v for k, v in data.items() if k != "schema_version"}
    check_section("record", body, version, "record", schemas)
    prompt = body["prompt"]
    check_section("prompt", prompt, version, "prompt", schemas)
    check_section("prompt_part", prompt["system"], version, "prompt.system", schemas)
    check_section("prompt_part", prompt["user"], version, "prompt.user", schemas)
    _check_list("tool", body["tools"], version, "tools", schemas)
    for i, turn in enumerate(body["transcript"]):
        check_section("turn", turn, version, f"transcript[{i}]", schemas)
        _check_turn(turn, version, f"transcript[{i}]", schemas)
    if body["failed_request"] is not None:
        check_section("failed_request", body["failed_request"], version, "failed_request", schemas)
        if body["failed_request"]["kind"] not in FAILURE_KINDS:
            raise RecordError(f"failed_request.kind: {body['failed_request']['kind']!r} is not one of {FAILURE_KINDS}")
    _check_list("tool_call", body["tool_calls"], version, "tool_calls", schemas)
    for i, entry in enumerate(body["tool_calls"]):
        if entry["status"] not in TOOL_STATUSES:
            raise RecordError(f"tool_calls[{i}].status: {entry['status']!r} is not one of {TOOL_STATUSES}")
        if entry["raw_chars"] < 0:
            raise RecordError(f"tool_calls[{i}].raw_chars: {entry['raw_chars']} is not a length")
        _check_waited(entry["infra_wait_by_kind"], f"tool_calls[{i}].infra_wait_by_kind")
    for i, reading in enumerate(body["clock"]):
        ok = isinstance(reading, list) and len(reading) == 2 and isinstance(reading[0], str) and _fits(reading[1], NUM)
        if not ok:
            raise RecordError(f"clock[{i}]: expected [label, seconds]")
    _check_treatment_kinds(body["treatment_kinds"])
    check_section("outcome", body["outcome"], version, "outcome", schemas)
    _check_outcome(body["outcome"], body["treatment_kinds"], len(body["transcript"]), "outcome")
    check_section("totals", body["totals"], version, "totals", schemas)
    _check_waited(body["totals"]["infra_wait_by_kind"], "totals.infra_wait_by_kind")
    for i, event in enumerate(body["totals"]["malformed_events"]):
        _check_keys(f"totals.malformed_events[{i}]", event, ("round", "call_id", "name", "error", "raw"))
    _check_configuration(body["agent_config"], version, schemas)
    _check_keys("code", body["code"], CODE_FIELDS)
    _check_route(body["route"])
    if any(not isinstance(p, str) for p in body.get("redacted_paths", ())):
        raise RecordError("redacted_paths: a list of field paths")
    for place in ("instruments", "scores"):
        held = body.get(place, {})  # absent from a record of a version that predates it
        bad = [k for k in held if not _NAMESPACE.fullmatch(k) or not isinstance(held[k], dict)]
        if bad:
            raise RecordError(f"{place}: each entry is an object under a lower-case namespace, not {bad}")
    return version
