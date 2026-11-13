# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/analysis/config.py, blob 4fc48e52df947cbc31c674d79a74bece2ca2c9d7
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Loading preregistration.toml: named contrasts, the Holm family, alpha, the bootstrap and the power settings.

The file is validated when it is loaded, against the campaign's factor schema (the conditions and factors its
contrasts name must exist there), with messages that name the file and the key, and the SHA-256 of its bytes
goes into every output manifest (P04). A copy made for a test marks that hash `+override`, so an output computed from
changed settings cannot pass for one computed from the file. The loaded object is deeply read-only: every nested
mapping is a read-only view and every list a tuple, so no edit in place can change a preregistration without the mark.

    alpha = 0.05
    holm_family = ["a-vs-b"]   # the confirmatory contrasts; a preregistration that makes no confirmatory claim (a
                               # pilot) leaves it out and says `exploratory_only = true` instead

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
    target_power = 0.8         # optional, default 0.8: the power a minimum detectable effect must reach

    [estimators.wilcoxon_cluster_bootstrap]   # optional parameters of a registered estimator
    method = "auto"

    [contrasts.a-vs-b]         # named-condition form: two conditions of the factor schema
    conditions = ["cond-a", "cond-b"]
    outcome = "score"
    estimator = "wilcoxon_cluster_bootstrap"
    cluster = "group"          # the task attribute that is the unit of inference
    equivalence_margin = 0.05  # optional: only with a margin can the result be called "equivalent"
    treatment_outcomes = "scored"  # optional: how treatment attempts enter: "scored" or "excluded" (every pair
                                   # with one is left out); a member of the Holm family that loses a pair to a
                                   # treatment attempt scored None is refused without it, and with "scored" too
    max_unscored_treatment = 1 # optional, with "scored" in the Holm family only: the pairs it may still lose to a
                               # treatment attempt that genuinely could not be measured (default 0)

    [contrasts.gate-effect]    # factor form: a declared factor of the schema and two of its levels
    factor = "gate"
    levels = ["open", "shut"]
    outcome = "score"
    estimator = "wilcoxon_cluster_bootstrap"
    cluster = "group"

    [descriptives.waited-in-a] # optional, S14: a one-arm estimate, descriptive, never in the Holm family
    conditions = ["cond-a"]    # one condition or a set of them, pooled
    outcome = "waited"         # a rate or share; or `numerator` and `denominator` for a ratio of totals
    cluster = "group"
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import tomllib
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from expcore.campaign.factors import BUILTIN_FACTORS, FactorSchema
from expcore.config.loading import ConfigError, Section
from expcore.stats import estimators
from expcore.stats.estimators import EstimatorSettings, ParamError, UnknownEstimator
from expcore.stats.onearm import DescriptiveSpec
from expcore.stats.pairing import TREATMENT_OUTCOMES, ContrastSpec
from expcore.stats.power import PowerSettings

OVERRIDE_MARK = "+override"
DEFAULT_CI_LEVEL = 0.95
DEFAULT_TARGET_POWER = 0.8
_TOP_KEYS = (
    "alpha",
    "holm_family",
    "exploratory_only",
    "bootstrap",
    "power",
    "estimators",
    "contrasts",
    "descriptives",
)
_DESCRIPTIVE_KEYS = ("conditions", "cluster", "outcome", "numerator", "denominator")
_CONTRAST_KEYS = (
    "conditions",
    "factor",
    "levels",
    "outcome",
    "estimator",
    "cluster",
    "equivalence_margin",
    "treatment_outcomes",
    "max_unscored_treatment",
)


@dataclasses.dataclass(frozen=True)
class BootstrapSettings:
    """The cluster bootstrap: replicates, the seed all contrast seeds derive from, and the interval level."""

    replicates: int
    seed: int
    ci_level: float = DEFAULT_CI_LEVEL


def freeze(value: Any) -> Any:
    """A deep, read-only copy: each mapping becomes a read-only view of a new dict, each list or tuple a tuple (P04).

    The copy shares no container with its argument, so neither side can change the other afterwards.
    """
    if isinstance(value, Mapping):
        return types.MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze(item) for item in value)
    return value


@dataclasses.dataclass(frozen=True)
class Preregistration:
    """A loaded preregistration. `sha256` is the hash of the file, or of the file plus `+override` for a test copy.

    `exploratory_only` is the preregistration's statement that it makes no confirmatory claim: it has no Holm family,
    and every contrast is exploratory (S12). Exactly one of the two holds: a family, or that statement.

    The mappings and sequences it holds are frozen when it is built, whichever way it is built: assigning into one
    raises TypeError, and a mapping or list handed in is copied, so the caller's own object stays the caller's (P04).
    """

    source: str
    sha256: str
    alpha: float
    bootstrap: BootstrapSettings
    contrasts: Mapping[str, ContrastSpec]
    holm_family: tuple[str, ...]
    power: PowerSettings | None = None
    estimator_params: Mapping[str, Mapping[str, Any]] = dataclasses.field(default_factory=dict)
    schema_digest: str | None = None  # the factor schema the contrasts were validated against
    exploratory_only: bool = False  # no confirmatory claim: no Holm family, every contrast exploratory (S12)
    descriptives: Mapping[str, DescriptiveSpec] = dataclasses.field(default_factory=dict)  # one-arm estimates (S14)

    def __post_init__(self) -> None:
        """Deep-freeze the contrasts, the family, the estimator parameters and the one-arm estimates into fresh
        read-only containers; refuse a family beside `exploratory_only`, and neither of them (S12)."""
        for name in ("contrasts", "holm_family", "estimator_params", "descriptives"):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        if self.exploratory_only and self.holm_family:
            raise ValueError("a preregistration with exploratory_only has no Holm family, and this one names one")
        if not self.exploratory_only and not self.holm_family:
            raise ValueError("a preregistration needs a Holm family, or exploratory_only to make no confirmatory claim")

    def with_override(self, **changes: Any) -> Preregistration:
        """Return a copy with fields replaced, for tests and never for production; its hash ends in `+override` (P04).

        The copy is built anew, so it shares no mapping or list with the original: editing either never reaches the
        other, and the original keeps its hash and its parameters.
        """
        if "sha256" in changes:
            raise TypeError("the hash cannot be overridden; with_override marks it itself")
        marked = self.sha256 if self.sha256.endswith(OVERRIDE_MARK) else self.sha256 + OVERRIDE_MARK
        return dataclasses.replace(self, sha256=marked, **changes)

    def manifest(self) -> dict[str, str]:
        """The entries every output manifest carries: the hash of the preregistration it was computed under (P04).

        When the preregistration was validated against a factor schema, the schema's digest goes with it, so an output
        says which conditions and factors its contrasts were checked against.
        """
        entries = {"preregistration_sha256": self.sha256}
        if self.schema_digest is not None:
            entries["factor_schema_sha256"] = self.schema_digest
        return entries

    def estimator_settings(self, spec: ContrastSpec) -> EstimatorSettings:
        """The settings one contrast's estimator receives.

        The seed is the bootstrap seed and a digest of the contrast's name, so a contrast draws the same resamples
        whichever contrasts run before it, or whether they run at all.
        """
        return EstimatorSettings(
            replicates=self.bootstrap.replicates,
            level=self.bootstrap.ci_level,
            seed=(self.bootstrap.seed, name_digest(spec.name)),
            params=dict(self.estimator_params.get(spec.estimator, {})),
        )

    def descriptive_settings(self, spec: DescriptiveSpec) -> EstimatorSettings:
        """The bootstrap a one-arm estimate draws (S14): the contrasts' replicates and level, its own name's seed."""
        return EstimatorSettings(
            replicates=self.bootstrap.replicates,
            level=self.bootstrap.ci_level,
            seed=(self.bootstrap.seed, name_digest(spec.name)),
        )


def name_digest(name: str) -> int:
    """A number derived from a contrast's name, to seed its draws apart from every other contrast's."""
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big")


def load(path: str | Path, schema: FactorSchema) -> Preregistration:
    """Read a preregistration file and validate it against the factor schema; raise ConfigError naming file and key."""
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
    return parse(raw, str(file), hashlib.sha256(data).hexdigest(), schema)


def parse(raw: Mapping[str, Any], source: str, sha256: str, schema: FactorSchema) -> Preregistration:
    """Validate parsed TOML against the factor schema and build the preregistration; `source` names the file."""
    root = Section(raw, source, "top level")
    root.reject_unknown(_TOP_KEYS)
    alpha = _fraction(root, "alpha")
    bootstrap = _bootstrap(_table_section(root, "bootstrap"))
    contrasts = {
        name: _contrast(source, name, table, schema, alpha) for name, table in _tables(root, "contrasts").items()
    }
    if not contrasts:
        raise root.fail("contrasts", "must declare at least one contrast")
    exploratory_only = _flag(root, "exploratory_only")
    family = () if exploratory_only else _family(root, contrasts)
    _check_family_claims(root, contrasts, family, exploratory_only)
    power = _power(_table_section(root, "power")) if "power" in raw else None
    params = {name: _estimator_params(source, name, table) for name, table in _tables(root, "estimators").items()}
    descriptives = {
        name: _descriptive(source, name, table, schema, contrasts)
        for name, table in _tables(root, "descriptives").items()
    }
    return Preregistration(
        source,
        sha256,
        alpha,
        bootstrap,
        contrasts,
        family,
        power,
        params,
        schema.digest,
        exploratory_only,
        descriptives,
    )


def _descriptive(
    source: str, name: str, table: Mapping[str, Any], schema: FactorSchema, contrasts: Mapping[str, ContrastSpec]
) -> DescriptiveSpec:
    """Read one [descriptives.<name>] table (S14): its conditions, cluster, and an outcome or a numerator and a
    denominator. Its name may not be a contrast's, which would share its seed and its place in the tables."""
    section = Section(table, source, f"[descriptives.{name}]")
    section.reject_unknown(_DESCRIPTIVE_KEYS)
    if name in contrasts:
        raise section.fail(None, f"{name!r} is also a contrast's name; a one-arm estimate needs a name of its own")
    conditions = section.strings("conditions")
    if not conditions:
        raise section.fail("conditions", "must name one condition or more")
    if len(set(conditions)) != len(conditions):
        raise section.fail("conditions", "must name each condition once")
    for condition in conditions:
        if condition not in schema.conditions:
            raise section.fail(
                "conditions",
                f"names unknown condition {condition!r}; the conditions of {schema.file} are: "
                f"{', '.join(schema.conditions)}",
            )
    cluster = section.string("cluster")
    ratio = [key for key in ("numerator", "denominator") if key in table]
    if "outcome" in table:
        if ratio:
            raise section.fail(
                ratio[0], "is given with an outcome: give an outcome, or a numerator and a denominator, not both"
            )
        return DescriptiveSpec(name, conditions, cluster, outcome=section.string("outcome"))
    if not ratio:
        raise section.fail("outcome", "is required: give an outcome, or a numerator and a denominator")
    numerator, denominator = section.string("numerator"), section.string("denominator")
    if numerator == denominator:
        raise section.fail("denominator", "must differ from the numerator: a ratio needs two different outcomes")
    return DescriptiveSpec(name, conditions, cluster, numerator=numerator, denominator=denominator)


def _flag(section: Section, key: str) -> bool:
    """Return an optional boolean; absent is False."""
    value = section.data.get(key, False)
    if not isinstance(value, bool):
        raise section.fail(key, f"must be true or false, got {type(value).__name__}")
    return value


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
    """Read [power]: the seed, the simulation count, the target clusters and difference, the SD grid, the target power."""
    section.reject_unknown(("seed", "n_simulations", "target_clusters", "target_difference", "sd_grid", "target_power"))
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
        target_power=_fraction(section, "target_power", DEFAULT_TARGET_POWER),
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


def _margin(section: Section, alpha: float) -> float | None:
    """Read the optional equivalence margin: positive, and only where 1 - 2 alpha is a level."""
    if "equivalence_margin" not in section.data:
        return None
    margin = _number(section, "equivalence_margin")
    if margin <= 0:
        raise section.fail("equivalence_margin", f"must be positive, got {margin}")
    if alpha >= 0.5:
        raise section.fail(
            "equivalence_margin", f"needs alpha below 0.5, since its interval is at 1 - 2 alpha; alpha is {alpha}"
        )
    return margin


def _treatment_outcomes(section: Section) -> str | None:
    """Read the optional declaration of how treatment attempts enter the contrast: "scored" or "excluded"."""
    if "treatment_outcomes" not in section.data:
        return None
    value = section.data["treatment_outcomes"]
    if not isinstance(value, str) or value not in TREATMENT_OUTCOMES:
        raise section.fail("treatment_outcomes", f"must be 'scored' or 'excluded', got {value!r}")
    return value


def _max_unscored(section: Section, treatment: str | None) -> int:
    """Read the optional tolerance of a "scored" contrast for treatment attempts that could not be measured."""
    if "max_unscored_treatment" not in section.data:
        return 0
    value = section.data["max_unscored_treatment"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise section.fail("max_unscored_treatment", f"must be a non-negative integer, got {value!r}")
    if value and treatment != "scored":
        raise section.fail("max_unscored_treatment", 'is only for a contrast with treatment_outcomes = "scored"')
    return value


def _contrast(source: str, name: str, table: Mapping[str, Any], schema: FactorSchema, alpha: float) -> ContrastSpec:
    """Read one [contrasts.<name>] table in either form and check its arms against the factor schema."""
    section = Section(table, source, f"[contrasts.{name}]")
    section.reject_unknown(_CONTRAST_KEYS)
    outcome, estimator, cluster = section.string("outcome"), section.string("estimator"), section.string("cluster")
    try:
        estimators.get(estimator)
    except UnknownEstimator as error:
        raise section.fail("estimator", str(error)) from None
    treatment = _treatment_outcomes(section)
    declared = {
        "equivalence_margin": _margin(section, alpha),
        "treatment_outcomes": treatment,
        "max_unscored_treatment": _max_unscored(section, treatment),
    }
    if "conditions" in table:
        for extra in ("factor", "levels"):
            if extra in table:
                raise section.fail(extra, "belongs to the factor form; this contrast names conditions")
        conditions = _arms(section, "conditions")
        _check_conditions(section, conditions, schema)
        return ContrastSpec(name, outcome, estimator, cluster, conditions=conditions, **declared)
    if "factor" not in table:
        raise section.fail("conditions", "is required unless the contrast gives a factor with its levels")
    if "levels" not in table:
        raise section.fail("levels", "is required with a factor")
    factor, levels = section.string("factor"), _arms(section, "levels")
    _check_factor(section, factor, levels, schema)
    return ContrastSpec(name, outcome, estimator, cluster, factor=factor, levels=levels, **declared)


def _check_conditions(section: Section, conditions: tuple[str, str], schema: FactorSchema) -> None:
    """Require both conditions to be conditions of the schema, and to share at least one task."""
    for condition in conditions:
        if condition not in schema.conditions:
            raise section.fail(
                "conditions",
                f"names unknown condition {condition!r}; the conditions of {schema.file} are: "
                f"{', '.join(schema.conditions)}",
            )
    first, second = (schema.condition_tasks[c] for c in conditions)
    if first is not None and second is not None and not set(first) & set(second):
        raise section.fail("conditions", f"{conditions[0]!r} and {conditions[1]!r} run on no task in common")


def _check_factor(section: Section, factor: str, levels: tuple[str, str], schema: FactorSchema) -> None:
    """Require a declared factor of the schema and two of its levels."""
    if factor in BUILTIN_FACTORS:
        raise section.fail("factor", f"{factor!r} is built in; a factor contrast varies a declared factor")
    if factor not in schema.declared:
        raise section.fail(
            "factor", f"names unknown factor {factor!r}; the factors of {schema.file} are: {', '.join(schema.declared)}"
        )
    for level in levels:
        if level not in schema.declared[factor]:
            raise section.fail(
                "levels",
                f"names unknown level {level!r} of {factor!r}; its levels are: {', '.join(schema.declared[factor])}",
            )


def _family(root: Section, contrasts: Mapping[str, ContrastSpec]) -> tuple[str, ...]:
    """Read holm_family: a non-empty list of declared contrast names, each once.

    An empty list is refused rather than read as "no confirmatory claim": it is more likely a family that was never
    filled in than a choice, so that choice is made explicitly, with `exploratory_only = true` (S12).
    """
    if "holm_family" not in root.data:
        raise root.fail(
            "holm_family",
            "is required: name the confirmatory contrasts or, if the preregistration makes no confirmatory claim "
            "(a pilot), say so with exploratory_only = true",
        )
    family = root.strings("holm_family")
    if not family:
        raise root.fail(
            "holm_family",
            "is empty, which is more likely a mistake than a choice: name at least one contrast or, if the "
            "preregistration makes no confirmatory claim (a pilot), leave holm_family out and say so with "
            "exploratory_only = true",
        )
    for name in family:
        if name not in contrasts:
            raise root.fail(
                "holm_family", f"names {name!r}, which is not a declared contrast; declared: {', '.join(contrasts)}"
            )
    if len(set(family)) != len(family):
        raise root.fail("holm_family", "names a contrast twice")
    return family


def _check_family_claims(
    root: Section, contrasts: Mapping[str, ContrastSpec], family: tuple[str, ...], exploratory_only: bool
) -> None:
    """Refuse a family beside `exploratory_only`, and a setting only a member of the family uses on a contrast that is
    not one (S10, S12)."""
    if exploratory_only and "holm_family" in root.data:
        raise root.fail(
            "holm_family",
            "is given with exploratory_only = true, which says no contrast is confirmatory: leave holm_family out, "
            "or set exploratory_only = false",
        )
    for name, spec in contrasts.items():
        if spec.max_unscored_treatment and name not in family:
            if exploratory_only:
                key, why = (
                    "exploratory_only",
                    f"is true, so there is no family and the max_unscored_treatment of {name!r}",
                )
            else:
                key, why = "holm_family", f"does not name {name!r}, whose max_unscored_treatment"
            raise root.fail(
                key, f"{why} would do nothing: only a member of the family is refused for a treatment attempt left None"
            )
