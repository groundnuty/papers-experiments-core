"""A manifest's files ingested into a space as its sole writer (O67): the ingest holds the federation alone, uploads
through one listings cache, checks each source against the manifest before it is sent, and ends with a fresh listing
of its root compared with the manifest, which any difference fails, naming it."""

import hashlib
import io
import json
import tarfile

import pytest
from fault_support import faults_table

from expcore_onedata.admission import INGEST, OUTAGE_RUN, FederationLock, fault_settings_of
from expcore_onedata.bulkingest import IngestMismatch, IngestRefused, ManifestFile, ingest_files, ingest_hold
from expcore_onedata.config import load_platform
from expcore_onedata.streamed import tar_members
from expcore_onedata.testing import SETTINGS, write_platform
from expcore_onedata.testing.world import with_placement

CHUNK = SETTINGS["chunk_bytes"]
ROOT = "datasets/one"
HOLDER = "federation.holder"


def content(index, size=2 * CHUNK + 9):
    return bytes((index * 17 + offset * 5) % 256 for offset in range(size))


@pytest.fixture
def platform_file(tmp_path):
    """The placement fixture's platform, with `[faults]`, so the federation has a lock an ingest can hold alone."""

    def edit(data):
        with_placement(data)
        data["faults"] = faults_table()

    return write_platform(tmp_path, edit)


@pytest.fixture
def lock(config):
    return FederationLock(fault_settings_of(config))


def manifest(tmp_path, paths, *, wrong=()):
    """A manifest of one file per path, each its own content; a path in `wrong` declares another file's hash."""
    files = []
    for index, path in enumerate(paths):
        data = content(index)
        source = tmp_path / "sources" / path.replace("/", "_")
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(data)
        declared = hashlib.sha256(content(index + 1000) if path in wrong else data).hexdigest()
        files.append(ManifestFile(path, source, len(data), declared))
    return files


FLAT = [f"file-{index:02d}.bin" for index in range(12)]
TREE = ["top.bin", "a/one.bin", "a/b/two.bin", "a/b/three.bin", "c/four.bin"]


def posts(fake):
    return [r for r in fake.requests if r.method in ("POST", "PUT", "DELETE")]


def listings_of(fake, directory_id):
    return [r for r in fake.requests if r.method == "GET" and r.path.endswith(f"/data/{directory_id}/children")]


# -- the hold ----------------------------------------------------------------------------------------------------------


def test_o67_an_ingest_without_the_federation_held_alone_is_refused_before_anything_is_sent(
    provisioner, fake, tmp_path
):
    """O67: the cache is right only for the space's sole writer, so an ingest outside an exclusive hold of its
    federation is refused before any request."""
    with pytest.raises(IngestRefused, match="alone"):
        ingest_files(provisioner, manifest(tmp_path, TREE), root=ROOT)
    assert posts(fake) == []


def test_o67_an_ingest_under_a_shared_admission_is_refused(provisioner, fake, tmp_path, lock):
    """O67: a shared admission is a run's or a tool's: others write beside it."""
    with lock.held(exclusive=False, timeout_s=1), pytest.raises(IngestRefused, match="alone"):
        ingest_files(provisioner, manifest(tmp_path, TREE), root=ROOT)
    assert posts(fake) == []


def test_o67_an_ingest_under_another_exclusive_purpose_is_refused(provisioner, fake, tmp_path, lock):
    """O67: an exclusive hold taken for an outage run is that run's, and its holder record names it, not the ingest."""
    with lock.held(exclusive=True, timeout_s=1, purpose=OUTAGE_RUN), pytest.raises(IngestRefused, match="ingest"):
        ingest_files(provisioner, manifest(tmp_path, TREE), root=ROOT)
    assert posts(fake) == []


def test_o67_an_ingest_held_for_another_federation_is_refused(provisioner, fake, tmp_path):
    """O67: the hold must be the provisioner's own federation's: one of another lock file holds nothing of it."""
    other = tmp_path / "other"
    other.mkdir()

    def edit(data):
        with_placement(data)
        data["faults"] = faults_table(journal=str(other / "fault-journal"))

    config = load_platform(write_platform(other, edit))
    with ingest_hold(config, label="alpha", timeout_s=1), pytest.raises(IngestRefused, match="another federation"):
        ingest_files(provisioner, manifest(tmp_path, TREE), root=ROOT)
    assert posts(fake) == []


def test_o67_a_platform_without_faults_has_no_lock_to_hold_and_is_refused(tmp_path, client, fake):
    """O67: without `[faults]` the federation has no lock, so no ingest can show it is the sole writer."""
    bare = tmp_path / "bare"
    bare.mkdir()
    config = load_platform(write_platform(bare, with_placement))
    with pytest.raises(IngestRefused, match=r"\[faults\]"), ingest_hold(config, label="alpha", timeout_s=1):
        pass


def test_o67_the_hold_keeps_every_other_admission_out_and_names_the_ingest(config, lock):
    """O67: while the ingest holds the federation, no run or tool is admitted (shared) and no other exclusive holder;
    its holder record names the ingest and the space; once it ends, the federation admits again."""
    with ingest_hold(config, label="alpha", timeout_s=1):
        assert lock.try_acquire(exclusive=False) is None
        assert lock.try_acquire(exclusive=True) is None
        record = json.loads((fault_settings_of(config).journal / HOLDER).read_text())
        assert (record["purpose"], record["target"]) == (INGEST, "alpha")
    admitted = lock.try_acquire(exclusive=False)
    assert admitted is not None
    admitted.close()


# -- the ingest --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("paths", [FLAT, TREE])
def test_o67_a_manifest_is_ingested_and_its_listing_matches(provisioner, fake, tmp_path, config, paths):
    """O67: every file is made under the root with its content, the report says what was done for each, and the final
    listing matches the manifest; an ingest run again skips every file and matches again."""
    files = manifest(tmp_path, paths)
    with ingest_hold(config, label="alpha", timeout_s=1):
        report = ingest_files(provisioner, files, root=ROOT)
    assert [f["path"] for f in report.files] == paths
    assert {f["action"] for f in report.files} == {"created"}
    for index, path in enumerate(paths):
        assert bytes(fake.nodes[fake.id_of(f"{ROOT}/{path}")].content) == content(index)
    assert report.listing["files"] == len(paths)
    with ingest_hold(config, label="alpha", timeout_s=1):
        again = ingest_files(provisioner, files, root=ROOT)
    assert {f["action"] for f in again.files} == {"skipped"}


def test_o67_a_flat_root_is_listed_once_while_files_go_in(provisioner, fake, tmp_path, config):
    """O67: the uploads list the flat root once (when it is made, empty, not even that); the final check lists it once
    more, afresh, so the listings do not grow with the files."""
    with ingest_hold(config, label="alpha", timeout_s=1):
        ingest_files(provisioner, manifest(tmp_path, FLAT), root=ROOT)
    assert len(listings_of(fake, fake.id_of(ROOT))) == 1  # the final check's


def test_o67_a_source_that_is_not_its_manifests_bytes_is_refused_before_it_is_sent(provisioner, fake, tmp_path, config):
    """O67: each source is hashed before its upload; one whose bytes are not the manifest's fails the ingest, naming
    it, with nothing of it sent, and the files before it stay as made."""
    files = manifest(tmp_path, TREE, wrong={"a/b/two.bin"})
    with ingest_hold(config, label="alpha", timeout_s=1), pytest.raises(IngestMismatch, match=r"a/b/two\.bin"):
        ingest_files(provisioner, files, root=ROOT)
    assert not fake.exists(f"{ROOT}/a/b/two.bin")
    assert fake.exists(f"{ROOT}/a/one.bin")


@pytest.mark.parametrize(
    ("paths", "why"),
    [
        (["a.bin", "a.bin"], "twice"),
        (["a", "a/b.bin"], "directory"),
        (["../escape.bin"], "path"),
        (["/abs.bin"], "path"),
        ([], "no file"),
    ],
)
def test_o67_a_manifest_that_cannot_be_a_tree_is_refused_before_anything_is_sent(
    provisioner, fake, tmp_path, config, paths, why
):
    """O67: a path held twice, a file that is also another's directory, a path that is not plain and relative, and an
    empty manifest are refused before any request."""
    one = manifest(tmp_path, ["one.bin"])[0]
    files = [ManifestFile(path, one.source, one.size, one.sha256) for path in paths]
    with ingest_hold(config, label="alpha", timeout_s=1), pytest.raises(IngestRefused, match=why):
        ingest_files(provisioner, files, root=ROOT)
    assert posts(fake) == []


# -- the final listing -------------------------------------------------------------------------------------------------


def behind(after, action):
    """An `on_file` that acts as another writer once the file at index `after` is in."""

    def on_file(record):
        if record["index"] == after:
            action()

    return on_file


def test_o67_an_entry_the_manifest_does_not_hold_fails_the_ingest_naming_it(provisioner, fake, tmp_path, config):
    """O67: another writer adds a file under the root while the ingest runs; the final listing finds it and the ingest
    fails, naming it."""
    with ingest_hold(config, label="alpha", timeout_s=1), pytest.raises(IngestMismatch, match=r"stray\.bin"):
        ingest_files(
            provisioner,
            manifest(tmp_path, TREE),
            root=ROOT,
            on_file=behind(1, lambda: fake.add_file(f"{ROOT}/a/stray.bin", b"x")),
        )


def test_o67_an_extra_directory_fails_the_ingest_too(provisioner, fake, tmp_path, config):
    with ingest_hold(config, label="alpha", timeout_s=1), pytest.raises(IngestMismatch, match="empty-dir"):
        ingest_files(
            provisioner,
            manifest(tmp_path, TREE),
            root=ROOT,
            on_file=behind(4, lambda: fake.add_file(f"{ROOT}/empty-dir/x", b"x")),
        )


def test_o67_a_file_removed_after_its_upload_fails_the_ingest_naming_it(provisioner, fake, tmp_path, config):
    with ingest_hold(config, label="alpha", timeout_s=1), pytest.raises(IngestMismatch, match=r"missing.*top\.bin"):
        ingest_files(
            provisioner,
            manifest(tmp_path, TREE),
            root=ROOT,
            on_file=behind(3, lambda: fake._delete(fake.id_of(f"{ROOT}/top.bin"))),
        )


def test_o67_a_file_cut_short_after_its_upload_fails_the_ingest_naming_its_size(provisioner, fake, tmp_path, config):
    def cut():
        del fake.nodes[fake.id_of(f"{ROOT}/a/one.bin")].content[CHUNK:]

    with ingest_hold(config, label="alpha", timeout_s=1), pytest.raises(IngestMismatch, match=r"a/one\.bin.*size"):
        ingest_files(provisioner, manifest(tmp_path, TREE), root=ROOT, on_file=behind(2, cut))


def test_o67_the_whole_space_as_root_is_compared_whole(provisioner, fake, tmp_path, config):
    """O67: with no root the ingest owns the space: a file the space already held fails the final check."""
    fake.add_file("baseline.txt", b"held before")
    with ingest_hold(config, label="alpha", timeout_s=1), pytest.raises(IngestMismatch, match=r"baseline\.txt"):
        ingest_files(provisioner, manifest(tmp_path, FLAT[:2]))


def test_o67_the_members_of_a_tar_are_ingested_from_their_archive(provisioner, fake, tmp_path, config):
    """O67 with O25: a manifest whose sources are the members of an uncompressed tar is checked and uploaded from the
    archive, never unpacked, and its listing matches."""
    paths = ["flat-1.bin", "flat-2.bin", "deep/a/b.bin"]
    archive = tmp_path / "space.tar"
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tar:
        for index, path in enumerate(paths):
            info = tarfile.TarInfo(path)
            info.size = len(content(index))
            tar.addfile(info, io.BytesIO(content(index)))
    members = tar_members(archive)
    files = [
        ManifestFile(path, members[path], len(content(i)), hashlib.sha256(content(i)).hexdigest())
        for i, path in enumerate(paths)
    ]
    with ingest_hold(config, label="alpha", timeout_s=1):
        report = ingest_files(provisioner, files, root=ROOT)
    assert report.actions() == {"created": 3}
    for index, path in enumerate(paths):
        assert bytes(fake.nodes[fake.id_of(f"{ROOT}/{path}")].content) == content(index)
