"""The reset's eviction stage waits only for what still works: a listing that still shows the reset's own eviction
running after its record said it ended holds nothing up, a job the reset did not send still does, and the stage
records every wait it took with its reason (O01, O07)."""

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
from expcore_onedata.testing.fake_data import Transfer

SETTLE_S, POLLS, POLL_S = 5.0, 3, 1.0  # the tests' `transfer_settle_s`, `transfer_settle_polls` and `poll_s`
LINGER_S = 8.0  # how long an ended job stays in the ongoing listing: past one settle, within two
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
        with_placement(data, transfer_settle_s=SETTLE_S, transfer_settle_polls=POLLS, poll_s=POLL_S, concurrency=4)
        data["support"]["providers"] = ["alpha", "beta"]

    return write_platform(tmp_path, document)


def test_o07_the_eviction_stage_records_each_wait_it_took_with_its_reason(provisioner, fake, ticker):
    """O07: the stage says what it waited for and how long: the eviction's transfer to end (one poll here), and its
    block maps to show the files empty on consecutive polls (one poll between two reads); no listing showed a job
    running, so nothing was waited for that."""
    eviction = SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]})["stages"]["eviction"]
    assert eviction["waits"] == {
        "transfers_ending": {"count": 1, "seconds": POLL_S},
        "block_maps": {"count": 1, "seconds": POLL_S},
    }


def test_o07_the_resets_own_eviction_still_listed_ongoing_after_it_ended_holds_nothing_up(provisioner, fake, ticker):
    """O07: the listings show a job ended late, so the reset's own eviction, its record ended on the provider it ran
    on, is still listed ongoing when the block maps are due; it changes no map, and the stage reads them at once,
    where it waited `transfer_settle_s` twice for the listing to catch up."""
    fake.ended_listed_ongoing_s = LINGER_S
    record = SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]})
    eviction = record["stages"]["eviction"]
    assert "listed_running" not in eviction["waits"]
    assert eviction["seconds"] == 2 * POLL_S
    assert fake.held(FILES[0], BETA_ID) == []


def test_o01_a_job_the_reset_did_not_send_still_listed_ongoing_is_waited_for(provisioner, fake, ticker):
    """O01: a replication no stage sent ends as the eviction does and is still listed ongoing; the reset cannot tell
    from the listing that it has stopped changing maps, so it waits for the listings to show none, and records the
    waits."""
    fake.ended_listed_ongoing_s = LINGER_S
    original = fake._finish

    def finish(transfer):
        original(transfer)
        if transfer.kind == "eviction" and "tid-outside" not in fake.transfers:
            now = int(ticker.now)
            fake.transfers["tid-outside"] = Transfer(
                "replication", fake.id_of(FILES[0]), BETA_ID, ended=True, scheduled=now, started=now, finished=now
            )

    fake._finish = finish
    eviction = SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]})["stages"]["eviction"]
    assert eviction["waits"]["listed_running"] == {"count": 2, "seconds": 2 * SETTLE_S}
