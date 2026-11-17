"""C39: what each model spent, by what became of the attempt, with the basis of every dollar.

The figures are the budget gate's own (`attempt_costs`, the lost-spend ledger and the probes' charges), read from a
snapshot of the store; nothing is priced a second time. Each attempt is shown on the basis it was charged on: measured
(the counts it reported, at the model's price), measured plus one request in flight, or its bound; its top-ups are
apart. A dollar that cannot be known is `None`, never 0.
"""

import json
import sqlite3

import pytest
from campaign_support import LATER, NOW, defect, finished, infra, planned_store, run_one

from expcore.campaign import probe_spend
from expcore.campaign.interrupted_spend import INFLIGHT, MEASURED
from expcore.campaign.lost_ledger import KEY as LOST_KEY
from expcore.campaign.model_usage import CLASSES, usage_of
from expcore.campaign.recovery import recover
from expcore.campaign.selection_ledger import Pricing, spend_of
from expcore.config.models import Limits, ModelSpec
from expcore.config.prices import Price, PriceTier, TieredPrice

# One attempt's bound is 30 USD and one request's 1.50, as in the interrupted-spend tests.
LIMITS = Limits(
    max_tool_rounds=10, time_cap_s=60.0, token_budget=1_900_000, output_cap_chars=100, max_output_tokens=20_000
)
SPEC = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen", price=Price(3.0, 15.0), context_tokens=100_000)
PRICING = Pricing({"m1": SPEC}, LIMITS)
TOKENS = {"input_tokens": 1_000_000, "output_tokens": 100_000}  # 4.5 USD
HALF = {"input_tokens": 500_000, "output_tokens": 100_000}  # 3.0 USD
FIRST = (10_000, 500, 0, 0)
FIRST_USD = (10_000 * 3.0 + 500 * 15.0) / 1e6
BOUND_USD, REQUEST_USD = 30.0, 1.5


def keys_of(manifest):
    return [row.key.text for row in manifest.rows]


def logged(store, key, *calls):
    """Begin an attempt with a call log and replay `calls` into it: `"sent"`, or a reply's counts."""
    begun = store.begin_attempt(key, NOW, 1, "", calls_logged=True)
    for call in calls:
        if call == "sent":
            store.note_call_sent(begun.id)
        else:
            store.note_call_answered(begun.id, call)
    return begun


def model_of(usage, name="m1"):
    [entry] = [m for m in usage["models"] if m["model"] == name]
    return entry


def klass(entry, name):
    [found] = [c for c in entry["classes"] if c["class"] == name]
    return found


def busy_store(tmp_path):
    """Every kind of attempt one model can have, and what each is charged: the keys it left out and the store."""
    store, _, manifest = planned_store(tmp_path)
    keys = keys_of(manifest)
    run_one(store, keys[0], finished(**TOKENS))  # analysed, measured 4.5
    run_one(store, keys[1], finished(**HALF))  # to be left out of the analysis, measured 3.0
    run_one(store, keys[2], infra("endpoint", input_tokens=100_000, output_tokens=10_000))  # measured 0.45
    run_one(store, keys[3], defect())  # no counts at all: its bound
    store.begin_attempt(keys[4], NOW, 1, "")  # killed with no log: its bound
    logged(store, keys[5], "sent", FIRST, "sent")  # killed with a request in flight
    recover(store, LATER, 3)
    topped = store.begin_attempt(keys[6], NOW, 1, "")  # an analysed attempt with a top-up
    store.add_topup(topped.id, 0.25, NOW, ("topup", "m1", "a send the cap could not account for"))
    store.finish_attempt(topped.id, finished(**TOKENS), 3)
    probe_spend.add(store, "m1", 0.1, NOW, ("probe", "m1", "an admission probe"))
    return store, keys


def test_c39_every_class_of_attempt_has_its_attempts_tokens_and_dollars_on_the_basis_it_was_charged_on(tmp_path):
    """C39: analysed, left out, infrastructure, harness error and interrupted attempts are each counted with their
    tokens and charged measured, measured plus a request in flight, or at their bound, their top-ups apart."""
    store, keys = busy_store(tmp_path)
    usage = usage_of(store.q, PRICING, left_out={keys[1]})
    m1 = model_of(usage)
    analysed = klass(m1, "analysed")
    assert (analysed["attempts"], analysed["input_tokens"], analysed["output_tokens"]) == (2, 2_000_000, 200_000)
    assert analysed["usd"] == {
        "measured": pytest.approx(9.0),
        "measured_plus_inflight": 0.0,
        "bound": 0.0,
        "charges": pytest.approx(0.25),
        "total": pytest.approx(9.25),
    }
    assert analysed["bounded_attempts"] == 1  # the one with a top-up holds a bound
    left = klass(m1, "left_out")
    assert (left["attempts"], left["usd"]["measured"]) == (1, pytest.approx(3.0))
    assert klass(m1, "infra_fail")["usd"]["measured"] == pytest.approx(0.45)
    harness = klass(m1, "harness_error")
    assert (harness["attempts"], harness["usage_incomplete_attempts"]) == (1, 1)
    assert harness["usd"]["bound"] == pytest.approx(BOUND_USD)
    killed = klass(m1, "interrupted")
    assert killed["attempts"] == 2
    assert killed["usd"]["bound"] == pytest.approx(BOUND_USD)
    assert killed["usd"]["measured_plus_inflight"] == pytest.approx(FIRST_USD + REQUEST_USD)
    assert killed["bounded_attempts"] == 2
    assert m1["probes_usd"] == pytest.approx(0.1)


def test_c39_the_models_total_is_the_budget_gates_own_figure_and_the_unanalysed_part_is_beside_it(tmp_path):
    """C39: what a model spent is what the gate counts (`spend_of`), probes included; the part spent on attempts that
    are not in the analysis is the total less the analysed attempts', so the overhead sits beside what scored runs
    cost."""
    store, keys = busy_store(tmp_path)
    usage = usage_of(store.q, PRICING, left_out={keys[1]})
    m1 = model_of(usage)
    gate = spend_of(store.q, PRICING)
    assert m1["usd"]["total"] == pytest.approx(gate.per_model["m1"])
    assert m1["usd"]["analysed"] == pytest.approx(9.25)
    assert m1["usd"]["not_analysed"] == pytest.approx(gate.per_model["m1"] - 9.25)
    assert [c["class"] for c in m1["classes"]] == [c for c in CLASSES if any(x["class"] == c for x in m1["classes"])]


def test_c39_a_class_with_no_attempt_is_not_listed_and_a_model_with_no_attempt_still_is(tmp_path):
    """C39: 0 is never mistaken for not reported: every model of the plan has an entry, with zero attempts and zero
    dollars, and a class nobody was charged in is left out of it."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], finished(**TOKENS))
    other = ModelSpec(
        key="m2", backend="stub", api_id="m2", status="frozen", price=Price(1.0, 2.0), context_tokens=1000
    )
    usage = usage_of(store.q, Pricing({"m1": SPEC, "m2": other}, LIMITS))
    assert [m["model"] for m in usage["models"]] == ["m1", "m2"]
    m2 = model_of(usage, "m2")
    assert (m2["classes"], m2["probes_usd"]) == ([], 0.0)
    assert m2["usd"] == {"total": 0.0, "analysed": 0.0, "not_analysed": 0.0}
    assert [c["class"] for c in model_of(usage)["classes"]] == ["analysed"]


def test_c39_an_attempt_that_failed_at_start_with_measured_zero_is_a_measured_attempt_of_zero_tokens(tmp_path):
    """C39 (C05): an attempt whose call log held no request, recorded with zero tokens, no turn and the basis measured,
    is counted, shown as 0 tokens and 0 dollars measured: not unknown, not dropped, not charged a bound."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], infra(**{**zero_fields(), "spend_basis": MEASURED}))
    m1 = model_of(usage_of(store.q, PRICING))
    cell = klass(m1, "infra_fail")
    assert (cell["attempts"], cell["input_tokens"], cell["output_tokens"], cell["usage_incomplete_attempts"]) == (
        1, 0, 0, 0,
    )  # fmt: skip
    assert cell["usd"] == {"measured": 0.0, "measured_plus_inflight": 0.0, "bound": 0.0, "charges": 0.0, "total": 0.0}
    assert cell["bounded_attempts"] == 0
    assert m1["usd"]["total"] == 0.0
    assert store.q("SELECT spend_basis, calls_sent FROM attempts")[0]["spend_basis"] == MEASURED


def zero_fields():
    return {
        "kind": "endpoint",
        "detail": "the fault could not be induced",
        "model_turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "turn_usage": (),
    }


def test_c39_the_basis_shown_is_the_one_the_dollars_were_charged_on_not_the_one_the_row_records(tmp_path):
    """C39: a row that records `measured` but whose model has a tiered price and no per-turn usage is charged its
    bound by the gate; the figure is in the bound column, never the measured one."""
    tiers = TieredPrice((PriceTier(200_000, Price(1.25, 10.0)), PriceTier(None, Price(2.5, 15.0))))
    tiered = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen", price=tiers, context_tokens=100_000)
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], infra("endpoint", spend_basis=MEASURED, **TOKENS))
    [row] = store.q("SELECT spend_basis, turn_usage FROM attempts")
    assert (row["spend_basis"], row["turn_usage"]) == (MEASURED, None)
    cell = klass(model_of(usage_of(store.q, Pricing({"m1": tiered}, LIMITS))), "infra_fail")
    assert cell["usd"]["measured"] == 0.0
    assert cell["usd"]["bound"] > 0
    assert cell["bounded_attempts"] == 1


def test_c39_an_interrupted_attempt_is_shown_on_the_basis_its_log_gave_and_a_measured_one_costs_its_replies(tmp_path):
    """C39: a kill between two requests leaves nothing in flight: the attempt is measured, its dollars the replies'."""
    store, _, manifest = planned_store(tmp_path)
    begun = logged(store, keys_of(manifest)[0], "sent", FIRST)
    recover(store, LATER, 3)
    assert store.q("SELECT spend_basis FROM attempts WHERE id=?", (begun.id,))[0]["spend_basis"] == MEASURED
    cell = klass(model_of(usage_of(store.q, PRICING)), "interrupted")
    assert cell["usd"]["measured"] == pytest.approx(FIRST_USD)
    assert (cell["usd"]["bound"], cell["bounded_attempts"]) == (0.0, 0)
    assert INFLIGHT != MEASURED


def test_c39_what_a_kill_lost_is_its_own_class_with_the_attempts_the_journal_counted_and_their_charges(tmp_path):
    """C39: attempts and charges the spend journal counted back (O62) are in the model's spend, as the gate counts
    them, in a class of their own."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], finished(**TOKENS))
    lost = {
        "id": 99,
        "run_key": keys_of(manifest)[1],
        "model": "m1",
        "started_utc": NOW,
        "state": "running",
        "reserved_capped": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_creation_tokens": None,
        "cache_read_tokens": None,
        "turn_usage": None,
        "usage_complete": None,
        "replies_checked": None,
        "topups": 1,
        "topup_usd": 0.5,
    }
    charges = [{"kind": "topup", "model": "m1", "usd": 0.2}, {"kind": "probe", "model": "m1", "usd": 0.1}]
    ledger = {
        "campaign": "c" * 64,
        "journal": "j2.jsonl",
        "counted": {"j1.jsonl": {"attempts": [lost], "charges": charges}},
    }
    store.put_meta(LOST_KEY, json.dumps(ledger), NOW)
    m1 = model_of(usage_of(store.q, PRICING))
    cell = klass(m1, "lost_in_kill")
    assert cell["attempts"] == 1
    assert cell["usd"]["bound"] == pytest.approx(BOUND_USD)  # the journal never saw it end: its bound
    assert cell["usd"]["charges"] == pytest.approx(0.5 + 0.2 + 0.1)
    assert m1["usd"]["total"] == pytest.approx(spend_of(store.q, PRICING).per_model["m1"])


def test_c39_a_model_without_a_price_has_tokens_and_no_dollars_and_nothing_raises(tmp_path):
    """C39: an unpriced model's attempts are counted with their tokens; every dollar of them is `None`, never 0."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], finished(**TOKENS))
    unpriced = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen")
    m1 = model_of(usage_of(store.q, Pricing({"m1": unpriced}, LIMITS)))
    cell = klass(m1, "analysed")
    assert (cell["attempts"], cell["input_tokens"], cell["unpriced_attempts"]) == (1, 1_000_000, 1)
    assert cell["usd"] is None
    assert m1["usd"] is None


def test_c39_a_free_model_costs_a_known_zero(tmp_path):
    """C39: a free model's dollars are 0, measured: known, not a bound and not unknown."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], finished(**TOKENS))
    free = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen", free=True)
    m1 = model_of(usage_of(store.q, Pricing({"m1": free}, LIMITS)))
    assert klass(m1, "analysed")["usd"]["measured"] == 0.0
    assert m1["usd"]["total"] == 0.0


def test_c39_prices_that_are_not_the_campaigns_give_tokens_and_no_dollars_and_say_why(tmp_path):
    """C39: with no pricing the campaign can be shown to have run under, every dollar is unknown and the reason says
    it, while the tokens are still the store's."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], finished(**TOKENS))
    usage = usage_of(store.q, None, why="the study's models.toml entries are not the ones the campaign ran under")
    assert usage["dollars"] == {
        "known": False,
        "reason": "the study's models.toml entries are not the ones the campaign ran under",
    }
    m1 = model_of(usage)
    assert klass(m1, "analysed")["input_tokens"] == 1_000_000
    assert (klass(m1, "analysed")["usd"], m1["usd"]) == (None, None)


def test_c39_a_bound_that_cannot_be_computed_is_unknown_dollars_with_the_reason_and_never_an_error(tmp_path):
    """C39: a priced model with no context window cannot be bounded: an analysis of such a campaign still reports,
    its dollars unknown, saying what is missing."""
    store, _, manifest = planned_store(tmp_path)
    store.begin_attempt(keys_of(manifest)[0], NOW, 1, "")
    recover(store, LATER, 3)  # interrupted with no log: needs its bound
    windowless = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen", price=Price(3.0, 15.0))
    usage = usage_of(store.q, Pricing({"m1": windowless}, LIMITS))
    assert usage["dollars"]["known"] is False
    assert "context_tokens" in usage["dollars"]["reason"]
    assert klass(model_of(usage), "interrupted")["usd"] is None


def test_c39_a_store_from_before_the_later_columns_is_read_as_it_was(tmp_path):
    """C39: a snapshot of a store made before a column existed is read, never migrated: the missing top-up and call
    log columns read as none."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], finished(**TOKENS))
    try:
        for column in ("topup_usd", "topups", "calls_sent", "calls_usage", "spend_basis"):
            store.db.execute(f"ALTER TABLE attempts DROP COLUMN {column}")
    except sqlite3.OperationalError:
        pytest.skip("this SQLite cannot drop a column")
    m1 = model_of(usage_of(store.q, PRICING))
    assert klass(m1, "analysed")["usd"]["total"] == pytest.approx(4.5)
