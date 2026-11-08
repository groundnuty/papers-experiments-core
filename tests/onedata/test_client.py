"""O09: every request that can change state leaves through one wrapper, which refuses a space outside the prefixes."""

import ast
from pathlib import Path

import pytest
from platform_support import DATA, SPACE_PREFIX

from expcore_onedata.client import AUTH_HEADER, OnezoneClient, OutOfScope, PlatformError
from expcore_onedata.credentials import Principal

SRC = Path(__file__).resolve().parents[2] / "src" / "expcore_onedata"


def test_o09_a_mutation_on_a_space_outside_the_prefixes_sends_nothing(client, fake):
    """O09: a space whose name does not start with a configured prefix is refused before any request is sent."""
    for name in ("other-data", "", "STUDY-A-data", "x" + SPACE_PREFIX):
        with pytest.raises(OutOfScope):
            client.mutate("DELETE", "/spaces/spmock0001", space_name=name)
    assert fake.requests == []


def test_o09_a_mutation_on_a_space_inside_the_prefixes_is_sent_with_the_admin_credential(client, fake):
    """O09: a space that starts with a configured prefix goes through, as the method and path the caller gave."""
    with pytest.raises(PlatformError) as caught:  # the fake has no such route; reaching it is the point
        client.mutate("PUT", "/spaces/spmock0001/anything", space_name=SPACE_PREFIX + "run-1", json={"a": 1})
    assert caught.value.status == 404
    sent = fake.requests[-1]
    assert (sent.method, sent.path, sent.token) == ("PUT", "/api/v3/onezone/spaces/spmock0001/anything", fake.ADMIN)


@pytest.mark.parametrize("method", ["GET", "HEAD", "get", "TRACE", ""])
def test_o09_mutate_takes_only_mutating_methods(method, client, fake):
    """O09: a read cannot be sent through the mutation call, nor a mutation through the read calls."""
    with pytest.raises(OutOfScope):
        client.mutate(method, "/spaces/spmock0001", space_name=SPACE_PREFIX + "x")
    assert fake.requests == []


@pytest.mark.parametrize(
    "path", ["/spaces/x", "/user/tokens/temporary", "/users/x/tokens/temporary", "/tokens/revoke", "/anything"]
)
def test_o09_query_takes_only_the_listed_read_only_posts(path, client, fake):
    """O09: a POST that is not on the short list of reading endpoints is refused before it is sent."""
    with pytest.raises(OutOfScope):
        client.query(path, {"token": "x"})
    assert fake.requests == []


@pytest.mark.parametrize(
    "path", ["spaces/x", "//other.test/x", "/a/../b", "/a/..", "http://other.test/x", "/a\\b", "/a://b"]
)
def test_o09_a_path_cannot_name_another_host_or_climb_out(path, client, fake):
    """O09: only a plain absolute path of the API is sent, so a caller cannot redirect a request."""
    with pytest.raises(OutOfScope):
        client.mutate("POST", path, space_name=SPACE_PREFIX + "x")
    with pytest.raises(OutOfScope):
        client.get(path)
    assert fake.requests == []


def test_o09_an_undeclared_provider_is_refused(client, fake):
    """O09: a request can name only a provider declared in the configuration."""
    with pytest.raises(OutOfScope):
        client.get("/spaces", provider="gamma")
    with pytest.raises(OutOfScope):
        client.mutate("POST", "/x", space_name=SPACE_PREFIX + "x", provider="gamma")
    assert fake.requests == []


def test_o09_a_user_mints_its_own_token_with_its_own_credential_and_no_other_way_exists(client, fake):
    """O09: the one credential call logs in as the user (HTTP Basic) at the user's own path; there is no revoke call."""
    value = client.mint_own_token(DATA, [{"type": "time", "validUntil": 1}])
    sent = fake.requests[-1]
    assert (sent.method, sent.path, sent.token) == ("POST", "/api/v3/onezone/user/tokens/temporary", None)
    assert fake.issued[value]["subject"]["id"] == DATA.user_id
    assert not hasattr(client, "revoke_temporary_tokens")
    assert not hasattr(client, "mint_temporary_token")


def test_o09_a_wrong_password_or_an_unknown_user_gets_no_token_and_the_error_has_no_secret(client, fake, tmp_path):
    """O09: a login the federation refuses is a PlatformError with the status only, never the password."""
    fake.users["login-data"] = ("another-password-9", DATA.user_id)
    with pytest.raises(PlatformError) as caught:
        client.mint_own_token(DATA, [{"type": "time", "validUntil": 1}])
    assert caught.value.status == 401
    assert "pw-data-0001" not in str(caught.value)
    assert fake.issued == {}


def test_o09_a_user_without_a_credential_file_cannot_mint(client, fake):
    """O09: no file for the user is an error that names the source and nothing about a value."""
    with pytest.raises(PlatformError, match="credential cannot be used"):
        client.mint_own_token(Principal("login-nobody", "u-nobody"), [{"type": "time", "validUntil": 1}])
    assert fake.requests == []


def test_a_failure_names_the_status_and_error_id_and_nothing_else(client, fake):
    fake.status_override = 503
    with pytest.raises(PlatformError) as caught:
        client.get("/spaces")
    message = str(caught.value)
    assert caught.value.status == 503
    assert caught.value.code == "internalServerError"
    assert "zone.test" not in message
    assert fake.admin_token not in message


def test_a_transport_error_is_a_platform_error_without_the_url(config, fake):
    import httpx

    def refuse(request):
        raise httpx.ConnectError("no route to zone.test with a secret in it")

    client = OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(refuse)))
    with pytest.raises(PlatformError) as caught:
        client.get("/spaces")
    assert "zone.test" not in str(caught.value)
    assert "secret" not in str(caught.value)
    assert str(caught.value) == "zone: GET failed (ConnectError)"


def test_the_client_does_not_show_its_credential(client, fake):
    assert fake.admin_token not in repr(client)
    assert AUTH_HEADER == "X-Auth-Token"


def test_o09_no_other_module_of_the_platform_layer_imports_httpx():
    """O09: callers cannot bypass the wrapper, because nothing but client.py can send an HTTP request."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "client.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            names += [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
            if any(name.split(".")[0] in {"httpx", "requests", "urllib3", "http", "socket"} for name in names):
                offenders.append(path.name)
    assert not offenders, f"HTTP outside client.py: {offenders}"
