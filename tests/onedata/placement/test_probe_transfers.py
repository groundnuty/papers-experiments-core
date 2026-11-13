"""The probes record every transfer of the space that started in the run's window, whoever started it (O08)."""

import pytest

from expcore_onedata.probes import PlatformProbes
from expcore_onedata.testing import ALPHA_ID, BETA_ID, DATA, tool_plane_replicates

PATH = "study/data.bin"


@pytest.fixture
def probes(client, provisioner, ticker, seeded):
    seeded.clock = ticker.clock  # the fake's jobs are timed by the test's clock, as the probes' window is
    return PlatformProbes(client, provisioner, {"data": PATH}, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock)


def made_with(client, provisioner, ticker, **bounds):
    return PlatformProbes(
        client, provisioner, {"data": PATH}, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock, **bounds
    )


def records_of(snapshot):
    section = snapshot["space_transfers"]
    assert section["probe_ok"] is True, section["error"]
    return {r["transfer_id"]: r for r in section["records"]}


def test_o08_a_transfer_the_agent_started_through_the_tool_plane_is_recorded(probes, client, seeded, ticker):
    """O08: a replication the agent asked for through the tool server is in the record with its type, its target by
    position, its state, its counters and its times, and is not marked as the harness's."""
    probes.start({})
    ticker.sleep(5)
    tid = tool_plane_replicates(seeded, client, PATH, BETA_ID)
    seeded.end_transfer(tid)
    record = records_of(probes.finish())[tid]
    assert record["type"] == "replication"
    assert (record["source"], record["target"]) == (None, client.provider_label("beta"))
    assert record["state"] == "completed"
    assert record["listed_as"] == "ended"
    assert (record["files_replicated"], record["bytes_replicated"]) == (1, len(DATA))
    assert (record["schedule_time"], record["start_time"], record["end_time"]) == (1005, 1005, 1005)
    assert record["file"] == "data"
    assert record["scheduled_by_harness"] is False


def test_o08_a_transfer_a_qos_requirement_triggered_is_recorded(probes, client, seeded, ticker):
    """O08: a job no client posted, as the provider starts one for a QoS requirement, is in the record as well."""
    probes.start({})
    ticker.sleep(2)
    tid = seeded.add_transfer("replication", PATH, BETA_ID, scheduled=ticker.now)
    record = records_of(probes.finish())[tid]
    assert (record["type"], record["target"], record["bytes_replicated"]) == (
        "replication",
        client.provider_label("beta"),
        len(DATA),
    )
    assert record["scheduled_by_harness"] is False


def test_o08_a_migration_names_its_source_and_its_target_by_position(probes, client, seeded, ticker):
    """O08: a migration's evicting provider is its source and its replicating one its target, each by position."""
    probes.start({})
    tid = seeded.add_transfer("migration", PATH, BETA_ID, source_id=ALPHA_ID, scheduled=ticker.now)
    record = records_of(probes.finish())[tid]
    assert (record["type"], record["source"], record["target"]) == (
        "migration",
        client.provider_label("alpha"),
        client.provider_label("beta"),
    )


def test_o08_a_job_still_waiting_at_run_end_is_recorded_by_when_it_was_scheduled(probes, seeded, ticker):
    """O08: a job that has not started has no start time; it is in the window by its schedule time and listed as
    waiting, not dropped."""
    probes.start({})
    ticker.sleep(1)
    seeded.transfer_flip_after = 99  # reading its record does not start or end it
    tid = seeded.add_transfer("replication", PATH, BETA_ID, scheduled=ticker.now, ended=False, waiting=True)
    record = records_of(probes.finish())[tid]
    assert record["listed_as"] == "waiting"
    assert record["schedule_time"] == 1001


def test_o08_transfers_that_started_before_the_run_window_are_not_recorded(probes, provisioner, seeded, ticker):
    """O08: the provisioner's replication before the run, and an older job, are not in the run's record; the harness's
    own job inside the window is, marked as the harness's. Since O32 the ended listing is read newest first down to
    the window, so the two older jobs, which ended before it, are not read at all: one job is listed, not three."""
    old = seeded.add_transfer("eviction", PATH, BETA_ID, scheduled=ticker.now - 500, finished=ticker.now - 400)
    provisioner.replicate(PATH, "beta")
    before = next(t for t in seeded.transfers if t != old)
    ticker.sleep(60)  # well before the window, past the allowance for clock skew
    probes.start({})
    provisioner.evict(PATH, "beta")
    inside = next(t for t in seeded.transfers if t not in (old, before))
    snapshot = probes.finish()
    records = records_of(snapshot)
    assert set(records) == {inside}
    assert records[inside]["scheduled_by_harness"] is True
    assert records[inside]["type"] == "eviction"
    assert snapshot["space_transfers"]["listed"] == 1


def test_o08_an_unreadable_listing_is_probe_ok_false_never_zero_transfers(probes, seeded, ticker):
    """O08: a listing answered with a 500 is a failed probe with a reason; the record holds no list of transfers at
    all, so a scorer cannot read it as a run in which none was started. The block maps do not rest on it."""
    probes.start({})
    seeded.statuses.append([f"{seeded.SPACE_ID}/transfers", 1, 500])
    snapshot = probes.finish()
    section = snapshot["space_transfers"]
    assert section["probe_ok"] is False
    assert "500" in section["error"]
    assert section["records"] is None
    assert snapshot["ok"] is False
    assert snapshot["pairs"] is not None


def test_o08_a_listing_that_never_answers_is_retried_within_the_bound_and_then_failed(probes, seeded, ticker):
    """O08: network resets on the listing are retried a bounded number of times, then the probe is failed."""
    probes.start({})
    seeded.resets.append([f"{seeded.SPACE_ID}/transfers", 99])
    section = probes.finish()["space_transfers"]
    assert section["probe_ok"] is False
    assert section["records"] is None
    assert len([c for c in seeded.provider_calls if c[2].endswith("/transfers")]) == 3


@pytest.mark.parametrize(
    "body", [{"items": []}, {"transfers": "tid-1"}, {"transfers": ["../x"]}, {"transfers": [], "nextPageToken": 7}]
)
def test_o08_a_listing_not_in_the_documented_shape_is_probe_ok_false(probes, seeded, body):
    """O08: a page without a list of plain ids, or with a token that is not text, is not read as an empty listing."""
    probes.start({})
    seeded.transfer_list_body = body
    section = probes.finish()["space_transfers"]
    assert section["probe_ok"] is False
    assert section["error"]
    assert section["records"] is None


@pytest.mark.parametrize("field", ["scheduleTime", "type", "replicatingProviderId", "bytesReplicated"])
def test_o08_a_record_without_a_field_the_window_or_the_sum_rests_on_is_probe_ok_false(probes, seeded, ticker, field):
    """O08: a listed job whose record lacks its schedule time, its type, its target or its byte counter cannot be
    placed in or out of the window, or counted; the listing is unknown, not missing that job."""
    probes.start({})
    seeded.add_transfer("replication", PATH, BETA_ID, scheduled=ticker.now)
    seeded.transfer_omit = {field}
    section = probes.finish()["space_transfers"]
    assert section["probe_ok"] is False
    assert field in section["error"]
    assert section["records"] is None


def test_o08_a_record_that_cannot_be_read_is_probe_ok_false(probes, seeded, ticker):
    """O08: a listed id whose record answers 404 is an unknown job, so the listing is unknown."""
    probes.start({})
    tid = seeded.add_transfer("replication", PATH, BETA_ID, scheduled=ticker.now)
    seeded.statuses.append([f"/transfers/{tid}", 2, 404])
    section = probes.finish()["space_transfers"]
    assert section["probe_ok"] is False
    assert "404" in section["error"]


def test_o08_a_listing_that_pages_is_followed_to_the_end(client, provisioner, seeded, ticker):
    """O08: with one id per page every job is still found, page after page, until the token runs out."""
    seeded.clock = ticker.clock
    probes = made_with(client, provisioner, ticker, page_limit=1)
    probes.start({})
    made = {seeded.add_transfer("replication", PATH, BETA_ID, scheduled=ticker.now) for _ in range(3)}
    snapshot = probes.finish()
    assert set(records_of(snapshot)) == made
    assert snapshot["space_transfers"]["pages"] > 3


def test_o08_a_listing_longer_than_the_bound_is_unknown_not_truncated(client, provisioner, seeded, ticker):
    """O08: past the bound on the jobs read, the listing is unknown; the record does not hold the first few as if they
    were all."""
    seeded.clock = ticker.clock
    probes = made_with(client, provisioner, ticker, page_limit=1, max_listed=2)
    probes.start({})
    for _ in range(3):
        seeded.add_transfer("replication", PATH, BETA_ID, scheduled=ticker.now)
    section = probes.finish()["space_transfers"]
    assert section["probe_ok"] is False
    assert "2" in section["error"]
    assert section["records"] is None


def test_o08_a_listing_whose_token_never_runs_out_stops_at_the_bound_as_unknown(client, provisioner, seeded, ticker):
    """O08: a listing that hands back a next-page token forever is read a bounded number of pages, then unknown."""
    probes = made_with(client, provisioner, ticker, page_limit=1, max_listed=2)
    probes.start({})
    seeded.transfer_list_body = {"transfers": [], "nextPageToken": "again"}
    section = probes.finish()["space_transfers"]
    assert section["probe_ok"] is False
    assert section["records"] is None
    assert len([c for c in seeded.provider_calls if c[2].endswith("/transfers")]) <= 2 * 3 * 3


def test_o08_the_listing_is_read_on_every_tracked_provider_and_one_that_fails_fails_it(probes, seeded):
    """O08: a job scheduled on one provider can lag in another's listing, so every tracked provider's is read; one
    that cannot be read makes the listing unknown."""
    probes.start({})
    seeded.resets.append(["/transfers", 99, BETA_ID])
    section = probes.finish()["space_transfers"]
    assert {c[0] for c in seeded.provider_calls if c[2].endswith("/transfers")} == {ALPHA_ID, BETA_ID}
    assert section["probe_ok"] is False
    assert section["records"] is None


def test_o08_a_snapshot_without_a_start_has_no_window_and_no_transfers(probes, seeded):
    """O08: without the start of the run there is no window to place a job in; the section is unknown."""
    section = probes.finish()["space_transfers"]
    assert section["probe_ok"] is False
    assert section["records"] is None
