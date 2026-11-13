# Provenance: new
"""The run times of a merged batch's export (#154): what its `## Shards` section reports as time by shard.

An export keeps no time of an analysed run: the store has the wall time of each attempt (`attempts.wall_s`), which the
export keeps only as sums over the discarded attempts, and the latency of each model turn lives only in the run's
record (`TurnRecord.latency_s`, kept per turn in the record's transcript). A merged batch has both at hand: the
snapshot of every shard, and the records the analysis verifies and reads anyway. So the merge's export, and only
the merge's, gives each analysed entry two more keys, after the ones every export has:

- `wall_s`: the wall time of the analysed attempt, in seconds, from its shard's snapshot; `None` when the store
  predates the column and never knew it (unknown, never zero);
- `turn_latency_s`: the latency of each model turn of the attempt's record, in turn order; an attempt that never got a
  reply has an empty list.

The export of an unmerged batch and of a shard on its own are made by `campaign.export` alone and have neither key, so
their bytes are what they were before shards existed. The reset time of a run is recorded nowhere in the core, so no
key carries it.
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from expcore.campaign.export import ExportError
from expcore.campaign.snapshot import StoreSnapshot
from expcore.records.record import RunRecord

WALL_KEY = "wall_s"
LATENCY_KEY = "turn_latency_s"
RecordFor = Callable[[str, str, str, int], RunRecord | None]  # digest, try directory, run key, attempt


def walls_of(snapshot: StoreSnapshot) -> dict[tuple[str, int], float | None]:
    """The wall time of every done attempt of a snapshot, by (run key, attempt)."""
    rows = snapshot.q("SELECT run_key, attempt, wall_s FROM attempts WHERE state = 'done'")
    return {(row["run_key"], row["attempt"]): row["wall_s"] for row in rows}


def timed_export(
    export: Mapping[str, Any], walls: Mapping[tuple[str, int], float | None], record_for: RecordFor
) -> dict[str, Any]:
    """A copy of one shard's export whose analysed entries also carry `wall_s` and `turn_latency_s`.

    `walls` is the shard's done attempts' wall time (`walls_of`) and `record_for` the verified record of an attempt
    (`VerifiedBatch.record_for`). Every other key and list is the export's own, in its own order. Refuses an entry
    whose attempt or record is not there, naming the run: a time is never guessed.
    """
    analysed = []
    for entry in export["analysed_attempts"]:
        run_key, attempt = entry["run_key"], entry["attempt"]
        if (run_key, attempt) not in walls:
            raise ExportError(f"run {run_key}: the shard's snapshot has no done attempt {attempt}")
        record = record_for(entry["digest"], Path(entry["run_dir"]).name, run_key, attempt)
        if record is None:
            raise ExportError(f"run {run_key}: no verified record for attempt {attempt}")
        latencies = [turn["latency_s"] for turn in record.transcript]
        analysed.append({**entry, WALL_KEY: walls[(run_key, attempt)], LATENCY_KEY: latencies})
    return {**export, "analysed_attempts": analysed}
