"""The reset's transfer settle: a space that shows nothing to cancel settles on two observations one
`transfer_settle_s` apart, the listing's lag (O07); a space where anything was seen or cancelled takes the full
`transfer_settle_polls` clean observations in a row."""

import itertools

import pytest
from fake_reset import FakeReset

from expcore_onedata.reset import SpaceReset
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

SETTLE_S, POLLS = 5.0, 3  # the tests' `transfer_settle_s` and `transfer_settle_polls`
FILES = [f"study/part-{i:02d}.bin" for i in range(3)]


@pytest.fixture
def fake(monkeypatch, ticker) -> FakeReset:
    """The federation with a reset's calls: alpha holds the data, beta supports the space and holds every file."""
    federation = FakeReset(space_name=SPACE_NAME)
    federation.clock = ticker.clock
    federation.add_provider(BETA_ID, "beta.test")
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    for login, (password, user_id) in USERS.items():
        federation.add_user(login, password, user_id)
        federation.grant(user_id, BASELINE)
    for path in FILES:
        federation.blocks[(federation.add_file(path, DATA), BETA_ID)] = [[0, len(DATA)]]
    return federation


@pytest.fixture
def platform_file(tmp_path):
    def document(data):
        with_placement(data, transfer_settle_s=SETTLE_S, transfer_settle_polls=POLLS, concurrency=4)
        data["support"]["providers"] = ["alpha", "beta"]

    return write_platform(tmp_path, document)


def listings_during_the_stage(fake, ticker) -> list[float]:
    """Record the instant of every observation of the transfer listings (one waiting listing read per provider)."""
    seen, original = [], fake._list_transfers

    def listed(space, query, provider):
        if query.get("state") == "waiting" and provider == BETA_ID and not query.get("page_token"):
            seen.append(ticker.now)
        return original(space, query, provider)

    fake._list_transfers = listed
    return seen


def test_o07_a_space_with_nothing_to_cancel_settles_on_two_observations_one_settle_apart(provisioner, fake, ticker):
    """O07: nothing waiting or ongoing at the first observation and nothing cancelled: one more observation after one
    `transfer_settle_s`, by when a job scheduled before the first is listed, is enough. The stage waits that once,
    where four observations in a row waited it three times."""
    seen = listings_during_the_stage(fake, ticker)
    transfers = SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]})["stages"]["transfers"]
    assert (transfers["observations"], transfers["cancelled"], transfers["fast"]) == (2, 0, True)
    assert transfers["seconds"] == SETTLE_S
    assert seen[1] - seen[0] == SETTLE_S


def test_o07_a_job_listed_only_at_the_second_observation_is_cancelled_and_takes_the_full_polls(
    provisioner, fake, ticker
):
    """O07: the first observation misses a job the listings show late; the second sees it, cancels it, and from
    then on the stage needs `transfer_settle_polls` clean observations in a row, as before."""
    fake.add_file("other/late.bin", DATA)
    tid = fake.add_transfer("replication", "other/late.bin", BETA_ID, scheduled=ticker.now, ended=False)
    fake.listing_lag[tid] = 4  # the four listings of the first observation (two states, two providers) miss it
    transfers = SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]})["stages"]["transfers"]
    assert fake.transfers[tid].cancelled
    assert (transfers["observations"], transfers["cancelled"], transfers["fast"]) == (2 + POLLS, 1, False)


def test_o07_a_job_seen_at_the_first_observation_keeps_the_full_polls(provisioner, fake, ticker):
    """O07: anything waiting or ongoing at the first observation is cancelled, and the stage needs the full
    `transfer_settle_polls` clean observations after it, each `transfer_settle_s` apart."""
    fake.add_file("other/left.bin", DATA)
    fake.add_transfer("replication", "other/left.bin", BETA_ID, scheduled=ticker.now, ended=False)
    seen = listings_during_the_stage(fake, ticker)
    transfers = SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]})["stages"]["transfers"]
    assert (transfers["observations"], transfers["cancelled"], transfers["fast"]) == (1 + POLLS, 1, False)
    assert transfers["seconds"] == POLLS * SETTLE_S
    assert [b - a for a, b in itertools.pairwise(seen[: 1 + POLLS])] == [SETTLE_S] * POLLS
