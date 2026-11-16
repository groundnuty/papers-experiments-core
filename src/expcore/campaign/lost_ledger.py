# Provenance: new
"""The store's record of the spend a kill took out of it (`lost_in_kill`, O62): read, checked and exported.

`campaign.lost_spend` counts the spend journals into it and charges it; this module only reads it, so the export can
read it without the cost code. The record is one JSON object in the store's meta:

    {"campaign": <the campaign's fingerprint hash when its first journal was opened: every journal's campaign>,
     "journal": <the journal of the start that last opened the store, or null>,
     "counted": {<journal name>: {"counted_utc": ...,
                                  "attempts": [<an attempt's last journaled row, with "usd": its charge when counted>],
                                  "charges": [{"kind": "topup" | "probe", "model": ..., "usd": ..., "run_key"?}]}}}

`campaign` is kept from the first start that journaled, so a plan that grows (which records a new fingerprint, C06)
still reads its earlier journals as its own. A record that is not that shape cannot be read: `read` answers None,
which the budget gate takes as a total it cannot know (it holds every model), and the export refuses it.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

KEY = "lost_in_kill"
ATTEMPT_KEYS = (
    "id",
    "run_key",
    "model",
    "started_utc",
    "state",
    "reserved_capped",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "turn_usage",
    "usage_complete",
    "replies_checked",
    "topups",
    "topup_usd",
)
CHARGE_KINDS = ("topup", "probe")


class LostSpendError(RuntimeError):
    """The spend a kill lost cannot be read or reconciled: the message names the journal or the record, and why."""


@dataclass(frozen=True)
class Ledger:
    """The store's record: the journal of the start that last opened it, and each journal counted, by name, with what
    it lost (`attempts`: the attempts' last journaled rows; `charges`: `{kind, model, usd}`, a top-up's with its
    run)."""

    journal: str | None
    counted: Mapping[str, Mapping[str, Any]]
    campaign: str | None = None


def read(store: Any) -> Ledger | None:
    """The store's ledger (empty when it has none); None when it cannot be read. `store` answers `q`."""
    rows = store.q("SELECT value FROM meta WHERE key=?", (KEY,))
    return parse(rows[0][0] if rows else None)


def parse(raw: str | None) -> Ledger | None:
    """The ledger in the stored text (empty for none); None when it is not the record's shape."""
    if not raw:
        return Ledger(None, {})
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or set(value) != {"campaign", "journal", "counted"}:
        return None
    campaign, journal, counted = value["campaign"], value["journal"], value["counted"]
    if not isinstance(campaign, str) or not (journal is None or isinstance(journal, str)):
        return None
    if not isinstance(counted, dict) or not all(_counted_ok(entry) for entry in counted.values()):
        return None
    return Ledger(journal, counted, campaign)


def _counted_ok(entry: Any) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("attempts"), list):
        return False
    if not isinstance(entry.get("charges"), list):
        return False
    for attempt in entry["attempts"]:
        if not isinstance(attempt, dict) or set(ATTEMPT_KEYS) - set(attempt):
            return False
        if not isinstance(attempt["model"], str) or not isinstance(attempt["state"], str):
            return False
        if not dollars(attempt["topup_usd"]) or not (attempt.get("usd") is None or dollars(attempt["usd"])):
            return False
    for charge in entry["charges"]:
        if not isinstance(charge, dict) or charge.get("kind") not in CHARGE_KINDS:
            return False
        if not isinstance(charge.get("model"), str) or not dollars(charge.get("usd")):
            return False
    return True


def dollars(value: Any) -> bool:
    """Whether `value` is a finite number of dollars, not below zero."""
    finite = isinstance(value, int | float) and not isinstance(value, bool) and value == value
    return finite and 0 <= value < float("inf")


def exported(store: Any) -> list[dict[str, Any]] | None:
    """The export's `lost_in_kill`: every lost attempt and charge, journal by journal in name order, each with its
    `journal` and `kind` (`attempt`, `topup` or `probe`); None when nothing was lost. An attempt says whether the
    journal saw it end (`ended`), its state, the bound it was reserved at, its counts, its top-ups and `usd`, what it
    was charged when counted (None for a model without a price); a charge, its model and dollars. Raises
    LostSpendError for a record that cannot be read."""
    ledger = read(store)
    if ledger is None:
        raise LostSpendError(f"the store's {KEY} cannot be read")
    entries: list[dict[str, Any]] = []
    for name in sorted(ledger.counted):
        lost = ledger.counted[name]
        for attempt in lost["attempts"]:
            fields = {key: attempt[key] for key in ATTEMPT_KEYS if key not in ("id", "turn_usage")}
            entry = {"journal": name, "kind": "attempt", "attempt_id": attempt["id"], **fields}
            entries.append({**entry, "ended": attempt["state"] != "running", "usd": attempt.get("usd")})
        entries += [{"journal": name, **charge} for charge in lost["charges"]]
    return entries or None
