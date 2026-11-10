"""Which vendored wheels the study-token list skips: a pypi.org release whose bytes the study's uv.lock records.

The campaign runs once per module; each build vendors stand-in wheels, with one distribution of the environment
replaced by a small wheel whose text holds a private name, and writes the lock the case needs beside the study.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest
from package_support import installed_distribution, stand_in_vendor, wheel_of
from study_support import Campaign, clean_environment, cli, copy_campaign, finish, make_copy

from expcore.package import wheels as wheels_module
from expcore.package.build import PACKAGE_FILE, collect
from expcore.package.lockfile import LOCK_FILE, PYPI, find_lock, pypi_hashes, token_exempt
from expcore.package.spec import PackageError
from expcore.package.wheels import Wheel, vendor_wheels
from expcore.provenance.environment import CORE_DISTRIBUTION, environment
from expcore.provenance.hashfile import render_hash_file
from expcore.study.config import load_study

PRIVATE = "zarnothwick"  # invented; private everywhere in these tests
AUTHOR = "Quillon Varek"  # invented
RELEASE = "numpy"  # a distribution of the example's environment, stood in for by a small wheel
NOTES = f"{RELEASE}/notes.txt"

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


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
def campaign(tmp_path_factory, clean_environment) -> Campaign:
    campaign = Campaign(make_copy(tmp_path_factory.mktemp("campaign"), "study"))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture(scope="module")
def versions(campaign) -> dict[str, str]:
    found = environment(load_study(campaign.root).requires)["distributions"]
    assert RELEASE in found
    assert CORE_DISTRIBUTION in found
    return found


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def replacing(wheels: dict[str, tuple[str, bytes]], *, direct: bool = False):
    """Stand-ins, with the named distributions' wheels replaced by the bytes given, as a supplied wheel would be."""

    def vendor(distributions, supplied=None):
        out = []
        for wheel in stand_in_vendor(distributions):
            if wheel.name in wheels:
                filename, data = wheels[wheel.name]
                wheel = Wheel(wheel.name, wheel.version, filename, data, from_index=False, direct=direct)
            out.append(wheel)
        return out

    return vendor


def write_lock(where: Path, *entries: tuple[str, str, str, str]) -> Path:
    """A uv.lock with one package entry per (name, version, registry, wheel SHA-256)."""
    blocks = ['version = 1\nrevision = 3\nrequires-python = ">=3.12"\n']
    for name, version, registry, digest in entries:
        url = f"https://files.pythonhosted.org/packages/00/aa/{name}-{version}-py3-none-any.whl"
        blocks.append(
            f'[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {{ registry = "{registry}" }}\n'
            f'sdist = {{ url = "{url}.tar.gz", hash = "sha256:{"0" * 64}", size = 1 }}\n'
            f'wheels = [\n    {{ url = "{url}", hash = "sha256:{digest}", size = 1 }},\n]\n'
        )
    path = where / LOCK_FILE
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def release(version: str, text: str) -> tuple[str, bytes]:
    return wheel_of(RELEASE, version, {NOTES: text.encode()})


def test_k11_a_pypi_release_whose_bytes_the_lock_records_skips_the_token_list_and_package_json_lists_it(
    campaign, versions, hashes, tmp_path
):
    """K11: a vendored wheel whose SHA-256 is one the study's uv.lock records for that name and version from the
    pypi.org registry is a public release, so a word the study-token list holds does not refuse the build; PACKAGE.json
    lists the wheel with the lock's hash, so a reader can audit the line against the index."""
    root = copy_campaign(campaign, tmp_path, "copy").root
    version = versions[RELEASE]
    filename, data = release(version, f"tuned on {PRIVATE}\n")
    write_lock(root, (RELEASE, version, PYPI, sha(data)))
    _, files = collect(root, hashes=hashes, vendor=replacing({RELEASE: (filename, data)}))
    assert files[f"wheels/{filename}"] == data
    package = json.loads(files[PACKAGE_FILE])
    assert package["study_tokens_exempt"] == [
        {
            "file": f"wheels/{filename}",
            "name": RELEASE,
            "version": version,
            "lock_sha256": sha(data),
            "lock_source": PYPI,
        }
    ]
    [entry] = [w for w in package["wheels"] if w["name"] == RELEASE]
    assert entry["read"] == "as it is"


def test_k11_without_a_lock_beside_or_above_the_study_no_wheel_is_exempt(campaign, versions, hashes, tmp_path):
    """K11: with no uv.lock, every wheel gets the study-token list; a lock above a study that lies in no repository is
    not the study's, so it exempts nothing either."""
    root = copy_campaign(campaign, tmp_path, "copy").root
    version = versions[RELEASE]
    filename, data = release(version, f"tuned on {PRIVATE}\n")
    vendor = replacing({RELEASE: (filename, data)})
    with pytest.raises(PackageError, match=rf"wheels/{re.escape(filename)}/{NOTES}:1 "):
        collect(root, hashes=hashes, vendor=vendor)
    write_lock(tmp_path, (RELEASE, version, PYPI, sha(data)))
    with pytest.raises(PackageError, match=rf"wheels/{re.escape(filename)}/{NOTES}:1 "):
        collect(root, hashes=hashes, vendor=vendor)


@pytest.mark.parametrize("case", ["other bytes", "other version", "other registry", "direct install"])
def test_k11_a_matching_name_and_version_alone_is_no_exemption(campaign, versions, hashes, tmp_path, case):
    """K11: the lock must record the exact bytes vendored, for that name and version, from pypi.org itself: a wheel of
    the same name and version with other bytes, a hash recorded for another version or from another registry, and a
    distribution installed from a URL or a directory all keep the study-token list."""
    root = copy_campaign(campaign, tmp_path, "copy").root
    version = versions[RELEASE]
    filename, data = release(version, f"tuned on {PRIVATE}\n")
    recorded = {"name": RELEASE, "version": version, "registry": PYPI, "digest": sha(data)}
    if case == "other bytes":
        recorded["digest"] = sha(release(version, f"tuned on {PRIVATE} \n")[1])
    elif case == "other version":
        recorded["version"] = f"{version}.1"
    elif case == "other registry":
        recorded["registry"] = "https://pypi.example.org/simple"
    write_lock(root, tuple(recorded.values()))  # type: ignore[arg-type]
    vendor = replacing({RELEASE: (filename, data)}, direct=case == "direct install")
    with pytest.raises(PackageError, match=rf"wheels/{re.escape(filename)}/{NOTES}:1 "):
        collect(root, hashes=hashes, vendor=vendor)


def test_k11_the_cores_own_wheel_is_never_exempt(campaign, versions, hashes, tmp_path):
    """K11: the core's wheel gets the study-token list whatever the lock says, even a pypi.org entry with its hash."""
    root = copy_campaign(campaign, tmp_path, "copy").root
    version = versions[CORE_DISTRIBUTION]
    filename, data = wheel_of(CORE_DISTRIBUTION, version, {"expcore/notes.txt": f"tuned on {PRIVATE}\n".encode()})
    write_lock(root, (CORE_DISTRIBUTION, version, PYPI, sha(data)))
    with pytest.raises(PackageError, match=rf"wheels/{re.escape(filename)}/expcore/notes\.txt:1 "):
        collect(root, hashes=hashes, vendor=replacing({CORE_DISTRIBUTION: (filename, data)}))


def test_k11_an_exempt_release_is_still_read_with_the_anonymisation_list(
    campaign, versions, hashes, anonymisation, tmp_path
):
    """K11: the exemption is from the study-token list only: an anonymised build reads an exempt wheel's text with the
    anonymisation list, so a listed term refuses it while the private name on the line before does not."""
    root = copy_campaign(campaign, tmp_path, "copy").root
    version = versions[RELEASE]
    filename, data = release(version, f"tuned on {PRIVATE}\nby {AUTHOR}\n")
    write_lock(root, (RELEASE, version, PYPI, sha(data)))
    with pytest.raises(PackageError, match=rf"wheels/{re.escape(filename)}/{NOTES}:2: a term") as refused:
        collect(root, hashes=hashes, anonymise=anonymisation, vendor=replacing({RELEASE: (filename, data)}))
    assert f"{NOTES}:1" not in str(refused.value)
    assert AUTHOR not in str(refused.value)
    filename, data = release(version, f"tuned on {PRIVATE}\n")
    write_lock(root, (RELEASE, version, PYPI, sha(data)))
    _, files = collect(root, hashes=hashes, anonymise=anonymisation, vendor=replacing({RELEASE: (filename, data)}))
    assert [e["file"] for e in json.loads(files[PACKAGE_FILE])["study_tokens_exempt"]] == [f"wheels/{filename}"]


CODE = {"numlib/__init__.py": f"WIRE = '{PRIVATE}'\n".encode()}


def test_k11_a_published_wheel_supplied_for_an_index_install_is_exempt_where_its_repacked_files_are_not(
    site, tmp_path, monkeypatch
):
    """K11: a wheel packed again from installed files is never the published artifact's bytes, so only the published
    wheel itself, supplied with --wheels, can match the lock; for a distribution installed from a repository the same
    supplied wheel is not exempt, whatever the lock says."""
    public = installed_distribution(site / "a", "numlib", "1.4.2", CODE)
    vcs = json.dumps({"url": "https://code.example.org/numlib", "vcs_info": {"vcs": "git", "commit_id": "c" * 40}})
    cloned = installed_distribution(site / "b", "numlib", "1.4.2", CODE, direct_url=vcs)
    supplied = tmp_path / "supplied"
    supplied.mkdir()
    filename, data = wheel_of("numlib", "1.4.2", CODE)
    (supplied / filename).write_bytes(data)
    lock = {("numlib", "1.4.2"): frozenset({sha(data)})}
    monkeypatch.setattr(wheels_module, "installed", lambda: {"numlib": public})
    [repacked] = vendor_wheels({"numlib": "1.4.2"})
    [given] = vendor_wheels({"numlib": "1.4.2"}, supplied)
    assert repacked.data != data
    assert given.data == data
    assert token_exempt([repacked], lock) == {}
    assert token_exempt([given], lock) == {given.path: sha(data)}
    monkeypatch.setattr(wheels_module, "installed", lambda: {"numlib": cloned})
    [from_vcs] = vendor_wheels({"numlib": "1.4.2"}, supplied)
    assert from_vcs.data == data
    assert from_vcs.direct
    assert token_exempt([from_vcs], lock) == {}


@pytest.fixture
def site(tmp_path):
    return tmp_path / "site"


def test_k11_the_lock_is_the_studys_own_or_its_repositorys_and_only_pypi_org_entries_count(make_repo, tmp_path):
    """K11: the lock is looked for in the study directory, then up to the top of its repository and no further; only
    a package whose source is the pypi.org registry contributes its wheels' hashes, the sdist's never."""
    repo = make_repo("repo")
    study = repo.path / "studies" / "one"
    study.mkdir(parents=True)
    assert find_lock(study) is None
    write_lock(tmp_path, ("numlib", "1.0", PYPI, "1" * 64))  # above the repository: not the study's
    assert find_lock(study) is None
    top = write_lock(
        repo.path, ("numlib", "1.0", PYPI, "2" * 64), ("toolkit", "3.1", "https://pypi.example.org/simple", "3" * 64)
    )
    assert find_lock(study) == top
    assert pypi_hashes(top) == {("numlib", "1.0"): frozenset({"2" * 64})}
    own = write_lock(study, ("numlib", "1.0", PYPI, "4" * 64))
    assert find_lock(study) == own
    outside = tmp_path / "snapshot"
    outside.mkdir()
    assert find_lock(outside) is None


def test_k11_a_lock_that_cannot_be_read_exempts_nothing_and_refuses_nothing_on_its_own(
    campaign, versions, hashes, tmp_path
):
    """K11: a uv.lock that cannot be parsed (an unrelated or broken one) does not refuse a build that needs no
    exemption: one warning names the file and every wheel gets both lists, so a private name still refuses."""
    root = copy_campaign(campaign, tmp_path, "copy").root
    (root / LOCK_FILE).write_text("version = [\n", encoding="utf-8")
    said: list[str] = []
    _, files = collect(root, hashes=hashes, vendor=stand_in_vendor, warn=said.append)
    assert json.loads(files[PACKAGE_FILE])["study_tokens_exempt"] == []
    [warning] = [line for line in said if LOCK_FILE in line]
    assert warning.endswith(
        f"{LOCK_FILE}: cannot be read (TOMLDecodeError), so no wheel is exempt from the study-token list"
    )
    filename, data = release(versions[RELEASE], f"tuned on {PRIVATE}\n")
    with pytest.raises(PackageError, match=rf"wheels/{re.escape(filename)}/{NOTES}:1 "):
        collect(root, hashes=hashes, vendor=replacing({RELEASE: (filename, data)}), warn=said.append)
