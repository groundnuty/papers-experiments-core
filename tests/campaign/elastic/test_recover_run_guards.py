"""`recover-run` decides again at the end what it decided at the start: the sealed final is read without the write lock,
and what changes while it is read (the run, its leases, the controller that holds the ledger) is seen by the last
decision, in a dry run as in a real one. Each test changes one thing of the lost run the success test accepts."""

import sqlite3
from contextlib import ExitStack

import pytest
from recover_support import events_of, lost_run, open_ledger
from recover_support import recover_found as _recover
from reopen_support import dump, sql

from expcore.campaign.elastic import recoverrun as module
from expcore.campaign.elastic.awareness import AWARE
from expcore.campaign.elastic.records import RecoverRefused
from expcore.campaign.lock import DispatchLock

OLDER = "Restart it on this commit or later"


def _while_reading(monkeypatch, action) -> None:
    """Run `action` after the first decision and the probe of the controller, while the sealed final is read."""
    reader = module.read_source

    def interleaved(*args):
        action()
        return reader(*args)

    monkeypatch.setattr(module, "read_source", interleaved)


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_run_that_another_operator_recovered_while_the_sealed_final_was_read_is_refused(
    tmp_path, monkeypatch, dry_run
):
    found = lost_run(tmp_path)
    _while_reading(
        monkeypatch, lambda: sql(found.path, "UPDATE runs SET state='pending' WHERE run_key=?", found.run_key)
    )
    with pytest.raises(RecoverRefused, match=r"^condition 1 \(the run\)"):
        _recover(found, dry_run=dry_run)
    assert events_of(found.path) == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_run_that_was_leased_again_while_the_sealed_final_was_read_is_refused(tmp_path, monkeypatch, dry_run):
    found = lost_run(tmp_path)
    _while_reading(monkeypatch, lambda: sql(found.path, "UPDATE leases SET state='sent' WHERE id=1"))
    with pytest.raises(RecoverRefused, match=r"^condition 2 \(no lease in flight\)"):
        _recover(found, dry_run=dry_run)
    assert events_of(found.path) == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_working_copy_whose_run_changed_while_the_sealed_final_was_read_has_diverged(tmp_path, monkeypatch, dry_run):
    """The count the sealed final was read with is not the one the transaction sees."""
    found = lost_run(tmp_path)
    before = dump(found.path)
    _while_reading(monkeypatch, lambda: sql(found.path, "UPDATE runs SET charged=2 WHERE run_key=?", found.run_key))
    with pytest.raises(RecoverRefused, match=r"^condition 4 \(sealed final\): .*the copy has diverged for this run"):
        _recover(found, dry_run=dry_run)
    assert events_of(found.path) == []
    assert dump(found.path)["runs"] != before["runs"]  # the change the test made, and nothing of the command's


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_controller_of_an_older_core_that_takes_the_ledger_while_the_sealed_final_is_read_refuses_the_recovery(
    tmp_path, monkeypatch, dry_run
):
    """No controller held the ledger at the first probe. One that runs a core without the operator pause holds it by the
    time the sealed final is read, and would lease the run on without the recovery in mind: its lock, no declaration."""
    found = lost_run(tmp_path)
    before = dump(found.path)
    held = ExitStack()
    _while_reading(monkeypatch, lambda: held.enter_context(DispatchLock(found.path.parent)))
    with held, pytest.raises(RecoverRefused, match=OLDER):
        _recover(found, dry_run=dry_run)
    assert dump(found.path) == before


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_controller_that_says_it_honours_operator_writes_and_takes_the_ledger_meanwhile_is_beside_the_recovery(
    tmp_path, monkeypatch, dry_run
):
    """The controller's state the command reports is the one at the last decision, not the first probe's."""
    found = lost_run(tmp_path)
    holders = []
    _while_reading(monkeypatch, lambda: holders.append(open_ledger(found)))
    try:
        done = _recover(found, dry_run=dry_run)
    finally:
        for ledger in holders:
            ledger.close()
    assert done.controller == AWARE
    assert (done.event_seq is None) is dry_run


def test_the_sealed_final_is_read_without_the_write_lock_so_a_controller_is_never_stalled_by_it(tmp_path, monkeypatch):
    """Another process takes the file's write lock while the sealed final is being read: it gets it at once."""
    found = lost_run(tmp_path)
    seen = []

    def watched():
        other = sqlite3.connect(found.path, timeout=0.2, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")  # raises `database is locked` if the command held the write lock
            other.execute("ROLLBACK")
            seen.append("free")
        finally:
            other.close()

    _while_reading(monkeypatch, watched)
    _recover(found)
    assert seen == ["free"]


def test_a_dry_run_that_nothing_changed_under_says_what_the_real_call_does(tmp_path):
    found = lost_run(tmp_path)
    would = _recover(found, charged=1, dry_run=True)
    did = _recover(found, charged=1)
    assert (would.event_seq, did.event_seq is not None) == (None, True)
    assert (would.run_key, would.source_sha256, would.rule, would.leases, would.controller) == (
        did.run_key,
        did.source_sha256,
        did.rule,
        did.leases,
        did.controller,
    )
