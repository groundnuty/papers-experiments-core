"""A space on a tree of the cluster's shared filesystem, imported read-only by its owner, on a live HPC federation
(O59). Skipped unless:

    EXPCORE_LIVE_HPC_IMPORT=1 EXPCORE_LIVE_PLATFORM=<root>/platform.toml \\
    EXPCORE_LIVE_IMPORT_SPACE=<the label of the shared import's entry in platform.toml> \\
    EXPCORE_LIVE_IMPORT_MANIFEST=<the manifest file of its tree> \\
    EXPCORE_LIVE_IMPORT_TREE=<the tree, as this host sees it> [EXPCORE_LIVE_IMPORT_OUT=<facts.json>] \\
    [EXPCORE_LIVE_MUTATE=1] uv run pytest tests/live/test_hpc_shared_import.py

The federation is one `hpcfed.py up` of a layout with a `shared = true` import (deploy/hpc-federation/layouts/
shared-import.toml); the test reads it and, with `EXPCORE_LIVE_MUTATE=1`, resets it. It never writes to a space: what it
sends is reads, and the reset's evictions from the mirrors and removals of the run's own QoS requirements.

The tree is described before the first read (every entry's relative path, size, modification time and mode, hashed),
and again after the reset: a federation that wrote to, renamed or deleted anything in the shared tree would change it.
The checks, one assertion each: the declared storage and the scan (the core's `ImportedSpace.provision`: the owner's
panel shows a read-only imported storage, the scan completed with no failed file, the listing on every provider is
exactly the manifest, every file whole on the owner); that the space is declared shared; and, after a reset, that the
owner still holds every file whole, the listing is still the manifest, and the tree on the shared filesystem is as it
was. With `EXPCORE_LIVE_IMPORT_OUT` what is read is written as JSON for the dated facts file. Not covered here, and
live-only: the read-only mount as the running service holds it (`importstorage.py mounts`, a control step on the owner's
node), and the scan's load on the shared filesystem.
"""

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

GATES = ("EXPCORE_LIVE_HPC_IMPORT",)
NEEDED = (
    "EXPCORE_LIVE_PLATFORM",
    "EXPCORE_LIVE_IMPORT_SPACE",
    "EXPCORE_LIVE_IMPORT_MANIFEST",
    "EXPCORE_LIVE_IMPORT_TREE",
)
if any(os.environ.get(gate) != "1" for gate in GATES) or not all(os.environ.get(name) for name in NEEDED):
    pytest.skip(
        "EXPCORE_LIVE_HPC_IMPORT=1, EXPCORE_LIVE_PLATFORM, EXPCORE_LIVE_IMPORT_SPACE, EXPCORE_LIVE_IMPORT_MANIFEST and "
        "EXPCORE_LIVE_IMPORT_TREE are needed: this test reads a live HPC federation's shared import",
        allow_module_level=True,
    )

from expcore_onedata.client import OnezoneClient  # noqa: E402 -- only once the module runs
from expcore_onedata.config import load_platform  # noqa: E402
from expcore_onedata.provision import Provisioner, resolve_provider_ids  # noqa: E402
from expcore_onedata.rangemap import load_placement  # noqa: E402
from expcore_onedata.reset import SpaceReset  # noqa: E402
from expcore_onedata.storageimport import ImportedSpace, load_manifest  # noqa: E402
from expcore_onedata.tool_admission import shared_admission  # noqa: E402

PLATFORM = Path(os.environ["EXPCORE_LIVE_PLATFORM"])
LABEL = os.environ["EXPCORE_LIVE_IMPORT_SPACE"]
MANIFEST = Path(os.environ["EXPCORE_LIVE_IMPORT_MANIFEST"])
TREE = Path(os.environ["EXPCORE_LIVE_IMPORT_TREE"])
OUT = os.environ.get("EXPCORE_LIVE_IMPORT_OUT")
MUTATE = os.environ.get("EXPCORE_LIVE_MUTATE") == "1"
TIMEOUT_S = 3600.0  # the scan and every listing's wait: a tree of 15,000 files scans over a shared filesystem
FACTS: dict = {}


def describe(tree: Path) -> str:
    """One SHA-256 of every entry's relative path, kind, size, modification time (ns) and mode, in path order: read with
    `lstat`, so no link is followed and no file is opened."""
    digest, count = hashlib.sha256(), 0
    pending = [Path()]
    while pending:
        here = pending.pop()
        with os.scandir(tree / here) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            relative = here / entry.name
            digest.update(f"{relative}\0{info.st_mode}\0{info.st_size}\0{info.st_mtime_ns}\n".encode())
            count += 1
            if entry.is_dir(follow_symlinks=False):
                pending.append(relative)
    FACTS["tree_entries"] = count
    return digest.hexdigest()


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    """The shared import checked once, the tree described before it, the provisioner and the check kept."""
    config = load_platform(PLATFORM)
    entry = config.space_named(LABEL)
    if entry.imported is None or not entry.imported.shared:
        pytest.skip("the entry is not declared as a shared import (import = { readonly = true, shared = true })")
    client = OnezoneClient(config)
    provisioner = Provisioner(
        client,
        config,
        load_placement(PLATFORM, config).settings,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path_factory.mktemp("hpc-import") / "changes.jsonl",
        space=LABEL,
    )
    manifest = load_manifest(MANIFEST)
    check = ImportedSpace(provisioner, manifest)
    FACTS["tree_before"] = describe(TREE)
    every = list(provisioner.space.support.providers)
    try:
        began = time.monotonic()
        FACTS["import"] = check.provision(timeout_s=TIMEOUT_S, providers=every, hashes=False)
        FACTS["import_check_s"] = round(time.monotonic() - began, 3)
        yield {"provisioner": provisioner, "check": check, "manifest": manifest, "providers": every}
    finally:
        FACTS["tree_after"] = describe(TREE)
        if OUT:
            Path(OUT).write_text(json.dumps(FACTS, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        client.close()


def test_o59_the_space_is_declared_shared_and_its_owners_storage_is_read_only(shared):
    """O59: the platform file says the tree is shared, and the owner's panel shows a read-only imported storage."""
    record = FACTS["import"]
    assert shared["provisioner"].space.imported.shared is True
    assert (record["ok"], record["shared"], record["readonly"]) == (True, True, True)
    assert record["storage"]["imported"] is True
    assert record["storage"]["readonly"] is True


def test_o59_the_scan_completed_and_every_provider_lists_exactly_the_manifest(shared):
    """O59: no failed file, and on the owner and every mirror the listing is the manifest's paths, kinds and sizes."""
    record, manifest = FACTS["import"], shared["manifest"]
    assert record["scan"]["status"] == "completed"
    assert record["scan"]["failedFiles"] == 0
    for listing in record["listing"].values():
        assert (listing["files"], listing["bytes"]) == (len(manifest.files), manifest.bytes)
        assert (listing["missing"], listing["wrong_size"], listing["wrong_kind"], listing["extra"]) == ([], [], [], 0)
    assert record["blocks"]["not_whole"] == []


@pytest.mark.skipif(not MUTATE, reason="EXPCORE_LIVE_MUTATE=1 is needed: a reset evicts replicas from the mirrors")
def test_o59_a_reset_leaves_the_owners_copy_the_listing_and_the_tree_on_the_shared_filesystem_as_they_were(shared):
    """O59: the reset evicts from the mirrors only; afterwards the owner holds every file whole, the listing is still
    the manifest, and the tree on the shared filesystem has the same entries, sizes, times and modes as before the
    first read."""
    provisioner, manifest = shared["provisioner"], shared["manifest"]
    mirrors = [p for p in shared["providers"] if p != provisioner.settings.holder]
    tops = sorted({path.split("/")[0] for path in (f.path for f in manifest.files)})
    began = time.monotonic()
    with shared_admission(provisioner.config, reason="live: the shared import's reset"):  # it holds the reset lock
        FACTS["reset_ok"] = SpaceReset(provisioner).run(dict.fromkeys(mirrors, tops))["ok"]
    FACTS["reset_s"] = round(time.monotonic() - began, 3)
    assert FACTS["reset_ok"] is True
    after = shared["check"].verify(timeout_s=TIMEOUT_S, providers=shared["providers"])
    assert after["ok"] is True
    assert after["blocks"]["not_whole"] == []
    assert describe(TREE) == FACTS["tree_before"]
