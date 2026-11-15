"""O11: a wait for the federation that runs out because another run legitimately holds it is a hold, never a failure.

An outage run holds the federation exclusively for its whole attempt, its restore and heal included, which can last
over an hour; every other run waits for it, bounded by `admission_timeout_s`. A wait that runs out says nothing about
the waiting run, so it is `federation_busy`, a hold that costs no attempt (as `ingest_held` is): piled-up waits never
exhaust `max_attempts`. The injector's own failures (`injector_failure`) are still charged.
"""

import asyncio
import contextlib
import threading
import time

import pytest
from campaign_support import keys_under, make_schema, planned_store
from fault_support import LOCAL_TARGET, SERVICES, FakeServices, injector_for, write_faults
from gate_support import noting_check
from heal_support import READER, TARGET, HealServices, heal_injector, stale
from scheduler_support import FakeExecutor, attempts_of, build, key_of, settings_for

from expcore.campaign.attempts import is_charged
from expcore.campaign.factors import FactorSchema
from expcore.campaign.healthgate import DEFER, FOUND, HOLD, HealthGate
from expcore.errors import InfraError, is_treatable, uncharged_kinds
from expcore_onedata.admission import OUTAGE_RUN, FederationBusy, FederationLock, run_admission
from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.faults import INJECTOR_FAILURE, PROVIDER_OUTAGE, FaultInjector, load_faults

BUSY = "federation_busy"
RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
OTHER_RUN = {"run_key": "task=task-b|model=m1|seed=1", "attempt": 1}
SMALL = {"tasks": ("task-a",), "seeds": (1,)}
SCHEMA = make_schema()
KEY = key_of(SCHEMA, "api-none")
ONE = FactorSchema(SCHEMA.declared, SCHEMA.order, {"api-none": SCHEMA.conditions["api-none"]})  # a plan of one run
ONE_KEY = key_of(ONE, "api-none")


def platform_in(directory, **faults):
    """platform.toml with `[faults]` in a directory of its own, beside the campaign's."""
    directory.mkdir(parents=True, exist_ok=True)
    return write_faults(directory, **faults)


def federation_lock(platform):
    return FederationLock(load_faults(platform, load_platform(platform, require_secrets=False)))


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


def test_o11_a_wait_for_the_federation_that_runs_out_is_a_hold_that_costs_no_attempt(tmp_path):
    """O11: a run that waits for the federation past its bound while another holds it did nothing and failed at
    nothing: the wait ends as `federation_busy`, shared or exclusive, a hold that is never treatable and costs no
    attempt; never `injector_failure`."""
    lock = federation_lock(write_faults(tmp_path))
    holder = lock.acquire(exclusive=True)
    try:
        for exclusive in (False, True):
            with pytest.raises(FederationBusy) as caught:
                lock.acquire(exclusive=exclusive, timeout_s=0.1)
            assert caught.value.kind == BUSY
            assert "past the bound of the wait" in caught.value.detail
    finally:
        holder.close()
    assert BUSY in uncharged_kinds()
    assert not is_charged(BUSY)
    assert not is_treatable(BUSY)
    assert is_charged(INJECTOR_FAILURE)  # the injector's own failure is charged as before


def test_o11_an_injector_whose_own_wait_for_the_federation_runs_out_holds_and_takes_nothing_down(tmp_path):
    """O11: an injector whose run holds no admission waits for the federation itself; while another outage run holds
    it, that wait runs out as the hold `federation_busy`, not as `injector_failure`, and nothing is stopped or
    journalled for its target."""
    platform = write_faults(tmp_path, admission_timeout_s=0.2)
    services = FakeServices()
    first, second = injector_for(platform, services), injector_for(platform, services, target=LOCAL_TARGET)
    first.start(RUN)
    try:
        with pytest.raises(InfraError) as caught:
            second.start(OTHER_RUN)
        assert caught.value.kind == BUSY
        assert services.running(SERVICES["alpha"]) is True  # its own target was never touched
        assert ("stop", SERVICES["alpha"]) not in services.calls
        assert second.snapshot()["injections"] == []
    finally:
        assert first.finish()["ok"] is True


def test_o11_a_wait_for_the_federation_that_runs_out_inside_the_session_drain_is_federation_busy(tmp_path):
    """O11: after an outage run the federation admits nobody while its sessions drain, and a run's bounded wait that
    runs out inside the drain did nothing and failed at nothing, as one behind a holder of the lock: it is the hold
    `federation_busy`, never `injector_failure`, whichever way it waits (`acquire`, shared or exclusive, `held`,
    `admitted`, and an injector's own wait at its start, which takes nothing down)."""
    platform = write_faults(tmp_path, session_drain_s=30, admission_timeout_s=0.2)
    lock = federation_lock(platform)
    lock.hold_for(30)  # what an outage run's release leaves: the federation closed for its drain
    caught = []
    for exclusive in (False, True):
        with pytest.raises(FederationBusy) as acquired:
            lock.acquire(exclusive=exclusive, timeout_s=0.1)
        caught.append(acquired.value)
    with pytest.raises(FederationBusy) as held, lock.held(exclusive=False, timeout_s=0.1):
        pytest.fail("admitted inside the drain")
    caught.append(held.value)

    async def in_call():
        async with lock.admitted(exclusive=False, timeout_s=0.1):
            pytest.fail("admitted inside the drain")

    with pytest.raises(FederationBusy) as admitted:
        asyncio.run(in_call())
    caught.append(admitted.value)
    services = FakeServices()
    with pytest.raises(InfraError) as started:
        injector_for(platform, services).start(RUN)
    caught.append(started.value)
    assert [error.kind for error in caught] == [BUSY] * 5
    assert all("sessions drain" in error.detail for error in caught), [error.detail for error in caught]
    assert [call for call in services.calls if call[0] == "stop"] == []
    assert not is_charged(BUSY)


def test_o11_an_injector_start_that_cannot_wait_out_a_drain_recorded_after_its_journal_read_is_federation_busy(
    tmp_path, monkeypatch
):
    """O11: the injector's start reads the drain record again after the journal, and a record written in between (a
    lockless restore's) holds it back; a start whose wait for it runs out is the hold `federation_busy`, as a wait for
    the lock is, never `injector_failure`, and it takes nothing down."""
    platform = write_faults(tmp_path, session_drain_s=30, admission_timeout_s=0.2)
    services = FakeServices()
    injector = injector_for(platform, services)
    original = FaultInjector._recover

    def then_a_record(self, **kwargs):
        result = original(self, **kwargs)
        self._federation.hold_for(30)  # a restore that held no lock wrote its record after this start read the journal
        return result

    monkeypatch.setattr(FaultInjector, "_recover", then_a_record)
    with pytest.raises(InfraError) as caught:
        injector.start(RUN)
    assert caught.value.kind == BUSY
    assert "sessions drain" in caught.value.detail
    assert [call for call in services.calls if call[0] == "stop"] == []


def test_o11_a_recovery_whose_wait_for_the_federation_runs_out_is_federation_busy_and_restores_nothing(tmp_path):
    """O11: a recovery that holds no admission (a retried `recover` after its run let the federation go) waits for it
    to restore under, inside `admission_timeout_s`; while another run holds it that wait runs out as the hold
    `federation_busy`, never `injector_failure`, and nothing is restored."""
    platform = write_faults(tmp_path, session_drain_s=30, admission_timeout_s=0.2)
    lock = federation_lock(platform)
    holder = lock.acquire(exclusive=True)
    try:
        services = FakeServices()
        with pytest.raises(InfraError) as caught:
            injector_for(platform, services).recover()
        assert caught.value.kind == BUSY
        assert [call for call in services.calls if call[0] == "start"] == []
    finally:
        holder.close()


def test_o11_a_health_check_whose_wait_runs_out_inside_the_session_drain_defers_and_charges_no_one(tmp_path):
    """O11: a health check that cannot take the federation while it stays closed for an outage run's session drain
    waits for it as for any legitimate holder: it is deferred and charges no one, even once it outlasts `max_defer_s`,
    since the drain says nothing about the run that found it."""
    platform = write_faults(tmp_path, session_drain_s=30, health_wait_s=0.1)
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    federation_lock(platform).hold_for(30)  # what an outage run's release leaves
    health = FaultGate(settings, FakeServices())()
    assert (health.ok, health.deferred, health.charge) == (False, True, False), health.detail
    assert "drain" in health.detail


def waits_for_the_federation(lock):
    """An executor's behaviour that takes the federation inside its attempt (the in-call form of the admission) and
    raises what its wait raises."""

    async def behaviour(request):
        async with lock.admitted(exclusive=False, timeout_s=0.1):
            raise AssertionError("admitted while another run holds the federation")

    return behaviour


def test_o11_waits_for_the_federation_inside_attempts_cost_no_attempt_however_often(tmp_path):
    """O11: an attempt whose wait for the federation runs out, four times, more than `max_attempts`, while another
    run holds it, is set aside uncharged each time under the same attempt number, and the fifth runs: piled-up
    time-outs behind an outage run's heal never exhaust a run."""
    lock = federation_lock(platform_in(tmp_path / "platform"))
    executor = FakeExecutor({KEY: [waits_for_the_federation(lock)] * 4})
    holder = lock.acquire(exclusive=True)
    try:
        settings = settings_for(max_attempts=2)
        with build(tmp_path / "campaign", executor=executor, plan=SMALL, settings=settings) as scheduler:
            summary = scheduler.run()
            assert attempts_of(scheduler.store, KEY) == [("infra_fail", 0, 1)] * 4 + [("done", 0, 1)]
            assert scheduler.store.charged_attempts(KEY) == 0
            kinds = {
                row["infra_kind"]
                for row in scheduler.store.q("SELECT infra_kind FROM attempts WHERE run_key=?", (KEY,))
            }
            assert kinds == {BUSY, None}
            assert summary.statuses == {"done": 3}
    finally:
        holder.close()


def fails_to_inject(platform):
    """An executor's behaviour whose injector takes the federation itself and then fails to take its target down: the
    injector's own failure."""

    async def behaviour(request):
        injector_for(platform, FakeServices(stop_noop=True)).start(RUN)
        raise AssertionError("an injection that was never seen down was not refused")

    return behaviour


def test_o11_a_real_injector_failure_is_still_charged_and_exhausts_the_run(tmp_path):
    """O11: only the wait is a hold: an injector that took the federation and then could not take its target down
    fails as `injector_failure`, which is charged, so the run is exhausted at `max_attempts`."""
    platform = platform_in(tmp_path / "platform")
    executor = FakeExecutor({KEY: [fails_to_inject(platform)] * 4})
    with build(
        tmp_path / "campaign", executor=executor, plan=SMALL, settings=settings_for(max_attempts=2)
    ) as scheduler:
        summary = scheduler.run()
        assert attempts_of(scheduler.store, KEY) == [("infra_fail", 1, 1), ("infra_fail", 1, 2)]
        kinds = {
            row["infra_kind"] for row in scheduler.store.q("SELECT infra_kind FROM attempts WHERE run_key=?", (KEY,))
        }
        assert kinds == {INJECTOR_FAILURE}
        assert summary.statuses == {"done": 2, "infra_exhausted": 1}


def test_o11_a_run_waiting_behind_an_outage_runs_heal_holds_uncharged_and_starts_once_the_heal_lets_go(tmp_path):
    """O11, O35: an outage run holds the federation exclusively through its restore's heal. A baseline run of the
    campaign, admitted shared by the platform's run admission, waits behind it; each wait that runs out is the hold
    `federation_busy`, so it is left unstarted and asked again, never charged (`max_attempts` is 1, so one charge
    would exhaust it). Once the heal ends and the outage run lets the federation go, the baseline run is admitted,
    runs once and is done."""
    federation = tmp_path / "federation"
    injector, services = heal_injector(federation, stale(), heal="all_links", admission_timeout_s=0.2)
    config = load_platform(federation / "platform.toml", require_secrets=False)
    admit = run_admission(config)
    refusals: list[str] = []
    healing, asked_enough, released = threading.Event(), threading.Event(), []

    @contextlib.contextmanager
    def counted(kinds, targets, cancel, exclusive):
        with contextlib.ExitStack() as stack:
            try:
                mode = stack.enter_context(admit(kinds, targets, cancel, exclusive))
            except InfraError as refusal:
                refusals.append(refusal.kind)
                if len(refusals) >= 3:
                    asked_enough.set()
                raise
            yield mode

    def hold_the_heal(reader):
        if not healing.is_set():  # the heal's first ping: the federation is held exclusively for the whole heal
            healing.set()
            asked_enough.wait(30)

    services.on_ping = hold_the_heal

    def outage_run():
        with admit((PROVIDER_OUTAGE,), {PROVIDER_OUTAGE: TARGET}, threading.Event(), False):
            injector.start(RUN)
            verified = injector.clear()
        released.append(time.monotonic())
        return verified

    thread, landed = in_thread(outage_run)
    assert healing.wait(30)
    started: list[float] = []

    async def baseline(request):
        started.append(time.monotonic())
        return await executor._run(request, "finish")

    executor = FakeExecutor({ONE_KEY: [baseline]})
    settings = settings_for(max_attempts=1)
    journal = load_faults(federation / "platform.toml", config).journal
    with build(
        tmp_path / "campaign",
        schema=ONE,
        executor=executor,
        plan=SMALL,
        settings=settings,
        health=noting_check(journal),  # the platform's admission needs a check: it notes the epoch, as the gate does
        admission=counted,
    ) as scheduler:
        summary = scheduler.run()
        assert attempts_of(scheduler.store, ONE_KEY) == [("done", 0, 1)]
        assert scheduler.store.charged_attempts(ONE_KEY) == 0
        unstarted = [r["detail"] for r in scheduler.store.q("SELECT detail FROM events WHERE kind='run_unstarted'")]
    thread.join(30)
    assert landed == [True]  # the heal mended every pair and the restore was verified
    assert len(refusals) >= 3
    assert set(refusals) == {BUSY}
    assert len(unstarted) == len(refusals)
    assert all("no attempt used" in detail for detail in unstarted)
    assert summary.statuses == {"done": 1}
    assert started[0] >= released[0]  # it ran only once the outage run had let the federation go


def test_o11_a_health_check_waiting_behind_a_heal_in_progress_defers_and_charges_no_one(tmp_path):
    """O11, O35: a heal journals its marker (`clear_failed` with `healing`) before its first action, so while it runs
    the injection reads as `clear_failed`. A health check whose wait runs out then waits behind a legitimate holder,
    no restore having failed yet: it is deferred and charges no one. Once the heal has failed (a reader gave no
    pong), the same wait finds the failed restore, as before."""
    federation = tmp_path / "federation"
    services = HealServices()
    services.pong[READER] = False
    injector, services = heal_injector(federation, services, heal="link", health_wait_s=0.1)
    settings = load_faults(
        federation / "platform.toml", load_platform(federation / "platform.toml", require_secrets=False)
    )
    readings = []
    services.on_ping = lambda reader: readings.append(FaultGate(settings, FakeServices())())
    injector.start(RUN)
    assert injector.clear() is False  # the heal failed closed, and the injector still holds the federation
    during, after = readings[0], FaultGate(settings, FakeServices())()
    assert (during.ok, during.deferred, during.charge) == (False, True, False), during.detail
    assert (after.ok, after.deferred) == (False, False), after.detail
    assert TARGET in after.detail


def test_o11_a_check_deferred_past_its_bound_behind_an_outage_run_closes_the_gate_and_charges_no_one(tmp_path):
    """O11: an outage run with a heal can hold the federation longer than `max_defer_s`. A pre-run check deferred
    past it closes the gate blocking, as for a holder that may never let go, but the run that found it is held, not
    charged: the wait was contention, so the platform's deferral charges no one."""
    platform = platform_in(tmp_path / "federation", health_wait_s=0.1)
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    store, schema, manifest = planned_store(campaign)
    stamps = iter(f"2026-01-01T{hour:02d}:{minute:02d}:00Z" for hour in range(24) for minute in range(0, 60, 10))
    gate = HealthGate(store, FaultGate(settings, FakeServices()), lambda: next(stamps), max_defer_s=1800)
    gate.load()
    key = keys_under(schema, manifest, "mount-read")[0]
    outage = FederationLock(settings).acquire(exclusive=True, purpose=OUTAGE_RUN, target=TARGET)
    try:
        verdicts = [gate.before_run(key).verdict for _ in range(8)]
    finally:
        outage.close()
    assert verdicts[0] == DEFER
    assert FOUND not in verdicts
    assert verdicts[-1] == HOLD
    assert gate.closed
    assert gate.blocking
    assert store.q("SELECT COUNT(*) FROM attempts")[0][0] == 0
    closing = [r["detail"] for r in store.q("SELECT detail FROM events") if "past max_defer_s" in r["detail"]]
    assert closing
    assert "no run is charged" in closing[0]
