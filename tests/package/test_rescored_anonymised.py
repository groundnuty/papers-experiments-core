"""R17: an anonymised package of a re-scored batch is built and checked with no flag, like a plain one.

Anonymising rewrites the records and the snapshot, then derives the outputs again from them and compares each with the
author's (`package.identity`). The author's analysis.json holds the decision recorded at `analyze` (`rescore`), and the
rewritten snapshot's fingerprint keeps its scorer digest, so the same entry is honoured there and nothing else is.

The campaign runs once per module with the core at an invented commit, as in test_anonymised.py, from the example with
both builders.
"""

import json
import tarfile
from pathlib import Path

import pytest
from build_analysis_support import SCORERS, rescored_copy, with_builders
from package_support import stand_in_vendor
from study_support import Campaign, clean_environment, cli, copy_campaign, finish

from expcore.cli import main
from expcore.package import build as package_build
from expcore.package.build import build_package, verify_package
from expcore.package.spec import PackageError
from expcore.provenance import core_identity
from expcore.provenance import environment as environment_module
from expcore.provenance.environment import CORE_DISTRIBUTION
from expcore.provenance.hashfile import render_hash_file

NAME = "tiny-study-package"
CORE_COMMIT = "b1" * 20  # invented
CORE_TREE = "7a" * 20  # invented
CORE_URL = "https://code.example.org/qvarek/tiny-core"  # invented
SOURCE = {"kind": "vcs", "vcs": "git", "url": CORE_URL, "commit": CORE_COMMIT}  # where the core was installed from
IDENTITY = core_identity.CoreIdentity(CORE_COMMIT, CORE_TREE, False, "f" * 64, "build")  # what the environment names

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


@pytest.fixture(scope="module", autouse=True)
def core_from_a_repository():
    """The core is at an invented commit, installed from a version-control source; every build vendors stand-in
    wheels."""
    real = environment_module.source_of

    def source_of(dist):
        return dict(SOURCE) if dist.metadata["Name"] == CORE_DISTRIBUTION else real(dist)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(core_identity, "current", lambda: IDENTITY)
        patch.setattr(environment_module, "source_of", source_of)
        patch.setattr(package_build, "vendor_wheels", stand_in_vendor)
        yield


@pytest.fixture(scope="module")
def hashes(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("hashes") / "private.sha256"
    path.write_text(render_hash_file(["zarnothwick"]), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def anonymisation(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("list") / "anon.toml"
    path.write_text('files = ["README.md"]\n\n[terms]\n"Quillon Varek" = "Author A"\n', encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def campaign(tmp_path_factory, clean_environment, core_from_a_repository) -> Campaign:
    campaign = Campaign(with_builders(tmp_path_factory.mktemp("campaign"), "study"))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture(scope="module")
def rescored(campaign, tmp_path_factory) -> Campaign:
    return rescored_copy(campaign, tmp_path_factory.mktemp("rescored"))


def unpack(archive: Path, where: Path) -> Path:
    with tarfile.open(archive) as members:
        members.extractall(where, filter="data")
    return where / NAME


def analysis_of(root: Path) -> dict:
    return json.loads((root / "out" / "analysis.json").read_text(encoding="utf-8"))


def test_r17_an_anonymised_package_of_a_re_scored_batch_builds_and_passes_its_check_with_no_flag(
    rescored, hashes, anonymisation, tmp_path, monkeypatch
):
    """R17: the anonymised records are analysed again under the author's recorded decision: the build's own check and
    the unpacked package's check pass, and the entry the package ships is the author's, with the digests unchanged by
    anonymising."""
    monkeypatch.setenv(SCORERS, "other")
    built = build_package(
        copy_campaign(rescored, tmp_path, "copy").root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation
    )
    root = unpack(built, tmp_path)
    assert analysis_of(root)["rescore"] == analysis_of(rescored.root)["rescore"]
    assert b"- Re-scored (`--rescore`):" in (root / "out" / "report.md").read_bytes()
    assert verify_package(root) == []
    assert main(["package-check", str(root)]) == 0


def test_r17_an_anonymised_package_of_a_corrected_scorer_with_no_rescore_entry_is_refused(
    campaign, hashes, anonymisation, tmp_path, monkeypatch
):
    """R17: the anonymised records cannot be analysed with scorers other than the run's unless the author's analysis
    recorded the re-score: the refusal is the guard's, naming the scorers and the flag, with no digest, and no archive
    is written."""
    monkeypatch.setenv(SCORERS, "other")
    with pytest.raises(PackageError, match="the anonymised records cannot be analysed") as refused:
        build_package(
            copy_campaign(campaign, tmp_path, "copy").root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation
        )
    assert "campaign was run with other scorers" in str(refused.value)
    assert "--rescore" in str(refused.value)
    assert not list((tmp_path / "dist").glob("*.tar.gz"))
