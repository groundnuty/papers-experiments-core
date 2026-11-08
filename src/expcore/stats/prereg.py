# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/analysis/config.py, blob 4fc48e52df947cbc31c674d79a74bece2ca2c9d7
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Loading preregistration.toml: named contrasts, the Holm family, alpha, the bootstrap and the power settings.

The file is validated when it is loaded, with messages that name the file and the key, and the SHA-256 of its bytes
goes into every output manifest (P04). A copy made for a test marks that hash `+override`, so an output computed from
changed settings cannot pass for one computed from the file.

    alpha = 0.05
    holm_family = ["a-vs-b"]

    [bootstrap]
    replicates = 2000
    seed = 20260101
    ci_level = 0.95            # optional, default 0.95

    [power]                    # optional
    seed = 1
    n_simulations = 2000
    target_clusters = 25
    target_difference = 0.1
    sd_grid = [0.1, 0.2]

    [estimators.wilcoxon_cluster_bootstrap]   # optional parameters of a registered estimator
    method = "auto"

    [contrasts.a-vs-b]         # named-condition form
    conditions = ["cond-a", "cond-b"]
    outcome = "score"
    estimator = "wilcoxon_cluster_bootstrap"
    cluster = "group"          # the task attribute that is the unit of inference

    [contrasts.gate-effect]    # factor form: pairs on every other factor
    factor = "gate"
    levels = ["open", "shut"]
    outcome = "score"
    estimator = "wilcoxon_cluster_bootstrap"
    cluster = "group"
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from expcore.config.loading import ConfigError, Section
from expcore.stats import estimators
from expcore.stats.estimators import EstimatorSettings, ParamError, UnknownEstimator
from expcore.stats.pairing import ContrastSpec
from expcore.stats.power import PowerSettings

OVERRIDE_MARK = "+override"
DEFAULT_CI_LEVEL = 0.95
_TOP_KEYS = ("alpha", "holm_family", "bootstrap", "power", "estimators", "contrasts")
_CONTRAST_KEYS = ("conditions", "factor", "levels", "outcome", "estimator", "cluster")


@dataclasses.dataclass(frozen=True)
class BootstrapSettings:
    """The cluster bootstrap: replicates, the seed all contrast seeds derive from, and the interval level."""

    replicates: int
    seed: int
    ci_level: float = DEFAULT_CI_LEVEL


@dataclasses.dataclass(frozen=True)
class Preregistration:
    """A loaded preregistration. `sha256` is the hash of the file, or of the file plus `+override` for a test copy."""

    source: str
    sha256: str
    alpha: float
    bootstrap: BootstrapSettings
    contrasts: Mapping[str, ContrastSpec]
    holm_family: tuple[str, ...]
    power: PowerSettings | None = None
    estimator_params: Mapping[str, Mapping[str, Any]] = dataclasses.field(default_factory=dict)

    def with_override(self, **changes: Any) -> Preregistration:
        """Return a copy with fields replaced, for tests and never for production; its hash ends in `+override` (P04)."""
        if "sha256" in changes:
            raise TypeError("the hash cannot be overridden; with_override marks it itself")
        marked = self.sha256 if self.sha256.endswith(OVERRIDE_MARK) else self.sha256 + OVERRIDE_MARK
        return dataclasses.replace(self, sha256=marked, **changes)

    def manifest(self) -> dict[str, str]:
        """The entries every output manifest carries: the hash of the preregistration it was computed under (P04)."""
        return {"preregistration_sha256": self.sha256}

    def estimator_settings(self, spec: ContrastSpec) -> EstimatorSettings:
        """The settings one contrast's estimator receives.

        The seed is the bootstrap seed and a digest of the contrast's name, so a contrast draws the same resamples
        whichever contrasts run before it, or whether they run at all.
        """
        digest = int.from_bytes(hashlib.sha256(spec.name.encode("utf-8")).digest()[:8], "big")
        return EstimatorSettings(
            replicates=self.bootstrap.replicates,
            level=self.bootstrap.ci_level,
            seed=(self.bootstrap.seed, digest),
            params=dict(self.estimator_params.get(spec.estimator, {})),
        )


def load(path: str | Path) -> Preregistration:
    """Read and validate a preregistration file; raise ConfigError naming the file and the key."""
    file = Path(path)
    try:
        data = file.read_bytes()
    except FileNotFoundError:
        raise ConfigError(f"{file}: file not found") from None
    except OSError as error:
        raise ConfigError(f"{file}: cannot be read ({type(error).__name__})") from None
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise ConfigError(f"{file}: not valid UTF-8") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{file}: not valid TOML: {error}") from None
    return parse(raw, str(file), hashlib.sha256(data).hexdigest())


def parse(raw: Mapping[str, Any], source: str, sha256: str) -> Preregistration:
    """Validate parsed TOML and build the preregistration; `source` names the file in messages."""
    root = Section(raw, source, "top level")
    root.reject_unknown(_TOP_KEYS)
    alpha = _fraction(root, "alpha")
    bootstrap = _bootstrap(_table_section(root, "bootstrap"))
    contrasts = {name: _contrast(source, name, table) for name, table in _tables(root, "contrasts").items()}
    if not contrasts:
        raise root.fail("contrasts", "must declare at least one contrast")
    family = _family(root, contrasts)
    power = _power(_table_section(root, "power")) if "power" in raw else None
    params = {name: _estimator_params(source, name, table) for name, table in _tables(root, "estimators").items()}
    return Preregistration(source, sha256, alpha, bootstrap, contrasts, family, power, params)


def _table_section(root: Section, key: str) -> Section:
    """Return a required top-level table as a Section that messages name `[key]`."""
    return Section(root.section(key).data, root.file, f"[{key}]")


def _tables(section: Section, key: str) -> dict[str, dict[str, Any]]:
    """Return a table of tables, each entry checked to be a table; an absent key is empty."""
    tables = section.table(key)
    for name, value in tables.items():
        if not isinstance(value, Mapping):
            raise section.fail(key, f"entry {name!r} must be a table, got {type(value).__name__}")
        if not name.strip():
            raise section.fail(key, "has an entry with a blank name")
    return tables


def _number(section: Section, key: str) -> float:
    """Return a required finite number (not a boolean)."""
    if key not in section.data:
        raise section.fail(key, "is required")
    return _as_number(section, key, section.data[key])


def _as_number(section: Section, key: str, value: object) -> float:
    """Check one value is a finite number and not a boolean."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise section.fail(key, f"must be a finite number, got {type(value).__name__}")
    return float(value)


def _fraction(section: Section, key: str, default: float | None = None) -> float:
    """Return a number strictly between 0 and 1; `default` stands in for an absent key when given."""
    if key not in section.data and default is not None:
        return default
    value = _number(section, key)
    if not 0.0 < value < 1.0:
        raise section.fail(key, f"must be strictly between 0 and 1, got {value}")
    return value


def _integer(section: Section, key: str, minimum: int) -> int:
    """Return a required integer of at least `minimum`."""
    value = section.optional_integer(key)
    if value is None:
        raise section.fail(key, "is required")
    if value < minimum:
        raise section.fail(key, f"must be at least {minimum}, got {value}")
    return value


def _bootstrap(section: Section) -> BootstrapSettings:
    """Read [bootstrap]: replicates, seed and the optional interval level."""
    section.reject_unknown(("replicates", "seed", "ci_level"))
    return BootstrapSettings(
        replicates=_integer(section, "replicates", 1),
        seed=_integer(section, "seed", 0),
        ci_level=_fraction(section, "ci_level", DEFAULT_CI_LEVEL),
    )


def _power(section: Section) -> PowerSettings:
    """Read [power]: the seed, the simulation count, the target number of clusters and difference, the SD grid."""
    section.reject_unknown(("seed", "n_simulations", "target_clusters", "target_difference", "sd_grid"))
    difference = _number(section, "target_difference")
    if difference <= 0:
        raise section.fail("target_difference", f"must be positive, got {difference}")
    grid = section.data.get("sd_grid")
    if not isinstance(grid, list) or not grid:
        raise section.fail("sd_grid", "must be a non-empty array of positive numbers")
    sds = tuple(_as_number(section, "sd_grid", value) for value in grid)
    if any(sd <= 0 for sd in sds):
        raise section.fail("sd_grid", "must hold positive numbers only")
    return PowerSettings(
        seed=_integer(section, "seed", 0),
        n_simulations=_integer(section, "n_simulations", 1),
        target_clusters=_integer(section, "target_clusters", 2),
        target_difference=difference,
        sd_grid=sds,
    )


def _estimator_params(source: str, name: str, table: Mapping[str, Any]) -> dict[str, Any]:
    """Check one [estimators.<name>] table against the registered estimator of that name."""
    section = Section(table, source, f"[estimators.{name}]")
    try:
        estimators.check_params(name, table)
    except UnknownEstimator as error:
        raise ConfigError(f"{source}: [estimators.{name}]: {error}") from None
    except ParamError as error:
        raise section.fail(error.key, error.message) from None
    return dict(table)


def _arms(section: Section, key: str) -> tuple[str, str]:
    """Return two different names from an array of two strings."""
    arms = section.strings(key)
    if len(arms) != 2 or arms[0] == arms[1]:
        raise section.fail(key, "must name two different strings")
    return arms[0], arms[1]


def _contrast(source: str, name: str, table: Mapping[str, Any]) -> ContrastSpec:
    """Read one [contrasts.<name>] table in either form."""
    section = Section(table, source, f"[contrasts.{name}]")
    section.reject_unknown(_CONTRAST_KEYS)
    outcome, estimator, cluster = section.string("outcome"), section.string("estimator"), section.string("cluster")
    try:
        estimators.get(estimator)
    except UnknownEstimator as error:
        raise section.fail("estimator", str(error)) from None
    if "conditions" in table:
        for extra in ("factor", "levels"):
            if extra in table:
                raise section.fail(extra, "belongs to the factor form; this contrast names conditions")
        return ContrastSpec(name, outcome, estimator, cluster, conditions=_arms(section, "conditions"))
    if "factor" not in table:
        raise section.fail("conditions", "is required unless the contrast gives a factor with its levels")
    if "levels" not in table:
        raise section.fail("levels", "is required with a factor")
    return ContrastSpec(
        name, outcome, estimator, cluster, factor=section.string("factor"), levels=_arms(section, "levels")
    )


def _family(root: Section, contrasts: Mapping[str, ContrastSpec]) -> tuple[str, ...]:
    """Read holm_family: a non-empty list of declared contrast names, each once."""
    family = root.strings("holm_family")
    if not family:
        raise root.fail("holm_family", "is required and must name at least one contrast")
    for name in family:
        if name not in contrasts:
            raise root.fail(
                "holm_family", f"names {name!r}, which is not a declared contrast; declared: {', '.join(contrasts)}"
            )
    if len(set(family)) != len(family):
        raise root.fail("holm_family", "names a contrast twice")
    return family
