"""The mount-guard part of an analysis: every run judged once a batch is checked, and the analysis again without the
runs that are not clean, as a sensitivity analysis."""

import tomllib

import pytest
from mountguard_logs import NONCE, inotify, log, proof, record
from stats_support import both_arms

from expcore.campaign.factors import FactorSchema
from expcore.mountguard import NAMESPACE, MountGuard, NoMount, judge
from expcore.mountguard.sensitivity import LEFT_OUT_AS, mount_guard_analysis
from expcore.stats.analysis import analyse
from expcore.stats.prereg import parse

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


@pytest.fixture
def prereg():
    return parse(tomllib.loads(TEXT), "inline", "a" * 64, SCHEMA)


def rows_of_pairs(n: int) -> list:
    """`n` pairs in `n` clusters, cond-a ahead of cond-b by 0.2."""
    rows = []
    for i in range(n):
        rows += both_arms(f"t{i}", "m", 1, 0.7 + 0.01 * i, 0.5 + 0.01 * i, cluster=f"g{i}", arm="a")
    for row in rows:  # the factor schema's arm follows the condition
        row.factors["arm"] = row.condition[-1]
    return rows


class Source:
    def __init__(self, data):
        self.data = data

    def mount_watch_log(self):
        return self.data


def guarded(data):
    return {NAMESPACE: MountGuard(Source(data), NONCE).finish()}


def clean():
    return guarded(log())


def touched():
    return guarded(log([inotify("dir", "IN_OPEN", "IN_ISDIR")]))


def unproven():
    return guarded(log(proofs=[proof(ok=False, problems=["same_pid_namespace"])]))


def runs_for(rows, instruments_of) -> list:
    return [(record(run_key=row.run_key, instruments=instruments_of(i)), row.condition) for i, row in enumerate(rows)]


def test_a_batch_whose_records_carry_no_mount_guard_is_not_checked(prereg):
    """A Docker batch: no verdicts and no section, so its report is the text it always was."""
    rows = rows_of_pairs(4)
    assert mount_guard_analysis(runs_for(rows, lambda i: {}), rows, [], prereg) is None


def test_once_one_record_carries_it_a_record_without_it_is_unknown_and_left_out(prereg):
    rows = rows_of_pairs(4)
    done = mount_guard_analysis(runs_for(rows, lambda i: clean() if i else {}), rows, [], prereg)
    assert [run.run_key for run in done.left_out] == [rows[0].run_key]
    assert done.left_out[0].verdict == "unknown"
    assert done.left_out[0].unknown == ("not_recorded",)


def test_with_every_run_clean_there_is_no_second_analysis(prereg):
    rows = rows_of_pairs(4)
    done = mount_guard_analysis(runs_for(rows, lambda i: clean()), rows, [], prereg)
    assert done.left_out == ()
    assert (done.sensitivity, done.refused) == (None, None)
    assert done.counts()["clean"] == 8


def test_the_sensitivity_analysis_leaves_out_the_flagged_and_the_unknown_runs(prereg):
    """O60: one run touched the token's directory, one has an unproven isolation; each in its own pair."""
    rows = rows_of_pairs(8)
    by_index = {0: touched, 3: unproven}
    done = mount_guard_analysis(runs_for(rows, lambda i: by_index.get(i, clean)()), rows, [], prereg)
    primary = analyse(rows, prereg).contrast("a-vs-b")
    again = done.sensitivity.contrast("a-vs-b")
    assert primary.n_pairs == 8
    assert again.n_pairs == 6
    assert again.pairs_missing["unpaired_or_left_out"] == 2
    assert [a.lost[LEFT_OUT_AS] for a in again.arm_runs] == [1, 1]
    counts = done.counts()
    assert (counts["flagged"], counts["unknown"], counts["clean"], counts["left_out"]) == (1, 1, 14, 2)
    assert (counts["token_access"], counts["isolation_unproven"]) == (1, 1)


def test_the_sensitivity_analysis_is_deterministic(prereg):
    rows = rows_of_pairs(8)
    runs = runs_for(rows, lambda i: touched() if i == 2 else clean())
    first = mount_guard_analysis(runs, rows, [], prereg).sensitivity.contrast("a-vs-b")
    second = mount_guard_analysis(runs, rows, [], prereg).sensitivity.contrast("a-vs-b")
    assert (first.estimate, first.ci_low, first.ci_high, first.p_value) == (
        second.estimate,
        second.ci_low,
        second.ci_high,
        second.p_value,
    )


def test_a_record_paired_with_another_runs_row_is_refused(prereg):
    rows = rows_of_pairs(2)
    runs = runs_for(rows, lambda i: clean())
    runs[0], runs[1] = runs[1], runs[0]
    with pytest.raises(ValueError, match="paired with the row"):
        mount_guard_analysis(runs, rows, [], prereg)


def test_a_rerun_the_kernel_refuses_keeps_the_primary_and_says_why(prereg, monkeypatch):
    from expcore.mountguard import sensitivity
    from expcore.stats.analysis import ContrastRefused

    def refuse(*args, **kwargs):
        raise ContrastRefused("a treatment attempt's outcome is None")

    monkeypatch.setattr(sensitivity, "analyse", refuse)
    rows = rows_of_pairs(4)
    done = mount_guard_analysis(runs_for(rows, lambda i: touched() if i == 0 else clean()), rows, [], prereg)
    assert done.sensitivity is None
    assert done.refused == "a treatment attempt's outcome is None"


# --- a campaign that mixes mount runs and REST runs (O60: not applicable) -------------------------------------------


def rest():
    return {NAMESPACE: NoMount("rest").finish()}


def mixed_instruments(i: int) -> dict:
    """Rows 0-3 are mount runs (0 touched the token's directory, 1 has no snapshot, 2 is clean, 3 is unproven), rows
    4-15 are REST runs, whose record says the guard does not apply."""
    if i >= 4:
        return rest()
    return {0: touched, 1: dict, 2: clean, 3: unproven}[i]()


def test_a_rest_run_is_not_applicable_and_stays_in_the_mount_guards_rerun(prereg):
    """O69: the REST runs had no mount exposure, so the rerun leaves out only the mount runs that are not clean."""
    rows = rows_of_pairs(8)  # 16 runs: pairs 0-1 are mount runs, pairs 2-7 REST runs
    done = mount_guard_analysis(runs_for(rows, mixed_instruments), rows, [], prereg)
    counts = done.counts()
    assert (counts["flagged"], counts["unknown"], counts["clean"], counts["not_applicable"]) == (1, 2, 1, 12)
    assert counts["left_out"] == 3
    out = {run.run_key for run in done.left_out}
    assert out == {rows[i].run_key for i in (0, 1, 3)}  # flagged, unrecorded and unproven mount runs; no REST run
    assert all(run.verdict == "not_applicable" for run in done.runs[4:])
    again = done.sensitivity.contrast("a-vs-b")
    assert again.n_pairs == 8 - 2  # rows 0, 1, 2, 3 sit in pairs 0 and 1: both pairs lose a run, six REST pairs stay
    assert sum(a.lost[LEFT_OUT_AS] for a in again.arm_runs) == 3


def test_a_mount_run_without_its_snapshot_is_still_unknown_and_excluded_beside_not_applicable_runs(prereg):
    """O69: not applicable is the run's own statement: the absence of a snapshot does not make a mount run one."""
    rows = rows_of_pairs(4)
    done = mount_guard_analysis(runs_for(rows, lambda i: {} if i == 0 else rest()), rows, [], prereg)
    assert [(run.run_key, run.verdict, run.unknown) for run in done.left_out] == [
        (rows[0].run_key, "unknown", ("not_recorded",))
    ]
    assert done.counts()["not_applicable"] == 7


def test_a_batch_of_not_applicable_runs_alone_is_checked_and_has_no_second_analysis(prereg):
    """O69: every run had no mount: the guard says so, nothing is left out, and a rerun would be the primary one."""
    rows = rows_of_pairs(3)
    done = mount_guard_analysis(runs_for(rows, lambda i: rest()), rows, [], prereg)
    assert done is not None
    assert done.left_out == ()
    assert (done.sensitivity, done.refused) == (None, None)
    assert done.counts()["not_applicable"] == 6
    assert done.counts()["clean"] == 0  # never clean: the guard did not apply


# --- a REST arm whose records carry no mount-guard snapshot at all -------------------------------------------------

DATA_PATHS = {"api": "rest", "mount": "mount"}  # the campaign fingerprint's `data_paths` kinds, by level


def arm_rows(pairs: int = 48) -> list:
    """`pairs` pairs: cond-a is the REST arm (data_path level `api`), cond-b the mount arm (level `mount`)."""
    rows = rows_of_pairs(pairs)
    for row in rows:
        row.factors["data_path"] = "api" if row.condition == "cond-a" else "mount"
    return rows


def arm_runs(rows, flagged=(0, 1, 2, 3)) -> list:
    """The REST arm lists no mount guard; the mount arm lists it, `flagged` of its runs touched the token's
    directory (the index counts the mount arm's runs)."""
    runs, mount_index = [], 0
    for row in rows:
        if row.condition == "cond-a":
            instruments = {}
        else:
            instruments = touched() if mount_index in flagged else clean()
            mount_index += 1
        runs.append((record(run_key=row.run_key, instruments=instruments, factors=row.factors), row.condition))
    return runs


def test_o69_without_the_campaigns_data_paths_every_rest_run_with_no_snapshot_is_unknown_and_left_out(prereg):
    """O69: the shape that went wrong: 48 REST runs with no snapshot and 4 flagged mount runs of 96 leave 52 out."""
    rows = arm_rows()
    done = mount_guard_analysis(arm_runs(rows), rows, [], prereg)
    counts = done.counts()
    assert (counts["analysed"], counts["left_out"], counts["unknown"], counts["flagged"]) == (96, 52, 48, 4)
    assert counts["not_applicable"] == 0


def test_o69_a_rest_arm_with_no_snapshot_is_not_applicable_by_the_campaigns_recorded_data_path(prereg):
    """O69: the campaign fingerprint's `data_paths` says the level `api` is a REST data path, so a run of it with no
    snapshot had no mount: not applicable, kept in the rerun; only the 4 flagged mount runs are left out."""
    rows = arm_rows()
    done = mount_guard_analysis(arm_runs(rows), rows, [], prereg, DATA_PATHS)
    counts = done.counts()
    assert (counts["analysed"], counts["left_out"], counts["not_applicable"]) == (96, 4, 48)
    assert (counts["flagged"], counts["clean"], counts["unknown"]) == (4, 44, 0)
    assert {run.not_applicable for run in done.runs if run.condition == "cond-a"} == {"rest"}
    assert {run.data_path_from for run in done.runs if run.condition == "cond-a"} == {"campaign"}
    again = done.sensitivity.contrast("a-vs-b")
    assert again.n_pairs == 48 - 4
    assert sum(a.lost[LEFT_OUT_AS] for a in again.arm_runs) == 4


def test_o69_a_mount_run_with_no_snapshot_is_unknown_whatever_the_campaign_records(prereg):
    """O69: the level `mount` is a mount data path, so the run's missing snapshot is not a statement of anything."""
    rows = arm_rows(4)
    runs = [(record(run_key=row.run_key, factors=row.factors), row.condition) for row in rows]
    runs[1] = (runs[1][0].__class__(**{**vars(runs[1][0]), "instruments": clean()}), runs[1][1])  # one run carries it
    done = mount_guard_analysis(runs, rows, [], prereg, DATA_PATHS)
    kinds = {run.run_key: (run.verdict, run.not_applicable) for run in done.runs}
    assert kinds[rows[0].run_key] == ("not_applicable", "rest")  # cond-a: no snapshot, a REST level
    assert kinds[rows[1].run_key] == ("clean", None)  # cond-b: a mount run that carries its snapshot
    assert kinds[rows[3].run_key] == ("unknown", None)  # cond-b: a mount run with no snapshot


@pytest.mark.parametrize(
    "factors",
    [
        {},  # no data_path factor
        {"data_path": "elsewhere"},  # a level the campaign does not map
        {"data_path": ["api"]},  # not a level
        {"data_path": None},
    ],
)
def test_o69_a_run_whose_level_the_campaign_does_not_map_to_a_data_path_stays_unknown(factors):
    done = judge(record(factors=factors), "c", DATA_PATHS)
    assert (done.verdict, done.not_applicable) == ("unknown", None)


@pytest.mark.parametrize("paths", [None, {}, {"api": "mount"}, {"api": "api"}, {"api": ""}, {"api": None}])
def test_o69_a_campaign_that_records_no_rest_or_none_data_path_for_the_level_gives_no_exemption(paths):
    done = judge(record(factors={"data_path": "api"}), "c", paths)
    assert (done.verdict, done.not_applicable) == ("unknown", None)


def test_o69_a_statement_that_contradicts_the_campaigns_data_path_is_unknown():
    """The record says no mount where the campaign says the level is a mount, or names another data path than it."""
    as_mount = judge(record(instruments={NAMESPACE: NoMount("rest").finish()}, factors={"data_path": "mount"}), "c",
                     DATA_PATHS)  # fmt: skip
    assert (as_mount.verdict, as_mount.unknown) == ("unknown", ("data_path_mismatch",))
    other = judge(
        record(instruments={NAMESPACE: NoMount("none").finish()}, factors={"data_path": "api"}), "c", DATA_PATHS
    )
    assert (other.verdict, other.unknown) == ("unknown", ("data_path_mismatch",))
    agrees = judge(
        record(instruments={NAMESPACE: NoMount("rest").finish()}, factors={"data_path": "api"}), "c", DATA_PATHS
    )
    assert (agrees.verdict, agrees.not_applicable, agrees.data_path_from) == ("not_applicable", "rest", "record")


def test_o69_a_real_snapshot_is_judged_as_it_is_whatever_the_campaign_says_of_the_level():
    done = judge(record(instruments=clean(), factors={"data_path": "api"}), "c", DATA_PATHS)
    assert (done.verdict, done.not_applicable) == ("clean", None)
    flagged = judge(record(instruments=touched(), factors={"data_path": "api"}), "c", DATA_PATHS)
    assert flagged.verdict == "flagged"
