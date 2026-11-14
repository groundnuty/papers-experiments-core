"""O11: a restore that is not verified is never passed silently. The exit of a session raises `RestoreNotVerified`; the
exit of `running`, which an executor holds its record inside, returns, and the injector's reading in the record carries
`restore_verified: false` for the campaign to refuse to score. The journal keeps the provider marked unhealthy for the
next gate either way, and a body's own exception is never lost.

Every test runs against `FakeServices`, whose `never_serves` knob is a provider that never comes back to serving.
"""

import asyncio
import json
import threading
import time

import pytest
from fault_support import OUTAGE_TARGET, SERVICES, FakeServices, injector_for, write_faults
from record_support import hand_made

from expcore.errors import InfraError
from expcore.records.injections import INJECTOR_FAILURE, restore_refusal
from expcore.records.instruments import finish_instruments
from expcore_onedata.admission import FederationLock
from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.fault_journal import JOURNAL
from expcore_onedata.faults import RestoreNotVerified, load_faults
from expcore_onedata.sandbox.kinds import INGEST_HELD

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
    assert snapshot["restore_verified"] is True
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


def test_o11_running_returns_when_the_restore_is_not_verified_so_the_executor_keeps_its_record(tmp_path):
    """O11: `running` does not raise for a restore that is not verified: an executor that records the run inside the
    block (`record = await record_run(...)`) keeps that record. The journal still holds the injection open and
    journalled `clear_failed`, so the gate and the next start refuse the provider."""
    platform, injector = unserving(tmp_path)

    async def scenario():
        async with injector.running(RUN):
            record = hand_made()  # what `record_run` returns inside the block
        return record

    record = asyncio.run(scenario())  # returned, not raised
    assert record.run_key == hand_made().run_key
    (still,) = open_injections(tmp_path)
    failed = [line for line in journal_lines(tmp_path) if line["event"] == "clear_failed"]
    assert [(line["id"], line["how"]) for line in failed] == [(still["id"], "not_serving")]
    assert federation_is_exclusive(platform) is True


def test_o11_the_record_of_a_running_whose_restore_is_not_verified_carries_the_failure(tmp_path):
    """O11: the reading the record keeps says the restore was not verified, with how it ended and what was checked,
    whether it is taken from the snapshot or put in by `finish_instruments`, so the failure is in the record itself and
    does not depend on the exception the exit no longer raises."""
    _, injector = unserving(tmp_path)

    async def scenario():
        async with injector.running(RUN):
            return hand_made()

    record = finish_instruments(asyncio.run(scenario()), [injector])
    reading = record.instruments["fault_injector"]
    assert reading["restore_verified"] is False
    assert (reading["how"], reading["identity_changed"], reading["serving"]) == ("not_serving", True, False)
    assert reading["ok"] is False
    assert SERVICE not in json.dumps(reading)


def test_o11_a_reading_that_records_the_restore_not_verified_is_a_record_that_is_set_aside(tmp_path):
    """O11: the core reads the injector's reading as the platform writes it: a record that carries it is set aside,
    so `rescore` hands it back unscored and the campaign sets the attempt aside as `injector_failure`."""
    _, injector = unserving(tmp_path)

    async def scenario():
        async with injector.running(RUN):
            return hand_made()

    record = finish_instruments(asyncio.run(scenario()), [injector])
    refusal = restore_refusal(record.instruments)
    assert refusal is not None
    assert (refusal["namespace"], refusal["infra_kind"]) == ("fault_injector", INJECTOR_FAILURE)
    assert OUTAGE_TARGET in refusal["detail"]
    assert "not_serving" in refusal["detail"]
    assert SERVICE not in refusal["detail"]
    assert record.set_aside is True


def test_o11_a_session_records_the_same_reading_in_its_snapshot(tmp_path):
    """O11: `session` raises for the restore, and the snapshot it leaves says the same as `running`'s."""
    _, injector = unserving(tmp_path)
    with pytest.raises(RestoreNotVerified):
        in_session(injector)
    reading = injector.snapshot()
    assert reading["restore_verified"] is False
    assert (reading["how"], reading["identity_changed"], reading["serving"]) == ("not_serving", True, False)


def test_o11_a_start_that_failed_and_cannot_restore_records_the_restore_not_verified(tmp_path):
    """O11: the provider may be down after a start that stopped it and then broke, so the reading says so even though
    the injection was never seen down."""
    injector = injector_for(write_faults(tmp_path), StopThenBreakAndNeverStart(start_fails=True))
    with pytest.raises(RestoreNotVerified):
        in_session(injector)
    reading = injector.snapshot()
    assert reading["injections"][0]["injected"] is False
    assert (reading["restore_verified"], reading["how"]) == (False, "start_failed")


def test_o11_an_injector_that_took_nothing_down_records_no_restore_at_all(tmp_path):
    """O11: `restore_verified` is the reading of a restore that was needed: it is absent before a start and after a
    start whose stop had no effect, so a record is never told that a restore that was not needed was verified."""
    injector = injector_for(write_faults(tmp_path), FakeServices(stop_noop=True))
    assert "restore_verified" not in injector.snapshot()
    with pytest.raises(InfraError):
        injector.start(RUN)
    assert "restore_verified" not in injector.snapshot()
    assert restore_refusal({"fault_injector": injector.snapshot()}) is None


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


def test_o11_a_cancellation_during_the_clear_after_a_body_that_raised_carries_the_failed_restore_too(tmp_path):
    """O11: the body raised, and the task is cancelled during the clear that follows: the cancellation, which replaces
    the body's exception as the one raised, notes the restore failure."""
    cancelled, _ = cancelled_during_the_clear(tmp_path, body_raises=True)
    assert any(OUTAGE_TARGET in note and "not_serving" in note for note in cancelled.__notes__)


def test_o11_a_clear_that_had_not_finished_when_the_wait_for_it_ends_is_noted_as_not_verified(tmp_path):
    """O11: the wait for the clear is bounded; when it ends with the clear still running, the cancellation says so
    and the injection stays open."""
    cancelled, _ = cancelled_during_the_clear(tmp_path, cancel_wait_s=0.05)
    assert any(OUTAGE_TARGET in note and "had not finished" in note for note in cancelled.__notes__)
    assert len(open_injections(tmp_path)) == 1


def test_o11_the_note_for_a_cancelled_clear_never_waits_for_the_injectors_lock(tmp_path):
    """O11: a clear task that was itself cancelled (the event loop's shutdown cancels every task) says nothing of its
    worker thread, which may still hold the injector's lock: the note is made without taking it, so the bound holds."""
    _, injector = unserving(tmp_path)
    held, release = threading.Event(), threading.Event()

    def hold_the_lock():
        with injector._mutex:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold_the_lock)
    holder.start()
    held.wait(10)

    async def scenario():
        clearing = asyncio.get_running_loop().create_future()
        clearing.cancel()
        error = asyncio.CancelledError()
        threading.Timer(3, release.set).start()  # a way out for a note that blocks
        started = time.monotonic()
        injector._note_clear(error, clearing)
        return error, time.monotonic() - started

    error, waited_s = asyncio.run(scenario())
    release.set()
    holder.join()
    assert any("had not finished" in note for note in error.__notes__)
    assert waited_s < 2


def test_o11_running_with_a_verified_restore_raises_nothing_and_records_it_verified(tmp_path):
    """O11: a verified restore leaves the exit of `running` as it was, and the reading says `restore_verified: true`,
    with none of the failure's fields."""
    injector = injector_for(write_faults(tmp_path), FakeServices())

    async def scenario():
        async with injector.running(RUN):
            pass

    asyncio.run(scenario())
    reading = injector.snapshot()
    assert reading["ok"] is True
    assert reading["restore_verified"] is True
    assert not {"how", "identity_changed", "serving"} & set(reading)
    assert open_injections(tmp_path) == []
    record = finish_instruments(hand_made(), [injector])
    assert restore_refusal(record.instruments) is None
    assert record.set_aside is False


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


@pytest.mark.parametrize("exit_by", ["session", "running"])
def test_o45_a_held_start_in_the_body_stays_the_hold_when_the_restore_fails_too(tmp_path, exit_by):
    """O45: the ingest's start is held back inside the injector's session (its baseline's mark refused it) and the
    restore then fails: the hold is raised as it was, with the restore failure in its notes, so the attempt costs no
    attempt; the injection stays open in the journal, so the health gate still fails for it."""
    _, injector = unserving(tmp_path)
    held = InfraError(INGEST_HELD, "the progressive ingest held its start back")

    async def scenario():
        async with injector.running(RUN):
            raise held

    def run():
        if exit_by == "session":
            in_session(injector, breaks(held))
        else:
            asyncio.run(scenario())

    with pytest.raises(InfraError) as caught:
        run()
    assert caught.value is held
    assert not isinstance(caught.value, RestoreNotVerified)
    assert any(OUTAGE_TARGET in note and "not_serving" in note for note in caught.value.__notes__)
    assert len(open_injections(tmp_path)) == 1
