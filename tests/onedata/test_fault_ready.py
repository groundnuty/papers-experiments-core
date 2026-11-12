"""O11: a verified restore is not a ready provider. A restore counts only when the provider serves, so the injection is
journalled cleared and the federation's exclusive admission released only after it does (live run of 12 Nov 2026: the
restarted provider's REST endpoint refused connections right after its restart and its health check passed 48 to 52 s
later). Every test runs against `FakeServices`, whose restored services answer `serves` no for a few readings first,
on a fake clock.
"""

import json
import multiprocessing

import pytest
from fault_support import (
    OUTAGE_TARGET,
    SERVICES,
    FakeClock,
    FakeServices,
    inject_and_die,
    injector_for,
    write_faults,
)

from expcore.errors import InfraError
from expcore_onedata.admission import FederationLock
from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.fault_journal import JOURNAL
from expcore_onedata.faults import INJECTOR_FAILURE, load_faults

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


def step(injector, name):
    return [s for s in injector.snapshot()["steps"] if s["step"] == name][-1]


def federation_is_exclusive(platform):
    """Whether the federation is held against a shared holder, as an outage run's exclusive admission holds it."""
    taken = FederationLock(settings_of(platform)).try_acquire(exclusive=False)
    if taken is not None:
        taken.close()
    return taken is None


def crashed(tmp_path, **options):
    """A process took the target down and died before clearing; return the platform file and the services' state."""
    platform, state = write_faults(tmp_path, **options), tmp_path / "services.json"
    FakeServices(path=state)  # the services, running, shared through the file
    child = multiprocessing.get_context("spawn").Process(
        target=inject_and_die, args=(str(platform), str(state), RUN["run_key"])
    )
    child.start()
    child.join(60)
    assert child.exitcode == 0
    assert len(open_injections(tmp_path)) == 1
    return platform, state


# --- the injector's own clear -------------------------------------------------------------------------------------


def test_o11_a_restore_whose_provider_serves_only_after_a_delay_waits_and_is_then_verified(tmp_path):
    """O11: the provider answers `serves` no for three readings after its restart. The clear polls until it does, only
    then journals the injection cleared, releases the federation and counts the restore verified, and the snapshot
    records the seconds it waited."""
    platform, clock = write_faults(tmp_path), FakeClock()
    services = FakeServices(serves_after=3)
    asked = []

    def while_asked(service):  # the injection is still open and the federation still held at every reading
        asked.append((len(open_injections(tmp_path)), federation_is_exclusive(platform)))

    services.on_serves = while_asked
    injector = injector_for(platform, services, clock=clock)
    injector.start(RUN)
    assert injector.clear() is True
    snapshot = injector.snapshot()
    assert snapshot["ok"] is True
    assert asked == [(1, True)] * 4  # three refusals, then it serves
    assert [call for call in services.calls if call[0] == "serves"] == [("serves", SERVICE)] * 4
    verified = step(injector, "verified")
    assert (verified["ok"], verified["identity_changed"], verified["ready_after_s"]) == (True, True, 3)
    assert snapshot["injections"] == [
        {"target": OUTAGE_TARGET, "injected": True, "cleared": True, "verified": True, "ready_after_s": 3}
    ]
    assert open_injections(tmp_path) == []
    assert federation_is_exclusive(platform) is False  # released only now


def test_o11_a_provider_that_serves_at_once_is_verified_with_no_wait(tmp_path):
    """O11: a provider that already serves at the first reading costs no wait, and the time to ready is 0."""
    injector = injector_for(write_faults(tmp_path), FakeServices())
    injector.start(RUN)
    assert injector.finish()["ok"] is True
    assert step(injector, "verified")["ready_after_s"] == 0


def test_o11_a_provider_that_never_serves_within_the_bound_leaves_the_restore_unverified_and_the_injection_open(
    tmp_path,
):
    """O11: its identity changed, but it never answers within `ready_timeout_s` (5 s): the restore is unverified
    (`not_serving`), the injection stays open and pending, and the federation stays held, exactly as for a failed
    restore. It does not start the service again: its identity is already verified."""
    platform, clock = write_faults(tmp_path), FakeClock()
    services = FakeServices(never_serves=True)
    injector = injector_for(platform, services, clock=clock)
    injector.start(RUN)
    started = clock.now
    assert injector.clear() is False
    assert 5 <= clock.now - started < 8  # the bound, counted on the injector's clock
    snapshot = injector.snapshot()
    assert snapshot["ok"] is False
    assert snapshot["injections"] == [{"target": OUTAGE_TARGET, "injected": True, "cleared": False, "verified": False}]
    cleared, verified = step(injector, "cleared"), step(injector, "verified")
    assert (cleared["ok"], cleared["how"]) == (False, "not_serving")
    assert (verified["ok"], verified["identity_changed"], verified["ready_after_s"]) == (False, True, None)
    assert [call for call in services.calls if call[0] == "start"] == [("start", SERVICE)]
    (still,) = open_injections(tmp_path)
    assert still["service"] == SERVICE
    failed = [line for line in journal_lines(tmp_path) if line["event"] == "clear_failed"]
    assert [(line["id"], line["how"]) for line in failed] == [(still["id"], "not_serving")]
    assert federation_is_exclusive(platform) is True


def test_o11_a_later_clear_verifies_once_the_unready_provider_serves(tmp_path):
    """O11: the unverified restore stays pending; once the provider serves, a second clear finds it restored (its
    identity changed in the first), waits no longer than it must and verifies it."""
    platform, clock = write_faults(tmp_path), FakeClock()
    services = FakeServices(never_serves=True)
    injector = injector_for(platform, services, clock=clock)
    injector.start(RUN)
    assert injector.clear() is False
    services.knobs["never_serves"] = False
    assert injector.clear() is True
    assert injector.snapshot()["ok"] is True
    assert step(injector, "cleared")["how"] == "found_restored"
    assert step(injector, "verified")["ready_after_s"] == 0
    assert open_injections(tmp_path) == []
    assert federation_is_exclusive(platform) is False


def test_o11_a_provider_the_stop_never_took_down_is_not_waited_for(tmp_path):
    """O11: a stop that took no effect left the provider running as it was; there is no restart to wait out."""
    services = FakeServices(stop_noop=True, never_serves=True)
    injector = injector_for(write_faults(tmp_path), services)
    with pytest.raises(InfraError):
        injector.start(RUN)
    assert step(injector, "cleared")["how"] == "never_down"
    assert ("serves", SERVICE) not in services.calls
    assert open_injections(tmp_path) == []


def test_o11_a_run_that_cannot_be_cancelled_mid_clear_is_waited_for_through_the_ready_bound(tmp_path):
    """O11: a cancelled run waits for a start's thread at most as long as the start can take, and a start's clears now
    wait for readiness: the bound counts `ready_timeout_s`."""
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    short = injector_for(write_faults(tmp_path / "a", ready_timeout_s=5), FakeServices())
    long = injector_for(write_faults(tmp_path / "b", ready_timeout_s=500), FakeServices())
    assert long.cancel_wait_s - short.cancel_wait_s == pytest.approx(2 * (500 - 5))


# --- the fault gate -----------------------------------------------------------------------------------------------


def test_o11_the_gate_fails_for_a_restore_whose_provider_never_served(tmp_path):
    """O11: the unverified restore is journalled `clear_failed`, so the health check's gate fails, naming the target
    by its label and how the restore failed, whoever still holds the federation."""
    platform = write_faults(tmp_path, health_wait_s=0.2)
    injector = injector_for(platform, FakeServices(never_serves=True))
    injector.start(RUN)
    assert injector.clear() is False
    health = FaultGate(settings_of(platform), FakeServices())()
    assert (health.ok, health.deferred) == (False, False)
    assert OUTAGE_TARGET in health.detail
    assert "not_serving" in health.detail


# --- recovery -----------------------------------------------------------------------------------------------------


def test_o11_recovery_of_a_crashed_runs_injection_waits_for_the_provider_to_serve_too(tmp_path):
    """O11: a process died with the target down; the next start restores it and, before it injects anything else,
    waits for it to serve, then reports the wait in its `recovered` reading."""
    platform, state = crashed(tmp_path)
    services, clock = FakeServices(path=state, serves_after=3), FakeClock()
    asked = []
    services.on_serves = lambda service: asked.append(len(open_injections(tmp_path)))
    injector = injector_for(platform, services, clock=clock)
    injector.start({**RUN, "attempt": 2})
    recovered = step(injector, "recovered")
    assert (recovered["ok"], recovered["how"], recovered["target"]) == (True, "restored", OUTAGE_TARGET)
    assert recovered["ready_after_s"] == 3
    assert asked[:4] == [1, 1, 1, 1]  # still open until it serves
    assert [s["step"] for s in injector.snapshot()["steps"]][:3] == ["sanctioned", "recovered", "injected"]
    assert injector.finish()["ok"] is True
    assert open_injections(tmp_path) == []


def test_o11_a_start_fails_when_the_provider_a_crash_left_down_does_not_serve_after_its_recovery(tmp_path):
    """O11: the recovery cannot verify the restore within the bound, so the start fails as injector_failure and
    injects nothing: the crashed run's injection stays open, for a later start or the gate to settle."""
    platform, state = crashed(tmp_path)
    services = FakeServices(path=state, never_serves=True)
    injector = injector_for(platform, services, clock=FakeClock())
    with pytest.raises(InfraError) as caught:
        injector.start({**RUN, "attempt": 2})
    assert (caught.value.kind, caught.value.target) == (INJECTOR_FAILURE, OUTAGE_TARGET)
    recovered = step(injector, "recovered")
    assert (recovered["ok"], recovered["how"]) == (False, "not_serving")
    assert [call for call in services.calls if call[0] == "stop"] == []  # nothing new was taken down
    assert len(open_injections(tmp_path)) == 1
    assert federation_is_exclusive(platform) is False  # no injection of this start holds it


def test_o11_the_health_checks_recovery_waits_for_the_provider_to_serve_before_it_passes(tmp_path):
    """O11: the fault gate settles what a crashed run left open when no run is on the federation, and its pass means
    the provider serves: it asks, waiting, before the injection is journalled cleared."""
    platform, state = crashed(tmp_path, poll_s=0.01, ready_timeout_s=0.5)  # the gate sleeps for real
    services = FakeServices(path=state, serves_after=3)
    asked = []
    services.on_serves = lambda service: asked.append(len(open_injections(tmp_path)))
    health = FaultGate(settings_of(platform), services)()
    assert health.ok is True, health.detail
    assert asked == [1, 1, 1, 1]
    assert open_injections(tmp_path) == []


def test_o11_the_health_check_fails_while_a_recovered_provider_does_not_serve(tmp_path):
    """O11: a provider that the recovery restored but that never serves leaves the check failing, and the injection
    open: no run is admitted to a federation whose provider is not serving."""
    platform, state = crashed(tmp_path, poll_s=0.01, ready_timeout_s=0.2)  # the gate sleeps for real
    health = FaultGate(settings_of(platform), FakeServices(path=state, never_serves=True))()
    assert health.ok is False
    assert OUTAGE_TARGET in health.detail
    assert len(open_injections(tmp_path)) == 1
