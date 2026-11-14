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
from expcore.records.build import RunIdentity
from expcore.records.instruments import finish_instruments, start_instruments
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
