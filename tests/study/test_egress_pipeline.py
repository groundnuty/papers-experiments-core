"""Egress detection through the pipeline (#179): a study whose executor adds the egress instrument is run, checked in a
fresh process and analysed, and its analysis, analysis.json and report carry the verdicts and the sensitivity
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
# Added to the copy's plugin: an egress instrument over a stand-in collector log, the gate's canary first, and an
# outside connect in every run of the task `lake`; a run of `hill` has a log that was never closed.
EGRESS = '''

import json as _json

from expcore.egress import EgressLog, declaration

_GATE = ("127.0.0.1", 41234)
_DECLARED = declaration([_GATE], _GATE)
_NONCE = "00112233445566778899aabbccddeeff"


def _rec(op, address, port):
    result = 0 if op == "result" else None
    return {"op": op, "pid": 5, "fd": 3, "time": "1.000000000", "type": "tcp", "family": "inet",
            "address": address, "port": port, "result": result, "dropped": 0}


class _StubLog:
    def __init__(self, task):
        self.task = task

    def egress_log(self):
        recs = [_rec("connect", *_GATE), _rec("result", *_GATE)]
        if self.task == "lake":
            recs.append(_rec("connect", "192.0.2.1", 443))
        lines = [{"event": "start", "format": "hpcop-egress/1", "nonce": _NONCE, "pid": 7, "uid": 1000}]
        lines += [{"event": "record", "peer": {"pid": 5, "uid": 1000}, "rec": r} for r in recs]
        if self.task != "hill":
            lines.append({"event": "end", "records": len(recs), "malformed": 0, "unwritten": 0})
        for seq, line in enumerate(lines):
            line.update(seq=seq, wall=float(seq), mono=float(seq))
        return "".join(_json.dumps(line) + "\\n" for line in lines).encode()
'''  # fmt: skip


def prepared(tmp_path: Path, *, egress: bool) -> Path:
    study = tmp_path / "study"
    shutil.copytree(EXAMPLE, study, ignore=shutil.ignore_patterns("out", "__pycache__"))
    if egress:
        plugin = study / "plugin.py"
        text = plugin.read_text(encoding="utf-8")
        assert INSTRUMENTS in text
        added = "instruments = [FactsServed(shelf), EgressLog(_StubLog(factors['task']), _DECLARED, _NONCE)]"
        plugin.write_text(text.replace(INSTRUMENTS, added) + EGRESS, encoding="utf-8")
    write_stamp(study, HEAD, load_study(study).scope)
    summary = run_study(study)
    assert summary.statuses == {"done": len(load_study(study).manifest.rows)}, summary.statuses
    return study


def checked_study(tmp_path, environment, *, egress: bool):
    study = prepared(tmp_path, egress=egress)
    code, stderr = cli(environment, "check", str(study / "out" / "runs"), process=True)
    assert code == 0, stderr
    return analyse_study(study)


@pytest.fixture
def analysed(tmp_path, clean_environment):
    return checked_study(tmp_path, clean_environment, egress=True)


def test_every_analysed_run_of_a_checked_study_is_judged_from_its_record(analysed):
    """O52: `lake`'s runs connected outside, `hill`'s logs were never closed, the rest are clean."""
    flags = {run.flags for run in analysed.egress.runs}
    assert ("outside",) in flags
    assert ("log_unknown",) in flags
    assert () in flags
    assert len(analysed.egress.runs) == len(analysed.rows)


def test_the_sensitivity_analysis_leaves_out_every_flagged_run(analysed):
    left = {run.run_key for run in analysed.egress.flagged}
    assert left
    assert analysed.egress.sensitivity is not None
    primary = {r.name: r.n_pairs for r in analysed.result.contrasts}
    again = {r.name: r.n_pairs for r in analysed.egress.sensitivity.contrasts}
    assert set(primary) == set(again)
    assert all(again[name] <= primary[name] for name in primary)
    assert any(again[name] < primary[name] for name in primary)


def test_analysis_json_and_the_report_carry_the_verdicts_and_the_sensitivity_analysis(analysed):
    made = output_bytes(analysed)
    summary = json.loads(made[ANALYSIS_FILE])
    assert summary["egress"]["counts"]["flagged"] == len(analysed.egress.flagged)
    assert {run["run_key"] for run in summary["egress"]["runs"]} == {row.run_key for row in analysed.rows}
    assert summary["egress"]["sensitivity"]["left_out"]
    report = made[REPORT_FILE].decode()
    assert "## Egress (detected, not prevented)" in report
    assert "192.0.2.1:443/tcp x1" in report
    assert "#### Preregistered contrasts without the flagged runs" in report


def test_a_study_without_the_egress_instrument_has_no_egress_anywhere(tmp_path, clean_environment):
    """A batch not checked for egress: the analysis, analysis.json and the report are as before."""
    analysis = checked_study(tmp_path, clean_environment, egress=False)
    made = output_bytes(analysis)
    assert analysis.egress is None
    assert "egress" not in json.loads(made[ANALYSIS_FILE])
    assert b"Egress" not in made[REPORT_FILE]
