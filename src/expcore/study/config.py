# Provenance: new
"""Loading a study directory: its four configuration files, checked against each other.

A study directory holds the four files and the plugin module that study.toml names. study.toml carries what only the
study knows, beside the factors, the identity order and the conditions (`campaign.factors`):

    [study]
    name = "tiny-study"
    plugin = "plugin.py"                  # the module that supplies the executor and the scorers, relative to this file
    code = ["study.toml", "plugin.py"]    # what the code identity covers (`provenance.codestamp.CodeScope`)
    requires = ["toy-platform"]           # optional: distributions beyond the core the study's code imports

    [tasks.task-1]                        # one table per task; every key is a text attribute of the task
    region = "north"

Two optional keys of `[study]` widen the infrastructure kinds a condition may declare as its treatment
(`campaign.factors`). Both take effect before the conditions are validated:

    [study]
    platforms = ["toy_platform"]          # modules imported first, so the kinds they register are known

    [study.kinds]                         # the study's own kinds; treatable is true unless it says false
    relay_flap = { description = "the relay restarted under the run", treatable = true }

A module that cannot be imported is refused, as is a study kind that is a core kind or a platform's. A platform's kinds
are the process's once imported. A study's own last only while it is loaded and while the pipeline works on it
(`errors.study_kinds(study.kinds)`), so a study's kinds do not outlast it. Neither key is part of the factor schema;
study.toml's hash is in the campaign fingerprint, as it always is.

A study whose runs write a file beside each record, and name it by its SHA-256 in that record, declares each such side
file (`replay.side_files`), so that the replay check, the analysis and a replication package verify it:

    [[study.side_files]]
    pattern = "extent_ranges.json.gz"               # one file name, looked for beside each record
    hash_field = "instruments.extent.side_sha256"   # the record field, a dotted path, that holds its SHA-256

A pattern is one literal file name: no directory, no glob character, no leading dot (the check never reads such a
file), and not a name the campaign or the check writes (a record, an outcome marker, the snapshot, the check). A field
is record keys joined by dots. Each pattern and each field is declared once. A declaration changes study.toml, whose
hash is in the campaign fingerprint, so a campaign resumed under another declaration is refused.

Everything is validated when it is loaded, with messages that name the file and the key: the models of the plan are in
the model table, the `[scheduler]` health and identity kinds are kinds known once the platforms are imported, and the
cluster each preregistered contrast names is an attribute of every task. Nothing is run.
"""

import hashlib
import importlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expcore.campaign.config import CampaignConfig, load_campaign
from expcore.campaign.factors import FactorSchema, load_factor_schema
from expcore.campaign.manifest import Manifest, build_manifest
from expcore.campaign.markers import MARKER_NAME
from expcore.campaign.shard import ShardError
from expcore.campaign.shard_budget import shard_budgets
from expcore.campaign.snapshot import SNAPSHOT_FILE
from expcore.config.key_locations import KeyLocationUnreachable, withhold_key_locations
from expcore.config.loading import ConfigError, Section, close_match, load_toml
from expcore.config.models import ModelTable, load_models
from expcore.errors import StudyKind, check_study_kind, infra_kinds, study_kinds
from expcore.provenance.codestamp import CodeScope, CodeScopeError
from expcore.provenance.environment import is_requirement_name
from expcore.replay.check import CHECK_FILE, is_record_name
from expcore.replay.side_files import SideFile
from expcore.stats import prereg
from expcore.stats.prereg import Preregistration

STUDY_FILE = "study.toml"
MODELS_FILE = "models.toml"
CAMPAIGN_FILE = "campaign.toml"
PREREG_FILE = "preregistration.toml"
OUT_DIR = "out"  # everything a study produces goes below it (`study.pipeline`)
CONFIG_FILES = (STUDY_FILE, MODELS_FILE, CAMPAIGN_FILE, PREREG_FILE)
PLATFORMS_KEY, KINDS_KEY, SIDE_FILES_KEY = "platforms", "kinds", "side_files"
KIND_KEYS = ("description", "treatable")
SIDE_FILE_KEYS = ("pattern", "hash_field")
_FIELD = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
_GLOB = frozenset("*?[]")


@dataclass(frozen=True)
class Task:
    """One task of the study: its id and the text attributes a contrast clusters by."""

    id: str
    attributes: Mapping[str, str]


@dataclass(frozen=True)
class StudyConfig:
    """A study directory, loaded: every configuration file validated, none of it run."""

    root: Path
    name: str
    schema: FactorSchema
    tasks: Mapping[str, Task]
    models: ModelTable
    campaign: CampaignConfig
    prereg: Preregistration
    plugin_file: Path
    scope: CodeScope
    config_sha256: Mapping[str, str]
    kinds: Mapping[str, StudyKind] = field(default_factory=dict)  # `[study.kinds]`, for `errors.study_kinds`
    requires: tuple[str, ...] = ()  # distributions beyond the core: part of the campaign's environment
    side_files: tuple[SideFile, ...] = ()  # `[[study.side_files]]`: what the check, analysis and package verify

    @property
    def manifest(self) -> Manifest:
        """The plan in the seeded run order: every condition over its tasks, the models and the seeds."""
        keys = self.schema.expand(self.tasks, self.campaign.models, self.campaign.seeds)
        return build_manifest(keys, self.campaign.order_seed)


@dataclass(frozen=True)
class StudyIdentity:
    """What study.toml says the study is: enough to check its records, with none of its code imported."""

    root: Path
    study_file: Path
    plugin_file: Path
    scope: CodeScope
    platforms: tuple[str, ...]
    kinds: Mapping[str, StudyKind]
    requires: tuple[str, ...]  # distributions beyond the core: what the check compares with the campaign's environment
    side_files: tuple[SideFile, ...] = ()  # `[[study.side_files]]`: what the check pairs with the records


def find_study_root(start: str | os.PathLike[str]) -> Path:
    """Return the nearest directory at or above `start` that holds a study.toml; raise ConfigError when none does."""
    first = Path(start).resolve()
    for directory in (first, *first.parents):
        if (directory / STUDY_FILE).is_file():
            return directory
    raise ConfigError(f"{first}: no {STUDY_FILE} in this directory or any above it; pass the study directory")


def _tasks(top: Section, file: Path) -> dict[str, Task]:
    declared = Section(top.table("tasks"), file, "[tasks]")
    if not declared.data:
        raise top.fail("tasks", "is required and must declare at least one task")
    tasks = {}
    for task_id in declared.data:
        table = declared.section(task_id)
        attributes = {key: table.string(key) for key in table.data}
        tasks[task_id] = Task(task_id, attributes)
    return tasks


def _study_table(top: Section, root: Path) -> tuple[str, Path, CodeScope, tuple[str, ...]]:
    section = Section(top.section("study").data, top.file, "[study]")
    section.reject_unknown(["name", "plugin", "code", PLATFORMS_KEY, KINDS_KEY, "requires", SIDE_FILES_KEY])
    name, plugin = section.string("name"), section.string("plugin")
    plugin_file = (root / plugin).resolve()
    if not plugin_file.is_file() or root.resolve() not in plugin_file.parents:
        raise section.fail("plugin", f"must name a file inside the study directory; {plugin!r} is not one")
    roots = section.strings("code")
    if not roots:
        raise section.fail("code", "must list the files the code identity covers")
    try:
        scope = CodeScope.of(roots, canonical={MODELS_FILE: models_hashed})  # where a key lives is not code (K13)
    except CodeScopeError as error:
        raise section.fail("code", str(error)) from None
    missing = [r for r in roots if not (root / r).exists()]
    if missing:
        raise section.fail("code", f"names {missing[0]!r}, which is not in the study directory")
    requires = section.strings("requires")
    bad = [r for r in requires if not is_requirement_name(r)]
    if bad or len(set(requires)) != len(requires):
        raise section.fail("requires", "must name installed distributions, each once, with no version")
    return name, plugin_file, scope, requires


def _platforms(top: Section) -> tuple[str, ...]:
    return Section(top.section("study").data, top.file, "[study]").strings(PLATFORMS_KEY)


def import_platforms(study_file: Path, modules: Sequence[str]) -> None:
    """Import the modules `[study] platforms` names, so the kinds they register are known for the whole process."""
    section = Section({}, study_file, "[study]")
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception as error:  # an import may fail in any way; each is a platform the study cannot use
            # The module is the study's own declaration. The exception's text may hold a credential, so it stays out.
            raise section.fail(
                PLATFORMS_KEY, f"names module {module!r}, which cannot be imported ({type(error).__name__})"
            ) from None


def _declared_kinds(top: Section) -> dict[str, StudyKind]:
    """Read and check the study's own kinds (`[study.kinds]`); a platform's are known by now, so a clash is seen."""
    section = Section(top.section("study").data, top.file, "[study]")
    kinds = Section(section.table(KINDS_KEY), top.file, f"[study.{KINDS_KEY}]")
    declared = {}
    for kind in kinds.data:
        entry = kinds.section(kind)
        entry.reject_unknown(KIND_KEYS)
        description = entry.string("description")
        treatable = entry.data.get("treatable", True)
        if not isinstance(treatable, bool):
            raise entry.fail("treatable", f"must be true or false, got {type(treatable).__name__}")
        try:
            check_study_kind(kind, description)
        except ValueError as error:
            raise kinds.fail(kind, f"cannot be declared: {error}") from None
        declared[kind] = StudyKind(description, treatable)
    return declared


def _check_scheduler_kinds(campaign: CampaignConfig) -> None:
    """Refuse a `[scheduler]` health or identity kind the vocabulary does not hold, naming campaign.toml and the key.

    Called once the study's platforms are imported and its own kinds are in scope, so the kinds named here are the ones
    the scheduler will find when the study runs. Whether a condition declares one as its treatment stays the
    scheduler's check (`SchedulerConfig.check_kinds`), which also runs when the campaign starts.
    """
    known = infra_kinds()
    section = Section({}, campaign.source, "[scheduler]")
    for key, kind in campaign.scheduler.kind_roles.items():
        if kind not in known:
            raise section.fail(
                key,
                f"names a kind that is not an infrastructure kind{close_match(kind, known)}; the kinds known here "
                f"are: {', '.join(known)}. A platform's kinds are known once its module is listed in [study] "
                f"platforms of {STUDY_FILE}, and the study's own are the ones it declares in [study.{KINDS_KEY}]",
            )


def _side_files(top: Section) -> tuple[SideFile, ...]:
    """Read and check the side files the study declares (`[[study.side_files]]`); none when it declares none."""
    section = Section(top.section("study").data, top.file, "[study]")
    entries = section.data.get(SIDE_FILES_KEY, [])
    if not isinstance(entries, list) or not all(isinstance(entry, Mapping) for entry in entries):
        raise section.fail(SIDE_FILES_KEY, "must be an array of tables, each a [[study.side_files]] table")
    declared: list[SideFile] = []
    for number, data in enumerate(entries, start=1):
        entry = Section(data, top.file, f"[[study.{SIDE_FILES_KEY}]] number {number}")
        entry.reject_unknown(SIDE_FILE_KEYS)
        pattern, hash_field = entry.string("pattern"), entry.string("hash_field")
        problem = _pattern_problem(pattern)
        if problem is not None:
            raise entry.fail("pattern", problem)
        if not _FIELD.fullmatch(hash_field):
            raise entry.fail(
                "hash_field", "must be record keys joined by dots, such as 'instruments.extent.side_sha256'"
            )
        for earlier in declared:
            if pattern == earlier.pattern:
                raise entry.fail("pattern", f"names {pattern!r}, which an earlier [[study.side_files]] names")
            if hash_field == earlier.hash_field:
                raise entry.fail("hash_field", f"names {hash_field!r}, which an earlier [[study.side_files]] names")
        declared.append(SideFile(pattern, hash_field))
    return tuple(declared)


def _pattern_problem(pattern: str) -> str | None:
    """Why a side file's pattern cannot be declared, or None: it must be one literal name the check can pair."""
    if "/" in pattern or "\\" in pattern:
        return "must be a file name, looked for beside each record, not a path"
    if _GLOB & set(pattern):
        return "must be one file name, not a glob: each side file is paired with the one record beside it"
    if pattern.startswith("."):
        return "may not start with a dot: the replay check takes such a file for a temporary one and never reads it"
    if pattern in (CHECK_FILE, SNAPSHOT_FILE) or pattern.endswith(MARKER_NAME) or is_record_name(pattern):
        return f"names {pattern!r}, a file the campaign or the replay check writes"
    return None


def _check_clusters(study: str, tasks: Mapping[str, Task], loaded: Preregistration) -> None:
    """Every contrast clusters by an attribute; a task without it would silently leave the contrast."""
    for contrast in loaded.contrasts.values():
        absent = [t.id for t in tasks.values() if contrast.cluster not in t.attributes]
        if absent:
            raise ConfigError(
                f"{loaded.source}: contrast {contrast.name!r} clusters by {contrast.cluster!r}, which task "
                f"{absent[0]!r} of {study} does not have as an attribute"
            )


def read_study_identity(path: str | os.PathLike[str]) -> StudyIdentity:
    """Read the `[study]` table of a study directory, importing nothing and reading no other configuration file.

    A condition may name a kind that only a platform registers, so the rest of the study cannot be validated without
    importing the platform. The replay check needs only this, and imports the study's code only for a record that
    cannot be read without it.
    """
    root = Path(path).resolve()
    study_file = root / STUDY_FILE
    top = Section(load_toml(study_file), study_file, "top level")
    _, plugin_file, scope, requires = _study_table(top, root)
    kinds, side_files = _declared_kinds(top), _side_files(top)
    return StudyIdentity(root, study_file, plugin_file, scope, _platforms(top), kinds, requires, side_files)


def models_hashed(data: bytes) -> bytes:
    """The bytes the code identity and the fingerprint hash for models.toml: its key locations withheld (K13). A file
    whose key locations cannot be withheld is refused, naming it, so it is never hashed, run or shipped as it is."""
    try:
        return withhold_key_locations(data)
    except KeyLocationUnreachable as error:
        raise KeyLocationUnreachable(f"{MODELS_FILE}: {error}") from None


def _hashed_bytes(root: Path, file: str) -> bytes:
    """The bytes of a configuration file the campaign fingerprint hashes: models.toml without its key locations."""
    data = (root / file).read_bytes()
    return models_hashed(data) if file == MODELS_FILE else data


def load_study(path: str | os.PathLike[str]) -> StudyConfig:
    """Load and cross-check the configuration of a study directory; raise ConfigError naming the file and the key."""
    root = Path(path).resolve()
    study_file = root / STUDY_FILE
    top = Section(load_toml(study_file), study_file, "top level")
    name, plugin_file, scope, requires = _study_table(top, root)
    side_files = _side_files(top)
    import_platforms(study_file, _platforms(top))  # before anything names a kind: the conditions, the campaign's kinds
    kinds = _declared_kinds(top)
    with study_kinds(kinds):  # held for this load only: a failed load, or the next study, finds none of them
        schema = load_factor_schema(study_file)
        tasks = _tasks(top, study_file)
        models = load_models(root / MODELS_FILE)
        campaign = load_campaign(root / CAMPAIGN_FILE)
        _check_scheduler_kinds(campaign)
        unknown = [m for m in campaign.models if m not in models]
        if unknown:
            raise ConfigError(
                f"{campaign.source}: key 'models' names {unknown[0]!r}{close_match(unknown[0], models)}, which "
                f"{MODELS_FILE} does not define; it defines: {', '.join(models)}"
            )
        loaded = prereg.load(root / PREREG_FILE, schema)
        _check_clusters(name, tasks, loaded)
    hashes = {file: hashlib.sha256(_hashed_bytes(root, file)).hexdigest() for file in CONFIG_FILES}
    study = StudyConfig(
        root, name, schema, tasks, models, campaign, loaded, plugin_file, scope, hashes, kinds, requires, side_files
    )
    if campaign.shards is not None and campaign.scheduler.budget is not None:
        try:
            shard_budgets_of(study)
        except ShardError as error:
            raise ConfigError(f"{campaign.source}: [shards] per_model with [budget]: {error}") from None
    return study


def shard_budgets_of(study: StudyConfig) -> dict[int, dict[str, Any]]:
    """Every shard's part of the budget of a study that shards by model and has a budget (C31), by shard index.

    `load_study` refuses a study for which this raises ShardError, so a start finds the same parts.
    """
    layout, budget = study.campaign.shards, study.campaign.scheduler.budget
    if layout is None or budget is None:
        raise ShardError("only a campaign with both [shards] per_model and [budget] shares its budget by model")
    models = {model: study.models.resolve(model) for model in study.campaign.models}
    return shard_budgets(budget, layout, study.manifest, models, study.campaign.agent.limits)
