"""The fault injector under failure: every path after the stop restores, the journal stays durable and readable, a
forked child never keeps a target's lock, an unreadable service is still started, and no rejected value is echoed
(O11, O12). Every test runs against `FakeServices`.
"""

import fcntl
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fault_support import (
    OUTAGE_TARGET,
    SERVICES,
    FakeServices,
    inject_fork_and_die,
    injector_for,
    write_faults,
)

from expcore.errors import InfraError
from expcore.loop.outcomes import Outcome
from expcore_onedata.config import load_platform
from expcore_onedata.fault_journal import JOURNAL, InjectionJournal
from expcore_onedata.faults import INJECTOR_FAILURE, FaultInjector, FaultRefused, load_faults

OUTAGE_SERVICE = SERVICES["beta"]
RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}


def journal_path(tmp_path, directory="fault-journal"):
    return tmp_path / directory / JOURNAL


def open_injections(path):
    lines = [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    cleared = {line["id"] for line in lines if line["event"] == "cleared"}
    return [line for line in lines if line["event"] == "injecting" and line["id"] not in cleared]


def this_boot():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None


# --- every path after the stop restores (item 1) ----------------------------------------------------------------


def test_o11_a_journal_append_failing_after_the_stop_restores_the_provider_and_fails_as_the_injectors_own(
    tmp_path, monkeypatch
):
    """O11: the disk fills between the stop and the `injected` line: the provider is brought back at once, the lock is
    released, the start fails as injector_failure, and the reading says it was down and then cleared."""
    real = InjectionJournal.append

    def disk_full_after_the_stop(self, line):
        if line["event"] == "injected":
            raise OSError(28, "No space left on device")
        return real(self, line)

    monkeypatch.setattr(InjectionJournal, "append", disk_full_after_the_stop)
    platform, services = write_faults(tmp_path), FakeServices()
    before = services.identity(OUTAGE_SERVICE)
    injector = injector_for(platform, services)
    with pytest.raises(InfraError) as caught:
        injector.start(RUN)
    assert (caught.value.kind, caught.value.target) == (INJECTOR_FAILURE, OUTAGE_TARGET)
    assert services.running(OUTAGE_SERVICE) is True
    assert services.identity(OUTAGE_SERVICE) != before
    assert open_injections(journal_path(tmp_path)) == []
    steps = [(step["step"], step["ok"]) for step in injector.snapshot()["steps"]]
    assert steps[1:] == [("injected", False), ("cleared", True), ("verified", True)]
    monkeypatch.setattr(InjectionJournal, "append", real)
    again = injector_for(platform, services)  # the lock was released: the target can be injected again
    again.start({**RUN, "attempt": 2})
    assert again.finish()["ok"] is True


class StopThenBreak(FakeServices):
    """A driver whose stop takes effect and then raises something no driver error covers."""

    def stop(self, service):
        super().stop(service)
        raise RuntimeError("the driver broke after stopping")


def test_o11_a_session_whose_start_breaks_after_the_stop_still_brings_the_provider_back(tmp_path):
    """O11: an unexpected exception after the stop command is the injector's own failure, and the provider is up."""
    services = StopThenBreak()
    injector = injector_for(write_faults(tmp_path), services)

    def trial():
        with injector.session(RUN):
            raise AssertionError("the body must not run when the injection failed")

    with pytest.raises(InfraError) as caught:
        trial()
    assert caught.value.kind == INJECTOR_FAILURE
    assert services.running(OUTAGE_SERVICE) is True
    assert open_injections(journal_path(tmp_path)) == []


# --- durable directories (item 2) --------------------------------------------------------------------------------


def test_o11_every_directory_the_journal_creates_is_durable_in_its_parent_before_the_stop(tmp_path, monkeypatch):
    """O11: the lock and the journal create their directories; the file, its directory and the parent of every new
    directory are fsynced, and all of it before the stop command runs."""
    synced: list[tuple[int, int]] = []
    real_fsync = os.fsync

    def recording(fd):
        status = os.fstat(fd)
        synced.append((status.st_dev, status.st_ino))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording)
    services = FakeServices()
    at_stop: list[int] = []
    services.on_stop = lambda service: at_stop.append(len(synced))
    injector = injector_for(write_faults(tmp_path, journal="faults/journal"), services)
    injector.start(RUN)
    before_stop = set(synced[: at_stop[0]])

    def key(path):
        status = os.stat(path)
        return status.st_dev, status.st_ino

    expected = {
        "the journal": key(tmp_path / "faults" / "journal" / JOURNAL),
        "its directory": key(tmp_path / "faults" / "journal"),
        "the parent of the journal's directory": key(tmp_path / "faults"),
        "the parent of the first new directory": key(tmp_path),
    }
    assert [name for name, path in expected.items() if path not in before_stop] == []
    injector.finish()


# --- a torn final line (item 3) ----------------------------------------------------------------------------------


def test_o11_a_torn_final_line_is_cut_before_the_next_append_and_the_journal_stays_readable(tmp_path):
    """O11: a crash mid-append leaves a line without its newline; the reader ignores it, the next append first cuts it
    off (and syncs), so recovery and every later read see whole lines only."""
    path = journal_path(tmp_path)
    path.parent.mkdir()
    whole = json.dumps({"event": "cleared", "id": "earlier", "how": "restored"})
    path.write_text(whole + "\n" + '{"event": "injecting", "id": "torn", "tar', encoding="utf-8")
    platform, services = write_faults(tmp_path), FakeServices()
    injector = injector_for(platform, services)
    injector.start(RUN)
    assert injector.finish()["ok"] is True
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert [json.loads(line)["event"] for line in text.splitlines()] == ["cleared", "injecting", "injected", "cleared"]
    assert InjectionJournal(path.parent).open_entries() == []
    later = injector_for(platform, services)
    later.start({**RUN, "attempt": 2})
    assert later.finish()["ok"] is True


def test_o11_an_invalid_complete_line_is_never_ignored(tmp_path):
    """O11: only an incomplete final line is a crash's leftover; a whole line that is not an event makes the journal
    unreadable, and the start fails closed without stopping anything."""
    path = journal_path(tmp_path)
    path.parent.mkdir()
    path.write_text('{"event": "injecting", "id": "x", "tar\n', encoding="utf-8")
    services = FakeServices()
    with pytest.raises(InfraError, match="journal cannot be read"):
        injector_for(write_faults(tmp_path), services).start(RUN)
    assert ("stop", OUTAGE_SERVICE) not in services.calls


# --- a forked child never holds the lock (item 4) ----------------------------------------------------------------


def test_o11_a_forked_child_that_outlives_the_injector_does_not_keep_the_targets_lock(tmp_path):
    """O11: the injecting process forks and dies before clearing; its child lives on with every inherited descriptor
    but the lock, so the next start settles the injection and brings the provider back."""
    platform, state, pid_file = write_faults(tmp_path), tmp_path / "services.json", tmp_path / "grandchild.pid"
    FakeServices(path=state)
    child = multiprocessing.get_context("spawn").Process(
        target=inject_fork_and_die, args=(str(platform), str(state), RUN["run_key"], str(pid_file))
    )
    child.start()
    deadline = time.monotonic() + 60
    while child.exitcode is None and time.monotonic() < deadline:  # join would wait for the grandchild too: it holds
        time.sleep(0.05)  # the inherited end of the sentinel pipe, which is the very inheritance this test is about
    assert child.exitcode == 0
    grandchild = int(pid_file.read_text(encoding="utf-8"))
    try:
        os.kill(grandchild, 0)  # alive, outliving the process that injected
        services = FakeServices(path=state)
        assert services.running(OUTAGE_SERVICE) is False
        (entry,) = open_injections(journal_path(tmp_path))
        assert entry["holder"] == {"pid": child.pid, "boot_id": this_boot()}
        injector = injector_for(platform, services)
        injector.start({**RUN, "attempt": 2})
        recovered = [s for s in injector.snapshot()["steps"] if s["step"] == "recovered"]
        assert [(s["ok"], s["how"]) for s in recovered] == [(True, "restored")]
        assert injector.finish()["ok"] is True
    finally:
        os.kill(grandchild, signal.SIGKILL)


def test_o11_a_lock_held_while_its_journalled_holder_is_dead_is_reported_stuck(tmp_path):
    """O11: the journal names the holder's pid and boot; a target whose lock someone holds while that holder is dead
    on this boot is stuck, and the start fails closed instead of skipping a provider nobody will bring back."""
    dead_pid = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid.wait()  # reaped: its pid names no live process
    platform, services = write_faults(tmp_path), FakeServices()
    services.stop(OUTAGE_SERVICE)
    path = journal_path(tmp_path)
    path.parent.mkdir()
    line = {
        "event": "injecting",
        "id": "left-open",
        "target": OUTAGE_TARGET,
        "service": OUTAGE_SERVICE,
        "identity_sha256": "0" * 64,
        "holder": {"pid": dead_pid.pid, "boot_id": this_boot()},
    }
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    stuck = os.open(path.parent / "providers-1.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(stuck, fcntl.LOCK_EX)
    try:
        with pytest.raises(InfraError, match="stuck") as caught:
            injector_for(platform, services).start(RUN)
        assert caught.value.kind == INJECTOR_FAILURE
    finally:
        os.close(stuck)


# --- an unreadable service is still started (item 5) -------------------------------------------------------------


def test_o11_a_service_that_cannot_be_inspected_is_still_started_and_the_restore_is_unverified(tmp_path):
    """O11: inspection fails while the start command works: the start is issued anyway (harmless on a running
    service), the restore cannot be verified, and the injection stays open as not cleared."""
    services = FakeServices()
    injector = injector_for(write_faults(tmp_path), services)
    injector.start(RUN)
    services.knobs["unreadable"] = True
    snapshot = injector.finish()
    assert ("start", OUTAGE_SERVICE) in services.calls
    assert services.running(OUTAGE_SERVICE) is True
    cleared = next(step for step in snapshot["steps"] if step["step"] == "cleared")
    assert (cleared["ok"], cleared["how"], snapshot["ok"]) == (False, "restore_unverified", False)
    assert len(open_injections(journal_path(tmp_path))) == 1


# --- a failed clear is retried (item 7) --------------------------------------------------------------------------


def test_o11_a_failed_restore_stays_pending_and_a_later_clear_really_restores(tmp_path):
    """O11: the start command fails, so the injection stays pending; once the cause is fixed, a second clear brings
    the provider back with a new identity and closes the journal entry."""
    services = FakeServices()
    before = services.identity(OUTAGE_SERVICE)
    injector = injector_for(write_faults(tmp_path), services)
    injector.start(RUN)
    services.knobs["start_fails"] = True
    assert injector.clear() is False
    assert services.running(OUTAGE_SERVICE) is False
    services.knobs["start_fails"] = False
    assert injector.clear() is True
    assert services.running(OUTAGE_SERVICE) is True
    assert services.identity(OUTAGE_SERVICE) != before
    assert open_injections(journal_path(tmp_path)) == []
    assert injector.snapshot()["ok"] is True


# --- no rejected value is echoed (item 8) ------------------------------------------------------------------------


@pytest.mark.parametrize("rejected", ["beta", OUTAGE_SERVICE, "beta.test"])
def test_o12_a_rejected_target_is_never_echoed(tmp_path, rejected):
    """O12: a target that is not a position label may be a configured name; the refusal says what is wrong with it,
    never what it was."""
    platform = write_faults(tmp_path)
    config = load_platform(platform, require_secrets=False)
    refusals = []
    for build in (
        lambda: InfraError("knowledge", "down", target=rejected),
        lambda: Outcome("infrastructure", "down", infra_kind="knowledge", infra_target=rejected),
        lambda: FaultInjector(config, load_faults(platform, config), rejected, sanctioned=True, driver=FakeServices()),
    ):
        with pytest.raises((ValueError, FaultRefused)) as caught:
            build()
        refusals.append(str(caught.value))
    assert [text for text in refusals if rejected in text] == []


def test_o12_a_journal_that_cannot_be_read_is_named_by_the_error_type_only(tmp_path):
    """O12: an OS error's text holds the journal's path; the failure names only the error's type."""
    journal_path(tmp_path).mkdir(parents=True)  # a directory where the file should be
    with pytest.raises(InfraError, match="IsADirectoryError") as caught:
        injector_for(write_faults(tmp_path), FakeServices()).start(RUN)
    assert str(tmp_path) not in str(caught.value)
    assert "fault-journal" not in str(caught.value)
