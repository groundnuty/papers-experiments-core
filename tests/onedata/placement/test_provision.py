"""Provisioning: placement confirmed by the block map (O01), bounded waits (O07) and scoped mutations (O09)."""

import pytest
from placement_support import ALPHA_ID, BETA_ID, DATA

from expcore_onedata import consistency
from expcore_onedata.client import PlatformError
from expcore_onedata.provision import NotPlaced, ProvisionError, Refused

PATH = "study/data.bin"


def mutating(fake):
    """The provider calls that could change state."""
    return [call for call in fake.provider_calls if call[1] in ("POST", "PUT", "DELETE")]


def test_o01_a_replication_reported_completed_with_zero_blocks_is_not_placed(provisioner, seeded):
    """O01: a job that ends as completed while the block map shows nothing is refused, not believed."""
    seeded.transfers_place_blocks = False
    with pytest.raises(NotPlaced) as raised:
        provisioner.replicate(PATH, "beta")
    assert [t.ended for t in seeded.transfers.values()] == [True]  # the fake did report the job as ended
    assert raised.value.report.bytes_short == len(DATA)
    assert seeded.held(PATH, BETA_ID) == []


def test_o01_a_replication_is_placed_once_the_block_map_covers_the_file(provisioner, seeded):
    """O01: success needs the map on the receiving provider to cover the range, on consecutive polls."""
    seeded.fetch_lag = 3  # the blocks show only after three more reads of the map
    record = provisioner.replicate(PATH, "beta")
    assert record["placement"]["fully_placed"] is True
    assert record["placement"]["held_bytes"] == len(DATA)
    assert seeded.held(PATH, BETA_ID) == [[0, len(DATA)]]
    assert record["transfer_states"] == {"completed": 1}


def test_o01_an_eviction_reported_completed_that_removed_nothing_is_not_empty(provisioner, seeded):
    """O01: an eviction is confirmed by the block map showing nothing held, not by its status."""
    provisioner.replicate(PATH, "beta")
    seeded.evictions_remove_blocks = False
    with pytest.raises(NotPlaced):
        provisioner.evict(PATH, "beta")
    seeded.evictions_remove_blocks = True
    assert provisioner.evict(PATH, "beta")["empty"] is True
    assert seeded.held(PATH, BETA_ID) == []


def test_o07_a_transfer_the_provider_does_not_show_yet_is_waited_for(provisioner, seeded, ticker):
    """O07: a job found only after some polls, and still running for more, counts once it has ended."""
    seeded.transfer_visible_after = 3
    seeded.transfer_flip_after = 2
    provisioner.replicate(PATH, "beta")
    assert len(ticker.slept) >= 5


def test_o07_a_transfer_that_never_ends_raises_at_the_bound(provisioner, seeded, ticker):
    """O07: the wait for a job ends at its timeout, naming the job as not ended; the clock never passes the bound."""
    seeded.transfer_flip_after = 10**6
    began = ticker.now
    with pytest.raises(consistency.WaitTimeout, match="had not ended"):
        provisioner.replicate(PATH, "beta")
    assert ticker.now - began <= 30.0 + 1.0
    assert seeded.held(PATH, BETA_ID) == []


def test_o07_a_new_space_root_that_answers_enoent_is_waited_out(provisioner, seeded, ticker):
    """O07: the first answers for a fresh space's root are "no such file"; the root arrives within the bound."""
    seeded.root_enoent_polls = 3
    assert provisioner.root() == "root-0001"
    assert len(ticker.slept) == 3


def test_o07_a_space_root_that_never_arrives_raises_at_the_bound(provisioner, seeded, ticker):
    """O07: after the last of the bounded tries the 404 is raised, and no other status is ever waited out."""
    seeded.root_enoent_polls = 10**6
    with pytest.raises(PlatformError) as raised:
        provisioner.root()
    assert raised.value.status == 404
    assert len(ticker.slept) == 29


def test_o07_a_space_root_failing_for_another_reason_is_not_waited_out(provisioner, seeded, ticker):
    """O07: only "no such file" is a consistency wait; a server error is raised at once."""
    seeded.statuses.append(["/spaces/", 1, 500])
    with pytest.raises(PlatformError) as raised:
        provisioner.root()
    assert raised.value.status == 500
    assert ticker.slept == []


def test_o09_a_space_whose_live_name_is_outside_the_prefixes_is_refused(provisioner, seeded):
    """O09: the live name is read before anything changes; one outside the prefixes stops every mutation."""
    seeded.space_name = "unrelated-space"
    with pytest.raises(Refused, match="outside the configured prefixes"):
        provisioner.put_file("scratch/a.bin", b"abc")
    with pytest.raises(Refused):
        provisioner.replicate(PATH, "beta")
    with pytest.raises(Refused):
        provisioner.teardown()
    assert mutating(seeded) == []


def test_o09_a_space_renamed_within_the_prefixes_is_still_not_the_configured_one(provisioner, seeded):
    """O09: a live name that differs from the configured one is refused even when it starts with a prefix."""
    seeded.space_name = "study-a-other"
    with pytest.raises(Refused, match="not the configured one"):
        provisioner.put_file("scratch/a.bin", b"abc")
    assert mutating(seeded) == []


def test_o09_the_data_providers_own_copy_is_never_evicted(provisioner, seeded):
    """O09: eviction from the data provider is refused by name before any request leaves."""
    with pytest.raises(Refused, match="own copy"):
        provisioner.evict(PATH, "alpha")
    assert [c for c in seeded.provider_calls if c[2] == "/transfers"] == []
    assert seeded.held(PATH, ALPHA_ID) == [[0, len(DATA)]]


def test_o09_the_data_providers_copy_is_refused_by_id_under_another_name(client, config, placement, seeded, tmp_path):
    """O09: a second name for the same provider id cannot be used to evict the holder's copy."""
    from expcore_onedata.provision import Provisioner

    ids = {"alpha": ALPHA_ID, "beta": ALPHA_ID}
    provisioner = Provisioner(client, config, placement.settings, provider_ids=ids, log=tmp_path / "log.jsonl")
    with pytest.raises(Refused, match="own copy"):
        provisioner.evict(PATH, "beta")
    assert [c for c in seeded.provider_calls if c[2] == "/transfers"] == []


def test_o09_teardown_deletes_exactly_what_it_created(provisioner, seeded):
    """O09: only logged ids are deleted; the study data beside them is untouched, and the report is clean."""
    file_id = provisioner.put_file("scratch/run/a.bin", DATA)
    assert seeded.exists("scratch/run/a.bin")
    report = provisioner.teardown()
    assert report.ok
    assert file_id in report.deleted
    assert not seeded.exists("scratch/run/a.bin")
    assert not seeded.exists("scratch")
    assert seeded.exists(PATH)
    deleted_ids = {c[2] for c in seeded.provider_calls if c[1] == "DELETE"}
    assert deleted_ids == {f"/data/{fid}" for fid in report.deleted}
    assert provisioner.logged() == []


def test_o09_teardown_reports_files_it_finds_left_and_deletes_nothing_it_did_not_create(provisioner, seeded):
    """O09: a directory holding a file nobody logged is left, with its reason, and the report is not ok."""
    provisioner.put_file("scratch/a.bin", DATA)
    seeded.add_file("scratch/foreign.bin", b"keep me")
    report = provisioner.teardown()
    assert not report.ok
    assert [entry["path"] for entry in report.left] == ["scratch"]
    assert "did not create" in report.left[0]["reason"]
    assert seeded.exists("scratch/foreign.bin")
    assert not seeded.exists("scratch/a.bin")
    with pytest.raises(ProvisionError, match="left"):
        report.raise_if_left()


def test_o09_teardown_reports_a_delete_that_failed(provisioner, seeded):
    """O09: a delete answered with a server error leaves the entry, and the teardown says so."""
    provisioner.put_file("scratch/a.bin", DATA)
    seeded.fail_deletes = True
    report = provisioner.teardown()
    assert not report.ok
    assert {entry["reason"] for entry in report.left} >= {"the delete was refused or failed"}
    assert seeded.exists("scratch/a.bin")
    assert [e["id"] for e in provisioner.logged()]  # still logged, so a later teardown can finish it


def test_o09_teardown_reports_a_file_still_listed_after_a_delete_that_was_accepted(provisioner, seeded):
    """O09: a delete answered 204 is not believed; the entry must be gone on consecutive reads, or it is left."""
    provisioner.put_file("scratch/a.bin", DATA)
    seeded.ignore_deletes = True
    report = provisioner.teardown()
    assert not report.ok
    assert any("still listed" in entry["reason"] for entry in report.left)


def test_o09_put_file_refuses_to_adopt_a_file_that_exists(provisioner, seeded):
    """O09: a file already at the path is refused, so teardown can never delete something it did not create."""
    with pytest.raises(ProvisionError, match="already exists"):
        provisioner.put_file(PATH, b"x")
    assert mutating(seeded) == []


def test_put_file_uploads_in_blocks_and_verifies_the_size(provisioner, seeded):
    """The content goes in blocks of the configured size, and the size read back must equal it."""
    provisioner.put_file("scratch/a.bin", DATA)
    puts = [c for c in seeded.provider_calls if c[1] == "PUT"]
    assert len(puts) == 4
    assert bytes(seeded.nodes[seeded.id_of("scratch/a.bin")].content) == DATA


def test_o09_an_id_with_an_equals_sign_reaches_the_gate_unescaped(provisioner, seeded):
    """O09: the gate refuses an escape in a mutation path, so ids are sent as they are and never percent-encoded."""
    seeded.id_suffix = "=="
    provisioner.put_file("scratch/a.bin", DATA)
    assert provisioner.teardown().ok
    assert all("%" not in route for _, _, route in seeded.provider_calls)
    assert any(route.endswith("==/content") for _, method, route in seeded.provider_calls if method == "PUT")


def test_o09_an_id_the_gate_would_refuse_is_refused_at_the_source(provisioner, seeded):
    """O09: an id outside the plain charset is not turned into an escaped path; nothing is sent for it."""
    seeded.id_suffix = "%2F"
    with pytest.raises(PlatformError, match="plain path segment"):
        provisioner.put_file("scratch/a.bin", DATA)
    assert mutating(seeded) == [("prmock0001", "POST", "/data/root-0001/children")]


def test_a_path_that_climbs_out_of_the_space_is_refused(provisioner, seeded):
    """A path with a parent reference or an absolute start never reaches a request."""
    for bad in ("../x", "/abs", "a//b", "a/./b", ""):
        with pytest.raises(ValueError, match="relative to the space root"):
            provisioner.put_file(bad, b"x")
    assert mutating(seeded) == []
