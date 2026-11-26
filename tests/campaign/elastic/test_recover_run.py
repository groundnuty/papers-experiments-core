"""`recover-run`: an operator puts a run the campaign lost back to pending with a chosen charged count, on a working
copy of the campaign's sealed final ledger, written beside a running controller. Each refusal changes one thing of the
lost run the success test shows is accepted, and must leave every table of the working copy, `sqlite_sequence`
included, and the sealed final's bytes as they were."""

import json
import os
import shutil
import stat

import pytest
from elastic_support import leased
from recover_support import (
    FED,
    WHY,
    another_fingerprint,
    assert_only_the_run_and_one_event_changed,
    drop_optional_tables,
    event_detail,
    events_of,
    lost_run,
    open_ledger,
)
from recover_support import assert_refused as _refused
from recover_support import recover_found as _recover
from reopen_support import dump, sha256_of, sql

from expcore.campaign.elastic import recoverrun as module
from expcore.campaign.elastic.awareness import AWARE, NONE
from expcore.campaign.elastic.records import RecoverRefused
from expcore.campaign.lock import DispatchLock


def test_an_infra_exhausted_run_goes_back_to_pending_with_the_chosen_count_and_only_its_row_and_one_event_change(
    tmp_path,
):
    """The success: the run is pending with N charged attempts and its latest lease as it was, every lease row and every
    other table is untouched, the log has one more row, of the new kind, and the sealed final is byte for byte as it
    was."""
    found = lost_run(tmp_path)
    before, final = dump(found.path), sha256_of(found.source)
    done = _recover(found, charged=0)
    after = dump(found.path)
    assert (done.run_key, done.prior_state, done.prior_charged, done.charged, done.rule, done.controller) == (
        found.run_key,
        "infra_exhausted",
        3,
        0,
        "waiver",
        NONE,
    )
    assert_only_the_run_and_one_event_changed(found, before, after)
    old, new = (next(row for row in table["runs"] if row[0] == found.run_key) for table in (before, after))
    assert (old[5], old[6], new[5], new[6]) == ("infra_exhausted", 3, "pending", 0)
    assert old[7] == new[7] == found.leases[-1]  # its latest lease is as it was
    (event,) = events_of(found.path)
    assert (event["run_key"], event["lease_id"], event["federation"], event["seq"]) == (
        found.run_key,
        None,
        None,
        done.event_seq,
    )
    assert sha256_of(found.source) == final == done.source_sha256


def test_the_event_names_the_run_its_states_and_counts_the_rule_the_leases_the_source_the_reason_and_the_actor(
    tmp_path,
):
    found = lost_run(tmp_path)
    _recover(found, charged=0)
    assert event_detail(found.path) == {
        "actor": "op-1",
        "charged": 0,
        "leases": [1, 2, 3],
        "prior_charged": 3,
        "prior_state": "infra_exhausted",
        "reason": WHY,
        "rule": "waiver",
        "run_key": found.run_key,
        "source_sha256": sha256_of(found.source),
    }


def test_a_harness_error_run_is_recovered_at_its_own_count_by_the_declared_rule(tmp_path):
    """A sandbox that failed at its start charged one attempt and ended the run; the operator keeps that attempt."""
    found = lost_run(tmp_path, state="harness_error", charges=(1,))
    before = dump(found.path)
    done = _recover(found, charged=1)
    assert (done.prior_state, done.prior_charged, done.charged, done.rule) == ("harness_error", 1, 1, "declared")
    after = dump(found.path)
    assert_only_the_run_and_one_event_changed(found, before, after)
    run = next(row for row in after["runs"] if row[0] == found.run_key)
    assert (run[5], run[6], run[7]) == ("pending", 1, found.leases[-1])
    assert event_detail(found.path) == {
        "actor": "op-1",
        "charged": 1,
        "leases": [1],
        "prior_charged": 1,
        "prior_state": "harness_error",
        "reason": WHY,
        "rule": "declared",
        "run_key": found.run_key,
        "source_sha256": sha256_of(found.source),
    }


@pytest.mark.parametrize(
    ("state", "charges", "charged", "rule"),
    [
        ("infra_exhausted", (1, 1, 1), 0, "waiver"),
        ("infra_exhausted", (1, 1, 1), 2, "waiver"),
        ("harness_error", (1,), 0, "waiver"),
        ("harness_error", (1,), 1, "declared"),
        ("harness_error", (0,), 0, "declared"),
        ("harness_error", (1, 1), 2, "declared"),
    ],
)
def test_the_run_is_pending_with_the_chosen_count_and_the_rule_says_whether_it_is_the_runs_own(
    tmp_path, state, charges, charged, rule
):
    found = lost_run(tmp_path, state=state, charges=charges)
    before = dump(found.path)
    done = _recover(found, charged=charged)
    assert (done.charged, done.rule) == (charged, rule)
    after = dump(found.path)
    assert_only_the_run_and_one_event_changed(found, before, after)
    assert [(row[5], row[6]) for row in after["runs"] if row[0] == found.run_key] == [("pending", charged)]
    assert event_detail(found.path)["rule"] == rule


def test_a_run_is_recovered_beside_a_controller_that_holds_the_ledger_and_is_leased_from_its_next_issue(tmp_path):
    """The dispatcher's lock and its declaration are held, as a running controller holds them; the command writes on the
    file's own lock, and the controller's next issue leases the run with the chosen count as the attempt base."""
    found = lost_run(tmp_path)
    ledger = open_ledger(found)
    try:
        done = _recover(found, charged=1)
        assert done.controller == AWARE
        again = leased(ledger, FED)
        assert (again.run_key, again.attempt_base) == (found.run_key, 1)
    finally:
        ledger.close()


def test_a_second_call_on_the_same_run_is_refused(tmp_path):
    found = lost_run(tmp_path)
    _recover(found)
    _refused(found, 1, f"run {found.run_key} is pending, not infra_exhausted or harness_error")
    assert len(events_of(found.path)) == 1


def test_a_run_the_ledger_does_not_hold_is_refused(tmp_path):
    _refused(lost_run(tmp_path), 1, "the ledger has no run task=none", run_key="task=none")


@pytest.mark.parametrize("state", ["pending", "leased", "done"])
def test_a_run_that_is_not_lost_is_refused(tmp_path, state):
    found = lost_run(tmp_path)
    sql(found.path, "UPDATE runs SET state=? WHERE run_key=?", state, found.run_key)
    _refused(found, 1, f"is {state}, not infra_exhausted or harness_error")


@pytest.mark.parametrize("state", ["issued", "sent", "reported"])
def test_a_lease_of_the_run_in_flight_or_reported_is_refused(tmp_path, state):
    found = lost_run(tmp_path)
    sql(found.path, "UPDATE leases SET state=? WHERE id=1", state)
    _refused(found, 2, f"lease 1 ({state})")


def test_a_lease_of_the_run_expired_with_its_holder_not_proven_ended_is_refused(tmp_path):
    """The ledger's own calls cannot reach this (a run is held, not leased again, while such a lease stands), so the
    earlier lease is made expired by hand, its federation's job not proven ended."""
    found = lost_run(tmp_path)
    sql(found.path, "UPDATE leases SET state='expired' WHERE id=1")
    _refused(found, 2, "lease 1 (expired)")


def test_a_lease_of_the_run_expired_or_lost_with_its_holder_proven_ended_is_not_in_the_way(tmp_path):
    found = lost_run(tmp_path)
    sql(found.path, "UPDATE leases SET state='expired' WHERE id=1")
    sql(found.path, "UPDATE leases SET state='lost' WHERE id=2")
    sql(found.path, "UPDATE federations SET ended_at=?, end_proof='terminal' WHERE label=?", found.clock() + 60, FED)
    assert _recover(found).event_seq is not None


@pytest.mark.parametrize("charged", [-1, 3, 4, 100])
def test_a_charged_count_that_is_not_from_zero_to_below_max_attempts_is_refused(tmp_path, charged):
    _refused(lost_run(tmp_path), 3, "is not a count from 0 to 2", charged=charged)


def test_a_charged_count_above_the_runs_own_is_refused(tmp_path):
    """The verb keeps or lowers the count, never raises it."""
    found = lost_run(tmp_path, state="harness_error", charges=(1,))
    _refused(found, 3, "has 1 charged attempts and 2 is more: the count is kept or lowered, never raised", charged=2)


@pytest.mark.parametrize("charged", [True, 1.0, "1", None])
def test_a_charged_count_that_is_not_a_whole_number_is_a_usage_error(tmp_path, charged):
    found = lost_run(tmp_path)
    with pytest.raises(ValueError, match="whole number"):
        _recover(found, charged=charged)


def test_a_sealed_final_that_is_not_there_is_refused(tmp_path):
    found = lost_run(tmp_path)
    _refused(found, 4, "cannot be opened", source=found.root / "none.sqlite")


def test_a_symbolic_link_to_the_sealed_final_is_refused(tmp_path):
    found = lost_run(tmp_path)
    link = found.root / "link.sqlite"
    link.symlink_to(found.source)
    _refused(found, 4, "is a symbolic link", source=link)


def test_a_directory_is_not_a_sealed_final(tmp_path):
    found = lost_run(tmp_path)
    _refused(found, 4, "is not a regular file", source=found.root)


def test_the_working_copy_itself_is_not_its_own_sealed_final(tmp_path):
    found = lost_run(tmp_path)
    _refused(found, 4, "is the ledger file itself", source=found.path)


def test_a_hard_link_to_the_working_copy_is_the_same_file_and_is_refused(tmp_path):
    """The same device and inode under another name: it would pass every comparison of rows."""
    found = lost_run(tmp_path)
    link = found.root / "linked.sqlite"
    os.link(found.path, link)
    _refused(found, 4, "is the ledger file itself", source=link)


def test_a_file_that_is_not_a_database_is_refused(tmp_path):
    found = lost_run(tmp_path)
    plain = found.root / "plain.sqlite"
    plain.write_bytes(b"this is not a ledger\n" * 100)
    _refused(found, 4, "is not a ledger of expcore-ledger/1", source=plain)


def test_a_database_of_another_schema_is_refused(tmp_path):
    found = lost_run(tmp_path)
    sql(found.source, "UPDATE meta SET value='expcore-ledger/2' WHERE key='schema'")
    _refused(found, 4, "is not a ledger of expcore-ledger/1")


def test_a_sealed_final_of_another_campaign_is_refused(tmp_path):
    found = lost_run(tmp_path)
    another_fingerprint(found.source)
    _refused(found, 4, "is not the ledger's")


def test_a_sealed_final_that_does_not_hold_the_run_is_refused(tmp_path):
    found = lost_run(tmp_path)
    sql(found.source, "UPDATE runs SET run_key='task=other' WHERE run_key=?", found.run_key)
    _refused(found, 4, f"holds no run {found.run_key}")


@pytest.mark.parametrize(
    ("change", "phrase"),
    [
        ("state='harness_error'", "holds run {run} as harness_error with 3 charged attempts"),
        ("state='pending'", "as pending with 3 charged attempts"),
        ("charged=2", "as infra_exhausted with 2 charged attempts"),
    ],
)
def test_a_sealed_final_whose_run_differs_from_the_working_copys_is_refused(tmp_path, change, phrase):
    """The copy has diverged for this run: the working copy was changed, or it is the copy of another final."""
    found = lost_run(tmp_path)
    sql(found.source, f"UPDATE runs SET {change} WHERE run_key=?", found.run_key)
    message = _refused(found, 4, "the copy has diverged for this run")
    assert phrase.format(run=found.run_key) in message


def test_a_sealed_final_with_bytes_in_a_write_ahead_log_beside_it_is_refused(tmp_path):
    """The file alone is not what such a ledger holds, and its hash would not name it."""
    found = lost_run(tmp_path)
    wal = found.source.with_name(found.source.name + "-wal")
    wal.write_bytes(b"x" * 64)
    _refused(found, 4, "holds bytes: the ledger is not sealed")


def test_an_empty_write_ahead_log_beside_the_sealed_final_is_not_in_the_way(tmp_path):
    found = lost_run(tmp_path)
    found.source.with_name(found.source.name + "-wal").write_bytes(b"")
    assert _recover(found).event_seq is not None


def test_the_sealed_final_is_read_without_a_trace_beside_it_even_where_its_directory_cannot_be_written(tmp_path):
    """Opened read-only and immutable: no `-wal` or `-shm` is made, so the final reads from a directory of mode 0555,
    and its bytes and its directory are as they were after a dry run and after the call."""
    if os.geteuid() == 0:
        pytest.skip("root writes where a mode forbids it, so the test would prove nothing")
    found = lost_run(tmp_path)
    directory = found.source.parent
    before = (sha256_of(found.source), sorted(os.listdir(directory)), found.source.stat().st_mtime_ns)
    directory.chmod(0o555)
    try:
        _recover(found, dry_run=True)
        assert stat.S_IMODE(directory.stat().st_mode) == 0o555
        _recover(found)
    finally:
        directory.chmod(0o755)
    assert (sha256_of(found.source), sorted(os.listdir(directory)), found.source.stat().st_mtime_ns) == before


def test_a_sealed_final_that_is_replaced_while_it_is_read_is_refused(tmp_path, monkeypatch):
    """The hash recorded must be of the file the rows were read from: another file at its path after the hash is not."""
    found = lost_run(tmp_path)
    other = found.root / "other.sqlite"
    shutil.copy2(found.source, other)
    digest = module._digest

    def swapped(handle):
        said = digest(handle)
        os.replace(other, found.source)
        return said

    monkeypatch.setattr(module, "_digest", swapped)
    before = dump(found.path)
    with pytest.raises(RecoverRefused, match=r"^condition 4 \(sealed final\): .*changed while it was read"):
        _recover(found)
    assert dump(found.path) == before


def test_a_dry_run_decides_every_condition_and_writes_nothing(tmp_path):
    found = lost_run(tmp_path)
    before, final = dump(found.path), sha256_of(found.source)
    done = _recover(found, dry_run=True)
    assert (done.event_seq, done.run_key, done.rule, done.source_sha256) == (None, found.run_key, "waiver", final)
    assert dump(found.path) == before
    assert sha256_of(found.source) == final
    assert events_of(found.path) == []


def test_a_dry_run_refuses_what_a_real_run_refuses(tmp_path):
    found = lost_run(tmp_path)
    before = dump(found.path)
    with pytest.raises(RecoverRefused, match=r"^condition 3 \(charged count\)"):
        _recover(found, charged=3, dry_run=True)
    assert dump(found.path) == before


def test_a_ledger_without_the_tables_an_older_one_lacks_is_recovered_as_any_other(tmp_path):
    """`cap_changes`, `declarations`, `lease_sets`, `undeclared_seen` and `federation_platforms` are created when first
    needed: the command touches none of them, so a ledger made before them is recovered with only its run and one event
    changed."""
    found = lost_run(tmp_path)
    drop_optional_tables(found.path)
    drop_optional_tables(found.source)
    before = dump(found.path)
    assert "declarations" not in before
    _recover(found)
    after = dump(found.path)
    assert set(after) == set(before)
    assert_only_the_run_and_one_event_changed(found, before, after)
    assert json.loads(events_of(found.path)[0]["detail"])["rule"] == "waiver"


def test_a_controller_that_does_not_say_it_honours_operator_writes_refuses_the_recovery(tmp_path):
    """A controller holding its lock with its declaration is beside the command (the success tests); one holding its
    lock without the declaration, as a core older than the operator's pause does, refuses it, and nothing is written."""
    found = lost_run(tmp_path)
    before = dump(found.path)
    with DispatchLock(found.path.parent), pytest.raises(RecoverRefused, match="Restart it on this commit or later"):
        _recover(found)
    assert dump(found.path) == before
