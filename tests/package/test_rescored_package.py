"""R17: the package of a re-scored batch is built and checked with no flag: the decision was recorded at `analyze`.

A scorer corrected after an HPC batch is re-scored with `analyze --rescore` and `report --rescore`, which write the
digests of both sets of scorers into out/analysis.json (`rescore`). The package's build and check pass no flag: they
honour a differing scorer digest exactly when that shipped entry equals what the guard records now (the digests the
store holds, the digest of the analysis plugin's scorers). Any other difference is refused as for an analysis, and the
byte comparison of the shipped outputs is still what decides the rest. The refusals are asserted by the guard's own
words: a byte mismatch would also fail a build, so a bare `PackageError` proves nothing.

The campaign runs once per module, from the example with both builders; `TINY_ANALYSIS_SCORERS` says which scorers
`build_analysis` gives (`other` is the corrected scorer, `extra` a set that is neither the run's nor the corrected one).
"""

import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from build_analysis_support import SCORERS, edit_analysis, rescored_copy, with_builders
from package_support import stand_in_vendor
from study_support import Campaign, clean_environment, cli, copy_campaign, finish
from tree_support import tree_of

from expcore.cli import main
from expcore.package import build as package_build
from expcore.package.build import build_package, verify_package
from expcore.package.spec import PackageError
from expcore.provenance.hashfile import render_hash_file

PRIVATE = "zarnothwick"  # invented; the test hash file treats it as a name private everywhere
NAME = "tiny-study-package"
GUARD = "campaign was run with other scorers"  # what the guard says of a digest that differs
NOT_THESE = "records a re-score that is not of these scorers on this store"  # and of an entry that is not the record
OUTPUTS = ("export.json", "scores.json", "analysis.json", "report.md")

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


@pytest.fixture(scope="module", autouse=True)
def stand_in_wheels():
    """Every build of this module vendors stand-in wheels."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(package_build, "vendor_wheels", stand_in_vendor)
        yield


@pytest.fixture(scope="module")
def hashes(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("hashes") / "private.sha256"
    path.write_text(render_hash_file([PRIVATE]), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def campaign(tmp_path_factory, clean_environment) -> Campaign:
    """The example with both builders, run, checked, analysed and reported with the run's own scorers."""
    home = tmp_path_factory.mktemp("campaign") / f"{PRIVATE}-home"
    home.mkdir()
    campaign = Campaign(with_builders(home, "study"))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture(scope="module")
def rescored(campaign, tmp_path_factory) -> Campaign:
    """The same batch analysed and reported again with the corrected scorer and `--rescore`."""
    return rescored_copy(campaign, tmp_path_factory.mktemp("rescored"))


def unpack(archive: Path, where: Path) -> Path:
    with tarfile.open(archive) as members:
        members.extractall(where, filter="data")
    return where / NAME


def package_of(source: Campaign, hashes: Path, base: Path, name: str = "copy") -> Path:
    """Build the package of a copy of a campaign and return the archive."""
    return build_package(copy_campaign(source, base, name).root, base / "dist", hashes=hashes)


def analysis_of(root: Path) -> dict:
    return json.loads((root / "out" / "analysis.json").read_text(encoding="utf-8"))


def test_r17_a_re_scored_batch_builds_a_package_that_passes_its_own_check_and_package_check(
    rescored, hashes, tmp_path, monkeypatch
):
    """R17: with the corrected scorers in force and no flag, the build's own check passes and the unpacked package
    checks again; the package ships the analysis that holds the recorded decision, byte for byte as the study wrote
    it."""
    monkeypatch.setenv(SCORERS, "other")
    root = unpack(package_of(rescored, hashes, tmp_path), tmp_path)
    assert analysis_of(root)["rescore"] == analysis_of(rescored.root)["rescore"]
    assert {name: (root / "out" / name).read_bytes() for name in OUTPUTS} == {
        name: (rescored.root / "out" / name).read_bytes() for name in OUTPUTS
    }
    assert b"- Re-scored (`--rescore`):" in (root / "out" / "report.md").read_bytes()
    shipped = tree_of(root)
    assert verify_package(root) == []
    assert main(["package-check", str(root)]) == 0
    assert tree_of(root) == shipped  # the check leaves the package as it found it


def test_r17_the_unpacked_package_of_a_re_scored_batch_passes_its_make_check(
    rescored, hashes, tmp_path, monkeypatch, clean_environment
):
    """R17: `make check`, which a reader of the package runs, takes no flag either: it regenerates every output of the
    re-scored batch from the records and compares byte for byte, under the corrected scorers."""
    monkeypatch.setenv(SCORERS, "other")
    root = unpack(package_of(rescored, hashes, tmp_path), tmp_path)
    done = subprocess.run(
        ["make", "-C", str(root), "-f", "Makefile", "check-installed", f"PYTHON={sys.executable}"],
        capture_output=True,
        text=True,
        env={**clean_environment, SCORERS: "other"},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "every output regenerates from the records byte for byte" in done.stdout


def test_r17_a_corrected_scorer_with_no_rescore_entry_is_refused_by_the_build_and_by_the_check(
    campaign, hashes, tmp_path, monkeypatch
):
    """R17: the study was analysed with the run's own scorers and nobody re-scored it: with the corrected scorers in
    force the build refuses with the guard's message (naming the scorers and the flag, no digest), writes no archive,
    and a package built before refuses to check, as an analysis refuses."""
    built = package_of(campaign, hashes, tmp_path, "first")  # the run's own scorers: as ever
    root = unpack(built, tmp_path)
    assert "rescore" not in analysis_of(root)
    monkeypatch.setenv(SCORERS, "other")
    with pytest.raises(PackageError, match="the package fails its own check") as refused:
        build_package(copy_campaign(campaign, tmp_path, "second").root, tmp_path / "again", hashes=hashes)
    assert GUARD in str(refused.value)
    assert "--rescore" in str(refused.value)
    assert "'tiny'" in str(refused.value)
    assert not list((tmp_path / "again").glob("*.tar.gz"))
    problems = verify_package(root)
    assert len(problems) == 1
    assert GUARD in problems[0]
    assert main(["package-check", str(root)]) == 1


@pytest.mark.parametrize("which", ["extra", "same"])
def test_r17_an_entry_that_is_not_for_the_scorers_in_force_now_is_refused_or_fails_the_comparison(
    which, rescored, hashes, tmp_path, monkeypatch
):
    """R17: the entry records the digest of the corrected scorers. Built and checked with a third set of scorers, the
    guard refuses with its message that the shipped analysis records another re-score. Checked with the run's own
    scorers again, nothing is re-scored, so the derived analysis has no entry and the shipped one fails the
    comparison."""
    monkeypatch.setenv(SCORERS, "other")
    root = unpack(package_of(rescored, hashes, tmp_path, "first"), tmp_path)
    monkeypatch.setenv(SCORERS, which)
    problems = verify_package(root)
    assert problems
    if which == "extra":
        assert len(problems) == 1
        assert NOT_THESE in problems[0]
        with pytest.raises(PackageError, match="the package fails its own check") as refused:
            build_package(copy_campaign(rescored, tmp_path, "second").root, tmp_path / "again", hashes=hashes)
        assert NOT_THESE in str(refused.value)
        assert not list((tmp_path / "again").glob("*.tar.gz"))
    else:
        assert "analysis.json: differs from what the records give" in problems
        assert not any(GUARD in problem for problem in problems)
    assert main(["package-check", str(root)]) == 1


def test_r17_an_entry_that_names_other_run_digests_than_the_store_holds_is_refused(
    rescored, hashes, tmp_path, monkeypatch
):
    """R17: the decision must be about this store: an entry whose run digests are not the ones the snapshot records is
    not honoured, so a package cannot carry another batch's decision."""
    monkeypatch.setenv(SCORERS, "other")
    copy = copy_campaign(rescored, tmp_path, "tampered")
    edit_analysis(copy.root, lambda data: data["rescore"].update(run_scorer_digests=["1" * 64]))
    with pytest.raises(PackageError, match="the package fails its own check") as refused:
        build_package(copy.root, tmp_path / "dist", hashes=hashes)
    assert NOT_THESE in str(refused.value)
    assert not list((tmp_path / "dist").glob("*.tar.gz"))


def test_r17_a_batch_that_was_not_re_scored_packages_and_checks_exactly_as_it_did(
    campaign, hashes, tmp_path, monkeypatch
):
    """R17: with the scorers the run had in force, nothing is recorded or honoured: the package builds, its analysis has
    no entry and no report line, and it checks, whatever the `rescore` branch of the guard allows."""
    monkeypatch.setenv(SCORERS, "same")
    root = unpack(package_of(campaign, hashes, tmp_path), tmp_path)
    assert "rescore" not in analysis_of(root)
    assert b"Re-scored" not in (root / "out" / "report.md").read_bytes()
    assert verify_package(root) == []
    assert main(["package-check", str(root)]) == 0


def test_r17_an_entry_on_a_batch_that_re_scored_nothing_is_refused_by_the_byte_comparison(
    campaign, hashes, tmp_path, monkeypatch
):
    """R17: the byte comparison stays the guard: an entry added to the analysis of a batch whose scorers are the run's
    own is no re-score, so the derived analysis has none and the build refuses the shipped file as differing."""
    monkeypatch.setenv(SCORERS, "same")
    copy = copy_campaign(campaign, tmp_path, "stray")
    digest = json.loads(copy.sql("SELECT value FROM meta WHERE key='fingerprint'")[0]["value"])["scorer_digest"]
    edit_analysis(
        copy.root, lambda data: data.update(rescore={"run_scorer_digests": [digest], "analysis_scorer_digest": digest})
    )
    with pytest.raises(PackageError, match="the package fails its own check") as refused:
        build_package(copy.root, tmp_path / "dist", hashes=hashes)
    assert "analysis.json: differs from what the records give" in str(refused.value)
    assert not list((tmp_path / "dist").glob("*.tar.gz"))
