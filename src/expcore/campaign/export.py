# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/export.py, blob da7dd6d933dc5c2fe6aa371a849e20e7b684f791
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Export of the campaign's run state for the analysis.

The store keeps every attempt; the analysis sees only scored runs. A run that used all its attempts on infrastructure
failures ('infra_exhausted') has no scored attempt, so without this export it would vanish from every denominator
(C09). The export lists those runs with their reasons, the runs that are neither done nor exhausted, the discarded
attempts' usage, and the one analysed attempt of every finished run. The three lists of runs partition the plan, and
`planned_runs` says how many it held, so a reader can check that no run went missing.

Rule: a run's analysed attempt is the one that ended with an agent outcome, or with an infrastructure fault that its
condition declares as its treatment (it carries `treatment` and the fault's `infra_kind`). Every other infrastructure
attempt is discarded, and so are all the attempts of an exhausted run. Interrupted, outage-discarded and harness-error
attempts are reported in their own rows of `discarded_usage`, never folded into one another.

Infrastructure retries are grouped by the kind from the closed vocabulary in `expcore.errors`, each under its own name
(C02). `infrastructure_by_condition` adds, per condition and kind, how many attempts were set aside and run again and
how many were scored as treatment, with the kinds each condition declares, so a reader sees whether a condition's
fault was measured or re-run away. Field names are generic: a run is named by its key, its condition, its model and its
factor values. The file is written to a temporary file in its directory and moved into place, so a reader sees the old
file or the new one; it is owner-only (0600), because an export can carry study data.

Each analysed attempt also says when and how it began (`dispatch_seq`, `started_at`, `lease_wait_s`, `lease_outcome`), so
an analysis can check the balance of the conditions over time: holds and unit caps can push one condition's runs late.

Empty lists are valid and must be written out: `read_export` rejects a file with a missing key.
"""

import collections
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from expcore.campaign.factors import FactorSchema, RunKey
from expcore.errors import infra_kinds

FORMAT = "campaign-export-1"
RUN_LISTS = ("exhausted_runs", "unfinished_runs", "analysed_attempts")
REQUIRED_KEYS = (
    "format",
    "factors",
    "planned_runs",
    *RUN_LISTS,
    "discarded_usage",
    "retries_by_kind",
    "infrastructure_by_condition",
    "kinds",
)
_DISCARDED_STATES = ("infra_fail", "outage_discarded", "interrupted", "harness_error")
_USAGE_SUMS = ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens", "wall_s")


class RunState(Protocol):
    """What the export reads of a campaign: the schema and read-only queries in one snapshot.

    A `Store` answers it, and so does a `StoreSnapshot`, the copy of a store whose bytes the replay check hashed.
    """

    schema: FactorSchema

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]: ...

    def snapshot(self) -> AbstractContextManager[None]: ...


class ExportError(RuntimeError):
    """An export file is unreadable or incomplete, or the store holds a state the export cannot place."""


def _identity(store: RunState, row: Any) -> dict[str, Any]:
    """Name a run by its key, digest, condition and factor values."""
    factors = json.loads(row["factors"])
    key = RunKey(store.schema.order, tuple(factors[name] for name in store.schema.order))
    return {
        "run_key": row["run_key"],
        "digest": row["digest"],
        "condition": store.schema.condition_of(key),
        "factors": factors,
    }


def export_run_state(store: RunState) -> dict[str, Any]:
    """Return the run state for the analysis, read in one snapshot so a running campaign cannot split it."""
    with store.snapshot():
        runs = store.q("SELECT * FROM runs ORDER BY seq")
        attempts: dict[str, list[Any]] = collections.defaultdict(list)
        for attempt in store.q("SELECT * FROM attempts ORDER BY id"):
            attempts[attempt["run_key"]].append(attempt)
        identities = {run["run_key"]: _identity(store, run) for run in runs}
    exhausted, unfinished, analysed = [], [], []
    for run in runs:
        who, tried = identities[run["run_key"]], attempts[run["run_key"]]
        if run["status"] == "infra_exhausted":
            charged = [a for a in tried if a["charged"]]
            exhausted.append(
                {**who, "attempts": len(charged), "reasons": [f"{a['infra_kind']}: {a['detail']}" for a in charged]}
            )
        elif run["status"] in ("pending", "harness_error"):
            unfinished.append({**who, "status": run["status"]})
        elif run["status"] == "done":
            done = [a for a in tried if a["state"] == "done"]
            if len(done) != 1:
                raise ExportError(f"run {run['run_key']} is done but has {len(done)} done attempts")
            analysed.append(
                {
                    **who,
                    "attempt": done[0]["attempt"],
                    "run_dir": done[0]["run_dir"],
                    "treatment": bool(done[0]["treatment"]),
                    "infra_kind": done[0]["infra_kind"],
                    **_dispatch(done[0]),
                }
            )
        else:
            raise ExportError(f"run {run['run_key']}: unknown status {run['status']!r}")
    return {
        "format": FORMAT,
        "factors": list(store.schema.order),
        "planned_runs": len(runs),
        "exhausted_runs": exhausted,
        "unfinished_runs": unfinished,
        "analysed_attempts": analysed,
        "discarded_usage": _discarded_usage(identities, attempts),
        "retries_by_kind": _retries_by_kind(identities, attempts),
        "infrastructure_by_condition": _infrastructure_by_condition(identities, attempts, store.schema.treatment_kinds),
        "kinds": list(infra_kinds()),
    }


def _dispatch(attempt: Any) -> dict[str, Any]:
    """Return when and how the attempt began, for the analysis to check that the conditions ran side by side in time.

    `dispatch_seq` is its place in the campaign's dispatch sequence (it grows with every attempt, and carries on across
    a restart), `started_at` the time it began (whole seconds, UTC), `lease_wait_s` the seconds it waited for its lease
    and `lease_outcome` how the lease was obtained: `granted`, or `none` for a run that needs no lease. A refused lease
    starts no attempt, so it has no row here; it is in the lease wait log and the event log. A value a database from
    before the column did not keep is `None`: unknown, never zero.
    """
    return {
        "dispatch_seq": attempt["dispatch_seq"],
        "started_at": attempt["started_utc"],
        "lease_wait_s": attempt["lease_wait_s"],
        "lease_outcome": attempt["lease_outcome"],
    }


def _none_last(item: tuple[tuple, Any]) -> tuple:
    """Order the cells of a table by their key, with a missing part (None) after every present one.

    A kind can be missing on an attempt a database recorded before the outage kind had its own column and that no
    backfill could name. Plain tuple ordering would raise on None against a string; this puts those cells last, and
    the same way every time.
    """
    return tuple((part is None, "" if part is None else part) for part in item[0])


def _kind_of(attempt: Any) -> str | None:
    """Return the kind an infrastructure attempt is counted under.

    An outage-discarded attempt is counted under the outage's kind; every other attempt, a treatment attempt included,
    under the kind of its own outcome, which an outage flag never replaces.
    """
    return attempt["outage_kind"] if attempt["state"] == "outage_discarded" else attempt["infra_kind"]


def _discarded_usage(identities: dict[str, dict], attempts: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Sum the usage of the attempts whose results were set aside, by condition, model and state."""
    cells: dict[tuple, dict[str, Any]] = {}
    for key, tried in attempts.items():
        who = identities[key]
        for attempt in tried:
            if attempt["state"] not in _DISCARDED_STATES:
                continue
            cell = cells.setdefault(
                (who["condition"], who["factors"]["model"], attempt["state"]),
                {"attempts": 0, "usage_incomplete_attempts": 0, **dict.fromkeys(_USAGE_SUMS, 0)},
            )
            cell["attempts"] += 1
            cell["usage_incomplete_attempts"] += int(not attempt["usage_complete"])
            for name in _USAGE_SUMS:
                cell[name] += attempt[name] or 0
    return [
        {"condition": condition, "model": model, "state": state, **cell}
        for (condition, model, state), cell in sorted(cells.items(), key=_none_last)
    ]


def _retries_by_kind(identities: dict[str, dict], attempts: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Count the infrastructure attempts by condition, model, kind and state; no kind is renamed or merged (C02)."""
    counts: collections.Counter[tuple] = collections.Counter()
    for key, tried in attempts.items():
        who = identities[key]
        for attempt in tried:
            if attempt["state"] in ("infra_fail", "outage_discarded"):
                counts[
                    (
                        who["condition"],
                        who["factors"]["model"],
                        _kind_of(attempt),
                        attempt["state"],
                        bool(attempt["charged"]),
                    )
                ] += 1
    return [
        {"condition": condition, "model": model, "kind": kind, "state": state, "charged": charged, "attempts": n}
        for (condition, model, kind, state, charged), n in sorted(counts.items(), key=_none_last)
    ]


def _infrastructure_by_condition(
    identities: dict[str, dict], attempts: dict[str, list[Any]], declared: dict[str, tuple[str, ...]]
) -> list[dict[str, Any]]:
    """Count, per condition and kind, the attempts set aside and run again and the attempts scored as treatment.

    Every kind a condition declares as its treatment has a row, with zeros when nothing of that kind happened.
    """
    cells: dict[tuple[str, str], dict[str, int]] = {}
    for condition, kinds in declared.items():
        for kind in kinds:
            cells[(condition, kind)] = {"set_aside": 0, "treatment": 0}
    for key, tried in attempts.items():
        condition = identities[key]["condition"]
        for attempt in tried:
            if attempt["treatment"]:
                field = "treatment"
            elif attempt["state"] in ("infra_fail", "outage_discarded"):
                field = "set_aside"
            else:
                continue
            cells.setdefault((condition, _kind_of(attempt)), {"set_aside": 0, "treatment": 0})[field] += 1
    return [
        {"condition": condition, "kind": kind, "declared_treatment": kind in declared[condition], **counts}
        for (condition, kind), counts in sorted(cells.items(), key=_none_last)
    ]


def write_export(store: RunState, path: Path) -> dict[str, Any]:
    """Write the export atomically and return it.

    The data goes to a temporary file of its own in the destination's directory (named by `tempfile.mkstemp`, so two
    writers never share one), is flushed and fsynced, and is moved over the destination with `os.replace`. A reader
    sees the old file or a complete new one, whoever else is writing.

    The file keeps the owner-only mode (0600) that `mkstemp` gives it, whatever the process umask. An export can carry
    study data (task and run keys, reasons that name tools and paths), so owner-only is the safe default; whoever
    needs to share it copies it or changes its mode on purpose.
    """
    data = export_run_state(store)
    write_export_data(data, path)
    return data


def write_export_data(data: dict[str, Any], path: Path) -> None:
    """Write an export that was already built, atomically and owner-only, as `write_export` does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_export(path: Path) -> dict[str, Any]:
    """Read an export and refuse one that is of another format, lacks a key, or does not account for every run."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ExportError(f"{path}: cannot be read as an export ({type(error).__name__})") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ExportError(f"{path}: not a {FORMAT} export")
    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        raise ExportError(f"{path}: missing key(s) {', '.join(missing)}; an empty list must be written out")
    listed = sum(len(data[name]) for name in RUN_LISTS)
    if listed != data["planned_runs"]:
        raise ExportError(f"{path}: lists {listed} runs but the plan held {data['planned_runs']}")
    return data
