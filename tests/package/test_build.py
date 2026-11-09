"""The replication package of the example study: built from its records, refused when it would leak or lie, and
verified by its own `make check`.

The campaign runs once per module, under a directory named by an invented private name that the test hash file
lists, so every package built here also shows that no path of the machine reaches the package.
"""

import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from study_support import Campaign, clean_environment, cli, copy_campaign, finish, make_copy

from expcore.cli import main
from expcore.package import build as package_build
from expcore.package.archive import SUMS_FILE, render_sums
from expcore.package.build import PACKAGE_FILE, build_package, default_hashes, private_hashes, verify_package
from expcore.package.spec import PackageError
from expcore.provenance.hashfile import render_hash_file

PRIVATE = "zarnothwick"  # invented; the test hash file treats it as a name private everywhere
AUTHOR = "Quillon Varek"  # invented; what an anonymisation list hides
NAME = "tiny-study-package"

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


@pytest.fixture(scope="module")
def hashes(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("hashes") / "private.sha256"
    path.write_text(render_hash_file([PRIVATE]), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def campaign(tmp_path_factory, clean_environment) -> Campaign:
    """The example study, run, checked, analysed and reported from a stamped clean copy."""
    home = tmp_path_factory.mktemp("campaign") / f"{PRIVATE}-home"
    home.mkdir()
    campaign = Campaign(make_copy(home, "study"))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture(scope="module")
def built(campaign, hashes, tmp_path_factory) -> Path:
    return build_package(campaign.root, tmp_path_factory.mktemp("dist"), hashes=hashes)


def unpack(archive: Path, where: Path) -> Path:
    with tarfile.open(archive) as members:
        members.extractall(where, filter="data")
    return where / NAME


def a_copy(campaign: Campaign, tmp_path: Path) -> Path:
    return copy_campaign(campaign, tmp_path, "copy").root


def test_k07_the_unpacked_package_of_the_example_passes_its_own_make_check(built, tmp_path, clean_environment):
    """K07: `make check` regenerates every table from the records and compares byte for byte; the example study's
    package, built from a clean stamped copy, passes it wherever it is unpacked."""
    root = unpack(built, tmp_path)
    done = subprocess.run(
        ["make", "-C", str(root), "check", f"PYTHON={sys.executable}"],
        capture_output=True,
        text=True,
        env=clean_environment,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "every output regenerates from the records byte for byte" in done.stdout
    assert not (root / "__pycache__").exists()  # the check leaves the package as it found it


def test_k07_a_changed_table_fails_the_check_even_with_its_checksum_updated(built, tmp_path):
    """K07: a table edited after the analysis is caught by the checksums, and by the regeneration when they agree."""
    root = unpack(built, tmp_path)
    report = root / "out" / "report.md"
    report.write_text(report.read_text(encoding="utf-8") + "A finding nobody computed.\n", encoding="utf-8")
    assert verify_package(root) == [f"out/report.md: its bytes differ from {SUMS_FILE}"]
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    (root / SUMS_FILE).write_bytes(render_sums(files))
    assert verify_package(root) == ["report.md: differs from what the records give"]
    assert main(["package-check", str(root)]) == 1


def test_k06_two_builds_of_one_tree_are_byte_identical_wherever_the_tree_lies(campaign, hashes, built, tmp_path):
    """K06: archives are deterministic: a copy of the study elsewhere builds the same bytes."""
    again = build_package(a_copy(campaign, tmp_path), tmp_path / "dist", hashes=hashes)
    assert again.read_bytes() == built.read_bytes()


def test_no_path_of_the_machine_the_campaign_ran_on_reaches_the_package(campaign, built):
    """The campaign ran under a directory with a private name and the token check passed; no member holds the path."""
    with tarfile.open(built) as members:
        names = members.getnames()
        contents = b"".join(members.extractfile(m).read() for m in members.getmembers())
    assert str(campaign.root).encode() not in contents
    assert PRIVATE.encode() not in contents
    assert {"Makefile", SUMS_FILE, PACKAGE_FILE, ".code-stamp.json", "out/report.md"} <= {
        n.removeprefix(f"{NAME}/") for n in names
    }
    assert not any("/out/state.sqlite" in n or "/out/leases" in n for n in names)  # the live campaign stays home


def test_k05_a_private_name_in_an_included_file_refuses_the_build_showing_only_a_hash_prefix(
    campaign, hashes, tmp_path
):
    """K05: findings never carry the matched value; the token check is the study-token scanner's own."""
    root = a_copy(campaign, tmp_path)
    with (root / "README.md").open("a", encoding="utf-8") as readme:
        readme.write(f"\nRun on the {PRIVATE} cluster.\n")
    with pytest.raises(PackageError, match=r"README\.md:\d+ text [0-9a-f]{12}") as refused:
        build_package(root, tmp_path / "dist", hashes=hashes)
    assert PRIVATE not in str(refused.value)
    assert not (tmp_path / "dist").exists() or not any((tmp_path / "dist").iterdir())


def test_k01_a_hash_file_that_is_missing_or_names_nothing_private_refuses_the_build(tmp_path):
    """K01: a check with no input fails."""
    with pytest.raises(PackageError, match="K01"):
        private_hashes(tmp_path / "absent.sha256")
    code_only = tmp_path / "code-only.sha256"
    code_only.write_text(render_hash_file([("a-condition-name", "code")]), encoding="utf-8")
    with pytest.raises(PackageError, match="K01"):
        private_hashes(code_only)


def test_the_default_hash_file_is_the_cores_public_one_with_names_private_everywhere():
    assert default_hashes().is_file()
    assert private_hashes(default_hashes())


def test_a_check_made_under_code_that_is_not_a_clean_commit_is_refused(campaign, hashes, tmp_path):
    root = a_copy(campaign, tmp_path)
    check_file = root / "out" / "runs" / "replay-check.json"
    check = json.loads(check_file.read_text(encoding="utf-8"))
    check["code"]["dirty"] = True
    check_file.write_text(json.dumps(check), encoding="utf-8")
    with pytest.raises(PackageError, match="not a clean commit"):
        build_package(root, tmp_path / "dist", hashes=hashes)


def test_a_table_the_records_do_not_give_is_refused_and_nothing_is_written(campaign, hashes, tmp_path):
    root = a_copy(campaign, tmp_path)
    analysis = root / "out" / "analysis.json"
    analysis.write_text(analysis.read_text(encoding="utf-8").replace('"alpha": 0.05', '"alpha": 0.1'), "utf-8")
    with pytest.raises(PackageError, match=r"analysis\.json: differs from what the records give"):
        build_package(root, tmp_path / "dist", hashes=hashes)
    assert list((tmp_path / "dist").iterdir()) == []


def test_every_file_the_check_needs_must_be_listed(campaign, hashes, tmp_path):
    root = a_copy(campaign, tmp_path)
    spec = root / "package.toml"
    spec.write_text(spec.read_text(encoding="utf-8").replace('    "out/report.md",\n', ""), encoding="utf-8")
    with pytest.raises(PackageError, match=r"needs out/report\.md"):
        build_package(root, tmp_path / "dist", hashes=hashes)


def test_outputs_made_by_another_core_version_are_refused(campaign, hashes, tmp_path, monkeypatch):
    monkeypatch.setattr(package_build, "sdk_version", lambda distribution: "9.9.9")
    with pytest.raises(PackageError, match=r"made with core .* this is core 9\.9\.9"):
        build_package(a_copy(campaign, tmp_path), tmp_path / "dist", hashes=hashes)


def test_the_output_may_not_lie_inside_the_batch_the_package_is_checked_against(campaign, hashes):
    with pytest.raises(PackageError, match="inside the study's out/"):
        build_package(campaign.root, campaign.root / "out" / "dist", hashes=hashes)


def anonymisation(where: Path, terms: dict[str, str]) -> Path:
    body = "\n".join(f"{json.dumps(term)} = {json.dumps(label)}" for term, label in terms.items())
    where.write_text(f'files = ["README.md"]\n\n[terms]\n{body}\n', encoding="utf-8")
    return where


def test_an_anonymised_package_hides_the_names_in_its_prose_and_still_passes_its_check(campaign, hashes, tmp_path):
    root = a_copy(campaign, tmp_path)
    with (root / "README.md").open("a", encoding="utf-8") as readme:
        readme.write(f"\nPrepared by {AUTHOR}.\n")
    listed = anonymisation(tmp_path / "anon.toml", {AUTHOR: "Author A"})
    archive = build_package(root, tmp_path / "dist", hashes=hashes, anonymise=listed)
    unpacked = unpack(archive, tmp_path / "unpacked")
    assert (unpacked / "README.md").read_text(encoding="utf-8").endswith("\nPrepared by Author A.\n")
    assert json.loads((unpacked / PACKAGE_FILE).read_text(encoding="utf-8"))["anonymised"] == ["README.md"]
    assert verify_package(unpacked) == []


def test_a_listed_term_in_a_code_file_fails_the_build_naming_file_and_line_and_is_never_rewritten(
    campaign, hashes, tmp_path
):
    """The term is a word of the plugin's code and of its records; the build names where, never what."""
    root = a_copy(campaign, tmp_path)
    before = (root / "plugin.py").read_bytes()
    with pytest.raises(PackageError, match=r"plugin\.py:\d+: a term of the anonymisation list") as refused:
        build_package(
            root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation(tmp_path / "a.toml", {"lookup": "L"})
        )
    assert "lookup" not in str(refused.value)
    assert (root / "plugin.py").read_bytes() == before


def test_the_command_line_builds_and_checks_a_package(campaign, hashes, tmp_path, capsys):
    assert main(["package", str(campaign.root), "--out", str(tmp_path / "dist"), "--hashes", str(hashes)]) == 0
    assert f"{NAME}.tar.gz (sha256 " in capsys.readouterr().out
    root = unpack(tmp_path / "dist" / f"{NAME}.tar.gz", tmp_path / "unpacked")
    assert main(["package-check", str(root)]) == 0
    shutil.rmtree(root / "out" / "runs")
    assert main(["package-check", str(root)]) == 1
    assert main(["package", str(tmp_path), "--out", str(tmp_path / "x")]) == 1  # no study there: refused, not a crash
