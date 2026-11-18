"""A ceiling raised mid-campaign: recorded, never lowered, never below what is committed, never in the fingerprint."""

import pytest
from elastic_support import (
    ENVIRONMENT,
    FINGERPRINT,
    SITE,
    assert_invariants,
    finish,
    join,
    leased,
    ledger_path,
    make,
    plan,
)

from expcore.campaign.elastic import Ceiling, CeilingRefused, Ceilings, JoinRefused, Ledger, Wait

BUDGET = {"runs": plan(tasks=4, conditions=("api-none",)), "max_usd": 2.0, "grants_usd": {"m1": 1.0}}
MODELS = {
    "runs": plan(models=("m1", "m2"), tasks=2, conditions=("api-none",)),
    "max_usd": 100.0,
    "per_model_usd": {"m1": 1.5},
    "grants_usd": {"m1": 1.0, "m2": 1.0},
}


def _config_bytes(ledger):
    return ledger.db.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0]


def test_c42_raising_the_ceiling_lets_a_blocked_priced_dispatch_go_out_and_records_time_reason_and_actor(tmp_path):
    """C42: two grants fill a 2 USD ceiling and a third federation waits; the raise is recorded with its time, its
    reason and its actor in its own table, and the third federation is leased at once."""
    ledger, clock = make(tmp_path, **BUDGET)
    for label in ("fed-1", "fed-2", "fed-3"):
        join(ledger, clock, label)
    leased(ledger, "fed-1"), leased(ledger, "fed-2")
    blocked = ledger.issue("fed-3")
    assert isinstance(blocked, Wait)
    assert "budget" in blocked.reason
    clock.advance(30)
    raised = ledger.raise_ceiling(3_000_000, "the grant covers a second wave", "operator-1")
    assert raised == Ceiling(1, clock(), None, 2_000_000, 3_000_000, "the grant covers a second wave", "operator-1")
    leased(ledger, "fed-3")
    assert ledger.ceilings() == Ceilings(max_micro=3_000_000, per_model_micro={})
    assert ledger.ceiling_history() == [raised]
    assert ledger.config.max_usd == 2.0  # the pinned configuration is read-only
    assert_invariants(ledger)


def test_c42_a_raise_survives_a_restart_and_a_later_raise_builds_on_it(tmp_path):
    """C42: the ceiling in force is read from the file, so a reopened ledger and a second raise start from the first."""
    ledger, clock = make(tmp_path, **BUDGET)
    ledger.raise_ceiling(3_000_000, "first", "op")
    ledger.close()
    ledger = Ledger.open(ledger_path(tmp_path), clock=clock)
    assert ledger.ceilings().max_micro == 3_000_000
    second = ledger.raise_ceiling(4_500_000, "second", "op")
    assert (second.ceiling_id, second.old_micro, second.new_micro) == (2, 3_000_000, 4_500_000)
    assert [c.new_micro for c in ledger.ceiling_history()] == [3_000_000, 4_500_000]
    event = [e for e in ledger.events() if e["kind"] == "raise_ceiling"][-1]
    assert event["detail"] == {"ceiling_id": 2, "model": None, "new_micro": 4_500_000, "old_micro": 3_000_000}


def test_c42_the_ceiling_is_never_lowered_nor_set_below_what_is_committed(tmp_path):
    """C42: the same ceiling and a lower one are refused; so is a higher one that is still below what is committed
    (a verified completion spent 3 USD against a 2 USD ceiling); that committed amount itself is accepted. A refusal
    records nothing."""
    ledger, clock = make(tmp_path, **BUDGET)
    join(ledger, clock, "fed-1")
    for value in (2_000_000, 1_999_999, 1):
        with pytest.raises(CeilingRefused, match="lower"):
            ledger.raise_ceiling(value, "no", "op")
    assert ledger.ceiling_history() == []
    lease = leased(ledger, "fed-1")
    assert finish(ledger, lease, spend_usd=3.0) == "counted"
    assert ledger.committed_micro()[0] == 3_000_000
    with pytest.raises(CeilingRefused, match="committed"):
        ledger.raise_ceiling(2_500_000, "still short", "op")
    assert ledger.ceiling_history() == []
    assert not any(e["kind"] == "raise_ceiling" for e in ledger.events())
    ledger.raise_ceiling(3_000_000, "what was spent", "op")
    assert ledger.ceilings().max_micro == 3_000_000


@pytest.mark.parametrize("value", [0, -5, 2.5, True, "3000000", None])
def test_c42_a_ceiling_that_is_not_a_whole_positive_number_of_micro_dollars_and_a_blank_reason_or_actor_are_refused(
    tmp_path, value
):
    """C42: the new ceiling is a count of micro-dollars above zero, and the record names why and who."""
    ledger, _ = make(tmp_path, **BUDGET)
    with pytest.raises(ValueError, match="new_max_micro_usd"):
        ledger.raise_ceiling(value, "why", "op")
    for reason, actor in (("", "op"), ("why", " "), (None, "op")):
        with pytest.raises(ValueError, match=r"reason|actor"):
            ledger.raise_ceiling(3_000_000, reason, actor)
    assert ledger.ceiling_history() == []


def test_c42_a_ledger_with_no_budget_has_no_ceiling_to_raise(tmp_path):
    """C42: with no budget nothing is capped, so any ceiling would be a lowering."""
    ledger, _ = make(tmp_path)
    with pytest.raises(CeilingRefused, match="no budget"):
        ledger.raise_ceiling(3_000_000, "why", "op")
    with pytest.raises(CeilingRefused, match="no ceiling"):
        ledger.raise_model_ceiling("m1", 3_000_000, "why", "op")


def test_c42_a_models_ceiling_is_raised_on_its_own_and_only_where_it_has_one(tmp_path):
    """C42: model m1's 1.5 USD ceiling holds its second lease while the campaign's has room; raising it records the
    model and lets that lease out; a model with no ceiling of its own has nothing to raise, and the model's ceiling
    never goes down or below what the model has committed."""
    ledger, clock = make(tmp_path, **MODELS)
    for label in ("fed-1", "fed-2", "fed-3", "fed-4"):
        join(ledger, clock, label)
    assert sorted(leased(ledger, label).model for label in ("fed-1", "fed-2", "fed-3")) == ["m1", "m2", "m2"]
    held = ledger.issue("fed-4")
    assert isinstance(held, Wait)
    assert "ceiling" in held.reason
    raised = ledger.raise_model_ceiling("m1", 2_500_000, "m1 is the dear model", "agent-3")
    assert (raised.model, raised.old_micro, raised.new_micro) == ("m1", 1_500_000, 2_500_000)
    assert leased(ledger, "fed-4").model == "m1"
    assert ledger.ceilings() == Ceilings(max_micro=100_000_000, per_model_micro={"m1": 2_500_000})
    with pytest.raises(CeilingRefused, match="no ceiling"):
        ledger.raise_model_ceiling("m2", 5_000_000, "why", "op")
    for value in (2_500_000, 2_400_000):
        with pytest.raises(CeilingRefused, match="lower"):
            ledger.raise_model_ceiling("m1", value, "no", "op")
    assert [c.model for c in ledger.ceiling_history()] == ["m1"]
    assert_invariants(ledger)


def test_c42_a_models_ceiling_is_not_raised_below_what_the_model_has_committed(tmp_path):
    """C42: m1 spent 2 USD against a 1.5 USD ceiling; a raise to 1.8 is above the ceiling and below the committed."""
    ledger, clock = make(tmp_path, **MODELS)
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    assert lease.model == "m1"
    assert finish(ledger, lease, spend_usd=2.0) == "counted"
    with pytest.raises(CeilingRefused, match="committed"):
        ledger.raise_model_ceiling("m1", 1_800_000, "short", "op")
    ledger.raise_model_ceiling("m1", 2_000_000, "what it spent", "op")
    assert ledger.ceilings().per_model_micro["m1"] == 2_000_000


def test_c42_a_raise_never_changes_the_campaign_fingerprint_or_the_pinned_configuration(tmp_path):
    """C42: the ceiling is not agent-visible, so it is not in the fingerprint: the pinned configuration's bytes are
    those of creation after a raise and a reopen, a federation still joins with the original fingerprint and another
    is still refused, and a lease still carries it."""
    ledger, clock = make(tmp_path, **MODELS)
    pinned = _config_bytes(ledger)
    ledger.raise_ceiling(120_000_000, "more headroom", "op")
    ledger.raise_model_ceiling("m1", 9_000_000, "m1 headroom", "op")
    ledger.close()
    ledger = Ledger.open(ledger_path(tmp_path), clock=clock)
    assert _config_bytes(ledger) == pinned
    assert ledger.config.fingerprint == FINGERPRINT
    assert ledger.config.max_usd == 100.0
    assert dict(ledger.config.per_model_usd) == {"m1": 1.5}
    join(ledger, clock, "fed-1")
    assert leased(ledger, "fed-1").fingerprint == FINGERPRINT
    with pytest.raises(JoinRefused, match="fingerprint"):
        ledger.join(
            "fed-2",
            site=SITE,
            job_ids=("job",),
            job_end_at=clock() + 1,
            fingerprint="0" * 64,
            environment=ENVIRONMENT,
            kill_wait_s=0.0,
        )


@pytest.mark.parametrize("model", [None, "", 3])
def test_c42_a_model_ceiling_needs_a_model_and_never_raises_the_campaigns(tmp_path, model):
    """C42: no model (or a blank one) is refused; it is not the campaign's ceiling."""
    ledger, _ = make(tmp_path, **BUDGET)
    with pytest.raises(ValueError, match="model"):
        ledger.raise_model_ceiling(model, 3_000_000, "why", "op")
    assert ledger.ceilings().max_micro == 2_000_000
    assert ledger.ceiling_history() == []


def test_c42_a_ceiling_past_what_the_file_can_hold_is_refused(tmp_path):
    """C42: a micro-dollar count must fit a signed 64-bit integer, or it is a usage error and not an overflow."""
    ledger, _ = make(tmp_path, **BUDGET)
    with pytest.raises(ValueError, match="new_max_micro_usd"):
        ledger.raise_ceiling(2**63, "why", "op")
    ledger.raise_ceiling(2**63 - 1, "the most there is", "op")
