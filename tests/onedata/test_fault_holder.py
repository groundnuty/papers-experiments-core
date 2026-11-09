"""O11: a wait for the federation that runs out is told apart: an outage run on it, a stuck holder, or a failed restore.

Whoever takes the federation exclusively writes a holder record beside its lock, durably: its pid and boot, its purpose,
the target's label and the time it took the lock. The platform's check reads it when its bounded wait runs out: a live
holder is an outage run on the federation, so the check is deferred; a holder that is gone while the lock stays held is
stuck, so the check recovers what it left open (or clears its record when nothing is open) and fails; and an injection
whose restore failed fails the check, naming the target by its label.
"""

import fcntl
import json
import multiprocessing
import os
import subprocess
import sys
import time

import pytest
from fault_support import OUTAGE_TARGET, SERVICES, FakeServices, inject_and_die, injector_for, write_faults

from expcore_onedata.admission import FederationLock
from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.fault_journal import boot_id
from expcore_onedata.faults import load_faults

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
HOLDER = "federation.holder"
OUTAGE_SERVICE = SERVICES["beta"]


def settings_of(platform):
    return load_faults(platform, load_platform(platform, require_secrets=False))


def fault_gate(platform, services):
    """The fault gate alone, with nothing inside it: it answers for the federation's faults only."""
    return FaultGate(settings_of(platform), services)


def dead_pid():
    """The pid of a process that has ended."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def leaked_lock(settings):
    """Hold the federation's lock exclusively through a descriptor no admission knows of, as a leaked one would."""
    settings.journal.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(settings.journal / "federation.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def write_record(settings, pid):
    settings.journal.mkdir(parents=True, exist_ok=True)
    record = {"token": "a" * 32, "pid": pid, "boot_id": boot_id(), "purpose": "outage_run", "target": OUTAGE_TARGET}
    (settings.journal / HOLDER).write_text(json.dumps({**record, "since_utc": "2026-01-01T00:00:00+00:00"}))


def test_o11_an_outage_run_on_the_federation_is_named_by_its_holder_record_and_the_check_is_deferred(tmp_path):
    """O11: the exclusive holder writes a record beside the lock: its pid, boot, purpose, target label and start time.
    A check whose wait runs out reads it: a live holder is an outage run on the federation, so the check is deferred.
    The record goes when the lock is let go."""
    platform = write_faults(tmp_path, health_wait_s=0.2)
    settings = settings_of(platform)
    injector = injector_for(platform, FakeServices())
    injector.start(RUN)
    try:
        record = json.loads((settings.journal / HOLDER).read_text(encoding="utf-8"))
        assert set(record) == {"token", "pid", "boot_id", "purpose", "target", "since_utc"}
        assert (record["pid"], record["boot_id"]) == (os.getpid(), boot_id())
        assert (record["purpose"], record["target"]) == ("injector", OUTAGE_TARGET)
        health = fault_gate(platform, FakeServices())()
        assert (health.ok, health.deferred) == (False, True), health.detail
        assert OUTAGE_TARGET in health.detail
    finally:
        assert injector.finish()["ok"] is True
    assert not (settings.journal / HOLDER).exists()


def test_o11_a_lock_held_while_its_recorded_holder_is_gone_is_stuck_and_its_record_is_cleared(tmp_path):
    """O11: the federation stays held, but the holder its record names is gone and the journal has nothing open: the
    lock is stuck, not contended. The check fails, saying so, and clears the record."""
    platform = write_faults(tmp_path, health_wait_s=0.2)
    settings = settings_of(platform)
    write_record(settings, dead_pid())
    descriptor = leaked_lock(settings)
    try:
        health = fault_gate(platform, FakeServices())()
    finally:
        os.close(descriptor)
    assert (health.ok, health.deferred) == (False, False)
    assert "stuck" in health.detail
    assert not (settings.journal / HOLDER).exists()


def test_o11_a_stuck_holder_that_left_its_target_down_is_recovered_by_the_check(tmp_path):
    """O11: an outage run died with its target down, and the federation's lock stayed held after it: the check finds
    its holder gone, brings the target back, verified, settles the journal and fails, naming the lock stuck."""
    platform, state = write_faults(tmp_path, health_wait_s=0.2), tmp_path / "services.json"
    settings = settings_of(platform)
    FakeServices(path=state)
    child = multiprocessing.get_context("spawn").Process(target=inject_and_die, args=(str(platform), str(state), "r"))
    child.start()
    child.join(60)
    assert child.exitcode == 0
    services = FakeServices(path=state)
    assert services.running(OUTAGE_SERVICE) is False
    assert json.loads((settings.journal / HOLDER).read_text(encoding="utf-8"))["pid"] == child.pid
    descriptor = leaked_lock(settings)
    try:
        health = fault_gate(platform, services)()
    finally:
        os.close(descriptor)
    assert (health.ok, health.deferred) == (False, False)
    assert "stuck" in health.detail
    assert services.running(OUTAGE_SERVICE) is True
    assert fault_gate(platform, services)().ok is True  # settled, and the lock is free again


def test_o11_a_live_holder_whose_restore_failed_fails_the_check_naming_the_target(tmp_path):
    """O11: an outage run's restore failed and it still holds the federation: the journal shows the injection open
    after a failed clear, so the check whose wait runs out reports the failed restore by the target's label, never as
    an outage run in progress."""
    platform = write_faults(tmp_path, health_wait_s=0.2)
    services = FakeServices()
    injector = injector_for(platform, services)
    injector.start(RUN)
    services.knobs["start_fails"] = True
    try:
        assert injector.clear() is False  # pending: it keeps the federation until a restore is verified
        health = fault_gate(platform, services)()
    finally:
        services.knobs["start_fails"] = False
        assert injector.finish()["ok"] is True
    assert (health.ok, health.deferred) == (False, False)
    assert "restore" in health.detail
    assert OUTAGE_TARGET in health.detail


@pytest.mark.parametrize("exclusive", [False, True])
def test_o11_a_record_left_by_a_holder_that_is_gone_is_cleared_by_the_next_admission(tmp_path, exclusive):
    """O11: a record whose holder let the lock go without clearing it is stale once anyone holds the federation: a
    shared admission clears it, an exclusive one replaces it with its own, and either way it is gone after."""
    platform = write_faults(tmp_path)
    settings = settings_of(platform)
    write_record(settings, dead_pid())
    started = time.time()
    with FederationLock(settings).held(exclusive=exclusive):
        path = settings.journal / HOLDER
        if exclusive:
            record = json.loads(path.read_text(encoding="utf-8"))
            assert (record["pid"], record["purpose"]) == (os.getpid(), "outage_run")
            assert record["since_utc"] >= time.strftime("%Y-%m-%dT%H:%M", time.gmtime(started))
        else:
            assert not path.exists()
    assert not (settings.journal / HOLDER).exists()
