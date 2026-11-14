"""Several spaces reach the token planes, the health check and the mutation gate: each plane's token is scoped to its
declared set of spaces, its principal's rights are read in each, and every message names a space by its position."""

import base64

import pytest

from expcore_onedata import toolserver
from expcore_onedata.client import OnezoneClient, OutOfScope, PlatformError
from expcore_onedata.config import load_platform
from expcore_onedata.health import PlatformHealth
from expcore_onedata.testing import (
    ALPHA_ID,
    BASELINE,
    BETA_ID,
    CEILING_S,
    DATA_PATH_USER,
    NOW,
    TOOL_SERVER_USER,
    USERS,
    write_platform,
)
from expcore_onedata.testing.world import SPACE_IDS, SPACE_NAMES, with_spaces
from expcore_onedata.tokencheck import CaveatSpec, PrivilegesMismatch, TokenRefused, require_examined
from expcore_onedata.tokens import Plane, TokenIssuer, data_path_spec, tool_server_spec


def configure(tmp_path, fake, *, count=2, edit=None):
    """A several-space configuration over the fake, with every added space known to Onezone, supported by alpha and
    beta, and every test user granted view and read in each."""
    fake.add_provider(BETA_ID, "beta.test")
    for space_id, name in zip(SPACE_IDS[1:count], SPACE_NAMES[1:count], strict=True):
        fake.add_space(space_id, name, [ALPHA_ID, BETA_ID])
        for _, user_id in USERS.values():
            fake.grant(user_id, BASELINE, space=space_id)

    def document(data):
        with_spaces(data, count)
        if edit is not None:
            edit(data)

    config = load_platform(write_platform(tmp_path, document))
    client = OnezoneClient(config, http=fake.client_http())
    return config, client, TokenIssuer(client, config, clock=lambda: NOW)


def whitelist(fake, token):
    """The decoded data.path whitelist the fake stored for a minted token."""
    caveat = next(c for c in fake.issued[token.reveal()]["caveats"] if c["type"] == "data.path")
    return [base64.b64decode(entry).decode() for entry in caveat["whitelist"]]


def test_o05_a_one_space_token_is_minted_as_before_whichever_way_its_space_is_named(issuer, fake):
    """O05: a sequence of one space id gives the same caveats as the id itself, so the one-space form's tokens do not
    change with several spaces in the layer."""
    assert data_path_spec(["spmock0001"]) == data_path_spec("spmock0001")
    token = issuer.mint(data_path_spec(("spmock0001",)), DATA_PATH_USER, ceiling_s=CEILING_S)
    assert whitelist(fake, token) == ["/spmock0001"]


def test_o05_each_planes_token_reaches_exactly_the_spaces_it_is_scoped_to(tmp_path, fake):
    """O05: with three spaces, the data path's token is scoped to the two its plane names and the tool server's to the
    two its plane names, one data.path entry each; neither reaches a space its plane is not scoped to."""

    def planes(data):
        data["spaces"][1]["tokens"] = ["data_path"]
        data["spaces"][2]["tokens"] = ["tool_server"]

    config, _, issuer = configure(tmp_path, fake, count=3, edit=planes)
    tokens = issuer.mint_run_tokens(data=DATA_PATH_USER, ceiling_s=CEILING_S, tool_server=(TOOL_SERVER_USER, "read"))
    assert whitelist(fake, tokens.data_path) == [f"/{SPACE_IDS[0]}", f"/{SPACE_IDS[1]}"]
    assert whitelist(fake, tokens.tool_server) == [f"/{SPACE_IDS[0]}", f"/{SPACE_IDS[2]}"]
    assert config.space_ids(Plane.DATA_PATH) == SPACE_IDS[:2]


def test_o05_the_examination_refuses_a_token_that_misses_or_adds_a_space(tmp_path, fake):
    """O05: a token whose whitelist lacks one of the plane's spaces, or names one more, fails its examination; the
    whitelist may list the spaces in any order."""
    spec = CaveatSpec(SPACE_IDS[:2], readonly=True, interface="rest")

    def examine(paths):
        caveats = [c for c in spec.caveats(int(NOW) + 2000) if c["type"] != "data.path"]
        caveats.append({"type": "data.path", "whitelist": [base64.b64encode(p.encode()).decode() for p in paths]})
        answer = {"id": "tid-0001", "subject": {"id": "u-data"}, "caveats": caveats}
        return require_examined(
            "token-value", spec, subject="u-data", examine=lambda _: answer, min_ttl_s=1, max_ttl_s=3000, now=NOW
        )

    assert examine([f"/{SPACE_IDS[1]}", f"/{SPACE_IDS[0]}"]).token_id == "tid-0001"
    for wrong in ([f"/{SPACE_IDS[0]}"], [f"/{s}" for s in SPACE_IDS], [f"/{SPACE_IDS[0]}", f"/{SPACE_IDS[0]}"]):
        with pytest.raises(TokenRefused, match=r"data\.path caveat is not the study space\(s\) alone"):
            examine(wrong)


def test_o05_a_principal_without_the_rights_in_one_space_is_refused_naming_that_space_by_position(tmp_path, fake):
    """O05: the plane's rights are read in every space its token reaches; a principal set up for other rights in the
    second space is refused before anything is minted, and the refusal names `spaces[1]`, never its id or name."""
    config, _, issuer = configure(tmp_path, fake)
    fake.grant(DATA_PATH_USER.user_id, ["space_view"], space=SPACE_IDS[1])
    with pytest.raises(PrivilegesMismatch) as caught:
        issuer.mint(data_path_spec(config.space_ids(Plane.DATA_PATH)), DATA_PATH_USER, ceiling_s=CEILING_S)
    assert str(caught.value).startswith("spaces[1]: ")
    assert fake.issued == {}


def test_o09_a_writing_conditions_token_reaches_its_spaces_through_its_principals_rights_alone(tmp_path, fake):
    """O09: a writing condition's token has no data.path caveat, so what bounds its writes is its principal: the rights
    are read in every space of the tool-server plane, and a principal without the condition's write right in the
    second space gets no token, the refusal naming spaces[1]; with the rights in both it gets one with no data
    caveat."""
    from expcore_onedata.testing import MANAGE

    config, _, issuer = configure(tmp_path, fake)
    spec = tool_server_spec(config.space_ids(Plane.TOOL_SERVER), config.condition("manage"))
    assert spec.caveats.space_ids == SPACE_IDS[:2]
    fake.grant(TOOL_SERVER_USER.user_id, MANAGE, space=SPACE_IDS[0])
    with pytest.raises(PrivilegesMismatch, match="lacks space_manage_qos") as caught:
        issuer.mint(spec, TOOL_SERVER_USER, ceiling_s=CEILING_S)
    assert str(caught.value).startswith("spaces[1]: ")
    assert fake.issued == {}
    fake.grant(TOOL_SERVER_USER.user_id, MANAGE, space=SPACE_IDS[1])
    token = issuer.mint(spec, TOOL_SERVER_USER, ceiling_s=CEILING_S)
    assert [c["type"] for c in fake.issued[token.reveal()]["caveats"]] == ["time", "interface"]


def test_o09_a_writing_token_whose_user_is_a_member_of_a_data_path_only_space_is_refused_naming_it(tmp_path, fake):
    """O09: a space the study declares for the data path alone was kept from the tool server by data.path. A writing
    token has no data.path, so a tool-server user who is a member of that space would reach it: the token's scope
    shows it, and the mint is refused naming the space by its position, never by its id or name."""
    from expcore_onedata.testing import MANAGE
    from expcore_onedata.tokens import DroppedToken

    config, _, issuer = configure(tmp_path, fake, edit=lambda data: data["spaces"][1].update(tokens=["data_path"]))
    spec = tool_server_spec(config.space_ids(Plane.TOOL_SERVER), config.condition("manage"))
    assert spec.caveats.space_ids == SPACE_IDS[:1]
    fake.grant(TOOL_SERVER_USER.user_id, MANAGE, space=SPACE_IDS[0])
    with pytest.raises(DroppedToken, match=r"has also spaces\[1\]$") as refused:
        issuer.mint(spec, TOOL_SERVER_USER, ceiling_s=CEILING_S)
    assert SPACE_IDS[1] not in str(refused.value)
    assert SPACE_NAMES[1] not in str(refused.value)


def test_o09_a_writing_token_whose_scope_lacks_one_of_the_planes_spaces_is_refused_naming_it(tmp_path, fake):
    """O09: the scope must be the plane's spaces exactly, so one the token misses is refused too, by its position."""
    from expcore_onedata.testing import MANAGE
    from expcore_onedata.tokens import DroppedToken

    config, _, issuer = configure(tmp_path, fake)
    for space_id in SPACE_IDS[:2]:
        fake.grant(TOOL_SERVER_USER.user_id, MANAGE, space=space_id)

    def without_second(answer):
        del answer["dataAccessScope"]["spaces"][SPACE_IDS[1]]
        return answer

    fake.scope_hook = without_second
    spec = tool_server_spec(config.space_ids(Plane.TOOL_SERVER), config.condition("manage"))
    with pytest.raises(DroppedToken, match=r"lacks spaces\[1\]$"):
        issuer.mint(spec, TOOL_SERVER_USER, ceiling_s=CEILING_S)


def test_o05_the_tool_server_launch_checks_its_token_against_every_space_of_its_plane(tmp_path, fake):
    """O05: the launch-time check builds the condition's spec from the tool-server plane's spaces, so a token minted
    for the first space alone does not pass it, and one minted for the plane's spaces does."""
    config, _, issuer = configure(tmp_path, fake)
    condition = config.condition("read")
    narrow = issuer.mint(tool_server_spec(SPACE_IDS[0], condition), TOOL_SERVER_USER, ceiling_s=CEILING_S)
    with pytest.raises(TokenRefused, match=r"data\.path caveat"):
        toolserver._prepare(issuer, config, condition, narrow, CEILING_S)
    scoped = issuer.mint(
        tool_server_spec(config.space_ids(Plane.TOOL_SERVER), condition), TOOL_SERVER_USER, ceiling_s=CEILING_S
    )
    toolserver._prepare(issuer, config, condition, scoped, CEILING_S)


def test_o04_health_checks_each_space_against_its_own_support_set(tmp_path, fake):
    """O04: every declared space is read from one scope answer and compared with its own providers; a second space
    whose support differs fails the check, named by its position."""
    config, client, issuer = configure(tmp_path, fake, edit=lambda d: d["spaces"][0].update(providers=["alpha"]))
    fake.supports.pop(BETA_ID)  # the study space: alpha alone, as its entry declares
    health = PlatformHealth(config, client, issuer, clock=lambda: NOW)
    passed = {
        "ok": True,
        "detail": "ok: each of the 2 spaces is supported by its declared set",
        "deferred": False,
        "charge": True,
    }
    assert health.space()._asdict() == passed  # the space check: the composed one probes links first (O35)
    fake.space_supports[SPACE_IDS[1]].pop(BETA_ID)
    verdict = health.space()
    assert verdict.ok is False
    assert verdict.detail.startswith("spaces[1]: the providers supporting the space differ from the declared set")


def test_o09_a_mutation_passes_the_gate_under_any_declared_spaces_prefix_and_no_other(tmp_path, fake):
    """O09: the gate reads the target space's live name and accepts it under the prefixes of any declared space; a
    space outside all of them is refused before anything is sent."""
    _, client, _ = configure(tmp_path, fake, count=3)
    fake.other_spaces["sp-outside-0004"] = "unrelated-space"
    with pytest.raises(OutOfScope):
        client.mutate("POST", "/spaces/sp-outside-0004/views")
    assert not [r for r in fake.requests if r.method == "POST"]
    with pytest.raises(PlatformError) as sent:  # in scope: sent, and the fake has no such route
        client.mutate("POST", f"/spaces/{SPACE_IDS[2]}/views")
    assert sent.value.status == 404
    assert [r.path for r in fake.requests if r.method == "POST"] == [f"/api/v3/onezone/spaces/{SPACE_IDS[2]}/views"]
