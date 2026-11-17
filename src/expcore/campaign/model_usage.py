# Provenance: new
"""What each model spent, by what became of the attempt, with the basis of every dollar (C39).

A campaign's outputs said what was scored and nothing of what it cost: the tokens, the dollars, the attempts that
were set aside or lost and the retries were in the store, the records and the budget gate, and in no report. `usage_of`
reads them from a snapshot of the store and breaks the budget gate's own figure down; nothing is priced a second time.

**Figures.** For each model of the plan, and for each class of attempt (`CLASSES`) that has one:
- the attempts and the tokens their rows recorded (input, output, cache writes and reads). A count the endpoint did not
  report is no 0: such an attempt is counted in `usage_incomplete_attempts` (a row whose totals are known to be short,
  one with a count missing, one still running), and the tokens are the sum of what was reported;
- the dollars, in `usd`: `measured` (each attempt's counts at the model's price, at the tier each request fell in, a
  free model's known zero), `measured_plus_inflight` (an interrupted attempt's replies, and a request that was sent and
  not answered at its bound), `bound` (the most the attempt's caps let it cost, because its usage is unknown), and
  `charges` (top-ups, each a request's bound, and the top-up and probe sends a kill lost), then their `total`. The
  basis is the one the dollars were charged on (`AttemptCost.charged_on`), never the one a row records: a row that says
  `measured` and is charged its bound is in `bound`. `bounded_attempts` counts the attempts whose charge holds a bound
  or a top-up, `unpriced_attempts` those of a model with no price, whose `usd` is then `None`;
- per model, `probes_usd` (what admission's probes were charged) and `usd`: `total`, `analysed` (the analysed
  attempts) and `not_analysed` (every other class and the probes), so the overhead is beside what scored runs cost.

The classes: `analysed` (the attempt of each analysed run), `left_out` (the done attempt of a run a partial analysis
leaves out), `infra_fail` and `outage_discarded` (set aside as infrastructure), `harness_error`, `interrupted`,
`running` and `lost_in_kill` (what the spend journal counted back after a kill, O62). An attempt that failed at start
and was charged a measured zero (C05) is an attempt of its class with 0 tokens and 0 measured dollars.

**Where dollars are unknown.** A dollar that cannot be known is `None`, never 0: a model with no price, prices that are
not the ones the campaign ran under (`Pricing.mismatch`, the caller's `why`), a bound that cannot be computed (a priced
model with no context window), a record of the probes or of a kill's loss that cannot be read. `dollars` says
whether the prices applied and, when they did not, why; the tokens are the store's either way.

A batch merged from shards adds its shards' figures (`merge_usage`). What the records say of the served model and the
retries is in `observed_by_model`, attached with `attach_observed`.
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any

from expcore.campaign.attempt_bounds import BILLED_STATES, LOST_STATE, AttemptCost
from expcore.campaign.interrupted_spend import BOUND, INFLIGHT, MEASURED
from expcore.campaign.selection_ledger import Pricing, Query, charges_of
from expcore.campaign.store import column_for

FORMAT = "model-usage-1"
CLASSES = (
    "analysed",
    "left_out",
    "infra_fail",
    "outage_discarded",
    "harness_error",
    "interrupted",
    "running",
    "lost_in_kill",
)
BUCKETS = (MEASURED, INFLIGHT, BOUND, "charges")  # where an attempt's dollars are counted, then their `total`
_TOKENS = ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens")
_COUNTS = ("attempts", *_TOKENS, "usage_incomplete_attempts", "bounded_attempts", "unpriced_attempts")


def usage_of(
    query: Query,
    pricing: Pricing | None,
    *,
    left_out: Collection[str] = (),
    why: str | None = None,
    models: Iterable[str] = (),
) -> dict[str, Any]:
    """What each model spent, from the store `query` reads (a store's or a snapshot's `q`), by class of attempt.

    `pricing` is the plan's models and caps, already shown to be the campaign's (`Pricing.mismatch`); None when they
    are not, with `why` saying so: the dollars are then unknown and the tokens are the store's. `left_out` are the run
    keys a partial analysis leaves out, whose done attempts are the class `left_out`.
    """
    rows = _attempt_rows(query)
    found, reason = None, why
    if pricing is not None:
        try:
            found = charges_of(query, pricing)
        except ValueError as error:  # a bound that cannot be computed (no context window, silly limits)
            reason = f"the bound of an attempt cannot be computed: {error}"
        else:
            if found is None:
                reason = "the store's record of its probes' charges or of what a kill lost cannot be read"
    elif reason is None:
        reason = "no prices were given"
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    by_id = {} if found is None else {c.attempt_id: c for c in found.costs if c.state != LOST_STATE}
    for row in rows:
        kind = _class_of(row["state"], row["run_key"], left_out)
        cell = _cell(cells, row["model"], kind)
        cell["attempts"] += 1
        for name in _TOKENS:
            cell[name] += row[name] or 0
        cell["usage_incomplete_attempts"] += int(_incomplete(row))
        if found is not None:
            _charge(cell, by_id.get(row["id"]))
    if found is not None:
        _add_lost(cells, found)
    entries = [_entry(model, pricing, cells, found) for model in _models(rows, pricing, models)]
    return {"format": FORMAT, "dollars": {"known": found is not None, "reason": reason}, "models": entries}


def _attempt_rows(query: Query) -> list[Any]:
    """Every attempt that has or may have spent tokens, with its model and its counts, in attempt order. A snapshot made
    before a column existed is read without it."""
    columns = {row["name"] for row in query("PRAGMA table_info(attempts)")}
    counts = ", ".join(f"a.{c}" if c in columns else f"NULL AS {c}" for c in _TOKENS)
    states = (*BILLED_STATES, "running")
    marks = ",".join("?" * len(states))
    return list(
        query(
            f"SELECT a.id, a.run_key, a.state, r.{column_for('model')} AS model, {counts}, "
            "COALESCE(a.usage_complete, 1) AS complete FROM attempts a JOIN runs r USING(run_key) "
            f"WHERE a.state IN ({marks}) ORDER BY a.id",
            states,
        )
    )


def _class_of(state: str, run_key: str, left_out: Collection[str]) -> str:
    """The class an attempt is shown under: a done attempt is analysed unless its run is left out of the analysis."""
    if state == "done":
        return "left_out" if run_key in left_out else "analysed"
    return state


def _incomplete(row: Any) -> bool:
    """Whether an attempt's usage is not known whole: its totals are known to be short, a count was not reported, or it
    has not ended."""
    unreported = row["input_tokens"] is None or row["output_tokens"] is None
    return row["state"] == "running" or not row["complete"] or unreported


def _blank() -> dict[str, Any]:
    return {
        **dict.fromkeys(_COUNTS, 0),
        "charged": False,  # whether any charge was added: a class of charges alone is listed
        "unknown": False,
        "buckets": dict.fromkeys(BUCKETS, 0.0),
    }


def _cell(cells: dict[tuple[str, str], dict[str, Any]], model: str, kind: str) -> dict[str, Any]:
    return cells.setdefault((model, kind), _blank())


def _charge(cell: dict[str, Any], cost: AttemptCost | None) -> None:
    """Add what an attempt was charged to its class: its basis's bucket without the top-ups, the top-ups apart. An
    attempt with no cost listed (a running one with no top-up) was charged nothing yet."""
    if cost is None:
        return
    cell["charged"] = True
    cell["bounded_attempts"] += int(cost.bounded)
    if cost.usd is None:
        cell["unpriced_attempts"] += 1
        cell["unknown"] = True
        return
    base = cost.usd - cost.topup_usd
    if cost.charged_on is not None:
        cell["buckets"][cost.charged_on] += base
    elif base:  # a charge with no basis that is not top-ups: never lose a dollar the gate counted
        cell["buckets"][BOUND] += base
    cell["buckets"]["charges"] += cost.topup_usd


def _add_lost(cells: dict[tuple[str, str], dict[str, Any]], found: Any) -> None:
    """The attempts and the charges a kill took out of the store, counted back from its spend journal (O62)."""
    for name in sorted(found.ledger.counted):
        for lost in found.ledger.counted[name]["attempts"]:
            cell = _cell(cells, lost["model"], "lost_in_kill")
            cell["attempts"] += 1
            for token in _TOKENS:
                cell[token] += lost.get(token) or 0
            unreported = lost.get("input_tokens") is None or lost.get("output_tokens") is None
            cell["usage_incomplete_attempts"] += int(lost["state"] == "running" or unreported or not _whole(lost))
    for cost in found.costs:
        if cost.state == LOST_STATE:
            _charge(_cell(cells, cost.model, "lost_in_kill"), cost)


def _whole(lost: Mapping[str, Any]) -> bool:
    return lost.get("usage_complete") is None or bool(lost["usage_complete"])


def _models(rows: Sequence[Any], pricing: Pricing | None, planned: Iterable[str]) -> list[str]:
    """The plan's models, and any other the store holds, sorted: a model with no attempt is listed with zeros."""
    named = {row["model"] for row in rows} | set(planned) | (set() if pricing is None else set(pricing.models))
    return sorted(named)


def _entry(
    model: str, pricing: Pricing | None, cells: Mapping[tuple[str, str], dict[str, Any]], found: Any
) -> dict[str, Any]:
    classes = []
    for kind in CLASSES:
        cell = cells.get((model, kind))
        if cell is None or not (cell["attempts"] or cell["charged"]):
            continue
        classes.append({"class": kind, **{name: cell[name] for name in _COUNTS}, "usd": _usd(cell, found)})
    probes = None if found is None else float(found.probes.get(model, 0.0))
    entry = {"model": model, "price": _price(model, pricing), "classes": classes, "probes_usd": probes}
    entry["usd"] = _model_usd(entry)
    return entry


def _price(model: str, pricing: Pricing | None) -> str | None:
    """Whether the model is `priced`, `free` or `unpriced` in the table the dollars were charged by; None if none."""
    if pricing is None:
        return None
    spec = pricing.models.get(model)
    if spec is None or (spec.price is None and not spec.free):
        return "unpriced"
    return "free" if spec.free else "priced"


def _usd(cell: Mapping[str, Any], found: Any) -> dict[str, float] | None:
    """A class's dollars by basis and in total; None when any of its attempts has no known cost, or without prices."""
    if found is None or cell["unknown"]:
        return None
    buckets = dict(cell["buckets"])
    return {**buckets, "total": float(sum(buckets.values()))}


def _model_usd(entry: Mapping[str, Any]) -> dict[str, float] | None:
    """A model's total, the part on its analysed attempts and the rest (the other classes and the probes); None unless
    every dollar of it is known."""
    probes = entry["probes_usd"]
    if probes is None or any(c["usd"] is None for c in entry["classes"]):
        return None
    analysed = float(sum(c["usd"]["total"] for c in entry["classes"] if c["class"] == "analysed"))
    other = float(sum(c["usd"]["total"] for c in entry["classes"] if c["class"] != "analysed")) + probes
    return {"total": analysed + other, "analysed": analysed, "not_analysed": other}


def prices_apply(pricing: Pricing, fingerprint: Mapping[str, Any] | None) -> str | None:
    """Why the study's prices cannot be taken for the ones a store's campaign ran under (`Pricing.mismatch`); None when
    they can. A store that records no campaign fingerprint has nothing to show it by, so no dollar is given for it."""
    if fingerprint is None:
        return "the store records no campaign fingerprint, so the study's prices cannot be shown to be the campaign's"
    return pricing.mismatch(fingerprint)


def usage_of_batch(
    stores: Sequence[tuple[Query, Mapping[str, Any] | None]],
    pricing: Pricing,
    *,
    left_out: Collection[str] = (),
    records: Iterable[Any] = (),
) -> dict[str, Any]:
    """What a batch spent and what served and retried its requests: each store's figures (a query over its snapshot,
    with the campaign fingerprint it records) added, in the order given, and what the verified `records` say attached.
    The study's prices apply to a store only where they are shown to be the campaign's (`prices_apply`)."""
    parts = []
    for query, fingerprint in stores:
        why = prices_apply(pricing, fingerprint)
        parts.append(usage_of(query, None if why else pricing, left_out=left_out, why=why, models=pricing.models))
    return attach_observed(merge_usage(parts), observed_by_model(records))


def merge_usage(parts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The figures of a batch merged from shards: the shards' added, class by class and model by model. A dollar is
    known only where every shard's is. The shards' order is the caller's, so a sum is the same bytes every time."""
    if len(parts) == 1:
        return dict(parts[0])
    unknown = [part["dollars"]["reason"] for part in parts if not part["dollars"]["known"]]
    entries: dict[str, dict[str, Any]] = {}
    for part in parts:
        for model in part["models"]:
            entries.setdefault(model["model"], {"model": model["model"], "price": model["price"], "parts": []})
            entries[model["model"]]["parts"].append(model)
    models = [_merged_entry(entry) for _, entry in sorted(entries.items())]
    dollars = {"known": not unknown, "reason": unknown[0] if unknown else None}
    return {"format": FORMAT, "dollars": dollars, "models": models}


def _merged_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    classes = []
    for kind in CLASSES:
        held = [c for part in entry["parts"] for c in part["classes"] if c["class"] == kind]
        if not held:
            continue
        merged = {"class": kind, **{name: sum(c[name] for c in held) for name in _COUNTS}}
        merged["usd"] = None if any(c["usd"] is None for c in held) else _sum_usd([c["usd"] for c in held])
        classes.append(merged)
    probes = [part["probes_usd"] for part in entry["parts"]]
    merged_entry = {
        "model": entry["model"],
        "price": entry["price"],
        "classes": classes,
        "probes_usd": None if any(p is None for p in probes) else float(sum(probes)),
    }
    merged_entry["usd"] = _model_usd(merged_entry)
    return merged_entry


def _sum_usd(parts: Iterable[Mapping[str, float]]) -> dict[str, float]:
    held = list(parts)
    return {name: float(sum(p[name] for p in held)) for name in (*BUCKETS, "total")}


# ----------------------------------------------------------------------------------------- what the records say


def observed_by_model(records: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """What the verified records say of each model: how many records, the served model ids and system fingerprints its
    turns carried (the turns and the records each served, a turn that carried neither is `None` for both), and the
    retries its requests made (the events by kind with their waits, the turns that retried). A record is read for its
    `factors["model"]` and its `transcript`; one that is not in the batch is not here, so a model with none has none."""
    found: dict[str, dict[str, Any]] = {}
    for record in records:
        model = str(record.factors["model"])
        entry = found.setdefault(model, {"records": 0, "turns": 0, "served": {}, "retries": {}, "turns_retried": 0})
        entry["records"] += 1
        seen = set()
        for turn in record.transcript:
            entry["turns"] += 1
            pair = (turn.get("served_model"), turn.get("system_fingerprint"))
            served = entry["served"].setdefault(pair, {"turns": 0, "records": 0})
            served["turns"] += 1
            if pair not in seen:
                seen.add(pair)
                served["records"] += 1
            events = turn.get("retries") or []
            entry["turns_retried"] += int(bool(events))
            for event in events:
                kind = entry["retries"].setdefault(str(event.get("kind")), {"events": 0, "wait_s": 0.0})
                kind["events"] += 1
                kind["wait_s"] += float(event.get("wait_s") or 0.0)
    return found


def attach_observed(usage: Mapping[str, Any], observed: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The figures with, for each model, what its records say (`observed_by_model`): `records`, `turns`, `served` (the
    served model ids and fingerprints with their turns and records, in order) and `retries` (`events`, `turns_retried`,
    `wait_s` and `by_kind`). A model with no record has zeros and an empty list, never a missing key."""
    models = []
    for entry in usage["models"]:
        seen = observed.get(entry["model"], {})
        served = sorted(
            seen.get("served", {}).items(), key=lambda item: tuple(("" if p is None else p) for p in item[0])
        )
        by_kind = sorted(seen.get("retries", {}).items())
        models.append(
            {
                **entry,
                "records": seen.get("records", 0),
                "turns": seen.get("turns", 0),
                "served": [
                    {"served_model": model, "system_fingerprint": fingerprint, **counts}
                    for (model, fingerprint), counts in served
                ],
                "retries": {
                    "events": sum(v["events"] for _, v in by_kind),
                    "turns_retried": seen.get("turns_retried", 0),
                    "wait_s": sum(v["wait_s"] for _, v in by_kind),
                    "by_kind": [{"kind": kind, **counts} for kind, counts in by_kind],
                },
            }
        )
    return {**usage, "models": models}
