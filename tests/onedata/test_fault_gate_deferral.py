"""O35: a campaign whose `max_defer_s` the fault gate's heal can outlast is refused at its start.

While the gate heals stuck pairs every check answers the uncharged deferral, for up to the heal's bound
(`heal_timeout_s` plus `heal_settle_s`), and after an outage run a session drain (`session_drain_s`) can come first,
with no check between them that passes. The core's `max_defer_s` (3,600 s by default) turns a longer deferral into a
blocking hold, and a campaign that exits when blocked (every `run`) would stop on a heal it should have waited out. So
the platform's check declares that bound (`max_deferral_s`), and the core refuses a shorter `max_defer_s` at the start.
"""

from functools import partial

import pytest
from fault_support import FakeServices, faults_table
from heal_support import heal_margin_s
from scheduler_support import build, settings_for

from expcore.config.loading import ConfigError
from expcore_onedata.admission import fault_settings_of
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.health import PlatformHealth
from expcore_onedata.testing import NOW, write_platform
from expcore_onedata.tokens import TokenIssuer

FIVE = ("alpha", "beta", "gamma", "delta", "epsilon")
FIVE_STORES = {name: f"store-{name}" for name in FIVE}
SMALL = {"tasks": ("task-a",), "seeds": (1,)}


def five_providers(data, **faults):
    """Five providers, each with a service, at the designed bounds of a restore's waits; the heal's bound is declared
    (the least five providers' `"all_links"` with a container fallback needs at these bounds, 4,290 s), so no default
    moves the figures, and the token margin is the one a declared heal needs at load."""
    for name, index in (("gamma", 3), ("delta", 4), ("epsilon", 5)):
        data["providers"][name] = {"url": f"https://{name}.test", "id": f"pr-{name}-{index}"}
    designed = {"settle_timeout_s": 180, "ready_timeout_s": 180, "heal_timeout_s": 4290, "session_drain_s": 320}
    data["faults"] = faults_table(**{"services": dict(FIVE_STORES), **designed, **faults})
    data["tokens"] = {**data.get("tokens", {}), "margin_s": heal_margin_s(data["faults"])}


def platform_check(tmp_path, fake, edit):
    """The platform's standard health check over the platform.toml `edit` writes, and that file's `[faults]`."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    platform = write_platform(tmp_path, edit)
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    issuer = TokenIssuer(client, config, clock=lambda: NOW)
    services = FakeServices(tuple(FIVE_STORES.values()))
    health = PlatformHealth(config, client, issuer, clock=lambda: NOW, fault_driver=services)
    return health, fault_settings_of(config)


@pytest.mark.parametrize("drain_s", [320, 0], ids=["with-a-drain", "no-drain"])
def test_o35_five_providers_healing_all_links_refuse_the_default_max_defer_s_at_the_start(tmp_path, fake, drain_s):
    """O35: five providers healing `"all_links"` defer the gate for up to `session_drain_s`, `heal_timeout_s` and
    `heal_settle_s` (4,790 s at the designed bounds, 4,470 s with no drain), past the default `max_defer_s` of 3,600 s:
    the campaign is refused when it starts, naming both figures and the fix."""
    edit = partial(five_providers, heal="all_links", session_drain_s=drain_s)
    health, faults = platform_check(tmp_path / "platform", fake, edit)
    bound = faults.session_drain_s + faults.heal_timeout_s + faults.heal_settle_s
    with pytest.raises(ConfigError) as refused:
        build(tmp_path / "campaign", health=health, plan=SMALL)
    text = str(refused.value)
    for part in ("[scheduler] max_defer_s", "3600 s", f"up to {bound:g} s", 'heal = "all_links"', 'heal = "off"'):
        assert part in text, text


def test_o35_a_max_defer_s_raised_to_the_heals_bound_starts(tmp_path, fake):
    """O35: raising `max_defer_s` to the declared bound of a drain and a heal starts the campaign."""
    health, faults = platform_check(tmp_path / "platform", fake, partial(five_providers, heal="all_links"))
    bound = faults.session_drain_s + faults.heal_timeout_s + faults.heal_settle_s
    assert health.max_deferral_s().seconds == bound
    with build(tmp_path / "campaign", health=health, settings=settings_for(max_defer_s=bound), plan=SMALL):
        pass


def test_o35_with_the_heal_off_the_platform_declares_no_longest_deferral(tmp_path, fake):
    """O35: with `heal = "off"` the gate heals nothing, so its check declares no longest deferral and any
    `max_defer_s` starts."""
    health, _ = platform_check(tmp_path / "platform", fake, partial(five_providers, heal="off"))
    assert health.max_deferral_s() is None
    with build(tmp_path / "campaign", health=health, settings=settings_for(max_defer_s=1.0), plan=SMALL):
        pass


def test_o35_without_faults_the_platform_declares_no_longest_deferral(tmp_path, fake):
    """O35: a platform.toml with no `[faults]` has no fault gate, and its check declares no longest deferral."""
    health, faults = platform_check(tmp_path / "platform", fake, lambda data: None)
    assert faults is None
    assert health.max_deferral_s() is None
