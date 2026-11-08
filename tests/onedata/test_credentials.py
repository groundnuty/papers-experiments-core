"""R12: a credential is registered as a secret the moment it is read, and no message, repr or record can hold it."""

import json
import os

import pytest
from record_support import clear_registry, record_run

from expcore.config.loading import ConfigError
from expcore.records import redact
from expcore.records.redact import REDACTED
from expcore_onedata.credentials import Principal, SecretSource, UserCredentials

PASSWORD = "pw-credentials-test-7391"


@pytest.fixture
def directory(tmp_path):
    folder = tmp_path / "credentials"
    folder.mkdir()
    file = folder / "alice.pass"
    file.write_text(PASSWORD + "\n", encoding="utf-8")
    os.chmod(file, 0o600)
    return folder


def registered() -> set[str]:
    with redact._lock:
        return set(redact._registered)


def test_r12_a_users_password_is_registered_when_it_is_read(directory):
    """R12: reading the credential registers it, so a record written afterwards cannot hold it."""
    clear_registry()
    credentials = UserCredentials("dir", str(directory))
    assert PASSWORD not in registered()
    assert credentials.read("alice") == PASSWORD
    assert PASSWORD in registered()


def test_r12_a_record_that_quotes_the_password_is_redacted(directory):
    """R12: a tool output that echoes the credential is redacted from the record."""
    value = UserCredentials("dir", str(directory)).read("alice")
    with pytest.warns(RuntimeWarning, match="redacted secrets"):
        record = record_run([[("show", {})], "done"], {"show": lambda args: f"the password is {value}"})
    assert value not in record.dumps()
    assert REDACTED in json.dumps(record.to_json())


def test_r12_an_admin_secret_is_registered_when_it_is_read(tmp_path):
    """R12: the admin token's source registers it on reading, whether it is a variable or a file."""
    clear_registry()
    token_file = tmp_path / "admin.token"
    token_file.write_text("admin-token-value-5512\n", encoding="utf-8")
    assert SecretSource("file", str(token_file)).read() == "admin-token-value-5512"
    assert SecretSource("env", "ADMIN_X").read({"ADMIN_X": "admin-env-value-5513"}) == "admin-env-value-5513"
    assert {"admin-token-value-5512", "admin-env-value-5513"} <= registered()


def test_r12_nothing_that_describes_a_source_holds_a_value(directory):
    """R12: reprs and error messages name the source and never the secret."""
    credentials = UserCredentials("dir", str(directory))
    credentials.read("alice")
    assert PASSWORD not in repr(credentials) + credentials.describe()
    with pytest.raises(ConfigError) as caught:
        UserCredentials("dir", str(directory)).read("bob")
    assert PASSWORD not in str(caught.value)
    assert "bob" not in str(caught.value)


def test_a_credential_too_short_to_redact_or_empty_is_refused_without_its_value(directory):
    (directory / "short.pass").write_text("abc\n", encoding="utf-8")
    os.chmod(directory / "short.pass", 0o600)
    (directory / "empty.pass").write_text("\n", encoding="utf-8")
    os.chmod(directory / "empty.pass", 0o600)
    credentials = UserCredentials("dir", str(directory))
    with pytest.raises(ConfigError, match="too short"):
        credentials.read("short")
    with pytest.raises(ConfigError, match="empty"):
        credentials.read("empty")


def test_a_file_open_to_others_or_not_a_file_is_refused(directory):
    credentials = UserCredentials("dir", str(directory))
    os.chmod(directory / "alice.pass", 0o640)
    with pytest.raises(ConfigError, match="readable by others"):
        credentials.read("alice")
    (directory / "folder.pass").mkdir()
    with pytest.raises(ConfigError, match="not a regular file"):
        credentials.check("folder")


@pytest.mark.parametrize("name", ["../alice", "a/b", "", ".hidden", "a b", "alice\n"])
def test_a_user_name_that_could_leave_the_directory_is_refused(name, directory):
    with pytest.raises(ConfigError, match="not a plain name"):
        UserCredentials("dir", str(directory)).path_for(name)


def test_the_pattern_names_the_file_and_the_directory_may_come_from_the_environment(directory, tmp_path):
    (directory / "bob-secret").write_text(PASSWORD + "\n", encoding="utf-8")
    os.chmod(directory / "bob-secret", 0o600)
    by_env = UserCredentials("env", "CREDENTIALS_DIR_VAR", "{user}-secret")
    environ = {"CREDENTIALS_DIR_VAR": str(directory)}
    assert by_env.path_for("bob", environ).name == "bob-secret"
    assert by_env.read("bob", environ) == PASSWORD
    with pytest.raises(ConfigError, match="CREDENTIALS_DIR_VAR"):
        by_env.read("bob", {})
    with pytest.raises(ConfigError, match="not a directory"):
        UserCredentials("dir", str(tmp_path / "missing")).read("bob")


def test_a_principal_has_a_login_and_an_id_and_both_are_plain():
    principal = Principal("login", "uid-1")
    assert (principal.username, principal.user_id) == ("login", "uid-1")
    for bad in ("a/b", "", "../x", "a b", "x?y", "x#y"):
        with pytest.raises(ValueError, match="plain name"):
            Principal(bad, "uid-1")
        with pytest.raises(ValueError, match="plain name"):
            Principal("login", bad)
