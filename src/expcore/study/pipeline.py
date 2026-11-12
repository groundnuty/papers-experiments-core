# Provenance: new
"""The pipeline of a study directory: run the campaign, check the records, analyse them (the CLI is a thin shell).

Everything a study produces goes under `<study>/out/`: the campaign store (`state.sqlite`), the leases, the runs
(`runs/<digest>/try<N>/`, one record and one outcome marker each), and what `analyse_study` writes (`export.json`,
`scores.json`, `analysis.json`).

- `run_study` opens or resumes the campaign and runs the scheduler. Its code identity is computed once, from the study
  directory and the scope study.toml names, and every record carries it. Its environment (the distributions the
  study's code imports, `provenance.environment`) goes into the campaign fingerprint, so a resume under another one
  is refused (C06). Each hold is said on stderr once, as it begins, changes or ends, with when dispatch resumes, so a
  run waiting out a pause is not silent. With a `shard` (#154) it runs that shard's part of the plan: the fingerprint
  is still computed from the totals, of the whole plan, and only then are the caps and the budget replaced by the
  shard's shares and the plugin's environment identity read, once. A shard says its environment on stderr as it
  starts (`campaign.shard_run.environment_block`), so a host that differs from the others shows before it runs.
- `check_batch` is the replay check over `out/runs`. The outcome markers are the campaign's own bookkeeping, not
  records: the check hashes them and does not replay them. It also copies the campaign store into the batch
  (`campaign-snapshot.sqlite`) and hashes the copy, so the analysis never has to read the live store; it refuses a
  store with attempts still running unless `partial`, which the check records. It reads only the study's `[study]`
  table and imports nothing, unless a record needs it, and says so first: a kind still unknown imports the modules
  `[study] platforms` names, and a backend no native parser is registered for also imports the plugin module, so
  what is registered there is known in a new process. `build` is not called, so no model or key is needed. A merged
  batch (`campaign.merge`) is the exception: its merge.json is rebuilt from the shards' snapshots and the study's plan,
  so its check loads the study's configuration, which imports those platforms.
  It refuses to run under another environment than the campaign's, and records the one it ran under. The side
  files study.toml declares (`[[study.side_files]]`) are found in the batch it parses, hashed with the records and
  paired each with the record beside it (`replay.side_files`), and the check records the declaration.
- `analyse_study` refuses unless the batch has a current passing check (R04, R05), made under the environment in use
  and the campaign's (the snapshot's fingerprint) and under the study's declaration of side files, and writes nothing.
  It then uses only bytes the check verified (`study.verified`): the snapshot and the replayed records, each read once
  and compared with the check's hash, cross-validated against each other, and each side file paired with its record
  again; a merged batch's merge.json is rebuilt from the snapshots so read and the plan, and refused when it differs.
  It exports from the snapshot, scores the records in memory (a record is never written after its check, since
  that would change its hash) and runs the preregistered analysis. A scorer that takes `side_files` is handed the
  record's side files as the bytes `load_verified` read and verified, all of them before the plugin is loaded, so
  no scorer runs on a batch that does not verify. Every score is derived anew from a record on each call and none is
  read back: `scores.json` is an output, so `analyze` is how a study is re-scored (R13), offline, after a change to a
  scorer and a new check (a scorer is code the identity covers).
- `output_bytes` is the one place the bytes of `export.json`, `scores.json`, `analysis.json` and `report.md` are made;
  `write_analysis` and `write_report` write them and `regenerate` compares them with the files in `out/`, which is
  what a replication package's `make check` runs. Every time in them comes from the records and the store, every
  list has a defined order, and no path of the machine appears, so the same records give the same bytes anywhere.
"""

import collections
import dataclasses
import hashlib
import json
import math
import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.accounting import condition_accounting
from expcore.campaign.executor import default_backend_factory
from expcore.campaign.export import export_run_state, export_text
from expcore.campaign.export_join import join_exports
from expcore.campaign.lease import LeasePool
from expcore.campaign.manifest import Manifest, build_fingerprint, shard_manifest
from expcore.campaign.merge import CHECK_KEY as MERGE_KEY
from expcore.campaign.merge import MergeRefused, read_merge
from expcore.campaign.merged import read_held, verify_merged, verify_queried
from expcore.campaign.resume import FINGERPRINT_KEY, FingerprintMismatch, read_fingerprint, recorded_fingerprint
from expcore.campaign.scheduler import STATE_FILE, RunSummary, Scheduler
from expcore.campaign.settings import SchedulerConfig
from expcore.campaign.shard import MERGE_FILE, ModelShards, ShardError, ShardSpec, by_model, check_identity, pair_of
from expcore.campaign.shard_run import (
    ShardStart,
    environment_block,
    refuse_unrunnable,
    share_settings,
    share_settings_by_model,
)
from expcore.campaign.snapshot import SNAPSHOT_FILE, SnapshotError, take_snapshot
from expcore.config.key_locations import withheld_entry, withhold_key_locations
from expcore.config.models import ModelSpec
from expcore.errors import study_kinds
from expcore.models.route import sdk_version
from expcore.provenance.codestamp import require_known
from expcore.provenance.codestamp import version as code_version
from expcore.provenance.environment import EnvironmentUnknown, differences, environment
from expcore.records.build import CORE_DISTRIBUTION
from expcore.records.record import RunRecord
from expcore.records.rescore import rescore, scorer_name, scorer_version
from expcore.replay.check import (
    BatchNotVerified,
    require_current_check,
    verify_batch,
    what_records_need,
    with_side_files,
    write_check,
)
from expcore.replay.native import has_native_parser
from expcore.report import render_report
from expcore.stats.analysis import AnalysisResult, TreatmentOutcomesRefused, analyse
from expcore.stats.rows import AbsentRun, Row
from expcore.study.config import (
    MODELS_FILE,
    OUT_DIR,
    StudyConfig,
    StudyIdentity,
    find_study_root,
    import_platforms,
    load_study,
    read_study_identity,
    shard_budgets_of,
)
from expcore.study.factory_version import check_factory_version
from expcore.study.plugin import StudyContext, StudyPlugin, import_plugin_module, load_plugin
from expcore.study.verified import UNCOVERED, Unverified, VerifiedBatch, load_verified

RUNS_DIR = "runs"
MARKER_PATTERN = "*attempt-marker.json"
HASHED_ONLY = (MARKER_PATTERN, SNAPSHOT_FILE)  # the campaign's own bookkeeping in a batch: hashed, never replayed
EXPORT_FILE = "export.json"
SCORES_FILE = "scores.json"
ANALYSIS_FILE = "analysis.json"
REPORT_FILE = "report.md"
OUTPUT_FILES = (EXPORT_FILE, SCORES_FILE, ANALYSIS_FILE, REPORT_FILE)  # what `analyze` and `report` write to out/
SNAPSHOT_KEY = "snapshot"  # the key of the check that says what its snapshot of the store held


class Refused(RuntimeError):
    """A command refused to go on; the message says why and what to do, and the command line names the command."""


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


def out_dir(study: StudyConfig | StudyIdentity) -> Path:
    """The directory a study writes to."""
    return study.root / OUT_DIR


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def model_digest(spec: ModelSpec) -> str:
    """The digest of a model entry in the campaign fingerprint: every field of it, each key location as its placeholder
    (K13), and its declared set of accepted fingerprints only when it declares one (B18). An entry that names no key
    location and declares no set keeps the digest it had before either."""
    return _digest(_fingerprinted(withheld_entry(spec)))


def _counted_digest(spec: ModelSpec) -> str:
    """The digest a core before K13 gave a model entry, where each key lives counted; for a refused resume's hint."""
    return _digest(_fingerprinted(dataclasses.asdict(spec)))


def _fingerprinted(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A model entry as its digest takes it: without `accepted_fingerprints` when it declares none (B18)."""
    return {k: v for k, v in entry.items() if k != "accepted_fingerprints" or v}


def study_environment(study: StudyConfig | StudyIdentity) -> dict[str, Any]:
    """The environment of the study's code: the core and what `[study] requires` names, with their requirements."""
    try:
        return environment(study.requires)
    except EnvironmentUnknown as error:
        raise Refused(f"{study.root}: {error}") from None


def campaign_environment(fingerprint: Mapping[str, Any] | None) -> Any:
    """The environment a campaign's fingerprint records; None when it records none."""
    return None if fingerprint is None else fingerprint.get("environment")


def fingerprint_of(study: StudyConfig, plugin: StudyPlugin) -> dict[str, Any]:
    """The campaign fingerprint: what makes two starts one experiment (C06), from the study and its plugin.

    A plugin's `backend_factory` enters as its name and its `version`. Code that lives in the study directory is
    already covered by the code identity every record carries; the version is what speaks for a factory that lives
    in an installed package, and it is refused unless it is that package's installed version (`factory_version`).
    Where a model's key lives is left out, of each model's digest and of models.toml's hash (K13).
    """
    check_factory_version(plugin, study)
    return build_fingerprint(
        study.schema,
        tasks={task.id: _digest(dict(task.attributes)) for task in study.tasks.values()},
        models={key: model_digest(study.models.resolve(key)) for key in study.campaign.models},
        seeds=study.campaign.seeds,
        caps=study.campaign.caps(),
        prompts=plugin.prompts,
        tools=plugin.tools,
        scorer_digest=plugin.scorer_digest(),
        configs=study.config_sha256,
        platform=plugin.platform,
        backend_factory=plugin.backend_factory_identity(),
        environment=study_environment(study),
    )


def run_study(
    path: str | os.PathLike[str],
    *,
    allow_dirty: bool = False,
    code_root: str | os.PathLike[str] | None = None,
    crash_hook: Callable[[str, str], None] | None = None,
    shard: ShardSpec | None = None,
) -> RunSummary:
    """Open or resume the study's campaign and run the scheduler until nothing more can start.

    Refuses code that cannot be vouched for (unknown, stale, or dirty unless `allow_dirty`). The scheduler returns when
    no run is in flight and every pending run is held by something that will not end by itself (a budget, a stop):
    a command line has no one to release it. Every backend is built by the plugin's `backend_factory`, or the core's
    when it supplies none. `crash_hook` is the scheduler's own test seam for killing the process at a chosen point of
    an attempt. With `shard` the campaign is that shard of N: its part of the plan, its shares of the caps and the
    budget, and the environment identity the plugin gives, kept in the store (`campaign.shard_run`); a shard that has
    no run, or whose share of a total is nothing, is refused before the store is touched.
    """
    study = load_study(path)
    with study_kinds(study.kinds):  # the attempts of the run end in these kinds, and its threads see them
        return _run_campaign(study, allow_dirty, code_root, crash_hook, shard)


def _run_campaign(
    study: StudyConfig,
    allow_dirty: bool,
    code_root: str | os.PathLike[str] | None,
    crash_hook: Callable[[str, str], None] | None,
    shard: ShardSpec | None = None,
) -> RunSummary:
    """Run the campaign of a loaded study, with its own infrastructure kinds in scope."""
    code = require_known("run", code_root or study.root, study.scope, allow_dirty=allow_dirty)
    plugin = load_plugin(StudyContext(study, code))
    out = out_dir(study)
    leases = None if plugin.probe is None else LeasePool(out / "leases", plugin.lease_keys, plugin.probe)
    settings = dataclasses.replace(study.campaign.scheduler, exit_when_blocked=True)
    fingerprint = fingerprint_of(study, plugin)  # from the totals, so every shard has the whole plan's fingerprint
    manifest, started = study.manifest, None
    if shard is not None:
        manifest, settings, started = _shard_of(study, plugin, settings, shard)
        print(environment_block(started.spec, fingerprint["environment"]), file=sys.stderr)  # before a store refuses
    options = {} if crash_hook is None else {"crash_hook": crash_hook}
    try:
        with Scheduler(
            out,
            study.schema,
            manifest,
            fingerprint,
            plugin.executor,
            agent_config=study.campaign.agent,
            models=study.models,
            endpoints=study.campaign.endpoints,
            settings=settings,
            health=plugin.health,
            leases=leases,
            lease_group=plugin.lease_group,
            admission=plugin.admission,
            backend_factory=default_backend_factory if plugin.backend_factory is None else plugin.backend_factory,
            on_hold=_say_hold,
            shard=started,
            **options,
        ) as scheduler:
            return scheduler.run()
    except FingerprintMismatch as refused:
        hint = _counted_key_locations(study, read_fingerprint(out / STATE_FILE))
        if not hint:
            raise
        raise FingerprintMismatch(refused.differences, hint=hint) from None


def _counted_key_locations(study: StudyConfig, recorded: Mapping[str, Any] | None) -> str:
    """Name the leaves of a recorded fingerprint that hold exactly what an earlier core gave by counting where each key
    lives, which this one leaves out (K13), so a refused resume says why they differ; empty when none does."""
    configs = recorded.get("configs") if isinstance(recorded, Mapping) else None
    models = recorded.get("models") if isinstance(recorded, Mapping) else None
    raw = (study.root / MODELS_FILE).read_bytes()
    leaves = []
    if isinstance(configs, Mapping) and raw != withhold_key_locations(raw):
        leaves += [f"configs.{MODELS_FILE}"] if configs.get(MODELS_FILE) == hashlib.sha256(raw).hexdigest() else []
    for key in study.campaign.models if isinstance(models, Mapping) else ():
        spec = study.models.resolve(key)
        counted = _counted_digest(spec)
        leaves += [f"models.{key}"] if counted != model_digest(spec) and models.get(key) == counted else []
    if not leaves:
        return ""
    return (
        f" ({', '.join(leaves)}: recorded with where each key lives, which the core that started the campaign counted "
        "and this one leaves out (K13); finish the campaign with that core, or start a new store)"
    )


def _shard_of(
    study: StudyConfig, plugin: StudyPlugin, settings: SchedulerConfig, spec: ShardSpec
) -> tuple[Manifest, SchedulerConfig, ShardStart]:
    """A shard's part of the plan, its shares of the caps and the budget and the identity it runs against.

    A campaign that declares `[shards] per_model` shards by model (`expcore-shard/3`, C32): the command line's spec
    takes that rule, and its N must be the layout's sum. Every refusal is made here, before the store is opened: an N
    that is not the sum, a shard with no run, a shard holding pairs of a model whose concurrency share is 0 (the
    message names the model), a total smaller than the number of shards (the message names the key), a budget that
    cannot pay for a run of every priced shard, and an environment identity that the plugin cannot give or that is
    not names mapped to SHA-256 digests (the message names the plugin).
    """
    layout = study.campaign.shards
    try:
        spec = spec if layout is None else by_model(spec, layout)
        manifest = shard_manifest(study.manifest, spec, layout)
    except ShardError as error:
        raise Refused(str(error)) from None
    if not manifest.rows:
        raise Refused(_no_run(spec, layout))
    unit_of = {model: endpoint.unit for model, endpoint in study.campaign.endpoints.items()}
    pairs = collections.Counter(model for _, model, _ in {pair_of(row.key.factors) for row in manifest.rows})
    try:
        refuse_unrunnable(spec, pairs, unit_of, settings.unit_caps, layout)
        if layout is None:
            shared, shares = share_settings(settings, spec)
        else:
            model = layout.at(spec.index)[0]
            budget = None if settings.budget is None else shard_budgets_of(study)[spec.index]
            shared, shares = share_settings_by_model(settings, spec, layout, unit_of[model], budget)
    except ShardError as error:
        raise Refused(str(error)) from None
    return manifest, shared, ShardStart(spec, _identity_of(study, plugin), shares)


def _no_run(spec: ShardSpec, layout: ModelShards | None) -> str:
    """Why a shard with no run of the plan is refused, and what to change."""
    if layout is None:
        return (
            f"shard {spec.index} of {spec.count} gets no run of this plan: there are fewer pairs of task, model and "
            "seed than shards; run with fewer shards"
        )
    model, k, count = layout.at(spec.index)
    return (
        f"shard {spec.index} of {spec.count} (model {model}, its shard {k} of {count}) gets no run of this plan: the "
        f"model has fewer pairs of task, model and seed than shards; give it fewer in [shards] per_model"
    )


def _identity_of(study: StudyConfig, plugin: StudyPlugin) -> dict[str, str]:
    """The plugin's environment identity, read once and checked; empty when it declares none.

    The plugin's own function can raise, and what it returns can be of a form no identity has: each is a refusal that
    names the plugin's file (by its name, not the path of this machine), not a traceback.
    """
    if plugin.environment_identity is None:
        return {}
    where = f"{study.plugin_file.name}: the plugin's environment_identity()"
    try:
        given = plugin.environment_identity()
    except Exception as error:  # the plugin is the study's code, and whatever it raises is the study's to fix
        detail = f": {error}" if str(error) else ""
        raise Refused(f"{where} raised {type(error).__name__}{detail}") from error
    try:
        return check_identity(given)
    except ShardError as error:
        raise Refused(f"{where} gave an unusable identity: {error}") from None


def _say_hold(line: str) -> None:
    """Say a hold of the campaign as it begins, changes or ends, on stderr."""
    print(f"run: {line}", file=sys.stderr)


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
    study = read_study_identity(find_study_root(study_root if study_root is not None else batch))
    with study_kinds(study.kinds):  # a replayed record may end in one of the study's own kinds
        return _check_batch_of(study, batch, allow_dirty, code_root, partial)


def _check_batch_of(
    study: StudyIdentity,
    batch: str | os.PathLike[str],
    allow_dirty: bool,
    code_root: str | os.PathLike[str] | None,
    partial: bool,
) -> dict[str, Any]:
    """Snapshot the store and replay the batch of a study, with its own infrastructure kinds in scope."""
    code = require_known("replay check", code_root or study.root, study.scope, allow_dirty=allow_dirty)
    directory = Path(batch)
    if not directory.is_dir():
        raise Refused(f"{directory}: no batch of records here; run the study first")
    env = study_environment(study)
    store = out_dir(study) / STATE_FILE
    if (out_dir(study) / MERGE_FILE).is_file():
        if store.is_file():
            raise Refused(
                f"{out_dir(study)}: holds a merged batch ({MERGE_FILE}) and a campaign store; use one or the other"
            )
        return _check_merged(study, directory, code, env, partial)
    if store.is_file():  # without a store, take_snapshot refuses below
        changed = differences(campaign_environment(read_fingerprint(store)), env)
        if changed:
            raise Refused(
                f"{store}: the campaign ran under another environment than this check would; check under the "
                "campaign's environment:\n  " + "\n  ".join(changed)
            )
    import_what_the_records_need(study, directory)
    try:
        running = take_snapshot(store, directory / SNAPSHOT_FILE, allow_running=partial)
    except SnapshotError as error:
        raise Refused(str(error)) from None
    check = verify_batch(
        directory, code=code, hashed_only=list(HASHED_ONLY), environment=env, side_files=study.side_files
    )
    check[SNAPSHOT_KEY] = {"file": SNAPSHOT_FILE, "attempts_running": running, "partial": partial}
    write_check(directory, check)
    return check


def _check_merged(
    study: StudyIdentity, directory: Path, code: Mapping[str, Any], env: Mapping[str, Any], partial: bool
) -> dict[str, Any]:
    """The replay check of a merged batch: merge.json is bound by its hash, each shard's snapshot is compared with the
    hash merge.json names and read under the environment in use, merge.json is rebuilt from the snapshots and the
    study's plan and must be that document (`campaign.merged`), and the records are replayed as one batch.

    There is no live store to copy: the shards' snapshots were taken at the merge. Rebuilding merge.json needs the
    plan, so the check of a merged batch loads the study's configuration, which imports the platforms `[study]` names.
    The check records the SHA-256 of merge.json (`check["merge"]`), and the analysis reads the snapshots against the
    hashes that file holds."""
    out = out_dir(study)
    raw = (out / MERGE_FILE).read_bytes()
    try:
        merged = read_merge(raw, out / MERGE_FILE)
        held = read_held(out, merged)
    except MergeRefused as error:
        raise Refused(str(error)) from None
    for shard in held.values():
        changed = differences(campaign_environment(shard.fingerprint), env)
        if changed:
            raise Refused(
                f"{shard.name}: its campaign ran under another environment than this check would; check under the "
                "campaign's environment:\n  " + "\n  ".join(changed)
            )
    try:
        verify_merged(merged, held, load_study(study.root).manifest)
    except MergeRefused as error:
        raise Refused(str(error)) from None
    import_what_the_records_need(study, directory)
    check = verify_batch(
        directory, code=code, hashed_only=[MARKER_PATTERN], environment=env, side_files=study.side_files
    )
    check[MERGE_KEY] = {"file": MERGE_FILE, "sha256": hashlib.sha256(raw).hexdigest(), "partial": partial}
    write_check(directory, check)
    return check


def import_what_the_records_need(study: StudyIdentity, batch: Path) -> None:
    """Import only what a record of the batch cannot be read without, and say so first.

    The study's own kinds are in scope already, read as data. A record that names a kind still unknown imports the
    modules `[study] platforms` names, installed code the study declares, and not the plugin; a kind still unknown after
    that fails its record. A record that names a backend no parser is registered for also imports the plugin, whose
    code is the study's own. A batch that needs neither imports nothing.
    """
    needs = what_records_need(batch, with_side_files(HASHED_ONLY, study.side_files))  # a side file is no record
    if needs.kinds and study.platforms:
        print(
            f"check: importing the study's platforms ({', '.join(study.platforms)}) to read the records of "
            f"infrastructure kind {', '.join(map(repr, needs.kinds))}; this runs those modules",
            file=sys.stderr,
        )
        import_platforms(study.study_file, study.platforms)
    unread = [backend for backend in needs.backends if not has_native_parser(backend)]  # a platform may have added it
    if not unread:
        return
    names = ", ".join(map(repr, unread))
    print(
        f"check: importing the study's platforms and plugin ({study.plugin_file.name}) to read the records of backend "
        f"{names}; this runs the study's code",
        file=sys.stderr,
    )
    import_platforms(study.study_file, study.platforms)
    import_plugin_module(study, error_text=False)


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
    with study_kinds(study.kinds):  # the snapshot's attempts and the records may end in one of the study's own kinds
        return _analyse_loaded(study, allow_dirty, code_root, partial, stamped)


def _analyse_loaded(
    study: StudyConfig,
    allow_dirty: bool,
    code_root: str | os.PathLike[str] | None,
    partial: bool,
    stamped: bool,
) -> StudyAnalysis:
    """Analyse the records of a loaded study, with its own infrastructure kinds in scope."""
    out = out_dir(study)
    batch = out / RUNS_DIR
    if not batch.is_dir():
        raise Refused(f"{out}: no campaign here; run the study first")
    code = code_version(code_root or study.root, study.scope, use_git=not stamped)
    env = study_environment(study)
    try:
        check = require_current_check(
            batch,
            code=code,
            allow_dirty=allow_dirty,
            rerun_command=f"python -m expcore check {batch}",
            environment=env,
            compare_sources=not stamped,
            side_files=study.side_files,
        )
        if (out / MERGE_FILE).is_file() and MERGE_KEY not in check:
            raise BatchNotVerified(
                f"{batch}: the replay check was made without {MERGE_FILE}, which is here now; "
                f"run `python -m expcore check {batch}` again"
            )
        verified = load_verified(batch, check, study.schema, partial=partial, side_files=study.side_files)
    except (BatchNotVerified, Unverified) as error:
        raise Refused(str(error)) from None
    if verified.merge is not None:  # what merge.json says must be what its snapshots say, for this plan
        try:
            queries = {index: snapshot.q for index, snapshot in verified.snapshots.items() if index is not None}
            verify_queried(verified.merge, queries, out, study.manifest)
        except MergeRefused as error:
            verified.close()
            raise Refused(str(error)) from None
    for snapshot in verified.snapshots.values():
        rows = snapshot.q("SELECT value FROM meta WHERE key=?", (FINGERPRINT_KEY,))
        changed = differences(campaign_environment(recorded_fingerprint(rows)), check["environment"])
        if changed:
            verified.close()
            raise Refused(
                "the replay check ran under another environment than the campaign's:\n  " + "\n  ".join(changed)
            )
    plugin = load_plugin(StudyContext(study, code))
    try:
        export = _export_of(verified, study.manifest)
    finally:
        verified.close()
    rows, scores = [], {}
    for entry in export["analysed_attempts"]:
        where = (entry["digest"], Path(entry["run_dir"]).name, entry["run_key"], entry["attempt"])
        record = verified.record_for(*where)
        if record is None:  # load_verified guarantees one for every attempt it did not exclude
            raise Refused(f"no verified record for {entry['run_key']}")
        scored = rescore(record, plugin.scorers, side_files=verified.side_files_for(*where))
        scores[entry["run_key"]] = scored.scores
        task = study.tasks[str(entry["factors"]["task"])]
        rows.append(Row.from_export(entry, _outcomes_of(scored), task.attributes))
    try:
        result = analyse(rows, study.prereg, _absent_runs(export))
    except TreatmentOutcomesRefused as error:
        raise Refused(str(error)) from None
    versions = {scorer_name(s): scorer_version(s) for s in plugin.scorers}
    return StudyAnalysis(study, code, sdk_version(CORE_DISTRIBUTION), export, scores, tuple(rows), result, versions)


def _export_of(verified: VerifiedBatch, plan: Manifest) -> dict[str, Any]:
    """The export the analysis reads: the campaign's from its snapshot, or, for a merged batch, the shards' exports
    joined in the order of the whole plan (`campaign.export_join`)."""
    left_out = dict.fromkeys(verified.excluded, UNCOVERED)
    if None in verified.snapshots:
        return export_run_state(verified.snapshots[None], excluded=left_out)
    parts = {}
    for index, snapshot in verified.snapshots.items():
        held = {row["run_key"] for row in snapshot.q("SELECT run_key FROM runs")}
        parts[index] = export_run_state(snapshot, excluded={key: why for key, why in left_out.items() if key in held})
    return join_exports(parts, {row.key.text: row.seq for row in plan.rows}, verified.shards)


def _summary(analysis: StudyAnalysis) -> dict[str, Any]:
    """What `analysis.json` holds: the preregistration, the code, the core, each condition's planned runs and how they
    ended (C23), and each contrast with its pairs used and missing by cause, its treatment declaration (S10) and its
    Holm decision. `exploratory_only` is true when the preregistration makes no confirmatory claim (S12): there is then
    no Holm decision, and every contrast is exploratory."""
    return {
        "preregistration": dict(analysis.result.manifest),
        "exploratory_only": analysis.result.exploratory_only,
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
