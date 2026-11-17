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
from expcore.campaign.start_diagnostic import WITHHELD
from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore.models.adapters import ModelTurn, ToolCall
from expcore.records.instruments import start_instruments
from expcore.records.redact import Redactor
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


class SilentInducer:
    """An instrument whose start fails and gives no diagnostic: nothing was seen that it would say."""

    namespace = "state_inducer"

    def start(self, identity):
        raise InfraError("sandbox", "refused")

    def finish(self):
        return {}


def run_failing_start(root, declare, *quoted, instrument=None):
    """A campaign whose executor runs `declare(request)`, then starts an instrument that fails quoting the secret (or
    the values given), or `instrument()` when one is given; return the first attempt's row, its marker and the
    export."""

    async def executor(request):
        declare(request)
        start_instruments([Inducer(*quoted) if instrument is None else instrument()], {"run_key": request.run_key})

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


def test_c40_an_undeclared_executors_diagnostic_is_withheld_from_everything_that_ships_and_kept_in_the_local_log(
    tmp_path,
):
    """C40: the rule: never a diagnostic redacted against a partial list of the run's secrets. The executor names a
    secret only to `record_run`, which a failed start never reaches, and never declares: the row, the marker, the export
    (which a package carries) and the report hold no trace of the diagnostic, only that it was withheld and why; the
    failed start is still counted; the text is in the campaign's local log (`out/logs`, never merged or packaged)."""
    ran = []

    async def executor(request):
        start_instruments([Inducer()], {"run_key": request.run_key})  # fails, quoting the secret
        ran.append("record_run")  # never reached
        await record_run(request, [], "s", "u", FakeExecutor().config, code=dict(CODE), secrets=[SECRET])

    root = tmp_path / "out"
    settings = settings_for(max_attempts=1)
    with build(root, plan=PLAN, executor=FakeExecutor(default=executor), settings=settings) as scheduler:
        scheduler.run()
        row = dict(scheduler.store.q("SELECT * FROM attempts ORDER BY id")[0])
        marker = read_marker(Path(row["run_dir"]), row["run_key"], row["id"])
        export = export_run_state(scheduler.store)
    assert not ran
    shipped = where_it_is_kept(row, marker, export)
    assert {place: SECRET in text for place, text in shipped.items()} == {
        "row": False,
        "marker": False,
        "export": False,
    }
    assert not any("the endpoint answered" in text for text in shipped.values())  # not even the part that is no secret
    report = report_of(export)
    assert SECRET not in report
    assert "the endpoint answered" not in report
    stored = json.loads(row["diagnostic"])
    assert stored == {"phase": "start", "ok": False, "error": "Withheld", "why": WITHHELD}
    assert "no secrets" in stored["why"]
    assert export["attempt_diagnostics"][0]["diagnostic"] == stored  # what the export says, and all it says
    assert (row["state"], row["charged"], row["infra_kind"]) == ("infra_fail", 1, "sandbox")
    assert "| - | sandbox | infra_fail | 1 | 1 | 0 | 1 | 0 | 0 |" in report  # counted, no instrument, withheld
    local = log_path(root).read_text(encoding="utf-8")
    assert "the endpoint answered with" in local  # kept, where nothing ships
    assert "state_inducer" in local
    assert "withheld" in local


def test_c40_a_declared_empty_executors_diagnostic_is_shown_in_the_export_and_the_report(tmp_path):
    """C40: `declare_secrets()` says the run has no secret beyond the environment's: the diagnostic is kept, shown in
    the export, and the report counts it as kept (not withheld); nothing is written to the local log for it."""
    root = tmp_path / "out"
    row, _, export = run_failing_start(root, lambda request: request.declare_secrets())
    stored = json.loads(row["diagnostic"])
    assert stored["diagnostic"] == {
        "seen": f"the endpoint answered with {SECRET}"
    }  # no secret declared: nothing to hide
    assert export["attempt_diagnostics"][0]["diagnostic"] == stored
    assert "| state_inducer | sandbox | infra_fail | 1 | 1 | 1 | 0 | 0 | 0 |" in report_of(export)  # shown
    assert not log_path(root).exists()


@pytest.mark.parametrize(
    ("declare", "instrument", "row_of_the_report"),
    [
        (lambda request: None, "-", "| - | sandbox | infra_fail | 1 | 1 | 0 | 0 | 1 | 0 |"),
        (
            lambda request: request.declare_secrets(),
            "state_inducer",
            "| state_inducer | sandbox | infra_fail | 1 | 1 | 0 | 0 | 1 | 0 |",
        ),
    ],
    ids=["undeclared", "declared"],
)
def test_c40_a_failed_start_that_gave_no_diagnostic_is_counted_as_having_none_neither_withheld_nor_unkept(
    tmp_path, declare, instrument, row_of_the_report
):
    """C40: the count gap of the report: whatever the executor declared, a start that gave no diagnostic has nothing to
    show and nothing to withhold. The snapshot is the phase alone (the instrument too once the executor declared), the
    report counts it in the "No diagnostic" column and in no other, and no note is written to the local log."""
    root = tmp_path / "out"
    row, _, export = run_failing_start(root, declare, instrument=SilentInducer)
    stored = json.loads(row["diagnostic"])
    assert stored == ({"phase": "start"} if instrument == "-" else {"phase": "start", "instrument": instrument})
    assert export["attempt_diagnostics"][0]["diagnostic"] == stored
    assert row_of_the_report in report_of(export)
    assert not log_path(root).exists()


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


def test_c40_a_withheld_snapshot_keeps_no_instrument_name_a_secret_only_record_run_would_name(tmp_path):
    """C40: the instrument's name is the study's own word, and can be a secret only the executor knows (named to
    `record_run`, which a failed start never reaches): withheld, the snapshot keeps the phase alone, so the name is in
    no row, marker, export or report."""
    name = "executor_only_credential_0123"

    class Named(Inducer):
        namespace = name

    async def executor(request):
        start_instruments([Named()], {"run_key": request.run_key})  # never declares: the diagnostic is withheld
        await record_run(request, [], "s", "u", FakeExecutor().config, code=dict(CODE), secrets=[name])

    with build(
        tmp_path / "out", plan=PLAN, executor=FakeExecutor(default=executor), settings=settings_for(max_attempts=1)
    ) as scheduler:
        scheduler.run()
        row = dict(scheduler.store.q("SELECT * FROM attempts ORDER BY id")[0])
        marker = read_marker(Path(row["run_dir"]), row["run_key"], row["id"])
        export = export_run_state(scheduler.store)
    assert json.loads(row["diagnostic"]) == {"phase": "start", "ok": False, "error": "Withheld", "why": WITHHELD}
    assert name not in json.dumps(where_it_is_kept(row, marker, export))
    assert name not in report_of(export)


def test_c40_the_harness_defect_log_is_redacted_with_the_processs_secrets_whatever_environment_record_run_is_given(
    tmp_path, monkeypatch
):
    """C40: `record_attempt` installs on the log a redactor built from the environment `record_run` is given; the log
    keeps the runner's own base (the process, the models, the registry) beside it, so a crash after `record_run` whose
    message quotes a process secret is written with it redacted, declared or not."""
    credential = "process_credential_9012"
    monkeypatch.setenv("EXAMPLE_API_KEY", credential)

    async def executor(request):
        request.backend.use([ModelTurn("done", [], 10, 5, "stop", 0.0)])
        await record_run(
            request, [], "s", "u", FakeExecutor().config, code=dict(CODE), environ={"UNRELATED": "nothing"}
        )
        raise RuntimeError(f"the harness quoted {credential}")  # a defect of the study's own, after the record

    root = tmp_path / "out"
    with build(
        root, plan=PLAN, executor=FakeExecutor(default=executor), settings=settings_for(max_attempts=1)
    ) as scheduler:
        scheduler.run()
    written = log_path(root).read_text(encoding="utf-8")
    assert "the harness quoted" in written
    assert credential not in written


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


def test_c40_an_executor_that_declared_and_gave_record_run_an_environment_of_its_own_keeps_the_processs_secrets_out(
    tmp_path, monkeypatch
):
    """C40: declared, and `record_attempt` then installs on the log a redactor built from the environment `record_run`
    is given: the diagnostic of an error raised after it is still redacted with the runner's own base too (the
    process, the models, the registry), so it is never redacted against the shorter list of the two."""
    credential = "process_credential_5678"
    monkeypatch.setenv("EXAMPLE_API_KEY", credential)

    async def executor(request):
        request.declare_secrets()
        request.backend.use([ModelTurn("done", [], 10, 5, "stop", 0.0)])
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


def test_c40_record_run_alone_is_not_a_declaration_so_an_error_after_it_is_withheld_too(tmp_path):
    """C40: only `declare_secrets` shows a diagnostic in the export: an executor that built the attempt's context
    through `record_run` but never declared has its diagnostic withheld, as one that failed before `record_run` does."""

    async def executor(request):
        request.backend.use([ModelTurn("done", [], 10, 5, "stop", 0.0)])
        await record_run(request, [], "s", "u", FakeExecutor().config, code=dict(CODE), secrets=[SECRET])
        raise InfraError("sandbox", "post-run check refused", diagnostic={"seen": f"it quoted {SECRET}"})

    with build(
        tmp_path / "out", plan=PLAN, executor=FakeExecutor(default=executor), settings=settings_for(max_attempts=1)
    ) as scheduler:
        scheduler.run()
        row = dict(scheduler.store.q("SELECT * FROM attempts ORDER BY id")[0])
    # an error raised after `record_run` names no instrument and no phase: nothing but that it was withheld, and why
    assert json.loads(row["diagnostic"]) == {"ok": False, "error": "Withheld", "why": WITHHELD}


def test_c40_a_refused_declaration_is_not_a_declaration_so_the_diagnostic_is_withheld(tmp_path):
    """C40: a declaration refused for a secret too short to redact leaves the executor undeclared: when it catches the
    refusal and goes on, a failed start's diagnostic is withheld, as for an executor that never declared."""

    def declare(request):
        with pytest.raises(ConfigError):
            request.declare_secrets(secrets=["zq"])

    row, marker, export = run_failing_start(tmp_path / "out", declare)
    assert json.loads(row["diagnostic"])["error"] == "Withheld"
    assert not any(SECRET in text for text in where_it_is_kept(row, marker, export).values())


def run_undeclared(root, diagnostic, *, before=lambda: None):
    """A campaign whose executor never declares and whose instrument fails its start with `diagnostic`; return the
    first attempt's row, its events and the scheduler's error, if it raised one."""

    class Quoting(Inducer):
        def start(self, identity):
            raise InfraError("sandbox", "refused", diagnostic=diagnostic)

    async def executor(request):
        before()
        start_instruments([Quoting()], {"run_key": request.run_key})

    settings = settings_for(max_attempts=1)
    with build(root, plan=PLAN, executor=FakeExecutor(default=executor), settings=settings) as scheduler:
        scheduler.run()
        row = dict(scheduler.store.q("SELECT * FROM attempts ORDER BY id")[0])
        events = [dict(e) for e in scheduler.store.q("SELECT kind, detail FROM events")]
    return row, events


def test_c40_a_local_log_that_cannot_be_written_costs_neither_the_attempt_nor_dispatch(tmp_path):
    """C40: the note an undeclared attempt leaves in the local log is a courtesy copy of what was withheld: when the log
    cannot be written (`out/logs` is a file here) the attempt is still set aside with its stub and charged as before,
    dispatch goes on (the scheduler does not stop, as it does for a traceback that cannot be written), and an event says
    the note was not written, with the exception's class and never its text."""
    root = tmp_path / "out"
    row, events = run_undeclared(
        root, {"seen": "x"}, before=lambda: (root / "logs").write_text("a file, not a directory", encoding="utf-8")
    )
    assert json.loads(row["diagnostic"])["error"] == "Withheld"
    assert (row["state"], row["charged"], row["infra_kind"]) == ("infra_fail", 1, "sandbox")
    noted = [e for e in events if e["kind"] == "diagnostic_note_unwritten"]
    assert noted  # one for each attempt that was set aside: dispatch went on past the first
    assert all("could not be written" in e["detail"] for e in noted)
    assert not any("a file, not a directory" in e["detail"] for e in noted)


def test_c40_the_local_note_is_redacted_structurally_and_never_cut_through_a_known_secret(tmp_path, monkeypatch):
    """C40: the note goes through the redaction a shown diagnostic does (keys that name a secret lose their values, the
    core's known secrets are replaced), whole: a diagnostic too large to keep is recorded as failed, never cut into a
    fragment of a secret the core knows."""
    known = "sk-live-secret-0123456789"
    monkeypatch.setenv("SERVICE_API_KEY", known)
    root = tmp_path / "out"
    run_undeclared(root, {"authorization": "Bearer cred-9876", "token": "tok-1234", "seen": f"quoted {known}"})
    written = log_path(root).read_text(encoding="utf-8")
    assert "withheld" in written
    assert not any(value in written for value in ("cred-9876", "tok-1234", known))
    assert written.count("[redacted]") >= 3
    big = tmp_path / "big"
    run_undeclared(big, {"pad": "x" * 16_340, "seen": known})
    cut = log_path(big).read_text(encoding="utf-8")
    assert "sk-live" not in cut
    assert "DiagnosticTooLarge" in cut


def test_c40_a_diagnostic_that_cannot_be_serialised_never_raises_from_the_runner_declared_or_not(tmp_path):
    """C40: a diagnostic nested past the recursion limit makes `json.dumps` raise: the attempt is set aside and its
    snapshot is recorded as failed (declared) or withheld (not), and nothing stops dispatch."""
    nested: dict = {}
    for _ in range(5000):
        nested = {"a": nested}
    row, _ = run_undeclared(tmp_path / "out", nested)
    assert json.loads(row["diagnostic"])["error"] == "Withheld"
    assert (row["state"], row["charged"]) == ("infra_fail", 1)

    async def executor(request):
        request.declare_secrets()
        raise InfraError("sandbox", "refused", diagnostic=nested)

    with build(
        tmp_path / "declared", plan=PLAN, executor=FakeExecutor(default=executor), settings=settings_for(max_attempts=1)
    ) as scheduler:
        scheduler.run()
        declared = dict(scheduler.store.q("SELECT * FROM attempts ORDER BY id")[0])
    assert json.loads(declared["diagnostic"])["ok"] is False
    assert (declared["state"], declared["charged"]) == ("infra_fail", 1)


def test_c40_what_the_executor_declared_stays_in_a_shown_diagnostics_redaction_whatever_replaces_the_logs_redactor(
    tmp_path,
):
    """C40: an executor that declared a secret and then installs a redactor of its own on the attempt's log (a direct
    `record_attempt` that does not forward `request.declared`) still has the declared secret redacted from the
    diagnostic it shows: the declaration is pinned, not only installed."""

    async def executor(request):
        request.declare_secrets(secrets=[SECRET])
        request.defect_log.redact_with(lambda: Redactor([]))  # replaces the log's redactor, the declared one with it
        raise InfraError("sandbox", "post-run check refused", diagnostic={"seen": f"it quoted {SECRET}"})

    with build(
        tmp_path / "out", plan=PLAN, executor=FakeExecutor(default=executor), settings=settings_for(max_attempts=1)
    ) as scheduler:
        scheduler.run()
        row = dict(scheduler.store.q("SELECT * FROM attempts ORDER BY id")[0])
    assert json.loads(row["diagnostic"])["diagnostic"] == {"seen": "it quoted [redacted]"}


def test_c40_a_declared_secret_that_contains_one_the_environment_holds_is_redacted_whole_from_the_diagnostic(
    tmp_path, monkeypatch
):
    """C40: a declared URL that embeds a key the process's environment already names is redacted whole from the
    diagnostic that ships (the row, the marker, the export and the report), as the record is: the shorter secret
    replaced first must not leave the user and the host of the longer one behind."""
    key = "process-api-key-0123456789"
    url = f"https://alice-private:{key}@private-host.example"
    monkeypatch.setenv("SERVICE_API_KEY", key)
    monkeypatch.setenv("SERVICE_ENDPOINT", url)
    row, marker, export = run_failing_start(
        tmp_path / "out", lambda request: request.declare_secrets(secret_env=["SERVICE_ENDPOINT"]), url
    )
    assert json.loads(row["diagnostic"])["diagnostic"] == {"seen": "the endpoint answered with [redacted]"}
    shipped = [*where_it_is_kept(row, marker, export).values(), report_of(export)]
    assert not any(piece in text for text in shipped for piece in (key, "alice-private", "private-host.example"))
