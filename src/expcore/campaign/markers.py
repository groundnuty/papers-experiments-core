# Provenance: new
"""The outcome marker of an attempt: written to the run directory before the store hears of the result (C04).

An attempt ends in three steps: the run finishes, its marker is written atomically, and the store records the result in
one transaction. A kill can fall between any two of them. Before the marker, nothing says how the attempt ended, so it
is interrupted and run again. After the marker and before the store, the marker is the only record of the result, and
the next open commits it; the attempt is never run again. After the store, there is nothing to repair.

The marker names the run and the store's id of the attempt, so a stale marker left in a reused directory by an earlier
attempt is not mistaken for this one's.

Schema 2 added the model turns the attempt completed and the treatment rule of its record's version. Schema 1 markers
are still read, since a campaign killed between its marker and the store must resume after an upgrade: they have no
turn count, so they are read under the version-1 rule (treatment by the declared kind alone) and marked
`legacy_marker`, which recovery records as an event.
"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from expcore.errors import infra_kinds
from expcore.loop.outcomes import HarnessDefect, Outcome

MARKER_NAME = "attempt-marker.json"
MARKER_SCHEMA = 2  # 2: the result carries the model turns completed, the treatment flag of its record and the rule
LEGACY_MARKER_SCHEMA = 1  # still readable: it has no model-turn count, so it is read under the version-1 rule
_RESULT_FIELDS = (
    "ended_utc",
    "wall_s",
    "outage_kind",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "usage_complete",
    "treatment_kinds",
    "model_turns",
    "recorded_treatment",
    "first_turn_rule",
)
# What a schema-1 marker holds of a result; the rest take their defaults under the version-1 rule.
_LEGACY_FIELDS = _RESULT_FIELDS[: _RESULT_FIELDS.index("model_turns")]


class MarkerError(RuntimeError):
    """A marker exists but cannot be trusted: unreadable, of another schema, or holding an impossible result."""


@dataclass(frozen=True)
class AttemptResult:
    """How an attempt came out, as the store records it.

    - `outcome`: the loop's outcome. An infrastructure outcome is never a scored result (C01).
    - `outage_kind`: set by the caller when an outage overlapped the attempt, so its result is set aside whatever the
      outcome was; the attempt is not charged. It names the infrastructure kind of the outage (C03).
    - usage: tokens as reported, `None` when the endpoint did not report them; `usage_complete` is False when the
      totals are known to be short.
    - `treatment_kinds`: the infrastructure kinds the attempt's record carried as its condition's treatment (the
      record's `treatment_kinds`; none by default). The store checks them against the condition's own declaration and
      refuses the result when they differ, so a record and the store cannot disagree about which failures are scored.
    - `model_turns`: how many model turns the attempt completed, from its record's transcript; 0 when there is no
      record (the executor raised, or nothing was run). An infrastructure outcome is treatment only after at least one
      completed turn (`Outcome.is_treatment`): a failure before the agent's first turn is set aside whatever the
      condition declares.
    - `recorded_treatment`: the treatment flag the attempt's record carries, or None when there is no record. The store
      refuses the result when its own decision differs, so the record and the store agree on which attempt is scored.
    - `first_turn_rule`: whether the record's schema version requires a completed turn for treatment (version 2 does,
      version 1 does not); the store decides with the rule of the record's version. A result with no record is new
      work and takes the rule.
    - `legacy_marker`: True for a result read from a schema-1 marker, which has no model-turn count. It is never
      written; recovery applies the version-1 rule to such a result (first_turn_rule False, the declared kind alone
      decides) and records a `legacy_marker` event.
    """

    outcome: Outcome
    ended_utc: str
    wall_s: float
    outage_kind: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    usage_complete: bool = True
    treatment_kinds: tuple[str, ...] = ()
    model_turns: int = 0
    recorded_treatment: bool | None = None
    first_turn_rule: bool = True
    legacy_marker: bool = False

    def __post_init__(self) -> None:
        """Refuse a kind outside the closed vocabulary, so a typo never reaches the store; keep the kinds sorted."""
        if isinstance(self.model_turns, bool) or not isinstance(self.model_turns, int) or self.model_turns < 0:
            raise ValueError("model_turns must be a count of completed turns, zero or more")
        if self.recorded_treatment is not None and not isinstance(self.recorded_treatment, bool):
            raise ValueError("recorded_treatment must be True, False or None")
        if not isinstance(self.first_turn_rule, bool) or not isinstance(self.legacy_marker, bool):
            raise ValueError("first_turn_rule and legacy_marker must be True or False")
        for kind in (*(() if self.outage_kind is None else (self.outage_kind,)), *self.treatment_kinds):
            if kind not in infra_kinds():
                raise ValueError(f"unknown infrastructure kind {kind!r}; known kinds: {', '.join(infra_kinds())}")
        object.__setattr__(self, "treatment_kinds", tuple(sorted(set(self.treatment_kinds))))


def _outcome_json(outcome: Outcome) -> dict[str, Any]:
    """Return an outcome as plain data."""
    return {
        "name": outcome.name,
        "detail": outcome.detail,
        "infra_kind": outcome.infra_kind,
        "error_type": outcome.error_type,
        "defect": None if outcome.defect is None else asdict(outcome.defect),
    }


def _outcome_from(data: dict[str, Any]) -> Outcome:
    """Rebuild an outcome; its own checks refuse an unknown name or kind."""
    defect = data.get("defect")
    return Outcome(
        name=data["name"],
        detail=data["detail"],
        infra_kind=data["infra_kind"],
        error_type=data["error_type"],
        defect=None if defect is None else HarnessDefect(**defect),
    )


def write_marker(run_dir: Path, run_key: str, attempt_id: int, result: AttemptResult) -> Path:
    """Write the marker atomically (temporary file, fsync, rename, fsync of the directory) and return its path."""
    run_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "schema": MARKER_SCHEMA,
        "run_key": run_key,
        "attempt_id": attempt_id,
        "result": {
            "outcome": _outcome_json(result.outcome),
            **{f: getattr(result, f) for f in _RESULT_FIELDS if f != "treatment_kinds"},
            "treatment_kinds": list(result.treatment_kinds),
        },
    }
    target = run_dir / MARKER_NAME
    temporary = run_dir / f".{MARKER_NAME}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(body, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    directory = os.open(run_dir, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return target


def read_marker(run_dir: Path, run_key: str, attempt_id: int) -> AttemptResult | None:
    """Return the result a marker records for this attempt.

    None when there is no marker, or the marker belongs to another run or attempt. A marker that exists and cannot be
    trusted raises MarkerError, because guessing would either lose a finished attempt or run it twice.
    """
    path = run_dir / MARKER_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise MarkerError(f"{path}: cannot be read ({type(error).__name__})") from None
    try:
        body = json.loads(text)
        schema = body["schema"]
        if isinstance(schema, bool) or schema not in (LEGACY_MARKER_SCHEMA, MARKER_SCHEMA):
            raise MarkerError(f"{path}: unknown marker schema {schema!r}")
        if body["run_key"] != run_key or body["attempt_id"] != attempt_id:
            return None
        data = body["result"]
        fields = _LEGACY_FIELDS if schema == LEGACY_MARKER_SCHEMA else _RESULT_FIELDS
        extra = set(data) - {"outcome", *fields}
        if extra:
            raise MarkerError(f"{path}: unknown result fields: {', '.join(sorted(extra))}")
        kept = {f: data[f] for f in fields}
        if schema == LEGACY_MARKER_SCHEMA:  # no turn count to hold it to: the rule of the time, and a mark for recovery
            kept.update(first_turn_rule=False, legacy_marker=True)
        return AttemptResult(outcome=_outcome_from(data["outcome"]), **kept)
    except (ValueError, KeyError, TypeError) as error:
        raise MarkerError(f"{path}: not a valid marker ({type(error).__name__}: {error})") from None
