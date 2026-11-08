# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/attempt_bounds.py, blob aa745b2e86b2bd41637333a5a7ec1a9256542c1f
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""The token budget an attempt ran under, from a store record (science, #34, 4 Nov 00:32Z).

An attempt killed before its outcome was recorded (an `interrupted` attempt: usage_complete 0, no counts, no transcript or usage log) is charged amendment 26(c)'s bound, one
attempt's token budget at the dearest rate (progress.bounded_usage). That budget is the harness config's NOW, and the config's value has moved (4,000,000 in the pilot, 12,190,000
in main): the ruling prices such an attempt at "the token budget then in force", the one it ran under. This record says which, per attempt, with who set it and from what source.
It is read fresh at every call, like the endpoint caps (endpoint_caps.py): by the spend alarm and the model_66 gate through `progress.bounded_usage`, never cached, so a record
written while the dispatcher runs applies at the next evaluation with no restart. `python -m dataset_76.campaign price-attempt` writes it.

    meta `attempt_bounds` = {"<attempts.id>": {"token_budget": <int > 0>, "who": <who set it>, "source": <who said so, where>, "set_utc": <UTC>}, ...}

Keyed by the attempt's row id (an interrupted attempt and its re-run share one attempt number). An entry applies to an attempt that bounded_usage charges for incomplete usage: billed
(progress.billed_cond) with usage_complete 0. Any other entry, and any record that cannot be read, is IGNORED with a reason (never a crash, never a zero bound): the attempt is then
charged at the harness config's budget, which is the dearer bound. A record only ever replaces the budget; the attempt's own per-call records still come first when the harness left any.
"""
from __future__ import annotations

import json
from typing import Any, NamedTuple

from .store import Store

META = "attempt_bounds"
FIELDS = ("token_budget", "who", "source", "set_utc")


class Bounds(NamedTuple):
    present: bool                       # a record exists in the state (readable or not)
    entries: dict[int, dict[str, Any]]  # attempt id -> the entry as stored, with the attempt's run_key, model and state added: what is applied
    ignored: list[str]                  # one reason per entry (or per record) that is not applied

    def budgets(self) -> dict[int, int]:
        return {i: e["token_budget"] for i, e in self.entries.items()}


MAX_BUDGET = 10 ** 18 - 1       # 18 digits, as the command takes: a larger figure prices to a Fraction no status can show as a float, and no run budget is near it


def _is_budget(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 < v <= MAX_BUDGET


def shape_problems(entry: Any) -> list[str]:
    """Why an entry is no record of a budget: not an object, an unknown field, a budget that is not a whole number above 0 (a bool or a float is not one), no who, source or time."""
    if not isinstance(entry, dict):
        return ["the entry is not an object"]
    out = [f"unknown field {k!r}" for k in entry if k not in FIELDS]
    if not _is_budget(entry.get("token_budget")):
        out.append(f"token_budget {str(entry.get('token_budget'))[:30]!r} is not a positive whole number up to {MAX_BUDGET}")
    out += [f"{k} is missing or empty" for k in ("who", "source", "set_utc") if not (isinstance(entry.get(k), str) and entry[k].strip())]
    return out


def attempt_problems(store: Store, billed: str, attempt_id: int) -> tuple[dict[str, Any] | None, str | None]:
    """(the attempt's run_key, model and state, None) when a bound can apply to it: it exists, bills (`billed`: a SQL tuple of states, see progress.billed_cond) and its usage is
    incomplete. Otherwise (None, why)."""
    from .progress import billed_cond
    rows = store.q(f"SELECT a.id, a.run_key, a.state, runs.model, COALESCE(a.usage_complete,1) uc, ({billed_cond(billed)}) bills FROM attempts a JOIN runs USING(run_key) WHERE a.id=?",
                   [attempt_id])
    if not rows:
        return None, f"no such attempt {attempt_id}"
    r = rows[0]
    if not r["bills"]:
        return None, f"attempt {attempt_id} ({r['run_key']}, {r['state']}) does not bill: a bound is for an attempt that did"
    if r["uc"] != 0:
        return None, f"attempt {attempt_id} ({r['run_key']}, {r['state']}) has complete usage: its recorded counts are the cost, a bound is for an attempt with incomplete usage"
    return {"run_key": r["run_key"], "model": r["model"], "state": r["state"]}, None


def parse(raw: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """(the record's entries, None), or (None, why the record cannot be read at all). No record at all is ({}, None)."""
    if raw is None:
        return {}, None
    try:
        rec = json.loads(raw)
    except ValueError as e:
        return None, f"the record is not JSON ({e})"
    if not isinstance(rec, dict):
        return None, "the record is not an object"
    return rec, None


def read(store: Store, billed: str) -> Bounds:
    """The record now in the state, entry by entry: what applies, and why each other entry does not. Reads the state on every call. `billed` is the caller's own SQL tuple of
    billed states (the gate's and the spend alarm's differ by `running`), so an entry applies to exactly the attempts that caller charges for incomplete usage."""
    raw = store.get_meta(META)
    rec, bad = parse(raw)
    if rec is None:
        return Bounds(True, {}, [f"attempt_bounds: {bad}, so no entry of it applies"])
    entries: dict[int, dict[str, Any]] = {}
    ignored: list[str] = []
    for key, entry in rec.items():
        if not (isinstance(key, str) and key.isascii() and key.isdigit() and len(key) <= 18 and key == str(int(key)) and int(key) > 0):       # 18 digits: within SQLite's integer
            ignored.append(f"attempt_bounds[{key!r}] ignored: the key is not an attempt id")
            continue
        why = shape_problems(entry)
        info = None
        if not why:
            info, w = attempt_problems(store, billed, int(key))
            why = [w] if w else []
        if why:
            ignored.append(f"attempt_bounds[{key}] ignored: " + "; ".join(why))
        else:
            entries[int(key)] = {**entry, **(info or {})}
    return Bounds(raw is not None, entries, ignored)
