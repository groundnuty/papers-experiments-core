"""O12: no exception of the platform layer holds a value from platform.toml; messages name the key or the role.

Every other test of this directory is watched too: conftest.py records the message of each platform-layer exception it
builds and fails the test that built one holding a value of the fixture configuration. These tests pin the three places
that did, and the scan itself.
"""

import pytest
from platform_support import CREDENTIALS_DIR, FAKE_TOOL_SERVER, SPACE_NAME, USERS, leaked_values, write_platform

from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import load_platform
from expcore_onedata.credentials import Principal, SecretSource, UserCredentials


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
        f"cannot run {FAKE_TOOL_SERVER}",
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
