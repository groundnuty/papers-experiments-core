"""O35: `[faults] heal_timeout_s`, left undeclared, is the least the loader asks of a declared heal plus a margin.

The loader refuses a `heal_timeout_s` that cannot hold one round of the declared mode's steps, and with the other
defaults (120 s commands, 180 s settles, the container fallback) five providers in `"all_links"` need 4,290 s, more
than the old fixed default of 4,200 s: a five-provider study that declared the mode and nothing else was refused. The
default is now derived from the declared providers and mode by the loader's own computation (`heal_round_s`), plus a
tenth, rounded up to the minute. These tests build the federation with only the keys a study must declare, so every
other setting is the real default.
"""

import pytest
from heal_support import NAMES as FOUR_NAMES
from heal_support import STORES as FOUR_STORES

from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.fault_settings import heal_round_s, heal_token_margin_s, load_faults
from expcore_onedata.testing import write_platform

NAMES = (*FOUR_NAMES, "epsilon")
ROOMY_MARGIN_S = 20_000  # a token margin no heal here needs more than: the tests of the timeout leave it out of play


def federation(count, *, margin_s=ROOMY_MARGIN_S, **faults):
    """An edit of the fixture platform: `count` providers, each with a service, a `[faults]` table that declares what
    a study must (the sanction, the journal, the Compose file and project, the services) and `faults`, and a token
    margin."""

    def edit(data):
        for index, name in enumerate(NAMES[2:count], start=3):
            data["providers"][name] = {"url": f"https://{name}.test", "id": f"pr-{name}-{index}"}
        data["faults"] = {
            "sanctioned": True,
            "journal": "fault-journal",
            "compose_file": "compose.yaml",
            "project": "stack-a",
            "services": {name: FOUR_STORES.get(name, f"store-{name}") for name in NAMES[:count]},
            **faults,
        }
        data["tokens"] = {"margin_s": margin_s}

    return edit


def load(tmp_path, count, **faults):
    platform = write_platform(tmp_path, federation(count, **faults))
    return load_faults(platform, load_platform(platform, require_secrets=False))


@pytest.mark.parametrize(
    ("count", "fallback", "least", "expected"),
    [(4, True, 3780, 4200), (4, False, 2220, 2460), (5, True, 4290, 4740), (5, False, 2730, 3060)],
)
def test_o35_an_undeclared_heal_timeout_is_the_least_a_round_needs_plus_a_tenth_to_the_minute(
    tmp_path, count, fallback, least, expected
):
    """O35: four and five providers in `"all_links"`, with the container fallback and without it, load with no
    `heal_timeout_s`. The effective one is at least the loader's least (3,780 s and 2,220 s for four providers, 4,290 s
    and 2,730 s for five, with the other defaults), and is that least plus 10 %, rounded up to a whole minute."""
    settings = load(tmp_path, count, heal="all_links", heal_container_fallback=fallback)
    assert heal_round_s(settings) == least
    assert settings.heal_timeout_s >= least
    assert settings.heal_timeout_s == expected
    assert settings.heal_timeout_s % 60 == 0


@pytest.mark.parametrize(("mode", "least", "expected"), [("link", 690, 780), ("link_then_container", 2250, 2520)])
def test_o35_the_per_reader_modes_derive_their_own_least(tmp_path, mode, least, expected):
    """O35: one link restart needs 690 s with the defaults, and with a container restart 2,250 s; the undeclared bound
    is each plus a tenth to the minute (780 s and 2,520 s)."""
    settings = load(tmp_path, 4, heal=mode)
    assert heal_round_s(settings) == least
    assert settings.heal_timeout_s == expected


def test_o35_the_derivation_follows_the_other_declared_bounds(tmp_path):
    """O35: the least is counted from the declared commands, settles and ready wait, so the default follows them: five
    providers with 10 s commands, 5 s waits and a 30 s settle need 1,045 s, and the undeclared bound is 1,200 s."""
    shorter = {"command_timeout_s": 10, "settle_timeout_s": 5, "ready_timeout_s": 5, "heal_settle_s": 30}
    settings = load(tmp_path, 5, heal="all_links", **shorter)
    assert heal_round_s(settings) == 1045
    assert settings.heal_timeout_s == 1200


def test_o35_a_least_that_is_a_whole_number_of_minutes_is_not_rounded_up_a_minute_too_far(tmp_path):
    """O35: the margin is counted in integer percent, so a least of exactly 3,000 s (one link restart: three 100 s
    commands, the RPC's 120 s and 30 s margin, and a 2,550 s settle) derives 3,300 s, not 3,360 s, which a float
    tenth would give."""
    settings = load(tmp_path, 4, heal="link", command_timeout_s=100, heal_settle_s=2550)
    assert heal_round_s(settings) == 3000
    assert settings.heal_timeout_s == 3300


@pytest.mark.parametrize("declared", [4289, 4200])
def test_o35_a_declared_heal_timeout_below_the_least_is_still_refused_naming_both_numbers(tmp_path, declared):
    """O35: a declared value is never derived or raised: five providers in `"all_links"` need 4,290 s, so 4,289 s and
    the old default of 4,200 s are refused at load, naming the least and the declared value; 4,290 s loads as
    declared, and a larger one too."""
    with pytest.raises(ConfigError, match=rf"heal_timeout_s.*at least 4290\b.*not {declared}\b"):
        load(tmp_path, 5, heal="all_links", heal_timeout_s=declared)
    assert load(tmp_path, 5, heal="all_links", heal_timeout_s=4290).heal_timeout_s == 4290
    assert load(tmp_path, 5, heal="all_links", heal_timeout_s=9000).heal_timeout_s == 9000


def test_o35_a_heal_that_is_off_needs_no_bound(tmp_path):
    """O35: with the heal off there is no round, so no bound is derived: nothing reads `heal_timeout_s`, and a study
    that declares no heal loads as it did, however many providers it has."""
    assert load(tmp_path, 5).heal_timeout_s == 0
    assert load(tmp_path, 5, heal="off").heal == "off"


def test_o35_the_long_token_margin_follows_the_derived_bound(tmp_path):
    """O35: the long margin, with which a heal runs while a step of the run that uses its tokens still runs
    (`heal_token_margin_s`), is the restore's margin, two heals of the effective `heal_timeout_s` and a session drain:
    for five providers at the derived 4,740 s, 2,134 + 2 x 4,740 + 320 = 11,934 s with the default drain, and
    1,814 + 2 x 4,740 = 11,294 s with none. It is never asked at load: a `[tokens] margin_s` one second short of it, and
    no shorter than the restore's own margin, loads, and the heal is then skipped while such a step runs."""
    for drain, needed in ((None, 11_934), (0, 11_294)):
        faults = {"heal": "all_links"} if drain is None else {"heal": "all_links", "session_drain_s": drain}
        settings = load(tmp_path, 5, margin_s=needed - 1, **faults)
        assert settings.heal_timeout_s == 4740
        assert heal_token_margin_s(settings) == needed
