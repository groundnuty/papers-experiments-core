"""O09: every request that can change state leaves through one wrapper, which refuses a space outside the prefixes."""

import ast
from pathlib import Path

import pytest
from platform_support import DATA, SPACE_NAME, SPACE_PREFIX

from expcore_onedata.client import AUTH_HEADER, OnezoneClient, OutOfScope, PlatformError
from expcore_onedata.credentials import Principal

SRC = Path(__file__).resolve().parents[2] / "src" / "expcore_onedata"


def mutations_sent(fake):
    """The requests of the fake that are not reads."""
    return [r for r in fake.requests if r.method != "GET"]


def test_o09_a_caller_cannot_assert_the_name_of_the_space_it_mutates(client, fake):
    """O09: `space_name` is gone, so a name a caller asserts for another space's id cannot pass the gate; the call is
    refused as a call, and nothing at all is sent."""
    fake.other_spaces["production-id"] = "production-data"
    with pytest.raises(TypeError):
        client.mutate("DELETE", "/spaces/production-id", space_name=SPACE_PREFIX + "ok")
    assert fake.requests == []


def test_o09_a_mutation_on_a_space_whose_resolved_name_is_outside_the_prefixes_is_never_sent(client, fake):
    """O09: the production space's id resolves to its real name, which is outside the prefixes, so the DELETE is not
    sent. Only the read that resolved the id leaves; the message holds neither the id nor the name."""
    fake.other_spaces["production-id"] = "production-data"
    with pytest.raises(OutOfScope) as caught:
        client.mutate("DELETE", "/spaces/production-id")
    assert [(r.method, r.path) for r in fake.requests] == [("GET", "/api/v3/onezone/spaces/production-id")]
    assert "production" not in str(caught.value)


@pytest.mark.parametrize(
    "name", ["other-data", "STUDY-A-data", "x" + SPACE_PREFIX, SPACE_PREFIX[:-1], " " + SPACE_PREFIX]
)
def test_o09_only_a_resolved_name_that_starts_with_a_prefix_passes(name, client, fake):
    """O09: a name that contains the prefix, differs in case or only begins like it does not pass."""
    fake.other_spaces["sp-other"] = name
    with pytest.raises(OutOfScope):
        client.mutate("DELETE", "/spaces/sp-other")
    assert mutations_sent(fake) == []


@pytest.mark.parametrize("answer", [{}, {"name": ""}, {"name": 7}, {"name": None}, [], "text"])
def test_o09_a_space_whose_read_gives_no_name_is_refused(answer, client, fake, monkeypatch):
    """O09: an answer without a usable name resolves to no space, so the mutation is refused, not sent."""
    import httpx

    original = fake.respond
    monkeypatch.setattr(
        fake,
        "respond",
        lambda request: (
            httpx.Response(200, json=answer) if request.url.path.endswith("/spaces/sp-x") else original(request)
        ),
    )
    client = OnezoneClient(client._config, http=httpx.Client(transport=httpx.MockTransport(fake.respond)))
    with pytest.raises(OutOfScope, match="holds no name"):
        client.mutate("DELETE", "/spaces/sp-x")
    assert mutations_sent(fake) == []


def test_o09_a_space_onezone_does_not_know_is_refused_and_a_failed_read_sends_no_mutation(client, fake):
    """O09: a 404 on the id is a target that resolves to no space; any other failure of the read propagates, and in
    both the mutation itself is not sent."""
    with pytest.raises(OutOfScope, match="unknown to Onezone"):
        client.mutate("DELETE", "/spaces/sp-nobody")
    fake.status_override = 503
    with pytest.raises(PlatformError) as caught:
        client.mutate("DELETE", "/spaces/spmock0001")
    assert not isinstance(caught.value, OutOfScope)
    assert caught.value.status == 503
    assert mutations_sent(fake) == []


def test_o09_a_mutation_on_a_space_inside_the_prefixes_is_sent_with_the_admin_credential(client, fake):
    """O09: a space whose resolved name starts with a configured prefix goes through, as the method and path given."""
    fake.other_spaces["sp-run-1"] = SPACE_PREFIX + "run-1"
    with pytest.raises(PlatformError) as caught:  # the fake has no such route; reaching it is the point
        client.mutate("PUT", "/spaces/sp-run-1/anything", json={"a": 1})
    assert not isinstance(caught.value, OutOfScope)
    assert caught.value.status == 404
    sent = fake.requests[-1]
    assert (sent.method, sent.path, sent.token) == ("PUT", "/api/v3/onezone/spaces/sp-run-1/anything", fake.ADMIN)


def test_o09_the_study_space_resolves_to_its_declared_name(client, fake):
    """O09: the name read for the study space's id is the one the federation gives it."""
    assert client.space_name("spmock0001") == SPACE_NAME


def test_o09_a_space_renamed_out_of_scope_after_an_earlier_mutation_is_refused(client, fake):
    """O09: the name that authorised one mutation authorises nothing later. The space was renamed out of the study's
    prefixes between two mutations; the second re-reads the name, finds it out of scope and sends nothing."""
    fake.other_spaces["sp-run-1"] = SPACE_PREFIX + "run-1"
    with pytest.raises(PlatformError) as first:  # the fake has no such route; reaching it is the point
        client.mutate("POST", "/spaces/sp-run-1/a")
    assert not isinstance(first.value, OutOfScope)
    fake.other_spaces["sp-run-1"] = "renamed-elsewhere"
    sent_before = len(mutations_sent(fake))
    with pytest.raises(OutOfScope, match="not one of the configured prefixes"):
        client.mutate("POST", "/spaces/sp-run-1/b")
    assert len(mutations_sent(fake)) == sent_before
    assert "renamed" not in repr(client)


def test_o09_every_mutation_reads_the_name_again_right_before_it_is_sent(client, fake):
    """O09: one read per mutation, each just before its request, whether or not the name changed; a space renamed
    back into scope is allowed again, and a name read for display is not what authorises a mutation."""
    fake.other_spaces["sp-run-1"] = SPACE_PREFIX + "run-1"
    for path in ("/spaces/sp-run-1/a", "/spaces/sp-run-1/b", "/spaces/sp-run-1"):
        with pytest.raises(PlatformError):
            client.mutate("POST", path)
    sequence = [(r.method, r.path.rsplit("/", 1)[-1]) for r in fake.requests]
    assert sequence == [
        ("GET", "sp-run-1"),
        ("POST", "a"),
        ("GET", "sp-run-1"),
        ("POST", "b"),
        ("GET", "sp-run-1"),
        ("POST", "sp-run-1"),
    ]
    assert client.space_name("sp-run-1") == SPACE_PREFIX + "run-1"
    fake.other_spaces["sp-run-1"] = "elsewhere"
    assert client.space_name("sp-run-1") == "elsewhere"  # nothing is kept from the earlier read
    with pytest.raises(OutOfScope):
        client.mutate("POST", "/spaces/sp-run-1/c")
    fake.other_spaces["sp-run-1"] = SPACE_PREFIX + "run-2"
    with pytest.raises(PlatformError) as again:
        client.mutate("POST", "/spaces/sp-run-1/d")
    assert not isinstance(again.value, OutOfScope)


@pytest.mark.parametrize(
    "path",
    [
        "/spaces",
        "/spaces/",
        "/spaces//x",
        "/user/tokens/temporary",
        "/groups/g1",
        "/data/file123",
        "/lookup-file-id/study-a-data/dir",
        "/replicas-id/file123",
        "/spaces/spmock0001%2F..%2Fx",
        "/spaces/spmock0001/%2e%2e/x",
        "/spaces/spmock0001?x=1",
        "/spaces/spmock0001#x",
        "/spaces/./x",
        "/spaces/sp mock/x",
        "/x/spaces/spmock0001",
        "spaces/spmock0001",
    ],
)
@pytest.mark.parametrize("provider", [None, "alpha"])
def test_o09_a_target_that_is_no_space_is_refused_with_nothing_sent(path, provider, client, fake):
    """O09: every mutation target must resolve to a space; a file id, a path under no space, a path that hides another
    one behind an escape and a path outside the API are refused, and not even a read is made."""
    with pytest.raises(OutOfScope):
        client.mutate("DELETE", path, provider=provider)
    assert fake.requests == []


@pytest.mark.parametrize("method", ["GET", "HEAD", "get", "TRACE", ""])
def test_o09_mutate_takes_only_mutating_methods(method, client, fake):
    """O09: a read cannot be sent through the mutation call, nor a mutation through the read calls."""
    with pytest.raises(OutOfScope):
        client.mutate(method, "/spaces/spmock0001")
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
        client.mutate("POST", path)
    with pytest.raises(OutOfScope):
        client.get(path)
    assert fake.requests == []


def test_o09_an_undeclared_provider_is_refused(client, fake):
    """O09: a request can name only a provider declared in the configuration."""
    with pytest.raises(OutOfScope):
        client.get("/spaces", provider="gamma")
    with pytest.raises(OutOfScope):
        client.mutate("POST", "/spaces/spmock0001", provider="gamma")
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
        # The data gate is a separate program that runs in its own container and passes read calls only (O16); it
        # imports nothing of this package, which test_gate checks. It is the one file besides the client with HTTP.
        if path.name == "client.py" or path.relative_to(SRC).as_posix() == "sandbox/gate.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            names += [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
            if any(name.split(".")[0] in {"httpx", "requests", "urllib3", "http", "socket"} for name in names):
                offenders.append(path.name)
    assert not offenders, f"HTTP outside client.py: {offenders}"
