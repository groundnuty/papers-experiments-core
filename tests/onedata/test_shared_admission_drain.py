"""O48: a tool outside a campaign is admitted the way a campaign run is, so it meets the session drain and the uncharged
hold `federation_busy`.

An outage run's release closes the federation for `[faults] session_drain_s` (`federation.drain`), and every admission
waits for that record inside its bound; one whose bound runs out is `FederationBusy`, the hold `federation_busy`, which
charges nothing. `shared_admission` goes through the same `FederationLock.held` (`admitted` for the async form), so a
tool waits for the drain too, and its time-out is the same uncharged hold. Every test runs against the real lock and
journal on the fixture's directory.
"""

import asyncio
import dataclasses
import time

import pytest
from fault_support import OUTAGE_TARGET, SERVICES, write_faults

from expcore.errors import InfraError
from expcore_onedata.admission import FederationBusy, FederationLock, current_admission
from expcore_onedata.config import load_platform
from expcore_onedata.fault_journal import DRAIN, InjectionJournal, holder
from expcore_onedata.faults import INJECTOR_FAILURE, load_faults
from expcore_onedata.sandbox.kinds import FEDERATION_BUSY
from expcore_onedata.shared_holds import SharedHolds
from expcore_onedata.tool_admission import shared_admission, shared_admission_async

WAIT_S = 10.0  # a bound no test should reach
PROBE_S = 0.2  # a bound that runs out
DRAIN_S = 3.0  # the session drain an outage run's release leaves in these tests: long enough to outlast a slow fsync
REASON = "smoke_warmup"


def federation(tmp_path, drain_s=DRAIN_S):
    """The platform's configuration and its `[faults]` settings, with a drain of `drain_s`."""
    platform = write_faults(tmp_path)
    config = load_platform(platform, require_secrets=False)
    settings = dataclasses.replace(load_faults(platform, config), session_drain_s=drain_s)
    return config, settings


def drained_outage(settings):
    """An outage run that restored its provider and let the federation go: its armed drain is recorded on release.
    Returns the wall-clock time the record holds."""
    outage = FederationLock(settings).acquire(exclusive=True, timeout_s=PROBE_S, target=OUTAGE_TARGET)
    outage.drain.arm()
    outage.close()
    until = InjectionJournal(settings.journal).read_drain()
    assert until is not None
    assert until > time.time()
    return until


def test_o48_a_tool_waits_for_the_session_drain_an_outage_run_left(tmp_path):
    """O48: a tool that arrives just after an outage run let the federation go waits until its session drain has
    passed, as a campaign run does, and only then holds the federation and records its hold."""
    config, settings = federation(tmp_path)
    until = drained_outage(settings)
    with shared_admission(config, reason=REASON, timeout_s=WAIT_S) as admission:
        assert time.time() >= until  # admitted only once the drain record had passed
        assert admission is not None
        assert admission.held
    (hold,) = SharedHolds(settings.journal).holds()
    assert hold["reason"] == REASON


def test_o48_a_tools_bound_that_ends_inside_the_drain_is_the_uncharged_federation_busy(tmp_path):
    """O48: a tool whose bound runs out inside an outage run's session drain is `FederationBusy`, the hold
    `federation_busy` that costs a campaign run nothing, never an `injector_failure`; it holds and records nothing."""
    config, settings = federation(tmp_path, drain_s=WAIT_S)
    drained_outage(settings)
    with pytest.raises(FederationBusy) as caught, shared_admission(config, reason=REASON, timeout_s=PROBE_S):
        raise AssertionError("a tool was admitted inside an outage run's session drain")
    assert caught.value.kind == FEDERATION_BUSY
    assert "drain" in caught.value.detail
    assert current_admission() is None
    assert SharedHolds(settings.journal).holds() == []


def test_o48_a_tools_wait_for_an_outage_run_that_runs_out_is_the_uncharged_federation_busy(tmp_path):
    """O48: a tool whose bound runs out while an outage run holds the federation is the same uncharged hold
    `federation_busy` a campaign run's wait ends in."""
    config, settings = federation(tmp_path)
    outage = FederationLock(settings).acquire(exclusive=True, timeout_s=PROBE_S, target=OUTAGE_TARGET)
    try:
        with pytest.raises(FederationBusy) as caught, shared_admission(config, reason=REASON, timeout_s=PROBE_S):
            raise AssertionError("a tool was admitted beside an outage run")
    finally:
        outage.close()
    assert caught.value.kind == FEDERATION_BUSY
    assert SharedHolds(settings.journal).holds() == []


def test_o48_a_drain_record_that_cannot_be_read_refuses_the_tool(tmp_path):
    """O48: a drain record that is not one refuses the tool as `injector_failure`, as it refuses a campaign run: no
    admission can tell the federation is open."""
    config, settings = federation(tmp_path)
    settings.journal.mkdir(parents=True, exist_ok=True)
    (settings.journal / DRAIN).write_text("not a record", encoding="utf-8")
    with pytest.raises(InfraError) as caught, shared_admission(config, reason=REASON, timeout_s=PROBE_S):
        raise AssertionError("a tool was admitted on an unreadable drain record")
    assert caught.value.kind == INJECTOR_FAILURE
    assert SharedHolds(settings.journal).holds() == []


def test_o48_the_async_form_waits_for_the_drain(tmp_path):
    """O48: `shared_admission_async` waits for the session drain as the synchronous form does."""
    config, settings = federation(tmp_path)
    until = drained_outage(settings)

    async def drive():
        async with shared_admission_async(config, reason=REASON, timeout_s=WAIT_S) as admission:
            return time.time(), admission.held

    admitted_at, held = asyncio.run(drive())
    assert admitted_at >= until
    assert held


def test_o48_the_async_forms_bound_that_ends_inside_the_drain_is_the_uncharged_federation_busy(tmp_path):
    """O48: the async form's bound that runs out inside the drain is the uncharged hold `federation_busy`."""
    config, settings = federation(tmp_path, drain_s=WAIT_S)
    drained_outage(settings)

    async def drive():
        async with shared_admission_async(config, reason=REASON, timeout_s=PROBE_S):
            raise AssertionError("a tool was admitted inside an outage run's session drain")

    with pytest.raises(FederationBusy) as caught:
        asyncio.run(drive())
    assert caught.value.kind == FEDERATION_BUSY
    assert SharedHolds(settings.journal).holds() == []


# --- a nested hold reuses its context's admission, checked as any admission is ------------------------------------


def test_o48_a_nested_tool_waits_out_a_drain_recorded_since_the_outer_hold_began(tmp_path):
    """O48: a tool entered inside another's hold reuses it, but a drain recorded since the outer hold began (a restore
    that held no admission) keeps it out as it keeps a fresh admission out: past its bound it is the uncharged
    `federation_busy`, and the outer hold is still held."""
    config, settings = federation(tmp_path)
    with shared_admission(config, reason=REASON, timeout_s=WAIT_S) as outer:
        FederationLock(settings).hold_for(WAIT_S)
        with pytest.raises(FederationBusy) as caught, shared_admission(config, reason=REASON, timeout_s=PROBE_S):
            raise AssertionError("a nested tool was admitted inside a session drain")
        assert caught.value.kind == FEDERATION_BUSY
        assert outer.held
        assert outer.shares.count == 1  # the nested try gave its share back


def test_o48_a_nested_tool_is_refused_while_an_injection_is_open(tmp_path):
    """O48: a nested tool is refused as `injector_failure` when the journal holds an open injection, as a fresh one
    is."""
    config, settings = federation(tmp_path)
    with shared_admission(config, reason=REASON, timeout_s=WAIT_S) as outer:
        line = {
            "event": "injecting",
            "id": "i1",
            "target": OUTAGE_TARGET,
            "service": SERVICES["beta"],
            "holder": holder(),
        }
        InjectionJournal(settings.journal).append(line)
        with pytest.raises(InfraError) as caught, shared_admission(config, reason=REASON, timeout_s=PROBE_S):
            raise AssertionError("a nested tool was admitted beside an open injection")
        assert caught.value.kind == INJECTOR_FAILURE
        assert outer.shares.count == 1


def test_o48_a_nested_async_tool_waits_out_a_drain_recorded_since_the_outer_hold_began(tmp_path):
    """O48: the async form's nested hold waits for a drain as the synchronous form's does."""
    config, settings = federation(tmp_path)

    async def drive():
        async with shared_admission_async(config, reason=REASON, timeout_s=WAIT_S) as outer:
            FederationLock(settings).hold_for(WAIT_S)
            with pytest.raises(FederationBusy) as caught:
                async with shared_admission_async(config, reason=REASON, timeout_s=PROBE_S):
                    raise AssertionError("a nested tool was admitted inside a session drain")
            return caught.value, outer.shares.count

    busy, shares = asyncio.run(drive())
    assert busy.kind == FEDERATION_BUSY
    assert shares == 1


def test_o48_a_nested_async_tool_whose_wait_cannot_start_gives_its_share_back(tmp_path, monkeypatch):
    """O48: a nested async hold whose check cannot even start (the event loop's executor refuses it) gives back the
    share it took, so the outer hold still lets the federation go when its owner leaves."""
    config, _settings = federation(tmp_path)

    async def refused_thread(*_args, **_kwargs):
        raise RuntimeError("the executor is shut down")

    async def drive():
        async with shared_admission_async(config, reason=REASON, timeout_s=WAIT_S) as outer:
            with monkeypatch.context() as patch:
                patch.setattr(asyncio, "to_thread", refused_thread)
                with pytest.raises(RuntimeError):
                    async with shared_admission_async(config, reason=REASON, timeout_s=PROBE_S):
                        raise AssertionError("a nested tool was admitted without its check")
            return outer, outer.shares.count

    outer, shares = asyncio.run(drive())
    assert shares == 1
    assert not outer.held
