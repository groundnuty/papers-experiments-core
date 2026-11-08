"""Row and contrast builders shared by the statistics tests; every name here is invented for the tests."""

from expcore.stats.pairing import ContrastSpec
from expcore.stats.rows import Row


def make_row(task, model, seed, score, *, condition=None, cluster="grp-a", outcome="score", **factors):
    """Build a row with one outcome, the task's cluster under the attribute `group`, and any declared factors."""
    return Row(
        task=task,
        model=model,
        seed=seed,
        outcomes={outcome: score},
        clusters={"group": cluster},
        factors=dict(factors),
        condition=condition,
    )


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
