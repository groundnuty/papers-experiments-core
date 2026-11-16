"""A member of an uncompressed tar uploaded straight from its archive, never unpacked (O25): a `Slice` is a byte range
of a local file read as a file of its own, and `tar_members` gives each regular member of a tar as one."""

import builtins
import bz2
import gzip
import hashlib
import io
import lzma
import math
import os
import tarfile
from pathlib import Path

import pytest

from expcore_onedata.client import PlatformError
from expcore_onedata.provision import ProvisionError
from expcore_onedata.streamed import ArchiveRefused, Digest, Slice, chunks, tar_members
from expcore_onedata.testing import SETTINGS

CHUNK = SETTINGS["chunk_bytes"]
TARGET = "ingest/member.bin"
BEFORE = bytes((index * 13 + 5) % 256 for index in range(9 * CHUNK + 7))  # a member ahead of the one uploaded
MEMBER = bytes((index * 7 + index // 251) % 256 for index in range(40 * CHUNK + 100))  # 40 chunks and a short one
AFTER = bytes((index * 3 + 1) % 256 for index in range(3 * CHUNK + 11))  # and one after it
LONG = "deep/" + "a" * 120 + "/member.bin"  # past ustar's 100-byte name: a pax header carries it


def digest(content):
    return Digest(len(content), hashlib.sha256(content).hexdigest())


def regular(name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, io.BytesIO(data)


def archive(tmp_path, members, *, name="space.tar", fmt=tarfile.PAX_FORMAT):
    """An uncompressed tar of `members` (TarInfo, file object or None) in the given format."""
    path = tmp_path / "archives" / name
    path.parent.mkdir(exist_ok=True)
    with tarfile.open(path, "w", format=fmt) as tar:
        for info, data in members:
            tar.addfile(info, data)
    return path


def three(tmp_path):
    """An archive of three members whose bytes all differ, the middle one the uploaded one."""
    return archive(
        tmp_path, [regular("before.bin", BEFORE), regular("member.bin", MEMBER), regular("after.bin", AFTER)]
    )


def writes(fake):
    return [r for r in fake.requests if r.method == "PUT" and r.path.endswith("/content")]


def changes(fake):
    return [call for call in fake.provider_calls if call[1] in ("POST", "PUT", "DELETE")]


def stored(fake, file_id):
    return bytes(fake.nodes[file_id].content)


class Bounded:
    """A handle on the archive that records every byte position a read reaches."""

    def __init__(self, handle, reached):
        self._handle, self._reached = handle, reached

    def read(self, size=-1):
        assert size is not None, "the archive was read to its end"
        assert size >= 0, "the archive was read to its end"
        start = self._handle.tell()
        block = self._handle.read(size)
        if block:
            self._reached.append((start, start + len(block)))
        return block

    def readall(self):
        raise AssertionError("the archive was read to its end")

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._handle.close()


def watch_archive_reads(monkeypatch, path):
    """Record the byte ranges every read of `path` covers, however it is opened."""
    reached = []
    real = builtins.open

    def opened(file, mode="r", *args, **kwargs):
        handle = real(file, mode, *args, **kwargs)
        named = isinstance(file, str | os.PathLike) and Path(file).resolve() == path.resolve()
        return Bounded(handle, reached) if named and "r" in mode else handle

    monkeypatch.setattr(builtins, "open", opened)
    monkeypatch.setattr(io, "open", opened)
    return reached


# -- the slice ---------------------------------------------------------------------------------------------------------


def test_o25_a_slices_chunks_are_its_bytes_at_its_own_offsets(tmp_path):
    """O25: a slice's chunks are the file's bytes from its offset, at most a chunk each, numbered from the slice's own
    first byte; nothing before the offset or past its end is in them."""
    path = tmp_path / "whole.bin"
    path.write_bytes(BEFORE + MEMBER + AFTER)
    part = Slice(path, len(BEFORE), len(MEMBER))
    got = list(chunks(part, CHUNK))
    assert [offset for offset, _ in got] == list(range(0, len(MEMBER), CHUNK))
    assert max(len(block) for _, block in got) <= CHUNK
    assert b"".join(block for _, block in got) == MEMBER


def test_o25_a_slices_chunks_from_a_start_to_a_stop_are_that_range_of_it(tmp_path):
    """O25: `start` and `stop` are the slice's own offsets, as a resume and a prefix upload pass them."""
    path = tmp_path / "whole.bin"
    path.write_bytes(BEFORE + MEMBER + AFTER)
    part = Slice(path, len(BEFORE), len(MEMBER))
    got = list(chunks(part, CHUNK, start=5 * CHUNK + 3, stop=9 * CHUNK))
    assert got[0][0] == 5 * CHUNK + 3
    assert b"".join(block for _, block in got) == MEMBER[5 * CHUNK + 3 : 9 * CHUNK]


@pytest.mark.parametrize("stop", [None, len(MEMBER), len(MEMBER) + 1, len(MEMBER) + 10 * CHUNK])
def test_o25_a_slice_is_never_read_past_its_end_whatever_the_stop(tmp_path, monkeypatch, stop):
    """O25: a stop past the slice's end, or none, ends its chunks at its last byte; no read of the file reaches a byte
    of the next member, nor one before the slice's offset."""
    path = tmp_path / "whole.bin"
    path.write_bytes(BEFORE + MEMBER + AFTER)
    reached = watch_archive_reads(monkeypatch, path)
    got = b"".join(block for _, block in chunks(Slice(path, len(BEFORE), len(MEMBER)), CHUNK, stop=stop))
    assert got == MEMBER
    assert reached
    assert min(start for start, _ in reached) >= len(BEFORE)
    assert max(end for _, end in reached) <= len(BEFORE) + len(MEMBER)


def test_o25_a_slice_started_at_or_past_its_end_yields_nothing(tmp_path):
    """O25: a resume of a slice whose target holds it all already, or more, reads nothing of the file."""
    path = tmp_path / "whole.bin"
    path.write_bytes(BEFORE + MEMBER + AFTER)
    part = Slice(path, len(BEFORE), len(MEMBER))
    assert list(chunks(part, CHUNK, start=len(MEMBER))) == []
    assert list(chunks(part, CHUNK, start=len(MEMBER) + 5)) == []


@pytest.mark.parametrize(
    ("offset", "size"), [(-1, 10), (0, -1), (True, 10), (0, False), (1.5, 10), (0, 2.0), ("0", 10), (None, 10)]
)
def test_o25_a_slice_is_a_whole_offset_and_size_of_zero_or_more(tmp_path, offset, size):
    """O25: the range is checked where the slice is made, so a bad one never reaches a read."""
    with pytest.raises(ValueError, match="whole number of bytes"):
        Slice(tmp_path / "whole.bin", offset, size)


def test_o25_a_slice_names_its_file_by_a_path(tmp_path):
    """O25: a slice's file is a path (text is taken as one); anything else is refused."""
    assert Slice(str(tmp_path / "whole.bin"), 0, 1).path == tmp_path / "whole.bin"
    with pytest.raises(ValueError, match="path"):
        Slice(7, 0, 1)


# -- the members of a tar ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", [tarfile.PAX_FORMAT, tarfile.GNU_FORMAT, tarfile.USTAR_FORMAT])
def test_o25_each_regular_member_is_the_byte_range_of_the_archive_that_holds_it(tmp_path, fmt):
    """O25: every regular member, a name past 100 bytes and an empty member included, is a slice of the archive whose
    bytes are the member's, by its name and in the archive's order."""
    members = [("before.bin", BEFORE), ("member.bin", MEMBER), ("empty.bin", b""), ("after.bin", AFTER)]
    if fmt != tarfile.USTAR_FORMAT:  # ustar holds no name past its 100 + 155 bytes split
        members.append((LONG, MEMBER[:777]))
    path = archive(tmp_path, [regular(name, data) for name, data in members], fmt=fmt)
    found = tar_members(path)
    assert list(found) == [name for name, _ in members]
    for name, data in members:
        assert found[name].path == path
        assert found[name].size == len(data)
        assert b"".join(block for _, block in chunks(found[name], CHUNK)) == data


@pytest.mark.parametrize(
    ("kind", "pack"),
    [
        ("gzip", gzip.compress),
        ("bzip2", bz2.compress),
        ("xz", lzma.compress),
        ("zstd", lambda data: b"\x28\xb5\x2f\xfd" + data),
        ("lz4", lambda data: b"\x04\x22\x4d\x18" + data),
    ],
)
def test_o25_a_compressed_archive_is_refused(tmp_path, kind, pack):
    """O25: a compressed tar holds no member as a byte range of the file, so it is refused, naming the compression."""
    plain = three(tmp_path)
    packed = tmp_path / "archives" / "packed.tar"
    packed.write_bytes(pack(plain.read_bytes()))
    with pytest.raises(ArchiveRefused, match=f"compressed .{kind}."):
        tar_members(packed)


def sparse_pax(name):
    """A pax member in GNU's sparse format 1.0: its data starts with the sparse map, so it is no plain byte range."""
    info = tarfile.TarInfo(name)
    body = b"1\n0\n4\n".ljust(512, b"\0") + b"data"
    info.size = len(body)
    info.pax_headers = {
        "GNU.sparse.major": "1",
        "GNU.sparse.minor": "0",
        "GNU.sparse.name": name,
        "GNU.sparse.realsize": "8192",
    }
    return info, io.BytesIO(body)


def gnu_sparse(name):
    info = tarfile.TarInfo(name)
    info.size = 4
    info.type = tarfile.GNUTYPE_SPARSE
    return info, io.BytesIO(b"data")


def link(name, kind):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = "member.bin"
    return info, None


def special(name, kind):
    info = tarfile.TarInfo(name)
    info.type = kind
    return info, None


@pytest.mark.parametrize(
    ("entry", "fmt", "why"),
    [
        (lambda: sparse_pax("sparse.bin"), tarfile.PAX_FORMAT, "sparse"),
        (lambda: gnu_sparse("sparse.bin"), tarfile.GNU_FORMAT, "sparse"),
        (lambda: link("hard.bin", tarfile.LNKTYPE), tarfile.PAX_FORMAT, "link"),
        (lambda: link("soft.bin", tarfile.SYMTYPE), tarfile.PAX_FORMAT, "link"),
        (lambda: special("dir", tarfile.DIRTYPE), tarfile.PAX_FORMAT, "not a regular file"),
        (lambda: special("fifo", tarfile.FIFOTYPE), tarfile.PAX_FORMAT, "not a regular file"),
        (lambda: special("char", tarfile.CHRTYPE), tarfile.PAX_FORMAT, "not a regular file"),
        (lambda: special("block", tarfile.BLKTYPE), tarfile.PAX_FORMAT, "not a regular file"),
    ],
)
def test_o25_an_archive_holding_a_member_that_is_no_plain_file_is_refused(tmp_path, entry, fmt, why):
    """O25: a sparse member's bytes in the archive are not its content, a link has none of its own, and a directory or
    a device is no file to upload: the whole archive is refused, naming the member and why."""
    path = archive(tmp_path, [regular("member.bin", MEMBER), entry()], fmt=fmt)
    with pytest.raises(ArchiveRefused, match=why):
        tar_members(path)


def test_o25_a_name_held_twice_is_refused(tmp_path):
    """O25: a tar may hold one name twice (extraction keeps the last); which bytes are the file's is then not known."""
    path = archive(
        tmp_path, [regular("member.bin", MEMBER), regular("after.bin", AFTER), regular("member.bin", BEFORE)]
    )
    with pytest.raises(ArchiveRefused, match="twice"):
        tar_members(path)


@pytest.mark.parametrize("name", ["/abs/member.bin", "../member.bin", "a/../member.bin", "./member.bin", "a//b.bin"])
def test_o25_a_member_whose_name_is_no_path_in_a_space_is_refused(tmp_path, name):
    """O25 with O09: each name must be a path relative to the space's root with plain parts (`safe_path`)."""
    path = archive(tmp_path, [regular("member.bin", MEMBER), regular(name, AFTER)])
    with pytest.raises(ArchiveRefused, match="not a path"):
        tar_members(path)


def test_o25_an_archive_cut_short_inside_a_member_is_refused(tmp_path):
    """O25: the last member's bytes reach past the archive's end, so it is no whole member."""
    path = three(tmp_path)
    found = tar_members(path)
    cut = found["after.bin"].offset + 100
    path.write_bytes(path.read_bytes()[:cut])
    with pytest.raises(ArchiveRefused):
        tar_members(path)


@pytest.mark.parametrize("content", [b"", b"no tar at all\n" * 100])
def test_o25_a_file_that_is_no_tar_is_refused(tmp_path, content):
    """O25: an empty file and text are no tar: refused, never read as an archive of no members."""
    path = tmp_path / "archives" / "plain.tar"
    path.parent.mkdir()
    path.write_bytes(content)
    with pytest.raises(ArchiveRefused, match="not an uncompressed tar"):
        tar_members(path)


# -- an upload from a member -------------------------------------------------------------------------------------------


def test_o25_a_member_is_uploaded_from_its_archive_a_chunk_at_a_time(provisioner, fake, tmp_path, monkeypatch):
    """O25: the member goes in a chunk at a time from its own byte range of the archive, verified by its hash; no read
    of the archive reaches outside that range, and the file is logged as a creation."""
    path = three(tmp_path)
    part = tar_members(path)["member.bin"]
    reached = watch_archive_reads(monkeypatch, path)
    result = provisioner.upload(TARGET, part)
    assert (result.action, result.digest, result.sent_bytes) == ("created", digest(MEMBER), len(MEMBER))
    assert stored(fake, result.file_id) == MEMBER
    assert len(writes(fake)) == math.ceil(len(MEMBER) / CHUNK)
    assert min(start for start, _ in reached) >= part.offset
    assert max(end for _, end in reached) <= part.offset + part.size
    assert result.file_id in [line["id"] for line in provisioner.logged()]


def test_o25_a_member_already_uploaded_is_skipped_and_nothing_is_sent(provisioner, fake, tmp_path):
    """O25: run again, the upload finds the target holding the member's size and SHA-256 and sends nothing."""
    part = tar_members(three(tmp_path))["member.bin"]
    first = provisioner.upload(TARGET, part)
    before = list(changes(fake))
    again = provisioner.upload(TARGET, part)
    assert (again.action, again.sent_bytes, again.file_id, again.digest) == (
        "skipped",
        0,
        first.file_id,
        digest(MEMBER),
    )
    assert changes(fake) == before


def test_o25_an_upload_of_a_member_cut_by_a_reset_resumes_from_the_members_own_bytes(provisioner, fake, tmp_path):
    """O25: the prefix a resume compares is the member's own first bytes, not the archive's (whose first bytes are a
    header and another member), so the cut upload resumes from where the target ends and sends no byte twice."""
    part = tar_members(three(tmp_path))["member.bin"]
    fake.writes_before_reset = 5
    with pytest.raises(PlatformError) as cut:
        provisioner.upload(TARGET, part)
    assert cut.value.transport
    fake.writes_before_reset = None
    done = len(writes(fake))
    result = provisioner.upload(TARGET, part)
    assert (result.action, result.sent_bytes) == ("resumed", len(MEMBER) - 5 * CHUNK)
    assert [int(r.query["offset"]) for r in writes(fake)[done:]] == list(range(5 * CHUNK, len(MEMBER), CHUNK))
    assert stored(fake, result.file_id) == MEMBER


def test_o25_a_member_uploaded_to_a_stop_leaves_that_prefix_and_the_next_upload_resumes(provisioner, fake, tmp_path):
    """O25: `stop` is a prefix of the member, and a later upload without one resumes from it into the same file."""
    part = tar_members(three(tmp_path))["member.bin"]
    first = provisioner.upload(TARGET, part, stop=3 * CHUNK + 10)
    assert (first.action, first.digest) == ("created", digest(MEMBER[: 3 * CHUNK + 10]))
    whole = provisioner.upload(TARGET, part)
    assert (whole.action, whole.file_id, whole.digest) == ("resumed", first.file_id, digest(MEMBER))
    assert stored(fake, first.file_id) == MEMBER


@pytest.mark.parametrize("stop", [0, len(MEMBER) + 1])
def test_o25_a_stop_outside_the_member_is_refused_before_anything_is_sent(provisioner, fake, tmp_path, stop):
    """O25: the member's size is its size, never the archive's: a stop past it is refused as for a local file."""
    part = tar_members(three(tmp_path))["member.bin"]
    with pytest.raises(ProvisionError, match="stop"):
        provisioner.upload(TARGET, part, stop=stop)
    assert changes(fake) == []


def test_o44_a_target_that_differs_from_the_member_is_written_again_from_the_members_bytes(provisioner, fake, tmp_path):
    """O44: a target this provisioner made with another byte is written again from the member's first byte."""
    part = tar_members(three(tmp_path))["member.bin"]
    made = provisioner.upload(TARGET, part)
    fake.nodes[made.file_id].content[100] ^= 0xFF
    result = provisioner.upload(TARGET, part)
    assert (result.action, result.file_id) == ("restarted", made.file_id)
    assert stored(fake, made.file_id) == MEMBER


def test_o25_a_slice_reaching_past_its_file_is_refused_before_anything_is_sent(provisioner, fake, tmp_path):
    """O25: a slice whose file no longer holds its whole range (the archive was cut, or the slice is wrong) is refused
    before anything is asked, as a local file that is not there is."""
    path = three(tmp_path)
    part = tar_members(path)["after.bin"]
    path.write_bytes(path.read_bytes()[: part.offset + 10])
    with pytest.raises(ProvisionError, match="past the end of its file"):
        provisioner.upload(TARGET, part)
    assert changes(fake) == []


def test_o44_a_range_of_a_member_is_written_into_the_same_range_of_a_file(provisioner, fake, tmp_path):
    """O44: `write_range` reads its range from the member's own bytes, and a range past the member is refused."""
    part = tar_members(three(tmp_path))["member.bin"]
    provisioner.make_dir("ingest")
    file_id = provisioner.preallocate(TARGET, len(MEMBER))
    sent = provisioner.write_range(file_id, part, 2 * CHUNK, 6 * CHUNK + 9)
    assert sent == digest(MEMBER[2 * CHUNK : 6 * CHUNK + 9])
    assert stored(fake, file_id)[2 * CHUNK : 6 * CHUNK + 9] == MEMBER[2 * CHUNK : 6 * CHUNK + 9]
    with pytest.raises(ProvisionError, match="within the local file"):
        provisioner.write_range(file_id, part, 0, len(MEMBER) + 1)


@pytest.mark.parametrize("first", ["BZh-first.bin", "LZIP-first.bin"])
def test_o25_a_plain_tar_whose_first_name_looks_like_a_compression_is_read(tmp_path, first):
    """O25: a tar's first header begins with its first member's name, so a name that starts like a compressed file's
    first bytes does not make a plain tar read as compressed: the magic counts only where no tar header is."""
    path = archive(tmp_path, [regular(first, BEFORE), regular("member.bin", MEMBER)])
    assert list(tar_members(path)) == [first, "member.bin"]
