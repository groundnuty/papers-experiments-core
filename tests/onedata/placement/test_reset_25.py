"""The reset and bulk eviction on Onedata 25.0 as measured: an eviction that evicted nothing is scheduled again and
judged by the block map alone, a provider that does not see an entry yet is waited for, absence is the one rule
`answered_absent`, and a cancel the gate never sent is never taken for an ended job (O01, O07, O09, O11)."""

import pytest
from fake_reset import FakeReset
from placement_support import BETA_ID, DATA, with_placement
from platform_support import ADMIN_ENV, BASELINE, SPACE_NAME, USERS, write_platform

from expcore_onedata.eviction import evict_ids, evict_tree
from expcore_onedata.provision import EVICTION_ATTEMPTS, NotPlaced
from expcore_onedata.reset import ResetFailed, SpaceReset

FILES = [f"study/part-{i:02d}.bin" for i in range(4)]


@pytest.fixture
def fake(monkeypatch, ticker) -> FakeReset:
    """The federation with a reset's calls: alpha holds the data, beta supports the space too."""
    federation = FakeReset(space_name=SPACE_NAME)
    federation.clock = ticker.clock
    federation.add_provider(BETA_ID, "beta.test")
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    for login, (password, user_id) in USERS.items():
        federation.add_user(login, password, user_id)
        federation.grant(user_id, BASELINE)
    return federation


def document(data):
    with_placement(data, transfer_settle_s=5.0, transfer_settle_polls=3, concurrency=4)
    data["support"]["providers"] = ["alpha", "beta"]


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, document)


@pytest.fixture
def stocked(fake) -> FakeReset:
    """Four files held by alpha and replicated whole to beta."""
    for path in FILES:
        fake.blocks[(fake.add_file(path, DATA), BETA_ID)] = [[0, len(DATA)]]
    return fake


def on_beta(fake):
    return [path for path in FILES if fake.held(path, BETA_ID)]


def posted_transfers(fake):
    return [call for call in fake.provider_calls if call[1:] == ("POST", "/transfers")]


# -- an eviction that evicted nothing ----------------------------------------------------------------------------------


def test_o01_a_directory_eviction_that_evicted_nothing_is_scheduled_again_and_judged_by_the_block_map(
    provisioner, stocked
):
    """O01: the directory's first eviction completes with filesEvicted 0 and removes nothing, as 25.0's can right
    after a placement; the block maps still show bytes, so the directory's eviction is scheduled again, and the second
    empties beta. The record says it took two attempts and two transfers."""
    stocked.evictions_noop_first = 1
    record = evict_tree(provisioner, "study", "beta")
    assert on_beta(stocked) == []
    assert (record["attempts"], record["transfers"], record["empty"]) == (2, 2, True)
    assert len(posted_transfers(stocked)) == 2


def test_o01_files_still_holding_bytes_after_a_bulk_eviction_are_scheduled_again_alone(provisioner, stocked):
    """O01: one file's eviction evicted nothing; only that file is scheduled again, the others' maps having shown
    them empty, and every file ends empty on beta."""
    stocked.evictions_noop_first = 1
    record = evict_ids(provisioner, [stocked.id_of(p) for p in FILES], "beta")
    assert on_beta(stocked) == []
    assert (record["attempts"], record["transfers"]) == (2, len(FILES) + 1)


def test_o01_an_eviction_the_block_map_never_shows_done_fails_after_the_last_attempt(provisioner, stocked):
    """O01: every eviction completes and leaves the blocks; the directory is scheduled `EVICTION_ATTEMPTS` times in
    all, and then the eviction is refused, never believed from the transfers' state."""
    stocked.evictions_remove_blocks = False
    with pytest.raises(NotPlaced, match=f"{len(FILES)} of {len(FILES)} file"):
        evict_tree(provisioner, "study", "beta")
    assert len(posted_transfers(stocked)) == EVICTION_ATTEMPTS


def test_o11_a_reset_whose_eviction_evicted_nothing_at_first_still_ends_with_beta_empty(provisioner, stocked):
    """O11: the reset's eviction stage follows the same rule, and records the attempts per provider."""
    stocked.evictions_noop_first = 1
    record = SpaceReset(provisioner).run({"beta": ["study"]})
    assert on_beta(stocked) == []
    assert record["stages"]["eviction"]["providers"]["providers[1]"]["attempts"] == 2


# -- visibility --------------------------------------------------------------------------------------------------------


def test_o07_a_directory_the_evicting_provider_does_not_see_yet_is_waited_for_not_refused(provisioner, stocked, ticker):
    """O07: beta answers the directory with 400 posix enoent for its first reads, as a second provider of 25.0 does
    for some seconds after a write elsewhere; the eviction waits, bounded, until beta sees it, then evicts."""
    directory = stocked.id_of("study")
    stocked.unseen[(BETA_ID, directory)] = 3
    stocked.unseen_answer = (400, "posix", "enoent")
    record = evict_tree(provisioner, "study", "beta")
    assert stocked.unseen[(BETA_ID, directory)] == 0
    assert len(ticker.slept) >= 3
    assert (record["transfers"], on_beta(stocked)) == (1, [])


# -- absence is one rule -----------------------------------------------------------------------------------------------


def test_o11_a_federation_answering_absence_as_25_does_has_its_requirements_removed_and_shown_gone(
    provisioner, stocked
):
    """O11: a removed requirement read on 25.0 answers 400 posix enoent, which is absence, so the qos stage passes
    and the whole reset with it."""
    stocked.answer_missing_as_25()
    stocked.add_qos(FILES[1])
    record = SpaceReset(provisioner).run({"beta": ["study"]})
    assert record["ok"] is True
    assert stocked.qos == {}
    assert record["stages"]["qos"]["removed"] == 1


def test_o11_a_removed_requirement_answered_with_another_400_fails_the_qos_stage_closed(provisioner, stocked):
    """O11: a 400 that is not posix enoent is no absence: the requirement is never shown gone, so the reset fails at
    its qos stage and evicts nothing."""
    stocked.missing_answer = (400, "badValueNotAllowed", None)
    stocked.add_qos(FILES[1])
    with pytest.raises(ResetFailed, match="qos stage") as caught:
        SpaceReset(provisioner).run({"beta": ["study"]})
    assert caught.value.stage == "qos"
    assert all(stocked.held(path, BETA_ID) for path in FILES)


def test_o09_a_cancel_the_gate_refused_unsent_is_never_taken_for_an_ended_job(provisioner, stocked, ticker):
    """O09: the gate's read of the job's record answers a 400, so the cancel is refused unsent (`ScopeUnread`, which
    keeps that 400); the reset never takes it for the 400 a cancel of an ended job answers, and fails at its transfers
    stage with no DELETE sent."""
    stocked.add_file("other/left.bin", DATA)
    tid = stocked.add_transfer("replication", "other/left.bin", BETA_ID, scheduled=ticker.now, ended=False)
    stocked.statuses = [[f"/transfers/{tid}", 1, 400]]
    with pytest.raises(ResetFailed, match="transfers stage") as caught:
        SpaceReset(provisioner).run({"beta": ["study"]})
    assert "ScopeUnread" in str(caught.value)
    assert not [c for c in stocked.provider_calls if c[1] == "DELETE" and c[2] == f"/transfers/{tid}"]
    assert not stocked.transfers[tid].ended
