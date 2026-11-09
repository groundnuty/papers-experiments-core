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


def test_o08_the_lower_bound_is_gained_less_the_transfer_bytes_into_the_provider(probes, provisioner, client, seeded):
    """O08: the agent replicates one file to beta through the tool plane (4096 bytes) and reads 1000 bytes of the other
    through beta. Beta gained 5096 bytes, transfers moved 4096 into it, so at least 1000 came on demand; alpha gained
    nothing and nothing moved into it."""
    probes.start({})
    seeded.end_transfer(tool_plane_replicates(seeded, client, MORE, BETA_ID))
    provisioner.read_through("beta", seeded.id_of(PATH), 0, 1000)
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
    seeded.add_transfer("replication", OTHER, BETA_ID, scheduled=ticker.now)
    provisioner.read_through("beta", seeded.id_of(PATH), 0, 1000)
    beta = probes.finish()["on_demand"]["beta"]
    assert (beta["gained_bytes"], beta["transfer_bytes_in"]) == (1000, len(DATA))
    assert beta["difference"] == 1000 - len(DATA)
    assert beta["on_demand_lower_bound"] == 0


def test_o08_an_eviction_moves_no_bytes_in_and_a_migration_moves_them_into_its_target(probes, seeded, ticker):
    """O08: only replicating jobs count, into their replicating provider: an eviction from beta adds nothing to beta's
    transfer bytes, and a migration from alpha to beta adds its byte counter to beta's alone."""
    probes.start({})
    seeded.add_transfer("migration", MORE, BETA_ID, source_id=ALPHA_ID, scheduled=ticker.now)
    seeded.add_transfer("eviction", PATH, BETA_ID, scheduled=ticker.now)
    split = probes.finish()["on_demand"]
    assert split["beta"]["transfer_bytes_in"] == len(DATA)
    assert split["beta"]["on_demand_lower_bound"] == 0
    assert split["alpha"]["transfer_bytes_in"] == 0


def test_o08_a_transfer_into_a_provider_still_running_at_run_end_makes_its_bound_unknown(probes, seeded, ticker):
    """O08: a job into beta that has not ended has a byte counter that is not final, so beta's bound is None with a
    reason; alpha's, which no such job touches, is still given."""
    probes.start({})
    seeded.add_transfer("replication", MORE, BETA_ID, scheduled=ticker.now, ended=False)
    split = probes.finish()["on_demand"]
    assert split["beta"]["on_demand_lower_bound"] is None
    assert "not ended" in split["beta"]["unknown"]
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
    assert snapshot["on_demand"]["beta"]["unknown"]


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
