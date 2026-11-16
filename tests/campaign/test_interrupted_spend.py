"""An interrupted attempt is charged what its call log recorded, plus one request's bound for a request in flight, and
its whole bound only when the log cannot be read or trusted; never less than was recorded (C05)."""

import asyncio
import json
import random
import threading

import pytest
from campaign_support import LATER, NOW, finished, planned_store, run_one
from loop_support import FakeProvider
from record_support import CODE
from scheduler_support import FakeExecutor, build, models_for, settings_for

from expcore.campaign.attempt_bounds import (
    attempt_costs,
    bound_usd,
    journaled_cost,
    mean_run_usd,
    request_bound_usd,
)
from expcore.campaign.executor import record_run
from expcore.campaign.gate import Budget, BudgetGate
from expcore.campaign.interrupted_spend import BOUND, INFLIGHT, MEASURED, CallLog, measured_usd, merged, read_log
from expcore.campaign.lost_ledger import KEY as LOST_KEY
from expcore.campaign.recovery import recover
from expcore.campaign.topup import AttemptSpend, Ledger
from expcore.config.models import Limits, ModelSpec
from expcore.config.prices import Price, PriceTier, TieredPrice
from expcore.models.adapters import ModelTurn, ToolCall

# The stub's bound is 30 USD: a 1.9M-token cap and its 100,000-token window, both at the dearest 15 USD per million,
# since nothing holds a stub's reply to its maximum (C25); one request's bound is that window at 15: 1.50.
LIMITS = Limits(
    max_tool_rounds=10, time_cap_s=60.0, token_budget=1_900_000, output_cap_chars=100, max_output_tokens=20_000
)
SPEC = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen", price=Price(3.0, 15.0), context_tokens=100_000)
BOUND_USD, REQUEST_USD = 30.0, 1.5
FIRST, SECOND = (10_000, 500, 0, 0), (20_000, 800, 0, 0)
FIRST_USD = (10_000 * 3.0 + 500 * 15.0) / 1e6
SECOND_USD = (20_000 * 3.0 + 800 * 15.0) / 1e6


def keys_of(manifest):
    return [row.key.text for row in manifest.rows]


def logged(store, key, *calls):
    """Begin an attempt with a call log and replay `calls` into it: `"sent"`, or a reply's counts (None: unread)."""
    begun = store.begin_attempt(key, NOW, 1, "", calls_logged=True)
    for call in calls:
        if call == "sent":
            store.note_call_sent(begun.id)
        else:
            store.note_call_answered(begun.id, call)
    return begun


def charged(store, models=None):
    [cost] = [c for c in attempt_costs(store, models or {"m1": SPEC}, LIMITS) if c.state == "interrupted"]
    return cost


def recorded_basis(store, attempt_id):
    return store.q("SELECT spend_basis FROM attempts WHERE id=?", (attempt_id,))[0]["spend_basis"]


def test_c05_two_replies_recorded_and_one_request_in_flight_are_charged_the_replies_and_that_requests_bound(tmp_path):
    """C05: a partial log, two requests answered and a third sent when the kill came, is charged the two replies'
    counts at the model's price and the third at the most one request can cost, far below the attempt's bound."""
    store, _, manifest = planned_store(tmp_path)
    begun = logged(store, keys_of(manifest)[0], "sent", FIRST, "sent", SECOND, "sent")
    recover(store, LATER, 3)
    cost = charged(store)
    assert cost.basis == INFLIGHT == recorded_basis(store, begun.id)
    assert cost.usd == pytest.approx(FIRST_USD + SECOND_USD + REQUEST_USD)
    assert request_bound_usd(SPEC, LIMITS) == REQUEST_USD
    assert cost.usd < bound_usd(SPEC, LIMITS) == BOUND_USD
    assert cost.bounded  # it holds a bound: no sample of what a run costs


def test_c05_a_kill_mid_call_charges_that_call_at_its_most(tmp_path):
    """C05: a request sent and never answered may have been billed in full: it is charged one request's bound, the
    prompt within the window and the reply within what holds it, at the dearest rates."""
    store, _, manifest = planned_store(tmp_path)
    logged(store, keys_of(manifest)[0], "sent")
    recover(store, LATER, 3)
    cost = charged(store)
    assert (cost.basis, cost.usd) == (INFLIGHT, pytest.approx(REQUEST_USD))


def test_c05_every_request_answered_is_charged_what_the_replies_recorded(tmp_path):
    """C05: a kill between two requests left nothing in flight: the attempt costs what its replies recorded, and it is
    still no sample of what a run costs, since the kill cut it short."""
    store, _, manifest = planned_store(tmp_path)
    first = keys_of(manifest)[0]
    logged(store, first, "sent", FIRST, "sent", SECOND)
    recover(store, LATER, 3)
    run_one(store, first, finished(input_tokens=1_000_000, output_tokens=100_000))  # its run, run again: 4.5
    costs = attempt_costs(store, {"m1": SPEC}, LIMITS)
    [cost] = [c for c in costs if c.state == "interrupted"]
    assert (cost.basis, cost.usd, cost.bounded) == (MEASURED, pytest.approx(FIRST_USD + SECOND_USD), False)
    assert mean_run_usd(store, costs, "m1") == pytest.approx(4.5)  # the interrupted attempt is left out (C05)


@pytest.mark.parametrize(
    ("usage", "sent"),
    [
        ("not json", 1),  # unreadable
        ("[[10000, 500, 0, 0]", 1),  # truncated
        ("[[10000, 500, 0, 0], [20000, 800, 0, 0]]", 1),  # more replies than requests
        ("[[10000, 500, 0, 0]]", 3),  # two requests unanswered: no sequential loop leaves that
        ("[[10000, -5, 0, 0]]", 1),  # a count that is no count
        ("[[100, 5, 90, 90]]", 1),  # cache parts above the prompt
        ('{"calls": 1}', 1),  # not a list
    ],
)
def test_c05_a_log_that_cannot_be_read_or_is_inconsistent_charges_the_whole_bound(tmp_path, usage, sent):
    """C05: an unreadable, truncated or inconsistent log may say less than was spent: the attempt costs its bound."""
    store, _, manifest = planned_store(tmp_path)
    begun = logged(store, keys_of(manifest)[0], "sent", FIRST)
    store.db.execute("UPDATE attempts SET calls_usage=?, calls_sent=? WHERE id=?", (usage, sent, begun.id))
    recover(store, LATER, 3)
    cost = charged(store)
    assert (cost.basis, cost.usd) == (BOUND, pytest.approx(BOUND_USD))
    assert recorded_basis(store, begun.id) == BOUND


@pytest.mark.parametrize("reply", [(10_000, None, 0, 0), (None, 500, None, None), None])
def test_c05_a_reply_that_left_a_count_out_or_was_not_read_charges_the_whole_bound(tmp_path, reply):
    """C05: a reply whose prompt or reply count is unknown, or that the endpoint billed and the backend could not read,
    has no cost the log can say: the attempt is charged its bound."""
    store, _, manifest = planned_store(tmp_path)
    logged(store, keys_of(manifest)[0], "sent", FIRST, "sent", reply)
    recover(store, LATER, 3)
    cost = charged(store)
    assert (cost.basis, cost.usd) == (BOUND, pytest.approx(BOUND_USD))


def test_c05_a_log_with_no_request_charges_the_bound(tmp_path):
    """C05: a kill before the first request and an executor whose requests bypass the recording (and so the log) leave
    the same empty log, and only the second may have spent: the bound."""
    store, _, manifest = planned_store(tmp_path)
    begun = logged(store, keys_of(manifest)[0])
    recover(store, LATER, 3)
    assert (charged(store).basis, charged(store).usd) == (BOUND, pytest.approx(BOUND_USD))
    assert recorded_basis(store, begun.id) == BOUND


def test_c05_an_attempt_that_kept_no_log_is_charged_its_bound_and_records_no_basis(tmp_path):
    """C05: without a budget no spend guard keeps a log: the attempt is charged its bound as before, and the store
    records no basis, so its export is the one it was."""
    store, _, manifest = planned_store(tmp_path)
    begun = store.begin_attempt(keys_of(manifest)[0], NOW, 1, "")
    recover(store, LATER, 3)
    cost = charged(store)
    assert (cost.basis, cost.usd) == (None, pytest.approx(BOUND_USD))
    assert recorded_basis(store, begun.id) is None


def test_c05_a_store_whose_ledger_names_a_spend_journal_records_the_bound_until_the_journal_confirms(tmp_path):
    """C05: a store that was journaled may be a snapshot whose log the attempt outgrew after the copy: recovery does
    not trust its log, and records `bound`, which only its journal's count raises (`lost_spend.reconcile`)."""
    store, _, manifest = planned_store(tmp_path)
    begun = logged(store, keys_of(manifest)[0], "sent", FIRST, "sent", SECOND)
    ledger = {"campaign": "c" * 64, "journal": "j1.jsonl", "counted": {}}
    store.put_meta(LOST_KEY, json.dumps(ledger), NOW)
    recover(store, LATER, 3)
    assert recorded_basis(store, begun.id) == BOUND
    assert charged(store).usd == pytest.approx(BOUND_USD)


def test_c05_an_unreadable_ledger_records_the_bound(tmp_path):
    """C05: a ledger that cannot be read cannot say the store was never journaled: the bound."""
    store, _, manifest = planned_store(tmp_path)
    begun = logged(store, keys_of(manifest)[0], "sent", FIRST)
    store.put_meta(LOST_KEY, "not a ledger", NOW)
    recover(store, LATER, 3)
    assert recorded_basis(store, begun.id) == BOUND


def test_c05_a_row_whose_log_no_longer_says_its_recorded_basis_is_charged_its_bound(tmp_path):
    """C05: the basis recorded at recovery is what the gate charges by, and only while the log still says it: a log
    changed since (a request added, a reply taken away) charges the bound, never the smaller figure."""
    store, _, manifest = planned_store(tmp_path)
    begun = logged(store, keys_of(manifest)[0], "sent", FIRST)
    recover(store, LATER, 3)
    assert charged(store).basis == MEASURED
    store.db.execute("UPDATE attempts SET calls_sent = calls_sent + 1 WHERE id=?", (begun.id,))
    cost = charged(store)
    assert (cost.basis, cost.usd) == (BOUND, pytest.approx(BOUND_USD))


def test_c05_cache_writes_and_reads_are_charged_at_their_own_prices(tmp_path):
    """C05: a reply's cache parts are priced as a finished attempt's are: a cache write dearer than input is never
    charged as input."""
    price = Price(3.0, 15.0, cache_write_per_mtok=3.75, cache_read_per_mtok=0.3)
    spec = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen", price=price, context_tokens=100_000)
    store, _, manifest = planned_store(tmp_path)
    logged(store, keys_of(manifest)[0], "sent", (10_000, 500, 4_000, 5_000))
    recover(store, LATER, 3)
    expected = (1_000 * 3.0 + 4_000 * 3.75 + 5_000 * 0.3 + 500 * 15.0) / 1e6
    assert charged(store, {"m1": spec}).usd == pytest.approx(expected)


def test_c05_a_tiered_price_charges_each_reply_at_the_tier_its_own_prompt_fell_in(tmp_path):
    """C05: the log keeps each request apart, so a tiered price applies to each at its own tier (C24)."""
    tiered = TieredPrice((PriceTier(15_000, Price(3.0, 15.0)), PriceTier(None, Price(6.0, 22.5))))
    spec = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen", price=tiered, context_tokens=100_000)
    store, _, manifest = planned_store(tmp_path)
    logged(store, keys_of(manifest)[0], "sent", FIRST, "sent", SECOND)
    recover(store, LATER, 3)
    expected = FIRST_USD + (20_000 * 6.0 + 800 * 22.5) / 1e6
    assert charged(store, {"m1": spec}).usd == pytest.approx(expected)


def test_c05_nothing_is_ever_charged_less_than_its_log_recorded():
    """C05: whatever the log holds, the charge is at least what its replies recorded at the model's price: measured,
    plus one request's bound for a request in flight, or the whole bound when it cannot be read."""
    rng = random.Random(5)
    for _ in range(500):
        replies = []
        for _ in range(rng.randrange(0, 7)):
            prompt = rng.randrange(1, 200_000)
            written = rng.randrange(0, prompt + 1)
            replies.append((prompt, rng.randrange(0, 20_000), written, rng.randrange(0, prompt - written + 1)))
        answered = tuple(replies)
        log = CallLog(len(answered) + rng.randrange(0, 2), answered)
        recorded = measured_usd(SPEC, answered)
        sent, usage = log.as_columns()
        row = {"model": "m1", "reserved_capped": 0, "calls_sent": sent, "calls_usage": usage}
        usd, _, basis = journaled_cost(SPEC, row, LIMITS)
        assert read_log(sent, usage) == log
        assert usd >= recorded
        if log.sent == 0:  # no request in the log: the bound
            assert (basis, usd) == (BOUND, BOUND_USD)
            continue
        assert usd == pytest.approx(recorded + (REQUEST_USD if log.in_flight else 0.0))
        assert basis == (INFLIGHT if log.in_flight else MEASURED)


def test_c05_a_merged_log_takes_the_longer_only_when_it_extends_the_other():
    """C05: the store's log and its own journal's are one sequence, either a prefix of the other; two that disagree
    say nothing that can be trusted."""
    short, longer = CallLog(2, (FIRST,)), CallLog(3, (FIRST, SECOND))
    assert merged(short, longer) == longer == merged(longer, short)
    assert merged(CallLog(2, (SECOND,)), longer) is None
    assert merged(None, longer) is None
    assert merged(short, None) is None


def test_c05_the_attempts_spend_guard_counts_each_request_before_it_goes_out_and_each_reply_with_its_parts(tmp_path):
    """C05: the guard writes a request into the log only once it is cleared, so a refused request is never counted,
    and each reply with its counts and cache parts, a reply that was not read as such."""
    store, _, manifest = planned_store(tmp_path)
    key = keys_of(manifest)[0]
    begun = store.begin_attempt(key, NOW, 1, "", calls_logged=True)
    gate = BudgetGate(store, {"m1": SPEC}, Budget(max_usd=1_000.0), LIMITS)
    ledger = Ledger(gate, store, lambda: NOW, (threading.Lock(),), lambda: ["m1"])
    guard = AttemptSpend(ledger, begun.id, key, "m1", reserved_capped=False, calls_logged=True)

    async def go():
        await guard.before_request()
        await guard.after_reply(10_000, 500, cache_creation_tokens=4_000, cache_read_tokens=5_000)
        await guard.before_request()
        await guard.after_reply(11_000, 600, unread=True)
        await guard.before_request()

    asyncio.run(go())
    [row] = store.q("SELECT calls_sent, calls_usage FROM attempts WHERE id=?", (begun.id,))
    assert row["calls_sent"] == 3
    assert json.loads(row["calls_usage"]) == [[10_000, 500, 4_000, 5_000], None]


def test_c05_a_request_the_guard_refuses_is_not_counted(tmp_path):
    """C05: a request that a top-up cannot cover is not sent, so it is not in the log."""
    store, _, manifest = planned_store(tmp_path)
    key = keys_of(manifest)[0]
    begun = store.begin_attempt(key, NOW, 1, "", calls_logged=True)
    gate = BudgetGate(store, {"m1": SPEC}, Budget(max_usd=BOUND_USD + 0.1), LIMITS)
    ledger = Ledger(gate, store, lambda: NOW, (threading.Lock(),), lambda: ["m1"])
    guard = AttemptSpend(ledger, begun.id, key, "m1", reserved_capped=False, calls_logged=True)

    async def go():
        await guard.before_request()
        await guard.after_reply(None, 60)  # a count left out: the next request needs a top-up, which does not fit
        await guard.before_request()

    with pytest.raises(Exception, match="was not sent"):
        asyncio.run(go())
    [row] = store.q("SELECT calls_sent FROM attempts WHERE id=?", (begun.id,))
    assert row["calls_sent"] == 1


def test_c05_a_store_refuses_a_log_entry_for_an_attempt_that_is_not_running_or_keeps_no_log(tmp_path):
    """C05: a request or reply noted for an attempt that ended, or that began without a log, is refused loudly."""
    store, _, manifest = planned_store(tmp_path)
    first, second = keys_of(manifest)[:2]
    plain = store.begin_attempt(first, NOW, 1, "")
    with pytest.raises(Exception, match="not running with a call log"):
        store.note_call_sent(plain.id)
    ended = logged(store, second, "sent", FIRST)
    store.finish_attempt(ended.id, finished(input_tokens=10_000, output_tokens=500), 3)
    with pytest.raises(Exception, match="not running with a call log"):
        store.note_call_answered(ended.id, FIRST)


# --- through the scheduler: the attempt's guard keeps the log from the core's recording -------------------------------


class ToolRoundThenAnswer:
    """The study's executor, through the core's recording: a tool round, then an answer, each reply with its counts."""

    def __init__(self):
        self.config = FakeExecutor().config

    async def __call__(self, request):
        tool_round = ModelTurn("", [ToolCall("call_first", "look", {})], 4_000, 50, "tool_calls", 0.0)
        request.backend.use([tool_round, ModelTurn("done", [], 5_000, 60, "stop", 0.0)])
        providers = [FakeProvider({"look": lambda arguments: "ok"})]
        return await record_run(request, providers, "be brief", "do the task", self.config, code=dict(CODE))


def killed_before_marker(point, run_key):
    if point == "before_marker":
        raise OSError("the process is killed here")


def test_c05_a_dispatcher_killed_after_its_replies_charges_them_at_the_restart_through_the_real_attempt(tmp_path):
    """C05: with a budget the attempt's row keeps a log, its guard counts each request and reply the recording makes,
    and a kill before the result was stored leaves the restart charging the replies' counts, not the bound."""
    table = models_for("m1", price=Price(3.0, 15.0), context_tokens=16_000)
    plan = {"models": ("m1",), "tasks": ("task-a",), "seeds": (1,)}
    options = {"executor": ToolRoundThenAnswer(), "settings": settings_for(budget=Budget(max_usd=50.0))}
    killing = build(tmp_path / "out", plan=plan, models=table, crash_hook=killed_before_marker, **options)
    with killing as killed, pytest.raises(RuntimeError, match="unexpected error"):
        killed.run()
    with build(tmp_path / "out", plan=plan, models=table, **options) as restart:
        [row] = restart.store.q("SELECT state, calls_sent, calls_usage, spend_basis FROM attempts")
        assert (row["state"], row["calls_sent"], row["spend_basis"]) == ("interrupted", 2, MEASURED)
        assert json.loads(row["calls_usage"]) == [[4_000, 50, None, None], [5_000, 60, None, None]]
        [cost] = attempt_costs(restart.store, {"m1": table.resolve("m1")}, restart.agent_config.limits)
        assert cost.usd == pytest.approx((9_000 * 3.0 + 110 * 15.0) / 1e6)


# --- the pilot's case: a 60 USD ceiling and a bound of 32.975 USD for one model --------------------------------------
PILOT_LIMITS = Limits(
    max_tool_rounds=30, time_cap_s=600.0, token_budget=1_119_000, output_cap_chars=8000, max_output_tokens=8000
)
PILOT = ModelSpec(
    key="m1", backend="stub", api_id="m1", status="frozen", price=Price(5.0, 25.0), context_tokens=200_000
)


def pilot_gate(tmp_path, *calls):
    store, _, manifest = planned_store(tmp_path)
    if calls:
        logged(store, keys_of(manifest)[0], *calls)
    else:
        store.begin_attempt(keys_of(manifest)[0], NOW, 1, "")  # a core before the log: no log
    recover(store, LATER, 3)
    return BudgetGate(store, {"m1": PILOT}, Budget(max_usd=60.0), PILOT_LIMITS)


def test_c05_the_pilots_kill_no_longer_holds_the_models_remaining_runs_when_its_measured_spend_allows(tmp_path):
    """C05: with a 60 USD ceiling and a bound of 32.975 USD, one attempt killed in flight and charged its bound left
    the next run over the ceiling (65.95 USD); charged its two replies and the request in flight, the next run fits."""
    assert bound_usd(PILOT, PILOT_LIMITS) == pytest.approx(32.975)
    held = pilot_gate(tmp_path / "before").assess([], LATER)
    assert "65.95 USD (spent 32.98 + in flight 0.00 + this run 32.98) is over the ceiling of 60.00 USD" in (
        held.reason("m1") or ""
    )
    gate = pilot_gate(tmp_path / "after", "sent", (50_000, 2_000, 0, 0), "sent", (60_000, 1_500, 0, 0), "sent")
    state = gate.assess([], LATER)
    measured = (110_000 * 5.0 + 3_500 * 25.0) / 1e6
    assert state.spent_usd == pytest.approx(measured + request_bound_usd(PILOT, PILOT_LIMITS))
    assert state.reason("m1") is None
    assert state.spent_usd + 32.975 <= 60.0
