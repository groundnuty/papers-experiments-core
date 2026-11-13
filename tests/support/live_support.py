"""What the live contract tests share with the offline tests that check them: minting a condition's token."""

from collections.abc import Mapping
from typing import Any

import pytest

from expcore_onedata.client import PlatformError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.credentials import Principal
from expcore_onedata.day0.outage import prerequisite as outage_prerequisite
from expcore_onedata.day0.outage_agent import mount_levels
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
        missing = outage_prerequisite(ctx)
        if missing is None and not mount_levels(ctx):  # the issue asks for the mount's answers: a live run needs one
            return "the provider_outage check's live run needs a [sandbox.data_paths] level that maps to the mount"
        return missing
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


def require_measured(entry: Entry) -> None:
    """Fail unless the outage check measured, in every space, the mount (the platform must declare one) and the
    agent's knowledge tools: the issue asks for both, and a pass without them would be the REST path's alone."""
    for facts in per_space_facts(entry):
        assert (facts.get("mount") or {}).get("measured") is True, f"the {entry.name} check did not read the mount"
        knowledge = facts.get("knowledge") or {}
        assert knowledge.get("measured") is True, f"the {entry.name} check did not call the knowledge tools"


def require_cleaned(entry: Entry) -> None:
    """Fail unless the check reports, explicitly, in every space it ran in, that what it created is gone: an absent
    result is not a true one."""
    for facts in per_space_facts(entry):
        assert "cleanup_verified" in facts, f"the {entry.name} check reports no cleanup result"
        assert facts["cleanup_verified"] is True, f"the {entry.name} check did not show its scratch entry gone"


# -- a read-only condition's tool server, live (O37) -------------------------------------------------------------------

VIEW_TOOLS = (
    "get_transfer",
    "list_space_transfers",
    "get_qos_requirement",
    "get_file_qos_summary",
    "get_file_distribution",
)
WRITE_TOOLS = ("schedule_file_replication", "add_file_qos_requirement")
# What an argument is, from a word its name holds, tried in this order, so that a name holding two words is the more
# specific one's: a state before a transfer (`transfer_state`), a transfer or a requirement before a file, and a
# provider before a count of replicas (Onedata names a replication's target `replicatingProviderId`).
ARGUMENT_KINDS = (
    ("state", "state"),
    ("transfer", "transfer"),
    ("requirement", "requirement"),
    ("expression", "expression"),
    ("provider", "provider"),
    ("replica", "replicas"),
    ("file", "file"),
    ("space", "space"),
)


def argument_kind(name: str) -> str | None:
    """What a tool's argument is, from its name; None when its name says none of `ARGUMENT_KINDS`."""
    word = name.lower()
    return next((kind for key, kind in ARGUMENT_KINDS if key in word), None)


def tool_arguments(schema: Mapping[str, Any] | None, values: Mapping[str, Any]) -> dict[str, Any] | str:
    """The arguments of a call from the tool's own input schema: each property whose name says a kind `values` has
    gets that value, as text when the schema types it a string. The tools' names, descriptions and arguments are the
    server's and are never assumed here. Return the reason, naming the properties, when a required one cannot be
    filled: such a tool is unmeasured, never called with a guess."""
    properties = (schema or {}).get("properties") or {}
    required = list((schema or {}).get("required") or [])
    filled: dict[str, Any] = {}
    for name, spec in properties.items():
        kind = argument_kind(name)
        if kind not in values:
            continue
        value = values[kind]
        if isinstance(spec, Mapping) and spec.get("type") == "string" and not isinstance(value, str):
            value = str(value)
        filled[name] = value
    unfilled = sorted(name for name in required if name not in filled)
    if unfilled:
        return f"its input schema requires {', '.join(unfilled)}, which the test cannot fill"
    return filled


def refused(error: PlatformError) -> bool:
    """Whether a platform error is the refusal of a request, as Onedata 25.0 answers one: 401, 403, or 400 `posix`
    `eacces` (`.claude/rules/onedata-25.md`). Any other answer, another 400 or a 404 among them, is not one, so a
    write that fails for another reason is never taken for a refused one."""
    return error.status in (401, 403) or (error.status, error.code, error.errno) == (400, "posix", "eacces")
