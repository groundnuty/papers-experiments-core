"""The probes split a provider's gained bytes into transfer bytes and a lower bound on bytes fetched on demand (O08)."""

import pytest
from placement_support import ALPHA_ID, BETA_ID, DATA, tool_plane_replicates

from expcore_onedata.probes import PlatformProbes

PATH = "study/data.bin"
MORE = "study/more.bin"
OTHER = "study/other.bin"  # a file the study does not track


@pytest.fixture
def probes(client, provisioner, ticker, seeded):
    """Two tracked files of 4096 bytes each, held by the data provider alone."""
    seeded.clock = ticker.clock
    seeded.add_file(MORE, DATA)
    seeded.add_file(OTHER, DATA)
    files = {"data": PATH, "more": MORE}
    return PlatformProbes(client, provisioner, files, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock)


INSIDE_S = 10  # seconds a scripted job keeps from the window's edges, past the default 5 s allowance for clock skew


def test_o08_the_lower_bound_is_gained_less_the_transfer_bytes_into_the_provider(
    probes, provisioner, client, seeded, ticker
):
    """O08: the agent replicates one file to beta through the tool plane (4096 bytes) and reads 1000 bytes of the other
    through beta. Beta gained 5096 bytes, transfers moved 4096 into it, so at least 1000 came on demand; alpha gained
    nothing and nothing moved into it."""
    probes.start({})
    ticker.sleep(INSIDE_S)
    seeded.end_transfer(tool_plane_replicates(seeded, client, MORE, BETA_ID))
    provisioner.read_through("beta", seeded.id_of(PATH), 0, 1000)
    ticker.sleep(INSIDE_S)
    snapshot = probes.finish()
    assert snapshot["ok"] is True
    assert snapshot["on_demand"]["beta"] == {
        "gained_bytes": len(DATA) + 1000,
        "transfer_bytes_in": len(DATA),
        "difference": 1000,
        "on_demand_lower_bound": 1000,
        "unknown": None,
    }
    assert snapshot["on_demand"]["alpha"]["on_demand_lower_bound"] == 0
    assert snapshot["on_demand"]["alpha"]["transfer_bytes_in"] == 0


def test_o08_transfer_bytes_of_a_file_the_study_does_not_track_floor_the_bound_at_zero_and_show_the_difference(
    probes, provisioner, seeded, ticker
):
    """O08: a QoS replication of an untracked file into beta counts in its transfer bytes, which then exceed what beta
    gained of the tracked files; the bound is zero, never negative, and the unfloored difference shows it."""
    probes.start({})
    ticker.sleep(INSIDE_S)
    seeded.add_transfer("replication", OTHER, BETA_ID, scheduled=ticker.now)
    provisioner.read_through("beta", seeded.id_of(PATH), 0, 1000)
    ticker.sleep(INSIDE_S)
    beta = probes.finish()["on_demand"]["beta"]
    assert (beta["gained_bytes"], beta["transfer_bytes_in"]) == (1000, len(DATA))
    assert beta["difference"] == 1000 - len(DATA)
    assert beta["on_demand_lower_bound"] == 0


def test_o08_an_eviction_moves_no_bytes_in_and_a_migration_moves_them_into_its_target(probes, seeded, ticker):
    """O08: only replicating jobs count, into their replicating provider: an eviction from beta adds nothing to beta's
    transfer bytes, and a migration from alpha to beta adds its byte counter to beta's alone."""
    probes.start({})
    ticker.sleep(INSIDE_S)
    seeded.add_transfer("migration", MORE, BETA_ID, source_id=ALPHA_ID, scheduled=ticker.now)
    seeded.add_transfer("eviction", PATH, BETA_ID, scheduled=ticker.now)
    ticker.sleep(INSIDE_S)
    split = probes.finish()["on_demand"]
    assert split["beta"]["transfer_bytes_in"] == len(DATA)
    assert split["beta"]["on_demand_lower_bound"] == 0
    assert split["alpha"]["transfer_bytes_in"] == 0


def test_o08_a_transfer_into_a_provider_still_running_at_run_end_makes_its_bound_unknown(probes, seeded, ticker):
    """O08: a job into beta that has not ended has a byte counter that is not final, so beta's bound is None with a
    reason; alpha's, which no such job touches, is still given."""
    probes.start({})
    seeded.transfer_flip_after = 99  # reading its record does not end it, as polling does in the fake
    seeded.add_transfer("replication", MORE, BETA_ID, scheduled=ticker.now, ended=False)
    split = probes.finish()["on_demand"]
    assert split["beta"]["on_demand_lower_bound"] is None
    assert "transfer_not_ended" in split["beta"]["unknown"]
    assert split["alpha"]["on_demand_lower_bound"] == 0


def test_o08_a_transfer_from_before_the_run_that_went_on_into_it_makes_its_targets_bound_unknown(
    probes, seeded, ticker
):
    """O08: a job scheduled before the window and still running is not in the run's record, but it may have placed
    bytes during the run, so beta's bound is None and the job is counted as carried over."""
    seeded.add_transfer("replication", MORE, BETA_ID, scheduled=ticker.now - 60, ended=False)
    probes.start({})
    snapshot = probes.finish()
    assert snapshot["space_transfers"]["records"] == []
    assert snapshot["space_transfers"]["carried_over"] == 1
    assert snapshot["on_demand"]["beta"]["on_demand_lower_bound"] is None
    assert "transfer_carried_over" in snapshot["on_demand"]["beta"]["unknown"]


def test_o08_providers_that_disagree_on_a_transfer_make_its_targets_bound_unknown(probes, client, seeded, ticker):
    """O08: one provider lists the agent's replication into beta as ongoing with no bytes moved, the other as ended
    with 4096. State and counters are never taken from different providers' answers, and the disagreement leaves
    beta's bound unknown with the reason listings_disagree, not a bound built from either side or from a mix."""
    probes.start({})
    ticker.sleep(10)
    tid = tool_plane_replicates(seeded, client, MORE, BETA_ID)
    seeded.end_transfer(tid)
    seeded.provider_view[(tid, ALPHA_ID)] = {"state": "ongoing", "bytesReplicated": 0, "finishTime": 0}
    ticker.sleep(10)
    snapshot = probes.finish()
    beta = snapshot["on_demand"]["beta"]
    assert beta["on_demand_lower_bound"] is None
    assert "listings_disagree" in beta["unknown"]
    record = {r["transfer_id"]: r for r in snapshot["space_transfers"]["records"]}[tid]
    assert record["listings_agree"] is False
    assert (record["listed_as"], record["bytes_replicated"]) in {("ongoing", 0), ("ended", len(DATA))}
    assert snapshot["on_demand"]["alpha"]["on_demand_lower_bound"] == 0


def test_o08_without_the_listing_there_is_no_split(probes, seeded):
    """O08: an unreadable listing leaves the transfer bytes unknown, so there is no split at all, not one of zero."""
    probes.start({})
    seeded.statuses.append([f"{seeded.SPACE_ID}/transfers", 1, 500])
    snapshot = probes.finish()
    assert snapshot["pairs"] is not None
    assert snapshot["on_demand"] is None


def test_o08_without_the_block_maps_there_is_no_split(probes, seeded):
    """O08: a block map that cannot be read leaves the gained bytes unknown, so there is no split either."""
    probes.start({})
    seeded.resets.append(["/distribution", 99, BETA_ID])
    snapshot = probes.finish()
    assert snapshot["space_transfers"]["probe_ok"] is True
    assert snapshot["on_demand"] is None


ONE_OBSERVATION = 6  # listing answers in one observation: two tracked providers, three states, one page each


def test_o07_a_transfer_that_appears_only_in_the_second_poll_is_counted_and_the_bound_is_right(
    probes, provisioner, client, seeded, ticker
):
    """O07 with O08: the agent's replication into beta lags in the listing, absent from the first observation and
    present from the second. The log waits the settle time and observes again until two observations in a row are the
    same, so the job is counted: 4096 transfer bytes into beta, and of its 5096 gained bytes at least 1000 on demand."""
    probes.start({})
    ticker.sleep(10)
    tid = tool_plane_replicates(seeded, client, MORE, BETA_ID)
    seeded.end_transfer(tid)
    seeded.listing_lag[tid] = ONE_OBSERVATION
    provisioner.read_through("beta", seeded.id_of(PATH), 0, 1000)
    ticker.sleep(10)
    snapshot = probes.finish()
    section = snapshot["space_transfers"]
    assert (section["settled"], section["polls"]) == (True, 3)
    assert tid in {r["transfer_id"] for r in section["records"]}
    beta = snapshot["on_demand"]["beta"]
    assert (beta["transfer_bytes_in"], beta["on_demand_lower_bound"], beta["unknown"]) == (len(DATA), 1000, None)
    assert ticker.slept.count(30.0) == 2  # the default settle time, between the three observations


def test_o07_a_listing_that_keeps_changing_leaves_every_bound_unknown(probes, seeded, ticker):
    """O07 with O08: a new job into beta shows in every observation, so no two in a row are the same within the
    bound of four. The listing has not settled, and a transfer that never appeared could be into any provider: every
    bound is unknown, listing_not_settled, alpha's too, though no changing job touches it."""
    probes.start({})
    ticker.sleep(10)
    for n in range(1, 4):
        tid = seeded.add_transfer("replication", MORE, BETA_ID, scheduled=ticker.now)
        seeded.listing_lag[tid] = n * ONE_OBSERVATION
    ticker.sleep(10)
    snapshot = probes.finish()
    assert (snapshot["space_transfers"]["settled"], snapshot["space_transfers"]["polls"]) == (False, 4)
    assert snapshot["on_demand"]["beta"]["on_demand_lower_bound"] is None
    assert "listing_not_settled" in snapshot["on_demand"]["beta"]["unknown"]
    assert snapshot["on_demand"]["alpha"]["on_demand_lower_bound"] is None
    assert "listing_not_settled" in snapshot["on_demand"]["alpha"]["unknown"]


def test_o08_readings_that_place_a_transfer_on_different_sides_of_the_window_make_its_bound_unknown(
    probes, seeded, ticker
):
    """O08: two providers agree on a replication's state and counters but not on when it finished: one says before
    the run, the other during it. Each reading is placed against the window on its own, so the job is neither dropped
    as before the run nor counted on one provider's say: beta's bound is unknown."""
    start = ticker.now
    tid = seeded.add_transfer("replication", MORE, BETA_ID, scheduled=start - 60, finished=start - 50)
    probes.start({})
    ticker.sleep(10)
    seeded.provider_view[(tid, ALPHA_ID)] = {"finishTime": int(start - 50)}
    seeded.provider_view[(tid, BETA_ID)] = {"finishTime": int(start + 5)}
    ticker.sleep(10)
    snapshot = probes.finish()
    beta = snapshot["on_demand"]["beta"]
    assert beta["on_demand_lower_bound"] is None
    assert {"listings_disagree", "transfer_carried_over"} & set(beta["unknown"])


def test_o08_readings_that_name_different_targets_make_both_bounds_unknown(probes, client, seeded, ticker):
    """O08: both providers list the agent's replication as ended with the same counters, but one names beta as its
    target and the other alpha. Which provider gained its bytes is not established, so neither bound is known."""
    probes.start({})
    ticker.sleep(10)
    tid = tool_plane_replicates(seeded, client, MORE, BETA_ID)
    seeded.end_transfer(tid)
    seeded.provider_view[(tid, ALPHA_ID)] = {"replicatingProviderId": ALPHA_ID}
    ticker.sleep(10)
    snapshot = probes.finish()
    for provider in ("alpha", "beta"):
        assert snapshot["on_demand"][provider]["on_demand_lower_bound"] is None
        assert "listings_disagree" in snapshot["on_demand"][provider]["unknown"]


def test_o08_a_file_tracked_twice_is_refused_and_one_file_is_never_counted_twice(client, provisioner, ticker, seeded):
    """O08: two labels naming one path would count its gained bytes twice against its one transfer, giving a known
    bound for on-demand reads that never happened. The probes refuse to track a path twice."""
    with pytest.raises(ValueError, match=r"files\[0\] and files\[1\] name the same path"):
        PlatformProbes(client, provisioner, {"a": PATH, "b": PATH}, ["alpha", "beta"], sleep=ticker.sleep)
