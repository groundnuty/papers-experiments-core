"""An imported dataset against an uploaded copy of the same bytes, on a live federation (O37, #184). Skipped unless:

    EXPCORE_LIVE_IMPORT=1 EXPCORE_LIVE_MUTATE=1 EXPCORE_LIVE_PLATFORM=<root>/platform.toml \\
    EXPCORE_LIVE_IMPORT_TREE=<the dataset's local tree> [EXPCORE_LIVE_IMPORT_OUT=<facts.json>] \\
    uv run pytest tests/live/test_storage_import.py

The federation is deploy/compose-federation's `layouts/storage-import.toml`, brought up with
`EXPCORE_FED_IMPORT_ALPHA` naming the same tree (`make_import_tree.py` writes one): the space labelled `imported` is
held by its owner on a read-only imported storage of the tree, scanned once at its support; the space labelled
`uploaded` is a regular one, empty until this test uploads the tree into it through `Provisioner.upload`, and emptied
again by its teardown. Both are mirrored by the other providers. Never run it on a federation another run is using:
it replicates, evicts, adds QoS requirements and resets both spaces.

The module's fixture provisions both copies once, through the core: the import is checked (`ImportedSpace.provision`:
the declared storage on the owner's panel, the scan, the listing on every provider, the owner's block maps and every
file's SHA-256), and the upload is timed and checked by the same code (`SpaceCheck.verify`). Each test then asserts one
thing that must be the same for both, the things a study measures: the tree, the bytes, the file ids' form and their
stability across a re-scan, the owner's block maps, the placement of an on-demand read through a mirror, a replication
and its transfer record, the reset's eviction and verification, and a QoS requirement's record and summary. The
attributes are compared too, and may differ only where the import is documented to (`docs/storage-import.md`: the
owner, the mode, the times, the ids). With `EXPCORE_LIVE_IMPORT_OUT`, what the tests read is written there as JSON for
the dated facts file.
"""

import json
import os
import time
from pathlib import Path

import pytest

GATES = ("EXPCORE_LIVE_IMPORT", "EXPCORE_LIVE_MUTATE")
NEEDED = ("EXPCORE_LIVE_PLATFORM", "EXPCORE_LIVE_IMPORT_TREE")
if any(os.environ.get(gate) != "1" for gate in GATES) or not all(os.environ.get(name) for name in NEEDED):
    pytest.skip(
        "EXPCORE_LIVE_IMPORT=1, EXPCORE_LIVE_MUTATE=1, EXPCORE_LIVE_PLATFORM and EXPCORE_LIVE_IMPORT_TREE are needed: "
        "this test uploads, replicates, evicts and resets in two spaces of a live federation",
        allow_module_level=True,
    )

from expcore_onedata import consistency  # noqa: E402 -- only once the module runs
from expcore_onedata.client import OnezoneClient  # noqa: E402
from expcore_onedata.config import load_platform  # noqa: E402
from expcore_onedata.ingestbase import plain  # noqa: E402
from expcore_onedata.provision import Provisioner, resolve_provider_ids  # noqa: E402
from expcore_onedata.rangemap import load_placement  # noqa: E402
from expcore_onedata.reset import STAGES, ResetFailed, SpaceReset  # noqa: E402
from expcore_onedata.storageimport import ImportedSpace, ImportFailed, SpaceCheck, manifest_of_tree  # noqa: E402

PLATFORM = Path(os.environ["EXPCORE_LIVE_PLATFORM"])
TREE = Path(os.environ["EXPCORE_LIVE_IMPORT_TREE"])
OUT = os.environ.get("EXPCORE_LIVE_IMPORT_OUT")
IMPORTED, UPLOADED = "imported", "uploaded"  # the labels of layouts/storage-import.platform.toml.template
TIMEOUT_S = 3600.0  # the scan, the upload's checks and every listing's wait, each
SAMPLE = 20  # the small files read back through each mirror, beside the largest
QOS_FULFIL_S = 300.0
# What an imported file is documented to differ in from an uploaded one (docs/storage-import.md, from op-worker
# f118ed9's import): its id (random, as two uploads' are), its parent's id, its owner (the storage file's uid through
# LUMA, or the space's virtual owner), its mode (the storage file's), its times (the storage file's stat) and the
# display uid and gid the storage gives. Every other attribute must be equal.
MAY_DIFFER = frozenset(
    {
        "file_id", "fileId", "guid", "parent_id", "parentFileId", "parentId", "owner_id", "ownerUserId", "ownerId",
        "mode", "posix_permissions", "posixPermissions", "atime", "mtime", "ctime", "creation_time", "creationTime",
        "display_uid", "displayUid", "display_gid", "displayGid", "storage_user_id", "storage_group_id",
    }
)  # fmt: skip
FACTS: dict = {}


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    """Both copies of the tree, provisioned and checked once: the import's and the upload's records, their
    provisioners, and the providers that mirror them. The uploaded copy is torn down at the end."""
    work = tmp_path_factory.mktemp("storage-import")
    config = load_platform(PLATFORM)
    labels = {entry.label for entry in config.spaces}
    if not {IMPORTED, UPLOADED} <= labels:
        pytest.skip("platform.toml has no [[spaces]] entries labelled imported and uploaded (layouts/storage-import)")
    if config.space_named(IMPORTED).imported is None:
        pytest.skip("the space labelled imported is not declared as held on an imported storage (import = {...})")
    client = OnezoneClient(config)
    settings = load_placement(PLATFORM, config).settings
    ids = resolve_provider_ids(config)

    def provisioner(label: str) -> Provisioner:
        return Provisioner(client, config, settings, provider_ids=ids, log=work / f"{label}.jsonl", space=label)

    imp, up = provisioner(IMPORTED), provisioner(UPLOADED)
    every = list(imp.space.support.providers)
    manifest = manifest_of_tree(TREE)
    imported, uploaded = ImportedSpace(imp, manifest), SpaceCheck(up, manifest)
    try:
        began = time.monotonic()
        FACTS["import"] = recorded(lambda: imported.provision(timeout_s=TIMEOUT_S, providers=every, hashes=True))
        FACTS["import_check_s"] = round(time.monotonic() - began, 3)
        began = time.monotonic()
        for entry in manifest.files:
            up.upload(entry.path, TREE / entry.path)
        for directory in manifest.directories:
            up.make_dir(directory)
        FACTS["upload_s"] = round(time.monotonic() - began, 3)
        FACTS["upload"] = recorded(lambda: uploaded.verify(timeout_s=TIMEOUT_S, providers=every, hashes=True))
        mirrors = [p for p in every if p != imp.settings.holder]
        yield {
            "imp": imp,
            "up": up,
            "imported": imported,
            "uploaded": uploaded,
            "manifest": manifest,
            "mirrors": mirrors,
        }
    finally:
        report = up.teardown()
        FACTS["upload_teardown_left"] = len(report.left)
        mirrors = [p for p in every if p != imp.settings.holder]
        try:  # the mirrors hold no replica of the imported copy after the module, whatever ran: recorded, not asserted
            plan = {mirror: top_paths(manifest) for mirror in mirrors}
            FACTS["imported_final_reset_ok"] = SpaceReset(imp).run(plan)["ok"]
        except ResetFailed as failed:
            FACTS["imported_final_reset_ok"] = False
            FACTS["imported_final_reset_stage"] = failed.stage
        if OUT:
            Path(OUT).write_text(json.dumps(FACTS, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        client.close()


def recorded(check):
    """Run a check that returns its record; keep a failed one's record in the facts before the failure goes on."""
    try:
        return check()
    except ImportFailed as failed:
        FACTS.setdefault("failed", []).append(failed.record)
        raise


def both(pair):
    return (pair["imp"], pair["imported"]), (pair["up"], pair["uploaded"])


def largest(pair) -> str:
    return max(pair["manifest"].files, key=lambda f: f.size).path


def sample(pair) -> list[str]:
    small = [f.path for f in pair["manifest"].files if f.size and f.path != largest(pair)]
    return [largest(pair), *small[:: max(1, len(small) // SAMPLE)][:SAMPLE]]


def top_paths(manifest) -> list[str]:
    """The entries directly under the space's root, which a reset's eviction plan names to empty a mirror."""
    paths = [*(f.path for f in manifest.files), *manifest.directories]
    return sorted({path.split("/")[0] for path in paths})


def tops(pair) -> list[str]:
    return top_paths(pair["manifest"])


def emptied(provisioner, path: str, file_id: str, mirror: str) -> None:
    """Evict a file from a mirror and require its block map there empty, so what a test places next is its own."""
    assert provisioner.evict(path, mirror)["empty"] is True
    assert provisioner.holds(mirror, [file_id], retry=provisioner.retry) == 0


def settled_blocks(provisioner, file_id: str, provider: str) -> list:
    """A provider's blocks of a file once its block map shows bytes on reads in a row (O07)."""
    settings = provisioner.settings
    result = consistency.converge(
        lambda: provisioner.blocks(file_id, provider),
        bool,
        timeout_s=settings.settle_timeout_s,
        poll_s=settings.poll_s,
        consecutive=settings.consecutive_polls,
    )
    assert result.converged, "the mirror's block map never showed the read's bytes"
    return result.value


# -- the declared storage, the tree and the bytes -------------------------------------------------------------------


def test_o37_the_owner_supports_the_imported_space_on_a_read_only_auto_import_that_does_not_rescan(pair):
    """O37: the owner's panel shows the declared storage: imported, read-only, the auto mode, no continuous scan."""
    # covers: GET /provider/spaces/{space}
    # covers: GET /provider/storages/{id}
    # covers: GET /provider/spaces/{space}/storage-import/auto/info
    storage = FACTS["import"]["storage"]
    assert (storage["imported"], storage["readonly"], storage["mode"], storage["continuous_scan"]) == (
        True,
        True,
        "auto",
        False,
    )
    assert FACTS["import"]["scan"]["status"] == "completed"


def test_o37_both_copies_hold_the_manifests_tree_on_every_provider(pair):
    """O37: every provider lists, in both spaces, exactly the manifest's paths, kinds and sizes."""
    for record in (FACTS["import"], FACTS["upload"]):
        assert record["ok"] is True
        assert all(listing["converged"] for listing in record["listing"].values())
    for label, listing in FACTS["import"]["listing"].items():
        uploaded = FACTS["upload"]["listing"][label]
        assert (listing["files"], listing["directories"], listing["bytes"]) == (
            uploaded["files"],
            uploaded["directories"],
            uploaded["bytes"],
        )


def test_o37_every_file_of_both_copies_reads_back_through_the_owner_with_the_manifests_hash(pair):
    """O25, O37: the bytes are the manifest's in both spaces, every file read whole through the owner."""
    assert FACTS["import"]["hashes"]["differ"] == FACTS["upload"]["hashes"]["differ"] == []


def test_o37_a_sample_reads_back_through_each_mirror_with_the_manifests_hash_in_both_copies(pair):
    """O37: through each mirror, which fetches from the owner, the largest file and a sample of small ones read back
    with the manifest's SHA-256 in both spaces; the reset test evicts what the reads placed."""
    planned = {f.path: f.sha256 for f in pair["manifest"].files}
    for provisioner, check in both(pair):
        listed = check.listing(provisioner.settings.holder)
        for mirror in pair["mirrors"]:
            for path in sample(pair):
                assert provisioner.read_file(listed[path].file_id, provider=mirror).sha256 == planned[path]


def test_o37_file_ids_have_one_form_in_both_copies(pair):
    """O37: an imported file's id is made as a created one's (op-worker's import, `datastore_key:new()` packed with the
    space's id): the client's plain segment, of the same lengths in both spaces."""
    lengths = []
    for provisioner, check in both(pair):
        ids = [entry.file_id for entry in check.listing(provisioner.settings.holder).values()]
        assert all(plain(fid) == fid for fid in ids)  # a plain segment, as the client's gate needs it
        lengths.append(sorted({len(fid) for fid in ids}))
    FACTS["id_lengths"] = lengths
    assert lengths[0] == lengths[1]


def test_o37_a_forced_rescan_keeps_every_imported_file_id_and_finds_nothing_new(pair):
    """O37: with continuous scans and change detection off, a forced scan only adds what is new on the storage: every
    id stays, and the scan counts nothing created, modified or deleted."""
    # covers: POST /provider/spaces/{space}/storage-import/auto/force-start
    record = pair["imported"].rescan(timeout_s=TIMEOUT_S)
    FACTS["rescan"] = record
    assert (record["ids_stable"], record["new_ids"]) == (True, [])
    scan = record["scan"]
    assert (scan["createdFiles"], scan["modifiedFiles"], scan["deletedFiles"], scan["failedFiles"]) == (0, 0, 0, 0)


def test_o01_the_owner_holds_every_file_as_one_whole_block_in_both_copies(pair):
    """O01, O37: an imported file's location is made with one block [0, size), as an uploaded file's ends; the owner's
    block map of every file is exactly that in both spaces."""
    for provisioner, check in both(pair):
        holder = provisioner.settings.holder
        listed = check.listing(holder)
        for entry in pair["manifest"].files:
            blocks = provisioner.blocks(listed[entry.path].file_id, holder, retry=provisioner.retry)
            assert blocks == ([(0, entry.size)] if entry.size else [])


# -- what happens through the mirrors --------------------------------------------------------------------------------


def test_o37_an_on_demand_read_through_a_mirror_places_the_same_window_in_both_copies(pair):
    """O02, O37: one byte read through a mirror from the middle of the largest file places the same window there in
    both spaces. The mirror is emptied first, verified on its block map, so the window is the read's alone, and again
    after."""
    path, mirror = largest(pair), pair["mirrors"][0]
    size = next(f.size for f in pair["manifest"].files if f.path == path)
    windows = []
    for provisioner, check in both(pair):
        file_id = check.listing(provisioner.settings.holder)[path].file_id
        emptied(provisioner, path, file_id, mirror)
        provisioner.fetch(mirror, file_id, size // 2, 1)
        windows.append(settled_blocks(provisioner, file_id, mirror))
        assert provisioner.evict(path, mirror)["empty"] is True
    FACTS["on_demand_windows"] = windows
    assert windows[0] == windows[1]


def test_o37_a_replication_onto_a_mirror_places_the_file_whole_with_the_same_transfer_record(pair):
    """O01, O37: a replication of the largest file onto a mirror emptied first, verified on its block map, places it
    whole in both spaces, so each moved the whole file, and the transfer records have the same fields and end in the
    same state."""
    path, mirror = largest(pair), pair["mirrors"][-1]
    shapes = []
    for provisioner, check in both(pair):
        emptied(provisioner, path, check.listing(provisioner.settings.holder)[path].file_id, mirror)
        done = provisioner.replicate(path, mirror)
        assert done["placement"]["fully_placed"] is True
        tid = provisioner.transfers()[-1][1]
        record = provisioner.client.get(f"/transfers/{tid}", provider=mirror)
        shapes.append((sorted(record), done["transfer_states"]))
        assert provisioner.evict(path, mirror)["empty"] is True
    FACTS["transfer_shapes"] = shapes
    assert shapes[0] == shapes[1]


def test_o37_the_reset_evicts_and_verifies_both_copies_alike(pair):
    """O01, O33, O37: with a file replicated onto each mirror and a range read through it, the reset evicts the
    mirrors' holdings in both spaces and passes every stage; the mirrors then hold nothing and the owner every block."""
    path = largest(pair)
    stages = []
    for provisioner, check in both(pair):
        holder = provisioner.settings.holder
        listed = check.listing(holder)
        small = next(f.path for f in pair["manifest"].files if f.size and f.path != path)
        for mirror in pair["mirrors"]:
            provisioner.replicate(small, mirror)
            provisioner.fetch(mirror, listed[path].file_id, 0, 1)
            settled_blocks(provisioner, listed[path].file_id, mirror)  # the eviction has bytes to remove
        record = SpaceReset(provisioner).run({mirror: tops(pair) for mirror in pair["mirrors"]})
        assert record["ok"] is True
        stages.append([stage for stage in STAGES if record["stages"][stage]["ok"]])
        for mirror in pair["mirrors"]:
            assert provisioner.holds(mirror, [listed[small].file_id, listed[path].file_id]) == 0
        assert provisioner.blocks(listed[path].file_id, holder) == [(0, listed[path].size)]
    FACTS["reset_stages"] = stages
    assert stages[0] == stages[1] == list(STAGES)


def test_o37_a_qos_requirement_is_fulfilled_and_viewed_alike_in_both_copies(pair):
    """O33, O37: an `anyStorage` requirement on a file through the owner reads the same in both spaces: its record's
    fields and its status once fulfilled, and the file's summary; the reset then removes both."""
    path = largest(pair)
    views = []
    for provisioner, check in both(pair):
        holder = provisioner.settings.holder
        file_id = check.listing(holder)[path].file_id
        body = {"fileId": file_id, "expression": "anyStorage", "replicasNum": 1}
        qid = provisioner.mutate("POST", "/qos_requirements", provider=holder, json=body)["qosRequirementId"]
        fulfilled = consistency.converge(
            lambda q=qid, p=provisioner, h=holder: p.client.get(f"/qos_requirements/{q}", provider=h),
            lambda record: record.get("status") == "fulfilled",
            timeout_s=QOS_FULFIL_S,
            poll_s=provisioner.settings.poll_s,
        )
        summary = provisioner.client.get(f"/data/{file_id}/qos/summary", provider=holder)
        views.append((fulfilled.converged, sorted(fulfilled.value or {}), qid in summary.get("requirements", {})))
        reset = SpaceReset(provisioner).run({mirror: tops(pair) for mirror in pair["mirrors"]})  # whatever ran before
        assert reset["stages"]["qos"]["removed"] >= 1
    FACTS["qos_views"] = views
    assert views[0] == views[1]
    assert views[0][0] is True


def test_o37_the_attributes_differ_only_where_the_import_is_documented_to(pair):
    """O37: every file's attributes, as the owner answers them, have the same names in both spaces and the same values
    but for the documented differences (`MAY_DIFFER`); what differs, and how often, is written to the facts."""
    differing: dict[str, int] = {}
    names = []
    imported, uploaded = both(pair)
    holder = imported[0].settings.holder
    listed = [check.listing(holder) for _, check in (imported, uploaded)]
    for entry in pair["manifest"].files:
        read = [
            provisioner.client.get(f"/data/{lst[entry.path].file_id}", provider=holder)
            for (provisioner, _), lst in zip((imported, uploaded), listed, strict=True)
        ]
        names.append((sorted(read[0]), sorted(read[1])))
        for key in set(read[0]) | set(read[1]):
            if read[0].get(key) != read[1].get(key):
                differing[key] = differing.get(key, 0) + 1
    FACTS["attribute_differences"] = differing
    assert all(a == b for a, b in names)
    assert set(differing) <= MAY_DIFFER, sorted(set(differing) - MAY_DIFFER)
