"""Fixtures for the platform layer: a fake federation, a loaded configuration, the client and the issuer."""

import pytest
from fake_federation import FakeFederation
from platform_support import ADMIN_ENV, BASELINE, NOW, SPACE_NAME, write_platform
from record_support import no_secrets_in_the_environment

from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import PlatformConfig, load_platform
from expcore_onedata.tokens import TokenIssuer

__all__ = ["no_secrets_in_the_environment"]


@pytest.fixture(autouse=True)
def _clean_environment(no_secrets_in_the_environment):
    """Start and end every test with no secret in the environment and none registered (the registry only grows)."""


@pytest.fixture
def fake(monkeypatch) -> FakeFederation:
    """A federation with the study space, one provider that supports it, and three users with view and read."""
    federation = FakeFederation(space_name=SPACE_NAME)
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    for user in ("u-data", "u-tools", "u-svc"):
        federation.grant(user, BASELINE)
    return federation


@pytest.fixture
def config(tmp_path, fake) -> PlatformConfig:
    return load_platform(write_platform(tmp_path))


@pytest.fixture
def client(config, fake) -> OnezoneClient:
    return OnezoneClient(config, http=fake.client_http())


@pytest.fixture
def issuer(client, config) -> TokenIssuer:
    return TokenIssuer(client, config, clock=lambda: NOW)
