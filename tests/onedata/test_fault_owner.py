"""An injection an earlier attempt left open charges no run that meets it: the injector's journal names the attempt
that made each injection, and the health verdicts charge only the attempt that did, never another (O80)."""

import fcntl
import gc
import json
import os
import subprocess
import sys

import pytest
from campaign_support import planned_store
from fault_support import OUTAGE_TARGET, SERVICES, FakeClock, FakeServices, injector_for, write_faults

from expcore.campaign import liveness
from expcore.campaign.healthgate import HOLD, HealthGate
from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate import FaultGate, _unsettled
from expcore_onedata.fault_journal import JOURNAL, InjectionJournal, boot_id
from expcore_onedata.faults import SettledInjections, load_faults

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
NONE_RECORDED = object()  # the entry has no `owner` key at all
SETTLED = f"an injection of {OUTAGE_TARGET} is unsettled: the federation is not in its baseline state"
FAILED = f"the restore of {OUTAGE_TARGET} (start_failed) failed: the federation is not in its baseline state"


@pytest.fixture
def campaign():
    """A campaign registered in this process: its store's attempt rows, by id, as a dict the test sets."""
    rows: dict[int, str] = {}
    key = liveness.register(rows.get)
    rows.update({5: "running", 6: "running"})
    yield key
    liveness.unregister(key)


def dead_pid():
    """The pid of a process that has ended."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def attempt(key, attempt_id):
    return {"process": liveness.this_process(), "campaign": key, "attempt_id": attempt_id}


def entry(owner=NONE_RECORDED, *, failed=None, target=OUTAGE_TARGET, id="inj-1"):
    """An open injection of the fault journal, as `InjectionJournal.open_entries` returns it."""
    named = {} if owner is NONE_RECORDED else {"owner": owner}
    return {
        "event": "injecting",
        "id": id,
        "target": target,
        "service": SERVICES["beta"],
        "injected": True,
        **named,
        **({} if failed is None else {"clear_failed": failed}),
    }


def journal_with(tmp_path, *entries):
    """A journal directory whose open injections are `entries`, written as the injector writes them."""
    platform = write_faults(tmp_path)
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    journal = InjectionJournal(settings.journal)
    for line in entries:
        journal.append({k: v for k, v in line.items() if k not in ("injected", "clear_failed")})
        if "clear_failed" in line:
            journal.append({"event": "clear_failed", "id": line["id"], "how": line["clear_failed"]})
    return SettledInjections(settings)


def test_o80_an_injection_line_names_the_attempt_that_made_it_and_outside_one_only_the_process(tmp_path, campaign):
    """O80: the `injecting` line carries the owner as the baseline marks do (`liveness.owner`): the process, the
    campaign and the store's id of the attempt inside one, and the process alone outside any."""
    platform, services = write_faults(tmp_path), FakeServices()
    injector = injector_for(platform, services)
    with liveness.running(campaign, 5):
        injector.start(RUN)
    [line] = [json.loads(t) for t in (tmp_path / "fault-journal" / JOURNAL).read_text().splitlines()[:1]]
    assert (line["event"], line["owner"]) == ("injecting", attempt(campaign, 5))
    injector.finish()
    outside = injector_for(platform, services)
    outside.start({**RUN, "attempt": 2})
    last = [json.loads(t) for t in (tmp_path / "fault-journal" / JOURNAL).read_text().splitlines()]
    assert [x["owner"] for x in last if x["event"] == "injecting"][-1] == {"process": liveness.this_process()}
    outside.finish()


def test_o80_an_attempts_own_unsettled_injection_is_charged_to_it_and_to_no_other(tmp_path, campaign):
    """O80: attempt 5's injection left open: its own post-run check is charged exactly as before; attempt 6's check, and
    a pre-run check outside any attempt, meet another attempt's leftover and charge no run, saying why."""
    check = journal_with(tmp_path, entry(attempt(campaign, 5)))
    with liveness.running(campaign, 5):
        own = check()
    assert (own.ok, own.deferred, own.charge, own.detail) == (False, False, True, SETTLED)
    for judged in (6, None):
        if judged is None:
            other = check()
        else:
            with liveness.running(campaign, judged):
                other = check()
        assert (other.ok, other.deferred, other.charge) == (False, False, False)
        assert other.detail.startswith(SETTLED)
        assert "left by another attempt" in other.detail


@pytest.mark.parametrize("failed", ["start_failed", "stop_unsettled"])
def test_o80_a_failed_restore_charges_the_attempt_that_injected_and_holds_every_other_uncharged(campaign, failed):
    """O80: the verdict that names a failed restore (`clear_failed`) charges its injecting attempt and no other."""
    detail = f"the restore of {OUTAGE_TARGET} ({failed}) failed: the federation is not in its baseline state"
    entries = [entry(attempt(campaign, 5), failed=failed)]
    with liveness.running(campaign, 5):
        own = _unsettled(entries)
    assert (own.charge, own.detail) == (True, detail)
    with liveness.running(campaign, 6):
        other = _unsettled(entries)
    assert (other.ok, other.charge) == (False, False)
    assert other.detail.startswith(detail)
    assert "left by another attempt" in other.detail
    assert _unsettled(entries).charge is False  # a pre-run check: no attempt is judged


def test_o80_an_entry_that_records_no_owner_is_charged_as_before_and_says_so(tmp_path, campaign):
    """O80: an injection written by code that recorded no owner (or one that cannot be read) is never guessed at: the
    check charges as it did, and its detail says that no owner is recorded."""
    for owner in (NONE_RECORDED, None, "a string", {"process": 3}):
        health = _unsettled([entry(owner, failed="start_failed")])
        assert (health.ok, health.charge) == (False, True), owner
        assert health.detail.startswith(FAILED), owner
        assert "records no attempt as its owner" in health.detail, owner
    health = journal_with(tmp_path)()  # nothing open: healthy, with nothing to say about owners
    assert health.ok is True


def test_o80_an_injection_made_outside_any_attempt_names_no_attempt_and_is_charged_as_before(campaign):
    """O80: a heal's container restart or a tool's injection is no attempt's failure to spare: its owner is the process
    alone, so a run that finds it is charged as before, with its detail saying no attempt owns it."""
    health = _unsettled([entry({"process": liveness.this_process()})])
    assert health.charge is True
    assert health.detail.startswith(SETTLED)
    assert "records no attempt as its owner" in health.detail


def test_o80_the_judged_attempts_own_injection_is_never_spared_by_anothers_beside_it(campaign):
    """O80: with entries of two attempts open, the check of one of them is charged (its own failure is never spared,
    as a baseline mark's is not); a check outside both charges no one."""
    entries = [entry(attempt(campaign, 5), id="a"), entry(attempt(campaign, 6), id="b", target="providers[1]")]
    with liveness.running(campaign, 6):
        assert _unsettled(entries).charge is True
    with liveness.running(campaign, 5):
        assert _unsettled(entries).charge is True
    assert _unsettled(entries).charge is False


def test_o80_an_entry_of_another_attempt_beside_one_with_no_owner_keeps_the_charge(campaign):
    """O80: an entry with no owner keeps the charge, so an ownerless leftover is never hidden by another's."""
    entries = [entry(attempt(campaign, 5), id="a"), entry(id="b", target="providers[1]")]
    with liveness.running(campaign, 6):
        health = _unsettled(entries)
    assert health.charge is True
    assert "records no attempt as its owner" in health.detail


def test_o80_a_journal_that_cannot_be_read_is_charged_as_before(tmp_path):
    """O80: an unreadable journal says nothing about whose it is: the check fails closed and charges, as it did."""
    platform = write_faults(tmp_path)
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    (settings.journal).mkdir(parents=True, exist_ok=True)
    (settings.journal / JOURNAL).write_text('{"event": "stopped-by-nobody"}\n', encoding="utf-8")
    health = SettledInjections(settings)()
    assert (health.ok, health.charge) == (False, True)
    assert "cannot be read" in health.detail


# --- through the real gate: a recovery that fails, and a check that reads the journal ---------------------------------


def left_failed(tmp_path, campaign, attempt_id):
    """An injection attempt `attempt_id` made whose restore never served, its injector gone so nobody holds the target:
    the next check recovers it, and that recovery fails too. Return the gate that checks, on a fake clock."""
    platform = write_faults(tmp_path, health_wait_s=0.2)
    injector = injector_for(platform, FakeServices(never_serves=True))
    with liveness.running(campaign, attempt_id):
        injector.start(RUN)
        assert injector.clear() is False
    del injector
    gc.collect()  # a dropped injector lets its target go
    clock = FakeClock()
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    return FaultGate(settings, FakeServices(never_serves=True), clock=clock, sleep=clock.sleep, wall=clock)


def test_o80_a_failed_recovery_of_an_earlier_attempts_injection_charges_no_run_but_its_own_attempt(tmp_path, campaign):
    """O80: the check that settles what an earlier run left open fails with `an injection of X that an earlier run left
    open was not cleared`, and charged the run that ran the check. Now only the attempt that made the injection is
    charged: its own check, as before; a pre-run check and another attempt's check hold uncharged."""
    gate = left_failed(tmp_path, campaign, 5)
    pre_run = gate()
    assert (pre_run.ok, pre_run.deferred, pre_run.charge) == (False, False, False)
    assert "an earlier run left open was not cleared" in pre_run.detail
    assert "left by another attempt" in pre_run.detail
    with liveness.running(campaign, 6):
        assert gate().charge is False
    with liveness.running(campaign, 5):
        own = gate()
    assert (own.ok, own.charge) == (False, True)
    assert "an earlier run left open was not cleared" in own.detail
    assert "left by another attempt" not in own.detail


def test_o80_the_gate_holds_the_run_after_an_attempts_failed_injection_uncharged_and_charges_that_attempt(
    tmp_path, campaign
):
    """O80: through the core's gate: attempt 5 left an injection it could not restore. The next run's pre-run check
    closes the gate and holds the run, charging no attempt, and the outage's record says `charged: false` with why;
    attempt 5's own post-run check, inside the attempt, still fails and is charged, as it always was."""
    store, _, manifest = planned_store(tmp_path / "store")
    key = manifest.rows[0].key.text
    stamps = iter(f"2026-01-01T00:{minute:02d}:00Z" for minute in range(1, 60))
    gate = left_failed(tmp_path / "faults", campaign, 5)
    health = HealthGate(store, gate, lambda: next(stamps), timeout_s=30.0)
    health.load()
    held = health.before_run(key)
    assert held.verdict == HOLD
    assert "left by another attempt" in held.detail
    assert store.q("SELECT COUNT(*) AS n FROM attempts")[0]["n"] == 0  # no attempt was begun, so none is charged
    [outage] = json.loads(store.get_meta("health_outages"))
    assert outage["charged"] is False
    assert "no run is charged, this one included" in " ".join(r["detail"] for r in store.q("SELECT detail FROM events"))
    other = HealthGate(store, left_failed(tmp_path / "again", campaign, 5), lambda: next(stamps), timeout_s=30.0)
    other.load()
    with liveness.running(campaign, 5):
        outcome, _ = other.after_run()
    assert (outcome.ok, outcome.charge) == (False, True)


def test_o80_a_lock_left_by_a_gone_holder_beside_an_open_injection_is_judged_by_the_injections_owner(
    tmp_path, campaign
):
    """O80: the dead-holder branch of the gate (the lock stays held while its recorded holder is gone) recovers what
    the holder left open before it answers, and fails the same way: it charges no run but the attempt that owns the
    open injection."""
    gate = left_failed(tmp_path, campaign, 5)
    platform = tmp_path / "platform.toml"
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    settings.journal.mkdir(parents=True, exist_ok=True)
    holder = {
        "token": "a" * 32,
        "pid": dead_pid(),
        "boot_id": boot_id(),
        "purpose": "outage_run",
        "target": OUTAGE_TARGET,
    }
    (settings.journal / "federation.holder").write_text(
        json.dumps({**holder, "since_utc": "2026-01-01T00:00:00+00:00"})
    )
    descriptor = os.open(settings.journal / "federation.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)  # held through a descriptor no admission knows of, as a leaked lock is
    try:
        gate = FaultGate(settings, FakeServices(never_serves=True), wall=FakeClock())
        verdicts = {}
        for attempt_id in (6, 5):
            with liveness.running(campaign, attempt_id):
                verdicts[attempt_id] = gate()
    finally:
        os.close(descriptor)
    assert [verdicts[6].charge, verdicts[5].charge] == [False, True]
    assert "an earlier run left open was not cleared" in verdicts[6].detail


def test_o80_the_judged_attempts_own_open_injection_is_charged_beside_anothers_failed_restore(campaign):
    """O80: the failed restore of another attempt's injection is what the detail names, but the check of an attempt
    whose own injection is open beside it is still charged: its own failure is never spared."""
    entries = [entry(attempt(campaign, 5), failed="start_failed", id="a"), entry(attempt(campaign, 6), id="b")]
    with liveness.running(campaign, 6):
        health = _unsettled(entries)
    assert health.charge is True
    assert health.detail.startswith(FAILED)
