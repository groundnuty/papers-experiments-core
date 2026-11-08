# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, day0/scripts/quick_screen.py:1-60
#   merged: icsa-2027-experiments @ 6fc798cf, day0/scripts/odsafe.py:1-17
"""The day-0 checks that attempt a change: read-only enforcement, and the placement round trip on a scratch file.

Both run only when the caller allowed mutation (`EXPCORE_LIVE_MUTATE=1` on the command line); without it each returns
unknown and sends nothing. A write that is expected to be refused still counts: if the enforcement were broken it would
succeed, so it is gated like any other change.

Everything they create is under the configured space, by the provisioner's scoped client, with a name of its own, and is
removed in a `finally` whose result is then verified (O11): a scratch entry that is still there fails the check even
when everything else passed.
"""

import hashlib
import secrets
from typing import Any

from expcore_onedata import consistency
from expcore_onedata.client import PlatformError
from expcore_onedata.day0.registry import Context, Result, failed, passed, register, unknown
from expcore_onedata.provision import ProvisionError
from expcore_onedata.rangemap import RangeRow
from expcore_onedata.ranges import place_ranges
from expcore_onedata.tokens import data_path_spec

SCRATCH_BYTES = 8192
REFUSALS = (401, 403)
ABSENCE_POLLS = 6  # reads of a deleted entry before its absence is given up on
ABSENCE_POLL_S = 1.0
CONSECUTIVE_ABSENT = 2  # reads in a row that must answer "not found" (O07)
NEEDS_CONSENT = "needs mutation consent (EXPCORE_LIVE_MUTATE=1), so nothing was sent"


def _scratch_name() -> str:
    """A name no study file will have: the check, then random hex."""
    return f"day0-{secrets.token_hex(6)}"


def _content() -> bytes:
    """Deterministic bytes in which every aligned 4-byte word differs, so a misplaced range changes the hash."""
    return b"".join(i.to_bytes(4, "big") for i in range(SCRATCH_BYTES // 4))


def _remove_and_verify(ctx: Context, provider: str, entry_id: str) -> dict[str, Any]:
    """Delete an entry as the admin and return `cleaned`, true only when its absence was then confirmed (O11).

    A DELETE that answers 204 proves nothing: the entry is read until the provider answers "not found" on consecutive
    polls, within a bounded number of polls. Any other outcome is `cleaned` False with a `cleanup_reason`.
    """
    try:
        ctx.client.mutate("DELETE", f"/data/{entry_id}", provider=provider)
    except PlatformError:
        return {"cleaned": False, "cleanup_reason": "the delete was refused or failed"}
    absent, reason = 0, "the entry could not be read to confirm that it is gone"
    for poll in range(ABSENCE_POLLS):
        if poll:
            ctx.sleep(ABSENCE_POLL_S)
        try:
            ctx.client.get(f"/data/{entry_id}", provider=provider)
            absent, reason = 0, "the entry was still there after the delete"
        except PlatformError as error:
            if error.status != consistency.NOT_FOUND:
                absent, reason = 0, "the entry could not be read to confirm that it is gone"
                continue
            absent, reason = absent + 1, "the entry was not found on a read, but not on consecutive reads"
            if absent >= CONSECUTIVE_ABSENT:
                return {"cleaned": True}
    return {"cleaned": False, "cleanup_reason": reason}


@register("read_only", "Read-only enforcement on each data path", mutates=True)
def read_only(ctx: Context) -> Result:
    """Send a write as the data-path token to each supporting provider; every one must refuse it."""
    if not ctx.allow_mutation:
        return unknown(f"the write attempt {NEEDS_CONSENT}")
    if ctx.config.principals.data_path is None:
        return unknown("no fixed [principals] data-path user is declared, so no token can be minted")
    space = ctx.config.space
    token = ctx.issuer.mint(data_path_spec(space.id), ctx.config.principals.data_path, ceiling_s=ctx.ceiling_s)
    rows: dict[str, Any] = {}
    for name in ctx.config.support.providers:
        created: str | None = None
        try:
            root = consistency.space_root(ctx.client, name, space.id, tries=3, wait_s=1.0, sleep=ctx.sleep)
            try:
                answer = ctx.client.mutate(
                    "POST",
                    f"/data/{root}/children",
                    provider=name,
                    params={"name": _scratch_name(), "type": "DIR"},
                    token=token.reveal(),
                )
                created = answer.get("fileId") if isinstance(answer, dict) else None
                rows[name] = {"enforced": False, "status": "accepted"}
            except PlatformError as error:
                if error.transport or error.status is None:
                    rows[name] = {"enforced": None, "error": "no answer"}
                else:
                    rows[name] = {"enforced": True if error.status in REFUSALS else None, "status": error.status}
        except PlatformError as error:
            rows[name] = {"enforced": None, "error": "the space root could not be read", "status": error.status}
        finally:
            if created:  # the write was accepted: remove what it made, as the admin, and show that it is gone
                rows[name].update(_remove_and_verify(ctx, name, created))
    if any(row["enforced"] is False for row in rows.values()):
        return failed("a write made with the read-only data-path token was ACCEPTED", providers=rows)
    if any(row["enforced"] is None for row in rows.values()):
        return unknown("a write was not refused with a 401 or 403, and its refusal could not be read", providers=rows)
    return passed(f"the write was refused on all {len(rows)} supporting provider(s)", providers=rows)


def _receiver(ctx: Context) -> str | None:
    """The first provider of the support set that is not the data provider, or None."""
    holder = ctx.placement.settings.holder if ctx.placement else None
    return next((p for p in ctx.config.support.providers if p != holder), None)


@register("round_trip", "Placement round trip on a scratch file", mutates=True)
def round_trip(ctx: Context) -> Result:
    """Create a scratch file, place half of it on another provider by range reads, confirm it by the block map, read it
    back and compare its hash, evict it again, and tear the scratch down, verifying that it is gone (O11)."""
    if not ctx.allow_mutation:
        return unknown(f"the round trip {NEEDS_CONSENT}")
    if ctx.placement is None:
        return unknown("platform.toml has no [placement] table, so the data provider is not declared")
    receiver = _receiver(ctx)
    if receiver is None:
        return unknown("the support set has no provider besides the data provider to place bytes on")
    missing = [p for p in (ctx.placement.settings.holder, receiver) if p not in ctx.provider_ids]
    if missing:
        return unknown(
            "a provider's id is neither declared in platform.toml nor read from the space", missing=len(missing)
        )
    provisioner = ctx.provisioner()
    data, path = _content(), f"{_scratch_name()}/scratch.bin"
    half = len(data) // 2
    facts: dict[str, Any] = {"bytes": len(data), "range_bytes": half, "steps": []}
    steps: list[str] = facts["steps"]
    failure: str | None = None
    try:
        provisioner.verify_space()
        file_id = provisioner.put_file(path, data)
        steps.append("created")
        record = place_ranges(provisioner, [RangeRow(path, receiver, ((0, half),))], start_empty=True)
        placed = record["providers"][receiver]
        facts.update(held_bytes=placed["held_after"], polls=placed["polls"])
        steps.append("placed")
        back = provisioner.fetch(receiver, file_id, 0, half)
        facts["bytes_match"] = hashlib.sha256(back).digest() == hashlib.sha256(data[:half]).digest()
        steps.append("read back")
        provisioner.evict(path, receiver)
        steps.append("evicted")
    except (ProvisionError, PlatformError, consistency.WaitTimeout) as error:
        failure = f"{type(error).__name__} after {steps[-1] if steps else 'nothing'}"
    finally:
        cleanup = _clean_up(ctx, path)
    facts["cleanup_verified"] = cleanup
    if failure is not None:
        return failed(f"the round trip failed ({failure}); scratch {'removed' if cleanup else 'NOT removed'}", **facts)
    if not cleanup:
        return failed("the round trip worked but the scratch it made was not removed and shown gone (O11)", **facts)
    if facts["bytes_match"] is not True or facts["held_bytes"] != half:
        return failed("the bytes read back, or the bytes held, are not what was placed", **facts)
    return passed("placed by range reads, confirmed by the block map, read back, evicted and removed", **facts)


def _clean_up(ctx: Context, path: str) -> bool:
    """Tear down what the provisioner logged and verify that the scratch is gone and the log holds nothing."""
    provisioner = ctx.provisioner()
    try:
        report = provisioner.teardown()
        gone = False
        try:
            provisioner.resolve(path)
        except KeyError:
            gone = True
        return bool(report.ok and gone and not provisioner.logged())
    except (ProvisionError, PlatformError, consistency.WaitTimeout):
        return False
