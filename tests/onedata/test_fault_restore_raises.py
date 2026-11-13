"""O11: a restore that is not verified is never passed silently. The exit of a session raises `RestoreNotVerified`, the
journal keeps the provider marked unhealthy for the next gate, and a body's own exception is never lost.

Every test runs against `FakeServices`, whose `never_serves` knob is a provider that never comes back to serving.
"""

import asyncio
import json
import threading

import pytest
from fault_support import OUTAGE_TARGET, SERVICES, FakeServices, injector_for, write_faults
from record_support import hand_made

from expcore.errors import InfraError
from expcore.records.instruments import finish_instruments
from expcore_onedata.admission import FederationLock
from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.fault_journal import JOURNAL
from expcore_onedata.faults import INJECTOR_FAILURE, RestoreNotVerified, load_faults

SERVICE = SERVICES["beta"]
RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}


def journal_lines(tmp_path):
    path = tmp_path / "fault-journal" / JOURNAL
    return [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def open_injections(tmp_path):
    lines = journal_lines(tmp_path)
    cleared = {line["id"] for line in lines if line["event"] == "cleared"}
    return [line for line in lines if line["event"] == "injecting" and line["id"] not in cleared]


def settings_of(platform):
    return load_faults(platform, load_platform(platform, require_secrets=False))


def federation_is_exclusive(platform):
    """Whether the federation is held against a shared holder, as an outage run's exclusive admission holds it."""
    taken = FederationLock(settings_of(platform)).try_acquire(exclusive=False)
    if taken is not None:
        taken.close()
    return taken is None


def unserving(tmp_path, **changes):
    """A platform and an injector whose provider comes back with a new identity and never serves."""
    platform = write_faults(tmp_path, **changes)
    return platform, injector_for(platform, FakeServices(never_serves=True))


class StopThenBreakAndNeverStart(FakeServices):
    """A driver whose stop takes effect and then raises, and whose start never works: nothing can bring it back."""

    def stop(self, service):
        super().stop(service)
        raise RuntimeError("the driver broke after stopping")


def in_session(injector, body=None):
    """Run `body`, when there is one, inside the injector's session."""
    with injector.session(RUN):
        if body is not None:
            body()


def breaks(error):
    """A body that raises `error`."""

    def body():
        raise error

    return body


# --- the session ------------------------------------------------------------------------------------------------


def test_o11_a_session_whose_provider_never_serves_again_raises_and_leaves_the_provider_marked_unhealthy(tmp_path):
    """O11: the restore changed the identity but the provider never served within the bound: the exit raises
    `RestoreNotVerified`, an `injector_failure` that names the label, what was checked and why it failed, never the
    service; the journal keeps the injection open, journalled `clear_failed`, and the federation stays held."""
    platform, injector = unserving(tmp_path)
    ran = []
    with pytest.raises(RestoreNotVerified) as caught:
        in_session(injector, lambda: ran.append(True))
    assert ran == [True]
    error = caught.value
    assert isinstance(error, InfraError)
    assert (error.kind, error.target) == (INJECTOR_FAILURE, OUTAGE_TARGET)
    assert (error.how, error.identity_changed, error.serving) == ("not_serving", True, False)
    assert OUTAGE_TARGET in error.detail
    assert "not_serving" in error.detail
    assert SERVICE not in str(error)
    (still,) = open_injections(tmp_path)
    failed = [line for line in journal_lines(tmp_path) if line["event"] == "clear_failed"]
    assert [(line["id"], line["how"]) for line in failed] == [(still["id"], "not_serving")]
    assert injector.snapshot()["ok"] is False
    assert federation_is_exclusive(platform) is True


def test_o11_the_next_gate_sees_the_restore_a_session_raised_for(tmp_path):
    """O11: the failed restore stays in the journal, so the platform's health gate fails for it, by the label and how it
    failed, and another injector's start is refused as `injector_failure` instead of running on a half-restored
    federation."""
    platform, injector = unserving(tmp_path, health_wait_s=0.2)
    with pytest.raises(RestoreNotVerified):
        in_session(injector)
    health = FaultGate(settings_of(platform), FakeServices())()
    assert (health.ok, health.deferred) == (False, False)
    assert OUTAGE_TARGET in health.detail
    assert "not_serving" in health.detail
    with pytest.raises(InfraError) as refused:
        injector_for(platform, FakeServices()).start({**RUN, "attempt": 2})
    assert refused.value.kind == INJECTOR_FAILURE


def test_o11_a_body_exception_and_a_failed_restore_both_surface(tmp_path):
    """O11: the body raised and the restore failed too: the restore failure is raised from the body's exception, which
    stays its cause, so neither is lost."""
    _, injector = unserving(tmp_path)
    with pytest.raises(RestoreNotVerified) as caught:
        in_session(injector, breaks(RuntimeError("the trial broke")))
    assert type(caught.value.__cause__) is RuntimeError
    assert str(caught.value.__cause__) == "the trial broke"
    assert len(open_injections(tmp_path)) == 1


def test_o11_an_interrupt_in_the_body_is_kept_and_carries_the_failed_restore_as_a_note(tmp_path):
    """O11: an interrupt or an exit is never replaced by another exception: it is raised as it was, with the restore
    failure in its notes, and the injection stays open in the journal."""
    _, injector = unserving(tmp_path)
    with pytest.raises(KeyboardInterrupt) as caught:
        in_session(injector, breaks(KeyboardInterrupt()))
    assert any(OUTAGE_TARGET in note and "not_serving" in note for note in caught.value.__notes__)
    assert len(open_injections(tmp_path)) == 1


def test_o11_a_start_that_failed_and_cannot_restore_raises_the_restore_failure_from_the_start_failure(tmp_path):
    """O11: the stop took the provider down and raised, and the provider cannot be started again: the start failed as
    `injector_failure` and its clear failed too. The session raises the restore failure, from the start's failure."""
    services = StopThenBreakAndNeverStart(start_fails=True)
    injector = injector_for(write_faults(tmp_path), services)
    with pytest.raises(RestoreNotVerified) as caught:
        in_session(injector, breaks(AssertionError("the body must not run when the injection failed")))
    assert (caught.value.kind, caught.value.how) == (INJECTOR_FAILURE, "start_failed")
    assert isinstance(caught.value.__cause__, InfraError)
    assert "could not be taken down" in caught.value.__cause__.detail
    assert len(open_injections(tmp_path)) == 1


def test_o11_a_verified_restore_leaves_the_exit_of_a_session_as_it_was(tmp_path):
    """O11: a restore that is verified raises nothing, leaves nothing open and reads `ok` in the snapshot."""
    injector = injector_for(write_faults(tmp_path), FakeServices())
    in_session(injector)
    assert open_injections(tmp_path) == []
    snapshot = injector.finish()
    assert snapshot["ok"] is True
    injector.check()  # a verified restore passes the post-run check


def test_o11_a_body_exception_with_a_verified_restore_is_raised_unchanged(tmp_path):
    """O11: the body's exception reaches the caller itself, with no restore failure attached, when the restore held."""
    injector = injector_for(write_faults(tmp_path), FakeServices())
    with pytest.raises(RuntimeError, match="the trial broke") as caught:
        in_session(injector, breaks(RuntimeError("the trial broke")))
    assert caught.value.__cause__ is None
    assert getattr(caught.value, "__notes__", []) == []
    assert open_injections(tmp_path) == []


# --- the async form -----------------------------------------------------------------------------------------------


def test_o11_running_raises_when_the_restore_is_not_verified(tmp_path):
    """O11: `running`, the async form, raises `RestoreNotVerified` at its exit as `session` does."""
    _, injector = unserving(tmp_path)

    async def scenario():
        async with injector.running(RUN):
            pass

    with pytest.raises(RestoreNotVerified) as caught:
        asyncio.run(scenario())
    assert (caught.value.kind, caught.value.target, caught.value.how) == (
        INJECTOR_FAILURE,
        OUTAGE_TARGET,
        "not_serving",
    )
    assert caught.value.__cause__ is None
    assert len(open_injections(tmp_path)) == 1


def test_o11_running_chains_the_restore_failure_to_what_the_body_raised(tmp_path):
    """O11: the body of `running` raised and the restore failed too: the restore failure is raised from the body's
    exception."""
    _, injector = unserving(tmp_path)

    async def scenario():
        async with injector.running(RUN):
            raise RuntimeError("the trial broke")

    with pytest.raises(RestoreNotVerified) as caught:
        asyncio.run(scenario())
    assert type(caught.value.__cause__) is RuntimeError
    assert str(caught.value.__cause__) == "the trial broke"


def test_o11_a_cancelled_run_stays_cancelled_and_carries_the_failed_restore_as_a_note(tmp_path):
    """O11: the cancellation of a run is raised again as it was, never replaced, with the restore failure noted."""
    _, injector = unserving(tmp_path)

    async def scenario():
        async def run():
            async with injector.running(RUN):
                await asyncio.sleep(3600)

        task = asyncio.create_task(run())
        for _ in range(500):  # until the injection is made, which a worker thread does
            if injector.snapshot()["injections"] and injector.snapshot()["injections"][0]["injected"]:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        return caught.value

    cancelled = asyncio.run(scenario())
    assert any(OUTAGE_TARGET in note and "not_serving" in note for note in cancelled.__notes__)
    assert len(open_injections(tmp_path)) == 1


def cancelled_during_the_clear(tmp_path, *, cancel_wait_s=None, body_raises=False):
    """Run `running` over a provider that never serves, with the clear held in its worker thread by the driver, cancel
    the task while the clear runs, let the clear go (after the cancellation's wait, when it is bounded), and return the
    cancellation raised and the injector."""
    release, held = threading.Event(), threading.Event()
    services = FakeServices(never_serves=True)

    def hold(service):
        held.set()
        release.wait(10)

    services.on_serves = hold
    injector = injector_for(write_faults(tmp_path), services)

    async def scenario():
        async def run():
            async with injector.running(RUN, cancel_wait_s=cancel_wait_s):
                if body_raises:
                    raise RuntimeError("the trial broke")

        task = asyncio.create_task(run())
        await asyncio.to_thread(held.wait, 10)
        task.cancel()
        if cancel_wait_s is None:
            threading.Timer(0.2, release.set).start()  # the clear finishes only after the cancellation
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        release.set()
        return caught.value

    return asyncio.run(scenario()), injector


def test_o11_a_run_cancelled_while_it_clears_carries_the_failed_restore_as_a_note(tmp_path):
    """O11: the body returned and the task was cancelled during the clear, which finished inside the wait with the
    restore not verified: the cancellation is raised as it was, with the restore failure in its notes."""
    cancelled, _ = cancelled_during_the_clear(tmp_path)
    assert any(OUTAGE_TARGET in note and "not_serving" in note for note in cancelled.__notes__)
    assert len(open_injections(tmp_path)) == 1


def test_o11_a_second_cancellation_during_the_clear_of_a_cancelled_body_carries_the_failed_restore_too(tmp_path):
    """O11: the body raised, and the clear that follows is cancelled too: the second cancellation notes the failure."""
    cancelled, _ = cancelled_during_the_clear(tmp_path, body_raises=True)
    assert any(OUTAGE_TARGET in note and "not_serving" in note for note in cancelled.__notes__)


def test_o11_a_clear_still_running_when_the_wait_for_it_ends_is_noted_as_not_verified(tmp_path):
    """O11: the wait for the clear is bounded; when it ends with the clear still running, the cancellation says so
    without taking the injector's lock, and the injection stays open."""
    cancelled, _ = cancelled_during_the_clear(tmp_path, cancel_wait_s=0.05)
    assert any(OUTAGE_TARGET in note and "still running" in note for note in cancelled.__notes__)
    assert len(open_injections(tmp_path)) == 1


def test_o11_running_with_a_verified_restore_raises_nothing(tmp_path):
    """O11: a verified restore leaves the exit of `running` as it was."""
    injector = injector_for(write_faults(tmp_path), FakeServices())

    async def scenario():
        async with injector.running(RUN):
            pass

    asyncio.run(scenario())
    assert injector.snapshot()["ok"] is True
    assert open_injections(tmp_path) == []


# --- the instrument's own route -----------------------------------------------------------------------------------


def test_o11_the_post_run_check_refuses_an_attempt_whose_restore_was_not_verified(tmp_path):
    """O11: an executor that starts the injector as an instrument and clears it in `finally`, ignoring `clear`'s False,
    gets the refusal from `finish_instruments`: the record keeps its snapshot (restore not verified) and holds the
    injector's refusal, an untreatable `injector_failure`."""
    _, injector = unserving(tmp_path)
    injector.start(RUN)
    assert injector.clear() is False  # what a caller that ignores the answer sees
    record = finish_instruments(hand_made(), [injector])
    assert record.instrument_refusal["namespace"] == "fault_injector"
    assert record.instrument_refusal["infra_kind"] == INJECTOR_FAILURE
    assert OUTAGE_TARGET in record.instrument_refusal["detail"]
    assert record.instruments["fault_injector"]["ok"] is False
    assert record.instruments["fault_injector"]["injections"][0]["injected"] is True


def test_o11_the_post_run_check_passes_for_a_verified_restore_and_for_an_injector_never_started(tmp_path):
    """O11: nothing is refused for an injector that took nothing down, or whose restore was verified."""
    injector = injector_for(write_faults(tmp_path), FakeServices())
    injector.check()
    injector.start(RUN)
    injector.finish()
    injector.check()


def test_o11_the_post_run_check_passes_for_a_start_that_took_nothing_down(tmp_path):
    """O11: a stop that had no effect fails the start and leaves the provider as it was, with nothing to restore."""
    injector = injector_for(write_faults(tmp_path), FakeServices(stop_noop=True))
    with pytest.raises(InfraError):
        injector.start(RUN)
    injector.check()
