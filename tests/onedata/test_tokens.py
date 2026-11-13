"""O05, O06 and R12 for the minting side: two planes, separate principals, derived lifetimes, no value in sight."""

import base64
import dataclasses
import json

import pytest
from record_support import record_run

from expcore.records.redact import REDACTED
from expcore_onedata.client import PlatformError
from expcore_onedata.config import TokenSettings
from expcore_onedata.credentials import Principal
from expcore_onedata.rights import WRITE_RIGHTS
from expcore_onedata.testing import ALPHA_ID, BASELINE, CEILING_S, MANAGE, NOW
from expcore_onedata.testing import DATA_PATH_USER as DATA
from expcore_onedata.testing import TOOL_SERVER_USER as TOOLS
from expcore_onedata.tokencheck import ExaminationFailed, PrivilegesMismatch, TokenRefused
from expcore_onedata.tokens import (
    DroppedToken,
    ExpiryMismatch,
    Plane,
    RevocationUnconfirmed,
    Secret,
    TokenIssuer,
    data_path_spec,
    token_window,
    tool_server_spec,
)
from expcore_onedata.tokenscope import AdminPrivileged

SETTINGS = TokenSettings()  # margin 900, excess 600, headroom 300


def types(fake, token):
    return [c["type"] for c in fake.issued[token.reveal()]["caveats"]]


def time_caveat(fake, token):
    return next(c["validUntil"] for c in fake.issued[token.reveal()]["caveats"] if c["type"] == "time")


def test_c19_a_token_is_minted_with_exactly_the_absolute_expiry_it_is_given(issuer, fake):
    """C19: a caller that journals the end of a token before minting passes the absolute expiry, and the token's time
    caveat (validUntil) and its examined expiry are exactly that, not a clock read after the privilege reads."""
    asked = int(NOW) + token_window(CEILING_S, SETTINGS).mint_ttl_s + 7
    token = issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S, valid_until=asked)
    assert time_caveat(fake, token) == asked
    assert token.expires_at == asked


def test_c19_a_token_whose_examined_expiry_is_not_the_one_it_was_given_is_refused_with_the_expiry_it_has(issuer, fake):
    """C19: the platform hands back a token that lives 50 s longer than asked for. It is refused, never returned, and
    the error carries the expiry the dropped token has, which is when it stops working."""
    asked = int(NOW) + token_window(CEILING_S, SETTINGS).mint_ttl_s
    fake.mint_hook = lambda user, caveats: [
        {**c, "validUntil": c["validUntil"] + 50} if c["type"] == "time" else c for c in caveats
    ]
    with pytest.raises(ExpiryMismatch) as refused:
        issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S, valid_until=asked)
    assert refused.value.expires_at == asked + 50
    token = issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)  # no expiry given: unchanged
    assert time_caveat(fake, token) == int(NOW) + token_window(CEILING_S, SETTINGS).mint_ttl_s + 50


def test_o05_the_data_path_token_is_read_only_and_scoped_to_the_space(issuer, fake):
    """O05: minted with read-only and path caveats, examined before it is returned, for the data-path user."""
    token = issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    assert (token.plane, token.subject) == (Plane.DATA_PATH, DATA.user_id)
    assert types(fake, token) == ["time", "data.readonly", "interface", "data.path"]
    assert [r.path for r in fake.requests if r.method == "POST"][-1].endswith("/tokens/examine")


def test_o05_a_server_that_drops_the_readonly_caveat_cannot_hand_out_a_token(issuer, fake):
    """O05: a token without the read-only caveat is refused; the caller never sees it, and nothing revokes it."""
    fake.mint_hook = lambda user, caveats: [c for c in caveats if c["type"] != "data.readonly"]
    with pytest.raises(TokenRefused, match=r"data\.readonly"):
        issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    assert not any(r.method == "DELETE" for r in fake.requests)


def test_o05_a_server_that_adds_a_caveat_or_widens_the_path_is_refused(issuer, fake):
    """O05: an extra caveat is as bad as a missing one."""
    fake.mint_hook = lambda user, caveats: [*caveats, {"type": "consumer", "whitelist": ["x"]}]
    with pytest.raises(TokenRefused, match="not asked for"):
        issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    root = base64.b64encode(b"/").decode()
    fake.mint_hook = lambda user, caveats: [
        {"type": "data.path", "whitelist": [root]} if c["type"] == "data.path" else c for c in caveats
    ]
    with pytest.raises(TokenRefused, match="study space"):
        issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)


def test_o05_an_examination_that_fails_refuses_the_token(issuer, fake):
    """O05: when Onezone cannot examine the token, it is not used."""
    fake.examine_hook = lambda answer: b"<html>gateway error</html>"
    with pytest.raises(TokenRefused, match="could not be examined"):
        issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)


def test_o05_a_principal_with_more_than_view_and_read_gets_no_token(issuer, fake):
    """O05: the principal's privileges are read back, direct and effective, before anything is minted."""
    fake.grant(DATA.user_id, BASELINE, effective=[*BASELINE, "space_write_data"])
    with pytest.raises(TokenRefused, match=r"effective.*space_write_data"):
        issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    fake.grant(DATA.user_id, ["space_view"])
    with pytest.raises(TokenRefused, match="lacks space_read_data"):
        issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    assert fake.issued == {}


def test_o05_a_principal_that_cannot_be_read_gets_no_token(issuer, fake):
    """O05: unknown is not a pass, for privileges as for tokens."""
    with pytest.raises(TokenRefused, match="could not be read"):
        issuer.mint(data_path_spec("spmock0001"), Principal(DATA.username, "u-nobody"), ceiling_s=CEILING_S)
    fake.status_override = 503
    with pytest.raises(TokenRefused, match="could not be read"):
        issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    assert fake.issued == {}


RIGHTS = {"read": BASELINE, "manage": MANAGE}  # each default condition's declared rights: read-only, and writing
BOTH_KINDS = pytest.mark.parametrize("condition", list(RIGHTS), ids=["read-only", "writing"])


def tool_spec(config, condition):
    """The tool-server spec of a default condition on the study space."""
    return tool_server_spec("spmock0001", config.condition(condition))


def test_o37_a_read_only_conditions_tool_server_token_carries_no_data_access_caveat(issuer, fake, config):
    """O37: a read-only condition's tool-server token is its expiry and the rest interface, as a writing condition's
    is. On Onedata 25.0 a data-access caveat limits a token to data access, and the 12 Nov live run saw the read-only
    token's `data.readonly` and `data.path` refuse its transfer and QoS-requirement views (#181)."""
    reading = issuer.mint(tool_spec(config, "read"), TOOLS, ceiling_s=CEILING_S)
    assert types(fake, reading) == ["time", "interface"]
    fake.grant(TOOLS.user_id, MANAGE)
    managing = issuer.mint(tool_spec(config, "manage"), TOOLS, ceiling_s=CEILING_S)
    assert types(fake, managing) == ["time", "interface"]


def test_o05_each_plane_and_condition_kind_mints_exactly_its_caveat_list(issuer, fake, config):
    """O05: the data path mints the same list it did before the tool-server tokens changed, byte for byte; a
    read-only condition and a writing one each mint their expiry and the rest interface alone (O37)."""
    expiry = int(NOW) + token_window(CEILING_S, SETTINGS).mint_ttl_s
    data_path = (
        f'[{{"type": "time", "validUntil": {expiry}}}, {{"type": "data.readonly"}}, '
        '{"type": "interface", "interface": "rest"}, {"type": "data.path", "whitelist": ["L3NwbW9jazAwMDE="]}]'
    )
    data = issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    reading = issuer.mint(tool_spec(config, "read"), TOOLS, ceiling_s=CEILING_S)
    fake.grant(TOOLS.user_id, MANAGE)
    writing = issuer.mint(tool_spec(config, "manage"), TOOLS, ceiling_s=CEILING_S)
    assert json.dumps(fake.issued[data.reveal()]["caveats"]) == data_path
    tool_server = [{"type": "time", "validUntil": expiry}, {"type": "interface", "interface": "rest"}]
    assert fake.issued[reading.reveal()]["caveats"] == fake.issued[writing.reveal()]["caveats"] == tool_server


@pytest.mark.parametrize(
    "added",
    [
        {"type": "data.path", "whitelist": [base64.b64encode(b"/spmock0001").decode()]},
        {"type": "data.readonly"},
        {"type": "data.objectid", "whitelist": ["000000000000"]},
    ],
    ids=lambda caveat: caveat["type"],
)
@BOTH_KINDS
def test_o05_a_tool_server_token_that_comes_back_with_a_data_access_caveat_is_dropped(
    added, condition, issuer, fake, config
):
    """O05: a server that adds a data-access caveat to a tool-server token, a read-only condition's or a writing one's,
    hands out a token whose transfers and QoS requirements Onedata 25.0 refuses; it is dropped, not returned."""
    fake.grant(TOOLS.user_id, RIGHTS[condition])
    fake.mint_hook = lambda user, caveats: [*caveats, added]
    with pytest.raises(TokenRefused, match="data-access caveat"):
        issuer.mint(tool_spec(config, condition), TOOLS, ceiling_s=CEILING_S)


WRITES = [*WRITE_RIGHTS, "space_write_data"]  # every write right a condition can declare, and the data write


@pytest.mark.parametrize("right", WRITES)
@pytest.mark.parametrize("held", ["direct", "effective"])
def test_o37_a_read_only_conditions_user_who_holds_any_write_right_gets_no_token(right, held, issuer, fake, config):
    """O37: with no data-access caveat, what keeps a read-only condition's token from writing is its user, who must
    hold exactly the condition's rights: one write right more, held directly or only through a group, and nothing is
    minted. `space_write_data` is no right a condition can declare, so no read-only condition admits it either."""
    effective = [*BASELINE, right]
    fake.grant(TOOLS.user_id, effective if held == "direct" else BASELINE, effective)
    with pytest.raises(PrivilegesMismatch, match=rf"the principal's {held} privileges.*has also {right}$"):
        issuer.mint(tool_spec(config, "read"), TOOLS, ceiling_s=CEILING_S)
    assert fake.issued == {}


OUTSIDE = "sp-outside-0009"  # a space Onezone knows that the configuration does not declare
SCOPE = "/api/v3/onezone/tokens/infer_access_token_scope"
ADMIN_READ = f"/api/v3/onezone/users/{TOOLS.user_id}/effective_privileges"
CLUSTERS = "/api/v3/onezone/user/effective_clusters"
PROVIDER_CLUSTER = "cl-provider-0001"  # the id of a provider's cluster, which no message may hold


@BOTH_KINDS
def test_o09_a_tool_server_token_whose_user_is_a_member_of_a_space_outside_the_plane_is_dropped(
    condition, issuer, fake, config
):
    """O09: a tool-server token carries no data-access caveat, a read-only condition's (O37) as a writing one's, so
    Onezone's scope of it is every space its user is a member of. After minting, a scope with one space more than the
    plane's is refused, so another study's space is out of reach: the token is dropped with the expiry it is known to
    have, and the message counts the space without naming it or giving its id."""
    fake.grant(TOOLS.user_id, RIGHTS[condition])
    fake.add_space(OUTSIDE, "unrelated-space", [ALPHA_ID])
    with pytest.raises(DroppedToken, match="has also 1 space the configuration does not declare") as refused:
        issuer.mint(tool_spec(config, condition), TOOLS, ceiling_s=CEILING_S)
    assert refused.value.expires_at == int(NOW) + token_window(CEILING_S, SETTINGS).mint_ttl_s
    assert OUTSIDE not in str(refused.value)
    assert "unrelated" not in str(refused.value)
    assert len(fake.issued) == 1  # it was minted, and dropped once its scope was read


@BOTH_KINDS
def test_o09_a_tool_server_user_with_a_onezone_admin_privilege_gets_no_token(condition, issuer, fake, config):
    """O09: an admin privilege of Onezone is live on a token without a data-access caveat, whatever the plane's spaces,
    so a tool-server user, a read-only condition's as a writing one's, must hold none. It is read, with the admin
    credential, before anything is minted, and the refusal names the privileges, which are no value of the
    federation."""
    fake.grant(TOOLS.user_id, RIGHTS[condition])
    fake.admin_privileges[TOOLS.user_id] = ["oz_spaces_add_relationships", "oz_groups_view"]
    with pytest.raises(AdminPrivileged, match="holds Onezone admin privileges: oz_groups_view, oz_spaces_add_rel"):
        issuer.mint(tool_spec(config, condition), TOOLS, ceiling_s=CEILING_S)
    assert fake.issued == {}
    assert [r.token for r in fake.requests if r.path == ADMIN_READ] == [fake.ADMIN]


@pytest.mark.parametrize(
    ("clusters", "count"),
    [([PROVIDER_CLUSTER], "1 cluster"), ([PROVIDER_CLUSTER, "cl-provider-0002"], "2 clusters")],
    ids=["one cluster", "two clusters"],
)
@BOTH_KINDS
def test_o09_a_tool_server_tokens_user_who_is_a_member_of_a_cluster_is_dropped(
    clusters, count, condition, issuer, fake, config
):
    """O09: a member of a provider's cluster (cluster_update, cluster_delete) can mint that provider's tokens and change
    or delete the provider, so it acts through it beyond the plane's spaces whatever its scope is. Onezone answers the
    current user's clusters only, so the read is sent as the minted token itself, never as the admin, and a member of
    any is refused, for a read-only condition as for a writing one: the token is dropped with the expiry it is known
    to have, and the message counts the clusters without naming one."""
    fake.grant(TOOLS.user_id, RIGHTS[condition])
    fake.clusters[TOOLS.user_id] = clusters
    with pytest.raises(DroppedToken, match=f"the principal is an effective member of {count}$") as refused:
        issuer.mint(tool_spec(config, condition), TOOLS, ceiling_s=CEILING_S)
    assert refused.value.expires_at == int(NOW) + token_window(CEILING_S, SETTINGS).mint_ttl_s
    assert PROVIDER_CLUSTER not in str(refused.value)
    (minted,) = fake.issued  # it was minted, and dropped once the clusters it acts in were read
    reads = [r for r in fake.requests if r.path == CLUSTERS]
    assert [(r.method, r.token) for r in reads] == [("GET", minted)]
    assert fake.ADMIN not in [r.token for r in reads]


@BOTH_KINDS
def test_o09_a_tool_server_token_without_a_cluster_is_minted_after_its_scope_and_before_it_is_returned(
    condition, issuer, fake, config
):
    """O09: a user who is a member of no cluster gets its token; the clusters are read once, after the scope."""
    fake.grant(TOOLS.user_id, RIGHTS[condition])
    token = issuer.mint(tool_spec(config, condition), TOOLS, ceiling_s=CEILING_S)
    paths = [r.path for r in fake.requests if r.path in (SCOPE, CLUSTERS)]
    assert paths == [SCOPE, CLUSTERS]
    assert [r.token for r in fake.requests if r.path == CLUSTERS] == [token.reveal()]


def test_o09_a_scope_that_is_refused_is_refused_before_the_clusters_are_read(issuer, fake, config):
    """O09: the clusters are read after the scope, so a user who fails both is refused for its scope alone and no
    request for its clusters is sent."""
    fake.grant(TOOLS.user_id, MANAGE)
    fake.add_space(OUTSIDE, "unrelated-space", [ALPHA_ID])
    fake.clusters[TOOLS.user_id] = [PROVIDER_CLUSTER]
    with pytest.raises(DroppedToken, match="the token's scope differs"):
        issuer.mint(tool_server_spec("spmock0001", config.condition("manage")), TOOLS, ceiling_s=CEILING_S)
    assert not [r for r in fake.requests if r.path == CLUSTERS]


def test_o05_the_data_path_reads_no_scope_admin_privileges_or_clusters(issuer, fake, config):
    """O05: the data path's boundary is its data.path caveat, so its mint is what it was: no scope is read for it, no
    admin privilege of its user and no cluster, even with a space outside the plane, an admin privilege and a cluster
    present. A data-access caveat would refuse a clusters read anyway."""
    fake.add_space(OUTSIDE, "unrelated-space", [ALPHA_ID])
    fake.admin_privileges[DATA.user_id] = ["oz_groups_view"]
    fake.clusters[DATA.user_id] = [PROVIDER_CLUSTER]
    issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    admin_read = f"/api/v3/onezone/users/{DATA.user_id}/effective_privileges"
    assert not [r for r in fake.requests if r.path in (SCOPE, admin_read, CLUSTERS)]


def test_o37_a_read_only_condition_reads_its_scope_admin_privileges_and_clusters_as_a_writing_one_does(
    issuer, fake, config
):
    """O37: a read-only condition's token has no data.path caveat to keep it to the plane, so its mint reads what a
    writing condition's does, in the same order: its user's privileges and admin privileges as the admin, and the
    token's scope and clusters, the clusters as the token itself."""
    reading = issuer.mint(tool_spec(config, "read"), TOOLS, ceiling_s=CEILING_S)
    read_requests = [(r.method, r.path) for r in fake.requests]
    boundary = [(r.path, r.token) for r in fake.requests if r.path in (ADMIN_READ, SCOPE, CLUSTERS)]
    assert boundary == [(ADMIN_READ, fake.ADMIN), (SCOPE, None), (CLUSTERS, reading.reveal())]
    fake.requests.clear()
    fake.grant(TOOLS.user_id, MANAGE)
    issuer.mint(tool_spec(config, "manage"), TOOLS, ceiling_s=CEILING_S)
    assert read_requests == [(r.method, r.path) for r in fake.requests]


def scope_without_spaces(answer):
    return {**answer, "dataAccessScope": None}


@pytest.mark.parametrize(
    ("bend", "outage", "reason"),
    [
        (lambda fake: setattr(fake, "scope_status", 503), True, "token's scope could not be read"),
        (lambda fake: setattr(fake, "scope_hook", scope_without_spaces), False, "scope answer lists no spaces"),
        (lambda fake: fake.admin_privileges.update({TOOLS.user_id: "oz_groups_view"}), False, "not a list of strings"),
        (lambda fake: setattr(fake, "clusters_status", 503), True, "token's clusters could not be read"),
        (lambda fake: setattr(fake, "clusters_hook", lambda answer: {}), False, "'clusters' is not a list of strings"),
        (lambda fake: setattr(fake, "clusters_hook", lambda a: {"clusters": [7]}), False, "not a list of strings"),
    ],
    ids=[
        "scope outage",
        "scope unreadable",
        "admin privileges unreadable",
        "clusters outage",
        "clusters answer without the key",
        "clusters that are no ids",
    ],
)
@BOTH_KINDS
def test_o05_a_tool_server_tokens_scope_admin_privileges_or_clusters_that_cannot_be_read_give_no_token(
    bend, outage, reason, condition, issuer, fake, config
):
    """O05: unknown is not a pass, for a read-only condition's token as for a writing one's. A scope, admin or clusters
    read that fails is an `ExaminationFailed` carrying the outage, so a caller can tell one apart; an answer that
    cannot be read is a refusal; neither gives a token."""
    fake.grant(TOOLS.user_id, RIGHTS[condition])
    bend(fake)
    with pytest.raises(TokenRefused, match=reason) as refused:
        issuer.mint(tool_spec(config, condition), TOOLS, ceiling_s=CEILING_S)
    cause = refused.value.__cause__ if isinstance(refused.value, DroppedToken) else refused.value
    assert isinstance(cause, ExaminationFailed) is outage
    if outage:
        assert isinstance(cause.error, PlatformError)
        assert cause.error.status == 503


def test_o05_the_tool_server_principal_must_hold_exactly_the_conditions_rights(issuer, fake, config):
    """O05: effective privileges equal the declared set; a missing write right and an undeclared one both refuse."""
    spec = tool_server_spec("spmock0001", config.condition("manage"))
    with pytest.raises(TokenRefused, match="lacks space_manage_qos"):  # the user has only view and read
        issuer.mint(spec, TOOLS, ceiling_s=CEILING_S)
    fake.grant(TOOLS.user_id, [*MANAGE, "space_schedule_replication"])
    with pytest.raises(TokenRefused, match="has also space_schedule_replication"):
        issuer.mint(spec, TOOLS, ceiling_s=CEILING_S)
    reading = tool_server_spec("spmock0001", config.condition("read"))
    fake.grant(TOOLS.user_id, MANAGE)
    with pytest.raises(
        TokenRefused, match="has also space_manage_qos"
    ):  # a writing user cannot run a read-only condition
        issuer.mint(reading, TOOLS, ceiling_s=CEILING_S)
    assert fake.issued == {}


def test_o05_the_two_planes_get_separate_principals_and_tokens(issuer, fake):
    """O05: the data path and the tool server never share a user or a token."""
    fake.grant(TOOLS.user_id, MANAGE)
    both = issuer.mint_run_tokens(data=DATA, ceiling_s=CEILING_S, tool_server=(TOOLS, "manage"))
    assert both.tool_server is not None
    assert both.data_path.subject != both.tool_server.subject
    assert both.data_path.token_id != both.tool_server.token_id
    assert both.data_path.reveal() != both.tool_server.reveal()
    assert types(fake, both.data_path) != types(fake, both.tool_server)


def test_o05_one_user_for_both_planes_is_refused_before_anything_is_minted(issuer, fake):
    """O05: a shared principal is refused up front."""
    with pytest.raises(TokenRefused, match="share a principal"):
        issuer.mint_run_tokens(data=DATA, ceiling_s=CEILING_S, tool_server=(DATA, "read"))
    assert fake.requests == []
    assert fake.issued == {}


def test_o05_two_tokens_that_examine_as_one_are_refused(issuer, fake):
    """O05: if the federation reports the same token id for both planes, nothing is handed out."""
    fake.examine_hook = lambda answer: {**answer, "id": "same-id"}
    with pytest.raises(TokenRefused, match="share a token"):
        issuer.mint_run_tokens(data=DATA, ceiling_s=CEILING_S, tool_server=(TOOLS, "read"))


def test_o05_a_refused_second_plane_returns_neither_token(issuer, fake):
    """O05: the pair is all or nothing: a refused tool-server plane leaves the caller with no data-path token either."""
    with pytest.raises(TokenRefused, match="lacks space_manage_qos"):
        issuer.mint_run_tokens(data=DATA, ceiling_s=CEILING_S, tool_server=(TOOLS, "manage"))


def test_o06_no_call_ever_revokes_a_token_and_a_token_ends_with_its_lifetime(issuer, fake):
    """O06: no source revokes a token, so none is revoked, refused or not; the lifetime is what ends it."""
    fake.grant(TOOLS.user_id, MANAGE)
    issuer.mint_run_tokens(data=DATA, ceiling_s=CEILING_S, tool_server=(TOOLS, "manage"))
    fake.mint_hook = lambda user, caveats: caveats[:1]
    with pytest.raises(TokenRefused):
        issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    assert not any(r.method == "DELETE" for r in fake.requests)
    assert not hasattr(issuer, "revoke")


def test_o06_the_lifetime_is_the_ceiling_plus_a_margin():
    """O06: the least a token may have, the most, and the lifetime asked for, all derived from the cap."""
    window = token_window(7200, SETTINGS)
    assert (window.min_ttl_s, window.max_ttl_s, window.mint_ttl_s) == (8100, 8700, 8400)
    assert token_window(0.5, SETTINGS).min_ttl_s == 901  # rounded up, never down


@pytest.mark.parametrize("cap", [0, -1, float("nan"), float("inf"), True, "600", None])
def test_o06_a_cap_that_is_not_a_positive_number_is_refused_before_minting(cap, issuer, fake):
    """O06: the lifetime is checked before minting, so a bad cap sends no request."""
    with pytest.raises(TokenRefused, match="ceiling"):
        issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=cap)
    assert fake.requests == []


def test_o06_a_lifetime_the_platform_cannot_grant_is_refused_before_minting(config, client, fake):
    """O06: with a platform limit, a run that needs a longer token is refused before anything is sent."""
    limited = dataclasses.replace(config, tokens=TokenSettings(max_ttl_s=1000))
    with pytest.raises(TokenRefused, match="does not grant"):
        TokenIssuer(client, limited, clock=lambda: NOW).mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    assert fake.requests == []


def test_o06_a_token_shorter_than_the_cap_plus_the_margin_is_refused(issuer, fake):
    """O06: if the federation grants less than was asked, the examination refuses the token."""

    def shorter(user, caveats):
        return [{**c, "validUntil": c["validUntil"] - 700} if c["type"] == "time" else c for c in caveats]

    fake.mint_hook = shorter
    with pytest.raises(TokenRefused, match="shorter"):
        issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    fake.mint_hook = lambda user, caveats: [
        {**c, "validUntil": c["validUntil"] + 5000} if c["type"] == "time" else c for c in caveats
    ]
    with pytest.raises(TokenRefused, match="longer"):
        issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)


def test_o06_the_minted_token_lives_from_the_cap_plus_margin_to_the_headroom(issuer, fake):
    """O06: the expiry in the token is now plus the derived lifetime, and the result reports it."""
    token = issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    window = token_window(CEILING_S, SETTINGS)
    expiry = fake.issued[token.reveal()]["caveats"][0]["validUntil"]
    assert expiry == int(NOW) + window.mint_ttl_s == token.expires_at
    assert window.min_ttl_s <= expiry - NOW <= window.max_ttl_s


def test_r12_a_minted_token_is_redacted_from_a_record_that_contains_it(issuer, fake):
    """R12: the platform layer registers what it mints, so a tool output that quotes the token is redacted."""
    token = issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    value = token.reveal()
    with pytest.warns(RuntimeWarning, match="redacted secrets"):
        record = record_run([[("show", {})], "done"], {"show": lambda args: f"the credential is {value} for now"})
    text = record.dumps()
    assert value not in text
    assert REDACTED in text
    assert value not in json.dumps(record.to_json())


def test_r12_the_value_is_in_no_repr_str_or_exception(issuer, fake, client):
    """R12: neither the token, its secret, the issuer nor an error that follows shows the value."""
    token = issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
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


def test_o05_a_federation_that_answers_privileges_as_a_dict_of_booleans_mints_nothing(issuer, fake):
    """O05: the privilege answer is read as a list of strings only; a dict of flags is refused before any mint."""
    fake.privileges[(fake.SPACE_ID, DATA.user_id)]["direct"] = {"space_view": True, "space_read_data": True}
    with pytest.raises(TokenRefused, match="direct privileges could not be read"):
        issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    assert fake.issued == {}


def test_c19_a_user_revokes_its_own_tokens_and_the_generation_must_rise(issuer, fake):
    """C19: the generation before and after the user's own revocation; a token minted before it is revoked."""
    token = issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    assert issuer.revoke_own(DATA) == (1, 2)
    issuer.require_revoked(token.reveal())  # answered 400 tokenRevoked
    fake.revoke_bumps = False
    with pytest.raises(RevocationUnconfirmed, match="did not rise"):
        issuer.revoke_own(DATA)


def test_c19_only_token_revoked_confirms_a_revocation(issuer, fake):
    """C19: examining a token only decodes it, and verifying refuses the caveated data-path token for its caveats
    anyway, so neither a clean answer nor any other error shows the token revoked: only 400 `tokenRevoked` does."""
    token = issuer.mint(data_path_spec("spmock0001"), DATA, ceiling_s=CEILING_S)
    with pytest.raises(RevocationUnconfirmed, match="tokenCaveatUnverified") as caught:
        issuer.require_revoked(token.reveal())  # not revoked: refused for its data-access caveats
    assert token.reveal() not in str(caught.value)
    for answer in ((200, None), (400, "badValueToken"), (403, "tokenRevoked"), (500, "internalServerError")):
        fake.verify_override = answer
        with pytest.raises(RevocationUnconfirmed):
            issuer.require_revoked(token.reveal())
    fake.verify_override = (400, "tokenRevoked")
    issuer.require_revoked(token.reveal())
    assert [r.path for r in fake.requests if "examine" in r.path] == [
        "/api/v3/onezone/tokens/examine"
    ]  # only the mint's own examination: a revocation is never confirmed by one
