# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, day0/scripts/quick_screen.py:1-60
#   merged: icsa-2027-experiments @ 6fc798cf, day0/scripts/odsafe.py:1-17
"""The day-0 checks that attempt a change: read-only enforcement, and the placement round trip on a scratch file.

Both run only when the caller allowed mutation (`EXPCORE_LIVE_MUTATE=1` on the command line); without it each returns
unknown and sends nothing. A write that is expected to be refused still counts: if the enforcement were broken it would
succeed, so it is gated like any other change.

Everything they create is under a configured space, by the provisioner's scoped client, with a name of its own, and is
removed in a `finally` whose result is then verified (O11): a scratch entry that is still there fails the check even
when everything else passed. With several spaces each runs in every space it is meant for, and the verdict is the worst
over them: read-only enforcement in each space the data path reaches, the round trip in each declared space.
"""

import hashlib
import secrets
from typing import Any

from expcore_onedata import consistency
from expcore_onedata.client import NotSent, PlatformError, answered_absent
from expcore_onedata.config import StudySpace
from expcore_onedata.day0.registry import Context, Result, failed, passed, per_space, register, unknown
from expcore_onedata.provision import Provisioner, ProvisionError
from expcore_onedata.rangemap import RangeRow
from expcore_onedata.ranges import place_ranges
from expcore_onedata.readretry import ONE_ATTEMPT
from expcore_onedata.tokencheck import TokenRefused
from expcore_onedata.tokens import Plane, data_path_spec

SCRATCH_BYTES = 8192
REFUSALS = (401, 403)
POSIX_REFUSAL = (400, "posix", "eacces")  # how Onedata 25.0 refuses a write made with a read-only token ([L1])
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
    polls, within a bounded number of polls. "Not found" is a 404 or Onedata 25.0's 400 posix enoent
    (`answered_absent`), exactly; any other answer, another 400 included, is not, and the reads go on to their bound.
    Any other outcome is `cleaned` False with a `cleanup_reason`. The absence is that provider's (#114).
    """
    try:
        ctx.client.mutate("DELETE", f"/data/{entry_id}", provider=provider)
    except PlatformError as error:
        if not answered_absent(error):  # already gone is confirmed by the reads below
            return {"cleaned": False, "cleanup_reason": "the delete was refused or failed"}
    absent, reason = 0, "the entry could not be read to confirm that it is gone"
    for poll in range(ABSENCE_POLLS):
        if poll:
            ctx.sleep(ABSENCE_POLL_S)
        try:
            ctx.client.get(f"/data/{entry_id}", provider=provider, retry=ONE_ATTEMPT)
            absent, reason = 0, "the entry was still there after the delete"
        except PlatformError as error:
            if not answered_absent(error):
                absent, reason = 0, "the entry could not be read to confirm that it is gone"
                continue
            absent, reason = absent + 1, "the entry was not found on a read, but not on consecutive reads"
            if absent >= CONSECUTIVE_ABSENT:
                return {"cleaned": True}
    return {"cleaned": False, "cleanup_reason": reason}


def _refused(error: PlatformError) -> bool:
    """Whether a provider refused the write: a 401 or 403, or exactly `POSIX_REFUSAL`; no other 400 is a refusal."""
    return error.status in REFUSALS or (error.status, error.code, error.errno) == POSIX_REFUSAL


def _attempt_write(ctx: Context, name: str, space_id: str, token: str) -> dict[str, Any]:
    """Send one write as the data-path token to a provider, at the root of a space, and say what became of it.

    `sent` is False when nothing reached the provider: the space root could not be read, or the client's gate refused
    the write or could not check it (`NotSent`), so the probe did not run there. A write that was sent is accepted
    (`enforced` False, and removed again), refused (`enforced` True: a 401 or 403, or exactly `POSIX_REFUSAL`, as
    Onedata 25.0 answers it), or neither (`enforced` None: no answer, or another status, kept with Onedata's error id
    as `code` and its errno as `errno`)."""
    created: str | None = None
    try:
        root = consistency.space_root(
            ctx.client, name, space_id, tries=3, wait_s=1.0, sleep=ctx.sleep, retry=ctx.plumbing_retry()
        )
    except PlatformError as error:
        return {"enforced": None, "sent": False, "error": "the space root could not be read", "status": error.status}
    try:
        answer = ctx.client.mutate(
            "POST",
            f"/data/{root}/children",
            provider=name,
            params={"name": _scratch_name(), "type": "DIR"},
            token=token,
        )
        created = answer.get("fileId") if isinstance(answer, dict) else None
        row: dict[str, Any] = {"enforced": False, "sent": True, "status": "accepted"}
    except NotSent as error:  # the gate's own refusal or failed lookup: no write reached the provider
        row = {"enforced": None, "sent": False, "error": str(error)[:200]}
    except PlatformError as error:
        if error.transport or error.status is None:
            row = {"enforced": None, "sent": True, "error": "no answer"}
        else:
            enforced = True if _refused(error) else None
            row = {"enforced": enforced, "sent": True, "status": error.status, "code": error.code}
            row.update({"errno": error.errno} if error.errno else {})
    finally:
        if created:  # the write was accepted: remove what it made, as the admin, and show that it is gone
            row.update(_remove_and_verify(ctx, name, created))
    return row


def _answered(row: dict[str, Any]) -> str:
    """How a sent write was answered, for a summary: its status, Onedata's error id and errno, or that none came."""
    if "status" not in row:
        return str(row.get("error"))
    shown = f"{row['code']}: {row['errno']}" if row.get("code") and row.get("errno") else row.get("code")
    return f"{row['status']} {shown}" if shown else str(row["status"])


@register("read_only", "Read-only enforcement on each data path", mutates=True)
def read_only(ctx: Context) -> Result:
    """Send a write as the data-path token to each supporting provider; every one must refuse it.

    It passes only when every provider refused a write it was sent. A write that was never sent measured nothing, so
    the probe could not run there and the check is unknown with that reason, never a refusal it could not read (O08).

    The token is the one a run mints, scoped to every space the data path reaches, and with several spaces the writes go
    to the root of each of those spaces, on the providers of its own support set; the verdict is the worst over them
    (`per_space`). A space the data path does not reach is not written to: the token's scope would refuse the write
    there, which says nothing of its read-only caveat."""
    if not ctx.allow_mutation:
        return unknown(f"the write attempt {NEEDS_CONSENT}")
    if ctx.config.principals.data_path is None:
        return unknown("no fixed [principals] data-path user is declared, so no token can be minted")
    try:
        token = ctx.issuer.mint(
            data_path_spec(ctx.config.space_ids(Plane.DATA_PATH)),
            ctx.config.principals.data_path,
            ceiling_s=ctx.ceiling_s,
        )
    except (TokenRefused, PlatformError) as error:
        return unknown(f"the probe could not run: no data-path token was minted, so no write was sent ({error})"[:300])
    reached = [entry for entry in ctx.config.spaces if Plane.DATA_PATH in entry.tokens]
    return per_space(ctx, reached, lambda entry: _enforced_in(ctx, entry, token.reveal()))


def _enforced_in(ctx: Context, entry: StudySpace, token: str) -> Result:
    """Read-only enforcement in one space: a write to its root on each provider of its support set."""
    rows = {name: _attempt_write(ctx, name, entry.space.id, token) for name in entry.support.providers}
    if any(row["enforced"] is False for row in rows.values()):
        return failed("a write made with the read-only data-path token was ACCEPTED", providers=rows)
    unsent = [name for name, row in rows.items() if row["sent"] is False]
    unanswered = [name for name, row in rows.items() if row["sent"] is True and row["enforced"] is None]
    reasons = []
    if unsent:
        first = rows[unsent[0]]["error"]
        reasons.append(f"the probe could not run on {', '.join(unsent)}: no write was sent ({first})")
    if unanswered:
        answers = sorted({_answered(rows[name]) for name in unanswered})
        reasons.append(
            f"a write was sent to {', '.join(unanswered)} and answered neither by acceptance nor by a refusal "
            f"(401, 403 or 400 posix: eacces), but {', '.join(answers)}"
        )
    if reasons:
        return unknown("; ".join(reasons), providers=rows)
    return passed(f"the write was refused on all {len(rows)} supporting provider(s)", providers=rows)


def _holder(ctx: Context, entry: StudySpace) -> str | None:
    """A space's data provider, as its provisioner takes it: a `[[spaces]]` entry's own, else `[placement] holder`."""
    if ctx.config.spaces_table:
        return entry.holder
    return ctx.placement.settings.holder if ctx.placement else None


@register("round_trip", "Placement round trip on a scratch file", mutates=True)
def round_trip(ctx: Context) -> Result:
    """Create a scratch file, place half of it on another provider by range reads, confirm it by the block map, read it
    back and compare its hash, evict it again, and tear the scratch down, verifying that it is gone (O11).

    The placement passes when the receiver's block map holds a range that covers the half read; a provider may place
    more (Onedata 25.0 placed the whole 8 KiB file), so the facts give the bytes and ranges held beside the bytes read
    (`range_bytes`), and how many evictions it took (`eviction_attempts`).

    With several spaces the trip runs in each declared space, since each has its own data provider and support set:
    its own scratch file, placed from that space's data provider onto the first other provider of its support set, by
    a provisioner bound to that space with a change log of its own. The verdict is the worst over them (`per_space`)."""
    if not ctx.allow_mutation:
        return unknown(f"the round trip {NEEDS_CONSENT}")
    if ctx.placement is None:
        return unknown("platform.toml has no [placement] table, so the data provider is not declared")
    return per_space(ctx, ctx.config.spaces, lambda entry: _round_trip_in(ctx, entry))


def _round_trip_in(ctx: Context, entry: StudySpace) -> Result:
    """The round trip in one declared space."""
    holder = _holder(ctx, entry)
    receiver = next((p for p in entry.support.providers if p != holder), None)
    if receiver is None:
        return unknown("the support set has no provider besides the data provider to place bytes on")
    missing = [p for p in (holder, receiver) if p not in ctx.provider_ids]
    if missing:
        return unknown(
            "a provider's id is neither declared in platform.toml nor read from the space", missing=len(missing)
        )
    provisioner = ctx.provisioner(entry.label)
    data, path = _content(), f"{_scratch_name()}/scratch.bin"
    half = len(data) // 2
    facts: dict[str, Any] = {"bytes": len(data), "range_bytes": half, "steps": []}
    steps: list[str] = facts["steps"]
    failure: str | None = None
    try:
        provisioner.verify_space()
        file_id = provisioner.put_file(path, data)
        steps.append("created")
        row = RangeRow(path, receiver, ((0, half),), space=entry.label)
        record = place_ranges(provisioner, [row], start_empty=True)
        placed = record["providers"][provisioner.client.provider_label(receiver)]
        held = placed["per_file"][path]
        facts.update(held_bytes=held["held_bytes"], held_ranges=held["held"], polls=placed["polls"])
        facts["held_covers_range"] = any(start <= 0 and half <= start + length for start, length in held["held"])
        steps.append("placed")
        back = provisioner.fetch(receiver, file_id, 0, half)
        facts["bytes_match"] = hashlib.sha256(back).digest() == hashlib.sha256(data[:half]).digest()
        steps.append("read back")
        facts["eviction_attempts"] = provisioner.evict(path, receiver)["attempts"]
        steps.append("evicted")
    except (ProvisionError, PlatformError, consistency.WaitTimeout) as error:
        failure = f"{type(error).__name__} after {steps[-1] if steps else 'nothing'}"
    finally:
        cleanup = _clean_up(provisioner, path)
    facts["cleanup_verified"] = cleanup
    if failure is not None:
        return failed(f"the round trip failed ({failure}); scratch {'removed' if cleanup else 'NOT removed'}", **facts)
    if not cleanup:
        return failed("the round trip worked but the scratch it made was not removed and shown gone (O11)", **facts)
    if facts["bytes_match"] is not True or facts["held_covers_range"] is not True:
        return failed("the bytes read back, or the ranges held, do not cover what was placed", **facts)
    return passed("placed by range reads, confirmed by the block map, read back, evicted and removed", **facts)


def _clean_up(provisioner: Provisioner, path: str) -> bool:
    """Tear down what the provisioner logged and verify that the scratch is gone and the log holds nothing."""
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
