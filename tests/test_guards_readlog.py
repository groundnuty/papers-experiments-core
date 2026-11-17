"""S20: the read-log scan joins the guards: a run whose code touched the read-log channel is left out of the rerun
without every run any guard leaves out, beside the runs the egress check, the mount guard and the process-visibility
check leave out, and a batch without the read-log instrument has the guards, counts and text it always had."""

import egress_logs as EL
import pytest
from readlog_support import FORGER, ON, preregistration, rows_of_pairs

from expcore.egress import NAMESPACE as EGRESS_NS
from expcore.egress import EgressLog, declaration
from expcore.egress.sensitivity import egress_analysis
from expcore.guards import GUARDS, READ_LOG, GuardedRun, guards_analysis
from expcore.readlog.scan import NAMESPACE as READ_LOG_NS
from expcore.readlog.sensitivity import read_log_analysis
from expcore.report_guards import guards_lines, summary_lines

DECLARED = declaration([EL.CANARY], EL.CANARY)


class EgressSource:
    def __init__(self, data):
        self.data = data

    def egress_log(self):
        return self.data


def egress_clean():
    return EgressLog(EgressSource(EL.log([])), DECLARED, EL.NONCE).finish()


def egress_outside():
    return EgressLog(EgressSource(EL.log([EL.rec("connect", "192.0.2.1", 443)])), DECLARED, EL.NONCE).finish()


def table(header, rows):
    return [" | ".join(header), *(" | ".join(str(c) for c in row) for row in rows)]


@pytest.fixture
def prereg():
    return preregistration()


# row index -> (egress is outside, the run's code touched the channel)
CASES = {0: (True, False), 2: (False, True), 5: (True, True)}


def analysed(rows, prereg, cases=CASES, *, egress=True, read_log=True):
    """The analyses of a batch whose records carry the guards asked for; `cases` says which rows are flagged."""
    runs = []
    for i, row in enumerate(rows):
        outside, touched = cases.get(i, (False, False))
        instruments = {}
        if egress:
            instruments[EGRESS_NS] = egress_outside() if outside else egress_clean()
        if read_log:
            instruments.update(ON)
        runs.append(
            (
                EL.record(run_key=row.run_key, cells=[FORGER if touched else "print(1)"], instruments=instruments),
                row.condition,
            )
        )
    first = egress_analysis(runs, rows, [], prereg) if egress else None
    seen = read_log_analysis(runs, rows, [], prereg) if read_log else None
    return first, seen, guards_analysis(rows, [], prereg, first, None, None, seen)


def test_s20_the_read_log_scan_is_a_guard_named_after_its_instrument_and_ordered_last():
    assert READ_LOG == "read_log"
    assert GUARDS == ("egress", "mount", "visibility", "read_log")
    assert READ_LOG_NS == "read_log"


def test_s20_a_run_whose_code_touched_the_channel_is_left_out_of_the_combined_rerun(prereg):
    """S20: flagged by egress alone, by the read-log scan alone and by both: each is left out of the combined rerun."""
    rows = rows_of_pairs(8)
    first, seen, both = analysed(rows, prereg)
    assert both.guards == ("egress", "read_log")
    assert [both.runs[i].by for i in CASES] == ["egress", "read_log", "both"]
    assert {run.run_key for run in both.left_out} == {rows[i].run_key for i in CASES}
    assert both.sensitivity.contrast("a-vs-b").n_pairs == 8 - 3  # three pairs of their own lose a run
    assert both.runs[2].read_log == "flagged"
    assert both.runs[0].read_log == "clean"
    assert seen.sensitivity is not None  # each guard keeps its own rerun
    assert {run.run_key for run in seen.flagged} == {rows[2].run_key, rows[5].run_key}


def test_s20_the_counts_name_the_read_log_scan_and_those_left_out_by_both(prereg):
    _, _, both = analysed(rows_of_pairs(8), prereg)
    assert both.counts() == {"analysed": 16, "left_out": 3, "egress_only": 1, "read_log_only": 1, "both": 1}
    assert both.by_condition()["cond-a"]["read_log_only"] == 1  # row 2, in cond-a


def test_s20_one_guard_alone_has_no_combined_rerun_and_the_read_log_scan_is_no_exception(prereg):
    *_, both = analysed(rows_of_pairs(4), prereg, egress=False)
    assert both is None


def test_s20_a_batch_without_the_read_log_instrument_has_no_read_log_state_or_count(prereg):
    """S20: the guards, counts and states of a batch the scan did not check are what they were before it existed."""
    rows = rows_of_pairs(4)
    _, seen, both = analysed(rows, prereg, read_log=False)
    assert seen is None
    assert both is None  # egress alone
    assert GuardedRun("k", 1, "c", "flagged", None).states == {"egress": "flagged"}
    assert GuardedRun("k", 1, "c", "flagged", None).read_log is None


def test_s20_a_read_log_state_this_code_does_not_know_leaves_the_run_out():
    """S20: only `clean` and `not_applicable` keep a run: an unknown word is never read as a pass."""
    assert GuardedRun("k", 1, "c", "clean", None, None, "clean").by is None
    assert GuardedRun("k", 1, "c", "clean", None, None, "flagged").by == "read_log"
    assert GuardedRun("k", 1, "c", "clean", None, None, "fine").by == "read_log"


def test_s20_verdicts_of_different_runs_are_refused_for_the_read_log_scan_too(prereg):
    rows = rows_of_pairs(2)
    first, seen, _ = analysed(rows, prereg, {})
    reordered = type(seen)(tuple(reversed(seen.runs)), seen.sensitivity, seen.refused)
    with pytest.raises(ValueError, match="same runs"):
        guards_analysis(rows, [], prereg, first, None, None, reordered)


def test_s20_the_combined_section_names_the_read_log_scan_and_says_what_its_states_are(prereg):
    _, _, both = analysed(rows_of_pairs(8), prereg)
    text = "\n".join(guards_lines(both, table, lambda result: ["TABLES"]))
    assert text.startswith("## Egress and read log together (detected, not prevented)")
    assert "its read log is `flagged` when its code names the read-log channel or a way to it" in text
    assert "Read log" in text  # the column of a row
    assert "the read-log scan alone" in text
    assert any("read-log" in line for line in summary_lines(both, lambda result, prefix: []))
