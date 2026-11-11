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


def test_o01_an_eviction_is_not_confirmed_by_a_block_map_that_gives_no_blocks(provisioner, seeded):
    """O01 and O08: the provider answers without a blocks field after the eviction, which is unknown and not empty,
    so the eviction is not confirmed; once the field is back and shows nothing held, it is."""
    provisioner.replicate(PATH, "beta")
    seeded.omit_blocks = True
    with pytest.raises(NotPlaced):
        provisioner.evict(PATH, "beta")
    seeded.omit_blocks = False
    assert provisioner.evict(PATH, "beta")["empty"] is True


def test_o01_an_eviction_is_not_confirmed_by_a_block_map_that_leaves_the_provider_out(provisioner, seeded):
    """O01 and O08: after the eviction the answer does not list the provider at all; absence is unknown, not empty,
    so the eviction is not confirmed until the provider is listed with an empty list of blocks."""
    provisioner.replicate(PATH, "beta")
    seeded.omit_providers = {BETA_ID}
    with pytest.raises(NotPlaced):
        provisioner.evict(PATH, "beta")
    seeded.omit_providers = set()
    assert provisioner.evict(PATH, "beta")["empty"] is True


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


def test_o09_every_provisioning_path_is_walked_never_asks_for_a_path_and_uploads_octets(
    config, placement, seeded, ticker, tmp_path
):
    """O09: put, fetch, replicate, evict and teardown each pass the client's gate, which finds a file's space by its
    parent links. No request of any of them asks a provider for the path attribute 25.0 does not have (every value of
    every `attribute` parameter is read, not only the last), the walk reads parent links, each mutation and range read
    comes right after the gate's read of the space's name, and every content upload is marked an octet stream."""
    import httpx

    from expcore_onedata.client import OnezoneClient
    from expcore_onedata.provision import Provisioner, resolve_provider_ids

    sent = []

    def recording(request):
        sent.append(request)
        return seeded.respond(request)

    client = OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(recording)))
    provisioner = Provisioner(
        client,
        config,
        placement.settings,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    )
    seeded.evictions_remove_blocks = True
    file_id = provisioner.put_file("scratch/a.bin", DATA)
    provisioner.fetch("beta", file_id, 0, 100)
    provisioner.replicate(PATH, "beta")
    provisioner.evict(PATH, "beta")
    assert provisioner.teardown().ok
    assert not [r for r in sent if "path" in r.url.params.get_list("attribute")]
    walked = [r.method for r in sent if r.url.params.get("attribute") == "parent_id"]
    assert walked
    gated = [r for r in sent if r.method in ("POST", "PUT", "DELETE") or r.headers.get("Range")]
    assert {r.method for r in gated} >= {"POST", "PUT", "DELETE", "GET"}  # creates, uploads, transfers, deletes, reads
    for request in gated:  # the gate read the space's name from Onezone right before each one
        previous = sent[sent.index(request) - 1]
        assert (previous.method, previous.url.path) == ("GET", f"/api/v3/onezone/spaces/{config.space.id}")
    uploads = [r for r in sent if r.method == "PUT" and r.url.path.endswith("/content")]
    assert uploads
    assert {r.headers.get("Content-Type") for r in uploads} == {"application/octet-stream"}


@pytest.mark.parametrize("answer", [(404, "notFound", None), (400, "posix", "enoent")])
def test_o07_replicate_and_fetch_wait_until_the_provider_sees_a_file_just_written_elsewhere(
    provisioner, seeded, ticker, answer
):
    """O07: a file just written through the data provider is not yet visible through another for some reads. Before a
    transfer is scheduled through that provider, and before a range is read through it, the provisioner waits until
    the provider reports the file at its size; it checks each provider and file once, so a second read waits for
    nothing."""
    seeded.unseen_after_create = {BETA_ID: 3}
    seeded.unseen_answer = answer
    file_id = provisioner.put_file("scratch/a.bin", DATA)
    assert provisioner.fetch("beta", file_id, 0, 100) == DATA[:100]
    slept = len(ticker.slept)
    assert slept >= 3
    provisioner.fetch("beta", file_id, 100, 100)
    assert len(ticker.slept) == slept  # no second wait
    other = provisioner.put_file("scratch/b.bin", DATA)
    provisioner.replicate("scratch/b.bin", "beta")
    assert seeded.held("scratch/b.bin", BETA_ID) == [[0, len(DATA)]]
    assert seeded.unseen[(BETA_ID, other)] == 0


def evictions(fake):
    return [t for t in fake.transfers.values() if t.kind == "eviction"]


def test_o01_an_eviction_that_completes_with_nothing_evicted_is_scheduled_again_until_the_map_is_empty(
    provisioner, seeded, ticker
):
    """O01: on Onedata 25.0 an eviction scheduled right after a provider fetched the bytes completes with filesEvicted
    0 and removes nothing, whichever provider schedules it; the same eviction a little later works. The block map is
    the evidence, so the eviction is scheduled again after a pause while the provider still holds bytes, bounded by
    `EVICTION_ATTEMPTS`, and the record says how many it took."""
    provisioner.replicate(PATH, "beta")
    seeded.evictions_noop_first = 2
    record = provisioner.evict(PATH, "beta")
    assert record["empty"] is True
    assert record["attempts"] == 3
    assert seeded.held(PATH, BETA_ID) == []
    assert [t.noop for t in evictions(seeded)] == [True, True, False]


def test_o01_an_eviction_that_never_removes_anything_fails_closed_after_its_attempts(provisioner, seeded):
    """O01: a provider whose evictions all complete and remove nothing is never reported empty: after
    `EVICTION_ATTEMPTS` evictions the block map still shows the bytes, and the eviction raises `NotPlaced`."""
    from expcore_onedata.provision import EVICTION_ATTEMPTS

    provisioner.replicate(PATH, "beta")
    seeded.evictions_remove_blocks = False
    with pytest.raises(NotPlaced):
        provisioner.evict(PATH, "beta")
    assert len(evictions(seeded)) == EVICTION_ATTEMPTS
    assert seeded.held(PATH, BETA_ID) == [[0, len(DATA)]]


OTHER_400S = [(400, "posix", "eacces"), (400, "posix", None), (400, "badValueNotAllowed", None), (400, None, None)]


@pytest.mark.parametrize("answer", [(404, "posix", "enoent"), (400, "posix", "enoent")])
def test_o07_a_fresh_space_root_answered_404_or_400_posix_enoent_is_waited_out(provisioner, seeded, ticker, answer):
    """O07: a fresh space's root that is not there yet, answered with a 404 or, as Onedata 25.0 answers it, with 400
    posix enoent, is waited out until it arrives within the bound."""
    seeded.root_enoent_polls = 3
    seeded.root_enoent_answer = answer
    assert provisioner.root() == "root-0001"
    assert len(ticker.slept) == 3


@pytest.mark.parametrize("answer", OTHER_400S)
def test_o07_a_space_root_answered_any_other_400_is_raised_at_once_and_never_waited_for(
    provisioner, seeded, ticker, answer
):
    """O07: any other 400 (another errno, none, another id, a body that is no error object) is no "not there yet":
    the root read fails closed at once, never waited out as if the root were coming."""
    seeded.root_enoent_polls = 3
    seeded.root_enoent_answer = answer
    with pytest.raises(PlatformError) as raised:
        provisioner.root()
    assert raised.value.status == 400
    assert ticker.slept == []


@pytest.mark.parametrize("answer", [(404, "notFound", None), (400, "posix", "enoent")])
def test_o07_a_transfer_not_seen_yet_answered_404_or_400_posix_enoent_is_waited(provisioner, seeded, ticker, answer):
    """O07: a job its provider does not show yet, answered with a 404 or 400 posix enoent, is waited for like a running
    one, and counts once it has ended."""
    seeded.transfer_visible_after = 3
    seeded.transfer_unseen_answer = answer
    provisioner.replicate(PATH, "beta")
    assert len(ticker.slept) >= 3


@pytest.mark.parametrize("answer", OTHER_400S)
def test_o07_a_transfer_answered_any_other_400_fails_closed_and_is_never_waited(provisioner, seeded, ticker, answer):
    """O07: any other 400 for a job is no "not visible yet": the wait raises at once instead of polling on."""
    seeded.transfer_visible_after = 3
    seeded.transfer_unseen_answer = answer
    with pytest.raises(PlatformError) as raised:
        provisioner.replicate(PATH, "beta")
    assert raised.value.status == 400
    assert ticker.slept == []


@pytest.mark.parametrize(("answer", "gone"), [((400, "posix", "enoent"), True), *((a, False) for a in OTHER_400S)])
def test_o11_the_data_providers_read_shows_a_file_gone_on_400_posix_enoent_and_on_no_other_400(
    provisioner, seeded, answer, gone
):
    """O11: the read teardown confirms absence with says gone on a 404 or exactly 400 posix enoent. Any other 400 is no
    absence: the read raises, so a poll can never count it as gone."""
    file_id = provisioner.put_file("scratch/a.bin", DATA)
    seeded.answer_missing_as_25(answer)
    seeded._delete(file_id)
    if gone:
        assert provisioner._gone(file_id) is True
    else:
        with pytest.raises(PlatformError):
            provisioner._gone(file_id)


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


def test_o11_a_deletion_is_logged_only_after_absence_is_verified_so_a_second_teardown_checks_again(provisioner, seeded):
    """O11 with O09: a DELETE that answered 204 and left the entry is not a deletion. The creation record stays, the
    second teardown does not report ok without deleting and verifying the entry again, and once the provider really
    removes it the log is empty."""
    file_id = provisioner.put_file("scratch/a.bin", DATA)
    seeded.ignore_deletes = True
    first = provisioner.teardown()
    assert not first.ok
    assert file_id not in first.deleted
    assert file_id in [line["id"] for line in provisioner.logged()]  # no deletion line was written
    deletes_before = len([c for c in seeded.provider_calls if c[1] == "DELETE"])
    second = provisioner.teardown()
    assert not second.ok  # not returned as ok without checking: the entry is still there
    assert len([c for c in seeded.provider_calls if c[1] == "DELETE"]) > deletes_before  # and was deleted again
    seeded.ignore_deletes = False
    third = provisioner.teardown()
    assert third.ok
    assert file_id in third.deleted
    assert provisioner.logged() == []
    assert not seeded.exists("scratch/a.bin")


def test_o09_a_content_upload_says_it_is_an_octet_stream_so_the_release_takes_it(provisioner, seeded):
    """O09: Onedata 25.0 refuses a raw body sent without a Content-Type (415), as the fake does. The provisioner's
    upload goes through the client's mutation, which marks every raw body application/octet-stream, so each block is
    taken and the file arrives whole."""
    file_id = provisioner.put_file("scratch/a.bin", DATA)
    assert bytes(seeded.nodes[file_id].content) == DATA
    assert len([c for c in seeded.provider_calls if c[1] == "PUT"]) == 4


@pytest.mark.parametrize(
    ("answer", "gone"),
    [
        ((404, "notFound", None), True),
        ((400, "posix", "enoent"), True),  # how Onedata 25.0 answers a deleted entry
        ((400, "posix", "eacces"), False),  # the same id with another errno
        ((400, "posix", None), False),  # the id with no errno
        ((400, "badValueNotAllowed", None), False),  # another 400
        ((400, None, None), False),  # a body that is no error object
    ],
)
def test_o11_teardown_takes_a_404_or_400_posix_enoent_as_gone_at_once_and_fails_closed_on_any_other_400(
    provisioner, seeded, ticker, answer, gone
):
    """O11: a deleted entry is gone when the data provider answers it with a 404 or, as Onedata 25.0 does, with 400
    posix enoent: teardown confirms it on consecutive reads and stops at once, without waiting out its bound. Any other
    400, an unreadable body included, is not absence: teardown keeps reading to its bound, deletes nothing from its
    log and reports every entry still listed, never removed, so a later run cannot start on data still there."""
    seeded.answer_missing_as_25(answer)
    provisioner.put_file("scratch/a.bin", DATA)
    began = ticker.clock()
    report = provisioner.teardown()
    assert report.ok is gone
    assert not seeded.exists("scratch")
    if gone:
        assert provisioner.logged() == []
        assert ticker.clock() - began < provisioner.settings.settle_timeout_s  # stopped long before the bound
    else:
        assert report.deleted == ()
        assert {entry["reason"] for entry in report.left} == {"still listed after its delete"}
        assert len(report.left) == 2  # the file and its directory
        assert len(provisioner.logged()) == 2  # both still logged, so a later teardown checks them again
        assert ticker.clock() - began >= provisioner.settings.settle_timeout_s  # waited to the bound, never "gone"
        with pytest.raises(ProvisionError, match="left"):
            report.raise_if_left()


def test_o11_a_logged_entry_someone_else_deleted_reads_as_gone_on_25(provisioner, seeded):
    """O11: an entry deleted before teardown runs is answered 400 posix enoent by its DELETE on Onedata 25.0 (its
    parent link still reads, so the gate lets the DELETE through); that is absence, so teardown confirms and logs it."""
    seeded.answer_missing_as_25()
    provisioner.put_file("scratch/a.bin", DATA)
    seeded._delete(seeded.id_of("scratch"))
    report = provisioner.teardown()
    assert report.ok
    assert provisioner.logged() == []


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
