"""A four-space platform over the fake federation, for the health check's readings (O90).

Three providers (`alpha`, `beta`, `gamma`, so `providers[0]`, `providers[1]`, `providers[2]`) and four spaces, each with
its own support set, so that one provider's outage shows in some spaces and not others. Onezone's `online` flag belongs
to the provider, so taking one down shows it in every space it supports."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fault_support import faults_table

from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import PlatformConfig, load_platform
from expcore_onedata.health import PlatformHealth
from expcore_onedata.testing import ALPHA_ID, BASELINE, BETA_ID, NOW, USERS, FakeFederation, write_platform
from expcore_onedata.testing.world import SPACE_IDS, SPACE_NAMES, with_spaces
from expcore_onedata.tokens import TokenIssuer

GAMMA_ID = "pr-gamma"
IDS = {"alpha": ALPHA_ID, "beta": BETA_ID, "gamma": GAMMA_ID}
FOURTH = ("ridgeline", "study-c-ridge", "sp-fourth-0004")  # label, name, id of the fourth space
# the declared support set of each space in order: `beta` (providers[1]) supports the second and the fourth
SUPPORTS = (("alpha", "gamma"), ("alpha", "beta"), ("alpha", "gamma"), ("alpha", "beta", "gamma"))
SPACE_ID_LIST = (*SPACE_IDS, FOURTH[2])


@dataclass
class World:
    """What a test of the reading holds: the check, the configuration, the platform file and the federation."""

    health: PlatformHealth
    config: PlatformConfig
    platform: Path
    fake: FakeFederation


def four_spaces(tmp_path, fake: FakeFederation, *, edit=None, faults: dict[str, Any] | None = None) -> World:
    """The four-space platform over `fake`: every space known to Onezone and supported as `SUPPORTS` says, every test
    user granted in each. `faults` adds a `[faults]` table with those changes over the fixture's; `edit` edits the
    document last. The check is built with no link check (`links=False`), which the fake has no data API for."""
    fake.add_provider(GAMMA_ID, "gamma.test")  # supports the study space, which is the first
    fake.add_provider(BETA_ID, "beta.test", supports=False)
    names = (*SPACE_NAMES, FOURTH[1])
    for index in range(1, 4):
        fake.add_space(SPACE_ID_LIST[index], names[index], [IDS[name] for name in SUPPORTS[index]])
        for _, user_id in USERS.values():
            fake.grant(user_id, BASELINE, space=SPACE_ID_LIST[index])

    def document(data: dict[str, Any]) -> None:
        with_spaces(data, 3)
        data["providers"]["gamma"] = {"url": "https://gamma.test", "id": GAMMA_ID}
        for entry, support in zip(data["spaces"], SUPPORTS, strict=False):
            entry["providers"] = list(support)
        label, name, space_id = FOURTH
        data["spaces"].append(
            {**data["spaces"][2], "label": label, "name": name, "id": space_id, "providers": list(SUPPORTS[3])}
        )
        data["spaces"][3]["prefixes"] = ["study-c-"]
        if faults is not None:
            data["faults"] = faults_table(**faults)
        if edit is not None:
            edit(data)

    platform = write_platform(tmp_path, document)
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    health = PlatformHealth(
        config, client, TokenIssuer(client, config, clock=lambda: NOW), clock=lambda: NOW, links=False
    )
    return World(health, config, platform, fake)


def down(fake: FakeFederation, *names: str) -> None:
    """Make Onezone report these providers (by declared name) offline."""
    for name in names:
        fake.providers[IDS[name]]["online"] = False


def up(fake: FakeFederation, *names: str) -> None:
    """Make Onezone report these providers (by declared name) online again."""
    for name in names:
        fake.providers[IDS[name]]["online"] = True
