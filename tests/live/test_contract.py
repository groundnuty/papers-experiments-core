"""Contract tests against a real deployment, skipped unless EXPCORE_LIVE_PLATFORM names a platform.toml.

    EXPCORE_LIVE_PLATFORM=/path/to/platform.toml uv run pytest tests/live
    EXPCORE_LIVE_SLOW=1 EXPCORE_LIVE_PLATFORM=... uv run pytest tests/live   # also the test that waits out a lifetime

They check what the fake can only assume: that the endpoints the layer calls answer in the shape it parses. They read
(health, discovery, the examination of a token) and mint a short-lived token per plane for the configured fixed users.
Tokens are never revoked, so each one ends with its lifetime. Nothing here changes a space, a file or a privilege, and
without the variable nothing is constructed or contacted.
"""

import os
import time

import pytest

PLATFORM = os.environ.get("EXPCORE_LIVE_PLATFORM")
if not PLATFORM:
    pytest.skip("EXPCORE_LIVE_PLATFORM is not set; these tests contact a real deployment", allow_module_level=True)

from expcore_onedata.client import OnezoneClient, PlatformError  # noqa: E402 -- only once the module is known to run
from expcore_onedata.config import load_platform  # noqa: E402
from expcore_onedata.federation import pin_local_provider, read_space  # noqa: E402
from expcore_onedata.health import PlatformHealth  # noqa: E402
from expcore_onedata.tokencheck import CaveatSpec, TokenRefused  # noqa: E402
from expcore_onedata.tokens import TokenIssuer, data_path_spec, tool_server_spec  # noqa: E402

CEILING_S = 300.0
EXPIRY_S = 5  # the shortest lifetime the expiry test asks for
SLOW = pytest.mark.skipif(
    os.environ.get("EXPCORE_LIVE_SLOW") != "1",
    reason="EXPCORE_LIVE_SLOW=1 is not set; this test waits out a token's lifetime",
)


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
        pytest.skip("platform.toml declares no [principals.service] user")
    return config.principals.service


def test_a_user_mints_its_own_token_and_it_examines_cleanly(config, issuer):
    # covers: POST /tokens/examine
    user = service_user(config)
    token = issuer.mint(data_path_spec(config.space.id), user, ceiling_s=CEILING_S)  # minted and examined inside mint
    assert token.subject == user.user_id  # the answer's subject names the user the token was minted for
    assert token.expires_at > time.time()


def test_the_study_space_resolves_to_its_declared_name(config, client):
    # covers: GET /spaces/{space}
    assert client.space_name(config.space.id) == config.space.name  # the name the mutation gate checks the prefixes of


def test_discovery_finds_the_space_and_the_declared_local_provider(config, issuer, client):
    # covers: POST /tokens/infer_access_token_scope
    user = service_user(config)
    token = issuer.mint(data_path_spec(config.space.id), user, ceiling_s=CEILING_S)
    view = read_space(client, token.reveal(), config.space.id)  # refuses a provider without name, domain or online
    pinned = pin_local_provider(view, config)
    assert view.name == config.space.name
    assert pinned.provider.name == config.support.local
    for provider in view.providers:
        assert isinstance(provider.name, str)
        assert isinstance(provider.online, bool)


def test_the_health_check_reads_the_declared_support_set(config, client, issuer):
    service_user(config)
    result = PlatformHealth(config, client, issuer)()
    assert result.ok, result.detail


def test_each_condition_mints_and_examines_its_tool_server_token(config, issuer):
    if config.toolserver is None or config.principals.toolserver is None:
        pytest.skip("platform.toml declares no tool server or no [principals.toolserver] user")
    for name in config.toolserver.conditions:
        spec = tool_server_spec(config.space.id, config.condition(name))
        try:
            issuer.mint(spec, config.principals.toolserver, ceiling_s=CEILING_S)
        except TokenRefused as refused:
            pytest.skip(f"the tool-server user does not hold condition {name}'s rights: {refused}")


@SLOW
def test_a_token_stops_working_when_its_lifetime_ends(config, client):
    """Tokens are not revoked; they expire. Mint with a minimal lifetime, wait it out, expect a refusal (401 or 403)."""
    user = service_user(config)
    caveats = CaveatSpec(config.space.id, readonly=True, interface="rest").caveats(int(time.time()) + EXPIRY_S)
    value = client.mint_own_token(user, caveats)
    read_space(client, value, config.space.id)  # it works while it lives
    time.sleep(EXPIRY_S + 5)
    with pytest.raises(PlatformError) as refused:
        read_space(client, value, config.space.id)
    assert refused.value.status in (401, 403)
