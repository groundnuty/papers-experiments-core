# Provenance: new
"""What each model spent, by what became of the attempt, with the basis of every dollar (C39).

A campaign's outputs said what was scored and nothing of what it cost: the tokens, the dollars, the attempts that
were set aside or lost and the retries were in the store, the records and the budget gate, and in no report. `usage_of`
reads them from a snapshot of the store and breaks the budget gate's own figure down; nothing is priced a second time.

**Figures.** For each model of the plan, and for each class of attempt (`CLASSES`) that has one:
- the attempts and the tokens their rows recorded (input, output, cache writes and reads). A count the endpoint did not
  report is no 0: such an attempt is counted in `usage_incomplete_attempts` (a row whose totals are known to be short,
  one with a count missing, one still running), and the tokens are the sum of what was reported. An interrupted attempt
  has no counts in its row: where its call log is trusted (its recorded basis is still the log's, as the gate charges
  it) the tokens are the replies' it recorded, so they agree with the dollars, and it is incomplete only if a request
  was in flight or closed by a failed send; a lost attempt's are the replies its journal recorded;
- the dollars, in `usd`: `measured` (each attempt's counts at the model's price, at the tier each request fell in, a
  free model's known zero), `measured_plus_inflight` (an interrupted attempt's replies, and a request that was sent and
  not answered at its bound), `bound` (the most the attempt's caps let it cost, because its usage is unknown), and
  `charges` (top-ups, each a request's bound, and the top-ups a kill lost), then their `total`. The
  basis is the one the dollars were charged on (`AttemptCost.charged_on`), never the one a row records: a row that says
  `measured` and is charged its bound is in `bound`. `bounded_attempts` counts the attempts whose charge holds a bound
  or a top-up, `unpriced_attempts` those of a model with no price, whose `usd` is then `None`;
- per model, `probes_usd` (what admission's probes were charged, and the probe sends a kill lost) and `usd`:
  `total`, `analysed` (the analysed attempts) and `not_analysed` (every other class and the probes), so the overhead is
  beside what scored runs cost.

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

from expcore.campaign.attempt_bounds import BILLED_STATES, LOST_STATE, AttemptCost, known_no_turn
from expcore.campaign.interrupted_spend import BOUND, INFLIGHT, MEASURED, CallLog, basis_of, read_log, trusted_log
from expcore.campaign.lost_ledger import Ledger
from expcore.campaign.lost_ledger import read as read_ledger
from expcore.campaign.selection_ledger import Pricing, Query, charges_of, store_view
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
_LOG = ("calls_sent", "calls_usage", "spend_basis")  # an interrupted attempt's call log and the basis it records
_TURNS = ("turn_usage", "replies_checked")  # what says an attempt is known to have had no turn
_COUNTS = ("attempts", *_TOKENS, "usage_incomplete_attempts", "bounded_attempts", "unpriced_attempts")
_DECIMALS = 12  # a sum of dollars is kept to a millionth of a millionth: no float noise in the bytes of an output


def _money(value: float) -> float:
    return round(float(value), _DECIMALS)


def _seconds(value: float) -> float:
    """Seconds of waiting to a millisecond, so a sum carries no float noise into the bytes of an output."""
    return round(float(value), 3)


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
    ledger = read_ledger(store_view(query))  # what a kill lost: its tokens are the store's whatever the prices are
    found, reason = None, why
    if pricing is not None:
        try:
            found = charges_of(query, pricing)
        except (ValueError, TypeError, KeyError) as error:  # a bound that cannot be computed, or a row it cannot read
            reason = f"the charges cannot be computed: {error}"
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
        counts, incomplete = _counted(row)
        for name, count in zip(_TOKENS, counts, strict=True):
            cell[name] += count
        cell["usage_incomplete_attempts"] += int(incomplete)
        if found is not None:
            _charge(cell, by_id.get(row["id"]))
    lost_probes: dict[str, float] = {}
    if ledger is not None:
        _add_lost(cells, lost_probes, ledger, found)
    named = _models(rows, pricing, models, ledger)
    entries = [_entry(model, pricing, cells, found, lost_probes) for model in named]
    return {"format": FORMAT, "dollars": {"known": found is not None, "reason": reason}, "models": entries}


def _attempt_rows(query: Query) -> list[Any]:
    """Every attempt that has or may have spent tokens, with its model and its counts, in attempt order. A snapshot made
    before a column existed is read without it."""
    columns = {row["name"] for row in query("PRAGMA table_info(attempts)")}
    counts = ", ".join(f"a.{c}" if c in columns else f"NULL AS {c}" for c in (*_TOKENS, *_LOG, *_TURNS))
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


def _counted(row: Any) -> tuple[tuple[int, ...], bool]:
    """The four token counts of an attempt and whether its usage is not known whole: the row's, or for an interrupted
    attempt whose call log the gate trusts, the replies' that log recorded."""
    log = trusted_log(row) if row["state"] == "interrupted" else None
    if log is not None:
        return _log_counts(log)
    return tuple(row[name] or 0 for name in _TOKENS), _incomplete(row)


def _log_counts(log: CallLog) -> tuple[tuple[int, ...], bool]:
    """The sum of the counts of the replies a call log holds; whole only if no request is in flight and none was closed
    by a failed send (its tokens are unknown: its top-up paid for it) or a reply that could not be read."""
    totals = [0, 0, 0, 0]
    for entry in log.answered:
        if isinstance(entry, tuple):
            for place, count in enumerate(entry):
                totals[place] += count or 0
    return tuple(totals), log.in_flight > 0 or not all(isinstance(entry, tuple) for entry in log.answered)


def _incomplete(row: Any) -> bool:
    """Whether an attempt's usage is not known whole: its totals are known to be short, a count was not reported, or it
    has not ended."""
    unreported = (row["input_tokens"] is None or row["output_tokens"] is None) and not known_no_turn(row)
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


def _add_lost(
    cells: dict[tuple[str, str], dict[str, Any]], probes: dict[str, float], ledger: Ledger, found: Any
) -> None:
    """The attempts a kill took out of the store and their tokens, counted back from its spend journal (O62), whatever
    the prices; and, where dollars are known, what they and the lost charges were charged: a top-up in the class, a
    probe send among the model's probes."""
    for name in sorted(ledger.counted):
        entry = ledger.counted[name]
        for lost in entry["attempts"]:
            cell = _cell(cells, lost["model"], "lost_in_kill")
            cell["attempts"] += 1
            counts, incomplete = _lost_counted(lost)
            for token, count in zip(_TOKENS, counts, strict=True):
                cell[token] += count
            cell["usage_incomplete_attempts"] += int(incomplete)
        if found is None:
            continue
        for charge in entry["charges"]:
            usd = float(charge["usd"])
            if charge["kind"] == "probe":
                probes[charge["model"]] = probes.get(charge["model"], 0.0) + usd
            else:
                cell = _cell(cells, charge["model"], "lost_in_kill")
                cell["charged"] = True
                cell["buckets"]["charges"] += usd
    if found is not None:
        for cost in found.costs:
            if cost.state == LOST_STATE and cost.attempt_id != 0:  # a lost attempt; a lost charge is above
                _charge(_cell(cells, cost.model, "lost_in_kill"), cost)


def _lost_counted(lost: Mapping[str, Any]) -> tuple[tuple[int, ...], bool]:
    """The tokens of a lost attempt and whether they are not whole: the counts its journal saw it end with, or, for one
    whose end it did not see, the replies of its journaled call log when that log is whole enough to charge by."""
    if lost["state"] == "running":
        log = read_log(lost.get("calls_sent"), lost.get("calls_usage"))
        if log is not None and basis_of(log) in (MEASURED, INFLIGHT):
            return _log_counts(log)
    unreported = (lost.get("input_tokens") is None or lost.get("output_tokens") is None) and not known_no_turn(lost)
    whole = lost.get("usage_complete") is None or bool(lost["usage_complete"])
    return tuple(lost.get(token) or 0 for token in _TOKENS), lost["state"] == "running" or unreported or not whole


def _models(rows: Sequence[Any], pricing: Pricing | None, planned: Iterable[str], ledger: Ledger | None) -> list[str]:
    """The plan's models, and any other the store or its ledger of a kill's losses holds, sorted: a model with no
    attempt is listed with zeros."""
    named = {row["model"] for row in rows} | set(planned) | (set() if pricing is None else set(pricing.models))
    for entry in () if ledger is None else ledger.counted.values():
        named |= {lost["model"] for lost in entry["attempts"]} | {charge["model"] for charge in entry["charges"]}
    return sorted(named)


def _entry(
    model: str,
    pricing: Pricing | None,
    cells: Mapping[tuple[str, str], dict[str, Any]],
    found: Any,
    lost_probes: Mapping[str, float],
) -> dict[str, Any]:
    price = _price(model, pricing)
    classes = []
    for kind in CLASSES:
        cell = cells.get((model, kind))
        if cell is None or not (cell["attempts"] or cell["charged"]):
            continue
        usd = None if price == "unpriced" else _usd(kind, cell, found)  # nothing priced a model with no price
        classes.append({"class": kind, **{name: cell[name] for name in _COUNTS}, "usd": usd})
    probes = None
    if found is not None and price != "unpriced":
        probes = _money(found.probes.get(model, 0.0) + lost_probes.get(model, 0.0))
    entry = {"model": model, "price": price, "classes": classes, "probes_usd": probes}
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


def _usd(kind: str, cell: Mapping[str, Any], found: Any) -> dict[str, float | None] | None:
    """A class's dollars by basis and in total; None when any of its attempts has no known cost, or without prices.
    An attempt still running has no spend of its own known yet: only the top-ups the gate charged it are."""
    if found is None or cell["unknown"]:
        return None
    buckets = {name: _money(value) for name, value in cell["buckets"].items()}
    if kind == "running":
        return {MEASURED: None, INFLIGHT: None, BOUND: None, "charges": buckets["charges"], "total": None}
    return {**buckets, "total": _money(sum(buckets.values()))}


def _gate_counted(usd: Mapping[str, float | None], kind: str) -> float:
    """What the gate counts of a class: its total, and for the attempts still running their top-ups alone."""
    return float(usd["charges"] if kind == "running" else usd["total"])


def _model_usd(entry: Mapping[str, Any]) -> dict[str, float] | None:
    """A model's total, the part on its analysed attempts and the rest (the other classes and the probes); None unless
    every dollar of it is known."""
    probes = entry["probes_usd"]
    if probes is None or any(c["usd"] is None for c in entry["classes"]):
        return None
    classes = [(c["class"], _gate_counted(c["usd"], c["class"])) for c in entry["classes"]]
    analysed = _money(sum(usd for kind, usd in classes if kind == "analysed"))
    other = _money(sum(usd for kind, usd in classes if kind != "analysed") + probes)
    return {"total": _money(analysed + other), "analysed": analysed, "not_analysed": other}


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
    known only where every shard's is: when any shard's are not, no class, probe or total dollar of the merge is, and
    the tokens are still all the shards'. The shards' order is the caller's, so a sum is the same bytes every time."""
    if len(parts) == 1:
        return dict(parts[0])
    unknown = [part["dollars"]["reason"] for part in parts if not part["dollars"]["known"]]
    entries: dict[str, list[Mapping[str, Any]]] = {}
    for part in parts:
        for model in part["models"]:
            entries.setdefault(model["model"], []).append(model)
    models = [_merged_entry(name, held, known=not unknown) for name, held in sorted(entries.items())]
    dollars = {"known": not unknown, "reason": unknown[0] if unknown else None}
    return {"format": FORMAT, "dollars": dollars, "models": models}


def _merged_entry(name: str, parts: Sequence[Mapping[str, Any]], *, known: bool) -> dict[str, Any]:
    classes = []
    for kind in CLASSES:
        held = [c for part in parts for c in part["classes"] if c["class"] == kind]
        if not held:
            continue
        merged = {"class": kind, **{count: sum(c[count] for c in held) for count in _COUNTS}}
        merged["usd"] = None if not known or any(c["usd"] is None for c in held) else _sum_usd([c["usd"] for c in held])
        classes.append(merged)
    probes = [part["probes_usd"] for part in parts]
    prices = {part["price"] for part in parts}
    entry = {
        "model": name,
        "price": prices.pop() if len(prices) == 1 else None,  # shards that disagree on it show none
        "classes": classes,
        "probes_usd": None if not known or any(p is None for p in probes) else _money(sum(probes)),
    }
    entry["usd"] = _model_usd(entry)
    return entry


def _sum_usd(parts: Iterable[Mapping[str, float | None]]) -> dict[str, float | None]:
    """The dollars of several classes added bucket by bucket; a bucket that is None in any (an attempt still running has
    no spend of its own known) is None in the sum."""
    held = list(parts)
    return {
        name: None if any(p[name] is None for p in held) else _money(sum(p[name] for p in held))
        for name in (*BUCKETS, "total")
    }


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
                kind = entry["retries"].setdefault(str(event.get("kind") or "unknown"), {"events": 0, "wait_s": 0.0})
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
                    "wait_s": _seconds(sum(v["wait_s"] for _, v in by_kind)),
                    "by_kind": [
                        {"kind": kind, "events": counts["events"], "wait_s": _seconds(counts["wait_s"])}
                        for kind, counts in by_kind
                    ],
                },
            }
        )
    return {**usage, "models": models}
