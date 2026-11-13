"""The reset's history stage places its window on Onezone's clock and counts the window alone (O32).

The stage reads the ended listing newest first down to `since` less the clock-skew allowance and the margin
(`transferorder.STOP_MARGIN_S`). A finish time is stamped on the platform's global clock, Onezone's, so the Date headers
of Onezone's answers place the band at the window's start, and Onezone's clock must not put it below that cutoff; a
provider's Date header gives its system clock, which places nothing. A job counts in `ended` only when it finished
after that band, and one in it counts in `uncertain`, never in `ended`. The fake stamps finish times on its own clock,
the global one, moves a provider's Date headers alone by `provider_skew`, as the source does, and Onezone's by
`zone_skew`."""

import pytest
from fake_reset import FakeReset

from expcore_onedata.reset import ResetFailed, SpaceReset
from expcore_onedata.testing import (
    ADMIN_ENV,
    ALPHA_ID,
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
    assert (history["ended"], history["uncertain"], history["earlier"]) == (1, 0, True)
    assert all((row["ended"], row["earlier"]) == (1, True) for row in history["providers"].values())


def test_o32_onezones_clock_behind_widens_the_band_and_a_job_in_it_is_uncertain(provisioner, stocked, ticker):
    """O32: Onezone's Date headers run 3 s behind this host's, within the margin, so the band at the window's start
    runs from 1000 less 3 s and the allowance to 1000 less 3 s plus the allowance. A job that finished at 996 lies in
    it: it may have ended on either side of the start, so it counts in `uncertain`, never in `ended`, and is no older
    job."""
    stocked.zone_skew = -3.0
    stocked.add_transfer("replication", FILES[0], BETA_ID, scheduled=996.0, finished=996.0)
    history = history_of(provisioner, ticker)
    assert (history["ended"], history["uncertain"], history["earlier"]) == (1, 1, False)
    assert -4.0 < history["clock"]["zone_least_offset_s"] <= -3.0  # a Date header gives whole seconds
    assert history["clock"]["band"][0] in (991, 992)


@pytest.mark.parametrize("skew", [8.0, -8.0])
@pytest.mark.parametrize(("finished", "ended", "uncertain"), [(1001.0, 1, 1), (1006.0, 2, 0)])
def test_o32_codexs_case_the_providers_date_offsets_move_no_job_out_of_the_window(
    provisioner, stocked, ticker, skew, finished, ended, uncertain
):
    """O32: the second delta review's case: both providers' Date headers run 8 s ahead, Onezone agrees with this host
    and the allowance is 5 s. The providers' offsets put the band at 1003 to 1013, so a job that truly finished at
    1001 was placed before the window, neither ended nor uncertain, and `uncertain` 0 claimed an exact `ended`. On
    Onezone's clock the band is 995 to 1005 whatever the providers' Date headers say, ahead or behind: the job at
    1001 is uncertain, and one at 1006 counts beside the reset's own eviction. The providers' offsets are recorded."""
    stocked.provider_skew.update({ALPHA_ID: skew, BETA_ID: skew})
    stocked.add_transfer("replication", FILES[0], BETA_ID, scheduled=finished, finished=finished)
    history = history_of(provisioner, ticker)
    assert history["clock"]["band"] == [995, 1005]
    assert (history["ended"], history["uncertain"], history["earlier"]) == (ended, uncertain, False)
    assert all(offsets == [skew, skew] for offsets in history["clock"]["provider_offsets_s"].values())


@pytest.mark.parametrize(
    ("finished", "ended", "uncertain", "earlier"),
    [(994.0, 1, 0, True), (995.0, 1, 1, False), (1005.0, 1, 1, False), (1006.0, 2, 0, False)],
)
def test_o32_the_bands_edges_are_uncertain_and_a_second_past_either_is_not(
    provisioner, stocked, ticker, finished, ended, uncertain, earlier
):
    """O32: with Onezone agreeing with this host, the band at 1000 runs from 995 to 1005, both ends included. A job a
    second before it ended before the window and shows an older job, one at either end is uncertain, and one a second
    after it counts beside the reset's own eviction. `ended` is exact only when `uncertain` is 0."""
    stocked.add_transfer("replication", FILES[0], BETA_ID, scheduled=finished, finished=finished)
    history = history_of(provisioner, ticker)
    assert (history["ended"], history["uncertain"], history["earlier"]) == (ended, uncertain, earlier)


def test_o32_onezones_clock_behind_by_more_than_the_margin_fails_the_reset(provisioner, stocked, ticker):
    """O32: Onezone's clock runs 30 s behind, beyond the margin (`STOP_MARGIN_S`): a job that finished at 1005 on this
    host's clock is stamped 975, below the cutoff (985), where the walk would stop short of it. The window would open
    before the cutoff on Onezone's clock, so the reset fails at its history stage, naming the clock, never omitting
    it."""
    stocked.zone_skew = -30.0
    stocked.add_transfer("replication", FILES[0], BETA_ID, scheduled=975.0, finished=975.0)
    assert SINCE - ALLOWANCE_S - STOP_MARGIN_S > 975.0  # the cutoff
    with pytest.raises(ResetFailed, match="history stage") as caught:
        history_of(provisioner, ticker)
    assert "Onezone's clock" in caught.value.record["stages"]["history"]["error"]


@pytest.mark.parametrize("date", [None, "not a date"])
def test_o32_an_answer_of_onezones_without_a_readable_date_fails_the_reset(provisioner, stocked, ticker, date):
    """O32: Onezone's answers carry no readable Date header, so the clock finish times are stamped on is unknown and
    the cutoff cannot be shown to lie below the window on it: the reset fails at its history stage rather than count
    on a guess."""
    stocked.zone_date = date
    with pytest.raises(ResetFailed, match="history stage") as caught:
        history_of(provisioner, ticker)
    assert "Date" in caught.value.record["stages"]["history"]["error"]


def test_o32_a_providers_date_unread_places_nothing_and_fails_nothing(provisioner, stocked, ticker):
    """O32: a provider's listing answers without a Date header leave its system clock unknown, which places no job:
    the history is read on Onezone's clock and the provider's offsets are recorded as unknown."""
    stocked.date_override[BETA_ID] = None
    history = history_of(provisioner, ticker)
    assert (history["ended"], history["uncertain"]) == (1, 0)
    assert None in history["clock"]["provider_offsets_s"].values()


def test_o32_a_job_the_reset_cancels_while_scheduled_is_in_cancelled_never_in_ended(provisioner, stocked, ticker):
    """O32: a replication scheduled at the window's start and still waiting when the reset runs is cancelled by its
    transfers stage. Cancelled before it started, it is given no finish time, so the ended listing puts it last, and
    `ended`, the jobs that ended with a finish time in the window, never holds it: it counts the reset's own eviction
    alone. The stage's `cancelled` counts it, from the reset's own cancel, and the short listing, read to its end,
    makes it neither uncertain nor an older job."""
    tid = stocked.add_transfer("replication", FILES[0], BETA_ID, scheduled=SINCE, ended=False, waiting=True)
    assert ticker.now == SINCE
    record = SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, since=SINCE)
    assert record["stages"]["transfers"]["cancelled"] == 1
    assert stocked.transfers[tid].finished == 0
    history = record["stages"]["history"]
    assert (history["ended"], history["uncertain"], history["earlier"]) == (1, 0, False)
