"""C01: under the outage condition only the manipulated provider's failure, while the injector held it down, is scored.

The acceptance of the fault injector, end to end against fakes: the condition declares `provider_outage` with the
label of the provider it takes down; the injector is an instrument around the agent's run; the record, the store and
the replay take the same decision.
"""

import dataclasses

import pytest
from campaign_support import LATER, NOW, keys_under, make_manifest, make_schema, open_store
from fault_support import OUTAGE_TARGET, FakeServices, injector_for, write_faults
from loop_support import FakeProvider
from record_support import hand_made, record_run

from expcore.campaign.executor import result_from_record
from expcore.campaign.factors import FactorSchema
from expcore.config.loading import ConfigError
from expcore.loop.outcomes import Outcome
from expcore.records.build import RunIdentity
from expcore.records.instruments import finish_instruments, start_instruments
from expcore.records.record import decode
from expcore.records.schema import CURRENT, RecordError, validate
from expcore.replay.replay import replay_record
from expcore_onedata.config import load_platform
from expcore_onedata.faults import PROVIDER_OUTAGE, provider_outage


@pytest.fixture
def campaign(tmp_path):
    """A store whose condition api-none takes providers[1] down, one run key of it, and the platform's file."""
    base = make_schema()
    declared = {"api-none": [{"kind": PROVIDER_OUTAGE, "target": OUTAGE_TARGET}]}
    schema = FactorSchema(base.declared, base.order, base.conditions, treatment=declared)
    store = open_store(tmp_path, schema)
    manifest = make_manifest(schema)
    store.sync_manifest(manifest.rows)
    text = keys_under(schema, manifest, "api-none")[0]
    key = next(row.key for row in manifest.rows if row.key.text == text)
    return schema, store, key, write_faults(tmp_path)


def attempt(campaign, failing: str | None, *, before_a_turn: bool = False):
    """Run one attempt the way an executor does, with the injector as an instrument and cleared in finally; the agent
    reads through a tool that fails when `failing` (a provider's name in the fixture) is unavailable."""
    schema, store, key, platform = campaign
    config = load_platform(platform, require_secrets=False)
    services = FakeServices()
    injector = injector_for(platform, services, target=schema.treatment_targets_of(key)[PROVIDER_OUTAGE])
    identity = RunIdentity(key.text, key.factors, 1)

    def read(arguments):
        raise provider_outage(config, failing, "the provider did not answer the read")

    script = [provider_outage(config, failing, "down")] if before_a_turn else [[("read", {"x": "1"})], "unreached"]
    start_instruments([injector], dataclasses.asdict(identity))
    try:
        record = record_run(
            script,
            {},
            providers=[FakeProvider({"read": read})],
            identity=identity,
            record_options={
                "treatment_kinds": schema.treatment_kinds_of(key),
                "treatment_targets": schema.treatment_targets_of(key),
            },
        )
    finally:
        injector.clear()
    record = finish_instruments(record, [injector])
    begun = store.begin_attempt(key.text, NOW, 1, "")
    store.finish_attempt(begun.id, result_from_record(record, ended_utc=LATER), 3)
    row = store.q("SELECT state, treatment, infra_kind FROM attempts")[0]
    return record, (row["state"], row["treatment"], row["infra_kind"]), services


def test_c01_under_the_outage_condition_the_manipulated_providers_failure_is_scored_as_treatment(campaign):
    """C01: the failure names providers[1], the injector held providers[1] down, a model turn completed: the record,
    the store and the replay all score it as the condition's treatment, and the provider is back afterwards."""
    record, row, services = attempt(campaign, "beta")
    assert (record.outcome["infra_target"], record.outcome["treatment"]) == (OUTAGE_TARGET, True)
    assert row == ("done", 1, PROVIDER_OUTAGE)
    assert replay_record(record).outcome["treatment"] is True
    assert record.instruments["fault_injector"]["ok"] is True
    assert all(services.running(service) for service in ("store-one", "store-two"))


def test_c01_a_run_where_a_different_provider_fails_is_set_aside(campaign):
    """C01: the agent's own provider (providers[0]) failing during the outage is not the manipulation."""
    record, row, _ = attempt(campaign, "alpha")
    assert (record.outcome["infra_target"], record.outcome["treatment"]) == ("providers[0]", False)
    assert row == ("infra_fail", 0, PROVIDER_OUTAGE)
    assert replay_record(record).outcome["treatment"] is False


def test_c01_a_run_that_failed_before_its_first_model_turn_is_set_aside(campaign):
    """C01: the manipulated provider's failure before the agent's first turn says nothing about the manipulation."""
    record, row, _ = attempt(campaign, "beta", before_a_turn=True)
    assert (record.transcript, record.outcome["infra_target"], record.outcome["treatment"]) == (
        [],
        OUTAGE_TARGET,
        False,
    )
    assert row == ("infra_fail", 0, PROVIDER_OUTAGE)


HELD, SPONTANEOUS = "providers[1]", "providers[0]"


def outage_of(target):
    """A provider outage naming `target` (None: naming none)."""
    return Outcome("infrastructure", "down", infra_kind=PROVIDER_OUTAGE, infra_target=target)


@pytest.mark.parametrize(
    ("named", "injected", "expected", "why"),
    [
        (HELD, {HELD}, True, "the declared provider failed while the injector held it down"),
        (SPONTANEOUS, {HELD}, False, "another provider's outage is not the manipulation"),
        (SPONTANEOUS, {HELD, SPONTANEOUS}, False, "nor is it when the injector held it down too: it is not declared"),
        (HELD, set(), False, "the declared provider failed with no injection of it: nothing shows it was held down"),
        (HELD, {SPONTANEOUS}, False, "the injector held another provider down, not the declared one"),
        (None, {HELD}, False, "an outage that names no provider is not the declared one"),
    ],
)
def test_c34_an_outage_is_the_treatment_only_of_the_declared_provider_while_the_injector_held_it(
    named, injected, expected, why
):
    """C34: with `providers[1]` declared, only an outage that names it, while the injector's snapshot shows it held
    down, is the treatment; another provider's outage is never counted, whatever else the injector held."""
    declared = {PROVIDER_OUTAGE: HELD}
    assert (
        outage_of(named).is_treatment((PROVIDER_OUTAGE,), 1, treatment_targets=declared, injected=injected) is expected
    ), why


@pytest.mark.parametrize("target_rule", [True, False], ids=["target-rule", "legacy-rule"])
@pytest.mark.parametrize("declared", [None, {}], ids=["no-table", "empty-table"])
@pytest.mark.parametrize("named", [HELD, SPONTANEOUS, None])
def test_c34_an_outage_declared_with_no_target_is_never_the_treatment(named, declared, target_rule):
    """C34: a `provider_outage` condition that carries no target (a schema cut to the kind names alone) never scores
    an outage as its treatment, whichever provider failed, even with the injector's snapshot holding it down and
    whichever rule of record version decides: the target is what says which outage is the manipulation."""
    outcome = outage_of(named)
    options = {"target_rule": target_rule, "treatment_targets": declared, "injected": {HELD, SPONTANEOUS}}
    assert outcome.is_treatment((PROVIDER_OUTAGE,), 1, **options) is False
    assert outcome.is_treatment((PROVIDER_OUTAGE,), 1, first_turn_rule=False, **options) is False


def test_c34_the_declared_target_of_an_outage_is_checked_whatever_rule_the_record_was_made_under():
    """C34: the target clause is not waived for an outage by `target_rule=False`: the declared provider, held down by
    the injector, is the only outage that is the treatment."""
    options = {"target_rule": False, "treatment_targets": {PROVIDER_OUTAGE: HELD}}
    assert outage_of(SPONTANEOUS).is_treatment((PROVIDER_OUTAGE,), 1, injected={HELD, SPONTANEOUS}, **options) is False
    assert outage_of(HELD).is_treatment((PROVIDER_OUTAGE,), 1, injected=set(), **options) is False
    assert outage_of(HELD).is_treatment((PROVIDER_OUTAGE,), 1, injected={HELD}, **options) is True


def test_c34_a_kind_without_the_requirement_is_still_decided_by_its_name_alone():
    """C34: the requirement is the outage's, not every kind's: a plain `endpoint` declaration still scores any
    endpoint failure, as it did."""
    endpoint = Outcome("infrastructure", "down", infra_kind="endpoint")
    assert endpoint.is_treatment(("endpoint",), 1) is True


def test_c34_a_record_cannot_declare_a_provider_outage_without_its_target():
    """C34: the record is refused when it is built, and when one is read back, if it declares `provider_outage` and no
    target, so no record of an untargeted outage condition is written or accepted."""
    script = [[("read", {"x": "1"})], "unreached"]
    options = {"treatment_kinds": [PROVIDER_OUTAGE]}
    with pytest.raises(RecordError, match="treatment_kinds: 'provider_outage' is declared with no target"):
        record_run(script, {}, providers=[FakeProvider({"read": lambda arguments: "ok"})], record_options=options)
    options["treatment_targets"] = {PROVIDER_OUTAGE: HELD}
    record = record_run(script, {}, providers=[FakeProvider({"read": lambda arguments: "ok"})], record_options=options)
    data = decode(record.dumps().encode())
    assert validate(data) == CURRENT
    del data["treatment_targets"]
    with pytest.raises(RecordError, match="treatment_kinds: 'provider_outage' is declared with no target"):
        validate(data)


def test_c34_a_schema_cut_to_the_kind_names_alone_is_refused_naming_the_condition_and_the_kind(campaign):
    """C34: the cut a study made to run one condition, the kinds without their targets, cannot be built for the outage
    condition: it would have scored any provider's outage, and the store would have compared the record to the cut."""
    schema, *_ = campaign
    names_only = {name: schema.treatment_kinds[name] for name in schema.conditions}
    with pytest.raises(ConfigError) as caught:
        FactorSchema(schema.declared, schema.order, schema.conditions, treatment=names_only)
    message = str(caught.value)
    assert "[conditions.api-none]" in message
    assert "'provider_outage'" in message
    assert "no target" in message


class Reading:
    """An instrument that records whether the manipulated provider's service runs at the moment it finishes."""

    namespace = "reading"

    def __init__(self, services):
        self.services = services

    def start(self, identity):
        pass

    def finish(self):
        return {"target_running": self.services.running("store-two")}


@pytest.mark.parametrize(("probe_first", "read_target_running"), [(False, False), (True, True)])
def test_c31_a_probe_listed_after_the_injector_finishes_before_the_target_is_restored(
    tmp_path, probe_first, read_target_running
):
    """C31: the injector's `finish` restores the provider, so an instrument listed after it finishes first and reads
    the provider down, and one listed before it finishes after the restore and reads it back. Either way the injector
    restored the provider and says so."""
    services = FakeServices()
    injector, probe = injector_for(write_faults(tmp_path), services), Reading(services)
    instruments = [probe, injector] if probe_first else [injector, probe]
    start_instruments(instruments, {"run_key": "run-1", "attempt": 1})
    assert not services.running("store-two")  # injected before the run
    record = finish_instruments(hand_made(), instruments)
    assert record.instruments["reading"] == {"target_running": read_target_running}
    assert record.instruments["fault_injector"]["ok"] is True
    assert services.running("store-two")
