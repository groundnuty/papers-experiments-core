"""O35: the session gate's REST readings against the fake federation: its probe file made before the injection,
Onezone's view of every provider of the probe space, and the block map read through each peer of the restored
provider."""

from pathlib import Path

import pytest
from fault_support import faults_table
from link_support import BETA_ID, GAMMA_ID, add_providers, three_providers

from expcore_onedata.client import OnezoneClient
from expcore_onedata.compose import ComposeDriver
from expcore_onedata.config import load_platform
from expcore_onedata.fault_serves import provider_serves
from expcore_onedata.fault_sessions import GATE_PROBE_NAME, await_sessions
from expcore_onedata.fault_settings import load_faults
from expcore_onedata.testing import ALPHA_ID, write_platform

STORES = {"alpha": "store-one", "beta": "store-two", "gamma": "store-three"}
COMPOSE = Path("/srv/stack/compose.yaml")  # never run: the gate's readings send no command
NO_CONNECTION = {"success": False, "error": {"id": "noConnectionToPeerOneprovider", "description": "no connection"}}


@pytest.fixture
def fake(fake):
    return add_providers(fake)


@pytest.fixture
def probe(tmp_path, fake):
    platform = write_platform(
        tmp_path, lambda d: (three_providers(d), d.__setitem__("faults", faults_table(services=STORES)))
    )
    config = load_platform(platform)
    return provider_serves(config, load_faults(platform, config), OnezoneClient(config, http=fake.client_http()))


def creations(fake):
    return [call for call in fake.provider_calls if call[1] == "POST" and call[2].endswith("/children")]


def test_o35_arming_writes_the_gate_probe_once_through_the_local_provider_and_every_provider_sees_it(probe, fake):
    """O35: the probe file is made before the injection, through the local provider, and seen at its size by every
    provider of the probe space; a later arming finds it and writes nothing."""
    file_id = probe.arm(STORES["beta"])
    assert fake.nodes[file_id].name == GATE_PROBE_NAME
    assert [call[0] for call in creations(fake)] == [ALPHA_ID]
    assert probe.arm(STORES["beta"]) == file_id
    assert len(creations(fake)) == 1


def test_o35_the_gate_passes_when_every_provider_is_online_and_every_peer_holds_an_answer_for_the_restored_one(
    probe, fake
):
    """O35: Onezone is asked for every provider of the probe space, and the block map is read through each peer of
    the restored provider, never through the restored provider itself, which starts with no sessions."""
    file_id = probe.arm(STORES["beta"])
    calls = len(fake.provider_calls)
    assert probe.sessions(STORES["beta"], file_id) is None
    readers = [c[0] for c in fake.provider_calls[calls:] if c[1] == "GET" and c[2].endswith("/distribution")]
    assert readers == [ALPHA_ID, GAMMA_ID]


def test_o35_a_provider_onezone_shows_offline_is_not_ready(probe, fake):
    """O35: a provider of the probe space Onezone does not show online fails the reading, named by its label."""
    file_id = probe.arm(STORES["beta"])
    fake.providers[GAMMA_ID]["online"] = False
    assert probe.sessions(STORES["beta"], file_id) == "Onezone does not show providers[2] online"


def test_o35_an_error_entry_for_the_restored_provider_keeps_the_gate_closed_past_its_bound(probe, fake, ticker):
    """O35: a peer whose session to the restored provider is not up keeps an error as that provider's entry of the
    block map; the gate, polled to its bound, fails closed naming the peer, and passes once the entry is an answer."""
    file_id = probe.arm(STORES["beta"])
    fake.distribution_entries[(file_id, BETA_ID)] = NO_CONNECTION
    driver = ComposeDriver(COMPOSE, probe=probe)
    reading = await_sessions(driver, STORES["beta"], file_id, 10, 1, ticker.clock, ticker.sleep)
    assert (reading["ok"], reading["how"]) == (False, "not_ready")
    assert reading["waited_s"] >= 10
    assert reading["detail"].startswith("providers[0] holds an error, or no entry, for providers[1]")
    del fake.distribution_entries[(file_id, BETA_ID)]
    assert await_sessions(driver, STORES["beta"], file_id, 10, 1, ticker.clock, ticker.sleep)["ok"] is True


def test_o35_an_injection_with_no_gate_probe_fails_the_gate_closed_at_once(probe, ticker):
    """O35: no file made later can stand in for one made before the injection, so an injection that journalled none
    fails the gate at once (`unarmed`), with no wait."""
    driver = ComposeDriver(COMPOSE, probe=probe)
    reading = await_sessions(driver, STORES["beta"], None, 240, 2, ticker.clock, ticker.sleep)
    assert (reading["ok"], reading["how"], reading["waited_s"]) == (False, "unarmed", 0.0)
