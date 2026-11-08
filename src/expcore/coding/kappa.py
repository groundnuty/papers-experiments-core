# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/causes/kappa.py, blob f1c7bc4f26220c8d1265804d890e790dfcb63b81
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Cohen's kappa between two coders on a primary code, with a seeded percentile bootstrap over runs.

Kappa reports when it is undefined (S07): NaN when the expected agreement is 1, which is when both coders used one
single code throughout; a bootstrap interval is NaN when fewer than half the replicates are defined; and a coding
sheet with a blank, a duplicate or a missing id is refused, because a kappa computed on the rows that happened to
match would not be the kappa of the sample.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence

import numpy as np

from expcore.stats.contrasts import percentile_interval


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> dict:
    """kappa = (po - pe) / (1 - pe), with the agreement po, the expected agreement pe and the run count n.

    Undefined (NaN) when pe == 1: both coders used one single code throughout, so there is no disagreement to
    correct for. With no run at all, everything but n is NaN.
    """
    if len(a) != len(b):
        raise ValueError("coders rated different numbers of runs")
    n = len(a)
    if n == 0:
        return {"kappa": math.nan, "po": math.nan, "pe": math.nan, "n": 0}
    count_a, count_b = Counter(a), Counter(b)
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pe = sum(count_a[c] * count_b[c] for c in set(count_a) | set(count_b)) / (n * n)
    kappa = math.nan if pe >= 1.0 else (po - pe) / (1 - pe)
    return {"kappa": kappa, "po": po, "pe": pe, "n": n}


def kappa_ci(a: Sequence[str], b: Sequence[str], *, n_boot: int = 2000, alpha: float = 0.05, seed: int = 0) -> dict:
    """Point estimate plus a percentile bootstrap interval over runs.

    Replicates where kappa is undefined are left out and counted (`n_boot_valid`); the interval is NaN if fewer than
    half are defined, by the same rule as every bootstrap interval in the core. One run is too few to resample.
    """
    base = cohen_kappa(a, b)
    n = base["n"]
    draws = np.full(n_boot, math.nan)
    if n >= 2:
        rng = np.random.default_rng(seed)
        first, second = np.asarray(a, dtype=object), np.asarray(b, dtype=object)
        for replicate in range(n_boot):
            picked = rng.integers(0, n, n)
            draws[replicate] = cohen_kappa(list(first[picked]), list(second[picked]))["kappa"]
    low, high, valid = percentile_interval(draws, 1 - alpha)
    return {**base, "ci_low": low, "ci_high": high, "n_boot_valid": valid, "alpha": alpha}


def _check_ids(ids: Sequence[str], what: str) -> None:
    """Refuse a blank or repeated id."""
    if any(not isinstance(i, str) or not i.strip() for i in ids):
        raise ValueError(f"{what} has a blank id")
    if len(set(ids)) != len(ids):
        raise ValueError(f"{what} has a duplicate id")


def aligned_codes(
    sample_ids: Sequence[str], first: Mapping[str, str], second: Iterable[tuple[str, str | None]]
) -> tuple[list[str], list[str]]:
    """Return the two coders' codes for the sampled ids, in sample order.

    `first` maps an id to the first coder's code; `second` is the filled sheet as (id, code) pairs, so a repeated id
    can be seen. A blank code or id, a duplicate id, an id of the sample missing from the filled sheet or without a
    first-coder code, and an id on the sheet that is not in the sample all raise ValueError (S07).
    """
    sheet = list(second)
    _check_ids(list(sample_ids), "the sample")
    _check_ids([i for i, _ in sheet], "the filled sheet")
    blank = sum(1 for _, code in sheet if code is None or not str(code).strip())
    if blank:
        raise ValueError(f"{blank} sheet rows have no primary code")
    filled = {i: str(code).strip() for i, code in sheet}
    if missing := set(sample_ids) - set(filled):
        raise ValueError(f"{len(missing)} sampled runs missing from the filled sheet")
    if extra := set(filled) - set(sample_ids):
        raise ValueError(f"{len(extra)} runs on the filled sheet are not in the sample")
    if gone := [i for i in sample_ids if i not in first or not str(first[i]).strip()]:
        raise ValueError(f"{len(gone)} sampled runs without a first-coder code")
    return [str(first[i]).strip() for i in sample_ids], [filled[i] for i in sample_ids]


def kappa_report(
    sample_ids: Sequence[str],
    first: Mapping[str, str],
    second: Iterable[tuple[str, str | None]],
    *,
    groups: Mapping[str, str] | None = None,
    n_boot: int = 2000,
    seed: int = 0,
) -> list[dict]:
    """Kappa with its interval for all sampled runs and, when `groups` maps each id to a label, for each label.

    Rows come back as dicts with a `scope` ("all", then each label in sorted order) and the fields of `kappa_ci`.
    The input is checked as in `aligned_codes`.
    """
    a, b = aligned_codes(sample_ids, first, second)
    rows = [{"scope": "all", **kappa_ci(a, b, n_boot=n_boot, seed=seed)}]
    if groups is not None:
        if unlabelled := [i for i in sample_ids if i not in groups]:
            raise ValueError(f"{len(unlabelled)} sampled runs have no group label")
        for label in sorted({groups[i] for i in sample_ids}):
            picked = [k for k, i in enumerate(sample_ids) if groups[i] == label]
            rows.append(
                {
                    "scope": label,
                    **kappa_ci([a[k] for k in picked], [b[k] for k in picked], n_boot=n_boot, seed=seed),
                }
            )
    return rows
