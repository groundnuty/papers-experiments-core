"""Fixtures for the platform layer: a fake federation, a loaded configuration, the client and the issuer."""

import pytest
from fake_federation import FakeFederation
from platform_support import ADMIN_ENV, BASELINE, NOW, SPACE_NAME, USERS, leaked_values, write_platform
from record_support import no_secrets_in_the_environment

from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore_onedata.client import OnezoneClient, OutOfScope, PlatformError
from expcore_onedata.config import PlatformConfig, load_platform
from expcore_onedata.federation import FederationUnreadable, PinRefused
from expcore_onedata.pin import PinError
from expcore_onedata.tokencheck import ExaminationFailed, TokenRefused
from expcore_onedata.tokens import TokenIssuer

__all__ = ["no_secrets_in_the_environment"]


@pytest.fixture(autouse=True)
def _clean_environment(no_secrets_in_the_environment):
    """Start and end every test with no secret in the environment and none registered (the registry only grows)."""


@pytest.fixture
def fake(monkeypatch) -> FakeFederation:
    """A federation with the study space, one supporting provider, and three users who can log in and read."""
    federation = FakeFederation(space_name=SPACE_NAME)
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    for login, (password, user_id) in USERS.items():
        federation.add_user(login, password, user_id)
        federation.grant(user_id, BASELINE)
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


# The exceptions the platform layer raises (and the core's two it passes on). Every message of every one built during
# a test of this directory is scanned for the values of the fixture configuration when the test ends (O12).
LAYER_EXCEPTIONS = (
    ConfigError,
    PlatformError,
    OutOfScope,
    FederationUnreadable,
    PinRefused,
    PinError,
    TokenRefused,
    ExaminationFailed,
    InfraError,
)


@pytest.fixture(autouse=True)
def _no_exception_holds_a_configured_value(monkeypatch, tmp_path):
    """O12: record the message of every platform-layer exception the test builds, and fail the test if any of them
    holds a value of the fixture configuration (a name, a URL, a host, an id, a user or a path)."""
    messages: list[str] = []

    def recording(original):
        def init(self, *args, **kwargs):
            original(self, *args, **kwargs)
            messages.append(str(self))

        return init

    for exception in LAYER_EXCEPTIONS:
        monkeypatch.setattr(exception, "__init__", recording(exception.__init__))
    yield
    leaks = {text: found for text in messages if (found := leaked_values(text, tmp_path))}
    assert not leaks, f"a message of the platform layer holds configured values: {leaks}"
