"""Row and contrast builders shared by the statistics tests; every name here is invented for the tests."""

from expcore.stats.pairing import ContrastSpec
from expcore.stats.ratio import RatioForm
from expcore.stats.rows import Row


def make_row(task, model, seed, score, *, condition="cond-a", cluster="grp-a", outcome="score", **declared):
    """Build a row as the export writes one: a run key, a condition, every factor, one outcome, the task's cluster."""
    factors = {"task": task, "model": model, "seed": seed, **declared}
    run_key = "|".join(f"{name}={value}" for name, value in [*factors.items(), ("condition", condition)])
    return Row(run_key, condition, factors, {outcome: score}, {"group": cluster})


def named(name="a-vs-b", a="cond-a", b="cond-b", *, outcome="score", estimator="wilcoxon_cluster_bootstrap"):
    """A named-condition contrast clustered by `group`."""
    return ContrastSpec(name, outcome, estimator, "group", conditions=(a, b))


def by_factor(name="gate-effect", factor="gate", levels=("open", "shut"), *, outcome="score"):
    """A factor contrast clustered by `group`."""
    return ContrastSpec(name, outcome, "wilcoxon_cluster_bootstrap", "group", factor=factor, levels=levels)


def both_arms(task, model, seed, a, b, *, cluster="grp-a", **kwargs):
    """Two rows for one pair: condition cond-a with score `a`, condition cond-b with score `b`."""
    return [
        make_row(task, model, seed, a, condition="cond-a", cluster=cluster, **kwargs),
        make_row(task, model, seed, b, condition="cond-b", cluster=cluster, **kwargs),
    ]


def ratio_row(task, model, seed, spend, wins, *, condition="cond-a", cluster="grp-a", treatment=False, extra=None):
    """A row with the two outcomes of a ratio contrast: `spend` (the numerator) and `wins` (the denominator), and any
    `extra` outcomes, such as a second numerator column."""
    factors = {"task": task, "model": model, "seed": seed}
    run_key = "|".join(f"{name}={value}" for name, value in [*factors.items(), ("condition", condition)])
    outcomes = {"spend": spend, "wins": wins, **(extra or {})}
    return Row(run_key, condition, factors, outcomes, {"group": cluster}, treatment)


def ratio_arms(task, model, seed, a, b, *, cluster="grp-a"):
    """Two rows for one pair of a ratio contrast: `a` and `b` are each arm's (spend, wins)."""
    return [
        ratio_row(task, model, seed, *a, condition="cond-a", cluster=cluster),
        ratio_row(task, model, seed, *b, condition="cond-b", cluster=cluster),
    ]


def ratio_spec(name="spend-per-win", *, zero="exclude", correction=None, estimator="ratio", **declared):
    """A ratio contrast of spend over wins between cond-a and cond-b, clustered by `group` (with `cluster_key =
    ("model", "group")` among `declared`, by each model's cell of a group)."""
    form = RatioForm("spend", "wins", zero, correction)
    return ContrastSpec(name, form.label, estimator, "group", conditions=("cond-a", "cond-b"), ratio=form, **declared)
