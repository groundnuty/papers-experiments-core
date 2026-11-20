"""Both guards together (#318): the analysis again without every run that the egress check or the mount guard leaves
out, flagged or unknown to either, beside each guard's own rerun."""

import tomllib

import egress_logs as EL
import mountguard_logs as ML
import pytest
from stats_support import both_arms

from expcore.campaign.factors import FactorSchema
from expcore.egress import NAMESPACE as EGRESS_NS
from expcore.egress import EgressLog, declaration
from expcore.egress.sensitivity import egress_analysis
from expcore.guards import LEFT_OUT_AS, guards_analysis
from expcore.mountguard import NAMESPACE as MOUNT_NS
from expcore.mountguard import MountGuard, NoMount
from expcore.mountguard.sensitivity import mount_guard_analysis
from expcore.stats.analysis import analyse
from expcore.stats.prereg import parse
from expcore.stats.rows import AbsentRun
from expcore.visibility import NAMESPACE as VIS_NS
from expcore.visibility import ProcessVisibility
from expcore.visibility.sensitivity import visibility_analysis

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
DECLARED = declaration([EL.CANARY], EL.CANARY)


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


class EgressSource:
    def __init__(self, data):
        self.data = data

    def egress_log(self):
        return self.data


class MountSource:
    def __init__(self, data):
        self.data = data

    def mount_watch_log(self):
        return self.data


EGRESS = {
    "clean": lambda: EgressLog(EgressSource(EL.log([])), DECLARED, EL.NONCE).finish(),
    "flagged": lambda: EgressLog(
        EgressSource(EL.log([EL.rec("connect", "192.0.2.1", 443)])), DECLARED, EL.NONCE
    ).finish(),
    "unknown": lambda: EgressLog(EgressSource(None), DECLARED, EL.NONCE).finish(),
}
MOUNT = {
    "clean": lambda: MountGuard(MountSource(ML.log()), ML.NONCE).finish(),
    "flagged": lambda: MountGuard(MountSource(ML.log([ML.inotify("dir", "IN_OPEN")])), ML.NONCE).finish(),
    "unknown": lambda: MountGuard(MountSource(None), ML.NONCE).finish(),
    "not_applicable": lambda: NoMount("rest").finish(),  # a REST run: no mount, the guard does not apply
}
# row index -> (egress, mount); every other row is clean to both
CASES = {
    0: ("flagged", "clean"),  # only egress
    3: ("clean", "flagged"),  # only the mount guard
    5: ("flagged", "flagged"),  # both
    6: ("clean", "unknown"),  # unknown to the mount guard, clean to egress
    8: ("unknown", "clean"),  # unknown to egress, clean to the mount guard
}


def analysed(rows, prereg, cases=CASES):
    """The three analyses of a batch whose records carry both instruments."""
    runs = []
    for i, row in enumerate(rows):
        egress, mount = cases.get(i, ("clean", "clean"))
        instruments = {EGRESS_NS: EGRESS[egress](), MOUNT_NS: MOUNT[mount]()}
        runs.append((EL.record(run_key=row.run_key, instruments=instruments), row.condition))
    egress = egress_analysis(runs, rows, [], prereg)
    mount = mount_guard_analysis(runs, rows, [], prereg)
    return egress, mount, guards_analysis(rows, [], prereg, egress, mount)


def test_a_run_either_guard_leaves_out_is_left_out_of_the_combined_rerun(prereg):
    """O60: flagged by egress alone, by the mount guard alone, by both, unknown to one and clean to the other: each is
    left out of the combined rerun, which keeps the pairs no guard touched."""
    rows = rows_of_pairs(8)
    egress, mount, both = analysed(rows, prereg)
    out = {run.run_key for run in both.left_out}
    assert out == {rows[i].run_key for i in CASES}
    again = both.sensitivity.contrast("a-vs-b")
    assert again.n_pairs == 3
    assert again.pairs_missing["unpaired_or_left_out"] == 5
    assert sum(a.lost[LEFT_OUT_AS] for a in again.arm_runs) == 5
    assert egress.sensitivity.contrast("a-vs-b").n_pairs == 5  # rows 0, 5, 8
    assert mount.sensitivity.contrast("a-vs-b").n_pairs == 5  # rows 3, 5, 6


def test_each_left_out_run_says_which_guard_left_it_out_and_what_each_found(prereg):
    rows = rows_of_pairs(8)
    _, _, both = analysed(rows, prereg)
    by_key = {run.run_key: run for run in both.runs}
    seen = {
        i: (by_key[rows[i].run_key].egress, by_key[rows[i].run_key].mount, by_key[rows[i].run_key].by) for i in CASES
    }
    assert seen == {
        0: ("flagged", "clean", "egress"),
        3: ("clean", "flagged", "mount"),
        5: ("flagged", "flagged", "both"),
        6: ("clean", "unknown", "mount"),
        8: ("unknown", "clean", "egress"),
    }
    assert by_key[rows[1].run_key].by is None


def test_the_counts_and_the_counts_by_condition(prereg):
    rows = rows_of_pairs(8)
    _, _, both = analysed(rows, prereg)
    assert both.counts() == {"analysed": 16, "left_out": 5, "egress_only": 2, "mount_only": 2, "both": 1}
    assert both.by_condition() == {
        "cond-a": {"analysed": 8, "left_out": 3, "egress_only": 2, "mount_only": 1, "both": 0},
        "cond-b": {"analysed": 8, "left_out": 2, "egress_only": 0, "mount_only": 1, "both": 1},
    }


def test_with_every_run_clean_to_both_there_is_no_combined_rerun(prereg):
    rows = rows_of_pairs(4)
    _, _, both = analysed(rows, prereg, cases={})
    assert both.left_out == ()
    assert (both.sensitivity, both.refused) == (None, None)


def test_a_batch_checked_by_one_guard_alone_has_no_combined_rerun(prereg):
    """Its one guard's rerun is the combined one: no third analysis."""
    rows = rows_of_pairs(4)
    egress, mount, _ = analysed(rows, prereg)
    assert guards_analysis(rows, [], prereg, egress, None) is None
    assert guards_analysis(rows, [], prereg, None, mount) is None


def test_the_combined_rerun_is_the_primary_analysis_without_the_left_out_runs(prereg):
    rows = rows_of_pairs(8)
    _, _, both = analysed(rows, prereg)
    out = {run.run_key for run in both.left_out}
    kept = [row for row in rows if row.run_key not in out]
    left = [AbsentRun(r.run_key, r.condition, r.factors, LEFT_OUT_AS) for r in rows if r.run_key in out]
    direct = analyse(kept, prereg, left).contrast("a-vs-b")
    again = both.sensitivity.contrast("a-vs-b")
    assert (again.estimate, again.p_value, again.n_pairs) == (direct.estimate, direct.p_value, direct.n_pairs)


def test_verdicts_of_different_runs_are_refused(prereg):
    rows = rows_of_pairs(2)
    egress, mount, _ = analysed(rows, prereg)
    reordered = type(mount)(tuple(reversed(mount.runs)), mount.sensitivity, mount.refused)
    with pytest.raises(ValueError, match="same runs"):
        guards_analysis(rows, [], prereg, egress, reordered)


def test_a_rerun_the_kernel_refuses_keeps_the_primary_and_says_why(prereg, monkeypatch):
    from expcore import guards
    from expcore.stats.analysis import ContrastRefused

    rows = rows_of_pairs(4)
    egress, mount, _ = analysed(rows, prereg, cases={0: ("flagged", "clean")})

    def refuse(*args, **kwargs):
        raise ContrastRefused("a treatment attempt's outcome is None")

    monkeypatch.setattr(guards, "analyse", refuse)
    both = guards_analysis(rows, [], prereg, egress, mount)
    assert both.sensitivity is None
    assert both.refused == "a treatment attempt's outcome is None"


# --- a campaign that mixes mount and REST runs: the combined rerun leaves a run out only for a guard that applies ----


def test_o75_a_run_the_mount_guard_does_not_apply_to_is_left_out_only_for_egress(prereg):
    """O75: not applicable to the mount guard and clean to egress: kept. Not applicable and flagged or unknown to
    egress: left out, by egress alone. A mount run unknown to the mount guard is left out, by the mount guard."""
    rows = rows_of_pairs(8)
    cases = {
        0: ("clean", "not_applicable"),
        1: ("flagged", "not_applicable"),
        2: ("unknown", "not_applicable"),
        3: ("clean", "unknown"),  # a mount run whose record has no usable snapshot
        4: ("flagged", "flagged"),
    }
    _, mount, both = analysed(rows, prereg, cases)
    by_index = {i: both.runs[i] for i in range(5)}
    assert [by_index[i].by for i in range(5)] == [None, "egress", "egress", "mount", "both"]
    assert [by_index[i].mount for i in range(5)] == [
        "not_applicable",
        "not_applicable",
        "not_applicable",
        "unknown",
        "flagged",
    ]
    assert {run.run_key for run in both.left_out} == {rows[i].run_key for i in (1, 2, 3, 4)}
    assert both.counts() == {"analysed": 16, "left_out": 4, "egress_only": 2, "mount_only": 1, "both": 1}
    assert mount.sensitivity is not None
    assert {run.run_key for run in mount.left_out} == {rows[3].run_key, rows[4].run_key}  # not the REST runs


def test_o75_a_run_not_applicable_to_the_mount_guard_and_clean_to_egress_stays_in_the_combined_rerun(prereg):
    """O75: nothing is left out, so there is no combined rerun."""
    rows = rows_of_pairs(4)
    _, mount, both = analysed(rows, prereg, dict.fromkeys(range(8), ("clean", "not_applicable")))
    assert both.left_out == ()
    assert (both.sensitivity, both.refused) == (None, None)
    assert mount.counts()["not_applicable"] == 8


def test_a_guard_state_this_code_does_not_know_leaves_the_run_out():
    """O75: only `clean` and `not_applicable` keep a run: an unknown word is never read as a pass."""
    from expcore.guards import GuardedRun

    assert GuardedRun("k", 1, "c", "clean", "not_applicable").by is None
    assert GuardedRun("k", 1, "c", "clean", "fine").by == "mount"
    assert GuardedRun("k", 1, "c", "", "clean").by == "egress"


# --- every guard: the process-visibility check joins the combined rerun (O76) ----------------------------------------

FIGURES = {"margin_bytes": 1, "start_unexplained_bytes": 0, "unexplained_peak_bytes": 0, "cgroup_anon_bytes": 5,
           "visible_anon_bytes": 5}  # fmt: skip


class VisibilitySource:
    def __init__(self, data):
        self.data = data

    def process_visibility(self):
        return self.data


def reading(memory="within", unknown=(), hidden_process=0, hidden_memory=0):
    return {"version": 1, "memory": memory, "unknown": list(unknown), "hidden_process": hidden_process,
            "hidden_memory": hidden_memory, "figures": dict(FIGURES)}  # fmt: skip


VISIBILITY = {
    "clean": lambda: ProcessVisibility(VisibilitySource(reading())).finish(),
    "flagged": lambda: ProcessVisibility(VisibilitySource(reading("unknown", ["hidden_child"], 1))).finish(),
    "unknown": lambda: ProcessVisibility(VisibilitySource(None)).finish(),
}


def analysed_all(rows, prereg, cases, guards=("egress", "mount", "visibility")):
    """The analyses of a batch whose records carry the guards named; `cases` maps a row index to its states in the
    order egress, mount, visibility (every other row is clean to all)."""
    runs = []
    for i, row in enumerate(rows):
        egress, mount, seen = cases.get(i, ("clean", "clean", "clean"))
        instruments = {}
        if "egress" in guards:
            instruments[EGRESS_NS] = EGRESS[egress]()
        if "mount" in guards:
            instruments[MOUNT_NS] = MOUNT[mount]()
        if "visibility" in guards:
            instruments[VIS_NS] = VISIBILITY[seen]()
        runs.append((EL.record(run_key=row.run_key, instruments=instruments), row.condition))
    egress = egress_analysis(runs, rows, [], prereg) if "egress" in guards else None
    mount = mount_guard_analysis(runs, rows, [], prereg) if "mount" in guards else None
    seen = visibility_analysis(runs, rows, [], prereg) if "visibility" in guards else None
    return egress, mount, seen, guards_analysis(rows, [], prereg, egress, mount, seen)


THREE = {
    0: ("clean", "clean", "flagged"),  # the process-visibility check alone
    1: ("flagged", "clean", "unknown"),  # egress and visibility
    3: ("clean", "not_applicable", "flagged"),  # a REST run: visibility alone, the mount guard does not apply
    5: ("flagged", "flagged", "flagged"),  # all three
    6: ("clean", "not_applicable", "clean"),  # a REST run clean to the others: kept
    8: ("clean", "unknown", "clean"),  # a mount run without a snapshot: the mount guard alone
    10: ("unknown", "clean", "clean"),  # egress alone
}


def test_o76_the_combined_rerun_leaves_out_a_run_any_of_the_three_guards_leaves_out(prereg):
    """O76: process visibility flagged or unknown, alone or with the others, leaves a run out of the combined rerun,
    beside the runs egress and the mount guard leave out; a run not applicable to the mount guard stays in."""
    rows = rows_of_pairs(8)
    _, mount, seen, both = analysed_all(rows, prereg, THREE)
    assert both.guards == ("egress", "mount", "visibility")
    by = {i: both.runs[i].by for i in THREE}
    assert by == {
        0: "visibility",
        1: "egress+visibility",
        3: "visibility",
        5: "egress+mount+visibility",
        6: None,
        8: "mount",
        10: "egress",
    }
    assert {run.run_key for run in both.left_out} == {rows[i].run_key for i in THREE if i != 6}
    # rows 0 and 1 share a pair, and 3, 5, 8 and 10 are in pairs of their own: five of the eight pairs are lost
    assert both.sensitivity.contrast("a-vs-b").n_pairs == 8 - 5
    assert seen.sensitivity is not None  # each guard keeps its own rerun
    assert {run.run_key for run in mount.left_out} == {rows[i].run_key for i in (5, 8)}


def test_o76_the_counts_of_three_guards_name_each_guard_and_those_left_out_by_several(prereg):
    rows = rows_of_pairs(8)
    _, _, _, both = analysed_all(rows, prereg, THREE)
    assert both.counts() == {
        "analysed": 16,
        "left_out": 6,
        "egress_only": 1,
        "mount_only": 1,
        "visibility_only": 2,
        "several": 2,
    }
    assert both.by_condition()["cond-a"]["visibility_only"] == 1  # row 0, in cond-a


def test_o76_a_batch_of_egress_and_the_visibility_check_alone_is_combined_with_the_word_both(prereg):
    rows = rows_of_pairs(8)
    cases = {0: ("flagged", "clean", "clean"), 1: ("clean", "clean", "unknown"), 2: ("flagged", "clean", "flagged")}
    _, mount, _, both = analysed_all(rows, prereg, cases, guards=("egress", "visibility"))
    assert mount is None
    assert both.guards == ("egress", "visibility")
    assert [both.runs[i].by for i in range(3)] == ["egress", "visibility", "both"]
    assert both.counts() == {"analysed": 16, "left_out": 3, "egress_only": 1, "visibility_only": 1, "both": 1}
    assert not both.legacy


def test_o76_a_batch_of_egress_and_the_mount_guard_alone_keeps_its_vocabulary(prereg):
    rows = rows_of_pairs(8)
    _, _, _, both = analysed_all(rows, prereg, {0: ("flagged", "flagged", "clean")}, guards=("egress", "mount"))
    assert both.legacy
    assert both.counts() == {"analysed": 16, "left_out": 1, "egress_only": 0, "mount_only": 0, "both": 1}
    assert both.runs[0].by == "both"


def test_o76_a_run_unknown_to_the_visibility_check_and_clean_to_the_others_is_left_out(prereg):
    rows = rows_of_pairs(4)
    _, _, _, both = analysed_all(rows, prereg, {0: ("clean", "clean", "unknown")})
    assert [run.run_key for run in both.left_out] == [rows[0].run_key]
    assert both.runs[0].visibility == "unknown"


def test_o76_one_guard_alone_has_no_combined_rerun_whichever_it_is(prereg):
    rows = rows_of_pairs(4)
    for guard in ("egress", "mount", "visibility"):
        *_, both = analysed_all(rows, prereg, {0: ("flagged", "flagged", "flagged")}, guards=(guard,))
        assert both is None


def test_o76_verdicts_of_different_runs_are_refused_for_any_guard(prereg):
    rows = rows_of_pairs(2)
    egress, mount, seen, _ = analysed_all(rows, prereg, {})
    reordered = type(seen)(tuple(reversed(seen.runs)), seen.sensitivity, seen.refused)
    with pytest.raises(ValueError, match="same runs"):
        guards_analysis(rows, [], prereg, egress, mount, reordered)


def test_the_combined_rerun_keeps_a_package_index_only_run_and_leaves_out_an_outside_one(prereg):
    """O95: the combined rerun reads the egress flags alone, so a run whose only hit is a declared index's install
    is kept and one with an outside connection is left out."""
    rows = rows_of_pairs(4)
    runs = []
    for i, row in enumerate(rows):
        instruments = {EGRESS_NS: EGRESS["flagged" if i == 0 else "clean"](), MOUNT_NS: MOUNT["clean"]()}
        run = EL.record(run_key=row.run_key, instruments=instruments, cells=["!pip install pandas"])
        runs.append((run, row.condition))
    egress = egress_analysis(runs, rows, [], prereg, package_indexes=["pypi.org"])
    both = guards_analysis(rows, [], prereg, egress, mount_guard_analysis(runs, rows, [], prereg))
    assert [run.run_key for run in both.left_out] == [rows[0].run_key]
    assert both.sensitivity.contrast("a-vs-b").n_pairs == 3
