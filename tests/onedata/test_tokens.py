"""O05, O06 and R12 for the minting side: two planes, separate principals, derived lifetimes, no value in sight."""

import base64
import dataclasses
import json

import pytest
from platform_support import BASELINE, MANAGE, NOW, TIME_CAP_S
from record_support import record_run

from expcore.records.redact import REDACTED
from expcore_onedata.client import PlatformError
from expcore_onedata.config import TokenSettings
from expcore_onedata.tokencheck import TokenRefused
from expcore_onedata.tokens import Plane, Secret, TokenIssuer, data_path_spec, token_window, tool_server_spec

SETTINGS = TokenSettings()  # margin 900, excess 600, headroom 300


def types(fake, token):
    return [c["type"] for c in fake.issued[token.reveal()]["caveats"]]


def test_o05_the_data_path_token_is_read_only_and_scoped_to_the_space(issuer, fake):
    """O05: minted with read-only and path caveats, examined before it is returned, for the data-path user."""
    token = issuer.mint(data_path_spec("spmock0001"), "u-data", time_cap_s=TIME_CAP_S)
    assert (token.plane, token.subject) == (Plane.DATA_PATH, "u-data")
    assert types(fake, token) == ["time", "data.readonly", "interface", "data.path"]
    assert [r.path for r in fake.requests if r.method == "POST"][-1].endswith("/tokens/examine")


def test_o05_a_server_that_drops_the_readonly_caveat_cannot_hand_out_a_token(issuer, fake):
    """O05: a token without the read-only caveat is refused and revoked; the caller never sees it."""
    fake.mint_hook = lambda user, caveats: [c for c in caveats if c["type"] != "data.readonly"]
    with pytest.raises(TokenRefused, match=r"data\.readonly"):
        issuer.mint(data_path_spec("spmock0001"), "u-data", time_cap_s=TIME_CAP_S)
    assert fake.revoked_users == ["u-data"]
    assert fake.issued == {}


def test_o05_a_server_that_adds_a_caveat_or_widens_the_path_is_refused(issuer, fake):
    """O05: an extra caveat is as bad as a missing one."""
    fake.mint_hook = lambda user, caveats: [*caveats, {"type": "consumer", "whitelist": ["x"]}]
    with pytest.raises(TokenRefused, match="not asked for"):
        issuer.mint(data_path_spec("spmock0001"), "u-data", time_cap_s=TIME_CAP_S)
    root = base64.b64encode(b"/").decode()
    fake.mint_hook = lambda user, caveats: [
        {"type": "data.path", "whitelist": [root]} if c["type"] == "data.path" else c for c in caveats
    ]
    with pytest.raises(TokenRefused, match="study space"):
        issuer.mint(data_path_spec("spmock0001"), "u-data", time_cap_s=TIME_CAP_S)


def test_o05_an_examination_that_fails_refuses_the_token(issuer, fake):
    """O05: when Onezone cannot examine the token, it is not used."""
    fake.examine_hook = lambda answer: b"<html>gateway error</html>"
    with pytest.raises(TokenRefused, match="could not be examined"):
        issuer.mint(data_path_spec("spmock0001"), "u-data", time_cap_s=TIME_CAP_S)
    assert fake.issued == {}


def test_o05_a_principal_with_more_than_view_and_read_gets_no_token(issuer, fake):
    """O05: the principal's privileges are read back, direct and effective, before anything is minted."""
    fake.grant("u-data", BASELINE, effective=[*BASELINE, "space_write_data"])
    with pytest.raises(TokenRefused, match=r"effective.*space_write_data"):
        issuer.mint(data_path_spec("spmock0001"), "u-data", time_cap_s=TIME_CAP_S)
    fake.grant("u-data", ["space_view"])
    with pytest.raises(TokenRefused, match="lacks space_read_data"):
        issuer.mint(data_path_spec("spmock0001"), "u-data", time_cap_s=TIME_CAP_S)
    assert fake.issued == {}


def test_o05_a_principal_that_cannot_be_read_gets_no_token(issuer, fake):
    """O05: unknown is not a pass, for privileges as for tokens."""
    with pytest.raises(TokenRefused, match="could not be read"):
        issuer.mint(data_path_spec("spmock0001"), "u-nobody", time_cap_s=TIME_CAP_S)
    fake.status_override = 503
    with pytest.raises(TokenRefused, match="could not be read"):
        issuer.mint(data_path_spec("spmock0001"), "u-data", time_cap_s=TIME_CAP_S)
    assert fake.issued == {}


def test_o05_a_principal_id_cannot_change_the_path_of_the_privilege_read(issuer, fake):
    """O05: the id goes into a URL path quoted, so an id with a slash or a query reads nothing but itself."""
    for user in ("u-data/../x", "u-data?a=b", "u-data#x"):
        with pytest.raises(TokenRefused, match="could not be read"):
            issuer.mint(data_path_spec("spmock0001"), user, time_cap_s=TIME_CAP_S)
    assert fake.requests
    assert all(r.query == {} for r in fake.requests)  # a "?" in the id stayed in the path
    assert fake.issued == {}


def test_o05_the_tool_server_token_has_the_conditions_caveats_no_more_and_no_less(issuer, fake, config):
    """O05: read-only when the condition declares no write right, without the read-only caveat when it does."""
    reading = issuer.mint(tool_server_spec("spmock0001", config.condition("read")), "u-tools", time_cap_s=TIME_CAP_S)
    assert types(fake, reading) == ["time", "data.readonly", "interface", "data.path"]
    fake.grant("u-tools", MANAGE)
    managing = issuer.mint(tool_server_spec("spmock0001", config.condition("manage")), "u-tools", time_cap_s=TIME_CAP_S)
    assert types(fake, managing) == ["time", "interface", "data.path"]


def test_o05_the_tool_server_principal_must_hold_exactly_the_conditions_rights(issuer, fake, config):
    """O05: effective privileges equal the declared set; a missing write right and an undeclared one both refuse."""
    spec = tool_server_spec("spmock0001", config.condition("manage"))
    with pytest.raises(TokenRefused, match="lacks space_manage_qos"):  # the user has only view and read
        issuer.mint(spec, "u-tools", time_cap_s=TIME_CAP_S)
    fake.grant("u-tools", [*MANAGE, "space_schedule_replication"])
    with pytest.raises(TokenRefused, match="has also space_schedule_replication"):
        issuer.mint(spec, "u-tools", time_cap_s=TIME_CAP_S)
    reading = tool_server_spec("spmock0001", config.condition("read"))
    fake.grant("u-tools", MANAGE)
    with pytest.raises(
        TokenRefused, match="has also space_manage_qos"
    ):  # a writing user cannot run a read-only condition
        issuer.mint(reading, "u-tools", time_cap_s=TIME_CAP_S)
    assert fake.issued == {}


def test_o05_the_two_planes_get_separate_principals_and_tokens(issuer, fake):
    """O05: the data path and the tool server never share a user or a token."""
    fake.grant("u-tools", MANAGE)
    both = issuer.mint_run_tokens(data_user="u-data", time_cap_s=TIME_CAP_S, tool_server=("u-tools", "manage"))
    assert both.tool_server is not None
    assert both.data_path.subject != both.tool_server.subject
    assert both.data_path.token_id != both.tool_server.token_id
    assert both.data_path.reveal() != both.tool_server.reveal()
    assert types(fake, both.data_path) != types(fake, both.tool_server)


def test_o05_one_user_for_both_planes_is_refused_before_anything_is_minted(issuer, fake):
    """O05: a shared principal is refused up front."""
    with pytest.raises(TokenRefused, match="share a principal"):
        issuer.mint_run_tokens(data_user="u-data", time_cap_s=TIME_CAP_S, tool_server=("u-data", "read"))
    assert fake.requests == []
    assert fake.issued == {}


def test_o05_two_tokens_that_examine_as_one_are_refused_and_both_users_are_revoked(issuer, fake):
    """O05: if the federation reports the same token id for both planes, nothing is handed out."""
    fake.examine_hook = lambda answer: {**answer, "id": "same-id"}
    with pytest.raises(TokenRefused, match="share a token"):
        issuer.mint_run_tokens(data_user="u-data", time_cap_s=TIME_CAP_S, tool_server=("u-tools", "read"))
    assert set(fake.revoked_users) >= {"u-data"}
    assert fake.issued == {}


def test_o05_a_failure_after_the_data_token_was_minted_revokes_it(issuer, fake):
    """O05: a refused second plane leaves no valid first token behind."""
    with pytest.raises(TokenRefused, match="lacks space_manage_qos"):
        issuer.mint_run_tokens(data_user="u-data", time_cap_s=TIME_CAP_S, tool_server=("u-tools", "manage"))
    assert "u-data" in fake.revoked_users
    assert fake.issued == {}


def test_o05_a_refusal_says_when_the_refused_token_could_not_be_revoked(issuer, fake):
    """O05: a revocation that fails is reported with the refusal, not swallowed."""
    fake.mint_hook = lambda user, caveats: caveats[:1]
    original = fake._admin_route

    def failing(method, route, sent):
        return fake._error(500, "internalServerError") if method == "DELETE" else original(method, route, sent)

    fake._admin_route = failing
    with pytest.raises(TokenRefused) as caught:
        issuer.mint(data_path_spec("spmock0001"), "u-data", time_cap_s=TIME_CAP_S)
    assert any("could not be revoked" in note for note in caught.value.__notes__)


def test_o06_the_lifetime_is_the_time_cap_plus_a_margin():
    """O06: the least a token may have, the most, and the lifetime asked for, all derived from the cap."""
    window = token_window(7200, SETTINGS)
    assert (window.min_ttl_s, window.max_ttl_s, window.mint_ttl_s) == (8100, 8700, 8400)
    assert token_window(0.5, SETTINGS).min_ttl_s == 901  # rounded up, never down


@pytest.mark.parametrize("cap", [0, -1, float("nan"), float("inf"), True, "600", None])
def test_o06_a_cap_that_is_not_a_positive_number_is_refused_before_minting(cap, issuer, fake):
    """O06: the lifetime is checked before minting, so a bad cap sends no request."""
    with pytest.raises(TokenRefused, match="time cap"):
        issuer.mint(data_path_spec("spmock0001"), "u-data", time_cap_s=cap)
    assert fake.requests == []


def test_o06_a_lifetime_the_platform_cannot_grant_is_refused_before_minting(config, client, fake):
    """O06: with a platform limit, a run that needs a longer token is refused before anything is sent."""
    limited = dataclasses.replace(config, tokens=TokenSettings(max_ttl_s=1000))
    with pytest.raises(TokenRefused, match="does not grant"):
        TokenIssuer(client, limited, clock=lambda: NOW).mint(
            data_path_spec("spmock0001"), "u-data", time_cap_s=TIME_CAP_S
        )
    assert fake.requests == []


def test_o06_a_token_shorter_than_the_cap_plus_the_margin_is_refused(issuer, fake):
    """O06: if the federation grants less than was asked, the examination refuses the token."""

    def shorter(user, caveats):
        return [{**c, "validUntil": c["validUntil"] - 700} if c["type"] == "time" else c for c in caveats]

    fake.mint_hook = shorter
    with pytest.raises(TokenRefused, match="shorter"):
        issuer.mint(data_path_spec("spmock0001"), "u-data", time_cap_s=TIME_CAP_S)
    fake.mint_hook = lambda user, caveats: [
        {**c, "validUntil": c["validUntil"] + 5000} if c["type"] == "time" else c for c in caveats
    ]
    with pytest.raises(TokenRefused, match="longer"):
        issuer.mint(data_path_spec("spmock0001"), "u-data", time_cap_s=TIME_CAP_S)


def test_o06_the_minted_token_lives_from_the_cap_plus_margin_to_the_headroom(issuer, fake):
    """O06: the expiry in the token is now plus the derived lifetime, and the result reports it."""
    token = issuer.mint(data_path_spec("spmock0001"), "u-data", time_cap_s=TIME_CAP_S)
    window = token_window(TIME_CAP_S, SETTINGS)
    expiry = fake.issued[token.reveal()]["caveats"][0]["validUntil"]
    assert expiry == int(NOW) + window.mint_ttl_s == token.expires_at
    assert window.min_ttl_s <= expiry - NOW <= window.max_ttl_s


def test_r12_a_minted_token_is_redacted_from_a_record_that_contains_it(issuer, fake):
    """R12: the platform layer registers what it mints, so a tool output that quotes the token is redacted."""
    token = issuer.mint(data_path_spec("spmock0001"), "u-data", time_cap_s=TIME_CAP_S)
    value = token.reveal()
    with pytest.warns(RuntimeWarning, match="redacted secrets"):
        record = record_run([[("show", {})], "done"], {"show": lambda args: f"the credential is {value} for now"})
    text = record.dumps()
    assert value not in text
    assert REDACTED in text
    assert value not in json.dumps(record.to_json())


def test_r12_the_value_is_in_no_repr_str_or_exception(issuer, fake, client):
    """R12: neither the token, its secret, the issuer nor an error that follows shows the value."""
    token = issuer.mint(data_path_spec("spmock0001"), "u-data", time_cap_s=TIME_CAP_S)
    value = token.reveal()
    for shown in (
        repr(token),
        str(token),
        repr(token.secret),
        f"{token.secret}",
        f"{token!s}",
        repr(issuer),
        repr(client),
    ):
        assert value not in shown
    fake.status_override = 500
    with pytest.raises(PlatformError) as caught:
        client.get("/spaces", token=value)
    assert value not in str(caught.value) + repr(caught.value)


def test_a_secret_shows_nothing_and_reveals_on_request():
    secret = Secret("abcd-1234")
    assert "abcd" not in repr(secret)
    assert "abcd" not in str(secret)
    assert secret.reveal() == "abcd-1234"
