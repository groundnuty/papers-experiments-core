"""O05: the live contract test of a condition's token skips only on a privileges mismatch, which is checked offline.
O39: the live test of a read-only condition's tools fills their arguments from their schemas, counts only refusals."""

import pytest
from live_support import mint_for_condition, refused, tool_arguments

from expcore_onedata.client import PlatformError
from expcore_onedata.testing import BASELINE, CEILING_S, MANAGE
from expcore_onedata.testing import TOOL_SERVER_USER as TOOLS
from expcore_onedata.tokencheck import PrivilegesMismatch, TokenRefused


def test_o05_a_user_with_exactly_the_conditions_rights_gets_its_token(config, issuer, fake):
    """O05: the helper mints and examines, and returns the token when the platform agrees on the rights."""
    fake.grant(TOOLS.user_id, MANAGE)
    token = mint_for_condition(issuer, config, "manage", TOOLS, CEILING_S)
    assert token.subject == TOOLS.user_id


@pytest.mark.parametrize(
    ("direct", "effective"),
    [(BASELINE, BASELINE), (MANAGE, MANAGE), (BASELINE, MANAGE), (MANAGE, BASELINE)],
    ids=["lacks one", "has the other condition's", "effective gained", "effective lost"],
)
def test_o05_a_privileges_mismatch_skips_only_that_condition(direct, effective, config, issuer, fake):
    """O05: the platform's report that the user holds other rights is the one reason to skip, and it skips this
    condition alone: the next call, for a condition the user does hold, goes on to mint."""
    fake.grant(TOOLS.user_id, direct, effective)
    with pytest.raises(pytest.skip.Exception, match="privileges are not the rights"):
        mint_for_condition(issuer, config, "manage" if direct != MANAGE else "read", TOOLS, CEILING_S)
    fake.grant(TOOLS.user_id, BASELINE)
    assert mint_for_condition(issuer, config, "read", TOOLS, CEILING_S).subject == TOOLS.user_id


def refuse_by_dropping(type_):
    """A federation that leaves a caveat out of the token it mints."""
    return lambda user, caveats: [c for c in caveats if c["type"] != type_]


@pytest.mark.parametrize(
    ("break_it", "reason"),
    [
        (
            lambda fake: setattr(fake, "mint_hook", lambda user, caveats: [*caveats, {"type": "data.readonly"}]),
            r"data-access caveat \('data\.readonly'\)",
        ),
        (lambda fake: setattr(fake, "mint_hook", refuse_by_dropping("interface")), "no interface caveat"),
        (lambda fake: setattr(fake, "mint_hook", refuse_by_dropping("time")), "no time caveat"),
        (
            lambda fake: setattr(
                fake,
                "mint_hook",
                lambda user, caveats: [{**c, "validUntil": 1} if c["type"] == "time" else c for c in caveats],
            ),
            "expired",
        ),
        (lambda fake: setattr(fake, "examine_hook", lambda answer: "not an object"), "could not be parsed"),
        (lambda fake: setattr(fake, "examine_hook", lambda answer: {**answer, "caveats": "x"}), "'caveats'"),
        (
            lambda fake: setattr(fake, "examine_hook", lambda answer: {**answer, "subject": {"id": "u-other"}}),
            "another principal",
        ),
    ],
    ids=[
        "data-access caveat",
        "no interface",
        "no time",
        "expired",
        "unparsable",
        "caveats not a list",
        "other subject",
    ],
)
def test_o05_any_other_refusal_is_a_failure_not_a_skip(break_it, reason, config, issuer, fake):
    """O05: a missing caveat, a data-access caveat on a tool-server token (O39), an expiry, a malformed examination or
    another principal's token is a TokenRefused that the live test lets through as a failure, whatever the rights the
    user holds."""
    break_it(fake)
    with pytest.raises(TokenRefused, match=reason) as caught:
        mint_for_condition(issuer, config, "read", TOOLS, CEILING_S)
    assert not isinstance(caught.value, PrivilegesMismatch)


def test_o05_a_privilege_answer_that_cannot_be_read_is_a_failure_not_a_skip(config, issuer, fake):
    """O05: unknown is not "other rights": an unreadable privilege answer fails the live test."""
    fake.privileges[(fake.SPACE_ID, TOOLS.user_id)]["direct"] = {"space_view": True}
    with pytest.raises(TokenRefused, match="could not be read"):
        mint_for_condition(issuer, config, "read", TOOLS, CEILING_S)


def test_o39_a_tools_arguments_come_from_its_own_input_schema_by_what_each_name_says():
    """O39: the live test of a read-only condition's tools never assumes the server's argument names: it reads each
    property's kind from its name, the more specific word first (a state before a transfer, a transfer or a
    requirement before a file, a provider before replicas), and gives each the value of that kind, as text when the
    schema types it a string."""
    schema = {
        "properties": {
            "file_id": {"type": "string"},
            "qos_requirement_id": {"type": "string"},
            "transfer_id": {"type": "string"},
            "replicas_num": {"type": "string"},
            "expression": {"type": "string"},
            "space": {"type": "string"},
            "state": {"type": "string"},
            "target_provider_id": {"type": "string"},
            "page_token": {"type": "string"},
        },
        "required": ["file_id", "transfer_id"],
    }
    values = {
        "file": "file-0001",
        "requirement": "requirement-0001",
        "transfer": "transfer-0001",
        "replicas": 1,
        "expression": "anyStorage",
        "space": "space-0001",
        "state": "ended",
        "provider": "provider-0001",
    }
    assert tool_arguments(schema, values) == {
        "file_id": "file-0001",
        "qos_requirement_id": "requirement-0001",
        "transfer_id": "transfer-0001",
        "replicas_num": "1",
        "expression": "anyStorage",
        "space": "space-0001",
        "state": "ended",
        "target_provider_id": "provider-0001",
    }
    assert tool_arguments({"properties": {"replicas": {"type": "integer"}}}, values) == {"replicas": 1}
    two_words = {"properties": {"transfer_state": {}, "replicating_provider_id": {}, "file_qos_requirement": {}}}
    assert tool_arguments(two_words, values) == {
        "transfer_state": "ended",
        "replicating_provider_id": "provider-0001",
        "file_qos_requirement": "requirement-0001",
    }


def test_o39_a_tool_whose_required_arguments_cannot_be_filled_is_unmeasured_not_guessed():
    """O39: a required property whose name says no known kind, or a kind the test has no value of, leaves the tool
    unmeasured, with the reason naming the properties; an optional one is left out."""
    schema = {"properties": {"file_id": {}, "mode": {}, "limit": {}}, "required": ["mode", "file_id"]}
    known = {"file": "file-0001"}
    assert tool_arguments(schema, known) == "its input schema requires mode, which the test cannot fill"
    assert tool_arguments(schema, {}) == "its input schema requires file_id, mode, which the test cannot fill"
    assert tool_arguments({"properties": {"file_id": {}, "limit": {}}}, known) == {"file_id": "file-0001"}
    assert tool_arguments(None, known) == {}


# The property names and required lists of the pinned tool server's seven tools, as the manifest of the 14 Nov live run
# froze them (onedata-mcp 8c9b10c): the argument names are the server's, so this checks the test fills each of them.
PINNED_TOOLS = {
    "get_transfer": (["transfer_id"], ["transfer_id"]),
    "list_space_transfers": (["limit", "page_token", "space_id", "state"], ["space_id"]),
    "get_qos_requirement": (["qos_id"], ["qos_id"]),
    "get_file_qos_summary": (["file_id_or_path"], ["file_id_or_path"]),
    "get_file_distribution": (["file_id_or_path"], ["file_id_or_path"]),
    "schedule_file_replication": (["file_id_or_path", "target_provider_id"], ["file_id_or_path", "target_provider_id"]),
    "add_file_qos_requirement": (["expression", "file_id_or_path", "replicas_num"], ["file_id_or_path", "expression"]),
}
TEST_VALUES = {
    "file": "file-0001",
    "requirement": "requirement-0001",
    "transfer": "transfer-0001",
    "replicas": 1,
    "expression": "anyStorage",
    "space": "space-0001",
    "state": "ended",
    "provider": "provider-0001",
}


@pytest.mark.parametrize("tool", sorted(PINNED_TOOLS))
def test_o39_every_required_argument_of_the_pinned_servers_tools_is_filled(tool):
    """O39: the 14 Nov live run failed `get_qos_requirement` because its `qos_id` named no kind the test knew. Each
    of the seven tools' required arguments, by the names the pinned server gives them, is filled by the kind the
    name says, so none is left unmeasured."""
    properties, required = PINNED_TOOLS[tool]
    schema = {"properties": {name: {"type": "string"} for name in properties}, "required": required}
    arguments = tool_arguments(schema, TEST_VALUES)
    assert isinstance(arguments, dict), arguments
    assert set(required) <= set(arguments)


def test_o39_a_qos_id_is_the_requirement_and_the_other_qos_names_keep_their_kind():
    """O39: a name holding `qos` is a requirement's id, but the kind a more specific word names wins: the expression and
    the replicas of a QoS requirement, and the file it is on, stay what they are."""
    schema = {
        "properties": {
            "qos_id": {"type": "string"},
            "qos_expression": {"type": "string"},
            "qos_replicas_num": {"type": "integer"},
            "file_qos_id": {"type": "string"},
            "file_id": {"type": "string"},
        }
    }
    assert tool_arguments(schema, TEST_VALUES) == {
        "qos_id": "requirement-0001",
        "qos_expression": "anyStorage",
        "qos_replicas_num": 1,
        "file_qos_id": "requirement-0001",
        "file_id": "file-0001",
    }


@pytest.mark.parametrize(
    ("status", "code", "errno", "is_refusal"),
    [
        (401, "unauthorized", None, True),
        (401, "tokenCaveatUnverified", None, True),
        (403, "forbidden", None, True),
        (400, "posix", "eacces", True),
        (400, "posix", "eperm", True),
        (400, "posix", "EPERM", True),
        (400, "posix", "enoent", False),
        (400, "posix", "eio", False),
        (400, "posix", None, False),
        (400, None, "eperm", False),
        (400, "badValueToken", "eperm", False),
        (400, "badValueToken", None, False),
        (404, "notFound", None, False),
        (404, "posix", "eperm", False),
        (500, "internalServerError", None, False),
        (500, "posix", "eperm", False),
        (None, None, None, False),
    ],
)
def test_o39_only_a_refusal_counts_as_a_refused_write(status, code, errno, is_refusal):
    """O39: a write of a read-only condition passes the live test only when Onedata refused it: 401, 403, or 400 posix
    with errno eacces (a token caveat's refusal) or eperm (a user without the privilege: the 14 Nov live run's answer
    to a data write by the read-only condition's token). A write that failed for another reason, a missing file, a
    bad value, a server error or no answer at all, is not a refusal, so it cannot make the test pass; nor is another
    status or error id that carries the same errno."""
    error = PlatformError("x", status=status, code=code, errno=errno, transport=status is None)
    assert refused(error) is is_refusal
