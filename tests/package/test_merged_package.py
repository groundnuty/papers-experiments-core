"""The replication package of a merged batch (#154, C29): merge.json and each shard's snapshot go in with the records,
the package's own check passes, and an anonymised build leaves no source in any snapshot and keeps the hashes true.

The shards run under a directory with an invented private name, so every package built here also shows that no path of
the machine the shards ran on reaches it. Every build vendors stand-in wheels (`package_support`).
"""

import hashlib
import json
import tarfile
from pathlib import Path

import pytest
from package_support import stand_in_vendor
from shard_support import run_shard
from study_support import Campaign, clean_environment, cli, copy_campaign, finish, make_copy

from expcore.campaign.shard import MERGE_FILE
from expcore.package import build as package_build
from expcore.package.build import build_package, verify_package
from expcore.package.spec import PackageError
from expcore.provenance import environment as environment_module
from expcore.provenance.environment import CORE_DISTRIBUTION
from expcore.provenance.hashfile import render_hash_file

PRIVATE = "zarnothwick"  # invented; the test hash file treats it as a name private everywhere
AUTHOR = "Quillon Varek"  # invented
CORE_COMMIT = "b1" * 20  # invented
CORE_URL = "https://code.example.org/qvarek/tiny-core"  # invented
NAME = "tiny-study-package"
SNAPSHOT = "campaign-snapshot.sqlite"

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


@pytest.fixture(scope="module", autouse=True)
def core_from_a_repository():
    """The core reports a version-control source, so its URL and commit reach every environment a campaign records;
    every build vendors stand-in wheels."""
    real = environment_module.source_of

    def source_of(dist):
        source = {"kind": "vcs", "vcs": "git", "url": CORE_URL, "commit": CORE_COMMIT}
        return source if dist.metadata["Name"] == CORE_DISTRIBUTION else real(dist)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(environment_module, "source_of", source_of)
        patch.setattr(package_build, "vendor_wheels", stand_in_vendor)
        yield


@pytest.fixture(scope="module")
def hashes(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("hashes") / "private.sha256"
    path.write_text(render_hash_file([PRIVATE]), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def anonymisation(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("list") / "anon.toml"
    path.write_text(f'files = ["README.md"]\n\n[terms]\n"{AUTHOR}" = "Author A"\n', encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def campaign(tmp_path_factory, clean_environment, core_from_a_repository) -> Campaign:
    """The example study run as two shards, merged into a copy that lists the merged files in its package.toml."""
    home = tmp_path_factory.mktemp("campaign") / f"{PRIVATE}-home"
    home.mkdir()
    one = run_shard(home, "one", clean_environment, 1)
    two = run_shard(home, "two", clean_environment, 2)
    merged = Campaign(make_copy(home, "study"))
    spec = merged.root / "package.toml"
    listed = '    "out/runs",\n    "out/merge.json",\n    "out/shards",\n'
    spec.write_text(spec.read_text(encoding="utf-8").replace('    "out/runs",\n', listed), encoding="utf-8")
    outs = [str(one.root / "out"), str(two.root / "out")]
    merged.codes["merge"], errors = cli(clean_environment, "merge", str(merged.root / "out"), *outs)
    assert merged.codes["merge"] == 0, errors
    finish(clean_environment, merged)
    assert merged.codes == {"merge": 0, "check": 0, "analyze": 0, "report": 0}
    return merged


def unpack(archive: Path, where: Path) -> Path:
    with tarfile.open(archive) as members:
        members.extractall(where, filter="data")
    return where / NAME


def every_byte(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_c29_the_package_of_a_merged_batch_holds_every_snapshot_and_passes_its_own_check(campaign, hashes, tmp_path):
    """C29: the package holds merge.json and the snapshot of each shard beside the records, no path of the machine,
    and its check regenerates every output from them byte for byte."""
    root = unpack(build_package(campaign.root, tmp_path / "dist", hashes=hashes), tmp_path)
    files = every_byte(root)
    assert {"out/merge.json", f"out/shards/1/{SNAPSHOT}", f"out/shards/2/{SNAPSHOT}"} <= set(files)
    assert f"out/runs/{SNAPSHOT}" not in files  # a merged batch has none of its own
    assert verify_package(root) == []
    for name, data in files.items():
        assert PRIVATE.encode() not in data, name
        assert str(campaign.root).encode() not in data, name
    document = json.loads(files["out/merge.json"])
    for shard in document["shards"]:
        assert shard["snapshot_sha256"] == hashlib.sha256(files[f"out/shards/{shard['index']}/{SNAPSHOT}"]).hexdigest()


def test_c29_a_package_that_leaves_out_what_a_merged_batch_needs_is_refused_naming_it(campaign, hashes, tmp_path):
    """C29: the package's check reads merge.json and each shard's snapshot, so a package.toml that omits them is
    refused, naming the first file missing."""
    root = copy_campaign(campaign, tmp_path, "no-shards").root
    spec = root / "package.toml"
    spec.write_text(spec.read_text(encoding="utf-8").replace('    "out/shards",\n', ""), encoding="utf-8")
    with pytest.raises(PackageError, match=rf"needs out/shards/1/{SNAPSHOT} and 1 more; add it to \[package\] include"):
        build_package(root, tmp_path / "dist", hashes=hashes)
    spec.write_text(spec.read_text(encoding="utf-8").replace('    "out/merge.json",\n', ""), encoding="utf-8")
    with pytest.raises(PackageError, match=rf"needs out/{MERGE_FILE}"):
        build_package(root, tmp_path / "dist", hashes=hashes)


def test_c29_an_anonymised_package_of_a_merged_batch_has_no_source_and_hashes_that_match(
    campaign, hashes, anonymisation, tmp_path
):
    """C29: anonymising rewrites each shard's snapshot (the environment's sources go), so merge.json is rewritten
    with the new hashes and the replay check names the new merge.json: the package still passes its check."""
    archive = build_package(campaign.root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation)
    root = unpack(archive, tmp_path)
    files = every_byte(root)
    for name, data in files.items():
        for hidden in (CORE_COMMIT[:12], "code.example.org", "qvarek"):
            assert hidden.encode() not in data, f"{name} holds {hidden}"
    document = json.loads(files["out/merge.json"])
    for shard in document["shards"]:
        assert shard["snapshot_sha256"] == hashlib.sha256(files[f"out/shards/{shard['index']}/{SNAPSHOT}"]).hexdigest()
    check = json.loads(files["out/runs/replay-check.json"])
    assert check["merge"]["sha256"] == hashlib.sha256(files["out/merge.json"]).hexdigest()
    assert verify_package(root) == []
