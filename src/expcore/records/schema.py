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

A version may also change a rule that the format does not show. Version 2 is that: no field was added, but an
infrastructure outcome is flagged as treatment only after the model completed a turn (`first_turn_rule`). A rule
attaches to the version that wrote the record: a record is validated, and replayed, under the rule of its own version,
and a later rule never applies to it retroactively, so a version-1 record that flags treatment with no completed turn
stays valid.

A version may mark fields it adds as optional (`SchemaVersion.optional`): a record of that version, or a later one, is
valid with or without them, an absent field means "not recorded", and a present one is checked like any other. Writing
a record never fills an absent optional field in as a null or a zero. Version 3 adds `extra` and `extra_cut` to the
`usage` of each turn, both optional: the numeric usage fields a provider reports beyond the counts, and how many of them
the cap left out. Every new record is written as version 3 and keeps version 2's rule; a record of version 1 or 2
lacks both fields, and is never given them. Version 3 is also the first under the reply-count rule
(`REPLY_COUNT_RULE_FROM`): its totals are incomplete when the backend counted a billed reply that became no turn.

Version 3 also carries the target of a fault, in two more optional fields: the outcome names the target an
infrastructure failure was attributed to (`infra_target`, a position label such as `providers[1]`), and the record
carries the targets its condition declares per kind (`treatment_targets`). Both are absent when there is none: a
failure that names no target, a condition that declares none. Where a declaration names a target, the treatment rule
holds a version-3 record to its target clause (`target_rule`): the failure must name that target and the run's fault
injector must have held it down in this attempt (`expcore.records.injections`). A record that declares no target is
decided as before, so the clause changes nothing for a version-3 record written before it.
Version 3 also says, in two optional fields of the totals, whether the attempt was held to the reservation R (C27):
`reserved_capped`, the bound the budget gate reserved the run by, the value the campaign store keeps for the attempt
(True: by the reply maximum, False: by the window), and `reply_cap_unheld`, whether the campaign had taken the
attempt's model off the reply maximum when the loop ended. A run that no gate decided has neither.
Version 3 also carries the per-call tool bound a study may declare (L17), in two optional places: the agent
configuration's `tool_call_bound_s`, left out when the study declares none (`TOOL_CALL_BOUND_FROM`), and `elapsed_s` of
each tool call that started, the wall seconds from the reading before the call to the one that ended it, left out of a
call that never started. A replay writes `elapsed_s` only when the stored record holds it.
Until v0.1.0 is tagged every extension of version 3 is strictly additive: optional fields only, no existing field
changes meaning or validation, the fields version 3 had at its first merge point keep the shape frozen in
`tests/data/record-v3-fields.json`, and the fixtures in `tests/data/record-v3/`, one per merge point, keep validating
and replaying byte for byte (docs/interfaces.md).

Every section is a table of field name to the types the field may hold, in the order the file writes them. An object
that a record may hold none of, such as an outcome's `defect` or a malformed event of the totals, is a section too, so
its keys are versioned like the others. Numbers are finite: a count the run did not obtain is `None`, never 0 and never
NaN (R10).
"""

import dataclasses
import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from expcore.config.models import Limits
from expcore.errors import TARGET_LABEL, infra_kinds, is_treatable, targeted_kinds
from expcore.loop.config import AgentConfig
from expcore.loop.outcomes import HarnessDefect, Outcome
from expcore.loop.records import TOOL_STATUSES
from expcore.models.usage import USAGE_EXTRA_CAP, USAGE_EXTRA_NAME_MAX, usage_name_ok, usage_number_ok
from expcore.plugin import WAIT_KINDS
from expcore.records.injections import injected_targets

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
        "treatment_targets": (dict,),
        "instrument_refusal": (dict,),
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
        "extra": (dict,),
        "extra_cut": (int,),
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
        "elapsed_s": NUM,
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
        "infra_target": (str,),
    },
    "defect": {"exception": (str,), "stack_sha256": (str,)},
    "malformed_event": {"round": (int,), "call_id": (str,), "name": (str,), "error": (str,), "raw": (str,)},
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
        "reserved_capped": (bool,),
        "reply_cap_unheld": (bool,),
    },
}
FAILURE_KINDS = ("infra", "context_overflow", "abandoned", "error", "cut")
# The fields of a retry event (`expcore.models.retry.Retrier`) and of a code identity (`codestamp.version`).
RETRY_FIELDS = ("retry", "kind", "error", "status", "retry_after_s", "wait_s", "message")
CODE_FIELDS = ("commit", "dirty", "source", "stale", "why", "tree_sha256")
# The fields of the refusal an instrument's post-run check records (C31); `_check_refusal` checks the object.
REFUSAL_FIELDS = ("namespace", "infra_kind", "detail")


class RecordError(ValueError):
    """A record is not valid: the message names the place and what is wrong."""


@dataclass(frozen=True)
class SchemaVersion:
    """One version of the record format.

    `added` maps a section name to the fields this version introduced into it. `config_before` maps a setting this
    version added to the agent configuration to the value that was in force before it existed. `first_turn_rule` says
    whether a record of this version flags an infrastructure outcome as treatment only after a completed model turn;
    the first version predates the rule. `target_rule` says whether a declared target must be the failure's and must
    have been injected in the attempt; versions before 3 predate it and declare no target. `optional` maps a section
    name to the fields among those in `added` that a record may lack: an absent one means "not recorded", and one that
    is present is validated like any other.
    """

    name: str
    added: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    config_before: Mapping[str, Any] = field(default_factory=dict)
    first_turn_rule: bool = False
    target_rule: bool = False
    optional: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Refuse a name outside the schema family, a table that lists a section or field the format lacks, and an
        optional field this version does not add."""
        if not self.name.startswith(SCHEMA_PREFIX) or not self.name[len(SCHEMA_PREFIX) :].isdigit():
            raise ValueError(f"schema version {self.name!r} must look like {SCHEMA_PREFIX}N")
        for section, names in self.added.items():
            known = SECTIONS.get(section)
            unknown = [n for n in names if known is None or n not in known]
            if unknown:
                raise ValueError(f"{self.name} lists fields the {section!r} section does not have: {unknown}")
        for section, names in self.optional.items():
            unknown = [n for n in names if n not in self.added.get(section, ())]
            if unknown:
                raise ValueError(f"{self.name} marks optional fields the {section!r} section does not add: {unknown}")


# The table of versions. Version 1 is frozen from its first pushed commit: nothing was published before it, so
# its shape is the one that ships, and a change to it breaks every record written since. After that push, a
# change to the format is a new version in this table that lists what it added, never an edit of version 1.
# `tests/data/golden-record-v1.json` is a record of version 1 that must keep loading and writing back byte for byte;
# `golden-record-v2.json` is the same for version 2, which adds no field but the first-turn rule for treatment.
# Version 3 is extended until v0.1.0 is tagged, each extension strictly additive (optional fields only), so it has a
# fixture per merge point in `tests/data/record-v3/`, which must keep validating and replaying byte for byte.
SCHEMAS: tuple[SchemaVersion, ...] = (
    SchemaVersion(f"{SCHEMA_PREFIX}1"),
    SchemaVersion(f"{SCHEMA_PREFIX}2", first_turn_rule=True),
    SchemaVersion(
        f"{SCHEMA_PREFIX}3",
        added={
            "usage": ("extra", "extra_cut"),
            "record": ("treatment_targets", "instrument_refusal"),
            "outcome": ("infra_target",),
            "totals": ("reserved_capped", "reply_cap_unheld"),
            "tool_call": ("elapsed_s",),
        },
        optional={
            "usage": ("extra", "extra_cut"),
            "record": ("treatment_targets", "instrument_refusal"),
            "outcome": ("infra_target",),
            "totals": ("reserved_capped", "reply_cap_unheld"),
            "tool_call": ("elapsed_s",),
        },
        first_turn_rule=True,
        target_rule=True,
    ),
)
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


def first_turn_rule(version: str, schemas: Sequence[SchemaVersion] = SCHEMAS) -> bool:
    """Tell whether a record of `version` flags treatment only after a completed model turn."""
    return schemas[position(version, schemas)].first_turn_rule


def target_rule(version: str, schemas: Sequence[SchemaVersion] = SCHEMAS) -> bool:
    """Tell whether a record of `version` holds a declared target to the failure's target and the injector's reading."""
    return schemas[position(version, schemas)].target_rule


# The first version whose totals are incomplete when the backend counted a reply that became no turn (C26): a 200 that
# carried usage and nothing the backend could read was billed, and the turns' totals miss it. Earlier versions keep the
# totals they were written with, so their records replay to the same bytes.
REPLY_COUNT_RULE_FROM = f"{SCHEMA_PREFIX}3"


# The first version whose agent configuration may declare the per-call tool bound (L17): an optional setting that a
# record leaves out when the study declared none, so a record of an earlier version never holds it.
TOOL_CALL_BOUND = "tool_call_bound_s"
TOOL_CALL_BOUND_FROM = f"{SCHEMA_PREFIX}3"


def reply_count_rule(version: str) -> bool:
    """Tell whether a record of `version` counts a billed reply that became no turn as incomplete usage."""
    return _number(version) >= _number(REPLY_COUNT_RULE_FROM)


def _number(version: str) -> int:
    """The number of a version of the schema family; refuse a name outside it."""
    digits = version[len(SCHEMA_PREFIX) :] if version.startswith(SCHEMA_PREFIX) else ""
    if not digits.isdigit():
        raise RecordError(f"unsupported record schema {version!r}")
    return int(digits)


def unread_replies(usage_seen: Mapping[str, Any] | None, turns: int) -> bool:
    """Tell whether a backend counted more replies than the record kept turns: a billed reply it could not read.
    `usage_seen` is the backend's tally as the route keeps it; a backend without one counts nothing."""
    responses = (usage_seen or {}).get("responses")
    return isinstance(responses, int) and not isinstance(responses, bool) and responses > turns


def added_after(version: str, section: str, schemas: Sequence[SchemaVersion] = SCHEMAS) -> frozenset[str]:
    """Return the fields of a section that versions later than `version` introduced."""
    later = schemas[position(version, schemas) + 1 :]
    return frozenset(name for s in later for name in s.added.get(section, ()))


def fields_of(section: str, version: str, schemas: Sequence[SchemaVersion] = SCHEMAS) -> dict[str, tuple[type, ...]]:
    """Return the fields a record of `version` holds in a section, in file order."""
    later = added_after(version, section, schemas)
    return {name: types for name, types in SECTIONS[section].items() if name not in later}


def optional_in(section: str, version: str, schemas: Sequence[SchemaVersion] = SCHEMAS) -> frozenset[str]:
    """Return the fields of a section that a record of `version` may lack: those that version, or an earlier one,
    marked optional. A later version's optional fields are not in it, as its other fields are not."""
    upto = schemas[: position(version, schemas) + 1]
    return frozenset(name for s in upto for name in s.optional.get(section, ()))


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
    """Refuse a mapping with a key outside the section's fields for this version, one of its required fields left out,
    or a value that mistypes. A field the version marks optional may be absent, and is checked when it is present."""
    if not isinstance(data, dict):
        raise RecordError(f"{where}: expected an object, got {type(data).__name__}")
    expected = fields_of(section, version, schemas)
    later = added_after(version, section, schemas)
    unknown = sorted(k for k in data if k not in expected)
    if unknown:
        late = [k for k in unknown if k in later]
        note = f" ({', '.join(late)} added by a later schema than {version})" if late else ""
        raise RecordError(f"{where}: unknown field(s) {unknown}{note}")
    optional = optional_in(section, version, schemas)
    missing = [k for k in expected if k not in data and k not in optional]
    if missing:
        raise RecordError(f"{where}: missing field(s) {missing}")
    for name, types in expected.items():
        if name in data and not _fits(data[name], types):
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


def _check_usage_extra(usage: dict, where: str) -> None:
    """Check the provider's other usage fields of a turn, each when it is present (both are optional): at most the
    cap, each a plain name and a bounded number, and a cut count that is not negative.

    A field the reply left out is not in the mapping (never 0); `extra_cut` counts the fields the cap left out.
    """
    extra = usage.get("extra", {})
    if len(extra) > USAGE_EXTRA_CAP:
        raise RecordError(f"{where}.extra: at most {USAGE_EXTRA_CAP} fields, not {len(extra)}")
    for name, value in extra.items():
        if not usage_name_ok(name):
            raise RecordError(
                f"{where}.extra: the field name {str(name)[:40]!r} is not a plain identifier of at most "
                f"{USAGE_EXTRA_NAME_MAX} characters, or two joined by one dot"
            )
        if not usage_number_ok(value):
            raise RecordError(
                f"{where}.extra.{name}: expected a number (an integer of 64 bits or a finite float), "
                f"got {type(value).__name__}"
            )
    if usage.get("extra_cut", 0) < 0:
        raise RecordError(f"{where}.extra_cut: is {usage['extra_cut']}, not negative")


def _check_turn(turn: dict, version: str, where: str, schemas: Sequence[SchemaVersion]) -> None:
    """Check the parts of one turn entry."""
    _check_waited(turn["infra_wait_by_kind"], f"{where}.infra_wait_by_kind")
    check_section("neutral", turn["neutral"], version, f"{where}.neutral", schemas)
    _check_list("neutral_call", turn["neutral"]["tool_calls"], version, f"{where}.neutral.tool_calls", schemas)
    check_section("usage", turn["usage"], version, f"{where}.usage", schemas)
    if "extra" in turn["usage"] or "extra_cut" in turn["usage"]:  # optional, from version 3
        _check_usage_extra(turn["usage"], f"{where}.usage")
    if turn["native"].get("role") != "assistant":
        raise RecordError(f"{where}.native: the native message of a turn must have the role 'assistant'")
    for i, retry in enumerate(turn["retries"]):
        _check_keys(f"{where}.retries[{i}]", retry, RETRY_FIELDS, required=())


def outcome_from_entry(outcome: Mapping[str, Any]) -> Outcome:
    """Rebuild an outcome from a record's outcome section; the loop's own checks refuse a name, kind or target outside
    the vocabulary (ValueError, KeyError or TypeError). The target is absent before version 3."""
    defect = outcome["defect"]
    held = None if defect is None else HarnessDefect(defect["exception"], defect["stack_sha256"])
    return Outcome(
        outcome["name"],
        outcome["detail"],
        outcome["infra_kind"],
        outcome["error_type"],
        held,
        outcome.get("infra_target"),
    )


def derived_treatment(body: Mapping[str, Any], version: str, schemas: Sequence[SchemaVersion] = SCHEMAS) -> bool:
    """Return the treatment flag a record's own parts make under the rules of its version (`Outcome.is_treatment`).

    The parts are the outcome, the declared `treatment_kinds` and `treatment_targets`, the completed turns of the
    transcript, and the targets the fault injector's reading records as injected in this attempt
    (`expcore.records.injections`). The record's validation, the record built after its instruments finished and the
    store's result all take the flag from here, so they cannot disagree.
    """
    return outcome_from_entry(body["outcome"]).is_treatment(
        body["treatment_kinds"],
        len(body["transcript"]),
        first_turn_rule=first_turn_rule(version, schemas),
        target_rule=target_rule(version, schemas),
        treatment_targets=body.get("treatment_targets") or {},
        injected=injected_targets(body.get("instruments") or {}, body["run_key"], body["attempt"]),
    )


def _check_outcome(body: dict, version: str, schemas: Sequence[SchemaVersion], where: str) -> None:
    """Build the outcome through the loop's own vocabulary check, so a name, kind or target outside it is refused here.

    A defect is checked as the `defect` section of the record's version. The `treatment` flag must be what the record's
    own parts make it under the rules of its version (`derived_treatment`). A record whose flag says otherwise
    contradicts itself (R02).
    """
    outcome = body["outcome"]
    if outcome["defect"] is not None:
        check_section("defect", outcome["defect"], version, f"{where}.defect", schemas)
    try:
        expected = derived_treatment(body, version, schemas)
    except (KeyError, TypeError, ValueError) as exc:
        raise RecordError(f"{where}: {type(exc).__name__}: {exc}") from None
    if outcome["treatment"] is not expected:
        raise RecordError(
            f"{where}.treatment: is {outcome['treatment']} but the outcome, treatment_kinds, treatment_targets, the "
            f"fault injector's reading and the {len(body['transcript'])} completed turn(s) make it {expected}"
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


def check_required_targets(kinds: Collection[str], targets: Mapping[str, Any]) -> None:
    """Refuse treatment kinds that are a fault on one target (`expcore.errors.targeted_kinds`) with no declared target.

    Such a condition's treatment is the failure of its target while the injector held it down; with none declared the
    plain kind would score any target's failure as the manipulation, so the record that carries it is refused, when it
    is built and when it is read back, and never left to `Outcome.is_treatment` alone."""
    held = targeted_kinds()
    missing = [kind for kind in kinds if kind in held and kind not in targets]
    if missing:
        raise RecordError(
            f"treatment_kinds: {missing[0]!r} is declared with no target; a condition names the target it manipulates "
            "in treatment_targets, or any target's failure would be scored as its treatment"
        )


def _check_treatment_targets(targets: Any, kinds: list[str]) -> None:
    """Check the declared targets: each names a declared treatment kind and a position label, written sorted by kind.

    The field is optional and written only when the condition declares a target, so an empty table is refused: a record
    that held one would not write back to its own bytes."""
    if not isinstance(targets, dict):
        raise RecordError("treatment_targets: an object of declared treatment kind to target")
    if not targets:
        raise RecordError(
            "treatment_targets: a condition that declares no target has no such field, never an empty one"
        )
    stray = [k for k in targets if k not in kinds]
    if stray:
        raise RecordError(f"treatment_targets: {stray[0]!r} is not one of the record's treatment_kinds")
    bad = [k for k, t in targets.items() if not isinstance(t, str) or not TARGET_LABEL.fullmatch(t)]
    if bad:
        raise RecordError(f"treatment_targets.{bad[0]}: a target is a position label such as 'providers[1]'")
    if list(targets) != sorted(targets):
        raise RecordError("treatment_targets: the kinds are written sorted")


def _check_refusal(refusal: Any) -> None:
    """Check the refusal an instrument's post-run check recorded: its keys, a namespace as plain as an instrument's, a
    kind of the registry that no condition can declare as its treatment, and a detail that is text (C31)."""
    where = "instrument_refusal"
    _check_keys(where, refusal, REFUSAL_FIELDS)
    if not isinstance(refusal["namespace"], str) or not _NAMESPACE.fullmatch(refusal["namespace"]):
        raise RecordError(f"{where}.namespace: a lower-case instrument namespace, not {refusal['namespace']!r}")
    kind = refusal["infra_kind"]
    if not isinstance(kind, str) or kind not in infra_kinds():
        raise RecordError(
            f"{where}.infra_kind: {kind!r} is not an infrastructure kind; known: {', '.join(infra_kinds())}"
        )
    if is_treatable(kind):
        raise RecordError(f"{where}.infra_kind: {kind!r} is treatable, so no instrument can refuse an attempt with it")
    if not isinstance(refusal["detail"], str):
        raise RecordError(f"{where}.detail: expected str, got {type(refusal['detail']).__name__}")


def _check_configuration(config: Any, version: str, schemas: Sequence[SchemaVersion]) -> None:
    """Check the agent configuration against the loop's own fields, less the settings a later version added.

    `tool_call_bound_s` is optional in a record of version 3 or later, and unknown to an earlier one: absent when the
    study declared none, never a null, and a number above nothing and finite when it did (L17).
    """
    later = set(config_defaults(version, schemas))
    allowed = ({f.name for f in dataclasses.fields(AgentConfig)} | {"terminal_tools"}) - later
    if _number(version) < _number(TOOL_CALL_BOUND_FROM):
        allowed.discard(TOOL_CALL_BOUND)
    _check_keys("agent_config", config, allowed, required=allowed - {TOOL_CALL_BOUND})
    bound = config.get(TOOL_CALL_BOUND)
    if TOOL_CALL_BOUND in config and not (_fits(bound, NUM) and bound > 0):
        raise RecordError(f"agent_config.{TOOL_CALL_BOUND}: expected a number of seconds above nothing, got {bound!r}")
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
        if "elapsed_s" in entry and entry["elapsed_s"] < 0:  # optional, from version 3
            raise RecordError(f"tool_calls[{i}].elapsed_s: {entry['elapsed_s']} is not a number of seconds")
    for i, reading in enumerate(body["clock"]):
        ok = isinstance(reading, list) and len(reading) == 2 and isinstance(reading[0], str) and _fits(reading[1], NUM)
        if not ok:
            raise RecordError(f"clock[{i}]: expected [label, seconds]")
    _check_treatment_kinds(body["treatment_kinds"])
    if "treatment_targets" in body:  # optional, from version 3: absent when the condition declares no target
        _check_treatment_targets(body["treatment_targets"], body["treatment_kinds"])
    check_required_targets(body["treatment_kinds"], body.get("treatment_targets", {}))
    check_section("outcome", body["outcome"], version, "outcome", schemas)
    for place in ("instruments", "scores"):
        held = body.get(place, {})  # absent from a record of a version that predates it
        bad = [k for k in held if not _NAMESPACE.fullmatch(k) or not isinstance(held[k], dict)]
        if bad:
            raise RecordError(f"{place}: each entry is an object under a lower-case namespace, not {bad}")
    _check_outcome(body, version, schemas, "outcome")
    check_section("totals", body["totals"], version, "totals", schemas)
    _check_waited(body["totals"]["infra_wait_by_kind"], "totals.infra_wait_by_kind")
    _check_list("malformed_event", body["totals"]["malformed_events"], version, "totals.malformed_events", schemas)
    _check_configuration(body["agent_config"], version, schemas)
    _check_keys("code", body["code"], CODE_FIELDS)
    _check_route(body["route"])
    if any(not isinstance(p, str) for p in body.get("redacted_paths", ())):
        raise RecordError("redacted_paths: a list of field paths")
    if "instrument_refusal" in body:  # optional, from version 3
        _check_refusal(body["instrument_refusal"])
    return version
