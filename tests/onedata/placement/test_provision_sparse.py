"""A file at its final size with only some ranges written: preallocated by an empty write, written at explicit offsets
and read back by its prefix, never with a write that truncates (O44, #107)."""

import hashlib

import pytest

from expcore_onedata.provision import ProvisionError
from expcore_onedata.streamed import Digest
from expcore_onedata.testing import ALPHA_ID, BETA_ID, DATA, SETTINGS

CHUNK = SETTINGS["chunk_bytes"]
FINAL = 4 * CHUNK + 500
SOURCE = bytes((i * 13 + i // 7) % 256 for i in range(FINAL))
TARGET = "study/day.bin"


def writes(fake):
    return [r for r in fake.requests if r.method == "PUT" and r.path.endswith("/content")]


def local(tmp_path, content=SOURCE):
    path = tmp_path / "staged.bin"
    path.write_bytes(content)
    return path


@pytest.fixture
def directory(fake):
    """The file's directory, which a preallocation never creates."""
    fake.add_file("study/kept.bin", DATA)
    return fake


def test_o44_a_preallocated_file_has_its_final_size_at_once_and_no_block_anywhere(provisioner, directory):
    """O44: the file is created and extended in one step, an empty body at offset FINAL marked as an octet stream, so
    its size is FINAL before any byte is written and no provider holds a block of it; the change log holds it."""
    file_id = provisioner.preallocate(TARGET, FINAL)
    node = directory.nodes[file_id]
    assert (len(node.content), directory.held(TARGET, ALPHA_ID), directory.held(TARGET, BETA_ID)) == (FINAL, [], [])
    (extend,) = writes(directory)
    assert (extend.query, extend.body, directory.content_types) == (
        {"offset": str(FINAL)},
        b"",
        ["application/octet-stream"],
    )
    assert [line["id"] for line in provisioner.logged()] == [file_id]


def test_o44_a_preallocation_over_an_existing_name_or_into_a_missing_directory_is_refused(provisioner, directory):
    """O44: a preallocation makes a new file and nothing else: an existing entry at the path and a directory that is
    not there are refused before anything is written."""
    with pytest.raises(ProvisionError, match="exists"):
        provisioner.preallocate("study/kept.bin", FINAL)
    with pytest.raises(ProvisionError, match="directory"):
        provisioner.preallocate("elsewhere/day.bin", FINAL)
    assert [c for c in directory.provider_calls if c[1] in ("POST", "PUT", "DELETE")] == []


def test_o44_a_range_is_written_at_its_offsets_and_holds_exactly_its_blocks(provisioner, directory, tmp_path):
    """O44: the range's bytes go chunk by chunk, each at its offset, and the block map holds exactly the written
    ranges while the size stays FINAL; the digest is that of the bytes sent."""
    file_id = provisioner.preallocate(TARGET, FINAL)
    staged = local(tmp_path)
    assert provisioner.write_range(file_id, staged, 0, CHUNK + 10) == Digest(
        CHUNK + 10, hashlib.sha256(SOURCE[: CHUNK + 10]).hexdigest()
    )
    sent = provisioner.write_range(file_id, staged, CHUNK + 10, 3 * CHUNK)
    assert sent.size == 2 * CHUNK - 10
    assert [int(r.query["offset"]) for r in writes(directory)[1:]] == [0, CHUNK, CHUNK + 10, 2 * CHUNK + 10]
    assert directory.held(TARGET, ALPHA_ID) == [[0, 3 * CHUNK]]
    assert bytes(directory.nodes[file_id].content) == SOURCE[: 3 * CHUNK] + bytes(FINAL - 3 * CHUNK)


def test_o44_a_range_into_a_file_the_provisioner_did_not_make_or_outside_the_source_is_refused(
    provisioner, seeded, tmp_path
):
    """O44 with O09: writing into a file adopts it, so one the change log does not hold is refused; so is a range that
    is empty, reversed or past the local file's end. Nothing is written."""
    stranger = seeded.id_of("study/data.bin")
    with pytest.raises(ProvisionError, match="did not create"):
        provisioner.write_range(stranger, local(tmp_path), 0, 10)
    file_id = provisioner.preallocate("study/day.bin", FINAL)
    for start, stop in ((10, 10), (20, 10), (-1, 10), (0, FINAL + 1)):
        with pytest.raises(ProvisionError, match="range"):
            provisioner.write_range(file_id, local(tmp_path), start, stop)
    assert len(writes(seeded)) == 1  # the preallocation's own
    assert bytes(seeded.nodes[stranger].content) == DATA


def test_o44_a_prefix_of_a_longer_file_is_read_back_by_its_hash_and_nothing_past_it(provisioner, directory, tmp_path):
    """O44: the prefix of a file of FINAL bytes is read by ranges that stop at its end, each answer checked against
    the file's size, so the gap after it is never read."""
    file_id = provisioner.preallocate(TARGET, FINAL)
    provisioner.write_range(file_id, local(tmp_path), 0, CHUNK + 10)
    before = len(directory.requests)
    found = provisioner.read_file(file_id, size=FINAL, stop=CHUNK + 10)
    assert found == Digest(CHUNK + 10, hashlib.sha256(SOURCE[: CHUNK + 10]).hexdigest())
    ranges = [r.range for r in directory.requests[before:] if r.range]
    assert ranges == [f"bytes=0-{CHUNK - 1}", f"bytes={CHUNK}-{CHUNK + 9}"]
    with pytest.raises(ProvisionError, match="stop"):
        provisioner.read_file(file_id, size=FINAL, stop=FINAL + 1)
