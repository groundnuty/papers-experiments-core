"""Budget at lease time, at the theoretical maximum: grants held until settled, never passed with every grant spent."""

import random

import pytest
from elastic_support import (
    SITE,
    archive,
    assert_invariants,
    completion,
    finish,
    join,
    leased,
    ledger_path,
    make,
    plan,
)

from expcore.campaign.attempt_bounds import request_bound_usd
from expcore.campaign.elastic import Lease, Ledger, LedgerError, Return, SiteReading, Wait, lease_grant_usd
from expcore.campaign.shard_budget import reservation_usd
from expcore.config.models import Limits, ModelSpec
from expcore.config.prices import Price

LIMITS = Limits(max_tool_rounds=4, time_cap_s=30.0, token_budget=20_000, output_cap_chars=500, max_output_tokens=256)


DEAR = Price(1.0, 2.0)


def model(key: str, price: Price | None = DEAR, *, free: bool = False) -> ModelSpec:
    """An invented stub model with a context window: priced, free, or with no price at all."""
    return ModelSpec(key=key, backend="stub", api_id=key, status="frozen", price=price, free=free, context_tokens=8192)


def test_c42_the_budget_is_never_passed_when_an_expired_run_is_leased_again_and_both_spend_their_grant(tmp_path):
    """C42: a run whose lease expired is leased again while the first holder may still be spending: both grants are
    held together, so with both spent to the bound the total is still within max_usd, and the next lease waits."""
    ledger, clock = make(tmp_path, runs=plan(tasks=3, conditions=("api-none",)), max_usd=3.0, grants_usd={"m1": 1.0})
    for label in ("fed-1", "fed-2", "fed-3", "fed-4"):
        join(ledger, clock, label)
    first, other = leased(ledger, "fed-1"), leased(ledger, "fed-2")
    ledger.observe_site(SITE, SiteReading(heartbeats={other.lease_id: 1}))
    clock.advance(200)
    ledger.observe_site(SITE, SiteReading(heartbeats={other.lease_id: 2}))
    clock.advance(101)
    ledger.observe_site(SITE, SiteReading(heartbeats={other.lease_id: 3}))
    assert ledger.lease(first.lease_id).state == "expired"
    again = leased(ledger, "fed-3")
    assert again.run_key == first.run_key
    assert_invariants(ledger)
    assert "budget" in ledger.issue("fed-4").reason
    for lease, verdict in ((first, "superseded"), (other, "counted"), (again, "counted")):
        assert finish(ledger, lease, spend_usd=1.0) == verdict
        assert_invariants(ledger)
    assert ledger.committed_micro()[0] == 3_000_000
    waiting = ledger.issue("fed-4")
    assert isinstance(waiting, Wait)
    assert "budget" in waiting.reason


def test_c42_a_models_ceiling_holds_its_own_leases(tmp_path):
    """C42: a model's ceiling is checked like the total: its leases wait while the other model's go out."""
    runs = plan(models=("m1", "m2"), tasks=2, conditions=("api-none",))
    ledger, clock = make(
        tmp_path, runs=runs, max_usd=100.0, per_model_usd={"m1": 1.5}, grants_usd={"m1": 1.0, "m2": 1.0}
    )
    for label in ("fed-1", "fed-2", "fed-3"):
        join(ledger, clock, label)
    models = [leased(ledger, label).model for label in ("fed-1", "fed-2", "fed-3")]
    assert sorted(models) == ["m1", "m2", "m2"]
    assert_invariants(ledger)


def test_c42_measured_spend_replaces_the_grant_only_once_the_archive_verifies(tmp_path):
    """C42: a completion read but not yet verified keeps its grant held; the verified archive returns the rest."""
    ledger, clock = make(tmp_path, max_usd=10.0, grants_usd={"m1": 4.0})
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    ledger.observe_site(SITE, SiteReading(completions=(completion(lease, spend_usd=0.2),)))
    assert ledger.committed_micro() == (4_000_000, {"m1": 4_000_000})
    assert ledger.verify_archive(lease.lease_id, archive(lease.lease_id)) == "counted"
    assert ledger.committed_micro() == (200_000, {"m1": 200_000})


def test_c42_the_grant_is_one_runs_reservation_plus_both_probes_and_the_top_up_allowance():
    """C42: what a lease is granted is the shard budget's reservation of one run, the bound of the two admission
    probes every store start sends, and the declared top-up allowance; a free model is granted nothing, and a model
    with no price is refused."""
    spec = model("m1")
    expected = reservation_usd(spec, LIMITS) + 2 * request_bound_usd(spec, LIMITS) + 0.5
    assert lease_grant_usd(spec, LIMITS, topup_allowance_usd=0.5) == pytest.approx(expected, abs=3e-6)
    assert lease_grant_usd(spec, LIMITS, topup_allowance_usd=0.5) >= expected
    assert lease_grant_usd(model("m2", price=None, free=True), LIMITS, topup_allowance_usd=0.5) == 0.0
    with pytest.raises(ValueError, match="m3"):
        lease_grant_usd(model("m3", price=None), LIMITS)


def test_c42_a_ledger_whose_grant_cannot_fit_its_budget_is_refused(tmp_path):
    """C42: a budget without a grant for a planned model, or a grant above the total or the model's ceiling, could
    never lease that model's runs and is refused at creation."""
    with pytest.raises(ValueError, match="m1"):
        make(tmp_path / "a", max_usd=10.0)
    with pytest.raises(ValueError, match="max_usd"):
        make(tmp_path / "b", max_usd=1.0, grants_usd={"m1": 2.0})
    with pytest.raises(ValueError, match="per_model_usd"):
        make(tmp_path / "c", max_usd=10.0, per_model_usd={"m1": 1.0}, grants_usd={"m1": 2.0})


def test_c42_a_random_walk_of_leases_expiries_completions_losses_proofs_and_restarts_keeps_every_invariant(tmp_path):
    """C42: a seeded walk over every operation, every completion spending its whole grant: no run is leased twice at
    once, no cap, ceiling or budget is passed, no run is counted done twice, and none is lost across expiries and
    restarts. Once the federations stop failing, every run ends or waits only for the budget, and no slot is held."""
    rng = random.Random(462)
    runs = plan(models=("m1", "m2"), tasks=8, sets={"api-none": 1})
    grants = {"m1": 1.0, "m2": 0.5}
    ledger, clock = make(
        tmp_path,
        runs=runs,
        caps={"m1": 2, "m2": 2},
        max_usd=24.0,
        per_model_usd={"m1": 15.5},
        grants_usd=grants,
        max_attempts=50,
    )
    labels = [f"fed-{n}" for n in range(1, 5)]
    for label in labels:
        join(ledger, clock, label)
    counters: dict[int, int] = {}
    for _ in range(400):
        ledger = _step(rng, ledger, clock, labels, counters, tmp_path)
        assert_invariants(ledger)
    for _ in range(100):  # the federations stop failing: every lease completes
        _settle(ledger, clock, labels)
        assert_invariants(ledger)
    states = {run.state for run in ledger.runs()}
    assert states <= {"done", "infra_exhausted", "pending"}
    assert "done" in states
    assert not any(lease.slot for lease in ledger.leases())
    if "pending" in states:
        waiting = ledger.issue(labels[0])
        assert isinstance(waiting, Wait), waiting
        assert "budget" in waiting.reason or "ceiling" in waiting.reason, waiting


def _live(ledger, label=None):
    return [
        lease
        for lease in ledger.leases()
        if lease.state in ("issued", "sent") and (label is None or lease.federation == label)
    ]


def _spend(rng, lease):
    """What a lease spent: its whole grant a third of the time, else a part of it."""
    return (
        lease.grant_micro / 1e6 if rng.random() < 1 / 3 else round(rng.uniform(0.1, 1.0) * lease.grant_micro / 1e6, 6)
    )


def _step(rng, ledger, clock, labels, counters, tmp_path):
    """One random operation; return the ledger, reopened when the step was a restart."""
    actions = ["issue", "issue", "issue", "beat", "time", "complete", "verify", "prove", "sweep", "restart", "cancel"]
    action = rng.choice(actions)
    if action == "cancel":
        issued = [lease for lease in ledger.leases() if lease.state == "issued"]
        if issued:
            ledger.cancel(rng.choice(issued).lease_id)
    elif action == "issue":
        got = ledger.issue(rng.choice(labels))
        if isinstance(got, Lease) and rng.random() < 0.7:
            ledger.mark_sent(got.lease_id)
    elif action == "beat":
        beats = {}
        for lease in _live(ledger):
            if rng.random() < 0.8:
                counters[lease.lease_id] = counters.get(lease.lease_id, 0) + 1
                beats[lease.lease_id] = counters[lease.lease_id]
        ledger.observe_site(SITE, SiteReading(heartbeats=beats))
    elif action == "time":
        clock.advance(rng.uniform(0, 400))
    elif action == "complete":
        open_leases = [lease for lease in ledger.leases() if lease.state in ("issued", "sent", "expired")]
        picked = [lease for lease in open_leases if rng.random() < 0.4]
        completions = tuple(
            completion(lease.lease_id, spend_usd=_spend(rng, lease), state=rng.choice(["done", "infra_fail"]))
            for lease in picked
            if rng.random() < 0.8
        )
        returns = tuple(
            Return(lease.lease_id, spend_usd=0.0)
            for lease in picked
            if lease.state in ("issued", "sent") and all(c.lease_id != lease.lease_id for c in completions)
        )
        ledger.observe_site(SITE, SiteReading(completions=completions, returns=returns))
    elif action == "verify":
        for lease in ledger.leases():
            if lease.state == "reported" and rng.random() < 0.6:
                ledger.verify_archive(lease.lease_id, archive(lease.lease_id) if rng.random() < 0.85 else None)
    elif action == "prove":
        label = rng.choice(labels)
        if any(lease.slot and lease.federation == label for lease in ledger.leases()):
            ledger.prove_ended(label, "cancelled")
            labels.remove(label)
            fresh = f"fed-{len(ledger.federations()) + 1}"
            join(ledger, clock, fresh)
            labels.append(fresh)
    elif action == "sweep":
        ledger.sweep()
    else:
        ledger.close()
        ledger = Ledger.open(ledger_path(tmp_path), clock=clock)
    return ledger


def _settle(ledger, clock, labels):
    """Complete every live lease, verify every archive, prove every held-back holder ended, lease out again."""
    for lease in ledger.leases():
        if lease.slot and lease.state == "expired":
            ledger.prove_ended(lease.federation, "cancelled")
            labels.remove(lease.federation)
            fresh = f"fed-{len(ledger.federations()) + 1}"
            join(ledger, clock, fresh)
            labels.append(fresh)
    completions = tuple(completion(lease.lease_id, spend_usd=lease.grant_micro / 1e6) for lease in _live(ledger))
    ledger.observe_site(SITE, SiteReading(completions=completions))
    for lease in ledger.leases():
        if lease.state == "reported":
            ledger.verify_archive(lease.lease_id, archive(lease.lease_id))
    for label in labels:
        ledger.issue(label)


def test_c42_a_lease_operation_on_an_unknown_lease_is_refused(tmp_path):
    """C42: nothing is applied for a lease the ledger never issued, or read at another site than its own."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    with pytest.raises(LedgerError, match="999"):
        ledger.observe_site(SITE, SiteReading(heartbeats={999: 1}))
    with pytest.raises(LedgerError, match="site-b"):
        ledger.observe_site("site-b", SiteReading(heartbeats={lease.lease_id: 1}))
