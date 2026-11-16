"""O58: a provider outage on an HPC federation, through the injector over the HPC driver (`fault_hpc.HpcDriver`), on
the fake federation of `hpc_support` and a fake clock.

The injector, its journal, the restore and its settle are the Compose federation's: the same lines in the same order,
the same steps, the same words, and the same fail-closed rules (O11): a stop journalled but never sent is `never_down`,
a stop of unknown outcome is waited out, and a restore that does not start or does not serve stays open, with the
fault gate closed for it, until a later clear verifies it.
"""

import json
import uuid

import pytest
from fault_support import OUTAGE_TARGET, SERVICES, FakeClock, FakeDaemon, injector_for, write_faults
from hpc_support import FakeHpc, hpc_driver

from expcore.errors import InfraError
from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.fault_journal import JOURNAL
from expcore_onedata.fault_recovery import Recovery, RestoreNotVerified, digest
from expcore_onedata.faults import INJECTOR_FAILURE, compose_driver, load_faults

SERVICE = SERVICES["beta"]  # providers[1], the target
LABELS = {service: f"providers[{index}]" for index, service in enumerate(SERVICES.values())}
RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
COMMAND_TIMEOUT_S, SETTLE_TIMEOUT_S = 3, 10


def journal(tmp_path):
    path = tmp_path / "fault-journal" / JOURNAL
    return [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def open_injections(tmp_path):
    lines = journal(tmp_path)
    cleared = {line["id"] for line in lines if line["event"] == "cleared"}
    return [line for line in lines if line["event"] == "injecting" and line["id"] not in cleared]


def steps(injector):
    return [(step["step"], step["ok"], step.get("how")) for step in injector.snapshot()["steps"]]


def hpc(tmp_path, **faults):
    """An injector for providers[1] over the HPC driver of a fake federation; with it the federation and the clock."""
    platform = write_faults(tmp_path, command_timeout_s=COMMAND_TIMEOUT_S, settle_timeout_s=SETTLE_TIMEOUT_S, **faults)
    clock = FakeClock()
    federation = FakeHpc(clock, LABELS)
    return injector_for(platform, hpc_driver(federation, timeout_s=COMMAND_TIMEOUT_S), clock=clock), federation


def settings_of(tmp_path):
    platform = tmp_path / "platform.toml"
    return load_faults(platform, load_platform(platform, require_secrets=False))


def test_o58_an_outage_on_hpc_journals_restores_and_settles_exactly_as_on_compose(tmp_path):
    """O58: the same outage, its provider slow to serve after the restore, run once over the Compose driver and once
    over the HPC driver, with the same REST side: the journal holds the same lines in the same order, the snapshot
    the same steps with the same words, and both restores are verified once the provider served, the session gate
    passed and every pair transferred. The HPC journal's `injected` line adds the stop's evidence: how it ended and
    its seconds, the figure a live slot sets `stop_seconds` from."""
    runs = {}
    for backend in ("compose", "hpc"):
        root = tmp_path / backend
        root.mkdir()
        platform = write_faults(root, command_timeout_s=COMMAND_TIMEOUT_S, settle_timeout_s=SETTLE_TIMEOUT_S)
        clock = FakeClock()
        federation = FakeHpc(clock, LABELS)
        federation.serves_after = 2
        if backend == "compose":
            daemon = FakeDaemon(clock)
            federation.slow[SERVICE] = 2  # a Compose start is not the fake federation's: its probe answers no twice
            driver = compose_driver(settings_of(root), daemon, probe=federation.probe)
        else:
            driver = hpc_driver(federation, timeout_s=COMMAND_TIMEOUT_S)
        injector = injector_for(platform, driver, clock=clock)
        injector.start(RUN)
        if backend == "hpc":
            assert federation.running(SERVICE) is False
        assert injector.clear() is True
        runs[backend] = (journal(root), injector.snapshot())
    (compose_lines, compose_shot), (hpc_lines, hpc_shot) = runs["compose"], runs["hpc"]
    shape = [(line["event"], line.get("how"), sorted(set(line) - {"stop"})) for line in hpc_lines]
    assert shape == [(line["event"], line.get("how"), sorted(line)) for line in compose_lines]
    evidence = {"stopped": True, "ended": "exit", "seconds": 0.0, "service_seconds": 0.0}
    assert [line.get("stop") for line in hpc_lines] == [None, None, evidence, None]
    assert [event for event, _, _ in shape] == ["injecting", "stopping", "injected", "cleared"]
    assert hpc_lines[0]["gate_probe"] == "gate-probe-1"
    words = [(s["step"], s["ok"], s.get("how")) for s in hpc_shot["steps"]]
    assert words == [(s["step"], s["ok"], s.get("how")) for s in compose_shot["steps"]]
    assert (hpc_shot["ok"], hpc_shot["restore_verified"]) == (True, True)
    assert hpc_shot["injections"] == [
        {"target": OUTAGE_TARGET, "injected": True, "cleared": True, "verified": True, "ready_after_s": 2.0}
    ]
    assert hpc_shot["injections"] == compose_shot["injections"]


def test_o58_the_stop_and_the_start_reach_the_target_service_alone_and_the_identity_changes(tmp_path):
    """O58: the outage stops the target's service and nothing else, and the restore starts it again: a new launch,
    so a new identity, which the snapshot records only as its digest."""
    injector, federation = hpc(tmp_path)
    injector.start(RUN)
    assert federation.acted("stop", "start") == [("stop", OUTAGE_TARGET)]
    assert injector.clear() is True
    assert federation.acted("stop", "start") == [("stop", OUTAGE_TARGET), ("start", OUTAGE_TARGET)]
    assert (federation.launches(SERVICE), federation.launches(SERVICES["alpha"])) == (2, 1)
    verified = [step for step in injector.snapshot()["steps"] if step["step"] == "verified"][-1]
    assert verified["identity_after_sha256"] == digest("2:4002:902")
    assert SERVICE not in json.dumps(injector.snapshot())


def crashed(tmp_path, *, stopping):
    """The journal a run killed mid-injection leaves on an HPC federation: `injecting`, and `stopping` after it when
    `stopping`; the stop command itself was never sent. Return the recovery, the federation and the clock."""
    platform = write_faults(tmp_path, command_timeout_s=COMMAND_TIMEOUT_S, settle_timeout_s=SETTLE_TIMEOUT_S)
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    clock = FakeClock()
    federation = FakeHpc(clock, LABELS)
    driver = hpc_driver(federation, timeout_s=COMMAND_TIMEOUT_S)
    recovery = Recovery(settings, driver, clock=clock, sleep=clock.sleep)
    entry = uuid.uuid4().hex
    line = {"target": OUTAGE_TARGET, "service": SERVICE, "identity_sha256": digest(driver.observe(SERVICE).identity)}
    recovery.journal.append({"event": "injecting", "id": entry, **line, "holder": {"pid": 999999, "boot_id": "x"}})
    if stopping:
        recovery.journal.append({"event": "stopping", "id": entry})
    return recovery, federation, clock


def test_o58_a_stop_journalled_but_never_sent_is_never_down_once_the_whole_bound_shows_the_service_as_it_was(tmp_path):
    """O58: nothing but `injecting` is journalled and the service runs with its identity through the whole settle
    bound, so nothing took it down: recovery calls it `never_down` only once the bound is over, starts nothing and
    does not wait for it to serve."""
    recovery, federation, clock = crashed(tmp_path, stopping=False)
    began = clock.now
    told = []
    recovery.recover(lambda *args, **kwargs: told.append((args, kwargs)))
    ((args, kwargs),) = told
    assert (args, kwargs["how"], kwargs["ready_after_s"]) == (("recovered", True), "never_down", None)
    assert clock.now - began >= SETTLE_TIMEOUT_S
    assert federation.acted("stop", "start") == []
    assert federation.reads == []
    assert recovery.journal.open_entries() == []


def test_o58_a_stop_that_was_asked_for_and_never_seen_down_is_not_taken_for_never_down(tmp_path):
    """O58: with the `stopping` line the stop may have been sent, so a service seen up through the bound is no proof
    it never went down: the injection stays open (`stop_unsettled`), nothing is started, and recovery fails closed."""
    recovery, federation, _ = crashed(tmp_path, stopping=True)
    with pytest.raises(InfraError, match="not cleared"):
        recovery.recover()
    (entry,) = recovery.journal.open_entries()
    assert entry["clear_failed"] == "stop_unsettled"
    assert federation.acted("start") == []


def test_o58_a_stop_that_returned_and_never_took_effect_is_never_down_and_nothing_is_started(tmp_path):
    """O58: the stop command answered, but the service was never seen down within the bound: the start fails as
    `injector_failure`, its clear finds the service running as it was (`never_down`), starts nothing, and closes
    the injection."""
    injector, federation = hpc(tmp_path)
    federation.stop_noop = True
    with pytest.raises(InfraError) as caught:
        injector.start(RUN)
    assert (caught.value.kind, caught.value.target) == (INJECTOR_FAILURE, OUTAGE_TARGET)
    assert steps(injector)[-3:] == [
        ("injected", False, None),
        ("cleared", True, "never_down"),
        ("verified", False, None),
    ]
    assert federation.acted("start") == []
    assert open_injections(tmp_path) == []


def test_o58_a_stop_killed_at_its_timeout_while_the_service_goes_on_stopping_is_waited_out_then_restored(tmp_path):
    """O58: the stop command is killed at its timeout (3 s) while the service stops 5 s after the request: the
    restore must not start a service that still runs and take it for never down; it waits for it to show down,
    starts it, sees a new identity and verifies the restore."""
    injector, federation = hpc(tmp_path)
    federation.stop_hangs, federation.exit_after_s = True, 5
    with pytest.raises(InfraError):
        injector.start(RUN)
    assert federation.running(SERVICE) is True
    assert federation.launches(SERVICE) == 2
    assert [line["event"] for line in journal(tmp_path)][:4] == ["injecting", "stopping", "stop_unknown", "cleared"]
    assert open_injections(tmp_path) == []
    restore = [step for step in steps(injector) if step[0] in ("injected", "cleared", "verified")]
    assert restore == [("injected", False, None), ("cleared", True, "restored"), ("verified", True, None)]


def test_o58_a_stop_answered_unconfirmed_journals_its_duration_with_the_stop_of_unknown_outcome(tmp_path):
    """O58: the stop verb could not confirm the stop (its step was still listed at the bound) and exits 1: the journal's
    `stop_unknown` line carries the stop's evidence, so every stop's real duration is in the journal, and the restore
    waits for the service to show down and brings it back as for any stop of unknown outcome."""
    injector, federation = hpc(tmp_path)
    federation.stop_unconfirmed, federation.exit_after_s = True, 5
    with pytest.raises(InfraError):
        injector.start(RUN)
    lines = journal(tmp_path)
    assert [line["event"] for line in lines][:4] == ["injecting", "stopping", "stop_unknown", "cleared"]
    assert lines[2]["stop"] == {"stopped": False, "ended": "exit", "seconds": 75.0, "service_seconds": 5.0}
    assert federation.launches(SERVICE) == 2
    assert open_injections(tmp_path) == []
    [failed] = [step for step in injector.snapshot()["steps"] if step["step"] == "injected"]
    assert failed["stop"] == lines[2]["stop"]


def test_o58_a_restore_that_does_not_serve_stays_open_and_a_later_clear_verifies_it_without_a_second_start(tmp_path):
    """O58: the service started anew but its provider does not serve within `ready_timeout_s`: `not_serving`, the
    injection open and the run's record refused; once it serves, a later clear verifies it with no second start."""
    injector, federation = hpc(tmp_path)
    federation.never_serving.add(SERVICE)
    injector.start(RUN)
    assert injector.clear() is False
    assert steps(injector)[-2][2] == "not_serving"
    assert injector.snapshot()["restore_verified"] is False
    with pytest.raises(RestoreNotVerified, match="not_serving"):
        injector.check()
    assert len(open_injections(tmp_path)) == 1
    federation.never_serving.clear()
    assert injector.clear() is True
    assert federation.acted("start") == [("start", OUTAGE_TARGET)]
    assert open_injections(tmp_path) == []


def test_o58_a_start_that_fails_leaves_the_injection_open_and_the_fault_gate_closed_until_it_is_restored(tmp_path):
    """O58: the start command fails on every attempt: the restore is `start_failed`, the injection stays open, and
    the fault gate, once the injector's process is gone, tries the restore again and fails while it still fails;
    once a start succeeds, the gate's own recovery restores and verifies the provider and the gate opens."""
    injector, federation = hpc(tmp_path)
    federation.start_fails.add(SERVICE)
    injector.start(RUN)
    assert injector.clear() is False
    assert steps(injector)[-2][2] == "start_failed"
    assert federation.running(SERVICE) is False
    injector._unlock()  # the injector's process ends: the kernel drops its locks
    injector._release_admission()
    clock = federation.clock
    gate = FaultGate(
        settings_of(tmp_path), hpc_driver(federation, timeout_s=COMMAND_TIMEOUT_S), clock=clock, sleep=clock.sleep
    )
    closed = gate()
    assert (closed.ok, closed.deferred) == (False, False)
    assert "not cleared" in closed.detail
    assert len(open_injections(tmp_path)) == 1
    federation.start_fails.clear()
    opened = gate()
    assert opened.ok is True, opened.detail
    assert federation.running(SERVICE) is True
    assert open_injections(tmp_path) == []
