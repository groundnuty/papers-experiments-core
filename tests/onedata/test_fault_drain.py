"""O11: after an outage run, the federation's exclusive admission stays closed until the run's sessions have drained.

An agent's read of a file only the stopped provider holds, through the local provider, leaves an on-demand sync job
there that nothing calls back; a later read of the same file and range joins it and hangs. The job ends only when the
run's callers end: a REST read when its request process dies (the gate's stop), a mount when oneclient's last
connection closes plus a grace (300 s, op-worker's `fuse_session_grace_period_seconds`, and about 15 s more). So the
admission of an outage run is let go at least `[faults] session_drain_s` (320 s) after the run's mount disconnected and
its REST gate stopped, whichever is later; the wait is the remainder after whatever the restore took, and times that
are missing mean the whole drain from the restore's start. Every test runs on a fake clock against `FakeServices`.
"""

import fcntl
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time

import pytest
from fault_support import (
    OUTAGE_TARGET,
    FakeClock,
    FakeServices,
    faults_table,
    inject_and_die,
    injector_for,
    write_faults,
)

from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore_onedata.admission import (
    _STRANDED,
    FEDERATION_LOCK,
    OUTAGE_RUN,
    FederationBusy,
    FederationLock,
    run_admission,
)
from expcore_onedata.config import load_platform
from expcore_onedata.fault_drain import GATE, MOUNT, SessionDrain
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.fault_journal import DRAIN, HOLDER, JOURNAL, InjectionJournal, boot_id
from expcore_onedata.faults import load_faults
from expcore_onedata.testing import write_platform

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
DRAIN_S = 320.0


def settings_of(platform):
    return load_faults(platform, load_platform(platform, require_secrets=False))


def step(injector, name):
    found = [s for s in injector.snapshot()["steps"] if s["step"] == name]
    return found[-1] if found else None


def open_injections(tmp_path):
    path = tmp_path / "fault-journal" / JOURNAL
    lines = [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    cleared = {line["id"] for line in lines if line["event"] == "cleared"}
    return [line for line in lines if line["event"] == "injecting" and line["id"] not in cleared]


def drained(drain_s=DRAIN_S, **kw):
    """A drain on a fake clock whose sleeps are recorded and move the clock."""
    clock = FakeClock()
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        clock.sleep(seconds)

    return SessionDrain(drain_s, clock=clock, sleep=sleep, **kw), clock, slept


# --- the setting -----------------------------------------------------------------------------------------------------


def test_o11_session_drain_s_is_a_number_of_at_least_zero_and_zero_declares_no_drain(tmp_path):
    """O11: `[faults] session_drain_s` is a finite number of seconds, 0 or more; 0 declares no drain, and a negative or
    non-numeric value is refused naming the key."""
    for name, value in (("b", 320), ("c", 0), ("d", 12.5)):
        (tmp_path / name).mkdir()
        assert settings_of(write_faults(tmp_path / name, session_drain_s=value)).session_drain_s == value
    for name, value in (("e", -1), ("f", "320"), ("g", True), ("h", float("inf"))):
        (tmp_path / name).mkdir()
        with pytest.raises(ConfigError, match="session_drain_s"):
            settings_of(write_faults(tmp_path / name, session_drain_s=value))


def test_o11_a_platform_that_declares_no_session_drain_waits_the_full_default(tmp_path):
    """O11: a `[faults]` table without the key drains 320 s, op-worker's 300 s grace for a mount's sessions, its 15 s
    removal retry and a margin; never nothing."""

    def edit(data):
        table = faults_table()
        del table["session_drain_s"]
        data["faults"] = table

    assert settings_of(write_platform(tmp_path, edit)).session_drain_s == 320.0


# --- the drain itself ------------------------------------------------------------------------------------------


def test_o11_a_drain_nothing_armed_waits_for_nothing():
    """O11: a run whose provider was never taken down leaves no job to drain: the wait is zero, with no sleep."""
    drain, clock, slept = drained()
    drain.begin(MOUNT).ended(MOUNT, at=clock.now)
    record = drain.wait()
    assert (slept, record["waited_s"], drain.armed) == ([], 0.0, False)


def test_o11_the_drain_runs_from_the_later_of_the_mounts_end_and_the_gates_stop():
    """O11: the mount ended at 100 and the gate stopped at 140: the admission opens at 460, 320 s after the later."""
    drain, clock, _ = drained()
    ticket = drain.begin(MOUNT, GATE)
    drain.arm(restore_started=90.0)
    ticket.ended(MOUNT, at=100.0)
    ticket.ended(GATE, at=140.0)
    clock.now = 150.0
    record = drain.wait()
    assert (record["basis"], record["mount_end"], record["gate_stop"], record["until"]) == (
        "session_end",
        100.0,
        140.0,
        140.0 + DRAIN_S,
    )
    assert clock.now == 140.0 + DRAIN_S
    assert record["waited_s"] == pytest.approx(DRAIN_S - 10.0)


def test_o11_the_drain_waits_only_the_remainder_after_the_restore_and_the_heals_settle_took_their_time():
    """O11: the sessions ended at 100; the restore, its heal's 180 s settle included, ran until 330: 90 s are left of
    the 320, not 320 again."""
    drain, clock, slept = drained()
    drain.begin(GATE).ended(GATE, at=100.0)
    drain.arm(restore_started=105.0)
    clock.now = 330.0  # the restore ended here
    record = drain.wait()
    assert record["waited_s"] == pytest.approx(90.0)
    assert clock.now == 420.0
    assert sum(slept) == pytest.approx(90.0)


def test_o11_a_drain_already_over_waits_for_nothing():
    """O11: when the restore itself outlasted the drain nothing more is waited, and the record says so."""
    drain, clock, slept = drained()
    drain.begin(GATE).ended(GATE, at=100.0)
    drain.arm(restore_started=105.0)
    clock.now = 500.0
    record = drain.wait()
    assert (slept, record["waited_s"], record["basis"]) == ([], 0.0, "session_end")


def test_o11_times_that_are_missing_mean_the_full_drain_from_the_restores_start_never_zero():
    """O11: no sandbox reported its sessions, so their end is unknown: the drain runs the whole 320 s from the start of
    the restore, and the record says why."""
    drain, clock, _ = drained()
    drain.arm(restore_started=100.0)
    clock.now = 130.0
    record = drain.wait()
    assert (record["basis"], record["until"], record["restore_started"]) == ("restore_start", 420.0, 100.0)
    assert clock.now == 420.0
    assert "no sandbox" in record["why"]


def test_o11_a_mount_that_never_reported_its_end_means_the_full_drain_even_with_the_gates_stop_known():
    """O11: a sandbox that began a mount and a gate but only recorded the gate's stop (its teardown was cut, say) is
    unknown: the drain is the later of the full wait from the restore's start and the known end plus the drain."""
    drain, clock, _ = drained()
    ticket = drain.begin(MOUNT, GATE)
    ticket.ended(GATE, at=200.0)
    drain.arm(restore_started=250.0)
    clock.now = 260.0
    record = drain.wait()
    assert (record["basis"], record["until"]) == ("restore_start", 250.0 + DRAIN_S)
    assert record["mount_end"] is None
    assert record["gate_stop"] == 200.0
    assert "mount" in record["why"]


def test_o11_a_known_end_after_the_restore_start_still_counts_when_another_is_missing():
    """O11: never earlier than the known end plus the drain."""
    drain, clock, _ = drained()
    ticket = drain.begin(MOUNT, GATE)
    ticket.ended(GATE, at=400.0)
    drain.arm(restore_started=250.0)
    clock.now = 410.0
    assert drain.wait()["until"] == 400.0 + DRAIN_S


def test_o11_a_sandbox_with_no_mount_and_no_gate_leaves_no_session_to_drain():
    """O11: a run whose data path is none reads through neither: nothing to drain, and the record says so."""
    drain, _, slept = drained()
    drain.begin()
    drain.arm(restore_started=100.0)
    record = drain.wait()
    assert (slept, record["basis"], record["until"]) == ([], "no_session", None)


def test_o11_a_drain_of_zero_seconds_is_no_drain():
    """O11: `session_drain_s = 0` is the declared way to ask for none, and the record says it was declared."""
    drain, _, slept = drained(0.0)
    drain.arm(restore_started=100.0)
    record = drain.wait()
    assert (slept, record["basis"]) == ([], "off")


def test_o11_a_sleep_that_comes_back_early_is_slept_again_until_the_deadline():
    """O11: a sleep interrupted short of its time does not end the drain: the wait goes on, as often as it takes, until
    the clock has reached the deadline."""
    clock = FakeClock()
    slept: list[float] = []

    def early(seconds):
        slept.append(seconds)
        clock.sleep(min(seconds, 100.0))  # each sleep comes back after 100 s at most

    drain = SessionDrain(DRAIN_S, clock=clock, sleep=early)
    drain.begin(GATE).ended(GATE, at=clock.now)
    ended = clock.now
    drain.arm(restore_started=ended)
    record = drain.wait()
    assert clock.now == ended + DRAIN_S
    assert len(slept) == 4  # 320 s in sleeps of at most 100 s
    assert record["waited_s"] == pytest.approx(DRAIN_S)


def test_o11_a_clock_that_does_not_move_across_a_sleep_cannot_hang_the_wait():
    """O11: nothing can wait out a deadline on a clock that stands still, as a fake whose sleep does nothing has: the
    wait ends rather than spin for ever."""
    clock = FakeClock()
    drain = SessionDrain(DRAIN_S, clock=clock, sleep=lambda seconds: None)
    drain.arm(restore_started=clock.now)
    record = drain.wait()
    assert record["waited_s"] == 0.0


def test_o11_an_end_is_noted_once_and_the_latest_of_two_sandboxes_counts():
    """O11: a ticket's end counts once; with two sandboxes of one run the later end of a kind is the one."""
    drain, _, _ = drained()
    first, second = drain.begin(MOUNT), drain.begin(MOUNT)
    first.ended(MOUNT, at=100.0)
    first.ended(MOUNT, at=900.0)  # a repeat of the same ticket is ignored
    drain.arm(restore_started=100.0)
    assert drain.plan().basis == "restore_start"  # the second sandbox's mount has not ended
    second.ended(MOUNT, at=150.0)
    assert drain.plan().until == 150.0 + DRAIN_S


# --- sessions owed to an earlier run ----------------------------------------------------------------------------


def test_o11_what_a_recovery_owes_is_never_removed_by_the_current_runs_sandbox_having_no_sessions():
    """O11: a recovery restored a provider an earlier run left down, so that run's sessions are unknown: a sandbox of
    the current run with no data path, which has none of its own, does not make the drain over."""
    drain, _, _ = drained()
    drain.begin()  # the current run's sandbox: no mount and no gate
    drain.owe(100.0)
    plan = drain.plan()
    assert (plan.basis, plan.until) == ("restore_start", 100.0 + DRAIN_S)
    drain.begin(GATE).ended(GATE, at=300.0)  # a later sandbox of this run ended later: that end counts too
    assert drain.plan().until == 300.0 + DRAIN_S


def test_o11_a_debt_counts_from_the_recoverys_start_not_from_when_the_plan_is_made():
    """O11: the debt of a recovery that began at 100 is over at 420, whenever it is looked at."""
    drain, clock, _ = drained()
    drain.owe(100.0)
    clock.now = 250.0
    assert drain.plan().until == 100.0 + DRAIN_S
    assert drain.wait()["until"] == 100.0 + DRAIN_S
    assert clock.now == 100.0 + DRAIN_S


# --- the admission ---------------------------------------------------------------------------------------------


def forbidden(seconds):
    raise AssertionError("an admission slept where it must let go at once")


def fake_federation(tmp_path, **changes):
    """A platform declaring the drain, its settings, a fake clock, and a lock whose drains run on that clock (and whose
    wall clock is that clock), whose releases must never sleep."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S, **changes), FakeClock()
    settings = settings_of(platform)
    return platform, settings, clock, FederationLock(settings, clock=clock, sleep=forbidden, wall=clock)


def test_o11_an_armed_exclusive_admission_is_let_go_into_a_federation_closed_until_its_drain_has_ended(tmp_path):
    """O11: letting an armed admission go returns at once and closes the federation to every admission, shared and
    exclusive, until the drain's end, on the federation's own record, and opens it then."""
    _, _, clock, lock = fake_federation(tmp_path)
    admission = lock.acquire(exclusive=True, purpose=OUTAGE_RUN, target=OUTAGE_TARGET)
    ended = clock.now
    admission.drain.begin(GATE).ended(GATE, at=ended)
    admission.drain.arm(restore_started=ended)
    admission.close()  # the sleep is forbidden: the record is what keeps the federation closed
    for exclusive in (False, True):
        assert lock.try_acquire(exclusive=exclusive) is None
    assert lock.drain_left() == pytest.approx(DRAIN_S)
    clock.now = ended + DRAIN_S - 1
    assert lock.try_acquire(exclusive=False) is None
    assert lock.drain_left() == pytest.approx(1.0)
    clock.now = ended + DRAIN_S
    for exclusive in (False, True):
        taken = lock.try_acquire(exclusive=exclusive)
        assert taken is not None
        taken.close()


def test_o11_a_wait_for_the_federation_waits_out_the_drain_inside_its_bound(tmp_path):
    """O11: `acquire` holds the lock it took and waits for the record to pass, polling, within the bound it was given:
    a bound that ends first is `FederationBusy`, and the lock it took is let go."""
    platform = write_faults(tmp_path, session_drain_s=3.0)
    lock = FederationLock(settings_of(platform))
    first = lock.acquire(exclusive=True, purpose=OUTAGE_RUN, target=OUTAGE_TARGET)
    armed = time.monotonic()
    first.drain.arm(restore_started=armed)  # no sandbox reported: the full 3 s from the restore's start
    first.close()
    with pytest.raises(FederationBusy):
        lock.acquire(exclusive=False, timeout_s=0.2)
    shared = lock.acquire(exclusive=False, timeout_s=30)
    assert time.monotonic() - armed >= 2.9  # not before the drain's end, whatever the host's load made of the rest
    shared.close()
    assert lock.try_acquire(exclusive=True) is not None  # nothing was left held by the refused wait


def test_o11_a_cancelled_wait_for_the_drain_lets_the_lock_go(tmp_path):
    """O11: a wait for the drain that is cancelled gives up the lock it held."""
    platform = write_faults(tmp_path, session_drain_s=30)
    settings = settings_of(platform)
    lock = FederationLock(settings)
    lock.hold_for(30)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InfraError, match="cancelled"):
        lock.acquire(exclusive=True, cancel=cancel)
    descriptor = os.open(settings.journal / FEDERATION_LOCK, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises BlockingIOError while the lock is still held
    finally:
        os.close(descriptor)


def test_o11_an_admission_nothing_armed_writes_no_record_and_opens_the_federation_at_once(tmp_path):
    """O11: a shared admission, an exclusive run with no fault and an outage run whose provider never went down write
    nothing and are let go without a sleep, whatever `session_drain_s` says."""
    _, settings, _, lock = fake_federation(tmp_path)
    for exclusive in (False, True):
        lock.acquire(exclusive=exclusive).close()
    assert not (settings.journal / DRAIN).exists()
    assert lock.drain_left() == 0.0
    taken = lock.try_acquire(exclusive=True)
    assert taken is not None
    taken.close()


def test_o11_a_run_that_is_not_an_outage_run_is_not_delayed_by_the_run_admission(tmp_path):
    """O11: the platform's run admission lets a baseline run and an exclusive control go the moment their attempt ends,
    with the default drain of 320 s declared, and the next is admitted at once."""
    platform = write_faults(tmp_path, session_drain_s=DRAIN_S)
    admit = run_admission(load_platform(platform, require_secrets=False))
    started = time.monotonic()
    for kinds, exclusive in (((), False), ((), True), ((), False)):
        with admit(kinds, {}, threading.Event(), exclusive) as held:
            assert held == ("exclusive" if exclusive else "shared")
    assert time.monotonic() - started < 10


def test_o11_the_drain_record_only_moves_later(tmp_path):
    """O11: a writer can raise the time before which nobody is admitted, never lower it."""
    journal = InjectionJournal(settings_of(write_faults(tmp_path)).journal)
    assert journal.read_drain() is None
    journal.raise_drain(500.0)
    journal.raise_drain(400.0)
    assert journal.read_drain() == 500.0
    journal.raise_drain(600.0)
    assert journal.read_drain() == 600.0


@pytest.mark.parametrize("text", ["not json", "[]", '{"until": "soon"}', '{"until": true}', '{"until": NaN}'])
def test_o11_a_drain_record_that_cannot_be_read_refuses_every_admission_and_holds_no_lock(tmp_path, text):
    """O11: an admission that cannot tell the federation is open does not grant it, in either form, and lets go of the
    lock it took while it looked."""
    platform = write_faults(tmp_path)
    settings = settings_of(platform)
    lock = FederationLock(settings)
    settings.journal.mkdir(parents=True, exist_ok=True)
    (settings.journal / DRAIN).write_text(text, encoding="utf-8")
    with pytest.raises(InfraError, match="drain record"):
        lock.try_acquire(exclusive=False)
    with pytest.raises(InfraError, match="drain record"):
        lock.acquire(exclusive=True, timeout_s=1)
    (settings.journal / DRAIN).unlink()
    taken = lock.try_acquire(exclusive=True)  # nothing was left held
    assert taken is not None
    taken.close()


def test_o11_a_drain_that_cannot_be_recorded_is_waited_out_before_the_lock_is_let_go(tmp_path, monkeypatch):
    """O11: a record that cannot be written is no reason to let the federation go inside the drain: the release waits
    in process, the lock held, for the whole of it."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S), FakeClock()
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        clock.sleep(seconds)

    def broken(self, until):
        raise OSError("the disk is full")

    monkeypatch.setattr(InjectionJournal, "raise_drain", broken)
    lock = FederationLock(settings_of(platform), clock=clock, sleep=sleep, wall=clock)
    admission = lock.acquire(exclusive=True, purpose=OUTAGE_RUN, target=OUTAGE_TARGET)
    admission.drain.arm(restore_started=clock.now)
    started = clock.now
    admission.close()
    assert slept == [DRAIN_S]
    assert clock.now == started + DRAIN_S
    assert lock.try_acquire(exclusive=True) is not None


def test_o11_a_drain_published_is_the_deadline_on_the_wall_clock(tmp_path):
    """O11: `publish` turns the plan's deadline on the drain's clock into the wall clock's, once armed."""
    journal = InjectionJournal(tmp_path / "journal")
    clock = FakeClock()
    drain = SessionDrain(DRAIN_S, clock=clock, sleep=clock.sleep, journal=journal, wall=lambda: 5000.0)
    assert drain.publish()["published"] is None  # not armed: nothing is written
    assert journal.read_drain() is None
    drain.begin(GATE).ended(GATE, at=clock.now - 20.0)
    drain.arm(restore_started=clock.now - 30.0)
    record = drain.publish()
    assert record["published"] == 5000.0 + (DRAIN_S - 20.0)
    assert journal.read_drain() == record["published"]


# --- the injector -----------------------------------------------------------------------------------------------------


def test_o11_the_injectors_own_admission_is_kept_until_the_drain_is_over_and_the_restores_time_counts(tmp_path):
    """O11: the sandbox's sessions ended just before the restore; the restore took 180 s serving (what a heal's settle
    takes); the injector holds its own admission and waits the 140 s that are left, so the clear takes 320 s from the
    end of the sessions and not 500, and the snapshot records the times, the wait and why."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S, ready_timeout_s=200), FakeClock()
    injector = injector_for(platform, FakeServices(serves_after=180), clock=clock)
    injector.start(RUN)
    ended = clock.now
    ticket = injector._admission.drain.begin(MOUNT, GATE)
    ticket.ended(MOUNT, at=ended)
    ticket.ended(GATE, at=ended - 30.0)
    assert injector.clear() is True
    assert clock.now - ended == pytest.approx(DRAIN_S)
    drain = step(injector, "drain")
    origin = injector._origin
    assert drain["ok"] is True
    assert drain["deferred"] is False
    assert drain["basis"] == "session_end"
    assert drain["drain_s"] == DRAIN_S
    assert drain["mount_end_s"] == pytest.approx(ended - origin)
    assert drain["gate_stop_s"] == pytest.approx(ended - 30.0 - origin)
    assert drain["until_s"] == pytest.approx(ended + DRAIN_S - origin)
    assert drain["waited_s"] == pytest.approx(DRAIN_S - 180.0, abs=2.0)
    assert drain["why"]
    assert open_injections(tmp_path) == []
    assert injector.snapshot()["ok"] is True


def test_o11_the_injectors_own_admission_stays_closed_while_it_drains(tmp_path):
    """O11: while the injector waits out the drain, the federation is still held against every other run."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S), FakeClock()
    injector = injector_for(platform, FakeServices(), clock=clock)
    lock = FederationLock(settings_of(platform))
    seen: list[bool] = []

    def watch(seconds):
        taken = lock.try_acquire(exclusive=False)
        seen.append(taken is None)
        if taken is not None:
            taken.close()
        clock.sleep(seconds)

    injector.start(RUN)
    injector._sleep = watch  # the drain sleeps through the injector's: it is probed at each of its sleeps
    assert injector.clear() is True
    assert seen  # the drain slept
    assert all(seen)  # and every sleep of it found the federation held
    taken = lock.try_acquire(exclusive=True)
    assert taken is not None  # and once it has ended the federation is free
    taken.close()


def test_o11_an_injector_with_no_report_of_the_sessions_waits_the_full_drain_from_its_restores_start(tmp_path):
    """O11: no sandbox reported its mount or gate to the injector's admission: the end is unknown, so the injector
    waits the whole 320 s from the start of its restore, the restore's own 100 s included."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S, ready_timeout_s=200), FakeClock()
    injector = injector_for(platform, FakeServices(serves_after=100), clock=clock)
    injector.start(RUN)
    began = clock.now
    assert injector.clear() is True
    assert clock.now - began == pytest.approx(DRAIN_S, abs=2.0)
    drain = step(injector, "drain")
    assert drain["basis"] == "restore_start"
    assert drain["mount_end_s"] is None
    assert drain["gate_stop_s"] is None
    assert drain["until_s"] == pytest.approx(began + DRAIN_S - injector._origin, abs=2.0)
    assert "no sandbox" in drain["why"]


def test_o11_an_injector_whose_provider_never_went_down_drains_nothing(tmp_path):
    """O11: a stop that took no effect injected nothing, so no read hung: no wait and no drain step."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S), FakeClock()
    injector = injector_for(platform, FakeServices(stop_noop=True), clock=clock)
    with pytest.raises(Exception, match="could not be taken down"):
        injector.start(RUN)
    assert step(injector, "drain") is None
    assert clock.now < 100.0 + DRAIN_S
    assert not (settings_of(platform).journal / DRAIN).exists()


def test_o11_a_drain_of_zero_leaves_the_snapshot_and_the_clear_as_they_were(tmp_path):
    """O11: with `session_drain_s = 0` there is no drain step, no record and the clear ends at once."""
    platform, clock = write_faults(tmp_path, session_drain_s=0), FakeClock()
    injector = injector_for(platform, FakeServices(), clock=clock)
    injector.start(RUN)
    began = clock.now
    assert injector.clear() is True
    assert step(injector, "drain") is None
    assert clock.now - began < 10
    assert not (settings_of(platform).journal / DRAIN).exists()


def test_o11_a_runs_admission_is_let_go_into_the_drain_when_it_ends_not_when_the_restore_does(tmp_path):
    """O11: the run's sandbox is still up when the injector restores (its mount ends at the launch's exit), so the
    restore defers the drain to the admission, whose release closes the federation for 320 s after the sandbox's end,
    and the snapshot says it was deferred."""
    platform, _, clock, lock = fake_federation(tmp_path)
    injector = injector_for(platform, FakeServices(), clock=clock)
    with lock.held(exclusive=True, target=OUTAGE_TARGET, purpose=OUTAGE_RUN) as admission:
        injector.start(RUN)
        ticket = admission.drain.begin(MOUNT)
        before = clock.now
        assert injector.clear() is True
        assert clock.now - before < 20  # no wait: the mount is still connected
        drain = step(injector, "drain")
        assert drain["deferred"] is True
        assert drain["waited_s"] == 0.0
        assert drain["basis"] == "restore_start"
        assert "mount" in drain["why"]
        assert open_injections(tmp_path) == []
        ticket.ended(MOUNT, at=clock.now)  # the launch's exit stops the sandbox
        ended = clock.now
    assert lock.try_acquire(exclusive=False) is None  # let go into the drain
    clock.now = ended + DRAIN_S - 1
    assert lock.try_acquire(exclusive=True) is None
    clock.now = ended + DRAIN_S
    taken = lock.try_acquire(exclusive=True)
    assert taken is not None
    taken.close()


def test_o11_a_runs_admission_whose_sandbox_ended_before_the_restore_overlaps_the_heals_settle(tmp_path):
    """O11: the sandbox ended, then a restore of 180 s ran (a heal's settle): the federation is closed only for what is
    left of the drain after it, not for 320 s more."""
    platform, _, clock, lock = fake_federation(tmp_path, ready_timeout_s=200)
    injector = injector_for(platform, FakeServices(serves_after=180), clock=clock)
    with lock.held(exclusive=True, target=OUTAGE_TARGET, purpose=OUTAGE_RUN) as admission:
        injector.start(RUN)
        ended = clock.now
        admission.drain.begin(GATE).ended(GATE, at=ended)
        assert injector.clear() is True
        restored = clock.now
        assert restored - ended >= 180
        assert step(injector, "drain")["deferred"] is True
    assert lock.drain_left() == pytest.approx(DRAIN_S - (restored - ended))  # not 320
    clock.now = ended + DRAIN_S
    taken = lock.try_acquire(exclusive=True)
    assert taken is not None
    taken.close()


def test_o11_an_unverified_restore_keeps_its_admission_and_drains_when_a_later_clear_verifies(tmp_path):
    """O11: a restore that is not verified is not drained and arms nothing: the injection stays open with the
    admission held; the clear that verifies it arms the drain."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S), FakeClock()
    services = FakeServices(never_serves=True)
    injector = injector_for(platform, services, clock=clock)
    injector.start(RUN)
    assert injector.clear() is False
    assert step(injector, "drain") is None
    assert injector._admission.held
    services.knobs["never_serves"] = False
    began = clock.now
    assert injector.clear() is True
    assert step(injector, "drain")["basis"] == "restore_start"
    assert clock.now - began == pytest.approx(DRAIN_S, abs=2.0)


def test_o11_the_clears_cancel_bound_counts_the_drain_it_may_wait(tmp_path):
    """O11: a cancelled run waits for a clear's thread at most as long as the clear can take, and the clear may now wait
    out the drain: the bound counts `session_drain_s`."""
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    short = injector_for(write_faults(tmp_path / "a", session_drain_s=0), FakeServices())
    long = injector_for(write_faults(tmp_path / "b", session_drain_s=500), FakeServices())
    assert long.cancel_wait_s - short.cancel_wait_s == pytest.approx(500)


# --- recovery --------------------------------------------------------------------------------------------------


def crashed(tmp_path, **options):
    """A process took the target down and died before clearing; return the platform file and the services' state."""
    platform, state = write_faults(tmp_path, **options), tmp_path / "services.json"
    FakeServices(path=state)
    child = multiprocessing.get_context("spawn").Process(
        target=inject_and_die, args=(str(platform), str(state), RUN["run_key"])
    )
    child.start()
    child.join(60)
    assert child.exitcode == 0
    assert len(open_injections(tmp_path)) == 1
    return platform, state


def test_o11_the_health_gates_recovery_of_a_crashed_runs_injection_closes_the_federation_for_the_drain(tmp_path):
    """O11: a restore the gate makes after a crashed outage run has no sandbox to take times from, so the federation is
    closed for the full drain from the start of its recovery, and its own check waits for it, charging no run."""
    platform, state = crashed(tmp_path, session_drain_s=DRAIN_S, poll_s=0.01, ready_timeout_s=5, health_wait_s=0.3)
    clock = FakeClock()
    began = clock.now
    gate = FaultGate(settings_of(platform), FakeServices(path=state), clock=clock, sleep=clock.sleep, wall=clock)
    health = gate()
    assert open_injections(tmp_path) == []  # restored
    assert (health.ok, health.deferred) == (False, True)
    assert "drain" in health.detail
    lock = FederationLock(settings_of(platform), wall=clock)
    assert lock.drain_left() == pytest.approx(DRAIN_S, abs=2.0)
    assert lock.try_acquire(exclusive=False) is None
    clock.now = began + DRAIN_S + 1
    assert gate().ok is True  # and once it has ended the check passes
    taken = lock.try_acquire(exclusive=True)
    assert taken is not None
    taken.close()


def test_o11_a_gate_with_nothing_to_recover_drains_nothing(tmp_path):
    """O11: no open injection, no restore, no record."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S), FakeClock()
    health = FaultGate(settings_of(platform), FakeServices(), clock=clock, sleep=clock.sleep, wall=clock)()
    assert health.ok is True, health.detail
    assert not (settings_of(platform).journal / DRAIN).exists()


def test_o11_a_start_that_recovers_a_crashed_runs_injection_owes_its_drain_whatever_its_own_sandbox_says(tmp_path):
    """O11: the start restores what a crashed run left, under the exclusive admission it holds: that run's sessions are
    unknown, so the admission owes the full drain from the start of the recovery, even when the current run's own
    sandbox, begun before, has no sessions."""
    platform, state = crashed(tmp_path, session_drain_s=DRAIN_S, poll_s=0.01, ready_timeout_s=5)
    clock = FakeClock()
    injector = injector_for(platform, FakeServices(path=state), clock=clock)
    lock = FederationLock(settings_of(platform), clock=clock, sleep=clock.sleep, wall=clock)
    with lock.held(exclusive=True, target=OUTAGE_TARGET, purpose=OUTAGE_RUN) as admission:
        admission.drain.begin()  # this run's sandbox has no data path
        began = clock.now
        injector.start({**RUN, "attempt": 2})
        assert step(injector, "recovered")["ok"] is True
        plan = admission.drain.plan()
        assert plan.basis == "restore_start"
        assert plan.until == pytest.approx(began + DRAIN_S, abs=2.0)
    assert lock.drain_left() > DRAIN_S - 10


def test_o11_a_recovery_that_holds_no_admission_still_closes_the_federation_for_the_drain(tmp_path):
    """O11: `FaultInjector.recover()` called with no admission held restores a crashed run's provider and clears its
    journal entry; with no admission to keep closed it writes the drain record itself, so the next run is not admitted
    inside the drain."""
    platform, state = crashed(tmp_path, session_drain_s=DRAIN_S, poll_s=0.01, ready_timeout_s=5)
    clock = FakeClock()
    injector = injector_for(platform, FakeServices(path=state), clock=clock, wall=clock)
    assert injector.recover() == []
    assert open_injections(tmp_path) == []
    lock = FederationLock(settings_of(platform), wall=clock)
    assert lock.drain_left() == pytest.approx(DRAIN_S, abs=2.0)
    assert lock.try_acquire(exclusive=False) is None


def test_o11_a_recovery_that_restored_nothing_closes_nothing(tmp_path):
    """O11: no open injection, nothing restored, no record."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S), FakeClock()
    injector = injector_for(platform, FakeServices(), clock=clock, wall=clock)
    assert injector.recover() == []
    assert not (settings_of(platform).journal / DRAIN).exists()


def test_o11_a_recovery_with_no_admission_closes_the_federation_before_it_restores_anything(tmp_path):
    """O11: with no admission held, nothing keeps the federation closed behind a restore, so the record is written
    before the provider is restarted and the injection cleared: no process is admitted in a gap between them."""
    platform, state = crashed(tmp_path, session_drain_s=DRAIN_S, poll_s=0.01, ready_timeout_s=5)
    clock = FakeClock()
    settings = settings_of(platform)
    lock = FederationLock(settings, wall=clock)
    services = FakeServices(path=state)
    seen: list[float] = []
    services.on_serves = lambda service: seen.append(lock.drain_left())  # every reading is before the journal's clear
    injector = injector_for(platform, services, clock=clock, wall=clock)
    assert injector.recover() == []
    assert seen
    assert all(left == pytest.approx(DRAIN_S, abs=2.0) for left in seen)
    assert open_injections(tmp_path) == []


def test_o11_a_recovery_with_no_admission_that_cannot_record_the_drain_restores_nothing(tmp_path, monkeypatch):
    """O11: when the record cannot be written the recovery fails as the injector's own failure before it restarts
    anything: the provider is left for a recovery that can close the federation behind it."""
    platform, state = crashed(tmp_path, session_drain_s=DRAIN_S, poll_s=0.01, ready_timeout_s=5)
    clock = FakeClock()
    services = FakeServices(path=state)
    injector = injector_for(platform, services, clock=clock, wall=clock)

    def broken(self, until):
        raise OSError("the disk is full")

    monkeypatch.setattr(InjectionJournal, "raise_drain", broken)
    with pytest.raises(InfraError, match="could not be recorded"):
        injector.recover()
    assert [call for call in services.calls if call[0] == "start"] == []
    assert len(open_injections(tmp_path)) == 1


def stuck_federation(settings):
    """The lock held through a descriptor no admission knows of, and a holder record naming a process that is gone."""
    settings.journal.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(settings.journal / FEDERATION_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    record = {
        "token": "a" * 32,
        "pid": gone.pid,
        "boot_id": boot_id(),
        "purpose": OUTAGE_RUN,
        "target": OUTAGE_TARGET,
        "since_utc": "2026-01-01T00:00:00+00:00",
    }
    (settings.journal / HOLDER).write_text(json.dumps(record), encoding="utf-8")
    return descriptor


def test_o11_the_gates_recovery_of_a_stuck_holders_injection_closes_the_federation_before_it_restores(tmp_path):
    """O11: a holder that is gone while the lock stays held left an injection open: the health gate recovers it with no
    admission, so it writes the drain record first, then restores."""
    platform, state = crashed(tmp_path, session_drain_s=DRAIN_S, poll_s=0.01, ready_timeout_s=5, health_wait_s=0.2)
    settings = settings_of(platform)
    clock = FakeClock()
    lock = FederationLock(settings, wall=clock)
    services = FakeServices(path=state)
    seen: list[float] = []
    services.on_serves = lambda service: seen.append(lock.drain_left())
    descriptor = stuck_federation(settings)
    try:
        health = FaultGate(settings, services, clock=clock, sleep=clock.sleep, wall=clock)()
    finally:
        os.close(descriptor)
    assert health.ok is False
    assert "stuck" in health.detail
    assert open_injections(tmp_path) == []  # recovered
    assert seen
    assert all(left == pytest.approx(DRAIN_S, abs=2.0) for left in seen)


# --- what the last review found --------------------------------------------------------------------------------


def test_o11_a_second_recovery_owes_its_own_drain_on_top_of_the_first():
    """O11: the first recovery restored provider A at 100 and failed on B; the retry at 200 restored B: the drain is
    owed from the later start, 520, not from the first, 420."""
    drain, _, _ = drained()
    drain.owe(100.0)
    drain.owe(200.0)
    assert drain.plan().until == 200.0 + DRAIN_S
    drain.owe(150.0)  # an earlier start noted late never lowers it
    assert drain.plan().until == 200.0 + DRAIN_S


def test_o11_a_pause_between_the_two_clocks_never_publishes_an_earlier_deadline(tmp_path):
    """O11: the wall clock is read just after the monotonic one, so a process paused between them (here the wall clock's
    own read takes 30 s) publishes a deadline that is 30 s later than the plan's, never earlier."""
    journal = InjectionJournal(tmp_path / "journal")
    clock = FakeClock()  # now = 100

    def wall():
        value = clock.now + 1000.0
        clock.now += 30.0  # the process is descheduled right after it has read the wall clock
        return value

    drain = SessionDrain(DRAIN_S, clock=clock, sleep=clock.sleep, journal=journal, wall=wall)
    drain.begin(GATE).ended(GATE, at=100.0)
    drain.arm(restore_started=100.0)
    record = drain.publish()
    assert record["published"] >= 1000.0 + 100.0 + DRAIN_S  # the plan's end on the wall clock, or later


def test_o11_an_admission_whose_drain_can_be_neither_recorded_nor_waited_out_is_never_let_go(tmp_path, monkeypatch):
    """O11: the record cannot be written, and the wait in its place is interrupted: the lock is kept, with its holder
    record, for as long as the process lives, instead of opening the federation inside the drain."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S), FakeClock()
    settings = settings_of(platform)

    def interrupted(seconds):
        raise KeyboardInterrupt

    def broken(self, until):
        raise OSError("the disk is full")

    monkeypatch.setattr(InjectionJournal, "raise_drain", broken)
    lock = FederationLock(settings, clock=clock, sleep=interrupted, wall=clock)
    admission = lock.acquire(exclusive=True, purpose=OUTAGE_RUN, target=OUTAGE_TARGET)
    admission.drain.arm(restore_started=clock.now)
    try:
        with pytest.raises(KeyboardInterrupt):
            admission.close()
        assert lock.try_acquire(exclusive=False) is None
        assert lock.try_acquire(exclusive=True) is None
        assert InjectionJournal(settings.journal).read_holder() is not None
    finally:
        for held in _STRANDED:
            held.close()
        _STRANDED.clear()
    taken = lock.try_acquire(exclusive=True)  # only the process's end would have let it go
    assert taken is not None
    taken.close()


# --- the drain is closed behind a restore before the restore, not after it ----------------------------------------


def test_o11_an_interrupt_during_the_gates_recovery_still_closes_the_federation(tmp_path):
    """O11: the gate's recovery arms its drain before it restores anything, so an interrupt after the provider is back
    and the journal cleared, before the recovery could report it, cannot leave the federation open: the admission's
    release publishes the drain whatever ended the recovery."""
    platform, state = crashed(tmp_path, session_drain_s=DRAIN_S, poll_s=0.01, ready_timeout_s=5)
    clock = FakeClock()
    services = FakeServices(path=state)

    def interrupted(service):
        raise KeyboardInterrupt

    services.on_serves = interrupted
    gate = FaultGate(settings_of(platform), services, clock=clock, sleep=clock.sleep, wall=clock)
    with pytest.raises(KeyboardInterrupt):
        gate()
    assert FederationLock(settings_of(platform), wall=clock).drain_left() == pytest.approx(DRAIN_S, abs=2.0)


def test_o11_a_recovery_under_an_admission_owes_its_drain_before_it_restores_and_holds_the_admission(tmp_path):
    """O11: `FaultInjector.recover()` under an exclusive admission owes the drain on it before the provider is
    restarted, and keeps a share of the admission until it has ended, so a cancelled owner cannot let the lock go
    under it."""
    platform, state = crashed(tmp_path, session_drain_s=DRAIN_S, poll_s=0.01, ready_timeout_s=5)
    clock = FakeClock()
    settings = settings_of(platform)
    lock = FederationLock(settings, clock=clock, sleep=clock.sleep, wall=clock)
    services = FakeServices(path=state)
    injector = injector_for(platform, services, clock=clock, wall=clock)
    seen: list[tuple[bool, int]] = []
    with lock.held(exclusive=True, target=OUTAGE_TARGET, purpose=OUTAGE_RUN) as admission:
        services.on_serves = lambda service: seen.append((admission.drain.armed, admission.shares.count))
        assert injector.recover() == []
        assert admission.shares.count == 1  # the share it took is given up
    assert seen
    assert all(armed and count >= 2 for armed, count in seen)  # armed, and the recovery held its own share
    assert lock.drain_left() == pytest.approx(DRAIN_S, abs=2.0)


def test_o11_a_later_clear_without_the_original_admission_closes_the_federation_before_it_restores(tmp_path):
    """O11: the first clear fails readiness and the run's admission is let go; the retry, which holds none, writes the
    drain record before it journals the injection cleared, and does not return `True` without one."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S), FakeClock()
    settings = settings_of(platform)
    lock = FederationLock(settings, clock=clock, sleep=clock.sleep, wall=clock)
    services = FakeServices(never_serves=True)
    injector = injector_for(platform, services, clock=clock, wall=clock)
    with lock.held(exclusive=True, target=OUTAGE_TARGET, purpose=OUTAGE_RUN):
        injector.start(RUN)
        assert injector.clear() is False  # not serving: the injection stays open
    clock.now += DRAIN_S + 1  # the first clear's drain, published as the admission went, is over
    assert lock.drain_left() == 0.0
    services.knobs["never_serves"] = False
    seen: list[float] = []
    services.on_serves = lambda service: seen.append(lock.drain_left())
    assert injector.clear() is True
    assert seen
    assert all(left == pytest.approx(DRAIN_S, abs=2.0) for left in seen)  # closed before the journal's clear
    assert lock.drain_left() > 0
    assert open_injections(tmp_path) == []


def test_o11_an_unverified_restore_under_a_runs_admission_still_closes_the_federation_when_it_ends(tmp_path):
    """O11: the provider went down and was restarted but never served: the injection stays open and the run's admission,
    armed before the restore, closes the federation for the drain behind it."""
    platform, _, clock, lock = fake_federation(tmp_path)
    injector = injector_for(platform, FakeServices(never_serves=True), clock=clock, wall=clock)
    with lock.held(exclusive=True, target=OUTAGE_TARGET, purpose=OUTAGE_RUN) as admission:
        admission.drain.begin(GATE).ended(GATE, at=clock.now)
        injector.start(RUN)
        assert injector.clear() is False
    assert lock.drain_left() > 0
    assert lock.try_acquire(exclusive=False) is None


def test_o11_a_provider_the_stop_never_took_down_closes_nothing_under_a_runs_admission(tmp_path):
    """O11: an outage run whose provider never went down arms nothing that survives its restore: no record is written
    when its admission ends, and the next run is admitted at once."""
    platform, settings, clock, lock = fake_federation(tmp_path)
    injector = injector_for(platform, FakeServices(stop_noop=True), clock=clock, wall=clock)
    with lock.held(exclusive=True, target=OUTAGE_TARGET, purpose=OUTAGE_RUN) as admission:
        with pytest.raises(InfraError, match="could not be taken down"):
            injector.start(RUN)
        assert not admission.drain.armed
    assert not (settings.journal / DRAIN).exists()
    taken = lock.try_acquire(exclusive=True)
    assert taken is not None
    taken.close()
