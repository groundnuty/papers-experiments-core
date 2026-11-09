"""The fault injector against cancellation, concurrency and neglect: a cancelled start still clears, journal writers
never cut each other's lines, a discarded injector lets its lock go, and an unsettled injection holds every other run
back (O11). Every test runs against `FakeServices`.
"""

import asyncio
import contextlib
import gc
import json
import threading
import time

import pytest
from fault_support import LOCAL_TARGET, OUTAGE_TARGET, SERVICES, FakeServices, injector_for, write_faults

from expcore.errors import InfraError
from expcore_onedata.config import load_platform
from expcore_onedata.fault_journal import JOURNAL, InjectionJournal
from expcore_onedata.faults import INJECTOR_FAILURE, SettledInjections, load_faults

OUTAGE_SERVICE = SERVICES["beta"]
RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}


def open_injections(tmp_path):
    path = tmp_path / "fault-journal" / JOURNAL
    lines = [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    cleared = {line["id"] for line in lines if line["event"] == "cleared"}
    return [line for line in lines if line["event"] == "injecting" and line["id"] not in cleared]


class SlowStop(FakeServices):
    """A stop command that takes effect only once the test lets it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stopping, self.release = threading.Event(), threading.Event()

    def stop(self, service):
        self.stopping.set()
        self.release.wait(10)
        super().stop(service)


def test_o11_cancelling_the_run_during_a_slow_stop_still_restores_the_provider_and_releases_its_lock(tmp_path):
    """O11: the start runs in a worker thread that a cancellation cannot stop; the session keeps that thread's future,
    waits for it shielded, clears in finally and raises the cancellation again, so nothing is left down or locked."""
    platform, services = write_faults(tmp_path), SlowStop()
    before = services.identity(OUTAGE_SERVICE)
    injector = injector_for(platform, services)

    async def scenario():
        async def run():
            async with injector.running(RUN):
                await asyncio.sleep(3600)

        task = asyncio.create_task(run())
        await asyncio.to_thread(services.stopping.wait, 10)
        task.cancel()
        threading.Timer(0.2, services.release.set).start()  # the stop finishes only after the cancellation
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert services.running(OUTAGE_SERVICE) is True
    assert services.identity(OUTAGE_SERVICE) != before
    assert open_injections(tmp_path) == []
    again = injector_for(platform, services)  # the lock was released
    again.start({**RUN, "attempt": 2})
    assert again.finish()["ok"] is True


def test_o11_two_writers_repairing_one_torn_tail_both_keep_their_lines(tmp_path, monkeypatch):
    """O11: the tail repair and the append are one critical section across the whole journal: a writer that read the
    torn tail cannot cut off the line another writer appended after it."""
    directory = tmp_path / "fault-journal"
    directory.mkdir()
    path = directory / JOURNAL
    path.write_bytes(b'{"event": "cleared", "how": "restored", "id": "earlier"}\n{"event": "injec')
    both_read = threading.Barrier(2)

    def racing_cut(self):
        data = self.path.read_bytes()
        keep = data.rfind(b"\n") + 1
        with contextlib.suppress(threading.BrokenBarrierError):  # broken: the other writer waits outside the section
            both_read.wait(timeout=1)
        if threading.current_thread().name == "second":
            time.sleep(0.3)  # the first writer has appended by now
        if keep < len(data):
            with open(self.path, "r+b") as handle:
                handle.truncate(keep)

    monkeypatch.setattr(InjectionJournal, "_cut_incomplete_line", racing_cut)
    lines = {name: {"event": "cleared", "how": "restored", "id": name} for name in ("first", "second")}
    writers = [
        threading.Thread(target=InjectionJournal(directory).append, args=(lines[name],), name=name)
        for name in ("first", "second")
    ]
    writers[0].start()
    time.sleep(0.05)
    writers[1].start()
    for writer in writers:
        writer.join(10)
    ids = [json.loads(text)["id"] for text in path.read_text(encoding="utf-8").splitlines()]
    assert ids == ["earlier", "first", "second"]


def test_o11_a_discarded_injector_lets_its_lock_go_so_recovery_restores_the_provider(tmp_path):
    """O11: a failed clear keeps the target's lock; once the injector is dropped and collected the lock goes with it,
    and the next start's recovery brings the provider back."""
    platform, services = write_faults(tmp_path), FakeServices()
    injector = injector_for(platform, services)
    injector.start(RUN)
    services.knobs["start_fails"] = True
    assert injector.clear() is False
    del injector
    gc.collect()
    services.knobs["start_fails"] = False
    again = injector_for(platform, services)
    again.start({**RUN, "attempt": 2})
    recovered = [step for step in again.snapshot()["steps"] if step["step"] == "recovered"]
    assert [(step["ok"], step["how"]) for step in recovered] == [(True, "restored")]
    assert again.finish()["ok"] is True
    assert services.running(OUTAGE_SERVICE) is True


def test_o11_while_an_injection_is_unsettled_no_other_injection_starts_and_the_health_gate_refuses(tmp_path):
    """O11: a provider held down by a live injector leaves the federation out of its baseline state: another
    injector's start refuses as injector_failure without touching its own target, and the platform's health check
    composed with `SettledInjections` refuses every run, whatever its condition, until the injection is cleared."""
    platform, services = write_faults(tmp_path), FakeServices()
    config = load_platform(platform, require_secrets=False)
    gate = SettledInjections(load_faults(platform, config))
    assert gate().ok is True
    holder = injector_for(platform, services)
    holder.start(RUN)
    health = gate()
    assert health.ok is False
    assert OUTAGE_TARGET in health.detail
    assert OUTAGE_SERVICE not in health.detail
    other = injector_for(platform, services, target=LOCAL_TARGET)
    with pytest.raises(InfraError, match="unsettled") as caught:
        other.start({**RUN, "run_key": "task=task-b|model=m1|seed=1"})
    assert caught.value.kind == INJECTOR_FAILURE
    assert services.running(SERVICES["alpha"]) is True  # its own target was never touched
    assert services.running(OUTAGE_SERVICE) is False  # nor was the live injection
    assert holder.finish()["ok"] is True
    assert gate().ok is True
    other.start({**RUN, "run_key": "task=task-b|model=m1|seed=1"})
    assert other.finish()["ok"] is True


def test_o11_the_settled_check_passes_on_to_the_platforms_own_health(tmp_path):
    """O11: composed with the platform's check, a settled journal answers what that check answers."""
    from expcore.campaign.healthgate import Health

    platform = write_faults(tmp_path)
    config = load_platform(platform, require_secrets=False)
    settings = load_faults(platform, config)
    assert SettledInjections(settings, inner=lambda: Health(False, "providers[0] is offline"))() == Health(
        False, "providers[0] is offline"
    )
    assert SettledInjections(settings, inner=lambda: Health(True, "ok"))().ok is True
