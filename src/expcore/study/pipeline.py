# Provenance: new
"""The pipeline of a study directory: run the campaign, check the records, analyse them (the CLI is a thin shell).

Everything a study produces goes under `<study>/out/`: the campaign store (`state.sqlite`), the leases, the runs
(`runs/<digest>/try<N>/`, one record and one outcome marker each), and what `analyse_study` writes (`export.json`,
`scores.json`, `analysis.json`).

- `run_study` opens or resumes the campaign and runs the scheduler. Its code identity is computed once, from the study
  directory and the scope study.toml names, and every record carries it.
- `check_batch` is the replay check over `out/runs`. The outcome markers are the campaign's own bookkeeping, not
  records: the check hashes them and does not replay them. It also copies the campaign store into the batch
  (`campaign-snapshot.sqlite`) and hashes the copy, so the analysis never has to read the live store; it refuses a
  store with attempts still running unless `partial`, which the check records.
- `analyse_study` refuses unless the batch has a current passing check (R04, R05), and writes nothing. It then uses
  only bytes the check verified (`study.verified`): the snapshot and the replayed records, each read once and compared
  with the check's hash, cross-validated against each other. It exports from the snapshot, scores the records in
  memory (a record is never written after its check, since that would change its hash) and runs the preregistered
  analysis. Every score is
  derived anew from a record on each call and none is read back: `scores.json` is an output, so `analyze` is how a
  study is re-scored (R13), offline, after a change to a scorer and a new check (a scorer is code the identity covers).
- `output_bytes` is the one place the bytes of `export.json`, `scores.json`, `analysis.json` and `report.md` are made;
  `write_analysis` and `write_report` write them and `regenerate` compares them with the files in `out/`, which is
  what a replication package's `make check` runs. Every time in them comes from the records and the store, every
  list has a defined order, and no path of the machine appears, so the same records give the same bytes anywhere.
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

from expcore.accounting import condition_accounting
from expcore.campaign.executor import default_backend_factory
from expcore.campaign.export import export_run_state, export_text
from expcore.campaign.lease import LeasePool
from expcore.campaign.manifest import build_fingerprint
from expcore.campaign.scheduler import STATE_FILE, RunSummary, Scheduler
from expcore.campaign.snapshot import SNAPSHOT_FILE, SnapshotError, take_snapshot
from expcore.models.route import sdk_version
from expcore.provenance.codestamp import require_known
from expcore.provenance.codestamp import version as code_version
from expcore.records.build import CORE_DISTRIBUTION
from expcore.records.record import RunRecord
from expcore.records.rescore import rescore, scorer_name, scorer_version
from expcore.replay.check import BatchNotVerified, require_current_check, verify_batch, write_check
from expcore.report import render_report
from expcore.stats.analysis import AnalysisResult, TreatmentOutcomesRefused, analyse
from expcore.stats.rows import AbsentRun, Row
from expcore.study.config import StudyConfig, find_study_root, load_study
from expcore.study.plugin import StudyContext, StudyPlugin, load_plugin
from expcore.study.verified import UNCOVERED, Unverified, load_verified

OUT_DIR = "out"
RUNS_DIR = "runs"
MARKER_PATTERN = "*attempt-marker.json"
EXPORT_FILE = "export.json"
SCORES_FILE = "scores.json"
ANALYSIS_FILE = "analysis.json"
REPORT_FILE = "report.md"
OUTPUT_FILES = (EXPORT_FILE, SCORES_FILE, ANALYSIS_FILE, REPORT_FILE)  # what `analyze` and `report` write to out/
SNAPSHOT_KEY = "snapshot"  # the key of the check that says what its snapshot of the store held


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
    scorers: Mapping[str, str]  # the version of each scorer that scored the records, by name


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
        platform=plugin.platform,
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
    a command line has no one to release it. Every backend is built by the plugin's `backend_factory`, or the core's
    when it supplies none. `crash_hook` is the scheduler's own test seam for killing the process at a chosen point of
    an attempt.
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
        backend_factory=default_backend_factory if plugin.backend_factory is None else plugin.backend_factory,
        **options,
    ) as scheduler:
        return scheduler.run()


def check_batch(
    batch: str | os.PathLike[str],
    *,
    study_root: str | os.PathLike[str] | None = None,
    allow_dirty: bool = False,
    code_root: str | os.PathLike[str] | None = None,
    partial: bool = False,
) -> dict[str, Any]:
    """Copy the campaign store into the batch, run the replay check over it, store the check and return it.

    The study is the one at `study_root`, or the nearest directory above the batch that holds a study.toml; its scope
    decides which code the check covers. The snapshot of the store is hashed with the records, so the analysis can use
    it instead of the live store. A store with attempts still running is refused unless `partial`; the check records
    how many were running and that a partial snapshot was asked for.
    """
    study = load_study(find_study_root(study_root if study_root is not None else batch))
    code = require_known("replay check", code_root or study.root, study.scope, allow_dirty=allow_dirty)
    directory = Path(batch)
    if not directory.is_dir():
        raise Refused(f"{directory}: no batch of records here; run the study first")
    try:
        running = take_snapshot(out_dir(study) / STATE_FILE, directory / SNAPSHOT_FILE, allow_running=partial)
    except SnapshotError as error:
        raise Refused(str(error)) from None
    check = verify_batch(directory, code=code, hashed_only=[MARKER_PATTERN, SNAPSHOT_FILE])
    check[SNAPSHOT_KEY] = {"file": SNAPSHOT_FILE, "attempts_running": running, "partial": partial}
    write_check(directory, check)
    return check


def _json_text(data: Any) -> str:
    """JSON with sorted keys and no non-finite number (a missing value is null), ending with a newline."""
    return json.dumps(_finite(data), indent=1, sort_keys=True, allow_nan=False) + "\n"


def _write_text(path: Path, text: str) -> None:
    """Write a file atomically and owner-only: a temporary file in its directory, then a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
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


ABSENT_LISTS = (("exhausted_runs", "exhausted"), ("unfinished_runs", "unfinished"), ("excluded_attempts", "excluded"))


def _absent_runs(export: Mapping[str, Any]) -> list[AbsentRun]:
    """The planned runs the export lists with no analysed attempt, each with the reason, for the pair accounting."""
    return [AbsentRun.from_export(entry, reason) for part, reason in ABSENT_LISTS for entry in export.get(part, [])]


def analyse_study(
    path: str | os.PathLike[str],
    *,
    allow_dirty: bool = False,
    code_root: str | os.PathLike[str] | None = None,
    partial: bool = False,
    stamped: bool = False,
) -> StudyAnalysis:
    """Export the campaign, score its records and run the preregistered analysis; refuse without a current check.

    Only bytes the check verified are used: the snapshot of the store and the replayed records, each read once and
    compared with the check's hash (`study.verified`). Nothing is written, into the batch or anywhere else. With
    `partial`, attempts still running at the check are tolerated and a final attempt that no replayed record covers is
    left out of the analysis and named in the result. With `stamped`, the code identity comes from the stamp beside
    the study and never from a repository around it, as in a replication package.
    """
    study = load_study(path)
    out = out_dir(study)
    batch = out / RUNS_DIR
    if not batch.is_dir():
        raise Refused(f"{out}: no campaign here; run the study first")
    code = code_version(code_root or study.root, study.scope, use_git=not stamped)
    try:
        check = require_current_check(
            batch, code=code, allow_dirty=allow_dirty, rerun_command=f"python -m expcore check {batch}"
        )
        verified = load_verified(batch, check, study.schema, partial=partial)
    except (BatchNotVerified, Unverified) as error:
        raise Refused(f"refusing to analyse: {error}") from None
    plugin = load_plugin(StudyContext(study, code))
    try:
        export = export_run_state(verified.snapshot, excluded=dict.fromkeys(verified.excluded, UNCOVERED))
    finally:
        verified.snapshot.close()
    rows, scores = [], {}
    for entry in export["analysed_attempts"]:
        record = verified.record_for(entry["digest"], Path(entry["run_dir"]).name, entry["run_key"], entry["attempt"])
        if record is None:  # load_verified guarantees one for every attempt it did not exclude
            raise Refused(f"refusing to analyse: no verified record for {entry['run_key']}")
        scored = rescore(record, plugin.scorers)
        scores[entry["run_key"]] = scored.scores
        task = study.tasks[str(entry["factors"]["task"])]
        rows.append(Row.from_export(entry, _outcomes_of(scored), task.attributes))
    try:
        result = analyse(rows, study.prereg, _absent_runs(export))
    except TreatmentOutcomesRefused as error:
        raise Refused(f"refusing to analyse: {error}") from None
    versions = {scorer_name(s): scorer_version(s) for s in plugin.scorers}
    return StudyAnalysis(study, code, sdk_version(CORE_DISTRIBUTION), export, scores, tuple(rows), result, versions)


def _summary(analysis: StudyAnalysis) -> dict[str, Any]:
    """What `analysis.json` holds: the preregistration, the code, the core, each condition's planned runs and how they
    ended (C23), and each contrast with its pairs used and missing by cause, its treatment declaration (S10) and its
    Holm decision."""
    return {
        "preregistration": dict(analysis.result.manifest),
        "alpha": analysis.result.alpha,
        "code": {key: analysis.code.get(key) for key in ("commit", "tree_sha256", "dirty")},
        "core_version": analysis.core_version,
        "scorers": dict(analysis.scorers),
        "partial": analysis.export["partial"],
        "running_attempts": analysis.export["running_attempts"],
        "excluded_attempts": analysis.export["excluded_attempts"],
        "conditions": condition_accounting(analysis.export),
        "contrasts": analysis.result.contrasts,
        "holm": analysis.result.holm,
    }


def report_text(analysis: StudyAnalysis) -> str:
    """The Markdown report of an analysis (`expcore.report`)."""
    return render_report(
        analysis.result,
        analysis.export,
        study=analysis.study.name,
        code=analysis.code,
        core_version=analysis.core_version,
        scorers=analysis.scorers,
        interval_level=analysis.study.prereg.bootstrap.ci_level,
    )


def output_bytes(analysis: StudyAnalysis) -> dict[str, bytes]:
    """The bytes of every file `analyze` and `report` write to out/, by file name, in OUTPUT_FILES order."""
    texts = {
        EXPORT_FILE: export_text(analysis.export),
        SCORES_FILE: _json_text(analysis.scores),
        ANALYSIS_FILE: _json_text(_summary(analysis)),
        REPORT_FILE: report_text(analysis),
    }
    return {name: texts[name].encode("utf-8") for name in OUTPUT_FILES}


def write_analysis(analysis: StudyAnalysis) -> list[Path]:
    """Write the export, the scores and the analysis to out/, each atomically and owner-only; return the files."""
    out, made = out_dir(analysis.study), output_bytes(analysis)
    written = [out / name for name in (EXPORT_FILE, SCORES_FILE, ANALYSIS_FILE)]
    for path in written:
        _write_text(path, made[path.name].decode("utf-8"))
    return written


def write_report(analysis: StudyAnalysis) -> Path:
    """Write out/report.md and return it."""
    target = out_dir(analysis.study) / REPORT_FILE
    target.write_text(report_text(analysis), encoding="utf-8")
    return target


def regenerate(
    path: str | os.PathLike[str], *, allow_dirty: bool = False, partial: bool = False, stamped: bool = False
) -> list[str]:
    """Derive every output anew from the records and compare it byte for byte with the file in out/; write nothing.

    Returns one line per output that is missing or differs, empty when all four match. The refusals of
    `analyse_study` (no current check, records changed since) are raised as they are.
    """
    analysis = analyse_study(path, allow_dirty=allow_dirty, partial=partial, stamped=stamped)
    out, problems = out_dir(analysis.study), []
    for name, made in output_bytes(analysis).items():
        target = out / name
        if not target.is_file():
            problems.append(f"{name}: missing")
        elif target.read_bytes() != made:
            problems.append(f"{name}: differs from what the records give")
    return problems
