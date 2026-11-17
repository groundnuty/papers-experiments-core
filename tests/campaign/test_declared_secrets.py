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

from expcore.campaign.defect_log import log_path
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
    """An instrument whose failing start quotes what it saw, a secret of the run among it (and others, if told)."""

    namespace = "state_inducer"

    def __init__(self, *quoted):
        self.quoted = quoted or (SECRET,)

    def start(self, identity):
        seen = " and ".join(f"the endpoint answered with {value}" for value in self.quoted)
        raise InfraError("sandbox", "refused", diagnostic={"seen": seen})

    def finish(self):
        return {}


def run_failing_start(root, declare, *quoted):
    """A campaign whose executor runs `declare(request)`, then starts an instrument that fails quoting the secret (or
    the values given); return the first attempt's row, its marker and the export."""

    async def executor(request):
        declare(request)
        start_instruments([Inducer(*quoted)], {"run_key": request.run_key})

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


def test_c40_a_secret_named_only_to_record_run_never_reaches_the_row_the_marker_the_export_or_the_report(tmp_path):
    """C40: the rule: never a diagnostic redacted against a partial list of the run's secrets. The executor names a
    secret only to `record_run`, which a start that fails never reaches: the stored diagnostic, the marker, the export
    and the report hold no trace of it, the diagnostic is withheld (it says why), and the failed start is still counted
    by instrument; the attempt is set aside and charged as it was."""
    ran = []

    async def executor(request):
        start_instruments([Inducer()], {"run_key": request.run_key})  # fails, quoting the secret
        ran.append("record_run")  # never reached
        await record_run(request, [], "s", "u", FakeExecutor().config, code=dict(CODE), secrets=[SECRET])

    settings = settings_for(max_attempts=1)
    with build(tmp_path / "out", plan=PLAN, executor=FakeExecutor(default=executor), settings=settings) as scheduler:
        scheduler.run()
        row = dict(scheduler.store.q("SELECT * FROM attempts ORDER BY id")[0])
        marker = read_marker(Path(row["run_dir"]), row["run_key"], row["id"])
        export = export_run_state(scheduler.store)
    assert not ran
    kept = where_it_is_kept(row, marker, export)
    assert {place: SECRET in text for place, text in kept.items()} == {"row": False, "marker": False, "export": False}
    report = report_of(export)
    assert SECRET not in report
    stored = json.loads(row["diagnostic"])
    assert (stored["phase"], stored["instrument"], stored["ok"], stored["error"]) == (
        "start",
        "state_inducer",
        False,
        "Withheld",
    )
    assert "diagnostic" not in stored
    assert "declare_secrets" in stored["why"]
    assert (row["state"], row["charged"], row["infra_kind"]) == ("infra_fail", 1, "sandbox")
    assert "| state_inducer | sandbox | infra_fail | 1 | 1 | 1 |" in report  # counted, and not kept


def test_c40_declaring_nothing_says_there_is_nothing_more_and_the_diagnostic_is_kept_redacted(tmp_path, monkeypatch):
    """C40: an executor with no secret beyond the environment's declares none (`declare_secrets()`): its redaction
    context is whole, so the diagnostic is kept, redacted of the environment's secrets."""
    monkeypatch.setenv("SOME_SERVICE_API_KEY", OTHER)
    row, marker, export = run_failing_start(tmp_path / "out", lambda request: request.declare_secrets(), OTHER)
    kept = json.loads(row["diagnostic"])
    assert kept["diagnostic"] == {"seen": "the endpoint answered with [redacted]"}
    assert OTHER not in json.dumps(where_it_is_kept(row, marker, export))


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


def record_texts(tmp_path, cases):
    """The text of the record each case's executor builds: it runs `declare(request)`, then `record_run` with the case's
    options, over a tool that answers with the secret."""
    texts = {}

    def executor_for(name, declare, options):
        async def executor(request):
            declare(request)
            script = [ModelTurn("", [ToolCall("call_one", "look", {})], 10, 5, "tool_calls", 0.0)]
            request.backend.use([*script, ModelTurn("done", [], 10, 5, "stop", 0.0)])
            providers = [FakeProvider({"look": lambda arguments: f"the answer carried {SECRET}"})]
            config = FakeExecutor().config
            record = await record_run(request, providers, "be brief", "do the task", config, code=dict(CODE), **options)
            texts[name] = record.dumps()
            return record

        return executor

    for name, (declare, options) in cases.items():
        executor = FakeExecutor(default=executor_for(name, declare, options))
        with build(tmp_path / name, plan=PLAN, executor=executor, settings=settings_for()) as scheduler:
            scheduler.run()
    return texts


def test_c40_what_the_study_declares_reaches_the_record_record_run_builds_with_no_secrets_of_its_own(tmp_path):
    """C40: `record_run` merges the declared secrets with its own `secrets`, so a study that declared first and passes
    nothing again gets a record redacted with them; undeclared, the value would stay in the record."""
    texts = record_texts(
        tmp_path,
        {"declared": (lambda r: r.declare_secrets(secrets=[SECRET]), {}), "not declared": (lambda r: None, {})},
    )
    assert SECRET not in texts["declared"]
    assert SECRET in texts["not declared"]  # the control: the secret is in the answer, and nothing else redacts it


def test_c40_a_secret_declared_and_passed_to_record_run_again_is_one_secret_and_both_ways_agree(tmp_path):
    """C40: declaring a secret twice, and passing it to `record_run` as well, is the same secret: no refusal, and the
    diagnostic and the record are redacted."""
    row, marker, export = run_failing_start(
        tmp_path / "out",
        lambda request: (request.declare_secrets(secrets=[SECRET]), request.declare_secrets(secrets=[SECRET])),
    )
    assert not any(SECRET in text for text in where_it_is_kept(row, marker, export).values())
    assert row["diagnostic"].count("[redacted]") == 1
    texts = record_texts(tmp_path, {"both": (lambda r: r.declare_secrets(secrets=[SECRET]), {"secrets": [SECRET]})})
    assert SECRET not in texts["both"]


OTHER = "an-api-key-the-environment-already-names-4567"


def test_c40_declaring_a_variable_with_a_partial_environment_never_narrows_what_was_redacted(tmp_path, monkeypatch):
    """C40: `environ` only says where the declared variables are read from: the process's secrets (a variable named
    like one, a model's key) are still redacted after it, never replaced by the mapping given."""
    monkeypatch.setenv("SOME_SERVICE_API_KEY", OTHER)

    def declare(request):
        request.declare_secrets(secret_env=["STUDY_PLAIN_NAME"], environ={"STUDY_PLAIN_NAME": SECRET})

    row, marker, export = run_failing_start(tmp_path / "out", declare, SECRET, OTHER)
    kept = where_it_is_kept(row, marker, export)
    assert {value: any(value in text for text in kept.values()) for value in (SECRET, OTHER)} == {
        SECRET: False,
        OTHER: False,
    }


def test_c40_a_declared_variables_value_is_kept_whatever_environment_record_run_is_given(tmp_path):
    """C40: the value of a declared variable is read when it is declared and kept as a secret, so a `record_run` with an
    environment of its own (which lacks the variable) redacts it from the record all the same."""
    texts = record_texts(
        tmp_path,
        {
            "own environ": (
                lambda r: r.declare_secrets(secret_env=["STUDY_PLAIN_NAME"], environ={"STUDY_PLAIN_NAME": SECRET}),
                {"environ": {"UNRELATED": "nothing"}},
            ),
        },
    )
    assert SECRET not in texts["own environ"]


def test_c40_a_second_declaration_with_another_environment_keeps_the_first_ones_values(tmp_path):
    """C40: each declaration reads its variables from its own environment when it is made, so a later one never takes
    an earlier one's values away."""

    def declare(request):
        request.declare_secrets(secret_env=["FIRST_NAME"], environ={"FIRST_NAME": SECRET})
        request.declare_secrets(secret_env=["SECOND_NAME"], environ={"SECOND_NAME": OTHER})

    row, marker, export = run_failing_start(tmp_path / "out", declare, SECRET, OTHER)
    kept = where_it_is_kept(row, marker, export)
    assert not any(value in text for value in (SECRET, OTHER) for text in kept.values())


def test_c40_a_string_is_one_secret_and_one_variable_name_not_its_characters(tmp_path):
    """C40: `secrets="value"` and `secret_env="NAME"` are one value and one name, as a reader of the call expects, not
    their characters (which would redact every letter, or refuse the call as too short)."""

    def declare(request):
        request.declare_secrets(secrets=SECRET, secret_env="STUDY_PLAIN_NAME", environ={"STUDY_PLAIN_NAME": OTHER})

    row, marker, export = run_failing_start(tmp_path / "out", declare, SECRET, OTHER)
    assert not any(
        value in text for value in (SECRET, OTHER) for text in where_it_is_kept(row, marker, export).values()
    )
    assert "answered with [redacted] and the endpoint answered with [redacted]" in row["diagnostic"]


def test_c40_a_refused_declaration_withholds_the_traceback_it_would_have_logged_with_the_secret_in_it(tmp_path):
    """C40: as `record_attempt` installs the attempt's redactor before it checks it, a declaration refused for a secret
    too short to redact leaves the harness-defect log with a redactor that cannot be built: the traceback of the
    ConfigError that escapes the executor, whose source line holds the value, is withheld and not written, and so is a
    diagnostic after it (recorded as failed)."""

    async def executor(request):
        request.declare_secrets(secrets=["zq"])  # too short: refused, and not caught

    root = tmp_path / "out"
    with build(
        root, plan=PLAN, executor=FakeExecutor(default=executor), settings=settings_for(max_attempts=1)
    ) as sched:
        sched.run()
        states = {row["state"] for row in sched.store.q("SELECT state FROM attempts")}
    written = log_path(root).read_text(encoding="utf-8")
    assert states == {"harness_error"}
    assert "withheld" in written
    assert "zq" not in written


def test_c40_the_environment_an_executor_declares_is_kept_as_it_is_redacted_from_a_record(tmp_path):
    """C40: `environ` is the mapping the executor gives `record_run`, which redacts the variables of it named like a
    secret: declared alone, with no names, the same variables are kept as secrets, so a failed start's diagnostic
    loses them as the record does, and a short one is refused as it is there."""
    row, marker, export = run_failing_start(
        tmp_path / "out", lambda request: request.declare_secrets(environ={"SANDBOX_AUTH_TOKEN": SECRET, "PLAIN": "x"})
    )
    assert not any(SECRET in text for text in where_it_is_kept(row, marker, export).values())
    seen = []

    def refused(request):
        with pytest.raises(ConfigError, match="SANDBOX_AUTH_TOKEN") as error:
            request.declare_secrets(environ={"SANDBOX_AUTH_TOKEN": "zq"})
        seen.append(str(error.value))

    run_failing_start(tmp_path / "again", refused)
    assert seen
    assert "zq" not in seen[0]


def test_c40_a_withheld_diagnostic_keeps_no_instrument_name_that_is_a_secret(tmp_path, monkeypatch):
    """C40: withholding keeps the phase and the instrument, and those go through the redactor too: an instrument whose
    name is the value of a secret of the environment is `[redacted]` in the row, the marker and the export, as it was
    before diagnostics were withheld."""
    monkeypatch.setenv("EXAMPLE_API_KEY", "probe_credential_1234")

    class Named(Inducer):
        namespace = "probe_credential_1234"

    async def executor(request):
        start_instruments([Named()], {"run_key": request.run_key})  # never declares: the diagnostic is withheld

    with build(
        tmp_path / "out", plan=PLAN, executor=FakeExecutor(default=executor), settings=settings_for(max_attempts=1)
    ) as scheduler:
        scheduler.run()
        row = dict(scheduler.store.q("SELECT * FROM attempts ORDER BY id")[0])
        marker = read_marker(Path(row["run_dir"]), row["run_key"], row["id"])
        export = export_run_state(scheduler.store)
    assert json.loads(row["diagnostic"])["error"] == "Withheld"
    assert "probe_credential_1234" not in json.dumps(where_it_is_kept(row, marker, export))
    assert json.loads(row["diagnostic"])["instrument"] == "[redacted]"


def test_c40_the_processs_secrets_stay_redacted_when_record_run_is_given_an_environment_of_its_own(
    tmp_path, monkeypatch
):
    """C40: `declare_secrets()` keeps the process's own secrets as values too, so a `record_run` that is given another
    environment (which replaces the redactor `record_attempt` installs, built from that environment alone) still
    redacts them: from the record, and from the diagnostic of an error raised after it."""
    credential = "process_credential_1234"
    monkeypatch.setenv("EXAMPLE_API_KEY", credential)
    seen = {}

    async def executor(request):
        request.declare_secrets()
        script = [ModelTurn("", [ToolCall("call_one", "look", {})], 10, 5, "tool_calls", 0.0)]
        request.backend.use([*script, ModelTurn("done", [], 10, 5, "stop", 0.0)])
        providers = [FakeProvider({"look": lambda arguments: f"the answer carried {credential}"})]
        record = await record_run(
            request, providers, "s", "u", FakeExecutor().config, code=dict(CODE), environ={"UNRELATED": "nothing"}
        )
        seen["record"] = record.dumps(environ={})  # the record as built: no environment applied at the write
        raise InfraError("sandbox", "post-run check refused", diagnostic={"seen": f"it quoted {credential}"})

    with build(
        tmp_path / "out", plan=PLAN, executor=FakeExecutor(default=executor), settings=settings_for(max_attempts=1)
    ) as scheduler:
        scheduler.run()
        row = dict(scheduler.store.q("SELECT * FROM attempts ORDER BY id")[0])
    assert credential not in seen["record"]
    assert credential not in row["diagnostic"]
    assert "[redacted]" in row["diagnostic"]


def test_c40_an_undeclared_executor_whose_record_run_has_an_environment_of_its_own_still_redacts_the_processs_secrets(
    tmp_path, monkeypatch
):
    """C40: `record_attempt` installs on the log a redactor built from the environment `record_run` is given and says
    the context is whole; the diagnostic of an error raised after it is redacted with the runner's own base too (the
    process, the models, the registry), so it is never redacted against a partial list, declared or not."""
    credential = "process_credential_5678"
    monkeypatch.setenv("EXAMPLE_API_KEY", credential)

    async def executor(request):
        script = [ModelTurn("done", [], 10, 5, "stop", 0.0)]
        request.backend.use(script)
        config = FakeExecutor().config
        await record_run(request, [], "s", "u", config, code=dict(CODE), environ={"UNRELATED": "nothing"})
        raise InfraError("sandbox", "post-run check refused", diagnostic={"seen": f"it quoted {credential}"})

    with build(
        tmp_path / "out", plan=PLAN, executor=FakeExecutor(default=executor), settings=settings_for(max_attempts=1)
    ) as scheduler:
        scheduler.run()
        row = dict(scheduler.store.q("SELECT * FROM attempts ORDER BY id")[0])
        marker = read_marker(Path(row["run_dir"]), row["run_key"], row["id"])
        export = export_run_state(scheduler.store)
    assert json.loads(row["diagnostic"])["diagnostic"] == {"seen": "it quoted [redacted]"}
    assert credential not in json.dumps(where_it_is_kept(row, marker, export))
