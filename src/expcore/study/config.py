# Provenance: new
"""Loading a study directory: its four configuration files, checked against each other.

A study directory holds the four files and the plugin module that study.toml names. study.toml carries what only the
study knows, beside the factors, the identity order and the conditions (`campaign.factors`):

    [study]
    name = "tiny-study"
    plugin = "plugin.py"                  # the module that supplies the executor and the scorers, relative to this file
    code = ["study.toml", "plugin.py"]    # what the code identity covers (`provenance.codestamp.CodeScope`)

    [tasks.task-1]                        # one table per task; every key is a text attribute of the task
    region = "north"

Two optional keys of `[study]` widen the infrastructure kinds a condition may declare as its treatment
(`campaign.factors`). Both take effect before the conditions are validated:

    [study]
    platforms = ["toy_platform"]          # modules imported first, so the kinds they register are known

    [study.kinds]                         # the study's own kinds; treatable is true unless it says false
    relay_flap = { description = "the relay restarted under the run", treatable = true }

A module that cannot be imported is refused, as is a study kind that is a core kind or a platform's. Neither key is part
of the factor schema; study.toml's hash is in the campaign fingerprint, as it always is.

Everything is validated when it is loaded, with messages that name the file and the key: the models of the plan are in
the model table, and the cluster each preregistered contrast names is an attribute of every task. Nothing is run.
"""

import hashlib
import importlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from expcore.campaign.config import CampaignConfig, load_campaign
from expcore.campaign.factors import FactorSchema, load_factor_schema
from expcore.campaign.manifest import Manifest, build_manifest
from expcore.config.loading import ConfigError, Section, load_toml
from expcore.config.models import ModelTable, load_models
from expcore.errors import register_study_kind
from expcore.provenance.codestamp import CodeScope, CodeScopeError
from expcore.stats import prereg
from expcore.stats.prereg import Preregistration

STUDY_FILE = "study.toml"
MODELS_FILE = "models.toml"
CAMPAIGN_FILE = "campaign.toml"
PREREG_FILE = "preregistration.toml"
CONFIG_FILES = (STUDY_FILE, MODELS_FILE, CAMPAIGN_FILE, PREREG_FILE)
PLATFORMS_KEY, KINDS_KEY = "platforms", "kinds"
KIND_KEYS = ("description", "treatable")


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

    @property
    def manifest(self) -> Manifest:
        """The plan in the seeded run order: every condition over its tasks, the models and the seeds."""
        keys = self.schema.expand(self.tasks, self.campaign.models, self.campaign.seeds)
        return build_manifest(keys, self.campaign.order_seed)


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


def _study_table(top: Section, root: Path) -> tuple[str, Path, CodeScope]:
    section = Section(top.section("study").data, top.file, "[study]")
    section.reject_unknown(["name", "plugin", "code", PLATFORMS_KEY, KINDS_KEY])
    name, plugin = section.string("name"), section.string("plugin")
    plugin_file = (root / plugin).resolve()
    if not plugin_file.is_file() or root.resolve() not in plugin_file.parents:
        raise section.fail("plugin", f"must name a file inside the study directory; {plugin!r} is not one")
    roots = section.strings("code")
    if not roots:
        raise section.fail("code", "must list the files the code identity covers")
    try:
        scope = CodeScope.of(roots)
    except CodeScopeError as error:
        raise section.fail("code", str(error)) from None
    missing = [r for r in roots if not (root / r).exists()]
    if missing:
        raise section.fail("code", f"names {missing[0]!r}, which is not in the study directory")
    return name, plugin_file, scope


def _widen_kinds(top: Section) -> None:
    """Import the platforms `[study] platforms` names and register the kinds of `[study.kinds]`, in that order."""
    section = Section(top.section("study").data, top.file, "[study]")
    for module in section.strings(PLATFORMS_KEY):
        try:
            importlib.import_module(module)
        except Exception as error:  # an import may fail in any way; each is a platform the study cannot use
            # The module is the study's own declaration. The exception's text may hold a credential, so it stays out.
            raise section.fail(
                PLATFORMS_KEY, f"names module {module!r}, which cannot be imported ({type(error).__name__})"
            ) from None
    kinds = Section(section.table(KINDS_KEY), top.file, f"[study.{KINDS_KEY}]")
    for kind in kinds.data:
        entry = kinds.section(kind)
        entry.reject_unknown(KIND_KEYS)
        description = entry.string("description")
        treatable = entry.data.get("treatable", True)
        if not isinstance(treatable, bool):
            raise entry.fail("treatable", f"must be true or false, got {type(treatable).__name__}")
        try:
            register_study_kind(kind, description, treatable=treatable)
        except ValueError as error:
            raise kinds.fail(kind, f"cannot be declared: {error}") from None


def _check_clusters(study: str, tasks: Mapping[str, Task], loaded: Preregistration) -> None:
    """Every contrast clusters by an attribute; a task without it would silently leave the contrast."""
    for contrast in loaded.contrasts.values():
        absent = [t.id for t in tasks.values() if contrast.cluster not in t.attributes]
        if absent:
            raise ConfigError(
                f"{loaded.source}: contrast {contrast.name!r} clusters by {contrast.cluster!r}, which task "
                f"{absent[0]!r} of {study} does not have as an attribute"
            )


def load_study(path: str | os.PathLike[str]) -> StudyConfig:
    """Load and cross-check the configuration of a study directory; raise ConfigError naming the file and the key."""
    root = Path(path).resolve()
    study_file = root / STUDY_FILE
    top = Section(load_toml(study_file), study_file, "top level")
    name, plugin_file, scope = _study_table(top, root)
    _widen_kinds(top)  # before anything names a kind: the conditions here, the campaign's kinds below
    schema = load_factor_schema(study_file)
    tasks = _tasks(top, study_file)
    models = load_models(root / MODELS_FILE)
    campaign = load_campaign(root / CAMPAIGN_FILE)
    unknown = [m for m in campaign.models if m not in models]
    if unknown:
        raise ConfigError(f"{campaign.source}: key 'models' names {unknown[0]!r}, which {MODELS_FILE} does not define")
    loaded = prereg.load(root / PREREG_FILE, schema)
    _check_clusters(name, tasks, loaded)
    hashes = {file: hashlib.sha256((root / file).read_bytes()).hexdigest() for file in CONFIG_FILES}
    return StudyConfig(root, name, schema, tasks, models, campaign, loaded, plugin_file, scope, hashes)
