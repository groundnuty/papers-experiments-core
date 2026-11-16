"""An upload's listings cache (O67): one operation's uploads list each directory once, so a flat directory's ingest is
linear, and a cached entry is read again by its id before it decides a skip, a resume or a restart."""

import hashlib

import pytest

from expcore_onedata.client import PlatformError
from expcore_onedata.provision import ProvisionError
from expcore_onedata.streamed import Digest
from expcore_onedata.testing import SETTINGS

CHUNK = SETTINGS["chunk_bytes"]


def content(index, size=3 * CHUNK + 17):
    return bytes((index * 31 + offset * 7) % 256 for offset in range(size))


def digest(data):
    return Digest(len(data), hashlib.sha256(data).hexdigest())


def local(tmp_path, name, data):
    path = tmp_path / "local" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def listings_sent(fake):
    """Every listing of a directory's children the fake answered."""
    return [r for r in fake.requests if r.method == "GET" and r.path.endswith("/children")]


def stored(fake, path):
    return bytes(fake.nodes[fake.id_of(path)].content)


def ingest(provisioner, tmp_path, names, *, listings):
    """Upload one file per name (each its own content), all with the same cache or none; return the results."""
    return [
        provisioner.upload(name, local(tmp_path, name.replace("/", "_"), content(index)), listings=listings)
        for index, name in enumerate(names)
    ]


FLAT = [f"flat/file-{index:03d}.bin" for index in range(25)]
NESTED = [f"tree/{a}/{b}/leaf-{c}.bin" for a in ("x", "y") for b in ("p", "q", "r") for c in range(3)]


@pytest.mark.parametrize("names", [FLAT, NESTED, FLAT[:1]])
def test_o67_the_same_files_and_actions_with_the_cache_as_without_it(provisioner, fake, tmp_path, names):
    """O67: uploading the same files with one cache gives the same actions, digests and stored bytes as without it, and
    so does uploading them again (every one skipped)."""
    cached = ingest(provisioner, tmp_path, names, listings={})
    assert [r.action for r in cached] == ["created"] * len(names)
    for index, (name, result) in enumerate(zip(names, cached, strict=True)):
        assert result.digest == digest(content(index))
        assert stored(fake, name) == content(index)
    again = ingest(provisioner, tmp_path, names, listings={})
    assert [(r.action, r.file_id) for r in again] == [("skipped", r.file_id) for r in cached]
    uncached = ingest(provisioner, tmp_path, names, listings=None)
    assert [(r.action, r.file_id) for r in uncached] == [("skipped", r.file_id) for r in cached]


def test_o67_a_flat_directory_is_listed_once_with_the_cache_and_once_per_file_without_it(provisioner, fake, tmp_path):
    """O67: without a cache every upload lists the target's directory whole, so n files in one directory cost n
    listings that grow with it; with one cache the directory is listed once, and each file made through it is added,
    so a flat directory's ingest is linear in its files."""
    ingest(provisioner, tmp_path, FLAT, listings={})
    assert len(listings_sent(fake)) == 1  # the root, when `flat` was made: `flat` itself was made empty, never listed
    before = len(listings_sent(fake))
    ingest(provisioner, tmp_path, [n.replace("flat/", "flat2/") for n in FLAT], listings=None)
    assert len(listings_sent(fake)) - before == 2 * len(FLAT)  # the root and `flat2` again for every file


def test_o67_a_cached_file_changed_behind_the_cache_is_compared_as_it_is_now(provisioner, fake, tmp_path):
    """O67: the cache holds the file at its size when it was made; another write then changes one byte. The upload
    reads the entry again by its id and compares the bytes the file holds now, so it is restarted, never skipped."""
    cache = {}
    source = local(tmp_path, "one.bin", content(1))
    made = provisioner.upload("dir/one.bin", source, listings=cache)
    fake.nodes[made.file_id].content[5] ^= 0xFF
    result = provisioner.upload("dir/one.bin", source, listings=cache)
    assert (result.action, result.file_id) == ("restarted", made.file_id)
    assert stored(fake, "dir/one.bin") == content(1)


def test_o67_a_cached_file_cut_short_behind_the_cache_is_resumed_from_where_it_ends_now(provisioner, fake, tmp_path):
    """O67: the cache saw the file whole; it was cut to two chunks since. The size the entry is compared at is the one
    read again, so the upload resumes from the two chunks and sends the rest, never skipping on the cached size."""
    cache = {}
    source = local(tmp_path, "one.bin", content(1))
    made = provisioner.upload("dir/one.bin", source, listings=cache)
    del fake.nodes[made.file_id].content[2 * CHUNK :]
    result = provisioner.upload("dir/one.bin", source, listings=cache)
    assert (result.action, result.sent_bytes) == ("resumed", len(content(1)) - 2 * CHUNK)
    assert stored(fake, "dir/one.bin") == content(1)


def test_o67_a_cached_file_removed_behind_the_cache_is_made_again(provisioner, fake, tmp_path):
    """O67: a removed file's id still answers a read of its size from its tombstone on 25.0, so the entry is read
    again by attributes that answer a removed entry as gone; the directory is then listed afresh and the file made."""
    cache = {}
    source = local(tmp_path, "one.bin", content(1))
    made = provisioner.upload("dir/one.bin", source, listings=cache)
    fake._delete(made.file_id)
    result = provisioner.upload("dir/one.bin", source, listings=cache)
    assert result.action == "created"
    assert result.file_id != made.file_id
    assert stored(fake, "dir/one.bin") == content(1)


def test_o67_a_cached_file_replaced_behind_the_cache_is_the_new_one(provisioner, fake, tmp_path):
    """O67: another writer removed the file and made another of the name with other bytes. The cached id is gone, so
    the directory is listed afresh: the new file, which this provisioner did not make, is refused, never written."""
    cache = {}
    source = local(tmp_path, "one.bin", content(1))
    made = provisioner.upload("dir/one.bin", source, listings=cache)
    fake._delete(made.file_id)
    other = fake.add_file("dir/one.bin", content(2))
    with pytest.raises(ProvisionError, match="did not create"):
        provisioner.upload("dir/one.bin", source, listings=cache)
    assert bytes(fake.nodes[other].content) == content(2)


def test_o67_a_cached_file_renamed_behind_the_cache_is_not_taken_for_the_name(provisioner, fake, tmp_path):
    """O67: the cached id now names a file of another name; it is no longer this path's, so the directory is listed
    afresh and the path, now free, is made again."""
    cache = {}
    source = local(tmp_path, "one.bin", content(1))
    made = provisioner.upload("dir/one.bin", source, listings=cache)
    fake.nodes[made.file_id].name = "renamed.bin"
    result = provisioner.upload("dir/one.bin", source, listings=cache)
    assert result.action == "created"
    assert result.file_id != made.file_id


def test_o67_a_name_absent_from_the_cache_is_made_and_a_concurrent_one_fails_loudly(provisioner, fake, tmp_path):
    """O67: the cache says what the directory held when it was listed and what was made through it; a file another
    writer made since is unseen, so the upload's creation of the name is refused by the platform, loudly, and nothing
    is written into the other writer's file (a concurrent writer is outside the cache: `bulkingest` holds the
    federation alone)."""
    cache = {}
    provisioner.upload("dir/one.bin", local(tmp_path, "one.bin", content(1)), listings=cache)
    other = fake.add_file("dir/two.bin", content(2))
    with pytest.raises((PlatformError, ProvisionError)):
        provisioner.upload("dir/two.bin", local(tmp_path, "two.bin", content(3)), listings=cache)
    assert bytes(fake.nodes[other].content) == content(2)
