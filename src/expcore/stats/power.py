# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/analysis/secondary.py, blob 142c57b3fcb519af1e7d90e24f13bf4bf1364365
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Denominators and bounds, pagination cell, instrument-error sensitivity,
mixed-effects models, power by simulation (#14). Every option passes through
the same code path."""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from scipy import stats

from dataset_76.analysis import audit as audit_mod, contrasts as C, inspection as INS
from dataset_76.analysis.config import Config


# ---------------------------------------------------------------- denominators
# outcome -> (lower-bound column, upper-bound column), per protocol/report-spec.md
BOUND_COLS = {"omission": ("omit_lower", "omit_upper"),
              "undisclosed": ("undis_lower", "undis_upper"),
              "end_to_end": ("end_to_end", "end_to_end_upper")}


def denominator_table(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Run counts per option x layout under each fixed denominator, and pooled run-level rates.

    O, C, U: finished (not time-capped) runs labelled omitting / complete / unknown;
    K: time-capped runs; O' / U': omitting / unknown runs whose report says complete == true.
    Omission = O/(O+C), bounds O/(O+C+U+K) and (O+U+K)/(O+C+U+K).
    Undisclosed = O'/(O+C+U-U'), bounds O'/(O+C+U) and (O'+U')/(O+C+U); K excluded.
    End-to-end over all runs N, upper bound adds finished, label U, correct.
    Pooled over models, regions and seeds: descriptive; the inferential versions are in the
    contrast tables.
    """
    d = df[df.option.isin(cfg.options + [cfg["pagination"]["option"]])]
    g = d.groupby(["option", "layout"], sort=True)
    fin = d[~d.time_cap & d.finished]
    gf = fin.groupby(["option", "layout"], sort=True)
    out = pd.DataFrame({
        "N": g.size(),
        "O": gf["omit"].sum(), "C": gf["omit"].count() - gf["omit"].sum(), "U": gf["unknown"].sum(),
        "K": g["time_cap"].sum(),
        "O_prime": gf["undis_lower"].sum(),
        "U_prime": gf["undis_upper"].sum() - gf["undis_lower"].sum(),
        "unfinished_other": g["finished"].apply(lambda x: (~x).sum()) - g["time_cap"].sum(),
        "end_to_end": g["end_to_end"].sum(), "end_to_end_upper": g["end_to_end_upper"].sum(),
        "event_free_runs": g["event_free"].sum(),
    }).fillna(0).infer_objects().reset_index()
    O, C_, U, K, Op, Up = (out[c] for c in ("O", "C", "U", "K", "O_prime", "U_prime"))
    with np.errstate(invalid="ignore", divide="ignore"):
        out["omission_rate"] = O / (O + C_)
        out["omission_lower"] = O / (O + C_ + U + K)
        out["omission_upper"] = (O + U + K) / (O + C_ + U + K)
        out["undisclosed_rate"] = Op / (O + C_ + U - Up)
        out["undisclosed_lower"] = Op / (O + C_ + U)
        out["undisclosed_upper"] = (Op + Up) / (O + C_ + U)
        out["end_to_end_rate"] = out["end_to_end"] / out["N"]
        out["end_to_end_rate_upper"] = out["end_to_end_upper"] / out["N"]
        out["time_cap_share"] = K / out["N"]
    return out


def bound_tables(df: pd.DataFrame, cfg: Config, bootstrap: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Contrasts of the bounded outcomes (omission, undisclosed omission, end-to-end) with the
    uncertain runs counted once against and once for the outcome (the lower- and upper-bound
    columns of `BOUND_COLS`), plus the extreme interval of the point effect (A at its upper
    bound against B at its lower bound, and the reverse), paired like the contrasts."""
    res = []
    for scen, k in (("lower_bound_columns", 0), ("upper_bound_columns", 1)):
        tab, _ = C.contrast_tables(df, cfg, outcomes=list(BOUND_COLS), bootstrap=bootstrap,
                                   value_override={n: c[k] for n, c in BOUND_COLS.items()})
        tab.insert(0, "scenario", scen)
        res.append(tab)
    out = pd.concat(res, ignore_index=True)
    main = df[df.option.isin(cfg.options)]
    rows = []
    for name, (lcol, ucol) in BOUND_COLS.items():
        lo = C.instance_values(main, lcol)
        hi = C.instance_values(main, ucol)
        for layout in cfg.layouts:
            for a, b in cfg.contrasts:
                def eff(x, y):
                    mat, _, _ = C.paired_matrix(pd.concat([x[x.option == a], y[y.option == b]]), a, b, layout)
                    return float(np.mean(C.region_means(mat))) if mat.size else float("nan")
                rows.append({"outcome": name, "layout": layout, "contrast": f"{a}-{b}",
                             "extreme_lo": eff(lo, hi), "extreme_hi": eff(hi, lo)})
    return out, pd.DataFrame(rows)


IMPUTE_OUTCOMES = ["omission", "undisclosed", "event_f1", "false_alarm"]   # rates with a denominator that can be empty


def imputation_sensitivity(df: pd.DataFrame, cfg: Config, bootstrap: bool = True) -> pd.DataFrame:
    """Rate outcomes with every undefined instance imputed once as 1 and once as 0."""
    res = []
    for v in (1.0, 0.0):
        tab, _ = C.contrast_tables(df, cfg, outcomes=IMPUTE_OUTCOMES, bootstrap=bootstrap, impute=v)
        tab.insert(0, "imputed_as", v)
        res.append(tab)
    return pd.concat(res, ignore_index=True)


COVERAGE_OUTCOMES = ("coverage_baseline", "coverage_target")


def coverage_bounds_contrasts(df: pd.DataFrame, cfg: Config, bootstrap: bool = True) -> pd.DataFrame:
    """The sensitivity for coverage, in place of imputing 0 and 1 (science, #2, 08:42Z): the same contrasts on every run's
    lower extent, then on its upper bound."""
    res = []
    for extent in ("lower", "upper"):
        tab, _ = C.contrast_tables(df, cfg, outcomes=list(COVERAGE_OUTCOMES), bootstrap=bootstrap,
                                   value_override={o: f"{o}_{extent}" for o in COVERAGE_OUTCOMES})
        tab.insert(0, "extent", extent)
        res.append(tab)
    return pd.concat(res, ignore_index=True)


def coverage_null_share(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """How many runs each point estimate leaves out, per option x layout x model: dropping them is selection if the share
    differs by option. Why: `unknown` (the instrument lost track), not finished, or no extent recorded."""
    d = df[df.option.isin(cfg.options + [cfg["pagination"]["option"]])]
    rows = []
    for (opt, layout, model), g in d.groupby(["option", "layout", "model"], sort=True):
        for o in COVERAGE_OUTCOMES:
            null = g[g[o].isna()]
            # `unknown` here is the instrument's (it lost track): a run inspection resolved keeps a null point unless both coders' extractions
            # agree exactly (19(f)), and is counted with them
            inst = null["instrument_unknown"].astype(bool)
            rows.append({"outcome": o, "option": opt, "layout": layout, "model": model, "runs": len(g), "null": len(null),
                         "null_share": len(null) / len(g), "null_unknown": int(inst.sum()),
                         "null_not_finished": int((~inst & ~null["finished"].astype(bool)).sum())})
    return pd.DataFrame(rows, columns=["outcome", "option", "layout", "model", "runs", "null", "null_share", "null_unknown", "null_not_finished"])


def with_null_shares(tab: pd.DataFrame, df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Beside every coverage contrast: the share of each option's runs in that layout with no point estimate."""
    d = df[df.option.isin(cfg.options + [cfg["pagination"]["option"]])]
    share = {(o, opt, layout): float(g[o].isna().mean()) for o in COVERAGE_OUTCOMES for (opt, layout), g in d.groupby(["option", "layout"])}
    out = tab.copy()
    for side in ("a", "b"):
        out[f"null_share_{side}"] = [share.get((o, x, l), float("nan"))
                                     for o, x, l in zip(out["outcome"], out[f"option_{side}"], out["layout"])]
    return out


# ------------------------------------------------------------------ tokens, flagged usage
def tokens_flagged_sensitivity(df: pd.DataFrame, cfg: Config, bootstrap: bool = True) -> pd.DataFrame:
    """The tokens contrast (every option contrast x layout) three times (science, #34, 21:41Z and amendment 26(d)):
    - `primary`: a run cut mid-request counts at its completed requests' tokens; only a run cut in its first request (no tokens) is out;
    - `flagged_left_out`: every flagged run (`usage_complete` False) left out;
    - `flagged_at_upper_bound`: a flagged run counts at its completed tokens plus the bound on the request that was cut (input + output, `dataset_76.cutrequest`,
      one token per byte); a first-request cut stays out.
    `n_runs_left_out` is the flagged runs of the main options that the variant does not count."""
    main = df[df.option.isin(cfg.options)]
    flagged = main[main.usage_complete.astype(bool).eq(False)]
    out = []
    for variant, d, col in (("primary", df, "log2_tokens"), ("flagged_left_out", df[df.usage_complete.astype(bool)], "log2_tokens"),
                            ("flagged_at_upper_bound", df, "log2_tokens_upper")):
        tab, _ = C.contrast_tables(d, cfg, outcomes=["tokens"], bootstrap=bootstrap, value_override={"tokens": col})
        tab.insert(0, "variant", variant)
        tab["n_runs_left_out"] = len(flagged) if variant == "flagged_left_out" else int(flagged[col].isna().sum())
        out.append(tab)
    return pd.concat(out, ignore_index=True)


# ------------------------------------------------------------------ pagination
def pagination_rows(df: pd.DataFrame, cfg: Config, all_seeds: bool = False) -> pd.DataFrame:
    """The runs the pagination cell compares: condition_a, and T on the primary seed or on all of them, on its layout."""
    pg = cfg["pagination"]
    d = df[((df.option == pg["reference_option"]) & ((df.seed == pg["primary_seed"]) | all_seeds)) | (df.option == pg["option"])]
    return d[d.layout == pg["layout"]]


def pagination_coverage_bounds(df: pd.DataFrame, cfg: Config, all_seeds: bool = False, bootstrap: bool = True) -> pd.DataFrame:
    """The coverage sensitivity for the pagination cell, on its own seeds and pairing: condition_a minus T, on every run's
    lower extent, then on its upper bound."""
    pg = cfg["pagination"]
    d = pagination_rows(df, cfg, all_seeds)
    rng = np.random.default_rng(cfg["bootstrap"]["seed"])
    rows = []
    for extent in ("lower", "upper"):
        for name in COVERAGE_OUTCOMES:
            inst = C.instance_values(d, f"{name}_{extent}", defined_only=False)
            r, _ = C.contrast_one(inst, pg["option"], pg["reference_option"], pg["layout"], C.OUTCOME[name], cfg, bootstrap, rng, pair="instance")
            rows.append({"extent": extent, **r})
    # Holm within each extent, as the main coverage bounds do: the lower and the upper scenario are not one family
    tab = pd.concat([C._decorate(g.copy(), cfg) for _, g in pd.DataFrame(rows).groupby("extent", sort=False)], ignore_index=True)
    tab.insert(0, "T_seeds", "all" if all_seeds else f"seed {pg['primary_seed']}")
    return tab


def pagination_cell(df: pd.DataFrame, cfg: Config, all_seeds: bool = False, bootstrap: bool = True):
    """condition_a minus T on one layout, paired by (instance, model), aggregated by region.

    Primary: T restricted to `primary_seed`; sensitivity: all T seeds. The
    unpaginated option has one seed per cell, whatever its value.
    """
    pg = cfg["pagination"]
    ref, un, layout = pg["reference_option"], pg["option"], pg["layout"]
    d = pagination_rows(df, cfg, all_seeds)
    rng = np.random.default_rng(cfg["bootstrap"]["seed"])
    rows, pms = [], []
    for o in C.OUTCOMES:
        inst = C.instance_values(d, o.column, defined_only=False)
        r, pm = C.contrast_one(inst, un, ref, layout, o, cfg, bootstrap, rng, pair="instance")
        rows.append(r)
        pms.append(pm)
    tab = C._decorate(pd.DataFrame(rows), cfg)
    tab.insert(0, "T_seeds", "all" if all_seeds else f"seed {pg['primary_seed']}")
    pms = [p for p in pms if len(p)]
    return tab, (pd.concat(pms, ignore_index=True) if pms else pd.DataFrame())


# -------------------------------------------------- instrument-error sensitivity
def correct_omission(p_obs: np.ndarray, false_complete: float, false_omission: float) -> np.ndarray:
    """Invert p_obs = p (1 - fc) + (1 - p) fo (linear, unclipped)."""
    den = 1.0 - false_complete - false_omission
    if den <= 0:
        raise ValueError("false_complete + false_omission must be < 1")
    return (p_obs - false_omission) / den


def _placeholder_rates(rates: dict, cfg: Config) -> "audit_mod.Rates":
    """preregistered.toml rates are per option; they apply to every layout."""
    return {(o, l): (r["false_complete"], r["false_omission"]) for o, r in rates.items() for l in cfg.layouts}


def instrument_sensitivity(df: pd.DataFrame, cfg: Config, bootstrap: bool = True,
                           audit: "audit_mod.AuditRates | None" = None) -> pd.DataFrame:
    """Recompute the omission contrasts under each audited-rate scenario.

    The correction is applied per option x layout to the instance-level omission proportion before
    aggregation (it is linear, so this equals correcting any later average). Not clipped to [0, 1].
    Scenarios: the placeholders of `preregistered.toml` (`rates_source` "placeholder") and, when the
    audit's output file is given, the audited ones (`rates_source` "audit:<file>@<sha>"). The audit
    is a result of the study and enters here only; without it, no row claims audited rates.
    """
    out = []
    main = df[df.option.isin(cfg.options)].copy()
    scen = {n: (_placeholder_rates(r, cfg), "placeholder (preregistered.toml)")
            for n, r in cfg["instrument_sensitivity"]["scenarios"].items()}
    if audit is not None:
        scen.update({n: (r, audit.source) for n, r in audit.scenarios.items()})
    for name, (rates, source) in scen.items():
        inst = C.instance_values(main, "omit", defined_only=False)
        for (o, lay), (fc, fo) in rates.items():
            m = (inst.option == o) & (inst.layout == lay)
            inst.loc[m, "value"] = correct_omission(inst.loc[m, "value"].to_numpy(), fc, fo)
        rng = np.random.default_rng(cfg["bootstrap"]["seed"])
        rows = []
        for layout in cfg.layouts:
            for a, b in cfg.contrasts:
                row, _ = C.contrast_one(inst, a, b, layout, C.OUTCOME["omission"], cfg, bootstrap, rng)
                rows.append(row)
        t = C._decorate(pd.DataFrame(rows), cfg)
        t.insert(0, "scenario", name)
        t.insert(1, "rates_source", source)
        out.append(t)
    return pd.concat(out, ignore_index=True)


# ------------------------------------------------- exhausted runs (infrastructure)
EXHAUSTED_SCENARIO = "exhausted_as_K_and_failures"


def exhausted_sensitivity(df: pd.DataFrame, ex: pd.DataFrame, cfg: Config,
                          bootstrap: bool = True) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """The primary frame plus the exhausted runs of the primary scope (`infra.exhausted_frame`):
    time-capped (K) in the omission bounds, failures (0) in the end-to-end outcome. Returns the
    denominator table, the bounded-outcome contrasts and their extreme intervals, each tagged with
    the scenario. The primary tables are those of `df` alone."""
    aug = pd.concat([df, ex], ignore_index=True) if len(ex) else df
    dn = denominator_table(aug, cfg).drop(columns=["event_free_runs"])
    n_ex = ex.groupby(["option", "layout"]).size().rename("K_exhausted") if len(ex) else None
    dn = dn.merge(n_ex.reset_index(), on=["option", "layout"], how="left") if n_ex is not None else dn.assign(K_exhausted=0)
    dn["K_exhausted"] = dn["K_exhausted"].fillna(0).astype(int)
    bt, extreme = bound_tables(aug, cfg, bootstrap=bootstrap)
    return tuple(t.assign(sensitivity=EXHAUSTED_SCENARIO) for t in (dn, bt, extreme))   # type: ignore[return-value]


# ----------------------------------------------------------------- mixed models
def mixed_models(df: pd.DataFrame, cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Descriptive: value ~ option * layout + (1|region) + (1|model), on instance-level values.

    Crossed random intercepts via one all-encompassing group with variance
    components. Returns (fixed effects, variance components/diagnostics).
    """
    import statsmodels.formula.api as smf
    main = df[df.option.isin(cfg.options)]
    fixed, diag = [], []
    for o in C.OUTCOMES:
        if main["backend"].nunique() > 1:          # seeds on different backends stay separate observations
            inst = pd.concat([C.instance_values(g, o.column).assign(backend=bk) for bk, g in main.groupby("backend")],
                             ignore_index=True)
        else:
            inst = C.instance_values(main, o.column).assign(backend=main["backend"].iloc[0] if len(main) else "unknown")
        if len(inst) < 10:
            diag.append({"outcome": o.name, "n": len(inst), "converged": False, "note": "too few rows"})
            continue
        inst = inst.assign(grp=1, mdl=inst["model"], reg=inst["region_id"])
        vc = {"region": "0 + reg", "model": "0 + mdl"}
        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                fixed_part = cfg["mixed_models"]["formula_fixed"]
                if inst["backend"].nunique() > 1:          # pre-registered on #8: backend as a fixed effect
                    fixed_part += " + backend"
                fit = smf.mixedlm("value ~ " + fixed_part, inst, groups="grp", re_formula="0",
                                  vc_formula=vc).fit(reml=True)
            ci = fit.conf_int()
            fe = pd.DataFrame({"outcome": o.name, "term": fit.fe_params.index, "estimate": fit.fe_params.values,
                               "se": fit.bse_fe.values, "ci_lo": ci.loc[fit.fe_params.index, 0].values,
                               "ci_hi": ci.loc[fit.fe_params.index, 1].values})
            fixed.append(fe)
            d = {"outcome": o.name, "n": len(inst), "converged": bool(fit.converged),
                 "note": "; ".join(sorted({str(x.message)[:80] for x in w})),
                 "scale_residual": float(fit.scale)}
            for n, v in zip(fit.model.exog_vc.names, np.atleast_1d(fit.vcomp)):
                d[f"var_{n}"] = float(v)
            diag.append(d)
        except Exception as e:  # descriptive model: report failure, never hide it
            diag.append({"outcome": o.name, "n": len(inst), "converged": False, "note": f"{type(e).__name__}: {e}"[:200]})
    return (pd.concat(fixed, ignore_index=True) if fixed else pd.DataFrame()), pd.DataFrame(diag)


# ------------------------------------------------------- across-model pooled rows
def pooled_model_sensitivity(df: pd.DataFrame, cfg: Config, bootstrap: bool = True) -> pd.DataFrame:
    """Sensitivity: the option contrasts with the models pooled before pairing.

    Each arm's instance value is the mean over the models that define it, so a region defined in
    both arms at region level is kept even when no single model has both arms (the primary,
    within-model pairing drops it). Compare `n_regions` / `n_regions_dropped` with the primary
    table. Pre-registered answer on #14: the primary stays within-model.
    """
    main = df[df.option.isin(cfg.options)]
    rng = np.random.default_rng(cfg["bootstrap"]["seed"])
    rows = []
    for o in C.OUTCOMES:
        inst = C.instance_values(main, o.column, defined_only=False)
        inst = (inst.groupby(["option", "layout", "instance_id", "region_id"], as_index=False)["value"].mean()
                .assign(model="pooled"))
        for layout in cfg.layouts:
            for a, b in cfg.contrasts:
                r, _ = C.contrast_one(inst, a, b, layout, o, cfg, bootstrap, rng)
                rows.append(r)
    return C._decorate(pd.DataFrame(rows), cfg)


# --------------------------------------------------------------------- backend
def _cell_shares(df: pd.DataFrame, cfg: Config, column: str) -> pd.DataFrame:
    d = df[df.option.isin(cfg.options + [cfg["pagination"]["option"]])]
    n = d.groupby(["model", "option", "layout", column]).size().rename("runs").reset_index()
    n["share"] = n["runs"] / n.groupby(["model", "option", "layout"])["runs"].transform("sum")
    return n


def backend_shares(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Share of runs per backend in each (model, option, layout) cell (reporting only)."""
    return _cell_shares(df, cfg, "backend")


def slot_shares(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Share of runs per model slot in each cell, beside the backend's (science, #34, 2 Nov; reporting only)."""
    return _cell_shares(df, cfg, "slot")


def fingerprint_shares(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Share of runs per `system_fingerprint` in each cell, beside the backend's (science, #34, 2 Nov; reporting only)."""
    return _cell_shares(df, cfg, "system_fingerprint")


def assignment_by_option(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Slot and build assignment must be unrelated to option (science, #34, 2 Nov): for each (model, layout) and each slot,
    build or backend, its lowest and highest share over the options, a value an option never got counting as 0. Reported,
    not judged: how far apart is "noticeably" is the reader's call, and the shares behind it are in the tables above."""
    out = []
    for column in ("backend", "slot", "system_fingerprint"):
        t = _cell_shares(df, cfg, column)
        for (model, layout), g in t.groupby(["model", "layout"]):
            w = g.pivot(index=column, columns="option", values="share").fillna(0.0)
            for value, shares in w.iterrows():
                out.append({"model": model, "layout": layout, "dimension": column, "value": value, "options": w.shape[1],
                            "share_min": float(shares.min()), "share_max": float(shares.max()),
                            "spread": float(shares.max() - shares.min())})
    return pd.DataFrame(out, columns=["model", "layout", "dimension", "value", "options", "share_min", "share_max", "spread"])


def majority_backend(df: pd.DataFrame, cfg: Config) -> dict[str, str]:
    """Each model's most frequent backend over its main-option runs (ties: alphabetical first)."""
    d = df[df.option.isin(cfg.options)]
    c = d.groupby(["model", "backend"]).size().rename("n").reset_index().sort_values(["model", "n", "backend"], ascending=[True, False, True])
    return c.drop_duplicates("model").set_index("model")["backend"].to_dict()


def backend_sensitivity(df: pd.DataFrame, cfg: Config, bootstrap: bool = True) -> pd.DataFrame:
    """Primary option contrasts recomputed with every model restricted to its majority backend.

    Reports, per outcome x layout x contrast, the primary and restricted effects and whether the
    direction flipped or the smallest-effect status ('at_least_smallest') changed. Models on a
    single backend are unchanged, so every model goes through the same rule.
    """
    maj = majority_backend(df, cfg)
    keep = df["model"].map(maj) == df["backend"]
    prim, _ = C.contrast_tables(df, cfg, bootstrap=bootstrap)
    rest, _ = C.contrast_tables(df[keep], cfg, bootstrap=bootstrap)
    key = ["outcome", "layout", "contrast"]
    m = prim[key + ["effect", "at_least_smallest", "counts_for_guide"]].merge(
        rest[key + ["effect", "at_least_smallest", "counts_for_guide"]], on=key, suffixes=("_primary", "_majority"))
    both = m["effect_primary"].notna() & m["effect_majority"].notna()
    m["direction_changed"] = np.where(both, np.sign(m["effect_primary"]) != np.sign(m["effect_majority"]), np.nan)
    both_t = m["at_least_smallest_primary"].notna() & m["at_least_smallest_majority"].notna()
    m["threshold_crossed"] = np.where(both_t, m["at_least_smallest_primary"] != m["at_least_smallest_majority"], np.nan)   # NaN = not assessable
    main_ = df[df.option.isin(cfg.options)]

    def n_in_domain(d: pd.DataFrame, outcome: str) -> int:        # runs of the outcome's own instance domain
        col = C.OUTCOME[outcome].column
        return int(C.DOMAIN[col](d).sum()) if col in C.DOMAIN else int(len(d))
    majority_ = df[keep & df.option.isin(cfg.options)]
    m["n_runs_primary"] = m["outcome"].map(lambda o: n_in_domain(main_, o))
    m["n_runs_majority"] = m["outcome"].map(lambda o: n_in_domain(majority_, o))
    return m


# ------------------------------------------------------------------------ amendment 19
CELL = ["option", "layout", "model"]


def routed_decided_complete(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Science, #2, 2 Nov 21:47Z, beside the instrument-only row: per option x layout, the runs routed to inspection (19(a)) and those both coders decided
    `complete`. These are the instrument's false omissions by the coders' judgement, which shows whether that row can be read. Two shares: of all the routed runs, and
    of the inspected ones (decided either way, or left unknown by the coders; a run not inspected is in neither count of the second)."""
    d = df[df.option.isin(cfg.options + [cfg["pagination"]["option"]]) & df.routed_to_inspection.astype(bool)]    # the pagination cell is inspected too
    rows = []
    for (o, la), g in d.groupby(["option", "layout"], sort=True):
        src = g.label_source
        n, comp = len(g), int(((src == "inspection") & (g.label == "complete")).sum())
        seen = n - int((src == "not_inspected").sum())
        rows.append({"option": o, "layout": la, "routed": n, "decided_complete": comp, "decided_complete_share": comp / n,
                     "decided_complete_share_of_inspected": comp / seen if seen else float("nan"), "decided_omitting": int(((src == "inspection") & (g.label == "omitting")).sum()),
                     "left_unknown": int((src == "unresolved").sum()), "not_inspected": int((src == "not_inspected").sum())})
    return pd.DataFrame(rows, columns=["option", "layout", "routed", "decided_complete", "decided_complete_share", "decided_complete_share_of_inspected", "decided_omitting",
                                 "left_unknown", "not_inspected"])


def routed_share(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """19(a): per option x layout x model, the finished runs the instrument labelled `omitting` before routing, and the share routed to inspection
    because their only cause was loaded_not_used. Runs whose raw label is not on record are counted apart, never as not routed."""
    d = df[df.option.isin(cfg.options + [cfg["pagination"]["option"]]) & df.finished.astype(bool)]       # the pagination cell is inspected too
    rows = []
    for key, g in d.groupby(CELL, sort=True):
        raw_o = g[g.label_raw == "omitting"]
        routed = int(raw_o.routed_to_inspection.astype(bool).sum())
        rows.append({**dict(zip(CELL, key)), "finished": len(g), "raw_omitting": len(raw_o), "routed": routed,
                     "routed_share": routed / len(raw_o) if len(raw_o) else float("nan"), "raw_label_not_recorded": int(g.label_raw.isna().sum()),
                     "raw_omitting_unknown_not_by_rule": int(g.raw_omitting_unknown_not_by_rule.astype(bool).sum())})
    return pd.DataFrame(rows, columns=[*CELL, "finished", "raw_omitting", "routed", "routed_share", "raw_label_not_recorded",
                                       "raw_omitting_unknown_not_by_rule"])


def _side_by_side(prim_df: pd.DataFrame, alt_df: pd.DataFrame, cfg: Config, name: str, bootstrap: bool) -> pd.DataFrame:
    prim, _ = C.contrast_tables(prim_df, cfg, bootstrap=bootstrap)
    alt, _ = C.contrast_tables(alt_df, cfg, bootstrap=bootstrap)
    key = ["outcome", "layout", "contrast"]
    m = prim[key + ["effect", "at_least_smallest"]].merge(alt[key + ["effect", "at_least_smallest"]], on=key, suffixes=("_primary", f"_{name}"))
    both = m["effect_primary"].notna() & m[f"effect_{name}"].notna()
    m["direction_changed"] = np.where(both, np.sign(m["effect_primary"]) != np.sign(m[f"effect_{name}"]), np.nan)
    both_t = m["at_least_smallest_primary"].notna() & m[f"at_least_smallest_{name}"].notna()
    m["threshold_crossed"] = np.where(both_t, m["at_least_smallest_primary"] != m[f"at_least_smallest_{name}"], np.nan)
    return m


def seed_one_balanced(df: pd.DataFrame, cfg: Config, bootstrap: bool = True) -> pd.DataFrame:
    """Amendment 29(c): the balanced seed-1-only row beside all of a part's cohort: the runs of seed 1 whose (instance, layout, model) has a row for every option,
    against all the runs of the part. `n_balanced_units` says how many such units there are, per layout."""
    d = df[df.option.isin(cfg.options) & (df.seed == 1)]
    unit = ["instance_id", "layout", "model"]
    want = set(cfg.options)
    full = d.groupby(unit)["option"].agg(lambda s: want <= set(s)).rename("_full").reset_index()
    kept = d.merge(full[full._full][unit], on=unit)
    m = _side_by_side(df, kept, cfg, "seed_one_balanced", bootstrap)
    n = full[full._full].groupby("layout").size().to_dict()
    return m.assign(n_balanced_units=m["layout"].map(n).fillna(0).astype(int))


def instrument_only_sensitivity(rows, cfg: Config, bootstrap: bool = True) -> pd.DataFrame:
    """19(a): the primary contrasts with every run's label the instrument's own, before routing to inspection. If a finished primary run has no raw
    label on record, the comparison would be over another set of runs, so nothing is computed and the count says why."""
    import dataclasses
    from . import adapter
    kept, _ = adapter.select_rows(rows, cfg)
    missing = sum(1 for r in kept if r.finished and r.label_raw is None)
    if missing:
        return pd.DataFrame([{"status": "not computed", "why": f"{missing} finished primary run(s) have no raw label on record"}])
    prim = adapter.rows_to_frame(kept, cfg)
    # nothing from inspection: the raw label, no label after inspection, and no coverage point taken from the coders. The raw label is
    # the label here, so there is no routing to check against it (label_raw=None: the routed columns are not read in this frame)
    raw = adapter.rows_to_frame([dataclasses.replace(r, label=r.label_raw, label_raw=None, label_after_inspection=None,
                                                     coder_pair=None,
                                                     **({"coverage_baseline": None, "coverage_target": None, "coverage_source": None}
                                                        if r.coverage_source == "inspection" else {})) for r in kept], cfg)
    return _side_by_side(prim, raw, cfg, "instrument_only", bootstrap).assign(status="computed")


def sol_only_sensitivity(raw_rows, inspection, cfg: Config, bootstrap: bool = True) -> pd.DataFrame:
    """Amendment 26(j): the primary contrasts with the inspection results as given, beside the same contrasts with only the Sol-coded runs' results applied
    (`inspection.sol_only`). A run the fallback pair coded (or whose record names no pair) is then not inspected, so its instrument `unknown` stands.
    `raw_rows`: the scored runs BEFORE inspection was applied (a row that already carries a label after inspection or a pair is refused: this function
    applies the inspection itself, twice). With no inspection run, or no Sol-coded run among the label records, nothing is computed and the row says why."""
    raw_rows = list(raw_rows)
    if inspection is None or not INS.coerce(inspection).ran:
        return pd.DataFrame([{"status": "not computed", "why": "no inspection input: inspection has not run"}])
    ins = INS.coerce(inspection)
    sol = INS.sol_only(ins)
    if not sol.labels:
        return pd.DataFrame([{"status": "not computed",
                              "why": f"none of the {len(ins.labels)} inspection label record(s) names a Sol-coded pair"}])
    if any(r.label_after_inspection is not None or r.coder_pair is not None for r in raw_rows):
        raise ValueError("sol_only_sensitivity takes the rows before inspection was applied: a row already carries a label after inspection or a pair")
    from . import adapter
    prim = adapter.rows_to_frame(INS.apply(raw_rows, ins), cfg)
    alt = adapter.rows_to_frame(INS.apply(raw_rows, sol), cfg)
    return _side_by_side(prim, alt, cfg, "sol_only", bootstrap).assign(status="computed")


def tracking_budget_sensitivity(df: pd.DataFrame, cfg: Config, bootstrap: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """19(b): the primary contrasts without the runs the tracking budget stopped and the time-capped runs whose tracking work exceeded half the
    budget, and per option x layout x model how many were left out (a capped run with no work on record is out, and counted apart)."""
    keep = ~df["budget_out"].astype(bool)
    m = _side_by_side(df, df[keep], cfg, "budget_excluded", bootstrap)
    d = df[df.option.isin(cfg.options)]
    counts = d.groupby(CELL, sort=True).agg(runs=("run_key", "size"), excluded=("budget_out", "sum"),
                                            work_not_recorded=("budget_unknown", "sum")).reset_index()
    return m, counts


# ------------------------------------------------------------------------ H4: directions across models (descriptive)
# parameters.md, block 2, tier A: the cheaper/stronger tier pair of H4 (reported names, config/models.toml). Not a registered constant.
H4_TIER_PAIR = ("model-12", "model-11")
# bytes between sites (19(g)) on the log2 scale, like tokens: a per-model effect is a mean log2 ratio. Descriptive only: never tested.
H4_BYTES = C.Outcome("bytes_between_sites", "log2_bytes", False, "log2", None)
_KEY = ["outcome", "layout", "contrast"]
_LOG2 = {"tokens", "bytes_between_sites"}


def h4_directions(tab: pd.DataFrame, pm: pd.DataFrame, df: pd.DataFrame, cfg: Config, bootstrap: bool = True,
                  tier_pair: tuple[str, str] = H4_TIER_PAIR) -> tuple[pd.DataFrame, pd.DataFrame]:
    """H4 (science, #14, 19:11Z): one descriptive table, no test. `tab`, `pm`: the primary contrast tables (`contrasts.contrast_tables`).

    Per model: each contrast's effect with its region-bootstrap CI (the primary contrasts' own resample), for every contrast outcome and for
    bytes between sites, per layout, and whether its sign agrees with the pooled (equal-model) direction. Per contrast: the pooled effect and
    CI, the models with an effect, the models agreeing in sign (signs on the 12-decimal grid; a pooled effect of 0 has no direction, so nothing
    agrees), and the cheaper and the stronger tier side by side (NaN, with the reason, when a tier has no effect in the data)."""
    main = df[df.option.isin(cfg.options)]
    inst = C.instance_values(main, H4_BYTES.column, defined_only=False)
    rng = np.random.default_rng(cfg["bootstrap"]["seed"])
    rows, pms = [], [pm]
    for layout in cfg.layouts:
        for a, b in cfg.contrasts:
            r, p = C.contrast_one(inst, a, b, layout, H4_BYTES, cfg, bootstrap, rng)
            rows.append(r)
            pms.append(p)
    cols = [*_KEY, "effect", "ci_lo", "ci_hi"]
    pooled = pd.concat([tab.reindex(columns=cols), pd.DataFrame(rows).reindex(columns=cols)], ignore_index=True)
    per = pd.concat([p for p in pms if len(p)], ignore_index=True) if any(len(p) for p in pms) else pd.DataFrame()
    per = per.reindex(columns=[*_KEY, "model", "effect", "ci_lo", "ci_hi"])
    per = per.merge(pooled[[*_KEY, "effect"]].rename(columns={"effect": "pooled_effect"}), on=_KEY, how="left")
    sp, sm = np.sign(C.snap(per["pooled_effect"], cfg)), np.sign(C.snap(per["effect"], cfg))
    per["agrees_with_pooled"] = np.where(per["effect"].isna() | per["pooled_effect"].isna() | (sp == 0), np.nan, (sm == sp).astype(float))
    per["tier"] = per["model"].map({tier_pair[0]: "cheaper", tier_pair[1]: "stronger"})
    per["_order"] = per["model"].map({tier_pair[0]: 0, tier_pair[1]: 1}).fillna(2)
    per = per.sort_values([*_KEY, "_order", "model"], kind="stable").drop(columns="_order").reset_index(drop=True)
    out = []
    for _, r in pooled.iterrows():
        g = per[(per.outcome == r.outcome) & (per.layout == r.layout) & (per.contrast == r.contrast)]
        rec = {"outcome": r.outcome, "layout": r.layout, "contrast": r.contrast, "pooled_effect": r.effect, "pooled_ci_lo": r.ci_lo,
               "pooled_ci_hi": r.ci_hi, "pooled_direction": float(np.sign(C.snap(r.effect, cfg))) if pd.notna(r.effect) else np.nan,
               "n_models": int(g["effect"].notna().sum()), "n_models_agreeing": int((g["agrees_with_pooled"] == 1.0).sum())}
        for tier, name in zip(("cheaper", "stronger"), tier_pair):
            t = g[g.model == name]
            rec[f"{tier}_model"] = name
            for c in ("effect", "ci_lo", "ci_hi", "agrees_with_pooled"):
                rec[f"{tier}_{c}"] = float(t[c].iloc[0]) if len(t) and pd.notna(t[c].iloc[0]) else np.nan
            rec[f"{tier}_note"] = None if len(t) and pd.notna(t["effect"].iloc[0]) else "no effect for this model in the data"
        out.append(rec)
    summ = pd.DataFrame(out)
    for t in (per, summ):
        e = "effect" if "effect" in t else "pooled_effect"
        is_log2 = t["outcome"].isin(_LOG2)
        t[f"{e}_ratio"] = np.where(is_log2, 2.0 ** t[e], np.nan)        # tokens and bytes: the geometric-mean ratio
    return per, summ


# ------------------------------------------------------------------------ power
def power_sim(sd: float, n: int, delta: float, alpha: float, n_sim: int, rng: np.random.Generator,
              cfg: Config | None = None) -> float:
    """Power of the paired Wilcoxon signed-rank test, by simulation.

    Region-level paired differences ~ N(delta, sd^2), n regions.
    """
    method = cfg["inference"]["wilcoxon_method"] if cfg else "auto"
    zero = cfg["inference"]["wilcoxon_zero_method"] if cfg else "wilcox"
    hits = 0
    x = rng.normal(delta, sd, size=(n_sim, n))
    for row in x:
        if stats.wilcoxon(row, zero_method=zero, method=method).pvalue < alpha:
            hits += 1
    return hits / n_sim


def pilot_region_sd(df: pd.DataFrame, cfg: Config, a: str, b: str, layout: str, outcome: str = "omission") -> float:
    """Between-region SD of the paired option difference, from (pilot) rows."""
    o = C.OUTCOME[outcome]
    mat, regions, _ = C.paired_matrix(C.instance_values(df, o.column), a, b, layout)
    if len(regions) < 2:
        return float("nan")
    return float(np.nanstd(np.nanmean(mat, axis=1), ddof=1))


def pilot_power(pilot: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Power re-estimate from the pilot block: its between-region SD of the paired difference, per contrast.

    `pilot` is `adapter.pilot_frame(...)`: pilot-block runs only, the sole use of those runs. Each
    contrast's SD feeds `power_table` (target regions, target difference); a contrast the pilot
    cannot estimate (fewer than 2 regions) keeps its row with NaN SD and power, so the gap shows.
    """
    pp = cfg["pilot_power"]
    rows = []
    for a, b in cfg.contrasts:
        mat, regions, _ = C.paired_matrix(C.instance_values(pilot, C.OUTCOME[pp["outcome"]].column), a, b, pp["layout"])
        sd = pilot_region_sd(pilot, cfg, a, b, pp["layout"], pp["outcome"])
        t = power_table(cfg, [sd]) if not np.isnan(sd) else power_table(cfg, [float("nan")], simulate=False)
        t.insert(0, "contrast", f"{a}-{b}")
        t.insert(1, "pilot_regions", len(regions))
        rows.append(t)
    return pd.concat(rows, ignore_index=True)


def power_table(cfg: Config, sds: list[float] | None = None, simulate: bool = True) -> pd.DataFrame:
    p = cfg["power"]
    rng = np.random.default_rng(p["seed"])
    n_c = len(cfg.contrasts)
    rows = []
    for sd in (sds or p["sd_grid"]):
        for label, alpha in (("alpha", cfg.alpha), ("alpha_over_contrasts", cfg.alpha / n_c)):
            rows.append({"sd": sd, "n_regions": p["target_regions"], "delta": p["target_difference"],
                         "alpha_level": label, "alpha": alpha,
                         "power": power_sim(sd, p["target_regions"], p["target_difference"], alpha, p["n_simulations"], rng, cfg)
                         if simulate else float("nan")})
    return pd.DataFrame(rows)
