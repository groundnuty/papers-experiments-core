"""C66: a lease whose grant is raised is not held by a gate that counts its spend and its run twice, or by one that
weighs a run pending again after a costly attempt.

Workers that predate C64 build every store's gate as `BudgetGate` does by default (`single_run=False`): while the run
of a lease is in flight it is reserved, and the next run is reserved again. On every core a run pending again after an
attempt that spent is weighed as what was spent plus its reservation (`single_run=True`, a lease's store today). A
grant of one reservation and two admission probes holds a lease in either case once it has spent a little. The grant is
the one lever left to a controller on workers it cannot change: it is raised on the ledger, and the worker reads it.
"""

import pytest
from campaign_support import NOW, finished, planned_store, run_one

from expcore.campaign.elastic.config import lease_grant_usd
from expcore.campaign.gate import Budget, BudgetGate
from expcore.config.models import Limits, ModelSpec
from expcore.config.prices import Price

# The reservation of one run of the priced stub is 30 USD (its 1.9M-token cap at 15 USD per million, and the request
# past the cap, its 100,000-token window at 15 too), and the bound of one request is 1.5 USD: a plain grant is
# 30 + 2 x 1.5 = 33 USD.
LIMITS = Limits(
    max_tool_rounds=10, time_cap_s=60.0, token_budget=1_900_000, output_cap_chars=100, max_output_tokens=20_000
)
PRICED = ModelSpec(
    key="m1", backend="stub", api_id="m1", status="frozen", price=Price(3.0, 15.0), context_tokens=100_000
)
SPENT = {"input_tokens": 1_000_000, "output_tokens": 100_000}  # 4.5 USD


def assessed(directory, grant_usd):
    """Why a lease's store, which has spent 4.5 USD on a first attempt and has its run in flight again, is held under a
    gate that counts a run in flight and the one to start (the default) and a budget of the grant; None: not held."""
    store, _, manifest = planned_store(directory)
    try:
        run_one(store, manifest.rows[0].key.text, finished(**SPENT))
        gate = BudgetGate(store, {"m1": PRICED}, Budget(max_usd=grant_usd), LIMITS)
        return gate.assess(["m1"], NOW).reason("m1")
    finally:
        store.close()


def test_c66_a_gate_that_counts_the_run_in_flight_twice_holds_at_the_plain_grant_and_not_at_the_raised_one(tmp_path):
    """C66: spent 4.5 + in flight 30 + this run 30 = 64.5 USD is over the plain grant of 33 and is not over the grant
    raised by an allowance of 31.5 (64.5): no hold, so the lease goes on."""
    assert lease_grant_usd(PRICED, LIMITS) == pytest.approx(33.0)
    raised = lease_grant_usd(PRICED, LIMITS, topup_allowance_usd=31.5)
    assert raised == pytest.approx(64.5)
    assert "4.50 + in flight 30.00 + this run 30.00" in assessed(tmp_path / "plain", lease_grant_usd(PRICED, LIMITS))
    assert assessed(tmp_path / "raised", raised) is None


def test_c66_the_raised_grant_admits_a_run_exactly_where_two_reservations_and_the_spend_fit_in_it(tmp_path):
    """C66: the allowance that makes the grant 2 x 30 + 4.5 = 64.5 admits, and a cent less holds."""
    exact = lease_grant_usd(PRICED, LIMITS, topup_allowance_usd=31.5)
    short = lease_grant_usd(PRICED, LIMITS, topup_allowance_usd=31.49)
    assert assessed(tmp_path / "exact", exact) is None
    assert "over the ceiling of 64.49 USD" in assessed(tmp_path / "short", short)


def test_c66_a_run_pending_again_after_a_costly_attempt_is_held_at_the_plain_grant_on_every_core_and_not_at_the_raised(
    tmp_path,
):
    """C66: in a lease's store today (`single_run=True`) a run pending again is weighed as what was spent (4.50) plus
    its reservation (30.00) = 34.50 USD: held at the plain grant of 33, admitted at the grant raised to 64.50. C64 left
    this case as it was."""
    store, _, manifest = planned_store(tmp_path / "pending")
    try:
        run_one(store, manifest.rows[0].key.text, finished(**SPENT))

        def held(grant_usd):
            gate = BudgetGate(store, {"m1": PRICED}, Budget(max_usd=grant_usd), LIMITS, single_run=True)
            return gate.assess([], NOW).reason("m1")

        assert "4.50 + in flight 0.00 + this run 30.00" in held(lease_grant_usd(PRICED, LIMITS))
        assert held(lease_grant_usd(PRICED, LIMITS, topup_allowance_usd=31.5)) is None
    finally:
        store.close()
