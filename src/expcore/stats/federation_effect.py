# Provenance: new
"""Site and federation effects of an elastic campaign, and the sensitivity rerun that takes them out.

An elastic campaign runs each run on one federation, a compute job, at one site. The primary analysis ignores both:
its estimators see only a clusters-by-models matrix of paired differences. The sensitivity rerun asks what the
contrasts would be if each run's outcome were corrected for where it ran. It is reported beside the primary result and
never replaces it.

The model, per outcome, over the runs that have a value for it:

    y = cell + task + site + u_federation + e,    u ~ N(0, tau2),    e ~ N(0, sigma2).

`cell` is one mean per (condition, model) present. `task` and `site` are fixed effects coded against the first name in
sorted order. Federations are nested in sites. The variances come from restricted maximum likelihood (REML) with one
variance component. With lambda = tau2 / sigma2 the covariance is sigma2 H, H = I + lambda Z Z', Z holding one indicator
column per federation. H is block-diagonal by federation: H^-1 = I - lambda / (1 + lambda n_g) J_g on the block of a
federation of n_g runs, and log|H| is the sum of log(1 + lambda n_g). The likelihood reads only sums over federations,
and no matrix with a row per pair of runs is built. sigma2 is profiled out. The profile is scanned over log lambda from
-12 to 8, the best point is refined by a bounded scalar search, and the result is compared with lambda = 0, which wins
a tie, so a federation variance of exactly zero is a possible answer.

The fixed coefficients are the generalised least squares ones at the fitted lambda. The federation effects are the best
linear unbiased predictions (BLUPs): lambda / (1 + lambda n_g) times the sum of the federation's residuals from the
fixed part. The site effects are the site coefficients, centred so that their run-weighted mean over the fitted runs is
zero, which leaves the overall level where it was. An adjusted outcome is the outcome less its site effect and its
federation effect.

The fixed design can be rank deficient: a site that holds exactly the runs of one cell, or a task run at one site only.
Columns are taken in the order cells, tasks, sites, and a column that is a combination of the earlier ones is left out,
so what it would carry stays with the earlier effect: a site aliased to a cell or a task gets the effect 0, and the cell
or task keeps the shift. No value is moved by an arbitrary split between the two.

An outcome is refused, and its values stay as they are, when its variances cannot be estimated: fewer than 3 runs have
it, no federation holds two or more of its runs, the federations are confounded with the fixed effects (one federation,
or one federation per site), the fixed effects use every degree of freedom, or they leave no residual variation. One
site is not a refusal: its effect is 0.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

import numpy as np
from scipy import optimize, sparse
from scipy.linalg import solve_triangular

from expcore.stats.analysis import AnalysisResult, analyse
from expcore.stats.prereg import Preregistration
from expcore.stats.rows import AbsentRun, Row

MIN_RUNS = 3  # fewer fitted runs than this and the outcome is not adjusted
LOG_LAMBDA = (-12.0, 8.0)  # the range of log(tau2 / sigma2) searched; lambda = 0 is compared with it separately
GRID_POINTS = 33  # the scan that picks the neighbourhood the scalar search refines
RANK_TOLERANCE = 1e-8  # a design column is dropped when this share of its squared length lies outside the earlier ones
NO_VARIATION = 1e-12  # the share of an outcome's variation below which nothing is left to fit
READ_BY_RATIO = "read by a ratio contrast: an additive adjustment of its sums is not defined"
RATIO_COLUMNS = ("numerator", "denominator", "numerator_a", "numerator_b", "denominator_a", "denominator_b")


@dataclass(frozen=True)
class OutcomeFit:
    """What was estimated for one outcome. With `refused` set the outcome is not adjusted and the effects are empty."""

    outcome: str
    runs: int  # rows whose outcome is not None, used in the fit
    sites: tuple[str, ...]  # sorted
    federations: tuple[str, ...]  # sorted
    sigma2: float | None  # residual variance (None when refused)
    tau2: float | None  # federation variance (None when refused)
    site_effects: Mapping[str, float]  # centred: their run-weighted mean over the fitted rows is 0
    federation_effects: Mapping[str, float]  # the BLUPs
    refused: str | None = None  # why this outcome is not adjusted


@dataclass(frozen=True)
class FederationAnalysis:
    """The fits, the outcomes left alone, and the primary analysis rerun on the adjusted rows."""

    fits: tuple[OutcomeFit, ...]  # one per adjusted-or-refused outcome, sorted by outcome
    unadjusted: tuple[tuple[str, str], ...]  # (outcome, reason) for outcomes deliberately left as they are
    sensitivity: AnalysisResult | None  # analyse() on the adjusted rows; None when refused
    refused: str | None = None  # why there is no rerun at all


class _Refusal(Exception):
    """A reason one outcome cannot be adjusted. `adjust` reports it in `OutcomeFit.refused`."""


@dataclass(frozen=True)
class _Solution:
    """One fit's variances and effects, in the outcome's own units."""

    sigma2: float
    tau2: float
    site_effects: dict[str, float]
    federation_effects: dict[str, float]


def _placements(rows: Sequence[Row], placement: Mapping[str, tuple[str, str]]) -> list[tuple[str, str]]:
    """The (site, federation) of each row. Every run must be placed and each federation sits at one site: a caller bug
    otherwise, so it raises, naming the run or the federation."""
    places: list[tuple[str, str]] = []
    home: dict[str, str] = {}
    for row in rows:
        if row.run_key not in placement:
            raise ValueError(f"{row.describe()} has no placement: every analysed run needs its (site, federation)")
        place = placement[row.run_key]
        if not (isinstance(place, tuple | list) and len(place) == 2 and all(isinstance(p, str) and p for p in place)):
            raise ValueError(f"{row.describe()}: a placement is a (site, federation) pair of names, got {place!r}")
        site, federation = place
        if home.setdefault(federation, site) != site:
            raise ValueError(
                f"federation {federation!r} is placed at two sites, {home[federation]!r} and {site!r}: "
                "a federation is nested in one site"
            )
        places.append((site, federation))
    return places


def _codes(keys: Sequence[Any]) -> tuple[np.ndarray, list]:
    """Each key as the position of its level among the sorted distinct keys, and those levels."""
    levels = sorted(set(keys))
    index = {level: position for position, level in enumerate(levels)}
    return np.fromiter((index[key] for key in keys), dtype=np.intp, count=len(keys)), levels


def _one_hot(codes: np.ndarray, width: int) -> sparse.csr_matrix:
    """The indicator matrix of the codes, one column per level."""
    return sparse.csr_matrix((np.ones(len(codes)), (np.arange(len(codes)), codes)), shape=(len(codes), width))


def _indicators(keys: Sequence[Any]) -> tuple[sparse.csr_matrix, list]:
    codes, levels = _codes(keys)
    return _one_hot(codes, len(levels)), levels


def _independent_columns(gram: np.ndarray) -> list[int]:
    """The columns of a design, in order, that are not combinations of the ones before them, found from its Gram matrix
    with a growing Cholesky factor (kept columns only). A column whose squared part outside the earlier ones is under
    RANK_TOLERANCE of its own squared length is left out."""
    kept: list[int] = []
    factor = np.zeros_like(gram)  # mutated in place, one row per kept column: the point is to grow it without copies
    for j in range(gram.shape[0]):
        k = len(kept)
        head = solve_triangular(factor[:k, :k], gram[kept, j], lower=True) if k else np.empty(0)
        rest = gram[j, j] - float(head @ head)
        if rest > RANK_TOLERANCE * gram[j, j]:
            factor[k, :k] = head
            factor[k, k] = math.sqrt(rest)
            kept.append(j)
    return kept


def _design(rows: Sequence[Row], sites: Sequence[str]) -> tuple[sparse.csr_matrix, dict[str, int]]:
    """The fixed design of full column rank, and the column of each site that stayed in it.

    Cells (all of them), tasks and sites (each without its first name in sorted order), in that order; a dependent
    column is dropped, so a site with no effect of its own beside the cells and tasks has no column."""
    cells, _ = _indicators([(row.condition, row.model) for row in rows])
    tasks, _ = _indicators([row.task for row in rows])
    site_columns, site_levels = _indicators(sites)
    full = sparse.hstack([cells, tasks[:, 1:], site_columns[:, 1:]], format="csr")
    kept = _independent_columns((full.T @ full).toarray())
    first_site = cells.shape[1] + tasks.shape[1] - 1
    position = {column: k for k, column in enumerate(kept)}
    sited = {site: position[first_site + j] for j, site in enumerate(site_levels[1:]) if first_site + j in position}
    return full[:, kept], sited


@dataclass(frozen=True)
class _Reml:
    """What the restricted likelihood of one outcome (on the unit scale) reads, as sums over federations."""

    xtx: np.ndarray
    xty: np.ndarray
    yty: float
    sums: np.ndarray  # per federation, the sum of each design column over its runs
    totals: np.ndarray  # per federation, the sum of the outcome
    counts: np.ndarray  # runs per federation
    n: int

    @classmethod
    def of(cls, x: sparse.csr_matrix, y: np.ndarray, group: np.ndarray, federations: int) -> _Reml:
        """The sums of a design, an outcome and a federation of each run."""
        z = _one_hot(group, federations)
        return cls(
            xtx=(x.T @ x).toarray(),
            xty=np.asarray(x.T @ y).ravel(),
            yty=float(y @ y),
            sums=(z.T @ x).toarray(),
            totals=np.bincount(group, weights=y, minlength=federations),
            counts=np.bincount(group, minlength=federations).astype(float),
            n=len(y),
        )

    @property
    def df(self) -> int:
        """Residual degrees of freedom: runs less design columns."""
        return self.n - self.xtx.shape[0]

    def weights(self, lam: float) -> np.ndarray:
        """lambda / (1 + lambda n_g) per federation: what H^-1 and the BLUPs shrink a federation's sums by."""
        return lam / (1.0 + lam * self.counts)

    def fixed_part(self, lam: float) -> tuple[np.ndarray, float, float]:
        """The GLS coefficients at lam, the residual form r' H^-1 r, and log|X' H^-1 X| (NaN if not positive)."""
        w = self.weights(lam)
        a = self.xtx - self.sums.T @ (w[:, None] * self.sums)
        b = self.xty - self.sums.T @ (w * self.totals)
        beta = np.linalg.lstsq(a, b, rcond=None)[0]
        rss = self.yty - float(w @ self.totals**2) - float(b @ beta)
        sign, logdet = np.linalg.slogdet(a)
        return beta, rss, float(logdet) if sign > 0 else math.nan

    def criterion(self, lam: float) -> float:
        """Minus twice the restricted log-likelihood with sigma2 profiled out, up to a constant; inf if undefined."""
        _, rss, logdet = self.fixed_part(lam)
        if not (rss > 0 and math.isfinite(logdet)):
            return math.inf
        return self.df * math.log(rss / self.df) + float(np.log1p(lam * self.counts).sum()) + logdet


def _best_lambda(reml: _Reml) -> float:
    """lambda that minimises the criterion: the best of a scan over log lambda, refined by a bounded scalar search in
    its neighbourhood, and compared with the boundary lambda = 0, which wins a tie."""
    grid = np.linspace(*LOG_LAMBDA, GRID_POINTS)

    def at(log_lam: float) -> float:
        return reml.criterion(math.exp(log_lam))

    values = [at(point) for point in grid]
    best = int(np.argmin(values))
    low, high = grid[max(best - 1, 0)], grid[min(best + 1, GRID_POINTS - 1)]
    refined = optimize.minimize_scalar(at, bounds=(low, high), method="bounded", options={"xatol": 1e-6})
    log_lam, value = (refined.x, refined.fun) if refined.fun < values[best] else (grid[best], values[best])
    return math.exp(log_lam) if value < reml.criterion(0.0) else 0.0


def _require_runs(feds: Sequence[str]) -> None:
    """Refuse an outcome with too few runs, or in which every federation holds one run."""
    if len(feds) < MIN_RUNS:
        raise _Refusal(f"fewer than {MIN_RUNS} runs have the outcome")
    if max(Counter(feds).values()) < 2:
        raise _Refusal("no federation holds two or more of its runs, so the federation variance is not identifiable")


def _require_information(reml: _Reml) -> None:
    """Refuse a design that leaves no residual to estimate from, or federations the fixed effects already contain."""
    if reml.df <= 0:
        raise _Refusal("no residual degrees of freedom: the fixed effects take as many columns as there are runs")
    if reml.fixed_part(0.0)[1] <= NO_VARIATION * reml.n:
        raise _Refusal("the fixed effects leave no residual variation, so no variance can be estimated")
    outside = reml.counts - np.einsum("gp,pq,gq->g", reml.sums, np.linalg.pinv(reml.xtx), reml.sums)
    if outside.max() <= RANK_TOLERANCE * reml.counts.max():
        raise _Refusal(
            "the federations are confounded with the fixed effects (one federation, or one federation per site), "
            "so the federation variance is not identifiable"
        )


@dataclass(frozen=True)
class _Problem:
    """One outcome ready to fit: standardised outcome, its design, and the labels the effects are reported under."""

    y: np.ndarray
    spread: float  # the outcome's standard deviation, which effects and variances are scaled back by
    x: sparse.csr_matrix
    sited: dict[str, int]
    sites: list[str]  # of each run
    site_levels: list[str]
    group: np.ndarray  # each run's federation, a position in `federations`
    federations: list[str]
    reml: _Reml


def _problem(rows: Sequence[Row], values: Sequence[float], sites: Sequence[str], feds: Sequence[str]) -> _Problem:
    """Standardise the outcome and build its design and sums; raises _Refusal when it cannot be fitted."""
    _require_runs(feds)
    raw = np.asarray(values, dtype=float)
    spread = float(raw.std())
    if spread <= NO_VARIATION * float(np.abs(raw).max()):
        raise _Refusal("the outcome has no variation")
    x, sited = _design(rows, sites)
    group, federations = _codes(feds)
    y = (raw - raw.mean()) / spread
    reml = _Reml.of(x, y, group, len(federations))
    _require_information(reml)
    return _Problem(y, spread, x, sited, list(sites), sorted(set(sites)), group, federations, reml)


def _solve(problem: _Problem) -> _Solution:
    """The REML fit and its effects, in the outcome's own units."""
    reml = problem.reml
    lam = _best_lambda(reml)
    beta, rss, _ = reml.fixed_part(lam)
    sigma2 = rss / reml.df
    residual = problem.y - problem.x @ beta
    by_federation = np.bincount(problem.group, weights=residual, minlength=len(problem.federations))
    blup = reml.weights(lam) * by_federation
    coefficient = {
        site: float(beta[problem.sited[site]]) if site in problem.sited else 0.0 for site in problem.site_levels
    }
    runs = Counter(problem.sites)
    level = sum(runs[site] * value for site, value in coefficient.items()) / reml.n
    scale = problem.spread
    return _Solution(
        sigma2 * scale**2,
        lam * sigma2 * scale**2,
        {site: (value - level) * scale for site, value in coefficient.items()},
        {name: float(value) * scale for name, value in zip(problem.federations, blup, strict=True)},
    )


def adjust(
    rows: Sequence[Row], placement: Mapping[str, tuple[str, str]], outcome: str
) -> tuple[list[float | None], OutcomeFit]:
    """The adjusted values of `outcome` for each row, in row order, and the fit.

    `placement[run_key]` is the (site, federation) of the run. A run whose outcome is None, or that has no such
    outcome, is not fitted and keeps None. An adjusted value is the outcome less the site's effect and the federation's
    effect, both taken from one offset per federation, so equal outcomes of one federation stay equal. A refused
    outcome (see the module docstring) comes back unchanged, with the reason in `fit.refused` and no effects. A run
    missing from `placement`, or a federation placed at two sites, is a caller bug and raises ValueError.
    """
    rows = list(rows)
    places = _placements(rows, placement)
    values = [row.outcomes.get(outcome) for row in rows]
    used = [index for index, value in enumerate(values) if value is not None]
    sites, feds = [places[index][0] for index in used], [places[index][1] for index in used]
    fit = OutcomeFit(outcome, len(used), tuple(sorted(set(sites))), tuple(sorted(set(feds))), None, None, {}, {})
    try:
        solution = _solve(_problem([rows[index] for index in used], [values[index] for index in used], sites, feds))
    except _Refusal as refusal:  # the reason is the result, reported in the fit
        return values, replace(fit, refused=str(refusal))
    offsets = {
        (site, fed): solution.site_effects[site] + solution.federation_effects[fed]
        for site, fed in set(zip(sites, feds, strict=True))
    }
    adjusted = list(values)
    for index, site, fed in zip(used, sites, feds, strict=True):
        adjusted[index] = float(values[index]) - offsets[(site, fed)]
    return adjusted, replace(fit, **asdict(solution))  # the solution's fields are the fit's own


def _outcomes_to_adjust(prereg: Preregistration) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """The outcomes of contrasts of one outcome that no ratio contrast reads, and those one reads, with the reason."""
    single = {spec.outcome for spec in prereg.contrasts.values() if spec.ratio is None}
    read = {
        getattr(spec.ratio, column)
        for spec in prereg.contrasts.values()
        if spec.ratio is not None
        for column in RATIO_COLUMNS
    }
    read.discard(None)
    return tuple(sorted(single - read)), tuple((name, READ_BY_RATIO) for name in sorted(single & read))


def _why_no_rerun(names: Sequence[str], fits: Sequence[OutcomeFit]) -> str:
    """The reason no outcome was adjusted: none to adjust, or each one's refusal."""
    if not names:
        return "no contrast of one outcome has a column to adjust: each is read by a ratio contrast, or there is none"
    return "no outcome could be adjusted: " + "; ".join(f"{fit.outcome}: {fit.refused}" for fit in fits)


def _adjusted_row(row: Row, index: int, columns: Mapping[str, list[float | None]]) -> Row:
    """The row with each adjusted column's value in place of its own; a column it lacks or has as None stays so."""
    changes = {name: values[index] for name, values in columns.items() if row.outcomes.get(name) is not None}
    return replace(row, outcomes={**row.outcomes, **changes}) if changes else row


def federation_analysis(
    rows: Sequence[Row],
    absent: Sequence[AbsentRun],
    prereg: Preregistration,
    placement: Mapping[str, tuple[str, str]],
) -> FederationAnalysis:
    """The primary analysis rerun with site as a fixed and federation as a random effect.

    The outcomes adjusted are those of the preregistered contrasts of one outcome (`spec.ratio is None`), less every
    column a ratio contrast reads. A column that both a contrast of one outcome and a ratio contrast read is left as it
    is and listed in `unadjusted` with the reason: an additive adjustment of the sums of a ratio is not defined. Ratio
    contrasts are refit on their unadjusted columns, so their result in the rerun equals the primary's, and they still
    enter the rerun's Holm families. Each adjusted outcome gets one fit (`adjust`); a refused one stays as it is.

    New rows carry the adjusted outcomes and `analyse` runs on them, with the same preregistration and absent runs: the
    same pairing, estimator and Holm machinery. The rerun refits the contrasts only: the one-arm descriptives (S14)
    are left out of it, since an adjusted value is no longer a rate or a total they can read. With no
    outcome to adjust, or every one refused, there is no rerun: `sensitivity` is None and `refused` says why. The inputs
    are only read. A run missing from `placement`, or a federation at two sites, raises ValueError.
    """
    rows, absent = list(rows), list(absent)
    _placements(rows, placement)  # a caller bug raises here even when nothing is to be adjusted
    names, unadjusted = _outcomes_to_adjust(prereg)
    fits, columns = [], {}
    for name in names:
        values, fit = adjust(rows, placement, name)
        fits.append(fit)
        if fit.refused is None:
            columns[name] = values
    if not columns:
        return FederationAnalysis(tuple(fits), unadjusted, None, _why_no_rerun(names, fits))
    adjusted_rows = [_adjusted_row(row, index, columns) for index, row in enumerate(rows)]
    contrasts_only = replace(prereg, descriptives={})
    return FederationAnalysis(tuple(fits), unadjusted, analyse(adjusted_rows, contrasts_only, absent))
