"""The public `FakeData`'s pending delete: a delete that was answered and is not done yet (`delete_delay_s`).

These are tests of the fake, not of the platform layer. The delay is counted on the fake's own `clock`, which every test
binds to a `Ticker`, so none of them waits. The answers a test pins as recorded are those the fake's docstring and
`docs/interfaces.md` mark so (`DELETE_FACTS`); the ones they mark as assumptions are pinned here too, so a live check
that disproves one has a test to change.
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from expcore_onedata.testing import (
    ALPHA_ID,
    BETA_ID,
    DATA,
    ERRORS_LEGACY,
    PROVIDER,
    ROOT_ID,
    FakeData,
    Ticker,
    make_federation,
)
from expcore_onedata.testing.fake_data import DELETE_FACTS, TOMBSTONE_FACTS, TOMBSTONE_S

HOSTS = {ALPHA_ID: "https://alpha.test", BETA_ID: "https://beta.test"}
TYPED = {"Content-Type": "application/octet-stream"}
GONE_25_0 = {"error": {"id": "posix", "details": {"errno": "enoent"}}}
DELAY_S = 5.0
REPO = Path(__file__).resolve().parents[2]


def call(fake: FakeData, provider: str, method: str, route: str, **options) -> httpx.Response:
    """Send one request to a provider's data API and return the fake's answer."""
    headers = {"X-Auth-Token": fake.admin_token, **options.pop("headers", {})}
    return fake.respond(httpx.Request(method, f"{HOSTS[provider]}{PROVIDER}{route}", headers=headers, **options))


def names(fake: FakeData, provider: str, directory: str = ROOT_ID) -> list[str]:
    return [c["name"] for c in call(fake, provider, "GET", f"/data/{directory}/children").json()["children"]]


def make(fake: FakeData, provider: str, parent: str, name: str, kind: str) -> str:
    made = call(fake, provider, "POST", f"/data/{parent}/children", params={"name": name, "type": kind})
    assert made.status_code == 201
    return made.json()["fileId"]


def write(fake: FakeData, provider: str, file_id: str, body: bytes) -> None:
    done = call(fake, provider, "PUT", f"/data/{file_id}/content", params={"offset": 0}, headers=TYPED, content=body)
    assert done.status_code == 204


def delete(fake: FakeData, provider: str, file_id: str) -> None:
    assert call(fake, provider, "DELETE", f"/data/{file_id}").status_code == 204


def delayed(**options) -> tuple[FakeData, Ticker]:
    """A federation whose deletes land `DELAY_S` seconds of its clock after they are answered."""
    ticker = Ticker()
    fake = make_federation(clock=ticker.clock, **options)
    fake.delete_delay_s = DELAY_S
    return fake, ticker


def absent(fake: FakeData, provider: str, file_id: str) -> bool:
    """Whether the provider answers an entry as 25.0 answers a deleted one, on every read of it."""
    reads = (f"/data/{file_id}", f"/data/{file_id}/content", f"/data/{file_id}/distribution")
    return all(call(fake, provider, "GET", read).json() == GONE_25_0 for read in reads)


def test_by_default_a_delete_is_done_at_once_everywhere_and_a_reused_name_keeps_its_files():
    ticker = Ticker()
    fake = make_federation(clock=ticker.clock)
    assert fake.delete_delay_s == 0.0
    fake.add_file("study/old.bin", DATA)
    study = fake.id_of("study")
    delete(fake, ALPHA_ID, study)
    assert absent(fake, ALPHA_ID, study)
    assert absent(fake, BETA_ID, study)
    assert names(fake, ALPHA_ID) == names(fake, BETA_ID) == []
    fresh = make(fake, ALPHA_ID, ROOT_ID, "study", "DIR")
    kept = make(fake, ALPHA_ID, fresh, "new.bin", "REG")
    write(fake, ALPHA_ID, kept, b"fresh")
    ticker.sleep(1000)
    assert call(fake, ALPHA_ID, "GET", f"/data/{kept}/content").content == b"fresh"  # nothing took it


def test_a_deleted_file_is_gone_where_the_delete_was_sent_and_shown_elsewhere_until_it_lands():
    fake, ticker = delayed()
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    assert absent(fake, ALPHA_ID, file_id)  # recorded: the provider the delete went to answers as for a deleted entry
    parent = call(fake, ALPHA_ID, "GET", f"/data/{file_id}", params={"attribute": "parent_id"})
    assert parent.json() == {"parent_id": ROOT_ID}  # recorded: the deleted entry's parent link still reads
    assert names(fake, ALPHA_ID) == []
    assert not fake.exists("a.bin")
    # an assumption, from the other providers' directory figures following a delete late: they still show the entry
    assert names(fake, BETA_ID) == ["a.bin"]
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}").status_code == 200
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}/content").content == DATA
    ticker.sleep(4)
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}").status_code == 200  # a second short of the delay
    ticker.sleep(1)
    assert absent(fake, BETA_ID, file_id)  # landed: every provider answers it as a deleted entry
    assert names(fake, BETA_ID) == []


def test_a_deleted_directory_pends_with_what_is_under_it():
    fake, ticker = delayed()
    inner = fake.add_file("study/deep/b.bin", DATA)
    study, deep = fake.id_of("study"), fake.id_of("study/deep")
    delete(fake, ALPHA_ID, study)
    assert all(absent(fake, ALPHA_ID, gone) for gone in (study, deep, inner))
    assert names(fake, ALPHA_ID) == []
    assert (names(fake, BETA_ID), names(fake, BETA_ID, study), names(fake, BETA_ID, deep)) == (
        ["study"],
        ["deep"],
        ["b.bin"],
    )
    ticker.sleep(5)
    assert all(absent(fake, BETA_ID, gone) for gone in (study, deep, inner))
    assert names(fake, BETA_ID) == []
    # and each keeps the parent it had, read by `parent_id` alone, which a deleted entry still answers
    assert call(fake, BETA_ID, "GET", f"/data/{inner}", params={"attribute": "parent_id"}).json() == {"parent_id": deep}


def test_a_directory_name_reused_during_its_delete_loses_the_files_made_in_it_when_the_delete_lands():
    fake, ticker = delayed()
    fake.add_file("study/old.bin", DATA)
    old = fake.id_of("study")
    delete(fake, ALPHA_ID, old)
    fresh = make(fake, ALPHA_ID, ROOT_ID, "study", "DIR")  # recorded: the create is accepted, not refused
    assert fresh != old
    kept = make(fake, ALPHA_ID, fresh, "new.bin", "REG")
    write(fake, ALPHA_ID, kept, b"fresh")
    assert call(fake, ALPHA_ID, "GET", f"/data/{kept}/content").content == b"fresh"
    assert names(fake, ALPHA_ID, fresh) == ["new.bin"]
    ticker.sleep(5)
    assert absent(fake, ALPHA_ID, kept)  # recorded: the old delete's late removal takes the new directory's files
    assert absent(fake, BETA_ID, old)
    assert call(fake, ALPHA_ID, "GET", f"/data/{fresh}").status_code == 200  # assumed: the directory itself stays
    assert names(fake, ALPHA_ID, fresh) == []
    assert names(fake, ALPHA_ID) == ["study"]
    assert fake.exists("study")
    assert not fake.exists("study/new.bin")


def test_a_directory_that_loses_files_loses_the_files_at_any_depth_and_keeps_the_directories():
    fake, ticker = delayed()
    fake.add_file("study/x.bin", DATA)
    delete(fake, ALPHA_ID, fake.id_of("study"))
    fresh = make(fake, ALPHA_ID, ROOT_ID, "study", "DIR")
    sub = make(fake, ALPHA_ID, fresh, "sub", "DIR")
    make(fake, ALPHA_ID, sub, "deep.bin", "REG")
    ticker.sleep(5)
    assert names(fake, ALPHA_ID, fresh) == ["sub"]  # assumed: only files go
    assert names(fake, ALPHA_ID, sub) == []


def test_code_that_waits_until_every_provider_answers_absent_keeps_what_it_writes_next():
    fake, ticker = delayed()
    fake.add_file("study/old.bin", DATA)
    old = fake.id_of("study")
    delete(fake, ALPHA_ID, old)
    started = ticker.now
    for _ in range(60):  # a bounded wait: a fake that never lands the delete fails here, it does not spin
        if all(absent(fake, provider, old) for provider in (ALPHA_ID, BETA_ID)):
            break
        ticker.sleep(1)
    assert ticker.now - started == DELAY_S
    fresh = make(fake, ALPHA_ID, ROOT_ID, "study", "DIR")
    kept = make(fake, ALPHA_ID, fresh, "new.bin", "REG")
    write(fake, ALPHA_ID, kept, b"fresh")
    ticker.sleep(1000)
    assert call(fake, ALPHA_ID, "GET", f"/data/{kept}/content").content == b"fresh"


def test_a_file_name_reused_during_its_delete_keeps_the_new_file():
    """The facts record the loss for a directory's name only, so the fake loses nothing of a file's (an assumption)."""
    fake, ticker = delayed()
    old = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, old)
    fresh = make(fake, ALPHA_ID, ROOT_ID, "a.bin", "REG")
    write(fake, ALPHA_ID, fresh, b"fresh")
    ticker.sleep(10)
    assert absent(fake, ALPHA_ID, old)
    assert absent(fake, BETA_ID, old)
    assert call(fake, BETA_ID, "GET", f"/data/{fresh}/content").content == b"fresh"
    assert names(fake, ALPHA_ID) == names(fake, BETA_ID) == ["a.bin"]


def test_a_provider_that_still_shows_the_entry_refuses_its_name_as_it_refuses_any_taken_one():
    """An assumption: the provider still shows the entry, so a create of its name collides there (409), as the fake
    answers for any taken name; the provider the delete went to takes the name (the test above)."""
    fake, ticker = delayed()
    old = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, old)
    refused = call(fake, BETA_ID, "POST", f"/data/{ROOT_ID}/children", params={"name": "a.bin", "type": "REG"})
    assert refused.status_code == 409
    ticker.sleep(5)
    assert make(fake, BETA_ID, ROOT_ID, "a.bin", "REG")  # landed: the name is free


def test_a_second_delete_of_an_entry_still_shown_does_not_move_when_it_lands():
    """An assumption: the second delete, through the provider that still shows the entry, is answered 204 and changes
    nothing; the entry lands when the first said."""
    fake, ticker = delayed()
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    ticker.sleep(3)
    delete(fake, BETA_ID, file_id)
    ticker.sleep(2)
    assert absent(fake, BETA_ID, file_id)


def test_the_delay_is_set_by_the_constructor_and_by_make_federation():
    assert FakeData(delete_delay_s=2.5).delete_delay_s == 2.5
    assert make_federation(delete_delay_s=2.5).delete_delay_s == 2.5


def test_a_pending_delete_lands_in_the_older_shape_as_that_shape_answers_a_missing_entry():
    fake, ticker = delayed(error_shape=ERRORS_LEGACY)
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}").status_code == 404
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}").status_code == 200
    ticker.sleep(5)
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}").status_code == 404


def test_the_facts_the_fake_names_are_files_of_the_repository():
    assert DELETE_FACTS
    assert all((REPO / path).is_file() for path in DELETE_FACTS)


def test_a_delete_that_fails_or_is_ignored_pends_nothing():
    fake, ticker = delayed()
    file_id = fake.add_file("a.bin", DATA)
    fake.ignore_deletes = True
    delete(fake, ALPHA_ID, file_id)
    ticker.sleep(10)
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}").status_code == 200
    fake.ignore_deletes, fake.fail_deletes = False, True
    assert call(fake, ALPHA_ID, "DELETE", f"/data/{file_id}").status_code == 500
    ticker.sleep(10)
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}").status_code == 200


@pytest.mark.parametrize("delay", [0.0, -1.0])
def test_a_delay_of_nothing_or_less_is_no_delay(delay):
    fake = make_federation(clock=Ticker().clock, delete_delay_s=delay)
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    assert absent(fake, ALPHA_ID, file_id)
    assert absent(fake, BETA_ID, file_id)


def test_a_test_can_land_every_delete_under_way_without_moving_the_clock():
    fake, ticker = delayed()
    file_id = fake.add_file("a.bin", DATA)
    inner = fake.add_file("study/b.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    delete(fake, ALPHA_ID, fake.id_of("study"))
    assert call(fake, BETA_ID, "GET", f"/data/{inner}").status_code == 200
    fake.land_deletes()
    assert ticker.now == Ticker().now  # the clock did not move
    for gone in (file_id, inner):
        assert absent(fake, ALPHA_ID, gone)
        assert absent(fake, BETA_ID, gone)
    assert names(fake, BETA_ID) == []


def test_threads_reading_at_once_while_deletes_land_never_see_the_tree_change_under_them():
    """The core's bulk reads run many at once; the first read after a delete is due lands it, in the tree the others
    iterate, so landing and the reads of the tree must be made under the fake's lock."""
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # switch threads as often as the interpreter allows, so an unlocked tree shows it
    try:
        results = []
        for _ in range(6):
            fake, ticker = delayed()
            for k in range(8):
                for i in range(25):
                    fake.add_file(f"d{k}/f{i}.bin", b"x")
            for k in range(8):
                delete(fake, ALPHA_ID, fake.id_of(f"d{k}"))
            ticker.sleep(DELAY_S + 1)

            def read(_: int, fake: FakeData = fake) -> list[str] | str:
                try:
                    return names(fake, BETA_ID)
                except Exception as error:  # the failure under test: whatever the fake raised to a reader
                    return f"{type(error).__name__}: {error}"

            with ThreadPoolExecutor(16) as pool:
                results.extend(pool.map(read, range(48)))
    finally:
        sys.setswitchinterval(interval)
    assert results == [[]] * 6 * 48  # every read saw the directories gone, none raised


@pytest.mark.parametrize("delay", [0.0, DELAY_S])
def test_a_transfer_of_a_file_whose_delete_is_under_way_is_refused_through_its_provider_as_with_no_delay(delay):
    fake = make_federation(clock=Ticker().clock, delete_delay_s=delay)
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    body = {"type": "replication", "fileId": file_id, "replicatingProviderId": BETA_ID}
    refused = call(fake, ALPHA_ID, "POST", "/transfers", json=body)
    assert (refused.status_code, refused.json()["error"]["id"]) == (400, "badValueNotAllowed")
    assert fake.transfers == {}


def test_a_transfer_of_a_file_under_a_directory_whose_delete_is_under_way_is_refused_through_the_deleting_provider():
    fake, ticker = delayed()
    inner = fake.add_file("study/b.bin", DATA)
    delete(fake, ALPHA_ID, fake.id_of("study"))
    body = {"type": "eviction", "fileId": inner, "evictingProviderId": ALPHA_ID}
    assert call(fake, ALPHA_ID, "POST", "/transfers", json=body).status_code == 400
    assert fake.transfers == {}
    # the providers the DELETE did not go to still show the file, so they accept the job until the delete lands
    assert call(fake, BETA_ID, "POST", "/transfers", json=body).status_code == 201
    ticker.sleep(DELAY_S)
    assert call(fake, BETA_ID, "POST", "/transfers", json=body).status_code == 400


def test_a_delay_with_the_clock_a_fake_starts_with_is_refused_at_the_first_delete_that_would_pend():
    fake = make_federation(delete_delay_s=DELAY_S)  # no clock bound: it would never move, and no delete would land
    file_id = fake.add_file("a.bin", DATA)
    with pytest.raises(ValueError, match=r"delete_delay_s.*never moves.*make_federation\(clock=ticker\.clock\)"):
        delete(fake, ALPHA_ID, file_id)
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}").status_code == 200  # nothing was left pending
    ticker = Ticker()
    fake.clock = ticker.clock  # the delay and the clock may be set in either order
    delete(fake, ALPHA_ID, file_id)
    assert absent(fake, ALPHA_ID, file_id)
    assert not absent(fake, BETA_ID, file_id)
    ticker.sleep(DELAY_S)
    assert absent(fake, BETA_ID, file_id)


def test_no_delay_needs_no_clock():
    fake = make_federation()
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    assert absent(fake, ALPHA_ID, file_id)
    assert absent(fake, BETA_ID, file_id)


def test_the_nodes_of_a_fake_still_hold_an_entry_whose_delete_is_under_way_though_its_helpers_find_it_gone():
    fake, ticker = delayed()
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    assert file_id in fake.nodes
    assert not fake.exists("a.bin")
    ticker.sleep(DELAY_S)
    fake.exists("a.bin")  # a read of the tree lands what is due
    assert file_id not in fake.nodes


# -- tombstones: what a deleted entry answers by id, per attribute selection -----------------------------------------

LENIENT = [("size",), ("parent_id",), ("file_id",), ("size", "parent_id"), ("file_id", "size", "parent_id")]
STRICT = [(), ("name",), ("type",), ("name", "size", "parent_id"), ("size", "type"), ("mode",), ("size", "mode")]
TOMBSTONE_NOTE = "deploy/compose-federation/day0/2026-11-13-federation-facts-deleted-entries-by-id.md"


def read(fake: FakeData, provider: str, file_id: str, *attributes: str) -> httpx.Response:
    """`GET /data/{id}` asking for the attributes given, as repeated `attribute` parameters; none: no parameter."""
    params = {"attribute": list(attributes)} if attributes else None
    return call(fake, provider, "GET", f"/data/{file_id}", params=params)


def test_the_default_tombstone_is_the_14_days_the_platform_keeps_one_which_no_test_waits_out():
    assert TOMBSTONE_S == 14 * 24 * 3600  # cluster-worker's `document_expiry`, 1 209 600 s
    assert make_federation().tombstone_s == FakeData().tombstone_s == TOMBSTONE_S
    assert FakeData(tombstone_s=5.0).tombstone_s == 5.0


def test_the_tombstone_names_the_facts_it_models():
    own, live = TOMBSTONE_FACTS
    assert own == TOMBSTONE_NOTE
    assert (REPO / own).is_file()
    assert live == "deploy/compose-federation/day0/2026-11-12-federation-facts-pending-deletes.md"  # merges with #192


@pytest.mark.parametrize("attributes", LENIENT, ids="+".join)
@pytest.mark.parametrize("provider", [ALPHA_ID, BETA_ID])
def test_a_deleted_entry_read_for_size_parent_or_file_id_alone_keeps_answering_from_its_last_state(
    provider, attributes
):
    fake = make_federation(clock=Ticker().clock)
    file_id = fake.add_file("study/data.bin", DATA)
    study = fake.id_of("study")
    delete(fake, ALPHA_ID, file_id)
    assert file_id not in fake.nodes  # the delete has landed everywhere
    answer = read(fake, provider, file_id, *attributes)
    assert answer.status_code == 200
    body = answer.json()
    assert (body.get("size"), body.get("parent_id")) == (
        len(DATA) if "size" in attributes else None,
        study if "parent_id" in attributes else None,
    )
    assert body.get("file_id") == (file_id if "file_id" in attributes else None)
    assert set(body) <= {"size", "parent_id", "file_id"}  # only what was asked, as the platform resolves


@pytest.mark.parametrize("provider", [ALPHA_ID, BETA_ID])
def test_file_id_alone_answers_200_from_a_deleted_entrys_tombstone_and_names_the_id_by_file_id(provider):
    """25.0 names the id attribute `file_id`; asked for alone it needs no strict read of the document."""
    fake = make_federation(clock=Ticker().clock)
    file_id = fake.add_file("study/data.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    answer = read(fake, provider, file_id, "file_id")
    assert (answer.status_code, answer.json()) == (200, {"file_id": file_id})


GUID = {"error": {"id": "badValueNotAllowed", "details": {}}}  # posix-free: no errno, as the fake answers that error id


@pytest.mark.parametrize("others", [(), ("size",), ("name",), ("parent_id", "file_id")], ids="+".join)
@pytest.mark.parametrize("provider", [ALPHA_ID, BETA_ID])
def test_guid_is_no_attribute_name_on_25_0_and_answers_400_badvaluenotallowed_for_every_entry(provider, others):
    """The deprecated names the `attribute` parameter takes have `file_id` and no `guid`; the names are checked before
    any lookup, so a live entry, a deleted one, one not shown yet and one that never was all answer the same 400, and a
    `guid` among other attributes fails the whole request."""
    fake = make_federation(clock=Ticker().clock)
    live = fake.add_file("study/live.bin", DATA)
    deleted = fake.add_file("study/deleted.bin", DATA)
    unseen = fake.add_file("study/unseen.bin", DATA)
    fake.unseen[(BETA_ID, unseen)] = 3
    delete(fake, ALPHA_ID, deleted)
    for file_id in (live, deleted, unseen, "no-such-id"):
        answer = read(fake, provider, file_id, "guid", *others)
        assert (answer.status_code, answer.json()) == (400, GUID), file_id
    assert fake.unseen[(BETA_ID, unseen)] == 3  # refused before the provider looked the entry up


def test_guid_is_refused_in_the_legacy_shape_too():
    fake = make_federation(clock=Ticker().clock, error_shape=ERRORS_LEGACY)
    file_id = fake.add_file("a.bin", DATA)
    live = read(fake, ALPHA_ID, file_id, "guid")
    assert (live.status_code, live.json()) == (400, GUID)
    delete(fake, ALPHA_ID, file_id)
    gone = read(fake, ALPHA_ID, file_id, "guid")
    assert (gone.status_code, gone.json()) == (400, GUID)  # not the 404 a gone entry answers there


@pytest.mark.parametrize("attributes", STRICT, ids=lambda a: "+".join(a) or "no-attribute")
@pytest.mark.parametrize("provider", [ALPHA_ID, BETA_ID])
def test_a_deleted_entry_read_with_no_attribute_or_a_strict_one_among_them_answers_enoent(provider, attributes):
    fake = make_federation(clock=Ticker().clock)
    file_id = fake.add_file("study/data.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    answer = read(fake, provider, file_id, *attributes)
    assert (answer.status_code, answer.json()) == (400, GONE_25_0)


def test_a_deleted_directory_and_what_was_under_it_keep_their_last_state_for_a_lenient_read_and_not_a_strict_one():
    """A directory's `size` alone is the traced case; what is under it is a file, whose `size` is assumed alike."""
    fake = make_federation(clock=Ticker().clock)
    inner = fake.add_file("study/deep/b.bin", DATA)
    study, deep = fake.id_of("study"), fake.id_of("study/deep")
    delete(fake, ALPHA_ID, study)
    for gone, parent in ((study, ROOT_ID), (deep, study), (inner, deep)):
        assert read(fake, BETA_ID, gone, "parent_id").json() == {"parent_id": parent}
        assert read(fake, BETA_ID, gone, "size").status_code == 200
        assert read(fake, BETA_ID, gone).json() == GONE_25_0
    assert read(fake, BETA_ID, inner, "size").json() == {"size": len(DATA)}


def test_a_tombstone_keeps_the_size_the_entry_had_when_it_was_deleted():
    """Pins two assumptions: the size is the one it had when its delete landed, and a deleted FILE's `size` alone
    answers from its tombstone as a directory's does (the source trace covered a directory's; a file's branch, through
    the file location, was not read and may answer while that exists). A live check that disproves one changes this."""
    fake = make_federation(clock=Ticker().clock)
    file_id = fake.add_file("a.bin", DATA)
    write(fake, ALPHA_ID, file_id, DATA + b"more")  # written past the end: the size it has when it is deleted
    delete(fake, ALPHA_ID, file_id)
    assert read(fake, ALPHA_ID, file_id, "size").json() == {"size": len(DATA) + 4}


def test_a_lenient_read_answers_only_the_bare_entry_and_never_its_children_content_or_block_map():
    fake = make_federation(clock=Ticker().clock)
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    for part in ("/children", "/content", "/distribution"):
        answer = call(fake, ALPHA_ID, "GET", f"/data/{file_id}{part}", params={"attribute": "size"})
        assert (answer.status_code, answer.json()) == (400, GONE_25_0)


def test_a_tombstone_answers_until_its_time_is_up_and_then_the_entry_is_gone_for_every_read():
    ticker = Ticker()
    fake = make_federation(clock=ticker.clock)
    fake.tombstone_s = 60.0
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    ticker.sleep(59.0)
    assert read(fake, BETA_ID, file_id, "size").status_code == 200
    ticker.sleep(1.0)
    assert read(fake, BETA_ID, file_id, "size").json() == GONE_25_0
    assert read(fake, ALPHA_ID, file_id, "parent_id").json() == GONE_25_0


def test_a_tombstone_of_no_time_makes_a_lenient_read_answer_enoent_at_once():
    fake = make_federation()  # a frozen clock: the time is up the moment it is 0 s long
    fake.tombstone_s = 0.0
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    assert read(fake, ALPHA_ID, file_id, "size").json() == GONE_25_0


def test_the_default_tombstone_needs_no_moving_clock():
    fake = make_federation()  # the frozen clock never moves, so the 14 days never pass
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    assert read(fake, ALPHA_ID, file_id, "size").status_code == 200


def test_during_a_delete_that_is_under_way_the_provider_it_went_to_answers_lenient_reads_and_the_others_everything():
    fake, ticker = delayed()
    fake.tombstone_s = 10.0
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    assert read(fake, ALPHA_ID, file_id).json() == GONE_25_0  # recorded: strict reads answer as for a deleted entry
    assert read(fake, ALPHA_ID, file_id, "size", "parent_id").json() == {"size": len(DATA), "parent_id": ROOT_ID}
    assert read(fake, BETA_ID, file_id).status_code == 200  # assumed: the others still show the whole entry
    ticker.sleep(DELAY_S)  # it lands everywhere, and its tombstone counts from then
    assert read(fake, BETA_ID, file_id).json() == GONE_25_0
    assert read(fake, BETA_ID, file_id, "size").status_code == 200
    ticker.sleep(9.0)
    assert read(fake, BETA_ID, file_id, "size").status_code == 200
    ticker.sleep(1.0)
    assert read(fake, BETA_ID, file_id, "size").json() == GONE_25_0


@pytest.mark.parametrize("attributes", [("size",), ("parent_id",), ("file_id",)])
def test_the_legacy_shape_keeps_no_tombstone(attributes):
    fake = make_federation(clock=Ticker().clock, error_shape=ERRORS_LEGACY)
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    assert read(fake, ALPHA_ID, file_id, *attributes).status_code == 404


def test_a_live_entry_is_unchanged_by_the_attributes_asked_for():
    fake = make_federation(clock=Ticker().clock)
    file_id = fake.add_file("a.bin", DATA)
    assert read(fake, BETA_ID, file_id, "size").json() == read(fake, BETA_ID, file_id).json()
    assert read(fake, BETA_ID, file_id, "size").json()["size"] == len(DATA)
