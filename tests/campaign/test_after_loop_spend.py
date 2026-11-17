"""An attempt that raises after its model loop is charged what its call log measured, and its bound only when that log
is unreadable or incomplete for it; one that raises during the loop is charged its bound as before (C05)."""

import asyncio
import json

import pytest
from loop_support import FakeProvider
from record_support import CODE
from scheduler_support import FakeExecutor, build, models_for, settings_for

from expcore.campaign.after_loop import settle_usage
from expcore.campaign.attempt_bounds import attempt_costs, bound_usd
from expcore.campaign.executor import record_run
from expcore.campaign.gate import Budget
from expcore.campaign.interrupted_spend import BOUND, MEASURED
from expcore.campaign.markers import MARKER_NAME, SPEND_BASES, AttemptResult, MarkerError, read_marker, write_marker
from expcore.config.prices import Price
from expcore.errors import InfraError
from expcore.loop.outcomes import Outcome
from expcore.models.adapters import ModelTurn, ToolCall

PRICE = Price(3.0, 15.0)
TABLE = models_for("m1", price=PRICE, context_tokens=16_000)
PLAN = {"models": ("m1",), "tasks": ("task-a",), "seeds": (1,)}
TOOL_ROUND, ANSWER = (4_000, 50), (5_000, 60)
MEASURED_USD = ((TOOL_ROUND[0] + ANSWER[0]) * 3.0 + (TOOL_ROUND[1] + ANSWER[1]) * 15.0) / 1e6
ENDED = "2026-01-01T00:00:00Z"


class AfterItsLoop:
    """The study's executor: a tool round and an answer through the core's recording, then `after`, which raises."""

    def __init__(self, after=None):
        self.config = FakeExecutor().config
        self.after = after or self.leak
        self.scheduler = None  # the test hands over the scheduler once built, for a step that reaches the store

    @staticmethod
    def leak(_executor):
        raise InfraError("sandbox", "a post-run guard found a leak in a tool answer")

    async def __call__(self, request):
        tool_round = ModelTurn("", [ToolCall("call_first", "look", {})], *TOOL_ROUND, "tool_calls", 0.0)
        request.backend.use([tool_round, ModelTurn("done", [], *ANSWER, "stop", 0.0)])
        providers = [FakeProvider({"look": lambda arguments: "ok"})]
        record = await record_run(request, providers, "be brief", "do the task", self.config, code=dict(CODE))
        self.after(self)
        return record


class DuringItsLoop(AfterItsLoop):
    """An executor that gives up on its loop while the second request is in flight, as a study's own timeout does."""

    async def __call__(self, request):
        tool_round = ModelTurn("", [ToolCall("call_first", "look", {})], *TOOL_ROUND, "tool_calls", 0.0)
        request.backend.use([tool_round, ModelTurn("done", [], *ANSWER, "stop", 0.0)])
        step = request.backend.step
        calls = []

        async def stalling():
            calls.append(1)
            if len(calls) == 2:
                await asyncio.sleep(3600)
            return await step()

        request.backend.step = stalling
        providers = [FakeProvider({"look": lambda arguments: "ok"})]
        return await asyncio.wait_for(
            record_run(request, providers, "be brief", "do the task", self.config, code=dict(CODE)), timeout=0.3
        )


def run_once(root, executor, *, budget=True, **options):
    settings = settings_for(max_attempts=1, **({"budget": Budget(max_usd=50.0)} if budget else {}))
    with build(root, plan=PLAN, models=TABLE, executor=executor, settings=settings, **options) as scheduler:
        executor.scheduler = scheduler
        scheduler.run()
        [row] = scheduler.store.q("SELECT * FROM attempts")
        [cost] = attempt_costs(scheduler.store, {"m1": TABLE.resolve("m1")}, scheduler.agent_config.limits)
        return dict(row), cost, bound_usd(TABLE.resolve("m1"), scheduler.agent_config.limits)


def test_c05_an_attempt_that_raises_after_its_loop_is_charged_its_measured_spend_and_records_the_basis(tmp_path):
    """C05: the post-run guard's raise, after every request was answered, keeps what the replies measured; the row
    says the usage is known and which basis it was charged on, and the charge is not the bound."""
    row, cost, bound = run_once(tmp_path / "out", AfterItsLoop())
    assert (row["state"], row["termination"], row["usage_complete"]) == ("infra_fail", "infrastructure", 1)
    assert (row["input_tokens"], row["output_tokens"]) == (9_000, 110)
    assert json.loads(row["turn_usage"]) == [[4_000, 50, None, None], [5_000, 60, None, None]]
    assert row["spend_basis"] == MEASURED
    assert (cost.usd, cost.bounded, cost.basis) == (pytest.approx(MEASURED_USD), False, None)
    assert cost.usd < bound


@pytest.mark.parametrize(
    ("damage", "why"),
    [
        ("UPDATE attempts SET calls_usage='not a log'", "the log cannot be read"),
        ("UPDATE attempts SET calls_usage='[[4000,50,null,null]]'", "a request was sent that no reply closed"),
        ("UPDATE attempts SET calls_usage='[[4000,50,null,null],null]'", "a reply was billed and not read"),
        ("UPDATE attempts SET calls_usage='[[4000,null,null,null],[5000,60,null,null]]'", "a reply left a count out"),
        ("UPDATE attempts SET calls_sent=0, calls_usage='[]'", "the log holds no request"),
        ("UPDATE attempts SET calls_sent=NULL, calls_usage=NULL", "the attempt kept no log"),
    ],
)
def test_c05_a_raise_after_the_loop_charges_the_bound_when_the_log_is_unreadable_or_incomplete(tmp_path, damage, why):
    """C05: measured spend is reported only where the log supports it. A log that cannot be read, holds no request,
    leaves a request without its reply or a reply without its counts charges the attempt's bound, and says so."""
    executor = AfterItsLoop(lambda me: me.scheduler.store.transact(lambda: me.scheduler.store.db.execute(damage)))
    row, cost, bound = run_once(tmp_path / "out", executor)
    assert (row["state"], row["usage_complete"], row["spend_basis"]) == ("infra_fail", 0, BOUND), why
    assert (row["input_tokens"], row["output_tokens"]) == (None, None)
    assert (cost.usd, cost.bounded) == (pytest.approx(bound), True), why


def test_c05_a_raise_during_the_loop_keeps_its_bound_and_records_no_basis(tmp_path):
    """C05: an attempt whose executor gives up while a request is in flight is as it was: its usage is unknown, its
    bound is charged, and the row records no basis (the loop did not end)."""
    row, cost, bound = run_once(tmp_path / "out", DuringItsLoop())
    assert (row["state"], row["usage_complete"], row["spend_basis"]) == ("harness_error", 0, None)
    assert (row["input_tokens"], row["output_tokens"]) == (None, None)
    assert (row["calls_sent"], json.loads(row["calls_usage"])) == (2, [[4_000, 50, None, None]])  # one in flight
    assert (cost.usd, cost.bounded) == (pytest.approx(bound), True)


def test_c05_an_executor_that_raises_before_any_request_keeps_its_bound_and_records_no_basis(tmp_path):
    """C05: a raise before the loop sent nothing the log can show; the loop did not end, so nothing is measured."""
    executor = FakeExecutor(default=("raise_infra", "sandbox"))
    row, cost, bound = run_once(tmp_path / "out", executor)
    assert (row["state"], row["usage_complete"], row["spend_basis"]) == ("infra_fail", 0, None)
    assert (cost.usd, cost.bounded) == (pytest.approx(bound), True)


def test_c05_without_a_budget_a_raise_after_the_loop_is_unchanged_since_no_call_log_is_kept(tmp_path):
    """C05: no budget, no spend guard and no call log: the usage stays unknown and the row records no basis."""
    row, _, _ = run_once(tmp_path / "out", AfterItsLoop(), budget=False)
    assert (row["state"], row["usage_complete"], row["spend_basis"]) == ("infra_fail", 0, None)
    assert (row["calls_sent"], row["input_tokens"]) == (None, None)


def test_c05_a_kill_after_the_marker_commits_the_measured_basis_with_the_marked_result(tmp_path):
    """C05: the marker carries the measured counts and the basis, so recovery commits them as the live finish would."""

    def killed_after_marker(point, run_key):
        if point == "after_marker":
            raise OSError("the process is killed here")

    options = {"executor": AfterItsLoop(), "settings": settings_for(budget=Budget(max_usd=50.0), max_attempts=1)}
    killing = build(tmp_path / "out", plan=PLAN, models=TABLE, crash_hook=killed_after_marker, **options)
    with killing as killed, pytest.raises(RuntimeError, match="unexpected error"):
        killed.run()
    with build(tmp_path / "out", plan=PLAN, models=TABLE, **options) as restart:
        [row] = restart.store.q("SELECT state, usage_complete, input_tokens, spend_basis FROM attempts")
        assert tuple(row) == ("infra_fail", 1, 9_000, MEASURED)
        [cost] = attempt_costs(restart.store, {"m1": TABLE.resolve("m1")}, restart.agent_config.limits)
        assert cost.usd == pytest.approx(MEASURED_USD)


def failed(**changes):
    return AttemptResult(
        Outcome("infrastructure", "leak", infra_kind="sandbox"), ENDED, 1.0, usage_complete=False, **changes
    )


def test_c05_the_basis_is_kept_by_a_marker_and_a_marker_without_one_still_reads(tmp_path):
    """C05: `spend_basis` is an optional field of marker schema 3: written and read back, absent in a marker written
    before it existed, and refused when it names a basis the core does not charge on."""
    assert SPEND_BASES == (MEASURED, BOUND)  # markers cannot import the names, so they say them again
    write_marker(tmp_path, "run", 1, failed(spend_basis=MEASURED))
    assert read_marker(tmp_path, "run", 1).spend_basis == MEASURED
    path = tmp_path / MARKER_NAME
    body = json.loads(path.read_text())
    del body["result"]["spend_basis"]
    path.write_text(json.dumps(body))
    assert read_marker(tmp_path, "run", 1).spend_basis is None
    body["result"]["spend_basis"] = "guessed"
    path.write_text(json.dumps(body))
    with pytest.raises(MarkerError, match="spend_basis"):
        read_marker(tmp_path, "run", 1)


def test_c05_settle_usage_leaves_a_result_whose_usage_is_known_as_it_is():
    """C05: a result that already knows its usage (no request was sent) is not touched by the log."""
    known = AttemptResult(
        Outcome("infrastructure", "x", infra_kind="sandbox"), ENDED, 1.0, input_tokens=0, output_tokens=0
    )
    assert settle_usage(known, 0, "[]") is known


def test_c05_settle_usage_sums_the_replies_and_leaves_the_cache_counts_unknown_when_none_was_reported():
    """C05: the totals are the replies' counts, a cache count no reply reported stays None (never 0), and a request
    closed by a failed send adds no turn: its top-up is what it cost."""
    settled = settle_usage(failed(), 3, json.dumps([[4_000, 50, 1_000, 500], [5_000, 60, None, None], "failed"]))
    assert (settled.input_tokens, settled.output_tokens) == (9_000, 110)
    assert (settled.cache_creation_tokens, settled.cache_read_tokens) == (1_000, 500)
    assert settled.turn_usage == ((4_000, 50, 1_000, 500), (5_000, 60, None, None))
    assert (settled.usage_complete, settled.replies_checked, settled.spend_basis) == (True, True, MEASURED)
    plain = settle_usage(failed(), 1, json.dumps([[4_000, 50, None, None]]))
    assert (plain.cache_creation_tokens, plain.cache_read_tokens) == (None, None)


def test_c05_settle_usage_of_a_request_closed_only_by_a_failed_send_measures_nothing_spent_but_its_top_up():
    """C05: every request failed to send: no reply, no turn, zero tokens known to be none, charged its top-ups."""
    settled = settle_usage(failed(), 1, json.dumps(["failed"]))
    assert (settled.input_tokens, settled.output_tokens, settled.turn_usage) == (0, 0, ())
    assert (settled.usage_complete, settled.spend_basis) == (True, MEASURED)
