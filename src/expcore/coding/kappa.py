# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/causes/kappa.py, blob f1c7bc4f26220c8d1265804d890e790dfcb63b81
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Cohen's kappa between the rule coder and the second coder, on the primary code, with a
seeded percentile bootstrap CI over runs, overall and per option."""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

import numpy as np
import pandas as pd


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> dict:
    """kappa = (po - pe) / (1 - pe). Undefined (NaN) when pe == 1: both coders used one
    single code throughout, so there is no disagreement to correct for."""
    if len(a) != len(b):
        raise ValueError("coders rated different numbers of runs")
    n = len(a)
    if n == 0:
        return {"kappa": math.nan, "po": math.nan, "pe": math.nan, "n": 0}
    ca, cb_ = Counter(a), Counter(b)
    po = sum(x == y for x, y in zip(a, b)) / n
    pe = sum(ca[c] * cb_[c] for c in set(ca) | set(cb_)) / (n * n)
    kappa = math.nan if pe >= 1.0 else (po - pe) / (1 - pe)
    return {"kappa": kappa, "po": po, "pe": pe, "n": n}


def kappa_ci(a: Sequence[str], b: Sequence[str], *, n_boot: int = 2000, alpha: float = 0.05,
             seed: int = 0) -> dict:
    """Point estimate plus percentile bootstrap CI. Replicates where kappa is undefined are
    dropped and counted (`n_boot_valid`); the CI is NaN if fewer than half are defined."""
    base = cohen_kappa(a, b)
    n = base["n"]
    ks = []
    if n >= 2:
        rng = np.random.default_rng(seed)
        a_, b_ = np.asarray(a, dtype=object), np.asarray(b, dtype=object)
        for _ in range(n_boot):
            i = rng.integers(0, n, n)
            k = cohen_kappa(list(a_[i]), list(b_[i]))["kappa"]
            if not math.isnan(k):
                ks.append(k)
    ok = len(ks) >= n_boot / 2
    lo, hi = (np.percentile(ks, [100 * alpha / 2, 100 * (1 - alpha / 2)]) if ok else (math.nan, math.nan))
    return {**base, "ci_low": float(lo), "ci_high": float(hi), "n_boot_valid": len(ks), "alpha": alpha}


def kappa_report(key: pd.DataFrame, first: pd.DataFrame, second: pd.DataFrame, *, n_boot: int = 2000,
                 seed: int = 0) -> pd.DataFrame:
    """`key`: sheet_id, block, run_key, option. `first`: the rule coder's table (block, run_key,
    cause). `second`: the filled sheet (sheet_id, coder_primary). One row for all runs and one
    per option. Sheet ids missing from the filled sheet, or left blank, are an error."""
    s = second[["sheet_id", "coder_primary"]].copy()
    s["coder_primary"] = s["coder_primary"].fillna("").astype(str).str.strip()
    if (s["coder_primary"] == "").any():
        raise ValueError(f"{int((s['coder_primary'] == '').sum())} sheet rows have no primary code")
    if s["sheet_id"].duplicated().any():
        raise ValueError("duplicate sheet_id in the filled sheet")
    missing = set(key["sheet_id"]) - set(s["sheet_id"])
    if missing:
        raise ValueError(f"{len(missing)} sampled runs missing from the filled sheet")
    m = key.merge(first[["block", "run_key", "cause"]], on=["block", "run_key"], how="left", validate="1:1") \
        .merge(s, on="sheet_id", how="inner", validate="1:1")
    if m["cause"].isna().any():
        raise ValueError("sampled runs without a rule-coder cause")
    rows = []
    for name, g in [("all", m)] + [(o, g) for o, g in m.groupby("option")]:
        r = kappa_ci(list(g["cause"]), list(g["coder_primary"]), n_boot=n_boot, seed=seed)
        rows.append({"scope": name, **r})
    return pd.DataFrame(rows)
