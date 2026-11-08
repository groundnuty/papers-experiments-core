# Provenance: new
"""The one comparison that decides a rejection: the p-value is at most the level (S06).

The analysis, Holm's procedure and the power simulation all decide a rejection through `rejects`, so a p-value that
sits exactly on its level is rejected in every one of them, and a change of the rule changes all of them together.
This file imports nothing, so the power simulator can share the rule with the pipeline it checks without importing
any of it (S09).
"""

from __future__ import annotations


def rejects(p_value, level):
    """Whether a p-value rejects at a level: `p_value <= level`, and False for a NaN p-value.

    Works on a number and on a numpy array of p-values alike. The boundary is inclusive, as declared: a p-value that
    equals its level is a rejection. Compute the level with `contrasts.level` so that the p-value and the level are
    the same expression and compare equal exactly.
    """
    return p_value <= level
