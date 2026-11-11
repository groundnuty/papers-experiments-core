"""Fixtures for provisioning and placement: the fake federation with its data API, two providers and a ticking clock."""

import pytest

from expcore_onedata.config import PlatformConfig, load_platform
from expcore_onedata.provision import Provisioner, resolve_provider_ids
from expcore_onedata.rangemap import Placement, load_placement
from expcore_onedata.testing import ADMIN_ENV, DATA, FakeData, Ticker, make_federation, write_with_placement


@pytest.fixture
def fake(monkeypatch, ticker) -> FakeData:
    """A federation with the study space, the data provider `alpha`, a second provider `beta`, and the three users.

    Its clock is the test's ticker, so its jobs' times and its providers' Date headers agree with the probes' clock
    unless a test skews a provider."""
    federation = make_federation(clock=ticker.clock)
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
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
