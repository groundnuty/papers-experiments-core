"""Top-ups for the sends a run's token cap cannot account for, and the budget hold when one does not fit (C26)."""

import asyncio
import json
import threading
import time

import pytest
from campaign_support import NOW, finished, infra, planned_store
from loop_support import FakeProvider
from record_support import CODE
from scheduler_support import FakeExecutor, attempts_of, build, event_kinds, models_for, settings_for

from expcore.campaign.attempt_bounds import attempt_costs, bound_usd, request_bound_usd
from expcore.campaign.executor import record_run
from expcore.campaign.export import export_run_state
from expcore.campaign.gate import Budget, BudgetGate
from expcore.campaign.markers import MARKER_NAME, write_marker
from expcore.campaign.recovery import recover
from expcore.campaign.topup import AttemptSpend, Ledger
from expcore.config.models import Limits
from expcore.config.prices import Price
from expcore.errors import BudgetHold
from expcore.models.adapters import ModelTurn, ToolCall
from expcore.replay.replay import replay_record
from expcore.report import render_report
from expcore.stats.analysis import AnalysisResult

MODEL = "quince"
TABLE = models_for(MODEL, price=Price(3.0, 15.0), context_tokens=16_000)
SPEC = TABLE.resolve(MODEL)
LIMITS = Limits(
    max_tool_rounds=10, time_cap_s=300.0, token_budget=100_000, output_cap_chars=200, max_output_tokens=4096
)  # the support executor's caps
# A stub's run is reserved its whole cap and its whole window at 15 USD per million, 1.50 + 0.24; nothing holds a
# stub's reply to the reply maximum, so one request's bound, a top-up, is the window at that rate: 0.24.
BOUND, TOPUP = 1.74, 0.24
NO_CONTRASTS = AnalysisResult((), (), 0.05, {"preregistration_sha256": "e" * 64})
STAMP = {"commit": "c" * 40, "tree_sha256": "d" * 64, "dirty": False, "why": None}


def ledger_for(store, max_usd: float, in_flight=(MODEL,)) -> Ledger:
    gate = BudgetGate(store, {MODEL: SPEC}, Budget(max_usd=max_usd), LIMITS)
    return Ledger(gate, store, lambda: NOW, (threading.Lock(),), lambda: list(in_flight))


def running(tmp_path):
    """A store with one running attempt of the model's first run."""
    store, _, manifest = planned_store(tmp_path, models=(MODEL,))
    key = manifest.rows[0].key.text
    return store, key, store.begin_attempt(key, NOW, 4321, str(tmp_path / "attempt"))


def condition_of(store, key) -> str:
    """The condition of a run."""
    factors = json.loads(store.q("SELECT factors FROM runs WHERE run_key=?", (key,))[0]["factors"])
    return store.schema.condition_named(factors, key)


def measured(store, key, **usage):
    """A finished result with the given usage, carrying the treatment kinds of the run's condition."""
    return finished(treatment_kinds=store.schema.treatment_kinds[condition_of(store, key)], **usage)


def charged(store, attempt_id):
    row = store.q("SELECT topups, topup_usd FROM attempts WHERE id=?", (attempt_id,))[0]
    return row["topups"], row["topup_usd"]


def test_c26_the_bounds_of_this_file_are_the_stubs_cap_and_window_at_the_dearest_rate():
    """C26: the figures the tests below rest on."""
    assert bound_usd(SPEC, LIMITS) == pytest.approx(BOUND)
    assert request_bound_usd(SPEC, LIMITS) == pytest.approx(TOPUP)


def test_c26_a_top_up_that_fits_is_charged_at_once_counted_while_the_attempt_runs_and_never_refunded(tmp_path):
    """C26: the top-up is spent from the moment it is reserved; once the attempt ends with known usage, it costs that
    usage plus its top-up, and is no measurement for the mean."""
    store, key, begun = running(tmp_path)
    ledger = ledger_for(store, 10.0)
    assert ledger.reserve(begun.id, key, MODEL, "a request after a reply that did not report its usage") is None
    assert charged(store, begun.id) == (1, pytest.approx(TOPUP))
    assert ledger.gate.assess([MODEL], NOW).spent_usd == pytest.approx(TOPUP)
    store.finish_attempt(begun.id, measured(store, key, input_tokens=10_000, output_tokens=1_000), 3)
    [cost] = attempt_costs(store, {MODEL: SPEC}, LIMITS)
    assert cost.usd == pytest.approx((10_000 * 3.0 + 1_000 * 15.0) / 1e6 + TOPUP)
    assert cost.bounded
    assert "topup" in event_kinds(store)


def test_c26_an_attempt_held_before_any_reply_costs_its_top_ups_alone_and_the_export_agrees_its_usage_is_known(
    tmp_path,
):
    """C26 and C05: a failed send was topped up, then the next send was refused, so the attempt ended a budget hold
    before any reply. Its usage is complete with no turn: nothing was answered, so it costs what its top-ups were
    charged and no bound, and the export counts its usage as complete, from the same column the gate reads."""
    store, key, begun = running(tmp_path)
    store.add_topup(begun.id, TOPUP, NOW, ("topup", MODEL, f"{key}: a send that failed"))
    kinds = store.schema.treatment_kinds[condition_of(store, key)]
    store.finish_attempt(begun.id, infra("budget_hold", model_turns=0, turn_usage=(), treatment_kinds=kinds), 3)
    [cost] = attempt_costs(store, {MODEL: SPEC}, LIMITS)
    assert cost.usd == pytest.approx(TOPUP)
    [cell] = export_run_state(store)["discarded_usage"]
    assert (cell["attempts"], cell["usage_incomplete_attempts"]) == (1, 0)


def test_c26_a_row_written_before_its_replies_were_checked_holding_no_turn_is_still_charged_its_bound(tmp_path):
    """C26 and C05: before a billed reply that became no turn left an attempt's usage incomplete, such an attempt was
    stored complete with no turn, like one held before any reply. A row written then cannot tell the two apart, so it
    keeps the rule it was written under and is charged its bound: a result recovered from a marker written then, and
    a row a store migration brought forward."""
    store, key, begun = running(tmp_path)
    kinds = store.schema.treatment_kinds[condition_of(store, key)]
    run_dir = tmp_path / "attempt"
    run_dir.mkdir(parents=True, exist_ok=True)
    write_marker(run_dir, key, begun.id, infra("endpoint", model_turns=0, turn_usage=(), treatment_kinds=kinds))
    marker = run_dir / MARKER_NAME
    body = json.loads(marker.read_text(encoding="utf-8"))
    body["result"].pop("replies_checked", None)  # what a marker written before the check holds
    marker.write_text(json.dumps(body), encoding="utf-8")
    assert recover(store, NOW, 3).committed == 1
    [cost] = attempt_costs(store, {MODEL: SPEC}, LIMITS)
    assert cost.usd == pytest.approx(BOUND)
    store.db.execute("UPDATE attempts SET replies_checked = NULL")  # a row from before the column
    [cost] = attempt_costs(store, {MODEL: SPEC}, LIMITS)
    assert cost.usd == pytest.approx(BOUND)


def test_c26_a_top_up_that_does_not_fit_is_refused_with_nothing_charged_and_the_figure_in_an_event(tmp_path):
    """C26: the figure is the gate's, the run's own reservation among the runs in flight and the top-up in place of a
    new run's bound."""
    store, key, begun = running(tmp_path)
    refusal = ledger_for(store, 1.80).reserve(begun.id, key, MODEL, "a request after a reply without usage")
    assert refusal == (
        "the campaign: 1.98 USD (spent 0.00 + in flight 1.74 + this top-up 0.24) is over the ceiling of 1.80 USD"
    )
    assert charged(store, begun.id) == (0, 0.0)
    [event] = store.q("SELECT kind, model, detail FROM events WHERE kind='topup_refused'")
    assert (event["model"], refusal in event["detail"], key in event["detail"]) == (MODEL, True, True)


def test_c26_top_ups_reserved_at_once_never_take_more_than_the_budget_has_left(tmp_path):
    """C26: each top-up is decided and charged under the dispatcher's locks, so of eight asked for at once with room
    for three, three are charged and five refused."""
    store, key, begun = running(tmp_path)
    ledger = ledger_for(store, BOUND + 3 * TOPUP + 0.1)
    start, refusals = threading.Barrier(8), []

    def ask() -> None:
        start.wait()
        refusals.append(ledger.reserve(begun.id, key, MODEL, "a request after a reply without usage"))

    workers = [threading.Thread(target=ask) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert sorted(r is None for r in refusals) == [False] * 5 + [True] * 3
    assert charged(store, begun.id) == (3, pytest.approx(3 * TOPUP))


def test_c26_after_a_reply_that_left_a_count_out_every_later_request_is_topped_up_until_one_does_not_fit(tmp_path):
    """C26: the loop counts a missing count as 0, so from that reply on the cap does not bound the run: each later
    request reserves its own bound, and the one that does not fit is a budget hold. The reply is an event."""
    store, key, begun = running(tmp_path)
    guard = AttemptSpend(ledger_for(store, BOUND + 2 * TOPUP + 0.01), begun.id, key, MODEL, reserved_capped=False)

    async def go():
        await guard.before_request()  # the first request: the cap accounts for it
        await guard.after_reply(4_000, 50)
        await guard.before_request()
        await guard.after_reply(None, 60)  # a reply that left its input out
        for _ in range(3):
            await guard.before_request()

    with pytest.raises(BudgetHold, match="a request after a reply that did not report its usage was not sent"):
        asyncio.run(go())
    assert charged(store, begun.id) == (2, pytest.approx(2 * TOPUP))
    [unreported] = store.q("SELECT detail FROM events WHERE kind='usage_unreported'")
    assert "reported input no and output 60 tokens" in unreported["detail"]


@pytest.mark.parametrize("max_usd", [10.0, 1.80])
def test_c26_every_send_that_failed_after_it_may_have_been_billed_is_charged_whether_or_not_it_is_sent_again(
    tmp_path, max_usd
):
    """C26: the top-up of a resend is reserved first; one that does not fit leaves the request unsent, as a budget
    hold, and the failed send is charged all the same, as is the last failed send of a request, after which nothing
    is sent again."""
    store, key, begun = running(tmp_path)
    guard = AttemptSpend(ledger_for(store, max_usd), begun.id, key, MODEL, reserved_capped=False)
    if max_usd == 10.0:
        asyncio.run(guard.after_failed_send("server", resend=True))
        asyncio.run(guard.after_failed_send("connection", resend=False))
        assert charged(store, begun.id) == (2, pytest.approx(2 * TOPUP))
        return
    with pytest.raises(BudgetHold, match="not sent again after a server failure"):
        asyncio.run(guard.after_failed_send("server", resend=True))
    assert charged(store, begun.id) == (1, pytest.approx(TOPUP))
    assert "topup_refused" in event_kinds(store)


def test_c26_a_top_up_survives_a_kill_both_when_the_attempt_is_interrupted_and_when_its_marker_is_committed(tmp_path):
    """C26 and C04: a top-up is in the store before the kill, and neither recovery path touches it: an interrupted
    attempt costs its bound and its top-ups, a result committed from its marker its usage and its top-ups."""
    store, _, manifest = planned_store(tmp_path, models=(MODEL,))
    first, second = manifest.rows[0].key.text, manifest.rows[1].key.text
    ledger = ledger_for(store, 10.0, in_flight=(MODEL, MODEL))
    killed = store.begin_attempt(first, NOW, 4321, str(tmp_path / "killed"))
    marked = store.begin_attempt(second, NOW, 4321, str(tmp_path / "marked"))
    for attempt, key in ((killed, first), (marked, second)):
        assert ledger.reserve(attempt.id, key, MODEL, "a request after a reply without usage") is None
    write_marker(tmp_path / "marked", second, marked.id, measured(store, second, input_tokens=2_000, output_tokens=100))
    outcome = recover(store, NOW, 3)
    assert (outcome.committed, outcome.interrupted) == (1, 1)
    costs = {c.attempt_id: c for c in attempt_costs(store, {MODEL: SPEC}, LIMITS)}
    assert costs[killed.id].usd == pytest.approx(BOUND + TOPUP)
    assert costs[marked.id].usd == pytest.approx((2_000 * 3.0 + 100 * 15.0) / 1e6 + TOPUP)
    assert charged(store, killed.id) == charged(store, marked.id) == (1, pytest.approx(TOPUP))


# --- through the scheduler --------------------------------------------------------------------------------------------


class UnreportedThenAnswer:
    """The study's executor: a tool round whose reply left its input count out, then an answer; keeps each record."""

    def __init__(self):
        self.config, self.records = FakeExecutor().config, []

    async def __call__(self, request):
        script = [ModelTurn("", [ToolCall("call_first", "look", {})], None, 9, "tool_calls", 0.0), "the answer"]
        request.backend.use(script)
        record = await record_run(
            request,
            [FakeProvider({"look": lambda arguments: "ok"})],
            "be brief",
            "do the task",
            self.config,
            code=dict(CODE),
            core_version="0.1.0",
        )
        self.records.append(record)
        return record


def campaign(tmp_path, budget: Budget | None):
    plan = {"models": (MODEL,), "tasks": ("task-a",), "seeds": (1,)}
    settings = settings_for(budget=budget) if budget is not None else settings_for()
    return build(tmp_path / "campaign", executor=UnreportedThenAnswer(), settings=settings, plan=plan, models=TABLE)


def attempts(store):
    return store.q("SELECT run_key, state, charged, infra_kind, topups, topup_usd FROM attempts ORDER BY id")


def test_c26_a_request_the_budget_cannot_top_up_is_not_sent_and_its_attempt_is_a_budget_hold_set_aside(tmp_path):
    """C26: the request after a reply without usage needs 0.24 more, and only 0.06 is left: it is not sent, and the
    attempt ends as infrastructure of kind budget_hold, uncharged, its run pending and in no analysed list; the
    attempt costs its bound, its usage being unknown, so the campaign then holds the model for its budget."""
    with campaign(tmp_path, Budget(max_usd=1.80)) as scheduler:
        summary = scheduler.run()
        store = scheduler.store
        [row] = attempts(store)  # the only run the budget let start
        assert (row["state"], row["charged"], row["infra_kind"], row["topups"]) == ("infra_fail", 0, "budget_hold", 0)
        assert attempts_of(store, row["run_key"]) == [("infra_fail", 0, 1)]
        assert summary.holds[MODEL].kind == "budget"
        assert {"usage_unreported", "topup_refused"} <= set(event_kinds(store))
        export = export_run_state(store)
        assert export["analysed_attempts"] == []
        assert {(r["model"], r["kind"], r["state"], r["charged"]) for r in export["retries_by_kind"]} == {
            (MODEL, "budget_hold", "infra_fail", False)
        }
        [held] = [r for r in export["infrastructure_by_condition"] if r["kind"] == "budget_hold"]
        assert (held["set_aside"], held["treatment"], held["declared_treatment"]) == (1, 0, False)
        report = render_report(NO_CONTRASTS, export, study="topup-study", code=STAMP, core_version="0.1.0")
        rows = [
            line for line in report.split("### Budget holds by condition and model")[1].splitlines() if MODEL in line
        ]
        assert f"| {held['condition']} | {MODEL} | 1 |" in rows
        assert sorted(row.rsplit("|", 2)[1].strip() for row in rows) == ["0", "0", "1"]  # the other conditions' zeros
        [record] = scheduler.executor.records
        assert (record.outcome["name"], record.outcome["infra_kind"]) == ("infrastructure", "budget_hold")
        assert record.set_aside
        assert replay_record(record).to_json() == record.to_json()  # the refusal is the request's recorded failure


def test_c26_a_budget_hold_an_outage_overlapped_is_still_counted_as_a_budget_hold_in_the_export_and_the_report(
    tmp_path,
):
    """C26: an outage sets the held attempt aside under the outage's kind, but the attempt ended because the budget
    refused a send, so the export's `budget_holds` and the report's table count it by its own kind; another kind's
    attempt is not counted, and a pair the plan runs without a hold has its row of zeros."""
    store, _, manifest = planned_store(tmp_path, models=(MODEL,))
    (held_key, held_condition), (other_key, other_condition) = [
        (row.key.text, condition_of(store, row.key.text)) for row in manifest.rows[:2]
    ]
    for key, kind in ((held_key, "budget_hold"), (other_key, "endpoint")):
        begun = store.begin_attempt(key, NOW, 4321, str(tmp_path / kind))
        kinds = store.schema.treatment_kinds[condition_of(store, key)]
        store.finish_attempt(begun.id, infra(kind, outage_kind="endpoint", treatment_kinds=kinds), 3)
    assert {r["state"] for r in store.q("SELECT state FROM attempts")} == {"outage_discarded"}
    export = export_run_state(store)
    report = render_report(NO_CONTRASTS, export, study="topup-study", code=STAMP, core_version="0.1.0")
    table = report.split("### Budget holds by condition and model")[1]
    assert f"| {held_condition} | {MODEL} | 1 |" in table
    assert f"| {other_condition} | {MODEL} | 0 |" in table
    counted = {(r["condition"], r["model"]): r["attempts"] for r in export["budget_holds"]}
    planned = {(condition_of(store, row.key.text), MODEL) for row in manifest.rows}
    assert counted == {pair: int(pair == (held_condition, MODEL)) for pair in planned}


def test_c26_a_request_the_budget_can_top_up_is_sent_and_its_top_up_is_part_of_what_the_run_cost(tmp_path):
    """C26: with 0.26 left the top-up fits; the run finishes and costs its bound, its usage being unknown, plus 0.24."""
    with campaign(tmp_path, Budget(max_usd=2.0)) as scheduler:
        scheduler.run()
        [row] = attempts(scheduler.store)  # the only run the budget let start
        assert (row["state"], row["topups"], row["topup_usd"]) == ("done", 1, pytest.approx(TOPUP))
        assert scheduler.budget.assess([], NOW).spent_usd == pytest.approx(BOUND + TOPUP)


def test_c26_without_a_budget_nothing_is_topped_up_and_nothing_changes(tmp_path):
    """C26: no ceiling, no ledger: the same run finishes as before, with no top-up and no event of one."""
    with campaign(tmp_path, None) as scheduler:
        scheduler.run()
        rows = attempts(scheduler.store)
        assert [(r["state"], r["topups"], r["topup_usd"]) for r in rows] == [("done", 0, 0.0)] * 3
        assert not {"topup", "topup_refused", "usage_unreported"} & set(event_kinds(scheduler.store))


class CrashBesideATopUp:
    """The study's executor for two runs at once. The first run waits until the second has started, then finishes;
    the crash hook then fails its bookkeeping before its marker, a crash in our own code that leaves its row running.
    The second run's first reply leaves its input count out, and its tool returns only once the first run has left the
    dispatcher's runs in flight, so its next request asks for a top-up while the crashed attempt's row still runs."""

    def __init__(self):
        self.config, self.started = FakeExecutor().config, []
        self.second_started = threading.Event()
        self.scheduler = None  # set once the scheduler is built

    async def __call__(self, request):
        self.started.append(request.run_key)
        if len(self.started) == 1:
            await asyncio.to_thread(self.second_started.wait, 10)
            script = ["the answer"]
        else:
            self.second_started.set()
            script = [ModelTurn("", [ToolCall("call_first", "look", {})], None, 9, "tool_calls", 0.0), "the answer"]
        request.backend.use(script)
        provider = FakeProvider({"look": self.look})
        return await record_run(
            request, [provider], "be brief", "do the task", self.config, code=dict(CODE), core_version="0.1.0"
        )

    def look(self, arguments):
        deadline = time.monotonic() + 10
        while self.started[0] in self.scheduler._inflight and time.monotonic() < deadline:
            time.sleep(0.01)
        return "ok"


def test_c26_a_running_row_whose_worker_crashed_is_counted_at_its_bound_when_a_sibling_asks_for_a_top_up(tmp_path):
    """C26 and C17: a worker whose bookkeeping crashed after its attempt began leaves the dispatcher's runs in flight,
    but its row stays running and what it spent is unknown until the next start settles it. A sibling's top-up is
    then refused against that row's bound: 1.74 for it, 1.74 for the sibling and 0.24 for the top-up are over the
    ceiling of 3.60, which the two runs' reservations alone fit."""
    executor = CrashBesideATopUp()

    def crash_after_the_first_run(point, run_key):
        if point == "before_marker" and run_key == executor.started[0]:
            raise OSError("no space left on device")

    plan = {"models": (MODEL,), "tasks": ("task-a",), "seeds": (1,)}
    settings = settings_for(budget=Budget(max_usd=3.60), workers=2)
    root = tmp_path / "campaign"
    with build(
        root, executor=executor, settings=settings, plan=plan, models=TABLE, crash_hook=crash_after_the_first_run
    ) as scheduler:
        executor.scheduler = scheduler
        with pytest.raises(RuntimeError, match="unexpected error"):
            scheduler.run()
        rows = {row["run_key"]: row for row in attempts(scheduler.store)}
        crashed, sibling = (rows[key] for key in executor.started)
        assert crashed["state"] == "running"
        assert (sibling["state"], sibling["infra_kind"], sibling["topups"]) == ("infra_fail", "budget_hold", 0)
        assert "topup_refused" in event_kinds(scheduler.store)
