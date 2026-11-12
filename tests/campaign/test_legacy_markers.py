"""C04: a schema-1 outcome marker, written before the model-turn count existed, is still committed by recovery."""

import json

import pytest
from campaign_support import (
    LATER,
    NOW,
    as_schema_two,
    finished,
    infra,
    keys_under,
    make_manifest,
    open_store,
    treatment_schema,
)

from expcore.campaign.markers import MARKER_NAME, MarkerError, read_marker, write_marker
from expcore.campaign.recovery import recover

NEW_FIELDS = ("model_turns", "recorded_treatment", "first_turn_rule", "turn_usage", "replies_checked")


def legacy_marker(run_dir, key, attempt_id, result):
    """Write the marker of a result as schema 1 wrote it: no model-turn count, no record flag, no rule."""
    path = write_marker(run_dir, key, attempt_id, result)
    body = as_schema_two(json.loads(path.read_text()))
    body["schema"] = 1
    for name in NEW_FIELDS:
        del body["result"][name]
    path.write_text(json.dumps(body, sort_keys=True))
    return path


def running_attempt(tmp_path, condition="api-none"):
    """A store with a treatment declared for endpoint, and a run of the condition with an attempt begun."""
    schema = treatment_schema(("endpoint",))
    store = open_store(tmp_path, schema)
    manifest = make_manifest(schema)
    store.sync_manifest(manifest.rows)
    key = keys_under(schema, manifest, condition)[0]
    run_dir = tmp_path / "run"
    return store, key, store.begin_attempt(key, NOW, 1, str(run_dir)), run_dir


def test_c04_a_schema_one_marker_reads_under_the_version_one_rule_and_is_marked_legacy(tmp_path):
    """C04: no turn count, so none is invented: zero turns, no record flag to check, the rule without the first turn."""
    path = legacy_marker(tmp_path, "k", 1, infra("endpoint", treatment_kinds=("endpoint",)))
    result = read_marker(tmp_path, "k", 1)
    assert (result.model_turns, result.recorded_treatment, result.first_turn_rule) == (0, None, False)
    assert result.legacy_marker is True
    assert result.replies_checked is False  # written before the check: an attempt of no turn is charged its bound
    assert result.outcome.infra_kind == "endpoint"
    assert json.loads(path.read_text())["schema"] == 1
    write_marker(tmp_path, "k", 1, infra("endpoint"))  # a marker written now is schema 3 and is not legacy
    assert read_marker(tmp_path, "k", 1).legacy_marker is False


def test_c04_recovery_commits_a_schema_one_success_and_records_a_legacy_marker_event(tmp_path):
    """C04: an ordinary result in a schema-1 marker completes as it always did; the event says it was legacy."""
    store, key, begun, run_dir = running_attempt(tmp_path)
    legacy_marker(run_dir, key, begun.id, finished(treatment_kinds=("endpoint",)))
    assert recover(store, LATER, 3).committed == 1
    row = store.q("SELECT state, charged, treatment FROM attempts")[0]
    assert (row["state"], row["charged"], row["treatment"]) == ("done", 0, 0)
    assert store.q("SELECT status FROM runs WHERE run_key=?", (key,))[0]["status"] == "done"
    kinds = [r["kind"] for r in store.q("SELECT kind FROM events ORDER BY id")]
    assert kinds == ["recovered_committed", "legacy_marker"]
    detail = store.q("SELECT detail FROM events WHERE kind='legacy_marker'")[0]["detail"]
    assert "schema-1 marker" in detail
    assert "version-1 treatment rule" in detail


def test_c04_recovery_applies_the_version_one_rule_to_a_schema_one_treatment_failure(tmp_path):
    """C04: the marker has no turn count, so a failure of a declared kind is treatment by the kind alone, as when
    the marker was written, and the run is done and not run again."""
    store, key, begun, run_dir = running_attempt(tmp_path)
    legacy_marker(run_dir, key, begun.id, infra("endpoint", treatment_kinds=("endpoint",)))
    assert recover(store, LATER, 3).committed == 1
    row = store.q("SELECT state, charged, treatment, infra_kind FROM attempts")[0]
    assert (row["state"], row["charged"], row["treatment"], row["infra_kind"]) == ("done", 0, 1, "endpoint")
    assert store.q("SELECT status FROM runs WHERE run_key=?", (key,))[0]["status"] == "done"
    assert "legacy_marker" in [r["kind"] for r in store.q("SELECT kind FROM events")]


def test_c04_a_schema_one_failure_of_an_undeclared_kind_is_still_set_aside(tmp_path):
    """C04: the version-1 rule is the declared kind, so another kind is set aside and the run goes again."""
    store, key, begun, run_dir = running_attempt(tmp_path)
    legacy_marker(run_dir, key, begun.id, infra("sandbox", treatment_kinds=("endpoint",)))
    assert recover(store, LATER, 3).committed == 1
    row = store.q("SELECT state, charged, treatment FROM attempts")[0]
    assert (row["state"], row["charged"], row["treatment"]) == ("infra_fail", 1, 0)
    assert store.q("SELECT status FROM runs WHERE run_key=?", (key,))[0]["status"] == "pending"


def test_c04_a_new_marker_commits_without_a_legacy_event(tmp_path):
    """C04: only a schema-1 marker is called legacy."""
    store, key, begun, run_dir = running_attempt(tmp_path)
    write_marker(run_dir, key, begun.id, finished(treatment_kinds=("endpoint",)))
    assert recover(store, LATER, 3).committed == 1
    assert [r["kind"] for r in store.q("SELECT kind FROM events")] == ["recovered_committed"]


@pytest.mark.parametrize("schema", [0, 4, "1", True, None])
def test_c04_any_other_marker_schema_is_still_refused(schema, tmp_path):
    """C04: only schemas 1 and 2 are readable."""
    path = write_marker(tmp_path, "k", 1, finished())
    body = json.loads(path.read_text())
    body["schema"] = schema
    path.write_text(json.dumps(body))
    with pytest.raises(MarkerError, match="unknown marker schema"):
        read_marker(tmp_path, "k", 1)
    assert path.name == MARKER_NAME


def test_c04_a_schema_one_marker_with_a_field_of_schema_two_is_not_trusted(tmp_path):
    """C04: a schema-1 marker holds exactly the schema-1 fields; one that also holds the new ones is not a marker."""
    path = write_marker(tmp_path, "k", 1, finished())
    body = as_schema_two(json.loads(path.read_text()))
    body["schema"] = 1
    path.write_text(json.dumps(body))
    unknown = "unknown result fields: first_turn_rule, model_turns, recorded_treatment, replies_checked, turn_usage"
    with pytest.raises(MarkerError, match=unknown):
        read_marker(tmp_path, "k", 1)


def test_c04_a_schema_two_marker_without_the_rule_field_reads_under_the_version_two_rule(tmp_path):
    """C04: a schema-2 marker written before the rule field existed holds a version-2 record, so recovery reads it
    with the first-turn rule rather than refusing it."""
    path = write_marker(tmp_path, "k", 1, infra("endpoint"))
    body = as_schema_two(json.loads(path.read_text()))
    del body["result"]["first_turn_rule"]
    path.write_text(json.dumps(body, sort_keys=True))
    result = read_marker(tmp_path, "k", 1)
    assert (result.first_turn_rule, result.legacy_marker) == (True, False)
