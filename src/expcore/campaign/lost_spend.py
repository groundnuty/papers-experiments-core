# Provenance: new
"""What a campaign spent that a kill took out of its store, read from the spend journal and counted once (O62).

A store copied back from a snapshot (`deploy/hpc-federation/shards.py`) has lost what its dispatcher committed after
the copy: attempts that began, top-ups and admission's probe sends, all billed. The spend journal
(`campaign.spend_journal`) holds them. A start given `--spend-journal FILE` counts, before its probes and its first
dispatch, every journal in FILE's directory against the store (`reconcile`), and records what the store lacks in its
meta under `lost_in_kill` (`campaign.lost_ledger`), in one transaction with the name of this start's journal:

- **the store's own journal**, the one the start that last opened this store wrote (the ledger's `journal`): each
  attempt it lists is matched to the store's row of the same id, run key, start and pid. An attempt the store lacks is
  lost; one it holds is the store's, and only the top-ups the journal lists past the row's are lost. A probe charge is
  the store's when its event (by id and model) is; any other is lost;
- **every other journal not yet counted**: the store holds none of it. A start counts every journal it finds before
  it writes its own, so every journal older than the store's own was counted into it already; one the ledger has not
  counted was written by a start whose store never reached this copy. Every attempt and probe charge of it is lost.

Each journal is counted once and the ledger says so (`counted`, by name), so a later start of this store, or of a copy
taken after this start, counts nothing twice; a copy taken before it counts the same journals again, rightly, since
that copy lacks them. One journal's probe charges are summed per model and never below 0 (a settlement whose charge the
store holds leaves the store's figure an over-count, which stands). A model a counted journal took off the reply
maximum is taken off it in the store as well, in the same transaction: the mark raises the bound its attempts are
charged, those the store holds included, and the copy may predate it.

**Refused**, naming what, before anything is changed: a ledger that cannot be read (what it holds would be lost); a
journal of another campaign or shard, or one that is not a journal (`spend_journal.read_journal`); a store whose own
journal is not in the directory, since what its start spent after the copy would then be unknown. A refused start
leaves no journal of its own behind.

**What is charged** (`costs`): a lost attempt as `attempt_bounds.attempt_costs` charges a row, at the budget gate's
every assessment: one whose end the journal did not see at its reserved bound (R, C27, by the window once its model
is taken off the reply maximum), one that ended at its measured cost (the provider's counts) or, when they are not
complete, its bound; its top-ups on top. A lost top-up or probe charge at its dollars. Each is an `AttemptCost` of
state `lost_in_kill`, so the gate counts it against the store's ceilings (a shard's are its shares, #154) and
`selection_ledger.spend_of` in what the store spent. A ledger that cannot be read holds every model, as the probes'
record does.
"""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.campaign import reply_cap
from expcore.campaign.attempt_bounds import LOST_STATE, AttemptCost, row_cost
from expcore.campaign.lost_ledger import ATTEMPT_KEYS, KEY, Ledger, LostSpendError, dollars, parse, read
from expcore.campaign.spend_journal import Journal, SpendJournal, SpendJournalError, journals_in, read_journal
from expcore.campaign.store import Store
from expcore.config.models import Limits, ModelSpec


@dataclass(frozen=True)
class Pricing:
    """The plan's models by key and the caps every attempt runs under: what a lost attempt is charged by."""

    models: Mapping[str, ModelSpec]
    limits: Limits


PROBE_EVENTS = ("probe_charged", "probe_settled")

__all__ = ["KEY", "LostSpendError", "Pricing", "costs", "open_journal", "read", "reconcile"]


# ------------------------------------------------------------------------------------------------ the reconciliation
def open_journal(
    store: Store, path: Path, campaign: str, shard: str | None, now: str, pricing: Pricing
) -> dict[str, Any]:
    """Create this start's journal at `path`, count every other journal of its directory (`reconcile`) and journal
    the store's spend from here on; what was counted. `campaign` is the hash of the fingerprint the store records;
    the campaign of every journal is the one the ledger kept from its first start, when it has one (a plan that grew
    records a new fingerprint). A refused start leaves no journal of its own behind."""
    ledger = read(store)
    if ledger is None:
        raise LostSpendError(f"the store's {KEY} cannot be read, so the spend a kill lost cannot be counted")
    journal = SpendJournal(path, ledger.campaign or campaign, shard, now)
    try:
        counted = reconcile(store, journal, now, pricing)
    except BaseException:
        journal.close()
        path.unlink(missing_ok=True)
        raise
    store.spend_journal = journal
    return counted


def reconcile(store: Store, journal: SpendJournal, now: str, pricing: Pricing) -> dict[str, Any]:
    """Count every journal of `journal`'s directory that the store's ledger has not, and record this start's as the
    store's own, in one transaction; return `{"journals", "attempts", "charges_usd"}` of what was counted now.
    Each lost attempt keeps what it was charged when counted (`usd`, by `pricing`: the plan's models and the caps,
    with the models taken off the reply maximum), for the export. A model a counted journal took off the reply maximum
    is taken off it in the store too (C27): the mark raises what its attempts are charged, the store's own included.
    Raises LostSpendError, or SpendJournalError for a journal that cannot be read, and changes nothing."""
    ledger = read(store)
    if ledger is None:
        raise LostSpendError(f"the store's {KEY} cannot be read, so the spend a kill lost cannot be counted")
    if ledger.campaign is not None and ledger.campaign != journal.campaign:
        raise LostSpendError(f"the journal {journal.name} is of campaign {journal.campaign}, not the store's")
    others = journals_in(journal.path.parent, but=journal.name)
    if ledger.journal is not None and ledger.journal not in {path.name for path in others}:
        raise LostSpendError(
            f"the store's own spend journal {ledger.journal} is not in the journal directory, so what its start "
            "spent after this store was copied is unknown; give the directory that holds it"
        )
    uncounted = [read_journal(p, journal.campaign, journal.shard) for p in others if p.name not in ledger.counted]
    unheld = sorted({model for journaled in uncounted for model in journaled.unheld})  # marks the copy may lack
    marks = reply_cap.unheld(store)
    taken_off = frozenset(m for m in pricing.models if reply_cap.is_unheld(marks, m) or m in unheld)
    found: dict[str, dict[str, Any]] = {}
    for journaled in uncounted:
        lost = _lost(store, journaled, own=journaled.name == ledger.journal, now=now)
        for attempt in lost["attempts"]:
            attempt["usd"] = _attempt_cost(attempt, pricing.models, pricing.limits, (), taken_off)[0]
        found[journaled.name] = lost

    def change(current: str | None) -> tuple[str, list[tuple[str, str | None, str]], None]:
        latest = parse(current)
        if latest is None or latest != ledger:
            raise LostSpendError(f"the store's {KEY} changed while it was reconciled")
        value = {"campaign": journal.campaign, "journal": journal.name, "counted": {**latest.counted, **found}}
        events = [("lost_in_kill", None, _detail(name, lost)) for name, lost in found.items() if _any(lost)]
        return json.dumps(value, sort_keys=True), events, None

    def record() -> None:  # the ledger and every mark the journals hold, in one transaction
        store.update_meta(KEY, change, now)
        for model in unheld:
            if not reply_cap.is_unheld(reply_cap.unheld(store), model):
                detail = f"{model}: taken off the reply maximum by a start whose store a kill lost (the spend journal)"
                reply_cap.mark_unheld(store, model, now, "reply_cap_lost_in_kill", detail)

    store.transact(record)
    charges: dict[str, float] = {}
    for lost in found.values():
        for charge in lost["charges"]:
            charges[charge["model"]] = charges.get(charge["model"], 0.0) + charge["usd"]
    return {
        "journals": len(found),
        "attempts": sum(len(lost["attempts"]) for lost in found.values()),
        "charges_usd": dict(sorted(charges.items())),
    }


def _lost(store: Store, journal: Journal, *, own: bool, now: str) -> dict[str, Any]:
    """What one journal lists that the store lacks: every attempt and probe charge of a journal not the store's own,
    and of its own those the store's rows and events do not hold."""
    rows = {r["id"]: r for r in store.q("SELECT id, run_key, started_utc, pid, topups, topup_usd FROM attempts")}
    marks = ",".join("?" * len(PROBE_EVENTS))
    events = {
        r["id"]: r["model"] for r in store.q(f"SELECT id, model FROM events WHERE kind IN ({marks})", PROBE_EVENTS)
    }
    attempts, charges = [], []
    for attempt_id, entry in journal.attempts.items():
        _check_attempt(journal.name, entry)
        row = rows.get(attempt_id) if own else None
        same = row is not None and (row["run_key"], row["started_utc"], row["pid"]) == (
            entry["run_key"],
            entry["started_utc"],
            entry["pid"],
        )
        if not same:
            attempts.append({name: entry[name] for name in ATTEMPT_KEYS})
        elif entry["topups"] > row["topups"] and entry["topup_usd"] > row["topup_usd"]:
            usd = float(entry["topup_usd"]) - float(row["topup_usd"])
            charges.append({"kind": "topup", "model": entry["model"], "run_key": entry["run_key"], "usd": usd})
    probes: dict[str, float] = {}
    for entry in journal.probes:
        model, usd = entry.get("model"), entry.get("usd")
        if not isinstance(model, str) or isinstance(usd, bool) or not isinstance(usd, int | float) or usd != usd:
            raise SpendJournalError(f"{journal.name}: a probe line has no model or no dollars")
        if own and events.get(entry["event_id"]) == model:  # the store holds the charge's event
            continue
        probes[model] = probes.get(model, 0.0) + float(usd)
    charges += [{"kind": "probe", "model": m, "usd": usd} for m, usd in sorted(probes.items()) if usd > 0]
    return {"counted_utc": now, "attempts": attempts, "charges": charges}


def _check_attempt(name: str, entry: Mapping[str, Any]) -> None:
    missing = [key for key in (*ATTEMPT_KEYS, "pid") if key not in entry]
    if missing:
        raise SpendJournalError(f"{name}: an attempt line lacks {', '.join(missing)}")
    topups = entry["topups"]
    if isinstance(topups, bool) or not isinstance(topups, int) or topups < 0 or not dollars(entry["topup_usd"]):
        raise SpendJournalError(f"{name}: attempt {entry['id']}'s top-ups are not a count and dollars")
    if not isinstance(entry["model"], str) or not isinstance(entry["state"], str):
        raise SpendJournalError(f"{name}: attempt {entry['id']} has no model or no state")


def _any(lost: Mapping[str, Any]) -> bool:
    return bool(lost["attempts"] or lost["charges"])


def _detail(name: str, lost: Mapping[str, Any]) -> str:
    usd = sum(charge["usd"] for charge in lost["charges"])
    return (
        f"{name}: {len(lost['attempts'])} attempt(s) and {usd:.6f} USD of top-ups and probe sends the store lacked, "
        "counted from the spend journal"
    )


# ------------------------------------------------------------------------------------------------ what it costs
def costs(
    ledger: Ledger,
    models: Mapping[str, ModelSpec],
    limits: Limits,
    *,
    reply_capped: Collection[str] = (),
    taken_off: Collection[str] = (),
) -> list[AttemptCost]:
    """What the ledger charges, as costs of state `lost_in_kill`: each lost attempt as its row would be charged (its
    bound when the journal never saw it end), its top-ups added, and each top-up and probe charge at its dollars."""
    out = []
    for name in sorted(ledger.counted):
        lost = ledger.counted[name]
        for attempt in lost["attempts"]:
            usd, bounded = _attempt_cost(attempt, models, limits, reply_capped, taken_off)
            out.append(AttemptCost(attempt["id"], attempt["run_key"], attempt["model"], LOST_STATE, usd, bounded))
        for charge in lost["charges"]:
            run_key = str(charge.get("run_key", ""))
            out.append(AttemptCost(0, run_key, charge["model"], LOST_STATE, float(charge["usd"]), True))
    return out


def _attempt_cost(
    attempt: Mapping[str, Any],
    models: Mapping[str, ModelSpec],
    limits: Limits,
    reply_capped: Collection[str],
    taken_off: Collection[str],
) -> tuple[float | None, bool]:
    """A lost attempt's cost, its top-ups included, as its row would be charged; None for an unpriced model."""
    row = {**attempt, "complete": 1 if attempt["usage_complete"] is None else attempt["usage_complete"]}
    usd, bounded = row_cost(
        models.get(attempt["model"]),
        row,
        limits,
        running=attempt["state"] == "running",
        reply_capped=reply_capped,
        taken_off=taken_off,
    )
    topped = float(attempt["topup_usd"])
    return (None if usd is None else usd + topped), bounded or topped > 0
