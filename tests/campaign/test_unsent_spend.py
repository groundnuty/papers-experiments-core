"""An attempt whose complete call log shows no model request is charged measured zero, whatever ended it; one whose
log is unreadable or incomplete, or whose backend says it started a request, is charged its bound as before (C05)."""

import asyncio
from pathlib import Path

import pytest
from campaign_support import LATER
from loop_support import FakeProvider
from record_support import CODE
from scheduler_support import FakeExecutor, build, event_kinds, models_for, settings_for

from expcore.campaign.attempt_bounds import attempt_costs, bound_usd
from expcore.campaign.executor import record_run
from expcore.campaign.export import export_run_state
from expcore.campaign.gate import Budget, BudgetGate
from expcore.campaign.interrupted_spend import MEASURED
from expcore.campaign.markers import read_marker
from expcore.config.prices import Price
from expcore.errors import InfraError
from expcore.models.adapters import ModelTurn

TABLE = models_for("m1", price=Price(3.0, 15.0), context_tokens=16_000)
PLAN = {"models": ("m1",), "tasks": ("task-a",), "seeds": (1,)}
REASON = "no model request in a complete journal"


class StateError(Exception):
    """A study's own exception at start, as a federation's state file that cannot be read raises it."""


class FailsAtStart(FakeExecutor):
    """The study's executor: its first `times` attempts raise `error` before any request, the rest finish."""

    def __init__(self, error, times=1, before=None):
        super().__init__()
        self.error, self.times, self.before, self.attempts = error, times, before, 0
        self.scheduler = None  # the test hands over the scheduler once built, for a step that reaches the store

    async def __call__(self, request):
        self.attempts += 1
        if self.attempts > self.times:
            return await super().__call__(request)
        if self.before is not None:
            self.before(self, request)
        raise self.error


def induction_failure():
    return InfraError("injector_failure", "the fault could not be induced")


def run_once(root, executor, *, budget=True):
    """Run the plan with an executor that fails every attempt; return the first attempt's row, cost and bound."""
    settings = settings_for(max_attempts=1, **({"budget": Budget(max_usd=50.0)} if budget else {}))
    with build(root, plan=PLAN, models=TABLE, executor=executor, settings=settings) as scheduler:
        executor.scheduler = scheduler
        scheduler.run()
        row = scheduler.store.q("SELECT * FROM attempts ORDER BY id")[0]
        limits = scheduler.agent_config.limits
        cost = attempt_costs(scheduler.store, {"m1": TABLE.resolve("m1")}, limits)[0]
        return dict(row), cost, bound_usd(TABLE.resolve("m1"), limits)


@pytest.mark.parametrize(
    ("error", "state", "termination"),
    [
        (induction_failure(), "infra_fail", "infrastructure"),
        (StateError("the federation state is not of the format"), "harness_error", "harness_defect"),
    ],
    ids=["induction-failure", "state-error"],
)
def test_c05_an_attempt_that_failed_at_start_with_no_request_in_its_log_is_charged_measured_zero(
    tmp_path, error, state, termination
):
    """C05: a failure before the first request, whatever its class, leaves a complete log with no request: the
    attempt's usage is known to be none, its row and marker say `measured`, and it costs 0 at any price."""
    row, cost, bound = run_once(tmp_path / "out", FailsAtStart(error, times=99))
    assert (row["state"], row["termination"], row["usage_complete"]) == (state, termination, 1)
    assert (row["input_tokens"], row["output_tokens"], row["turn_usage"]) == (0, 0, "[]")
    assert (row["calls_sent"], row["calls_usage"], row["spend_basis"]) == (0, "[]", MEASURED)
    assert (cost.usd, cost.bounded) == (0.0, False)
    assert bound > 0
    marker = read_marker(Path(row["run_dir"]), row["run_key"], row["id"])
    assert (marker.input_tokens, marker.output_tokens, marker.usage_complete) == (0, 0, True)
    assert marker.spend_basis == MEASURED


def test_c05_a_start_failure_leaves_the_gates_spent_figure_unchanged_and_says_why(tmp_path):
    """C05: the budget gate's spent figure after the failure is the figure before it, the export counts the attempt's
    usage as complete and the store records the reason, so a reader sees why it was charged nothing."""
    executor = FailsAtStart(induction_failure(), times=99)
    settings = settings_for(max_attempts=1, budget=Budget(max_usd=50.0))
    with build(tmp_path / "out", plan=PLAN, models=TABLE, executor=executor, settings=settings) as scheduler:
        store, limits = scheduler.store, scheduler.agent_config.limits
        gate = BudgetGate(store, {"m1": TABLE.resolve("m1")}, Budget(max_usd=50.0), limits)
        before = gate.assess([], LATER).spent_usd
        scheduler.run()
        rows = store.q("SELECT state, spend_basis FROM attempts")
        assert len(rows) > 0
        assert {(row["state"], row["spend_basis"]) for row in rows} == {("infra_fail", MEASURED)}
        assert gate.assess([], LATER).spent_usd == before == 0.0
        cells = [c for c in export_run_state(store)["discarded_usage"] if c["state"] == "infra_fail"]
        assert sum(c["attempts"] for c in cells) == len(rows)
        assert sum(c["usage_incomplete_attempts"] for c in cells) == 0
        assert event_kinds(store).count("usage_measured_zero") == len(rows)
        details = [r["detail"] for r in store.q("SELECT detail FROM events WHERE kind='usage_measured_zero'")]
        assert all(REASON in detail for detail in details)


def test_c05_two_start_failures_no_longer_exhaust_a_budget_whose_bound_is_half_the_ceiling(tmp_path):
    """C05: the pilot's case. A model's bound is about half its ceiling; two attempts that failed at start, before any
    request, were each charged that bound, so the third was held and the model's budget was spent with nothing sent.
    Charged what they sent, nothing, the campaign runs every run to its end."""
    limits = FakeExecutor().config.limits
    bound = bound_usd(TABLE.resolve("m1"), limits)
    executor = FailsAtStart(induction_failure(), times=2)
    settings = settings_for(max_attempts=5, budget=Budget(max_usd=2.5 * bound))
    with build(tmp_path / "out", plan=PLAN, models=TABLE, executor=executor, settings=settings) as scheduler:
        scheduler.run()
        store = scheduler.store
        assert {row["status"] for row in store.q("SELECT status FROM runs")} == {"done"}
        states = [row["state"] for row in store.q("SELECT state FROM attempts ORDER BY id")]
        assert (states.count("infra_fail"), states.count("done")) == (2, 3)
        spent = BudgetGate(store, {"m1": TABLE.resolve("m1")}, settings.budget, limits).assess([], LATER).spent_usd
        assert spent < bound  # the two failures cost nothing; what the three runs sent is a fraction of one bound


@pytest.mark.parametrize(
    ("damage", "why"),
    [
        ("UPDATE attempts SET calls_usage='not a log'", "the log cannot be read"),
        ("UPDATE attempts SET calls_sent=NULL, calls_usage=NULL", "the attempt kept no log"),
        ("UPDATE attempts SET calls_sent=1, calls_usage='[]'", "a request was begun and never closed"),
        ("UPDATE attempts SET calls_sent=1, calls_usage='[\"failed\"]'", "a send with no reply, closed by a failure"),
        ("UPDATE attempts SET calls_sent=1, calls_usage='[[4000,50,null,null]]'", "a send that was answered"),
        ("UPDATE attempts SET calls_sent=0, calls_usage='[[4000,50,null,null]]'", "more replies than requests"),
        ("UPDATE attempts SET topups=1, topup_usd=0.25", "a top-up was charged"),
    ],
)
def test_c05_an_unreadable_or_incomplete_log_or_a_send_charges_the_bound_as_before(tmp_path, damage, why):
    """C05: only a log that is readable, complete and holds no request, with no top-up, is charged nothing. A log that
    cannot be read, one with a request begun and not closed, a send with no reply or an answered one, and an attempt
    that was topped up keep their usage unknown and their bound, and the row records no basis."""

    def damaged(me, _request):
        me.scheduler.store.transact(lambda: me.scheduler.store.db.execute(damage))  # the log as the guard left it

    row, cost, bound = run_once(tmp_path / "out", FailsAtStart(induction_failure(), times=99, before=damaged))
    assert (row["state"], row["usage_complete"], row["spend_basis"]) == ("infra_fail", 0, None), why
    assert (row["input_tokens"], row["output_tokens"]) == (None, None)
    assert (cost.usd, cost.bounded) == (pytest.approx(bound + (row["topup_usd"] or 0.0)), True), why


class SendsOutsideTheRecording(FailsAtStart):
    """An executor that sends a request to the backend itself, so no request is in its call log, and then fails."""

    async def __call__(self, request):
        request.backend.use([ModelTurn("hello", [], 100, 5, "stop", 0.0)])
        await request.backend.step()
        raise induction_failure()


def test_c05_a_backend_that_started_a_request_the_log_never_saw_keeps_the_bound(tmp_path):
    """C05: a log with no request cannot tell an executor that sent none from one whose requests bypass the recording,
    so a backend that counts its requests vetoes it: one that started a request is charged the bound."""
    row, cost, bound = run_once(tmp_path / "out", SendsOutsideTheRecording())
    assert (row["calls_sent"], row["usage_complete"], row["spend_basis"]) == (0, 0, None)
    assert (cost.usd, cost.bounded) == (pytest.approx(bound), True)


def test_c05_a_backend_that_keeps_no_count_of_its_requests_follows_the_log(tmp_path):
    """C05: a backend with no `requests_sent` cannot veto, as for a first-turn refusal (C31): its attempt is charged
    by its log, nothing here. The residual is an executor that bypasses the recording on such a backend."""

    def uncounted(_me, request):
        del request.backend.requests_sent

    row, cost, _ = run_once(tmp_path / "out", FailsAtStart(induction_failure(), times=99, before=uncounted))
    assert (row["usage_complete"], row["spend_basis"], cost.usd) == (1, MEASURED, 0.0)


class StallsOnItsFirstRequest(FakeExecutor):
    """An executor whose first request goes out through the recording and is never answered: the study's own timeout
    gives up on it, so a send is in the log with no reply."""

    async def __call__(self, request):
        request.backend.use([ModelTurn("hello", [], 100, 5, "stop", 0.0)])
        step = request.backend.step

        async def stalling():
            await asyncio.sleep(3600)
            return await step()

        request.backend.step = stalling
        return await asyncio.wait_for(
            record_run(request, [FakeProvider({})], "be brief", "do the task", self.config, code=dict(CODE)),
            timeout=0.3,
        )


def test_c05_a_request_that_went_out_and_got_no_reply_keeps_its_bound_and_records_no_basis(tmp_path):
    """C05: a request sent and not answered may have been billed in full; it is in the log, so the attempt is never
    charged as one that sent no request: its usage stays unknown and its bound is charged, as before."""
    row, cost, bound = run_once(tmp_path / "out", StallsOnItsFirstRequest())
    assert (row["calls_sent"], row["calls_usage"]) == (1, "[]")
    assert (row["state"], row["usage_complete"], row["spend_basis"]) == ("harness_error", 0, None)
    assert (cost.usd, cost.bounded) == (pytest.approx(bound), True)


def test_c05_without_a_budget_a_start_failure_is_unchanged_since_no_call_log_is_kept(tmp_path):
    """C05: no budget, no spend guard and no call log: nothing can show that no request was sent, so the usage stays
    unknown and the row records no basis."""
    row, _, _ = run_once(tmp_path / "out", FailsAtStart(induction_failure(), times=99), budget=False)
    assert (row["state"], row["usage_complete"], row["spend_basis"]) == ("infra_fail", 0, None)
    assert (row["calls_sent"], row["input_tokens"]) == (None, None)


def test_c05_a_kill_after_the_marker_commits_the_measured_zero_with_the_marked_result(tmp_path):
    """C05: the marker carries the zero counts and the basis, so recovery commits them as the live finish would."""

    def killed_after_marker(point, run_key):
        if point == "after_marker":
            raise OSError("the process is killed here")

    options = {
        "executor": FailsAtStart(induction_failure(), times=99),
        "settings": settings_for(budget=Budget(max_usd=50.0), max_attempts=1),
    }
    killing = build(tmp_path / "out", plan=PLAN, models=TABLE, crash_hook=killed_after_marker, **options)
    with killing as killed, pytest.raises(RuntimeError, match="unexpected error"):
        killed.run()
    with build(tmp_path / "out", plan=PLAN, models=TABLE, **options) as restart:
        [row] = restart.store.q("SELECT state, usage_complete, input_tokens, spend_basis FROM attempts")
        assert tuple(row) == ("infra_fail", 1, 0, MEASURED)
        [cost] = attempt_costs(restart.store, {"m1": TABLE.resolve("m1")}, restart.agent_config.limits)
        assert (cost.usd, cost.bounded) == (0.0, False)
