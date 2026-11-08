"""Each attempt says where it stands in the campaign's dispatch sequence and how it got its lease (the export's
balance-over-time fields): the store keeps them, the scheduler fills them, the export shows them, and a restart
carries on."""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from campaign_support import NOW, make_schema, planned_store
from lease_support import Clock, Probe
from scheduler_crash import KILLED, PLAN
from scheduler_support import FakeExecutor, build, settings_for

from expcore.campaign.attempts import LEASE_OUTCOMES
from expcore.campaign.export import export_run_state
from expcore.campaign.lease import LeasePool
from expcore.campaign.markers import AttemptResult
from expcore.campaign.store import Store
from expcore.loop.outcomes import Outcome

CRASH = Path(__file__).resolve().parents[1] / "support" / "scheduler_crash.py"
FIELDS = ("dispatch_seq", "started_at", "lease_wait_s", "lease_outcome")


def analysed(store) -> list[dict]:
    """The export's analysed attempts in the order the attempts began."""
    return sorted(export_run_state(store)["analysed_attempts"], key=lambda entry: entry["dispatch_seq"])


def test_every_attempt_takes_the_next_place_in_the_sequence_and_a_reopened_store_carries_on(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    keys = [row.key.text for row in manifest.rows]
    first = store.begin_attempt(keys[0], NOW, 1, "")
    second = store.begin_attempt(keys[1], NOW, 1, "")
    assert (first.dispatch_seq, second.dispatch_seq) == (1, 2)
    path = store.path
    store.close()
    reopened = Store(path, make_schema())
    third = reopened.begin_attempt(keys[2], NOW, 1, "")
    assert third.dispatch_seq == 3
    stored = [r["dispatch_seq"] for r in reopened.q("SELECT dispatch_seq FROM attempts ORDER BY id")]
    assert stored == [1, 2, 3]


def test_an_interrupted_attempt_run_again_is_a_new_row_with_a_later_place(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    store.begin_attempt(key, NOW, 1, "")
    store.db.execute("UPDATE attempts SET state='interrupted'")
    again = store.begin_attempt(key, NOW, 1, "")
    assert again.dispatch_seq == 2


def test_the_lease_an_attempt_ran_under_is_stored_with_it(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    keys = [row.key.text for row in manifest.rows]
    store.begin_attempt(keys[0], NOW, 1, "", lease_wait_s=2.5, lease_outcome="granted")
    store.begin_attempt(keys[1], NOW, 1, "")
    rows = store.q("SELECT lease_wait_s, lease_outcome FROM attempts ORDER BY id")
    assert [(r["lease_wait_s"], r["lease_outcome"]) for r in rows] == [(2.5, "granted"), (None, "none")]


def test_a_lease_outcome_outside_the_vocabulary_is_refused(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    with pytest.raises(ValueError, match="lease_outcome"):
        store.begin_attempt(manifest.rows[0].key.text, NOW, 1, "", lease_outcome="timeout")
    assert store.q("SELECT 1 FROM attempts") == []
    assert LEASE_OUTCOMES == ("granted", "none")


def test_c20_a_database_from_before_the_columns_gets_them_one_at_a_time_and_its_attempts_keep_their_order(tmp_path):
    """C20: the new columns go in like the others; an older attempt's place is its id's order, its lease unknown."""
    store, _, manifest = planned_store(tmp_path)
    keys = [row.key.text for row in manifest.rows]
    for key in keys[:2]:
        store.begin_attempt(key, NOW, 1, "")
    path = store.path
    store.close()
    raw = sqlite3.connect(path)
    for column in ("lease_outcome", "lease_wait_s", "dispatch_seq"):
        raw.execute(f"ALTER TABLE attempts DROP COLUMN {column}")
    raw.commit()
    raw.close()
    reopened = Store(path, make_schema())
    rows = reopened.q("SELECT dispatch_seq, lease_wait_s, lease_outcome FROM attempts ORDER BY id")
    assert [tuple(r) for r in rows] == [(1, None, None), (2, None, None)]
    assert reopened.begin_attempt(keys[2], NOW, 1, "").dispatch_seq == 3


def test_the_export_shows_where_each_analysed_attempt_stood_and_the_places_grow_with_the_attempts(tmp_path):
    executor = FakeExecutor()
    with build(tmp_path, executor=executor, plan=PLAN) as scheduler:
        scheduler.run()
        entries = analysed(scheduler.store)
        by_id = [r["dispatch_seq"] for r in scheduler.store.q("SELECT dispatch_seq FROM attempts ORDER BY id")]
    assert all(set(FIELDS) <= set(entry) for entry in entries)
    assert [entry["dispatch_seq"] for entry in entries] == [1, 2, 3]
    assert by_id == sorted(by_id)  # monotone in the order the attempts began
    assert all(entry["started_at"].endswith("Z") for entry in entries)
    assert [entry["started_at"] for entry in entries] == sorted(entry["started_at"] for entry in entries)
    assert {(e["lease_wait_s"], e["lease_outcome"]) for e in entries} == {(None, "none")}  # the plan needs no lease


def test_a_run_that_needs_a_lease_records_that_it_was_granted_and_how_long_it_waited(tmp_path):
    pool = LeasePool(
        tmp_path / "leases", lambda group: ["k1"], Probe(gone_keys={"k1"}), owner="dispatcher", clock=Clock()
    )
    with build(tmp_path / "c", executor=FakeExecutor(), leases=pool, lease_group=lambda f: "g", plan=PLAN) as scheduler:
        scheduler.run()
        entries = analysed(scheduler.store)
    assert [e["lease_outcome"] for e in entries] == ["granted"] * 3
    assert all(isinstance(e["lease_wait_s"], float) and e["lease_wait_s"] >= 0 for e in entries)


def test_c04_the_places_survive_a_kill_and_a_resume_and_the_new_attempts_come_after_the_old(tmp_path):
    """C04: a killed dispatcher's attempts keep their places; what the next one dispatches takes later ones."""
    done = subprocess.run(
        [sys.executable, str(CRASH), str(tmp_path), "after_marker", "2"], capture_output=True, text=True
    )
    assert done.returncode == KILLED, done.stderr
    raw = sqlite3.connect(tmp_path / "state.sqlite")
    before = dict(raw.execute("SELECT id, dispatch_seq FROM attempts").fetchall())
    raw.close()
    assert sorted(before.values()) == [1, 2]
    with build(tmp_path, executor=FakeExecutor(), plan=PLAN, settings=settings_for()) as scheduler:
        scheduler.run()
        after = {r["id"]: r["dispatch_seq"] for r in scheduler.store.q("SELECT id, dispatch_seq FROM attempts")}
        entries = analysed(scheduler.store)
    assert {i: after[i] for i in before} == before  # the dead process's attempts are unchanged
    assert min(after[i] for i in after if i not in before) > max(before.values())
    assert sorted(after.values()) == list(range(1, len(after) + 1))  # no place twice, none skipped
    assert [entry["dispatch_seq"] for entry in entries] == sorted(entry["dispatch_seq"] for entry in entries)
    assert len(entries) == 3


def test_a_finished_attempt_keeps_its_place(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    key = manifest.rows[0].key.text
    begun = store.begin_attempt(key, NOW, 1, "")
    store.finish_attempt(begun.id, AttemptResult(Outcome("finished"), NOW, 1.0), 3)
    assert store.q("SELECT dispatch_seq FROM attempts")[0]["dispatch_seq"] == begun.dispatch_seq == 1
