# Provenance: new
"""The study's plugin module: how the core finds the executor, the scorers and the platform's probes.

study.toml names one Python file (`[study] plugin`). The core loads it and calls its `build` function with a
`StudyContext`; `build` returns a `StudyPlugin`:

    def build(context: StudyContext) -> StudyPlugin: ...

The module is the study's own code and lives in the study directory, so the code identity covers it. Nothing in the core
imports a study by name.
"""

import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from expcore.campaign.executor import BackendFactory, RunExecutor
from expcore.campaign.healthgate import HealthCheck
from expcore.campaign.lease import ResourceProbe
from expcore.config.loading import ConfigError, Section
from expcore.config.models import BACKENDS
from expcore.plugin import ToolSpec
from expcore.records.rescore import Scorer, scorer_name, scorer_version
from expcore.study.config import StudyConfig

BUILD = "build"


@dataclass(frozen=True)
class StudyContext:
    """What the core hands the plugin's `build`: the loaded study and the code identity every record will carry.

    `code` is computed once by the core, from the study directory and `StudyConfig.scope`; the plugin passes it to
    `record_run` unchanged and never computes it.
    """

    study: StudyConfig
    code: Mapping[str, Any]


@dataclass(frozen=True)
class StudyPlugin:
    """What a study supplies.

    - `executor`: runs one attempt and returns its record (`campaign.executor.RunExecutor`).
    - `scorers`: pure functions of a record (`records.rescore.Scorer`); their names must differ.
    - `prompts`, `tools`: for each condition, the SHA-256 of its prompt template and of its tool manifest
      (`tools_sha256`); they go into the campaign fingerprint, so a resume cannot change either unnoticed.
    - `health`: the platform's quick look at whether runs can start; None when it has no such check.
    - `probe`, `lease_group`, `lease_keys`: the platform's resources. `lease_group` maps a run's factor values to the
      group of leases it needs (None: no lease) and `lease_keys` the keys of a group; `probe` answers for holders and
      resources. Leave them out for a study that shares nothing.
    - `platform`: the platform's settings that make two starts one experiment, as plain data without a secret (the
      Onedata layer's `PlatformConfig.fingerprint()`: its TLS mode and CA bundle hash). They go into the campaign
      fingerprint, so a resume under another is refused. Leave it empty for a study without a platform.
    - `backend_factory`: builds the backend of every model, for admission and for each run, in place of the core's
      `default_backend_factory`; None keeps the core's. A model whose backend the core does not build
      (`config.models.BACKENDS`) needs one, and the factory decides which models it serves: it hands the others to
      `default_backend_factory`.
    """

    executor: RunExecutor
    scorers: Sequence[Scorer]
    prompts: Mapping[str, str]
    tools: Mapping[str, str]
    health: HealthCheck | None = None
    probe: ResourceProbe | None = None
    lease_group: Callable[[Mapping[str, Any]], str | None] = lambda factors: None
    lease_keys: Callable[[str], Sequence[str]] = lambda group: ()
    platform: Mapping[str, Any] = field(default_factory=dict)
    backend_factory: BackendFactory | None = None

    def scorer_digest(self) -> str:
        """The SHA-256 of the scorers' names and versions, for the campaign fingerprint."""
        entries = sorted([scorer_name(s), scorer_version(s)] for s in self.scorers)
        return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode("utf-8")).hexdigest()


def tools_sha256(specs: Sequence[ToolSpec]) -> str:
    """The SHA-256 of a tool set: each tool's name, description, schema and terminal flag, in name order."""
    entries = sorted(
        [{"name": s.name, "description": s.description, "input_schema": s.input_schema, "terminal": s.terminal}
         for s in specs],
        key=lambda entry: entry["name"],
    )  # fmt: skip
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def load_plugin(context: StudyContext) -> StudyPlugin:
    """Import the study's plugin module from its file and call its `build`; raise ConfigError when it cannot serve."""
    file = context.study.plugin_file
    module_name = f"expcore_study_plugin_{hashlib.sha256(str(file).encode()).hexdigest()[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, file)
    if spec is None or spec.loader is None:
        raise ConfigError(f"{file}: cannot be loaded as a Python module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # dataclasses and pickling look the module up by name
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        del sys.modules[module_name]
        raise ConfigError(f"{file}: failed on import ({type(error).__name__}: {error})") from error
    build = getattr(module, BUILD, None)
    if not callable(build):
        raise ConfigError(f"{file}: must define a function {BUILD}(context) that returns a StudyPlugin")
    plugin = build(context)
    if not isinstance(plugin, StudyPlugin):
        raise ConfigError(f"{file}: {BUILD}(context) returned {type(plugin).__name__}, not a StudyPlugin")
    _check(file, context.study, plugin)
    return plugin


def _check(file: Any, study: StudyConfig, plugin: StudyPlugin) -> None:
    """Refuse a plugin that does not cover the study: a scorer twice, a condition without a prompt or tool hash, a
    model whose backend nothing can build."""
    names = [scorer_name(s) for s in plugin.scorers]
    if not names or len(set(names)) != len(names):
        raise ConfigError(f"{file}: the plugin needs at least one scorer, and their names must differ: {names}")
    for label, hashes in (("prompts", plugin.prompts), ("tools", plugin.tools)):
        missing = [c for c in study.schema.conditions if c not in hashes]
        if missing:
            raise ConfigError(f"{file}: the plugin's {label} has no entry for condition {missing[0]!r}")
    if not isinstance(plugin.platform, Mapping):
        kind = type(plugin.platform).__name__
        raise ConfigError(f"{file}: the plugin's platform must be a mapping of plain data, not a {kind}")
    if plugin.backend_factory is None:
        _check_backends(study)


def _check_backends(study: StudyConfig) -> None:
    """Without a factory of the plugin's, every model of the table must name a backend the core builds."""
    for key in study.models:
        spec = study.models.resolve(key)
        if not spec.core_backend:
            raise Section({}, study.models.source, f"[models.{key}]").fail(
                "backend",
                f"names {spec.backend!r}, a backend the core does not build ({', '.join(BACKENDS)}); the plugin "
                "must supply a backend_factory that serves it",
            )
