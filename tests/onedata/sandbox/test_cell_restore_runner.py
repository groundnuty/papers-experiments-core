"""O97: the cell runner's side of a restorable abort. A deadline record that left a restore pending is published as the
cell's own (no end of the run, no stop, the kill stays the agent's time limit); `restore` runs the backend's restore
outside the metadata's lock and publishes `mount_restored`, or ends the run as kind `sandbox` when it fails; nothing
execs while it is pending; a run whose body ends first needs none; and an end already published stands."""

import threading
import time

import pytest

from expcore.errors import InfraError
from expcore_onedata.sandbox import cells as cells_module
from expcore_onedata.sandbox import deadline, kinds
from expcore_onedata.sandbox.api import ExecResult, SandboxError

ATTRIBUTION = {
    "verdict": "agent",
    "why": "blocked_on_held_down_holder",
    "target": "providers[1]",
    "space_holder": "providers[1]",
}


def pending(**fields) -> dict:
    """The record of a deadline whose abort freed the agent's survivor, with its restore pending."""
    return {
        "utc": "2026-01-01T00:00:00.000+00:00", "grace_s": 5.0, "margin_s": 30.0, "outcome": deadline.MOUNT_ABORTED,
        "restore": deadline.RESTORE_PENDING, "attribution": dict(ATTRIBUTION), **fields,
        "steps": [{"step": "kill", "t_s": 0.1, "signalled": 1}, {"step": "abort", "t_s": 5.0, "ok": True}],
    }  # fmt: skip


def result() -> ExecResult:
    return ExecResult("", "", -9, 31.0, timed_out=True, kill={"reason": "time_limit", "category": "agent"})


@pytest.fixture
def cells(world, mounted):
    return mounted._cells


def entry_of(cells, cell: int = 1) -> dict:
    return next(e for e in cells._meta["cell_deadlines"] if e["cell"] == cell)


# --------------------------------------------------------------------------------------------------------- publishing
def test_o97_a_pending_restore_is_published_as_the_cells_own_and_ends_nothing(cells):
    """O97: the timer's thread publishes the record under `cell_deadlines` and as the running cell's deadline, keeps it
    as the restore to run, sets no `ended`, and orders no stop (`mount_aborted` orders none)."""
    record = pending()
    then = cells._publish(1, record)
    assert then is None
    assert cells.ended is None
    assert cells.pending_restore is record
    assert cells.restoring
    assert record["cell"] == 1  # the restore's failure says whose deadline it was
    assert entry_of(cells)["restore"] == deadline.RESTORE_PENDING


def test_o97_a_deadline_that_is_infrastructure_still_ends_the_run_as_it_did(cells):
    """O97: `mount_aborted` with no restore (an infrastructure verdict, a backend that cannot restore) is published as
    the run's end, with no pending restore."""
    record = pending()
    del record["restore"]
    cells._publish(1, record)
    assert cells.ended is record
    assert cells.pending_restore is None
    assert not cells.restoring


def test_o97_an_end_already_published_stands_and_no_restore_is_kept_beside_it(cells):
    """O97: a bounded wait that gave up on this outcome ended the run as `survived_kill` (#226); the late
    `mount_aborted` that was restorable is kept among the cell's deadlines and never replaces the end, and no
    restore is pending, so nothing restores a mount on a run that is already ended."""
    published = {"outcome": deadline.SURVIVED, "steps": []}
    cells.ended = published
    cells._publish(1, pending())
    assert cells.ended is published
    assert cells.pending_restore is None


def test_o97_the_cells_kill_stays_the_agents_time_limit_and_the_result_says_a_restore_is_pending(cells):
    """O97: `_with_deadline` leaves the cell's kill as the harness's timer made it (`time_limit`, agent), not
    `kill_survived`, and sets `ExecResult.restore_pending` for the code tool to run the restore."""
    record, res, entry = pending(), result(), {"cell": 1}
    cells._publish(1, record)
    assert cells._with_deadline(res, entry, record) is True  # the deadline escalated
    assert res.kill == {"reason": "time_limit", "category": "agent"}
    assert res.restore_pending is True
    assert entry["deadline"] is record
    assert "kill" not in entry


def test_o97_a_deadline_that_ends_the_run_still_makes_the_kill_kill_survived_and_no_restore_pending(cells):
    """O97: the infrastructure path: `kill_survived`, infrastructure, no `restore_pending`."""
    record = pending()
    del record["restore"]
    res, entry = result(), {"cell": 1}
    cells._publish(1, record)
    assert cells._with_deadline(res, entry, record) is True
    assert res.kill["reason"] == "kill_survived"
    assert res.kill["category"] == "infrastructure"
    assert res.restore_pending is False


# --------------------------------------------------------------------------------------------------------- the restore
def test_o97_a_restore_that_succeeds_makes_the_deadline_mount_restored_and_the_run_goes_on(cells):
    """O97: the backend's restore answers `ok: True` with its generation: the record's outcome is `mount_restored`
    (here and in the metadata's copy of it), its `restore` is `restored`, a `restored` step names the seconds and the
    generation, nothing is pending, and the run is not ended."""
    record = pending()
    cells._publish(1, record)
    answer = cells.restore(lambda: {"ok": True, "generation": 2, "wait_s": 11.8})
    assert answer["ok"] is True
    for read in (record, entry_of(cells)):
        assert read["outcome"] == deadline.MOUNT_RESTORED
        assert read["restore"] == deadline.RESTORE_DONE
    restored = [s for s in record["steps"] if s["step"] == "restored"]
    assert len(restored) == 1
    assert restored[0]["generation"] == 2
    assert restored[0]["s"] >= 0
    assert set(restored[0]) == {"step", "s", "generation"}  # nothing else of the restore's answer is copied
    assert cells.ended is None
    assert cells.pending_restore is None
    assert not cells.restoring
    assert not deadline.ends_run(record)


def test_o97_the_restore_runs_with_the_metadata_lock_free_and_the_state_running(cells):
    """O97: a restore may take a minute and a half: the lock the timer's thread, `settle` and the metadata's readers
    take is not held across it, and the record says `running` meanwhile, which `ends_run` reads as standing."""
    record, seen = pending(), {}
    cells._publish(1, record)

    def restore() -> dict:
        free = []

        def take_the_lock() -> None:  # another thread, as the timer's is: it holds the lock only to read and release it
            got = cells.meta_lock.acquire(timeout=5)
            free.append(got)
            if got:
                cells.meta_lock.release()

        thread = threading.Thread(target=take_the_lock)
        thread.start()
        thread.join()
        seen["lock_free"] = free == [True]
        seen["state"] = record["restore"]
        seen["restoring"] = cells.restoring
        return {"ok": True, "generation": 2}

    cells.restore(restore)
    assert seen == {"lock_free": True, "state": deadline.RESTORE_RUNNING, "restoring": True}


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({"ok": False, "why": "oneclient did not come up"}, id="not ok"),
        pytest.param({"ok": None}, id="not attempted"),
        pytest.param({"ok": "yes"}, id="a truthy ok that is not True"),
        pytest.param({}, id="no ok"),
        pytest.param(RuntimeError("the unmount hung"), id="a raise"),
    ],
)
def test_o97_a_restore_that_does_not_succeed_ends_the_run_as_sandbox_and_never_raises(cells, answer):
    """O97: anything but `ok: True` is a failure, an exception too, and never raised out of `restore` (the call keeps
    its own result, a timeout): the record says `failed` with a `restore_failed` step, the run is ended
    (`mount_lost`, by `restore`, the cell's number), and the error every later call gets is kind `sandbox`."""
    record = pending()
    cells._publish(1, record)

    def restore() -> dict:
        if isinstance(answer, Exception):
            raise answer
        return answer

    cells.restore(restore)
    assert record["restore"] == deadline.RESTORE_FAILED
    assert record["outcome"] == deadline.MOUNT_ABORTED
    assert entry_of(cells)["restore"] == deadline.RESTORE_FAILED
    assert [s["step"] for s in record["steps"]][-1] == "restore_failed"
    assert cells.ended == {"outcome": deadline.MOUNT_LOST, "by": deadline.RESTORE, "cell": 1, "why": cells.ended["why"]}
    assert deadline.ends_run(record)
    assert cells.pending_restore is None
    error = kinds.run_ended_error(cells.ended)
    assert isinstance(error, InfraError)
    assert error.kind == "sandbox"
    assert "cell 1" in error.detail


def test_o97_the_reason_a_restore_failed_is_one_short_sentence_and_a_raise_names_only_its_class(cells):
    """O97: what the record and the error keep of a failure is the restore's own `why`, cut to a sentence, or for a
    raise the exception's class alone, never its text, which may hold a path."""
    cells._publish(1, pending())

    def restore() -> dict:
        raise OSError("/home/someone/run/mount: transport endpoint is not connected")

    cells.restore(restore)
    assert cells.ended["why"] == "the restore raised OSError"
    assert "/home" not in kinds.run_ended_error(cells.ended).detail
    cells2 = cells
    cells2.ended = None
    cells2._publish(2, pending())
    cells2.restore(lambda: {"ok": False, "why": "x" * 1000})
    assert len(cells2.ended["why"]) == 200


def test_o97_a_restore_with_nothing_pending_is_a_no_op_that_calls_nothing(cells):
    """O97: `restore` on a run with no pending restore (a clean kill, an infrastructure abort, a restore already done)
    answers `ok: None` and never calls the backend's restore."""
    called = []
    assert cells.restore(lambda: called.append(1) or {"ok": True})["ok"] is None
    cells._publish(1, pending())
    cells.restore(lambda: {"ok": True, "generation": 2})
    assert cells.restore(lambda: called.append(1) or {"ok": True})["ok"] is None
    assert called == []


def test_o97_a_failed_restore_leaves_an_end_already_published_in_place(cells):
    """O97: an end published first stands (#226): the failure is on the record's own `restore`, not the run's end."""
    record = pending()
    cells._publish(1, record)
    first = {"outcome": deadline.SURVIVED, "steps": []}
    cells.ended = first
    cells.restore(lambda: {"ok": False, "why": "no"})
    assert cells.ended is first
    assert record["restore"] == deadline.RESTORE_FAILED


# ------------------------------------------------------------------------------------------------------------ settle
def test_o97_a_run_whose_body_ends_with_a_restore_pending_needs_none(cells):
    """O97: `settle` (the run's body is over) turns a pending restore into `not_needed`: nothing reads the mount again,
    the run is not ended (the collection proceeds on what the abort left), and a later restore is a no-op."""
    record = pending()
    cells._publish(1, record)
    assert cells.settle() is None
    assert record["restore"] == deadline.RESTORE_NOT_NEEDED
    assert entry_of(cells)["restore"] == deadline.RESTORE_NOT_NEEDED
    assert record["outcome"] == deadline.MOUNT_ABORTED
    assert cells.pending_restore is None
    assert not deadline.ends_run(record)
    assert cells.restore(lambda: {"ok": True})["ok"] is None


def running_in_a_thread(cells, fn):
    """Start `cells.restore(fn)` in a thread, as the code tool's `to_thread` does, once it is under way."""
    thread = threading.Thread(target=lambda: cells.restore(fn), daemon=True)
    thread.start()
    return thread


def test_o97_a_settle_waits_for_a_restore_under_way_and_leaves_its_outcome_to_stand(cells, monkeypatch):
    """O97: the loop cut the call while the restore ran in its thread, and `launch` settles: nothing runs beside a
    restore, so the settle waits for it (at most `RESTORE_WAIT_S`) and then finds `mount_restored`, never marking it
    `not_needed` and never ending the run."""
    record, entered, release = pending(), threading.Event(), threading.Event()
    cells._publish(1, record)

    def restore() -> dict:
        entered.set()
        release.wait(10)
        return {"ok": True, "generation": 2}

    thread = running_in_a_thread(cells, restore)
    assert entered.wait(10)
    threading.Timer(0.2, release.set).start()
    assert cells.settle() is None
    thread.join(10)
    assert (record["outcome"], record["restore"]) == (deadline.MOUNT_RESTORED, deadline.RESTORE_DONE)
    assert cells.ended is None


def test_o97_a_restore_that_outlasts_the_wait_ends_the_run_and_its_late_answer_changes_nothing(cells, monkeypatch):
    """O97: a restore still running after `RESTORE_WAIT_S` of the settle is given up on: the run is ended as the
    restore's failure (`mount_lost`, by `restore`, "not finished before the run's end"), the pending state is cleared,
    and when the restore returns, as a success even, the record and the end stay as they were: a stopped run's
    mount is never made good by a thread that outlived it."""
    monkeypatch.setattr(cells_module, "RESTORE_WAIT_S", 0.2)
    record, entered, release = pending(), threading.Event(), threading.Event()
    cells._publish(1, record)

    def restore() -> dict:
        entered.set()
        release.wait(10)
        return {"ok": True, "generation": 2}

    thread = running_in_a_thread(cells, restore)
    assert entered.wait(10)
    ended = cells.settle()
    assert ended == {"outcome": deadline.MOUNT_LOST, "by": deadline.RESTORE, "cell": 1,
                     "why": "not finished before the run's end"}  # fmt: skip
    assert record["restore"] == deadline.RESTORE_FAILED
    assert entered.is_set()
    assert cells.pending_restore is None
    assert kinds.run_ended_error(ended).kind == "sandbox"
    release.set()
    thread.join(10)
    assert (record["outcome"], record["restore"]) == (deadline.MOUNT_ABORTED, deadline.RESTORE_FAILED)
    assert [s["step"] for s in record["steps"]].count("restored") == 0
    assert cells.ended is ended


def test_o97_a_stop_waits_for_a_restore_under_way_before_it_tears_anything_down(cells, monkeypatch):
    """O97: the sandbox's stop calls `wait_decided` first: it waits for a restore under way, so its teardown never
    runs beside the restore (a token file written after the teardown's token step, an oneclient started after it
    read what to stop), and gives up on one that outlasts `RESTORE_WAIT_S`."""
    record, entered, release = pending(), threading.Event(), threading.Event()
    cells._publish(1, record)

    def restore() -> dict:
        entered.set()
        release.wait(10)
        return {"ok": True, "generation": 2}

    thread = running_in_a_thread(cells, restore)
    assert entered.wait(10)
    threading.Timer(0.2, release.set).start()
    cells.wait_decided()
    assert record["restore"] == deadline.RESTORE_DONE  # it had finished when the stop was let go on
    thread.join(10)
    monkeypatch.setattr(cells_module, "RESTORE_WAIT_S", 0.2)
    again, held, stuck_entered = pending(), threading.Event(), threading.Event()
    cells.ended = cells.pending_restore = None
    cells._publish(2, again)

    def stuck_restore() -> dict:
        stuck_entered.set()
        held.wait(10)
        return {"ok": True}

    stuck = running_in_a_thread(cells, stuck_restore)
    assert stuck_entered.wait(10)
    cells.wait_decided()
    assert again["restore"] == deadline.RESTORE_FAILED
    assert cells.ended["by"] == deadline.RESTORE
    held.set()
    stuck.join(10)


def test_o97_a_settle_with_no_restore_under_way_does_not_wait(cells, monkeypatch):
    """O97: the idle flag is set whenever no restore runs: a settle, a stop's wait and `wait_restored` do not wait."""
    monkeypatch.setattr(cells_module, "RESTORE_WAIT_S", 60.0)
    began = time.monotonic()
    assert cells.settle() is None
    cells.wait_decided()
    assert cells.wait_restored(60.0) is True
    assert time.monotonic() - began < 5


def test_o97_the_idle_flag_is_set_again_whatever_the_restore_did(cells):
    """O97: a restore that fails, raises or succeeds leaves the run's waits free."""
    for answer in ({"ok": True}, {"ok": False}, RuntimeError("x")):
        cells.ended = cells.pending_restore = None
        cells._publish(1, pending())

        def restore(answer=answer) -> dict:
            if isinstance(answer, Exception):
                raise answer
            return answer

        cells.restore(restore)
        assert cells.wait_restored(1.0) is True


# --------------------------------------------------------------------------------------------------------- the sandbox
def test_o97_nothing_execs_while_a_restore_is_pending_and_the_refusal_is_a_sandbox_error(world, mounted):
    """O97: an exec while the mount is aborted and not restored raises `SandboxError` (kind `sandbox`): it fails closed,
    and the code tool never meets it, since it restores before it returns."""
    mounted._cells._publish(1, pending())
    with pytest.raises(SandboxError, match="not been restored"):
        mounted.exec("print(1)", 5)


def test_o97_a_restore_that_reports_failure_ends_the_run_and_with_nothing_pending_it_is_a_no_op(world, mounted):
    """O97: `restore_mount` on a sandbox whose backend's restore reports failure (the Docker backend restores for real,
    `test_docker_restore`; here the body is replaced, and the mount was never aborted) answers `ok: False` and ends the
    run, as every abort did before: the next exec raises the `sandbox` error. With nothing pending it is a no-op."""
    mounted._restore_host_mount = lambda: {"ok": False, "why": "this backend cannot restore its mount"}
    assert mounted.restore_mount()["ok"] is None
    mounted._cells._publish(1, pending())
    assert mounted.restore_mount()["ok"] is False
    with pytest.raises(InfraError) as ended:
        mounted.exec("print(1)", 5)
    assert ended.value.kind == "sandbox"


# ---------------------------------------------------------------- a restore that reaches its lock after the settle
def test_o97_a_restore_that_starts_after_the_settle_finds_nothing_pending_and_never_runs(cells):
    """O97: the loop cut the call as the code tool handed `restore_mount` to its thread, and `launch` settled before
    that thread entered `restore()`: the settle retired the pending restore (`not_needed`), so the late call is a no-op
    that never calls the backend's restore: no token written, no oneclient started beside the teardown."""
    record, called = pending(), []
    cells._publish(1, record)
    assert cells.settle() is None
    assert record["restore"] == deadline.RESTORE_NOT_NEEDED
    answer = cells.restore(lambda: called.append(1) or {"ok": True, "generation": 2})
    assert answer["ok"] is None
    assert called == []
    assert (record["outcome"], record["restore"]) == (deadline.MOUNT_ABORTED, deadline.RESTORE_NOT_NEEDED)
    assert cells.pending_restore is None
    assert cells.wait_restored(1.0) is True


def test_o97_a_restore_that_starts_after_the_stops_wait_finds_nothing_pending_and_never_runs(cells):
    """O97: the same for the sandbox's stop, which calls `wait_decided` first and never settles: it retires a
    restore not begun too, so the teardown it then runs is never beside one."""
    record, called = pending(), []
    cells._publish(1, record)
    cells.wait_decided()
    assert record["restore"] == deadline.RESTORE_NOT_NEEDED
    assert cells.restore(lambda: called.append(1) or {"ok": True})["ok"] is None
    assert called == []


def test_o97_a_restore_already_running_is_waited_for_by_the_settle_and_never_retired(cells):
    """O97: the retirement takes only a restore not begun: one that `restore()` already set `running`, with the idle
    flag clear, is waited for, and its outcome stands."""
    record, entered, release = pending(), threading.Event(), threading.Event()
    cells._publish(1, record)

    def restore() -> dict:
        entered.set()
        release.wait(10)
        return {"ok": True, "generation": 2}

    thread = running_in_a_thread(cells, restore)
    assert entered.wait(10)
    assert cells.pending_restore is record
    threading.Timer(0.2, release.set).start()
    cells.wait_decided()
    thread.join(10)
    assert (record["outcome"], record["restore"]) == (deadline.MOUNT_RESTORED, deadline.RESTORE_DONE)


def test_o97_the_retirement_and_the_start_of_a_restore_are_one_or_the_other_under_the_lock(cells):
    """O97: `restore()` sets `running` and clears the idle flag in one hold of `meta_lock`, as the retirement takes or
    leaves `pending` in one: with the two racing, over many rounds, every restore either ran whole, with the settle
    waiting for it, or never began, and none is ever running when the settle returns."""
    for _ in range(200):
        cells.ended = cells.pending_restore = None
        record, ran, start = pending(), [], threading.Barrier(2)
        cells._publish(1, record)

        def late(ran=ran, start=start) -> None:
            start.wait(5)
            cells.restore(lambda: ran.append(1) or {"ok": True, "generation": 2})

        thread = threading.Thread(target=late, daemon=True)
        thread.start()
        start.wait(5)
        cells.settle()
        assert cells.wait_restored(0) is True  # nothing runs once the settle has returned
        thread.join(10)
        assert (ran == [1]) is (record["restore"] == deadline.RESTORE_DONE)
        assert record["restore"] in (deadline.RESTORE_DONE, deadline.RESTORE_NOT_NEEDED)
