"""The wheels a package vendors: installed files packed again, exactly and deterministically, or the author's own."""

import io
import json
import shutil
import subprocess
import sys
import zipfile
from importlib import metadata

import pytest
from package_support import installed_distribution, stand_in

from expcore.package import wheels as wheels_module
from expcore.package.spec import PackageError
from expcore.package.wheels import (
    MAKEFILE,
    REQUIREMENTS,
    Wheel,
    makefile,
    repack,
    requirements,
    supplied_wheel,
    vendor_wheels,
)

CODE = {"toy_platform/__init__.py": b"WIRE = 'blue'\n", "toy_platform/data/table.txt": b"one\ntwo\n"}


def members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


@pytest.fixture
def site(tmp_path):
    where = tmp_path / "lib" / "site-packages"
    where.mkdir(parents=True)
    return where


def test_k10_an_installed_distribution_is_packed_again_with_its_files_and_without_what_the_installer_added(site):
    """K10: the wheel holds every file RECORD lists inside the site directory, its metadata, and a RECORD written
    anew; bytecode, the installer's own records and files installed outside the site directory are left out."""
    dist = installed_distribution(site, "toy-platform", "2.0.0", CODE)
    (site / "toy_platform" / "__pycache__").mkdir()
    (site / "toy_platform" / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"compiled")
    name, data = repack(dist)
    assert name == "toy_platform-2.0.0-py3-none-any.whl"
    found = members(data)
    assert found["toy_platform/__init__.py"] == CODE["toy_platform/__init__.py"]
    assert found["toy_platform/data/table.txt"] == CODE["toy_platform/data/table.txt"]
    info = "toy_platform-2.0.0.dist-info"
    assert set(found) == {*CODE, f"{info}/METADATA", f"{info}/WHEEL", f"{info}/RECORD"}
    assert list(found)[-1] == f"{info}/RECORD"
    record = found[f"{info}/RECORD"].decode()
    assert "toy_platform/__init__.py,sha256=" in record
    assert "INSTALLER" not in record
    assert "uv_cache" not in record
    assert record.endswith(f"{info}/RECORD,,\n")


def test_k10_a_real_distribution_of_this_environment_packs_with_its_modules_and_its_own_tags():
    """K10: a distribution as a real installer laid it out packs too, every file agreeing with its RECORD."""
    name, data = repack(metadata.distribution("idna"))
    assert name.startswith("idna-")
    assert name.endswith("-py3-none-any.whl")
    found = members(data)
    assert "idna/__init__.py" in found
    assert not any(n.endswith((".pyc", "/INSTALLER", "/direct_url.json")) for n in found)


def test_k06_a_packed_wheel_is_the_same_bytes_every_time_and_wherever_it_is_installed(site, tmp_path):
    """K06: entries in order, with a fixed date and mode, so two builds of one environment give the same wheel."""
    first = repack(installed_distribution(site, "toy-platform", "2.0.0", CODE))
    elsewhere = tmp_path / "other" / "site-packages"
    again = repack(installed_distribution(elsewhere, "toy-platform", "2.0.0", CODE))
    assert first == again
    with zipfile.ZipFile(io.BytesIO(first[1])) as archive:
        assert {i.date_time for i in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}


def test_k10_an_installed_file_that_differs_from_its_record_is_refused(site):
    """K10: a file changed after it was installed is not the distribution's code, so it is not packed."""
    dist = installed_distribution(site, "toy-platform", "2.0.0", CODE)
    (site / "toy_platform" / "__init__.py").write_bytes(b"WIRE = 'red'\n")
    with pytest.raises(PackageError, match=r"toy_platform/__init__\.py differs from its RECORD"):
        repack(dist)


def test_k10_an_editable_install_is_refused_unless_the_author_supplies_its_wheel(site, tmp_path, monkeypatch):
    """K10: an editable install's files are a pointer to a checkout, so its wheel must come from `--wheels`; the
    supplied wheel is taken byte for byte, and one whose metadata names another version is refused."""
    editable = json.dumps({"url": "file:///somewhere/toy", "dir_info": {"editable": True}})
    dist = installed_distribution(site, "toy-platform", "2.0.0", CODE, direct_url=editable)
    monkeypatch.setattr(wheels_module, "installed", lambda: {"toy-platform": dist})
    with pytest.raises(PackageError, match=r"installed editable.*--wheels DIR"):
        vendor_wheels({"toy-platform": "2.0.0"})
    supplied = tmp_path / "supplied"
    supplied.mkdir()
    name, data = stand_in("toy-platform", "2.0.0")
    (supplied / name).write_bytes(data)
    [wheel] = vendor_wheels({"toy-platform": "2.0.0"}, supplied)
    assert (wheel.filename, wheel.data, wheel.scanned) == (name, data, True)
    (supplied / name).write_bytes(stand_in("toy-platform", "9.9.9")[1])
    with pytest.raises(PackageError, match=r"does not name toy-platform 2\.0\.0"):
        supplied_wheel(supplied, "toy-platform", "2.0.0")


def test_k10_a_distribution_from_an_index_is_attested_and_one_from_a_direct_url_is_scanned(site, monkeypatch):
    """K10: a public release (no direct_url.json) goes in unread, attested by hash; anything else is read."""
    public = installed_distribution(site, "numlib", "1.4.2", {"numlib.py": b"X = 1\n"})
    vcs = json.dumps({"url": "https://code.example.org/toy", "vcs_info": {"vcs": "git", "commit_id": "c" * 40}})
    own = installed_distribution(site, "toy-platform", "2.0.0", CODE, direct_url=vcs)
    monkeypatch.setattr(wheels_module, "installed", lambda: {"numlib": public, "toy-platform": own})
    found = {w.name: w.scanned for w in vendor_wheels({"numlib": "1.4.2", "toy-platform": "2.0.0"})}
    assert found == {"numlib": False, "toy-platform": True}
    with pytest.raises(PackageError, match="not installed here"):
        vendor_wheels({"numlib": "1.5.0"})


def test_k10_the_make_environment_target_installs_exactly_the_vendored_wheels_offline(site, tmp_path):
    """K10: `make check` first makes a fresh environment beside the package and installs into it, from wheels/ alone
    (no index, hashes required), each wheel requirements.txt pins; an index the reader's pip is configured with is
    never asked."""
    name, data = repack(installed_distribution(site, "toy-platform", "2.0.0", CODE))
    wheel = Wheel("toy-platform", "2.0.0", name, data, scanned=True)
    package = tmp_path / "pkg"
    (package / "wheels").mkdir(parents=True)
    (package / wheel.path).write_bytes(wheel.data)
    (package / REQUIREMENTS).write_bytes(requirements([wheel]))
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    (package / "Makefile").write_bytes(makefile({"python": python, "distributions": {}}))
    assert "--no-index" in MAKEFILE
    assert "--require-hashes" in MAKEFILE
    venv = tmp_path / "fresh"
    environ = {"PATH": "/usr/bin:/bin", "PIP_INDEX_URL": "http://127.0.0.1:9/simple", "HOME": str(tmp_path)}
    done = subprocess.run(
        ["make", "-C", str(package), "-f", "Makefile", "environment", f"PYTHON={sys.executable}", f"VENV={venv}"],
        capture_output=True,
        text=True,
        env=environ,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    shown = subprocess.run(
        [str(venv / "bin" / "python"), "-c", "import toy_platform; print(toy_platform.WIRE)"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert shown.stdout.strip() == "blue"
    shutil.rmtree(venv)
    (package / wheel.path).write_bytes(wheel.data + b"tampered")
    tampered = subprocess.run(
        ["make", "-C", str(package), "-f", "Makefile", "environment", f"PYTHON={sys.executable}", f"VENV={venv}"],
        capture_output=True,
        text=True,
        env=environ,
    )
    assert tampered.returncode != 0
    assert "hash" in (tampered.stdout + tampered.stderr).lower()
    shutil.rmtree(venv, ignore_errors=True)
