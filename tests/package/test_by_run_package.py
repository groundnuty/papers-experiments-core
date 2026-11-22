"""C67: the replication package of a batch merged by run (merge.json `expcore-merge/2`): merge.json, the ledger
snapshot, every counted lease's snapshot and every evidence lease's go in with the records, the package's own check
passes, and an anonymised build leaves no source anywhere and re-pins the campaign fingerprint's hash everywhere the
merge binds it.

The fleet is the example study leased run by run through a real ledger (`merge_by_run_support.small_fleet`) with two
evidence leases, one superseded with its store and one that left nothing, under a directory with an invented private
name, so every package built here also shows that no path of the machine reaches it. Every build vendors stand-in
wheels (`package_support`).
"""

import hashlib
import json
import re
import sqlite3
import tarfile
from pathlib import Path

import pytest
from merge_by_run_support import Fleet, by_run_study, small_fleet
from package_support import stand_in_vendor
from study_support import Campaign, clean_environment, copy_campaign, finish

from expcore.campaign.manifest import fingerprint_sha256
from expcore.campaign.resume import FINGERPRINT_KEY
from expcore.campaign.shard import META_ENVIRONMENT, META_SHARD
from expcore.package import build as package_build
from expcore.package import by_run
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
LEDGER = "out/ledger-snapshot.sqlite"
MERGE = "out/merge.json"
MERGED = ("out/runs", MERGE, LEDGER, "out/leases", "out/evidence")  # what package.toml lists of the batch
LISTED = "".join(f'    "{pattern}",\n' for pattern in MERGED)

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
def fleet(tmp_path_factory, clean_environment, core_from_a_repository) -> Fleet:
    home = tmp_path_factory.mktemp("by-run") / f"{PRIVATE}-home"
    home.mkdir()
    return small_fleet(home, clean_environment, superseded=True)


@pytest.fixture(scope="module")
def campaign(fleet) -> Campaign:
    """Every counted store and the superseded one merged by run into a copy that lists the merged files in its
    package.toml, then checked, analysed and reported."""
    merged = Campaign(by_run_study(fleet.base, "merged"))
    spec = merged.root / "package.toml"
    spec.write_text(spec.read_text(encoding="utf-8").replace('    "out/runs",\n', LISTED), encoding="utf-8")
    merged.codes["merge"], errors = fleet.merge(merged, fleet.outs(fleet.superseded))
    assert merged.codes["merge"] == 0, errors
    finish(fleet.env, merged)
    assert merged.codes == {"merge": 0, "check": 0, "analyze": 0, "report": 0}
    return merged


def unpack(archive: Path, where: Path) -> Path:
    with tarfile.open(archive) as members:
        members.extractall(where, filter="data")
    return where / NAME


def every_byte(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def meta(data: bytes, where: Path, key: str) -> str:
    """One meta value of a snapshot, read from a copy of its bytes."""
    where.write_bytes(data)
    connection = sqlite3.connect(where)
    try:
        return connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]
    finally:
        connection.close()


def stored(document: dict) -> list[tuple[str, str]]:
    """Each lease store's snapshot merge.json lists, by its place in the package, with the hash it names."""
    found = [(f"out/leases/{e['lease_id']}/{SNAPSHOT}", e["snapshot_sha256"]) for e in document["leases"]]
    found += [(f"out/evidence/{e['lease_id']}/{SNAPSHOT}", e["snapshot_sha256"]) for e in document["evidence"]]
    return [(path, digest) for path, digest in found if digest is not None]


def test_c67_the_package_holds_every_snapshot_merge_json_lists_as_the_batch_holds_it_and_passes_its_own_check(
    campaign, fleet, hashes, tmp_path
):
    """C67: the package holds merge.json, the ledger snapshot, each counted lease's snapshot and the superseded
    lease's beside the records, each the bytes merge.json names and every file under out/ the merged batch's own
    bytes; the lease that left nothing is listed with no hash and has no file. No path of the machine is in it, and its
    check regenerates every output from it byte for byte."""
    root = unpack(build_package(campaign.root, tmp_path / "dist", hashes=hashes), tmp_path)
    files = every_byte(root)
    document = json.loads(files[MERGE])
    assert document["schema"] == "expcore-merge/2"
    assert sha(files[LEDGER]) == document["ledger_sha256"]
    assert {e["lease_id"]: e["snapshot_sha256"] is None for e in document["evidence"]} == {
        fleet.superseded: False,
        fleet.vanished: True,
    }
    for path, digest in stored(document):
        assert sha(files[path]) == digest, path
    assert {path for path, _ in stored(document)} >= {f"out/evidence/{fleet.superseded}/{SNAPSHOT}"}
    assert not [name for name in files if name.startswith(f"out/evidence/{fleet.vanished}/")]
    for name, data in files.items():
        if name.startswith("out/"):
            assert data == (campaign.root / name).read_bytes(), name
    assert verify_package(root) == []
    for name, data in files.items():
        assert PRIVATE.encode() not in data, name
        assert str(campaign.root).encode() not in data, name


def test_c67_an_anonymised_package_has_no_source_and_re_pins_the_fingerprint_everywhere_the_merge_binds_it(
    campaign, hashes, anonymisation, tmp_path
):
    """C67: anonymising rewrites every lease snapshot, the evidence's included, without the environment's sources, so
    the campaign fingerprint's hash changes: each snapshot's fingerprint, each store's pinned lease and the ledger's
    configuration name the new hash, merge.json names it and every rewritten file's hash, the check names the new
    merge.json, PACKAGE.json records the original hash beside the new one, and the package passes its own check. Built
    twice, the archive is the same bytes."""
    archives = [
        build_package(campaign.root, tmp_path / name, hashes=hashes, anonymise=anonymisation) for name in ("a", "b")
    ]
    assert archives[0].read_bytes() == archives[1].read_bytes()
    root = unpack(archives[0], tmp_path / "unpacked")
    files = every_byte(root)
    for name, data in files.items():
        for hidden in (CORE_COMMIT[:12], "code.example.org", "qvarek"):
            assert hidden.encode() not in data, f"{name} holds {hidden}"
    document = json.loads(files[MERGE])
    assert sha(files[LEDGER]) == document["ledger_sha256"]
    assert (
        json.loads(meta(files[LEDGER], tmp_path / "ledger.sqlite", "config"))["fingerprint"] == document["fingerprint"]
    )
    assert len(stored(document)) == len(document["leases"]) + 1  # the superseded lease's store is rewritten too
    for path, digest in stored(document):
        assert sha(files[path]) == digest, path
        fingerprint = json.loads(meta(files[path], tmp_path / "store.sqlite", FINGERPRINT_KEY))
        assert "sources" not in fingerprint["environment"]
        assert fingerprint_sha256(fingerprint) == document["fingerprint"]
        pinned = json.loads(meta(files[path], tmp_path / "store.sqlite", META_SHARD))
        assert pinned["campaign_sha256"] == document["fingerprint"]
    original = campaign.read("merge.json")["fingerprint"]
    assert original != document["fingerprint"]
    repinned = json.loads(files["PACKAGE.json"])["campaign_fingerprint_repinned"]
    assert (repinned["original_sha256"], repinned["sha256"]) == (original, document["fingerprint"])
    check = json.loads(files["out/runs/replay-check.json"])
    assert check["merge"]["sha256"] == sha(files[MERGE])
    assert verify_package(root) == []


@pytest.mark.parametrize(
    ("place", "left"),
    [
        ("_repin_lease", lambda pinned, repinned, where: dict(pinned)),
        ("_repin_ledger", lambda config, repinned, where: dict(config)),
        ("_repin_merge", lambda document, repinned: document),
    ],
    ids=["pinned-lease", "ledger-config", "merge-file"],
)
def test_c67_an_anonymised_package_with_one_place_left_at_the_original_hash_is_refused(
    campaign, hashes, anonymisation, tmp_path, monkeypatch, place, left
):
    """C67: a re-pin that leaves any one of the three places (each store's pinned lease, the ledger's configuration,
    merge.json's fingerprint) at the original hash makes a package whose own analysis refuses its stores, so the build
    refuses it and writes nothing."""
    monkeypatch.setattr(by_run, place, left)
    with pytest.raises(PackageError, match="the anonymised records cannot be analysed"):
        build_package(campaign.root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation)
    assert not list((tmp_path / "dist").glob("*.tar.gz"))


def first(root: Path, directory: str) -> Path:
    """The first lease store snapshot merge.json lists under `directory` (leases or evidence)."""
    document = json.loads((root / MERGE).read_text(encoding="utf-8"))
    return root / next(path for path, _ in stored(document) if path.startswith(f"out/{directory}/"))


def edited(path: Path, root: Path) -> str:
    """Give a lease store snapshot another environment identity after the check."""
    connection = sqlite3.connect(path)
    try:
        identity = json.dumps({"layout": "d" * 64}, sort_keys=True)
        connection.execute("UPDATE meta SET value=? WHERE key=?", (identity, META_ENVIRONMENT))
        connection.commit()
    finally:
        connection.close()
    return rf"{re.escape(path.relative_to(root).as_posix())} differs from the snapshot out/merge\.json names"


def removed(path: Path, root: Path) -> str:
    path.unlink()
    return rf"needs {re.escape(path.relative_to(root).as_posix())}; add it to \[package\] include"


def edit_snapshot(root: Path) -> str:
    """Change the first counted lease's snapshot after the check."""
    return edited(first(root, "leases"), root)


def edit_evidence(root: Path) -> str:
    """Change the superseded lease's snapshot after the check."""
    return edited(first(root, "evidence"), root)


def edit_ledger(root: Path) -> str:
    """Append a byte to the ledger snapshot after the check."""
    with (root / LEDGER).open("ab") as handle:
        handle.write(b"\0")
    return rf"{re.escape(LEDGER)} differs from the snapshot out/merge\.json names"


def edit_merge_file(root: Path) -> str:
    """Give merge.json another fingerprint after the check."""
    path = root / MERGE
    document = json.loads(path.read_text(encoding="utf-8")) | {"fingerprint": "f" * 64}
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return r"out/merge\.json differs from what the replay check hashed"


def remove_snapshot(root: Path) -> str:
    """Remove the first counted lease's snapshot after the check."""
    return removed(first(root, "leases"), root)


def remove_evidence(root: Path) -> str:
    """Remove the superseded lease's snapshot after the check, the only one under out/evidence, and the pattern that
    named it (which would select nothing)."""
    spec = root / "package.toml"
    spec.write_text(spec.read_text(encoding="utf-8").replace('    "out/evidence",\n', ""), encoding="utf-8")
    return removed(first(root, "evidence"), root)


def remove_ledger(root: Path) -> str:
    """Remove the ledger snapshot after the check, and the pattern that named it alone (which would select nothing)."""
    (root / LEDGER).unlink()
    spec = root / "package.toml"
    spec.write_text(spec.read_text(encoding="utf-8").replace(f'    "{LEDGER}",\n', ""), encoding="utf-8")
    return rf"needs {re.escape(LEDGER)}; add it to \[package\] include"


EDITS = (edit_snapshot, edit_evidence, edit_ledger, edit_merge_file, remove_snapshot, remove_evidence, remove_ledger)


@pytest.mark.parametrize("anonymised", [False, True], ids=["plain", "anonymised"])
@pytest.mark.parametrize("edit", EDITS, ids=lambda edit: edit.__name__.replace("_", "-"))
def test_c67_a_batch_merged_by_run_tampered_after_its_check_is_refused_naming_the_file(
    campaign, hashes, anonymisation, tmp_path, anonymised, edit
):
    """C67: a build compares merge.json with the hash the replay check names, and the ledger snapshot and every lease
    store's snapshot, counted or evidence, with the hash merge.json names, before it reads or rewrites any of them, and
    needs every one: a file changed or removed after the check is refused, named by its place in the package."""
    root = copy_campaign(campaign, tmp_path, "edited").root
    refusal = edit(root)
    with pytest.raises(PackageError, match=refusal):
        build_package(root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation if anonymised else None)
