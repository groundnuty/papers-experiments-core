"""O90: inside the run's own held injection, the link check leaves out the pairs that touch the held target.

An outage run's own post-run check runs under its own admission while its injector holds the target's service stopped,
and Onezone may still report the target online. When the last link check is older than `[link_check] every_s`, the
check ran every pair, the target's pairs failed, and the gate raised the federation's link epoch inside the run's own
measured window: the run read "check failed, nothing offline" and was set aside. The check now leaves out every pair
that touches a target the fault gate explains as held by the run's own injection (and this call's reading says is held),
neither checking nor counting it; the other pairs are checked and counted as ever. A skipped check is said in the
reading.
"""

import contextlib
import json
import threading
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fault_support import SERVICES, FakeServices, faults_table, injector_for
from link_support import ALPHA_ID, BETA_ID, BOUND_S, GAMMA_ID, add_providers, three_providers

from expcore.campaign import liveness, prerun
from expcore_onedata import fault_serves, linkcheck
from expcore_onedata.admission import run_admission
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.fault_epoch import LinkEpoch
from expcore_onedata.fault_journal import InjectionJournal
from expcore_onedata.fault_settings import load_faults
from expcore_onedata.health import PlatformHealth
from expcore_onedata.healthreading import HealthReading, target_only
from expcore_onedata.linkcheck import check_links
from expcore_onedata.testing import write_platform
from expcore_onedata.testing.world import PROBE_SPACE_ID, PROBE_SPACE_NAME, with_link_check
from expcore_onedata.tokens import TokenIssuer

TARGET = "providers[1]"  # beta
RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
STORES = {**SERVICES, "gamma": "store-three"}
ISSUER_CLOCK = 1_800_000_000.0
ANOTHER = {"process": "host-a:100:1", "campaign": "camp-a", "attempt_id": 7}
SKIPPED = "every pair touches the run's own held target"


@pytest.fixture(autouse=True)
def checks_on_the_tickers_clock(monkeypatch, ticker):
    """Every check runs on the test's `Ticker`, a pair at a time (as `test_link_record_root` does)."""
    monkeypatch.setattr(linkcheck, "WORKERS", 1)
    monkeypatch.setattr(
        fault_serves,
        "check_links",
        partial(check_links, clock=ticker.clock, sleep=ticker.sleep, now=ticker.clock, workers=1),
    )


@dataclass
class World:
    health: PlatformHealth
    platform: Path
    config: Any
    fake: Any
    ticker: Any
    epoch: LinkEpoch


def build(tmp_path, fake, ticker, document) -> World:
    platform = write_platform(tmp_path, document)
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    issuer = TokenIssuer(client, config, clock=lambda: ISSUER_CLOCK)
    health = PlatformHealth(config, client, issuer, monotonic=ticker.clock, sleep=ticker.sleep, clock=ticker.clock)
    return World(health, platform, config, fake, ticker, LinkEpoch(load_faults(platform, config).journal))


def three(data):
    three_providers(data)
    data["faults"] = faults_table(services=STORES)


def two(data):
    data["support"] = {"providers": ["alpha", "beta"], "local": "alpha"}
    with_link_check(data, ["alpha", "beta"], timeout_s=BOUND_S)
    data["faults"] = faults_table()


@pytest.fixture
def world(tmp_path, fake, ticker) -> World:
    """Three providers (alpha local, beta the target, gamma): six ordered pairs, four of them touch beta."""
    add_providers(fake)
    return build(tmp_path, fake, ticker, three)


@pytest.fixture
def pair_world(tmp_path, fake, ticker) -> World:
    """Two providers: both ordered pairs touch beta, so a held beta leaves no pair to check."""
    fake.supports[BETA_ID] = {"readonly": False}
    fake.add_data_space(PROBE_SPACE_ID, PROBE_SPACE_NAME, [ALPHA_ID, BETA_ID])
    return build(tmp_path, fake, ticker, two)


def stop_links_of(fake, provider_id, *, others=(ALPHA_ID, BETA_ID, GAMMA_ID)):
    """The target's service is stopped: every ordered pair that reads from it or through it transfers nothing."""
    for other in others:
        if other != provider_id:
            fake.links[(other, provider_id)] = "fail"
            fake.links[(provider_id, other)] = "fail"
    fake.hang_hook = None


@contextlib.contextmanager
def inside_injection(world: World, *, extra=None, services=None):
    """The outage run's order: its pre-run check (a full link check, which passes), its exclusive admission, the
    injector started with the target's service stopped, then the body makes the run's own check under that admission;
    after it the injector restores. `extra` is given the journal and the attempt's own token while the injection is
    open, to put another's lines there."""
    admit = run_admission(world.config)
    injector = injector_for(world.platform, services or FakeServices((*SERVICES.values(), "store-three")))
    journal = InjectionJournal(load_faults(world.platform, world.config).journal)
    with prerun.opened(), liveness.running("camp-a", 5):
        before = world.health()
        assert before.ok, before.detail
        with admit(("provider_outage",), {"provider_outage": TARGET}, threading.Event(), False):
            injector.start(RUN)
            if extra is not None:
                extra(journal, liveness.current())
            yield injector
            for entry in journal.open_entries():  # whoever's: the test's journal is left settled
                if entry["target"] != TARGET:
                    journal.append({"event": "cleared", "id": entry["id"]})
            assert injector.finish()["ok"] is True


def epoch_of(world: World) -> int:
    return world.epoch.read()["epoch"]


def own_check(world: World) -> tuple[Any, HealthReading]:
    """The run's own check once its pre-run link check is stale: the answer and the reading that call made."""
    world.ticker.sleep(world.config.link_check.every_s)
    answer = world.health()
    return answer, world.health.reading


def test_o90_a_stale_link_check_inside_the_runs_own_injection_leaves_out_the_held_targets_pairs(world):
    """O90: the target's service is stopped (its pairs transfer nothing) and Onezone still reports it online. The
    link check is stale, so the run's own check makes it: the four pairs that touch the target are neither checked nor
    counted, the other two pass, the check passes and the reading is target-only, and the federation's link epoch is
    NOT raised. The next call outside the injection checks every pair again: the left-out pairs are never passed on
    the strength of this one."""
    with inside_injection(world):
        noted = epoch_of(world)
        stop_links_of(world.fake, BETA_ID)
        calls = len(world.fake.provider_calls)
        answer, reading = own_check(world)
        assert len(world.fake.provider_calls) > calls  # the two other pairs were really fetched
    assert answer.ok, answer.detail
    assert reading.held == {TARGET}
    assert "links_skipped" not in reading.to_json()
    assert target_only(reading) is True
    assert epoch_of(world) == noted
    assert world.health.links is not None
    assert len(world.health.links.pairs) == 6  # the pre-run check's, not replaced
    world.fake.links.clear()  # the restore is done; one target pair breaks after it, and no time passes
    world.fake.links[(ALPHA_ID, BETA_ID)] = "fail"
    after = world.health()
    assert not after.ok
    assert "no transfer from providers[1] to providers[0]" in after.detail


def test_o90_a_real_failure_among_the_other_pairs_still_fails_and_raises_the_epoch(world):
    """O90: a pair that does not touch the held target and fails is a real link failure: the check fails naming it and
    the federation's link epoch is raised, as ever. The left-out pairs are not named."""
    with inside_injection(world):
        noted = epoch_of(world)
        stop_links_of(world.fake, BETA_ID)
        world.fake.links[(ALPHA_ID, GAMMA_ID)] = "fail"  # alpha reading from gamma: not the target's
        answer, reading = own_check(world)
    assert not answer.ok
    assert "no transfer from providers[2] to providers[0]" in answer.detail
    assert "providers[1]" not in answer.detail
    assert reading.held == {TARGET}
    assert epoch_of(world) > noted


def test_o90_when_every_pair_touches_the_held_target_the_link_check_is_skipped_and_the_reading_says_so(pair_world):
    """O90: with two providers both ordered pairs touch the target: no link check is made for this call (no probe is
    fetched), it passes on the space alone, the epoch is not raised, and the reading records that it was skipped and
    why, in its JSON only when it was."""
    world = pair_world
    with inside_injection(world, services=FakeServices(tuple(SERVICES.values()))):
        noted = epoch_of(world)
        stop_links_of(world.fake, BETA_ID, others=(ALPHA_ID, BETA_ID))
        world.ticker.sleep(world.config.link_check.every_s)
        calls = len(world.fake.provider_calls)
        answer = world.health()
        reading = world.health.reading
        assert len(world.fake.provider_calls) == calls  # nothing was fetched
    assert answer.ok, answer.detail
    assert reading.links_skipped == SKIPPED
    assert reading.held == {TARGET}
    assert target_only(reading) is True
    assert reading.to_json()["links_skipped"] == SKIPPED
    assert HealthReading.from_json(json.loads(json.dumps(reading.to_json()))) == reading
    assert epoch_of(world) == noted
    assert world.health.links is not None
    assert len(world.health.links.pairs) == 2  # the pre-run check's, kept


def test_o90_a_reading_with_nothing_skipped_has_the_fields_it_always_had(world):
    """O90: `links_skipped` is in a reading's JSON only when set: every other reading keeps its keys, and a stored one
    without it still reads back."""
    with inside_injection(world):
        answer, reading = own_check(world)
    assert answer.ok, answer.detail
    assert set(reading.to_json()) == {"spaces", "targets", "held"}
    assert HealthReading.from_json(reading.to_json()) == reading
    assert HealthReading.from_json({**reading.to_json(), "links_skipped": 7}) is None
    assert HealthReading.from_json({**reading.to_json(), "links_skipped": SKIPPED}) is not None  # held is not empty
    bare = {**reading.to_json(), "held": [], "links_skipped": SKIPPED}
    assert HealthReading.from_json(bare) is None  # a skipped check says which target was held


def test_o90_outside_an_own_injection_a_stale_link_check_and_the_epoch_are_as_they_were(world):
    """O90: no own injection (the pre-run check of the next run): the stale check makes every pair, the target's
    failing pairs fail it and raise the epoch, and nothing is left out or skipped."""
    assert world.health().ok
    with prerun.opened():
        noted = epoch_of(world)
        stop_links_of(world.fake, BETA_ID)
        answer, reading = own_check(world)
    assert not answer.ok
    assert "stopped transferring during the campaign" in answer.detail
    assert "providers[1]" in answer.detail
    assert reading.held == frozenset()
    assert "links_skipped" not in reading.to_json()
    assert epoch_of(world) > noted


def another_attempts(journal, _):
    journal.append({"event": "injecting", "id": "other", "target": "providers[2]", "service": "svc", "owner": ANOTHER})
    journal.append({"event": "stopping", "id": "other"})
    journal.append({"event": "injected", "id": "other"})


def ownerless(journal, mine):
    """The run's own entry as a harness outside any attempt leaves it: owned by the process alone."""
    lines = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
    for line in lines:
        if line.get("event") == "injecting" and line.get("target") == TARGET:
            line["owner"] = {"process": mine["process"]}
    journal.path.write_text("".join(json.dumps(line, sort_keys=True) + "\n" for line in lines), encoding="utf-8")


@pytest.mark.parametrize("extra", [another_attempts, ownerless], ids=["another-attempts", "ownerless"])
def test_o90_an_injection_that_is_not_the_runs_own_stays_unsettled_and_checks_no_link(world, extra):
    """O90: another attempt's open injection, or the run's own entry with no attempt as its owner, is not explained by
    the run's admission: the check fails as unsettled before any space or link is read, nothing is left out or
    skipped, no probe is fetched, and the epoch is not raised."""
    with inside_injection(world, extra=extra):
        noted = epoch_of(world)
        stop_links_of(world.fake, BETA_ID)
        world.ticker.sleep(world.config.link_check.every_s)
        calls = len(world.fake.provider_calls)
        answer = world.health()
        reading = world.health.reading
        assert len(world.fake.provider_calls) == calls
    assert not answer.ok
    assert " is unsettled: the federation is not in its baseline state" in answer.detail
    assert reading.spaces == ()  # unread
    assert "links_skipped" not in reading.to_json()
    assert epoch_of(world) == noted
