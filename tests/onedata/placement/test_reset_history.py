"""The reset's history stage places its window on the providers' clocks and counts the window alone (O32).

The stage reads the ended listing newest first down to `since` less the clock-skew allowance and the margin
(`transferorder.STOP_MARGIN_S`); the providers' clocks, from the Date headers of those listing answers, must not put
the window's start below that cutoff, and only the jobs that ended since `since`, on the providers' clocks, count."""

import pytest
from fake_reset import FakeReset

from expcore_onedata.reset import ResetFailed, SpaceReset
from expcore_onedata.testing import (
    ADMIN_ENV,
    BASELINE,
    BETA_ID,
    DATA,
    SPACE_NAME,
    USERS,
    with_placement,
    write_platform,
)
from expcore_onedata.transferorder import STOP_MARGIN_S

FILES = [f"study/part-{i:02d}.bin" for i in range(2)]
SINCE = 1000.0  # the window's start: the ticker's first reading, before any stage runs
ALLOWANCE_S = 5.0  # the default clock-skew allowance of the placement settings


@pytest.fixture
def fake(monkeypatch, ticker) -> FakeReset:
    """The placement fixture's federation, with a reset's calls: beta supports the space and holds every file."""
    federation = FakeReset(space_name=SPACE_NAME)
    federation.clock = ticker.clock
    federation.add_provider(BETA_ID, "beta.test")
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    for login, (password, user_id) in USERS.items():
        federation.add_user(login, password, user_id)
        federation.grant(user_id, BASELINE)
    return federation


def document(data):
    """The placement settings of the tests, with the space supported by both providers, alpha holding its data."""
    with_placement(data, transfer_settle_s=5.0, transfer_settle_polls=3, concurrency=4)
    data["support"]["providers"] = ["alpha", "beta"]


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, document)


@pytest.fixture
def stocked(fake) -> FakeReset:
    """Two files held by alpha (the data provider) and replicated whole to beta."""
    for path in FILES:
        file_id = fake.add_file(path, DATA)
        fake.blocks[(file_id, BETA_ID)] = [[0, len(DATA)]]
    return fake


def history_of(provisioner, ticker) -> dict:
    """Run the reset from `SINCE`, evicting beta's copy (the reset's own one eviction job), and return its history."""
    assert ticker.now == SINCE
    return SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, since=SINCE)["stages"]["history"]


def test_o32_a_job_in_the_margin_below_the_window_is_not_counted_and_says_older_jobs_exist(
    provisioner, stocked, ticker
):
    """O32: with synchronised clocks, a job that finished at 990 lies above the cutoff (985) and is read, but it ended
    before the window's start (1000): the history counts the reset's own eviction alone and says an older job is
    listed. The margin decides where reading stops, never what counts."""
    stocked.add_transfer("replication", FILES[0], BETA_ID, scheduled=990.0, finished=990.0)
    history = history_of(provisioner, ticker)
    assert (history["ended"], history["earlier"]) == (1, True)
    assert all((row["ended"], row["earlier"]) == (1, True) for row in history["providers"].values())


def test_o32_a_provider_clock_behind_moves_the_window_onto_it(provisioner, stocked, ticker):
    """O32: beta's clock runs 3 s behind this host's, within the margin; a job beta finished one second into the
    window reports 998, below the window's start on this host's clock, and is counted, since the window opens on the
    providers' clocks at 997. A record's times are on the clock of the provider that wrote them, so the window opens
    where the least offset puts it, on every provider's listing."""
    stocked.provider_skew[BETA_ID] = -3.0
    stocked.add_transfer("replication", FILES[0], BETA_ID, scheduled=998.0, finished=998.0)
    history = history_of(provisioner, ticker)
    assert (history["ended"], history["earlier"]) == (2, False)
    assert -4.0 < history["clock"]["least_offset_s"] <= -3.0  # a Date header gives whole seconds
    assert history["clock"]["window_opens"] in (996, 997)


def test_o32_a_provider_clock_behind_by_more_than_the_margin_fails_the_reset(provisioner, stocked, ticker):
    """O32: beta's clock runs 30 s behind, beyond the margin (`STOP_MARGIN_S`): a job that finished at 1005 on this
    host's clock reports 975, below the cutoff (985), where the walk would stop short of it. The window would open
    before the cutoff on beta's clock, so the reset fails at its history stage, naming the clock, never omitting it."""
    stocked.provider_skew[BETA_ID] = -30.0
    stocked.add_transfer("replication", FILES[0], BETA_ID, scheduled=975.0, finished=975.0)
    assert SINCE - ALLOWANCE_S - STOP_MARGIN_S > 975.0  # the cutoff
    with pytest.raises(ResetFailed, match="history stage") as caught:
        history_of(provisioner, ticker)
    assert "clock" in caught.value.record["stages"]["history"]["error"]


def test_o32_a_listing_answer_without_a_readable_date_fails_the_reset(provisioner, stocked, ticker):
    """O32: a provider whose listing answers carry no Date header leaves its clock unknown, so the cutoff cannot be
    shown to lie below the window on its clock: the reset fails at its history stage rather than count on a guess."""
    stocked.date_override[BETA_ID] = None
    with pytest.raises(ResetFailed, match="history stage") as caught:
        history_of(provisioner, ticker)
    assert "Date" in caught.value.record["stages"]["history"]["error"]
