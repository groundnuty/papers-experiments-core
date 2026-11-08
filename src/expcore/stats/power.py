# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/analysis/secondary.py, blob 142c57b3fcb519af1e7d90e24f13bf4bf1364365
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Power by simulation: the power functions of the imported secondary analyses (#14)."""
from __future__ import annotations

import numpy as np
import pandas as pd
from dataset_76.analysis import contrasts as C
from dataset_76.analysis.config import Config
from scipy import stats


# ------------------------------------------------------------------------ power
def power_sim(
    sd: float, n: int, delta: float, alpha: float, n_sim: int, rng: np.random.Generator, cfg: Config | None = None
) -> float:
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
    for sd in sds or p["sd_grid"]:
        for label, alpha in (("alpha", cfg.alpha), ("alpha_over_contrasts", cfg.alpha / n_c)):
            rows.append(
                {
                    "sd": sd,
                    "n_regions": p["target_regions"],
                    "delta": p["target_difference"],
                    "alpha_level": label,
                    "alpha": alpha,
                    "power": power_sim(
                        sd, p["target_regions"], p["target_difference"], alpha, p["n_simulations"], rng, cfg
                    )
                    if simulate
                    else float("nan"),
                }
            )
    return pd.DataFrame(rows)
