"""Range placement: bytes are placed by range reads through the receiving provider, confirmed by the block map (O02)."""

import httpx
import pytest

from expcore_onedata import consistency
from expcore_onedata.client import PlatformError
from expcore_onedata.provision import NotPlaced, ProvisionError
from expcore_onedata.rangemap import RangeRow
from expcore_onedata.ranges import place_ranges
from expcore_onedata.testing import ALPHA_ID, BETA_ID, DATA, PROVIDER

PATH = "study/data.bin"
ROW = RangeRow(PATH, "beta", ((0, 100), (2000, 500)))


def sent_to(fake, provider_id):
    return [call for call in fake.provider_calls if call[0] == provider_id]


def test_o02_a_range_placement_issues_range_reads_and_never_a_transfer_job(provisioner, seeded):
    """O02: the declared ranges arrive on the receiving provider through reads, and no transfer is ever scheduled."""
    record = place_ranges(provisioner, [ROW])
    assert [c for c in seeded.provider_calls if c[2].startswith("/transfers")] == []
    reads = [c for c in sent_to(seeded, BETA_ID) if c[2].endswith("/content")]
    assert len(reads) == 2  # one read per declared range
    assert [c for c in sent_to(seeded, ALPHA_ID) if c[2].endswith("/content")] == []  # the data provider is not read
    assert seeded.held(PATH, BETA_ID) == [[0, 100], [2000, 500]]  # exactly the declared bytes, not the whole file
    assert record["providers"]["providers[1]"]["bytes_read"] == 600
    assert record["providers"]["providers[1]"]["placement"]["fully_placed"] is True


def test_o02_the_placement_is_confirmed_by_the_block_map_after_it_lags(provisioner, seeded, ticker):
    """O02: bytes that show in the map only after some polls are waited for, within the bound."""
    seeded.fetch_lag = 4
    place_ranges(provisioner, [ROW])
    assert len(ticker.slept) >= 4
    assert seeded.held(PATH, BETA_ID) == [[0, 100], [2000, 500]]


def test_o02_placement_records_per_file_what_the_block_map_holds_beside_what_was_asked(provisioner, seeded):
    """O02: on Onedata 25.0 a range read places at least 1 MiB from its offset, clipped to the file, whatever it asked
    for. Placement records, per file, the ranges and bytes asked for and the ranges and bytes the receiver's block map
    holds afterwards, never the requested ones as held."""
    seeded.onthefly_unit = 1024
    record = place_ranges(provisioner, [RangeRow(PATH, "beta", ((100, 10), (3500, 10)))])
    per_file = record["providers"]["providers[1]"]["per_file"][PATH]
    assert per_file["asked"] == [[100, 10], [3500, 10]]
    assert per_file["asked_bytes"] == 20
    assert per_file["held"] == [[100, 1024], [3500, 596]]  # the second clipped at the file's end
    assert per_file["held_bytes"] == 1620
    assert record["providers"]["providers[1]"]["held_after"] == 1620


UNSEEN = [(404, "notFound", None), (400, "posix", "enoent")]


@pytest.mark.parametrize("answer", UNSEEN)
def test_o07_placement_waits_until_the_receiver_sees_a_file_written_through_the_data_provider(
    provisioner, seeded, ticker, answer
):
    """O07: on Onedata 25.0 a second provider sees a file written through the data provider some seconds later, and
    answers it meanwhile with a 404 or 400 posix enoent. Placement waits, bounded, until the receiver reports the file
    at its size before any read through it, then places the ranges as usual."""
    file_id = seeded.id_of(PATH)
    seeded.unseen[(BETA_ID, file_id)] = 3
    seeded.unseen_answer = answer
    record = place_ranges(provisioner, [ROW])
    assert record["providers"]["providers[1]"]["placement"]["fully_placed"] is True
    assert seeded.held(PATH, BETA_ID) == [[0, 100], [2000, 500]]
    assert seeded.unseen[(BETA_ID, file_id)] == 0
    assert len(ticker.slept) >= 3  # it waited, with pauses, for the three reads that did not see the file


def test_o07_a_receiver_that_never_sees_the_file_fails_at_the_bound_naming_it_by_its_label(provisioner, seeded, ticker):
    """O07: past `VISIBLE_TIMEOUT_S` the wait raises `NotVisible`, a `WaitTimeout`, naming the receiver by its position
    in platform.toml, and nothing is read through it."""
    from expcore_onedata.consistency import VISIBLE_TIMEOUT_S, NotVisible, WaitTimeout

    seeded.unseen[(BETA_ID, seeded.id_of(PATH))] = 10**6
    began = ticker.clock()
    with pytest.raises(NotVisible, match=r"providers\[1\]") as raised:
        place_ranges(provisioner, [ROW])
    assert isinstance(raised.value, WaitTimeout)
    assert "beta" not in str(raised.value)
    assert VISIBLE_TIMEOUT_S <= ticker.clock() - began <= VISIBLE_TIMEOUT_S + 2
    assert [c for c in sent_to(seeded, BETA_ID) if c[2].endswith(("/content", "/distribution"))] == []


def delete_on_alpha(fake, file_id):
    """Delete a file through the data provider, the way a client does; with no delay it lands at once, everywhere."""
    request = httpx.Request(
        "DELETE", f"https://alpha.test{PROVIDER}/data/{file_id}", headers={"X-Auth-Token": fake.admin_token}
    )
    assert fake.respond(request).status_code == 204


@pytest.fixture
def attribute_reads(fake):
    """The `attribute` values of every `GET /data/{id}` the platform layer sends, per host, in order. It wraps the
    fake's handler, so a test that uses it names it before the fixtures that build a client."""
    seen: list[tuple[str, list[str]]] = []
    handle = fake.respond

    def respond(request):
        if request.method == "GET" and request.url.path.rsplit("/", 2)[-2] == "data":
            seen.append((request.url.host, request.url.params.get_list("attribute")))
        return handle(request)

    fake.respond = respond
    return seen


def test_o07_a_file_deleted_during_the_wait_is_never_reported_visible(client, seeded, ticker):
    """O07: a deleted entry keeps answering a read of its `size` alone from a tombstone on 25.0, so the wait reads the
    name with it, which a deleted entry refuses (400 posix enoent): a file deleted while the wait polls is not visible,
    and the wait ends at its bound with `NotVisible`."""
    file_id = seeded.id_of(PATH)
    seeded.unseen[(BETA_ID, file_id)] = 2  # beta does not show it yet: the wait polls, and the file goes meanwhile

    def sleep_and_delete(seconds):
        ticker.sleep(seconds)
        if file_id in seeded.nodes:
            delete_on_alpha(seeded, file_id)

    with pytest.raises(consistency.NotVisible, match=r"providers\[1\]"):
        consistency.wait_visible(
            client, "beta", file_id, size=len(DATA), clock=ticker.clock, sleep=sleep_and_delete, timeout_s=20.0
        )
    assert file_id not in seeded.nodes
    assert ticker.now - 1000.0 >= 20.0  # it waited out its bound, and did not return at the tombstone's size


def test_o07_a_file_that_was_deleted_before_the_wait_is_not_visible_either(client, seeded, ticker):
    """O07: the same for a file already deleted when the wait begins, on a provider that never showed it."""
    file_id = seeded.id_of(PATH)
    delete_on_alpha(seeded, file_id)
    with pytest.raises(consistency.NotVisible):
        consistency.wait_visible(
            client, "beta", file_id, size=len(DATA), clock=ticker.clock, sleep=ticker.sleep, timeout_s=10.0
        )


def test_o07_a_file_deleted_before_the_provisioners_wait_is_refused_and_never_taken_as_visible(provisioner, seeded):
    """O07: `await_visible` reads the file's size on the data provider first; for a deleted file that read answers
    400 posix enoent, which fails closed, so it neither passes on the tombstone's size nor reports the file visible
    through another provider."""
    file_id = seeded.id_of(PATH)
    delete_on_alpha(seeded, file_id)
    for _ in range(2):  # and the refusal is not remembered as a file seen
        with pytest.raises(PlatformError) as raised:
            provisioner.await_visible("beta", [file_id])
        assert (raised.value.status, raised.value.code, raised.value.errno) == (400, "posix", "enoent")


def test_o07_the_waits_read_name_size_and_parent_id_together(attribute_reads, provisioner, seeded):
    """O07: a read of `size` alone is answered from a tombstone after a delete, so both reads of the visibility wait,
    the data provider's and the receiver's, ask for `name`, `size` and `parent_id` together, and size is checked from
    that answer."""
    file_id = seeded.id_of(PATH)
    attribute_reads.clear()
    provisioner.await_visible("beta", [file_id])
    wanted = ["name", "size", "parent_id"]
    assert attribute_reads == [("alpha.test", wanted), ("beta.test", wanted)]


@pytest.mark.parametrize("answer", [(400, "posix", "eacces"), (400, "badValueNotAllowed", None), (400, None, None)])
def test_o07_any_other_error_while_waiting_for_the_receiver_fails_closed_at_once(provisioner, seeded, ticker, answer):
    """O07: only a 404 or 400 posix enoent means "not visible yet"; any other answer raises at once, unwaited."""
    from expcore_onedata.client import PlatformError

    seeded.unseen[(BETA_ID, seeded.id_of(PATH))] = 3
    seeded.unseen_answer = answer
    with pytest.raises(PlatformError) as raised:
        place_ranges(provisioner, [ROW])
    assert raised.value.status == 400
    assert ticker.slept == []
    assert [c for c in sent_to(seeded, BETA_ID) if c[2].endswith("/content")] == []


def test_o02_reads_that_leave_the_block_map_short_are_not_placed(provisioner, seeded):
    """O02 with O01: a provider that answers the reads but never records the blocks has not placed anything."""
    seeded.onthefly_places = False
    with pytest.raises(NotPlaced, match="short") as raised:
        place_ranges(provisioner, [ROW])
    assert raised.value.report.bytes_short == 600


def test_o02_a_range_beyond_the_end_of_its_file_is_refused_before_any_read(provisioner, seeded):
    """O02: clipping a declared range would place less than declared, so the map is refused and nothing is read."""
    with pytest.raises(ProvisionError, match="beyond the end"):
        place_ranges(provisioner, [RangeRow(PATH, "beta", ((4000, 500),))])
    assert [c for c in seeded.provider_calls if c[2].endswith("/content")] == []


def test_o02_a_provider_that_must_start_empty_and_does_not_is_refused_before_any_read(provisioner, seeded):
    """O02: bytes cached by an earlier run would pass as placed, so the cold-start rule stops before reading."""
    place_ranges(provisioner, [ROW])
    before = len(seeded.provider_calls)
    with pytest.raises(NotPlaced, match="does not start empty"):
        place_ranges(provisioner, [ROW], start_empty=True)
    assert [c for c in seeded.provider_calls[before:] if c[2].endswith("/content")] == []


def test_o02_long_ranges_are_read_in_blocks_of_the_configured_size(provisioner, seeded):
    """O02: no single read is longer than chunk_bytes (1024 in these tests)."""
    place_ranges(provisioner, [RangeRow(PATH, "beta", ((0, 3000),))])
    reads = [r for r in seeded.requests if r.range]
    assert [r.range for r in reads] == ["bytes=0-1023", "bytes=1024-2047", "bytes=2048-2999"]
    assert seeded.held(PATH, BETA_ID) == [[0, 3000]]


def test_o02_the_bytes_read_are_the_files_bytes(client, provisioner, seeded):
    """O02: the range read returns the file's own bytes at that offset."""
    place_ranges(provisioner, [ROW])
    entry = provisioner.resolve(PATH)
    data = client.read_range(f"/data/{entry.file_id}/content", provider="beta", offset=2000, length=500)
    assert data == DATA[2000:2500]


def test_placing_nothing_is_an_error(provisioner, seeded):
    """An empty map is a mistake, not a placement that trivially succeeded."""
    with pytest.raises(ProvisionError, match="no ranges"):
        place_ranges(provisioner, [])
