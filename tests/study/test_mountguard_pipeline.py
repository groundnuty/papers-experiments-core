"""The mount guard through the pipeline (O60): a study whose executor adds the mount_guard instrument is run, checked
in a fresh process and analysed, and its analysis, analysis.json and report carry the verdicts and the sensitivity
analysis; a study without the instrument gets none of them."""

import json
import shutil
from pathlib import Path

import pytest
from study_support import HEAD, clean_environment, cli

from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study
from expcore.study.pipeline import ANALYSIS_FILE, REPORT_FILE, analyse_study, output_bytes, run_study

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
INSTRUMENTS = "instruments = [FactsServed(shelf)]"
# Added to the copy's plugin: a mount_guard instrument over a stand-in watcher log; every run of the task `lake`
# touched the token's directory, a run of `hill` has a log that was never closed, the others are clean.
GUARD = '''

import json as _json

from expcore.mountguard import MountGuard, NoMount

_NONCE = "00112233445566778899aabbccddeeff"


class _StubWatch:
    def __init__(self, task):
        self.task = task

    def mount_watch_log(self):
        def inotify(target, mask, entry="self"):
            return {"event": "inotify", "target": target, "mask": [mask], "entry": entry}

        lines = [{"event": "start", "format": "expcore-mountwatch/1", "nonce": _NONCE, "pid": 7, "uid": 1000,
                  "nspid_depth": 1, "ptrace_scope": 1, "watches": {"dir": True, "token": True}, "mount_seen": True},
                 {"event": "oneclient", "pid": 99, "seen": True, "nspid_depth": 1},
                 inotify("token", "IN_DELETE_SELF"), inotify("dir", "IN_DELETE", "token"), {"event": "armed"},
                 {"event": "proof", "label": "cell", "ok": True, "problems": [],
                  "cell": {"pid": 4242, "start_time": 777, "nspid_depth": 2}}]
        if self.task == "lake":
            lines.append(inotify("dir", "IN_OPEN"))
        lines.append({"event": "disarmed"})
        if self.task != "hill":
            lines.append({"event": "end", "lines": len(lines), "samples": 3, "polls": 4, "polls_failed": 0,
                          "unwritten": 0})
        for seq, line in enumerate(lines):
            line.update(seq=seq, wall=float(seq), mono=float(seq))
        return "".join(_json.dumps(line) + "\\n" for line in lines).encode()
'''  # fmt: skip


# Added beside the guard for a batch checked by both: an egress log of the gate's canary alone, so every run is clean
# to egress but those of the task `hill`, whose log was never closed (unknown).
EGRESS = '''

from expcore.egress import EgressLog, declaration

_GATE = ("127.0.0.1", 41234)


class _StubEgress:
    def __init__(self, task):
        self.task = task

    def egress_log(self):
        recs = [{"op": op, "pid": 5, "fd": 3, "time": "1.000000000", "type": "tcp", "family": "inet",
                 "address": _GATE[0], "port": _GATE[1], "result": 0 if op == "result" else None, "dropped": 0}
                for op in ("connect", "result")]
        lines = [{"event": "start", "format": "hpcop-egress/1", "nonce": _NONCE, "pid": 7, "uid": 1000}]
        lines += [{"event": "record", "peer": {"pid": 5, "uid": 1000}, "rec": r} for r in recs]
        if self.task != "hill":
            lines.append({"event": "end", "records": len(recs), "malformed": 0, "unwritten": 0})
        for seq, line in enumerate(lines):
            line.update(seq=seq, wall=float(seq), mono=float(seq))
        return "".join(_json.dumps(line) + "\\n" for line in lines).encode()
'''  # fmt: skip


# Added beside the guard for a batch checked by all three: a process-visibility reading that is within the limit for
# every run but those of the task `river`, whose memory reading is unknown.
VISIBILITY = '''

from expcore.visibility import ProcessVisibility


class _StubSeen:
    def __init__(self, task):
        self.task = task

    def process_visibility(self):
        figures = dict.fromkeys(("margin_bytes", "start_unexplained_bytes", "unexplained_peak_bytes",
                                 "cgroup_anon_bytes", "visible_anon_bytes"), 1)
        unknown = self.task == "river"
        return {"version": 1, "memory": "unknown" if unknown else "within",
                "unknown": ["memory_unreadable"] if unknown else [], "hidden_process": 0, "hidden_memory": 0,
                "figures": figures}
'''  # fmt: skip


def prepared(
    tmp_path: Path, *, guard: bool, egress: bool = False, rest_task: str | None = None, visibility: bool = False
) -> Path:
    study = tmp_path / "study"
    shutil.copytree(EXAMPLE, study, ignore=shutil.ignore_patterns("out", "__pycache__"))
    if guard:
        plugin = study / "plugin.py"
        text = plugin.read_text(encoding="utf-8")
        assert INSTRUMENTS in text
        added = "instruments = [FactsServed(shelf), MountGuard(_StubWatch(factors['task']), _NONCE)]"
        if rest_task is not None:  # a campaign that mixes data paths: this task's runs are REST runs, with no mount
            added = (
                f"instruments = [FactsServed(shelf), NoMount('rest') if factors['task'] == {rest_task!r} "
                "else MountGuard(_StubWatch(factors['task']), _NONCE)]"
            )
        if egress:
            added = added[:-1] + ", EgressLog(_StubEgress(factors['task']), declaration([_GATE], _GATE), _NONCE)]"
        if visibility:
            added = added[:-1] + ", ProcessVisibility(_StubSeen(factors['task']))]"
        extra = GUARD + (EGRESS if egress else "") + (VISIBILITY if visibility else "")
        plugin.write_text(text.replace(INSTRUMENTS, added) + extra, encoding="utf-8")
    write_stamp(study, HEAD, load_study(study).scope)
    summary = run_study(study)
    assert summary.statuses == {"done": len(load_study(study).manifest.rows)}, summary.statuses
    return study


def checked_study(
    tmp_path, environment, *, guard: bool, egress: bool = False, rest_task: str | None = None, visibility: bool = False
):
    study = prepared(tmp_path, guard=guard, egress=egress, rest_task=rest_task, visibility=visibility)
    code, stderr = cli(environment, "check", str(study / "out" / "runs"), process=True)
    assert code == 0, stderr
    return analyse_study(study)


@pytest.fixture
def analysed(tmp_path, clean_environment):
    return checked_study(tmp_path, clean_environment, guard=True)


def test_every_analysed_run_of_a_guarded_study_is_judged_from_its_record(analysed):
    """O60: `lake`'s runs touched the token's directory, `hill`'s logs were never closed, the rest are clean."""
    verdicts = {run.verdict for run in analysed.mount_guard.runs}
    assert verdicts == {"clean", "flagged", "unknown"}
    assert len(analysed.mount_guard.runs) == len(analysed.rows)
    assert analysed.egress is None  # one detector does not make a batch checked by the other


def test_the_sensitivity_analysis_leaves_out_every_run_that_is_not_clean(analysed):
    assert analysed.mount_guard.left_out
    assert analysed.mount_guard.sensitivity is not None
    primary = {r.name: r.n_pairs for r in analysed.result.contrasts}
    again = {r.name: r.n_pairs for r in analysed.mount_guard.sensitivity.contrasts}
    assert set(primary) == set(again)
    assert all(again[name] <= primary[name] for name in primary)
    assert any(again[name] < primary[name] for name in primary)


def test_analysis_json_and_the_report_carry_the_verdicts_and_the_sensitivity_analysis(analysed):
    made = output_bytes(analysed)
    summary = json.loads(made[ANALYSIS_FILE])
    assert summary["mount_guard"]["counts"]["left_out"] == len(analysed.mount_guard.left_out)
    assert {run["run_key"] for run in summary["mount_guard"]["runs"]} == {row.run_key for row in analysed.rows}
    assert summary["mount_guard"]["sensitivity"]["left_out"]
    report = made[REPORT_FILE].decode()
    assert "## Mount isolation (detected, not prevented)" in report
    assert "#### Preregistered contrasts without the runs that are not clean" in report


def test_a_study_without_the_instrument_has_no_mount_guard_anywhere(tmp_path, clean_environment):
    """A batch whose mount was not guarded: the analysis, analysis.json and the report are as before."""
    analysis = checked_study(tmp_path, clean_environment, guard=False)
    made = output_bytes(analysis)
    assert analysis.mount_guard is None
    assert "mount_guard" not in json.loads(made[ANALYSIS_FILE])
    assert b"Mount isolation" not in made[REPORT_FILE]


def test_a_study_checked_by_both_guards_reruns_without_the_runs_either_leaves_out(tmp_path, clean_environment):
    """O60: `lake`'s runs touched the token's directory (the mount guard leaves them out), `hill`'s mount and egress
    logs were never closed (both leave them out); the combined rerun leaves out every one of them, and analysis.json,
    the report and the terminal summary carry it beside the two single-guard reruns."""
    analysis = checked_study(tmp_path, clean_environment, guard=True, egress=True)
    both = analysis.guards
    assert both is not None
    out = {run.run_key for run in both.left_out}
    assert out == {run.run_key for run in analysis.mount_guard.left_out} | {
        run.run_key for run in analysis.egress.flagged
    }
    assert {run.by for run in both.left_out} == {"mount", "both"}
    made = output_bytes(analysis)
    summary = json.loads(made[ANALYSIS_FILE])
    assert summary["guards"]["counts"]["left_out"] == len(out)
    assert set(summary["guards"]["by_condition"]) == {row.condition for row in analysis.rows}
    assert summary["guards"]["sensitivity"]["left_out"]
    report = made[REPORT_FILE].decode()
    assert (
        report.index("## Egress (detected") < report.index("## Mount isolation") < report.index("## Egress and mount")
    )
    assert "#### Preregistered contrasts without the runs either guard leaves out" in report


def test_o69_a_campaign_that_mixes_data_paths_keeps_its_rest_runs_in_the_mount_guards_rerun(
    tmp_path, clean_environment
):
    """O69: `river`'s runs are REST runs whose record says they had no mount, `lake`'s mount runs touched the token's
    directory, `hill`'s logs were never closed and the rest are clean: the REST runs are not applicable, are kept in the
    rerun and are shown so."""
    analysis = checked_study(tmp_path, clean_environment, guard=True, rest_task="river")
    runs = {row.run_key: row for row in analysis.rows}
    verdicts = {run.run_key: run.verdict for run in analysis.mount_guard.runs}
    river = {key for key, row in runs.items() if row.factors["task"] == "river"}
    assert river
    assert {verdicts[key] for key in river} == {"not_applicable"}
    assert {verdicts[key] for key in set(runs) - river} == {"clean", "flagged", "unknown"}  # the mount runs
    assert not river & {run.run_key for run in analysis.mount_guard.left_out}
    assert analysis.mount_guard.sensitivity is not None
    made = output_bytes(analysis)
    summary = json.loads(made[ANALYSIS_FILE])["mount_guard"]
    assert summary["counts"]["not_applicable"] == len(river)
    assert not river & set(summary["sensitivity"]["left_out"])
    assert "Not applicable" in made[REPORT_FILE].decode()


def test_o70_a_study_checked_by_all_three_guards_reruns_without_the_runs_any_of_them_leaves_out(
    tmp_path, clean_environment
):
    """O70: the process-visibility check joins the combined rerun: `river`'s runs, whose memory reading is unknown, are
    left out of it beside the runs the mount guard and egress leave out, and analysis.json, the report and the
    terminal carry it."""
    analysis = checked_study(tmp_path, clean_environment, guard=True, egress=True, visibility=True)
    both = analysis.guards
    assert both is not None
    assert both.guards == ("egress", "mount", "visibility")
    out = {run.run_key for run in both.left_out}
    assert out == (
        {run.run_key for run in analysis.mount_guard.left_out}
        | {run.run_key for run in analysis.egress.flagged}
        | {run.run_key for run in analysis.visibility.left_out}
    )
    assert analysis.visibility.left_out
    assert {run.by for run in both.left_out if run.visibility == "unknown"} >= {"visibility"}
    made = output_bytes(analysis)
    summary = json.loads(made[ANALYSIS_FILE])["guards"]
    assert summary["guards"] == ["egress", "mount", "visibility"]
    assert summary["counts"]["left_out"] == len(out)
    report = made[REPORT_FILE].decode()
    assert "## Egress, mount isolation and process visibility together" in report
    assert "#### Preregistered contrasts without the runs any guard leaves out" in report
