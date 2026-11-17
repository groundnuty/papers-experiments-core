"""S20: the read-log scan through the pipeline: a study whose agent has a code tool and whose executor adds the
`read_log` instrument is run, checked in a fresh process and analysed, and its analysis, analysis.json and report carry
the verdicts and the sensitivity analysis; a study without the instrument gets none of them, and an analysis whose
reruns are not those the preregistration declares is refused."""

import json
import shutil
from pathlib import Path

import pytest
from study_support import HEAD, clean_environment, cli

from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study
from expcore.study.pipeline import ANALYSIS_FILE, REPORT_FILE, Refused, analyse_study, output_bytes, run_study

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
INSTRUMENTS = "instruments = [FactsServed(shelf)]"
# Added to the copy's plugin: a code tool beside the others, a read-log instrument (a stand-in that only names its
# namespace: the scan reads the record's code, not the log), and, in every run of the task `lake`, a first turn whose
# code sends a datagram to the channel's socket.
READ_LOG = '''

from expcore.plugin import ToolSpec as _ToolSpec

_FORGER = (
    "import socket\\ns = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)\\n"
    "s.sendto(b'{}', '/expcore-readlog/sock')"
)
_tool_specs = tool_specs
_script_for = script_for


def tool_specs(tools):
    code = {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}
    return [*_tool_specs(tools), _ToolSpec("python", "Run code.", code)]


def provider_for(tools, shelf):
    lookup_spec, submit_spec, code_spec = tool_specs(tools)
    return FunctionProvider(
        [(lookup_spec, shelf.lookup), (submit_spec, submit), (code_spec, lambda arguments, *, timeout_s: "ok")]
    )


def script_for(condition, task, model, tools):
    turns = _script_for(condition, task, model, tools)
    return [[("python", {"code": _FORGER})], *turns] if task == "lake" else turns


class _ReadLogStub:
    namespace = "read_log"

    def start(self, identity):
        pass

    def finish(self):
        return {"version": 1, "log": "intact", "side_sha256": None}
'''  # fmt: skip


def prepared(tmp_path: Path, *, read_log: bool, declare: bool | None = None) -> Path:
    """A stamped copy of the example, with the read-log instrument and code tool when asked, and the rerun declared
    (by default when the instrument is added)."""
    study = tmp_path / "study"
    shutil.copytree(EXAMPLE, study, ignore=shutil.ignore_patterns("out", "__pycache__"))
    if read_log if declare is None else declare:
        declared = study / "preregistration.toml"
        declared.write_text(declared.read_text("utf-8") + '\n[sensitivity]\nreruns = ["read_log"]\n', "utf-8")
    if read_log:
        plugin = study / "plugin.py"
        text = plugin.read_text(encoding="utf-8")
        assert INSTRUMENTS in text
        added = "instruments = [FactsServed(shelf), _ReadLogStub()]"
        plugin.write_text(text.replace(INSTRUMENTS, added) + READ_LOG, encoding="utf-8")
    write_stamp(study, HEAD, load_study(study).scope)
    summary = run_study(study)
    assert summary.statuses == {"done": len(load_study(study).manifest.rows)}, summary.statuses
    return study


def checked(tmp_path, environment, **options):
    study = prepared(tmp_path, **options)
    code, stderr = cli(environment, "check", str(study / "out" / "runs"), process=True)
    assert code == 0, stderr
    return study


@pytest.fixture
def analysed(tmp_path, clean_environment):
    return analyse_study(checked(tmp_path, clean_environment, read_log=True))


def test_s20_every_analysed_run_of_a_checked_study_is_judged_from_its_record(analysed):
    """S20: `lake`'s runs sent a datagram to the channel's socket from their code cell, the rest ran no code."""
    flagged = {run.run_key for run in analysed.read_log.flagged}
    assert flagged
    assert len(analysed.read_log.runs) == len(analysed.rows)
    assert all(run.condition for run in analysed.read_log.runs)
    kinds = {run.kinds for run in analysed.read_log.flagged}
    assert kinds == {("channel_path", "unix_datagram")}
    assert len(flagged) < len(analysed.rows)


def test_s20_the_sensitivity_analysis_leaves_out_every_flagged_run(analysed):
    left = {run.run_key for run in analysed.read_log.flagged}
    assert left
    assert analysed.read_log.sensitivity is not None
    primary = {r.name: r.n_pairs for r in analysed.result.contrasts}
    again = {r.name: r.n_pairs for r in analysed.read_log.sensitivity.contrasts}
    assert set(primary) == set(again)
    assert all(again[name] <= primary[name] for name in primary)
    assert any(again[name] < primary[name] for name in primary)


def test_s20_analysis_json_and_the_report_carry_the_verdicts_and_the_sensitivity_analysis(analysed):
    made = output_bytes(analysed)
    summary = json.loads(made[ANALYSIS_FILE])
    assert summary["read_log"]["counts"]["flagged"] == len(analysed.read_log.flagged)
    assert {run["run_key"] for run in summary["read_log"]["runs"]} == {row.run_key for row in analysed.rows}
    assert summary["read_log"]["sensitivity"]["left_out"]
    assert summary["sensitivity_reruns"] == ["read_log"]
    report = made[REPORT_FILE].decode()
    assert "## Read log: runs that touched the channel (detected, not prevented)" in report
    assert "channel_path /expcore-readlog/sock (cell 0)" in report
    assert "#### Preregistered contrasts without the runs that touched the read-log channel" in report


def test_s20_a_study_without_the_read_log_instrument_has_no_read_log_anywhere(tmp_path, clean_environment):
    """A batch the scan did not check: the analysis, analysis.json and the report are as before."""
    analysis = analyse_study(checked(tmp_path, clean_environment, read_log=False))
    made = output_bytes(analysis)
    assert analysis.read_log is None
    assert "read_log" not in json.loads(made[ANALYSIS_FILE])
    assert b"Read log" not in made[REPORT_FILE]


def test_s20_an_analysis_whose_read_log_rerun_is_not_declared_is_refused(tmp_path, clean_environment):
    study = checked(tmp_path, clean_environment, read_log=True, declare=False)
    with pytest.raises(Refused, match="run but not declared: read_log"):
        analyse_study(study)


def test_s20_a_declared_read_log_rerun_no_record_supports_is_refused(tmp_path, clean_environment):
    study = checked(tmp_path, clean_environment, read_log=False, declare=True)
    with pytest.raises(Refused, match="declared but not run: read_log"):
        analyse_study(study)
