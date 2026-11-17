"""A replication that ended short is scheduled again (O01, O44): its target runs it from its own copy of the other
providers' block maps, which dbsync brings late, so a replication scheduled right after a write can copy only what the
copy showed and still report completed. The provisioner judges by the target's own block map and tries again, bounded
by the settle bound, recording every try (op-worker f118ed9; `2026-11-14-federation-facts-replication-lag.md`)."""

import dataclasses

import pytest

from expcore_onedata import consistency
from expcore_onedata.provision import (
    REPLICATION_ATTEMPTS,
    REPLICATION_RETRY_PAUSES_S,
    NotPlaced,
    Provisioner,
    resolve_provider_ids,
)
from expcore_onedata.testing import ALPHA_ID, BETA_ID, DATA, SETTINGS, write_with_placement

CHUNK = SETTINGS["chunk_bytes"]
FINAL = 4 * CHUNK + 500
PREFIX = CHUNK + 7
SOURCE = bytes((i * 13 + i // 7) % 256 for i in range(FINAL))
TARGET = "study/day.bin"
SETTLE_S = 300.0  # the live default, so the tries run out before the bound does


@pytest.fixture
def platform_file(tmp_path):
    return write_with_placement(tmp_path, settle_timeout_s=SETTLE_S)


def completed_late(fake, provisioner, ticker, tmp_path, lag_s):
    """A file the data provider made at its final size with a prefix long ago, then completed just now: beta's copy of
    alpha's block map lags by `lag_s` seconds, so it still shows the prefix alone. Return the file's id."""
    fake.add_file("study/kept.bin", DATA)  # the directory, which a preallocation never creates
    source = tmp_path / "staged.bin"
    source.write_bytes(SOURCE)
    file_id = provisioner.preallocate(TARGET, FINAL)
    provisioner.write_range(file_id, source, 0, PREFIX)
    ticker.sleep(60.0)  # the prefix is in every provider's copy
    fake.replication_view_lag_s = lag_s
    provisioner.write_range(file_id, source, PREFIX, FINAL)
    assert fake.held(TARGET, ALPHA_ID) == [[0, FINAL]]
    return file_id


def replications(fake, file_id):
    return [(tid, job) for tid, job in fake.transfers.items() if job.kind == "replication" and job.file_id == file_id]


def test_o01_a_replication_that_completed_short_is_scheduled_again_until_the_targets_own_map_covers_the_file(
    fake, provisioner, ticker, tmp_path
):
    """O01: beta runs the first replication from a copy of alpha's map that shows the prefix alone, so it copies the
    prefix and reports completed; beta's own block map stays short, so after a pause the replication is scheduled
    again, and the second, from a copy that has caught up, places the rest. The record names both tries, their ids
    and the seconds from the first one's end until the map covered the file; the change log holds both ids."""
    file_id = completed_late(fake, provisioner, ticker, tmp_path, lag_s=12.0)
    record = provisioner.replicate_file(file_id, FINAL, "beta")
    jobs = replications(fake, file_id)
    assert [job.bytes_replicated for _, job in jobs] == [PREFIX, FINAL - PREFIX]
    assert fake.held(TARGET, BETA_ID) == [[0, FINAL]]
    assert (record["tries"], record["transfer_ids"]) == (2, [tid for tid, _ in jobs])
    assert record["transfer_states"] == {"completed": 2}
    assert record["placement"]["fully_placed"] is True
    assert REPLICATION_RETRY_PAUSES_S[0] < record["converged_s"] <= record["seconds"]
    logged = [tid for provider, tid, kind in provisioner.transfers() if kind == "replication" and provider == "beta"]
    assert logged == record["transfer_ids"]


def test_o01_a_replication_whose_target_never_catches_up_fails_closed_after_its_tries_within_the_bound(
    fake, provisioner, ticker, tmp_path
):
    """O01: beta's copy of alpha's map never catches up, so every try copies nothing and reports completed. The
    replication is scheduled again after pauses of 5, 10 and 15 s, at most `REPLICATION_ATTEMPTS` times, and then
    refused as `NotPlaced` with the bytes short and the tries named, the whole within the settle bound from the first
    try's end; every try's id is in the change log."""
    file_id = completed_late(fake, provisioner, ticker, tmp_path, lag_s=10.0**6)
    since, began = len(ticker.slept), ticker.now
    with pytest.raises(NotPlaced, match=f"{REPLICATION_ATTEMPTS} tries") as refused:
        provisioner.replicate_file(file_id, FINAL, "beta")
    assert refused.value.report.bytes_short == FINAL
    assert fake.held(TARGET, BETA_ID) == []
    jobs = replications(fake, file_id)
    assert len(jobs) == REPLICATION_ATTEMPTS
    assert all(job.ended for _, job in jobs)
    pauses = [s for s in ticker.slept[since:] if s > SETTINGS["poll_s"]]
    assert pauses == list(REPLICATION_RETRY_PAUSES_S) == [5.0, 10.0, 15.0, 15.0]
    # the first try's wait for its record (a poll), then the settle bound, which a last poll may pass by under one poll
    assert ticker.now - began <= SETTLE_S + 2 * SETTINGS["poll_s"]
    logged = [tid for provider, tid, kind in provisioner.transfers() if kind == "replication" and provider == "beta"]
    assert logged == [tid for tid, _ in jobs]


def test_o01_a_try_still_running_at_the_deadline_fails_as_a_wait_timeout_within_the_settle_bound(
    fake, provisioner, client, config, placement, ticker, tmp_path, monkeypatch
):
    """O01: with a settle bound of 20 s, the first try copies nothing and the second takes 3 s to schedule and never
    ends. Its wait is cut to what is left of the bound once it is scheduled, counted from the first try's end, so the
    replication fails as `WaitTimeout`, a job that may still change the replica, by the deadline; it never passes, and
    both ids are in the change log."""
    file_id = completed_late(fake, provisioner, ticker, tmp_path, lag_s=10.0**6)
    settings = dataclasses.replace(placement.settings, settle_timeout_s=20.0)
    short = Provisioner(
        client,
        config,
        settings,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "short.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    )
    schedule = short.schedule_transfer

    def slow_second(kind, target_id, provider):
        if len(short.transfers()) == 1:  # the second try: slow to schedule, and never ends
            ticker.sleep(3.0)
            fake.transfer_flip_after = 10**6
        return schedule(kind, target_id, provider)

    monkeypatch.setattr(short, "schedule_transfer", slow_second)
    began = ticker.now
    with pytest.raises(consistency.WaitTimeout, match="had not ended"):
        short.replicate_file(file_id, FINAL, "beta")
    first_end = began + SETTINGS["poll_s"]  # the first try's record is read twice, a poll apart
    assert ticker.now <= first_end + 20.0 + SETTINGS["poll_s"]
    assert len(short.transfers()) == 2
    assert fake.held(TARGET, BETA_ID) == []


def test_o01_a_replication_that_places_the_file_at_once_is_scheduled_once(fake, provisioner, ticker, tmp_path):
    """O01: with beta's copy of alpha's map up to date, the first replication places the whole file: one try, one id,
    and no pause."""
    file_id = completed_late(fake, provisioner, ticker, tmp_path, lag_s=0.0)
    since = len(ticker.slept)
    record = provisioner.replicate_file(file_id, FINAL, "beta")
    assert (record["tries"], record["transfer_ids"]) == (1, [tid for tid, _ in replications(fake, file_id)])
    assert fake.held(TARGET, BETA_ID) == [[0, FINAL]]
    assert [s for s in ticker.slept[since:] if s > SETTINGS["poll_s"]] == []
