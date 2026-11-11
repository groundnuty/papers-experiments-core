"""What an attempt of a tiered model cost: each request at its own tier, from the usage its turns recorded (C24)."""

import dataclasses
import json
import sqlite3

import pytest
from campaign_support import finished, planned_store, run_one
from loop_support import ScriptedBackend
from record_support import make_config, no_secrets_in_the_environment, record_run
from scheduler_support import build, settings_for

from expcore.campaign.attempt_bounds import attempt_costs, bound_usd
from expcore.campaign.executor import result_from_record
from expcore.campaign.gate import Budget
from expcore.campaign.healthgate import Health
from expcore.campaign.markers import MARKER_NAME, read_marker, write_marker
from expcore.campaign.snapshot import StoreSnapshot
from expcore.config.models import ModelSpec, ModelTable
from expcore.config.prices import Price, PriceTier, TieredPrice
from expcore.models.adapters import ModelTurn, ToolCall
from expcore.records.record import load_record

pytestmark = pytest.mark.usefixtures("no_secrets_in_the_environment")
__all__ = ["no_secrets_in_the_environment"]

LOW, HIGH = Price(1.25, 10.0, None, 0.125), Price(2.5, 15.0, None, 0.25)
TIERED = TieredPrice((PriceTier(200_000, LOW), PriceTier(None, HIGH)))
FLAT = Price(3.0, 15.0, 3.75, 0.3)
CAP = 2_000_000
LIMITS = make_config(token_budget=CAP).limits
ENDED = "2026-01-01T00:10:00Z"
EXACT = LOW.usd(150_000, 2_000) + HIGH.usd(250_000, 3_000, 0, 100_000)


def spec(price) -> ModelSpec:
    return ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen", price=price, context_tokens=1_000_000)


def two_requests():
    """The record of a run of two requests either side of the tier boundary: a tool call on a 150,000-token prompt,
    then an answer on a 250,000-token prompt of which 100,000 are read from the cache."""
    script = [
        ModelTurn("", [ToolCall("first-call", "echo", {"x": "1"})], 150_000, 2_000, "tool_calls", 0.1),
        ModelTurn("done", [], 250_000, 3_000, "stop", 0.1, None, 150_000, 0, 100_000),
    ]
    return record_run([], {"echo": lambda args: "echoed"}, backend=ScriptedBackend(script), token_budget=CAP)


def charged(tmp_path, result, price):
    """The cost the store's measure gives an attempt with this result, of a model with this price."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, manifest.rows[0].key.text, result)
    [cost] = attempt_costs(store, {"m1": spec(price)}, LIMITS)
    return cost


def test_c24_an_attempt_of_a_tiered_model_costs_each_request_at_its_own_tier(tmp_path):
    """C24: the store keeps each turn's usage, and the cost measure prices every request at the tier its prompt fell
    in; the attempt's totals at either tier would be wrong."""
    result = result_from_record(two_requests(), ended_utc=ENDED)
    assert result.turn_usage == ((150_000, 2_000, None, None), (250_000, 3_000, 0, 100_000))
    cost = charged(tmp_path, result, TIERED)
    assert (cost.usd, cost.bounded) == (pytest.approx(EXACT), False)
    assert cost.usd != pytest.approx(HIGH.usd(400_000, 5_000, 0, 100_000))
    assert cost.usd != pytest.approx(LOW.usd(400_000, 5_000, 0, 100_000))


def test_c24_the_cost_is_derived_again_from_the_record_read_back_from_disk(tmp_path):
    """C24: what the store charged is what the record's own per-turn usage gives, so the records the replay check
    replays re-derive every attempt's cost exactly."""
    again = load_record(two_requests().write(tmp_path / "runs"))
    cost = charged(tmp_path, result_from_record(again, ended_utc=ENDED), TIERED)
    from_record = sum(
        TIERED.request_usd(
            turn["usage"]["input_tokens"],
            turn["usage"]["output_tokens"],
            turn["usage"]["cache_creation_input_tokens"] or 0,
            turn["usage"]["cache_read_input_tokens"] or 0,
        )
        for turn in again.transcript
    )
    assert cost.usd == from_record
    assert cost.usd == pytest.approx(EXACT)


def test_c24_a_model_without_tiers_is_charged_from_its_totals_exactly_as_before(tmp_path):
    """C24: a flat price keeps the measure it had: the attempt's totals at the one price, to the last bit."""
    cost = charged(tmp_path, result_from_record(two_requests(), ended_utc=ENDED), FLAT)
    assert cost.usd == FLAT.usd(400_000, 5_000, 0, 100_000)


def test_c24_a_tiered_attempt_without_per_turn_usage_is_charged_its_bound_never_a_guessed_tier(tmp_path):
    """C24 and C05: totals alone cannot say which tier each token fell in (a database older than the per-turn usage),
    so the attempt is charged its bound, as one whose usage is unknown."""
    cost = charged(tmp_path, finished(input_tokens=400_000, output_tokens=5_000), TIERED)
    assert (cost.usd, cost.bounded) == (bound_usd(spec(TIERED), LIMITS), True)


def test_c24_a_tiered_attempt_with_a_turn_whose_usage_is_unknown_is_charged_its_bound(tmp_path):
    """C24 and R10: a turn that reported no count leaves the request's tier and cost unknown, never zero."""
    result = result_from_record(two_requests(), ended_utc=ENDED)
    result = dataclasses.replace(result, turn_usage=((150_000, 2_000, None, None), (None, 3_000, 0, 0)))
    cost = charged(tmp_path, result, TIERED)
    assert (cost.usd, cost.bounded) == (bound_usd(spec(TIERED), LIMITS), True)


def test_c24_a_snapshot_of_a_store_older_than_the_per_turn_usage_still_gives_flat_costs(tmp_path):
    """C24 and C21: a read-only snapshot of a store made before `turn_usage` existed cannot be migrated, so the cost
    measure reads it without that column: a flat price is charged its totals, and a tiered one its bound."""
    store, schema, manifest = planned_store(tmp_path)
    run_one(store, manifest.rows[0].key.text, finished(input_tokens=400_000, output_tokens=5_000))
    store.close()
    old = sqlite3.connect(tmp_path / "campaign.db")
    old.execute("ALTER TABLE attempts DROP COLUMN turn_usage")
    old.commit()
    old.execute("PRAGMA journal_mode = DELETE")  # a snapshot's bytes are a plain database, as `snapshot` writes one
    snapshot = StoreSnapshot(old.serialize(), schema)
    old.close()
    assert "turn_usage" not in [row["name"] for row in snapshot.q("PRAGMA table_info(attempts)")]
    [flat] = attempt_costs(snapshot, {"m1": spec(FLAT)}, LIMITS)
    assert (flat.usd, flat.bounded) == (FLAT.usd(400_000, 5_000), False)
    [tiered] = attempt_costs(snapshot, {"m1": spec(TIERED)}, LIMITS)
    assert (tiered.usd, tiered.bounded) == (bound_usd(spec(TIERED), LIMITS), True)


def test_c24_the_marker_keeps_the_per_turn_usage_and_an_older_marker_without_it_still_reads(tmp_path):
    """C24 and C04: a result recovered from its marker keeps each turn's usage; a marker written before the field
    existed reads with none, so its tiered attempt is charged its bound."""
    result = result_from_record(two_requests(), ended_utc=ENDED)
    write_marker(tmp_path, "k", 1, result)
    assert read_marker(tmp_path, "k", 1) == result
    path = tmp_path / MARKER_NAME
    body = json.loads(path.read_text(encoding="utf-8"))
    del body["result"]["turn_usage"]
    path.write_text(json.dumps(body), encoding="utf-8")
    assert read_marker(tmp_path, "k", 1).turn_usage is None


class DownOnce:
    """A health check that fails once, then passes: the first run finds the outage with its pre-run check."""

    def __init__(self):
        self.checks = 0

    def __call__(self):
        self.checks += 1
        return Health(self.checks > 1, "fine" if self.checks > 1 else "platform unreachable")


def test_c24_a_run_that_found_an_outage_before_it_started_costs_nothing_whatever_its_models_price(tmp_path):
    """C24 and C11: the run that finds an outage with its pre-run check is the outage's one attempt and sends nothing.
    Its usage is known to be no request at all, so a tiered model's attempt costs 0, as a flat one's does, and is never
    charged the bound of an attempt whose usage is unknown."""
    budget = Budget(max_usd=1_000.0)
    settings = settings_for(recheck_s=0.01, health_timeout_s=5, budget=budget)
    table = ModelTable({"m1": spec(TIERED)})
    plan = {"tasks": ("task-a",), "seeds": (1,)}
    with build(tmp_path, settings=settings, health=DownOnce(), plan=plan, models=table) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        store = scheduler.store
        [outage] = store.q("SELECT id, turn_usage, reserved_capped FROM attempts WHERE state='infra_fail'")
        assert (outage["turn_usage"], outage["reserved_capped"]) == ("[]", 0)
        costs = attempt_costs(store, {"m1": spec(TIERED)}, scheduler.agent_config.limits)
        assert [(cost.usd, cost.bounded) for cost in costs if cost.attempt_id == outage["id"]] == [(0.0, False)]
