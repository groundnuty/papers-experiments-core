# Provenance: new
"""Progressive ingest: part of a dataset arrives during a run, as if its owner were still ingesting it (O27).

`ProgressiveIngest` is an `expcore.plugin.Instrument` (namespace `progressive_ingest`, `progressive_ingest_space<i>`
with several spaces) that carries out a plan (`ingestplan.py`) in the space its provisioner is bound to, and nowhere
else: every write goes through the provisioner, so through the mutation gate bound to that space (O09).

- **`start`** first restores the dataset's baseline, which also recovers what a crashed run left (a file half written,
  a file removed and never restored); then it sets the start state: each file whose baseline is "complete" is removed
  and confirmed absent on every provider of the space (`ingestbase.py` has the rules for what may be removed). It then
  starts the schedule's clock and a thread that writes each part when it falls due. A start that cannot set the start
  state restores what it can and raises `InfraError("ingest_failure")`: the attempt is set aside before the agent runs.
- **Each part** is written by `Provisioner.upload` up to its end (`stop`), verified by the plan's hash of that prefix,
  then confirmed: the data provider's block map covers it and every provider that must see the file (`visible_on`, by
  default every provider of the space) shows it at that size. A part that cannot be written or confirmed is an ingest
  failure: the schedule stops, and the parts after it are recorded as abandoned, never dropped.
- **`finish`** stops the schedule (a part not yet due is recorded as not due: the run ended first, which is the
  manipulation, not a failure), restores the baseline and returns the snapshot. **`restore`** is the same, for an
  executor's `finally` when `finish` was not reached; calling it again is harmless. Each file is restored on its own,
  so one that fails leaves no other missing. Both block, so an executor on an event loop calls them through
  `asyncio.to_thread`, as it calls `finish_instruments`. The restore changes the files the run left, and
  `finish_instruments` finishes the last listed instrument first (C31): list the ingest before the probes that read
  those files, which then read them before the restore.
- **`check`**, the post-run check that `finish_instruments` calls once every snapshot is in (C31), raises
  `InfraError("ingest_failure")`, a kind no condition may declare, naming the step, when the start, an arrival, the
  stop or the restore failed: the core records the refusal and sets the attempt aside as charged infrastructure, run
  again, whatever the executor does. **`raise_if_failed`** is the same check, a second guard for the executor to call
  once the record is written.

**The preallocated mode** (`[ingest] mode = "preallocated"`, #107, O44; `ingestsparse.py`) takes each planned file,
part of the baseline, from complete to its final size with only its declared prefix written, at the start: the
complete file is removed (confirmed absent everywhere), made again under its name, extended to its final size by an
empty write and given its prefix, each step confirmed. The schedule starts at the agent's first model turn, through the
first-turn hook (`at_first_turn`, C31), which an executor calls with `first_turn_instruments`; a run whose first turn
never came is refused by the post-run check. Each part is written at its offsets once the data provider's block map is
exactly the ranges before it, then its prefix's hash is read back and the block maps must show exactly the written
ranges at the final size. The restore completes each file in place and never removes it, and replicates it to each
provider the file's `replicas` declare. The baseline's marks in the journal (`ingesthealth.py`), each naming the
attempt that wrote it, hold a start back, with nothing changed, on a baseline another live attempt holds open, or that
a failed start or reset, or a run that ended or died without its reset, left not whole: the start raises
`InfraError("ingest_held")`, which costs the run no attempt (an attempt that changed the baseline and failed is an
`ingest_failure`, as before). **`recover`**, on an instrument that has not started, brings a baseline left not whole
back, verified, and the platform's health check given the journals fails until then, charging no run.

The snapshot (version 1) is clock-labelled: a reading carries `clock`, the provisioner's monotonic clock, which the
loop's clock readings use too, `t_rel_s` from the schedule's start (`anchor`: `instrument_start`, or `first_turn` in
the preallocated mode, whose `first_turn` holds the run's clock reading the hook was given and the seconds from the end
of the start, `after_start_s`) and `utc`. In the preallocated mode `start.files` holds each file's verified start state
(its old and new id, its size and blocks), and a file no part of which has arrived is `prefix_only`. `steps`
holds each part as planned, written (with how late), confirmed (with the seconds each provider took to show it),
failed, abandoned or not due; `ids` every file id the ingest removed, created or restored, with the old and new id and
the file's label, so a study can map a gate's errors to files after the run, and `id_map` each old id to the new one
in its place, for a map keyed by id; `start_s` is the start's seconds, before the first turn. Providers are named by
their position, files by their label, never by a name or a path (O12).

The provisioner given to the instrument must be its own: its change log tells the restore which files the ingest made,
and must never be given to a teardown, which would delete the dataset's restored files. The instrument claims the log
(`Provisioner.claim_log`) when it is built: a log that already holds another user's creation, or another role's
claim, is refused with ProvisionError, and a teardown over a claimed log is refused with nothing sent. Several
instruments over one provisioner, one run after another, share the claim.

A space held on a read-only imported storage (`[space] import`, O41) cannot take an ingest: it is refused when built
(`Refused`), since op-worker refuses the write-back there and a delete leaves the file on the storage. On a writable
imported storage the ingest runs as on a regular one; its removals then unlink the files from the import directory
and its write-backs are files made through REST, with new ids (`docs/storage-import.md`).
"""

import contextlib
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from expcore.errors import InfraError
from expcore_onedata.ingestbase import FAILURES, Baseline, RestoreFailed, detail
from expcore_onedata.ingesthealth import OPEN
from expcore_onedata.ingestplan import PREALLOCATED, IngestPlan, Step
from expcore_onedata.ingestsparse import BaselineNotWhole, SparseBaseline
from expcore_onedata.provision import Provisioner, ProvisionError, Refused
from expcore_onedata.sandbox.kinds import INGEST_FAILURE, INGEST_HELD
from expcore_onedata.streamed import Digest

NAMESPACE = "progressive_ingest"
VERSION = 1
ANCHOR = "instrument_start"  # the arrival schedule's zero: the end of `start`
FIRST_TURN = "first_turn"  # the preallocated schedule's zero: the agent's first model turn (`at_first_turn`)


def id_map(ids: list[dict[str, Any]]) -> dict[str, str]:
    """Each id a planned file had, mapped to the id the ingest gave it in its place, in the order the changes came: the
    map a study keyed by file id follows (a data gate's rows are keyed so), the start's recreations among them."""
    return {e["old_id"]: e["new_id"] for e in ids if e["old_id"] and e["new_id"] and e["old_id"] != e["new_id"]}


class ProgressiveIngest:
    """The instrument of one run's progressive ingest: one per run, over a provisioner of its own."""

    namespace = NAMESPACE

    def __init__(
        self,
        provisioner: Provisioner,
        plan: IngestPlan,
        *,
        journal: Path | str,
        max_wait_s: float = 1.0,
        join_timeout_s: float | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        """`journal` is the JSON-lines file of every id change, kept across runs. The schedule's thread looks at the
        clock at least every `max_wait_s`; `stop` waits at most `join_timeout_s` (the placement's `transfer_timeout_s`
        unless given) for a part being written. `now` is the epoch clock of the readings' `utc`."""
        if plan.space != provisioner.space.label:
            raise ValueError("the plan's space is not the space the provisioner is bound to")
        if provisioner.read_only:  # it removes files and writes them back through the data provider (O41)
            raise Refused(
                f"{provisioner.where}the space is held on a read-only imported storage, so a progressive ingest, which "
                "removes files and writes them back through the data provider, is refused; nothing was claimed or sent"
            )
        provisioner.claim_log(NAMESPACE)  # its change log is the ingest's alone: refused when another user wrote it
        several = bool(provisioner.where)
        index = provisioner.space.position.removeprefix("spaces[").removesuffix("]")
        self.namespace = f"{NAMESPACE}_space{index}" if several else NAMESPACE
        self._space = provisioner.space.position if several else None
        self._p, self._plan = provisioner, plan
        self._sparse = plan.mode == PREALLOCATED
        self._base = (SparseBaseline if self._sparse else Baseline)(provisioner, plan, Path(journal), now)
        self._marks = self._base if isinstance(self._base, SparseBaseline) else None  # the preallocated mode's marks
        self._opened = False  # this instrument marked the baseline open: its start began to change it (O44)
        self._steps = plan.steps()
        self._max_wait_s = max_wait_s
        self._join_timeout_s = provisioner.settings.transfer_timeout_s if join_timeout_s is None else join_timeout_s
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = self._closed = False
        self._retried_before = provisioner.reads_retried  # the count the snapshot's `reads_retried` is taken from
        self._t0: float | None = None
        self._ready: float | None = None  # the provisioner's clock at the end of a start that set the start state
        self._first_turn: dict[str, Any] | None = None
        self._readings: list[dict[str, Any]] = []
        self._files = {f.label: {"baseline_id": None, "arrived_id": None, "restored_id": None} for f in plan.files}
        self._done = 0
        self._failure: dict[str, Any] | None = None
        self._defect: str | None = None
        self._start: dict[str, Any] | None = None
        self._restore: dict[str, Any] | None = None

    @property
    def progress(self) -> int:
        """How many of the plan's parts have been written and confirmed so far."""
        return self._done

    @property
    def failure(self) -> dict[str, Any] | None:
        """The ingest failure recorded so far, or None."""
        return self._failure

    @property
    def at_first_turn(self) -> Callable[[Mapping[str, Any]], None] | None:
        """The first-turn hook (C31) in the preallocated mode, which anchors the schedule there; None in the arrival
        mode, whose schedule starts with the instrument, so `first_turn_instruments` takes no reading for it."""
        return self._anchor_at_first_turn if self._sparse else None

    def _reading(self, kind: str, step: Step, **fields: Any) -> None:
        base = {"step": kind, "label": step.label, "part": step.part, "parts": step.parts, "due_s": step.due_s}
        with self._base.lock:
            self._readings.append({**base, **fields})

    def _failed(self, phase: str, error: BaseException, began: float) -> dict[str, Any]:
        """Journal a failure, and in the preallocated mode, once this instrument began to change the baseline, mark the
        baseline unhealthy, before any recovery is tried; return the failure's record."""
        text = detail(error)
        self._base.journal(entry="failure", phase=phase, error=type(error).__name__, detail=text)
        if self._marks is not None and self._opened:
            self._marks.mark_failed(phase, type(error).__name__, text)
        return {
            "ok": False,
            "seconds": round(self._p.clock() - began, 3),
            "error": type(error).__name__,
            "detail": text,
        }

    # -- start ---------------------------------------------------------------------------------------------------------
    def start(self, identity: Mapping[str, Any]) -> None:
        """Restore the baseline (recovering a crashed run), set the start state, and start the schedule; raise
        `InfraError("ingest_failure")` when the start state cannot be set, after restoring what can be."""
        if self._started:
            raise RuntimeError("a ProgressiveIngest instruments one run: make a new one for each")
        self._started = True
        self._retried_before = self._p.reads_retried  # the provisioner serves one run's ingest after another (O29)
        began = self._p.clock()
        try:
            self._p.verify_space()
            self._base.read_journal()
            if self._marks is not None:  # a baseline not whole refuses; every staged copy is checked; then it is open
                self._marks.admit_start()
                self._opened = True
            restored = self._base.restore_all("start")  # each file on its own, its staged source checked first
            self._base.check_directories()
            for planned in self._plan.files:
                if planned.baseline == "absent":  # a "complete" one was checked by its restore
                    self._base.check_source(planned)
            states = self._set_start_state()
        except BaselineNotWhole as error:  # held back by the baseline's mark, with nothing changed: a hold (O44)
            text = detail(error)
            self._start = {"ok": False, "held": True, "seconds": round(self._p.clock() - began, 3), "detail": text}
            raise self._start_held(text) from None
        except RestoreFailed as error:  # every file was tried, and every other one restored
            self._start = {**self._failed("start", error, began), "restore": error.files}
            raise self._start_refused(error) from None
        except FAILURES as error:
            self._start = {**self._failed("start", error, began), "recovered": self._recover()}
            raise self._start_refused(error) from None
        self._start = {"ok": True, "seconds": round(self._p.clock() - began, 3), "restore": restored}
        self._start.update({} if states is None else {"files": states})
        if self._marks is not None:
            self._marks.marks.mark(OPEN, "run")
        self._ready = self._p.clock()
        for step in self._steps:
            self._reading("planned", step, bytes=step.stop, sha256=step.sha256)
        if not self._sparse:  # the preallocated schedule starts at the agent's first turn (`at_first_turn`)
            self._begin(self._ready)

    def _set_start_state(self) -> dict[str, Any] | None:
        """Remove each "complete" file (the arrival mode), or take it to its prefix at its final size (the preallocated
        mode, whose verified state per file is returned); note the ids."""
        if not self._sparse:
            for label, file_id in self._base.remove_for_start().items():
                self._files[label]["baseline_id"] = file_id
            return None
        assert isinstance(self._base, SparseBaseline)
        states = self._base.start_state()
        for label, state in states.items():
            self._files[label].update(baseline_id=state["baseline_id"], arrived_id=state["file_id"])
        return states

    def _begin(self, t0: float) -> None:
        self._t0 = t0
        self._thread = threading.Thread(target=self._drive, name="progressive-ingest", daemon=True)
        self._thread.start()

    def _anchor_at_first_turn(self, moment: Mapping[str, Any]) -> None:
        """The preallocated mode's first-turn hook: start the schedule now, its offsets counted from here, and record
        the run's clock reading the executor gave and the seconds since the start state was set. Nothing starts after
        a start that did not set the start state, or after the schedule was stopped."""
        if self._ready is None or self._halt.is_set():
            return
        if self._t0 is not None:
            raise RuntimeError("the first-turn hook anchors one run's schedule once")
        t0 = self._p.clock()
        clock_s = moment.get("clock_s")
        self._first_turn = {
            "clock": round(t0, 3),
            "run_clock_s": clock_s if isinstance(clock_s, int | float) and not isinstance(clock_s, bool) else None,
            "after_start_s": round(t0 - self._ready, 3),
            "utc": self._base.stamp()["utc"],
        }
        self._begin(t0)

    def _start_refused(self, error: BaseException) -> InfraError:
        text = f"{self._p.where}the progressive ingest could not set its start state: {detail(error)}"
        return InfraError(INGEST_FAILURE, text[:600])

    def _start_held(self, why: str) -> InfraError:
        """The hold of a start its baseline's mark refused, with nothing changed: `ingest_held`, which costs the run no
        attempt, so the campaign tries it again (once the run on the baseline resets it, or a recovery brings it
        back)."""
        return InfraError(INGEST_HELD, f"the progressive ingest held its start back: {why}"[:600])

    def _recover(self) -> dict[str, Any] | None:
        """After a failed start, restore the baseline as far as it goes; the result is recorded, never raised. In the
        preallocated mode a start refused before it changed anything recovers nothing (None), and a recovery that
        verifies every file marks the baseline whole, as a verified clear settles an injection."""
        if self._marks is not None and not self._opened:
            return None
        try:
            files = self._base.restore_all("start")
        except FAILURES as error:
            files = error.files if isinstance(error, RestoreFailed) else None
            return {"ok": False, "error": type(error).__name__, "detail": detail(error), "files": files}
        if self._marks is not None:
            self._marks.marks.whole("start")
        return {"ok": True, "files": files}

    # -- the schedule --------------------------------------------------------------------------------------------------
    def _drive(self) -> None:
        """The schedule's thread: write each part when it falls due, until the plan ends, a part fails or `stop`."""
        step = None
        try:
            for index, step in enumerate(self._steps):
                if not self._wait_until(step.due_s):
                    return
                self._perform(step)
                self._done = index + 1
        except FAILURES as error:
            self._fail("arrival", type(error).__name__, detail(error), step)
        except Exception as error:  # a defect of the schedule: kept for raise_if_failed, never taken for the platform's
            self._defect = type(error).__name__

    def _wait_until(self, due_s: float) -> bool:
        """Wait until the schedule's clock reaches `due_s`; False when the schedule was stopped first."""
        deadline = (self._t0 or 0.0) + due_s
        while not self._halt.is_set():
            left = deadline - self._p.clock()
            if left <= 0:
                return True
            self._halt.wait(min(left, self._max_wait_s))
        return False

    def _perform(self, step: Step) -> None:
        if self._sparse:
            self._perform_part(step)
            return
        planned, ids = self._plan.file(step.label), self._files[step.label]
        old = self._base.last_id.get(step.label)
        try:
            done = self._base.upload(planned, "run", old, stop=step.stop)
        except FAILURES:
            if self._base.last_id.get(step.label) != old:  # the upload made a file before it failed, now recorded
                ids["arrived_id"] = self._base.last_id[step.label]
            raise
        written = self._base.stamp()
        if ids["arrived_id"] != done.file_id:  # the first part, or a file made again under the label: recorded at once
            self._base.record_id(step.label, "created", "run", old, done.file_id, action=done.action)
            ids["arrived_id"] = done.file_id
        if done.digest != Digest(step.stop, step.sha256):
            raise ProvisionError(f"{self._p.where}{step.label}: a written part does not have the plan's hash")
        late = round(written["clock"] - (self._t0 or 0.0) - step.due_s, 3)
        self._reading(
            "written", step, **written, late_s=late, bytes=step.stop, action=done.action, file_id=done.file_id
        )
        confirmed = self._base.confirm(done.file_id, step.stop)
        self._reading("confirmed", step, **self._base.stamp(), bytes=step.stop, file_id=done.file_id, **confirmed)

    def _perform_part(self, step: Step) -> None:
        """A part of the preallocated mode: written at its offsets into the file the start made, then its prefix's
        hash read back and the file's state confirmed (`ingestsparse.py`)."""
        assert isinstance(self._base, SparseBaseline)
        planned, file_id = self._plan.file(step.label), self._files[step.label]["arrived_id"]
        sent = self._base.write_part(planned, step, file_id)
        written = self._base.stamp()
        self._base.check_part(planned, step, file_id)
        late = round(written["clock"] - (self._t0 or 0.0) - step.due_s, 3)
        self._reading(
            "written", step, **written, late_s=late, start=step.start, bytes=step.stop, sent=sent.size, file_id=file_id
        )
        confirmed = self._base.confirm_part(planned, step, file_id)
        self._reading("confirmed", step, **self._base.stamp(), bytes=step.stop, file_id=file_id, **confirmed)

    def _fail(self, phase: str, error: str, text: str, step: Step | None = None) -> None:
        """Record an ingest failure (the first one is the attempt's) and, for a part, its reading."""
        failure = {"kind": INGEST_FAILURE, "phase": phase, "error": error, "detail": text}
        if step is not None:
            failure.update(label=step.label, part=step.part, parts=step.parts)
            self._reading("failed", step, **self._base.stamp(), error=error, detail=text)
        if self._failure is None:
            self._failure = failure
        self._base.journal(entry="failure", **failure)

    def stop(self) -> bool:
        """Stop the schedule: no part not yet written will be. Return True once its thread has ended, or False when a
        part was still being written at the bound, which is an ingest failure. `finish` and `restore` call it."""
        self._halt.set()
        if self._thread is not None:
            self._thread.join(self._join_timeout_s)
            if self._thread.is_alive():
                if self._failure is None or self._failure["phase"] != "stop":
                    self._fail("stop", "Timeout", "a part was still being written when the schedule's bound ran out")
                return False
        if not self._closed and self._ready is not None:  # the start state was set, so the plan's parts are recorded
            self._closed = True
            failed = self._failure is not None and self._failure["phase"] == "arrival"
            for step in self._steps[self._done + (1 if failed else 0) :]:
                self._reading("abandoned" if failed else "not_due", step)
        return True

    # -- restore and finish --------------------------------------------------------------------------------------------
    def restore(self) -> dict[str, Any]:
        """Stop the schedule and bring every planned file back to its baseline, verified; return the record, or raise
        `InfraError("ingest_failure")` when the baseline cannot be shown back. Calling it again is harmless."""
        if self._restore is not None and self._restore["ok"]:
            return self._restore
        if not self.stop():
            text = "a part was still being written; not restored"
            self._restore = {"ok": False, "error": "Timeout", "detail": text}
            if self._marks is not None and self._opened:
                self._marks.mark_failed("restore", "Timeout", text)
            raise InfraError(INGEST_FAILURE, f"{self._p.where}the progressive ingest could not stop its schedule")
        if self._marks is not None and not self._opened:  # it changed nothing; a marked baseline needs `recover`
            self._restore = {"ok": True, "seconds": 0.0, "files": {}, "untouched": True}
            return self._restore
        began = self._p.clock()
        try:
            self._p.verify_space()
            if not self._started:
                self._base.read_journal()
            files = self._base.restore_all("restore")
        except FAILURES as error:
            self._restore = self._failed("restore", error, began)
            if isinstance(error, RestoreFailed):  # every other file came back: say which, and with which id
                self._restore["files"] = self._restored(error.files)
            raise InfraError(
                INGEST_FAILURE,
                f"{self._p.where}the progressive ingest could not restore the baseline: {detail(error)}"[:600],
            ) from None
        self._restore = {"ok": True, "seconds": round(self._p.clock() - began, 3), "files": self._restored(files)}
        if self._marks is not None:
            self._marks.marks.whole("restore")
        return self._restore

    def recover(self) -> dict[str, Any]:
        """Bring the baseline back after a run left it not whole, on an instrument that has not started: every planned
        file restored and verified, the mirror's replicas included, and in the preallocated mode the baseline marked
        whole, so the health check passes and starts are admitted again (`ingesthealth.py`). Return the record (each
        file's result, and `id_map` for a file it wrote back under a new id), or
        raise `InfraError("ingest_failure")`, the baseline marked unhealthy, when it cannot be shown back. Refused
        while another process that still runs holds the baseline open: run it with no run on the plan's space."""
        if self._started:
            raise RuntimeError("recover() is for an instrument that has not started: make a new one")
        with self._marks.marks.locked() if self._marks is not None else contextlib.nullcontext():
            files, seconds = self._recover_whole()
        with self._base.lock:
            changed = id_map(list(self._base.ids))  # a name written back has a new id, which a study's map follows
        return {"ok": True, "seconds": seconds, "files": files, "id_map": changed}

    def _recover_whole(self) -> tuple[dict[str, Any], float]:
        """`recover`'s restore, under the journal's admission lock in the preallocated mode, so no start is admitted
        until it has marked the baseline; the files' results by label, and its seconds."""
        began, marking = self._p.clock(), False
        try:
            self._p.verify_space()
            self._base.read_journal()
            if self._marks is not None:
                self._marks.admit_recovery()
                marking = True  # from here a failure marks the baseline unhealthy
            files = self._base.restore_all("recover")
        except FAILURES as error:
            self._failed("recover", error, began)
            if self._marks is not None and marking:
                self._marks.mark_failed("recover", type(error).__name__, detail(error))
            raise InfraError(
                INGEST_FAILURE,
                f"{self._p.where}the progressive ingest could not recover the baseline: {detail(error)}"[:600],
            ) from None
        if self._marks is not None:
            self._marks.marks.whole("recover")
        return files, round(self._p.clock() - began, 3)

    def _restored(self, files: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Note the id each restored file ends with; return the restore's results."""
        for label, done in files.items():
            if done["ok"]:
                self._files[label]["restored_id"] = done["file_id"]
        return files

    def finish(self) -> dict[str, Any]:
        """Stop the schedule, restore the baseline and return the snapshot. A restore that fails is in the snapshot
        (`restore.ok` false), and `raise_if_failed` raises it once the record is written."""
        with contextlib.suppress(InfraError):  # recorded in self._restore, never lost
            self.restore()
        return self._snapshot()

    def check(self) -> None:
        """The post-run check (`expcore.records.instruments.finish_instruments` calls it once every snapshot is in,
        C31): return when the start state was set, every due part arrived and the baseline came back; otherwise raise
        `InfraError("ingest_failure")`, a kind no condition may declare, naming the step that failed. The core then puts
        the refusal in the record and sets the attempt aside as charged infrastructure, run again, whatever the
        executor does after; an ingest that failed is never scored as the agent's outcome. A start, a finish or a
        restore that never ran refuses too, since unknown is not a pass. A defect of the schedule is raised as one
        (RuntimeError), so the attempt is a harness defect."""
        if self._defect is not None:
            raise RuntimeError(f"the progressive ingest's schedule failed with a defect ({self._defect})")
        if self._start is not None and self._start.get("held"):  # a hold, as the start raised it: no attempt charged
            raise self._start_held(self._start["detail"])
        if self._start is None or not self._start["ok"]:
            why = f": {self._start['detail']}" if self._start is not None else ": it never ran"
            self._refuse(f"at the start, which did not set the start state{why}")
        if self._unanchored():
            self._refuse("before its schedule began: the agent's first model turn, which anchors it, never came")
        if self._failure is not None:
            step = "an arrival" if self._failure["phase"] == "arrival" else "the stop of its schedule"
            part = f" ({self._failure['label']}, part {self._failure['part']})" if "label" in self._failure else ""
            self._refuse(f"at {step}{part}: {self._failure['detail']}")
        if self._restore is None or not self._restore["ok"]:
            why = f": {self._restore['detail']}" if self._restore is not None else ": it never ran"
            self._refuse(f"at the restore, which did not bring the baseline back{why}")

    def _unanchored(self) -> bool:
        """A preallocated plan with parts whose schedule never began: its first turn never came."""
        return self._sparse and bool(self._steps) and self._t0 is None

    def _refuse(self, what: str) -> None:
        raise InfraError(INGEST_FAILURE, f"{self._p.where}the progressive ingest failed {what}"[:600])

    def raise_if_failed(self) -> None:
        """The same as `check`, for an executor to call once the record is written: a second guard, since the core
        already sets the attempt aside on the check's refusal."""
        self.check()

    def _snapshot(self) -> dict[str, Any]:
        def relative(reading: dict[str, Any]) -> dict[str, Any]:
            if "clock" not in reading or self._t0 is None:
                return reading
            return {**reading, "t_rel_s": round(reading["clock"] - self._t0, 3)}

        with self._base.lock:
            steps = [relative(r) for r in self._readings]
            ids = [relative(e) for e in self._base.ids]
        failed = self._failure.get("label") if self._failure else None
        files = {}
        for planned in self._plan.files:
            parts = sum(1 for s in steps if s["step"] == "confirmed" and s["label"] == planned.label)
            none = "prefix_only" if self._sparse else "not_arrived"
            state = "arrived" if parts == planned.parts else "partial" if parts else none
            state = "failed" if failed == planned.label else state
            files[planned.label] = {"baseline": planned.baseline, "state": state, "parts_confirmed": parts}
            files[planned.label].update(self._files[planned.label])
        ok = (  # what `check` passes, and nothing else
            bool(self._start and self._start["ok"])
            and not self._unanchored()
            and self._failure is None
            and self._defect is None
            and bool(self._restore and self._restore["ok"])
        )
        return {
            "version": VERSION,
            **({} if self._space is None else {"space": self._space}),
            "ok": ok,
            "anchor": FIRST_TURN if self._sparse else ANCHOR,
            "anchor_clock": None if self._t0 is None else round(self._t0, 3),
            **({"first_turn": self._first_turn} if self._sparse else {}),
            "providers": [self._p.client.provider_label(p) for p in self._base.visible_on],
            "plan": self._plan.summary(),
            "start": self._start,
            "steps": steps,
            "ids": ids,
            "id_map": id_map(ids),
            "start_s": None if self._start is None else self._start["seconds"],
            "files": files,
            "failure": self._failure,
            "defect": self._defect,
            "restore": self._restore,
            "reads_retried": self._p.reads_retried - self._retried_before,
        }


__all__ = ["ANCHOR", "FIRST_TURN", "NAMESPACE", "VERSION", "ProgressiveIngest"]
