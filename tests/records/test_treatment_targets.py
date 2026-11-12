"""C01: under record version 3 a declared target is treatment only for its own failure, while the injector held it down.

The record, the store, `classify` and the replay all take the decision from `Outcome.is_treatment`, with the injected
targets read from the fault injector's snapshot in the record (`expcore.records.injections`).
"""

import copy
import dataclasses

import pytest
from campaign_support import LATER, NOW, keys_under, make_manifest, make_schema, open_store
from loop_support import FakeProvider
from record_support import no_secrets_in_the_environment, record_run

from expcore.campaign.attempts import TreatmentKindsMismatch, classify
from expcore.campaign.executor import failure_result, result_from_record
from expcore.campaign.factors import FactorSchema
from expcore.campaign.markers import read_marker, write_marker
from expcore.errors import InfraError
from expcore.loop.outcomes import Outcome
from expcore.records.build import RunIdentity
from expcore.records.injections import FAULT_NAMESPACE, injected_targets
from expcore.records.record import RunRecord, decode
from expcore.records.schema import CURRENT, RecordError, validate
from expcore.replay.replay import replay_record

pytestmark = pytest.mark.usefixtures("no_secrets_in_the_environment")
__all__ = ["no_secrets_in_the_environment"]

TARGET, OTHER = "servers[1]", "servers[0]"
DECLARED = {"knowledge": TARGET}
ECHO = {"echo": lambda args: "echoed"}


def down(target=TARGET):
    """A tool server failure attributed to `target` (None: attributed to nothing)."""
    return InfraError("knowledge", "the tool server went down", target=target)


def after_a_turn(target=TARGET):
    return [[("echo", {"x": "1"})], down(target)]


def reading(identity, *targets, injected=True):
    """The fault injector's snapshot of an attempt that took `targets` down."""
    return {
        "ok": True,
        "run_key": identity.run_key,
        "attempt": identity.attempt,
        "injections": [{"target": t, "injected": injected} for t in targets],
    }


def run(script, *, targets=DECLARED, identity=None, providers=None):
    """A record of a scripted run under a condition declaring knowledge, with `targets`, as its treatment."""
    options = {"treatment_kinds": ["knowledge"], "treatment_targets": targets}
    extra = {} if identity is None else {"identity": identity}
    return record_run(script, ECHO, providers=providers, record_options=options, **extra)


def injected(record, *targets, **kwargs):
    """The record once the fault injector's snapshot is added, as `finish_instruments` adds it."""
    identity = RunIdentity(record.run_key, record.factors, record.attempt)
    return record.with_instrument(FAULT_NAMESPACE, reading(identity, *targets, **kwargs))


def test_c01_the_decision_over_the_target_and_the_injection():
    """C01: a declared target needs the failure to name it and the injector to have taken it down; an undeclared target
    leaves the kind alone to decide, whatever the failure names."""
    named = Outcome("infrastructure", "down", infra_kind="knowledge", infra_target=TARGET)
    assert named.is_treatment(("knowledge",), 1, treatment_targets=DECLARED, injected={TARGET}) is True
    assert named.is_treatment(("knowledge",), 1, treatment_targets=DECLARED, injected=()) is False
    assert named.is_treatment(("knowledge",), 1, treatment_targets=DECLARED, injected={OTHER}) is False
    assert named.is_treatment(("knowledge",), 0, treatment_targets=DECLARED, injected={TARGET}) is False
    other = Outcome("infrastructure", "down", infra_kind="knowledge", infra_target=OTHER)
    assert other.is_treatment(("knowledge",), 1, treatment_targets=DECLARED, injected={TARGET, OTHER}) is False
    unnamed = Outcome("infrastructure", "down", infra_kind="knowledge")
    assert unnamed.is_treatment(("knowledge",), 1, treatment_targets=DECLARED, injected={TARGET}) is False
    assert other.is_treatment(("knowledge",), 1) is True  # declared with no target: the kind decides
    assert unnamed.is_treatment(("knowledge",), 1) is True
    assert named.is_treatment(("knowledge",), 1, target_rule=False, treatment_targets=DECLARED) is True


@pytest.mark.parametrize("label", ["provider-a", "providers", "providers[01]", "Providers[1]", "providers[-1]", ""])
def test_c01_a_target_is_a_position_label_never_a_name(label):
    """C01: what a failure names is a table and a position, so a configured name cannot reach a record."""
    with pytest.raises(ValueError, match="position label"):
        InfraError("knowledge", "down", target=label)
    with pytest.raises(ValueError, match="position label"):
        Outcome("infrastructure", "down", infra_kind="knowledge", infra_target=label)
    assert InfraError("knowledge", "down", target="providers[12]").target == "providers[12]"
    with pytest.raises(ValueError, match="only an infrastructure outcome names a target"):
        Outcome("finished", infra_target="providers[1]")


def test_c01_the_manipulated_target_failing_while_injected_is_treatment_and_replays_so():
    """C01: the failure names the declared target and the injector's snapshot records it injected in this attempt:
    the record built after the instruments finished flags treatment, writes back byte for byte and replays to it."""
    loop_record = run(after_a_turn())
    assert loop_record.schema_version == CURRENT == "expcore-record/3"
    assert (loop_record.outcome["infra_target"], loop_record.treatment_targets) == (TARGET, DECLARED)
    assert loop_record.outcome["treatment"] is False  # no injector reading yet: nothing shows the target was down
    record = injected(loop_record, TARGET)
    assert (record.outcome["treatment"], record.set_aside) == (True, False)
    text = record.dumps()
    assert RunRecord.from_json(decode(text.encode())).dumps() == text
    replayed = replay_record(record)
    assert replayed.outcome["treatment"] is True
    assert replayed.dumps() == text


def test_c01_a_tool_failure_naming_the_target_replays_with_its_target():
    """C01: a tool provider's failure keeps its target through the record and the replay."""
    script = [[("fetch", {"x": "1"})], "never reached"]

    def fetch(args):
        raise down()

    record = injected(run(script, providers=[FakeProvider({"fetch": fetch})]), TARGET)
    assert (record.outcome["infra_target"], record.outcome["treatment"]) == (TARGET, True)
    assert replay_record(record).dumps() == record.dumps()


@pytest.mark.parametrize(
    ("script", "snapshot", "why"),
    [
        (after_a_turn(OTHER), (TARGET,), "another provider failed, the agent's own included"),
        (after_a_turn(None), (TARGET,), "the failure names no target"),
        (after_a_turn(), (), "the injection did not happen"),
        (after_a_turn(), (OTHER,), "another target was injected"),
        ([down()], (TARGET,), "the failure came before the first model turn"),
    ],
)
def test_c01_anything_else_under_a_declared_target_is_set_aside(script, snapshot, why):
    """C01: another target's spontaneous failure, a failure naming none, an injection that did not happen and a
    failure before the first turn are set aside by the record and by its replay."""
    record = injected(run(script), *snapshot)
    assert (record.outcome["treatment"], record.set_aside) == (False, True), why
    assert replay_record(record).outcome["treatment"] is False


def test_c01_an_injection_recorded_as_not_verified_or_for_another_attempt_does_not_count():
    """C01: only an entry marked injected, in a snapshot of this run's attempt, counts."""
    record = run(after_a_turn())
    assert injected(record, TARGET, injected=False).outcome["treatment"] is False
    stale = RunIdentity(record.run_key, record.factors, record.attempt + 1)
    assert record.with_instrument(FAULT_NAMESPACE, reading(stale, TARGET)).outcome["treatment"] is False
    assert injected_targets({FAULT_NAMESPACE: {"ok": False, "error": "InfraError"}}, record.run_key, 1) == frozenset()


def test_r02_a_version_three_record_whose_flag_ignores_the_injector_is_refused():
    """R02: the flag must be what the outcome, the declared targets and the injector's reading make it."""
    data = decode(injected(run(after_a_turn()), TARGET).dumps().encode())
    assert validate(data) == CURRENT
    without = copy.deepcopy(data)
    del without["instruments"][FAULT_NAMESPACE]
    with pytest.raises(RecordError, match=r"outcome\.treatment: is True but .* make it False"):
        validate(without)
    stray = copy.deepcopy(data)
    stray["treatment_targets"] = {"endpoint": TARGET}
    with pytest.raises(RecordError, match="treatment_targets: 'endpoint' is not one of the record's treatment_kinds"):
        validate(stray)
    named = copy.deepcopy(data)
    named["treatment_targets"] = {"knowledge": "server-a"}
    with pytest.raises(RecordError, match="position label"):
        validate(named)


def test_r02_a_version_two_record_cannot_hold_a_target():
    """R02: the target fields are version 3's; a version-2 record holding one is refused, and a live record of an older
    version is refused before it is built."""
    data = decode(injected(run(after_a_turn()), TARGET).dumps().encode())
    data["schema_version"] = "expcore-record/2"
    with pytest.raises(RecordError, match="added by a later schema"):
        validate(data)
    options = {"treatment_kinds": ["knowledge"], "treatment_targets": DECLARED, "schema_version": "expcore-record/2"}
    with pytest.raises(RecordError, match="predates declared targets"):
        record_run(after_a_turn(), ECHO, record_options=options)


def test_c01_a_condition_may_declare_a_kind_with_its_target(tmp_path):
    """C01: the table form beside the plain names; the targets are part of the schema, and a schema with none keeps
    the digest it had before targets existed."""
    base = make_schema()
    plain = FactorSchema(base.declared, base.order, base.conditions, treatment={"api-none": ["knowledge"]})
    targeted = FactorSchema(
        base.declared,
        base.order,
        base.conditions,
        treatment={"api-none": ["endpoint", {"kind": "knowledge", "target": TARGET}]},
    )
    assert targeted.treatment_kinds["api-none"] == ("endpoint", "knowledge")
    assert targeted.treatment_targets["api-none"] == DECLARED
    assert plain.treatment_targets["api-none"] == {}
    assert plain.to_json()["treatment"]["api-none"] == ["knowledge"]
    assert targeted.to_json()["treatment"]["api-none"] == ["endpoint", {"kind": "knowledge", "target": TARGET}]
    assert plain.digest != targeted.digest


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"kind": "knowledge", "target": "server-a"}, "position label"),
        ({"kind": "knowledge"}, "a name or a table"),
        ({"kind": "knowledge", "target": TARGET, "also": 1}, "a name or a table"),
        ({"kind": "", "target": TARGET}, "must be an infrastructure kind"),
        ({"kind": "tool_plane_integrity", "target": TARGET}, "can never be a treatment"),
    ],
)
def test_c01_a_malformed_targeted_declaration_is_refused(entry, message):
    """C01: a table entry holds exactly a treatable kind and a position label."""
    base = make_schema()
    with pytest.raises(Exception, match=message):
        FactorSchema(base.declared, base.order, base.conditions, treatment={"api-none": [entry]})


def targeted_store(tmp_path):
    base = make_schema()
    schema = FactorSchema(
        base.declared, base.order, base.conditions, treatment={"api-none": [{"kind": "knowledge", "target": TARGET}]}
    )
    store = open_store(tmp_path, schema)
    manifest = make_manifest(schema)
    store.sync_manifest(manifest.rows)
    text = keys_under(schema, manifest, "api-none")[0]
    return schema, store, next(row.key for row in manifest.rows if row.key.text == text)


def record_of(schema, key, script, *snapshot):
    identity = RunIdentity(key.text, key.factors, 1)
    record = record_run(
        script,
        ECHO,
        identity=identity,
        record_options={
            "treatment_kinds": schema.treatment_kinds_of(key),
            "treatment_targets": schema.treatment_targets_of(key),
        },
    )
    return record.with_instrument(FAULT_NAMESPACE, reading(identity, *snapshot))


@pytest.mark.parametrize(
    ("script", "snapshot", "state", "treatment"),
    [
        (after_a_turn(), (TARGET,), "done", 1),
        (after_a_turn(OTHER), (TARGET,), "infra_fail", 0),
        (after_a_turn(), (), "infra_fail", 0),
        ([down()], (TARGET,), "infra_fail", 0),
    ],
)
def test_c01_the_store_takes_the_same_decision_from_the_result(tmp_path, script, snapshot, state, treatment):
    """C01: the store decides from the result's outcome, declared targets and injected targets, agrees with the
    record's flag, and scores only the manipulated target's failure while it was down."""
    schema, store, key = targeted_store(tmp_path)
    record = record_of(schema, key, script, *snapshot)
    result = result_from_record(record, ended_utc=LATER)
    assert result.injected_targets == snapshot
    assert (result.treatment_targets, result.target_rule) == (DECLARED, True)
    assert classify(result, ("knowledge",), DECLARED).treatment is bool(treatment)
    begun = store.begin_attempt(key.text, NOW, 1, "")
    store.finish_attempt(begun.id, result, 3)
    row = store.q("SELECT state, treatment FROM attempts")[0]
    assert (row["state"], row["treatment"]) == (state, treatment)


def test_c01_the_store_refuses_a_result_whose_targets_differ_from_the_condition(tmp_path):
    """C01: the record's declared targets must be the condition's, as its kinds must."""
    schema, store, key = targeted_store(tmp_path)
    record = record_of(schema, key, after_a_turn(), TARGET)
    result = result_from_record(record, ended_utc=LATER)
    begun = store.begin_attempt(key.text, NOW, 1, "")
    wrong = dataclasses.replace(result, treatment_targets={"knowledge": OTHER})
    with pytest.raises(TreatmentKindsMismatch, match="treatment targets"):
        store.finish_attempt(begun.id, wrong, 3)
    flagged = dataclasses.replace(result, injected_targets=())
    with pytest.raises(TreatmentKindsMismatch, match="injected targets"):
        store.finish_attempt(begun.id, flagged, 3)
    assert store.q("SELECT state FROM attempts")[0]["state"] == "running"


def test_c01_an_executor_that_raised_names_the_target_and_is_never_treatment():
    """C01: with no record there is no completed turn, so the target's failure is set aside."""
    result = failure_result(
        down(), ended_utc=LATER, wall_s=1.0, treatment_kinds=("knowledge",), treatment_targets=DECLARED
    )
    assert (result.outcome.infra_target, result.model_turns, result.injected_targets) == (TARGET, 0, ())
    assert classify(result, ("knowledge",), DECLARED).treatment is False


def test_c04_a_marker_keeps_the_target_the_declared_targets_and_the_injected_ones(tmp_path):
    """C04: the marker a kill leaves behind holds everything the store's decision needs."""
    record = injected(run(after_a_turn()), TARGET)
    result = result_from_record(record, ended_utc=LATER)
    write_marker(tmp_path, "k", 1, result)
    again = read_marker(tmp_path, "k", 1)
    assert again == result
    assert (again.outcome.infra_target, again.treatment_targets, again.injected_targets) == (
        TARGET,
        DECLARED,
        (TARGET,),
    )
