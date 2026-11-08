"""The store: factor columns, the transaction refusals, charging by cause (C03), migrations (C20) and C01."""

import sqlite3
import threading
from dataclasses import replace

import pytest
from campaign_support import (
    LATER,
    NOW,
    defect,
    finished,
    infra,
    keys_under,
    make_manifest,
    open_store,
    planned_store,
    run_one,
    treatment_schema,
)

from expcore.campaign.attempts import AttemptRefused, PinMismatch, SchemaMismatch, classify
from expcore.campaign.factors import FactorSchema
from expcore.campaign.markers import AttemptResult, write_marker
from expcore.campaign.recovery import recover
from expcore.campaign.store import _ADDED_COLUMNS, Store, column_for
from expcore.errors import CORE_KINDS, InfraError
from expcore.loop.outcomes import OUTCOMES, HarnessDefect, Outcome


def columns(store, table):
    return [r["name"] for r in store.q(f"PRAGMA table_xinfo({table})")]


# --- layout ---------------------------------------------------------------------------------------------------------


def test_the_database_is_wal_with_full_synchronous_writes(tmp_path):
    store = open_store(tmp_path)
    assert store.q("PRAGMA journal_mode")[0][0] == "wal"
    assert store.q("PRAGMA synchronous")[0][0] == 2  # FULL
    tables = {r["name"] for r in store.q("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "attempts", "meta", "events"} <= tables


def test_a_run_row_holds_the_key_the_digest_the_factors_as_json_and_a_generated_column_per_factor(tmp_path):
    store, schema, manifest = planned_store(tmp_path)
    row = store.q("SELECT * FROM runs WHERE run_key=?", (manifest.rows[0].key.text,))[0]
    key = manifest.rows[0].key
    assert (row["run_key"], row["digest"], row["factors"], row["status"]) == (
        key.text,
        key.digest,
        key.factors_json,
        "pending",
    )
    for factor, value in key.factors.items():
        assert row[column_for(factor)] == value
    assert isinstance(row["f_seed"], int)  # the seed keeps its type in the generated column
    indexed = {r["name"] for r in store.q("PRAGMA index_list(runs)")}
    assert {f"runs_{column_for(f)}" for f in schema.order} <= indexed


def test_the_runs_table_holds_no_study_columns(tmp_path):
    store = open_store(tmp_path)
    fixed = {"run_key", "digest", "seq", "factors", "status"}
    assert set(columns(store, "runs")) == fixed | {column_for(f) for f in store.schema.order}


def test_runs_can_be_selected_by_the_value_of_a_factor(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    mount = store.pending(data_path="mount")
    assert len(mount) == len(manifest.rows) // 3
    assert all(r["f_data_path"] == "mount" for r in mount)
    assert [r["seq"] for r in mount] == sorted(r["seq"] for r in mount)
    assert len(store.pending(data_path="mount", seed=1)) == len(mount) // 2
    with pytest.raises(ValueError, match="unknown factor 'colour'"):
        store.pending(colour="red")


def test_syncing_the_manifest_twice_adds_nothing_and_keeps_every_status(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    first = manifest.rows[0].key.text
    run_one(store, first, finished())
    assert store.sync_manifest(manifest.rows) == 0
    assert store.q("SELECT status FROM runs WHERE run_key=?", (first,))[0]["status"] == "done"


def test_a_grown_plan_adds_the_new_runs_and_refreshes_the_places_of_the_old(tmp_path):
    store, schema, small = planned_store(tmp_path)
    large = make_manifest(schema, tasks=("task-a", "task-b", "task-c"))
    assert store.sync_manifest(large.rows) == len(large.rows) - len(small.rows)
    places = {r["run_key"]: r["seq"] for r in store.q("SELECT run_key, seq FROM runs")}
    assert places == {row.key.text: row.seq for row in large.rows}


def test_r11_a_run_key_listed_twice_in_a_manifest_is_refused_by_the_store(tmp_path):
    """R11: a duplicate key is refused, even in a manifest built by hand."""
    store, _, manifest = planned_store(tmp_path)
    twice = replace(manifest, rows=(*manifest.rows, manifest.rows[0]))
    with pytest.raises(ValueError, match="appears more than once in the manifest"):
        store.sync_manifest(twice.rows)
    assert store.q("SELECT COUNT(*) c FROM runs")[0]["c"] == len(manifest.rows)


def test_a_key_of_another_identity_order_is_refused(tmp_path):
    store, schema, _ = planned_store(tmp_path)
    other = FactorSchema(schema.declared, ["seed", *schema.order[:-1]], schema.conditions)
    with pytest.raises(ValueError, match="identity order"):
        store.sync_manifest(make_manifest(other).rows)


def test_a_database_made_for_other_factors_is_refused_not_migrated(tmp_path):
    first = open_store(tmp_path)
    first.close()
    other = FactorSchema({"depth": ["1", "2"]}, ["task", "depth", "model", "seed"], {"a": {"depth": "1"}})
    with pytest.raises(SchemaMismatch, match="factor columns"):
        open_store(tmp_path, other)
    open_store(tmp_path).close()  # the right schema still opens it


# --- the refusals inside the transaction ------------------------------------------------------------------------------


def test_a_second_running_attempt_for_one_run_is_refused_in_the_transaction(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    store.begin_attempt(key, NOW, 1, "")
    with pytest.raises(AttemptRefused, match="already has a running attempt"):
        store.begin_attempt(key, NOW, 2, "")
    assert store.q("SELECT COUNT(*) c FROM attempts")[0]["c"] == 1


@pytest.mark.parametrize("result", [finished(), infra()])
def test_an_attempt_for_a_run_that_is_not_pending_is_refused(tmp_path, result):
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    for _ in range(3):
        status = run_one(store, key, result)
        if status != "pending":
            break
    assert status in ("done", "infra_exhausted")
    with pytest.raises(AttemptRefused, match=f"is not pending \\(it is {status}\\)"):
        store.begin_attempt(key, NOW, 1, "")


def test_an_attempt_for_a_run_the_store_does_not_hold_is_refused(tmp_path):
    store = open_store(tmp_path)
    with pytest.raises(AttemptRefused, match="is not in the store"):
        store.begin_attempt("task=x", NOW, 1, "")


def test_a_result_is_recorded_once(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    begun = store.begin_attempt(key, NOW, 1, "")
    store.finish_attempt(begun.id, infra(), 3)
    with pytest.raises(AttemptRefused, match="is not running"):
        store.finish_attempt(begun.id, finished(), 3)  # would turn an infrastructure failure into a score
    assert store.q("SELECT status FROM runs WHERE run_key=?", (key,))[0]["status"] == "pending"
    assert store.charged_attempts(key) == 1


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_the_attempt_limit_must_be_a_positive_integer(tmp_path, limit):
    store, _, manifest = planned_store(tmp_path)
    begun = store.begin_attempt(manifest.rows[0].key.text, NOW, 1, "")
    with pytest.raises(ValueError, match="max_attempts"):
        store.finish_attempt(begun.id, finished(), limit)


# --- C01, C03 -----------------------------------------------------------------------------------------------------


def outcome_for(name: str) -> Outcome:
    extra = {
        "infrastructure": {"infra_kind": "endpoint"},
        "error": {"error_type": "ContextOverflow"},
        "harness_defect": {"defect": HarnessDefect("ValueError", "b" * 64)},
    }
    return Outcome(name, "detail", **extra.get(name, {}))


def test_c01_only_an_agent_outcome_can_be_scored_and_an_infrastructure_one_never_is():
    """C01: an adapter or API error is recorded as infrastructure, never as a scored agent outcome."""
    states = {name: classify(AttemptResult(outcome_for(name), LATER, 1.0))[0] for name in OUTCOMES}
    assert states["infrastructure"] == "infra_fail"
    assert states["harness_defect"] == "harness_error"
    assert {n for n, s in states.items() if s == "done"} == set(OUTCOMES) - {"infrastructure", "harness_defect"}


@pytest.mark.parametrize("name", OUTCOMES)
def test_c01_the_store_accepts_every_outcome_of_the_loop_vocabulary(tmp_path, name):
    """C01: every ending the loop can report is recorded, and only the two set-aside ones are not scored."""
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    status = run_one(store, key, AttemptResult(outcome_for(name), LATER, 1.0))
    expected = {"infrastructure": "pending", "harness_defect": "harness_error"}.get(name, "done")
    assert status == expected
    assert store.q("SELECT termination FROM attempts")[0]["termination"] == name


def test_c01_an_adapter_error_is_an_infrastructure_attempt_and_leaves_the_run_unscored(tmp_path):
    """C01: the error a backend raised after its retries becomes an infrastructure attempt, not a result."""
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    error = InfraError("endpoint", "5 retries, HTTP 503")
    outcome = Outcome("infrastructure", error.detail, infra_kind=error.kind)
    status = run_one(store, key, AttemptResult(outcome, LATER, 2.0))
    attempt = store.q("SELECT * FROM attempts WHERE run_key=?", (key,))[0]
    assert status == "pending"  # run again, not scored
    assert (attempt["state"], attempt["infra_kind"], attempt["termination"], attempt["charged"]) == (
        "infra_fail",
        "endpoint",
        "infrastructure",
        1,
    )
    assert store.q("SELECT COUNT(*) c FROM attempts WHERE state='done'")[0]["c"] == 0


def test_an_outage_kind_outside_the_vocabulary_is_refused_before_it_reaches_the_store():
    with pytest.raises(ValueError, match="unknown infrastructure kind 'wifi'"):
        AttemptResult(Outcome("finished"), LATER, 1.0, outage_kind="wifi")


def test_c03_a_mixed_sequence_charges_exactly_the_infrastructure_attempts(tmp_path):
    """C03: infrastructure attempts count; key rejections, outage-discarded and interrupted ones do not."""
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    assert run_one(store, key, infra("key_rejected")) == "pending"
    assert run_one(store, key, AttemptResult(Outcome("finished"), LATER, 5.0, outage_kind="sandbox")) == "pending"
    store.begin_attempt(key, NOW, 1, "")  # killed: never finished
    recover(store, LATER, 3)
    assert store.charged_attempts(key) == 0
    assert run_one(store, key, infra("endpoint")) == "pending"
    assert run_one(store, key, defect()) == "harness_error"
    recover(store, LATER, 3)  # the harness-error run goes back to pending
    assert run_one(store, key, infra("sandbox")) == "pending"
    assert store.charged_attempts(key) == 2
    assert run_one(store, key, infra("unattributed_kill")) == "infra_exhausted"
    assert store.charged_attempts(key) == 3
    rows = store.q("SELECT state, infra_kind, charged FROM attempts ORDER BY id")
    assert [(r["state"], r["infra_kind"], r["charged"]) for r in rows] == [
        ("infra_fail", "key_rejected", 0),
        ("outage_discarded", "sandbox", 0),
        ("interrupted", None, 0),
        ("infra_fail", "endpoint", 1),
        ("harness_error", None, 0),
        ("infra_fail", "sandbox", 1),
        ("infra_fail", "unattributed_kill", 1),
    ]


def test_c03_the_attempt_number_is_the_next_charged_one_so_uncharged_attempts_are_rerun_under_it(tmp_path):
    """C03: only charged attempts advance the attempt number."""
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    numbers = []
    for result in (infra("key_rejected"), infra("endpoint"), infra("endpoint"), finished()):
        begun = store.begin_attempt(key, NOW, 1, "")
        numbers.append(begun.number)
        store.finish_attempt(begun.id, result, 5)
    assert numbers == [1, 1, 2, 3]


def test_c03_the_limit_is_a_parameter_and_exhaustion_is_set_in_the_finishing_transaction(tmp_path):
    """C03: a run is exhausted when its charged attempts reach the limit given, not before."""
    store, _, manifest = planned_store(tmp_path)
    one, two = (manifest.rows[0].key.text, manifest.rows[1].key.text)
    assert run_one(store, one, infra(), max_attempts=1) == "infra_exhausted"
    assert run_one(store, two, infra(), max_attempts=2) == "pending"
    assert run_one(store, two, infra(), max_attempts=2) == "infra_exhausted"


@pytest.mark.parametrize("kind", CORE_KINDS)
def test_c03_only_a_key_rejection_among_the_core_kinds_is_uncharged(tmp_path, kind):
    """C03: the infrastructure kinds other than key_rejected all count against the limit."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, manifest.rows[0].key.text, infra(kind))
    assert store.charged_attempts(manifest.rows[0].key.text) == (0 if kind == "key_rejected" else 1)


def test_usage_is_stored_as_reported_and_unreported_stays_null(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    run_one(store, key, finished(input_tokens=100, output_tokens=40, cache_read_tokens=60))
    row = store.q("SELECT * FROM attempts")[0]
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"], row["cache_creation_tokens"]) == (
        100,
        40,
        60,
        None,
    )
    assert (row["usage_complete"], row["wall_s"], row["termination"]) == (1, 12.5, "finished")


# --- C20 ------------------------------------------------------------


def test_c20_a_kill_between_two_migrations_reopens_and_finishes(tmp_path, monkeypatch):
    """C20: columns are added one at a time, so a kill after the first leaves a database the next open completes."""
    calls = []
    original = Store._add_column

    def killed_after_the_first(self, table, column, declaration):
        if calls:
            raise RuntimeError("killed")
        calls.append(column)
        original(self, table, column, declaration)

    monkeypatch.setattr(Store, "_add_column", killed_after_the_first)
    with pytest.raises(RuntimeError, match="killed"):
        open_store(tmp_path)
    monkeypatch.undo()
    raw = sqlite3.connect(tmp_path / "campaign.db")
    assert [r[1] for r in raw.execute("PRAGMA table_info(attempts)")].count(_ADDED_COLUMNS[0][1]) == 1
    assert _ADDED_COLUMNS[-1][1] not in [r[1] for r in raw.execute("PRAGMA table_info(attempts)")]
    raw.close()
    store = open_store(tmp_path)
    assert {c for _, c, _ in _ADDED_COLUMNS} <= set(columns(store, "attempts"))


def test_c20_a_database_from_before_a_column_gets_it_when_opened(tmp_path):
    """C20: an older database is brought forward column by column."""
    open_store(tmp_path).close()
    raw = sqlite3.connect(tmp_path / "campaign.db")
    raw.execute("ALTER TABLE attempts DROP COLUMN usage_complete")
    raw.execute("ALTER TABLE attempts DROP COLUMN cache_read_tokens")
    raw.commit()
    raw.close()
    assert "usage_complete" in columns(open_store(tmp_path), "attempts")


def test_c20_shared_metadata_is_updated_under_begin_immediate(tmp_path):
    """C20: a read-modify-write of a campaign-level fact takes the write lock before it reads."""
    store = open_store(tmp_path)
    seen = []
    store.db.set_trace_callback(seen.append)
    store.update_meta("k", lambda current: ("1", [], None), NOW)
    store.db.set_trace_callback(None)
    assert seen[0] == "BEGIN IMMEDIATE"
    assert seen[-1] == "COMMIT"
    assert any(statement.startswith("SELECT value") for statement in seen[1:-1])


def test_c20_two_writers_of_one_fact_never_overwrite_each_others_step(tmp_path):
    """C20: the dispatcher and a command from a shell, each with its own connection, lose no update."""
    first, second = open_store(tmp_path), open_store(tmp_path)
    first.put_meta("count", "0", NOW)

    def bump(current):
        return str(int(current) + 1), [("bumped", None, "")], None

    def work(store):
        for _ in range(25):
            store.update_meta("count", bump, NOW)

    threads = [threading.Thread(target=work, args=(s,)) for s in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert first.get_meta("count") == "50"
    assert first.q("SELECT COUNT(*) c FROM events WHERE kind='bumped'")[0]["c"] == 50


def test_a_failure_inside_a_transaction_rolls_all_of_it_back(tmp_path):
    store = open_store(tmp_path)
    with pytest.raises(RuntimeError, match="stop"):
        store.update_meta("k", lambda current: (_ for _ in ()).throw(RuntimeError("stop")), NOW)
    assert store.get_meta("k") is None
    store.put_meta_many({"a": "1", "b": "2"}, NOW, [("two", None, "facts")])
    assert (store.get_meta("a"), store.get_meta("b")) == ("1", "2")


# --- pins and snapshots ------------------------------------------------------------


def test_a_pinned_fact_cannot_change_without_the_operator_flag(tmp_path):
    store = open_store(tmp_path)
    assert store.pin("order_seed", "7", NOW) == "7"
    assert store.pin("order_seed", "7", LATER) == "7"
    with pytest.raises(PinMismatch, match="order_seed is pinned to 7"):
        store.pin("order_seed", "8", LATER)
    assert store.get_meta("order_seed") == "7"
    assert store.pin("order_seed", "8", LATER, repin=True) == "8"
    assert store.q("SELECT detail FROM events WHERE kind='repin'")[0]["detail"] == "order_seed: 7 -> 8 (operator flag)"


def test_a_snapshot_reads_the_database_as_it_was_at_its_first_read(tmp_path):
    reader, writer = open_store(tmp_path), open_store(tmp_path)
    writer.put_meta("k", "before", NOW)
    with reader.snapshot():
        assert reader.get_meta("k") == "before"
        writer.put_meta("k", "after", LATER)
        assert reader.get_meta("k") == "before"
    assert reader.get_meta("k") == "after"


# --- infrastructure that is a condition's treatment ----------------------------------------------------------------


@pytest.fixture
def treatment():
    """A store schema whose api-none condition declares a failed endpoint as its treatment."""
    return treatment_schema(("endpoint",))


def treatment_run(tmp_path, schema, condition="api-none"):
    """Open a store for the schema and return it with the run keys of a condition."""
    store = open_store(tmp_path, schema)
    manifest = make_manifest(schema)
    store.sync_manifest(manifest.rows)
    return store, keys_under(schema, manifest, condition)


def test_c01_a_declared_infrastructure_kind_is_a_scored_final_attempt_that_is_not_run_again(tmp_path, treatment):
    """C01: adds that under a condition declaring the kind, infrastructure is a scored treatment attempt."""
    store, (key, *_) = treatment_run(tmp_path, treatment)
    assert run_one(store, key, infra("endpoint", "HTTP 503 after 5 retries"), max_attempts=1) == "done"
    row = store.q("SELECT * FROM attempts")[0]
    assert (row["state"], row["treatment"], row["charged"]) == ("done", 1, 0)
    assert (row["termination"], row["infra_kind"], row["detail"]) == (
        "infrastructure",
        "endpoint",
        "HTTP 503 after 5 retries",
    )
    assert store.charged_attempts(key) == 0
    assert key not in [r["run_key"] for r in store.pending()]
    with pytest.raises(AttemptRefused, match=r"is not pending \(it is done\)"):
        store.begin_attempt(key, NOW, 1, "")


def test_c01_another_kind_under_the_same_condition_is_set_aside_and_run_again(tmp_path, treatment):
    """C01: adds that only the declared kinds are treatment; the condition's other faults are re-run as usual."""
    store, (key, *_) = treatment_run(tmp_path, treatment)
    assert run_one(store, key, infra("sandbox")) == "pending"
    row = store.q("SELECT * FROM attempts")[0]
    assert (row["state"], row["treatment"], row["charged"]) == ("infra_fail", 0, 1)
    assert key in [r["run_key"] for r in store.pending()]
    assert run_one(store, key, infra("endpoint")) == "done"  # and the declared kind still ends the run
    assert [(r["state"], r["treatment"], r["charged"]) for r in store.q("SELECT * FROM attempts ORDER BY id")] == [
        ("infra_fail", 0, 1),
        ("done", 1, 0),
    ]


def test_c01_the_same_kind_under_a_condition_that_does_not_declare_it_is_set_aside(tmp_path, treatment):
    """C01: adds that the declaration belongs to one condition and does not leak to the others."""
    store, (key, *_) = treatment_run(tmp_path, treatment, condition="mount-read")
    assert run_one(store, key, infra("endpoint")) == "pending"
    row = store.q("SELECT * FROM attempts")[0]
    assert (row["state"], row["treatment"], row["charged"]) == ("infra_fail", 0, 1)


def test_c03_a_treatment_attempt_is_not_charged_against_the_infrastructure_limit(tmp_path, treatment):
    """C03: adds that a treatment attempt costs the run no attempt, so earlier charged ones stay as they were."""
    store, (key, *_) = treatment_run(tmp_path, treatment)
    assert run_one(store, key, infra("sandbox"), max_attempts=2) == "pending"
    assert run_one(store, key, infra("endpoint"), max_attempts=2) == "done"  # not exhausted by it
    assert store.charged_attempts(key) == 1


def test_c04_a_treatment_attempt_committed_from_its_marker_is_a_scored_final_attempt(tmp_path, treatment):
    """C04: adds that recovery applies the condition's declaration, as a live finish does."""
    store, (key, *_) = treatment_run(tmp_path, treatment)
    begun = store.begin_attempt(key, NOW, 1, str(tmp_path / "run"))
    write_marker(tmp_path / "run", key, begun.id, infra("endpoint"))
    assert recover(store, LATER, 3).committed == 1
    row = store.q("SELECT * FROM attempts")[0]
    assert (row["state"], row["treatment"], row["charged"]) == ("done", 1, 0)
    assert store.q("SELECT status FROM runs WHERE run_key=?", (key,))[0]["status"] == "done"


def test_c01_classify_marks_treatment_only_for_an_infrastructure_outcome_of_a_listed_kind():
    """C01: adds the treatment mark to the classification, and keeps an outage flag on other outcomes a set-aside."""
    assert classify(infra("endpoint"), ("endpoint",)) == ("done", False, True)
    assert classify(infra("sandbox"), ("endpoint",)) == ("infra_fail", True, False)
    assert classify(infra("key_rejected"), ("endpoint",)) == ("infra_fail", False, False)
    assert classify(infra("key_rejected"), ("key_rejected",)) == ("done", False, True)
    assert classify(finished(), ("endpoint",)) == ("done", False, False)
    flagged = AttemptResult(Outcome("finished"), LATER, 1.0, outage_kind="endpoint")
    assert classify(flagged, ("endpoint",)) == ("outage_discarded", False, False)
