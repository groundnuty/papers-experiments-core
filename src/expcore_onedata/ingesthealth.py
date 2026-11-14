# Provenance: new
"""Whether a preallocated ingest's baseline is whole, written in its journal and read by the health check (O44, #107).

A preallocated start removes each planned file and makes it again with only its prefix written, so between the start's
first removal and the reset's verified completion the dataset's baseline is not whole: by design while a run is on it,
and by accident when a start or a reset fails or the process running it dies. The journal says which, as the fault
injector's journal says whether a provider is down (`fault_journal.py`): a **baseline mark** is a line of the ingest's
journal, pushed to disk before the step it announces (with the journal's path, by each instrument's first line), with
the space's position, a `state`, the `phase` that wrote it, the plan's hash (`plan`), the writer (`holder`: its pid
and boot id, `fault_journal.holder`) and its **owner** (`owner`, `expcore.campaign.liveness.owner`): the process as the
lease pool names a holder (boot id, pid and start time, so a reused pid is not mistaken for it) and, when the start
runs inside a campaign's attempt, that campaign and the store's id of the attempt's row. A mark written before owners
were recorded is read by its `holder`, as before.

- `open`: a start is about to change the baseline (journalled before its first removal, and before the restore it runs
  first), or a run is on it (`phase` `run`). It stays until the reset verifies every file whole.
- `unhealthy`: a start, a reset or a recovery failed after the baseline began to change, with the file and the step it
  was at, when known, and what failed. Journalled before the start's own recovery runs.
- `whole`: a restore verified every planned file whole at its path, by its hash and the data provider's block map, and
  on every provider its replicas declare, by that provider's block map: the end of a reset, of a start's own recovery
  after a failure, or of `ProgressiveIngest.recover`.

**An open mark is held or orphaned** (`owner_state`). It is *held* while its owner is live: the attempt it names is
running (the campaign's store says so, `liveness.state`), or, for a mark that names no attempt, its process still
runs. It is *orphaned*, and as unhealthy as an `unhealthy` mark, the moment no live attempt owns it: its attempt has
ended (the store holds its result, finished or set aside, or this process runs that campaign no more), or its process
is gone, or the mark names no owner that can be read. Nothing waits for the run's ceiling. An attempt of another
process that still runs reads held, since this process cannot read that process's store.

**A start is held back** (`start_refusal`), before anything is read or sent, unless the space's last mark is `whole`
or there is none: a held baseline (another run is on it), an orphaned one and an `unhealthy` one. The ingest raises
`InfraError("ingest_held")`, a hold that costs its run no attempt (`sandbox.kinds`), whichever it is: the run is set
aside and tried again, and only an attempt that did change the baseline and failed is an `ingest_failure`. The start
reads the mark again under the journal's admission lock (`Marks.locked`) before it writes `open`, so no two starts pass
one `whole` mark. **`ProgressiveIngest.recover` is the way back** from an orphaned or unhealthy baseline: under the same
lock it restores every file, verified, and marks the baseline `whole`, or `unhealthy` again when it fails. It is
refused for a mark of another plan than its own (that run's files and replicas are what it must verify) and for a held
`open` mark, a run in progress (`recovery_refusal`), but for one this process took outside any attempt (an instrument
it never restored).

**The health check reads the marks** (`baseline_health`; `health.PlatformHealth(..., ingest_journals=...)`). It fails,
so no run is admitted, while a space's last mark is `unhealthy` or an orphaned `open`, a run that failed or died and
left its baseline sparse or missing; the failure charges no run (`Health(..., charge=False)`), since the attempt that
left it was charged already, so the gate closes and holds every run until a recovery marks the baseline whole. A held
`open` mark is a run in progress: a check outside any attempt (the pre-run check, the dispatcher's own) is deferred, so
the campaign holds its runs, charging none, and asks again after its `recheck_s`, until the run's reset; a check inside
an attempt (its post-run check) reads it healthy, as another run's post-run check judges that run, unless the mark's
owner is that same attempt, whose ingest then ended without its reset. A journal that cannot be read fails the check;
a torn last line, which a crash can leave, is skipped, as the journal's other readers skip it. A missing journal has no
marks: no preallocated start ever ran there.

The detail names the space by its position and a file by its label, never a path, a host or a name (O12).
"""

import contextlib
import fcntl
import json
import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from expcore.campaign import liveness
from expcore.campaign.healthgate import Health
from expcore.campaign.liveness import owner
from expcore_onedata.fault_journal import holder, holder_alive, make_directories

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
    """The fields of a baseline mark, the writer named as its holder and its owner (the attempt this context runs, or
    this process outside one); `Baseline.journal` adds the space."""
    return {"entry": BASELINE, "state": state, "phase": phase, "holder": holder(), "owner": owner(), **extra}


def owner_state(mark: Mapping[str, Any]) -> str | None:
    """Whether a mark's owner is live: `liveness.LIVE`, `liveness.ENDED` or None (it cannot be told). A mark written
    before owners were recorded is read by its holder's pid and boot id."""
    if "owner" in mark:
        return liveness.state(mark["owner"])
    alive = holder_alive(mark.get("holder"))
    return liveness.LIVE if alive is True else liveness.ENDED if alive is False else None


def held(mark: Mapping[str, Any] | None) -> bool:
    """Whether a baseline is held: its last mark is `open` and a live attempt (or live process) owns it."""
    return mark is not None and mark["state"] == OPEN and owner_state(mark) == liveness.LIVE


def _owned_here(mark: Mapping[str, Any]) -> bool:
    """Whether a mark's owner is the attempt this context runs."""
    owner, here = mark.get("owner"), liveness.current()
    if not isinstance(owner, Mapping) or here is None:
        return False
    return all(owner.get(field) == here[field] for field in ("process", "campaign", "attempt_id"))


def _taken_here_outside_an_attempt(mark: Mapping[str, Any]) -> bool:
    """Whether a mark was written by this process outside any attempt (or before owners were recorded): an instrument
    of this process that was never restored, which this process may recover."""
    owner = mark.get("owner")
    if isinstance(owner, Mapping):
        return "attempt_id" not in owner and owner.get("process") == liveness.this_process()
    named = mark.get("holder")
    return isinstance(named, Mapping) and named.get("pid") == os.getpid()


def _phase(mark: Mapping[str, Any]) -> str:
    phase = mark.get("phase")
    return phase if isinstance(phase, str) and phase.isidentifier() else "a step"


def _at(mark: Mapping[str, Any]) -> str:
    """The file and step a failed start's mark names, as words; empty when it names none."""
    file, step = mark.get("file"), mark.get("step")
    return f" at {file}'s {step}" if isinstance(file, str) and isinstance(step, str) else ""


def start_refusal(mark: Mapping[str, Any] | None) -> str | None:
    """Why a start may not begin on this baseline, or None when its last mark is `whole` or there is none: held by a
    live attempt (the start waits for its reset), or orphaned or `unhealthy` (a recovery must bring it back)."""
    if mark is None or mark["state"] == WHOLE:
        return None
    if mark["state"] == UNHEALTHY:
        return f"its baseline is not whole: its {_phase(mark)} failed{_at(mark)}; {RECOVER}"
    if held(mark):
        return (
            f"its baseline is held open by a live attempt's ingest since its {_phase(mark)}: a run in progress; a "
            "start waits for its reset"
        )
    return (
        f"its baseline is not whole: a run's ingest stopped during its {_phase(mark)} and never restored it (its "
        f"attempt has ended, or its process is gone); {RECOVER}"
    )


def recovery_refusal(mark: Mapping[str, Any] | None, plan: str) -> str | None:
    """Why a recovery with the plan of this hash may not run now: a baseline another plan's run left not whole (its
    files and replicas are what the recovery must verify, and this plan may name others), or a held `open` baseline,
    a run in progress it would write into, unless this process took it outside any attempt; None otherwise."""
    if mark is None or mark["state"] == WHOLE:
        return None
    if mark.get("plan") != plan:
        return (
            "its baseline was left not whole by a run of another plan, or one its mark does not name: recover with "
            "the plan that run used, whose files and replicas the recovery verifies"
        )
    if mark["state"] != OPEN:
        return None
    if held(mark) and not _taken_here_outside_an_attempt(mark):
        return "its baseline is held by a live attempt's ingest, a run in progress; recover once it has ended"
    return None


def health_refusal(mark: Mapping[str, Any]) -> str | None:
    """Why the health check fails for this mark: `unhealthy`; an `open` one that no live attempt owns; or, in a check
    inside an attempt, an `open` one that attempt owns, whose ingest ended without its reset."""
    if mark["state"] == UNHEALTHY or (mark["state"] == OPEN and not held(mark)):
        return start_refusal(mark)
    if mark["state"] == OPEN and _owned_here(mark):
        return f"its run's ingest ended without its reset, which leaves its baseline open since its {_phase(mark)}"
    return None


def baseline_health(journals: Iterable[Path | str]) -> Health:
    """The ingests' half of the health check, never raising: fails, naming the space, while a baseline is unhealthy
    or orphaned, charging no run (an earlier attempt left it), or when a journal cannot be read; deferred, outside any
    attempt, while a live attempt holds a baseline open, so runs wait for its reset; otherwise healthy. A failure of
    any space wins over a deferral of another."""
    deferral: Health | None = None
    for journal in journals:
        try:
            marks = last_marks(Path(journal))
        except (OSError, UnicodeDecodeError, Unreadable) as error:
            why = str(error) if isinstance(error, Unreadable) else "the ingest's journal could not be read"
            return Health(False, f"a progressive ingest's baseline is unknown: {why}")
        for space, mark in sorted(marks.items()):
            refusal = health_refusal(mark)
            if refusal is not None:
                return Health(False, f"{space}: the progressive ingest refuses runs: {refusal}", charge=False)
            if deferral is None and held(mark) and liveness.current() is None:
                why = f"{space}: the progressive ingest holds runs: {start_refusal(mark)}"
                deferral = Health(False, why, deferred=True, charge=False)
    return deferral or Health(True, "every progressive ingest's baseline is whole or held by a run in progress")


class Marks:
    """The baseline marks of one space in one ingest's journal, each naming the plan's hash: written through `write`
    (the baseline's journal, which pushes each line to disk) and read back from `journal`. `locked` holds the
    journal's admission lock (`<journal>.lock`, an exclusive `flock` across processes and instruments), under which a
    start reads the mark again before it writes its own, so no two starts pass one `whole` mark, and a recovery runs
    whole."""

    def __init__(self, journal: Path, space: str, write: Callable[..., None], plan: str) -> None:
        self._journal, self._space, self._write, self._plan = journal, space, write, plan

    def last(self) -> dict[str, Any] | None:
        """The space's last mark, or None; `Unreadable` for a journal that cannot be read."""
        return last_marks(self._journal).get(self._space)

    def mark(self, state: str, phase: str, **extra: Any) -> None:
        self._write(**mark_fields(state, phase, plan=self._plan, **extra))

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the journal's admission lock, waiting for it; the kernel drops it with a holder that dies."""
        make_directories(self._journal.parent)  # durable, as the journal's own are
        descriptor = os.open(self._journal.with_name(self._journal.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)  # closing the descriptor drops the lock


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
    "held",
    "last_marks",
    "mark_fields",
    "owner_state",
    "recovery_refusal",
    "start_refusal",
]
