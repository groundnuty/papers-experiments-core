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
    """Four providers' services with a scripted link check, session gate, ping and link restart.

    `stuck` holds the failing pairs as (reader, source) labels. `link_fixes[reader]` is what a link restart of that
    reader mends: by default its own stuck pairs and nothing else, the shape seen live (each stale reader of a crashed
    source is cleared by its own `restart_link` alone, never by the source's or another reader's); `()` mends nothing.
    `link_breaks[reader]` is the pairs it breaks, `container_fixes` the readers a container restart mends, and `grow`
    adds pairs to `stuck` before the given check.
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
        # what a restart of a reader disturbs: pairs with it as their source, each failing for that many seconds of the
        # clock after the restart (the peer's link redials it), or for good (None: a cascade)
        self.disturbs: dict[str, dict[tuple[str, str], float | None]] = {}
        self.disturbed: dict[tuple[str, str], float | None] = {}  # failing until that time, or for good
        # the seconds a reader's link takes to come back after its restart: a fetch through it meanwhile (any link
        # check) is handed a secret the link never allowed, which wedges every pair from it until that pair's reader's
        # own link restarts (op-worker's `generate_secret`; the wedge is inference from private code)
        self.up_s: dict[str, float] = {}
        self.restarted_at: dict[str, float] = {}
        self.actions: list[tuple[str, str]] = []
        self.at: list[tuple[str, str, float]] = []  # each action with the clock's time
        self.checked_at: list[float] = []  # each link check's time

    def _failing(self) -> set[tuple[str, str]]:
        now = self.clock.now if self.clock is not None else 0.0
        return self.stuck | {pair for pair, until in self.disturbed.items() if until is None or until > now}

    def _matrix(self) -> LinkMatrix:
        labels, failing = list(LABEL.values()), self._failing()
        pairs = tuple(
            PairState(reader, source, (reader, source) not in failing, 0.1, (reader, source) in failing)
            for reader in labels
            for source in labels
            if reader != source
        )
        shown = [f"{source}->{reader}" for reader, source in sorted(failing)]
        return LinkMatrix(not failing, "stuck: " + ", ".join(shown) if shown else "every pair passed", pairs)

    def _restarted(self, reader: str) -> None:
        """A reader's link restarted: its own disturbed pairs clear, and it disturbs the pairs it is the source of."""
        self.disturbed = {pair: until for pair, until in self.disturbed.items() if pair[0] != reader}
        now = self.clock.now if self.clock is not None else 0.0
        self.restarted_at[reader] = now
        for pair, after in self.disturbs.get(reader, {}).items():
            self.disturbed[pair] = None if after is None else now + after

    def _fetch_through_coming_links(self) -> None:
        """A link check fetches every pair: those from a reader whose link is still coming back wedge."""
        now = self.clock.now if self.clock is not None else 0.0
        for source, began in self.restarted_at.items():
            if began <= now < began + self.up_s.get(source, 0.0):
                for reader in LABEL.values():
                    if reader != source:
                        self.disturbed[(reader, source)] = None

    def _note(self, verb: str, label: str) -> None:
        self.actions.append((verb, label))
        self.at.append((verb, label, self.clock.now if self.clock is not None else 0.0))

    def link_matrix(self, service):
        self.checks += 1
        self.checked_at.append(self.clock.now if self.clock is not None else 0.0)
        if self.on_check is not None:
            self.on_check()
        self.stuck |= self.grow.get(self.checks, set())
        self._fetch_through_coming_links()
        return self._matrix()

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
        fixes = self.link_fixes.get(reader)
        mended = {pair for pair in self.stuck if pair[0] == reader} if fixes is None else set(fixes)
        self.stuck = (self.stuck - mended) | set(self.link_breaks.get(reader, ()))
        if fixes is None:
            self._restarted(reader)
        return self.link_result.get(reader, "ok")

    def stop(self, service):
        self._note("stop", LABEL[service])
        super().stop(service)

    def start(self, service):
        self._note("start", LABEL[service])
        super().start(service)
        if LABEL[service] in self.container_fixes:
            self.stuck = {pair for pair in self.stuck if pair[0] != LABEL[service]}
            self._restarted(LABEL[service])


def four_providers(data, **faults):
    """Four providers, each with a service; short commands and settles, so a declared heal fits its default bound."""
    for name, index in (("gamma", 3), ("delta", 4)):
        data["providers"][name] = {"url": f"https://{name}.test", "id": f"pr-{name}-{index}"}
    data["faults"] = faults_table(services=dict(STORES), **{"command_timeout_s": 10, "heal_settle_s": 30, **faults})


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
    assert (DEFAULT_GATE_TIMEOUT_S, DEFAULT_HEAL_TIMEOUT_S, DEFAULT_HEAL_SETTLE_S) == (240.0, 900.0, 90.0)


def test_o35_a_heal_whose_bound_cannot_hold_one_round_of_its_actions_is_refused_at_load(tmp_path):
    """O35: no action starts past `heal_timeout_s`, so a declared heal whose bound cannot hold one link restart (and,
    in `link_then_container`, one container restart) at their declared bounds would never act: it is refused, naming
    the key and the least it may be. With 120 s commands and a 90 s settle, `link` needs 600 s, which 900 s covers."""
    slow = {"command_timeout_s": 120, "heal_settle_s": 90}
    platform = write_platform(tmp_path, lambda data: four_providers(data, heal="link", **slow))
    assert load_faults(platform, load_platform(platform, require_secrets=False)).heal_timeout_s == 900
    platform = write_platform(tmp_path, lambda data: four_providers(data, heal="link_then_container", **slow))
    with pytest.raises(ConfigError, match=r"heal_timeout_s.*at least 1185"):
        load_faults(platform, load_platform(platform, require_secrets=False))
    platform = write_platform(
        tmp_path, lambda data: four_providers(data, heal="link_then_container", heal_timeout_s=1185, **slow)
    )
    assert load_faults(platform, load_platform(platform, require_secrets=False)).heal == "link_then_container"


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
    `link_then_container`."""
    services = HealServices()
    services.pong[READER] = False
    services.on_ping = lambda label: services.stuck.add((SECOND, READER)) if label == READER else None
    injector_, services = injector(tmp_path, services, heal="link_then_container")
    injector_.start(RUN)
    assert injector_.clear() is False
    assert ("stop", READER) not in services.actions
    assert ("restart_link", READER) not in services.actions


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


# --- the settle after an action, and the pairs it disturbs (seen live) ---------------------------------------------


def test_o35_a_pair_an_action_disturbed_that_recovers_by_itself_after_the_settle_is_left_alone(tmp_path):
    """O35: a link restart of a reader drops every connection into its link; a pair with that reader as its source
    fails at the check after the settle and passes at the second, a settle later, with no action on it: it is
    disturbed and recovered, recorded so, and the restore is verified."""
    services = HealServices()
    services.disturbs[READER] = {(SECOND, READER): 40.0}  # the peer's link redials READER 40 s after its restart
    injector_, services = injector(tmp_path, services, heal="link")
    injector_.start(RUN)
    assert injector_.clear() is True
    heal = step(injector_, "heal")
    assert heal["disturbed"] == [{"reader": SECOND, "source": READER, "ended": "recovered", "by": READER}]
    (restart,) = [a for a in heal["actions"] if a["action"] == "restart_link"]
    assert restart["settle_s"] == [30, 30]
    assert restart["matrix_first"]["failing_sources"] == {READER: 1}
    assert restart["matrix_after"]["ok"] is True
    assert acted(services, "restart_link") == [("restart_link", READER)]


def test_o35_a_disturbed_pair_still_failing_after_the_second_settle_is_a_cascade_healed_by_its_own_reader(tmp_path):
    """O35: a disturbed pair that still fails a settle later is a cascade: a stuck pair whose reader the heal goes on
    to restart, within the bounds, and the restore is then verified."""
    services = HealServices()
    services.disturbs[READER] = {(SECOND, READER): None}
    injector_, services = injector(tmp_path, services, heal="link")
    injector_.start(RUN)
    assert injector_.clear() is True
    heal = step(injector_, "heal")
    assert heal["disturbed"] == [{"reader": SECOND, "source": READER, "ended": "cascade", "by": READER}]
    assert acted(services, "restart_link") == [("restart_link", READER), ("restart_link", SECOND)]


def test_o35_the_heal_never_acts_on_a_pair_it_disturbed_before_its_second_settle(tmp_path):
    """O35: the cascade's reader is pinged only after both settles of the action that disturbed its pair."""
    services = HealServices()
    services.disturbs[READER] = {(SECOND, READER): None}
    injector_, services = injector(tmp_path, services, heal="link")
    injector_.start(RUN)
    assert injector_.clear() is True
    restarted = next(at for verb, label, at in services.at if (verb, label) == ("restart_link", READER))
    pinged = next(at for verb, label, at in services.at if (verb, label) == ("ping", SECOND))
    assert pinged >= restarted + 2 * 30


def test_o35_a_cascade_beyond_the_bounds_fails_closed(tmp_path):
    """O35: a cascade whose reader already had its one link restart in this restore cannot be healed in `link`: the
    restore fails closed, the cascade recorded."""
    services = HealServices()
    services.stuck = {(READER, CRASHED), (SECOND, CRASHED)}
    services.disturbs[SECOND] = {(READER, SECOND): None}  # the second reader's restart strands the first again
    injector_, services = injector(tmp_path, services, heal="link")
    injector_.start(RUN)
    assert injector_.clear() is False
    assert step(injector_, "cleared")["how"] == "links_stuck"
    heal = step(injector_, "heal")
    assert heal["disturbed"] == [{"reader": READER, "source": SECOND, "ended": "cascade", "by": SECOND}]
    assert acted(services, "restart_link") == [("restart_link", READER), ("restart_link", SECOND)]


def test_o35_no_run_is_admitted_before_the_final_all_pairs_check_passes(tmp_path):
    """O35: the injector holds the federation exclusively through the whole heal, its settles and checks included, so
    a run's shared admission is refused at every check until the restore is verified, and granted after."""
    services = HealServices()
    services.disturbs[READER] = {(SECOND, READER): None}
    injector_, services = injector(tmp_path, services, heal="link")
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
