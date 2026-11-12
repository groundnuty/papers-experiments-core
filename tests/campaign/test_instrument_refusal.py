"""C31: the scheduler sets a finished attempt aside when an instrument refused it, in the same place as the post-run
health check and the served-identity check, and charges and reruns it as any infrastructure failure."""

import pytest
from campaign_support import make_manifest, make_schema, treatment_schema
from loop_support import FakeProvider
from record_support import CODE, make_config
from scheduler_support import FakeExecutor, attempts_of, build, event_kinds, key_of, settings_for

from expcore.campaign.executor import record_run
from expcore.campaign.healthgate import Health
from expcore.errors import InfraError
from expcore.records.instruments import finish_instruments, first_turn_instruments

SMALL = {"tasks": ("task-a",), "seeds": (1,)}
REFUSED = "tool_plane_integrity"  # a core kind no condition may declare as its treatment


class Refuser:
    """An instrument whose check refuses, or whose first-turn hook does."""

    namespace = "manipulation"

    def __init__(self, *, check=None, hook=None):
        self._check, self._hook = check, hook

    def start(self, identity):
        pass

    def at_first_turn(self, moment):
        if self._hook is not None:
            raise self._hook

    def finish(self):
        return {"state": "recorded"}

    def check(self):
        if self._check is not None:
            raise self._check


def run_with(instrument, script=(), kept=None):
    """An executor behaviour: the run goes through the loop, the instrument finishes and checks, and its record is
    what the scheduler gets (and what `kept`, when given, collects)."""

    async def behave(request):
        first_turn_instruments([instrument], request.timer)
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
        record = finish_instruments(record, [instrument])
        if kept is not None:
            kept.append(record)
        return record

    return behave


def refusing(detail="the plan failed mid-run"):
    return Refuser(check=InfraError(REFUSED, detail))


def attempt_rows(store, key):
    query = "SELECT state, charged, treatment, infra_kind, detail FROM attempts WHERE run_key=? ORDER BY id"
    return store.q(query, (key,))


def test_c31_an_attempt_the_loop_scored_is_set_aside_when_an_instrument_refuses_it_and_is_run_again(tmp_path):
    """C31: the agent finished its run, and the instrument then found the manipulation was not realised: the attempt
    is infrastructure of the instrument's kind, charged, kept for diagnosis and rerun; the rerun is scored."""
    schema = make_schema()
    key = key_of(schema, "api-none")
    executor = FakeExecutor({key: [run_with(refusing())]})
    with build(tmp_path, schema=schema, executor=executor, plan=SMALL) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        store = scheduler.store
        assert attempts_of(store, key) == [("infra_fail", 1, 1), ("done", 0, 2)]
        first = attempt_rows(store, key)[0]
        assert first["infra_kind"] == REFUSED
        assert "manipulation" in first["detail"]
        assert "the plan failed mid-run" in first["detail"]
        assert "instrument_refused" in event_kinds(store)
        assert executor.calls_of(key) == 2


def test_c31_a_scored_treatment_attempt_is_set_aside_too_and_the_store_and_the_record_do_not_disagree(tmp_path):
    """C31: under a condition whose treatment is a fault, an attempt that ended in the fault after a turn is a scored
    final attempt; an instrument that finds the fault was not as planned sets it aside. The record still says what the
    loop saw (treatment), the store decides otherwise, and that is not a mismatch."""
    schema = treatment_schema(("knowledge",))
    key = key_of(schema, "api-none")
    script = [[("look", {})], InfraError("knowledge", "the tool server went down")]
    records = []
    planned = run_with(refusing("the outage was not the planned one"), script, records)
    executor = FakeExecutor({key: [planned, ("infra_after_turn", "knowledge")]})
    with build(tmp_path, schema=schema, executor=executor, plan=SMALL) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        store = scheduler.store
        first, second = attempt_rows(store, key)
        assert (first["state"], first["charged"], first["treatment"]) == ("infra_fail", 1, 0)
        assert first["infra_kind"] == REFUSED
        assert (second["state"], second["treatment"], second["infra_kind"]) == ("done", 1, "knowledge")
        assert records[0].outcome["treatment"] is True  # what the loop saw, kept in the record
        assert records[0].set_aside is True  # and the record says the attempt was refused


def test_c31_an_attempt_already_set_aside_keeps_its_outcome_and_the_refusal_is_noted(tmp_path):
    """C31: an attempt the loop ended as infrastructure of another kind is set aside already; the instrument's refusal
    does not rewrite why, and the event says it stands."""
    schema = make_schema()
    key = key_of(schema, "api-none")
    behaviour = run_with(refusing(), [InfraError("endpoint", "the endpoint failed")])
    with build(tmp_path, schema=schema, executor=FakeExecutor({key: [behaviour]}), plan=SMALL) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        first = attempt_rows(scheduler.store, key)[0]
        assert (first["state"], first["infra_kind"]) == ("infra_fail", "endpoint")
        (event,) = scheduler.store.q("SELECT detail FROM events WHERE kind='instrument_refused'")
        assert "the outcome stands" in event["detail"]


def test_c31_the_post_run_health_check_decides_first_and_the_refusal_does_not_rewrite_its_outcome(tmp_path):
    """C31: the order of the checks in one place: the health check first, then the instruments', then the endpoint's
    identity. A post-run health check that failed already set the attempt aside as the health kind; the refusal that
    follows notes that the outcome stands, and the attempt is charged as the health failure it was."""

    class FailsOnce:
        """Healthy, except for the one check made after the first run, which the first run's executor arms."""

        down = False

        def __call__(self):
            was, self.down = self.down, False
            return Health(not was, "platform unreachable")

    health, schema = FailsOnce(), make_schema()
    key = make_manifest(schema, **SMALL).rows[0].key.text  # the run the scheduler dispatches first
    refuse = run_with(refusing())

    async def refuse_then_arm_the_health_check(request):
        record = await refuse(request)
        health.down = True
        return record

    executor = FakeExecutor({key: [refuse_then_arm_the_health_check]})
    with build(tmp_path, schema=schema, executor=executor, plan=SMALL, health=health) as scheduler:
        scheduler.run()
        first = attempt_rows(scheduler.store, key)[0]
        assert (first["state"], first["infra_kind"]) == ("infra_fail", "sandbox")
        assert "post-run health check failed" in first["detail"]
        (event,) = scheduler.store.q("SELECT detail FROM events WHERE kind='instrument_refused'")
        assert "the outcome stands" in event["detail"]


def test_c31_an_attempt_an_instrument_refused_at_the_first_turn_is_infrastructure_of_its_kind_and_run_again(tmp_path):
    """C31: the hook refuses before any request is sent; the executor's caller sees infrastructure of that kind, with
    no completed turn, so it is set aside whatever the condition declares, charged and rerun."""
    schema = make_schema()
    key = key_of(schema, "api-none")
    refuser = Refuser(hook=InfraError(REFUSED, "the files were not in their start state"))
    executor = FakeExecutor({key: [run_with(refuser)]})
    with build(tmp_path, schema=schema, executor=executor, plan=SMALL) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        assert attempts_of(scheduler.store, key) == [("infra_fail", 1, 1), ("done", 0, 2)]
        first = attempt_rows(scheduler.store, key)[0]
        assert first["infra_kind"] == REFUSED
        assert "start state" in first["detail"]
        assert executor.calls_of(key) == 2


@pytest.mark.parametrize("where", ["hook", "check"])
def test_c31_an_instrument_that_raises_a_kind_a_condition_could_declare_is_a_harness_defect(tmp_path, where):
    """C31: refusing with a treatable kind would let the refused attempt be scored, so it is the instrument's defect:
    the attempt is a harness error, not infrastructure of that kind."""
    schema = treatment_schema(("knowledge",))
    key = key_of(schema, "api-none")
    wrong = InfraError("knowledge", "down")
    refuser = Refuser(hook=wrong) if where == "hook" else Refuser(check=wrong)
    executor = FakeExecutor({key: [run_with(refuser)]})
    with build(tmp_path, schema=schema, executor=executor, plan=SMALL) as scheduler:
        scheduler.run()
        first = attempt_rows(scheduler.store, key)[0]
        assert first["state"] == "harness_error"
        assert "InstrumentRefusalError" in first["detail"]


class ClosesTheGateOnce:
    """Healthy, except the one check made after the run the executor armed it in, when another run has found an outage
    and the gate is closed: the check fails with the gate closed, so the finished attempt overlapped an outage."""

    armed = False
    scheduler = None

    def __call__(self):
        if not self.armed:
            return Health(True, "fine")
        self.armed = False
        self.scheduler.gate._close("another-run", "platform unreachable")
        return Health(False, "platform unreachable")


def test_c31_a_refused_treatment_attempt_is_not_scored_when_the_health_gate_reports_an_outage_too(tmp_path):
    """C31 and C01: a treatment attempt is scored whatever an outage flag says, because the treatment rule comes first.
    When the instrument refuses it, the attempt is no longer scored whether or not an outage overlapped it: it is
    rewritten to the refusal's kind, which is never treatment, so it is discarded as the outage's, not charged, and
    run again. The store and the record agree that it is set aside, and the run's final attempt is the rerun's."""
    probe = make_schema()
    first = make_manifest(probe, **SMALL).rows[0]
    schema = treatment_schema(("knowledge",), condition=probe.condition_of(first.key))
    key = first.key.text  # the run the scheduler dispatches first, whose post-run check the executor arms
    check, records = ClosesTheGateOnce(), []
    script = [[("look", {})], InfraError("knowledge", "the tool server went down")]
    behave = run_with(refusing("the outage was not the planned one"), script, records)

    async def refuse_then_arm_the_health_check(request):
        record = await behave(request)
        check.armed = True
        return record

    executor = FakeExecutor({key: [refuse_then_arm_the_health_check, ("infra_after_turn", "knowledge")]})
    settings = settings_for(recheck_s=0.01, health_timeout_s=5)
    with build(tmp_path, schema=schema, executor=executor, plan=SMALL, health=check, settings=settings) as scheduler:
        check.scheduler = scheduler
        assert scheduler.run().statuses == {"done": 3}
        store = scheduler.store
        columns = "state, charged, treatment, infra_kind, outage_kind, detail"
        first_try, second = store.q(f"SELECT {columns} FROM attempts WHERE run_key=? ORDER BY id", (key,))
        assert records[0].outcome["treatment"] is True  # what the loop saw
        assert records[0].set_aside is True  # and the record says the attempt was refused
        assert (first_try["state"], first_try["charged"], first_try["treatment"]) == ("outage_discarded", 0, 0)
        assert (first_try["infra_kind"], first_try["outage_kind"]) == (REFUSED, "sandbox")
        assert "manipulation" in first_try["detail"]
        assert (second["state"], second["treatment"], second["infra_kind"]) == ("done", 1, "knowledge")
        assert executor.calls_of(key) == 2
        assert "instrument_refused" in event_kinds(store)
