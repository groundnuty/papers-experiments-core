"""The public `FakeData`, tested as the fake it is: its block maps, its per-reader views and its error shapes.

These are tests of the fake, not of the platform layer. They call the fake the way the platform layer's client does, as
HTTP requests it answers, and use the public names of `expcore_onedata.testing` only.
"""

import base64
from email.utils import parsedate_to_datetime

import httpx
import pytest

from expcore_onedata.testing import (
    ALPHA_ID,
    BETA_ID,
    DATA,
    ERRORS_25_0,
    ERRORS_LEGACY,
    PROVIDER,
    ROOT_ID,
    USERS,
    FakeData,
    Ticker,
    make_federation,
)

HOSTS = {ALPHA_ID: "https://alpha.test", BETA_ID: "https://beta.test"}
TYPED = {"Content-Type": "application/octet-stream"}
GONE_25_0 = {"error": {"id": "posix", "details": {"errno": "enoent"}}}


def call(fake: FakeData, provider: str, method: str, route: str, *, token: str | None = None, **options):
    """Send one request to a provider's data API and return the fake's answer."""
    headers = {"X-Auth-Token": token or fake.admin_token, **options.pop("headers", {})}
    return fake.respond(httpx.Request(method, f"{HOSTS[provider]}{PROVIDER}{route}", headers=headers, **options))


def blocks(fake: FakeData, file_id: str, provider: str) -> list[list[int]]:
    """The ranges a provider holds of a file, as the block-map read of that file reports them."""
    answer = call(fake, ALPHA_ID, "GET", f"/data/{file_id}/distribution").json()
    return answer["distributionPerProvider"][provider]["distributionPerStorageBackend"]["storage-1"]["blocks"]


def run_job(fake: FakeData, provider: str, kind: str, file_id: str) -> None:
    """Post a replication or an eviction for a provider and read its record until it has ended."""
    field = "evictingProviderId" if kind == "eviction" else "replicatingProviderId"
    made = call(fake, ALPHA_ID, "POST", "/transfers", json={"type": kind, field: provider, "fileId": file_id})
    assert made.status_code == 201
    job = made.json()["transferId"]
    for _ in range(fake.transfer_flip_after + 1):
        record = call(fake, ALPHA_ID, "GET", f"/transfers/{job}").json()
    assert record.get("replicationStatus", record.get("evictionStatus")) == "completed"


def children(fake: FakeData, provider: str, directory: str = ROOT_ID) -> list[str]:
    return [c["name"] for c in call(fake, provider, "GET", f"/data/{directory}/children").json()["children"]]


# -- per-file block maps per provider, and eviction -------------------------------------------------------------------


def test_each_providers_block_map_follows_a_range_read_a_replication_and_an_eviction():
    fake = make_federation(clock=Ticker().clock)
    file_id = fake.add_file("study/data.bin", DATA)
    assert (blocks(fake, file_id, ALPHA_ID), blocks(fake, file_id, BETA_ID)) == ([[0, len(DATA)]], [])

    for first, last in ((100, 149), (150, 199), (1000, 1009)):  # a range read through beta fetches it there, on the fly
        read = call(fake, BETA_ID, "GET", f"/data/{file_id}/content", headers={"Range": f"bytes={first}-{last}"})
        assert (read.status_code, read.content) == (206, DATA[first : last + 1])
    assert blocks(fake, file_id, BETA_ID) == [[100, 100], [1000, 10]]  # adjacent ranges merge; the others stay apart
    assert blocks(fake, file_id, ALPHA_ID) == [[0, len(DATA)]]  # and no provider's map changes another's

    run_job(fake, BETA_ID, "replication", file_id)  # a replication places the whole file
    assert blocks(fake, file_id, BETA_ID) == [[0, len(DATA)]]

    run_job(fake, BETA_ID, "eviction", file_id)  # an eviction removes all of the evicting provider's blocks
    assert blocks(fake, file_id, BETA_ID) == []
    assert blocks(fake, file_id, ALPHA_ID) == [[0, len(DATA)]]
    assert fake.held("study/data.bin", BETA_ID) == []
    assert fake.held("study/data.bin", ALPHA_ID) == [[0, len(DATA)]]


def write(fake: FakeData, file_id: str, offset: int | None, body: bytes) -> httpx.Response:
    params = {} if offset is None else {"offset": offset}
    return call(fake, ALPHA_ID, "PUT", f"/data/{file_id}/content", params=params, content=body, headers=TYPED)


def distribution(fake: FakeData, file_id: str, reader: str = ALPHA_ID) -> dict:
    return call(fake, reader, "GET", f"/data/{file_id}/distribution").json()["distributionPerProvider"]


def test_o44_an_empty_write_past_the_end_extends_the_size_and_writes_no_block():
    """O44: as op-worker f118ed9 does (read_write_req.erl:58-86, fslogic_blocks.erl:125-126) and the 10 Nov facts
    measured: an empty body at an offset sets the size to that offset, and every provider's block map keeps only what
    was written, before the prefix is written and after; the file's physical size is the prefix's."""
    fake = make_federation(clock=Ticker().clock)
    file_id = make_on_alpha(fake)
    assert write(fake, file_id, 8192, b"").status_code == 204
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}").json()["size"] == 8192
    assert (blocks(fake, file_id, ALPHA_ID), blocks(fake, file_id, BETA_ID)) == ([], [])
    write(fake, file_id, 0, DATA[:1000])
    write(fake, file_id, 4096, b"")  # inside the size: nothing changes
    per = distribution(fake, file_id)
    assert per[ALPHA_ID]["virtualSize"] == 8192
    assert per[ALPHA_ID]["distributionPerStorageBackend"]["storage-1"] == {"physicalSize": 1000, "blocks": [[0, 1000]]}
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}").json()["size"] == 8192


def test_o44_an_unwritten_range_reads_as_zeros_and_places_no_block_anywhere():
    """O44: no provider holds the gap, so a range read of it, through the holder or another provider, answers 206 with
    zeros at the range's full length and fetches nothing (replica_finder.erl:53-89 keeps only blocks a remote provider
    holds); a read across the prefix's end places on the reader only the part the holder has."""
    fake = make_federation(clock=Ticker().clock)
    file_id = make_on_alpha(fake)
    write(fake, file_id, 8192, b"")
    write(fake, file_id, 0, DATA[:1000])
    for reader in (ALPHA_ID, BETA_ID):
        read = call(fake, reader, "GET", f"/data/{file_id}/content", headers={"Range": "bytes=4096-8191"})
        assert (read.status_code, read.content) == (206, bytes(4096))
        assert read.headers["Content-Range"] == "bytes 4096-8191/8192"
    assert (blocks(fake, file_id, ALPHA_ID), blocks(fake, file_id, BETA_ID)) == ([[0, 1000]], [])
    across = call(fake, BETA_ID, "GET", f"/data/{file_id}/content", headers={"Range": "bytes=900-1099"})
    assert across.content == DATA[900:1000] + bytes(100)
    assert blocks(fake, file_id, BETA_ID) == [[900, 100]]
    fake.onthefly_unit = 4096  # a 1 MiB-like window stops at the end of what a provider holds
    call(fake, BETA_ID, "GET", f"/data/{file_id}/content", headers={"Range": "bytes=0-9"})
    assert blocks(fake, file_id, BETA_ID) == [[0, 1000]]


def test_o44_a_content_write_without_an_offset_truncates_the_file_first():
    """O44: as the API describes and the source shows (file_content_rest_handler.erl:224-228), which is why the client
    never sends one: the file's bytes and every provider's blocks are gone before the body is written at 0."""
    fake = make_federation(clock=Ticker().clock)
    file_id = make_on_alpha(fake)
    write(fake, file_id, 8192, b"")
    write(fake, file_id, 0, DATA[:1000])
    write(fake, file_id, None, b"xy")
    assert bytes(fake.nodes[file_id].content) == b"xy"
    assert blocks(fake, file_id, ALPHA_ID) == [[0, 2]]


def test_an_eviction_that_ends_without_removing_a_block_can_be_scripted():
    fake = make_federation(clock=Ticker().clock)
    file_id = fake.add_file("study/data.bin", DATA)
    run_job(fake, BETA_ID, "replication", file_id)
    fake.evictions_remove_blocks = False
    run_job(fake, BETA_ID, "eviction", file_id)
    assert blocks(fake, file_id, BETA_ID) == [[0, len(DATA)]]


# -- per-reader views with a visibility delay -------------------------------------------------------------------------


def make_on_alpha(fake: FakeData, name: str = "new.bin") -> str:
    made = call(fake, ALPHA_ID, "POST", f"/data/{ROOT_ID}/children", params={"name": name, "type": "REG"})
    assert made.status_code == 201
    return made.json()["fileId"]


def test_a_new_file_shows_on_another_provider_only_after_the_delay_on_the_fakes_own_clock():
    ticker = Ticker()
    fake = make_federation(clock=ticker.clock, visibility_delay_s=6.0)
    file_id = make_on_alpha(fake)
    for read in (f"/data/{file_id}", f"/data/{file_id}/content", f"/data/{file_id}/distribution"):
        assert call(fake, ALPHA_ID, "GET", read).status_code == 200  # the provider it was made on shows it at once
        assert call(fake, BETA_ID, "GET", read).json() == GONE_25_0  # the other one does not, and says so as 25.0 does
    assert (children(fake, ALPHA_ID), children(fake, BETA_ID)) == (["new.bin"], [])
    ticker.sleep(5)
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}").status_code == 400  # still a second short of the delay
    ticker.sleep(1)
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}").status_code == 200
    assert children(fake, BETA_ID) == ["new.bin"]


def test_every_reader_has_its_own_view_and_its_own_delay():
    ticker = Ticker()
    fake = make_federation(clock=ticker.clock, visibility_delay_s=6.0)
    fake.visibility_delay_by_provider[BETA_ID] = 2.0
    on_alpha = make_on_alpha(fake, "from-alpha.bin")
    on_beta = call(fake, BETA_ID, "POST", f"/data/{ROOT_ID}/children", params={"name": "from-beta.bin", "type": "REG"})
    on_beta = on_beta.json()["fileId"]
    ticker.sleep(2)  # beta's delay has run out, alpha's has not
    assert call(fake, BETA_ID, "GET", f"/data/{on_alpha}").status_code == 200
    assert call(fake, ALPHA_ID, "GET", f"/data/{on_beta}").status_code == 400
    ticker.sleep(4)
    assert call(fake, ALPHA_ID, "GET", f"/data/{on_beta}").status_code == 200


def test_a_file_a_test_adds_is_made_on_its_holder_and_the_directory_on_the_way_with_it():
    ticker = Ticker()
    fake = make_federation(clock=ticker.clock, visibility_delay_s=6.0)
    file_id = fake.add_file("study/data.bin", DATA)  # alpha holds it
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}").status_code == 400
    assert children(fake, BETA_ID) == []  # the directory `study` is not shown either
    assert children(fake, ALPHA_ID) == ["study"]
    ticker.sleep(6)
    assert children(fake, BETA_ID) == ["study"]
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}").status_code == 200


def test_without_a_delay_every_provider_shows_a_new_file_at_once():
    fake = make_federation(clock=Ticker().clock)
    file_id = make_on_alpha(fake)
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}").status_code == 200
    assert children(fake, BETA_ID) == ["new.bin"]


# -- Onedata 25.0's error shapes, and the older one --------------------------------------------------------------------


def mint_read_only_token(fake: FakeData) -> str:
    login, (password, _) = next(iter(USERS.items()))
    basic = base64.b64encode(f"{login}:{password}".encode()).decode()
    caveats = [{"type": "time", "validUntil": 2_000_000_000}, {"type": "data.readonly"}]
    answer = fake.respond(
        httpx.Request(
            "POST",
            "https://zone.test/api/v3/onezone/user/tokens/temporary",
            headers={"Authorization": f"Basic {basic}"},
            json={"caveats": caveats},
        )
    )
    assert answer.status_code == 200
    return answer.json()["token"]


def test_the_default_is_onedata_25_0s_shape_and_the_versions_are_named():
    assert make_federation().error_shape == ERRORS_25_0 == "25.0"
    assert ERRORS_LEGACY == "legacy"
    doc = FakeData.__doc__ or ""
    assert "Onedata 25.0" in doc
    assert "ERRORS_LEGACY" in doc
    assert "No live version is known" in doc  # the legacy shape is not claimed for any version


def test_an_unknown_error_shape_is_refused_when_given_and_when_set():
    with pytest.raises(ValueError, match="error_shape must be one of"):
        FakeData(error_shape="24.0")
    fake = make_federation()
    with pytest.raises(ValueError, match="error_shape must be one of"):
        fake.error_shape = "24.0"
    assert fake.error_shape == ERRORS_25_0  # the refused value changed nothing


def test_a_deleted_entry_and_a_space_root_not_there_yet_answer_400_posix_enoent_in_25_0():
    fake = make_federation(clock=Ticker().clock)
    file_id = fake.add_file("study/data.bin", DATA)
    assert call(fake, ALPHA_ID, "DELETE", f"/data/{file_id}").status_code == 204
    gone = call(fake, ALPHA_ID, "GET", f"/data/{file_id}")
    assert (gone.status_code, gone.json()) == (400, GONE_25_0)
    fake.root_enoent_polls = 1
    root = call(fake, ALPHA_ID, "GET", f"/spaces/{fake.SPACE_ID}")
    assert (root.status_code, root.json()) == (400, GONE_25_0)
    assert call(fake, ALPHA_ID, "GET", f"/spaces/{fake.SPACE_ID}").status_code == 200


def test_in_the_legacy_shape_a_gone_entry_is_a_404():
    ticker = Ticker()
    fake = make_federation(clock=ticker.clock, error_shape=ERRORS_LEGACY, visibility_delay_s=6.0)
    file_id = fake.add_file("study/data.bin", DATA)
    not_yet = call(fake, BETA_ID, "GET", f"/data/{file_id}")
    assert (not_yet.status_code, not_yet.json()["error"]["id"]) == (404, "notFound")
    ticker.sleep(6)
    assert call(fake, ALPHA_ID, "DELETE", f"/data/{file_id}").status_code == 204
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}").status_code == 404
    fake.root_enoent_polls = 1
    root = call(fake, ALPHA_ID, "GET", f"/spaces/{fake.SPACE_ID}")
    assert (root.status_code, root.json()) == (404, GONE_25_0)  # the space root has always carried the posix error


@pytest.mark.parametrize(
    ("shape", "refusal"),
    [(ERRORS_25_0, (400, "posix", "eacces")), (ERRORS_LEGACY, (403, "forbidden", None))],
)
def test_a_write_with_a_read_only_token_is_refused_and_changes_nothing(shape, refusal):
    fake = make_federation(clock=Ticker().clock, error_shape=shape)
    file_id = fake.add_file("study/data.bin", DATA)
    token = mint_read_only_token(fake)
    write = call(
        fake,
        ALPHA_ID,
        "PUT",
        f"/data/{file_id}/content",
        token=token,
        params={"offset": "0"},
        content=b"x",
        headers=TYPED,
    )
    body = write.json()["error"]
    assert (write.status_code, body["id"], body["details"].get("errno")) == refusal
    assert bytes(fake.nodes[file_id].content) == DATA
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}/content", token=token).status_code == 200  # reads are fine
    assert (
        call(
            fake, ALPHA_ID, "PUT", f"/data/{file_id}/content", params={"offset": "0"}, content=b"x", headers=TYPED
        ).status_code
        == 204
    )


def test_an_upload_without_a_content_type_answers_415_in_25_0_and_writes_nothing():
    fake = make_federation(clock=Ticker().clock)
    file_id = fake.add_file("study/data.bin", DATA)
    refused = call(fake, ALPHA_ID, "PUT", f"/data/{file_id}/content", params={"offset": "0"}, content=b"x")
    assert refused.status_code == 415
    assert bytes(fake.nodes[file_id].content) == DATA
    typed = call(fake, ALPHA_ID, "PUT", f"/data/{file_id}/content", params={"offset": "0"}, content=b"x", headers=TYPED)
    assert typed.status_code == 204
    assert bytes(fake.nodes[file_id].content[:1]) == b"x"


def test_an_upload_without_a_content_type_is_accepted_in_the_legacy_shape():
    fake = make_federation(clock=Ticker().clock, error_shape=ERRORS_LEGACY)
    file_id = fake.add_file("study/data.bin", DATA)
    accepted = call(fake, ALPHA_ID, "PUT", f"/data/{file_id}/content", params={"offset": "0"}, content=b"x")
    assert accepted.status_code == 204


def test_the_shape_can_be_changed_between_calls():
    fake = make_federation(clock=Ticker().clock)
    file_id = fake.add_file("study/data.bin", DATA)
    call(fake, ALPHA_ID, "DELETE", f"/data/{file_id}")
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}").status_code == 400
    fake.error_shape = ERRORS_LEGACY
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}").status_code == 404


def test_each_shape_sets_its_answers_and_one_set_after_it_bends_only_that_answer():
    fake = make_federation(clock=Ticker().clock)
    assert (fake.missing_answer, fake.unseen_answer, fake.readonly_answer) == (
        (400, "posix", "enoent"),
        (400, "posix", "enoent"),
        (400, "posix", "eacces"),
    )
    assert (fake.keep_deleted_parents, fake.require_octet_stream) == (True, True)
    file_id = fake.add_file("study/data.bin", DATA)
    call(fake, ALPHA_ID, "DELETE", f"/data/{file_id}")
    fake.missing_answer = (400, "badValueNotAllowed", None)  # one answer bent: the shape is still 25.0
    gone = call(fake, ALPHA_ID, "GET", f"/data/{file_id}")
    assert (gone.status_code, gone.json()["error"]["id"]) == (400, "badValueNotAllowed")
    assert fake.error_shape == ERRORS_25_0
    assert fake.root_enoent_answer == (400, "posix", "enoent")
    fake.error_shape = ERRORS_LEGACY  # a shape set again sets every answer to its own
    assert (fake.missing_answer, fake.readonly_answer) == ((404, "notFound", None), (403, "forbidden", None))
    assert (fake.keep_deleted_parents, fake.require_octet_stream) == (False, False)


@pytest.mark.parametrize(("shape", "kept"), [(ERRORS_25_0, True), (ERRORS_LEGACY, False)])
def test_a_deleted_entrys_parent_link_still_reads_in_25_0_and_not_in_the_legacy_shape(shape, kept):
    fake = make_federation(clock=Ticker().clock, error_shape=shape)
    file_id = fake.add_file("study/data.bin", DATA)
    parent = fake.id_of("study")
    call(fake, ALPHA_ID, "DELETE", f"/data/{file_id}")
    link = call(fake, ALPHA_ID, "GET", f"/data/{file_id}", params={"attribute": "parent_id"})
    if kept:
        assert (link.status_code, link.json()) == (200, {"parent_id": parent})
    else:
        assert link.status_code == 404
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}").status_code == (400 if kept else 404)


def test_a_file_held_back_for_reads_answers_unseen_until_they_are_used_up_and_stays_listed():
    fake = make_federation(clock=Ticker().clock)
    fake.unseen_after_create = {BETA_ID: 2}
    file_id = fake.add_file("new.bin", DATA)
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}").status_code == 200  # only beta is held back
    assert children(fake, BETA_ID) == ["new.bin"]  # a listing is no read of the file and uses nothing up
    assert [call(fake, BETA_ID, "GET", f"/data/{file_id}").json() for _ in range(2)] == [GONE_25_0] * 2
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}").status_code == 200
    assert fake.unseen[(BETA_ID, file_id)] == 0


def test_an_upload_of_another_type_answers_415_in_25_0():
    fake = make_federation(clock=Ticker().clock)
    file_id = fake.add_file("study/data.bin", DATA)
    other = {"Content-Type": "text/plain"}
    refused = call(
        fake, ALPHA_ID, "PUT", f"/data/{file_id}/content", params={"offset": "0"}, content=b"x", headers=other
    )
    assert (refused.status_code, refused.json()["error"]["id"]) == (415, "unsupportedMediaType")
    assert bytes(fake.nodes[file_id].content) == DATA


def test_asked_for_a_path_a_file_answers_400_as_in_25_0_and_its_attributes_name_its_parent():
    fake = make_federation(clock=Ticker().clock)
    file_id = fake.add_file("study/data.bin", DATA)
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}", params={"attribute": "path"}).status_code == 400
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}").json()["parent_id"] == fake.id_of("study")


# -- the transfer listing's order (O32) ------------------------------------------------------------------------------


def listed(fake: FakeData, state: str, limit: int, token: str | None = None) -> dict:
    """One page of the space's transfer listing in one state, as alpha answers it."""
    params = {"state": state, "limit": limit, **({"page_token": token} if token else {})}
    return call(fake, ALPHA_ID, "GET", f"/spaces/{fake.SPACE_ID}/transfers", params=params).json()


def test_o32_the_transfer_listing_is_newest_first_and_a_page_resumes_after_its_token():
    """O32: as on the 25.0 build (op-worker f118ed9, transfer_links.erl:160-164), an ended job is listed by its finish
    time and a waiting or ongoing one by its schedule time, the newest first, whatever order they were made in; a full
    page's token resumes the listing right after the last job it gave, and a short page ends it."""
    fake = make_federation(clock=Ticker().clock)
    fake.add_file("study/data.bin", DATA)
    middle = fake.add_transfer("replication", "study/data.bin", BETA_ID, scheduled=100, finished=300)
    oldest = fake.add_transfer("replication", "study/data.bin", BETA_ID, scheduled=150, finished=200)
    newest = fake.add_transfer("eviction", "study/data.bin", BETA_ID, scheduled=50, finished=400)
    late = fake.add_transfer("replication", "study/data.bin", BETA_ID, scheduled=90, ended=False)
    early = fake.add_transfer("replication", "study/data.bin", BETA_ID, scheduled=500, ended=False)
    first = listed(fake, "ended", 2)
    assert first["transfers"] == [newest, middle]
    assert listed(fake, "ended", 2, first["nextPageToken"]) == {"transfers": [oldest], "nextPageToken": None}
    assert listed(fake, "ongoing", 10)["transfers"] == [early, late]


def test_o32_a_listing_time_puts_a_job_out_of_its_order_for_a_test_of_a_readers_check():
    """O32: `listing_time` gives the time a job's listing key is built from in place of its record's, so a test can
    list an old job ahead of newer ones while its record keeps its own finish time."""
    fake = make_federation(clock=Ticker().clock)
    fake.add_file("study/data.bin", DATA)
    old = fake.add_transfer("replication", "study/data.bin", BETA_ID, scheduled=100, finished=100)
    new = fake.add_transfer("replication", "study/data.bin", BETA_ID, scheduled=200, finished=200)
    fake.listing_time[old] = 300
    assert listed(fake, "ended", 10)["transfers"] == [old, new]
    assert call(fake, ALPHA_ID, "GET", f"/transfers/{old}").json()["finishTime"] == 100


# -- Onezone's reading of the global clock, and the Date header of its answers -------------------------------------

ZONE_TIME = "https://onezone.test/api/v3/onezone/provider/public/get_current_time"


def zone_time(fake: FakeData, **headers) -> httpx.Response:
    """Ask Onezone for the global clock, as anyone may: with no credential unless the test names a header."""
    return fake.respond(httpx.Request("GET", ZONE_TIME, headers=headers))


def test_o32_onezones_current_time_answers_the_fakes_clock_in_milliseconds_with_no_credential():
    """O32: as oz-worker's `get_current_time` does (14b5bda, `provider_routes.erl:192-198`, no privilege needed;
    `provider_rest_translator.erl:128-129`, the body), the public call answers `{"timeMillis": N}`, the global clock,
    which is the fake's `clock`, the one a job's times are stamped on. A credential is not needed, and one is
    accepted."""
    fake = make_federation(clock=lambda: 1234.5)
    assert zone_time(fake).status_code == 200
    assert zone_time(fake).json() == {"timeMillis": 1234500}
    assert zone_time(fake, **{"X-Auth-Token": fake.admin_token}).json() == {"timeMillis": 1234500}
    assert [r.token for r in fake.requests if r.path.endswith("get_current_time")] == [None, None, fake.admin_token]


def test_o32_the_date_header_of_the_answer_is_onezones_node_clock_not_the_global_one():
    """O32: the header is the answering node's system clock, which `zone_skew` moves and the global clock does not: a
    node 8 s ahead dates its answer 1008 while `timeMillis` still says 1000, as a node whose global-clock bias is -8 s
    does. `zone_date` replaces the header, and None removes it."""
    fake = make_federation(clock=lambda: 1000.0)
    fake.zone_skew = 8.0
    answer = zone_time(fake)
    assert parsedate_to_datetime(answer.headers["Date"]).timestamp() == 1008.0
    assert answer.json() == {"timeMillis": 1000000}
    fake.zone_date = None
    assert "Date" not in zone_time(fake).headers
    fake.zone_date = "not a date"
    assert zone_time(fake).headers["Date"] == "not a date"


def test_o32_a_test_can_fail_or_reshape_the_current_time_answer_alone():
    """O32: `time_status` makes the current-time call alone fail with that status and `time_hook` replaces its body,
    while every other call of Onezone's goes on answering; `status_override` still fails every call."""
    fake = make_federation(clock=lambda: 1000.0)
    fake.time_status = 503
    assert zone_time(fake).status_code == 503
    assert (
        fake.respond(
            httpx.Request("GET", ZONE_TIME.replace("/provider/public/get_current_time", "/spaces/x"))
        ).status_code
        == 401
    )
    fake.time_status, fake.time_hook = None, lambda answer: {"timeMillis": answer["timeMillis"] + 5000}
    assert zone_time(fake).json() == {"timeMillis": 1005000}
    fake.status_override = 500
    assert zone_time(fake).status_code == 500
