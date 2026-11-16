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

from expcore.mountguard import MountGuard

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


def prepared(tmp_path: Path, *, guard: bool) -> Path:
    study = tmp_path / "study"
    shutil.copytree(EXAMPLE, study, ignore=shutil.ignore_patterns("out", "__pycache__"))
    if guard:
        plugin = study / "plugin.py"
        text = plugin.read_text(encoding="utf-8")
        assert INSTRUMENTS in text
        added = "instruments = [FactsServed(shelf), MountGuard(_StubWatch(factors['task']), _NONCE)]"
        plugin.write_text(text.replace(INSTRUMENTS, added) + GUARD, encoding="utf-8")
    write_stamp(study, HEAD, load_study(study).scope)
    summary = run_study(study)
    assert summary.statuses == {"done": len(load_study(study).manifest.rows)}, summary.statuses
    return study


def checked_study(tmp_path, environment, *, guard: bool):
    study = prepared(tmp_path, guard=guard)
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
