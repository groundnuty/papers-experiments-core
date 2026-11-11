"""A complete reset at scale: QoS requirements, running transfers, bulk eviction and the ended-transfer history, each
verified, timed per stage, and failed closed (O01, O07, O08, O09, O11)."""

import threading

import pytest
from fake_reset import FakeReset
from placement_support import ALPHA_ID, BETA_ID, DATA
from platform_support import ADMIN_ENV, BASELINE, SPACE_IDS, SPACE_NAME, USERS

from expcore_onedata import consistency
from expcore_onedata.client import OutOfScope
from expcore_onedata.eviction import evict_ids, evict_tree
from expcore_onedata.provision import NotPlaced, Refused
from expcore_onedata.reset import STAGES, ResetFailed, SpaceReset

FILES = [f"study/part-{i:02d}.bin" for i in range(12)]


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


@pytest.fixture
def platform_file(tmp_path):
    from placement_support import write_with_placement

    return write_with_placement(tmp_path, transfer_settle_s=5.0, transfer_settle_polls=3, concurrency=4)


@pytest.fixture
def stocked(fake) -> FakeReset:
    """Twelve files held by alpha (the data provider) and replicated whole to beta."""
    for path in FILES:
        file_id = fake.add_file(path, DATA)
        fake.blocks[(file_id, BETA_ID)] = [[0, len(DATA)]]
    return fake


def on_beta(fake):
    return [path for path in FILES if fake.held(path, BETA_ID)]


def posted_transfers(fake):
    return [call for call in fake.provider_calls if call[1:] == ("POST", "/transfers")]


# -- bulk eviction -------------------------------------------------------------------------------------------------


def test_o01_a_directory_leaves_a_provider_in_one_transfer_and_every_file_is_verified_on_its_block_map(
    provisioner, stocked
):
    """O01: one eviction of the directory, then every file under it is shown empty on beta by its block map; the
    record counts the files and the one transfer."""
    record = evict_tree(provisioner, "study", "beta")
    assert len(posted_transfers(stocked)) == 1
    assert on_beta(stocked) == []
    assert (record["files"], record["transfers"], record["empty"]) == (len(FILES), 1, True)


def test_o01_a_directory_eviction_reported_completed_that_removed_nothing_is_not_empty(provisioner, stocked):
    """O01: the transfer ends as completed and the blocks stay; the block maps, not the status, fail the eviction."""
    stocked.evictions_remove_blocks = False
    with pytest.raises(NotPlaced, match=f"{len(FILES)} of {len(FILES)} file"):
        evict_tree(provisioner, "study", "beta")


def test_o07_many_files_are_evicted_in_parallel_and_verified_together_not_in_turn(provisioner, stocked, ticker):
    """O07: twelve files, their transfers waited on together and their maps polled together: the waits are shared, so
    the clock moves a few polls in all, where one file after another would wait at least a poll each."""
    record = evict_ids(provisioner, [stocked.id_of(p) for p in FILES], "beta")
    assert on_beta(stocked) == []
    assert record["transfers"] == len(FILES)
    assert len(ticker.slept) <= 3
    maps = [c for c in stocked.provider_calls if c[2].endswith("/distribution")]
    assert len(maps) == len(FILES) * provisioner.settings.consecutive_polls  # each file read just enough times


def test_o07_a_bulk_operation_never_has_more_requests_in_flight_than_its_bound():
    """O07: the bound is a ceiling on the calls running at once, which it reaches (the first four calls wait for each
    other, so they cannot pass one at a time), and every result comes back in the keys' order."""
    running, peak, lock = [0], [0], threading.Lock()
    together = threading.Barrier(4, timeout=10)

    def call(key):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        if key < 4:
            together.wait()
        with lock:
            running[0] -= 1
        return key * 2

    assert consistency.bounded_map(call, list(range(40)), 4) == [k * 2 for k in range(40)]
    assert peak[0] == 4


def test_o09_a_bulk_eviction_from_the_data_provider_is_refused_before_anything_is_sent(provisioner, stocked):
    """O09: by name, for a directory and for files alike; no transfer leaves."""
    with pytest.raises(Refused, match="own copy"):
        evict_tree(provisioner, "study", "alpha")
    with pytest.raises(Refused, match="own copy"):
        evict_ids(provisioner, [stocked.id_of(FILES[0])], "alpha")
    assert posted_transfers(stocked) == []
    assert all(stocked.held(path, ALPHA_ID) for path in FILES)


# -- the reset's stages ----------------------------------------------------------------------------------------------


def test_o11_a_clean_space_resets_with_every_stage_verified_and_timed(provisioner, stocked, ticker):
    """O11: no requirement, no running job: each stage passes, the files leave beta, and the record has each stage's
    seconds on the provisioner's clock, the transfer stage at least the settle time it waits."""
    record = SpaceReset(provisioner).run({"beta": ["study"]})
    assert record["ok"] is True
    assert list(record["stages"]) == list(STAGES)
    assert all(isinstance(stage["seconds"], float) for stage in record["stages"].values())
    assert record["stages"]["transfers"]["seconds"] >= 5.0
    assert record["seconds"] >= sum(stage["seconds"] for stage in record["stages"].values()) - 0.01
    assert record["stages"]["eviction"]["providers"]["providers[1]"]["files"] == len(FILES)
    assert on_beta(stocked) == []


def test_o11_every_qos_requirement_in_the_space_is_found_removed_and_shown_gone(provisioner, stocked):
    """O11: requirements on a file, on a directory and on the root are found from the summaries of the root and the
    leaves, removed through the gate, each answers 404 after, and a second walk finds none."""
    for where in (FILES[3], "study"):
        stocked.add_qos(where)
    stocked.qos["qos-root"] = {"qosRequirementId": "qos-root", "fileId": "root-0001", "expression": "anyStorage"}
    record = SpaceReset(provisioner).run()
    assert stocked.qos == {}
    assert record["stages"]["qos"]["found"] == record["stages"]["qos"]["removed"] == 3
    deletes = [c for c in stocked.provider_calls if c[1] == "DELETE" and c[2].startswith("/qos_requirements/")]
    assert len(deletes) == 3


def test_o11_a_removal_the_provider_accepts_and_ignores_fails_the_reset_and_runs_no_later_stage(provisioner, stocked):
    """O11: a 204 is not believed: the requirement still answers, so the reset fails at its qos stage, the record
    holds that stage with its seconds, and no transfer listing is read after it."""
    stocked.add_qos(FILES[0])
    stocked.ignore_qos_deletes = True
    with pytest.raises(ResetFailed, match="qos stage") as caught:
        SpaceReset(provisioner).run({"beta": ["study"]})
    assert caught.value.stage == "qos"
    assert caught.value.record["stages"]["qos"]["ok"] is False
    assert "seconds" in caught.value.record["stages"]["qos"]
    assert not [c for c in stocked.provider_calls if c[2].endswith("/transfers") and c[1] == "GET"]
    assert all(stocked.held(path, BETA_ID) for path in FILES)  # nothing was evicted


def test_o09_a_requirement_id_that_is_not_one_plain_segment_fails_the_reset_before_any_removal(provisioner, stocked):
    """O09: an id from a summary becomes a path of the next calls, so one with a separator is refused as unreadable
    and nothing is removed, not even the plain requirements beside it."""
    stocked.add_qos(FILES[0])
    stocked.qos["zz/inner"] = {"qosRequirementId": "zz/inner", "fileId": "root-0001", "expression": "anyStorage"}
    with pytest.raises(ResetFailed, match="qos stage"):
        SpaceReset(provisioner).run()
    assert len(stocked.qos) == 2
    assert not [c for c in stocked.provider_calls if c[1] == "DELETE"]


def test_o11_a_running_transfer_is_cancelled_and_two_observations_then_show_none(provisioner, stocked, ticker):
    """O11: a replication the agent left running is cancelled, reports cancelled, and the listings of both providers
    show nothing waiting or ongoing twice in a row; it never placed its blocks."""
    stocked.add_file("other/left.bin", DATA)
    tid = stocked.add_transfer("replication", "other/left.bin", BETA_ID, scheduled=ticker.now, ended=False)
    record = SpaceReset(provisioner).run()
    assert stocked.transfers[tid].ended
    assert record["stages"]["transfers"] == {**record["stages"]["transfers"], "cancelled": 1, "observations": 3}
    assert stocked.held("other/left.bin", BETA_ID) == []


def test_o07_a_job_the_listing_shows_late_is_caught_by_a_later_observation(provisioner, stocked, ticker):
    """O07: a job the listings leave out of their first answers is found by a later observation and cancelled
    before two clean observations end the stage."""
    stocked.add_file("other/late.bin", DATA)
    tid = stocked.add_transfer("replication", "other/late.bin", BETA_ID, scheduled=ticker.now, ended=False)
    stocked.listing_lag[tid] = 2  # the waiting and ongoing listings of the first observation miss it
    record = SpaceReset(provisioner).run()
    assert record["stages"]["transfers"]["cancelled"] == 1
    assert stocked.transfers[tid].cancelled


def test_o11_a_cancel_the_provider_ignores_fails_the_reset_at_its_transfer_stage(provisioner, stocked, ticker):
    """O11: the cancel answers 204 and the job goes on; it never reports an ended state within the bound, so the
    reset fails at its transfers stage and evicts nothing."""
    stocked.add_file("other/stuck.bin", DATA)
    stocked.add_transfer("replication", "other/stuck.bin", BETA_ID, scheduled=ticker.now, ended=False)
    stocked.ignore_cancels, stocked.transfer_flip_after = True, 10**6
    with pytest.raises(ResetFailed, match="transfers stage") as caught:
        SpaceReset(provisioner).run({"beta": ["study"]})
    assert list(caught.value.record["stages"]) == ["qos", "transfers"]
    assert all(stocked.held(path, BETA_ID) for path in FILES)


def test_o08_the_ended_history_is_recorded_with_its_count_and_newest_finish_and_never_removed(
    provisioner, stocked, ticker
):
    """O08: ended transfers cannot be removed, so the record says what the listing shows the next agent, per provider
    and together: two earlier jobs and the reset's own eviction, the newest finish among them."""
    stocked.add_transfer("replication", FILES[0], BETA_ID, scheduled=100.0, finished=150.0)
    stocked.add_transfer("replication", FILES[1], BETA_ID, scheduled=200.0, finished=260.0)
    record = SpaceReset(provisioner).run({"beta": ["study"]})
    history = record["stages"]["history"]
    assert history["removable"] is False
    assert history["ended"] == 3  # the two, and the reset's one eviction of the directory
    assert history["newest_finish"] >= 260.0
    assert set(history["providers"]) == {"providers[0]"}
    assert not [c for c in stocked.provider_calls if c[1] == "DELETE" and c[2].startswith("/transfers/")]


def test_o08_an_ended_record_without_a_finish_time_fails_the_reset_rather_than_guess_one(provisioner, stocked):
    """O08: a history that cannot be read is unknown, never zero or partial: the reset fails at its history stage."""
    stocked.add_transfer("replication", FILES[0], BETA_ID, scheduled=100.0)
    stocked.transfer_omit = {"finishTime"}
    with pytest.raises(ResetFailed, match="history stage") as caught:
        SpaceReset(provisioner).run()
    assert caught.value.record["stages"]["history"]["ok"] is False


def test_o09_a_reset_that_would_evict_the_data_providers_copy_is_refused_before_any_stage(provisioner, stocked):
    """O09: the plan is checked first, so a refused plan changes nothing: no requirement removed, no listing read."""
    stocked.add_qos(FILES[0])
    with pytest.raises(Refused, match="own copy"):
        SpaceReset(provisioner).run({"alpha": ["study"]})
    assert stocked.qos
    assert stocked.provider_calls == []


# -- the gate of a cancel and of a removal -----------------------------------------------------------------------------


def test_o09_a_cancel_is_scoped_by_the_space_its_transfer_record_names(client, stocked, ticker):
    """O09: the gate reads the record's spaceId and its live name before the cancel; outside the prefixes, an unknown
    job, or a record without the field: refused, and no DELETE leaves."""
    tid = stocked.add_transfer("replication", FILES[0], BETA_ID, scheduled=ticker.now, ended=False)
    stocked.transfer_flip_after = 10**6  # the gate's reads of the record do not end the job
    stocked.space_name = "elsewhere"
    with pytest.raises(OutOfScope, match="configured prefixes"):
        client.mutate("DELETE", f"/transfers/{tid}", provider="alpha")
    stocked.space_name = SPACE_NAME
    with pytest.raises(OutOfScope, match="unknown to its provider"):
        client.mutate("DELETE", "/transfers/tid-unknown", provider="alpha")
    stocked.transfer_omit = {"spaceId"}
    with pytest.raises(OutOfScope, match="holds no spaceId"):
        client.mutate("DELETE", f"/transfers/{tid}", provider="alpha")
    assert not [c for c in stocked.provider_calls if c[1] == "DELETE"]
    stocked.transfer_omit = set()
    client.mutate("DELETE", f"/transfers/{tid}", provider="alpha")
    assert stocked.transfers[tid].cancelled


def test_o09_a_qos_removal_is_scoped_by_the_file_its_requirement_names(client, stocked):
    """O09: the gate reads the requirement's fileId and that file's space; a requirement in a space outside the
    prefixes is not removed."""
    stocked.add_data_space(SPACE_IDS[2], "unrelated-space", [ALPHA_ID])
    stocked.add_file("foreign.bin", b"keep", space=SPACE_IDS[2])
    foreign = stocked.add_qos("foreign.bin", space=SPACE_IDS[2])
    with pytest.raises(OutOfScope, match="configured prefixes"):
        client.mutate("DELETE", f"/qos_requirements/{foreign}", provider="alpha")
    assert foreign in stocked.qos
    mine = stocked.add_qos(FILES[0])
    client.mutate("DELETE", f"/qos_requirements/{mine}", provider="alpha")
    assert mine not in stocked.qos
