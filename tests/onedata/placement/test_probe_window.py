"""The probes read the run's window of the ended-transfer listing alone, newest first, so ended jobs of earlier runs,
which the platform never removes, cannot make a run's reading unknown (O32)."""

import pytest

from expcore_onedata.probes import PlatformProbes
from expcore_onedata.testing import BETA_ID, DATA
from expcore_onedata.transferlog import MAX_LISTED
from expcore_onedata.transferorder import STOP_MARGIN_S

PATH = "study/data.bin"
OTHER = "study/other.bin"  # a file the study does not track
ALLOWANCE_S = 5.0  # the default clock-skew allowance of the placement settings


def made_with(client, provisioner, ticker, **bounds):
    return PlatformProbes(
        client, provisioner, {"data": PATH}, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock, **bounds
    )


def earlier_runs(fake, count: int) -> list[str]:
    """`count` ended evictions of the untracked file from beta, as earlier runs and resets leave them, ten a second
    from 100 s on the fake's clock, long before any window here; their ids, oldest first."""
    fake.add_file(OTHER, DATA)
    return [
        fake.add_transfer("eviction", OTHER, BETA_ID, scheduled=100 + i // 10, finished=100 + i // 10)
        for i in range(count)
    ]


def record_reads(fake) -> list:
    return [c for c in fake.provider_calls if c[1] == "GET" and c[2].startswith("/transfers/")]


def test_o32_thousands_of_ended_jobs_from_earlier_runs_leave_the_runs_reading_known(
    client, provisioner, seeded, ticker
):
    """O32: 3000 ended jobs of earlier runs, more than `MAX_LISTED`, and the run's own replication: the probes read
    the ended listing newest first and stop at the first job older than the window, so the reading is known, holds
    the run's job alone, and the bound on bytes fetched on demand is known. At most the page that holds the stop and
    the page after it, whose every record checks the order across the stop, are read past the window, never the
    history."""
    seeded.clock = ticker.clock
    history = earlier_runs(seeded, 3000)
    assert len(history) > MAX_LISTED
    probes = made_with(client, provisioner, ticker, page_limit=100)
    probes.start({})
    ticker.sleep(10)
    tid = seeded.add_transfer("replication", PATH, BETA_ID, scheduled=ticker.now)
    ticker.sleep(10)
    snapshot = probes.finish()
    section = snapshot["space_transfers"]
    assert section["probe_ok"] is True, section["error"]
    assert [r["transfer_id"] for r in section["records"]] == [tid]
    assert section["listed"] == 1
    assert snapshot["on_demand"]["providers[1]"]["on_demand_lower_bound"] == 0
    assert len(record_reads(seeded)) <= 2 * section["polls"] * (100 + 100)  # each provider, each observation


@pytest.mark.parametrize(("inside", "known"), [(2, True), (3, False)])
def test_o32_the_bound_counts_the_jobs_of_the_window_alone(client, provisioner, seeded, ticker, inside, known):
    """O32: with a bound of two jobs, fifty older ones never count against it: two jobs in the window are read, and
    three are still more than the bound, so that listing is unknown, not cut."""
    seeded.clock = ticker.clock
    earlier_runs(seeded, 50)
    probes = made_with(client, provisioner, ticker, page_limit=1, max_listed=2)
    probes.start({})
    ticker.sleep(10)
    for _ in range(inside):
        seeded.add_transfer("replication", PATH, BETA_ID, scheduled=ticker.now)
    ticker.sleep(10)
    section = probes.finish()["space_transfers"]
    assert section["probe_ok"] is known, section["error"]
    if known:
        assert section["listed"] == 2
    else:
        assert "more than 2" in section["error"]
        assert section["records"] is None


@pytest.mark.parametrize("page_limit", [1000, 1])
@pytest.mark.parametrize(
    "moved", ["an earlier run's job ahead of the run's", "the run's first job ahead of its second"]
)
def test_o32_a_listing_out_of_newest_first_order_fails_closed_naming_the_assumption(
    client, provisioner, seeded, ticker, moved, page_limit
):
    """O32: the stop rests on the order, so a finish time later than the one before it makes the reading unknown,
    with the assumption named, rather than ending the walk early and missing the run's jobs. Moving an older job ahead
    of the run's is caught by the page that holds the stop, which is read to its end for the check, or, when that job
    fills its page alone, by the page after it, whose every record the walk reads for the check."""
    seeded.clock = ticker.clock
    old = earlier_runs(seeded, 5)[-1]
    probes = made_with(client, provisioner, ticker, page_limit=page_limit)
    probes.start({})
    ticker.sleep(10)
    first = seeded.add_transfer("replication", PATH, BETA_ID, scheduled=ticker.now)
    ticker.sleep(5)
    seeded.add_transfer("replication", PATH, BETA_ID, scheduled=ticker.now)
    seeded.listing_time[old if moved.startswith("an earlier") else first] = int(ticker.now + 100)
    ticker.sleep(10)
    section = probes.finish()["space_transfers"]
    assert section["probe_ok"] is False
    assert "newest first" in section["error"]
    assert section["records"] is None


def test_o32_waiting_and_ongoing_jobs_are_all_read_however_old(client, provisioner, seeded, ticker):
    """O32: the window bounds the ended listing alone. Five jobs scheduled long before the run that have not ended,
    three ongoing and two waiting, are all read across their pages and all carried over, behind a long ended
    history that is not read."""
    seeded.clock = ticker.clock
    earlier_runs(seeded, 50)
    seeded.transfer_flip_after = 10**6  # reading a record never ends its job
    for i in range(3):
        seeded.add_transfer("replication", OTHER, BETA_ID, scheduled=10 + i, ended=False)
    for i in range(2):
        seeded.add_transfer("replication", OTHER, BETA_ID, scheduled=20 + i, ended=False, waiting=True)
    probes = made_with(client, provisioner, ticker, page_limit=2)
    probes.start({})
    ticker.sleep(10)
    section = probes.finish()["space_transfers"]
    assert section["probe_ok"] is True, section["error"]
    assert section["carried_over"] == 5
    assert section["listed"] == 5


def test_o32_the_ended_listing_is_read_back_to_the_allowance_and_the_margin_before_the_window(
    client, provisioner, seeded, ticker
):
    """O32: the walk keeps every job that finished no earlier than the run's start less the clock-skew allowance and
    `STOP_MARGIN_S`, on this host's clock, and stops at the first that finished before that: a job at the cutoff is
    read (and placed outside the window), one a second before it is where the walk stops. The section names the
    cutoff."""
    seeded.clock = ticker.clock
    seeded.add_file(OTHER, DATA)
    cutoff = ticker.now - ALLOWANCE_S - STOP_MARGIN_S
    seeded.add_transfer("eviction", OTHER, BETA_ID, scheduled=cutoff - 1, finished=cutoff - 1)
    seeded.add_transfer("eviction", OTHER, BETA_ID, scheduled=cutoff, finished=cutoff)
    probes = made_with(client, provisioner, ticker)
    probes.start({})
    ticker.sleep(10)
    section = probes.finish()["space_transfers"]
    assert section["probe_ok"] is True, section["error"]
    assert (section["listed"], section["records"]) == (1, [])
    assert section["ended_since"] == cutoff


def test_o32_a_provider_clock_behind_by_more_than_the_margin_fails_closed(client, provisioner, seeded, ticker):
    """O32: the cutoff is on this host's clock; a provider whose clock runs more than `STOP_MARGIN_S` behind would put
    the window's start before the cutoff, so a job it holds in the window could lie past the stop: the reading is
    unknown, naming the clock, never read short."""
    seeded.clock = ticker.clock
    seeded.provider_skew[BETA_ID] = -(STOP_MARGIN_S + 2)
    probes = made_with(client, provisioner, ticker)
    probes.start({})
    ticker.sleep(10)
    section = probes.finish()["space_transfers"]
    assert section["probe_ok"] is False
    assert "clock" in section["error"]
    assert section["records"] is None
