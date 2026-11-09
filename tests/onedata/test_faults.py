"""The fault injector: sanctioned, journalled, cleared in finally and verified by a changed identity (O10, O11, O12).

Every test runs against `FakeServices`; nothing here contacts Docker or a deployment.
"""

import json
import multiprocessing

import pytest
from fault_support import (
    LOCAL_TARGET,
    OUTAGE_TARGET,
    SERVICES,
    FakeClock,
    FakeServices,
    inject_and_die,
    injector_for,
    write_faults,
)
from platform_support import configured_values

from expcore.config.loading import ConfigError
from expcore.errors import InfraError, is_treatable
from expcore.records.injections import FAULT_NAMESPACE
from expcore_onedata.config import load_platform
from expcore_onedata.fault_journal import JOURNAL
from expcore_onedata.faults import (
    INJECTOR_FAILURE,
    PROVIDER_OUTAGE,
    FaultInjector,
    FaultRefused,
    Unsanctioned,
    load_faults,
    provider_outage,
)

OUTAGE_SERVICE = SERVICES["beta"]
RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1, "factors": {}}


def journal_lines(tmp_path):
    path = tmp_path / "fault-journal" / JOURNAL
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def open_injections(tmp_path):
    lines = journal_lines(tmp_path)
    cleared = {line["id"] for line in lines if line["event"] == "cleared"}
    return [line for line in lines if line["event"] == "injecting" and line["id"] not in cleared]


def test_o10_the_platform_registers_a_treatable_outage_and_an_untreatable_injector_failure():
    """O10: a condition may declare the outage, never the injector's own failure."""
    assert is_treatable(PROVIDER_OUTAGE) is True
    assert is_treatable(INJECTOR_FAILURE) is False


# --- O10: the sanction ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("argument", [False, 1, "yes", None])
def test_o10_an_injector_without_the_callers_sanction_raises_before_anything_runs(tmp_path, argument):
    """O10: the explicit argument must be exactly True; the driver is never called."""
    platform = write_faults(tmp_path)
    config = load_platform(platform, require_secrets=False)
    services = FakeServices()
    with pytest.raises(Unsanctioned, match="sanctioned=True from its caller"):
        FaultInjector(config, load_faults(platform, config), OUTAGE_TARGET, sanctioned=argument, driver=services)
    assert services.calls == []


@pytest.mark.parametrize("flag", [False, None])
def test_o10_an_injector_without_the_configurations_sanction_raises(tmp_path, flag):
    """O10: platform.toml must say `sanctioned = true`; absent is false."""
    platform = write_faults(tmp_path, sanctioned=flag) if flag is not None else write_faults(tmp_path)
    if flag is None:
        text = platform.read_text(encoding="utf-8").replace("sanctioned = true\n", "")
        platform.write_text(text, encoding="utf-8")
    services = FakeServices()
    with pytest.raises(Unsanctioned, match=r"'sanctioned = true' in platform\.toml"):
        injector_for(platform, services)
    assert services.calls == []


def test_o10_a_sanction_that_is_not_a_boolean_is_refused_at_load(tmp_path):
    """O10: only true sanctions; a string that looks like yes is a mistake in the file."""
    platform = write_faults(tmp_path, sanctioned="yes")
    config = load_platform(platform, require_secrets=False)
    with pytest.raises(ConfigError, match="key 'sanctioned' must be true or false"):
        load_faults(platform, config)


# --- O11: inject, clear, verify -------------------------------------------------------------------------------------


def test_o11_the_injection_is_journalled_durably_before_the_service_is_stopped(tmp_path, monkeypatch):
    """O11: when the stop command runs, the journal already holds the injection, fsynced with its new directory."""
    synced: list[int] = []
    real_fsync = __import__("os").fsync
    monkeypatch.setattr("os.fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    services = FakeServices()
    seen_at_stop: list[list] = []
    services.on_stop = lambda service: seen_at_stop.append(open_injections(tmp_path))
    injector = injector_for(write_faults(tmp_path), services)
    injector.start(RUN)
    assert len(seen_at_stop) == 1
    (line,) = seen_at_stop[0]
    assert (line["target"], line["service"]) == (OUTAGE_TARGET, OUTAGE_SERVICE)
    assert len(synced) >= 3  # the line, the new journal file's directory, and the new directory's parent
    assert services.running(OUTAGE_SERVICE) is False
    assert [line["event"] for line in journal_lines(tmp_path)] == ["injecting", "injected"]
    injector.finish()


def test_o11_finish_brings_the_provider_back_with_a_new_identity_and_records_every_step(tmp_path):
    """O11: the restore counts because the identity changed; each step is a clock-labelled reading."""
    clock = FakeClock()
    services = FakeServices()
    before = services.identity(OUTAGE_SERVICE)
    injector = injector_for(write_faults(tmp_path), services, clock=clock)
    injector.start(RUN)
    clock.now += 30  # the agent's run
    snapshot = injector.finish()
    assert services.running(OUTAGE_SERVICE) is True
    assert services.identity(OUTAGE_SERVICE) != before
    assert snapshot["ok"] is True
    assert (snapshot["run_key"], snapshot["attempt"], snapshot["target"]) == (RUN["run_key"], 1, OUTAGE_TARGET)
    assert snapshot["injections"] == [{"target": OUTAGE_TARGET, "injected": True, "cleared": True, "verified": True}]
    steps = [(s["step"], s["ok"]) for s in snapshot["steps"]]
    assert steps == [("sanctioned", True), ("injected", True), ("cleared", True), ("verified", True)]
    times = [s["at_s"] for s in snapshot["steps"]]
    assert times == sorted(times)
    assert (times[0], times[-1] >= 30) == (0.0, True)
    assert snapshot["steps"][-1]["identity_changed"] is True
    assert open_injections(tmp_path) == []
    assert injector.clear() is True  # again: nothing left to do


def test_o11_a_restore_that_does_not_change_the_identity_is_a_failure(tmp_path):
    """O11: back and running, but with the identity it had: not a restore. The snapshot says so, the journal keeps
    the injection open, and the next start fails as the injector's own failure instead of running on."""
    services = FakeServices(start_keeps_identity=True)
    platform = write_faults(tmp_path)
    injector = injector_for(platform, services)
    injector.start(RUN)
    snapshot = injector.finish()
    assert services.running(OUTAGE_SERVICE) is True  # ready, yet not restarted as far as the identity shows
    assert snapshot["ok"] is False
    assert snapshot["injections"][0]["verified"] is False
    assert snapshot["steps"][-1] == {
        **snapshot["steps"][-1],
        "step": "verified",
        "ok": False,
        "identity_changed": False,
    }
    assert len(open_injections(tmp_path)) == 1
    with pytest.raises(InfraError) as caught:
        injector_for(platform, services).start({**RUN, "attempt": 2})
    assert (caught.value.kind, caught.value.target) == (INJECTOR_FAILURE, OUTAGE_TARGET)


def test_o11_a_raising_trial_still_clears_the_fault(tmp_path):
    """O11: the session clears in finally, whatever the agent's run raised."""
    services = FakeServices()
    injector = injector_for(write_faults(tmp_path), services)
    seen_down: list[bool] = []

    def trial() -> None:
        with injector.session(RUN):
            seen_down.append(not services.running(OUTAGE_SERVICE))
            raise RuntimeError("the trial broke")

    with pytest.raises(RuntimeError, match="the trial broke"):
        trial()
    assert seen_down == [True]
    assert services.running(OUTAGE_SERVICE) is True
    assert open_injections(tmp_path) == []
    assert injector.finish()["ok"] is True  # the reading is still there for the record


def test_o11_an_injection_that_never_took_effect_fails_the_start_and_leaves_nothing_open(tmp_path):
    """O11: a stop that does not take the provider down is not an injection: the start fails as the injector's own,
    untreatable failure, and the provider, never down, is left as it was."""
    services = FakeServices(stop_noop=True)
    before = services.identity(OUTAGE_SERVICE)
    injector = injector_for(write_faults(tmp_path), services)
    with pytest.raises(InfraError, match="could not be taken down") as caught:
        injector.start(RUN)
    assert caught.value.kind == INJECTOR_FAILURE
    assert (services.running(OUTAGE_SERVICE), services.identity(OUTAGE_SERVICE)) == (True, before)
    assert open_injections(tmp_path) == []
    assert injector.snapshot()["injections"][0]["injected"] is False


def test_o11_a_crash_between_inject_and_clear_leaves_no_provider_down_after_the_next_start(tmp_path):
    """O11: a process takes the provider down and dies before clearing; the next start brings it back with a new
    identity before anything else runs, journals it cleared and says so in its reading."""
    platform, state = write_faults(tmp_path), tmp_path / "services.json"
    FakeServices(path=state)  # the services, running, shared through the file
    child = multiprocessing.get_context("spawn").Process(
        target=inject_and_die, args=(str(platform), str(state), RUN["run_key"])
    )
    child.start()
    child.join(60)
    assert child.exitcode == 0
    services = FakeServices(path=state)
    assert services.running(OUTAGE_SERVICE) is False  # left down by the dead process
    assert len(open_injections(tmp_path)) == 1
    before_restart = services.identity(OUTAGE_SERVICE)
    injector = injector_for(platform, services)
    injector.start({**RUN, "attempt": 2})
    recovered = [s for s in injector.snapshot()["steps"] if s["step"] == "recovered"]
    assert [(s["ok"], s["how"], s["target"]) for s in recovered] == [(True, "restored", OUTAGE_TARGET)]
    assert [s["step"] for s in injector.snapshot()["steps"]][:3] == ["sanctioned", "recovered", "injected"]
    injector.finish()
    assert services.running(OUTAGE_SERVICE) is True
    assert services.identity(OUTAGE_SERVICE) != before_restart
    assert open_injections(tmp_path) == []


def test_o11_recovery_leaves_alone_a_provider_a_live_run_holds_down(tmp_path):
    """O11: recovery settles only injections whose holder is gone, never one a live injector holds on purpose."""
    platform, services = write_faults(tmp_path), FakeServices()
    holder = injector_for(platform, services)
    holder.start(RUN)
    other = injector_for(platform, services, target=LOCAL_TARGET)
    other.start({**RUN, "run_key": "task=task-b|model=m1|seed=1"})
    assert services.running(OUTAGE_SERVICE) is False  # still down: its injector is alive
    assert services.running(SERVICES["alpha"]) is False
    with pytest.raises(InfraError, match="held by another run"):
        injector_for(platform, services).start({**RUN, "attempt": 2})
    assert holder.finish()["ok"] is True
    assert other.finish()["ok"] is True


def test_o11_an_unreadable_journal_fails_the_start_closed(tmp_path):
    """O11: a journal that cannot be read may hide a provider left down, so nothing is injected."""
    platform, services = write_faults(tmp_path), FakeServices()
    journal = tmp_path / "fault-journal" / JOURNAL
    journal.parent.mkdir()
    journal.write_text("not json\n{}\n", encoding="utf-8")
    with pytest.raises(InfraError, match="journal cannot be read"):
        injector_for(platform, services).start(RUN)
    assert ("stop", OUTAGE_SERVICE) not in services.calls


def test_o11_a_provider_that_cannot_be_read_before_the_injection_is_not_touched(tmp_path):
    """O11: without the identity read before, no restore could be confirmed, so the provider is never stopped."""
    services = FakeServices(unreadable=True)
    with pytest.raises(InfraError, match="could not be read before the injection"):
        injector_for(write_faults(tmp_path), services).start(RUN)
    assert [call for call, _ in services.calls] == ["observe"]


# --- O12: names live in platform.toml only --------------------------------------------------------------------------


def test_o12_a_failure_the_platform_attributes_to_a_provider_names_it_by_its_label(tmp_path):
    """O12: the target the core compares is the label the condition declares, never the provider's name."""
    config = load_platform(write_faults(tmp_path), require_secrets=False)
    error = provider_outage(config, "beta", "the provider stopped answering")
    assert (error.kind, error.target) == (PROVIDER_OUTAGE, OUTAGE_TARGET)
    assert "beta" not in str(error)


def test_o12_the_reading_and_the_messages_hold_no_configured_name(tmp_path):
    """O12: the snapshot, every step and every refusal name the provider by its label, never by a provider, service
    or host name; identities are digests."""
    platform, services = write_faults(tmp_path), FakeServices()
    injector = injector_for(platform, services)
    injector.start(RUN)
    snapshot = json.dumps(injector.finish())
    secrets = [*configured_values(tmp_path), *SERVICES.values(), *(services.identity(s) for s in SERVICES.values())]
    assert [value for value in secrets if value.lower() in snapshot.lower()] == []
    config = load_platform(platform, require_secrets=False)
    with pytest.raises(FaultRefused) as caught:
        FaultInjector(config, load_faults(platform, config), "providers[7]", sanctioned=True, driver=services)
    assert "providers[7]" not in str(caught.value)  # a rejected target is never echoed
    other = tmp_path / "other"
    other.mkdir()
    unlisted = write_faults(other, services={"alpha": "store-one"})
    config = load_platform(unlisted, require_secrets=False)
    with pytest.raises(FaultRefused, match=r"no service is declared for providers\[1\]"):
        FaultInjector(config, load_faults(unlisted, config), OUTAGE_TARGET, sanctioned=True, driver=services)


def test_o12_a_service_entry_is_named_by_its_position_in_messages(tmp_path):
    """O12: a mistake in `[faults.services]` is reported by the entry's position, never by a provider or a service."""
    platform = write_faults(tmp_path, services={"alpha": "store-one", "gamma-x": "store-three"})
    config = load_platform(platform, require_secrets=False)
    with pytest.raises(ConfigError, match=r"entry 1 is not a provider declared in \[providers\]") as caught:
        load_faults(platform, config)
    assert "gamma-x" not in str(caught.value)
    platform = write_faults(tmp_path, services={"alpha": "store one!"})
    with pytest.raises(ConfigError, match="entry 0 must be a service name") as caught:
        load_faults(platform, config)
    assert "store one!" not in str(caught.value)


def test_c01_the_injector_writes_under_the_namespace_the_core_reads(tmp_path):
    """C01: the treatment rule finds the injector's reading where the injector puts it."""
    assert injector_for(write_faults(tmp_path), FakeServices()).namespace == FAULT_NAMESPACE


def test_o10_the_live_fault_test_needs_its_own_switch_beside_the_platform():
    """O10: the live test that takes a provider down runs only with EXPCORE_LIVE_FAULTS=1 as well as a platform."""
    from pathlib import Path

    live = (Path(__file__).resolve().parents[1] / "live" / "test_contract.py").read_text(encoding="utf-8")
    assert 'os.environ.get("EXPCORE_LIVE_FAULTS") != "1"' in live
    assert "@FAULTS\ndef test_a_provider_taken_down" in live


def test_o11_a_start_command_that_keeps_failing_is_recorded_as_such_and_left_open(tmp_path):
    """O11: the restore is tried the configured number of times; the reading says the start failed, and the journal
    keeps the injection open for the next start to settle."""
    services = FakeServices()
    injector = injector_for(write_faults(tmp_path), services)
    injector.start(RUN)
    services.knobs["start_fails"] = True
    snapshot = injector.finish()
    assert [call for call, _ in services.calls].count("start") == 2  # clear_attempts in the fixture
    cleared = next(step for step in snapshot["steps"] if step["step"] == "cleared")
    assert (cleared["ok"], cleared["how"], snapshot["ok"]) == (False, "start_failed", False)
    assert len(open_injections(tmp_path)) == 1
