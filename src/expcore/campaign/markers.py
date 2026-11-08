# Provenance: new
"""The outcome marker of an attempt: written to the run directory before the store hears of the result (C04).

An attempt ends in three steps: the run finishes, its marker is written atomically, and the store records the result in
one transaction. A kill can fall between any two of them. Before the marker, nothing says how the attempt ended, so it
is interrupted and run again. After the marker and before the store, the marker is the only record of the result, and
the next open commits it; the attempt is never run again. After the store, there is nothing to repair.

The marker names the run and the store's id of the attempt, so a stale marker left in a reused directory by an earlier
attempt is not mistaken for this one's.
"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from expcore.errors import infra_kinds
from expcore.loop.outcomes import HarnessDefect, Outcome

MARKER_NAME = "attempt-marker.json"
MARKER_SCHEMA = 1
_RESULT_FIELDS = (
    "ended_utc",
    "wall_s",
    "outage_kind",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "usage_complete",
)


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

    def __post_init__(self) -> None:
        """Refuse an outage kind outside the closed vocabulary, so a typo never reaches the store."""
        if self.outage_kind is not None and self.outage_kind not in infra_kinds():
            raise ValueError(
                f"unknown infrastructure kind {self.outage_kind!r}; known kinds: {', '.join(infra_kinds())}"
            )


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
        "result": {"outcome": _outcome_json(result.outcome), **{f: getattr(result, f) for f in _RESULT_FIELDS}},
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
        if body["schema"] != MARKER_SCHEMA:
            raise MarkerError(f"{path}: unknown marker schema {body['schema']!r}")
        if body["run_key"] != run_key or body["attempt_id"] != attempt_id:
            return None
        data = body["result"]
        extra = set(data) - {"outcome", *_RESULT_FIELDS}
        if extra:
            raise MarkerError(f"{path}: unknown result fields: {', '.join(sorted(extra))}")
        return AttemptResult(outcome=_outcome_from(data["outcome"]), **{f: data[f] for f in _RESULT_FIELDS})
    except (ValueError, KeyError, TypeError) as error:
        raise MarkerError(f"{path}: not a valid marker ({type(error).__name__}: {error})") from None
