"""A start that failed right after a failed post-run check costs its run no attempt, once.

After an attempt whose post-run check failed and was not deferred, the run's next attempt, should the executor's own
result be a charged infrastructure failure before the agent's first turn, is set aside as one an outage overlapped,
uncharged, its detail led by why (`expcore.campaign.start_after_check`); the failure after it is charged again. Seen
live in a running campaign: such a start was charged, and the run came within one failure of being exhausted.
"""

import pytest
from campaign_support import LATER, make_schema
from loop_support import FakeProvider
from record_support import CODE
from scheduler_support import FakeExecutor, attempts_of, build, key_of, models_for, settings_for

from expcore.campaign.attempts import classify
from expcore.campaign.executor import record_run
from expcore.campaign.factors import FactorSchema
from expcore.campaign.gate import Budget
from expcore.campaign.healthgate import Health
from expcore.campaign.markers import AttemptResult, read_marker, write_marker
from expcore.campaign.start_after_check import EVENT, REASON, RiskedStarts, spared
from expcore.config.prices import Price
from expcore.errors import InfraError, register_infra_kind
from expcore.loop.outcomes import Outcome
from expcore.models.adapters import ModelTurn

START = "start_unverified"  # a study's own start, as a kind it declares
register_infra_kind(START, "the start the run induces on the platform did not verify")

SCHEMA = make_schema()
ONE = FactorSchema(SCHEMA.declared, SCHEMA.order, {"api-none": SCHEMA.conditions["api-none"]})  # a plan of one run
KEY = key_of(ONE, "api-none")
PLAN = {"models": ("m1",), "tasks": ("task-a",), "seeds": (1,)}
TABLE = models_for("m1", price=Price(3.0, 15.0), context_tokens=16_000)
BUDGET = Budget(max_usd=50.0)  # a budget keeps the call log that shows a raised start sent no request


class Scripted:
    """A health check answering call by call, pre-run and post-run alike: `pass`, `fail` or `deferred`; a pass once
    the script is used up."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)

    def __call__(self) -> Health:
        answer = self.answers.pop(0) if self.answers else "pass"
        if answer == "pass":
            return Health(True, "fine")
        if answer == "deferred":
            return Health(False, "the federation is held on purpose", deferred=True, charge=False)
        return Health(False, "a provider pair was not tested")


async def turn_then_raise(request):
    """A study's executor that takes one model turn through the core's recording, then raises its own start kind."""
    request.backend.use([ModelTurn("done", [], 100, 10, "stop", 0.0)])
    config = FakeExecutor().config
    await record_run(request, [FakeProvider({})], "be brief", "do the task", config, code=dict(CODE))
    raise InfraError(START, "the start state was lost after the answer")


def options(executor, *, budget=True, **more):
    settings = settings_for(max_attempts=3, **({"budget": BUDGET} if budget else {}))
    return {"schema": ONE, "executor": executor, "plan": PLAN, "models": TABLE, "settings": settings, **more}


def campaign(tmp_path, check: Scripted, *behaviours, budget=True):
    """One run, its attempts behaving as given in order, under `check`; return its attempts, the rule's events and each
    attempt's detail."""
    executor = FakeExecutor({KEY: list(behaviours)})
    with build(tmp_path, health=check, **options(executor, budget=budget)) as scheduler:
        scheduler.run()
        rows = attempts_of(scheduler.store, KEY)
        events = [r["detail"] for r in scheduler.store.q("SELECT detail FROM events WHERE kind=?", (EVENT,))]
        details = [r["detail"] for r in scheduler.store.q("SELECT detail FROM attempts ORDER BY id")]
    return rows, events, details


def test_a_start_failure_right_after_a_failed_post_run_check_is_set_aside_uncharged_once(tmp_path):
    """The first attempt's post-run check fails; the second attempt's start fails before the agent's first turn, with
    no request in its log: it is set aside as an outage overlapped it, uncharged, its detail says why first, one event
    is written, and the run goes on."""
    rows, events, details = campaign(tmp_path, Scripted("pass", "fail"), "finish", ("raise_infra", START), "finish")
    assert rows == [("infra_fail", 1, 1), ("outage_discarded", 0, 2), ("done", 0, 2)]
    assert details[1].startswith(REASON)
    assert "failed before the loop" in details[1]  # the executor's own words follow
    assert len(events) == 1
    assert START in events[0]


def test_the_rule_never_spares_two_starts_in_a_row(tmp_path):
    """The spared start's own post-run check fails too: it marks nothing, so the start after it is charged, and a run
    on a platform that keeps failing its checks still reaches its attempt limit."""
    check = Scripted("pass", "fail", "pass", "fail")
    rows, events, _ = campaign(tmp_path, check, "finish", ("raise_infra", START), ("raise_infra", START), "finish")
    assert rows == [("infra_fail", 1, 1), ("outage_discarded", 0, 2), ("infra_fail", 1, 2), ("done", 0, 3)]
    assert len(events) == 1


def test_a_start_failure_with_no_failed_post_run_check_before_it_is_still_charged(tmp_path):
    """No post-run check failed before it: a start's own failure costs the run an attempt, as it always did."""
    rows, events, _ = campaign(tmp_path, Scripted(), ("raise_infra", START), "finish")
    assert rows == [("infra_fail", 1, 1), ("done", 0, 2)]
    assert events == []


def test_a_deferred_post_run_check_marks_nothing(tmp_path):
    """A deferred post-run check verified nothing and found nothing failing: the next start's failure is charged."""
    rows, events, _ = campaign(tmp_path, Scripted("pass", "deferred"), "finish", ("raise_infra", START), "finish")
    assert rows == [("infra_fail", 1, 1), ("infra_fail", 1, 2), ("done", 0, 3)]
    assert events == []


@pytest.mark.parametrize(
    "behaviour",
    [("infra_after_turn", START), turn_then_raise],
    ids=["a record with a completed turn", "a raise after a turn, its requests in the log"],
)
def test_a_failure_after_the_first_turn_is_charged_after_a_failed_check(tmp_path, behaviour):
    """Only a failure before the agent's first turn is covered: one after a completed turn, whether the record counts
    the turn or the executor raised with the turn's request in its call log, is charged as before."""
    rows, events, _ = campaign(tmp_path, Scripted("pass", "fail"), "finish", behaviour, "finish")
    assert rows == [("infra_fail", 1, 1), ("infra_fail", 1, 2), ("done", 0, 3)]
    assert events == []


def test_a_raised_start_with_no_call_log_is_charged_since_no_turn_is_known(tmp_path):
    """Without a budget no call log is kept, so a raised executor's usage is unknown and nothing shows that it took no
    turn: the start's failure is charged, as before."""
    check = Scripted("pass", "fail")
    rows, events, _ = campaign(tmp_path, check, "finish", ("raise_infra", START), "finish", budget=False)
    assert rows == [("infra_fail", 1, 1), ("infra_fail", 1, 2), ("done", 0, 3)]
    assert events == []


def test_a_spared_start_committed_from_its_marker_after_a_kill_is_uncharged(tmp_path):
    """The decision rides on the result (`outage_kind`, the detail), in its marker: a kill between the spared start's
    marker and its row leaves the attempt running, and recovery at the next start commits it uncharged, as the live
    finish would have."""
    tries = []

    def killed_after_the_second_marker(point, run_key):
        if point == "after_marker":
            tries.append(run_key)
            if len(tries) == 2:
                raise OSError("the process is killed here")

    executor = FakeExecutor({KEY: ["finish", ("raise_infra", START)]})
    killing = build(
        tmp_path, health=Scripted("pass", "fail"), crash_hook=killed_after_the_second_marker, **options(executor)
    )
    with killing as killed, pytest.raises(RuntimeError, match="unexpected error"):
        killed.run()
    with build(tmp_path, health=Scripted(), **options(FakeExecutor())) as restart:
        rows = restart.store.q("SELECT state, charged, attempt, detail FROM attempts ORDER BY id")
    assert [tuple(row)[:3] for row in rows[:2]] == [("infra_fail", 1, 1), ("outage_discarded", 0, 2)]
    assert rows[1]["detail"].startswith(REASON)


def test_the_mark_is_used_up_by_the_runs_next_attempt_whatever_it_ends_in():
    """The run's next attempt takes the mark once, so only the start directly after the failed check is covered."""
    risked = RiskedStarts()
    risked.mark(KEY)
    assert risked.take(KEY) is True
    assert risked.take(KEY) is False
    assert risked.take("another run") is False


def start_failure(kind: str = START, turns: int = 0, usage=()) -> AttemptResult:
    outcome = Outcome("infrastructure", "the start did not verify", infra_kind=kind)
    return AttemptResult(outcome, LATER, 2.0, model_turns=turns, turn_usage=usage)


@pytest.mark.parametrize(
    ("result", "recorded", "covered"),
    [
        (start_failure(), False, True),
        (start_failure("sandbox"), False, True),
        (start_failure(usage=None), False, False),
        (start_failure(usage=None), True, True),
        (start_failure(turns=1, usage=((10, 1, None, None),)), True, False),
        (start_failure("key_rejected"), False, False),
        (AttemptResult(Outcome("finished"), LATER, 2.0), True, False),
    ],
    ids=[
        "raised, no request",
        "raised, the core's kind, no request",
        "raised, usage unknown",
        "recorded, no turn",
        "recorded, a turn",
        "uncharged already",
        "scored",
    ],
)
def test_only_a_charged_infrastructure_failure_before_the_first_turn_is_spared(result, recorded, covered):
    """`spared` sets aside a charged infrastructure failure whose record counts no turn, or which raised with a usage
    known to be no request, whatever its kind; everything else is left as it is."""
    found = spared(result, "sandbox", recorded=recorded)
    assert (found is not None) is covered
    if covered:
        assert found.outage_kind == "sandbox"
        assert found.outcome.detail == f"{REASON}; {result.outcome.detail}"
        assert found.outcome.infra_kind == result.outcome.infra_kind
        assert classify(found).state == "outage_discarded"


def test_a_spared_start_reads_back_from_its_marker_unchanged(tmp_path):
    """No marker field is added: the spared result is written and read back as it is."""
    found = spared(start_failure(), "sandbox", recorded=False)
    write_marker(tmp_path, KEY, 7, found)
    assert read_marker(tmp_path, KEY, 7) == found
