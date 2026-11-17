"""C40: the secrets of a run are declared before its instruments start, so a failed start's diagnostic is redacted
with every one of them.

An executor starts its instruments before `record_run`, so a secret it names only to `record_run(secrets=...)` is not
known when a start fails: its diagnostic would be kept with the value in it, on the row, in the marker, in the export.
`RunRequest.declare_secrets(secrets=, secret_env=, environ=)` is the executor's way to tell the core first: the
attempt's redaction context (the one its diagnostic and its harness-defect log are redacted with) then holds them, and
`record_run` merges them with what it is given, so the record is redacted with them too.
"""

import json
from pathlib import Path

import pytest
from loop_support import FakeProvider
from record_support import CODE
from scheduler_support import FakeExecutor, build, settings_for

from expcore.campaign.executor import record_run
from expcore.campaign.export import export_run_state
from expcore.campaign.markers import read_marker
from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore.models.adapters import ModelTurn, ToolCall
from expcore.records.instruments import start_instruments
from expcore.report import render_report
from expcore.stats.analysis import AnalysisResult

PLAN = {"models": ("m1",), "tasks": ("task-a",), "seeds": (1,)}
SECRET = "run-secret-known-only-to-the-study-0123"
STAMP = {"commit": "c" * 40, "tree_sha256": "d" * 64, "dirty": False, "why": None}


class Inducer:
    """An instrument whose failing start quotes what it saw, a secret of the run among it."""

    namespace = "state_inducer"

    def start(self, identity):
        raise InfraError("sandbox", "refused", diagnostic={"seen": f"the endpoint answered with {SECRET}"})

    def finish(self):
        return {}


def run_failing_start(root, declare):
    """A campaign whose executor runs `declare(request)`, then starts an instrument that fails quoting the secret;
    return the first attempt's row, its marker and the export."""

    async def executor(request):
        declare(request)
        start_instruments([Inducer()], {"run_key": request.run_key})

    settings = settings_for(max_attempts=1)
    with build(root, plan=PLAN, executor=FakeExecutor(default=executor), settings=settings) as scheduler:
        scheduler.run()
        row = dict(scheduler.store.q("SELECT * FROM attempts ORDER BY id")[0])
        marker = read_marker(Path(row["run_dir"]), row["run_key"], row["id"])
        return row, marker, export_run_state(scheduler.store)


def where_it_is_kept(row, marker, export):
    """The places a diagnostic is kept, as the text each holds."""
    return {"row": row["diagnostic"], "marker": json.dumps(marker.diagnostic), "export": json.dumps(export)}


def report_of(export):
    result = AnalysisResult((), (), 0.05, {"preregistration_sha256": "e" * 64})
    return render_report(result, export, study="a-study", code=STAMP, core_version="1.2.3")


def test_c40_a_secret_the_study_declares_before_its_instruments_start_is_redacted_from_a_failed_starts_diagnostic(
    tmp_path,
):
    """C40: declared first, a secret that only the study knows is gone from the row, the marker, the export and the
    report, whatever the instrument quoted; the attempt is set aside and charged as it was."""
    row, marker, export = run_failing_start(tmp_path / "out", lambda request: request.declare_secrets(secrets=[SECRET]))
    kept = where_it_is_kept(row, marker, export)
    assert {place: SECRET in text for place, text in kept.items()} == {"row": False, "marker": False, "export": False}
    assert SECRET not in report_of(export)
    assert "[redacted]" in row["diagnostic"]
    assert (row["state"], row["charged"], row["infra_kind"]) == ("infra_fail", 1, "sandbox")


def test_c40_without_the_declaration_a_secret_only_record_run_would_name_is_not_known_when_the_start_fails(tmp_path):
    """C40: the contrast that says what the declaration is for: record_run has not been called yet, so the same run
    without `declare_secrets` keeps the value in the stored diagnostic (the documented limit: declare first, or
    register the secret with `register_secret`)."""
    row, _, _ = run_failing_start(tmp_path / "out", lambda request: None)
    assert SECRET in row["diagnostic"]


def test_c40_a_secret_variable_and_an_environment_the_study_declares_are_redacted_too(tmp_path):
    """C40: `secret_env` names variables whose values are secrets whatever their names, and `environ` is the mapping
    they are read from, as `record_run` takes them; both reach the diagnostic before the start."""

    def declare(request):
        request.declare_secrets(secret_env=["STUDY_PLAIN_NAME"], environ={"STUDY_PLAIN_NAME": SECRET})

    row, marker, export = run_failing_start(tmp_path / "out", declare)
    assert not any(SECRET in text for text in where_it_is_kept(row, marker, export).values())


def test_c40_a_secret_too_short_to_redact_is_refused_at_the_declaration_naming_the_variable(tmp_path):
    """C40: as `record_attempt` refuses it before the run, the declaration does: a ConfigError that names the variable
    and never shows the value, raised in the executor, before any instrument starts."""
    seen = []

    def declare(request):
        with pytest.raises(ConfigError, match="SHORT_ONE") as refused:
            request.declare_secrets(secret_env=["SHORT_ONE"], environ={"SHORT_ONE": "zq"})
        seen.append(str(refused.value))

    run_failing_start(tmp_path / "out", declare)
    assert seen
    assert "zq" not in seen[0]


def test_c40_what_the_study_declares_reaches_the_record_record_run_builds_with_no_secrets_of_its_own(tmp_path):
    """C40: `record_run` merges the declared secrets with its own `secrets` and `secret_env`, so a study that declared
    first and passes nothing again gets a record redacted with them; undeclared, the value would stay in the record."""
    texts = {}

    def executor_for(name, declare):
        async def executor(request):
            declare(request)
            script = [ModelTurn("", [ToolCall("call_one", "look", {})], 10, 5, "tool_calls", 0.0)]
            request.backend.use([*script, ModelTurn("done", [], 10, 5, "stop", 0.0)])
            providers = [FakeProvider({"look": lambda arguments: f"the answer carried {SECRET}"})]
            record = await record_run(
                request, providers, "be brief", "do the task", FakeExecutor().config, code=dict(CODE)
            )
            texts[name] = record.dumps()
            return record

        return executor

    for name, declare in (
        ("declared", lambda r: r.declare_secrets(secrets=[SECRET])),
        ("not declared", lambda r: None),
    ):
        with build(
            tmp_path / name,
            plan=PLAN,
            executor=FakeExecutor(default=executor_for(name, declare)),
            settings=settings_for(),
        ) as scheduler:
            scheduler.run()
    assert SECRET not in texts["declared"]
    assert SECRET in texts["not declared"]  # the control: the secret is in the answer, and nothing else redacts it


def test_c40_a_secret_the_study_gives_twice_is_declared_once_and_the_two_ways_agree(tmp_path):
    """C40: declaring a secret and passing it to `record_run` again is the same secret, not a refusal and not two."""
    row, marker, export = run_failing_start(
        tmp_path / "out",
        lambda request: (request.declare_secrets(secrets=[SECRET]), request.declare_secrets(secrets=[SECRET])),
    )
    assert not any(SECRET in text for text in where_it_is_kept(row, marker, export).values())
    assert row["diagnostic"].count("[redacted]") == 1
