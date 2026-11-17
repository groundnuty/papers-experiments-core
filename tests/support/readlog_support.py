"""Batches for the tests of the read-log scan's analysis and report: pairs of runs under one preregistration, each
run's record carrying the code cells it ran and, unless a test says otherwise, the `read_log` instrument."""

import tomllib

from egress_logs import record
from expcore.readlog.scan import NAMESPACE
from readlog_cells import FORGING
from stats_support import both_arms

from expcore.campaign.factors import FactorSchema
from expcore.stats.prereg import parse

SCHEMA = FactorSchema({"arm": ["a", "b"]}, ["task", "arm", "model", "seed"], {f"cond-{a}": {"arm": a} for a in "ab"})
PREREG = """\
alpha = 0.05
holm_family = ["a-vs-b"]

[bootstrap]
replicates = 200
seed = 5

[contrasts.a-vs-b]
conditions = ["cond-a", "cond-b"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
"""
FORGER = FORGING["socket path and datagram socket"][0]
ON = {NAMESPACE: {"side_sha256": "a" * 64}}  # a record that carries the instrument (its snapshot is the reader's)


def preregistration():
    return parse(tomllib.loads(PREREG), "inline", "a" * 64, SCHEMA)


def rows_of_pairs(n: int) -> list:
    """`n` pairs in `n` clusters, cond-a ahead of cond-b by 0.2; a pair is two rows."""
    rows = []
    for i in range(n):
        rows += both_arms(f"t{i}", "m", 1, 0.7 + 0.01 * i, 0.5 + 0.01 * i, cluster=f"g{i}", arm="a")
    for row in rows:  # the factor schema's arm follows the condition
        row.factors["arm"] = row.condition[-1]
    return rows


def runs_for(rows, cells_of, instruments=ON) -> list:
    """Each row's record with the code cells `cells_of(i)` gives it, paired with the row's condition."""
    return [
        (record(run_key=row.run_key, cells=cells_of(i), instruments=dict(instruments)), row.condition)
        for i, row in enumerate(rows)
    ]
