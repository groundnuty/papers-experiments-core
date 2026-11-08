"""What the live contract tests share with the offline tests that check them: minting a condition's token."""

import pytest

from expcore_onedata.config import PlatformConfig
from expcore_onedata.credentials import Principal
from expcore_onedata.tokencheck import PrivilegesMismatch
from expcore_onedata.tokens import MintedToken, TokenIssuer, tool_server_spec


def mint_for_condition(
    issuer: TokenIssuer, config: PlatformConfig, name: str, principal: Principal, ceiling_s: float
) -> MintedToken:
    """Mint and examine a condition's tool-server token; skip the test only if the platform says the user is set up
    for other rights, and let every other refusal (a caveat, an expiry, an examination that cannot be read) fail it.

    The skip reason names no condition, so a study's condition names never reach the test report.
    """
    spec = tool_server_spec(config.space.id, config.condition(name))
    try:
        return issuer.mint(spec, principal, ceiling_s=ceiling_s)
    except PrivilegesMismatch:
        pytest.skip("the tool-server user's privileges are not the rights this condition declares")
