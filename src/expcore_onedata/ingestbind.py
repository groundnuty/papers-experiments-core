# Provenance: new
"""Bind a recorded progressive-ingest snapshot to the plan that made it, by the plan's content (O27, O44, #262).

A snapshot names the plan it ran under (`snapshot["plan"]`: `IngestPlan.summary()`) and holds one `planned` reading per
part, then the written and confirmed ones, each with the part's due time. A study that reads a run's arrivals "relative
to the plan" must pair the snapshot with the plan it ran under: matching by the files' labels alone pairs it with any
plan that names the same files, one whose every part is 300 s later included. `check_recorded_plan(plan, snapshot)` is
that pairing, and a study's own checker (a `recorded_arrival`, say) calls it before it reads an arrival time.

It compares the plan's hash first, then every field the snapshot itemises, so a refusal can say where the plans differ:

- the plan's hash (`plan.sha256`), which covers the space, the providers a part must be seen on, each file's path,
  staged content, schedule and baseline, and in the preallocated mode the mode, prefixes and replicas;
- the mode, and each file by label and in order: `bytes`, `sha256`, `at_s`, `parts`, `over_s`, `baseline` and `prefix`;
- each reading of `steps`: the part it names exists, with the plan's part count, due time and, where the reading holds
  them, end byte and prefix hash; and, once the start set the start state, one `planned` reading per part of the plan.

Anything that differs raises `PlanMismatch` naming the file and the field. Anything missing raises it too, saying
"unknown is not a pass": a snapshot that records no plan, no hash, a file without its schedule or no steps is never
taken for a match. The two keys a snapshot from before the preallocated mode did not keep, `mode` and each file's
`prefix`, read as `arrival` and 0: every such snapshot came from an arrival plan, whose hash is the one it had then, so
it binds to the arrival plan that made it, and a preallocated plan differs from it in `mode`.
"""

from collections.abc import Mapping
from typing import Any

from expcore_onedata.ingestplan import ARRIVAL, IngestPlan, Step

FILE_FIELDS = ("bytes", "sha256", "at_s", "parts", "over_s", "baseline", "prefix")
_LEGACY = {"mode": ARRIVAL, "prefix": 0}  # what a snapshot from before O44 means by a key it does not hold
_SHOWN = 48  # how much of a recorded value a refusal shows


class PlanMismatch(ValueError):
    """A snapshot was not recorded by this plan, or cannot be shown to have been. `label` names the file and `field`
    the field that differs (`mode`, `at_s`, `due_s`, `sha256`, ...); `label` is None when no one file differs."""

    def __init__(self, text: str, *, label: str | None = None, field: str | None = None) -> None:
        super().__init__(f"the snapshot was not recorded by this plan: {text}")
        self.label, self.field = label, field


def _unknown(what: str, *, label: str | None = None, field: str | None = None) -> PlanMismatch:
    return PlanMismatch(f"{what}: unknown is not a pass", label=label, field=field)


def _same(planned: object, recorded: object) -> bool:
    """Equal in value and in kind: a number is not text and a boolean is not a number (30 and 30.0 are equal)."""
    if isinstance(planned, bool) or isinstance(recorded, bool):
        return type(planned) is type(recorded) and planned == recorded
    if isinstance(planned, int | float):
        return isinstance(recorded, int | float) and planned == recorded
    return type(planned) is type(recorded) and planned == recorded


def _differs(where: str, field: str, planned: object, recorded: object, *, label: str | None) -> PlanMismatch:
    shown = repr(recorded)
    shown = shown if len(shown) <= _SHOWN else shown[:_SHOWN] + "..."
    return PlanMismatch(
        f"{where} {field} differs (the plan has {planned!r}, the snapshot recorded {shown})", label=label, field=field
    )


def check_recorded_plan(plan: IngestPlan, snapshot: Mapping[str, Any]) -> None:
    """Return when `snapshot`, one of `ProgressiveIngest`'s, was recorded under exactly `plan`; raise `PlanMismatch`
    naming the file and the field otherwise, and when the snapshot cannot say."""
    recorded = snapshot.get("plan") if isinstance(snapshot, Mapping) else None
    if not isinstance(recorded, Mapping):
        raise _unknown("it records no plan")
    digest = recorded.get("sha256")
    if not isinstance(digest, str) or not digest:
        raise _unknown("it records no plan hash", field="sha256")
    same_hash = digest == plan.sha256  # settled first, and said last: the fields below name where the plans differ
    mode = recorded.get("mode", _LEGACY["mode"])
    if not _same(plan.mode, mode):
        raise _differs("the plan's", "mode", plan.mode, mode, label=None)
    _check_files(plan, recorded.get("files"))
    _check_steps(plan, snapshot)
    if not same_hash:
        raise PlanMismatch(
            "the plan's hash differs from the one it recorded, though every field a snapshot itemises agrees: the "
            "difference lies in what it does not (the space, visible_on, a file's path or its replicas)",
            field="sha256",
        )


def _check_files(plan: IngestPlan, files: object) -> None:
    """Each planned file, by label and in order, with the schedule, size, source and prefix the snapshot recorded."""
    if not isinstance(files, list):
        raise _unknown("it records no files of the plan", field="files")
    labels = [entry.get("label") if isinstance(entry, Mapping) else None for entry in files]
    if not all(isinstance(label, str) for label in labels):
        raise _unknown("a file it records has no label", field="label")
    _check_labels([f.label for f in plan.files], labels)
    for expected, entry in zip(plan.summary()["files"], files, strict=True):
        for field in FILE_FIELDS:
            if field not in entry and field not in _LEGACY:
                raise _unknown(f"{expected['label']}: it records no {field}", label=expected["label"], field=field)
            value = entry.get(field, _LEGACY.get(field))
            if not _same(expected[field], value):
                raise _differs(f"{expected['label']}:", field, expected[field], value, label=expected["label"])


def _check_labels(planned: list[str], recorded: list[str]) -> None:
    """The snapshot records every planned file once, no other, in the plan's order."""
    for label in recorded:
        if recorded.count(label) > 1:
            raise PlanMismatch(f"{label}: the snapshot records the file twice", label=label, field="label")
    for label in planned:
        if label not in recorded:
            text = f"{label}: the plan has the file and the snapshot records none"
            raise PlanMismatch(text, label=label, field="label")
    for label in recorded:
        if label not in planned:
            text = f"{label}: the snapshot records the file and the plan has none"
            raise PlanMismatch(text, label=label, field="label")
    if recorded != planned:
        first = next(n for n, (a, b) in enumerate(zip(planned, recorded, strict=True)) if a != b)
        raise PlanMismatch(f"{planned[first]}: the files are in another order", label=planned[first], field="label")


def _check_steps(plan: IngestPlan, snapshot: Mapping[str, Any]) -> None:
    """Each reading of `steps` names a part of the plan and agrees with it; once the start set the start state, every
    part has its one `planned` reading."""
    rows = snapshot.get("steps")
    if not isinstance(rows, list):
        raise _unknown("it records no steps", field="steps")
    parts = {(s.label, s.part): s for s in plan.steps()}
    labels = {f.label for f in plan.files}
    planned: set[tuple[str, int]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise _unknown("a step it records is not a reading", field="steps")
        label, part = row.get("label"), row.get("part")
        valid = isinstance(label, str) and isinstance(part, int) and not isinstance(part, bool)  # True is not part 1
        step = parts.get((label, part)) if valid else None
        if step is None:
            field = "part" if label in labels else "label"
            raise PlanMismatch(
                f"a step reading names {label!r} part {part!r}, which the plan does not have",
                label=label if isinstance(label, str) and label in labels else None,
                field=field,
            )
        _check_reading(step, row)
        if row.get("step") == "planned":
            if (step.label, step.part) in planned:
                raise PlanMismatch(f"{step.label} part {step.part}: planned twice", label=step.label, field="steps")
            planned.add((step.label, step.part))
    start = snapshot.get("start")
    if isinstance(start, Mapping) and start.get("ok") is True:  # the start set its state: every part was planned
        missing = [key for key in parts if key not in planned]
        if missing:
            label, part = missing[0]
            raise _unknown(f"{label} part {part}: it records no planned reading of it", label=label, field="steps")


def _check_reading(step: Step, row: Mapping[str, Any]) -> None:
    """One reading against the plan's part: its part count and due time always; its end byte where it holds one, and a
    `planned` reading's prefix hash."""
    where = f"{step.label} part {step.part}:"
    wanted = {"parts": step.parts, "due_s": step.due_s}
    if row.get("step") == "planned":
        wanted |= {"bytes": step.stop, "sha256": step.sha256}
    elif "bytes" in row:
        wanted["bytes"] = step.stop
    for field, planned in wanted.items():
        if field not in row:
            raise _unknown(f"{where} it records no {field}", label=step.label, field=field)
        if not _same(planned, row[field]):
            raise _differs(where, field, planned, row[field], label=step.label)


__all__ = ["FILE_FIELDS", "PlanMismatch", "check_recorded_plan"]
