# Provenance: new
"""The shortcut of a reset's occupancy and quiet stages: re-read only what may have changed since the planner's reading
(O46, O51).

The occupancy stage and the quiet stage each read every listed file's block map at every provider that can hold the
space's bytes (`occupancy.holdings`): about 106 s a pass for the first study's large space. Given a planner's reading
(`SpaceReset.plan_scan`, each file's bytes at each provider, made before the eviction), they need re-read only:

- **the files the reading showed holding bytes** at that provider, and those it has no figure for (a file the scan after
  a removal found);
- **every file a transfer touched since the reading** at every provider: each job the ended listings show finished since
  the reading's start (this host's epoch clock, less `clock_skew_allowance_s` and `transferorder.STOP_MARGIN_S`, the
  global clock placed by Onezone's reading as the history stage places it), and each job the transfers stage cancelled,
  by the `fileId` its record names; the reset's own evictions excepted, whose files are the next item;
- **the files the reset evicted** and **every file a QoS requirement of the planner's scan applies to**, kept or
  removed: a QoS replication leaves no transfer record;
- **every hard link**: it shows its target's blocks under its own id, so a transfer of the target is a transfer of it.

Every other file is carried as holding nothing there (`occupancy.Reading.carried`). **The premise:** nothing placed
bytes since the reading but what the transfer listings show. An on-demand read places bytes and leaves no transfer
record, so the reading and the reset must both be made inside the reset lock's exclusive section, which no reader holds
beside (`resetgrant.py`); a reading made outside a live section, or with `[placement] reset_shortcut` off, gets the full
reads. A job from before the section is listed within `transfer_settle_s` of its scheduling (O07), so every one of them
is listed by the transfers stage's last observation, cancelled or ended; the walk is made at least `transfer_settle_s`
after that observation (`SpaceReset._occupancy_due`), by when an ended job is on the ended listing.

**It falls back to the full reads, never failing the reset**, when: the section's hold has lapsed before the occupancy
stage (`grant_lapsed`); a job since the reading names a file that is no listed file, a directory or the root
(`transfer_not_on_a_file`); or the listings, a record or the global clock cannot be read (`transfers_unread`, with the
error's type). The record (`occupancy_reads`) says the mode, why, the grant (`resetgrant.ResetGrant.facts`), the
reading's start and size, and what was re-read. **A hold that lapses after the narrowed reads** (the admission let go
during the reset) fails the stage that finds it: those reads certify nothing.
"""

from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Any

from expcore_onedata import consistency
from expcore_onedata.client import PlatformError
from expcore_onedata.occupancy import Listing
from expcore_onedata.provision import ProvisionError
from expcore_onedata.rangemap import PlacementSettings
from expcore_onedata.transferrecord import Unreadable

if TYPE_CHECKING:  # the reset imports this module; only the interfaces are used here
    from expcore_onedata.reset import SpaceReset
    from expcore_onedata.resetplan import Holdings

SHORTCUT, FULL = "shortcut", "full"  # the mode of the occupancy and quiet stages' reads
OFF, NOT_PLANNED, OUTSIDE_GRANT = "off", "not_planned", "outside_grant"
GRANT_LAPSED, NOT_ON_A_FILE, TRANSFERS_UNREAD = "grant_lapsed", "transfer_not_on_a_file", "transfers_unread"
UNREAD = (Unreadable, PlatformError, consistency.WaitTimeout)  # a walk that cannot be read: the full reads


def initial(settings: PlacementSettings, planned: "Holdings | None") -> dict[str, Any]:
    """The mode a reset starts with, and the record of it: the shortcut only with the setting on and a planner's reading
    made inside an exclusive section that still holds (`resetgrant.ResetGrant.shortcut`)."""
    grant = None if planned is None else planned.grant
    reads: dict[str, Any] = {
        "mode": FULL,
        "why": None,
        "grant": None if grant is None else grant.facts(),
        "reading": None if planned is None else _reading_facts(planned),
    }
    if not settings.reset_shortcut:
        reads["why"] = OFF
    elif planned is None:
        reads["why"] = NOT_PLANNED
    elif grant is None or planned.read_epoch is None:
        reads["why"] = OUTSIDE_GRANT
    elif grant.why is not None:
        reads["why"] = grant.why
    elif not grant.held:
        reads["why"] = OUTSIDE_GRANT
    else:
        reads["mode"] = SHORTCUT
    return reads


def _reading_facts(planned: "Holdings") -> dict[str, Any]:
    """The reading's start on this host's epoch clock and its size: the files it listed, the (file, provider) pairs it
    read and those that held bytes."""
    return {
        "read_epoch": planned.read_epoch,
        "files": len(planned.scan.listing.ids()),
        "pairs_read": sum(len(held) for held in planned.held.values()),
        "held_pairs": sum(1 for held in planned.held.values() for n in held.values() if n),
    }


def files_to_read(
    listing: Listing, reading: Mapping[str, Mapping[str, int]], touched: Collection[str]
) -> dict[str, frozenset[str]]:
    """For each provider the reading read, the listed ids to read there: those it showed holding bytes, those it has no
    figure for, every hard link and every id `touched`. A provider the reading did not read is not in the answer, and
    gets the full reads."""
    always = frozenset(touched) | listing.links
    return {
        provider: frozenset(i for i in listing.ids() if held.get(i) != 0 or i in always)
        for provider, held in reading.items()
    }


def narrowed(reset: "SpaceReset", run: dict[str, Any]) -> dict[str, frozenset[str]] | None:
    """The ids the occupancy and quiet stages read at each provider (`files_to_read`), or None for the full reads: the
    mode is not the shortcut, or it falls back now, which `run["reads"]` then says (mutated on purpose: it is the
    record)."""
    reads = run["reads"]
    if reads["mode"] != SHORTCUT:
        return None
    planned: Holdings = run["planned"]
    if not planned.grant.held:
        return _fall_back(reads, GRANT_LAPSED)
    listing: Listing = run["files"]
    try:
        jobs = reset._ended_since(planned.read_epoch, run["cancelled"])
    except UNREAD as error:
        return _fall_back(reads, TRANSFERS_UNREAD, type(error).__name__)
    own = run["own"] - {tid for _, tid in run["cancelled"]}  # the evictions the reset sent: their files come below
    moved = {file_id for tid, file_id in jobs.items() if tid not in own}
    if any(file_id not in listing.regular and file_id not in listing.links for file_id in moved):
        return _fall_back(reads, NOT_ON_A_FILE)
    required = {file_id for files in run["scan"].files.values() for file_id in files} if run["scan"] else set()
    evicted = {file_id for file_id, _ in run["evicted"]}
    only = files_to_read(listing, planned.held, moved | required | evicted)
    reads["reread"] = {
        "transferred": len(moved),
        "required": len(required),
        "evicted": len(evicted),
        "links": len(listing.links),
        "pairs": sum(len(ids) for ids in only.values()),
    }
    return only


def _fall_back(reads: dict[str, Any], why: str, error: str | None = None) -> None:
    reads.update(mode=FULL, why=why, **({"error": error} if error else {}))
    reads.pop("reread", None)
    return None


def require_held(run: dict[str, Any]) -> None:
    """After reads narrowed by the shortcut: refuse when the exclusive hold that made them sound has lapsed."""
    if run["reads"]["mode"] == SHORTCUT and not run["planned"].grant.held:
        raise ProvisionError(
            "the reset lock's exclusive hold lapsed during a shortcut reset: a reader may have placed bytes on a file "
            "it did not read again, so its reading certifies nothing"
        )


__all__ = [
    "FULL",
    "GRANT_LAPSED",
    "NOT_ON_A_FILE",
    "NOT_PLANNED",
    "OFF",
    "OUTSIDE_GRANT",
    "SHORTCUT",
    "TRANSFERS_UNREAD",
    "files_to_read",
    "initial",
    "narrowed",
    "require_held",
]
