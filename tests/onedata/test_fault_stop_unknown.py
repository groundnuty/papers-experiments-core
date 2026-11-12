"""A stop whose outcome is unknown: the CLI is killed at its timeout while the daemon goes on stopping the container,
so the restore waits for the service to show down before it judges the identity (O11). Every test runs the real
Compose driver over a fake runner (`FakeDaemon`), on a fake clock.
"""

import json

import pytest
from fault_support import (
    OUTAGE_TARGET,
    SERVICES,
    FakeClock,
    FakeDaemon,
    injector_for,
    write_faults,
)

from expcore.errors import InfraError
from expcore_onedata.compose import Observation
from expcore_onedata.config import load_platform
from expcore_onedata.fault_journal import JOURNAL
from expcore_onedata.fault_recovery import Recovery, digest
from expcore_onedata.faults import INJECTOR_FAILURE, compose_driver, load_faults

SERVICE = SERVICES["beta"]
RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
COMMAND_TIMEOUT_S, SETTLE_TIMEOUT_S = 3, 10


def open_injections(tmp_path):
    path = tmp_path / "fault-journal" / JOURNAL
    lines = [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    cleared = {line["id"] for line in lines if line["event"] == "cleared"}
    return [line for line in lines if line["event"] == "injecting" and line["id"] not in cleared]


def hanging_stop(tmp_path, exit_after_s):
    """An injector over a daemon whose `stop` hangs until the runner's timeout; the container exits `exit_after_s`
    seconds after the stop was asked for (never, when None)."""
    platform = write_faults(tmp_path, command_timeout_s=COMMAND_TIMEOUT_S, settle_timeout_s=SETTLE_TIMEOUT_S)
    clock = FakeClock()
    daemon = FakeDaemon(clock)
    daemon.stop_hangs, daemon.exit_after_s = True, exit_after_s
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    return injector_for(platform, compose_driver(settings, daemon), clock=clock), daemon, clock, settings


def steps(injector):
    return [(step["step"], step["ok"]) for step in injector.snapshot()["steps"]]


def test_o11_a_stop_that_times_out_while_the_container_keeps_stopping_is_waited_out_then_restored(tmp_path):
    """O11: the CLI is killed at its timeout (3 s) while the daemon stops the container 5 s after the request. The
    restore must not start a container that is still running (a no-op) and take it for never down: it waits for the
    container to exit, starts it, sees a new identity and reports the restore verified."""
    injector, daemon, clock, _ = hanging_stop(tmp_path, exit_after_s=5)
    with pytest.raises(InfraError) as caught:
        injector.start(RUN)
    assert (caught.value.kind, caught.value.target) == (INJECTOR_FAILURE, OUTAGE_TARGET)
    clock.sleep(60)  # long after the daemon finished stopping
    assert daemon.running(SERVICE) is True
    assert daemon.starts(SERVICE) == 2
    assert open_injections(tmp_path) == []
    assert steps(injector)[-3:] == [("injected", False), ("cleared", True), ("verified", True)]


def test_o11_a_stop_that_times_out_and_never_stops_within_the_bound_leaves_the_restore_unverified(tmp_path):
    """O11: the container is not seen down within the settle timeout, so nothing says the restore happened: it is
    unverified, the injection stays open and pending, and the provider is never reported as restored."""
    injector, daemon, _, _ = hanging_stop(tmp_path, exit_after_s=None)
    with pytest.raises(InfraError):
        injector.start(RUN)
    assert (daemon.starts(SERVICE), ("start", SERVICE) in daemon.commands) == (1, False)
    assert len(open_injections(tmp_path)) == 1
    assert steps(injector)[-3:] == [("injected", False), ("cleared", False), ("verified", False)]
    assert injector.snapshot()["ok"] is False
    cleared = next(step for step in injector.snapshot()["steps"] if step["step"] == "cleared")
    assert cleared["how"] == "stop_unsettled"


def test_o11_a_later_clear_verifies_once_the_container_that_outlasted_the_bound_has_exited(tmp_path):
    """O11: the unverified restore stays pending; a second clear waits again, sees the container down, starts it and
    verifies the new identity."""
    # the daemon finishes at 20 s: past the first clear's bound (3 s + 10 s), within the second's
    injector, daemon, _, _ = hanging_stop(tmp_path, exit_after_s=20)
    with pytest.raises(InfraError):
        injector.start(RUN)
    assert len(open_injections(tmp_path)) == 1
    assert injector.clear() is True
    assert (daemon.running(SERVICE), daemon.starts(SERVICE)) == (True, 2)
    assert open_injections(tmp_path) == []
    assert steps(injector)[-2:] == [("cleared", True), ("verified", True)]


def test_o11_a_recovery_after_a_crash_in_an_unknown_stop_waits_for_the_container_too(tmp_path):
    """O11: the journal keeps that the stop's outcome was unknown, so a later process that settles the injection
    waits for the container to exit as well, instead of finding it running with its old identity and calling it
    never down."""
    injector, daemon, clock, settings = hanging_stop(tmp_path, exit_after_s=20)
    with pytest.raises(InfraError):
        injector.start(RUN)
    assert len(open_injections(tmp_path)) == 1
    recovery = Recovery(settings, compose_driver(settings, daemon), clock=clock, sleep=clock.sleep)
    (entry,) = recovery.entries()
    assert entry["stop_unknown"] is True
    assert recovery.settle(entry)[:2] == (True, "restored")
    clock.sleep(60)
    assert (daemon.running(SERVICE), daemon.starts(SERVICE)) == (True, 2)


class _RunningWithoutIdentity:
    """A driver whose every reading says running and names no identity."""

    def observe(self, service):
        return Observation(True, None)

    def stop(self, service):
        raise AssertionError("never stopped")

    def start(self, service):
        pass


@pytest.mark.parametrize("flags", [{"injected": True}, {"injected": False}, {"injected": False, "stop_unknown": True}])
def test_o11_running_with_no_identity_is_not_a_restore(tmp_path, flags):
    """O11: a reading that shows the service running but names no identity cannot show the identity changed, so it
    never counts as restored, whether the stop was seen or its outcome unknown."""
    platform = write_faults(tmp_path, settle_timeout_s=SETTLE_TIMEOUT_S)
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    clock = FakeClock()
    recovery = Recovery(settings, _RunningWithoutIdentity(), clock=clock, sleep=clock.sleep)
    entry = {"id": "left-open", "service": SERVICE, "identity_sha256": digest("old"), **flags}
    done, how, after = recovery.settle(entry)
    assert (done, after) == (False, None)
    assert how not in ("restored", "found_restored", "never_down")
