"""O48: a tool outside a campaign holds the federation's shared admission, so no outage can overlap it.

A campaign attempt takes the federation's lock shared and an outage run takes it exclusively. A tool outside a campaign
(a study's smoke driver, a warm-up, `verify`) took none, so it could run beside an outage. `shared_admission` takes the
same shared lock: an outage run waits for it and it waits for an outage run, it is refused while an injection is open or
a journal says the federation is not in its baseline state, and it records who held it, beside the fault journal.
Every test runs against the real lock on the fixture's journal directory.
"""

import asyncio
import json
import math
import multiprocessing
import os
import threading
import time
from datetime import datetime

import pytest
from fault_support import OUTAGE_TARGET, SERVICES, write_faults
from shared_support import hold_and_die, hold_fork_and_unwind

import expcore_onedata.admission as admission_module
from expcore.campaign import liveness
from expcore.errors import InfraError
from expcore_onedata.admission import (
    AdmissionRefused,
    FederationBusy,
    FederationLock,
    current_admission,
    require_admission,
    retain_admission,
)
from expcore_onedata.config import load_platform
from expcore_onedata.fault_journal import InjectionJournal, boot_id, holder
from expcore_onedata.faults import INJECTOR_FAILURE, load_faults
from expcore_onedata.ingesthold import BaselineHold
from expcore_onedata.shared_holds import HOLDS, SharedHolds
from expcore_onedata.testing import write_platform
from expcore_onedata.tool_admission import shared_admission, shared_admission_async

WAIT_S = 10.0  # the bound of every wait of a test that holds the lock between threads
GRACE_S = 0.3  # how long a party that must be held back is given to get in anyway (a secondary check)
PROBE_S = 0.2  # how long the test thread's own try for the lock is given before it counts as refused
REASON = "smoke: warm-up of the first space"


def federation(tmp_path):
    """The platform's configuration, its `[faults]` settings and the federation's lock."""
    platform = write_faults(tmp_path)
    config = load_platform(platform, require_secrets=False)
    settings = load_faults(platform, config)
    return config, settings, FederationLock(settings)


def holds_of(settings):
    return SharedHolds(settings.journal).holds()


def refused(lock, exclusive):
    """Whether a bounded try for the lock is refused: it times out instead of being granted. It takes no admission as
    the running context's, so it works from inside a tool's own `with`."""
    try:
        lock.acquire(exclusive=exclusive, timeout_s=PROBE_S).close()
    except FederationBusy:
        return True
    return False


def in_thread(work):
    """Run `work` in a thread; return the thread and the list its result or exception lands in."""
    landed: list = []

    def run():
        try:
            landed.append(work())
        except BaseException as error:
            landed.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, landed


def open_injection(settings):
    """A journal with an injection no `cleared` line settles, as a run killed with its target down leaves."""
    line = {"event": "injecting", "id": "i1", "target": OUTAGE_TARGET, "service": SERVICES["beta"], "holder": holder()}
    InjectionJournal(settings.journal).append(line)


def baseline_journal(tmp_path, state, **mark):
    path = tmp_path / "ingest.jsonl"
    line = {"entry": "baseline", "space": "spaces[0]", "state": state, "phase": "start", "holder": holder(), **mark}
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return path


# --- the lock: an outage run and a tool exclude each other ----------------------------------------------------------


def test_o48_an_outage_run_cannot_take_the_federation_while_a_tool_holds_it(tmp_path):
    """O48: an exclusive admission is refused for as long as a tool holds the shared one, and granted when it ends."""
    config, _, lock = federation(tmp_path)
    with shared_admission(config, reason=REASON):
        assert refused(lock, exclusive=True)
    outage = lock.acquire(exclusive=True, timeout_s=PROBE_S, target=OUTAGE_TARGET)
    assert outage.exclusive
    outage.close()


def test_o48_a_tool_waits_for_an_outage_run_in_progress(tmp_path):
    """O48: a tool that arrives while an outage run holds the federation exclusively waits for its end, and its hold
    starts after it."""
    config, settings, lock = federation(tmp_path)
    outage = lock.acquire(exclusive=True, timeout_s=PROBE_S, target=OUTAGE_TARGET)
    entered = threading.Event()

    def tool():
        with shared_admission(config, reason=REASON):
            entered.set()

    thread, landed = in_thread(tool)
    time.sleep(GRACE_S)
    assert not entered.is_set()  # held back (a thread slow to start proves nothing: the next assertions do)
    ended = datetime.now().astimezone()
    outage.close()
    thread.join(WAIT_S)
    assert entered.is_set()
    assert landed == [None]
    (hold,) = holds_of(settings)
    assert datetime.fromisoformat(hold["since_utc"]) >= ended  # the hold began once the outage run was over


def test_o48_a_tool_is_refused_within_its_bound_while_an_outage_run_holds_the_federation(tmp_path):
    """O48: with a bound it does not wait for ever: it is refused as a busy federation and records nothing."""
    config, settings, lock = federation(tmp_path)
    outage = lock.acquire(exclusive=True, timeout_s=PROBE_S, target=OUTAGE_TARGET)
    try:
        with pytest.raises(FederationBusy), shared_admission(config, reason=REASON, timeout_s=PROBE_S):
            raise AssertionError("a tool was admitted beside an outage run")
        assert holds_of(settings) == []
    finally:
        outage.close()


def test_o48_two_tools_hold_the_federation_together(tmp_path):
    """O48: a tool's hold is shared, as a campaign attempt's is: it excludes an outage run, not another tool."""
    config, settings, _lock = federation(tmp_path)
    entered, leave = threading.Event(), threading.Event()

    def other_tool():
        with shared_admission(config, reason="another tool"):
            entered.set()
            leave.wait(WAIT_S)

    thread, landed = in_thread(other_tool)
    try:
        assert entered.wait(WAIT_S)
        with shared_admission(config, reason=REASON, timeout_s=PROBE_S):
            assert {hold["reason"] for hold in SharedHolds(settings.journal).open_holds()} == {"another tool", REASON}
    finally:
        leave.set()
        thread.join(WAIT_S)
    assert landed == [None]


def test_o48_the_tools_admission_admits_the_sandbox_and_the_tool_server(tmp_path):
    """O48: under the hold the running context holds the federation, so a launch passes `require_admission`."""
    config, _, _ = federation(tmp_path)
    with pytest.raises(AdmissionRefused):
        require_admission(config)
    with shared_admission(config, reason=REASON) as admission:
        assert current_admission() is admission
        assert not admission.exclusive
        require_admission(config)
    assert current_admission() is None


def test_o48_a_tool_inside_a_shared_admission_reuses_it_and_records_nothing_more(tmp_path):
    """O48: a warm-up called by a smoke driver that holds the federation already does not take it twice."""
    config, settings, _ = federation(tmp_path)
    with shared_admission(config, reason=REASON) as outer, shared_admission(config, reason="a warm-up") as inner:
        assert inner is outer
    assert [hold["reason"] for hold in holds_of(settings)] == [REASON]


def test_o48_a_tool_inside_an_outage_runs_exclusive_admission_is_refused(tmp_path):
    """O48: a context that holds the federation exclusively takes no shared hold: that would be a second, no upgrade."""
    config, settings, lock = federation(tmp_path)
    with (
        lock.held(exclusive=True, timeout_s=PROBE_S, target=OUTAGE_TARGET),
        pytest.raises(AdmissionRefused),
        shared_admission(config, reason=REASON),
    ):
        raise AssertionError("a tool was admitted inside an exclusive admission")
    assert holds_of(settings) == []


# --- refusals: an injection open, a journal that is not whole ------------------------------------------------------


def test_o48_a_tool_is_refused_while_an_injection_is_open_and_names_it(tmp_path):
    """O48: an injection no `cleared` line settles means a provider may be down: the tool is refused as
    injector_failure, naming the target by its label, and holds nothing."""
    config, settings, lock = federation(tmp_path)
    open_injection(settings)
    with pytest.raises(InfraError, match="unsettled") as caught, shared_admission(config, reason=REASON):
        raise AssertionError("a tool was admitted onto a federation with a provider down")
    assert caught.value.kind == INJECTOR_FAILURE
    assert OUTAGE_TARGET in caught.value.detail
    assert holds_of(settings) == []
    assert not refused(lock, exclusive=True)  # the refused tool let the lock go


def test_o48_a_tool_is_refused_when_the_fault_journal_cannot_be_read(tmp_path):
    """O48: a journal that cannot be read says nothing about the providers, so the tool is refused, not admitted."""
    config, settings, _ = federation(tmp_path)
    settings.journal.mkdir(parents=True, exist_ok=True)
    (settings.journal / "injections.jsonl").write_text("this is not an event\n", encoding="utf-8")
    with pytest.raises(InfraError, match="cannot be read"), shared_admission(config, reason=REASON):
        raise AssertionError("a tool was admitted with an unreadable journal")


def test_o48_a_tool_is_refused_when_its_own_record_cannot_be_read(tmp_path):
    """O48: the record of the holds is part of what the tool relies on: a complete line that is not a hold refuses."""
    config, settings, _ = federation(tmp_path)
    settings.journal.mkdir(parents=True, exist_ok=True)
    (settings.journal / HOLDS).write_text('{"event": "something else"}\n', encoding="utf-8")
    with (
        pytest.raises(InfraError, match="record of the tools' holds cannot be read"),
        shared_admission(config, reason=REASON),
    ):
        raise AssertionError("a tool was admitted with an unreadable record")


def test_o48_a_tool_is_refused_for_an_ingest_baseline_that_is_not_whole(tmp_path):
    """O48 (O44, O45): an ingest journal whose baseline is unhealthy refuses the tool, naming the space by its position;
    a whole one admits it."""
    config, settings, _ = federation(tmp_path)
    broken = baseline_journal(tmp_path, "unhealthy", file="day-13", step="prefix")
    with (
        pytest.raises(InfraError, match=r"spaces\[0\]: the progressive ingest refuses runs") as caught,
        shared_admission(config, reason=REASON, ingest_journals=[broken]),
    ):
        raise AssertionError("a tool was admitted onto a baseline that is not whole")
    assert caught.value.kind == INJECTOR_FAILURE
    assert holds_of(settings) == []
    whole = baseline_journal(tmp_path, "whole")
    with shared_admission(config, reason=REASON, ingest_journals=[whole]) as admission:
        assert admission is not None


def test_o48_a_tool_is_refused_while_a_live_run_holds_a_baseline_open(tmp_path):
    """O48: a baseline held open by a live run is being reset: a tool that reads it would read it in flux, so it is
    refused (the campaign's own check defers there)."""
    config, _settings, _ = federation(tmp_path)
    journal = baseline_journal(tmp_path, "open", phase="run", owner=liveness.owner())
    hold = BaselineHold(journal, "spaces[0]")
    assert hold.take(wait_s=0)
    try:
        with (
            pytest.raises(InfraError, match="holds runs"),
            shared_admission(config, reason=REASON, ingest_journals=[journal]),
        ):
            raise AssertionError("a tool was admitted onto a baseline a run holds open")
    finally:
        hold.release()


def test_o48_a_platform_without_faults_has_no_federation_to_hold(tmp_path):
    """O48: with no `[faults]` there is no injector and no lock: the tool is admitted with no admission, as a campaign's
    run admission is none; its ingest journals still refuse it."""
    config = load_platform(write_platform(tmp_path), require_secrets=False)
    with shared_admission(config, reason=REASON) as admission:
        assert admission is None
    broken = baseline_journal(tmp_path, "unhealthy")
    with (
        pytest.raises(InfraError, match="refuses runs"),
        shared_admission(config, reason=REASON, ingest_journals=[broken]),
    ):
        raise AssertionError("a tool was admitted onto a baseline that is not whole")


@pytest.mark.parametrize("reason", ["", "   ", "x" * 201, "two\nlines", 5, None])
def test_o48_a_reason_that_is_not_one_short_line_of_text_is_refused_before_anything_is_taken(tmp_path, reason):
    """O48: the reason goes into a journal that people read: one printable line of 1 to 200 characters."""
    config, settings, lock = federation(tmp_path)
    with pytest.raises(ValueError, match="reason"), shared_admission(config, reason=reason):
        raise AssertionError("a tool was admitted with no reason")
    assert holds_of(settings) == []
    assert not refused(lock, exclusive=True)


@pytest.mark.parametrize("timeout_s", [-1, math.nan, math.inf, True])
def test_o48_a_bound_that_is_not_a_finite_number_of_seconds_is_refused(tmp_path, timeout_s):
    """O48: `timeout_s` is a number of seconds at or above 0, never unbounded."""
    config, _, _ = federation(tmp_path)
    with pytest.raises(ValueError, match="timeout_s"), shared_admission(config, reason=REASON, timeout_s=timeout_s):
        raise AssertionError("a tool was admitted with no bound")


# --- the record ----------------------------------------------------------------------------------------------------


def test_o48_the_hold_is_recorded_with_its_reason_its_process_and_its_times(tmp_path):
    """O48: beside the fault journal, a line says who holds the federation: the reason, the process (boot id, pid and
    start time, as the lease pool names a holder) and the start; the end is added when the hold ends."""
    config, settings, _ = federation(tmp_path)
    with shared_admission(config, reason=REASON):
        (hold,) = SharedHolds(settings.journal).open_holds()
        boot, pid, started = hold["owner"]["process"].split(":")
        assert (boot, pid) == (boot_id(), str(os.getpid()))
        assert started.isdigit()
        assert hold["owner"]["process"] == liveness.this_process()
        assert hold["holder"] == {"pid": os.getpid(), "boot_id": boot_id()}
        assert hold["reason"] == REASON
        assert hold["live"] is True
        assert hold["until_utc"] is None
        assert datetime.fromisoformat(hold["since_utc"])
    (hold,) = holds_of(settings)
    assert datetime.fromisoformat(hold["until_utc"]) >= datetime.fromisoformat(hold["since_utc"])
    assert hold["how"] == "released"
    lines = [json.loads(text) for text in (settings.journal / HOLDS).read_text(encoding="utf-8").splitlines()]
    assert [line["event"] for line in lines] == ["shared_held", "shared_released"]


def test_o48_the_hold_is_released_when_the_body_raises(tmp_path):
    """O48: an exception in the tool lets the lock go, ends the record and reaches the caller unchanged."""
    config, settings, lock = federation(tmp_path)

    class SmokeFailed(Exception):
        pass

    with pytest.raises(SmokeFailed), shared_admission(config, reason=REASON):
        raise SmokeFailed
    assert current_admission() is None
    assert not refused(lock, exclusive=True)
    (hold,) = holds_of(settings)
    assert hold["until_utc"] is not None


def test_o48_a_worker_that_outlives_the_tool_keeps_the_hold_and_the_record_open(tmp_path):
    """O48: a worker handed a share (`retain_admission`) keeps the federation held after the tool's body ends, and the
    record ends with the lock, not before it."""
    config, settings, lock = federation(tmp_path)
    with shared_admission(config, reason=REASON):
        give_up = retain_admission()
    assert refused(lock, exclusive=True)
    (hold,) = holds_of(settings)
    assert hold["until_utc"] is None
    give_up()
    assert not refused(lock, exclusive=True)
    (hold,) = holds_of(settings)
    assert hold["until_utc"] is not None


def test_o48_a_torn_last_line_of_the_record_is_ignored_and_cut_by_the_next_hold(tmp_path):
    """O48: a crash in the middle of an append leaves a line without its newline: it was never acted on."""
    config, settings, _ = federation(tmp_path)
    with shared_admission(config, reason=REASON):
        pass
    with open(settings.journal / HOLDS, "a", encoding="utf-8") as handle:
        handle.write('{"event": "shared_held", "id": "torn')
    assert [hold["reason"] for hold in holds_of(settings)] == [REASON]
    with shared_admission(config, reason="after the tear"):
        pass
    assert [hold["reason"] for hold in holds_of(settings)] == [REASON, "after the tear"]


def test_o48_an_end_that_names_no_time_makes_the_record_unreadable(tmp_path):
    """O48: an end that does not say when and how the hold ended is no end: the record is refused, not read as open."""
    _, settings, _ = federation(tmp_path)
    records = SharedHolds(settings.journal)
    token = records.begin(REASON)
    InjectionJournal(settings.journal).append_to(HOLDS, {"event": "shared_released", "id": token})
    with pytest.raises(ValueError, match="not a hold event"):
        records.holds()


# --- a dead tool's hold is released ---------------------------------------------------------------------------------


def killed_tool(tmp_path):
    """A tool process that took the hold and died inside it: its lock went with it, its record has no end."""
    platform = write_faults(tmp_path)
    child = multiprocessing.get_context("spawn").Process(target=hold_and_die, args=(str(platform), "a killed tool"))
    child.start()
    child.join(120)
    assert child.exitcode == 0
    config = load_platform(platform, require_secrets=False)
    return config, load_faults(platform, config)


def test_o48_a_dead_tools_hold_reads_as_gone_and_never_blocks_an_outage_run(tmp_path):
    """O48: the kernel drops the dead tool's lock, so an outage run is admitted at once; the record says its process
    is gone, and the exclusive admission, which proves no tool holds the federation, ends it as `holder_gone`."""
    _config, settings = killed_tool(tmp_path)
    (hold,) = SharedHolds(settings.journal).open_holds()
    assert hold["reason"] == "a killed tool"
    assert hold["live"] is False
    outage = FederationLock(settings).try_acquire(exclusive=True, target=OUTAGE_TARGET)
    assert outage is not None
    outage.close()
    (hold,) = holds_of(settings)
    assert hold["how"] == "holder_gone"
    assert hold["until_utc"] is not None


def test_o48_the_next_tool_ends_a_dead_tools_record_without_an_exclusive_admission(tmp_path):
    """O48: a record whose process is gone is ended by the next hold, though no outage run has come since."""
    config, settings = killed_tool(tmp_path)
    with shared_admission(config, reason=REASON):
        (dead, live) = holds_of(settings)
        assert (dead["reason"], dead["how"]) == ("a killed tool", "holder_gone")
        assert live["reason"] == REASON
        assert live["until_utc"] is None


def test_o48_an_exclusive_admission_ends_every_record_the_lock_proves_unheld(tmp_path):
    """O48: whoever holds the federation exclusively knows that no tool holds it shared, whatever a record says of its
    process: a record that names a live process is ended as gone, as the shared admission clears an exclusive record."""
    _config, settings, lock = federation(tmp_path)
    SharedHolds(settings.journal).begin(REASON)  # a record this live process never ends: its lock was never taken
    (hold,) = SharedHolds(settings.journal).open_holds()
    assert hold["live"] is True
    lock.acquire(exclusive=True, timeout_s=PROBE_S, target=OUTAGE_TARGET).close()
    (hold,) = holds_of(settings)
    assert hold["how"] == "holder_gone"


def test_o48_an_outage_run_that_waits_in_vain_names_the_tools_that_hold_the_federation(tmp_path):
    """O48: the wait of an exclusive admission that runs out says that tools hold the federation, and since when; the
    reason is the tool's own text and stays in the record (O12)."""
    config, _settings, lock = federation(tmp_path)
    with (
        shared_admission(config, reason="a reason that names the first space"),
        pytest.raises(FederationBusy) as caught,
    ):
        lock.acquire(exclusive=True, timeout_s=PROBE_S, target=OUTAGE_TARGET)
    assert "1 tool outside a campaign holds it shared since " in caught.value.detail
    assert "first space" not in caught.value.detail


def test_o48_a_task_that_reuses_the_hold_keeps_the_federation_held_when_the_owner_leaves(tmp_path):
    """O48: a task inherits its parent's context, and so its admission: when it enters the hold it takes a share, so the
    parent's exit, or its cancellation, does not let an outage run in while the task is still inside its own body."""
    config, settings, lock = federation(tmp_path)

    async def drive():
        inside, leave = asyncio.Event(), asyncio.Event()

        async def child():
            async with shared_admission_async(config, reason="the child") as admission:
                assert admission is not None
                inside.set()
                await leave.wait()

        async with shared_admission_async(config, reason=REASON):
            task = asyncio.ensure_future(child())
            await inside.wait()
        assert await asyncio.to_thread(refused, lock, True)  # the owner has left, the child has not
        assert [hold["until_utc"] for hold in holds_of(settings)] == [None]
        leave.set()
        await task
        assert not await asyncio.to_thread(refused, lock, True)

    asyncio.run(drive())
    (hold,) = holds_of(settings)
    assert hold["until_utc"] is not None


def test_o48_an_interrupt_while_a_record_ends_still_lets_the_lock_go(tmp_path):
    """O48: an ending that is interrupted (a KeyboardInterrupt is no Exception) must not strand the lock: it goes in a
    `finally`, or an outage run would wait for a share nobody can give up again."""
    _, _, lock = federation(tmp_path)
    admission = lock.acquire(exclusive=False, timeout_s=PROBE_S)

    def interrupted():
        raise KeyboardInterrupt

    admission.endings.append(interrupted)
    with pytest.raises(KeyboardInterrupt):
        admission.close()
    assert not refused(lock, exclusive=True)


class SlowThreading:
    """The `threading` module as the admission sees it, with an Event whose `is_set` lingers after it has read: a second
    caller of a once-only call then arrives between its check and its set, where a race would be."""

    class Event(threading.Event):
        def is_set(self):
            seen = super().is_set()
            time.sleep(0.1)
            return seen

    def __getattr__(self, name):
        return getattr(threading, name)


def test_o48_a_share_given_up_by_two_threads_at_once_counts_once(tmp_path, monkeypatch):
    """O48: the call that gives a retained share up counts only its first call, whoever makes it: two threads that call
    it together must not take two shares off the admission, or the owner's own would go and the lock with it."""
    _, _, lock = federation(tmp_path)
    monkeypatch.setattr(admission_module, "threading", SlowThreading())
    admission = lock.acquire(exclusive=False, timeout_s=PROBE_S)
    give_up = admission.retain()
    barrier = threading.Barrier(2)

    def call():
        barrier.wait()
        give_up()

    callers = [threading.Thread(target=call) for _ in range(2)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(WAIT_S)
    assert admission.held, "the owner's share went with a share given up twice"
    assert refused(lock, exclusive=True)  # the owner still holds it
    admission.close()
    assert not refused(lock, exclusive=True)


def test_o48_a_forked_child_that_gives_up_its_copy_of_the_hold_ends_nothing_of_its_parents(tmp_path):
    """O48: the fork hook closes the lock descriptors a child inherits, so its copy of the admission holds nothing; it
    must not end the parent's record either, or the journal would say the parent let go while it still holds it."""
    platform = write_faults(tmp_path)
    result = tmp_path / "result.json"
    child = multiprocessing.get_context("spawn").Process(target=hold_fork_and_unwind, args=(str(platform), str(result)))
    child.start()
    child.join(120)
    assert child.exitcode == 0
    assert json.loads(result.read_text(encoding="utf-8")) == {"refused": True, "open": ["a tool that forks"]}


# --- the async form ------------------------------------------------------------------------------------------------


def test_o48_the_async_form_holds_the_lock_and_records_the_hold(tmp_path):
    """O48: a driver that runs in an event loop takes the hold without blocking the loop, and gives it up on exit."""
    config, settings, lock = federation(tmp_path)

    async def drive():
        async with shared_admission_async(config, reason=REASON) as admission:
            assert current_admission() is admission
            assert await asyncio.to_thread(refused, lock, True)
        return current_admission()

    assert asyncio.run(drive()) is None
    assert not refused(lock, exclusive=True)
    (hold,) = holds_of(settings)
    assert hold["reason"] == REASON
    assert hold["until_utc"] is not None


def test_o48_the_async_form_is_refused_while_an_injection_is_open(tmp_path):
    """O48: the refusals are the sync form's."""
    config, settings, lock = federation(tmp_path)
    open_injection(settings)

    async def drive():
        async with shared_admission_async(config, reason=REASON):
            raise AssertionError("a tool was admitted onto a federation with a provider down")

    with pytest.raises(InfraError, match="unsettled"):
        asyncio.run(drive())
    assert holds_of(settings) == []
    assert not refused(lock, exclusive=True)
