"""C58: an operator drains one federation while a controller holds the ledger, on C55's live path (the file's own
write lock, never the dispatcher's): the controller gives it no lease from its next issue, a lease it holds runs to its
end and counts, and the controller's next tick retires it once none is in flight. Refused, writing nothing, beside a
controller that does not say it honours operator writes, and for a federation that cannot be drained."""

import time
from pathlib import Path

import pytest
from control_support import member, settings, site
from elastic_support import FakeClock, finish, join, leased, ledger_path, make, plan

from expcore.campaign.elastic import Ledger, LedgerReader
from expcore.campaign.elastic.__main__ import main
from expcore.campaign.elastic.awareness import AWARE
from expcore.campaign.elastic.control import Retire, decide, ledger_view
from expcore.campaign.elastic.drains import drain_in_file
from expcore.campaign.elastic.records import DrainRefused, Wait
from expcore.campaign.lock import DispatchLock

WHY = "two consecutive health-gate failures"
FED, OTHER = "site-a-fed-1", "site-a-fed-2"  # the ledger's labels of members fed-1 and fed-2 of site-a


def _retired(ledger: Ledger, clock: FakeClock) -> list[str]:
    """The labels the controller's tick retires, from what it reads of the ledger now (its `ledger_view`)."""
    fleet = site(members=[member("fed-1"), member("fed-2")])
    decision = decide(clock(), ledger_view(ledger), [(settings(), fleet)])
    return [action.label for action in decision.actions if isinstance(action, Retire)]


def _drain_events(path: Path) -> list[dict]:
    reader = LedgerReader(path)
    try:
        return [event for event in reader.events() if event["kind"] == "drain"]
    finally:
        reader.close()


def _closed_with(tmp_path, *labels) -> tuple[Path, FakeClock]:
    """A ledger no controller holds, its federations joined with job ends a day after the real clock's now (the
    command reads the real clock)."""
    ledger, clock = make(tmp_path, runs=plan(tasks=2, conditions=("api-none",)))
    end_in = time.time() + 86_400.0 - clock()
    for label in labels:
        join(ledger, clock, label, job_end_in_s=end_in)
    ledger.close()
    return ledger_path(tmp_path), clock


def test_c58_a_drain_beside_a_live_controller_is_honoured_at_its_next_issue_and_tick(tmp_path):
    """C58: the controller holds the ledger (its dispatcher lock and its declaration); the drain is written beside it
    from another connection, recorded as a `drain` with the operator's reason and actor; the controller's next issue
    to the federation waits and its next tick retires it, and the other federation is untouched."""
    ledger, clock = make(tmp_path, runs=plan(tasks=2, conditions=("api-none",)))
    join(ledger, clock, FED)
    join(ledger, clock, OTHER)
    assert _retired(ledger, clock) == []
    event, controller = drain_in_file(ledger_path(tmp_path), FED, WHY, "op-1", clock=clock)
    assert controller == AWARE
    got = ledger.issue(FED)
    assert isinstance(got, Wait)
    assert "draining" in got.reason
    assert _retired(ledger, clock) == [FED]
    (found,) = _drain_events(ledger_path(tmp_path))
    assert (found["seq"], found["federation"]) == (event, FED)
    assert found["detail"] == {"by": "operator", "reason": WHY, "actor": "op-1"}
    assert leased(ledger, OTHER).federation == OTHER
    ledger.close()


def test_c58_a_lease_in_flight_runs_to_its_end_and_counts_and_only_then_is_its_federation_retired(tmp_path):
    """C58: drained while it holds a sent lease, the federation is not retired; the lease reports and is counted as any
    other, and the next tick retires the federation."""
    ledger, clock = make(tmp_path, runs=plan(tasks=2, conditions=("api-none",)))
    join(ledger, clock, FED)
    lease = leased(ledger, FED)
    ledger.mark_sent(lease.lease_id)
    drain_in_file(ledger_path(tmp_path), FED, WHY, "op-1", clock=clock)
    assert _retired(ledger, clock) == []
    assert ledger.lease(lease.lease_id).state == "sent"
    clock.advance(60)
    assert finish(ledger, lease, spend_usd=0.25) == "counted"
    assert _retired(ledger, clock) == [FED]
    ledger.close()


def test_c58_drain_is_refused_and_writes_nothing_beside_an_older_controller(tmp_path, capsys):
    """C58: a controller holds the dispatcher lock and never declares itself (an older core): the command exits 1
    naming the restart, and the federation and the events are as they were."""
    path, _ = _closed_with(tmp_path, FED)
    with DispatchLock(path.parent):
        assert main(["drain", str(path), "--label", FED, "--reason", WHY, "--actor", "op-1"]) == 1
    assert "Restart it on this commit or later" in capsys.readouterr().err
    assert _drain_events(path) == []
    reader = LedgerReader(path)
    try:
        assert reader.federations()[0].drained_at is None
    finally:
        reader.close()


@pytest.mark.parametrize("case", ["unknown", "ended", "drained", "past its end"])
def test_c58_a_federation_that_cannot_be_drained_is_refused_with_nothing_written(tmp_path, case):
    """C58: a label the ledger does not hold, a federation proven ended, one already drained, and one past its
    recorded job end (that is proven, never drained) are each refused, and no drain is recorded."""
    ledger, clock = make(tmp_path, runs=plan(tasks=2, conditions=("api-none",)))
    join(ledger, clock, FED, job_end_in_s=600.0)
    if case == "ended":
        ledger.prove_ended(FED, "terminal")
    elif case == "drained":
        ledger.drain(FED)
    elif case == "past its end":
        clock.advance(601.0)
    before = _drain_events(ledger_path(tmp_path))
    with pytest.raises(DrainRefused):
        drain_in_file(ledger_path(tmp_path), "site-a-fed-9" if case == "unknown" else FED, WHY, "op-1", clock=clock)
    assert _drain_events(ledger_path(tmp_path)) == before
    ledger.close()


def test_c58_the_command_drains_a_ledger_no_controller_holds_and_says_the_next_one_reads_it(tmp_path, capsys):
    """C58: `drain LEDGER --label L --reason R --actor A` exits 0, prints the event and that no controller holds the
    ledger; a controller that opens it then finds the federation drained."""
    path, clock = _closed_with(tmp_path, FED)
    assert main(["drain", str(path), "--label", FED, "--reason", WHY, "--actor", "op-1"]) == 0
    out = capsys.readouterr().out
    assert f"drained federation {FED}" in out
    assert "no controller holds this ledger" in out
    ledger = Ledger.open(path, clock=clock)
    try:
        assert ledger.federations()[0].drained_at is not None
    finally:
        ledger.close()
