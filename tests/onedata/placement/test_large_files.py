"""Large files in and out of the space: a streamed upload that skips, resumes or restarts and is verified by its hash,
and a streamed read that hashes as it goes (O25)."""

import builtins
import hashlib
import io
import math
import os
import tracemalloc
from pathlib import Path

import httpx
import pytest

from expcore_onedata.client import OnezoneClient, OutOfScope, PlatformError, answered_absent
from expcore_onedata.provision import ProvisionError
from expcore_onedata.streamed import OCTET_STREAM, Digest
from expcore_onedata.testing import BETA_ID, DATA, PROVIDER, SETTINGS
from expcore_onedata.testing.fake_federation import ZONE

CHUNK = SETTINGS["chunk_bytes"]
TARGET = "ingest/table.bin"
SEEDED = "study/data.bin"  # the file the `seeded` fixture holds, which no provisioner created
BULKY = bytes((index * 7 + index // 251) % 256 for index in range(64 * CHUNK + 100))  # 64 chunks and a short one


def digest(content):
    return Digest(len(content), hashlib.sha256(content).hexdigest())


def local_file(tmp_path, content, name="table.bin"):
    path = tmp_path / "local" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    return path


def writes(fake):
    """Every write of content the fake received, in order."""
    return [r for r in fake.requests if r.method == "PUT" and r.path.endswith("/content")]


def changes(fake):
    """Every provider call that could change state."""
    return [call for call in fake.provider_calls if call[1] in ("POST", "PUT", "DELETE")]


def stored(fake, file_id):
    return bytes(fake.nodes[file_id].content)


class Watched:
    """A local file handle that records the size of every read and refuses a read of the whole rest."""

    def __init__(self, handle, sizes):
        self._handle, self._sizes = handle, sizes

    def read(self, size=-1):
        assert size is not None, "the local file was read whole"
        assert size >= 0, "the local file was read whole"
        self._sizes.append(size)
        return self._handle.read(size)

    def readall(self):
        raise AssertionError("the local file was read whole")

    readlines = readall

    def __iter__(self):
        raise AssertionError("the local file was read by lines")

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._handle.close()


def watch_local_reads(monkeypatch, source):
    """Record the size of every read of `source`, however it is opened, and refuse a whole read of it."""
    sizes = []
    real = builtins.open

    def opened(file, mode="r", *args, **kwargs):
        handle = real(file, mode, *args, **kwargs)
        named = isinstance(file, str | os.PathLike) and Path(file).resolve() == source.resolve()
        return Watched(handle, sizes) if named and "r" in mode else handle

    def whole(self, *args, **kwargs):
        raise AssertionError("the local file was read whole")

    monkeypatch.setattr(builtins, "open", opened)
    monkeypatch.setattr(io, "open", opened)
    monkeypatch.setattr(Path, "read_bytes", whole)
    return sizes


# -- the upload --------------------------------------------------------------------------------------------------------


def test_o25_a_large_file_goes_in_a_chunk_at_a_time_and_is_never_read_whole(provisioner, fake, tmp_path, monkeypatch):
    """O25: memory follows the chunk size, not the file's. No read of the local file asks for more than a chunk and
    none reads it whole; every write carries at most a chunk, at its own offset, and the result is the file."""
    source = local_file(tmp_path, BULKY)
    sizes = watch_local_reads(monkeypatch, source)
    result = provisioner.upload(TARGET, source)
    assert result.action == "created"
    assert result.digest == digest(BULKY)
    assert result.sent_bytes == len(BULKY)
    assert sizes
    assert max(sizes) <= CHUNK
    sent = writes(fake)
    assert len(sent) == math.ceil(len(BULKY) / CHUNK)
    assert max(len(r.body) for r in sent) <= CHUNK
    assert [int(r.query["offset"]) for r in sent] == list(range(0, len(BULKY), CHUNK))
    assert stored(fake, result.file_id) == BULKY
    assert result.file_id in [line["id"] for line in provisioner.logged()]  # a creation, so teardown removes it


def test_o25_a_write_carries_the_content_type_its_caller_names_and_octets_by_default(provisioner, fake, tmp_path):
    """O25: every write of `put_file` and of an upload carries a Content-Type, application/octet-stream, the one
    Onedata 25.0 is known to take, unless the caller names another. The fake refuses any other type, as its 25.0 shape
    does; with that rule off, a named type is sent as named."""
    provisioner.put_file("ingest/small.bin", DATA)
    provisioner.upload(TARGET, local_file(tmp_path, DATA))
    assert fake.content_types == [OCTET_STREAM] * 8
    with pytest.raises(PlatformError) as refused:
        provisioner.upload("ingest/typed.csv", local_file(tmp_path, DATA, "typed.csv"), content_type="text/csv")
    assert refused.value.status == 415
    fake.require_octet_stream = False
    provisioner.upload("ingest/other.csv", local_file(tmp_path, DATA, "other.csv"), content_type="text/csv")
    assert fake.content_types[8:] == ["text/csv"] * 5  # the refused first write, then the whole file


@pytest.mark.parametrize("named", ["", "octet", "text/csv\r\nX-Other: 1", "a/b/c", None])
def test_o25_a_content_type_that_is_not_a_plain_media_type_is_refused_before_anything_is_sent(
    provisioner, fake, tmp_path, named
):
    """O25: the type goes into a header, so one that is empty, has no subtype or holds a line break is refused."""
    with pytest.raises(ValueError, match="content type"):
        provisioner.upload(TARGET, local_file(tmp_path, DATA), content_type=named)
    assert changes(fake) == []


def test_o25_a_file_already_there_with_the_same_bytes_is_skipped_and_nothing_is_sent(provisioner, fake, tmp_path):
    """O25: an upload run again finds the target holding the same size and SHA-256 and sends nothing."""
    source = local_file(tmp_path, BULKY)
    first = provisioner.upload(TARGET, source)
    before = list(changes(fake))
    again = provisioner.upload(TARGET, source)
    assert (again.action, again.sent_bytes, again.file_id) == ("skipped", 0, first.file_id)
    assert again.digest == digest(BULKY)
    assert changes(fake) == before


def test_o25_a_file_another_made_with_the_same_bytes_is_skipped_and_stays_out_of_the_log(provisioner, seeded, tmp_path):
    """O25 with O09: skipping writes nothing, so a file this provisioner did not create can be skipped; it is not
    adopted into the log, and teardown leaves it."""
    result = provisioner.upload(SEEDED, local_file(tmp_path, DATA))
    assert (result.action, result.file_id) == ("skipped", seeded.id_of(SEEDED))
    assert changes(seeded) == []
    assert provisioner.logged() == []


def test_o25_an_upload_cut_by_a_reset_resumes_from_where_the_target_ends(provisioner, fake, tmp_path):
    """O25: the connection drops after five writes. The upload run again finds a strict prefix and sends the rest by
    offset writes from its end; no byte of the prefix is sent twice, and the whole is verified."""
    source = local_file(tmp_path, BULKY)
    fake.writes_before_reset = 5
    with pytest.raises(PlatformError) as cut:
        provisioner.upload(TARGET, source)
    assert cut.value.transport
    fake.writes_before_reset = None
    done = len(writes(fake))
    result = provisioner.upload(TARGET, source)
    assert result.action == "resumed"
    assert result.sent_bytes == len(BULKY) - 5 * CHUNK
    offsets = [int(r.query["offset"]) for r in writes(fake)[done:]]
    assert offsets == list(range(5 * CHUNK, len(BULKY), CHUNK))
    assert stored(fake, result.file_id) == BULKY


def test_o25_a_prefix_that_ends_inside_a_chunk_is_resumed_from_its_last_byte(provisioner, fake, tmp_path):
    """O25: a write cut part way leaves a prefix that is not a whole number of chunks; the rest starts at its end."""
    source = local_file(tmp_path, BULKY)
    first = provisioner.upload(TARGET, source)
    cut = 7 * CHUNK + 300
    del fake.nodes[first.file_id].content[cut:]
    done = len(writes(fake))
    result = provisioner.upload(TARGET, source)
    assert result.action == "resumed"
    assert [int(r.query["offset"]) for r in writes(fake)[done:]][:2] == [cut, cut + CHUNK]
    assert stored(fake, first.file_id) == BULKY


def test_o25_a_target_this_provisioner_made_that_differs_is_written_again_from_a_truncating_first_write(
    provisioner, fake, tmp_path
):
    """O25: a target that holds more bytes than the local file, or as many but other ones, is no prefix. The first
    write goes without an offset, which truncates the file (the API's rule), and the rest follow at their offsets."""
    made = provisioner.upload(TARGET, local_file(tmp_path, BULKY + b"tail"))

    def restarted():
        done = len(writes(fake))
        result = provisioner.upload(TARGET, local_file(tmp_path, BULKY))
        assert (result.action, result.file_id) == ("restarted", made.file_id)
        first, *rest = writes(fake)[done:]
        assert "offset" not in first.query
        assert [int(r.query["offset"]) for r in rest] == list(range(CHUNK, len(BULKY), CHUNK))
        assert stored(fake, made.file_id) == BULKY

    restarted()  # the target was longer
    fake.nodes[made.file_id].content[100] ^= 0xFF  # one byte of the target changed behind the upload
    restarted()


def test_o25_an_empty_file_over_a_longer_target_truncates_it(provisioner, fake, tmp_path):
    """O25: an empty local file has no chunk to send, so the truncating write goes with an empty body."""
    made = provisioner.upload(TARGET, local_file(tmp_path, DATA))
    result = provisioner.upload(TARGET, local_file(tmp_path, b"", name="empty.bin"))
    assert (result.action, result.digest) == ("restarted", digest(b""))
    assert stored(fake, made.file_id) == b""


def test_o25_a_differing_file_this_provisioner_did_not_create_is_refused_and_nothing_is_sent(
    provisioner, seeded, tmp_path
):
    """O25 with O09: resuming or restarting writes into the file, which would adopt it; a file the log does not hold
    is refused whether it is a prefix of the local file or not, and nothing is written."""
    for content in (DATA + b"more", DATA[:-1] + b"?"):
        with pytest.raises(ProvisionError, match="did not create"):
            provisioner.upload(SEEDED, local_file(tmp_path, content))
    assert changes(seeded) == []
    assert stored(seeded, seeded.id_of(SEEDED)) == DATA


def test_o25_an_upload_whose_bytes_come_back_otherwise_is_not_a_success(provisioner, fake, tmp_path):
    """O25: the platform stores something other than what was sent (same size, other bytes). The hash read back
    differs and the upload fails; it is never reported as done."""
    fake.corrupt_writes = True
    with pytest.raises(ProvisionError, match="SHA-256"):
        provisioner.upload(TARGET, local_file(tmp_path, BULKY))


def test_o25_a_path_that_is_a_directory_is_refused(provisioner, fake, tmp_path):
    fake.add_file("ingest/table.bin/inner.bin", b"x")
    with pytest.raises(ProvisionError, match="not a regular file"):
        provisioner.upload(TARGET, local_file(tmp_path, DATA))
    assert changes(fake) == []


def test_o25_an_upload_into_a_space_outside_the_prefixes_sends_nothing(provisioner, seeded, tmp_path):
    """O25 with O09: the upload goes through the same gate as every write; the space's live name is checked first."""
    seeded.space_name = "unrelated-space"
    with pytest.raises(ProvisionError, match="outside the configured prefixes"):
        provisioner.upload(TARGET, local_file(tmp_path, BULKY))
    assert changes(seeded) == []


# -- the streamed, hashed read -----------------------------------------------------------------------------------------


def test_o25_a_file_is_read_to_a_local_path_by_ranges_and_hashed_as_it_comes(provisioner, seeded, tmp_path):
    """O25: the read goes range by range, each of at most a chunk, and the local file appears only once it is whole."""
    file_id = provisioner.put_file("ingest/table.bin", BULKY)
    destination = tmp_path / "back" / "table.bin"
    destination.parent.mkdir()
    found = provisioner.read_file(file_id, dest=destination)
    assert found == digest(BULKY)
    assert destination.read_bytes() == BULKY
    assert sorted(p.name for p in destination.parent.iterdir()) == ["table.bin"]
    ranged = [r.range for r in seeded.requests if r.range]
    assert len(ranged) == math.ceil(len(BULKY) / CHUNK)
    assert ranged[0] == f"bytes=0-{CHUNK - 1}"


def test_o25_a_read_without_a_destination_only_hashes(provisioner, seeded):
    assert provisioner.read_file(seeded.id_of(SEEDED)) == digest(DATA)


def test_o25_a_read_of_a_file_outside_the_prefixes_sends_no_range(provisioner, seeded, client, tmp_path):
    """O25 with O09: each range is gated as a range read is, by the file's space read afresh, so a file outside the
    study's prefixes is refused before any of its content is asked for. An empty file needs no range, and is refused
    all the same, so a read outside the study never succeeds."""
    file_id = seeded.id_of(SEEDED)
    empty = seeded.add_file("study/empty.bin", b"")
    seeded.space_name = "elsewhere"
    for target, size in ((file_id, len(DATA)), (empty, 0)):
        with pytest.raises(OutOfScope, match="configured prefixes"):
            client.read_content(f"/data/{target}/content", provider="alpha", size=size, chunk_bytes=CHUNK, deadline_s=5)
    assert [r for r in seeded.requests if r.range] == []


def test_o25_a_read_through_another_provider_waits_until_it_sees_the_file(provisioner, seeded, ticker):
    """O25 with O07: a provider other than the data provider sees a file some seconds after it was written, answering
    400 `posix` `enoent` meanwhile. `read_file` through it waits, bounded, until it reports the file at its size, as
    every provisioning read does, and then reads the whole file."""
    file_id = seeded.id_of(SEEDED)
    seeded.unseen[(BETA_ID, file_id)] = 3
    assert provisioner.read_file(file_id, provider="beta") == digest(DATA)
    assert seeded.unseen[(BETA_ID, file_id)] == 0
    assert len(ticker.slept) >= 3


def test_o25_a_streamed_read_of_a_deleted_file_keeps_the_errno_so_it_reads_as_absent(provisioner, client, tmp_path):
    """O25: a range of a file deleted after its size was read is answered as Onedata 25.0 answers a deleted entry, 400
    `posix` `enoent`. The streamed read's error keeps the error id and the errno, as every other answer's does, so
    `answered_absent` tells it from another failure."""
    uploaded = provisioner.upload(TARGET, local_file(tmp_path, DATA))
    client.mutate("DELETE", f"/data/{uploaded.file_id}", provider="alpha")
    with pytest.raises(PlatformError) as gone:
        provisioner.read_file(uploaded.file_id, size=len(DATA))
    assert (gone.value.status, gone.value.code, gone.value.errno) == (400, "posix", "enoent")
    assert answered_absent(gone.value)


def test_o25_a_read_that_fails_part_way_leaves_no_file_at_its_destination(config, placement, seeded, tmp_path, ticker):
    """O25: the connection drops on the third range, after two have been written. The partial bytes are removed, so
    nothing at the destination can be taken for the file."""
    from expcore_onedata.provision import Provisioner, resolve_provider_ids

    file_id = seeded.add_file("ingest/table.bin", BULKY)
    ranges = []

    def dropping(request):
        if request.headers.get("Range"):
            ranges.append(request)
            if len(ranges) == 3:
                raise httpx.ReadError("connection reset")
        return seeded.respond(request)

    client = OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(dropping)))
    reader = Provisioner(
        client, config, placement.settings, provider_ids=resolve_provider_ids(config), log=tmp_path / "log.jsonl"
    )
    folder = tmp_path / "back"
    folder.mkdir()
    with pytest.raises(PlatformError) as dropped:
        reader.read_file(file_id, dest=folder / "table.bin")
    assert dropped.value.transport
    assert len(ranges) == 3
    assert list(folder.iterdir()) == []


def answering(config, answer):
    """A client whose federation answers the gate's reads (the space's root, `fid` right under it, the space's name)
    and every content request with `answer(request)`."""

    def handler(request):
        path = request.url.path
        if path == f"{ZONE}/spaces/{config.space.id}":
            return httpx.Response(200, json={"name": config.space.name})
        if path == f"{PROVIDER}/spaces/{config.space.id}":
            return httpx.Response(200, json={"fileId": "root-s"})
        if request.url.params.get("attribute") == "parent_id":
            return httpx.Response(200, json={"parent_id": "root-s"})
        return answer(request)

    return OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(handler)))


class Pieces(httpx.SyncByteStream):
    """A body made as it is read, `piece` bytes at a time, so nothing holds it whole unless the reader does."""

    def __init__(self, length, piece=16 * 1024):
        self.length, self.piece = length, piece

    def __iter__(self):
        sent = 0
        while sent < self.length:
            size = min(self.piece, self.length - sent)
            yield bytes(size)
            sent += size


LARGE = 32 << 20


def ranged_pieces(request):
    """A 206 for the range asked for, of a file of LARGE bytes, made as it is read."""
    first, last = (int(x) for x in request.headers["Range"].removeprefix("bytes=").split("-"))
    return httpx.Response(
        206, headers={"Content-Range": f"bytes {first}-{last}/{LARGE}"}, stream=Pieces(last - first + 1)
    )


def test_o25_the_read_keeps_its_memory_flat_whatever_the_size(config, monkeypatch):
    """O25: 32 MiB come back in ranges of 8 MiB, made as they are read. No answer of content is read whole, and the
    peak of memory the read allocates stays far below one range, let alone the file."""
    real = httpx.Response.read

    def read(self):
        assert "content-range" not in self.headers, "an answer of content was read whole"
        return real(self)

    monkeypatch.setattr(httpx.Response, "read", read)
    client = answering(config, ranged_pieces)
    chunks = []
    tracemalloc.start()
    try:
        found = client.read_content(
            "/data/fid/content",
            provider="alpha",
            size=LARGE,
            chunk_bytes=8 << 20,
            deadline_s=60,
            sink=lambda piece: chunks.append(len(piece)),
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert found == Digest(LARGE, hashlib.sha256(bytes(LARGE)).hexdigest())
    assert sum(chunks) == LARGE
    assert max(chunks) <= 64 * 1024
    assert peak < 2 << 20


@pytest.mark.parametrize(
    ("status", "body", "content_range"),
    [
        (200, b"abcd", None),  # the whole file, not the range asked for
        (206, b"abcd", None),  # no Content-Range
        (206, b"abcd", "bytes 1-4/8"),  # another start
        (206, b"abcd", "bytes 0-3/9"),  # the file's size changed since it was read
        (206, b"abcde", "bytes 0-3/8"),  # more bytes than the range
        (206, b"abc", "bytes 0-3/8"),  # fewer bytes than the range
    ],
)
def test_o25_a_range_answered_with_other_bytes_than_asked_for_is_refused(config, status, body, content_range):
    """O25: the read is bounded by the range: every answer must be a 206 for exactly the bytes asked for, of a file of
    the size it was read at, and must carry neither more nor fewer bytes."""
    headers = {"Content-Range": content_range} if content_range else {}
    client = answering(config, lambda request: httpx.Response(status, content=body, headers=headers))
    with pytest.raises(PlatformError):
        client.read_content("/data/fid/content", provider="alpha", size=8, chunk_bytes=4, deadline_s=5)


def test_o25_a_read_that_passes_its_deadline_stops(config):
    """O25: the deadline is checked as the bytes arrive, so a slow file ends the read instead of holding it open."""
    ticks = iter(range(0, 1000, 3))
    client = answering(config, ranged_pieces)
    with pytest.raises(PlatformError, match="deadline"):
        client.read_content(
            "/data/fid/content",
            provider="alpha",
            size=LARGE,
            chunk_bytes=8 << 20,
            deadline_s=10,
            clock=lambda: next(ticks),
        )


def test_o25_a_read_names_only_content_paths_and_a_declared_provider(config, seeded, client):
    with pytest.raises(OutOfScope, match="content only"):
        client.read_content("/data/fid", provider="alpha", size=4, chunk_bytes=4, deadline_s=5)
    with pytest.raises(OutOfScope, match="not declared"):
        client.read_content("/data/fid/content", provider="nowhere", size=4, chunk_bytes=4, deadline_s=5)
    with pytest.raises(OutOfScope, match="bound"):
        client.read_content("/data/fid/content", provider="alpha", size=-1, chunk_bytes=4, deadline_s=5)
    assert seeded.requests == []
