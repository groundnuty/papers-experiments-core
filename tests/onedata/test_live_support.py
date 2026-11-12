"""O05: the live contract test of a condition's token skips only on a privileges mismatch, which is checked offline."""

import pytest
from live_support import mint_for_condition

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
        (lambda fake: setattr(fake, "mint_hook", refuse_by_dropping("data.readonly")), r"no data\.readonly caveat"),
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
    ids=["no read-only", "no interface", "no time", "expired", "unparsable", "caveats not a list", "other subject"],
)
def test_o05_any_other_refusal_is_a_failure_not_a_skip(break_it, reason, config, issuer, fake):
    """O05: a missing caveat, an expiry, a malformed examination or another principal's token is a TokenRefused that
    the live test lets through as a failure, whatever the rights the user holds."""
    break_it(fake)
    with pytest.raises(TokenRefused, match=reason) as caught:
        mint_for_condition(issuer, config, "read", TOOLS, CEILING_S)
    assert not isinstance(caught.value, PrivilegesMismatch)


def test_o05_a_privilege_answer_that_cannot_be_read_is_a_failure_not_a_skip(config, issuer, fake):
    """O05: unknown is not "other rights": an unreadable privilege answer fails the live test."""
    fake.privileges[(fake.SPACE_ID, TOOLS.user_id)]["direct"] = {"space_view": True}
    with pytest.raises(TokenRefused, match="could not be read"):
        mint_for_condition(issuer, config, "read", TOOLS, CEILING_S)
