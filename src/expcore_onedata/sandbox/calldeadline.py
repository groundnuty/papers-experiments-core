# Provenance: new
"""What a cell's deadline says on the call that ran it, and the digest of it a run's ending carries (O34, O97).

A cell whose process outlived its kill at the deadline leaves a record in the runner (`deadline.enforce`, published
by `cells.CellRunner`): its steps, what each survivor was waiting on and whose block it was. The code tool puts it on
the call it returns (`ToolOutput.cell_deadline`, `tool_calls[i].cell_deadline`), cut to the keys the record's schema
holds (`expcore.records.cell_deadline`) and checked against it, so the record is never one that cannot be written back.
A call the loop cut at the agent's cap returns no tool output, so its deadline reaches the record by the run's ending
instead: `digest` is a line of at most `DIGEST_CHARS` characters about the first survivor, which `kinds.run_ended_error`
appends to the error's detail, and so to the attempt's outcome. It carries a process name, a state, a kernel symbol, the
role of a mount and an fd number, and the abort's method and result: nothing a cell wrote, and no path.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from expcore.records import cell_deadline as shape

KEYS = (
    "outcome", "utc", "grace_s", "margin_s", "restore", "steps", "survivors", "survivors_cut", "connection",
    "attribution",
)  # fmt: skip
DIGEST_CHARS = 300
_KILLED = "killed"  # a clean kill: nothing outlived it, so there is nothing to say


def cell_deadline_of(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The deadline `record` as a call carries it: a deep copy of the keys of `KEYS` it holds, or None when it has
    nothing to say (no record, a clean kill, or a record that was never decided, which has no time of its cap). It is
    checked against the record's schema; one that does not pass is cut to its outcome, its times, the names of its steps
    and the verdict, and one that still does not pass is left out: a call never carries what its record would refuse."""
    if not record or record.get("outcome") in (None, _KILLED) or not isinstance(record.get("utc"), str):
        return None
    kept = {key: copy.deepcopy(record[key]) for key in KEYS if key in record}
    if shape.problem(kept) is None:
        return kept
    steps = [{"step": s["step"]} for s in kept.get("steps", []) if isinstance(s, dict) and "step" in s]
    minimal = {key: kept[key] for key in ("outcome", "utc", "grace_s", "margin_s", "restore") if key in kept}
    minimal["steps"] = steps[: shape.STEPS_CAP]
    if "attribution" in kept and shape.problem({**minimal, "attribution": kept["attribution"]}) is None:
        minimal["attribution"] = kept["attribution"]
    return minimal if shape.problem(minimal) is None else None


def digest(record: Mapping[str, Any]) -> str:
    """One line about the first survivor of a deadline's `record`, for the detail of the run's ending: its name, its
    state, the first task's kernel wait, what it was blocked on and the abort's method and result; "" when the record
    read no survivor. At most `DIGEST_CHARS` characters."""
    survivors = record.get("survivors")
    if not isinstance(survivors, list) or not survivors or not isinstance(survivors[0], Mapping):
        return ""
    first = survivors[0]
    parts = [f"survivor {first.get('comm')} state {first.get('state')}"]
    tasks = [t for t in first.get("tasks") or [] if isinstance(t, Mapping) and t.get("state") == "D"]
    if tasks and tasks[0].get("wchan"):
        parts.append(f"wchan {tasks[0]['wchan']}")
    waits = [w for w in first.get("waits_on") or [] if isinstance(w, Mapping)]
    if waits:
        parts.append(f"blocked on the {waits[0].get('mount')} mount, fd {waits[0].get('fd')}")
    abort = next((s for s in record.get("steps") or [] if isinstance(s, Mapping) and s.get("step") == "abort"), None)
    if abort is not None:
        parts.append(f"abort {abort.get('method')}: {'ok' if abort.get('ok') is True else 'not ok'}")
    return "; ".join(parts)[:DIGEST_CHARS]


__all__ = ["DIGEST_CHARS", "KEYS", "cell_deadline_of", "digest"]
