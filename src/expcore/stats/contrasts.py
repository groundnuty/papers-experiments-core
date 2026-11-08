# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/analysis/contrasts.py, blob 075305b68276374c1f62e3439eb94b4979491f68
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Aggregation, paired region-level tests, Holm, cluster bootstrap (#14).

Stages (each a plain mean): seeds -> instance -> region x model -> region.
A contrast A-B is paired at the (instance, model) cell *before* averaging up,
so a cell missing in one option drops out of that contrast only. The region is
the unit of inference. All options pass through the same code.
"""
from __future__ import annotations

import dataclasses
import math
import warnings

import numpy as np
import pandas as pd
from scipy import stats

from dataset_76.analysis.config import Config


@dataclasses.dataclass(frozen=True)
class Outcome:
    name: str
    column: str
    primary: bool          # one of the pre-registered primary outcomes (coverage counts as two)
    scale: str             # "proportion" or "log2"
    smallest: str | None   # "proportion", "cost" or None (no pre-registered smallest effect)


OUTCOMES = [
    Outcome("omission", "omit", True, "proportion", "proportion"),
    # coverage is two outcomes, never combined: each is its own Holm family per layout
    Outcome("coverage_baseline", "coverage_baseline", True, "proportion", "proportion"),
    Outcome("coverage_target", "coverage_target", True, "proportion", "proportion"),
    Outcome("event_f1", "event_f1", True, "proportion", None),
    Outcome("tokens", "log2_tokens", True, "log2", "cost"),
    Outcome("undisclosed", "undis", False, "proportion", None),
    Outcome("end_to_end", "end_to_end", False, "proportion", None),
    Outcome("false_alarm", "false_alarm", False, "proportion", None),
]
OUTCOME = {o.name: o for o in OUTCOMES}


def grid_decimals(cfg: Config) -> int:
    """Decimal places of the rounding grid, from `inference.zero_tolerance` (1e-12 -> 12)."""
    return int(round(-math.log10(float(cfg["inference"]["zero_tolerance"]))))


def snap(x, cfg: Config) -> np.ndarray:
    """Round to 12 decimal places (science, #14, 19:11Z): far below the data's own grid, so it merges the float noise the chain of
    means leaves (about 1e-16 relative: 0.1 + 0.2 against 0.3) and never two values that really differ (omission and coverage sit on
    grids of 1/1050 or coarser at region level; a coverage share's own grid is above 1e-9). Every Wilcoxon input and every comparison
    with the smallest effect goes through it. -0.0 becomes 0.0."""
    return np.round(np.asarray(x, dtype=float), grid_decimals(cfg)) + 0.0


# Instance domain of the outcomes that exist on only one kind of instance. Out-of-domain
# instances are not part of that outcome at all: not undefined, not dropped, not imputed.
DOMAIN = {"event_f1": lambda d: ~d["event_free"].astype(bool),     # events to match
          "false_alarm": lambda d: d["event_free"].astype(bool)}   # event-free instances only


def instance_values(df: pd.DataFrame, column: str, defined_only: bool = True, impute: float | None = None) -> pd.DataFrame:
    """Seeds -> instance: mean over the seeds whose denominator is defined.

    An instance with no defined seed is *undefined* for the outcome: dropped
    (`defined_only`, default), kept with value NaN (`defined_only=False`, so the
    design is still visible), or set to `impute` (sensitivity analysis: 1 and 0).
    """
    keys = ["option", "layout", "model", "instance_id", "region_id"]
    if column in DOMAIN:
        df = df[DOMAIN[column](df)]
    g = df.groupby(keys, as_index=False, sort=True)[column].mean()   # NaN where no seed defined
    g = g.rename(columns={column: "value"})
    if impute is not None:
        g["value"] = g["value"].fillna(impute)
    elif defined_only:
        g = g.dropna(subset=["value"]).reset_index(drop=True)
    return g


def candidate_regions(inst: pd.DataFrame, a: str, b: str, layout: str) -> list:
    """Regions present in the design for this layout in either arm (defined or not)."""
    m = (inst["layout"] == layout) & inst["option"].isin([a, b])
    return sorted(inst.loc[m, "region_id"].unique())


def paired_matrix(inst: pd.DataFrame, a: str, b: str, layout: str, pair: str = "region"):
    """Region x model matrix of mean paired differences A-B.

    pair="region" (the protocol rule): each arm's region value is the mean of the
    instances defined in that arm (per model); the difference is taken between arms.
    A region with no defined instance in either arm (for every model) is dropped.
    pair="instance" (pagination cell): the arms are first paired at the (instance, model)
    cell, so only instances defined in both arms count.
    Returns (matrix, regions, models) with all-NaN regions and models removed.
    """
    lay = inst["layout"].to_numpy() == layout
    opt = inst["option"].to_numpy()[lay]
    mod = inst["model"].to_numpy(dtype=object)[lay]
    ins = inst["instance_id"].to_numpy(dtype=object)[lay]
    reg = inst["region_id"].to_numpy(dtype=object)[lay]
    val = inst["value"].to_numpy(float)[lay]
    models = np.sort(np.unique(mod))
    insts, first = np.unique(ins, return_index=True)
    regions = np.sort(np.unique(reg))
    reg_of = reg[first]
    member = (reg_of[None, :] == regions[:, None]).astype(float)  # (R, I)

    def dense(o):
        v = np.full((len(models), len(insts)), np.nan)
        m = opt == o
        v[np.searchsorted(models, mod[m]), np.searchsorted(insts, ins[m])] = val[m]
        return v

    def region_mean(v):                                        # (M, I) -> (M, R), mean of defined instances
        ok = ~np.isnan(v)
        num = np.where(ok, v, 0.0) @ member.T
        cnt = ok.astype(float) @ member.T
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(cnt > 0, num / cnt, np.nan)

    va, vb = dense(a), dense(b)
    if pair == "instance":
        d = va - vb
        mat = region_mean(d).T
    elif pair == "region":
        mat = (region_mean(va) - region_mean(vb)).T
    else:
        raise ValueError(pair)
    if not mat.size:
        return mat[:0, :0], [], []
    keep_r, keep_m = ~np.all(np.isnan(mat), axis=1), ~np.all(np.isnan(mat), axis=0)
    return mat[np.ix_(keep_r, keep_m)], list(regions[keep_r]), list(models[keep_m])


def paired_region_model(inst: pd.DataFrame, a: str, b: str, layout: str, pair: str = "region") -> pd.DataFrame:
    """Long form of `paired_matrix` (columns region_id, model, diff), cells without data dropped."""
    mat, regions, models = paired_matrix(inst, a, b, layout, pair)
    if not len(regions):
        return pd.DataFrame(columns=["region_id", "model", "diff"])
    df = pd.DataFrame(mat, index=regions, columns=models).rename_axis(index="region_id", columns="model")
    return df.stack().rename("diff").reset_index()


def _matrix(rm: pd.DataFrame) -> tuple[np.ndarray, list[str], list[str]]:
    piv = rm.pivot(index="region_id", columns="model", values="diff").sort_index()
    return piv.to_numpy(float), list(piv.index), list(piv.columns)


def _equal_model_mean(mat: np.ndarray) -> np.ndarray:
    """Per-resample statistic: mean over regions per model, then over models. mat: (..., R, M)."""
    mask = ~np.isnan(mat)
    num = np.where(mask, mat, 0.0).sum(axis=-2)
    den = mask.sum(axis=-2)
    with np.errstate(invalid="ignore", divide="ignore"):
        per_model = np.where(den > 0, num / den, np.nan)
    return per_model  # (..., M)


def region_means(mat: np.ndarray) -> np.ndarray:
    """Region-level difference: equal-weight mean over the models with a paired cell."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(mat, axis=-1)


def cluster_bootstrap(mat: np.ndarray, B: int, level: float, rng: np.random.Generator):
    """Region-cluster bootstrap. One region resample is shared by all models.

    Overall statistic = mean over regions of the region-level difference (exactly the
    quantity the Wilcoxon test sees). Per-model statistics = mean over regions of that
    model's column. Returns overall (point, lo, hi) and per-model arrays (point, lo, hi).
    """
    R, M = mat.shape
    reg = region_means(mat)                      # (R,)
    point = float(np.mean(reg))
    idx = rng.integers(0, R, size=(B, R))
    overall = reg[idx].mean(axis=1)
    point_pm = _equal_model_mean(mat)
    pm = _equal_model_mean(mat[idx])             # (B, M)
    q = [(1 - level) / 2 * 100, (1 + level) / 2 * 100]
    lo, hi = np.percentile(overall, q)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        mlo, mhi = np.nanpercentile(pm, q, axis=0)
    return (point, lo, hi), (point_pm, mlo, mhi)


def _wilcoxon_input(diffs: np.ndarray, cfg: Config) -> np.ndarray:
    """The differences scipy sees: untestable regions (NaN) removed, float noise snapped to zero.

    A region difference that is zero in arithmetic (T and D agree) comes out of the chain of means
    as +-1e-17 about two times in three; scipy would rank that noise as the smallest signed rank
    instead of dropping it as a zero. Differences below `inference.zero_tolerance` (1e-12; the
    smallest real difference of any outcome is orders of magnitude larger) are therefore exact zeros.
    The rest are rounded to the same grid (`snap`): differences that are equal in arithmetic reach scipy as
    equal, so they tie, whatever order the means were summed in (#14 dry run: p moved 1.12e-5 -> 6.09e-6).
    """
    d = diffs[~np.isnan(diffs)]
    return snap(np.where(np.abs(d) < float(cfg["inference"]["zero_tolerance"]), 0.0, d), cfg)


def zeros_dropped(diffs: np.ndarray, cfg: Config) -> int:
    """How many region differences are zero and so dropped by the Wilcoxon test.

    Counted on the array `wilcoxon_p` hands to scipy, with scipy's own test (`d == 0`). Only
    zero_method "wilcox" drops them; "pratt" and "zsplit" keep them, so those report 0.
    """
    if cfg["inference"]["wilcoxon_zero_method"] != "wilcox":
        return 0
    return int((_wilcoxon_input(diffs, cfg) == 0).sum())


def wilcoxon_p(diffs: np.ndarray, cfg: Config) -> float:
    d = _wilcoxon_input(diffs, cfg)
    if d.size == 0:
        return float("nan")
    if np.all(d == 0):
        return 1.0                      # no evidence of a difference; scipy would raise
    inf = cfg["inference"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return float(stats.wilcoxon(d, zero_method=inf["wilcoxon_zero_method"],
                                    method=inf["wilcoxon_method"]).pvalue)


def holm(p: pd.Series) -> pd.Series:
    """Holm step-down adjusted p over one pre-registered family.

    The family keeps its full size: an untestable contrast (NaN) counts as p = 1 in the
    adjustment, so it cannot shrink the family and make the others look more significant.
    Its own adjusted value stays NaN. Written out (not statsmodels.multipletests, which
    calls gc.collect on every call); the tests check it against statsmodels.
    """
    out = pd.Series(np.nan, index=p.index)
    if p.notna().sum() == 0:
        return out
    v = p.fillna(1.0).to_numpy(float)
    order = np.argsort(v, kind="stable")
    m = len(v)
    adj = np.maximum.accumulate((m - np.arange(m)) * v[order])
    res = np.empty(m)
    res[order] = np.minimum(adj, 1.0)
    out[:] = np.where(p.isna().to_numpy(), np.nan, res)
    return out


def contrast_one(inst: pd.DataFrame, a: str, b: str, layout: str, outcome: Outcome,
                 cfg: Config, bootstrap: bool = True, rng: np.random.Generator | None = None,
                 pair: str = "region"):
    """One contrast on one outcome and layout. Returns (summary dict, per-model DataFrame)."""
    mat, regions, models = paired_matrix(inst, a, b, layout, pair)
    row = {"outcome": outcome.name, "layout": layout, "contrast": f"{a}-{b}", "option_a": a, "option_b": b}
    row["n_regions_dropped"] = len(candidate_regions(inst, a, b, layout)) - len(regions)
    if not regions:
        row.update(n_regions=0, n_models=0, n_zero_dropped=0, effect=np.nan, ci_lo=np.nan, ci_hi=np.nan, p_raw=np.nan)
        return row, pd.DataFrame()
    region_diff = region_means(mat)
    pm_point = _equal_model_mean(mat)
    point = float(np.mean(region_diff))
    row.update(n_regions=len(regions), n_models=len(models), effect=point,
               n_zero_dropped=zeros_dropped(region_diff, cfg),    # zero region differences the Wilcoxon test drops
               p_raw=wilcoxon_p(region_diff, cfg))
    permodel = pd.DataFrame({"outcome": outcome.name, "layout": layout, "contrast": f"{a}-{b}",
                             "model": models, "effect": pm_point})
    if bootstrap:
        bs = cfg["bootstrap"]
        rng = rng or np.random.default_rng(bs["seed"])
        (pt, lo, hi), (_, mlo, mhi) = cluster_bootstrap(mat, int(bs["B"]), float(bs["ci_level"]), rng)
        row.update(effect=float(pt), ci_lo=float(lo), ci_hi=float(hi))
        permodel["ci_lo"], permodel["ci_hi"] = mlo, mhi
    else:
        row.update(ci_lo=np.nan, ci_hi=np.nan)
    permodel["direction"] = np.sign(permodel["effect"]).astype("Int64")
    return row, permodel


def _decorate(tab: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Holm within outcome x layout, effect on the reporting scale, 'counts for the guide'."""
    eff = cfg["effects"]
    tab["p_holm"] = tab.groupby(["outcome", "layout"], group_keys=False)["p_raw"].apply(holm)
    tab["significant"] = np.where(tab["p_holm"].isna(), np.nan, (tab["p_holm"] < cfg.alpha).astype(float))
    sm = []
    for _, r in tab.iterrows():
        k = OUTCOME[r["outcome"]].smallest
        sm.append({"proportion": eff["smallest_effect_proportion"],
                   "cost": float(np.log2(eff["smallest_effect_cost_factor"])), None: np.nan}[k])
    tab["smallest_effect"] = sm
    undefined = tab["smallest_effect"].isna() | tab["effect"].isna() | tab["p_holm"].isna()
    # "at least" the smallest effect (registration, as science ruled on #14, 19:11Z): |effect| >= 0.10, or |log2 ratio| >= 1 for cost,
    # both sides rounded to the same grid as the tests' inputs, so an effect exactly at the smallest effect counts whatever its float noise
    at_least = (snap(tab["effect"].abs(), cfg) >= snap(tab["smallest_effect"], cfg)).astype(float)
    tab["at_least_smallest"] = np.where(tab["smallest_effect"].isna() | tab["effect"].isna(), np.nan, at_least)
    tab["counts_for_guide"] = np.where(undefined, np.nan, ((tab["p_holm"] < cfg.alpha) & (at_least == 1.0)).astype(float))
    is_tok = tab["outcome"] == "tokens"
    for c in ("effect", "ci_lo", "ci_hi"):
        tab[f"{c}_ratio"] = np.where(is_tok, 2.0 ** tab[c], np.nan)
    return tab


def contrast_tables(df: pd.DataFrame, cfg: Config, outcomes: list[str] | None = None,
                    bootstrap: bool = True, value_override: dict[str, str] | None = None,
                    impute: float | None = None):
    """All option contrasts x layouts x outcomes. Returns (summary, per-model) DataFrames.

    `value_override` maps an outcome name to another column of `df` (used by the
    bounds, which swap the omission column).
    """
    outs = [OUTCOME[n] for n in (outcomes or [o.name for o in OUTCOMES])]
    rng = np.random.default_rng(cfg["bootstrap"]["seed"])
    rows, pms = [], []
    main = df[df.option.isin(cfg.options)]
    for o in outs:
        col = (value_override or {}).get(o.name, o.column)
        inst = instance_values(main, col, defined_only=False, impute=impute)
        for layout in cfg.layouts:
            for a, b in cfg.contrasts:
                r, pm = contrast_one(inst, a, b, layout, o, cfg, bootstrap, rng)
                rows.append(r)
                pms.append(pm)
    tab = _decorate(pd.DataFrame(rows), cfg)
    tab["primary"] = tab["outcome"].map(lambda n: OUTCOME[n].primary)
    pm = pd.concat([p for p in pms if len(p)], ignore_index=True) if any(len(p) for p in pms) else pd.DataFrame()
    return tab, pm
