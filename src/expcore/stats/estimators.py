# Provenance: new
"""Estimators: named functions from paired cluster differences to an estimate, a p-value and an interval.

A preregistration names the estimator of each contrast. The contrast interface, the pairing and the results carry no
assumption about which estimator that is; `wilcoxon_cluster_bootstrap` is the first one registered, and another (a
permutation test, a mixed model for binary outcomes) is added with `register` without touching either.

An estimator is a function `(PairedDifferences, EstimatorSettings) -> EstimatorResult`. It must be a pure function
of its arguments: the settings carry the bootstrap seed as entropy, so a call twice with equal settings returns equal
results whatever ran in between.

An estimator may also register its p-value as a function of many studies' cluster differences at once, `(studies,
params) -> p-values`. The minimum detectable effect is then simulated with that function and the contrast's own
parameters, not with a stand-in test (S06, S09). It must return for each study exactly the p-value the estimator
reports for those differences; an estimator that registers none has no such simulation.

Each estimator reads one form of contrast (`FORMS`), declared when it is registered: "difference" (the default),
whose input holds the mean differences A - B of one outcome per cluster and model, or "ratio" (S13), whose input
holds one log ratio of sums per cluster: in a single column, `POOLED`, when each cluster pools its models, or one
(model, cluster) cell per row in its model's column when the contrast's cluster key names the model (see
`expcore.stats.ratio`). A contrast whose form is not its estimator's is refused, so neither can be handed the other's
input. `ratio` is the second estimator registered.
"""

from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from expcore.stats import contrasts as kernel

_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")
FORMS = ("difference", "ratio")  # the form of contrast an estimator reads
POOLED = "pooled"  # the one column of a ratio contrast's input when a cluster's sums pool its models


@dataclass(frozen=True)
class PairedDifferences:
    """Paired differences A - B per cluster and model: rows are clusters, columns models, NaN where a cell has no pair.

    A cluster or a model without any paired cell is not part of the input: pairing drops it and counts it. `pooled`
    says that each cluster's value pools its models, in the one column `POOLED` (a ratio contrast under its default
    key, S13); it is said by the input, never read from a column's name, which a model may share.
    """

    clusters: tuple[str, ...]
    models: tuple[str, ...]
    matrix: np.ndarray
    pooled: bool = False

    def __post_init__(self) -> None:
        """Check the shape, drop writability, and refuse an all-NaN row or column, and a pooled input that is not the
        one pooled column."""
        if self.pooled and self.models not in ((POOLED,), ()):
            raise ValueError(
                f"a pooled input has the one column {POOLED!r} of its pooled models; got "
                f"{', '.join(map(repr, self.models))}"
            )
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


PValuesFunction = Callable[[np.ndarray, Mapping[str, Any]], np.ndarray]  # (studies x clusters), params -> p per study


@dataclass(frozen=True)
class _Entry:
    function: Estimator
    check_params: Callable[[Mapping[str, Any]], None]
    p_values: PValuesFunction | None = None
    form: str = "difference"


_REGISTRY: dict[str, _Entry] = {}


def _no_params(params: Mapping[str, Any]) -> None:
    """Accept no parameter at all."""
    for key in params:
        raise ParamError(key, "this estimator takes no parameters")


def register(
    name: str,
    *,
    check_params: Callable[[Mapping[str, Any]], None] | None = None,
    p_values: PValuesFunction | None = None,
    form: str = "difference",
):
    """Register the decorated function under `name`.

    `check_params` raises ParamError for a bad parameter table. `p_values`, when given, is the estimator's p-value
    for a whole (studies x clusters) array of cluster differences under a parameter table; row by row it must equal
    the p-value the estimator itself reports for those differences. `form` is the form of contrast it reads, one of
    `FORMS`.
    """
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"estimator name {name!r} must be lower-case letters, digits and underscores")
    if form not in FORMS:
        raise ValueError(f"estimator {name!r}: form must be one of {', '.join(map(repr, FORMS))}, got {form!r}")

    def decorate(function: Estimator) -> Estimator:
        if name in _REGISTRY:
            raise ValueError(f"estimator {name!r} is already registered")
        _REGISTRY[name] = _Entry(function, check_params or _no_params, p_values, form)
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


def form(name: str) -> str:
    """The form of contrast the estimator `name` reads, one of `FORMS`; raise UnknownEstimator for an unknown name."""
    get(name)
    return _REGISTRY[name].form


def check_params(name: str, params: Mapping[str, Any]) -> None:
    """Validate an estimator's parameter table; raise UnknownEstimator or ParamError."""
    get(name)
    _REGISTRY[name].check_params(params)


def p_values_function(name: str, params: Mapping[str, Any]) -> Callable[[np.ndarray], np.ndarray] | None:
    """The p-values the estimator `name` reports under `params`, as a function of many studies' cluster differences.

    None when the estimator registered no such function. Raises UnknownEstimator or ParamError as `check_params`
    does, so a parameter that the analysis would refuse is refused here too.
    """
    check_params(name, params)
    function = _REGISTRY[name].p_values
    if function is None:
        return None
    frozen = dict(params)
    return lambda studies: function(studies, frozen)


WILCOXON = "wilcoxon_cluster_bootstrap"
RATIO = "ratio"
_WILCOXON_DEFAULTS: dict[str, Any] = {"snap_decimals": kernel.DEFAULT_SNAP_DECIMALS, "method": "auto"}


def _check_wilcoxon_params(params: Mapping[str, Any]) -> None:
    """The parameters of the Wilcoxon cluster bootstrap."""
    _kernel_params(WILCOXON, params)


def _check_ratio_params(params: Mapping[str, Any]) -> None:
    """The parameters of the ratio estimator: the kernel's, as the Wilcoxon cluster bootstrap takes them."""
    _kernel_params(RATIO, params)


def _kernel_params(name: str, params: Mapping[str, Any]) -> None:
    """Accept `snap_decimals` (an integer from 1 to 15) and `method` (auto, exact or approx), naming `name`."""
    for key, value in params.items():
        if key not in _WILCOXON_DEFAULTS:
            raise ParamError(key, f"is not a parameter of {name}; known: {', '.join(_WILCOXON_DEFAULTS)}")
        if key == "snap_decimals" and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 15):
            raise ParamError(key, "must be an integer from 1 to 15")
        if key == "method" and value not in kernel.WILCOXON_METHODS:
            raise ParamError(key, f"must be one of {', '.join(kernel.WILCOXON_METHODS)}")


def _undefined(n_clusters: int = 0) -> EstimatorResult:
    """The result when no cluster has a paired cell."""
    nan = math.nan
    return EstimatorResult(nan, nan, nan, nan, n_clusters, (), {"zero_dropped": 0})


def _wilcoxon_p_values(studies: np.ndarray, params: Mapping[str, Any]) -> np.ndarray:
    """The Wilcoxon p-value of each study (row) of cluster differences, under the estimator's parameters."""
    merged = {**_WILCOXON_DEFAULTS, **params}
    _check_wilcoxon_params(merged)
    return kernel.wilcoxon_p_studies(studies, merged["snap_decimals"], merged["method"])


@register(WILCOXON, check_params=_check_wilcoxon_params, p_values=_wilcoxon_p_values)
def wilcoxon_cluster_bootstrap(paired: PairedDifferences, settings: EstimatorSettings) -> EstimatorResult:
    """Wilcoxon signed-rank test on the cluster differences, with a cluster-bootstrap percentile interval.

    The estimate is the mean cluster difference. The p-value is the two-sided signed-rank test on the snapped
    differences, with zero differences dropped and counted (`zero_dropped`) and all-zero input giving p = 1 (S01,
    S03). The interval resamples the clusters once for all models, and each model's interval comes from the same
    resamples (S04); a model whose interval is NaN had fewer than half its replicates defined (S05).
    """
    params = {**_WILCOXON_DEFAULTS, **settings.params}
    _check_wilcoxon_params(params)
    return _signed_rank_cluster_bootstrap(paired, settings, params)


def _signed_rank_cluster_bootstrap(
    paired: PairedDifferences, settings: EstimatorSettings, params: Mapping[str, Any]
) -> EstimatorResult:
    """The kernel's test and interval of the cluster differences, each model's interval from the same draws."""
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


@register(RATIO, check_params=_check_ratio_params, p_values=_wilcoxon_p_values, form="ratio")
def ratio(paired: PairedDifferences, settings: EstimatorSettings) -> EstimatorResult:
    """A ratio contrast's estimate, test and interval, on the log scale (S13; the estimand is in `expcore.stats.ratio`).

    The input is one log ratio of ratios of sums per cluster, ln((N_A / D_A) / (N_B / D_B)): in the one column
    `POOLED` when each cluster pools its models (`PairedDifferences.pooled`), or, when the cluster key names the model,
    one (model, cluster) cell per row in its model's column and NaN in the others. The zero rule has already excluded
    or corrected every cluster with a zero sum, so each value is finite. The estimate is their mean, theta, a
    natural-log ratio: exp(theta) is the geometric mean over clusters of arm A's ratio over arm B's. The p-value is
    the two-sided Wilcoxon signed-rank test of the cluster log ratios after float noise is snapped, with zero log
    ratios (clusters whose arms have one ratio) dropped and counted (`zero_dropped`) and all-zero input giving p = 1
    (S01, S03); it assumes independent clusters whose log ratios are symmetric about
    their centre, and tests that centre. The interval is the cluster bootstrap's percentile interval: clusters drawn
    with replacement, the mean log ratio of each draw (S04, S05). One cluster gives an estimate and a point interval;
    its p-value is the test's for one difference (1 by the exact method, 0.3173 by the normal approximation, which no
    level a study uses rejects). Its parameters are the Wilcoxon cluster bootstrap's. Pooled, there is no estimate by
    model. By cell, each model's estimate is the mean of its own cells' log ratios, with its interval from the same
    cluster draws (S04): a breakdown with no test of its own.
    """
    params = {**_WILCOXON_DEFAULTS, **settings.params}
    _check_ratio_params(params)
    pooled = paired.pooled  # said by the input: a model may be called POOLED too
    if paired.clusters and not pooled and not (np.sum(~np.isnan(paired.matrix), axis=1) == 1).all():
        raise ValueError(
            f"the ratio estimator reads one log ratio per cluster, in the one column of the pooled models "
            f"({POOLED!r}) or in its model's column of a (model, cluster) cell; got rows holding more than one of the "
            f"columns {', '.join(map(repr, paired.models))}"
        )
    result = _signed_rank_cluster_bootstrap(paired, settings, params)
    return dataclasses.replace(result, per_model=()) if pooled else result
