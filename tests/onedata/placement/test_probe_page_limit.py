"""`[placement] transfer_page_limit` sets the ids per page of the probes' transfer listing (O08, O32). The walk reads
every record of the page holding its stop and of the page past it, for its order check, so a run after a long history
read two pages of 1,000 records per provider and observation; at 100 it reads a tenth of that, and a listing of up to
`transferlog.MAX_LISTED` ids still ends within the walk's page bound."""

import pytest

from expcore_onedata.probes import PlatformProbes
from expcore_onedata.testing import BETA_ID, write_with_placement
from expcore_onedata.transferlog import MAX_LISTED

PATH = "study/data.bin"
PAGE = 100  # the main study's
HISTORY = 2500  # earlier ended jobs of the space


@pytest.fixture
def platform_file(tmp_path):
    return write_with_placement(tmp_path, transfer_page_limit=PAGE)


def run_end(client, provisioner, ticker, seeded, during=None):
    """A run's start and end with the probes the settings shape; return the snapshot and the record reads at its end."""
    seeded.clock = ticker.clock
    tracked = {"data": PATH}
    probes = PlatformProbes(client, provisioner, tracked, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock)
    probes.start({})
    ticker.sleep(5)
    if during is not None:
        during()
    walk = len(seeded.provider_calls)
    snapshot = probes.finish()
    reads = [
        route
        for _, method, route in seeded.provider_calls[walk:]
        if method == "GET" and route.startswith("/transfers/") and route.count("/") == 2
    ]
    return snapshot, reads


def test_o08_a_page_of_100_reads_at_most_800_records_after_a_long_history(client, provisioner, ticker, seeded):
    """O08, O32: after 2,500 earlier ended jobs, the walk's two observations on two providers read at most 800 records:
    the page of its stop and the page past it, 100 ids each, where pages of 1,000 read 8,000."""
    assert provisioner.settings.transfer_page_limit == PAGE
    for i in range(HISTORY):
        seeded.add_transfer("replication", PATH, BETA_ID, scheduled=100 + i)
    ticker.sleep(4000)
    snapshot, reads = run_end(client, provisioner, ticker, seeded)
    assert snapshot["space_transfers"]["probe_ok"] is True
    assert len(reads) <= 800


@pytest.mark.parametrize("state", ["waiting", "ongoing", "ended"])
def test_o08_a_listing_of_2000_ids_still_ends_at_a_page_of_100(client, provisioner, ticker, seeded, state):
    """O08: 2,000 jobs waiting, ongoing, or ended in the run's window are listed whole at 100 ids a page, within the
    walk's page bound (`MAX_LISTED // 100 + 1` pages), never cut or taken for unknown."""

    def jobs():
        for _ in range(MAX_LISTED):
            seeded.add_transfer(
                "replication",
                PATH,
                BETA_ID,
                scheduled=ticker.now,
                ended=state == "ended",
                waiting=state == "waiting",
            )

    seeded.transfer_flip_after = 10**6  # reading a record moves no job on
    snapshot, _ = run_end(client, provisioner, ticker, seeded, during=jobs)
    section = snapshot["space_transfers"]
    assert section["probe_ok"] is True, section["error"]
    assert section["listed"] == MAX_LISTED
