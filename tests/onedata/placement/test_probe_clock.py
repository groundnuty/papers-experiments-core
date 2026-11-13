"""The transfer log reads each provider's clock offset from its Date header and widens the run's window by it (O08)."""

import pytest

from expcore_onedata.probes import PlatformProbes
from expcore_onedata.testing import BETA_ID, DATA

PATH = "study/data.bin"
MORE = "study/more.bin"


@pytest.fixture
def probes(client, provisioner, ticker, seeded):
    """Two tracked files of 4096 bytes each, held by the data provider alone."""
    seeded.add_file(MORE, DATA)
    files = {"data": PATH, "more": MORE}
    return PlatformProbes(client, provisioner, files, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock)


def test_o08_a_transfer_timed_by_a_provider_10_s_behind_is_counted_or_makes_the_bound_unknown(
    probes, client, seeded, ticker
):
    """O08: beta's clock runs 10 s behind this host's. The agent's replication into beta starts 2 s into the run and
    ends 5 s in, which beta's clock records as 992 and 995 for a run that began at 1000. The log reads beta's offset
    from its Date header, widens the window by it and the allowance, and so counts the job; its times lie close to the
    run's start, so beta's bound is unknown, transfer_near_window_edge, never a known bound with the job left out."""
    seeded.provider_skew[BETA_ID] = -10.0
    probes.start({})
    start = ticker.now
    tid = seeded.add_transfer("replication", MORE, BETA_ID, scheduled=start - 8, finished=start - 5)
    ticker.sleep(30)
    snapshot = probes.finish()
    section = snapshot["space_transfers"]
    assert tid in {r["transfer_id"] for r in section["records"]}
    assert section["clock"]["offsets"][client.provider_label("beta")]["min_s"] == -10.0
    assert section["clock"]["offsets"][client.provider_label("alpha")]["max_s"] == 0.0
    beta = snapshot["on_demand"]["providers[1]"]
    assert beta["on_demand_lower_bound"] is None
    assert "transfer_near_window_edge" in beta["unknown"]


def test_o32_a_job_one_provider_stamped_before_the_run_is_flagged_never_a_known_bound(probes, seeded, ticker):
    """O32: the reset's delta-review case, on the probes: alpha's clock agrees with this host's, beta's Date headers
    run 8 s behind, and a replication into beta was scheduled and finished at 995, before a run that starts at 1000 on
    alpha's clock. The window opens at the least offset less the allowance, so the job is recorded, but it lies in the
    band at the window's start: its record says so and beta's bound is unknown, transfer_near_window_edge. No single
    offset makes it part of a known figure, so the probes need no change for it."""
    seeded.provider_skew[BETA_ID] = -8.0
    probes.start({})
    start = ticker.now
    tid = seeded.add_transfer("replication", MORE, BETA_ID, scheduled=start - 5, finished=start - 5)
    ticker.sleep(30)
    snapshot = probes.finish()
    records = {r["transfer_id"]: r for r in snapshot["space_transfers"]["records"]}
    assert records[tid]["near_window_edge"] is True
    beta = snapshot["on_demand"]["providers[1]"]
    assert beta["on_demand_lower_bound"] is None
    assert "transfer_near_window_edge" in beta["unknown"]


def test_o08_a_transfer_that_ended_just_before_the_runs_end_makes_its_targets_bound_unknown(probes, seeded, ticker):
    """O08: a job into beta that finished a second before the block maps were read at the run's end lies within the
    allowance of the window's edge, so on clocks that only agree within an estimate it may have landed after that
    reading. Beta's bound is unknown, transfer_near_window_edge; alpha's is still given."""
    probes.start({})
    ticker.sleep(30)
    seeded.add_transfer("replication", MORE, BETA_ID, scheduled=ticker.now - 10, finished=ticker.now - 1)
    split = probes.finish()["on_demand"]
    assert split["providers[1]"]["on_demand_lower_bound"] is None
    assert "transfer_near_window_edge" in split["providers[1]"]["unknown"]
    assert split["providers[0]"]["on_demand_lower_bound"] == 0


@pytest.mark.parametrize("date", [None, "not a date"])
def test_o08_a_provider_whose_clock_cannot_be_read_makes_every_bound_unknown(probes, seeded, ticker, date):
    """O08: without a readable Date header from beta its clock's offset is unknown, so no job can be placed in or out
    of the window with confidence: every bound is unknown, clock_offset_unknown. The jobs are still recorded."""
    seeded.date_override[BETA_ID] = date
    probes.start({})
    ticker.sleep(30)
    seeded.add_transfer("replication", MORE, BETA_ID, scheduled=ticker.now)
    ticker.sleep(30)
    snapshot = probes.finish()
    for provider in ("providers[0]", "providers[1]"):
        assert snapshot["on_demand"][provider]["on_demand_lower_bound"] is None
        assert "clock_offset_unknown" in snapshot["on_demand"][provider]["unknown"]
    assert snapshot["space_transfers"]["clock"]["known"] is False
    assert snapshot["space_transfers"]["records"]


def test_o08_with_no_tracked_provider_no_clock_is_read_so_it_is_unknown_not_a_crash(client, provisioner, ticker):
    """O08: tracking no provider lists nothing and reads no Date header; the clock is unknown, never assumed."""
    probes = PlatformProbes(client, provisioner, {}, [], sleep=ticker.sleep, now=ticker.clock)
    probes.start({})
    section = probes.finish()["space_transfers"]
    assert section["clock"]["known"] is False
