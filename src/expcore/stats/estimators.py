# Provenance: new
"""Estimators: named functions from paired cluster differences to an estimate, a p-value and an interval.

A preregistration names the estimator of each contrast. The contrast interface, the pairing and the results carry no
assumption about which estimator that is; `wilcoxon_cluster_bootstrap` is the first one registered, and another (a
permutation test, a mixed model for binary outcomes) is added with `register` without touching either.

An estimator is a function `(PairedDifferences, EstimatorSettings) -> EstimatorResult`. It must be a pure function
of its arguments: the settings carry the bootstrap seed as entropy, so a call twice with equal settings returns equal
results whatever ran in between.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from expcore.stats import contrasts as kernel

_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True)
class PairedDifferences:
    """Paired differences A - B per cluster and model: rows are clusters, columns models, NaN where a cell has no pair.

    A cluster or a model without any paired cell is not part of the input: pairing drops it and counts it.
    """

    clusters: tuple[str, ...]
    models: tuple[str, ...]
    matrix: np.ndarray

    def __post_init__(self) -> None:
        """Check the shape, drop writability, and refuse an all-NaN row or column."""
        matrix = np.array(self.matrix, dtype=float)
        if matrix.shape != (len(self.clusters), len(self.models)):
            raise ValueError(
                f"matrix shape {matrix.shape} does not match {len(self.clusters)} clusters by {len(self.models)} models"
            )
        if matrix.shape[0] and (np.isnan(matrix).all(axis=1).any() or np.isnan(matrix).all(axis=0).any()):
            raise ValueError("every cluster and every model needs at least one paired cell; drop the others first")
        matrix.flags.writeable = False
        object.__setattr__(self, "matrix", matrix)

    @property
    def cluster_differences(self) -> np.ndarray:
        """The cluster-level difference: the equal-weight mean over the models with a paired cell."""
        return kernel.cluster_means(self.matrix)


@dataclass(frozen=True)
class EstimatorSettings:
    """What every estimator receives besides the data: the bootstrap and the estimator's own parameters."""

    replicates: int
    level: float
    seed: tuple[int, ...]
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelEstimate:
    """One model's effect with its interval, from the same cluster resamples as the overall one."""

    model: str
    estimate: float
    ci_low: float
    ci_high: float
    n_undefined_replicates: int


@dataclass(frozen=True)
class EstimatorResult:
    """An estimate with its p-value and interval; NaN where the estimator could not compute one."""

    estimate: float
    p_value: float
    ci_low: float
    ci_high: float
    n_clusters: int
    per_model: tuple[ModelEstimate, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)  # the estimator's own counts, such as dropped zeros


class Estimator(Protocol):
    """The call every registered estimator answers."""

    def __call__(self, paired: PairedDifferences, settings: EstimatorSettings) -> EstimatorResult:
        """Estimate the effect of the paired differences."""
        ...


class UnknownEstimator(ValueError):
    """A name that no estimator is registered under."""


class ParamError(ValueError):
    """An estimator parameter that is unknown or unusable; `key` names it."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(f"{key}: {message}")
        self.key = key
        self.message = message


@dataclass(frozen=True)
class _Entry:
    function: Estimator
    check_params: Callable[[Mapping[str, Any]], None]


_REGISTRY: dict[str, _Entry] = {}


def _no_params(params: Mapping[str, Any]) -> None:
    """Accept no parameter at all."""
    for key in params:
        raise ParamError(key, "this estimator takes no parameters")


def register(name: str, *, check_params: Callable[[Mapping[str, Any]], None] | None = None):
    """Register the decorated function under `name`; `check_params` raises ParamError for a bad parameter table."""
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"estimator name {name!r} must be lower-case letters, digits and underscores")

    def decorate(function: Estimator) -> Estimator:
        if name in _REGISTRY:
            raise ValueError(f"estimator {name!r} is already registered")
        _REGISTRY[name] = _Entry(function, check_params or _no_params)
        return function

    return decorate


def names() -> tuple[str, ...]:
    """Return the registered estimator names, in registration order."""
    return tuple(_REGISTRY)


def get(name: str) -> Estimator:
    """Return the estimator registered under `name`, or raise UnknownEstimator listing the registered names."""
    if name not in _REGISTRY:
        raise UnknownEstimator(f"unknown estimator {name!r}; registered: {', '.join(_REGISTRY) or 'none'}")
    return _REGISTRY[name].function


def check_params(name: str, params: Mapping[str, Any]) -> None:
    """Validate an estimator's parameter table; raise UnknownEstimator or ParamError."""
    get(name)
    _REGISTRY[name].check_params(params)


WILCOXON = "wilcoxon_cluster_bootstrap"
_WILCOXON_DEFAULTS: dict[str, Any] = {"snap_decimals": kernel.DEFAULT_SNAP_DECIMALS, "method": "auto"}


def _check_wilcoxon_params(params: Mapping[str, Any]) -> None:
    """Accept `snap_decimals` (an integer from 1 to 15) and `method` (auto, exact or approx)."""
    for key, value in params.items():
        if key not in _WILCOXON_DEFAULTS:
            raise ParamError(key, f"is not a parameter of {WILCOXON}; known: {', '.join(_WILCOXON_DEFAULTS)}")
        if key == "snap_decimals" and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 15):
            raise ParamError(key, "must be an integer from 1 to 15")
        if key == "method" and value not in kernel.WILCOXON_METHODS:
            raise ParamError(key, f"must be one of {', '.join(kernel.WILCOXON_METHODS)}")


def _undefined(n_clusters: int = 0) -> EstimatorResult:
    """The result when no cluster has a paired cell."""
    nan = math.nan
    return EstimatorResult(nan, nan, nan, nan, n_clusters, (), {"zero_dropped": 0})


@register(WILCOXON, check_params=_check_wilcoxon_params)
def wilcoxon_cluster_bootstrap(paired: PairedDifferences, settings: EstimatorSettings) -> EstimatorResult:
    """Wilcoxon signed-rank test on the cluster differences, with a cluster-bootstrap percentile interval.

    The estimate is the mean cluster difference. The p-value is the two-sided signed-rank test on the snapped
    differences, with zero differences dropped and counted (`zero_dropped`) and all-zero input giving p = 1 (S01,
    S03). The interval resamples the clusters once for all models, and each model's interval comes from the same
    resamples (S04); a model whose interval is NaN had fewer than half its replicates defined (S05).
    """
    params = {**_WILCOXON_DEFAULTS, **settings.params}
    _check_wilcoxon_params(params)
    if not paired.clusters:
        return _undefined()
    boot = kernel.cluster_bootstrap(paired.matrix, settings.replicates, np.random.default_rng(list(settings.seed)))
    low, high, _ = kernel.percentile_interval(boot.overall, settings.level)
    per_model = []
    for column, model in enumerate(paired.models):
        model_low, model_high, defined = kernel.percentile_interval(boot.per_model[:, column], settings.level)
        per_model.append(
            ModelEstimate(
                model, float(boot.point_per_model[column]), model_low, model_high, settings.replicates - defined
            )
        )
    differences = paired.cluster_differences
    decimals = params["snap_decimals"]
    return EstimatorResult(
        estimate=boot.point,
        p_value=kernel.wilcoxon_p(differences, decimals, params["method"]),
        ci_low=low,
        ci_high=high,
        n_clusters=len(paired.clusters),
        per_model=tuple(per_model),
        counts={"zero_dropped": kernel.zeros_dropped(differences, decimals)},
    )
