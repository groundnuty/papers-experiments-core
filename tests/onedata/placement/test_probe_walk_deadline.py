"""The probes' transfer walk has one deadline, `[placement] transfer_walk_deadline_s`, and records each observation's
seconds (O08): two federations' walks once ran for 41 to 47 minutes. Past the deadline nothing more is read and the
listing is unknown with the reason `deadline`, never no transfers."""

import pytest

from expcore_onedata.probes import PlatformProbes
from expcore_onedata.testing import write_with_placement

PATH = "study/data.bin"
DEADLINE_S = 45  # past one `transfer_settle_s` (30 s): a walk that settles on two observations fits


@pytest.fixture
def platform_file(tmp_path):
    return write_with_placement(tmp_path, transfer_walk_deadline_s=DEADLINE_S)


@pytest.fixture
def probes(client, provisioner, ticker, seeded) -> PlatformProbes:
    seeded.clock = ticker.clock  # the fake's jobs are timed by the test's clock, as the probes' window is
    return PlatformProbes(client, provisioner, {"data": PATH}, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock)


def test_o08_a_transfer_walk_past_its_deadline_fails_closed_with_the_reason(probes, seeded, ticker):
    """O08: a listing page that takes 60 s, past the walk's deadline of 45 s, leaves the listing unknown with the reason
    `deadline` and no records; no further page is read, and the observation's seconds are recorded."""
    probes.start({})
    pages, original = [], seeded._transfer_page

    def stalled(query, provider, include):
        pages.append(provider)
        if len(pages) == 1:
            ticker.sleep(60.0)
        return original(query, provider, include)

    seeded._transfer_page = stalled
    snapshot = probes.finish()
    section = snapshot["space_transfers"]
    assert (section["probe_ok"], section["reason"], section["records"]) == (False, "deadline", None)
    assert section["deadline_s"] == DEADLINE_S
    assert section["observations_s"] == [60.0]
    assert len(pages) == 1
    assert snapshot["ok"] is False


def test_o08_each_observation_of_the_transfer_walk_records_its_seconds(probes):
    """O08: a walk within its deadline records each observation's seconds, one per observation it made."""
    probes.start({})
    section = probes.finish()["space_transfers"]
    assert section["probe_ok"] is True
    assert len(section["observations_s"]) == section["polls"]
    assert "reason" not in section
