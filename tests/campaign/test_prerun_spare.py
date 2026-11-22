"""A pre-run outage right after a failed post-run check costs its run no attempt, once.

A run whose previous attempt's post-run check failed, and whose next attempt's pre-run check finds the outage at once
(a failed heal that only an operator can mend), nothing started: the attempt is set aside uncharged, its detail led by
why, with an event, the gate's own record saying no run was charged (`expcore.campaign.start_after_check`). It is the
same bound as the spared start: once per failed check, never two in a row, in this process. Seen live: such a pre-run
outage was charged as the outage's one attempt, and the run was exhausted.
"""

import json

import pytest
from campaign_support import make_fingerprint, make_manifest, make_schema
from scheduler_support import Factory, FakeExecutor, attempts_of, build, key_of, models_for, seed_store, settings_for

from expcore.campaign.executor import Endpoint
from expcore.campaign.export import export_run_state
from expcore.campaign.factors import FactorSchema
from expcore.campaign.gate import Budget
from expcore.campaign.health_context import POST_RUN, PRE_RUN
from expcore.campaign.healthgate import Health
from expcore.campaign.manifest import Manifest, fingerprint_sha256
from expcore.campaign.run_lease import RunLease
from expcore.campaign.scheduler import Scheduler
from expcore.campaign.shard_run import ShardStart
from expcore.campaign.start_after_check import EVENT, REASON, RiskedStarts
from expcore.config.prices import Price
from expcore.errors import register_infra_kind

START = "start_unverified"  # a study's own start, as a kind it declares (registering it again is harmless)
register_infra_kind(START, "the start the run induces on the platform did not verify")

SCHEMA = make_schema()
ONE = FactorSchema(SCHEMA.declared, SCHEMA.order, {"api-none": SCHEMA.conditions["api-none"]})  # a plan of one run
KEY = key_of(ONE, "api-none")
PLAN = {"models": ("m1",), "tasks": ("task-a",), "seeds": (1,)}
TABLE = models_for("m1", price=Price(3.0, 15.0), context_tokens=16_000)
BUDGET = Budget(max_usd=50.0)  # a budget keeps the call log that shows a raised start sent no request
HEAL = "a failed heal: operator action needed (stuck pairs: one reading from another)"


class Phased:
    """A health check that answers by the phase the gate says it judges, so the dispatcher's own checks (given no
    context) never take an answer: `pre` and `post` are the answers of the pre-run and of the post-run checks in order,
    each `pass`, `fail` or `left` (a failure an earlier attempt left, which charges no one), a pass once its script is
    used up. A failure says what a failed heal says."""

    def __init__(self, pre: tuple[str, ...] = (), post: tuple[str, ...] = ()) -> None:
        self.script = {PRE_RUN: list(pre), POST_RUN: list(post)}

    def __call__(self, context=None) -> Health:
        answers = self.script.get(None if context is None else context.phase)
        answer = answers.pop(0) if answers else "pass"
        if answer == "pass":
            return Health(True, "fine")
        return Health(False, HEAL, charge=answer != "left")


def options(executor):
    settings = settings_for(max_attempts=3, budget=BUDGET, recheck_s=0.01)  # the closed gate is re-checked at once
    return {"schema": ONE, "executor": executor, "plan": PLAN, "models": TABLE, "settings": settings}


def campaign(tmp_path, check: Phased, *behaviours):
    """One run, its attempts behaving as given in order, under `check`; return its attempts, the rule's events, each
    attempt's detail and the gate's `health_gate_closed` events."""
    executor = FakeExecutor({KEY: list(behaviours)})
    with build(tmp_path, health=check, **options(executor)) as scheduler:
        scheduler.run()
        store = scheduler.store
        rows = attempts_of(store, KEY)
        events = [r["detail"] for r in store.q("SELECT detail FROM events WHERE kind=?", (EVENT,))]
        details = [r["detail"] for r in store.q("SELECT detail FROM attempts ORDER BY id")]
        closed = [r["detail"] for r in store.q("SELECT detail FROM events WHERE kind='health_gate_closed'")]
    return rows, events, details, closed


def test_a_pre_run_outage_right_after_a_failed_post_run_check_is_set_aside_uncharged(tmp_path):
    """C03: The first attempt's post-run check fails; the second attempt's pre-run check finds the outage and nothing
    starts: that attempt is set aside as one an outage overlapped, uncharged, its detail says why first, one event is
    written, the run's charged count is the first attempt's alone, and the run goes on."""
    check = Phased(pre=("pass", "fail"), post=("fail",))
    executor = FakeExecutor({KEY: ["finish", "finish"]})
    with build(tmp_path, health=check, **options(executor)) as scheduler:
        scheduler.run()
        rows = attempts_of(scheduler.store, KEY)
        [spared] = scheduler.store.q("SELECT * FROM attempts WHERE state='outage_discarded'")
        events = [r["detail"] for r in scheduler.store.q("SELECT detail FROM events WHERE kind=?", (EVENT,))]
        charged = scheduler.store.charged_attempts(KEY)
    assert rows == [("infra_fail", 1, 1), ("outage_discarded", 0, 2), ("done", 0, 2)]
    assert charged == 1
    assert spared["detail"].startswith(f"{REASON}; pre-run health check failed: {HEAL}")
    assert (spared["charged"], spared["infra_kind"], spared["outage_kind"]) == (0, "sandbox", "sandbox")
    assert len(events) == 1
    assert "pre-run health check found an outage" in events[0]
    assert len(executor.requests) == 2  # the pre-run outage never reached the executor


def test_a_pre_run_outage_with_no_failed_post_run_check_before_it_is_still_charged(tmp_path):
    """The first attempt failed at its start, its post-run check passed: nothing covers the pre-run outage after it, and
    it is the outage's one attempt, charged as it always was; the gate's record says it charged a run."""
    executor = FakeExecutor({KEY: [("raise_infra", START), "finish"]})
    with build(tmp_path, health=Phased(pre=("pass", "fail")), **options(executor)) as scheduler:
        scheduler.run()
        rows = attempts_of(scheduler.store, KEY)
        events = scheduler.store.q("SELECT detail FROM events WHERE kind=?", (EVENT,))
        [hold] = export_run_state(scheduler.store)["operator_holds"]
    assert rows == [("infra_fail", 1, 1), ("infra_fail", 1, 2), ("done", 0, 3)]
    assert list(events) == []
    assert hold["charged"] is True


def test_the_gates_record_and_the_hold_report_say_no_run_was_charged_for_a_spared_outage(tmp_path):
    """The gate closes for the spared pre-run outage as it does for a check that charges no one: its record says
    `"charged": false`, which the export's `operator_holds` (the report's "Holds that needed an operator") and the
    status alarm read, and its event says no run is charged, so the report agrees with the run's own row."""
    executor = FakeExecutor({KEY: ["finish", "finish"]})
    check = Phased(pre=("pass", "fail"), post=("fail",))
    with build(tmp_path, health=check, **options(executor)) as scheduler:
        scheduler.run()
        [hold] = export_run_state(scheduler.store)["operator_holds"]
        closed = [r["detail"] for r in scheduler.store.q("SELECT detail FROM events WHERE kind='health_gate_closed'")]
    assert hold["charged"] is False
    assert "no run is charged, this one included" in closed[0]


def test_two_pre_run_outages_in_a_row_after_one_failed_check_spare_only_the_first(tmp_path):
    """The mark is used up by the first pre-run outage, which never ran a post-run check to mark the run again: the
    next pre-run outage is charged, so a platform that stays down still takes the run to its attempt limit."""
    check = Phased(pre=("pass", "fail", "fail"), post=("fail",))
    rows, events, _, closed = campaign(tmp_path, check, "finish", "finish")
    assert rows == [("infra_fail", 1, 1), ("outage_discarded", 0, 2), ("infra_fail", 1, 2), ("done", 0, 3)]
    assert len(events) == 1
    assert ["no run is charged" in text for text in closed] == [True, False]  # the second outage charged its finder


def test_a_spared_pre_run_outage_leaves_no_second_free_start_failure(tmp_path):
    """One failed post-run check covers one attempt, whichever it is: after the pre-run outage was spared, the run's
    next attempt fails at its start and is charged."""
    check = Phased(pre=("pass", "fail"), post=("fail",))
    rows, events, details, _ = campaign(tmp_path, check, "finish", ("raise_infra", START), "finish")
    assert rows == [("infra_fail", 1, 1), ("outage_discarded", 0, 2), ("infra_fail", 1, 2), ("done", 0, 3)]
    assert len(events) == 1
    assert details[1].startswith(REASON)
    assert not details[2].startswith(REASON)


def test_a_spared_start_failure_leaves_no_free_pre_run_outage(tmp_path):
    """The other order: the spared start used the mark, so the pre-run outage after it is charged; and the start that
    the existing rule spares is still spared."""
    check = Phased(pre=("pass", "pass", "fail"), post=("fail",))
    rows, events, details, _ = campaign(tmp_path, check, "finish", ("raise_infra", START), "finish")
    assert rows == [("infra_fail", 1, 1), ("outage_discarded", 0, 2), ("infra_fail", 1, 2), ("done", 0, 3)]
    assert len(events) == 1
    assert START in events[0]
    assert details[1].startswith(REASON)
    assert details[2].startswith("pre-run health check failed")


def test_an_attempt_a_post_run_check_discarded_uncharged_covers_nothing_after_it(tmp_path):
    """A post-run check that charges no one (a failure an earlier attempt left) discards its attempt uncharged, and
    that attempt marks nothing: the pre-run outage after it is charged, and the gate's record says it charged a run, so
    a platform that alternates the two kinds of answer cannot make attempts free."""
    check = Phased(pre=("pass", "fail"), post=("left",))
    executor = FakeExecutor({KEY: ["finish", "finish"]})
    with build(tmp_path, health=check, **options(executor)) as scheduler:
        scheduler.run()
        rows = attempts_of(scheduler.store, KEY)
        events = scheduler.store.q("SELECT detail FROM events WHERE kind=?", (EVENT,))
        [hold] = export_run_state(scheduler.store)["operator_holds"]
    assert rows == [("outage_discarded", 0, 1), ("infra_fail", 1, 1), ("done", 0, 2)]
    assert list(events) == []
    assert hold["charged"] is True


def test_nor_does_such_an_attempt_cover_the_start_failure_after_it(tmp_path):
    """The same bound for the spared start: after an attempt discarded uncharged by a post-run check that charges no
    one, the start failure is charged, as it is after a deferred check."""
    rows, events, _, _ = campaign(tmp_path, Phased(post=("left",)), "finish", ("raise_infra", START), "finish")
    assert rows == [("outage_discarded", 0, 1), ("infra_fail", 1, 1), ("done", 0, 2)]
    assert events == []


def test_a_spared_pre_run_outage_committed_from_its_marker_after_a_kill_is_uncharged(tmp_path):
    """The decision rides on the result (`outage_kind`, the detail), in its marker: a kill between the spared attempt's
    marker and its row leaves it running, and recovery at the next start commits it uncharged, as the live finish would
    have, with its detail's lead."""
    tries = []

    def killed_after_the_second_marker(point, run_key):
        if point == "after_marker":
            tries.append(run_key)
            if len(tries) == 2:
                raise OSError("the process is killed here")

    executor = FakeExecutor({KEY: ["finish", "finish"]})
    check = Phased(pre=("pass", "fail"), post=("fail",))
    killing = build(tmp_path, health=check, crash_hook=killed_after_the_second_marker, **options(executor))
    with killing as killed, pytest.raises(RuntimeError, match="unexpected error"):
        killed.run()
    with build(tmp_path, health=Phased(), **options(FakeExecutor())) as restart:
        rows = restart.store.q("SELECT state, charged, attempt, detail FROM attempts ORDER BY id")
    assert [tuple(row)[:3] for row in rows[:2]] == [("infra_fail", 1, 1), ("outage_discarded", 0, 2)]
    assert rows[1]["detail"].startswith(REASON)


def test_a_restart_forgets_the_mark_so_the_pre_run_outage_after_it_is_charged(tmp_path):
    """The mark lives in memory: the first process spared the pre-run outage in its mind and was killed before its
    marker, the second never heard of the failed post-run check, and the pre-run outage it finds is charged."""
    tries = []

    def killed_before_the_second_marker(point, run_key):
        if point == "before_marker":
            tries.append(run_key)
            if len(tries) == 2:
                raise OSError("the process is killed here")

    executor = FakeExecutor({KEY: ["finish", "finish"]})
    first = Phased(pre=("pass", "fail"), post=("fail",))
    killing = build(tmp_path, health=first, crash_hook=killed_before_the_second_marker, **options(executor))
    with killing as killed, pytest.raises(RuntimeError, match="unexpected error"):
        killed.run()
    with build(tmp_path, health=Phased(pre=("fail",)), **options(FakeExecutor())) as restart:
        restart.run()
        rows = attempts_of(restart.store, KEY)
        events = restart.store.q("SELECT detail FROM events WHERE kind=?", (EVENT,))
    assert rows == [("infra_fail", 1, 1), ("interrupted", 0, 2), ("infra_fail", 1, 2), ("done", 0, 3)]
    assert list(events) == []


def test_marked_looks_at_the_mark_and_leaves_it():
    """`marked` is what the gate is told before its check; `take` is what the attempt then spends."""
    risked = RiskedStarts()
    assert risked.marked(KEY) is False
    risked.mark(KEY)
    assert (risked.marked(KEY), risked.marked(KEY), risked.take(KEY), risked.marked(KEY)) == (True, True, True, False)


def test_a_lease_stores_journal_and_count_take_the_spared_pre_run_outage_as_uncharged(tmp_path):
    """The live shape, in the store a lease runs its one run in, with the spend journal the controller reads when the
    lease is lost: the spared attempt's journal line says `outage_discarded`, charged 0, and the store's count and the
    sum its completion names are the first attempt's alone, so a settle that reads the journal agrees."""
    schema = make_schema()
    plan = {"tasks": ("task-a",), "models": ("m1",), "seeds": (1,)}
    whole = make_manifest(schema, 7, **plan)
    fingerprint = make_fingerprint(schema, task_ids=("task-a",), model_keys=("m1",), seed_ids=(1,))
    key = whole.rows[0].key.text
    lease = RunLease(
        run_key=key,
        lease_id=1,
        attempt_base=0,
        grant_micro_usd=40_000_000,
        campaign_sha256=fingerprint_sha256(fingerprint),
        expires_utc="2099-01-01T00:00:00Z",
    )
    seed_store(tmp_path, schema)
    executor = FakeExecutor({key: ["finish", "finish"]})
    journal = tmp_path / "spend.jsonl"
    scheduler = Scheduler(
        tmp_path,
        schema,
        Manifest(whole.order_seed, (whole.rows[0],)),
        fingerprint,
        executor,
        agent_config=executor.config,
        models=TABLE,
        endpoints={"m1": Endpoint("m1", "unit-m1", "https://llm.example.invalid/m1")},
        settings=settings_for(max_attempts=3, budget=Budget(max_usd=40.0), recheck_s=0.01),
        backend_factory=Factory(),
        shard=ShardStart(lease, {}, {"unit_caps": {"unit-m1": 1}, "budget": {}}),
        health=Phased(pre=("pass", "fail"), post=("fail",)),
        spend_journal=journal,
    )
    with scheduler:
        scheduler.run()
        rows = attempts_of(scheduler.store, key)
        charged = scheduler.store.charged_attempts(key)
    lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    ended = [
        (line["state"], line["charged"])
        for line in lines
        if line.get("kind") == "attempt" and line["state"] != "running"
    ]
    assert rows == [("infra_fail", 1, 1), ("outage_discarded", 0, 2), ("done", 0, 2)]
    assert ended == [("infra_fail", 1), ("outage_discarded", 0), ("done", 0)]
    assert charged == 1
