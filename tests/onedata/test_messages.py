"""O12: no exception of the platform layer holds a value from platform.toml; messages name the key or the role.

Every other test of this directory is watched too: conftest.py records the message of each platform-layer exception it
builds and fails the test that built one holding a value of the fixture configuration. These tests pin the three places
that did, and the scan itself.
"""

import pytest
from platform_support import leaked_values

from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import load_platform
from expcore_onedata.credentials import Principal, SecretSource, UserCredentials
from expcore_onedata.testing import CREDENTIALS_DIR, SPACE_NAME, TOOL_SERVER_ARGS, USERS, write_platform


def test_o12_the_scan_finds_a_configured_value_and_ignores_the_config_file_and_ordinary_words(tmp_path):
    """O12: a host, a name, an id or a credential directory in a message is found; the path of platform.toml, a role
    word and a condition name used as a word are not."""
    for message in (
        "failed to reach alpha.test",
        f"user {USERS['login-data'][1]} is unknown",
        f"cannot read {tmp_path / CREDENTIALS_DIR}",
        "no condition 'read'",
        f"the space {SPACE_NAME} is gone",
        "provider pr-beta answered",
        f"cannot run {TOOL_SERVER_ARGS[1]}",
    ):
        assert leaked_values(message, tmp_path), message
    for message in (
        f"{tmp_path / 'platform.toml'}: [providers[1]]: key 'url' is required",
        "zone: GET answered 503 (internalServerError)",
        "the token could not be read as a list of privileges",
        "[credentials.admin] file: is not a regular file owned by the current user",
    ):
        assert leaked_values(message, tmp_path) == [], message


def test_o12_a_provider_is_named_by_its_position_in_a_request_error(config, fake, client):
    """O12: a failing request to a provider says `providers[1]`, never the provider's name or host."""
    fake.status_override = 503
    with pytest.raises(PlatformError) as caught:
        client.get("/spaces", provider="beta")
    assert str(caught.value) == "providers[1]: GET answered 503 (internalServerError)"
    with pytest.raises(PlatformError, match=r"providers\[0\]: GET answered 503"):
        client.get("/spaces", provider="alpha")


def test_o12_a_transport_failure_of_a_provider_names_its_position_only(config):
    """O12: the label of a transport error is also the position, the same as for an answer."""
    import httpx

    def refuse(request):
        raise httpx.ConnectError("no route")

    client = OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(refuse)))
    with pytest.raises(PlatformError) as caught:
        client.get("/spaces", provider="beta")
    assert str(caught.value) == "providers[1]: GET failed (ConnectError)"


def test_o12_a_credential_source_is_named_by_its_config_key_not_by_its_variable_file_or_directory(tmp_path):
    """O12: a missing admin variable, admin file, user directory and user file are each reported by the key."""
    variable = SecretSource("env", "SOME_ADMIN_VARIABLE")
    missing = tmp_path / "no-such-admin-file"
    for source in (variable, SecretSource("file", str(missing))):
        with pytest.raises(ConfigError, match=r"\[credentials\.admin\]") as caught:
            source.read({})
        assert "SOME_ADMIN_VARIABLE" not in str(caught.value)
        assert str(missing) not in str(caught.value)
    by_env = UserCredentials("env", "SOME_USERS_VARIABLE")
    with pytest.raises(ConfigError, match=r"\[credentials\.users\] directory_env") as caught:
        by_env.read("login-data", {})
    assert "SOME_USERS_VARIABLE" not in str(caught.value)
    absent = UserCredentials("dir", str(tmp_path / "no-such-directory"))
    with pytest.raises(ConfigError, match=r"\[credentials\.users\] directory") as caught:
        absent.read("login-data")
    assert "no-such-directory" not in str(caught.value)


def test_o12_a_user_that_cannot_log_in_is_reported_without_the_directory(client, fake, tmp_path):
    """O12: the client's message for a user whose credential cannot be used holds the key, not the directory."""
    with pytest.raises(PlatformError, match=r"\[credentials\.users\]") as caught:
        client.mint_own_token(Principal("login-nobody", "u-nobody"), [{"type": "time", "validUntil": 1}])
    assert str(tmp_path / CREDENTIALS_DIR) not in str(caught.value)


def test_o12_a_provider_or_condition_with_a_bad_name_is_reported_by_position(tmp_path, monkeypatch, fake):
    """O12: a bad entry of a table of named tables is `providers[2]` or `conditions[1]`, not its name."""

    def bad_provider(data):
        data["providers"]["9-bad-provider"] = {"url": "https://gamma.test", "id": "pr-gamma"}

    with pytest.raises(ConfigError, match=r"providers\[2\]") as caught:
        load_platform(write_platform(tmp_path, bad_provider))
    assert "9-bad-provider" not in str(caught.value)

    def bad_condition(data):
        data["toolserver"]["conditions"]["manage"]["allow"] = []

    with pytest.raises(ConfigError, match=r"conditions\[1\]") as caught:
        load_platform(write_platform(tmp_path, bad_condition))
    assert "manage" not in str(caught.value).replace(str(tmp_path), "")

    def bad_condition_name(data):
        data["toolserver"]["conditions"]["9-bad-condition"] = data["toolserver"]["conditions"]["read"]

    with pytest.raises(ConfigError, match=r"conditions\[2\]") as caught:
        load_platform(write_platform(tmp_path, bad_condition_name))
    assert "9-bad-condition" not in str(caught.value)


def test_o12_a_provider_entry_that_is_not_a_table_is_reported_by_position(tmp_path, fake):
    """O12: `providers.<name> = "..."` is reported as an entry that is not a table, by position."""
    with pytest.raises(ConfigError, match=r"providers.*entry \d must be a table"):
        load_platform(write_platform(tmp_path, lambda d: d["providers"].update(gamma="https://gamma.test")))


def declared_both(data):
    data["support"]["providers"] = ["alpha", "beta"]


def health_detail(tmp_path, fake, edit=None):
    from expcore_onedata.health import PlatformHealth
    from expcore_onedata.testing import NOW
    from expcore_onedata.tokens import TokenIssuer

    config = load_platform(write_platform(tmp_path, edit))
    client = OnezoneClient(config, http=fake.client_http())
    return PlatformHealth(config, client, TokenIssuer(client, config, clock=lambda: NOW), clock=lambda: NOW)().detail


def test_o12_a_health_detail_names_declared_providers_by_position_and_others_by_their_reported_id(tmp_path, fake):
    """O12: whatever fails, the detail holds `providers[i]` for a declared provider and the federation's own id for a
    reported one nobody declared, and none of the file's names, ids, hosts or URLs."""
    details = []
    details.append(health_detail(tmp_path, fake, declared_both))  # beta is declared and not reported: missing
    fake.add_provider("pr-beta", "beta.test", online=False)
    fake.providers["prmock0001"]["online"] = False
    details.append(health_detail(tmp_path, fake, declared_both))  # both declared and reported, both offline
    details.append(health_detail(tmp_path, fake))  # beta is reported but not in the support set
    fake.add_provider("pr-stranger", "stranger.test")
    details.append(health_detail(tmp_path, fake, declared_both))  # a stranger beside two declared providers
    fake.add_provider("pr-second", "alpha.test")
    details.append(health_detail(tmp_path, fake))
    assert "missing providers[1]" in details[0]
    assert "offline: providers[0], providers[1]" in details[1]
    assert "declared but not in the support set: providers[1]" in details[2]
    assert "not declared: pr-stranger" in details[3]
    assert "2 reported provider(s) not declared: pr-second, pr-stranger" in details[4]
    for detail in details:
        assert leaked_values(detail, tmp_path) == [], detail
        assert "stranger.test" not in detail


def test_o12_a_host_only_declaration_that_is_ambiguous_is_reported_by_position(tmp_path, fake):
    """O12: an ambiguous match names the declared provider by position, not the name, host or id."""
    import dataclasses

    from expcore_onedata.federation import ReportedProvider, SpaceView
    from expcore_onedata.health import compare

    config = load_platform(write_platform(tmp_path))
    host_only = dataclasses.replace(config.providers["beta"], id=None)
    config = dataclasses.replace(config, providers={**config.providers, "beta": host_only})
    view = SpaceView(
        "spmock0001",
        config.space.name,
        (
            ReportedProvider("prmock0001", "a", "alpha.test", True),
            ReportedProvider("pr-one", "b", "beta.test", True),
            ReportedProvider("pr-two", "c", "beta.test", True),
        ),
    )
    detail = compare(view, config).detail
    assert "ambiguous match for providers[1]" in detail
    assert leaked_values(detail, tmp_path) == [], detail


def test_o12_reported_ids_that_are_not_plain_are_not_echoed_and_many_are_counted(tmp_path, fake):
    """O12: the federation's own ids are shown only when they are plain, and only a few of them."""
    from expcore_onedata.federation import ReportedProvider, SpaceView
    from expcore_onedata.health import compare

    config = load_platform(write_platform(tmp_path))
    strangers = tuple(ReportedProvider(f"pr-{i}", "x", "x.test", True) for i in range(8))
    odd = ReportedProvider("bad id\n<script>", "x", "x.test", True)
    detail = compare(SpaceView("spmock0001", config.space.name, (*strangers, odd)), config).detail
    assert "9 reported provider(s) not declared: pr-0, pr-1, pr-2, pr-3, pr-4 and 4 more" in detail
    assert "script" not in detail
    detail = compare(SpaceView("spmock0001", config.space.name, (odd,)), config).detail
    assert "not declared: (an id that is not plain)" in detail
