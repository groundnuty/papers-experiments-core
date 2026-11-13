"""The transfer log places the run's window on Onezone's clock, the one a job's times are stamped on, by the Date
headers of Onezone's answers, and records the providers' Date offsets beside it, a diagnostic (O08, O32)."""

import pytest

from expcore_onedata.probes import PlatformProbes
from expcore_onedata.testing import ALPHA_ID, BETA_ID, DATA

PATH = "study/data.bin"
MORE = "study/more.bin"


@pytest.fixture
def probes(client, provisioner, ticker, seeded):
    """Two tracked files of 4096 bytes each, held by the data provider alone."""
    seeded.add_file(MORE, DATA)
    files = {"data": PATH, "more": MORE}
    return PlatformProbes(client, provisioner, files, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock)


def test_o08_a_transfer_into_a_provider_whose_date_runs_10_s_behind_is_placed_on_onezones_clock(
    probes, client, seeded, ticker
):
    """O08: beta's Date headers run 10 s behind this host's, its system clock, while it stamps a job's times on the
    global clock, Onezone's, which agrees with this host. The agent's replication into beta starts 2 s into the run and
    ends 5 s in: the log places it on Onezone's clock and counts it, beta's offset is recorded and moves nothing, and
    its times lie close to the run's start, so beta's bound is unknown, transfer_near_window_edge."""
    seeded.provider_skew[BETA_ID] = -10.0
    probes.start({})
    start = ticker.now
    tid = seeded.add_transfer("replication", MORE, BETA_ID, scheduled=start + 2, finished=start + 5)
    ticker.sleep(30)
    snapshot = probes.finish()
    section = snapshot["space_transfers"]
    assert tid in {r["transfer_id"] for r in section["records"]}
    assert section["clock"]["offsets"][client.provider_label("beta")]["min_s"] == -10.0
    assert section["clock"]["offsets"][client.provider_label("alpha")]["max_s"] == 0.0
    assert section["clock"]["zone"]["min_s"] == section["clock"]["zone"]["max_s"] == 0.0
    beta = snapshot["on_demand"]["providers[1]"]
    assert beta["on_demand_lower_bound"] is None
    assert "transfer_near_window_edge" in beta["unknown"]


def test_o32_codexs_case_providers_ahead_leave_a_job_at_1001_in_the_window_and_flagged(probes, seeded, ticker):
    """O32: the second delta review's case, on the probes: both providers' Date headers run 8 s ahead, Onezone agrees
    with this host, and a replication into beta was scheduled and finished at 1001, a second into a run that starts at
    1000 on the clock that stamped it. The providers' offsets opened the window at 1003: the job lay before it and
    outside the band, so beta's bound was known with its bytes left out. On Onezone's clock it is in the window and in
    the band at its start: recorded, flagged, and beta's bound unknown, transfer_near_window_edge."""
    seeded.provider_skew.update({ALPHA_ID: 8.0, BETA_ID: 8.0})
    probes.start({})
    start = ticker.now
    tid = seeded.add_transfer("replication", MORE, BETA_ID, scheduled=start + 1, finished=start + 1)
    ticker.sleep(30)
    snapshot = probes.finish()
    records = {r["transfer_id"]: r for r in snapshot["space_transfers"]["records"]}
    assert records[tid]["near_window_edge"] is True
    assert snapshot["space_transfers"]["clock"]["first"] == start - 5
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
def test_o08_onezones_clock_unread_makes_every_bound_unknown(probes, seeded, ticker, date):
    """O08: without a readable Date header from Onezone the clock a job's times are stamped on is unknown, so no job
    can be placed in or out of the window with confidence: every bound is unknown, clock_offset_unknown. The jobs are
    still recorded."""
    seeded.zone_date = date
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


def test_o08_a_providers_date_unread_places_nothing(probes, client, seeded, ticker):
    """O08: a provider's Date header gives its system clock, which places no job: without one from beta the clock is
    still known, Onezone's, and beta's offsets are recorded as unreadable."""
    seeded.date_override[BETA_ID] = None
    probes.start({})
    ticker.sleep(30)
    snapshot = probes.finish()
    clock = snapshot["space_transfers"]["clock"]
    assert clock["known"] is True
    assert clock["offsets"][client.provider_label("beta")]["min_s"] is None
    assert "clock_offset_unknown" not in (snapshot["on_demand"]["providers[1]"]["unknown"] or [])


def test_o08_with_no_tracked_provider_onezones_clock_is_still_read(client, provisioner, ticker):
    """O08: tracking no provider lists nothing and reads no provider's Date header; Onezone's clock, the one that would
    place a job, is read all the same."""
    probes = PlatformProbes(client, provisioner, {}, [], sleep=ticker.sleep, now=ticker.clock)
    probes.start({})
    section = probes.finish()["space_transfers"]
    assert section["clock"]["zone"]["samples"] >= 1
    assert section["clock"]["offsets"] == {}
