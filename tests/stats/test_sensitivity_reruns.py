"""S19: a preregistration declares the sensitivity reruns the study expects, in `[sensitivity] reruns`, and what an
analysis would run must be exactly that; an absent table reads as `reruns = []`."""

import pytest

from expcore.campaign.factors import FactorSchema
from expcore.config.loading import ConfigError
from expcore.stats.prereg import load as load_checked
from expcore.stats.reruns import ANY_GUARD, EGRESS, MOUNT_GUARD, PROCESS_VISIBILITY, READ_LOG, RERUNS, difference

SCHEMA = FactorSchema({"arm": ["a", "b"]}, ["task", "arm", "model", "seed"], {f"cond-{a}": {"arm": a} for a in "ab"})
BASE = """\
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
ALL = '[sensitivity]\nreruns = ["egress", "mount_guard", "process_visibility", "read_log", "any_guard"]\n'


def write(tmp_path, text=BASE):
    path = tmp_path / "preregistration.toml"
    path.write_text(text, encoding="utf-8")
    return path


def load(path):
    return load_checked(path, SCHEMA)


def test_s19_the_reruns_are_named_as_the_core_runs_them():
    """S19, S20: the four guards' reruns and the one of every guard together, in this order."""
    assert RERUNS == ("egress", "mount_guard", "process_visibility", "read_log", "any_guard")
    assert (EGRESS, MOUNT_GUARD, PROCESS_VISIBILITY, READ_LOG, ANY_GUARD) == RERUNS


def test_s19_a_preregistration_declares_the_reruns_it_expects_in_the_order_it_gives(tmp_path):
    """S19: the table loads in the order given."""
    prereg = load(write(tmp_path, BASE + '\n[sensitivity]\nreruns = ["any_guard", "egress"]\n'))
    assert prereg.sensitivity_reruns == ("any_guard", "egress")
    assert load(write(tmp_path, BASE + "\n" + ALL)).sensitivity_reruns == RERUNS


def test_s19_a_preregistration_that_declares_nothing_has_none_declared(tmp_path):
    """S19: the field says whether a table was written (None for none, `()` for `reruns = []`); the check reads both as
    an empty declaration, and analysis.json says only what was written."""
    assert load(write(tmp_path)).sensitivity_reruns is None
    assert load(write(tmp_path, BASE + "\n[sensitivity]\nreruns = []\n")).sensitivity_reruns == ()


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ('[sensitivity]\nreruns = ["egres"]\n', "did you mean 'egress'"),
        ('[sensitivity]\nreruns = ["both"]\n', "egress, mount_guard, process_visibility, read_log, any_guard"),
        ('[sensitivity]\nreruns = ["egress", "egress"]\n', "twice"),
        ('[sensitivity]\nreruns = "egress"\n', "array"),
        ("[sensitivity]\nreruns = [1]\n", "array"),
        ("[sensitivity]\n", "is required"),
        ('[sensitivity]\nreruns = ["egress"]\nextra = 1\n', "not a known key"),
    ],
)
def test_s19_a_declaration_that_is_not_one_is_refused_with_the_file_the_table_and_the_key(tmp_path, text, fragment):
    """S19: a malformed declaration is refused naming the file, the table and the key."""
    with pytest.raises(ConfigError, match=fragment) as raised:
        load(write(tmp_path, BASE + "\n" + text))
    assert "preregistration.toml" in str(raised.value)
    assert "[sensitivity]" in str(raised.value)


def test_s19_a_sensitivity_key_that_is_not_a_table_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="must be a table"):
        load(write(tmp_path, 'sensitivity = ["egress"]\n' + BASE))


def test_s19_an_override_checks_the_declaration_too_and_leaves_the_original_alone(tmp_path):
    prereg = load(write(tmp_path, BASE + ALL))
    changed = prereg.with_override(sensitivity_reruns=["egress"])
    assert (changed.sensitivity_reruns, prereg.sensitivity_reruns) == (("egress",), RERUNS)
    for bad in (["nope"], ["egress", "egress"], "egress", [3]):
        with pytest.raises(ValueError, match="sensitivity"):
            prereg.with_override(sensitivity_reruns=bad)


def test_s19_the_declaration_is_in_the_file_so_in_its_hash(tmp_path):
    plain = load(write(tmp_path)).sha256
    assert load(write(tmp_path, BASE + ALL)).sha256 != plain


# --- what would run against what is declared ----------------------------------------------------------------------


def test_s19_the_reruns_that_match_what_is_declared_are_accepted():
    """S19: what the analysis would run is what is declared, whatever the order."""
    assert difference(("egress", "any_guard"), ("egress", "any_guard")) is None
    assert difference(("any_guard", "egress"), ("egress", "any_guard")) is None  # the order is not a difference
    assert difference((), ()) is None


def test_s19_a_declared_rerun_that_cannot_run_is_named_with_why():
    """S19: a declared rerun no record supports is named with why."""
    said = difference(("egress", "mount_guard"), ("egress",))
    assert said is not None
    assert "declared but not run: mount_guard" in said
    assert "no analysed record carries the mount_guard instrument" in said
    assert "run but not declared" not in said


def test_s19_a_rerun_that_would_run_and_is_not_declared_is_named():
    """S19: a rerun the records would produce and nobody declared is named."""
    said = difference(("egress",), ("egress", "process_visibility"))
    assert said is not None
    assert "run but not declared: process_visibility" in said
    assert "declared but not run" not in said


def test_s19_both_directions_are_named_together_with_the_two_lists():
    said = difference(("egress", "any_guard"), ("mount_guard",))
    assert "declared but not run: egress" in said
    assert "any_guard" in said.split("declared but not run:")[1]
    assert "run but not declared: mount_guard" in said
    assert "declared: egress, any_guard; would run: mount_guard" in said


def test_s19_any_guard_needs_two_guards_and_says_which_checked_the_batch():
    said = difference(("any_guard",), ("egress",))
    assert "any_guard (it needs two or more guards to have checked the batch; only egress did)" in said
    assert "none did" in difference(("any_guard",), ())


def test_s19_a_declaration_of_nothing_refuses_a_batch_that_would_run_a_rerun():
    said = difference((), ("egress",))
    assert said is not None
    assert "run but not declared: egress" in said


def test_s19_a_guard_that_checked_the_batch_ran_its_rerun_whether_or_not_it_left_a_run_out():
    """The rerun is run when the guard's analysis exists: a batch whose every run is clean has no second analysis."""
    from expcore.egress.sensitivity import EgressAnalysis
    from expcore.stats.reruns import ran_reruns

    clean = EgressAnalysis((), None)
    assert clean.sensitivity is None
    assert ran_reruns(clean, None, None, None) == ("egress",)
    assert ran_reruns(None, None, None, None) == ()
    assert ran_reruns(clean, clean, clean, clean, clean) == RERUNS
    assert ran_reruns(clean, clean, clean, clean) == ("egress", "mount_guard", "process_visibility", "any_guard")
    assert ran_reruns(None, clean, None, clean) == ("mount_guard", "any_guard")


def test_s19_the_refusal_for_an_absent_table_names_the_table_to_add():
    """S19: an absent table reads as `reruns = []`; the sentence names the file and the reruns to declare."""
    from expcore.stats.reruns import missing_table

    said = missing_table("study/preregistration.toml", ("egress", "any_guard"))
    assert said == (
        "study/preregistration.toml has no [sensitivity] table, and an absent table declares none; add [sensitivity] "
        'reruns = ["egress", "any_guard"]'
    )
    assert difference((), ("egress", "any_guard")) is not None  # what an absent table is checked as
    assert difference((), ()) is None  # nothing guarded, nothing to declare


def test_s20_the_read_log_rerun_is_run_when_its_analysis_exists_and_ordered_before_any_guard():
    """S20: the read-log scan's analysis exists when a record carries the `read_log` instrument, whether or not a run
    was flagged; its rerun is named `read_log`, between the process-visibility check's and the combined one."""
    from expcore.egress.sensitivity import EgressAnalysis
    from expcore.readlog.sensitivity import ReadLogAnalysis
    from expcore.stats.reruns import ran_reruns

    clean = ReadLogAnalysis((), None)
    assert ran_reruns(None, None, None, None, clean) == ("read_log",)
    assert ran_reruns(EgressAnalysis((), None), None, None, clean, clean) == ("egress", "read_log", "any_guard")
    assert ran_reruns(None, None, None, None) == ()


def test_s20_a_declared_read_log_rerun_that_cannot_run_is_named_with_why():
    said = difference(("read_log",), ("egress",))
    assert "declared but not run: read_log (no analysed record carries the read_log instrument)" in said
    assert "run but not declared: egress" in said


def test_s20_any_guard_names_the_read_log_scan_among_the_guards_that_checked_the_batch():
    said = difference(("any_guard",), ("egress", "read_log"))
    assert said is None
    said = difference(("any_guard",), ("read_log",))
    assert "only read_log did" in said


def test_s20_a_preregistration_declares_the_read_log_rerun(tmp_path):
    prereg = load(write(tmp_path, BASE + '\n[sensitivity]\nreruns = ["read_log", "any_guard"]\n'))
    assert prereg.sensitivity_reruns == ("read_log", "any_guard")
    with pytest.raises(ConfigError, match="did you mean 'read_log'"):
        load(write(tmp_path, BASE + '\n[sensitivity]\nreruns = ["read_lo"]\n'))
