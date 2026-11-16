"""O56: the restore's link heal on an HPC federation, through the injector over the HPC driver, on the four-provider
federation of the heal's tests (`heal_support`) run by the fake federation of `hpc_support`, on a fake clock.

`restart_link` and `ping` are exec'd into each provider's Apptainer service, and the container fallback is the
reader's service stopped and started again, journalled and verified as on Compose. The rule the live heal of 15 Nov
2026 kept holds here too: nothing fetches through the links from the first restart to the end of the settle.
"""

import json
import time

import pytest
from fault_support import FakeClock, injector_for
from heal_support import CRASHED, LABEL, READER, RESTORED, SERVICE, TARGET, four_providers
from hpc_support import PING, RESTART_LINK, FakeHpc, hpc_driver

from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.fault_recovery import RestoreNotVerified
from expcore_onedata.fault_settings import load_faults
from expcore_onedata.testing import write_platform

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
SETTLE_S = 30.0  # `heal_settle_s` of the four-provider federation
FETCHES = ("serves", "matrix", "sessions")  # reads through a provider; a transfer listing is none


def healing(tmp_path, **faults):
    """An injector for providers[1] over the HPC driver of a fake four-provider federation whose pair
    providers[0] -> providers[2] is stuck after the outage; with it the federation."""
    platform = write_platform(tmp_path, lambda data: four_providers(data, **faults))
    clock = FakeClock()
    federation = FakeHpc(clock, LABEL)
    federation.stuck = {(READER, CRASHED)}
    return injector_for(platform, hpc_driver(federation), target=TARGET, clock=clock), federation


def step(injector, name):
    found = [s for s in injector.snapshot()["steps"] if s["step"] == name]
    return found[-1] if found else None


def journal(tmp_path):
    path = tmp_path / "fault-journal" / "injections.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def execs(federation, inner):
    return [(argv[2], bound) for argv, bound in federation.calls if argv[1] == "exec" and argv[4:] == inner]


def test_o56_all_links_execs_restart_link_into_every_providers_service_and_fetches_nothing_inside_the_settle(
    tmp_path,
):
    """O56: the all-links sequence on HPC: every provider's node pinged, then its link restarted, each by an exec into
    its Apptainer service (the ping within the command bound, the restart within the RPC's 150 s), in label order and
    one after another; then the settle, with no read through any provider (no serving probe, no session gate, no link
    check) from the first restart to its end, and only then the gate and every pair, which pass: the restore is
    verified, the stuck pair healed by one round, no service restarted."""
    injector, federation = healing(tmp_path, heal="all_links")
    injector.start(RUN)
    assert injector.clear() is True
    order = [SERVICE[label] for label in sorted(LABEL.values())]
    assert execs(federation, PING) == [(service, 10.0) for service in order]
    assert execs(federation, RESTART_LINK) == [(service, 150.0) for service in order]
    heal = step(injector, "heal")
    assert (heal["outcome"], heal["shared_source"]) == ("healed", CRASHED)
    restarts = [a for a in heal["actions"] if a["action"] == "restart_link"]
    assert [(a["provider"], a["result"]) for a in restarts] == [(label, "ok") for label in sorted(LABEL.values())]
    first = next(index for index, entry in enumerate(federation.log) if entry[0] == "restart_link")
    last = max(at for verb, _, at in federation.log if verb == "restart_link")
    inside = [entry for entry in federation.log[first:] if entry[0] in FETCHES and entry[2] < last + SETTLE_S]
    assert inside == []
    after = [entry for entry in federation.log[first:] if entry[0] in FETCHES]
    assert after[0][2] >= last + SETTLE_S
    assert ("matrix", "all") in [(what, label) for what, label, _ in after]
    assert federation.acted("stop", "start") == [("stop", RESTORED), ("start", RESTORED)]
    lines = journal(tmp_path)
    assert next(line.get("healing") for line in lines if line["event"] == "clear_failed") is True
    assert (lines[-1]["event"], lines[-1]["how"]) == ("cleared", "restored")


def test_o56_the_container_fallback_restarts_the_readers_service_journalled_and_verified(tmp_path):
    """O56: link restarts that mend nothing leave the pair stuck, so the reader's service is restarted, through the
    injector's own stop and start: journalled as an injection of its own before the stop (`heal`), the service seen
    down, started anew (a new identity) and serving before the settle, its gate and every pair; the pair passes, so
    the restart and the restore are cleared and verified."""
    injector, federation = healing(tmp_path, heal="all_links")
    federation.link_fixes = dict.fromkeys(LABEL.values(), ())
    federation.container_fixes.add(READER)
    injector.start(RUN)
    assert injector.clear() is True
    assert federation.acted("stop", "start") == [
        ("stop", RESTORED),
        ("start", RESTORED),
        ("stop", READER),
        ("start", READER),
    ]
    assert federation.launches(SERVICE[READER]) == 2
    (restart,) = [a for a in step(injector, "heal")["actions"] if a["action"] == "container_restart"]
    assert (restart["provider"], restart["result"], restart["settle_s"]) == (READER, "restored", SETTLE_S)
    lines = journal(tmp_path)
    (child,) = [line for line in lines if line.get("heal") is True]
    assert (child["event"], child["target"], child["service"]) == ("injecting", READER, SERVICE[READER])
    events = [line["event"] for line in lines if line.get("id") == child["id"]]
    assert events == ["injecting", "stopping", "injected", "cleared"]
    assert injector.snapshot()["restore_verified"] is True


def test_o56_a_fallback_restart_that_fails_keeps_the_gate_closed_until_the_gates_recovery_verifies_it(tmp_path):
    """O56: the reader's service, stopped by the fallback, does not start again: the restart is unverified
    (`heal_restart_failed`), its settle is held all the same, the restore fails closed and both injections stay open.
    The fault gate's recovery, once the injector's process is gone, tries the start again and fails closed while it
    still fails; once it starts, the gate restarts the reader, holds a settle before any read through it (a wait
    that charges no run), then finds it serving with a new identity, passes its gate and clears it, then the restore
    it healed, and opens."""
    injector, federation = healing(tmp_path, heal="all_links")
    federation.link_fixes = dict.fromkeys(LABEL.values(), ())
    federation.container_fixes.add(READER)
    federation.start_fails.add(SERVICE[READER])
    injector.start(RUN)
    assert injector.clear() is False
    assert step(injector, "cleared")["how"] == "heal_restart_failed"
    (restart,) = [a for a in step(injector, "heal")["actions"] if a["action"] == "container_restart"]
    assert (restart["result"], restart["settle_s"]) == ("start_failed", SETTLE_S)
    with pytest.raises(RestoreNotVerified, match="heal_restart_failed"):
        injector.check()
    assert federation.running(SERVICE[READER]) is False
    injector._unlock()  # the injector's process ends: the kernel drops its locks
    injector._release_admission()

    platform = tmp_path / "platform.toml"
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    shift = [3600.0]  # the gate's wall clock, past every settle the injector's heal held
    clock = federation.clock
    gate = FaultGate(
        settings,
        hpc_driver(federation),
        labels=SERVICE,
        clock=clock,
        sleep=clock.sleep,
        wall=lambda: time.time() + shift[0],
    )
    closed = gate()
    assert (closed.ok, closed.deferred) == (False, False)
    assert "not cleared" in closed.detail
    assert federation.running(SERVICE[READER]) is False

    federation.start_fails.clear()
    started = len(federation.log)
    settling = gate()
    assert (settling.ok, settling.deferred, settling.charge) == (False, True, False)
    assert federation.running(SERVICE[READER]) is True
    assert [entry[0] for entry in federation.log[started:] if entry[0] in ("sessions", "matrix")] == []

    shift[0] += 2 * SETTLE_S
    opened = gate()
    assert opened.ok is True, opened.detail
    lines = journal(tmp_path)
    cleared = {line["id"] for line in lines if line["event"] == "cleared"}
    assert {line["id"] for line in lines if line["event"] == "injecting"} <= cleared
    assert federation.acted("start")[-1] == ("start", READER)
