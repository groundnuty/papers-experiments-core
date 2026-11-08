"""Contract tests against a real deployment, read-only, skipped unless EXPCORE_LIVE_PLATFORM names a platform.toml.

    EXPCORE_LIVE_PLATFORM=/path/to/platform.toml uv run pytest tests/live

They check what the fake can only assume: that the endpoints the layer calls answer in the shape it parses. They read
(health, discovery, the examination of a token) and mint one short-lived token per plane, which each test revokes.
Nothing here changes a space, a file or a privilege, and without the variable nothing is constructed or contacted.
"""

import os
import time

import pytest

PLATFORM = os.environ.get("EXPCORE_LIVE_PLATFORM")
if not PLATFORM:
    pytest.skip("EXPCORE_LIVE_PLATFORM is not set; these tests contact a real deployment", allow_module_level=True)

from expcore_onedata.client import OnezoneClient  # noqa: E402 -- imported only once the module is known to run
from expcore_onedata.config import load_platform  # noqa: E402
from expcore_onedata.federation import pin_local_provider, read_space  # noqa: E402
from expcore_onedata.health import PlatformHealth  # noqa: E402
from expcore_onedata.tokencheck import TokenRefused  # noqa: E402
from expcore_onedata.tokens import TokenIssuer, data_path_spec, token_window, tool_server_spec  # noqa: E402

TIME_CAP_S = 300.0


@pytest.fixture(scope="module")
def config():
    return load_platform(PLATFORM)


@pytest.fixture(scope="module")
def client(config):
    client = OnezoneClient(config)
    yield client
    client.close()


@pytest.fixture(scope="module")
def issuer(client, config):
    return TokenIssuer(client, config, clock=time.time)


def service_user(config):
    if config.principals.service is None:
        pytest.skip("platform.toml declares no [principals] service user")
    return config.principals.service


def test_a_freshly_minted_data_path_token_examines_cleanly_and_is_revoked(config, issuer):
    user = service_user(config)
    spec = data_path_spec(config.space.id)
    token = issuer.mint(spec, user, time_cap_s=TIME_CAP_S)  # examined inside mint
    try:
        assert token.subject == user
        assert token.expires_at > time.time()
    finally:
        issuer.revoke(token)
    with pytest.raises(TokenRefused):  # a revoked token is no longer examined as valid
        issuer.examine(token.reveal(), spec, user, token_window(TIME_CAP_S, config.tokens))


def test_discovery_finds_the_space_and_the_declared_local_provider(config, issuer, client):
    user = service_user(config)
    token = issuer.mint(data_path_spec(config.space.id), user, time_cap_s=TIME_CAP_S)
    try:
        view = read_space(client, token.reveal(), config.space.id)
        pinned = pin_local_provider(view, config)
    finally:
        issuer.revoke(token)
    assert view.name == config.space.name
    assert pinned.provider.name == config.support.local


def test_the_health_check_reads_the_declared_support_set(config, client, issuer):
    service_user(config)
    result = PlatformHealth(config, client, issuer)()
    assert result.ok, result.detail


def test_each_condition_mints_and_examines_its_tool_server_token(config, issuer):
    if config.toolserver is None or config.principals.toolserver is None:
        pytest.skip("platform.toml declares no tool server or no [principals] toolserver user")
    for name in config.toolserver.conditions:
        spec = tool_server_spec(config.space.id, config.condition(name))
        try:
            token = issuer.mint(spec, config.principals.toolserver, time_cap_s=TIME_CAP_S)
        except TokenRefused as refused:
            pytest.skip(f"the tool-server user does not hold condition {name}'s rights: {refused}")
        issuer.revoke(token)
