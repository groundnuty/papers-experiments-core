"""`reopen-run`: an operator puts a run a plugin defect ended as harness_error back to pending, with the evidence,
written beside a running controller. Each refusal changes one thing of the incident the success test shows is
accepted, and must leave every table of the ledger, `sqlite_sequence` included, as it was."""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from elastic_support import assert_invariants, leased
from reopen_support import (
    DEFECT,
    FED,
    LEDGER_ERROR,
    REFUSED,
    Incident,
    Row,
    dump,
    fake_zstd_archive,
    incident,
    install_stand_in_zstd,
    make_archive,
    open_ledger,
    real_zstd_archive,
    sql,
)

from expcore.campaign.elastic import reopenrun as module
from expcore.campaign.elastic.__main__ import main
from expcore.campaign.elastic.awareness import AWARE, NONE
from expcore.campaign.elastic.records import ReopenRefused
from expcore.campaign.elastic.reopenrun import REOPEN, reopen_in_file
from expcore.campaign.lock import DispatchLock

WHY = "the plugin's ledger refused the restart of an attempt the key had already refused"
needs_zstd = pytest.mark.skipif(shutil.which("zstd") is None, reason="the zstd program is not installed")


def _reopen(found: Incident, **changes):
    arguments = {"lease": found.lease_id, "archive": found.archive, "dry_run": False}
    arguments.update(changes)
    return reopen_in_file(
        found.path,
        arguments["lease"],
        arguments["archive"],
        WHY,
        "op-1",
        dry_run=arguments["dry_run"],
        clock=found.clock,
    )


def _refused(found: Incident, number: int, phrase: str, **changes) -> str:
    """The command is refused naming condition `number`, and every table is as it was."""
    before = dump(found.path)
    with pytest.raises(ReopenRefused) as refusal:
        _reopen(found, **changes)
    message = str(refusal.value)
    assert message.startswith(f"condition {number} ("), message
    assert phrase in message, message
    assert dump(found.path) == before
    return message


def _events(path: Path) -> list[sqlite3.Row]:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        return db.execute("SELECT * FROM events WHERE kind=? ORDER BY seq", (REOPEN,)).fetchall()
    finally:
        db.close()


def test_a_harness_error_run_goes_back_to_pending_and_only_its_state_and_one_event_change(tmp_path):
    """The success: the run is pending with its charged attempt and its latest lease as they were, every other table is
    untouched, and the log has one more row, of the new kind."""
    found = incident(tmp_path)
    before = dump(found.path)
    done = _reopen(found)
    after = dump(found.path)
    assert (done.run_key, done.lease_id, done.run_charged, done.controller) == (found.run_key, found.lease_id, 1, NONE)
    assert {table for table in after if after[table] != before[table]} == {"runs", "events", "sqlite_sequence"}
    changed = [(old, new) for old, new in zip(before["runs"], after["runs"], strict=True) if old != new]
    assert len(changed) == 1
    old, new = changed[0]
    assert (old[0], old[5], new[5]) == (found.run_key, "harness_error", "pending")  # run_key, state
    assert old[:5] == new[:5]  # the run's key, factors and set are as they were
    assert old[6:] == new[6:]  # its charged count and its latest lease are as they were
    assert after["events"][:-1] == before["events"]
    (event,) = _events(found.path)
    assert (event["run_key"], event["lease_id"], event["federation"], event["seq"]) == (
        found.run_key,
        found.lease_id,
        None,
        done.event_seq,
    )


def test_the_event_names_the_run_the_lease_the_archive_the_attempt_rows_the_reason_and_the_actor(tmp_path):
    found = incident(tmp_path)
    _reopen(found)
    (event,) = _events(found.path)
    assert json.loads(event["detail"]) == {
        "actor": "op-1",
        "archive_form": "tar",
        "archive_sha256": found.sha256,
        "attempts": [
            {"id": 1, "attempt": 1, "state": "infra_fail", "infra_kind": "key_rejected", "charged": 0},
            {"id": 2, "attempt": 1, "state": "harness_error", "infra_kind": None, "charged": 0},
        ],
        "reason": WHY,
        "run_charged": 1,
    }
    assert LEDGER_ERROR not in event["detail"]  # the defect's detail is judged, never copied into the log


def test_a_run_is_reopened_beside_a_controller_that_holds_the_ledger_and_is_leased_from_its_next_issue(tmp_path):
    """The dispatcher's lock and its declaration are held, as a running controller holds them; the command writes on the
    file's own lock, and the controller's next issue leases the run with its charged count as the attempt base."""
    found = incident(tmp_path)
    ledger = open_ledger(found)
    try:
        done = _reopen(found)
        assert done.controller == AWARE
        again = leased(ledger, FED)
        assert (again.run_key, again.attempt_base) == (found.run_key, 1)
        assert_invariants(ledger)
    finally:
        ledger.close()


def test_a_tar_zst_archive_is_reopened_on_the_hash_of_the_tar_it_holds(tmp_path, monkeypatch):
    install_stand_in_zstd(tmp_path / "bin", monkeypatch)
    found = incident(tmp_path)
    _reopen(found, archive=fake_zstd_archive(found.archive))
    (event,) = _events(found.path)
    assert json.loads(event["detail"])["archive_form"] == "tar.zst"


@needs_zstd
def test_a_tar_compressed_by_the_real_zstd_is_reopened(tmp_path):
    found = incident(tmp_path)
    _reopen(found, archive=real_zstd_archive(found.archive))
    assert [json.loads(row["detail"])["archive_form"] for row in _events(found.path)] == ["tar.zst"]


def test_the_attempt_rows_are_read_with_the_wal_the_archive_holds(tmp_path):
    """A defect row that lives only in the archived -wal is the last attempt; a charged row that lives only there
    refuses the run."""
    found = incident(tmp_path, (REFUSED,), wal_rows=(DEFECT,))
    _reopen(found)
    assert len(_events(found.path)) == 1
    charged = incident(tmp_path / "charged", (REFUSED, DEFECT), wal_rows=(Row(2, "infra_fail", 1, "endpoint", "x"),))
    _refused(charged, 4, "uncharged")


def test_a_lease_the_ledger_never_issued_is_refused(tmp_path):
    _refused(incident(tmp_path), 1, "has no lease 99", lease=99)


@pytest.mark.parametrize("state", ["reported", "lost", "superseded", "expired", "returned", "cancelled", "sent"])
def test_a_lease_that_is_not_counted_is_refused(tmp_path, state):
    found = incident(tmp_path)
    sql(found.path, "UPDATE leases SET state=? WHERE id=?", state, found.lease_id)
    _refused(found, 1, f"is {state}, not counted")


def test_a_lease_that_is_not_its_runs_latest_is_refused(tmp_path):
    """A later lease of the run exists (made by hand: the ledger leases a run only once it is pending)."""
    found = incident(tmp_path)
    sql(
        found.path,
        "INSERT INTO leases(run_key, federation, site, model, condition, attempt_base, grant_micro, issued_at,"
        " renewed_at, hard_term_at, state, charge_micro, slot) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        found.run_key, FED, "site-a", "m1", "api-none", 1, 0, 1.0, 1.0, 2.0, "cancelled", 0, 0,
    )  # fmt: skip
    _refused(found, 1, "not the latest lease of its run (lease 3 is later)")


def test_an_earlier_lease_of_a_run_that_was_reopened_and_leased_again_is_not_its_latest(tmp_path):
    found = incident(tmp_path)
    _reopen(found)
    ledger = open_ledger(found)
    try:
        assert leased(ledger, FED).lease_id == found.lease_id + 1
    finally:
        ledger.close()
    _refused(found, 1, f"(lease {found.lease_id + 1} is later)")


@pytest.mark.parametrize("state", ["pending", "leased", "infra_exhausted", "done"])
def test_a_run_that_is_not_harness_error_is_refused(tmp_path, state):
    found = incident(tmp_path)
    sql(found.path, "UPDATE runs SET state=? WHERE run_key=?", state, found.run_key)
    _refused(found, 2, f"is {state}, not harness_error")


def test_a_run_whose_lease_ended_done_is_refused(tmp_path):
    _refused(incident(tmp_path, state="done"), 2, "is done, not harness_error")


def test_a_second_call_on_the_same_run_is_refused(tmp_path):
    found = incident(tmp_path)
    _reopen(found)
    _refused(found, 2, "is pending, not harness_error")
    assert len(_events(found.path)) == 1


def test_an_archive_that_is_not_the_one_the_completion_names_is_refused(tmp_path):
    found = incident(tmp_path)
    forged, _ = make_archive(
        found.root, found.run_key, (REFUSED, Row(1, "harness_error", 0, None, "KeyError x")), name="f.tar"
    )
    _refused(found, 3, "not the archive the ledger verified", archive=forged)


def test_a_symbolic_link_to_the_right_archive_is_refused(tmp_path):
    found = incident(tmp_path)
    link = found.root / "link.tar"
    link.symlink_to(found.archive)
    _refused(found, 3, "is a symbolic link", archive=link)


@pytest.mark.parametrize("kind", ["directory", "missing"])
def test_a_path_that_is_not_a_regular_file_is_refused(tmp_path, kind):
    found = incident(tmp_path)
    path = found.root / "not-a-file.tar"
    if kind == "directory":
        path.mkdir()
    _refused(found, 3, "not a regular file" if kind == "directory" else "cannot be opened", archive=path)


@pytest.mark.parametrize(
    ("rows", "wal_rows", "phrase"),
    [
        ((REFUSED, Row(1, "harness_error", 1, None, LEDGER_ERROR)), (), "uncharged"),
        ((Row(1, "infra_fail", 0, "endpoint", "x"), DEFECT), (), "no attempt ended infra_fail of kind key_rejected"),
        ((REFUSED, Row(1, "infra_fail", 0, "endpoint", "x")), (), "not harness_error"),
        ((REFUSED, Row(1, "harness_error", 0, None, "ValueError 0123456789ab: boom")), (), "does not start with"),
        ((REFUSED, Row(1, "harness_error", 0, None, None)), (), "does not start with"),
        ((REFUSED, DEFECT), (Row(2, "infra_fail", 1, "endpoint", "x"),), "uncharged"),
    ],
)
def test_attempt_rows_that_are_not_what_a_plugin_defect_leaves_are_refused(tmp_path, rows, wal_rows, phrase):
    """The completion names this archive, so only its rows are what fails (condition 4, not 3)."""
    _refused(incident(tmp_path, rows, wal_rows=wal_rows), 4, phrase)


@pytest.mark.parametrize("state", ["issued", "sent"])
def test_a_lease_of_the_run_in_flight_is_refused(tmp_path, state):
    found = incident(tmp_path)
    sql(found.path, "UPDATE leases SET state=? WHERE id=1", state)
    _refused(found, 5, f"lease 1 ({state})")


def test_a_lease_of_the_run_expired_with_its_holder_not_proven_ended_is_refused(tmp_path):
    """The ledger's own calls cannot reach this (a run is held, not leased again, while such a lease stands), so the
    earlier lease is made expired by hand, its federation's job not proven ended."""
    found = incident(tmp_path)
    sql(found.path, "UPDATE leases SET state='expired' WHERE id=1")
    _refused(found, 5, "lease 1 (expired)")


def test_a_lease_of_the_run_expired_with_its_holder_proven_ended_is_not_in_the_way(tmp_path):
    found = incident(tmp_path)
    sql(found.path, "UPDATE leases SET state='expired' WHERE id=1")
    sql(found.path, "UPDATE federations SET ended_at=?, end_proof='terminal' WHERE label=?", found.clock() + 60, FED)
    assert _reopen(found).event_seq is not None


def test_a_run_with_no_attempt_left_is_refused(tmp_path):
    """A pending run with its charged attempts at `max_attempts` would be one the ledger itself calls infra_exhausted,
    and no lease could take it."""
    found = incident(tmp_path)
    sql(found.path, "UPDATE runs SET charged=3 WHERE run_key=?", found.run_key)
    _refused(found, 6, "has 3 charged attempts")


def test_a_dry_run_decides_every_condition_and_writes_nothing(tmp_path, capsys):
    found = incident(tmp_path)
    before = dump(found.path)
    done = _reopen(found, dry_run=True)
    assert (done.event_seq, done.run_key) == (None, found.run_key)
    assert dump(found.path) == before
    args = ["reopen-run", str(found.path), "--lease", str(found.lease_id), "--archive", str(found.archive)]
    assert main([*args, "--reason", WHY, "--actor", "op-1", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"would reopen run {found.run_key}: harness_error -> pending" in out
    assert "nothing was written" in out
    assert dump(found.path) == before


def test_a_dry_run_refuses_what_a_real_run_refuses(tmp_path, capsys):
    found = incident(tmp_path, (REFUSED, Row(1, "harness_error", 1, None, LEDGER_ERROR)))
    args = ["reopen-run", str(found.path), "--lease", str(found.lease_id), "--archive", str(found.archive)]
    assert main([*args, "--reason", WHY, "--actor", "op-1", "--dry-run"]) == 1
    assert "condition 4" in capsys.readouterr().err


def test_the_command_reopens_prints_what_it_did_and_a_second_call_is_refused(tmp_path, capsys):
    found = incident(tmp_path)
    args = ["reopen-run", str(found.path), "--lease", str(found.lease_id), "--archive", str(found.archive)]
    assert main([*args, "--reason", WHY, "--actor", "op-1"]) == 0
    out = capsys.readouterr().out
    assert f"reopened run {found.run_key}: harness_error -> pending, its 1 charged attempt(s) unchanged" in out
    assert f"archive tar with SHA-256 {found.sha256}" in out
    assert "row 1: attempt 1 infra_fail key_rejected charged 0" in out
    assert "row 2: attempt 1 harness_error - charged 0" in out
    assert "no controller holds this ledger" in out
    before = dump(found.path)
    assert main([*args, "--reason", WHY, "--actor", "op-1"]) == 1
    assert "condition 2 (the run): run " in capsys.readouterr().err
    assert dump(found.path) == before


def test_the_command_is_refused_beside_a_controller_that_does_not_say_it_honours_operator_writes(tmp_path, capsys):
    found = incident(tmp_path)
    before = dump(found.path)
    args = ["reopen-run", str(found.path), "--lease", str(found.lease_id), "--archive", str(found.archive)]
    with DispatchLock(found.path.parent):  # a controller of an older core: the lock held, no declaration
        assert main([*args, "--reason", WHY, "--actor", "op-1"]) == 1
    assert "Restart it on this commit or later" in capsys.readouterr().err
    assert dump(found.path) == before


def test_the_command_reports_a_ledger_that_is_not_there_and_a_reason_that_says_nothing_as_usage_errors(
    tmp_path, capsys
):
    found = incident(tmp_path)
    args = ["reopen-run", str(found.path), "--lease", str(found.lease_id), "--archive", str(found.archive)]
    assert main([*args, "--reason", "  ", "--actor", "op-1"]) == 2
    assert "reason" in capsys.readouterr().err
    assert main(["reopen-run", str(tmp_path / "none.sqlite"), *args[2:], "--reason", WHY, "--actor", "op-1"]) == 2
    assert "no ledger there" in capsys.readouterr().err


@pytest.mark.parametrize("lease", ["0", "-3", "two"])
def test_a_lease_that_is_not_a_whole_number_from_one_is_a_usage_error(tmp_path, lease):
    found = incident(tmp_path)
    with pytest.raises(SystemExit) as usage:
        main(
            [
                "reopen-run",
                str(found.path),
                "--lease",
                lease,
                "--archive",
                str(found.archive),
                "--reason",
                "r",
                "--actor",
                "a",
            ]
        )
    assert usage.value.code == 2


def test_the_archive_is_read_without_the_write_lock_so_a_controller_is_never_stalled_by_it(tmp_path, monkeypatch):
    """Another process takes the file's write lock while the archive is being read: it gets it at once."""
    found = incident(tmp_path)
    reader = module.read_evidence
    seen = []

    def watched(*args):
        other = sqlite3.connect(found.path, timeout=0.2, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")  # raises `database is locked` if the command held the write lock
            other.execute("ROLLBACK")
            seen.append("free")
        finally:
            other.close()
        return reader(*args)

    monkeypatch.setattr(module, "read_evidence", watched)
    _reopen(found)
    assert seen == ["free"]


def test_every_ledger_condition_is_decided_again_under_the_write_lock(tmp_path, monkeypatch):
    """The ledger changes while the archive is read (another process puts the run back to pending): the command writes
    nothing, and the run is what that process made it."""
    found = incident(tmp_path)
    reader = module.read_evidence

    def interleaved(*args):
        sql(found.path, "UPDATE runs SET state='pending' WHERE run_key=?", found.run_key)
        return reader(*args)

    monkeypatch.setattr(module, "read_evidence", interleaved)
    before_events = len(_events(found.path))
    with pytest.raises(ReopenRefused) as refusal:
        _reopen(found)
    assert str(refusal.value).startswith("condition 2 (the run)")
    assert len(_events(found.path)) == before_events == 0
    db = sqlite3.connect(found.path)
    try:
        assert db.execute("SELECT state FROM runs WHERE run_key=?", (found.run_key,)).fetchone() == ("pending",)
    finally:
        db.close()


def test_a_completion_that_names_another_archive_under_the_write_lock_is_refused(tmp_path, monkeypatch):
    """The hash the archive was read against must be the one the transaction sees: the completion of a counted lease
    never changes in a ledger the core wrote, but the transaction does not rely on it."""
    found = incident(tmp_path)
    reader = module.read_evidence

    def interleaved(*args):
        db = sqlite3.connect(found.path)
        try:
            row = db.execute("SELECT completion FROM leases WHERE id=?", (found.lease_id,)).fetchone()
            other = json.loads(row[0])
            other["archive_sha256"] = "e" * 64
            db.execute("UPDATE leases SET completion=? WHERE id=?", (json.dumps(other), found.lease_id))
            db.commit()
        finally:
            db.close()
        return reader(*args)

    monkeypatch.setattr(module, "read_evidence", interleaved)
    with pytest.raises(ReopenRefused) as refusal:
        _reopen(found)
    assert str(refusal.value).startswith("condition 3 (archive)")
    assert "different archive" in str(refusal.value)
    assert _events(found.path) == []
