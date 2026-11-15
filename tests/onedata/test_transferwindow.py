"""The run's window over jobs read from records in the live shape, and the split of a provider's gained bytes (O32).

A record gives both status halves, the one its job does not do marked `skipped` (`transfer_shapes`: the replication's
is a live answer, the eviction's and the migration's are derived from op-worker's source). A job of such a record that
ended is final: one that ended before the window is not carried over into it, and one in the window leaves
its target's bound known when the bytes reconcile."""

import pytest
from transfer_shapes import EVICTION_ENDED, LIVE_REPLICATION, MIGRATION_ENDED, MIGRATION_HALFWAY, statuses

from expcore_onedata.testing import ALPHA_ID, BETA_ID
from expcore_onedata.transferrecord import Job, job_of
from expcore_onedata.transferwindow import CARRIED_OVER, NOT_ENDED, Edges, Window, split_gained

EDGES = Edges(Window(1000.0, 1100.0), 0.0, 0.0, 5.0)  # the widened window runs from 995 to 1105
NAMES = {ALPHA_ID: "providers[0]", BETA_ID: "providers[1]"}.get
SIZE = 4096


def job(kind: str, statuses: dict, scheduled: int, finished: int, listed_as: str = "ended") -> Job:
    """A job of `kind` from a record with these status fields: a replication into beta, an eviction from beta, or a
    migration from alpha to beta, each of one file of `SIZE` bytes."""
    body = {
        "type": kind,
        "fileId": f"file-{scheduled}",
        "replicatingProviderId": None if kind == "eviction" else BETA_ID,
        "evictingProviderId": {"replication": None, "eviction": BETA_ID, "migration": ALPHA_ID}[kind],
        "scheduleTime": scheduled,
        "startTime": scheduled + 1,
        "finishTime": finished,
        "bytesReplicated": 0 if kind == "eviction" else SIZE,
        "filesReplicated": 0 if kind == "eviction" else 1,
        "filesEvicted": 0 if kind == "replication" else 1,
        "filesFailed": 0,
        **statuses,
    }
    return job_of(f"tid-{scheduled}", listed_as, body)


def bounds(jobs: list[Job], gained: dict[str, int]) -> dict:
    """Place the jobs as the transfer log does: those scheduled in the window are the run's, and any other that may
    have run into it is carried over and leaves its target's bound unknown."""
    inside = [j for j in jobs if EDGES.holds(j.scheduled)]
    carried = [(j.target_id, CARRIED_OVER) for j in jobs if j.target_id and EDGES.carried_over(j)]
    return split_gained(gained, inside, NAMES, flagged=carried)


REPLICATED, EVICTED, MIGRATED = statuses(LIVE_REPLICATION), statuses(EVICTION_ENDED), statuses(MIGRATION_ENDED)


@pytest.mark.parametrize(
    ("kind", "statuses"),
    [("replication", REPLICATED), ("eviction", EVICTED), ("migration", MIGRATED)],
)
def test_o32_a_job_that_ended_before_the_window_is_not_carried_over_into_it(kind, statuses):
    """O32: the live symptom. A job listed ended, its record in the live shape, scheduled at 900 and finished at 950,
    before the widened window opens at 995: it ended, so it is neither carried over nor bears on the run."""
    earlier = job(kind, statuses, 900, 950)
    assert earlier.state == "completed"
    assert EDGES.carried_over(earlier) is False
    assert EDGES.bears_on(earlier) is False


def test_o32_a_replication_only_run_has_a_known_bound_and_its_bytes_reconcile():
    """O32: two replications into beta before the run and two in it, every record in the live shape. Nothing is
    carried over, the run's two are final, and the 8192 bytes they moved are exactly what beta gained: its bound is
    known, 0, not unknown."""
    jobs = [job("replication", REPLICATED, s, f) for s, f in ((900, 950), (960, 970), (1010, 1020), (1030, 1050))]
    split = bounds(jobs, {"providers[0]": 0, "providers[1]": 2 * SIZE})
    assert split["providers[1]"] == {
        "gained_bytes": 2 * SIZE,
        "transfer_bytes_in": 2 * SIZE,
        "difference": 0,
        "on_demand_lower_bound": 0,
        "unknown": None,
    }
    assert split["providers[0]"]["on_demand_lower_bound"] == 0


def test_o32_an_eviction_only_run_has_a_known_bound_and_moves_no_bytes_in():
    """O32: evictions from beta before the run and in it, every record in the live shape. None is carried over, none
    moves a byte in, and beta, which gained nothing, has a known bound of 0."""
    jobs = [job("eviction", EVICTED, s, f) for s, f in ((900, 950), (1010, 1020), (1030, 1050))]
    assert not [j for j in jobs if EDGES.carried_over(j)]
    split = bounds(jobs, {"providers[0]": 0, "providers[1]": 0})
    for provider in ("providers[0]", "providers[1]"):
        assert split[provider] == {
            "gained_bytes": 0,
            "transfer_bytes_in": 0,
            "difference": 0,
            "on_demand_lower_bound": 0,
            "unknown": None,
        }


def test_o32_a_replication_in_the_window_that_is_still_replicating_still_leaves_its_bound_unknown():
    """O32: the rule reads a job's own half, so one still running is still not final: a replication into beta whose
    record says `replicating`, `active` and `skipped` leaves beta's bound unknown, `transfer_not_ended`."""
    running = {"transferStatus": "replicating", "replicationStatus": "active", "evictionStatus": "skipped"}
    split = bounds([job("replication", running, 1010, 0, listed_as="ongoing")], {"providers[1]": SIZE})
    assert split["providers[1]"]["on_demand_lower_bound"] is None
    assert split["providers[1]"]["unknown"] == [NOT_ENDED]


def test_o32_a_migration_halfway_through_still_leaves_its_bound_unknown():
    """O32: a migration from alpha to beta whose replication completed and whose eviction is still active is not
    final, though one half is: beta's bound is unknown, and the same job from before the run is carried over."""
    halfway = statuses(MIGRATION_HALFWAY)
    inside = job("migration", halfway, 1010, 0, listed_as="ongoing")
    assert inside.state == "active"
    assert bounds([inside], {"providers[1]": SIZE})["providers[1]"]["unknown"] == [NOT_ENDED]
    assert EDGES.carried_over(job("migration", halfway, 900, 0, listed_as="ongoing")) is True
