"""C01: a failure before the agent's first turn is never treatment; the record, the store and the replay agree."""

import dataclasses

import pytest
from campaign_support import LATER, NOW, infra, keys_under, make_manifest, open_store, treatment_schema
from record_support import no_secrets_in_the_environment, record_run

from expcore.campaign.attempts import TreatmentKindsMismatch, classify
from expcore.campaign.executor import failure_result, result_from_record
from expcore.campaign.markers import AttemptResult, read_marker, write_marker
from expcore.campaign.recovery import recover
from expcore.errors import InfraError, is_treatable
from expcore.loop.outcomes import Outcome
from expcore.records.build import RunIdentity
from expcore.replay.replay import replay_record

pytestmark = pytest.mark.usefixtures("no_secrets_in_the_environment")
__all__ = ["no_secrets_in_the_environment"]

ECHO = {"echo": lambda args: "echoed"}
BEFORE_A_TURN = [InfraError("knowledge", "the tool server did not start")]
AFTER_A_TURN = [[("echo", {"x": "1"})], InfraError("knowledge", "the tool server went down")]


def setup_run(tmp_path, kinds=("knowledge",)):
    """A store, the schema declaring `kinds` for condition api-none, and one run key of that condition."""
    schema = treatment_schema(kinds)
    store = open_store(tmp_path, schema)
    manifest = make_manifest(schema)
    store.sync_manifest(manifest.rows)
    text = keys_under(schema, manifest, "api-none")[0]
    return schema, store, next(row.key for row in manifest.rows if row.key.text == text)


def record_for(schema, key, script):
    """The record of a scripted attempt of the key, with the condition's treatment kinds."""
    return record_run(
        script,
        ECHO,
        identity=RunIdentity(key.text, key.factors, 1),
        record_options={"treatment_kinds": schema.treatment_kinds_of(key)},
    )


def test_c01_only_an_infrastructure_outcome_after_a_completed_turn_of_a_declared_treatable_kind_is_treatment():
    """C01: the decision, in one place, over its four inputs: the outcome, the declared kinds, whether the kind can be
    a treatment, and the completed turns."""
    knowledge = Outcome("infrastructure", "down", infra_kind="knowledge")
    assert knowledge.is_treatment(("knowledge",), 1) is True
    assert knowledge.is_treatment(("knowledge",), 7) is True
    assert knowledge.is_treatment(("knowledge",), 0) is False  # before the first turn
    assert knowledge.is_treatment(("endpoint",), 1) is False  # not declared
    assert knowledge.is_treatment((), 1) is False
    integrity = Outcome("infrastructure", "changed", infra_kind="tool_plane_integrity")
    assert is_treatable("tool_plane_integrity") is False
    assert integrity.is_treatment(("tool_plane_integrity",), 3) is False  # declared, but never treatable
    assert Outcome("finished").is_treatment(("knowledge",), 3) is False


def test_c01_a_knowledge_failure_before_the_first_turn_is_set_aside_by_the_record_and_by_the_store(tmp_path):
    """C01: under a condition declaring knowledge as treatment, a knowledge failure before the model's first turn is
    set aside: the record says so, the store charges it and runs the attempt again, and the two agree."""
    schema, store, key = setup_run(tmp_path)
    record = record_for(schema, key, BEFORE_A_TURN)
    assert record.treatment_kinds == ["knowledge"]
    assert record.transcript == []
    assert (record.outcome["infra_kind"], record.outcome["treatment"], record.set_aside) == ("knowledge", False, True)
    result = result_from_record(record, ended_utc=LATER)
    assert (result.model_turns, result.recorded_treatment) == (0, False)
    begun = store.begin_attempt(key.text, NOW, 1, "")
    assert store.finish_attempt(begun.id, result, 3) == "pending"  # run again, not final
    row = store.q("SELECT state, charged, treatment FROM attempts")[0]
    assert (row["state"], row["charged"], row["treatment"]) == ("infra_fail", 1, 0)


def test_c01_a_knowledge_failure_after_the_first_turn_is_scored_as_treatment_by_both(tmp_path):
    """C01: the same declared kind, failing after the model completed a turn, is the manipulation: a scored final
    attempt in the record and in the store."""
    schema, store, key = setup_run(tmp_path)
    record = record_for(schema, key, AFTER_A_TURN)
    assert len(record.transcript) == 1
    assert (record.outcome["infra_kind"], record.outcome["treatment"], record.set_aside) == ("knowledge", True, False)
    result = result_from_record(record, ended_utc=LATER)
    assert (result.model_turns, result.recorded_treatment) == (1, True)
    begun = store.begin_attempt(key.text, NOW, 1, "")
    assert store.finish_attempt(begun.id, result, 3) == "done"
    row = store.q("SELECT state, charged, treatment FROM attempts")[0]
    assert (row["state"], row["charged"], row["treatment"]) == ("done", 0, 1)


@pytest.mark.parametrize(("script", "scored"), [(BEFORE_A_TURN, False), (AFTER_A_TURN, True)])
def test_c01_a_replay_derives_the_same_treatment_flag_as_the_record(script, scored, tmp_path):
    """C01: replaying a record re-derives the outcome's treatment flag from the replayed turns, and gets the stored
    one, whether the failure came before the first turn or after it."""
    schema, _, key = setup_run(tmp_path)
    record = record_for(schema, key, script)
    replayed = replay_record(record)
    assert record.outcome["treatment"] is scored
    assert replayed.outcome["treatment"] is scored
    assert replayed.treatment_kinds == record.treatment_kinds
    assert replayed.set_aside is (not scored)


def test_c01_the_store_refuses_a_result_whose_record_flag_it_does_not_share_and_the_attempt_stays_running(tmp_path):
    """C01: the cross-check in finish_attempt. A result that claims one completed turn while its record says the
    failure was not treatment is a record and a store that disagree: refused, the attempt stays running."""
    schema, store, key = setup_run(tmp_path)
    early = result_from_record(record_for(schema, key, BEFORE_A_TURN), ended_utc=LATER)
    begun = store.begin_attempt(key.text, NOW, 1, "")
    liar = dataclasses.replace(early, model_turns=1)  # the store would score it; the record says it was not
    with pytest.raises(TreatmentKindsMismatch, match="record says treatment=False but the store decides True"):
        store.finish_attempt(begun.id, liar, 3)
    assert store.q("SELECT state FROM attempts")[0]["state"] == "running"
    flipped = dataclasses.replace(
        result_from_record(record_for(schema, key, AFTER_A_TURN), ended_utc=LATER), model_turns=0
    )
    with pytest.raises(TreatmentKindsMismatch, match="record says treatment=True but the store decides False"):
        store.finish_attempt(begun.id, flipped, 3)
    assert store.finish_attempt(begun.id, early, 3) == "pending"


def test_c01_a_result_with_no_record_has_no_completed_turn_and_is_never_treatment(tmp_path):
    """C01: an executor that raises returns no record, so no turn is known: even a declared kind is set aside, and
    there is no record flag to cross-check."""
    setup_run(tmp_path)
    result = failure_result(InfraError("knowledge", "raised outside the loop"), ended_utc=LATER, wall_s=1.0)
    assert (result.model_turns, result.recorded_treatment) == (0, None)
    assert classify(dataclasses.replace(result, treatment_kinds=("knowledge",)), ("knowledge",)) == (
        "infra_fail",
        True,
        False,
    )


def test_c01_the_marker_keeps_the_turns_and_the_flag_so_recovery_decides_as_a_live_finish_does(tmp_path):
    """C01: a kill between the marker and the store leaves the marker as the only record: it carries the completed
    turns and the record's flag, and the next open commits the same decision the live finish would have made."""
    schema, store, key = setup_run(tmp_path)
    results = {
        "early": result_from_record(record_for(schema, key, BEFORE_A_TURN), ended_utc=LATER),
        "late": result_from_record(record_for(schema, key, AFTER_A_TURN), ended_utc=LATER),
    }
    for name, expected in (("early", ("infra_fail", 1, 0)), ("late", ("done", 0, 1))):
        begun = store.begin_attempt(key.text, NOW, 1, str(tmp_path / name))
        write_marker(tmp_path / name, key.text, begun.id, results[name])
        assert read_marker(tmp_path / name, key.text, begun.id) == results[name]
        assert recover(store, LATER, 3).committed == 1
        row = store.q("SELECT state, charged, treatment FROM attempts WHERE id=?", (begun.id,))[0]
        assert (row["state"], row["charged"], row["treatment"]) == expected


def test_c01_a_marker_with_a_negative_or_boolean_turn_count_is_not_a_result():
    """C01: the count of completed turns is a count."""
    for bad in (-1, True, 1.5, "1"):
        with pytest.raises(ValueError, match="model_turns"):
            AttemptResult(infra("endpoint").outcome, LATER, 1.0, model_turns=bad)
    with pytest.raises(ValueError, match="recorded_treatment"):
        AttemptResult(infra("endpoint").outcome, LATER, 1.0, recorded_treatment="yes")


def record_of_version(schema, key, script, version):
    """The record of a scripted attempt of the key, written as the given schema version."""
    return record_run(
        script,
        ECHO,
        identity=RunIdentity(key.text, key.factors, 1),
        record_options={"treatment_kinds": schema.treatment_kinds_of(key), "schema_version": version},
    )


def test_c01_the_store_decides_with_the_rule_of_the_records_version(tmp_path):
    """C01: a result from a version-1 record is decided without the first-turn rule, as the record flagged it, and
    one from a version-2 record with it; the cross-check in finish_attempt holds each to its own version's rule."""
    schema, store, key = setup_run(tmp_path)
    old = record_of_version(schema, key, BEFORE_A_TURN, "expcore-record/1")
    new = record_of_version(schema, key, BEFORE_A_TURN, "expcore-record/2")
    old_result, new_result = (result_from_record(r, ended_utc=LATER) for r in (old, new))
    assert (old_result.first_turn_rule, old_result.recorded_treatment, old_result.model_turns) == (False, True, 0)
    assert (new_result.first_turn_rule, new_result.recorded_treatment, new_result.model_turns) == (True, False, 0)
    begun = store.begin_attempt(key.text, NOW, 1, "")
    wrong_rule = dataclasses.replace(
        old_result, first_turn_rule=True
    )  # the store would set it aside; the record scored it
    with pytest.raises(TreatmentKindsMismatch, match="record says treatment=True but the store decides False"):
        store.finish_attempt(begun.id, wrong_rule, 3)
    assert store.finish_attempt(begun.id, old_result, 3) == "done"  # version 1: treatment by the declared kind alone
    assert store.q("SELECT state, charged, treatment FROM attempts")[0][:] == ("done", 0, 1)


def test_c01_the_same_failure_in_a_version_two_record_is_set_aside_by_the_store(tmp_path):
    """C01: the version-2 record of that failure says set aside and the store agrees."""
    schema, store, key = setup_run(tmp_path)
    new = record_of_version(schema, key, BEFORE_A_TURN, "expcore-record/2")
    begun = store.begin_attempt(key.text, NOW, 1, "")
    assert store.finish_attempt(begun.id, result_from_record(new, ended_utc=LATER), 3) == "pending"
    assert store.q("SELECT state, charged, treatment FROM attempts")[0][:] == ("infra_fail", 1, 0)
