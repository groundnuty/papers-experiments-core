"""O35: the session gate and the bounded heal of a link a restore left stuck, through the injector, on fakes.

The federation has four providers; `beta` (providers[1]) is the one the injector takes down. The pattern is the one
seen live: during the outage `alpha` (providers[0]) fetched from it and its link crashed, so after the restore readers
of `alpha` fail while every pair `alpha` reads passes, and the restored provider's own pairs pass. The fake driver
scripts each pair, each reader's `ping` and `restart_link`, and what a link or a container restart mends or breaks.
"""

import json

import pytest
from fault_support import FakeClock, FakeServices, faults_table, injector_for

from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore_onedata.config import load_platform
from expcore_onedata.fault_heal import LinkMatrix, PairState
from expcore_onedata.fault_recovery import Recovery, RestoreNotVerified
from expcore_onedata.fault_settings import DEFAULT_GATE_TIMEOUT_S, DEFAULT_HEAL_TIMEOUT_S, load_faults
from expcore_onedata.testing import write_platform

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
NAMES = ("alpha", "beta", "gamma", "delta")
STORES = {name: f"store-{name}" for name in NAMES}
LABEL = {STORES[name]: f"providers[{index}]" for index, name in enumerate(NAMES)}
SERVICE = {label: service for service, label in LABEL.items()}
CRASHED, RESTORED, READER, SECOND = (f"providers[{index}]" for index in range(4))
TARGET = RESTORED


class HealServices(FakeServices):
    """Four providers' services with a scripted link check, session gate, ping and link restart.

    `stuck` holds the failing pairs as (reader, source) labels. `link_fixes[reader]` is what a link restart of that
    reader mends (its own stuck pairs by default, `()` for nothing), `link_breaks[reader]` the pairs it breaks, and
    `container_fixes` the readers a container restart mends. `grow` adds pairs to `stuck` before the given check.
    """

    def __init__(self, **knobs):
        super().__init__(tuple(STORES.values()), **knobs)
        self.stuck: set[tuple[str, str]] = {(READER, CRASHED)}
        self.pong: dict[str, bool] = {}
        self.link_result: dict[str, str] = {}
        self.link_fixes: dict[str, tuple[tuple[str, str], ...]] = {}
        self.link_breaks: dict[str, tuple[tuple[str, str], ...]] = {}
        self.container_fixes: set[str] = set()
        self.grow: dict[int, set[tuple[str, str]]] = {}
        self.gate_detail: str | None = None
        self.gate_after: int = 0  # readings of the gate that answer not ready first
        self.checks = 0
        self.clock: FakeClock | None = None
        self.link_s = 0.0  # seconds a link restart takes on the clock
        self.actions: list[tuple[str, str]] = []

    def _matrix(self) -> LinkMatrix:
        labels = list(LABEL.values())
        pairs = tuple(
            PairState(reader, source, (reader, source) not in self.stuck, 0.1, (reader, source) in self.stuck)
            for reader in labels
            for source in labels
            if reader != source
        )
        failing = [f"{source}->{reader}" for reader, source in sorted(self.stuck)]
        return LinkMatrix(not self.stuck, "stuck: " + ", ".join(failing) if failing else "every pair passed", pairs)

    def link_matrix(self, service):
        self.checks += 1
        self.stuck |= self.grow.get(self.checks, set())
        return self._matrix()

    def sessions_ready(self, service, gate_probe):
        if self.gate_after > 0:
            self.gate_after -= 1
            return "providers[3] holds an error for the restored provider"
        return self.gate_detail

    def ping(self, service):
        self.actions.append(("ping", LABEL[service]))
        return self.pong.get(LABEL[service], True)

    def restart_link(self, service):
        reader = LABEL[service]
        self.actions.append(("restart_link", reader))
        if self.clock is not None:
            self.clock.sleep(self.link_s)
        fixes = self.link_fixes.get(reader)
        mended = {pair for pair in self.stuck if pair[0] == reader} if fixes is None else set(fixes)
        self.stuck = (self.stuck - mended) | set(self.link_breaks.get(reader, ()))
        return self.link_result.get(reader, "ok")

    def stop(self, service):
        self.actions.append(("stop", LABEL[service]))
        super().stop(service)

    def start(self, service):
        self.actions.append(("start", LABEL[service]))
        super().start(service)
        if LABEL[service] in self.container_fixes:
            self.stuck = {pair for pair in self.stuck if pair[0] != LABEL[service]}


def four_providers(data, **faults):
    for name, index in (("gamma", 3), ("delta", 4)):
        data["providers"][name] = {"url": f"https://{name}.test", "id": f"pr-{name}-{index}"}
    data["faults"] = faults_table(services=dict(STORES), **faults)


def injector(tmp_path, services=None, **faults):
    platform = write_platform(tmp_path, lambda data: four_providers(data, **faults))
    services = services or HealServices()
    clock = FakeClock()
    services.clock = clock
    return injector_for(platform, services, target=TARGET, clock=clock), services


def step(injector, name):
    found = [s for s in injector.snapshot()["steps"] if s["step"] == name]
    return found[-1] if found else None


def acted(services, *verbs):
    return [action for action in services.actions if action[0] in verbs]


def journal_lines(tmp_path):
    path = tmp_path / "fault-journal" / "injections.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- the settings ---------------------------------------------------------------------------------------------------


def test_o35_the_heal_is_off_by_default_and_its_bounds_default_to_the_designed_ones(tmp_path):
    """O35: without `heal`, a stuck link fails the restore closed as before; the gate waits 240 s, the heal 900 s."""
    platform = write_platform(tmp_path, four_providers)
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    assert (settings.heal, settings.gate_timeout_s, settings.heal_timeout_s) == ("off", 240.0, 900.0)
    assert (DEFAULT_GATE_TIMEOUT_S, DEFAULT_HEAL_TIMEOUT_S) == (240.0, 900.0)


@pytest.mark.parametrize("value", ["always", "", 1, True])
def test_o35_a_heal_mode_that_is_not_declared_is_refused_naming_the_key(tmp_path, value):
    """O35: `heal` is one of "off", "link" and "link_then_container"; anything else is refused at load."""
    platform = write_platform(tmp_path, lambda data: four_providers(data, heal=value))
    with pytest.raises(ConfigError, match="heal"):
        load_faults(platform, load_platform(platform, require_secrets=False))


# --- the heal -------------------------------------------------------------------------------------------------------


def test_o35_off_fails_a_stuck_restore_closed_as_before_and_acts_on_nothing(tmp_path):
    """O35: with the heal off a stuck pair leaves the restore unverified (`links_stuck`), and nothing is pinged or
    restarted."""
    injector_, services = injector(tmp_path)
    injector_.start(RUN)
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "links_stuck"
    assert acted(services, "ping", "restart_link") == []
    assert step(injector_, "heal") is None
    assert step(injector_, "gate")["ok"] is True


def test_o35_a_stuck_pair_is_healed_by_its_readers_link_restart_and_the_restore_is_verified(tmp_path):
    """O35: the reader of the stuck pair is pinged and its link restarted; the gate and every pair pass after it, so
    the restore is verified, with the heal recorded: the action, its result, the gate and the matrix after it."""
    injector_, services = injector(tmp_path, heal="link")
    injector_.start(RUN)
    assert injector_.clear() is True
    snapshot = injector_.snapshot()
    assert snapshot["ok"] is True
    assert snapshot["restore_verified"] is True
    heal = step(injector_, "heal")
    assert (heal["ok"], heal["outcome"], heal["shared_source"]) == (True, "healed", CRASHED)
    assert [(a["action"], a["provider"], a["result"]) for a in heal["actions"]] == [
        ("ping", READER, "pong"),
        ("restart_link", READER, "ok"),
    ]
    restart = heal["actions"][1]
    assert restart["gate"]["ok"] is True
    assert restart["matrix_after"]["ok"] is True
    assert restart["start_utc"] <= restart["end_utc"]
    assert heal["first"]["failing_sources"] == {CRASHED: 1}
    assert acted(services, "restart_link") == [("restart_link", READER)]


def test_o35_a_pair_only_a_container_restart_mends_is_healed_in_link_then_container(tmp_path):
    """O35: a link restart that mends nothing is followed by the reader's container restart, through the injector's
    own stop and start, journalled as an injection of its own and cleared once it serves; then the pair passes."""
    services = HealServices()
    services.link_fixes[READER] = ()
    services.container_fixes.add(READER)
    injector_, services = injector(tmp_path, services, heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is True
    heal = step(injector_, "heal")
    assert [(a["action"], a["provider"]) for a in heal["actions"]] == [
        ("ping", READER),
        ("restart_link", READER),
        ("container_restart", READER),
    ]
    assert heal["actions"][2]["result"] == "restored"
    assert acted(services, "stop", "start") == [
        ("stop", RESTORED),
        ("start", RESTORED),
        ("stop", READER),
        ("start", READER),
    ]
    healed = [line for line in journal_lines(tmp_path) if line.get("heal") is True]
    assert [(line["event"], line["target"]) for line in healed] == [("injecting", READER)]
    assert "cleared" in [line["event"] for line in journal_lines(tmp_path) if line.get("id") == healed[0]["id"]]


def test_o35_a_heal_action_that_breaks_a_pair_that_passed_stops_and_fails_closed(tmp_path):
    """O35: a link restart after which a pair that passed before it fails stops the heal at once
    (`heal_broke_pair`): the new failure is not healed, and no container is restarted."""
    services = HealServices()
    services.link_breaks[READER] = ((CRASHED, READER),)
    injector_, services = injector(tmp_path, services, heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "heal_broke_pair"
    heal = step(injector_, "heal")
    assert heal["outcome"] == "heal_broke_pair"
    assert "providers[2]->providers[0]" in heal["why"]
    assert acted(services, "restart_link", "stop", "start") == [
        ("stop", RESTORED),
        ("start", RESTORED),
        ("restart_link", READER),
    ]
    with pytest.raises(RestoreNotVerified, match="heal_broke_pair"):
        injector_.check()


def test_o35_a_badrpc_is_never_retried_and_a_later_clear_does_not_heal_again(tmp_path):
    """O35: a link restart that timed out (`badrpc_timeout`) may still be running on the node, so it is never issued
    again: the matrix decides, and the restore fails closed naming what was tried; a later clear of the same
    injection only checks."""
    services = HealServices()
    services.link_fixes[READER] = ()
    services.link_result[READER] = "badrpc_timeout"
    injector_, services = injector(tmp_path, services, heal="link")
    injector_.start(RUN)
    assert injector_.clear() is False
    assert injector_.clear() is False
    assert acted(services, "restart_link") == [("restart_link", READER)]
    assert step(injector_, "cleared")["how"] == "links_stuck"
    tried = r"the link heal tried .*restart_link on providers\[2\] \(badrpc_timeout\)"
    with pytest.raises(RestoreNotVerified, match=tried):
        injector_.check()


def test_o35_after_a_badrpc_the_container_step_follows_and_not_a_second_link_restart(tmp_path):
    """O35: with `link_then_container`, a timed-out link restart that mended nothing goes on to the container."""
    services = HealServices()
    services.link_fixes[READER] = ()
    services.link_result[READER] = "badrpc_timeout"
    services.container_fixes.add(READER)
    injector_, services = injector(tmp_path, services, heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is True
    assert acted(services, "restart_link") == [("restart_link", READER)]
    assert ("stop", READER) in services.actions


def test_o35_no_pong_fails_closed_in_link_mode_and_goes_to_the_container_in_link_then_container(tmp_path):
    """O35: a reader whose node answers no pong cannot have its link restarted: `link` fails closed at once, and
    `link_then_container` restarts its container instead."""
    services = HealServices()
    services.pong[READER] = False
    injector_, services = injector(tmp_path, services, heal="link")
    injector_.start(RUN)
    assert injector_.clear() is False
    assert acted(services, "restart_link") == []
    assert "no pong" in step(injector_, "heal")["why"]

    services = HealServices()
    services.pong[READER] = False
    services.container_fixes.add(READER)
    injector_, services = injector(tmp_path / "second", services, heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is True
    assert acted(services, "restart_link") == []
    assert ("stop", READER) in services.actions


def test_o35_the_source_of_a_failing_pair_is_never_acted_on(tmp_path):
    """O35: the crashed source's readers hold the stale state, so the source is never pinged or restarted while it is
    the source of a failing pair, whatever its own pairs show: here it also reads from the restored provider through a
    stuck link, and is healed only once its readers' pairs pass (the second round)."""
    services = HealServices()
    services.stuck = {(READER, CRASHED), (SECOND, CRASHED), (CRASHED, RESTORED)}
    injector_, services = injector(tmp_path / "a", services, heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is True
    order = [label for verb, label in acted(services, "restart_link")]
    assert order == [READER, SECOND, CRASHED]  # CRASHED only after no failing pair has it as its source

    services = HealServices()
    services.stuck = {(READER, CRASHED), (SECOND, CRASHED)}
    services.link_fixes = {READER: (), SECOND: ()}
    injector_, services = injector(tmp_path / "b", services, heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is False
    assert all(label != CRASHED for _, label in services.actions)


def test_o35_a_stuck_set_that_grew_after_the_first_check_is_read_fresh_before_any_action(tmp_path):
    """O35: a pair untested or passing at the restore's first check can fail later (seen live, 46 minutes on), so the
    heal reads every pair again before it chooses, and heals the readers that reading shows."""
    services = HealServices()
    services.grow = {2: {(SECOND, CRASHED)}}  # the heal's own first reading shows a second reader stuck
    injector_, services = injector(tmp_path, services, heal="link")
    injector_.start(RUN)
    assert injector_.clear() is True
    heal = step(injector_, "heal")
    assert heal["first"]["failing_sources"] == {CRASHED: 1}
    assert heal["before"]["failing_sources"] == {CRASHED: 2}
    assert heal["before"]["shared_source"] == CRASHED
    assert acted(services, "restart_link") == [("restart_link", READER), ("restart_link", SECOND)]


def test_o35_the_bounds_hold_one_of_each_action_per_provider_and_two_rounds(tmp_path):
    """O35: when nothing mends, each reader gets at most one link restart and one container restart in the whole
    restore, and the restore fails closed (`links_stuck`)."""
    services = HealServices()
    services.stuck = {(READER, CRASHED), (SECOND, CRASHED)}
    services.link_fixes = {READER: (), SECOND: ()}
    injector_, services = injector(tmp_path, services, heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is False
    assert acted(services, "restart_link") == [("restart_link", READER), ("restart_link", SECOND)]
    assert acted(services, "stop") == [("stop", RESTORED), ("stop", READER), ("stop", SECOND)]
    assert step(injector_, "cleared")["how"] == "links_stuck"


def test_o35_no_action_starts_that_the_heals_time_bound_cannot_cover(tmp_path):
    """O35: `heal_timeout_s` bounds the heal: an action starts only when its own longest wait and the gate's fit in
    what is left, so a slow first link restart leaves the second reader untouched and the restore fails closed."""
    services = HealServices()
    services.stuck = {(READER, CRASHED), (SECOND, CRASHED)}
    services.link_s = 400.0
    # a link restart reserves ping (120 s) + its command (150 s) + 5 s of quiet + the gate (5 s): 280 s
    injector_, services = injector(tmp_path, services, heal="link", gate_timeout_s=5, heal_timeout_s=600)
    injector_.start(RUN)
    assert injector_.clear() is False
    assert acted(services, "restart_link") == [("restart_link", READER)]
    heal = step(injector_, "heal")
    assert heal["outcome"] == "links_stuck"
    assert "heal_timeout_s" in heal["why"]


def test_o35_the_restored_providers_container_is_never_restarted_by_the_heal(tmp_path):
    """O35: a stuck pair the restored provider reads gets its link restarted, never its container: that is the
    outage again."""
    services = HealServices()
    services.stuck = {(RESTORED, CRASHED)}
    services.link_fixes[RESTORED] = ()
    injector_, services = injector(tmp_path, services, heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is False
    assert acted(services, "restart_link") == [("restart_link", RESTORED)]
    assert acted(services, "stop") == [("stop", RESTORED)]  # the injection's own stop alone
    assert step(injector_, "heal")["skipped"][0]["provider"] == RESTORED


def test_o35_a_settling_without_the_exclusive_hold_never_heals(tmp_path):
    """O35: the heal runs only under the injector's exclusive admission, between runs; a settling that is not asked to
    heal (the health check's recovery, which holds the federation shared) fails a stuck link closed and acts on
    nothing, whatever `heal` says."""
    injector_, services = injector(tmp_path, heal="link_then_container")
    injector_.start(RUN)
    platform = tmp_path / "platform.toml"
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    clock = FakeClock()
    recovery = Recovery(settings, services, clock=clock, sleep=clock.sleep, labels=SERVICE)
    (entry,) = recovery.entries()
    assert recovery.settle(entry).how == "links_stuck"
    assert acted(services, "ping", "restart_link") == []
    assert "heal" not in recovery.restore_record


def test_o35_the_injection_journals_its_gate_probe_before_the_stop_and_a_probe_not_made_stops_nothing(tmp_path):
    """O35: the gate's probe file is made before the injection and journalled with it, so the restore's gate reads a
    file the restored provider knew; one that cannot be made fails the start with nothing stopped."""
    services = HealServices()
    services.stuck = set()
    services.arm_gate = lambda service: "gate-file-1"
    seen: list[str | None] = []
    services.sessions_ready = lambda service, probe: seen.append(probe)
    injector_, services = injector(tmp_path, services)
    injector_.start(RUN)
    (line,) = [line for line in journal_lines(tmp_path) if line["event"] == "injecting"]
    assert line["gate_probe"] == "gate-file-1"
    assert injector_.clear() is True
    assert seen == ["gate-file-1"]

    services = HealServices()

    def broken(service):
        raise RuntimeError("the probe space cannot be written")

    services.arm_gate = broken
    injector_, services = injector(tmp_path / "second", services)
    with pytest.raises(InfraError, match="session gate's probe could not be made"):
        injector_.start(RUN)
    assert acted(services, "stop") == []


# --- the gate -------------------------------------------------------------------------------------------------------


def test_o35_the_gate_fails_closed_past_its_bound_and_no_link_is_checked(tmp_path):
    """O35: a gate that never passes (a peer keeps an error entry for the restored provider, or Onezone shows a
    provider offline) fails the restore closed (`sessions_not_ready`) once `gate_timeout_s` is over, before any link
    is checked, and records its wait."""
    services = HealServices()
    services.gate_detail = "Onezone does not show providers[1] online"
    injector_, services = injector(tmp_path, services, heal="link", gate_timeout_s=30)
    injector_.start(RUN)
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "sessions_not_ready"
    gate = step(injector_, "gate")
    assert (gate["ok"], gate["how"], gate["detail"]) == (False, "not_ready", services.gate_detail)
    assert gate["waited_s"] >= 30
    assert services.checks == 0
    assert acted(services, "ping", "restart_link") == []


def test_o35_the_gate_waits_for_the_peers_and_then_the_links_are_checked(tmp_path):
    """O35: readings that are not ready yet are polled again within the bound; the first that passes lets the link
    check run, and its wait is recorded."""
    services = HealServices()
    services.stuck = set()
    services.gate_after = 3
    injector_, services = injector(tmp_path, services, gate_timeout_s=30)
    injector_.start(RUN)
    assert injector_.clear() is True
    gate = step(injector_, "gate")
    assert (gate["ok"], gate["how"], gate["waited_s"]) == (True, "ready", 3.0)
    assert services.checks == 1


def test_o35_a_gate_that_fails_after_a_heal_action_stops_the_heal_closed(tmp_path):
    """O35: the gate runs again after each action, for its reader; one that does not pass stops the heal."""
    services = HealServices()
    original = services.restart_link

    def restart_then_unready(service):
        services.gate_detail = "providers[0] holds an error for providers[2]"
        return original(service)

    services.restart_link = restart_then_unready
    injector_, services = injector(tmp_path, services, heal="link", gate_timeout_s=10)
    injector_.start(RUN)
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "sessions_not_ready"
    assert step(injector_, "heal")["actions"][-1]["gate"]["ok"] is False
