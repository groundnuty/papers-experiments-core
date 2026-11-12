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
  `asyncio.to_thread`, as it calls `finish_instruments`.
- **`check`**, the post-run check that `finish_instruments` calls once every snapshot is in (C31), raises
  `InfraError("ingest_failure")`, a kind no condition may declare, naming the step, when the start, an arrival, the
  stop or the restore failed: the core records the refusal and sets the attempt aside as charged infrastructure, run
  again, whatever the executor does. **`raise_if_failed`** is the same check, a second guard for the executor to call
  once the record is written.

The snapshot (version 1) is clock-labelled: a reading carries `clock`, the provisioner's monotonic clock, which the
loop's clock readings use too, `t_rel_s` from the schedule's start (`anchor`: `instrument_start`) and `utc`. `steps`
holds each part as planned, written (with how late), confirmed (with the seconds each provider took to show it),
failed, abandoned or not due; `ids` every file id the ingest removed, created or restored, with the old and new id and
the file's label, so a study can map a gate's errors to files after the run. Providers are named by their position,
files by their label, never by a name or a path (O12).

The provisioner given to the instrument must be its own: its change log tells the restore which files the ingest made,
and must never be given to a teardown, which would delete the dataset's restored files. The instrument claims the log
(`Provisioner.claim_log`) when it is built: a log that already holds another user's creation, or another role's
claim, is refused with ProvisionError, and a teardown over a claimed log is refused with nothing sent. Several
instruments over one provisioner, one run after another, share the claim.
"""

import contextlib
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from expcore.errors import InfraError
from expcore_onedata.ingestbase import FAILURES, Baseline, RestoreFailed, detail
from expcore_onedata.ingestplan import IngestPlan, Step
from expcore_onedata.provision import Provisioner, ProvisionError
from expcore_onedata.sandbox.kinds import INGEST_FAILURE
from expcore_onedata.streamed import Digest

NAMESPACE = "progressive_ingest"
VERSION = 1
ANCHOR = "instrument_start"  # the schedule's zero: the end of `start`


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
        provisioner.claim_log(NAMESPACE)  # its change log is the ingest's alone: refused when another user wrote it
        several = bool(provisioner.where)
        index = provisioner.space.position.removeprefix("spaces[").removesuffix("]")
        self.namespace = f"{NAMESPACE}_space{index}" if several else NAMESPACE
        self._space = provisioner.space.position if several else None
        self._p, self._plan = provisioner, plan
        self._base = Baseline(provisioner, plan, Path(journal), now)
        self._steps = plan.steps()
        self._max_wait_s = max_wait_s
        self._join_timeout_s = provisioner.settings.transfer_timeout_s if join_timeout_s is None else join_timeout_s
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = self._closed = False
        self._retried_before = provisioner.reads_retried  # the count the snapshot's `reads_retried` is taken from
        self._t0: float | None = None
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

    def _reading(self, kind: str, step: Step, **fields: Any) -> None:
        base = {"step": kind, "label": step.label, "part": step.part, "parts": step.parts, "due_s": step.due_s}
        with self._base.lock:
            self._readings.append({**base, **fields})

    def _failed(self, phase: str, error: BaseException, began: float) -> dict[str, Any]:
        text = detail(error)
        self._base.journal(entry="failure", phase=phase, error=type(error).__name__, detail=text)
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
            restored = self._base.restore_all("start")  # each file on its own, its staged source checked first
            self._base.check_directories()
            for planned in self._plan.files:
                if planned.baseline == "absent":  # a "complete" one was checked by its restore
                    self._base.check_source(planned)
            for label, file_id in self._base.remove_for_start().items():
                self._files[label]["baseline_id"] = file_id
        except RestoreFailed as error:  # every file was tried, and every other one restored
            self._start = {**self._failed("start", error, began), "restore": error.files}
            raise self._start_refused(error) from None
        except FAILURES as error:
            self._start = {**self._failed("start", error, began), "recovered": self._recover()}
            raise self._start_refused(error) from None
        self._start = {"ok": True, "seconds": round(self._p.clock() - began, 3), "restore": restored}
        self._t0 = self._p.clock()
        for step in self._steps:
            self._reading("planned", step, bytes=step.stop, sha256=step.sha256)
        self._thread = threading.Thread(target=self._drive, name="progressive-ingest", daemon=True)
        self._thread.start()

    def _start_refused(self, error: BaseException) -> InfraError:
        text = f"{self._p.where}the progressive ingest could not set its start state: {detail(error)}"
        return InfraError(INGEST_FAILURE, text[:600])

    def _recover(self) -> dict[str, Any]:
        """After a failed start, restore the baseline as far as it goes; the result is recorded, never raised."""
        try:
            return {"ok": True, "files": self._base.restore_all("start")}
        except FAILURES as error:
            files = error.files if isinstance(error, RestoreFailed) else None
            return {"ok": False, "error": type(error).__name__, "detail": detail(error), "files": files}

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
        if not self._closed and self._t0 is not None:
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
            self._restore = {"ok": False, "error": "Timeout", "detail": "a part was still being written; not restored"}
            raise InfraError(INGEST_FAILURE, f"{self._p.where}the progressive ingest could not stop its schedule")
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
        return self._restore

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
        if self._start is None or not self._start["ok"]:
            why = f": {self._start['detail']}" if self._start is not None else ": it never ran"
            self._refuse(f"at the start, which did not set the start state{why}")
        if self._failure is not None:
            step = "an arrival" if self._failure["phase"] == "arrival" else "the stop of its schedule"
            part = f" ({self._failure['label']}, part {self._failure['part']})" if "label" in self._failure else ""
            self._refuse(f"at {step}{part}: {self._failure['detail']}")
        if self._restore is None or not self._restore["ok"]:
            why = f": {self._restore['detail']}" if self._restore is not None else ": it never ran"
            self._refuse(f"at the restore, which did not bring the baseline back{why}")

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
            state = "arrived" if parts == planned.parts else "partial" if parts else "not_arrived"
            state = "failed" if failed == planned.label else state
            files[planned.label] = {"baseline": planned.baseline, "state": state, "parts_confirmed": parts}
            files[planned.label].update(self._files[planned.label])
        ok = self._failure is None and self._defect is None and bool(self._restore and self._restore["ok"])
        return {
            "version": VERSION,
            **({} if self._space is None else {"space": self._space}),
            "ok": ok,
            "anchor": ANCHOR,
            "anchor_clock": None if self._t0 is None else round(self._t0, 3),
            "providers": [self._p.client.provider_label(p) for p in self._base.visible_on],
            "plan": self._plan.summary(),
            "start": self._start,
            "steps": steps,
            "ids": ids,
            "files": files,
            "failure": self._failure,
            "defect": self._defect,
            "restore": self._restore,
            "reads_retried": self._p.reads_retried - self._retried_before,
        }


__all__ = ["ANCHOR", "NAMESPACE", "VERSION", "ProgressiveIngest"]
