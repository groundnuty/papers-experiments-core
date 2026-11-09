"""The replication package of the example study: built from its records, refused when it would leak or lie, and
verified by its own `make check`.

The campaign runs once per module, under a directory named by an invented private name that the test hash file
lists, so every package built here also shows that no path of the machine reaches the package. Every package here
vendors stand-in wheels (`package_support`); the make target that installs real ones is tested in test_wheels.py.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import zlib
from pathlib import Path

import pytest
from package_support import stand_in, stand_in_vendor
from study_support import Campaign, clean_environment, cli, copy_campaign, finish, make_copy

from expcore.cli import main
from expcore.package import build as package_build
from expcore.package.archive import SUMS_FILE, render_sums
from expcore.package.build import (
    PACKAGE_FILE,
    build_package,
    default_hashes,
    private_hashes,
    private_list_guard,
    verify_package,
)
from expcore.package.spec import PackageError
from expcore.package.wheels import REQUIREMENTS, Wheel
from expcore.provenance.environment import CORE_DISTRIBUTION
from expcore.provenance.hashfile import render_hash_file
from expcore.study import pipeline

PRIVATE = "zarnothwick"  # invented; the test hash file treats it as a name private everywhere
AUTHOR = "Quillon Varek"  # invented; what an anonymisation list hides
NAME = "tiny-study-package"

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


@pytest.fixture(scope="module", autouse=True)
def stand_in_wheels():
    """Every build of this module vendors stand-in wheels, the command line's included."""
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
        ["make", "-C", str(root), "-f", "Makefile", "check-installed", f"PYTHON={sys.executable}"],
        capture_output=True,
        text=True,
        env=clean_environment,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "every output regenerates from the records byte for byte" in done.stdout
    # the instructions name the Makefile, so no other file make would read first can stand in for it
    assert json.loads((root / PACKAGE_FILE).read_text(encoding="utf-8"))["check"].startswith("make -f Makefile check")
    assert "`make -f Makefile check`" in (root / "Makefile").read_text(encoding="utf-8")
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


def test_k03_an_anonymisation_list_in_the_studys_repository_refuses_the_build(
    campaign, hashes, repo, tmp_path, monkeypatch
):
    """K03: the private list never enters the repository or the output: a list anywhere in the repository that holds
    the study, not only in the study's own directory, refuses the build before anything is written."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))  # no repository above this test's directory
    root = copy_campaign(campaign, repo.path / "studies", "study").root
    listed = anonymisation(repo.path / "anon.toml", {AUTHOR: "Author A"})
    with pytest.raises(PackageError, match="K03"):
        build_package(root, tmp_path / "dist", hashes=hashes, anonymise=listed)
    assert list((tmp_path / "dist").iterdir()) == []
    assert private_list_guard(root, None) == [repo.path, root]
    assert private_list_guard(tmp_path, tmp_path / "dist") == [tmp_path, tmp_path / "dist"]


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


FIGURE = b"%PDF-1.4\n<< /Filter /FlateDecode >>\nstream\n" + zlib.compress(b"a drawn figure") + b"\nendstream\n"


def with_figure(campaign: Campaign, tmp_path: Path, attested: str | None) -> Path:
    """A copy of the study that includes a PDF figure, attested with the given SHA-256 or not at all."""
    root = a_copy(campaign, tmp_path)
    (root / "figure.pdf").write_bytes(FIGURE)
    spec = root / "package.toml"
    text = spec.read_text(encoding="utf-8").replace('    "README.md",\n', '    "README.md",\n    "figure.pdf",\n')
    if attested is not None:
        text += f'\n[[opaque]]\npath = "figure.pdf"\nsha256 = "{attested}"\nreason = "a figure, read by hand"\n'
    spec.write_text(text, encoding="utf-8")
    return root


def test_k03_a_pdf_the_scan_cannot_read_is_refused_unless_attested_by_its_exact_hash(campaign, hashes, tmp_path):
    """K03: the scan fails closed: a PDF is refused, and goes in unscanned only when package.toml attests that exact
    file by its SHA-256 with a reason; a file that differs from the attested one is refused."""
    with pytest.raises(PackageError, match=r"figure\.pdf: refused, PDF content"):
        build_package(with_figure(campaign, tmp_path / "plain", None), tmp_path / "dist-plain", hashes=hashes)
    wrong = with_figure(campaign, tmp_path / "wrong", "0" * 64)
    with pytest.raises(PackageError, match=r"figure\.pdf: its SHA-256 is not the one \[\[opaque\]\] attests"):
        build_package(wrong, tmp_path / "dist-wrong", hashes=hashes)
    right = with_figure(campaign, tmp_path / "right", hashlib.sha256(FIGURE).hexdigest())
    archive = build_package(right, tmp_path / "dist-right", hashes=hashes)
    unpacked = unpack(archive, tmp_path / "unpacked")
    assert (unpacked / "figure.pdf").read_bytes() == FIGURE
    attested = json.loads((unpacked / PACKAGE_FILE).read_text(encoding="utf-8"))["opaque"]
    assert attested == [
        {"path": "figure.pdf", "sha256": hashlib.sha256(FIGURE).hexdigest(), "reason": "a figure, read by hand"}
    ]
    assert verify_package(unpacked) == []


def test_the_command_line_builds_and_checks_a_package(campaign, hashes, tmp_path, capsys):
    assert main(["package", str(campaign.root), "--out", str(tmp_path / "dist"), "--hashes", str(hashes)]) == 0
    assert f"{NAME}.tar.gz (sha256 " in capsys.readouterr().out
    root = unpack(tmp_path / "dist" / f"{NAME}.tar.gz", tmp_path / "unpacked")
    assert main(["package-check", str(root)]) == 0
    shutil.rmtree(root / "out" / "runs")
    assert main(["package-check", str(root)]) == 1
    assert main(["package", str(tmp_path), "--out", str(tmp_path / "x")]) == 1  # no study there: refused, not a crash


# --- the environment and its wheels ----------------------------------------------------------------------------------


def test_k10_the_package_records_the_checks_environment_and_vendors_one_pinned_wheel_per_distribution(built, tmp_path):
    """K10: PACKAGE.json names the environment the replay check ran under, and wheels/ holds one wheel per distribution
    of it, each listed with its hash in PACKAGE.json, in requirements.txt (what make check installs) and in
    SHA256SUMS."""
    root = unpack(built, tmp_path)
    package = json.loads((root / PACKAGE_FILE).read_text(encoding="utf-8"))
    check = json.loads((root / "out" / "runs" / "replay-check.json").read_text(encoding="utf-8"))
    assert package["environment"] == check["environment"] == pipeline.environment()
    assert CORE_DISTRIBUTION in package["environment"]["distributions"]
    listed = {wheel["name"]: wheel for wheel in package["wheels"]}
    assert set(listed) == set(package["environment"]["distributions"])
    pins = (root / REQUIREMENTS).read_text(encoding="utf-8")
    sums = (root / SUMS_FILE).read_text(encoding="utf-8")
    for name, wheel in listed.items():
        data = (root / wheel["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == wheel["sha256"]
        assert f"{name}=={wheel['version']} --hash=sha256:{wheel['sha256']}\n" in pins
        assert f"{wheel['sha256']}  {wheel['file']}\n" in sums
        assert wheel["scanned"] == (name == CORE_DISTRIBUTION)
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    assert "--no-index" in makefile
    assert f"Python {package['environment']['python']}" in makefile


def test_k10_the_package_check_refuses_an_environment_other_than_the_one_it_names(built, tmp_path, monkeypatch):
    """K10: a reader whose environment differs from the package's, in one version, is told so and how to install
    the right one, before any output is derived."""
    root = unpack(built, tmp_path)
    changed = pipeline.environment()
    changed["distributions"]["numpy"] = "0.0.1"
    monkeypatch.setattr(package_build, "environment", lambda requires=(): changed)
    [problem] = verify_package(root)
    assert "is not the one PACKAGE.json names" in problem
    assert "numpy" in problem
    assert "`make -f Makefile check` installs it from wheels/" in problem


def test_k10_the_cores_wheel_is_read_by_the_token_check_and_a_public_release_goes_in_attested(
    campaign, hashes, tmp_path
):
    """K10: a wheel from a direct-URL install, the core's, is read like any file, so a private name in it refuses the
    build; a public release's wheel goes in unread, listed as not scanned with its hash."""

    def leaky(distributions, supplied=None):
        wheels = stand_in_vendor(distributions)
        core = next(w for w in wheels if w.name == CORE_DISTRIBUTION)
        name, data = stand_in(core.name, core.version)
        with_name = data.replace(b"Version: ", f"Summary: built on {PRIVATE}\nVersion: ".encode(), 1)
        return [w if w is not core else Wheel(w.name, w.version, name, with_name, scanned=True) for w in wheels]

    def public_leak(distributions, supplied=None):
        return [
            Wheel(w.name, w.version, w.filename, w.data + PRIVATE.encode(), scanned=False) if w.name == "numpy" else w
            for w in stand_in_vendor(distributions)
        ]

    with pytest.raises(PackageError, match=r"wheels/papers_experiments_core-.*\.whl") as refused:
        build_package(a_copy(campaign, tmp_path / "a"), tmp_path / "dist-a", hashes=hashes, vendor=leaky)
    assert PRIVATE not in str(refused.value)
    archive = build_package(a_copy(campaign, tmp_path / "b"), tmp_path / "dist-b", hashes=hashes, vendor=public_leak)
    package = json.loads((unpack(archive, tmp_path / "unpacked") / PACKAGE_FILE).read_text(encoding="utf-8"))
    assert {w["name"]: w["scanned"] for w in package["wheels"]}["numpy"] is False


def test_k10_a_build_under_another_environment_than_the_checks_is_refused(campaign, hashes, tmp_path, monkeypatch):
    """K10: the wheels are packed from the build's environment, so it must be the one the check ran under."""
    changed = pipeline.environment()
    changed["distributions"]["numpy"] = "0.0.1"
    monkeypatch.setattr(package_build, "environment", lambda requires=(): changed)
    with pytest.raises(PackageError, match="another environment than this build's"):
        build_package(a_copy(campaign, tmp_path), tmp_path / "dist", hashes=hashes)
