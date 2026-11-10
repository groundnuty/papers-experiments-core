# Provenance: new
"""The study's plugin module: how the core finds the executor, the scorers and the platform's probes.

study.toml names one Python file (`[study] plugin`). The core loads it and calls its `build` function with a
`StudyContext`; `build` returns a `StudyPlugin`:

    def build(context: StudyContext) -> StudyPlugin: ...

The module is the study's own code and lives in the study directory, so the code identity covers it. It may import
modules beside it relatively (`from .scoring import Scorer`), each a file `[study] code` lists. Nothing in the core
imports a study by name.
"""

import datetime as dt
import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any

from expcore.campaign.defect_log import write_failure
from expcore.campaign.executor import BackendFactory, RunExecutor
from expcore.campaign.healthgate import HealthCheck
from expcore.campaign.lease import ResourceProbe
from expcore.config.loading import ConfigError, Section
from expcore.config.models import ModelTable, not_built_by_core
from expcore.plugin import ToolSpec
from expcore.provenance.codestamp import covered_files
from expcore.records.build import redactor_for
from expcore.records.rescore import Scorer, scorer_name, scorer_version
from expcore.study.config import OUT_DIR, STUDY_FILE, StudyConfig, StudyIdentity

BUILD = "build"
PLUGIN_MODULE_PREFIX = "expcore_study_plugin_"  # the rest of a plugin's module name is a hash of its path


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
      `default_backend_factory`. It must have a `version` attribute, a non-empty string: the factory's name and version
      go into the campaign fingerprint, so a resume under another factory or version is refused (C06). Code in the
      study directory is covered by the code identity as well; the version speaks for a factory in an installed package,
      and must then be that package's installed version (`factory_version.check_factory_version`).
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

    def backend_factory_identity(self) -> dict[str, str] | None:
        """The factory's name and version for the campaign fingerprint; None when the plugin supplies no factory.

        The name is qualified by its module, except that a factory in the plugin file goes by its name alone, and one in
        a module beside the plugin by that module's name below the plugin's: a plugin's module name holds the path of
        the study directory, and moving a study is not another experiment.
        """
        factory = self.backend_factory
        if factory is None:
            return None
        module, qualname = factory_name(factory)
        head, _, below = module.partition(".")
        if head.startswith(PLUGIN_MODULE_PREFIX):
            name = f"{below}.{qualname}" if below else qualname
        else:
            name = f"{module}.{qualname}"
        return {"name": name, "version": factory.version}


def factory_name(factory: Any) -> tuple[str, str]:
    """The module and qualified name of a factory, or of its class when it is a callable instance."""
    named = factory if hasattr(factory, "__qualname__") else type(factory)
    return str(named.__module__), str(named.__qualname__)


def tools_sha256(specs: Sequence[ToolSpec]) -> str:
    """The SHA-256 of a tool set: each tool's name, description, schema and terminal flag, in name order."""
    entries = sorted(
        [{"name": s.name, "description": s.description, "input_schema": s.input_schema, "terminal": s.terminal}
         for s in specs],
        key=lambda entry: entry["name"],
    )  # fmt: skip
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def import_plugin_module(
    study: StudyConfig | StudyIdentity,
    *,
    error_text: bool = True,
    models: ModelTable | None = None,
    log_root: Path | None = None,
) -> ModuleType:
    """Import the study's plugin module from its file, running its top level, and return it; ConfigError if it cannot.

    The plugin is imported as a package whose path is its own directory, under a name made from a hash of its path, so
    it imports a module beside it relatively (`from .scoring import Scorer`), and two studies' modules of one name never
    meet. Every import reads the files anew: the modules an earlier import of the same plugin left are dropped first.
    Each module it imports from its directory must be a file `[study] code` lists (`check_plugin_modules`).

    `build` is not called, so no model or key is read. What the module registers when imported, the native parser of a
    backend of the study's own, is then there for a step that needs nothing else of the plugin: the replay check. That
    step passes `error_text=False`, so a refusal names the file and the exception's type and never its text, which can
    hold a credential. Otherwise the text is shown redacted as a record is, with the secrets of the environment, the
    registered ones and the keys of `models` (the value of each `key_env`, the contents of each `key_file`); a value
    too short to redact everywhere is redacted where it stands as a token of its own, so it cannot wipe the rest of
    the diagnostic; and with `log_root` (a study's `out/`) the redacted
    traceback goes to the harness-error log there, which the message names.
    """
    file = study.plugin_file
    module_name = f"{PLUGIN_MODULE_PREFIX}{hashlib.sha256(str(file).encode()).hexdigest()[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, file, submodule_search_locations=[str(file.parent)])
    if spec is None or spec.loader is None:
        raise ConfigError(f"{file}: cannot be loaded as a Python module")
    _forget(module_name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # dataclasses and pickling look the module up by name
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        _forget(module_name)
        hint = _beside_hint(file, error)
        if not error_text:
            raise ConfigError(f"{file}: failed on import ({type(error).__name__}){hint}") from None
        redactor = partial(redactor_for, None, models=models, short_tokens=True)
        text = redactor().with_lines().text(str(error))
        message = f"{file}: failed on import ({type(error).__name__}: {text}){hint}"
        if log_root is not None:
            stamp = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            title = f"importing {file.name}: {type(error).__name__}"
            logged = write_failure(log_root, error, title=title, stamp=stamp, redactor=redactor)
            message += f"; its traceback, redacted, is in {logged}"
        raise ConfigError(message) from error
    check_plugin_modules(study, module)
    return module


def _forget(module_name: str) -> None:
    """Drop a plugin module and every module imported beneath it, so the next import reads their files anew."""
    for name in [n for n in sys.modules if n == module_name or n.startswith(f"{module_name}.")]:
        del sys.modules[name]


def _beside_hint(file: Path, error: Exception) -> str:
    """The fix for an absolute import of a module that lies beside the plugin; empty for any other error.

    It names a file of the study, never the exception's text, so the replay check may show it too.
    """
    name = getattr(error, "name", None) if isinstance(error, ModuleNotFoundError) else None
    if not isinstance(name, str) or "." in name:
        return ""
    if not ((file.parent / f"{name}.py").is_file() or (file.parent / name).is_dir()):
        return ""
    return (
        f"; {name!r} is a module beside the plugin: import it relatively (from .{name} import ...) and list its file "
        "in [study] code"
    )


def check_plugin_modules(study: StudyConfig | StudyIdentity, module: ModuleType) -> None:
    """Refuse a module the plugin imported from its directory whose file the code identity does not cover.

    Such a file would run uncovered: a change to it would leave the identity, and so the replay check's and the
    package's claims about the code, as they were. A namespace directory holds no code and is passed over.
    """
    root = study.root.resolve()
    covered = set(covered_files(root, study.scope))
    prefix = f"{module.__name__}."
    for name in sorted(n for n in list(sys.modules) if n.startswith(prefix)):
        found = getattr(sys.modules.get(name), "__file__", None)
        if not isinstance(found, str):
            continue
        path = Path(found).resolve()
        relative = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
        if relative not in covered:
            raise Section({}, root / STUDY_FILE, "[study]").fail(
                "code",
                f"does not list {relative!r}, which the plugin imports from beside it ({name.removeprefix(prefix)}); "
                "list every file the plugin imports in [study] code, so the code identity covers the code that runs",
            )


def load_plugin(context: StudyContext) -> StudyPlugin:
    """Import the study's plugin module from its file and call its `build`; raise ConfigError when it cannot serve."""
    file = context.study.plugin_file
    module = import_plugin_module(context.study, models=context.study.models, log_root=context.study.root / OUT_DIR)
    build = getattr(module, BUILD, None)
    if not callable(build):
        raise ConfigError(f"{file}: must define a function {BUILD}(context) that returns a StudyPlugin")
    plugin = build(context)
    check_plugin_modules(context.study, module)  # again: `build` may import a module beside it too
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
    else:
        version = getattr(plugin.backend_factory, "version", None)
        if not isinstance(version, str) or not version.strip():
            raise ConfigError(
                f"{file}: the plugin's backend_factory needs a 'version' attribute, a non-empty string, for the "
                "campaign fingerprint; change it whenever the factory's behaviour changes"
            )


def _check_backends(study: StudyConfig) -> None:
    """Without a factory of the plugin's, every model of the table must name a backend the core builds."""
    for key in study.models:
        spec = study.models.resolve(key)
        if not spec.core_backend:
            raise Section({}, study.models.source, f"[models.{key}]").fail("backend", not_built_by_core(spec.backend))
