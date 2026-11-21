"""C66: `elastic set-grant` raises a model's lease grant on an existing ledger, before any lease of the model is issued,
for workers on a core that cannot be changed (they read the grant from the ledger, and the ledger's pinned
configuration, which an older core also reads, stays byte for byte as it was)."""

import json
import sqlite3

import pytest
from elastic_support import FINGERPRINT, assert_invariants, join, ledger_path, make, plan

from expcore.campaign.elastic import Ledger, LedgerConfig, LedgerReader, Wait
from expcore.campaign.elastic.__main__ import main
from expcore.campaign.elastic.controlinputs import ledger_view
from expcore.campaign.elastic.grants import set_in_file
from expcore.campaign.elastic.records import GrantRefused

MICRO = 1_000_000
RUNS = plan(models=("m1", "m2"), tasks=4, conditions=("api-none",))
WHY = ("a raise before the first lease", "agent-7")


def closed(tmp_path, runs=RUNS, **changes):
    """A ledger made and closed, so no controller holds it: what an operator raises a grant on, before the first
    lease. Its models are granted 100 and 50 USD."""
    settings = {"max_usd": 3600.0, "grants_usd": {"m1": 100.0, "m2": 50.0}, **changes}
    ledger, clock = make(tmp_path, runs=runs, **settings)
    ledger.close()
    return ledger_path(tmp_path), clock


def reopened(path, clock):
    ledger = Ledger.open(path, clock)
    return ledger


def raise_grants(path, clock, **dollars):
    """Raise grants as the command does, each in dollars as an operator types them."""
    return set_in_file(path, {model: round(usd * MICRO) for model, usd in dollars.items()}, *WHY, clock=clock)


def meta(path):
    db = sqlite3.connect(path)
    try:
        return dict(db.execute("SELECT key, value FROM meta"))
    finally:
        db.close()


def leases_by_model(ledger, clock, federations=4):
    """Join federations and lease every run; each model's lease grants in micro-dollars."""
    out: dict[str, set[int]] = {}
    for number in range(federations):
        label = f"fed-{number}"
        join(ledger, clock, label)
        lease = ledger.issue(label)
        assert not isinstance(lease, Wait), lease
        out.setdefault(lease.model, set()).add(lease.grant_micro)
    return out


def test_c66_set_grant_raises_the_named_models_grant_and_every_later_lease_of_it_carries_it(tmp_path):
    """C66: m1 is raised from 100 to 276.99 USD; its leases are issued at that grant (the figure a worker reads as
    its whole budget) and m2's, not named, at the 50 it had."""
    path, clock = closed(tmp_path)
    raise_grants(path, clock, m1=276.99)
    ledger = reopened(path, clock)
    try:
        assert leases_by_model(ledger, clock) == {"m1": {276_990_000}, "m2": {50_000_000}}
        assert_invariants(ledger)
    finally:
        ledger.close()


def test_c66_the_pinned_configuration_is_the_text_it_was_so_an_older_reader_sees_no_change(tmp_path):
    """C66: the raised grant is a meta row of its own and the event log's: the `config` row is byte for byte what it
    was, so a reader of an older core (its merge, a sibling controller, a worker) reads the grants it always read."""
    path, clock = closed(tmp_path)
    before = meta(path)
    raise_grants(path, clock, m1=276.99)
    after = meta(path)
    assert after["config"] == before["config"]
    assert set(after) - set(before) == {"grant_overrides_micro_usd"}
    assert json.loads(after["grant_overrides_micro_usd"]) == {"m1": 276_990_000}
    assert LedgerConfig.from_json(after["config"]).grants_usd == {"m1": 100.0, "m2": 50.0}


def test_c66_the_campaign_fingerprint_is_untouched_and_a_federation_still_joins_with_it(tmp_path):
    """C66: the grant is no input to the fingerprint the ledger pins, and a federation joins with the campaign's own."""
    path, clock = closed(tmp_path)
    raise_grants(path, clock, m1=276.99)
    ledger = reopened(path, clock)
    try:
        assert ledger.config.fingerprint == FINGERPRINT
        join(ledger, clock, "fed-1")  # refused with JoinRefused for any other fingerprint
    finally:
        ledger.close()


@pytest.mark.parametrize("usd", [100.0, 99.99, 1.0])
def test_c66_a_grant_is_only_raised_never_set_to_the_current_one_or_below(tmp_path, usd):
    """C66: a new grant that is not above the current one is refused and nothing is written."""
    path, clock = closed(tmp_path)
    before = meta(path)
    with pytest.raises(GrantRefused, match="not above"):
        raise_grants(path, clock, m1=usd)
    assert meta(path) == before


def test_c66_a_second_raise_is_judged_against_the_first(tmp_path):
    """C66: the grant in force is the latest raise, so a later raise must be above it."""
    path, clock = closed(tmp_path)
    raise_grants(path, clock, m1=150.0)
    with pytest.raises(GrantRefused, match="not above"):
        raise_grants(path, clock, m1=150.0)
    raise_grants(path, clock, m1=200.0)
    assert json.loads(meta(path)["grant_overrides_micro_usd"]) == {"m1": 200_000_000}


def test_c66_a_model_with_a_lease_ever_issued_is_refused_so_no_issued_lease_changes(tmp_path):
    """C66: once a lease of a model exists, in any state, its grant is pinned, so no lease's accounting changes."""
    path, clock = closed(tmp_path)
    ledger = reopened(path, clock)
    join(ledger, clock, "fed-1")
    taken = ledger.issue("fed-1")
    ledger.close()
    before = meta(path)
    with pytest.raises(GrantRefused, match=f"{taken.model}.*lease {taken.lease_id}"):
        raise_grants(path, clock, **{taken.model: 276.99})
    assert meta(path) == before


def test_c66_a_call_that_names_one_model_with_a_lease_writes_nothing_for_the_other(tmp_path):
    """C66: the models are raised together or not at all."""
    path, clock = closed(tmp_path)
    ledger = reopened(path, clock)
    join(ledger, clock, "fed-1")
    taken = ledger.issue("fed-1").model
    ledger.close()
    other = "m2" if taken == "m1" else "m1"
    before = meta(path)
    with pytest.raises(GrantRefused):
        raise_grants(path, clock, **{taken: 276.99, other: 276.99})
    assert meta(path) == before


def test_c66_a_raise_is_a_ledger_event_with_its_actor_model_old_and_new_grant_and_time(tmp_path):
    """C66: each model raised is one event of the ledger's log, and `status` shows the history."""
    path, clock = closed(tmp_path)
    clock.advance(42.0)
    raise_grants(path, clock, m1=276.99, m2=69.245)
    reader = LedgerReader(path)
    try:
        events = [e for e in reader.events() if e["kind"] == "set_grant"]
        raised = reader.status().grant_raises
    finally:
        reader.close()
    assert [(e["detail"]["model"], e["detail"]["old_micro"], e["detail"]["new_micro"]) for e in events] == [
        ("m1", 100_000_000, 276_990_000),
        ("m2", 50_000_000, 69_245_000),
    ]
    assert {e["detail"]["actor"] for e in events} == {"agent-7"}
    assert {e["at"] for e in events} == {clock()}
    assert [(r.model, r.old_micro, r.new_micro, r.actor, r.reason) for r in raised] == [
        ("m1", 100_000_000, 276_990_000, "agent-7", WHY[0]),
        ("m2", 50_000_000, 69_245_000, "agent-7", WHY[0]),
    ]


def test_c66_grants_at_a_cap_of_ten_each_that_fit_the_budget_are_accepted_and_all_twenty_leases_go_out(tmp_path):
    """C66: caps 10 and 10 at grants 276.99 and 69.245 need 10 x 276.99 + 10 x 69.245 = 3,462.35 USD, under the
    3,600 of `max_usd`: accepted, and the issue path (a grant against `max_usd` less what is committed, settled plus
    locked) lets every lease go out."""
    runs = plan(models=("m1", "m2"), tasks=10, conditions=("api-none",))
    path, clock = closed(tmp_path, runs=runs, caps={"m1": 10, "m2": 10})
    raise_grants(path, clock, m1=276.99, m2=69.245)
    ledger = reopened(path, clock)
    try:
        leases_by_model(ledger, clock, federations=20)
        assert ledger.committed_micro() == (3_462_350_000, {"m1": 2_769_900_000, "m2": 692_450_000})
        assert_invariants(ledger)
    finally:
        ledger.close()


def test_c66_grants_whose_caps_would_not_fit_the_budget_are_refused_with_the_arithmetic(tmp_path):
    """C66: caps 10 and 10 at grants 300 and 70 need 3,700 USD against 3,600: not every cap slot could hold a lease,
    so the raise is refused and says so."""
    path, clock = closed(tmp_path, caps={"m1": 10, "m2": 10})
    before = meta(path)
    with pytest.raises(GrantRefused) as refused:
        raise_grants(path, clock, m1=300.0, m2=70.0)
    assert "10 x 300" in str(refused.value)
    assert "3700" in str(refused.value)
    assert "3600" in str(refused.value)
    assert meta(path) == before


def test_c66_a_grant_above_the_campaigns_or_the_models_own_ceiling_is_refused(tmp_path):
    """C66: a lease no ceiling could hold would never go out."""
    path, clock = closed(tmp_path, max_usd=500.0, per_model_usd={"m1": 300.0})
    with pytest.raises(GrantRefused, match="ceiling"):
        raise_grants(path, clock, m2=500.01)
    with pytest.raises(GrantRefused, match="ceiling"):
        raise_grants(path, clock, m1=300.01)
    raise_grants(path, clock, m1=300.0)  # exactly the ceiling fits


def test_c66_a_raise_is_refused_while_a_controller_holds_the_ledger_and_works_once_it_is_gone(tmp_path):
    """C66: the command takes the dispatcher's own lock for its one transaction, so it never runs beside a controller
    (whose core may not read a raised grant) and no controller starts under it; the refusal names the controller."""
    ledger, clock = make(tmp_path, runs=RUNS, max_usd=3600.0, grants_usd={"m1": 100.0, "m2": 50.0})
    before = meta(ledger_path(tmp_path))
    with pytest.raises(GrantRefused, match="controller holds"):
        raise_grants(ledger_path(tmp_path), clock, m1=276.99)
    assert meta(ledger_path(tmp_path)) == before
    ledger.close()
    raise_grants(ledger_path(tmp_path), clock, m1=276.99)


def test_c66_a_ledger_with_no_budget_a_free_model_or_an_unplanned_model_has_no_grant_to_raise(tmp_path):
    """C66: no budget: a lease is granted nothing; a model granted 0 is free; a model with no run is not the
    ledger's."""
    path, clock = closed(tmp_path / "none", max_usd=None, grants_usd={})
    with pytest.raises(GrantRefused, match="no budget"):
        raise_grants(path, clock, m1=5.0)
    path, clock = closed(tmp_path / "free", grants_usd={"m1": 100.0, "m2": 0.0})
    with pytest.raises(GrantRefused, match=r"m2.*free"):
        raise_grants(path, clock, m2=5.0)
    path, clock = closed(tmp_path / "other")
    with pytest.raises(GrantRefused, match=r"no runs of model nobody"):
        raise_grants(path, clock, nobody=5.0)


def test_c66_the_issue_path_weighs_the_raised_grant_against_the_budget_left(tmp_path):
    """C66: with 300 USD, m1 at 276.99 leaves 23.01: the first lease goes out and the second waits over the budget
    left, where at the 100 it had three would have gone out."""
    runs = plan(models=("m1",), tasks=3, conditions=("api-none",))
    path, clock = closed(tmp_path, runs=runs, max_usd=300.0, grants_usd={"m1": 100.0})
    raise_grants(path, clock, m1=276.99)
    ledger = reopened(path, clock)
    try:
        join(ledger, clock, "fed-1")
        join(ledger, clock, "fed-2")
        assert not isinstance(ledger.issue("fed-1"), Wait)
        waits = ledger.issue("fed-2")
        assert isinstance(waits, Wait)
        assert "276.99" in waits.reason
        assert "over the budget left" in waits.reason
    finally:
        ledger.close()


def test_c66_the_controllers_view_sizes_the_fleet_with_the_raised_grant(tmp_path):
    """C66: what the controller reads as the model's grant, to size how many federations the budget can hold, is the
    raised one."""
    path, clock = closed(tmp_path)
    raise_grants(path, clock, m1=276.99)
    ledger = reopened(path, clock)
    try:
        assert ledger_view(ledger).grants_micro == {"m1": 276_990_000, "m2": 50_000_000}
    finally:
        ledger.close()


def test_c66_the_command_raises_a_grant_and_says_what_it_did(tmp_path, capsys):
    """C66: `set-grant LEDGER MODEL=USD... --reason TEXT --actor NAME`: exit 0, each model's old and new grant, and the
    next controller's core named."""
    path, _ = closed(tmp_path)
    args = ["set-grant", str(path), "m1=276.99", "m2=69.245", "--reason", "before the first lease", "--actor", "a"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "m1" in out
    assert "100.000000 USD -> 276.990000 USD" in out
    assert "69.245000 USD" in out
    assert "this commit or later" in out
    assert json.loads(meta(path)["grant_overrides_micro_usd"]) == {"m1": 276_990_000, "m2": 69_245_000}


def test_c66_the_command_exits_one_for_a_ledger_refusal_and_two_for_a_model_named_twice(tmp_path, capsys):
    """C66: a refusal by the ledger (a grant not above the current one) is exit 1 with the reason on stderr; a model
    named twice is a usage error, exit 2, and neither writes a grant."""
    path, _ = closed(tmp_path)
    tail = ["--reason", "r", "--actor", "a"]
    assert main(["set-grant", str(path), "m1=100", *tail]) == 1
    assert "not above" in capsys.readouterr().err
    assert main(["set-grant", str(path), "m1=200", "m1=300", *tail]) == 2
    assert "more than once" in capsys.readouterr().err
    assert "grant_overrides_micro_usd" not in meta(path)


@pytest.mark.parametrize(
    ("typed", "word"),
    [
        ("m1", "MODEL=USD"),
        ("=5", "MODEL=USD"),
        ("m1=", "number of dollars"),
        ("m1=abc", "number of dollars"),
        ("m1=nan", "finite"),
        ("m1=inf", "finite"),
        ("m1=0", "above zero"),
        ("m1=-1", "above zero"),
        ("m1=1.1234567", "micro-dollar"),
        ("m1=9007199254.740993", "at most"),
        ("m1=1e1000000", "at most"),
    ],
)
def test_c66_an_amount_that_is_not_a_finite_number_of_dollars_above_zero_to_the_micro_dollar_is_a_usage_error(
    tmp_path, capsys, typed, word
):
    """C66: the form is MODEL=USD; USD is read exactly as typed (never through a float), finite, above zero, no finer
    than the micro-dollar the ledger counts in, and no more than a grant held as a float of dollars keeps exactly (a
    huge exponent is refused, not overflowed). A usage error as argparse ends it, and nothing is written."""
    path, _ = closed(tmp_path)
    before = meta(path)
    with pytest.raises(SystemExit) as usage:
        main(["set-grant", str(path), typed, "--reason", "r", "--actor", "a"])
    assert usage.value.code == 2
    assert word in capsys.readouterr().err
    assert meta(path) == before
