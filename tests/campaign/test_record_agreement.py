"""The record and the store agree on which infrastructure failures are scored, as both read the condition's kinds."""

import pytest
from campaign_support import LATER, NOW, keys_under, make_manifest, open_store, treatment_schema
from record_support import no_secrets_in_the_environment, record_run

from expcore.campaign.markers import AttemptResult
from expcore.errors import InfraError
from expcore.loop.outcomes import Outcome
from expcore.records.build import RunIdentity

pytestmark = pytest.mark.usefixtures("no_secrets_in_the_environment")
__all__ = ["no_secrets_in_the_environment"]


@pytest.mark.parametrize(
    ("condition", "kind", "scored"),
    [("api-none", "endpoint", True), ("api-none", "sandbox", False), ("mount-read", "endpoint", False)],
)
def test_c01_the_record_and_the_store_agree_on_which_infrastructure_failure_is_scored(
    tmp_path, condition, kind, scored
):
    """C01: adds that a record built with the condition's kinds and the store's classification give one answer."""
    schema = treatment_schema(("endpoint",))
    store = open_store(tmp_path, schema)
    manifest = make_manifest(schema)
    store.sync_manifest(manifest.rows)
    text = keys_under(schema, manifest, condition)[0]
    key = next(row.key for row in manifest.rows if row.key.text == text)

    record = record_run(
        [InfraError(kind, "HTTP 503 after 8 retries")],
        {},
        identity=RunIdentity(key.text, key.factors, 1),
        record_options={"treatment_kinds": schema.treatment_kinds_of(key)},
    )
    held = record.outcome
    outcome = Outcome(held["name"], held["detail"], infra_kind=held["infra_kind"])
    begun = store.begin_attempt(key.text, NOW, 1, "")
    store.finish_attempt(begun.id, AttemptResult(outcome, LATER, 1.0), 3)

    row = store.q("SELECT * FROM attempts")[0]
    assert record.outcome["treatment"] is scored
    assert bool(row["treatment"]) is scored
    assert record.set_aside is (row["state"] != "done")
    assert record.set_aside is (not scored)
    assert record.treatment_kinds == list(schema.treatment_kinds[condition])


def test_c01_a_record_built_without_the_condition_s_kinds_is_the_one_that_would_contradict_the_store(tmp_path):
    """C01: adds the failure the lookup prevents: without the kinds the record calls a scored attempt aside."""
    schema = treatment_schema(("endpoint",))
    key = next(row.key for row in make_manifest(schema).rows if schema.condition_of(row.key) == "api-none")
    forgot = record_run(
        [InfraError("endpoint", "down")], {}, identity=RunIdentity(key.text, key.factors, 1), record_options={}
    )
    assert forgot.set_aside is True  # the store would score this attempt: this is what passing the kinds avoids
    passed = record_run(
        [InfraError("endpoint", "down")],
        {},
        identity=RunIdentity(key.text, key.factors, 1),
        record_options={"treatment_kinds": schema.treatment_kinds_of(key)},
    )
    assert passed.set_aside is False
