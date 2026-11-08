"""The stop and the unit's failure a result causes are stored with the result, and derived again at each start."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from campaign_support import NOW, finished
from scheduler_crash import KILLED, PLAN
from scheduler_support import FakeExecutor, attempts_of, build, event_kinds

from expcore.campaign.attempts import AttemptRefused
from expcore.campaign.harness_stop import KEY as STOPS_KEY
from expcore.campaign.units import META as UNITS_META

CRASH = Path(__file__).resolve().parents[1] / "support" / "scheduler_crash.py"
SMALL = {"tasks": ("task-a",), "seeds": (1,)}  # three runs, one per condition
UNIT = "unit-m1"


def kill_on_the_third(root, point, behaviour):
    """Run the campaign in a child that dies at `point` of its third attempt, every attempt ending as `behaviour`."""
    done = subprocess.run(
        [sys.executable, str(CRASH), str(root), point, "3", behaviour], capture_output=True, text=True, check=False
    )
    assert done.returncode == KILLED, done.stderr


@pytest.mark.parametrize("point", ["after_marker", "after_metadata"])
def test_c10_a_kill_on_the_third_same_kind_harness_error_leaves_dispatch_stopped_after_the_restart(tmp_path, point):
    """C04 and C10: killed at the third harness error of a kind, after its marker or after its metadata write, the
    model stays stopped at the restart: dispatch holds and nothing runs."""
    kill_on_the_third(tmp_path, point, "defect")
    executor = FakeExecutor()
    with build(tmp_path, executor=executor, plan=PLAN) as scheduler:
        assert scheduler.recovery.committed == (1 if point == "after_marker" else 0)  # a marker's result is committed
        assert scheduler.recovery.requeued == 3  # the three harness-error runs are pending again from the same rows
        assert scheduler.stops.stopped() == (frozenset({"m1"}), False)
        summary = scheduler.run()
        assert summary.dispatched == 0
        assert executor.requests == []
        assert summary.holds["m1"].kind == "harness_errors"
        assert summary.statuses == {"pending": 3}
        assert event_kinds(scheduler.store).count("harness_error_stop") == 1  # fired once, not again by the derivation


def test_c10_the_stop_is_stored_in_the_transaction_of_the_result_that_causes_it(tmp_path):
    """C04 and C10: a process that dies right after the result's commit has the stop in the store, with no restart."""
    commits = []

    def die_after_the_third_commit(at, run_key):
        if at == "after_metadata":
            commits.append(run_key)
            if len(commits) == 3:
                raise SystemError("the process dies here, before anything that follows the commit")

    with build(
        tmp_path, executor=FakeExecutor(default=("defect",)), plan=SMALL, crash_hook=die_after_the_third_commit
    ) as scheduler:
        with pytest.raises(RuntimeError, match="unexpected error"):
            scheduler.run()
        assert scheduler.stops.stopped() == (frozenset({"m1"}), False)
        assert event_kinds(scheduler.store).count("harness_error_stop") == 1


def test_c10_a_stop_the_record_lacks_is_derived_from_the_stored_attempts_at_the_start(tmp_path):
    """C10: the record of stops is never trusted alone: three same-kind harness errors stored stop the model."""
    with build(tmp_path, executor=FakeExecutor(default=("defect",)), plan=SMALL) as scheduler:
        scheduler.run()
        scheduler.store.db.execute("DELETE FROM meta WHERE key=?", (STOPS_KEY,))  # the record forgot
        assert scheduler.stops.stopped() == (frozenset(), False)
    with build(tmp_path, plan=SMALL) as again:
        assert again.stops.stopped() == (frozenset({"m1"}), False)
        assert again.run().dispatched == 0


def test_c04_a_result_and_the_accounting_it_causes_are_stored_together_or_not_at_all(tmp_path):
    """C04: if the accounting step raises, the result is rolled back with it and the attempt stays running."""
    with build(tmp_path, plan=SMALL) as scheduler:
        store = scheduler.store
        run_key = store.pending()[0]["run_key"]
        begun = store.begin_attempt(run_key, NOW, 1, "")

        def failing():
            store.put_meta("accounting", "partly", NOW)
            raise AttemptRefused("the accounting failed")

        with pytest.raises(AttemptRefused):
            store.finish_attempt(begun.id, finished(), 3, also=failing)
        assert attempts_of(store, run_key) == [("running", 0, 1)]
        assert store.get_meta("accounting") is None
        assert store.finish_attempt(begun.id, finished(), 3, also=lambda: store.put_meta("accounting", "all", NOW))
        assert store.get_meta("accounting") == "all"


def test_c15_a_unit_the_record_forgot_is_paused_again_from_the_stored_attempts_at_the_start(tmp_path):
    """C15: three endpoint failures are stored; with the unit's record gone, the start derives the pause from them."""
    with build(tmp_path, executor=FakeExecutor(default=("infra", "endpoint")), plan=SMALL) as scheduler:
        assert scheduler.run(max_dispatch=3).dispatched == 3
        assert scheduler.units.down(UNIT)
        scheduler.store.db.execute("DELETE FROM meta WHERE key=?", (UNITS_META,))  # the record lost its state
    with build(tmp_path, executor=FakeExecutor(), plan=SMALL) as again:
        assert again.units.down(UNIT)
        assert not again.units.ok(UNIT)
        assert "unit_reconciled" in event_kinds(again.store)


def test_c15_the_units_failures_are_stored_with_the_result_and_a_restart_adds_nothing(tmp_path):
    """C15: the accounting made with the result is what a later start finds; deriving it again changes nothing."""
    with build(tmp_path, executor=FakeExecutor(default=("infra", "endpoint")), plan=SMALL) as scheduler:
        scheduler.run(max_dispatch=2)
        before = scheduler.units.status()
        assert before[UNIT]["fails"] == 2
        assert json.loads(scheduler.store.get_meta(UNITS_META))[UNIT]["fails"] == 2
    with build(tmp_path, executor=FakeExecutor(), plan=SMALL) as again:
        assert again.units.status() == before
        assert "unit_reconciled" not in event_kinds(again.store)


def test_c15_a_kill_after_the_marker_of_the_third_endpoint_failure_still_pauses_the_unit(tmp_path):
    """C04 and C15: the third failure reaches the store from its marker at the restart; the derivation counts it."""
    kill_on_the_third(tmp_path, "after_marker", "endpoint")
    with build(tmp_path, executor=FakeExecutor(), plan=PLAN) as scheduler:
        assert scheduler.recovery.committed == 1
        assert scheduler.units.down(UNIT)
        assert not scheduler.units.ok(UNIT)
