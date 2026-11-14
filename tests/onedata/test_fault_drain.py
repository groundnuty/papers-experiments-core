"""O11: after an outage run, the federation's exclusive admission stays closed until the run's sessions have drained.

An agent's read of a file only the stopped provider holds, through the local provider, leaves an on-demand sync job
there that nothing calls back; a later read of the same file and range joins it and hangs. The job ends only when the
run's callers end: a REST read when its request process dies (the gate's stop), a mount when oneclient's last
connection closes plus a grace (300 s, op-worker's `fuse_session_grace_period_seconds`, and about 15 s more). So the
admission of an outage run is let go at least `[faults] session_drain_s` (320 s) after the run's mount disconnected and
its REST gate stopped, whichever is later; the wait is the remainder after whatever the restore took, and times that
are missing mean the whole drain from the restore's start. Every test runs on a fake clock against `FakeServices`.
"""

import json
import multiprocessing
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
from expcore_onedata.admission import OUTAGE_RUN, FederationLock, run_admission
from expcore_onedata.config import load_platform
from expcore_onedata.fault_drain import GATE, MOUNT, SessionDrain
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.fault_journal import JOURNAL
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


# --- the admission ---------------------------------------------------------------------------------------------


class GatedSleep:
    """A sleep that does not return until the test lets it, so that the admission can be probed while it waits."""

    def __init__(self, clock):
        self.clock, self.asleep, self.release, self.slept = clock, threading.Event(), threading.Event(), []

    def __call__(self, seconds):
        self.slept.append(seconds)
        self.asleep.set()
        assert self.release.wait(30), "the test never let the drain end"
        self.clock.sleep(seconds)


def test_o11_an_armed_exclusive_admission_stays_closed_to_every_other_run_until_its_drain_has_ended(tmp_path):
    """O11: closing the admission of an outage run blocks, holding the lock, until the drain is over: a bounded try for
    the lock, shared or exclusive, is refused meanwhile and granted once it has ended."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S), FakeClock()
    gated = GatedSleep(clock)
    lock = FederationLock(settings_of(platform), clock=clock, sleep=gated)
    admission = lock.acquire(exclusive=True, purpose=OUTAGE_RUN, target=OUTAGE_TARGET)
    admission.drain.begin(GATE).ended(GATE, at=clock.now)
    admission.drain.arm(restore_started=clock.now)
    closing = threading.Thread(target=admission.close)
    closing.start()
    assert gated.asleep.wait(10), "the close never entered its drain"
    for _ in range(5):  # the drain is under way: nothing gets in, shared or exclusive
        assert lock.try_acquire(exclusive=False) is None
        assert lock.try_acquire(exclusive=True) is None
        time.sleep(0.01)
    assert closing.is_alive()
    gated.release.set()
    closing.join(10)
    assert not closing.is_alive()
    assert gated.slept == [DRAIN_S]
    for exclusive in (False, True):
        taken = lock.try_acquire(exclusive=exclusive)
        assert taken is not None
        taken.close()


def test_o11_an_admission_nothing_armed_is_let_go_at_once(tmp_path):
    """O11: a shared admission, an exclusive run with no fault, an outage run whose provider never went down: none is
    delayed, whatever `session_drain_s` says."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S), FakeClock()

    def forbidden(seconds):
        raise AssertionError("an admission nothing armed slept")

    lock = FederationLock(settings_of(platform), clock=clock, sleep=forbidden)
    for exclusive in (False, True):
        admission = lock.acquire(exclusive=exclusive)
        assert admission.drain is None or not admission.drain.armed
        admission.close()
        again = lock.try_acquire(exclusive=True)
        assert again is not None
        again.close()


def test_o11_a_run_that_is_not_an_outage_run_is_not_delayed_by_the_run_admission(tmp_path):
    """O11: the platform's run admission lets a baseline run and an exclusive control go the moment their attempt ends,
    with the default drain of 320 s declared."""
    platform = write_faults(tmp_path, session_drain_s=DRAIN_S)
    admit = run_admission(load_platform(platform, require_secrets=False))
    started = time.monotonic()
    for kinds, exclusive in (((), False), ((), True)):
        with admit(kinds, {}, threading.Event(), exclusive) as held:
            assert held == ("exclusive" if exclusive else "shared")
    assert time.monotonic() - started < 10


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
    assert drain["waited_s"] == pytest.approx(DRAIN_S - 180.0 - 0.0, abs=2.0)
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


def test_o11_a_drain_of_zero_leaves_the_snapshot_and_the_clear_as_they_were(tmp_path):
    """O11: with `session_drain_s = 0` there is no drain step and the clear ends at once."""
    platform, clock = write_faults(tmp_path, session_drain_s=0), FakeClock()
    injector = injector_for(platform, FakeServices(), clock=clock)
    injector.start(RUN)
    began = clock.now
    assert injector.clear() is True
    assert step(injector, "drain") is None
    assert clock.now - began < 10


def test_o11_a_runs_admission_is_drained_when_it_ends_not_when_the_restore_does(tmp_path):
    """O11: the run's sandbox is still up when the injector restores (its mount ends at the launch's exit), so the
    restore defers the drain to the admission, which waits the whole 320 s after the sandbox's end, and the snapshot
    says it was deferred."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S), FakeClock()
    settings = settings_of(platform)
    injector = injector_for(platform, FakeServices(), clock=clock)
    lock = FederationLock(settings, clock=clock, sleep=clock.sleep)
    with lock.held(exclusive=True, target=OUTAGE_TARGET, purpose=OUTAGE_RUN) as admission:
        injector.start(RUN)
        ticket = admission.drain.begin(MOUNT)
        before = clock.now
        assert injector.clear() is True
        assert clock.now - before < 20  # no wait yet: the mount is still connected
        drain = step(injector, "drain")
        assert drain["deferred"] is True
        assert drain["waited_s"] == 0.0
        assert drain["basis"] == "restore_start"
        assert "mount" in drain["why"]
        assert open_injections(tmp_path) == []
        ticket.ended(MOUNT, at=clock.now)  # the launch's exit stops the sandbox
        ended = clock.now
    assert clock.now - ended == pytest.approx(DRAIN_S)  # the exit of the admission waited
    taken = lock.try_acquire(exclusive=True)
    assert taken is not None
    taken.close()


def test_o11_a_runs_admission_whose_sandbox_ended_before_the_restore_overlaps_the_heals_settle(tmp_path):
    """O11: the sandbox ended, then a restore of 180 s ran (a heal's settle): the admission's exit waits the rest."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S, ready_timeout_s=200), FakeClock()
    settings = settings_of(platform)
    injector = injector_for(platform, FakeServices(serves_after=180), clock=clock)
    lock = FederationLock(settings, clock=clock, sleep=clock.sleep)
    with lock.held(exclusive=True, target=OUTAGE_TARGET, purpose=OUTAGE_RUN) as admission:
        injector.start(RUN)
        ended = clock.now
        admission.drain.begin(GATE).ended(GATE, at=ended)
        assert injector.clear() is True
        restored = clock.now
        assert restored - ended >= 180
        assert step(injector, "drain")["deferred"] is True
    assert clock.now - ended == pytest.approx(DRAIN_S)  # not 180 + 320
    assert clock.now > restored


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


def test_o11_the_health_gates_recovery_of_a_crashed_runs_injection_drains_before_it_lets_the_federation_go(tmp_path):
    """O11: a restore the gate makes after a crashed outage run has no sandbox to take times from, so the exclusive
    admission it holds is kept the full drain from the start of its recovery."""
    platform, state = crashed(tmp_path, session_drain_s=DRAIN_S, poll_s=0.01, ready_timeout_s=5)
    clock = FakeClock()
    began = clock.now
    health = FaultGate(settings_of(platform), FakeServices(path=state), clock=clock, sleep=clock.sleep)()
    assert health.ok is True, health.detail
    assert clock.now - began == pytest.approx(DRAIN_S, abs=2.0)
    assert open_injections(tmp_path) == []


def test_o11_a_gate_with_nothing_to_recover_drains_nothing(tmp_path):
    """O11: no open injection, no restore, no wait."""
    platform, clock = write_faults(tmp_path, session_drain_s=DRAIN_S), FakeClock()
    began = clock.now
    health = FaultGate(settings_of(platform), FakeServices(), clock=clock, sleep=clock.sleep)()
    assert health.ok is True, health.detail
    assert clock.now == began


def test_o11_a_start_that_recovers_a_crashed_runs_injection_arms_the_drain_of_its_admission(tmp_path):
    """O11: the start restores what a crashed run left, under the exclusive admission it holds: that restore arms the
    drain, so even a start that then fails before it injects lets the federation go only after it."""
    platform, state = crashed(tmp_path, session_drain_s=DRAIN_S, poll_s=0.01, ready_timeout_s=5)
    clock = FakeClock()
    services = FakeServices(path=state)
    injector = injector_for(platform, services, clock=clock)
    injector.start({**RUN, "attempt": 2})
    assert injector._admission.drain.armed
    assert step(injector, "recovered")["ok"] is True
