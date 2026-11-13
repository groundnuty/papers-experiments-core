"""Provisioning a space by storage import: the declared storage checked on the data provider's panel, the scan waited
for, and the space shown to hold exactly its dataset's manifest, every file whole on the data provider (O37, #184)."""

import hashlib
import os

import pytest

from expcore.config.loading import ConfigError
from expcore_onedata.client import OutOfScope, PlatformError
from expcore_onedata.config import load_platform
from expcore_onedata.provision import Provisioner, ProvisionError, resolve_provider_ids
from expcore_onedata.rangemap import load_placement
from expcore_onedata.storageimport import (
    ImportedSpace,
    ImportFailed,
    SpaceCheck,
    load_manifest,
    manifest_of_tree,
    write_manifest,
)
from expcore_onedata.testing import ADMIN_ENV, ALPHA_ID, BETA_ID, with_placement, write_platform
from expcore_onedata.testing.fake_import import make_import_federation

TREE = {
    "records/day-01.csv": b"01,ok\n" * 40,
    "records/day-02.csv": b"02,ok\n" * 70,
    "notes/readme.txt": b"an invented dataset\n",
    "large.bin": bytes(range(256)) * 20,
    "empty.dat": b"",
}
EMPTY_DIRS = ("scratch",)


def platform(readonly: bool | None):
    def edit(data):
        with_placement(data)
        data["support"]["providers"] = ["alpha", "beta"]
        if readonly is not None:
            data["space"]["import"] = {"readonly": readonly}

    return edit


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, platform(readonly=True))


@pytest.fixture
def fake(monkeypatch, ticker):
    """The fake federation with the study space supported by alpha on a read-only imported storage holding `TREE`,
    and by beta on a regular one; the initial scan is enqueued and runs over two reads of its state."""
    federation = make_import_federation(clock=ticker.clock, readonly=True)
    federation.add_provider(BETA_ID, "beta.test")
    for path, content in TREE.items():
        federation.put_on_storage(path, content)
    for path in EMPTY_DIRS:
        federation.put_dir_on_storage(path)
    federation.scan_polls = 2
    federation.start_scan()
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    return federation


@pytest.fixture
def tree(tmp_path):
    """The same dataset as a local tree, the copy the study keeps and makes its manifest from."""
    root = tmp_path / "dataset"
    for path, content in TREE.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)
    for path in EMPTY_DIRS:
        (root / path).mkdir(parents=True)
    return root


@pytest.fixture
def manifest(tree):
    return manifest_of_tree(tree)


@pytest.fixture
def imported(provisioner, manifest):
    return ImportedSpace(provisioner, manifest)


def panel_calls(fake, method=None):
    return [call for call in fake.panel_calls if method is None or call[1] == method]


# -- the manifest ----------------------------------------------------------------------------------------------------


def test_o37_a_trees_manifest_lists_every_file_with_its_size_and_hash_and_every_directory(manifest):
    """O37: the manifest is what the space must hold: each file's path, size and SHA-256, and each directory, an empty
    one included, sorted by path."""
    assert [(f.path, f.size, f.sha256) for f in manifest.files] == sorted(
        (path, len(content), hashlib.sha256(content).hexdigest()) for path, content in TREE.items()
    )
    assert manifest.directories == ("notes", "records", "scratch")
    assert manifest.bytes == sum(len(content) for content in TREE.values())


def test_o37_a_tree_holding_a_link_is_refused_before_anything_is_hashed(tree):
    """O37: op-worker's import skips anything that is neither a regular file nor a directory, a symbolic link included
    (its CHANGELOG, VFS-9118), so such a tree would never import whole: it is refused, by count, naming no path."""
    os.symlink(tree / "large.bin", tree / "records" / "linked.bin")
    with pytest.raises(ValueError, match="1 entry of the tree is neither a regular file nor a directory") as caught:
        manifest_of_tree(tree)
    assert "linked" not in str(caught.value)
    assert str(tree) not in str(caught.value)


def test_o37_a_written_manifest_loads_as_it_was(manifest, tmp_path):
    """O37: the manifest a study writes beside its dataset is the one it loads at every federation."""
    path = tmp_path / "manifest.json"
    write_manifest(manifest, path)
    assert load_manifest(path) == manifest
    assert load_manifest(path).digest() == manifest.digest()


BAD_MANIFESTS = {
    "version": ({"version": 2, "files": [], "directories": []}, "version"),
    "climbs out": (
        {"version": 1, "files": [{"path": "../x", "size": 1, "sha256": "0" * 64}], "directories": []},
        "files[0]",
    ),
    "twice": (
        {
            "version": 1,
            "files": [{"path": "a", "size": 1, "sha256": "0" * 64}, {"path": "a", "size": 1, "sha256": "0" * 64}],
            "directories": [],
        },
        "files[1]",
    ),
    "negative size": (
        {"version": 1, "files": [{"path": "a", "size": -1, "sha256": "0" * 64}], "directories": []},
        "files[0]",
    ),
    "bad hash": ({"version": 1, "files": [{"path": "a", "size": 1, "sha256": "xyz"}], "directories": []}, "files[0]"),
    "directory not listed": (
        {"version": 1, "files": [{"path": "d/a", "size": 1, "sha256": "0" * 64}], "directories": []},
        "files[0]",
    ),
}


@pytest.mark.parametrize("case", list(BAD_MANIFESTS))
def test_o37_a_manifest_that_cannot_be_used_is_refused_naming_the_entry_and_no_path(case, tmp_path):
    """O37, O12: a manifest is a study's file: a mistake names the file and the entry's position, never a path."""
    import json

    body, words = BAD_MANIFESTS[case]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ConfigError, match=words.replace("[", r"\[").replace("]", r"\]")) as caught:
        load_manifest(path)
    assert "../x" not in str(caught.value)


# -- the declared storage --------------------------------------------------------------------------------------------


def test_o37_an_import_is_refused_for_a_space_not_declared_imported(tmp_path, fake, client, ticker, manifest):
    """O37: a space without `import` was ingested; checking it as an import would read panels for nothing."""
    regular = tmp_path / "regular"
    regular.mkdir()
    path = write_platform(regular, platform(readonly=None))
    config = load_platform(path)
    plain = Provisioner(
        client,
        config,
        load_placement(path, config).settings,
        provider_ids=resolve_provider_ids(config),
        log=regular / "changes.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    )
    with pytest.raises(ProvisionError, match="not declared as held on an imported storage"):
        ImportedSpace(plain, manifest)


def test_o37_the_declared_storage_is_the_one_the_data_providers_panel_shows(imported, fake):
    """O37: the data provider's support of the space is an imported storage, read-only as declared, in the auto mode,
    and the record says how its scans are set."""
    shown = imported.check_storage()
    assert shown == {
        "imported": True,
        "readonly": True,
        "mode": "auto",
        "continuous_scan": False,
        "detect_modifications": False,
        "detect_deletions": False,
    }
    assert all(provider == ALPHA_ID for provider, _, _ in fake.panel_calls)


MISMATCHES = {
    "a regular storage": (lambda f: setattr(f, "imported_storage", False), "is not an imported storage"),
    "read-only differs": (lambda f: setattr(f, "readonly", False), "read-only is not as declared"),
    "manual mode": (lambda f: setattr(f, "mode", "manual"), "manual"),
    "continuous scans": (lambda f: f.auto_config.update(continuousScan=True), "scans continuously"),
}


@pytest.mark.parametrize("case", list(MISMATCHES))
def test_o37_a_storage_other_than_declared_is_refused(case, imported, fake):
    """O37: a declaration the federation does not bear out fails closed, before anything is waited for."""
    change, words = MISMATCHES[case]
    change(fake)
    with pytest.raises(ImportFailed, match=words):
        imported.check_storage()


def test_o37_a_panel_read_outside_the_known_reads_is_refused_unsent(client, fake):
    """O37: the client reads three panel paths and nothing else; another is refused before anything is sent."""
    with pytest.raises(OutOfScope):
        client.panel_get("alpha", "/provider/storages")
    with pytest.raises(OutOfScope):
        client.panel_get("alpha", "/provider/spaces/spmock0001/storage-import/auto/info?x=1")
    assert fake.panel_calls == []


# -- provisioning by import ------------------------------------------------------------------------------------------


def test_o37_provisioning_waits_for_the_scan_and_shows_the_space_holds_exactly_the_manifest(imported, fake, manifest):
    """O37: the scan is waited for until it completes; then every path, kind and size of the listing is the
    manifest's, on consecutive polls, and every file is whole on the data provider by its block map (O01)."""
    record = imported.provision(timeout_s=60)
    assert record["ok"] is True
    assert record["scan"]["status"] == "completed"
    assert record["scan"]["failedFiles"] == 0
    listing = record["listing"]["providers[0]"]
    assert (listing["files"], listing["directories"], listing["bytes"]) == (5, 3, manifest.bytes)
    assert (listing["missing"], listing["extra"], listing["wrong_size"], listing["wrong_kind"]) == ([], 0, [], [])
    assert record["blocks"] == {"files": 5, "whole": 5, "not_whole": []}
    assert record["manifest"]["sha256"] == manifest.digest()
    assert len(panel_calls(fake, "GET")) >= 3  # the scan's state was read until it completed
    assert fake.held("records/day-02.csv", ALPHA_ID) == [[0, len(TREE["records/day-02.csv"])]]


@pytest.mark.parametrize(("outcome", "failed"), [("failed", 0), ("aborted", 0), ("completed", 2)])
def test_o37_a_scan_that_fails_aborts_or_fails_files_fails_the_import_with_its_record(outcome, failed, imported, fake):
    """O37: `failed`, `aborted`, or a completed scan with failed files, ends the import: the record keeps what the
    panel said."""
    fake.scan_outcome, fake.scan_failed_files = outcome, failed
    with pytest.raises(ImportFailed) as caught:
        imported.provision(timeout_s=60)
    assert caught.value.record["ok"] is False
    assert caught.value.record["scan"]["status"] == outcome


def test_o37_a_scan_that_does_not_complete_within_the_bound_fails_the_import(imported, fake):
    """O37: the wait is bounded, never open."""
    fake.scan_polls = 10_000
    fake.start_scan()
    with pytest.raises(ImportFailed, match="did not complete"):
        imported.provision(timeout_s=30)


DIFFERENCES = {
    "a file missing": (lambda f: f.drop_from_storage("notes/readme.txt"), "missing", ["manifest.files[2]"]),
    "a file of another size": (lambda f: f.put_on_storage("large.bin", b"short"), "wrong_size", ["manifest.files[1]"]),
    "an extra file": (lambda f: f.put_on_storage("stray.txt", b"stray"), "extra", 1),
}


@pytest.mark.parametrize("case", list(DIFFERENCES))
def test_o37_a_space_that_differs_from_the_manifest_fails_the_import_naming_no_path(case, imported, fake):
    """O37, O12: a file missing, of another size, or not in the manifest fails the import at the bound; the record
    names a manifest's file by its position and counts the extra entries, and the message holds no path."""
    change, key, expected = DIFFERENCES[case]
    change(fake)
    with pytest.raises(ImportFailed, match="does not hold exactly the manifest") as caught:
        imported.provision(timeout_s=30)
    assert caught.value.record["listing"]["providers[0]"][key] == expected
    for path in (*TREE, "stray.txt"):
        assert path not in str(caught.value)


def test_o37_a_file_not_whole_on_the_data_provider_fails_the_import(imported, fake):
    """O01, O37: a listed file of the right size is not enough: the block map must hold all of it."""
    imported.await_scan(timeout_s=60)
    fake.blocks[(fake.id_of("records/day-01.csv"), ALPHA_ID)] = [[0, 10]]
    with pytest.raises(ImportFailed, match="not whole on the data provider") as caught:
        imported.verify(timeout_s=30)
    assert caught.value.record["blocks"]["not_whole"] == ["manifest.files[3]"]


def test_o37_with_hashes_every_files_bytes_are_read_back_and_compared(imported, fake, tree, provisioner):
    """O37: `hashes=True` reads every file through the data provider and compares its SHA-256 with the manifest's;
    bytes that differ at the same size fail the import."""
    imported.await_scan(timeout_s=60)
    assert imported.verify(timeout_s=30, hashes=True)["hashes"] == {"files": 5, "equal": 5, "differ": []}
    node = fake.nodes[fake.id_of("records/day-01.csv")]
    node.content[0] ^= 0xFF
    with pytest.raises(ImportFailed, match="SHA-256") as caught:
        ImportedSpace(provisioner, manifest_of_tree(tree)).verify(timeout_s=30, hashes=True)
    assert caught.value.record["hashes"]["differ"] == ["manifest.files[3]"]


def test_o37_the_listing_is_waited_for_on_every_provider_named(imported, fake):
    """O37, O07: a mirror shows the imported entries after its visibility delay; the import waits for its listing to
    hold the manifest too, and records how long it took."""
    fake.visibility_delay_by_provider[BETA_ID] = 6.0
    imported.await_scan(timeout_s=60)
    record = imported.verify(timeout_s=60, providers=["alpha", "beta"])
    assert record["listing"]["providers[1]"]["converged"] is True
    assert record["listing"]["providers[1]"]["seconds"] >= 6.0


def test_o37_an_uploaded_space_is_checked_against_the_manifest_by_the_same_code(
    fake, client, ticker, tree, manifest, tmp_path
):
    """O37: `SpaceCheck` verifies a space whose dataset was uploaded exactly as an imported one, so the live
    equivalence tests compare the two by one check."""
    fake.readonly = False  # this test's space is a regular one: alpha takes writes
    regular = tmp_path / "regular"
    regular.mkdir()
    path = write_platform(regular, platform(readonly=None))
    config = load_platform(path)
    plain = Provisioner(
        client,
        config,
        load_placement(path, config).settings,
        provider_ids=resolve_provider_ids(config),
        log=regular / "changes.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    )
    for entry in manifest.files:
        plain.upload(entry.path, tree / entry.path)
    for directory in manifest.directories:
        plain.make_dir(directory)
    record = SpaceCheck(plain, manifest).verify(timeout_s=30, hashes=True)
    assert (record["ok"], record["blocks"]["whole"], record["hashes"]["equal"]) == (True, 5, 5)
    assert (record["storage"], record["scan"]) == (None, None)


# -- a re-scan -------------------------------------------------------------------------------------------------------


def test_o37_a_forced_rescan_keeps_every_file_id_and_counts_nothing_new(imported, fake):
    """O37: with the detection of changes off, a second scan only adds what is new on the storage: every id stays."""
    imported.provision(timeout_s=60)
    record = imported.rescan(timeout_s=60)
    assert record["ids_stable"] is True
    assert (record["scan"]["createdFiles"], record["scan"]["modifiedFiles"], record["scan"]["deletedFiles"]) == (
        0,
        0,
        0,
    )
    assert record["scan"]["totalScans"] == 2
    assert panel_calls(fake, "POST") == [
        (ALPHA_ID, "POST", "/provider/spaces/spmock0001/storage-import/auto/force-start")
    ]


def test_o37_a_file_deleted_from_a_read_only_space_comes_back_under_a_new_id_by_a_rescan(imported, fake):
    """O37: on a read-only storage a delete removes the file from the space and leaves it on the storage, with its sync
    record gone (op-worker f118ed9, fslogic_delete.erl, maybe_delete_storage_file), so a re-scan imports it again under
    a new id: the record says which manifest file changed id."""
    imported.provision(timeout_s=60)
    fake.remove_from_space("notes/readme.txt")
    record = imported.rescan(timeout_s=60)
    assert record["ids_stable"] is False
    assert record["new_ids"] == ["manifest.files[2]"]
    assert record["listing"]["providers[0]"]["missing"] == []


def test_o09_a_forced_scan_of_a_space_whose_live_name_left_the_prefixes_is_refused_unsent(imported, fake, client):
    """O09, O37: the force-start is a mutation; the space's live name is read first and must be the space's own."""
    fake.space_name = "elsewhere-data"
    with pytest.raises(OutOfScope):
        client.force_scan("alpha", imported.space)
    assert panel_calls(fake, "POST") == []


def test_o37_a_panel_that_refuses_the_admin_is_an_error_answer_never_a_scan_state(imported, fake):
    """O08, O37: the panel answers only a member of the provider's cluster; a 403 is raised as the platform's answer."""
    fake.panel_status = 403
    with pytest.raises(PlatformError) as caught:
        imported.scan_info()
    assert caught.value.status == 403
