"""`reopen-run` decides again at the end what it decided at the start: a lease that charged its run is never reopened,
and what changes while the archive is read (the run, the completion, the controller that holds the ledger) is seen by
the last decision, in a dry run as in a real one. Each test changes one thing of the incident the success test
accepts."""

import json
import sqlite3
from contextlib import ExitStack

import pytest
from reopen_support import Incident, dump, events_of, incident, open_ledger, sql
from reopen_support import assert_refused as _refused
from reopen_support import reopen_found as _reopen

from expcore.campaign.elastic import reopenrun as module
from expcore.campaign.elastic.awareness import AWARE
from expcore.campaign.elastic.records import ReopenRefused
from expcore.campaign.lock import DispatchLock

OLDER = "Restart it on this commit or later"


def _set_completion(found: Incident, **fields) -> None:
    """Change fields of lease N's stored completion by hand."""
    db = sqlite3.connect(found.path)
    try:
        stored = json.loads(db.execute("SELECT completion FROM leases WHERE id=?", (found.lease_id,)).fetchone()[0])
        stored.update(fields)
        db.execute("UPDATE leases SET completion=? WHERE id=?", (json.dumps(stored), found.lease_id))
        db.commit()
    finally:
        db.close()


def test_a_lease_that_charged_its_run_is_refused_though_its_archive_shows_nothing_charged(tmp_path):
    """The archive's rows are all uncharged, as a reopening needs, yet the ledger counted lease N for one attempt."""
    found = incident(tmp_path, charged=1)
    _refused(found, 1, "lease 2 charged its run (counted for 1 attempt(s), its completion says 1)")


def test_a_lease_whose_ledger_row_charged_but_whose_completion_says_none_is_refused(tmp_path):
    found = incident(tmp_path)
    sql(found.path, "UPDATE leases SET charged_added=1 WHERE id=?", found.lease_id)
    _refused(found, 1, "counted for 1 attempt(s), its completion says 0")


@pytest.mark.parametrize("said", [1, 2, None, "0", False])
def test_a_lease_whose_completion_does_not_say_charged_zero_is_refused(tmp_path, said):
    found = incident(tmp_path)
    _set_completion(found, charged=said)
    _refused(found, 1, "charged its run")


def test_a_run_an_earlier_lease_charged_is_reopened_when_the_lease_of_its_archive_charged_nothing(tmp_path):
    """The incident's lease 1 charged the run one attempt: only the lease that is reopened must have charged nothing."""
    found = incident(tmp_path)
    assert dump(found.path)["runs"][0][6] == 1  # the run's charged count, from lease 1
    assert _reopen(found).run_charged == 1


def _while_reading(monkeypatch, action) -> None:
    """Run `action` after the first decision and the probe of the controller, while the archive is read."""
    reader = module.read_evidence

    def interleaved(*args):
        action()
        return reader(*args)

    monkeypatch.setattr(module, "read_evidence", interleaved)


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_controller_of_an_older_core_that_takes_the_ledger_while_the_archive_is_read_refuses_the_reopen(
    tmp_path, monkeypatch, dry_run
):
    """No controller held the ledger at the first probe. One that runs a core without the operator pause holds it by the
    time the archive is read, and would lease the run on without the reopening in mind: its lock, no declaration."""
    found = incident(tmp_path)
    before = dump(found.path)
    held = ExitStack()
    _while_reading(monkeypatch, lambda: held.enter_context(DispatchLock(found.path.parent)))
    with held, pytest.raises(ReopenRefused, match=OLDER):
        _reopen(found, dry_run=dry_run)
    assert dump(found.path) == before


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_controller_that_says_it_honours_operator_writes_and_takes_the_ledger_meanwhile_is_beside_the_reopen(
    tmp_path, monkeypatch, dry_run
):
    """The controller's state the command reports is the one at the last decision, not the first probe's."""
    found = incident(tmp_path)
    holders = []
    _while_reading(monkeypatch, lambda: holders.append(open_ledger(found)))
    try:
        done = _reopen(found, dry_run=dry_run)
    finally:
        for ledger in holders:
            ledger.close()
    assert done.controller == AWARE
    assert (done.event_seq is None) is dry_run


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_lease_that_is_counted_for_an_attempt_while_the_archive_is_read_is_refused_at_the_last_decision(
    tmp_path, monkeypatch, dry_run
):
    found = incident(tmp_path)
    _while_reading(monkeypatch, lambda: sql(found.path, "UPDATE leases SET charged_added=1 WHERE id=?", found.lease_id))
    with pytest.raises(ReopenRefused, match=r"^condition 1 \(the lease\): lease 2 charged its run"):
        _reopen(found, dry_run=dry_run)
    assert events_of(found.path) == []


def test_a_dry_run_refuses_a_run_that_another_operator_reopened_while_the_archive_was_read(tmp_path, monkeypatch):
    found = incident(tmp_path)
    _while_reading(
        monkeypatch, lambda: sql(found.path, "UPDATE runs SET state='pending' WHERE run_key=?", found.run_key)
    )
    with pytest.raises(ReopenRefused, match=r"^condition 2 \(the run\)"):
        _reopen(found, dry_run=True)
    assert events_of(found.path) == []


def test_a_dry_run_refuses_a_run_that_was_leased_again_while_the_archive_was_read(tmp_path, monkeypatch):
    found = incident(tmp_path)
    _while_reading(
        monkeypatch,
        lambda: sql(found.path, "UPDATE leases SET state='sent' WHERE id=1"),
    )
    with pytest.raises(ReopenRefused, match=r"^condition 5 \(no lease in flight\)"):
        _reopen(found, dry_run=True)


def test_a_dry_run_refuses_a_completion_that_names_another_archive_when_the_archive_was_read(tmp_path, monkeypatch):
    found = incident(tmp_path)
    _while_reading(monkeypatch, lambda: _set_completion(found, archive_sha256="e" * 64))
    with pytest.raises(ReopenRefused, match=r"^condition 3 \(archive\): .*different archive"):
        _reopen(found, dry_run=True)


def test_a_dry_run_that_nothing_changed_under_says_what_the_real_call_does(tmp_path):
    found = incident(tmp_path)
    would = _reopen(found, dry_run=True)
    did = _reopen(found)
    assert (would.event_seq, did.event_seq is not None) == (None, True)
    assert (would.run_key, would.archive_sha256, would.run_charged, would.attempts) == (
        did.run_key,
        did.archive_sha256,
        did.run_charged,
        did.attempts,
    )
