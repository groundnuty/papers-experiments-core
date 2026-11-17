"""C40: a failed instrument start keeps a diagnostic snapshot on the attempt, redacted like an instrument's, in the
set-aside record: its row, its marker, the export, and, for a batch merged from shards, with its shard.

The snapshot is `{"phase": "start", "instrument": <namespace>, "diagnostic": <what the instrument gave>}`. An
infrastructure failure from anywhere else that carries a diagnostic keeps `{"diagnostic": ...}`. One that cannot be
kept (not a JSON object, too big, its redaction raised) is `{"ok": false, "error": <class>}` and never costs the attempt
its result. Nothing changes for an attempt whose failure carries none.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from campaign_support import LATER, defect, finished, infra, planned_store, run_one
from record_support import clear_registry
from scheduler_support import FakeExecutor, build, models_for, settings_for

from expcore.campaign import recovery
from expcore.campaign.export import FORMAT, ExportError, export_run_state
from expcore.campaign.export_join import join_exports
from expcore.campaign.gate import Budget
from expcore.campaign.markers import MARKER_NAME, AttemptResult, read_marker, write_marker
from expcore.campaign.model_usage import usage_of
from expcore.campaign.selection_ledger import Pricing
from expcore.campaign.start_diagnostic import EXPORT_KEY, LIMIT_BYTES, WITHHELD, snapshot_of
from expcore.errors import CORE_KINDS, InfraError
from expcore.loop.outcomes import Outcome
from expcore.records.instruments import start_instruments
from expcore.records.redact import Redactor, register_secret

SECRET = "sk-diagnostic-secret-0123456789"
SEEN = {"checked": "the replica count", "seen": "2 of 3", "wanted": 3}
STARTED = {"phase": "start", "instrument": "state_inducer", "diagnostic": SEEN}


class Inducer:
    namespace = "state_inducer"

    def __init__(self, diagnostic=SEEN, detail="the start state was not reached"):
        self.error = InfraError("sandbox", detail, diagnostic=diagnostic)

    def start(self, identity):
        raise self.error

    def finish(self):
        return {}


def failed_start(diagnostic=SEEN) -> InfraError:
    """The error a failed start lets through `start_instruments`, as the executor's caller sees it."""
    try:
        start_instruments([Inducer(diagnostic)], {"run_key": "r", "factors": {}, "attempt": 1})
    except InfraError as error:
        return error
    raise AssertionError("the start did not fail")


def plain(redactor=None):
    return lambda: redactor or Redactor()


def keys_of(manifest):
    return [row.key.text for row in manifest.rows]


# --- the snapshot -----------------------------------------------------------------------------------------------------


def test_c40_a_failed_start_with_a_diagnostic_is_kept_with_its_instrument_and_phase():
    assert snapshot_of(failed_start(), plain()) == STARTED


def test_c40_a_diagnostic_is_withheld_when_the_redaction_context_is_not_known_to_be_whole():
    """C40: never a diagnostic redacted against a partial list of the run's secrets: when the executor has not told the
    core its secrets (and `record_attempt` has not given the whole context), the snapshot keeps the phase and the
    instrument and says why it holds no evidence."""
    kept = snapshot_of(failed_start(), plain(), complete=False)
    assert kept == {
        "phase": "start",
        "instrument": "state_inducer",
        "ok": False,
        "error": "Withheld",
        "why": WITHHELD,
    }
    assert "declare_secrets" in kept["why"]
    assert json.dumps(SEEN) not in json.dumps(kept)


def test_c40_a_failed_start_with_nothing_to_redact_is_kept_whatever_the_context():
    """C40: an instrument that gave no diagnostic has nothing to redact: the phase and the instrument are kept."""
    assert snapshot_of(failed_start(None), plain(), complete=False) == {"phase": "start", "instrument": "state_inducer"}


def test_c40_a_failed_start_without_a_diagnostic_is_kept_as_the_instrument_that_failed():
    assert snapshot_of(failed_start(None), plain()) == {"phase": "start", "instrument": "state_inducer"}


def test_c40_a_diagnostic_of_an_error_raised_elsewhere_is_kept_without_a_phase():
    error = InfraError("sandbox", "the probe refused", diagnostic={"seen": "x"})
    assert snapshot_of(error, plain()) == {"diagnostic": {"seen": "x"}}


@pytest.mark.parametrize("error", [InfraError("sandbox", "no diagnostic"), ValueError("boom"), KeyError("k")])
def test_c40_an_error_with_nothing_to_keep_has_no_snapshot(error):
    """C40: no diagnostic and not a failed start, or no infrastructure error at all: nothing is kept."""
    assert snapshot_of(error, plain()) is None


def test_c40_the_snapshot_is_redacted_like_an_instruments_and_a_secret_is_never_kept():
    """C40: every string of the snapshot, keys included, goes through the attempt's redactor."""
    error = failed_start({"seen": f"a header with {SECRET} in it", f"key {SECRET}": "value", "token": "anything"})
    kept = snapshot_of(error, plain(Redactor([SECRET])))
    text = json.dumps(kept)
    assert SECRET not in text
    assert "[redacted]" in text
    assert kept["diagnostic"]["token"] == "[redacted]"  # a key that names a secret loses its value whatever it is


@pytest.mark.parametrize(
    ("diagnostic", "error_name"),
    [
        ({"seen": {1, 2}}, "TypeError"),  # not JSON
        ({"seen": float("nan")}, "ValueError"),  # a number JSON cannot hold
        (["not", "an", "object"], "TypeError"),
        ({"seen": "x" * (LIMIT_BYTES + 1)}, "DiagnosticTooLarge"),
    ],
)
def test_c40_a_diagnostic_that_cannot_be_kept_is_recorded_as_failed_and_names_the_instrument(diagnostic, error_name):
    """C40: never dropped and never guessed, as an instrument whose finish failed is (O08)."""
    kept = snapshot_of(failed_start(diagnostic), plain())
    assert kept == {"phase": "start", "instrument": "state_inducer", "ok": False, "error": error_name}


def test_c40_a_redaction_that_raises_is_recorded_as_failed_and_costs_nothing():
    def broken():
        raise RuntimeError("no redactor")

    kept = snapshot_of(failed_start(), broken)
    assert kept == {"phase": "start", "instrument": "state_inducer", "ok": False, "error": "RuntimeError"}


def test_c40_an_infra_error_subclass_that_skips_the_base_initialiser_is_still_named_and_kept():
    """C40: reading `origin` and `diagnostic` with a default, a subclass whose own initialiser never calls the base one
    gives `snapshot_of` an error it can keep, and `start_instruments` an error it can name; nothing raises."""

    class Odd(InfraError):
        def __init__(self):
            RuntimeError.__init__(self, "odd")

    class Starter:
        namespace = "state_inducer"

        def start(self, identity):
            raise Odd()

        def finish(self):
            return {}

    with pytest.raises(Odd) as caught:
        start_instruments([Starter()], {"run_key": "r"})
    assert snapshot_of(caught.value, plain()) == {"phase": "start", "instrument": "state_inducer"}
    assert snapshot_of(Odd(), plain()) is None


# --- the attempt's result, its marker and its row -------------------------------------------------------------------


def test_c40_a_result_holds_a_diagnostic_that_is_a_json_object_or_none():
    result = AttemptResult(Outcome("infrastructure", "x", infra_kind="sandbox"), LATER, 1.0, diagnostic=STARTED)
    assert result.diagnostic == STARTED
    with pytest.raises(ValueError, match="diagnostic"):
        AttemptResult(Outcome("infrastructure", "x", infra_kind="sandbox"), LATER, 1.0, diagnostic=["x"])


def test_c40_the_marker_carries_the_diagnostic_only_when_there_is_one(tmp_path):
    """C40: a marker without one is the bytes it was, and a marker with one gives it back."""
    plain_result = infra("sandbox", "x")
    write_marker(tmp_path / "a", "k", 1, plain_result)
    assert "diagnostic" not in json.loads((tmp_path / "a" / MARKER_NAME).read_text())["result"]
    assert read_marker(tmp_path / "a", "k", 1).diagnostic is None
    write_marker(tmp_path / "b", "k", 1, infra("sandbox", "x", diagnostic=STARTED))
    assert json.loads((tmp_path / "b" / MARKER_NAME).read_text())["result"]["diagnostic"] == STARTED
    assert read_marker(tmp_path / "b", "k", 1).diagnostic == STARTED


def test_c40_the_row_keeps_the_diagnostic_as_canonical_json_and_null_for_every_other_attempt(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    first, second = keys_of(manifest)[:2]
    run_one(store, first, infra("sandbox", "not induced", diagnostic=STARTED))
    run_one(store, second, infra("sandbox", "no diagnostic"))
    rows = store.q("SELECT diagnostic FROM attempts ORDER BY id")
    assert json.loads(rows[0]["diagnostic"]) == STARTED
    assert rows[0]["diagnostic"] == json.dumps(STARTED, sort_keys=True)
    assert rows[1]["diagnostic"] is None


def test_c40_recovery_commits_the_diagnostic_from_the_marker_a_kill_left(tmp_path):
    """C40: a kill between the marker and the store loses no diagnostic: it is in the marker."""
    store, _, manifest = planned_store(tmp_path)
    key = keys_of(manifest)[0]
    run_dir = tmp_path / "try1"
    begun = store.begin_attempt(key, LATER, 1, str(run_dir))
    result = infra("sandbox", "not induced", diagnostic=STARTED, treatment_kinds=())
    write_marker(run_dir, key, begun.id, result)
    assert recovery.recover(store, LATER, 3).committed == 1
    [row] = store.q("SELECT state, charged, diagnostic FROM attempts")
    assert (row["state"], row["charged"]) == ("infra_fail", 1)
    assert json.loads(row["diagnostic"]) == STARTED


# --- an attempt through the scheduler ---------------------------------------------------------------------------------


PLAN = {"models": ("m1",), "tasks": ("task-a",), "seeds": (1,)}


def run_failing(root, diagnostic=SEEN):
    """A campaign whose every attempt fails at the start of its instrument; return the first row, its marker and the
    export."""

    async def executor(request):
        request.declare_secrets()  # nothing more than the environment's: the redaction context is whole
        start_instruments([Inducer(diagnostic)], {"run_key": request.run_key})

    with build(
        root, plan=PLAN, executor=FakeExecutor(default=executor), settings=settings_for(max_attempts=1)
    ) as sched:
        sched.run()
        row = dict(sched.store.q("SELECT * FROM attempts ORDER BY id")[0])
        marker = read_marker(Path(row["run_dir"]), row["run_key"], row["id"])
        return row, marker, export_run_state(sched.store)


def test_c40_an_attempt_whose_instrument_failed_to_start_keeps_the_diagnostic_and_is_charged_as_before(tmp_path):
    """C40: the attempt is set aside and charged as any infrastructure failure of its kind; the diagnostic is on its
    row and in its marker, and the export lists it with the run, the condition and the kind."""
    row, marker, export = run_failing(tmp_path / "out")
    assert (row["state"], row["charged"], row["infra_kind"]) == ("infra_fail", 1, "sandbox")
    assert json.loads(row["diagnostic"]) == STARTED
    assert marker.diagnostic == STARTED
    assert [e["attempt_id"] for e in export[EXPORT_KEY]] == sorted(e["attempt_id"] for e in export[EXPORT_KEY])
    entry = export[EXPORT_KEY][0]  # every run of the plan failed its first attempt, so there is one entry for each
    assert entry["attempt_id"] == row["id"]
    assert entry["diagnostic"] == STARTED
    assert (entry["run_key"], entry["state"], entry["infra_kind"], entry["charged"]) == (
        row["run_key"],
        "infra_fail",
        "sandbox",
        True,
    )
    assert entry["condition"]
    assert (entry["model"], entry["attempt"]) == ("m1", 1)


def test_c40_the_runner_redacts_the_diagnostic_with_the_attempts_secrets(tmp_path, monkeypatch):
    """C40: a secret the environment holds is gone from the row and the marker before either is written."""
    monkeypatch.setenv("INDUCER_API_KEY", SECRET)
    row, marker, _ = run_failing(tmp_path / "out", {"seen": f"Authorization failed for {SECRET}"})
    assert SECRET not in row["diagnostic"]
    assert SECRET not in json.dumps(marker.diagnostic)
    assert "[redacted]" in row["diagnostic"]


def test_c40_a_diagnostic_that_cannot_be_kept_never_stops_the_attempt_or_changes_its_charge(tmp_path):
    """C40: an exception escaping the runner here would stop dispatch (C17): it is recorded as failed instead."""
    row, marker, _ = run_failing(tmp_path / "out", {"seen": {1, 2}})
    assert (row["state"], row["charged"], row["infra_kind"]) == ("infra_fail", 1, "sandbox")
    assert json.loads(row["diagnostic"]) == {
        **{k: STARTED[k] for k in ("phase", "instrument")},
        "ok": False,
        "error": "TypeError",
    }
    assert marker.diagnostic["ok"] is False


def test_c40_an_induction_that_failed_before_any_request_keeps_its_diagnostic_and_is_a_measured_zero_in_the_usage(
    tmp_path,
):
    """C40 with C05: a start that failed before the loop sent no request: with a budget the attempt is charged a
    measured zero (0 tokens, basis measured, no bound), keeps its diagnostic, and the model usage counts it as a
    measured attempt of 0 tokens and 0 dollars, never as a bound or as unknown."""

    async def executor(request):
        request.declare_secrets()
        start_instruments([Inducer()], {"run_key": request.run_key})

    settings = settings_for(max_attempts=1, budget=Budget(max_usd=50.0))
    with build(tmp_path / "out", plan=PLAN, executor=FakeExecutor(default=executor), settings=settings) as sched:
        sched.run()
        rows = sched.store.q("SELECT * FROM attempts ORDER BY id")
        pricing = Pricing({"m1": models_for("m1").resolve("m1")}, sched.agent_config.limits)
        usage = usage_of(sched.store.q, pricing)
    assert {
        (r["state"], r["input_tokens"], r["output_tokens"], r["usage_complete"], r["spend_basis"]) for r in rows
    } == {("infra_fail", 0, 0, 1, "measured")}
    assert all(json.loads(r["diagnostic"]) == STARTED for r in rows)
    [m1] = usage["models"]
    [failed] = [c for c in m1["classes"] if c["class"] == "infra_fail"]
    assert (failed["attempts"], failed["input_tokens"], failed["usage_incomplete_attempts"]) == (len(rows), 0, 0)
    assert failed["usd"] == {"measured": 0.0, "measured_plus_inflight": 0.0, "bound": 0.0, "charges": 0.0, "total": 0.0}
    assert (failed["bounded_attempts"], m1["usd"]["total"]) == (0, 0.0)


def test_c40_the_runner_redacts_with_the_secrets_the_executor_gave_the_recording_once_it_has(tmp_path):
    """C40: the snapshot is redacted with the same redactor the attempt's harness-defect log uses, which the recording
    replaces with the whole context of the attempt (secrets and secret variables passed to `record_run`) once the
    executor has called it: a diagnostic of an error raised after that has the secrets the executor named for the
    record gone too."""
    run_secret = "run-scoped-secret-9876543210"

    async def executor(request):
        request.defect_log.redact_with(lambda: Redactor([run_secret]))  # what `record_run` does with its `secrets=`
        start_instruments([Inducer({"seen": f"rejected {run_secret}"})], {"run_key": request.run_key})

    with build(
        tmp_path / "out", plan=PLAN, executor=FakeExecutor(default=executor), settings=settings_for(max_attempts=1)
    ) as sched:
        sched.run()
        row = dict(sched.store.q("SELECT * FROM attempts ORDER BY id")[0])
        marker = read_marker(Path(row["run_dir"]), row["run_key"], row["id"])
    assert run_secret not in row["diagnostic"]
    assert run_secret not in json.dumps(marker.diagnostic)
    assert "[redacted]" in row["diagnostic"]


def test_c40_a_secret_registered_before_a_failed_start_is_redacted_from_its_diagnostic(tmp_path):
    """C40: an executor starts its instruments before `record_run`, so a secret it names only there is not known when
    the start fails; one it registers (`register_secret`, as a platform that mints a token does) is read when the
    snapshot is made, so the diagnostic that quotes it loses it."""
    token = "minted-at-run-time-0123456789"

    async def executor(request):
        request.declare_secrets()
        register_secret(token)
        start_instruments([Inducer({"seen": f"refused {token}"})], {"run_key": request.run_key})

    try:
        with build(
            tmp_path / "out", plan=PLAN, executor=FakeExecutor(default=executor), settings=settings_for(max_attempts=1)
        ) as sched:
            sched.run()
            row = dict(sched.store.q("SELECT * FROM attempts ORDER BY id")[0])
    finally:
        clear_registry()  # the registry only grows: leave none behind for the next test
    assert token not in row["diagnostic"]
    assert "[redacted]" in row["diagnostic"]


# --- the export -------------------------------------------------------------------------------------------------------


def test_c40_an_export_has_the_key_only_when_an_attempt_kept_a_diagnostic_and_is_the_one_it_was_otherwise(tmp_path):
    """C40: no key, and no change to any other, for a campaign none of whose attempts kept one."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], infra("sandbox", "x"))
    run_one(store, keys_of(manifest)[1], finished())
    before = export_run_state(store)
    assert EXPORT_KEY not in before
    run_one(store, keys_of(manifest)[2], infra("sandbox", "not induced", diagnostic=STARTED))
    after = export_run_state(store)
    assert list(after)[: len(before)] == list(before)[: len(before)]
    assert list(after).index(EXPORT_KEY) == len(before)  # added after every key a campaign without it has
    assert [e["run_key"] for e in after[EXPORT_KEY]] == [keys_of(manifest)[2]]


def test_c40_the_entries_are_in_attempt_order_with_the_state_the_charge_and_the_run(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    keys = keys_of(manifest)
    run_one(store, keys[0], infra("sandbox", "not induced", diagnostic=STARTED))
    run_one(store, keys[1], defect())  # no diagnostic: not listed
    run_one(store, keys[2], infra("sandbox", "again", diagnostic={"seen": "x"}))
    entries = export_run_state(store)[EXPORT_KEY]
    assert [e["run_key"] for e in entries] == [keys[0], keys[2]]
    assert [e["attempt_id"] for e in entries] == sorted(e["attempt_id"] for e in entries)
    assert set(entries[0]) == {
        "attempt_id", "run_key", "condition", "model", "attempt", "state", "infra_kind", "charged", "diagnostic"
    }  # fmt: skip


def test_c40_a_snapshot_made_before_the_column_exports_none_and_does_not_fail(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], infra("sandbox", "x"))
    try:
        store.db.execute("ALTER TABLE attempts DROP COLUMN diagnostic")
    except sqlite3.OperationalError:
        pytest.skip("this SQLite cannot drop a column")
    assert EXPORT_KEY not in export_run_state(store)


def test_c40_a_stored_diagnostic_that_is_not_json_is_refused_naming_the_attempt(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], infra("sandbox", "x", diagnostic=STARTED))
    store.db.execute("UPDATE attempts SET diagnostic='not json'")
    with pytest.raises(ExportError, match=r"attempt 1.*diagnostic"):
        export_run_state(store)


def shard_export(*keys: str, **more) -> dict:
    """A shard's export holding the given runs as analysed ones, and nothing else."""
    return {
        "format": FORMAT,
        "factors": ["task", "model", "seed"],
        "planned_runs": len(keys),
        "exhausted_runs": [],
        "unfinished_runs": [],
        "analysed_attempts": [
            {"run_key": key, "digest": key * 2, "condition": "base", "factors": {"task": key}} for key in keys
        ],
        "discarded_usage": [],
        "retries_by_kind": [],
        "infrastructure_by_condition": [],
        "budget_holds": [],
        "kinds": list(CORE_KINDS),
        "partial": False,
        "running_attempts": 0,
        "excluded_attempts": [],
        **more,
    }


def test_c40_a_merged_export_lists_every_shards_entries_in_shard_order_tagged_with_the_shard():
    """C40: each shard's diagnostics are its own; a shard with none adds none and the key is there when any has it."""
    kept = {"attempt_id": 1, "run_key": "a", "condition": "base", "model": "m", "attempt": 1, "state": "infra_fail",
            "infra_kind": "sandbox", "charged": True, "diagnostic": STARTED}  # fmt: skip
    shards = [{"index": 1, "label": "first-shard"}, {"index": 2, "label": "second-shard"}]
    one, two = shard_export("a"), shard_export("b", **{EXPORT_KEY: [kept]})
    joined = join_exports({1: one, 2: two}, {"a": 0, "b": 1}, shards)
    assert joined[EXPORT_KEY] == [{**kept, "shard": 2}]
    assert EXPORT_KEY not in join_exports({1: one, 2: shard_export("b")}, {"a": 0, "b": 1}, shards)
