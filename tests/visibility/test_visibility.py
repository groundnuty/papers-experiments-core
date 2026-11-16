"""O56: the process-visibility instrument and its verdicts, from the record alone: a run whose code hid a process or its
memory from the harness's watch is flagged, one whose memory reading is unknown is unknown (its limit condition
unknown, never within its limit), and the analysis is run again without every run that is not clean."""

import tomllib
from types import SimpleNamespace

import pytest
from stats_support import both_arms

from expcore.campaign.factors import FactorSchema
from expcore.stats.analysis import analyse
from expcore.stats.prereg import parse
from expcore.visibility import NAMESPACE, ProcessVisibility, judge, snapshot, verdict_of
from expcore.visibility.instrument import readable
from expcore.visibility.sensitivity import LEFT_OUT_AS, visibility_analysis

FIGURES = {"margin_bytes": 134217728, "start_unexplained_bytes": 0, "unexplained_peak_bytes": 0,
           "cgroup_anon_bytes": 400000000, "visible_anon_bytes": 400000000}  # fmt: skip
SCHEMA = FactorSchema({"arm": ["a", "b"]}, ["task", "arm", "model", "seed"], {f"cond-{a}": {"arm": a} for a in "ab"})
TEXT = """\
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


def reading(memory="within", unknown=(), hidden_process=0, hidden_memory=0, **figures):
    return {"version": 1, "memory": memory, "unknown": list(unknown), "hidden_process": hidden_process,
            "hidden_memory": hidden_memory, "figures": {**FIGURES, **figures}}  # fmt: skip


class Source:
    def __init__(self, data):
        self.data = data

    def process_visibility(self):
        return self.data


def watched(data):
    return {NAMESPACE: ProcessVisibility(Source(data)).finish()}


def record(*, instruments=None, run_key="run-1", attempt=1):
    return SimpleNamespace(run_key=run_key, attempt=attempt, instruments=dict(instruments or {}))


# ----------------------------------------------------------------------------- the instrument
def test_o56_a_reading_within_is_kept_as_it_is():
    assert snapshot(reading()) == reading()
    assert ProcessVisibility(Source(reading())).namespace == "process_visibility"


def test_o56_no_reading_is_unknown_never_within():
    """O56: a sandbox with no watch reading (none kept, or never asked) gives an unknown snapshot."""
    found = snapshot(None)
    assert (found["memory"], found["unknown"]) == ("unknown", ["not_watched"])
    assert set(found["figures"].values()) == {None}


@pytest.mark.parametrize(
    "bad",
    [
        reading(memory="fine"),
        reading(memory="unknown"),  # unknown without a reason
        reading(unknown=["hidden_process"]),  # within with a reason
        reading(hidden_process=1),  # within with a hidden process
        reading(hidden_memory=-1, memory="over"),
        {**reading(), "extra": 1},
        {**reading(), "figures": {"margin_bytes": 1}},
        reading(margin_bytes="128MiB"),
        reading(memory="unknown", unknown=[""]),
        "within",
    ],
)
def test_o56_a_reading_this_code_does_not_read_is_unknown(bad):
    found = snapshot(bad)
    assert (found["memory"], found["unknown"]) == ("unknown", ["not_a_reading"])
    assert readable(found) is not None


# ----------------------------------------------------------------------------- the verdict
def test_o56_a_run_within_its_limit_with_nothing_hidden_is_clean():
    run = judge(record(instruments=watched(reading())), "cond-a")
    assert (run.verdict, run.flags, run.memory) == ("clean", (), "within")
    assert not run.left_out


def test_o56_a_hidden_process_is_flagged_and_its_unknown_memory_said_too():
    run = judge(record(instruments=watched(reading("unknown", ["hidden_process"], hidden_process=2))), "cond-a")
    assert run.verdict == "flagged"
    assert run.flags == ("hidden_process", "memory_unknown")
    assert run.hidden_process == 2


def test_o56_a_cell_killed_for_hidden_memory_is_flagged_and_over():
    run = judge(record(instruments=watched(reading("over", hidden_memory=1))), "cond-a")
    assert (run.verdict, run.flags, run.memory) == ("flagged", ("hidden_memory",), "over")


def test_o56_a_run_over_its_limit_by_visible_memory_alone_is_clean():
    """O56: a visible process over the limit is the agent's own outcome (`memory_limit`), nothing hidden."""
    assert judge(record(instruments=watched(reading("over"))), "cond-a").verdict == "clean"


def test_o56_an_unknown_memory_reading_is_unknown_never_clean():
    run = judge(record(instruments=watched(reading("unknown", ["unattributed"]))), "cond-a")
    assert (run.verdict, run.flags, run.unknown) == ("unknown", ("memory_unknown",), ("unattributed",))


@pytest.mark.parametrize(
    ("instruments", "why"),
    [({}, "not_recorded"), ({NAMESPACE: {"ok": False, "error": "x"}}, "instrument_failed"),
     ({NAMESPACE: {"version": 2}}, "not_a_snapshot")],
)  # fmt: skip
def test_o56_a_record_without_a_readable_snapshot_is_unknown(instruments, why):
    run = judge(record(instruments=instruments), "cond-a")
    assert (run.verdict, run.unknown, run.memory) == ("unknown", (why,), "unknown")
    assert verdict_of(instruments.get(NAMESPACE)) == "unknown"


# ----------------------------------------------------------------------------- the analysis
@pytest.fixture
def prereg():
    return parse(tomllib.loads(TEXT), "inline", "a" * 64, SCHEMA)


def rows_of_pairs(n: int) -> list:
    rows = []
    for i in range(n):
        rows += both_arms(f"t{i}", "m", 1, 0.7 + 0.01 * i, 0.5 + 0.01 * i, cluster=f"g{i}", arm="a")
    for row in rows:
        row.factors["arm"] = row.condition[-1]
    return rows


def runs_for(rows, instruments_of) -> list:
    return [(record(run_key=row.run_key, instruments=instruments_of(i)), row.condition) for i, row in enumerate(rows)]


def test_o56_a_batch_whose_records_carry_no_reading_is_not_checked(prereg):
    rows = rows_of_pairs(4)
    assert visibility_analysis(runs_for(rows, lambda i: {}), rows, [], prereg) is None


def test_o56_once_one_record_carries_it_a_record_without_it_is_unknown_and_left_out(prereg):
    rows = rows_of_pairs(4)
    done = visibility_analysis(runs_for(rows, lambda i: watched(reading()) if i else {}), rows, [], prereg)
    assert [run.run_key for run in done.left_out] == [rows[0].run_key]
    assert done.left_out[0].unknown == ("not_recorded",)


def test_o56_with_every_run_clean_there_is_no_second_analysis(prereg):
    rows = rows_of_pairs(4)
    done = visibility_analysis(runs_for(rows, lambda i: watched(reading())), rows, [], prereg)
    assert (done.left_out, done.sensitivity, done.refused) == ((), None, None)
    assert done.counts()["memory_within"] == 8


def test_o56_the_sensitivity_analysis_leaves_out_the_flagged_and_the_unknown_runs(prereg):
    rows = rows_of_pairs(8)
    by_index = {0: reading("over", hidden_memory=1), 3: reading("unknown", ["cgroup_unreadable"])}
    done = visibility_analysis(runs_for(rows, lambda i: watched(by_index.get(i, reading()))), rows, [], prereg)
    primary = analyse(rows, prereg).contrast("a-vs-b")
    again = done.sensitivity.contrast("a-vs-b")
    assert (primary.n_pairs, again.n_pairs) == (8, 6)
    assert [a.lost[LEFT_OUT_AS] for a in again.arm_runs] == [1, 1]
    counts = done.counts()
    assert (counts["flagged"], counts["unknown"], counts["clean"], counts["left_out"]) == (1, 1, 14, 2)
    assert (counts["hidden_memory"], counts["memory_unknown"]) == (1, 1)
    assert (counts["memory_within"], counts["memory_over"], counts["memory_unknown_reading"]) == (14, 1, 1)


def test_o56_a_record_paired_with_another_run_s_row_is_refused(prereg):
    rows = rows_of_pairs(2)
    runs = runs_for(rows, lambda i: watched(reading()))
    runs[0], runs[1] = runs[1], runs[0]
    with pytest.raises(ValueError, match="paired with the row"):
        visibility_analysis(runs, rows, [], prereg)
