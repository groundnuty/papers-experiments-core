# Provenance: new
"""Whether a preallocated ingest's baseline is whole, written in its journal and read by the health check (O44, #107).

A preallocated start removes each planned file and makes it again with only its prefix written, so between the start's
first removal and the reset's verified completion the dataset's baseline is not whole: by design while a run is on it,
and by accident when a start or a reset fails or the process running it dies. The journal says which, as the fault
injector's journal says whether a provider is down (`fault_journal.py`): a **baseline mark** is a line of the ingest's
journal, pushed to disk before the step it announces, with the space's position, a `state`, the `phase` that wrote it
and the writer (`holder`: its pid and boot id, `fault_journal.holder`).

- `open`: a start is about to change the baseline (journalled before its first removal, and before the restore it runs
  first), or a run is on it (`phase` `run`). It stays until the reset verifies every file whole.
- `unhealthy`: a start, a reset or a recovery failed after the baseline began to change, with the file and the step it
  was at, when known, and what failed. Journalled before the start's own recovery runs.
- `whole`: a restore verified every planned file whole at its path, by its hash and the data provider's block map, and
  on every provider its replicas declare, by that provider's block map: the end of a reset, of a start's own recovery
  after a failure, or of `ProgressiveIngest.recover`.

**A start is refused** (`start_refusal`), before anything is read or sent, unless the space's last mark is `whole` or
there is none: an `unhealthy` baseline, and an `open` one, whoever holds it (a live holder is a run in progress, or an
instrument of this process that was never restored). **`ProgressiveIngest.recover` is the way back**: it restores
every file, verified, and marks the baseline `whole`, or `unhealthy` again when it fails. It is refused only for an
`open` mark whose holder is another process that still runs (`recovery_refusal`).

**The health check reads the marks** (`baseline_health`; `health.PlatformHealth(..., ingest_journals=...)`) and fails,
so no run is admitted, while a space's last mark is `unhealthy`, or `open` with a holder that is gone (its pid names no
process, or it ran in an earlier boot) or that the mark does not name: a run that died and left its baseline sparse or
missing. An `open` mark whose holder still runs is a run in progress and reads healthy here; that run's own post-run
check judges it. A journal that cannot be read fails the check; a torn last line, which a crash can leave, is skipped,
as the journal's other readers skip it. A missing journal has no marks: no preallocated start ever ran there.

The detail names the space by its position and a file by its label, never a path, a host or a name (O12).
"""

import json
import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from expcore.campaign.healthgate import Health
from expcore_onedata.fault_journal import holder, holder_alive

BASELINE = "baseline"  # the entry of a baseline mark in the ingest's journal
OPEN, UNHEALTHY, WHOLE = "open", "unhealthy", "whole"
STATES = (OPEN, UNHEALTHY, WHOLE)
RECOVER = "the progressive ingest's recover() must verify every file whole before a run starts"


class Unreadable(ValueError):
    """A journal line other than a torn last one is not a JSON object, or a mark has a state that is not one of ours."""


def last_marks(journal: Path) -> dict[str, dict[str, Any]]:
    """The last baseline mark of each space in a journal, by the space's position; none for a missing journal. A torn
    last line is skipped; any other line that cannot be read raises `Unreadable`."""
    if not journal.exists():
        return {}
    lines = journal.read_text(encoding="utf-8").splitlines()
    marks: dict[str, dict[str, Any]] = {}
    for number, text in enumerate(lines, start=1):
        try:
            line = json.loads(text)
        except json.JSONDecodeError:
            if number == len(lines):
                continue
            raise Unreadable("the ingest's journal has a line that cannot be read") from None
        if not isinstance(line, dict):
            raise Unreadable("the ingest's journal has a line that is not an object")
        if line.get("entry") != BASELINE:
            continue
        if line.get("state") not in STATES or not isinstance(line.get("space"), str):
            raise Unreadable("the ingest's journal has a baseline mark it cannot read")
        marks[line["space"]] = line
    return marks


def mark_fields(state: str, phase: str, **extra: Any) -> dict[str, Any]:
    """The fields of a baseline mark, the writer named as its holder; `Baseline.journal` adds the space."""
    return {"entry": BASELINE, "state": state, "phase": phase, "holder": holder(), **extra}


def _phase(mark: Mapping[str, Any]) -> str:
    phase = mark.get("phase")
    return phase if isinstance(phase, str) and phase.isidentifier() else "a step"


def _at(mark: Mapping[str, Any]) -> str:
    """The file and step a failed start's mark names, as words; empty when it names none."""
    file, step = mark.get("file"), mark.get("step")
    return f" at {file}'s {step}" if isinstance(file, str) and isinstance(step, str) else ""


def start_refusal(mark: Mapping[str, Any] | None) -> str | None:
    """Why a start may not begin on this baseline, or None when its last mark is `whole` or there is none."""
    if mark is None or mark["state"] == WHOLE:
        return None
    if mark["state"] == UNHEALTHY:
        return f"its baseline is not whole: its {_phase(mark)} failed{_at(mark)}; {RECOVER}"
    if holder_alive(mark.get("holder")) is True:
        return (
            f"its baseline is held open by a run's ingest since its {_phase(mark)}: one under way, or one this "
            "process never restored; a start waits for its reset"
        )
    return (
        f"its baseline is not whole: a run's ingest stopped during its {_phase(mark)} and never restored it; {RECOVER}"
    )


def recovery_refusal(mark: Mapping[str, Any] | None) -> str | None:
    """Why a recovery may not run now: an `open` baseline held by another process that still runs, whose run it would
    write into; None otherwise."""
    if mark is None or mark["state"] != OPEN:
        return None
    named = mark.get("holder")
    pid = named.get("pid") if isinstance(named, Mapping) else None
    if holder_alive(named) is True and pid != os.getpid():
        return "its baseline is held by a run's ingest in another process that still runs; recover once it has ended"
    return None


def health_refusal(mark: Mapping[str, Any]) -> str | None:
    """Why the health check fails for this mark: `unhealthy`, or `open` with a holder that is gone or not named."""
    if mark["state"] == UNHEALTHY or (mark["state"] == OPEN and holder_alive(mark.get("holder")) is not True):
        return start_refusal(mark)
    return None


def baseline_health(journals: Iterable[Path | str]) -> Health:
    """The ingests' half of the health check: fails, naming the space, while a baseline is not whole and no live run
    holds it, or when a journal cannot be read; never raises."""
    for journal in journals:
        try:
            marks = last_marks(Path(journal))
        except (OSError, UnicodeDecodeError, Unreadable) as error:
            why = str(error) if isinstance(error, Unreadable) else "the ingest's journal could not be read"
            return Health(False, f"a progressive ingest's baseline is unknown: {why}")
        for space, mark in sorted(marks.items()):
            refusal = health_refusal(mark)
            if refusal is not None:
                return Health(False, f"{space}: the progressive ingest refuses runs: {refusal}")
    return Health(True, "every progressive ingest's baseline is whole or held by a run in progress")


class Marks:
    """The baseline marks of one space in one ingest's journal: written through `write` (the baseline's journal, which
    pushes each line to disk) and read back from `journal`."""

    def __init__(self, journal: Path, space: str, write: Callable[..., None]) -> None:
        self._journal, self._space, self._write = journal, space, write

    def last(self) -> dict[str, Any] | None:
        """The space's last mark, or None; `Unreadable` for a journal that cannot be read."""
        return last_marks(self._journal).get(self._space)

    def mark(self, state: str, phase: str, **extra: Any) -> None:
        self._write(**mark_fields(state, phase, **extra))


__all__ = [
    "BASELINE",
    "OPEN",
    "STATES",
    "UNHEALTHY",
    "WHOLE",
    "Marks",
    "Unreadable",
    "baseline_health",
    "health_refusal",
    "last_marks",
    "mark_fields",
    "recovery_refusal",
    "start_refusal",
]
