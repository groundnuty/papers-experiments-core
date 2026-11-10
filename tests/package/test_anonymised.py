"""An anonymised package holds no commit id and no source URL, of the study or of the core, and still passes its check.

The campaign runs once per module with the core's source reported as a version-control install, so its URL and commit
reach every place an environment is recorded; a real one in these tests is editable and has neither.
"""

import io
import json
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from package_support import stand_in_vendor, wheel_of
from study_support import HEAD, Campaign, clean_environment, cli, copy_campaign, finish, make_copy

from expcore.package import build as package_build
from expcore.package.build import PACKAGE_FILE, build_package, verify_package
from expcore.package.spec import PackageError
from expcore.package.wheels import Wheel
from expcore.provenance import environment as environment_module
from expcore.provenance.codestamp import STAMP
from expcore.provenance.environment import CORE_DISTRIBUTION
from expcore.provenance.hashfile import render_hash_file
from expcore.study.pipeline import OUTPUT_FILES

NAME = "tiny-study-package"
AUTHOR = "Quillon Varek"  # invented
CORE_COMMIT = "b1" * 20  # invented
CORE_URL = "https://code.example.org/qvarek/tiny-core"  # invented
SOURCE = {"kind": "vcs", "vcs": "git", "url": CORE_URL, "commit": CORE_COMMIT}

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


@pytest.fixture(scope="module", autouse=True)
def core_from_a_repository():
    """The core reports a version-control source; every build vendors stand-in wheels."""
    real = environment_module.source_of

    def source_of(dist):
        return dict(SOURCE) if dist.metadata["Name"] == CORE_DISTRIBUTION else real(dist)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(environment_module, "source_of", source_of)
        patch.setattr(package_build, "vendor_wheels", stand_in_vendor)
        yield


@pytest.fixture(scope="module")
def hashes(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("hashes") / "private.sha256"
    path.write_text(render_hash_file(["zarnothwick"]), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def campaign(tmp_path_factory, clean_environment, core_from_a_repository) -> Campaign:
    campaign = Campaign(make_copy(tmp_path_factory.mktemp("campaign"), "study"))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture(scope="module")
def anonymisation(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("list") / "anon.toml"
    path.write_text(f'files = ["README.md"]\n\n[terms]\n"{AUTHOR}" = "Author A"\n', encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def built(campaign, hashes, anonymisation, tmp_path_factory) -> Path:
    return build_package(campaign.root, tmp_path_factory.mktemp("dist"), hashes=hashes, anonymise=anonymisation)


def unpack(archive: Path, where: Path) -> Path:
    with tarfile.open(archive) as members:
        members.extractall(where, filter="data")
    return where / NAME


def every_byte(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def record_files(root: Path) -> list[Path]:
    return [p for p in (root / "out" / "runs").rglob("*.json") if "." in p.stem and p.stem.split(".")[-1].isdigit()]


def test_k11_no_commit_id_or_source_url_of_the_study_or_the_core_is_anywhere_in_the_package(built, tmp_path):
    """K11: the study's commit and the core's commit and URL, recorded by the campaign in the fingerprint and the
    check, appear in no file of the package, the database and the wheels included."""
    files = every_byte(unpack(built, tmp_path))
    assert any(name.endswith(".sqlite") for name in files)
    for name, data in files.items():
        for hidden in (HEAD[:12], CORE_COMMIT[:12], "code.example.org", "qvarek"):
            assert hidden.encode() not in data, f"{name} holds {hidden}"


def test_k11_the_records_hold_the_pseudo_commit_of_their_tree_and_nothing_else_changed(campaign, built, tmp_path):
    """K11: each record's commit is the first 40 digits of its own tree hash, and every other byte of the record is
    the author's, so every digest the record pins stays valid."""
    root = unpack(built, tmp_path)
    records = record_files(root)
    assert len(records) == 22
    for path in records:
        rewritten = json.loads(path.read_text(encoding="utf-8"))
        original = json.loads((campaign.root / path.relative_to(root)).read_text(encoding="utf-8"))
        assert rewritten["code"]["commit"] == rewritten["code"]["tree_sha256"][:40]
        assert {**rewritten, "code": original["code"]} == original


def test_k11_the_anonymised_package_passes_its_own_check_byte_for_byte(campaign, built, tmp_path, clean_environment):
    """K11: the check was made again over the rewritten records and replayed them, and the tables regenerate; they
    are the author's tables with the commit replaced."""
    root = unpack(built, tmp_path)
    assert verify_package(root) == []
    check = json.loads((root / "out" / "runs" / "replay-check.json").read_text(encoding="utf-8"))
    assert check["n_ok"] == check["n_records"] == 22
    assert check["failures"] == []
    pseudo = check["code"]["tree_sha256"][:40]
    assert check["code"]["commit"] == pseudo
    assert json.loads((root / STAMP).read_text(encoding="utf-8"))["head"] == pseudo
    for name in OUTPUT_FILES:
        original = (campaign.root / "out" / name).read_bytes()
        assert (root / "out" / name).read_bytes() == original.replace(HEAD.encode(), pseudo.encode())
    done = subprocess.run(
        ["make", "-C", str(root), "-f", "Makefile", "check-installed", f"PYTHON={sys.executable}"],
        capture_output=True,
        text=True,
        env=clean_environment,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_k11_package_json_says_the_identity_is_the_tree_hash_and_keeps_only_names_and_versions(built, tmp_path):
    """K11: PACKAGE.json states what the identity is, with no source; the snapshot's fingerprint and the check agree."""
    root = unpack(built, tmp_path)
    package = json.loads((root / PACKAGE_FILE).read_text(encoding="utf-8"))
    assert package["code"]["commit"] == package["code"]["tree_sha256"][:40]
    assert package["code_identity"].startswith("tree hash only")
    assert "sources" not in package["environment"]
    assert package["environment"]["distributions"][CORE_DISTRIBUTION] == package["core_version"]
    check = json.loads((root / "out" / "runs" / "replay-check.json").read_text(encoding="utf-8"))
    snapshot = sqlite3.connect(root / "out" / "runs" / "campaign-snapshot.sqlite")
    fingerprint = json.loads(snapshot.execute("SELECT value FROM meta WHERE key = 'fingerprint'").fetchone()[0])
    snapshot.close()
    assert check["environment"] == fingerprint["environment"] == package["environment"]


def test_k06_two_anonymised_builds_of_one_tree_are_byte_identical(campaign, hashes, anonymisation, built, tmp_path):
    """K06: the rewritten records, the rebuilt snapshot and the new check depend on nothing but the tree."""
    again = build_package(
        copy_campaign(campaign, tmp_path, "copy").root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation
    )
    assert again.read_bytes() == built.read_bytes()


def test_k11_a_plain_package_says_where_the_core_came_from(campaign, hashes, tmp_path):
    """K11: without an anonymisation list, PACKAGE.json and the check keep the core's URL and commit (#45)."""
    root = unpack(build_package(campaign.root, tmp_path / "dist", hashes=hashes), tmp_path / "unpacked")
    package = json.loads((root / PACKAGE_FILE).read_text(encoding="utf-8"))
    assert package["environment"]["sources"][CORE_DISTRIBUTION] == SOURCE
    assert package["code"]["commit"] == HEAD
    assert package["code_identity"] == "commit and tree hash"
    assert verify_package(root) == []


@pytest.mark.parametrize(
    ("line", "finding"),
    [
        (f"Built at {HEAD}.", "a commit id of the study's repository"),
        (f"The core was {CORE_COMMIT[:12]}.", "a commit id of the study's repository"),
        ("Source: https://code.example.org/qvarek/tiny-core.git", "a source URL of the study's repository"),
        ("Fork of git@code.example.org:qvarek/tiny-core.git today", "a source URL of the study's repository"),
        ("See code.example.org/qvarek/tiny-core for the tool", "a source URL of the study's repository"),
    ],
)
def test_k11_a_commit_or_source_url_left_in_a_file_refuses_the_build_without_showing_it(
    campaign, hashes, anonymisation, tmp_path, line, finding
):
    """K11: the safety net behind the rewriting: a commit id or source URL in a file nothing rewrites refuses the
    build, naming the file and line and never the value (K05)."""
    root = copy_campaign(campaign, tmp_path, "copy").root
    with (root / "README.md").open("a", encoding="utf-8") as readme:
        readme.write(f"\n{line}\n")
    with pytest.raises(PackageError, match=rf"README\.md:\d+: {finding}") as refused:
        build_package(root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation)
    assert CORE_COMMIT[:12] not in str(refused.value)
    assert HEAD[:12] not in str(refused.value)
    assert "qvarek" not in str(refused.value)
    assert not (tmp_path / "dist").exists() or not list((tmp_path / "dist").glob("*.tar.gz"))


@pytest.mark.parametrize(
    "line",
    [
        "Clone git@forge.example:someauthor/thestudy.git to start.",
        "See https://forge.example/someauthor/thestudy for the data.",
        "See someauthor/thestudy on the forge.",
    ],
)
def test_k11_a_remote_of_the_studys_repository_is_found_in_every_form_it_can_be_written(
    campaign, hashes, anonymisation, repo, tmp_path, monkeypatch, line
):
    """K11: the study's own repository is an identifying URL too: its remote is searched for in scp form, as a URL and
    as owner/name, and the commit the repository is at has to go with it."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    root = copy_campaign(campaign, repo.path, "study").root
    repo.git("remote", "add", "origin", "git@forge.example:someauthor/thestudy.git")
    with (root / "README.md").open("a", encoding="utf-8") as readme:
        readme.write(f"\n{line}\n")
    repo.commit("the study")
    with pytest.raises(PackageError, match=r"README\.md:\d+: a source URL") as refused:
        build_package(root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation)
    assert "someauthor" not in str(refused.value)


def test_k11_the_core_wheel_is_searched_for_the_identity_too(campaign, hashes, anonymisation, tmp_path):
    """K11: a wheel read by the checks is searched like any file, so a core whose sources name its URL refuses the
    build; the example the core ships must therefore never name it."""

    def naming_the_url(distributions, supplied=None):
        out = []
        for wheel in stand_in_vendor(distributions):
            if wheel.name == CORE_DISTRIBUTION:
                filename, data = wheel_of(
                    wheel.name, wheel.version, {"expcore/home.py": f"HOME = '{CORE_URL}'\n".encode()}
                )
                wheel = Wheel(wheel.name, wheel.version, filename, data, wheel.from_index)
            out.append(wheel)
        return out

    root = copy_campaign(campaign, tmp_path, "copy").root
    with pytest.raises(PackageError, match=r"papers_experiments_core-.*whl.*a source URL"):
        build_package(root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation, vendor=naming_the_url)


def test_k11_a_core_installed_from_an_index_with_a_listed_authors_name_is_refused(
    campaign, hashes, anonymisation, tmp_path
):
    """K11: a wheel is read whatever it was installed from, so a core from an index (or a private one) that names a
    listed author in its metadata refuses the anonymised build; a wheel that carries a direct_url.json, which names a
    URL and a commit, is refused too."""

    def index_core(distributions, supplied=None):
        out = []
        for wheel in stand_in_vendor(distributions):
            if wheel.name == CORE_DISTRIBUTION:
                notice = {"expcore/__init__.py": f'"""The core, by {AUTHOR}."""\n'.encode()}
                filename, data = wheel_of(wheel.name, wheel.version, notice)
                wheel = Wheel(wheel.name, wheel.version, filename, data, from_index=True)
            out.append(wheel)
        return out

    def with_direct_url(distributions, supplied=None):
        out = []
        for wheel in stand_in_vendor(distributions):
            if wheel.name == "numpy":
                buffer = io.BytesIO(wheel.data)
                with zipfile.ZipFile(buffer, "a") as archive:
                    archive.writestr(f"numpy-{wheel.version}.dist-info/direct_url.json", '{"url": "https://x.example"}')
                wheel = Wheel(wheel.name, wheel.version, wheel.filename, buffer.getvalue(), from_index=True)
            out.append(wheel)
        return out

    root = copy_campaign(campaign, tmp_path, "copy").root
    with pytest.raises(
        PackageError, match=r"papers_experiments_core-[^/]+\.whl/expcore/__init__\.py:1: a term"
    ) as refused:
        build_package(root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation, vendor=index_core)
    assert AUTHOR not in str(refused.value)
    with pytest.raises(PackageError, match=r"direct_url\.json: names where the distribution came from"):
        build_package(root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation, vendor=with_direct_url)


def test_the_core_itself_names_no_repository_url_in_what_it_ships():
    """K11: the example and the sources travel in the core's wheel, which an anonymised package vendors, and the
    package's checks read it, so a URL of the core's repository in them would refuse every anonymised build."""
    root = Path(__file__).resolve().parents[2]
    for directory in ("src", "examples"):
        for path in (root / directory).rglob("*"):
            if path.is_file() and path.suffix in (".py", ".md", ".toml", ".txt"):
                assert "github.com/" not in path.read_text(encoding="utf-8"), path


def test_k11_a_local_version_label_refuses_an_anonymised_build(campaign, hashes, anonymisation, tmp_path, monkeypatch):
    """K11: a version such as 1.2+g1a2b3c can spell a commit, and the anonymised package keeps every version."""
    root = copy_campaign(campaign, tmp_path, "copy").root
    check_file = root / "out" / "runs" / "replay-check.json"
    check = json.loads(check_file.read_text(encoding="utf-8"))
    check["environment"]["distributions"]["numpy"] = "2.5.4+g1a2b3c"
    check_file.write_text(json.dumps(check), encoding="utf-8")
    now = json.loads(json.dumps(check["environment"]))
    monkeypatch.setattr(package_build, "environment", lambda requires=(): now)
    with pytest.raises(PackageError, match="numpy has a local version label"):
        build_package(root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation)


def test_k11_a_record_changed_after_the_check_refuses_the_anonymised_build_naming_it(
    campaign, hashes, anonymisation, tmp_path
):
    """K11: anonymising makes the check again, so it first holds every record to the bytes the original check hashed;
    a record swapped in afterwards would otherwise get a pseudo-commit and pass under the original check's time."""
    root = copy_campaign(campaign, tmp_path, "copy").root
    record = sorted(record_files(root))[0]
    data = json.loads(record.read_text(encoding="utf-8"))
    data["code"]["commit"] = "c" * 40
    record.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    rel = record.relative_to(root).as_posix()
    with pytest.raises(PackageError, match=rf"{rel} differs from what the replay check hashed"):
        build_package(root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation)
    record.write_bytes((campaign.root / rel).read_bytes())
    (record.parent / "extra.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PackageError, match=r"extra\.json is not what the replay check hashed"):
        build_package(root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation)


def test_k11_a_table_the_records_do_not_give_is_refused_in_an_anonymised_build(
    campaign, hashes, anonymisation, tmp_path
):
    """K11: the anonymised tables are the author's with the commit replaced, or the build is refused."""
    root = copy_campaign(campaign, tmp_path, "copy").root
    analysis = root / "out" / "analysis.json"
    analysis.write_text(analysis.read_text(encoding="utf-8").replace('"alpha": 0.05', '"alpha": 0.1'), "utf-8")
    with pytest.raises(PackageError, match=r"analysis\.json: differs from what the records give"):
        build_package(root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation)
