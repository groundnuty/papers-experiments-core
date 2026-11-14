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
from expcore_onedata.admission import FederationLock
from expcore_onedata.config import load_platform
from expcore_onedata.fault_heal import LinkMatrix, PairState
from expcore_onedata.fault_recovery import Recovery, RestoreNotVerified
from expcore_onedata.fault_settings import (
    DEFAULT_GATE_TIMEOUT_S,
    DEFAULT_HEAL_SETTLE_S,
    DEFAULT_HEAL_TIMEOUT_S,
    load_faults,
)
from expcore_onedata.testing import write_platform

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
NAMES = ("alpha", "beta", "gamma", "delta")
STORES = {name: f"store-{name}" for name in NAMES}
LABEL = {STORES[name]: f"providers[{index}]" for index, name in enumerate(NAMES)}
SERVICE = {label: service for service, label in LABEL.items()}
CRASHED, RESTORED, READER, SECOND = (f"providers[{index}]" for index in range(4))
TARGET = RESTORED


class HealServices(FakeServices):
    """Four providers' services with a scripted link check, session gate, ping, link restart and transfer listings.

    `stuck` holds the failing pairs as (reader, source) labels. `link_fixes[reader]` is what a link restart of that
    reader mends: by default its own stuck pairs and nothing else, the shape seen live (each stale reader of a crashed
    source is cleared by its own `restart_link` alone, never by the source's or another reader's); `()` mends nothing.
    `link_breaks[reader]` is the pairs it breaks, `container_fixes` the readers a container restart mends, and `grow`
    adds pairs to `stuck` before the given check.

    `wedge_s` is the rule the live data settled: after a provider's link restarts, a fetch from it (any link check)
    within `wedge_s` seconds wedges every pair with it as the source, failing until that pair's reader's own link
    restarts; a fetch after the window finds the link up. (Live: a check 23 s after a restart left a reverse pair
    stale, while a clean 3-minute settle left every reverse pair healthy, even one minutes old.) `transfers` is the
    script of active-transfer readings, 0 once it runs out.
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
        self.ping_s = 0.0  # seconds a ping takes on the clock
        self.on_ping = None  # called with the reader's label while it is pinged
        self.on_check = None  # called before each link check
        self.wedge_s = 0.0
        self.restarted_at: dict[str, float] = {}  # each provider's last link restart, on the clock
        self.transfers: list[int] = []
        self.never_serving: set[str] = set()  # providers whose container, restarted, never serves
        self.actions: list[tuple[str, str]] = []
        self.at: list[tuple[str, str, float]] = []  # each action with the clock's time
        self.checked_at: list[float] = []  # each link check's time

    def _matrix(self) -> LinkMatrix:
        labels = list(LABEL.values())
        pairs = tuple(
            PairState(reader, source, (reader, source) not in self.stuck, 0.1, (reader, source) in self.stuck)
            for reader in labels
            for source in labels
            if reader != source
        )
        shown = [f"{source}->{reader}" for reader, source in sorted(self.stuck)]
        return LinkMatrix(not self.stuck, "stuck: " + ", ".join(shown) if shown else "every pair passed", pairs)

    def _note(self, verb: str, label: str) -> None:
        self.actions.append((verb, label))
        self.at.append((verb, label, self.clock.now if self.clock is not None else 0.0))

    def _fetch_through_restarting_links(self) -> None:
        """A link check fetches every pair: those from a provider whose link restarted under `wedge_s` ago wedge."""
        now = self.clock.now if self.clock is not None else 0.0
        for source, began in self.restarted_at.items():
            if now < began + self.wedge_s:
                self.stuck |= {(reader, source) for reader in LABEL.values() if reader != source}

    def link_matrix(self, service):
        self.checks += 1
        self.checked_at.append(self.clock.now if self.clock is not None else 0.0)
        if self.on_check is not None:
            self.on_check()
        self.stuck |= self.grow.get(self.checks, set())
        self._fetch_through_restarting_links()
        return self._matrix()

    def active_transfers(self):
        return self.transfers.pop(0) if self.transfers else 0

    def serves(self, service):
        if LABEL[service] in self.never_serving:
            self.calls.append(("serves", service))
            return False
        return super().serves(service)

    def sessions_ready(self, service, gate_probe):
        if self.gate_after > 0:
            self.gate_after -= 1
            return "providers[3] holds an error for the restored provider"
        return self.gate_detail

    def ping(self, service):
        self._note("ping", LABEL[service])
        if self.clock is not None:
            self.clock.sleep(self.ping_s)
        if self.on_ping is not None:
            self.on_ping(LABEL[service])
        return self.pong.get(LABEL[service], True)

    def restart_link(self, service):
        reader = LABEL[service]
        self._note("restart_link", reader)
        if self.clock is not None:
            self.clock.sleep(self.link_s)
        self.restarted_at[reader] = self.clock.now if self.clock is not None else 0.0
        fixes = self.link_fixes.get(reader)
        mended = {pair for pair in self.stuck if pair[0] == reader} if fixes is None else set(fixes)
        self.stuck = (self.stuck - mended) | set(self.link_breaks.get(reader, ()))
        return self.link_result.get(reader, "ok")

    def stop(self, service):
        self._note("stop", LABEL[service])
        super().stop(service)

    def start(self, service):
        self._note("start", LABEL[service])
        super().start(service)
        if LABEL[service] in self.container_fixes:
            self.stuck = {pair for pair in self.stuck if pair[0] != LABEL[service]}


def four_providers(data, **faults):
    """Four providers, each with a service; short commands and settles, so a declared heal fits its default bound."""
    for name, index in (("gamma", 3), ("delta", 4)):
        data["providers"][name] = {"url": f"https://{name}.test", "id": f"pr-{name}-{index}"}
    data["faults"] = faults_table(**{"services": dict(STORES), "command_timeout_s": 10, "heal_settle_s": 30, **faults})


def injector(tmp_path, services=None, **faults):
    tmp_path.mkdir(parents=True, exist_ok=True)  # a test may build a second federation under a subdirectory
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
    """O35: without `heal`, a stuck link fails the restore closed as before; the gate waits 240 s, the heal at most
    4,200 s, and its settle is 180 s (both provisional)."""
    platform = write_platform(tmp_path, four_providers)
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    assert (settings.heal, settings.gate_timeout_s, settings.heal_timeout_s) == ("off", 240.0, 4200.0)
    assert (DEFAULT_GATE_TIMEOUT_S, DEFAULT_HEAL_TIMEOUT_S, DEFAULT_HEAL_SETTLE_S) == (240.0, 4200.0, 180.0)


@pytest.mark.parametrize(
    ("mode", "least"),
    [("link", 600), ("link_then_container", 1545), ("all_links", 3075)],
)
def test_o35_a_heal_whose_bound_cannot_hold_one_round_of_its_steps_is_refused_at_load(tmp_path, mode, least):
    """O35: no step starts past `heal_timeout_s`, so a declared heal whose bound cannot hold one round of its steps at
    their declared bounds would never act: it is refused, naming the key and the least it may be. With 120 s commands
    and a 90 s settle: one link restart, 600 s; with a container restart (seven commands with its margin), 1,545 s;
    four providers' all-links sequence with a container restart, 3,075 s."""
    slow = {"command_timeout_s": 120, "heal_settle_s": 90, "heal": mode}
    platform = write_platform(tmp_path, lambda data: four_providers(data, heal_timeout_s=least - 1, **slow))
    with pytest.raises(ConfigError, match=rf"heal_timeout_s.*at least {least}\b"):
        load_faults(platform, load_platform(platform, require_secrets=False))
    platform = write_platform(tmp_path, lambda data: four_providers(data, heal_timeout_s=least, **slow))
    assert load_faults(platform, load_platform(platform, require_secrets=False)).heal == mode


@pytest.mark.parametrize("value", ["always", "", 1, True])
def test_o35_a_heal_mode_that_is_not_declared_is_refused_naming_the_key(tmp_path, value):
    """O35: `heal` is one of "off", "all_links", "link_then_container" and "link"; anything else is refused."""
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
        ("transfers", "all", "quiet"),  # before the restart
        ("transfers", "all", "quiet"),  # at the end of its settle; the restart's own record follows, once it is read
        ("restart_link", READER, "ok"),
    ]
    restart = heal["actions"][3]
    assert restart["settle_s"] == 30
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
    steps = [a for a in heal["actions"] if a["action"] != "transfers"]
    assert [(a["action"], a["provider"]) for a in steps] == [
        ("ping", READER),
        ("restart_link", READER),
        ("container_restart", READER),
    ]
    assert steps[2]["result"] == "restored"
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
    """O35: a link restart after which a pair that passed before it fails, one that does not have the restarted reader
    as its source (so no redial of the reader explains it), stops the heal at once (`heal_broke_pair`): the new
    failure is not healed, and no container is restarted."""
    services = HealServices()
    services.link_breaks[READER] = ((SECOND, CRASHED),)
    injector_, services = injector(tmp_path, services, heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "heal_broke_pair"
    heal = step(injector_, "heal")
    assert heal["outcome"] == "heal_broke_pair"
    assert "providers[0]->providers[3]" in heal["why"]
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
    stuck link, and is healed only once its readers' pairs pass."""
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
    # a ping and a link restart reserve two lookups and the ping (3 x 10 s), the RPC's 150 s and one 30 s settle: 210 s
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


def test_o35_a_gate_reading_that_passes_only_after_the_bound_is_no_pass(tmp_path):
    """O35: a reading that ends after `gate_timeout_s` passes nothing, however it ends: the bound is the bound."""
    services = HealServices()
    services.stuck = set()

    def slow(service, probe):
        services.clock.sleep(20)
        return None

    services.sessions_ready = slow
    injector_, services = injector(tmp_path, services, gate_timeout_s=5)
    injector_.start(RUN)
    assert injector_.clear() is False
    gate = step(injector_, "gate")
    assert (gate["ok"], gate["how"]) == (False, "not_ready")
    assert services.checks == 0


# --- the shape seen live, and what review found -------------------------------------------------------------------


def test_o35_link_mode_heals_every_stale_reader_of_a_crashed_source_one_at_a_time(tmp_path):
    """O35: one crashed source and N stale readers, each cleared only by its own link restart (seen live): `link`
    heals them all in turn, the matrix read after each action shows one more pair passing, the snapshot names the
    source they share, and the source itself and the restored provider are never touched."""
    services = HealServices()
    services.stuck = {(READER, CRASHED), (SECOND, CRASHED)}
    injector_, services = injector(tmp_path, services, heal="link")
    injector_.start(RUN)
    assert injector_.clear() is True
    heal = step(injector_, "heal")
    assert heal["shared_source"] == CRASHED
    assert heal["before"]["failing_sources"] == {CRASHED: 2}
    restarts = [a for a in heal["actions"] if a["action"] == "restart_link"]
    assert [a["provider"] for a in restarts] == [READER, SECOND]
    assert [a["matrix_after"]["failing_sources"] for a in restarts] == [{CRASHED: 1}, {}]
    assert all(label not in (CRASHED, RESTORED) for _, label in acted(services, "ping", "restart_link"))


def test_o35_a_reader_that_became_a_failing_source_during_its_ping_is_not_restarted(tmp_path):
    """O35: the matrix is read again after a ping, so a reader that became the source of a failing pair while it was
    pinged (another reader's pair from it failed meanwhile) is left alone, even with no pong and
    `link_then_container`: its container is restarted only once that other pair passes."""
    services = HealServices()
    services.pong[READER] = False
    services.container_fixes.add(READER)
    services.on_ping = lambda label: services.stuck.add((SECOND, READER)) if label == READER else None
    injector_, services = injector(tmp_path, services, heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is True
    assert ("restart_link", READER) not in services.actions
    assert services.actions.index(("restart_link", SECOND)) < services.actions.index(("stop", READER))


def test_o35_a_slow_ping_leaves_no_link_restart_past_the_heals_bound(tmp_path):
    """O35: the bound is checked again after the ping, with the link restart's own lookup, RPC and quiet."""
    services = HealServices()
    services.ping_s = 450.0  # then the link restart's own 190 s no longer fits in 600 s
    injector_, services = injector(tmp_path, services, heal="link", heal_timeout_s=600)
    injector_.start(RUN)
    assert injector_.clear() is False
    assert acted(services, "ping", "restart_link") == [("ping", READER)]
    assert "after the ping" in step(injector_, "heal")["why"]


def test_o35_no_later_process_heals_an_injection_a_heal_already_ran_for(tmp_path):
    """O35: the heal is journalled before its first action, so another process's recovery of the same injection (a
    fresh recovery, with no memory of it) only checks: no second link restart, after a badrpc above all."""
    services = HealServices()
    services.link_fixes[READER] = ()
    services.link_result[READER] = "badrpc_timeout"
    injector_, services = injector(tmp_path, services, heal="link")
    injector_.start(RUN)
    assert injector_.clear() is False
    platform = tmp_path / "platform.toml"
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    clock = FakeClock()
    fresh = Recovery(settings, services, clock=clock, sleep=clock.sleep, labels=SERVICE)
    (entry,) = fresh.entries()
    assert entry["healed"] is True
    assert fresh.settle(entry, heal=True).how == "links_stuck"
    assert acted(services, "restart_link") == [("restart_link", READER)]


def test_o35_a_restarted_readers_entry_is_cleared_only_with_every_pair_passing_and_recovered_first(tmp_path):
    """O35: a reader's container restart is an injection of its own, journalled with the healed injection's gate
    probe. A heal that ends with a pair still failing leaves it open, failed, and recovery settles it before the
    injection it healed, which cannot pass its gate while a reader is down."""
    services = HealServices()
    services.link_fixes[READER] = ()
    services.arm_gate = lambda service: "gate-file-7"
    injector_, services = injector(tmp_path, services, heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is False
    lines = journal_lines(tmp_path)
    (child,) = [line for line in lines if line.get("heal") is True]
    assert (child["target"], child["gate_probe"]) == (READER, "gate-file-7")
    events = [line["event"] for line in lines if line.get("id") == child["id"]]
    assert "cleared" not in events
    assert events[-1] == "clear_failed"
    injector_._unlock()  # the injector's process ends: the kernel drops its lock on the target
    platform = tmp_path / "platform.toml"
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    clock = FakeClock()
    order: list[str] = []
    fresh = Recovery(settings, services, clock=clock, sleep=clock.sleep, labels=SERVICE)
    with pytest.raises(InfraError):
        fresh.recover(lambda step, ok, target, **kw: order.append(target), heal=True)
    assert order[0] == READER


def test_o35_a_clear_after_the_runs_exclusive_admission_ended_never_heals(tmp_path):
    """O35: the heal needs the federation held exclusively at the moment it would act. An injection made under its
    run's exclusive admission, whose first clear failed at the gate, is cleared later, once that admission has ended:
    that clear only checks, and fails closed."""
    services = HealServices()
    services.gate_detail = "Onezone does not show providers[1] online"
    injector_, services = injector(tmp_path, services, heal="link", gate_timeout_s=2)
    platform = tmp_path / "platform.toml"
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    with FederationLock(settings).held(exclusive=True, timeout_s=5):
        injector_.start(RUN)
        assert injector_.clear() is False
    services.gate_detail = None
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "links_stuck"
    assert acted(services, "ping", "restart_link") == []


# --- all_links: every link restarted at once, one settle, then every pair ------------------------------------------


def stale():
    """The live pattern under the settled rule: the crashed source's readers stale, and a fetch from a provider within
    20 s of its link restart (the tests' settle is 30 s) wedging the pair."""
    services = HealServices()
    services.stuck = {(READER, CRASHED), (SECOND, CRASHED)}
    services.wedge_s = 20.0
    return services


def test_o35_the_per_reader_heal_with_its_enforced_settle_heals_each_reader_and_leaves_reverse_pairs_healthy(tmp_path):
    """O35: the rule the live data settled: after a link restart on a reader, nothing fetches from it until the settle
    ends. The per-reader design restarts each stale reader's link in turn, each followed by the settle with no fetch,
    so every reverse pair stays healthy and the restore is verified."""
    injector_, services = injector(tmp_path, stale(), heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is True
    assert acted(services, "restart_link") == [("restart_link", READER), ("restart_link", SECOND)]
    for verb, _, at in services.at:
        if verb == "restart_link":
            assert not [checked for checked in services.checked_at if at < checked < at + 30]


def test_o35_a_check_made_during_a_restarts_window_wedges_the_reverse_pairs_and_the_heal_stops_closed(tmp_path):
    """O35: a heal that read the pairs before the window ended (here a settle of 5 s against a 20 s window), as the
    live check 23 s after a restart did, wedges every pair from the restarted reader; the strict rule stops it at
    once, closed, rather than chasing the wedged pairs."""
    injector_, services = injector(tmp_path, stale(), heal="link_then_container", heal_settle_s=5)
    injector_.start(RUN)
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "heal_broke_pair"
    assert acted(services, "restart_link") == [("restart_link", READER)]


def test_o35_all_links_heals_every_stale_pair_in_one_round(tmp_path):
    """O35: every provider is pinged, then every link restarted in label order with nothing fetched in between, the
    restored provider and the crashed source included, then one settle with no fetch at all, then every pair passes
    and the restore is verified, with every step and the settle window recorded."""
    injector_, services = injector(tmp_path, stale(), heal="all_links")
    injector_.start(RUN)
    assert injector_.clear() is True
    labels = [CRASHED, RESTORED, READER, SECOND]
    assert acted(services, "ping") == [("ping", label) for label in labels]
    assert acted(services, "restart_link") == [("restart_link", label) for label in labels]
    assert acted(services, "stop") == [("stop", RESTORED)]  # the injection's own: no container restarted
    heal = step(injector_, "heal")
    assert (heal["outcome"], heal["shared_source"]) == ("healed", CRASHED)
    assert heal["window"]["settle_s"] == 30
    sequence = ["transfers", "restart_link"] * 4  # a reading before each restart: waited for before the first only
    assert [a["action"] for a in heal["actions"]] == ["ping"] * 4 + sequence + ["transfers", "check"]
    assert heal["actions"][-1]["matrix_after"]["ok"] is True


def test_o35_all_links_fetches_nothing_from_its_first_restart_to_the_end_of_its_settle(tmp_path):
    """O35: no probe or check runs between the first link restart and the end of the settle."""
    services = stale()
    services.ping_s = 1.0  # the clock moves between the checks before the sequence and its restarts
    injector_, services = injector(tmp_path, services, heal="all_links")
    injector_.start(RUN)
    assert injector_.clear() is True
    first = min(at for verb, _, at in services.at if verb == "restart_link")
    assert not [at for at in services.checked_at if first <= at < first + 30]
    assert [at for at in services.checked_at if at >= first + 30]  # the check after the settle


def test_o35_all_links_waits_for_an_active_transfer_to_end_before_its_restarts(tmp_path):
    """O35: a replication could dial a link mid-sequence, so the restarts wait, within the bound, for the transfer
    listings of the study and probe spaces to show no active job."""
    services = stale()
    services.transfers = [2, 1]  # active at the first two readings, quiet from the third
    injector_, services = injector(tmp_path, services, heal="all_links")
    injector_.start(RUN)
    assert injector_.clear() is True
    readings = [a for a in step(injector_, "heal")["actions"] if a["action"] == "transfers"]
    phases = [("before", "quiet")] + [("between", "quiet")] * 3 + [("after", "quiet")]
    assert [(a["phase"], a["result"]) for a in readings] == phases
    assert readings[0]["waited_s"] == 2


def test_o35_all_links_fails_closed_on_a_transfer_that_stays_active(tmp_path):
    """O35: a transfer still active past the bound fails the heal closed (`transfers_active`), before any restart."""
    services = stale()
    services.transfers = [1] * 1000
    injector_, services = injector(tmp_path, services, heal="all_links", gate_timeout_s=10)
    injector_.start(RUN)
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "transfers_active"
    assert acted(services, "restart_link") == []


def test_o35_all_links_fails_closed_on_a_transfer_active_at_the_end_of_its_window(tmp_path):
    """O35: a transfer active at the end of the settle, past the bound, fails it closed before any check."""
    services = stale()
    services.transfers = [0, 0, 0, 0, 1]  # quiet before and between the restarts, active at the window's end
    injector_, services = injector(tmp_path, services, heal="all_links", gate_timeout_s=10)
    injector_.start(RUN)
    checks = services.checks
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "transfers_active"
    assert len(acted(services, "restart_link")) == 4
    assert services.checks - checks == 2  # the restore's check and the heal's fresh one: none after the window
    (last,) = [a for a in step(injector_, "heal")["actions"] if a.get("phase") == "after"]
    assert last["waited_s"] == 0  # read once, never waited for: a transfer then may have fetched mid-settle


def test_o35_a_transfer_that_starts_mid_sequence_stops_it_after_the_settle(tmp_path):
    """O35: a transfer active before a later restart of the sequence stops it there, closed, and the links already
    restarted still get their whole settle before anything else is read."""
    services = stale()
    services.transfers = [0, 0, 1]  # quiet before the first two restarts, active before the third
    injector_, services = injector(tmp_path, services, heal="all_links")
    injector_.start(RUN)
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "transfers_active"
    assert acted(services, "restart_link") == [("restart_link", CRASHED), ("restart_link", RESTORED)]
    assert step(injector_, "heal")["window"]["settle_s"] == 30
    last = max(at for verb, _, at in services.at if verb == "restart_link")
    assert not [checked for checked in services.checked_at if last < checked < last + 30]


def test_o35_a_per_reader_settle_ends_closed_on_a_transfer_active_at_its_end(tmp_path):
    """O35: the per-reader settle's end is read once, never waited out: an active transfer then fails the heal."""
    services = HealServices()
    services.transfers = [0, 1]  # quiet before the restart, active at the end of its settle
    injector_, services = injector(tmp_path, services, heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "transfers_active"


def test_o35_a_container_restart_that_is_not_verified_still_gets_its_whole_settle(tmp_path):
    """O35: a reader's container restart that does not serve is unverified, and the settle after it is enforced all
    the same before the heal returns, so no later read reaches its link early."""
    services = HealServices()
    services.link_fixes[READER] = ()
    services.never_serving = {READER}
    injector_, services = injector(tmp_path, services, heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "heal_restart_failed"
    (restart,) = [a for a in step(injector_, "heal")["actions"] if a["action"] == "container_restart"]
    assert restart["settle_s"] == 30


def test_o35_all_links_restarts_nothing_when_a_provider_gives_no_pong(tmp_path):
    """O35: a partial sequence is what leaves pairs stale, so one provider without a pong fails it before any
    restart."""
    services = stale()
    services.pong[SECOND] = False
    injector_, services = injector(tmp_path, services, heal="all_links")
    injector_.start(RUN)
    assert injector_.clear() is False
    assert acted(services, "restart_link") == []
    assert "no pong" in step(injector_, "heal")["why"]


def test_o35_after_all_links_a_pair_still_failing_falls_back_to_its_readers_container(tmp_path):
    """O35: a pair the link restarts did not mend has its reader's container restarted, then the settle, its gate and
    every pair again; never a second link restart."""
    services = HealServices()
    services.link_fixes = dict.fromkeys(SERVICE, ())  # no link restart mends anything
    services.container_fixes.add(READER)
    injector_, services = injector(tmp_path, services, heal="all_links")
    injector_.start(RUN)
    assert injector_.clear() is True
    assert len(acted(services, "restart_link")) == 4
    assert acted(services, "stop") == [("stop", RESTORED), ("stop", READER)]


def test_o35_the_container_fallback_never_restarts_the_restored_provider_and_fails_closed(tmp_path):
    """O35: a pair the restored provider reads that no link restart mends is never mended by restarting the restored
    provider's container: the heal fails closed."""
    services = HealServices()
    services.stuck = {(RESTORED, CRASHED)}
    services.link_fixes = dict.fromkeys(SERVICE, ())
    injector_, services = injector(tmp_path, services, heal="all_links")
    injector_.start(RUN)
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "links_stuck"
    assert acted(services, "stop") == [("stop", RESTORED)]


def test_o35_all_links_starts_no_restart_its_bound_cannot_cover(tmp_path):
    """O35: once the pings are done the restarts must still fit in what is left of `heal_timeout_s`, or none
    starts."""
    services = stale()
    services.ping_s = 200.0  # four pings take 800 s of a 1,000 s bound, leaving less than the restarts' 640 s
    injector_, services = injector(tmp_path, services, heal="all_links", heal_timeout_s=1000)
    injector_.start(RUN)
    assert injector_.clear() is False
    assert acted(services, "restart_link") == []
    assert "all-links restarts" in step(injector_, "heal")["why"]


def test_o35_a_second_settling_never_runs_the_all_links_sequence_again(tmp_path):
    """O35: the heal is journalled before it acts, so a fresh recovery of the same injection only checks: no
    provider gets a second link restart in its restore."""
    services = HealServices()
    services.link_fixes = dict.fromkeys(SERVICE, ())
    injector_, services = injector(tmp_path, services, heal="all_links")
    injector_.start(RUN)
    assert injector_.clear() is False
    platform = tmp_path / "platform.toml"
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    clock = FakeClock()
    fresh = Recovery(settings, services, clock=clock, sleep=clock.sleep, labels=SERVICE)
    (entry,) = [entry for entry in fresh.entries() if entry["target"] == RESTORED]  # and the reader's container's
    assert fresh.settle(entry, heal=True).how == "links_stuck"
    assert len(acted(services, "restart_link")) == 4


def test_o35_no_run_is_admitted_before_the_final_all_pairs_check_passes(tmp_path):
    """O35: the injector holds the federation exclusively through the whole heal, its settle and checks included, so
    a run's shared admission is refused at every check until the restore is verified, and granted after."""
    injector_, services = injector(tmp_path, stale(), heal="all_links")
    platform = tmp_path / "platform.toml"
    lock = FederationLock(load_faults(platform, load_platform(platform, require_secrets=False)))
    refused: list[bool] = []

    def try_a_run():
        admission = lock.try_acquire(exclusive=False)
        refused.append(admission is None)
        if admission is not None:
            admission.close()

    services.on_check = try_a_run
    injector_.start(RUN)
    assert injector_.clear() is True
    assert refused
    assert all(refused)
    admission = lock.try_acquire(exclusive=False)
    assert admission is not None
    admission.close()


# --- the second review round ----------------------------------------------------------------------------------------


def test_o35_all_links_is_refused_at_load_when_a_provider_of_a_pair_has_no_service(tmp_path):
    """O35: a partial sequence is what leaves pairs stale, so `all_links` needs a service for every provider whose
    links are checked; one missing is refused at load, named by its label."""
    services = {"beta": STORES["beta"]}  # alpha supports the study space and has none
    platform = write_platform(tmp_path, lambda data: four_providers(data, heal="all_links", services=services))
    with pytest.raises(ConfigError, match=r"heal.*providers\[0\]"):
        load_faults(platform, load_platform(platform, require_secrets=False))


def test_o35_all_links_fails_closed_when_the_driver_cannot_tell_whether_a_transfer_is_active(tmp_path):
    """O35: a driver that cannot read the transfer listings shows no quiet: no link is restarted."""
    services = stale()
    services.active_transfers = lambda: None
    injector_, services = injector(tmp_path, services, heal="all_links")
    injector_.start(RUN)
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "transfers_active"
    assert acted(services, "restart_link") == []


def test_o35_a_quiet_transfer_reading_seen_only_after_its_bound_is_no_quiet(tmp_path):
    """O35: a reading of the listings that ends past the wait's bound passes nothing, however it ends."""
    services = stale()

    def slow():
        services.clock.sleep(20)
        return 0

    services.active_transfers = slow
    injector_, services = injector(tmp_path, services, heal="all_links", gate_timeout_s=5)
    injector_.start(RUN)
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "transfers_active"
    assert acted(services, "restart_link") == []


def test_o35_a_clear_keeps_the_runs_admission_held_when_the_run_lets_it_go_midway(tmp_path):
    """O35: a cancelled run may let its admission go while the clear still restores or heals in its worker; the clear
    holds a share of it until it ends, so no run is admitted meanwhile and the heal still runs under the hold."""
    platform_dir = tmp_path
    injector_, services = injector(platform_dir, stale(), heal="all_links")
    platform = platform_dir / "platform.toml"
    lock = FederationLock(load_faults(platform, load_platform(platform, require_secrets=False)))
    refused: list[bool] = []
    with lock.held(exclusive=True, timeout_s=5) as admission:
        injector_.start(RUN)

        def let_go_then_try_a_run():
            admission.close()  # the run's context lets its admission go, as a cancellation that outlasts its wait does
            other = lock.try_acquire(exclusive=False)
            refused.append(other is None)
            if other is not None:
                other.close()

        services.on_check = let_go_then_try_a_run
        assert injector_.clear() is True
    assert refused
    assert all(refused)
    assert len(acted(services, "restart_link")) == 4  # the heal ran under the hold it kept
    other = lock.try_acquire(exclusive=False)
    assert other is not None
    other.close()


@pytest.mark.parametrize("poll_s", [1, 1000])
def test_o35_a_heals_container_restart_ends_its_waits_at_its_budget_with_the_start_sent(tmp_path, poll_s):
    """O35: the heal gives a reader's container restart what is left of its bound; its waits end there, no pause
    between two readings crosses it however long `poll_s` is, the start is sent all the same, so the reader is never
    left stopped, and a restart cut short is unverified with its entry open for recovery."""
    services = HealServices()
    services.never_serving = {READER}
    platform = write_platform(tmp_path, lambda data: four_providers(data, ready_timeout_s=60, poll_s=poll_s))
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    clock = FakeClock()
    services.clock = clock
    recovery = Recovery(settings, services, clock=clock, sleep=clock.sleep, labels=SERVICE)
    began = clock.now
    done = recovery.restart_container(READER, SERVICE[READER], None, 3.0)
    assert (done["ok"], done["how"]) == (False, "not_serving")
    assert clock.now - began <= 3.0 + 1.0  # one poll past the budget at most, far below the 60 s ready bound
    assert acted(services, "stop", "start") == [("stop", READER), ("start", READER)]
    (open_,) = recovery.entries()
    assert open_["target"] == READER


def test_o35_an_interrupted_heal_leaves_no_read_before_its_settle_could_end(tmp_path):
    """O35: a heal interrupted right after a link restart was sent (an interrupt, an exit) skips its settle, so the
    restart is held before it is sent: the next clear of the same injector reads no gate and checks no pair until that
    settle could have ended."""
    services = stale()
    original = services.restart_link

    def interrupted(service):
        original(service)
        raise KeyboardInterrupt

    services.restart_link = interrupted
    injector_, services = injector(tmp_path, services, heal="link_then_container")
    injector_.start(RUN)
    with pytest.raises(KeyboardInterrupt):
        injector_.clear()
    stopped, checks = services.clock.now, len(services.checked_at)
    injector_.clear()
    later = services.checked_at[checks:]
    assert later
    assert min(later) >= stopped + 30  # the settle, at least, before any check


def test_o35_a_restart_that_raises_settles_before_the_failure_unwinds(tmp_path):
    """O35: a link restart that raises (not a driver error) may have been sent: its settle runs before the failure
    leaves the heal, while the injector still holds the federation, so no other recovery (the health check's, which
    knows nothing of the hold) can read through the link early."""
    services = stale()

    def broken(service):
        services._note("restart_link", LABEL[service])
        raise RuntimeError("the command broke after it was sent")

    services.restart_link = broken
    injector_, services = injector(tmp_path, services, heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is False
    sent_at = next(at for verb, _, at in services.at if verb == "restart_link")
    assert services.clock.now >= sent_at + 30  # the settle ran before the clear returned
    assert not [checked for checked in services.checked_at if sent_at < checked < sent_at + 30]


# --- the PI's authority: link restarts on any provider, a container only where declared ------------------------


def test_o35_all_links_without_the_container_fallback_restarts_links_only_and_fails_closed(tmp_path):
    """O35: with `heal_container_fallback = false`, `all_links` restarts every link and nothing else: a pair the link
    restarts do not mend fails the restore closed for an operator to decide, and no container but the injection's own
    target is ever stopped."""
    services = HealServices()
    services.link_fixes = dict.fromkeys(SERVICE, ())  # no link restart mends anything
    services.container_fixes.add(READER)  # a container restart would, were one allowed
    injector_, services = injector(tmp_path, services, heal="all_links", heal_container_fallback=False)
    injector_.start(RUN)
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "links_stuck"
    assert len(acted(services, "restart_link")) == 4
    assert acted(services, "stop", "start") == [("stop", RESTORED), ("start", RESTORED)]
    assert "fallback off" in step(injector_, "heal")["why"]


@pytest.mark.parametrize(("mode", "least"), [("all_links", 2130), ("link", 600)])
def test_o35_a_heal_without_the_container_fallback_needs_no_container_reserve(tmp_path, mode, least):
    """O35: without the fallback, the least `heal_timeout_s` holds no container restart: four providers' all-links
    sequence alone at 120 s commands and a 90 s settle is 2,130 s."""
    slow = {"command_timeout_s": 120, "heal_settle_s": 90, "heal": mode, "heal_container_fallback": False}
    platform = write_platform(tmp_path, lambda data: four_providers(data, heal_timeout_s=least - 1, **slow))
    with pytest.raises(ConfigError, match=rf"heal_timeout_s.*at least {least}\b"):
        load_faults(platform, load_platform(platform, require_secrets=False))
    platform = write_platform(tmp_path, lambda data: four_providers(data, heal_timeout_s=least, **slow))
    assert load_faults(platform, load_platform(platform, require_secrets=False)).heal_container_fallback is False


def test_o35_link_then_container_with_the_container_fallback_off_is_refused(tmp_path):
    """O35: a mode of container restarts declared to make none is a contradiction, refused at load naming the key."""
    platform = write_platform(
        tmp_path, lambda data: four_providers(data, heal="link_then_container", heal_container_fallback=False)
    )
    with pytest.raises(ConfigError, match="heal_container_fallback"):
        load_faults(platform, load_platform(platform, require_secrets=False))


FIVE = ("alpha", "beta", "gamma", "delta", "epsilon")
FIVE_STORES = {name: f"store-{name}" for name in FIVE}
FIVE_LABEL = {FIVE_STORES[name]: f"providers[{index}]" for index, name in enumerate(FIVE)}


class FiveServices(FakeServices):
    """Five providers' services: one stale pair (providers[3] reading providers[0]), mended by its reader's own link
    restart; every step any service sees is recorded, by the provider's label."""

    def __init__(self):
        super().__init__(tuple(FIVE_STORES.values()))
        self.stuck = {("providers[3]", "providers[0]")}
        self.seen: list[tuple[str, str]] = []

    def stop(self, service):
        self.seen.append(("stop", FIVE_LABEL[service]))
        super().stop(service)

    def start(self, service):
        self.seen.append(("start", FIVE_LABEL[service]))
        super().start(service)

    def link_matrix(self, service):
        labels = list(FIVE_LABEL.values())
        pairs = tuple(
            PairState(r, s, (r, s) not in self.stuck, 0.1, (r, s) in self.stuck)
            for r in labels
            for s in labels
            if r != s
        )
        return LinkMatrix(not self.stuck, "stuck" if self.stuck else "every pair passed", pairs)

    def active_transfers(self):
        return 0

    def ping(self, service):
        self.seen.append(("ping", FIVE_LABEL[service]))
        return True

    def restart_link(self, service):
        label = FIVE_LABEL[service]
        self.seen.append(("restart_link", label))
        self.stuck = {pair for pair in self.stuck if pair[0] != label}
        return "ok"


def five_providers(data, **faults):
    for name, index in (("gamma", 3), ("delta", 4), ("epsilon", 5)):
        data["providers"][name] = {"url": f"https://{name}.test", "id": f"pr-{name}-{index}"}
    data["faults"] = faults_table(
        **{"services": dict(FIVE_STORES), "command_timeout_s": 10, "heal_settle_s": 30, **faults}
    )


@pytest.mark.parametrize("heal", ["off", "all_links"])
def test_o35_a_listed_service_is_never_an_injection_target_and_the_heal_only_pings_and_restarts_its_link(
    tmp_path, heal
):
    """O35 (and #263): `[faults.services]` lists five providers so that the heal can reach them; only the target the
    condition declares is ever injected. The injector stops and journals that one alone, and the heal, links only
    (`heal_container_fallback = false`), touches every other provider by `ping` and `restart_link` and nothing else."""
    platform = write_platform(tmp_path, lambda data: five_providers(data, heal=heal, heal_container_fallback=False))
    services = FiveServices()
    clock = FakeClock()
    injector_ = injector_for(platform, services, target="providers[1]", clock=clock)
    injector_.start(RUN)
    injector_.clear()
    injected = [line for line in journal_lines(tmp_path) if line["event"] == "injecting"]
    assert [line["target"] for line in injected] == ["providers[1]"]
    assert [step for step in services.seen if step[0] in ("stop", "start")] == [
        ("stop", "providers[1]"),
        ("start", "providers[1]"),
    ]
    others = [step for step in services.seen if step[1] != "providers[1]"]
    assert {verb for verb, _ in others} <= {"ping", "restart_link"}
    if heal == "all_links":
        assert sorted(label for verb, label in others if verb == "restart_link") == [
            "providers[0]",
            "providers[2]",
            "providers[3]",
            "providers[4]",
        ]
        assert injector_.snapshot()["restore_verified"] is True
    else:
        assert others == []


def test_o35_a_settle_one_recovery_held_holds_every_other_recoverys_reads(tmp_path):
    """O35: a heal holds each restart's settle in the journal on the wall clock, so another recovery (the health
    check's, or the next process's after this one died mid-settle) reads no gate and checks no pair before it ends."""
    injector_, services = injector(tmp_path, stale(), heal="link")
    injector_.start(RUN)
    platform = tmp_path / "platform.toml"
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    clock = FakeClock()
    holding = Recovery(settings, services, clock=clock, sleep=clock.sleep, labels=SERVICE, wall=clock)
    (entry,) = holding.entries()
    holding.hold(30, entry["id"])
    other = Recovery(settings, services, clock=clock, sleep=clock.sleep, labels=SERVICE, wall=clock)
    began = clock.now
    other._gate(SERVICE[RESTORED], None)
    assert clock.now >= began + 30
    (reread,) = other.entries()
    assert reread["settle_until"] == began + 30
