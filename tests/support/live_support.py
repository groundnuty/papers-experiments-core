"""What the live contract tests share with the offline tests that check them: minting a condition's token."""

from collections.abc import Mapping

import pytest

from expcore_onedata.config import PlatformConfig
from expcore_onedata.credentials import Principal
from expcore_onedata.day0.outage import prerequisite as outage_prerequisite
from expcore_onedata.day0.registry import Context
from expcore_onedata.day0.report import Entry
from expcore_onedata.tokencheck import PrivilegesMismatch
from expcore_onedata.tokens import MintedToken, TokenIssuer, tool_server_spec


def mint_for_condition(
    issuer: TokenIssuer, config: PlatformConfig, name: str, principal: Principal, ceiling_s: float
) -> MintedToken:
    """Mint and examine a condition's tool-server token; skip the test only if the platform says the user is set up
    for other rights, and let every other refusal (a caveat, an expiry, an examination that cannot be read) fail it.

    The skip reason names no condition, so a study's condition names never reach the test report.
    """
    spec = tool_server_spec(config.space_ids("tool_server"), config.condition(name))
    try:
        return issuer.mint(spec, principal, ceiling_s=ceiling_s)
    except PrivilegesMismatch:
        pytest.skip("the tool-server user's privileges are not the rights this condition declares")


# -- what the state-changing day-0 checks must show in a live run -----------------------------------------------------


def prerequisite_missing(name: str, ctx: Context) -> str | None:
    """Name what a state-changing day-0 check needs that platform.toml does not give, or None when it has it.

    This is the one reason such a live test is skipped, and the skip names it. The names are of keys and tables, never
    of a provider or a space, so no configured value reaches a test report.
    """
    if name == "read_only":
        if ctx.config.principals.data_path is None:
            return "the read_only check needs a fixed [principals.data_path] user, which platform.toml does not declare"
        return None
    if name == "round_trip":
        if ctx.placement is None:
            return "the round_trip check needs a [placement] table, which platform.toml does not have"
        holder = ctx.placement.settings.holder
        receiver = next((p for p in ctx.config.support.providers if p != holder), None)
        if receiver is None:
            return (
                "the round_trip check needs a supporting provider besides the data provider; the support set has none"
            )
        if any(p not in ctx.provider_ids for p in (holder, receiver)):
            return "the round_trip check needs the id of the data provider and of the receiving provider"
        return None
    if name == "provider_outage":
        return outage_prerequisite(ctx)
    raise ValueError("this helper knows the read_only, round_trip and provider_outage checks only")


def require_passed(entry: Entry) -> None:
    """Fail unless the check ran and passed: `ok` is True. A check that was unknown ran nothing and never passes."""
    assert entry.ok is True, f"the {entry.name} check did not pass (ok={entry.ok!r}): {entry.summary}"


def require_every_provider_refused(entry: Entry) -> None:
    """Fail unless the check shows a refusal on at least one provider and on every one it names, with nothing left."""
    rows = entry.facts.get("providers")
    assert isinstance(rows, Mapping), f"the {entry.name} check reports no provider it ran on"
    assert rows, f"the {entry.name} check reports no provider it ran on"
    assert all(row.get("enforced") is True for row in rows.values()), (
        f"the {entry.name} check shows a write not refused"
    )
    assert all(row.get("cleaned") is not False for row in rows.values()), f"the {entry.name} check left a scratch entry"


def per_space_facts(entry: Entry) -> list[Mapping]:
    """A check's facts for each space it ran in: under `spaces` with several spaces (`per_space`), else its own."""
    spaces = entry.facts.get("spaces")
    if isinstance(spaces, Mapping):
        assert spaces, f"the {entry.name} check reports no space it ran in"
        return list(spaces.values())
    return [entry.facts]


def require_restored(entry: Entry) -> None:
    """Fail unless the check shows the provider back in every space it ran in: the injector's restore cleared and
    verified by a changed identity, and its REST answering again. A pass that does not say so is no pass."""
    for facts in per_space_facts(entry):
        restore = facts.get("restore")
        assert isinstance(restore, Mapping), f"the {entry.name} check reports no restore"
        assert restore.get("cleared") is True, f"the {entry.name} check did not bring the provider back"
        assert restore.get("verified") is True, f"the {entry.name} check did not verify the restore"
        assert restore.get("how") in ("restored", "found_restored"), (
            f"the {entry.name} check found the provider never down"
        )
        assert restore.get("rest_answers_again") is True, (
            f"the {entry.name} check did not see the provider answer again"
        )


def require_cleaned(entry: Entry) -> None:
    """Fail unless the check reports, explicitly, in every space it ran in, that what it created is gone: an absent
    result is not a true one."""
    for facts in per_space_facts(entry):
        assert "cleanup_verified" in facts, f"the {entry.name} check reports no cleanup result"
        assert facts["cleanup_verified"] is True, f"the {entry.name} check did not show its scratch entry gone"
