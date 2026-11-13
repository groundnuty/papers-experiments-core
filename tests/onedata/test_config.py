"""O12: the federation's names live in platform.toml and are validated when it is loaded."""

import pytest

from expcore.config.loading import ConfigError
from expcore_onedata.config import SecretSource, load_platform
from expcore_onedata.rights import declares_write
from expcore_onedata.testing import ADMIN_ENV, BASELINE, MANAGE, SPACE_NAME, write_platform

SENTINEL = "sentinel-value-0042"  # a stand-in for a secret or a name that must never be echoed


def load(tmp_path, edit=None, **kwargs):
    return load_platform(write_platform(tmp_path, edit), **kwargs)


def test_o12_a_good_file_loads_with_every_name_from_the_file(tmp_path, monkeypatch):
    """O12: zone, providers, space, support set, local provider and per-condition rights all come from the file."""
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")
    config = load(tmp_path)
    assert config.zone.url == "https://zone.test"
    assert set(config.providers) == {"alpha", "beta"}
    assert (config.space.name, config.space.id, config.space.prefixes) == (SPACE_NAME, "spmock0001", ("study-a-",))
    assert config.support.providers == ("alpha",)
    assert config.local_provider.host == "alpha.test"
    assert config.condition("read").rights == tuple(sorted(BASELINE))
    assert config.condition("manage").writes
    assert not config.condition("read").writes
    assert config.condition("manage").manages_qos  # the right `SpaceReset.run(qos_rights=)` is told (O33)
    assert not config.condition("read").manages_qos
    assert config.condition("manage").allow == ("echo", "add", "fail")
    assert config.admin == SecretSource("env", ADMIN_ENV)


def test_o12_the_credential_is_a_source_not_a_value(tmp_path, monkeypatch):
    """O12: the configuration records where the credential comes from; reading it needs the environment."""
    monkeypatch.setenv(ADMIN_ENV, SENTINEL)
    config = load(tmp_path)
    assert SENTINEL not in repr(config)
    assert SENTINEL not in str(config.admin)
    assert config.admin.read() == SENTINEL


def test_o12_a_credential_file_is_read_relative_to_the_configuration(tmp_path):
    """O12: a file source is resolved next to platform.toml and its contents are never kept in the configuration."""
    (tmp_path / "admin.secret").write_text(f"{SENTINEL}\n", encoding="utf-8")
    (tmp_path / "admin.secret").chmod(0o600)
    config = load(tmp_path, lambda d: d["credentials"].update(admin={"file": "admin.secret"}))
    assert config.admin.kind == "file"
    assert SENTINEL not in repr(config)
    assert config.admin.read() == SENTINEL


def test_o12_a_missing_credential_fails_at_load_naming_the_source_only(tmp_path, monkeypatch):
    """O12: a variable that is not set fails at load, not at the first request, and the message names the variable."""
    monkeypatch.delenv(ADMIN_ENV, raising=False)
    with pytest.raises(ConfigError, match=r"credentials\.admin.*environment variable.*not set") as caught:
        load(tmp_path)
    assert ADMIN_ENV not in str(caught.value)
    assert load(tmp_path, require_secrets=False).admin.ref == ADMIN_ENV


def _directory_env_that_is_a_path(data):
    """Replace the directory with an environment variable name that is really a path."""
    del data["credentials"]["users"]["directory"]
    data["credentials"]["users"]["directory_env"] = "a path/not a name"


BAD_EDITS = {
    "unknown key": (lambda d: d["space"].update(extra=1), "extra"),
    "url with credentials": (lambda d: d["zone"].update(url=f"https://user:{SENTINEL}@zone.test"), "url"),
    "url with a path": (lambda d: d["zone"].update(url="https://zone.test/api"), "url"),
    "url without a scheme": (lambda d: d["zone"].update(url="zone.test"), "url"),
    "provider without url": (lambda d: d["providers"]["alpha"].pop("url"), "url"),
    "provider without id": (lambda d: d["providers"]["alpha"].pop("id"), "id"),
    "two providers with one id": (lambda d: d["providers"]["beta"].update(id=d["providers"]["alpha"]["id"]), "id"),
    "no providers": (lambda d: d.update(providers={}), "providers"),
    "support names an undeclared provider": (lambda d: d["support"].update(providers=["alpha", "gamma"]), "providers"),
    "local outside the support set": (lambda d: d["support"].update(local="beta"), "local"),
    "duplicate support entry": (lambda d: d["support"].update(providers=["alpha", "alpha"]), "providers"),
    "space outside its prefixes": (lambda d: d["space"].update(name="other-data"), "name"),
    "no prefixes": (lambda d: d["space"].update(prefixes=[]), "prefixes"),
    "space without id": (lambda d: d["space"].pop("id"), "id"),
    "credential names a value": (lambda d: d["credentials"].update(admin={"env": f"two words {SENTINEL}"}), "env"),
    "credential with both sources": (lambda d: d["credentials"]["admin"].update(file="x"), "exactly one"),
    "same principal id on both planes": (
        lambda d: d["principals"]["toolserver"].update(user_id=d["principals"]["data_path"]["user_id"]),
        "toolserver",
    ),
    "same login on both planes": (
        lambda d: d["principals"]["toolserver"].update(username=d["principals"]["data_path"]["username"]),
        "toolserver",
    ),
    "principal with a path for a name": (lambda d: d["principals"]["service"].update(username="../x"), "username"),
    "principal without an id": (lambda d: d["principals"]["service"].pop("user_id"), "user_id"),
    "user credentials with both sources": (
        lambda d: d["credentials"]["users"].update(directory_env="VAR"),
        "exactly one",
    ),
    "user credentials with a path in the pattern": (
        lambda d: d["credentials"]["users"].update(pattern="../{user}"),
        "pattern",
    ),
    "user credentials with no user in the pattern": (
        lambda d: d["credentials"]["users"].update(pattern="x.pass"),
        "pattern",
    ),
    "user credentials naming a value": (_directory_env_that_is_a_path, "directory_env"),
    "unknown right": (
        lambda d: d["toolserver"]["conditions"]["read"].update(rights=[*BASELINE, "space_delete"]),
        "rights",
    ),
    "rights without view": (
        lambda d: d["toolserver"]["conditions"]["read"].update(rights=["space_read_data"]),
        "rights",
    ),
    "empty allow-list": (lambda d: d["toolserver"]["conditions"]["read"].update(allow=[]), "allow"),
    "missing manifest": (lambda d: d["toolserver"]["conditions"]["read"].update(manifest="nope.json"), "manifest"),
    "clone without commit": (lambda d: d["toolserver"].update(clone="."), "commit"),
    "margin below one": (lambda d: d.update(tokens={"margin_s": 0}), "margin_s"),
    "headroom above the excess": (
        lambda d: d.update(tokens={"max_excess_s": 10, "mint_headroom_s": 20}),
        "mint_headroom_s",
    ),
}


@pytest.mark.parametrize("name", sorted(BAD_EDITS))
def test_o12_a_bad_file_names_the_file_and_the_key_and_echoes_no_value(name, tmp_path, monkeypatch):
    """O12: every validation failure names platform.toml and the key, and never prints a value from the file."""
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")
    edit, key = BAD_EDITS[name]
    with pytest.raises(ConfigError) as caught:
        load(tmp_path, edit)
    message = str(caught.value)
    assert "platform.toml" in message
    assert key in message
    for private in (SENTINEL, "zone.test", "alpha.test", "admin-credential-value"):
        assert private not in message


def test_o12_the_tool_server_section_is_optional_and_a_missing_condition_is_refused_without_its_name(
    tmp_path, monkeypatch
):
    """O12: a platform without a tool server loads; asking it for a condition names the file and the table, not the
    condition, so a study's condition names never reach a message."""
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")
    config = load(tmp_path, lambda d: d.pop("toolserver"))
    assert config.toolserver is None
    with pytest.raises(ConfigError, match=r"platform\.toml.*\[toolserver\.conditions\]") as caught:
        config.condition("read")
    assert "read" not in str(caught.value).replace(str(tmp_path), "")


def test_a_write_right_is_one_of_the_closed_set():
    assert declares_write(MANAGE)
    assert not declares_write(BASELINE)
    assert declares_write(["space_schedule_replication"])


def test_o12_a_fixed_principal_without_a_credential_file_fails_at_load_naming_no_value(tmp_path, monkeypatch):
    """O12: each fixed principal needs a credential file at load; the message names the source, not a value."""
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")
    write_platform(tmp_path)
    (tmp_path / "credentials" / "login-svc.pass").unlink()
    with pytest.raises(ConfigError, match=r"\[principals\].*no credential file") as caught:
        load_platform(tmp_path / "platform.toml")
    assert "login-svc" not in str(caught.value)
    assert load_platform(tmp_path / "platform.toml", require_secrets=False).principals.service.user_id == "u-svc"


def test_o12_a_credential_file_that_others_can_read_is_refused_at_load(tmp_path, monkeypatch):
    """O12: as the earlier code refused it, a password file open to group or others fails the load."""
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")
    write_platform(tmp_path)
    (tmp_path / "credentials" / "login-data.pass").chmod(0o644)
    with pytest.raises(ConfigError, match="mode 0600"):
        load_platform(tmp_path / "platform.toml")


def test_o12_principals_need_user_credentials_to_be_declared(tmp_path, monkeypatch):
    """O12: a fixed principal with nowhere to find its password is a configuration error, not a late one."""
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")
    with pytest.raises(ConfigError, match="users"):
        load(tmp_path, lambda d: d["credentials"].pop("users"))


def test_o12_the_user_credentials_directory_may_be_named_by_an_environment_variable(tmp_path, monkeypatch):
    """O12: the directory is a path in the file or the name of a variable that holds it, never the value."""
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")
    monkeypatch.setenv("PLATFORM_TEST_USERS_DIR", str(tmp_path / "credentials"))

    def use_variable(data):
        del data["credentials"]["users"]["directory"]
        data["credentials"]["users"]["directory_env"] = "PLATFORM_TEST_USERS_DIR"

    config = load(tmp_path, use_variable)
    assert (config.users.kind, config.users.ref) == ("env", "PLATFORM_TEST_USERS_DIR")
    assert str(tmp_path) not in repr(config.users)
    monkeypatch.delenv("PLATFORM_TEST_USERS_DIR")
    with pytest.raises(ConfigError, match=r"directory_env.*not set") as caught:
        load(tmp_path, use_variable)
    assert "PLATFORM_TEST_USERS_DIR" not in str(caught.value)
