"""O11 and C31: the scheduler never scores an attempt whose record carries the fault injector's reading of a restore
that was not verified, whatever the post-run health check decides and whether or not the instrument's own check
refused.

The record is the only evidence the executor hands over (`running` returns for a restore that was not verified, so the
executor keeps its record), so the refusal is read from the record itself, in the place the post-run judgement already
sets attempts aside (`AttemptRunner._judge_instruments`).
"""

import pytest
from campaign_support import make_schema, treatment_schema
from loop_support import FakeProvider
from record_support import CODE, make_config
from scheduler_support import FakeExecutor, attempts_of, build, event_kinds, key_of

from expcore.campaign.executor import record_run
from expcore.campaign.healthgate import Health
from expcore.errors import InfraError, register_infra_kind
from expcore.records.injections import FAULT_NAMESPACE, INJECTOR_FAILURE
from expcore.records.instruments import finish_instruments

register_infra_kind(INJECTOR_FAILURE, "the fault injector could not inject, clear or verify its fault", treatable=False)

SMALL = {"tasks": ("task-a",), "seeds": (1,)}
TARGET = "providers[1]"


class Injector:
    """An instrument filed under the fault injector's namespace, with the reading the platform's injector writes: the
    restore verified or not. Its own check, when it has one, passes: nothing is refused after the run."""

    namespace = FAULT_NAMESPACE

    def __init__(self, *, verified, with_check):
        self._verified, self._identity = verified, None
        if with_check:
            self.check = lambda: None

    def start(self, identity):
        self._identity = identity

    def finish(self):
        reading = {
            "ok": self._verified,
            "run_key": self._identity["run_key"],
            "attempt": self._identity["attempt"],
            "kind": "provider_outage",
            "target": TARGET,
            "injections": [{"target": TARGET, "injected": True, "cleared": self._verified, "verified": self._verified}],
            "steps": [],
            "restore_verified": self._verified,
        }
        if not self._verified:
            reading.update(how="not_serving", identity_changed=True, serving=False)
        return reading


def running_the_injector(*, verified, script=(), kept=None, with_check=True):
    """An executor behaviour: the injector starts, the run goes through the loop, the injector finishes into the
    record, and that record is what the scheduler gets."""

    async def behave(request):
        injector = Injector(verified=verified, with_check=with_check)
        injector.start({"run_key": request.run_key, "attempt": request.identity.attempt})
        request.backend.use(list(script))
        record = await record_run(
            request,
            [FakeProvider({"look": lambda arguments: "ok"})],
            "be brief",
            "do the task",
            make_config(),
            code=dict(CODE),
            core_version="0.1.0",
        )
        record = finish_instruments(record, [injector])
        if kept is not None:
            kept.append(record)
        return record

    return behave


def passing_gate():
    """The post-run health check, which finds the platform healthy: nothing in it can set the attempt aside."""
    return Health(True, "the platform is healthy")


def rows(store, key):
    query = "SELECT state, charged, treatment, infra_kind, detail FROM attempts WHERE run_key=? ORDER BY id"
    return store.q(query, (key,))


@pytest.mark.parametrize("with_check", [True, False], ids=["its check passed", "no check at all"])
def test_o11_an_attempt_whose_record_says_the_restore_was_not_verified_is_never_scored_though_the_gate_passes(
    tmp_path, with_check
):
    """O11: the post-run health check passes, the instrument's own check passes (or it has none), and the attempt
    finished as an answer, which `classify` would score. Its record carries `restore_verified: false`, so the attempt
    is set aside as `injector_failure`, charged, and run again; only the rerun, whose restore held, is scored."""
    schema = make_schema()
    key = key_of(schema, "api-none")
    kept = []
    first = running_the_injector(verified=False, kept=kept, with_check=with_check)
    second = running_the_injector(verified=True, with_check=with_check)
    executor = FakeExecutor({key: [first, second]})
    with build(tmp_path, schema=schema, executor=executor, plan=SMALL, health=passing_gate) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        store = scheduler.store
        assert kept[0].instrument_refusal is None  # the refusal is the record's reading, not an instrument's check
        assert kept[0].outcome["name"] == "finished"  # what the loop saw
        assert attempts_of(store, key) == [("infra_fail", 1, 1), ("done", 0, 2)]
        refused, scored = rows(store, key)
        assert refused["infra_kind"] == INJECTOR_FAILURE
        assert TARGET in refused["detail"]
        assert "not_serving" in refused["detail"]
        assert (scored["state"], scored["infra_kind"]) == ("done", None)
        assert "instrument_refused" in event_kinds(store)
        assert executor.calls_of(key) == 2


def test_o11_a_scored_treatment_attempt_is_set_aside_too_when_its_record_says_the_restore_was_not_verified(tmp_path):
    """O11 and C01: under a condition whose treatment is a fault, an attempt that ended in the fault after a turn is a
    scored final attempt, and a gate that passes does not change that. A record that says the restore was not verified
    sets it aside all the same: the run's fault is not the manipulation the condition declares, so it is run again."""
    schema = treatment_schema(("knowledge",))
    key = key_of(schema, "api-none")
    script = [[("look", {})], InfraError("knowledge", "the tool server went down")]
    kept = []
    refused = running_the_injector(verified=False, script=script, kept=kept)
    executor = FakeExecutor({key: [refused, ("infra_after_turn", "knowledge")]})
    with build(tmp_path, schema=schema, executor=executor, plan=SMALL, health=passing_gate) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        first, second = rows(scheduler.store, key)
        assert kept[0].outcome["treatment"] is True  # what the loop saw, kept in the record
        assert kept[0].set_aside is True  # and the record says the attempt is not to be scored
        assert (first["state"], first["charged"], first["treatment"]) == ("infra_fail", 1, 0)
        assert first["infra_kind"] == INJECTOR_FAILURE
        assert (second["state"], second["treatment"], second["infra_kind"]) == ("done", 1, "knowledge")


def test_o11_an_attempt_whose_restore_was_verified_is_scored_as_before(tmp_path):
    """O11: a record that says the restore was verified is not refused: the first attempt is scored, once, and the run
    is not run again."""
    schema = make_schema()
    key = key_of(schema, "api-none")
    kept = []
    executor = FakeExecutor({key: [running_the_injector(verified=True, kept=kept)]})
    with build(tmp_path, schema=schema, executor=executor, plan=SMALL, health=passing_gate) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        assert attempts_of(scheduler.store, key) == [("done", 0, 1)]
        assert kept[0].instruments[FAULT_NAMESPACE]["restore_verified"] is True
        assert kept[0].set_aside is False
        assert "instrument_refused" not in event_kinds(scheduler.store)
        assert executor.calls_of(key) == 1
