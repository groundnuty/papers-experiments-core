"""A stop whose outcome is unknown: the CLI is killed at its timeout while the daemon goes on stopping the container,
so the restore waits for the service to show down before it judges the identity (O11). Every test runs the real
Compose driver over a fake runner (`FakeDaemon`), on a fake clock.
"""

import json
import uuid

import pytest
from fault_support import (
    OUTAGE_TARGET,
    SERVICES,
    FakeClock,
    FakeDaemon,
    FakeServices,
    injector_for,
    serving,
    write_faults,
)

from expcore.errors import InfraError
from expcore_onedata.compose import Observation
from expcore_onedata.config import load_platform
from expcore_onedata.fault_journal import JOURNAL, InjectionJournal
from expcore_onedata.fault_recovery import Recovery, digest
from expcore_onedata.faults import INJECTOR_FAILURE, compose_driver, load_faults

SERVICE = SERVICES["beta"]
RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
COMMAND_TIMEOUT_S, SETTLE_TIMEOUT_S = 3, 10


def journal_lines(tmp_path):
    path = tmp_path / "fault-journal" / JOURNAL
    return [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def open_injections(tmp_path):
    lines = journal_lines(tmp_path)
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
    return injector_for(platform, compose_driver(settings, daemon, probe=serving), clock=clock), daemon, clock, settings


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
    recovery = Recovery(settings, compose_driver(settings, daemon, probe=serving), clock=clock, sleep=clock.sleep)
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

    def serves(self, service):
        return True


@pytest.mark.parametrize("flags", [{"injected": True}, {"injected": False}, {"injected": False, "stop_unknown": True}])
def test_o11_running_with_no_identity_is_not_a_restore(tmp_path, flags):
    """O11: a reading that shows the service running but names no identity cannot show the identity changed, so it
    never counts as restored, whether the stop was seen or its outcome unknown."""
    platform = write_faults(tmp_path, settle_timeout_s=SETTLE_TIMEOUT_S)
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    clock = FakeClock()
    recovery = Recovery(settings, _RunningWithoutIdentity(), clock=clock, sleep=clock.sleep)
    entry = {"id": "left-open", "service": SERVICE, "identity_sha256": digest("old"), **flags}
    done, how, after, _ = recovery.settle(entry)
    assert (done, after) == (False, None)
    assert how not in ("restored", "found_restored", "never_down")


# --- a process killed while the stop command blocks: nothing but `injecting` reached the journal ---------------------


class Probe:
    """A readiness probe that answers no for its first `no` readings, and counts every reading."""

    def __init__(self, no=0):
        self.no, self.asked = no, 0

    def __call__(self, service):
        self.asked += 1
        return self.asked > self.no


def crashed_run(tmp_path, *, marker, asked_to_stop, exit_after_s=None, driver=None, probe=serving):
    """The journal a run that was killed mid-injection leaves: `injecting`, with `stopping` after it when `marker`. When
    `asked_to_stop`, the daemon was asked to stop the container (it exits `exit_after_s` seconds later) before the
    process died; otherwise the process died before the stop command. Return the recovery, the daemon and the clock."""
    platform = write_faults(tmp_path, command_timeout_s=COMMAND_TIMEOUT_S, settle_timeout_s=SETTLE_TIMEOUT_S)
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    clock = FakeClock()
    daemon = FakeDaemon(clock)
    real = compose_driver(settings, daemon, probe=probe)
    recovery = Recovery(settings, driver or real, clock=clock, sleep=clock.sleep)
    entry = uuid.uuid4().hex
    recovery.journal.append(
        {
            "event": "injecting",
            "id": entry,
            "target": OUTAGE_TARGET,
            "service": SERVICE,
            "identity_sha256": digest(real.observe(SERVICE).identity),
            "holder": {"pid": 999999, "boot_id": "x"},
        }
    )
    if marker:
        recovery.journal.append({"event": "stopping", "id": entry})
    if asked_to_stop:
        daemon.exit_after_s = exit_after_s
        daemon(["docker", "compose", "stop", SERVICE], timeout_s=COMMAND_TIMEOUT_S)  # the CLI died; the daemon goes on
    return recovery, daemon, clock


def recover(recovery):
    """Run the recovery; return what it told (`on_step`) of each injection it settled."""
    told = []
    recovery.recover(lambda *args, **kwargs: told.append((args, kwargs)))
    return told


@pytest.mark.parametrize("marker", [True, False])
def test_o11_a_crash_while_the_stop_blocks_is_waited_out_then_restored_and_verified_only_once_it_serves(
    tmp_path, marker
):
    """O11: the process was killed while the stop command blocked, so the journal holds `injecting` and at most the
    `stopping` marker. The container still runs with its old identity when recovery looks, and the daemon exits it
    8 s on: recovery must not call that never down. It waits for the exit, restores, and reports the restore only
    after the provider served (the third reading), never before."""
    probe = Probe(no=2)
    recovery, daemon, _ = crashed_run(tmp_path, marker=marker, asked_to_stop=True, exit_after_s=8, probe=probe)
    ((args, told),) = recover(recovery)
    assert (args, told["how"], told["ready_after_s"]) == (("recovered", True), "restored", 2)
    assert (daemon.running(SERVICE), daemon.starts(SERVICE)) == (True, 2)
    assert probe.asked == 3
    assert recovery.journal.open_entries() == []


def test_o11_a_crash_before_the_stop_is_issued_leaves_the_container_running_through_the_bound_so_it_never_went_down(
    tmp_path,
):
    """O11: nothing but `injecting` is journalled and the container stays up with its identity for the whole settle
    bound, so nothing took it down: recovery reads that only once the bound is over, restarts nothing and does not
    wait for it to serve."""
    probe = Probe()
    recovery, daemon, clock = crashed_run(tmp_path, marker=False, asked_to_stop=False, probe=probe)
    began = clock.now
    ((args, told),) = recover(recovery)
    assert (args, told["how"], told["ready_after_s"]) == (("recovered", True), "never_down", None)
    assert clock.now - began >= SETTLE_TIMEOUT_S
    assert (probe.asked, ("start", SERVICE) in daemon.commands) == (0, False)
    assert (daemon.running(SERVICE), daemon.starts(SERVICE)) == (True, 1)
    assert recovery.journal.open_entries() == []


def test_o11_a_stop_that_was_asked_for_and_is_not_seen_down_within_the_bound_is_not_taken_for_never_down(tmp_path):
    """O11: the `stopping` marker says the daemon was asked to stop the container, which then outlasts the settle
    bound: recovery cannot tell that it never went down, so it fails closed, leaves the injection open and restarts
    nothing. A later recovery, after the daemon finished, restores it."""
    recovery, daemon, clock = crashed_run(tmp_path, marker=True, asked_to_stop=True, exit_after_s=60)
    with pytest.raises(InfraError, match="not cleared"):
        recover(recovery)
    (entry,) = recovery.journal.open_entries()
    assert entry["clear_failed"] == "stop_unsettled"
    assert ("start", SERVICE) not in daemon.commands
    clock.sleep(60)
    ((_, told),) = recover(recovery)
    assert told["how"] == "restored"
    assert (daemon.running(SERVICE), daemon.starts(SERVICE)) == (True, 2)


def test_o11_a_container_that_cannot_be_read_through_the_bound_is_not_taken_for_never_down(tmp_path):
    """O11: with no outcome journalled, only readings that show the container running with its identity, all through
    the bound, say it never went down; readings that fail say nothing, so the injection stays open."""
    unreadable = FakeServices(unreadable=True)
    recovery, _, _ = crashed_run(tmp_path, marker=False, asked_to_stop=False, driver=unreadable)
    with pytest.raises(InfraError):
        recover(recovery)
    (entry,) = recovery.journal.open_entries()
    assert entry["clear_failed"] == "stop_unsettled"


@pytest.mark.parametrize(
    ("later", "unknown", "unrecorded"),
    [
        ([], False, True),  # no line after `injecting`: the stop may or may not have been asked for
        (["stopping"], True, False),  # the stop was asked for and nothing says how it ended
        (["stopping", "stop_unknown"], True, False),
        (["stopping", "injected"], False, False),  # seen down: nothing to wait for
    ],
)
def test_o11_the_journal_tells_an_open_injection_whose_stop_may_still_run_from_one_that_was_seen_down(
    tmp_path, later, unknown, unrecorded
):
    """O11: an open injection is `stop_unknown` once the stop was asked for and not seen to end, `stop_unrecorded` when
    no line says the stop was asked for, and neither once the target was seen down."""
    journal = InjectionJournal(tmp_path / "fault-journal")
    line = {"target": OUTAGE_TARGET, "service": SERVICE, "identity_sha256": "0" * 64}
    journal.append({"event": "injecting", "id": "left-open", **line})
    for event in later:
        journal.append({"event": event, "id": "left-open"})
    (entry,) = journal.open_entries()
    assert (bool(entry.get("stop_unknown")), bool(entry.get("stop_unrecorded"))) == (unknown, unrecorded)


def test_o11_the_stop_is_journalled_before_it_is_asked_for(tmp_path):
    """O11: a process killed while the stop command blocks leaves `injecting` and `stopping` in the journal, so the
    next recovery knows the daemon may be stopping the container."""
    services, seen = FakeServices(), []
    services.on_stop = lambda service: seen.append([line["event"] for line in journal_lines(tmp_path)])
    injector = injector_for(write_faults(tmp_path), services)
    injector.start(RUN)
    assert seen == [["injecting", "stopping"]]
    assert [line["event"] for line in journal_lines(tmp_path)] == ["injecting", "stopping", "injected"]
    injector.finish()


def test_o11_a_stop_that_cannot_be_journalled_is_not_asked_for(tmp_path, monkeypatch):
    """O11: the marker is journalled durably before the stop like the injection is: when the journal fails at it, the
    target is not stopped, and the start fails as the injector's own failure."""
    real_append = InjectionJournal.append

    def failing(self, line):
        if line["event"] == "stopping":
            raise OSError("the journal's disk is gone")
        real_append(self, line)

    monkeypatch.setattr(InjectionJournal, "append", failing)
    services = FakeServices()
    injector = injector_for(write_faults(tmp_path), services)
    with pytest.raises(InfraError, match="could not be taken down"):
        injector.start(RUN)
    assert [call for call in services.calls if call[0] == "stop"] == []
    assert services.running(SERVICE) is True
    assert open_injections(tmp_path) == []
