# Provenance: new
"""The pipeline of a study directory: run the campaign, check the records, analyse them (the CLI is a thin shell).

Everything a study produces goes under `<study>/out/`: the campaign store (`state.sqlite`), the leases, the runs
(`runs/<digest>/try<N>/`, one record and one outcome marker each), and what `analyse_study` writes (`export.json`,
`scores.json`, `analysis.json`).

- `run_study` opens or resumes the campaign and runs the scheduler. Its code identity is computed once, from the study
  directory and the scope study.toml names, and every record carries it.
- `check_batch` is the replay check over `out/runs`. The outcome markers are the campaign's own bookkeeping, not
  records: the check hashes them and does not replay them.
- `analyse_study` refuses unless the batch has a current passing check (R04, R05), and then reads the export, scores the
  records in memory (a record is never written after its check, since that would change its hash) and runs the
  preregistered analysis.
"""

import dataclasses
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.campaign.export import write_export
from expcore.campaign.lease import LeasePool
from expcore.campaign.manifest import build_fingerprint
from expcore.campaign.scheduler import STATE_FILE, RunSummary, Scheduler
from expcore.campaign.store import Store
from expcore.models.route import sdk_version
from expcore.provenance.codestamp import require_known
from expcore.provenance.codestamp import version as code_version
from expcore.records.build import CORE_DISTRIBUTION
from expcore.records.record import RunRecord, load_record, record_filename
from expcore.records.rescore import rescore
from expcore.replay.check import BatchNotVerified, require_current_check, verify_batch, write_check
from expcore.stats.analysis import AnalysisResult, analyse
from expcore.stats.rows import Row
from expcore.study.config import StudyConfig, find_study_root, load_study
from expcore.study.plugin import StudyContext, StudyPlugin, load_plugin

OUT_DIR = "out"
RUNS_DIR = "runs"
MARKER_PATTERN = "*attempt-marker.json"
EXPORT_FILE = "export.json"
SCORES_FILE = "scores.json"
ANALYSIS_FILE = "analysis.json"
REPORT_FILE = "report.md"


class Refused(RuntimeError):
    """A command refused to go on; the message says why and what to do."""


@dataclass(frozen=True)
class StudyAnalysis:
    """The preregistered analysis of a study's records with everything that went into it."""

    study: StudyConfig
    code: Mapping[str, Any]
    core_version: str | None
    export: Mapping[str, Any]
    scores: Mapping[str, Any]
    rows: tuple[Row, ...]
    result: AnalysisResult


def out_dir(study: StudyConfig) -> Path:
    """The directory a study writes to."""
    return study.root / OUT_DIR


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def fingerprint_of(study: StudyConfig, plugin: StudyPlugin) -> dict[str, Any]:
    """The campaign fingerprint: what makes two starts one experiment (C06), from the study and its plugin."""
    return build_fingerprint(
        study.schema,
        tasks={task.id: _digest(dict(task.attributes)) for task in study.tasks.values()},
        models={key: _digest(dataclasses.asdict(study.models.resolve(key))) for key in study.campaign.models},
        seeds=study.campaign.seeds,
        caps=study.campaign.caps(),
        prompts=plugin.prompts,
        tools=plugin.tools,
        scorer_digest=plugin.scorer_digest(),
        configs=study.config_sha256,
    )


def run_study(
    path: str | os.PathLike[str],
    *,
    allow_dirty: bool = False,
    code_root: str | os.PathLike[str] | None = None,
    crash_hook: Callable[[str, str], None] | None = None,
) -> RunSummary:
    """Open or resume the study's campaign and run the scheduler until nothing more can start.

    Refuses code that cannot be vouched for (unknown, stale, or dirty unless `allow_dirty`). The scheduler returns when
    no run is in flight and every pending run is held by something that will not end by itself (a budget, a stop):
    a command line has no one to release it. `crash_hook` is the scheduler's own test seam for killing the process at
    a chosen point of an attempt.
    """
    study = load_study(path)
    code = require_known("run", code_root or study.root, study.scope, allow_dirty=allow_dirty)
    plugin = load_plugin(StudyContext(study, code))
    out = out_dir(study)
    leases = None if plugin.probe is None else LeasePool(out / "leases", plugin.lease_keys, plugin.probe)
    settings = dataclasses.replace(study.campaign.scheduler, exit_when_blocked=True)
    options = {} if crash_hook is None else {"crash_hook": crash_hook}
    with Scheduler(
        out,
        study.schema,
        study.manifest,
        fingerprint_of(study, plugin),
        plugin.executor,
        agent_config=study.campaign.agent,
        models=study.models,
        endpoints=study.campaign.endpoints,
        settings=settings,
        health=plugin.health,
        leases=leases,
        lease_group=plugin.lease_group,
        **options,
    ) as scheduler:
        return scheduler.run()


def check_batch(
    batch: str | os.PathLike[str],
    *,
    study_root: str | os.PathLike[str] | None = None,
    allow_dirty: bool = False,
    code_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Run the replay check over a batch of records, store it beside them and return it.

    The study is the one at `study_root`, or the nearest directory above the batch that holds a study.toml; its scope
    decides which code the check covers.
    """
    study = load_study(find_study_root(study_root if study_root is not None else batch))
    code = require_known("replay check", code_root or study.root, study.scope, allow_dirty=allow_dirty)
    check = verify_batch(batch, code=code, hashed_only=[MARKER_PATTERN])
    write_check(batch, check)
    return check


def _write_json(path: Path, data: Any) -> None:
    """Write JSON atomically, with no non-finite number (a missing value is null)."""
    text = json.dumps(_finite(data), indent=1, sort_keys=True, allow_nan=False) + "\n"
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _finite(value: Any) -> Any:
    """Reduce a value to JSON data: dataclasses and mappings to dicts, sequences to lists, NaN and infinity to None."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _finite(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _finite(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_finite(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _record_of(batch: Path, entry: Mapping[str, Any]) -> RunRecord:
    """Load the record of an analysed attempt. The path is rebuilt from the batch, so a moved study still finds it."""
    where = batch / entry["digest"] / Path(entry["run_dir"]).name
    return load_record(where / record_filename(entry["run_key"], entry["attempt"]))


def _outcomes_of(record: RunRecord) -> dict[str, float | None]:
    """The named numeric outcomes of every scorer of a scored record; two scorers may not give one name."""
    merged: dict[str, float | None] = {}
    for scorer, held in record.scores.items():
        outcomes = held["result"].get("outcomes")
        if not isinstance(outcomes, Mapping):
            raise Refused(f"scorer {scorer!r} returned no 'outcomes' mapping for {record.run_key}")
        clash = sorted(set(merged) & set(outcomes))
        if clash:
            raise Refused(f"scorer {scorer!r} gives outcome {clash[0]!r}, which another scorer already gives")
        merged.update(outcomes)
    return merged


def analyse_study(
    path: str | os.PathLike[str], *, allow_dirty: bool = False, code_root: str | os.PathLike[str] | None = None
) -> StudyAnalysis:
    """Export the campaign, score its records and run the preregistered analysis; refuse without a current check.

    Nothing is written into the batch. The export is written to `out/export.json`.
    """
    study = load_study(path)
    out = out_dir(study)
    batch = out / RUNS_DIR
    if not (out / STATE_FILE).is_file():
        raise Refused(f"{out}: no campaign here; run the study first")
    code = code_version(code_root or study.root, study.scope)
    try:
        require_current_check(
            batch, code=code, allow_dirty=allow_dirty, rerun_command=f"python -m expcore check {batch}"
        )
    except BatchNotVerified as error:
        raise Refused(f"refusing to analyse: {error}") from None
    plugin = load_plugin(StudyContext(study, code))
    store = Store(out / STATE_FILE, study.schema)
    try:
        export = write_export(store, out / EXPORT_FILE)
    finally:
        store.close()
    rows, scores = [], {}
    for entry in export["analysed_attempts"]:
        scored = rescore(_record_of(batch, entry), plugin.scorers)
        scores[entry["run_key"]] = scored.scores
        task = study.tasks[str(entry["factors"]["task"])]
        rows.append(Row.from_export(entry, _outcomes_of(scored), task.attributes))
    result = analyse(rows, study.prereg)
    return StudyAnalysis(study, code, sdk_version(CORE_DISTRIBUTION), export, scores, tuple(rows), result)


def write_analysis(analysis: StudyAnalysis) -> list[Path]:
    """Write the scores and the analysis beside the export; return the files written."""
    out = out_dir(analysis.study)
    summary = {
        "preregistration": dict(analysis.result.manifest),
        "alpha": analysis.result.alpha,
        "code": {key: analysis.code.get(key) for key in ("commit", "tree_sha256", "dirty")},
        "core_version": analysis.core_version,
        "contrasts": analysis.result.contrasts,
        "holm": analysis.result.holm,
    }
    _write_json(out / SCORES_FILE, analysis.scores)
    _write_json(out / ANALYSIS_FILE, summary)
    return [out / EXPORT_FILE, out / SCORES_FILE, out / ANALYSIS_FILE]
