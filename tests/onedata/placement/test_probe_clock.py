"""The transfer log places the run's window on the global clock, the one a job's times are stamped on, by Onezone's own
reading of it (its public current-time endpoint), and records the Date offsets of Onezone's answer and of the providers'
beside it, a cross-check that places nothing (O08, O32)."""

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


def test_o08_a_transfer_into_a_provider_whose_date_runs_10_s_behind_is_placed_on_the_global_clock(
    probes, client, seeded, ticker
):
    """O08: beta's Date headers run 10 s behind this host's, its system clock, while it stamps a job's times on the
    global clock, which agrees with this host. The agent's replication into beta starts 2 s into the run and ends 5 s
    in: the log places it on the global clock and counts it, beta's offset is recorded and moves nothing, and its times
    lie close to the run's start, so beta's bound is unknown, transfer_near_window_edge."""
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
    """O32: the second delta review's case, on the probes: both providers' Date headers run 8 s ahead, the global
    clock agrees with this host, and a replication into beta was scheduled and finished at 1001, a second into a run
    that starts at 1000 on the clock that stamped it. The providers' offsets opened the window at 1003: the job lay
    before it and outside the band, so beta's bound was known with its bytes left out. On the global clock it is in the
    window and in the band at its start: recorded, flagged, and beta's bound unknown, transfer_near_window_edge."""
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


@pytest.mark.parametrize("skew", [8.0, -8.0])
def test_o32_codexs_third_case_onezones_date_ahead_of_the_global_clock_leaves_a_job_at_1001_flagged(
    probes, seeded, ticker, skew
):
    """O32: the third delta review's case, on the probes: a multi-node Onezone's answering node has a system clock 8 s
    ahead and a global-clock bias of -8 s, so its Date headers say +8 (`zone_skew`; and its mirror) while the global
    clock, which providers stamp finish times with, agrees with this host. A replication into beta scheduled and
    finished at 1001 in a run that starts at 1000 was placed before the window by the Date offset, outside the band,
    and beta's bound known with its bytes left out. The global clock's own reading opens the band at 995: the job is
    recorded, flagged, beta's bound is unknown, and the Date's offset is recorded as a cross-check."""
    seeded.zone_skew = skew
    probes.start({})
    start = ticker.now
    tid = seeded.add_transfer("replication", MORE, BETA_ID, scheduled=start + 1, finished=start + 1)
    ticker.sleep(30)
    snapshot = probes.finish()
    section = snapshot["space_transfers"]
    records = {r["transfer_id"]: r for r in section["records"]}
    assert records[tid]["near_window_edge"] is True
    assert section["clock"]["first"] == start - 5
    assert section["clock"]["zone"]["min_s"] == section["clock"]["zone"]["max_s"] == 0.0
    assert section["clock"]["zone"]["date_offsets_s"] == [skew, skew]
    beta = snapshot["on_demand"]["providers[1]"]
    assert beta["on_demand_lower_bound"] is None
    assert "transfer_near_window_edge" in beta["unknown"]


def test_o32_a_global_clock_behind_this_host_moves_the_window_and_its_bands(client, provisioner, seeded, ticker):
    """O32: this host's clock runs 3 s ahead of the global clock, which stamps a job's times: a job that finished at
    the global clock's 1001 is 1004 by this host's. The window starts at 1003 on the host's clock and 1000 on the
    global one: the band opens at 995 on it, not at 998, and the job is recorded, flagged."""
    seeded.add_file(MORE, DATA)
    behind = PlatformProbes(
        client,
        provisioner,
        {"data": PATH, "more": MORE},
        ["alpha", "beta"],
        sleep=ticker.sleep,
        now=lambda: ticker.now + 3.0,
    )
    behind.start({})
    tid = seeded.add_transfer("replication", MORE, BETA_ID, scheduled=ticker.now + 1, finished=ticker.now + 1)
    ticker.sleep(30)
    section = behind.finish()["space_transfers"]
    assert section["window"]["start"] == 1003.0
    assert section["clock"]["zone"]["min_s"] == section["clock"]["zone"]["max_s"] == -3.0
    assert section["clock"]["first"] == 1003 - 3 - 5
    assert {r["transfer_id"]: r for r in section["records"]}[tid]["near_window_edge"] is True


def test_o32_half_the_round_trip_of_the_read_widens_the_bands(probes, seeded, ticker):
    """O32: each read of the global clock takes 2 s of this host's clock, so its midpoint is within 1 s of the stamp:
    the offsets the readings allow run from -2 s to 0, the band opens 7 s below the run's start (the allowance and the
    half round trip), and the round trip is recorded."""
    seeded.time_hook = lambda answer: (ticker.sleep(2.0), answer)[1]
    probes.start({})
    start = ticker.now
    ticker.sleep(30)
    section = probes.finish()["space_transfers"]
    assert section["clock"]["zone"]["half_trip_s"] == 1.0
    assert (section["clock"]["zone"]["min_s"], section["clock"]["zone"]["max_s"]) == (-2.0, 0.0)
    assert section["clock"]["first"] == start - 7


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


@pytest.mark.parametrize(
    "fault",
    [
        {"time_hook": lambda answer: {}},
        {"time_hook": lambda answer: {"timeMillis": "soon"}},
        {"time_hook": lambda answer: {"timeMillis": -1}},
        {"time_status": 500},
    ],
    ids=["no timeMillis", "a string", "negative", "answers 500"],
)
def test_o08_the_global_clock_unread_makes_every_bound_unknown(probes, seeded, ticker, fault):
    """O08: without a readable reading of the global clock from Onezone, the clock a job's times are stamped on is
    unknown, so no job can be placed in or out of the window with confidence: every bound is unknown,
    clock_offset_unknown. The jobs are still recorded."""
    for name, value in fault.items():
        setattr(seeded, name, value)
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


@pytest.mark.parametrize("date", [None, "not a date"])
def test_o08_onezones_date_unread_places_nothing_and_leaves_the_clock_known(probes, seeded, ticker, date):
    """O08: the Date header of Onezone's answer is a cross-check, never the clock: without a readable one the global
    clock is still known, every bound that needs only it is given, and the cross-check is recorded as missing."""
    seeded.zone_date = date
    probes.start({})
    ticker.sleep(30)
    snapshot = probes.finish()
    clock = snapshot["space_transfers"]["clock"]
    assert clock["known"] is True
    assert clock["zone"]["date_offsets_s"] is None
    assert snapshot["on_demand"]["providers[1]"]["on_demand_lower_bound"] == 0


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
    """O08: tracking no provider lists nothing and reads no provider's Date header; the global clock, the one that
    would place a job, is read all the same."""
    probes = PlatformProbes(client, provisioner, {}, [], sleep=ticker.sleep, now=ticker.clock)
    probes.start({})
    section = probes.finish()["space_transfers"]
    assert section["clock"]["zone"]["samples"] >= 1
    assert section["clock"]["offsets"] == {}
