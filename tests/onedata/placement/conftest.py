"""Fixtures for provisioning and placement: the fake federation with its data API, two providers and a ticking clock."""

import pytest
from fake_data import FakeData
from placement_support import BETA_ID, DATA, Ticker, write_with_placement
from platform_support import ADMIN_ENV, BASELINE, SPACE_NAME, USERS

from expcore_onedata.config import PlatformConfig, load_platform
from expcore_onedata.provision import Provisioner, resolve_provider_ids
from expcore_onedata.rangemap import Placement, load_placement


@pytest.fixture
def fake(monkeypatch) -> FakeData:
    """A federation with the study space, the data provider `alpha`, a second provider `beta`, and the three users."""
    federation = FakeData(space_name=SPACE_NAME)
    federation.add_provider(BETA_ID, "beta.test", supports=False)
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    for login, (password, user_id) in USERS.items():
        federation.add_user(login, password, user_id)
        federation.grant(user_id, BASELINE)
    return federation


@pytest.fixture
def platform_file(tmp_path):
    return write_with_placement(tmp_path)


@pytest.fixture
def config(platform_file, fake) -> PlatformConfig:
    return load_platform(platform_file)


@pytest.fixture
def placement(platform_file, config) -> Placement:
    return load_placement(platform_file, config)


@pytest.fixture
def ticker() -> Ticker:
    return Ticker()


@pytest.fixture
def seeded(fake) -> FakeData:
    """The fake with one 4096-byte file held by the data provider."""
    fake.add_file("study/data.bin", DATA)
    return fake


@pytest.fixture
def provisioner(client, config, placement, ticker, tmp_path) -> Provisioner:
    return Provisioner(
        client,
        config,
        placement.settings,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    )
