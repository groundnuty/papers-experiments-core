# Provenance: new
"""How the Markdown report (`expcore.report`) says what each model spent, and what served and retried its requests.

A section "Model usage and spend" follows the conditions (and the shards of a merged batch). It is given the figures of
`expcore.campaign.model_usage` (`usage_of`, with what the records say attached), which are the budget gate's own, and
shows, per model:

- **Spend by model**: what it spent in all, on the attempts the analysis uses and on the rest, and on each basis, so
  the overhead is beside what scored runs cost. A dollar not known is `n/a`, never 0.
- **Attempts, tokens and spend by what became of the attempt**: for each class of attempt that has one, its attempts,
  tokens and dollars on each basis (measured, measured plus a request in flight, bound, top-ups).
- **Attempts set aside as infrastructure**, by kind, from the export's `retries_by_kind`.
- **Requests retried inside an attempt**, and **Served model ids and fingerprints**, from the verified records.

The notes under the heading say what each basis means; the provenance list has one line (`provenance_line`) with the
total, the part that is measured and the part that is a bound. A report given no figures has none of this and is the
text it was.
"""

import textwrap
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from expcore.campaign.model_usage import BUCKETS, FORMAT
from expcore.report_notes import UNKNOWN

HEADING = "## Model usage and spend"
NOT_REPORTED = "not reported"
NONE = "-"  # the cells of a model's row of zeros
LABELS = {
    "analysed": "analysed runs",
    "left_out": "runs left out of the analysis",
    "infra_fail": "infrastructure failures",
    "outage_discarded": "attempts an outage overlapped",
    "harness_error": "harness errors",
    "interrupted": "interrupted attempts",
    "running": "attempts still running",
    "lost_in_kill": "attempts lost in a kill",
}
NOTES = textwrap.wrap(
    "What each model cost, as the budget gate counts it (the same figures, read from the campaign's snapshot of its "
    "store), by what became of each attempt. Tokens are the counts the store recorded. A count the endpoint did not "
    "report is not 0: the attempt is counted in Usage incomplete (its totals are known to be short, a count is "
    "missing, or it was still running). Each attempt's dollars are on one basis. Measured: its counts at the model's "
    "price, each request at the tier its prompt fell in (a free model's zero is measured too). Measured plus in "
    "flight: an interrupted attempt's replies, and a request sent and not answered at the most one request can cost. "
    "Bound: the most its caps let it cost, because its usage is unknown. Top-ups: a send the token cap could not "
    "account for, charged one request's bound when it was reserved and never refunded. Probes: the sends of "
    "admission's probes, charged at the dearest rates of the counts their replies reported, or a send's bound. "
    "Charged at a bound counts the attempts whose charge holds a bound or a top-up. An attempt that failed before any "
    "request was sent and was charged a measured zero has 0 tokens and 0 dollars. n/a: not known, because the model "
    "has no price, the prices are not the ones the campaign ran under, a bound cannot be computed or a record cannot "
    "be read; it is never 0. Spent is the model's total, On analysed attempts that of the attempts the analysis uses, "
    "and On the rest everything else (attempts that were never scored, runs left out of a partial analysis, what a "
    "kill lost, probes), so what scored runs cost can be read beside the overhead.",
    width=118,
)
ATTEMPT_NOTES = textwrap.wrap(
    "Every attempt of the export's `retries_by_kind`, added over the conditions; Charged says whether it used one of "
    "the run's attempts. A model with none has a row of zeros.",
    width=118,
)
REQUEST_NOTES = textwrap.wrap(
    "From the records the batch holds: each retry the model's requests made before a reply, by the kind of error, and "
    "the seconds it waited. Records are the attempts that kept one.",
    width=118,
)
SERVED_NOTES = textwrap.wrap(
    "What the replies of the model's records say they were served by; not reported is a reply that named none, never "
    "the id that was asked for.",
    width=118,
)
Table = Callable[[Sequence[str], Sequence[Sequence[object]]], list[str]]


def check(usage: Mapping[str, Any]) -> None:
    """Refuse figures that are not of the shape `campaign.model_usage` writes."""
    if usage.get("format") != FORMAT:
        raise ValueError(f"the model usage is not of format {FORMAT!r}; the report cannot place it")


def _usd(value: float | None) -> str:
    """Dollars to six decimals, as the lost-spend section shows them; n/a when not known (never 0)."""
    return UNKNOWN if value is None else f"{value:.6f}"


def _sum(entry: Mapping[str, Any], bucket: str) -> float | None:
    """A model's dollars on one basis over its classes; None unless every dollar of the model is known."""
    if entry["usd"] is None:
        return None
    return float(sum(c["usd"][bucket] for c in entry["classes"]))


def _spend_rows(models: Sequence[Mapping[str, Any]]) -> list[list[object]]:
    rows = []
    for entry in models:
        usd = entry["usd"] or {}
        rows.append(
            [
                entry["model"],
                _usd(usd.get("total")),
                _usd(usd.get("analysed")),
                _usd(usd.get("not_analysed")),
                *(_usd(_sum(entry, bucket)) for bucket in BUCKETS),
                _usd(entry["probes_usd"]),
            ]
        )
    return rows


def _class_rows(models: Sequence[Mapping[str, Any]]) -> list[list[object]]:
    rows = []
    for entry in models:
        for found in entry["classes"]:
            usd = found["usd"]
            dollars = [_usd(None if usd is None else usd[name]) for name in (*BUCKETS, "total")]
            rows.append(
                [
                    entry["model"],
                    LABELS[found["class"]],
                    found["attempts"],
                    found["input_tokens"],
                    found["output_tokens"],
                    found["cache_creation_tokens"],
                    found["cache_read_tokens"],
                    found["usage_incomplete_attempts"],
                    found["bounded_attempts"],
                    *dollars,
                ]
            )
    return rows


def _attempt_retries(export: Mapping[str, Any], models: Sequence[Mapping[str, Any]]) -> list[list[object]]:
    """The attempts set aside as infrastructure per model, kind, state and whether they were charged; a model with none
    has a row of zeros."""
    counts: Counter[tuple[str, str, str, bool]] = Counter()
    for entry in export["retries_by_kind"]:
        counts[(entry["model"], entry["kind"] or UNKNOWN, entry["state"], bool(entry["charged"]))] += entry["attempts"]
    rows = []
    for model in (m["model"] for m in models):
        found = sorted((key, n) for key, n in counts.items() if key[0] == model)
        rows += [[model, kind, state, "yes" if charged else "no", n] for (_, kind, state, charged), n in found]
        if not found:
            rows.append([model, NONE, NONE, NONE, 0])
    return rows


def _seconds(value: float) -> str:
    return f"{value:.2f}"


def _request_retries(models: Sequence[Mapping[str, Any]]) -> tuple[list[list[object]], list[list[object]]]:
    """Per model, its records, turns, turns that retried, retries and their wait; then each model's retries by kind."""
    summary, by_kind = [], []
    for entry in models:
        retries = entry["retries"]
        summary.append(
            [
                entry["model"],
                entry["records"],
                entry["turns"],
                retries["turns_retried"],
                retries["events"],
                _seconds(retries["wait_s"]),
            ]
        )
        by_kind += [[entry["model"], k["kind"], k["events"], _seconds(k["wait_s"])] for k in retries["by_kind"]]
    return summary, by_kind


def _served_rows(models: Sequence[Mapping[str, Any]]) -> list[list[object]]:
    rows = []
    for entry in models:
        for seen in entry["served"]:
            model, fingerprint = seen["served_model"], seen["system_fingerprint"]
            rows.append(
                [
                    entry["model"],
                    NOT_REPORTED if model is None else model,
                    NOT_REPORTED if fingerprint is None else fingerprint,
                    seen["turns"],
                    seen["records"],
                ]
            )
        if not entry["served"]:
            rows.append([entry["model"], NONE, NONE, 0, 0])
    return rows


def section(usage: Mapping[str, Any] | None, export: Mapping[str, Any], table: Table) -> list[str]:
    """The lines of the section, ending with a blank line; none for a report given no figures."""
    if usage is None:
        return []
    check(usage)
    models = usage["models"]
    spend = ["Model", "Spent (USD)", "On analysed attempts", "On the rest", "Measured", "Measured plus in flight"]
    spend += ["Bound", "Top-ups", "Probes"]
    classes = ["Model", "Attempts of", "Attempts", "Input tokens", "Output tokens", "Cache writes", "Cache reads"]
    classes += ["Usage incomplete", "Charged at a bound", "Measured (USD)", "Measured plus in flight (USD)"]
    classes += ["Bound (USD)", "Top-ups (USD)", "Total (USD)"]
    lines = [HEADING, "", *NOTES, "", "### Spend by model", "", *table(spend, _spend_rows(models)), ""]
    lines += ["### Attempts, tokens and spend by what became of the attempt", ""]
    lines += [*table(classes, _class_rows(models)), ""]
    lines += ["### Attempts set aside as infrastructure", ""]
    lines += [*ATTEMPT_NOTES, ""]
    lines += [*table(["Model", "Kind", "State", "Charged", "Attempts"], _attempt_retries(export, models)), ""]
    if not all("retries" in entry for entry in models):  # figures that carry nothing of the records
        return lines
    summary, by_kind = _request_retries(models)
    lines += ["### Requests retried inside an attempt", ""]
    lines += [*REQUEST_NOTES, ""]
    lines += [*table(["Model", "Records", "Turns", "Turns that retried", "Retries", "Wait (s)"], summary), ""]
    lines += [*table(["Model", "Kind", "Retries", "Wait (s)"], by_kind), ""] if by_kind else []
    lines += ["### Served model ids and fingerprints", ""]
    lines += [*SERVED_NOTES, ""]
    lines += [*table(["Model", "Served model id", "System fingerprint", "Turns", "Records"], _served_rows(models)), ""]
    return lines


def provenance_line(usage: Mapping[str, Any] | None) -> list[str]:
    """The one line of the report's provenance that says what was spent and on which basis; none without figures."""
    if usage is None:
        return []
    check(usage)
    pointer = f'see "{HEADING.removeprefix("## ")}"'
    dollars = usage["dollars"]
    if not dollars["known"]:
        return [f"- Spend: {UNKNOWN} ({dollars['reason']}); the tokens are the store's, {pointer}"]
    known = [m for m in usage["models"] if m["usd"] is not None]
    unknown = [m["model"] for m in usage["models"] if m["usd"] is None]
    if not known:
        return [f"- Spend: {UNKNOWN} for {', '.join(unknown)} (no price); {pointer}"]
    total = sum(m["usd"]["total"] for m in known)
    basis = {name: sum(_sum(m, name) or 0.0 for m in known) for name in BUCKETS}
    probes = sum(m["probes_usd"] for m in known)
    analysed = sum(m["usd"]["analysed"] for m in known)
    rest = sum(m["usd"]["not_analysed"] for m in known)
    whole = "on the models with a price" if unknown else "in all"  # never "in all" beside a model whose spend is n/a
    line = (
        f"- Spend: {total:.6f} USD {whole}: {basis['measured']:.6f} measured, "
        f"{basis['measured_plus_inflight']:.6f} measured plus requests in flight, {basis['bound']:.6f} at a bound, "
        f"{basis['charges']:.6f} in top-ups, {probes:.6f} in probes; {analysed:.6f} on the analysed attempts and "
        f"{rest:.6f} on the rest"
    )
    if unknown:
        line += f"; {UNKNOWN} for {', '.join(unknown)} (no price)"
    return [f"{line}; {pointer}"]
