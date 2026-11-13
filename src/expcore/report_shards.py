# Provenance: new
"""The `## Shards` section of the report of a batch merged from shards (#154): what each host did, descriptively.

The shards are a check that the hosts are interchangeable, not a factor of the design, so nothing here is a test
across shards, and no score is shown by shard: a pair never crosses shards, so a host effect meets both arms alike, and
a score by shard would mostly show which instances landed where. The section shows, per shard:

- the runs of each condition that were analysed, exhausted, unfinished or left out, with those rates (from the run
  lists, each entry naming its `shard`);
- the infrastructure attempts by kind: the attempts set aside in all the shard's runs, the charged attempts of its
  exhausted runs, and the attempts scored as treatment;
- the wall time of its analysed runs, the latency of its model turns and the time of its resets, as median and 90th
  percentile with N.

**What the export carries.** The run lists carry `shard` on each entry, so the first table and the last two columns of
the second are counted from them: the reasons of an exhausted run name the kind of each charged attempt, and an
analysed attempt scored as treatment names its `infra_kind`. A merged export the core makes also carries:

- `shard_retries_by_kind`: the rows of each shard's own `retries_by_kind` with a `shard` added, one set per shard
  (`retries_by_kind` itself is summed over the shards by the merge). An empty list has recorded them, zero included.
- In each `analysed_attempts` entry, `wall_s` (the attempt's wall time, from its shard's snapshot) and
  `turn_latency_s` (each model turn's latency in turn order, from the run's record), both added by
  `study.merged_times`.

The section is still read from an export that lacks any of them, and a figure without its source reads `not recorded`
(never 0). `reset_s` (the seconds the run's environment reset took) is the one the core does not record: no store
column, record field or export key holds it, so it reads `not recorded` in every merged export the core makes.

N is the number of values a figure rests on. A shard whose analysed runs carry only some of them shows the smaller N.
"""

import math
import statistics
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from expcore.accounting import shard_accounting
from expcore.report_notes import NO_KIND, SHARD_KIND_NOTES, SHARD_NOTES, SHARD_TIME_NOTES, UNKNOWN

NOT_RECORDED = "not recorded"
UNKNOWN_KIND = "unknown"  # a kind an attempt did not record (a database from before the column)
RUN_COUNTS = ("planned", "analysed", "exhausted", "unfinished", "left_out")
RATES = ("analysed", "exhausted", "unfinished")

Table = Callable[[Sequence[str], Sequence[Sequence[object]]], list[str]]


def _rate(count: int, planned: int) -> str:
    """A share of the planned runs as a percentage to one decimal; n/a when none were planned (never 0%)."""
    return f"{100 * count / planned:.1f}%" if planned else UNKNOWN


def _seconds(value: float) -> str:
    """Seconds to four significant figures, never in exponent form."""
    return f"{value:.4g}" if abs(value) < 10_000 else f"{value:.0f}"


def _p90(values: Sequence[float]) -> float:
    """The 90th percentile, interpolated between the order statistics that lie either side of it."""
    ordered = sorted(values)
    position = 0.9 * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _shards_by_index(export: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return sorted(export["shards"], key=lambda shard: shard["index"])


def _runs_table(export: Mapping[str, Any], table: Table) -> list[str]:
    """One row per shard and condition: the runs by how they ended, and the rates of the first three."""
    header = ["Shard", "Started (UTC)", "Condition", "Planned", "Analysed", "Exhausted", "Unfinished", "Left out"]
    rows = [
        [
            cell["label"],
            cell["started_utc"] or UNKNOWN,
            cell["condition"],
            *(cell[field] for field in RUN_COUNTS),
            *(_rate(cell[field], cell["planned"]) for field in RATES),
        ]
        for cell in shard_accounting(export)
    ]
    return table([*header, "Analysed %", "Exhausted %", "Unfinished %"], rows)


def _kind(name: object) -> str:
    return UNKNOWN_KIND if name is None or name == "None" else str(name)


def _by_shard_and_kind(export: Mapping[str, Any]) -> tuple[Counter, Counter, Counter | None]:
    """The charged attempts of exhausted runs, the attempts scored as treatment and, when the export has recorded them
    per shard, the attempts set aside, each counted by (shard index, kind)."""
    known = {shard["index"] for shard in export["shards"]}
    exhausted: Counter = Counter()
    for entry in export["exhausted_runs"]:  # a reason reads "<kind>: <detail>", one per charged attempt
        for reason in entry.get("reasons", []):
            exhausted[(entry["shard"], _kind(str(reason).split(": ", 1)[0]))] += 1
    treatment: Counter = Counter()
    for entry in export["analysed_attempts"]:
        if entry.get("treatment"):
            treatment[(entry["shard"], _kind(entry.get("infra_kind")))] += 1
    if "shard_retries_by_kind" not in export:
        return exhausted, treatment, None
    set_aside: Counter = Counter()
    for row in export["shard_retries_by_kind"]:
        if row.get("shard") not in known:
            raise ValueError(
                f"shard_retries_by_kind names shard {row.get('shard')!r}, which the export's shards do not list"
            )
        set_aside[(row["shard"], _kind(row.get("kind")))] += row["attempts"]
    return exhausted, treatment, set_aside


def _kinds_table(export: Mapping[str, Any], table: Table) -> list[str]:
    """One row per shard and kind; a shard that never met a kind has zeros, and with no kind at all, a row of `none`."""
    exhausted, treatment, set_aside = _by_shard_and_kind(export)
    seen = {kind for _, kind in (*exhausted, *treatment, *(set_aside or ()))}
    kinds = sorted(seen) or [NO_KIND]
    rows = [
        [
            shard["label"],
            kind,
            NOT_RECORDED if set_aside is None else set_aside[(shard["index"], kind)],
            exhausted[(shard["index"], kind)],
            treatment[(shard["index"], kind)],
        ]
        for shard in _shards_by_index(export)
        for kind in kinds
    ]
    header = ["Shard", "Kind", "Set aside (all runs)", "Charged in exhausted runs", "Scored as treatment"]
    return table(header, rows)


def _values(entries: Sequence[Mapping[str, Any]], field: str) -> list[float]:
    """The numbers a field holds over the analysed entries; a list field contributes each of its items.

    Refuses anything but a finite, non-negative number, naming the run: a figure never rests on a guess.
    """
    found = []
    for entry in entries:
        value = entry.get(field)
        for item in [] if value is None else (value if isinstance(value, list) else [value]):
            if isinstance(item, bool) or not isinstance(item, int | float) or not math.isfinite(item) or item < 0:
                raise ValueError(f"run {entry['run_key']!r}: {field} holds {item!r}, not a number of seconds")
            found.append(float(item))
    return found


def _figure(values: Sequence[float], analysed: int, *, with_p90: bool = True) -> list[object]:
    """N, the median and (unless `with_p90` is false) the 90th percentile; `not recorded` when no analysed run carries
    the figure, and `n/a` for a shard with no analysed run to carry it."""
    if values:
        cells = [_seconds(statistics.median(values)), *([_seconds(_p90(values))] if with_p90 else [])]
    else:
        cells = [NOT_RECORDED if analysed else UNKNOWN] * (2 if with_p90 else 1)
    return [len(values), *cells]


def _time_table(export: Mapping[str, Any], table: Table) -> list[str]:
    """One row per shard: the wall time of its analysed runs, its turns' latency and its resets' time, with N."""
    rows = []
    for shard in _shards_by_index(export):
        mine = [entry for entry in export["analysed_attempts"] if entry["shard"] == shard["index"]]
        rows.append(
            [
                shard["label"],
                *_figure(_values(mine, "wall_s"), len(mine)),
                *_figure(_values(mine, "turn_latency_s"), len(mine)),
                *_figure(_values(mine, "reset_s"), len(mine), with_p90=False),
            ]
        )
    header = ["Shard", "Runs timed", "Run wall time median (s)", "Run wall time p90 (s)", "Turns timed"]
    header += ["Turn latency median (s)", "Turn latency p90 (s)", "Resets timed", "Reset median (s)"]
    return table(header, rows)


def shard_lines(export: Mapping[str, Any], table: Table) -> list[str]:
    """The lines of the `## Shards` section, built with `table` (the report's own table renderer); nothing for an
    export without `shards`, so the report of an unmerged batch is not touched.

    Refuses a `shards` that does not account for every run, a per-shard row of a shard it does not list, and a figure
    that is not a number of seconds.
    """
    if "shards" not in export:
        return []
    return [
        "## Shards",
        "",
        *_runs_table(export, table),
        "",
        *SHARD_NOTES,
        "",
        "### Infrastructure attempts by shard and kind",
        "",
        *_kinds_table(export, table),
        "",
        *SHARD_KIND_NOTES,
        "",
        "### Time by shard",
        "",
        *_time_table(export, table),
        "",
        *SHARD_TIME_NOTES,
        "",
    ]
