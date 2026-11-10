"""The wheels a package vendors: installed files packed again, exactly and deterministically, or the author's own."""

import io
import json
import shutil
import subprocess
import sys
import zipfile
from importlib import metadata
from pathlib import Path

import pytest
from package_support import installed_distribution, stand_in, wheel_of

from expcore.package import wheels as wheels_module
from expcore.package.spec import PackageError
from expcore.package.wheels import (
    MAKEFILE,
    REQUIREMENTS,
    Wheel,
    compare_supplied,
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
    checkout = tmp_path / "checkout"
    for rel, data in CODE.items():
        (checkout / rel).parent.mkdir(parents=True, exist_ok=True)
        (checkout / rel).write_bytes(data)
    dist = installed_distribution(site, "toy-platform", "2.0.0", {}, direct_url=editable, checkout=checkout)
    monkeypatch.setattr(wheels_module, "installed", lambda: {"toy-platform": dist})
    with pytest.raises(PackageError, match=r"installed editable.*--wheels DIR"):
        vendor_wheels({"toy-platform": "2.0.0"})
    supplied = tmp_path / "supplied"
    supplied.mkdir()
    name, data = wheel_of("toy-platform", "2.0.0", CODE)
    (supplied / name).write_bytes(data)
    [wheel] = vendor_wheels({"toy-platform": "2.0.0"}, supplied)
    assert (wheel.filename, wheel.data, wheel.from_index) == (name, data, False)
    (supplied / name).write_bytes(stand_in("toy-platform", "9.9.9")[1])
    with pytest.raises(PackageError, match=r"does not name toy-platform 2\.0\.0"):
        supplied_wheel(supplied, "toy-platform", "2.0.0")


def test_k10_a_supplied_wheel_of_older_code_under_the_same_version_is_refused_naming_the_member(
    site, tmp_path, monkeypatch
):
    """K10: a supplied wheel is held to what is installed, member by member against the RECORD hashes, so a wheel of
    older code that carries the same version is refused, naming the member that differs; so is one that lacks a file
    or holds one that is not installed. The wheel of exactly what is installed is taken byte for byte."""
    dist = installed_distribution(site, "toy-platform", "2.0.0", CODE)
    monkeypatch.setattr(wheels_module, "installed", lambda: {"toy-platform": dist})
    supplied = tmp_path / "supplied"
    supplied.mkdir()
    cases = [
        (
            {**CODE, "toy_platform/__init__.py": b"WIRE = 'grey'\n"},
            r"toy_platform/__init__\.py differs from the installed",
        ),
        ({"toy_platform/__init__.py": CODE["toy_platform/__init__.py"]}, r"lacks toy_platform/data/table\.txt"),
        ({**CODE, "toy_platform/extra.py": b"X = 1\n"}, r"toy_platform/extra\.py is in the wheel but not installed"),
    ]
    for files, refusal in cases:
        name, data = wheel_of("toy-platform", "2.0.0", files)
        (supplied / name).write_bytes(data)
        with pytest.raises(PackageError, match=refusal):
            vendor_wheels({"toy-platform": "2.0.0"}, supplied)
    name, data = wheel_of("toy-platform", "2.0.0", CODE)
    (supplied / name).write_bytes(data)
    [wheel] = vendor_wheels({"toy-platform": "2.0.0"}, supplied)
    assert (wheel.data, wheel.uncompared) == (data, ())


def test_k10_an_installed_file_edited_after_install_refuses_the_original_wheel_though_record_still_matches(
    site, tmp_path, monkeypatch
):
    """K10: RECORD only says which installed files to compare; the bytes compared are the installed file's, so a file
    edited by hand after the install, its RECORD line left as it was, makes the wheel it was installed from refused."""
    dist = installed_distribution(site, "toy-platform", "2.0.0", CODE)
    (site / "toy_platform" / "__init__.py").write_bytes(b"WIRE = 'patched by hand'\n")
    monkeypatch.setattr(wheels_module, "installed", lambda: {"toy-platform": dist})
    supplied = tmp_path / "supplied"
    supplied.mkdir()
    name, data = wheel_of("toy-platform", "2.0.0", CODE)
    (supplied / name).write_bytes(data)
    with pytest.raises(PackageError, match=r"toy_platform/__init__\.py differs from the installed one"):
        vendor_wheels({"toy-platform": "2.0.0"}, supplied)


def test_k10_a_supplied_wheel_for_an_editable_install_is_held_to_its_checkout(site, tmp_path, monkeypatch):
    """K10: for an editable install each member is compared with the checkout's file, a Python file of the checkout
    the wheel lacks refuses it, and a member with nothing to compare against (data the build maps in) goes in,
    listed."""
    checkout = tmp_path / "checkout"
    for rel, data in CODE.items():
        (checkout / rel).parent.mkdir(parents=True, exist_ok=True)
        (checkout / rel).write_bytes(data)
    editable = json.dumps({"url": "file:///somewhere/toy", "dir_info": {"editable": True}})
    dist = installed_distribution(site, "toy-platform", "2.0.0", {}, direct_url=editable, checkout=checkout)
    monkeypatch.setattr(wheels_module, "installed", lambda: {"toy-platform": dist})
    supplied = tmp_path / "supplied"
    supplied.mkdir()
    older = {**CODE, "toy_platform/__init__.py": b"WIRE = 'grey'\n"}
    for files, refusal in (
        (older, r"toy_platform/__init__\.py differs"),
        ({"toy_platform/data/table.txt": CODE["toy_platform/data/table.txt"]}, r"lacks toy_platform/__init__\.py"),
    ):
        name, data = wheel_of("toy-platform", "2.0.0", files)
        (supplied / name).write_bytes(data)
        with pytest.raises(PackageError, match=refusal):
            vendor_wheels({"toy-platform": "2.0.0"}, supplied)
    name, data = wheel_of("toy-platform", "2.0.0", {**CODE, "toy_platform/examples/notes.txt": b"mapped in\n"})
    (supplied / name).write_bytes(data)
    [wheel] = vendor_wheels({"toy-platform": "2.0.0"}, supplied)
    assert wheel.uncompared == ("toy_platform/examples/notes.txt",)


def test_k10_the_cores_own_wheel_built_from_this_checkout_matches_its_editable_install():
    """K10: a wheel laid out as the core's build lays it out (its packages, the example mapped in, the public hash
    file) passes the comparison with this editable install; the example's files are the ones listed as uncompared."""
    root = Path(__file__).resolve().parents[2]
    files = {}
    for package in ("expcore", "expcore_onedata"):
        for path in sorted((root / "src" / package).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                files[path.relative_to(root / "src").as_posix()] = path.read_bytes()
    for path in sorted((root / "examples" / "tiny_study").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and "out" not in path.relative_to(root).parts:
            files[f"expcore/examples/tiny_study/{path.relative_to(root / 'examples' / 'tiny_study').as_posix()}"] = (
                path.read_bytes()
            )
    files["expcore/provenance/study_tokens.sha256"] = (root / "tests" / "data" / "study_tokens.sha256").read_bytes()
    name, data = wheel_of("papers-experiments-core", "0.1.0", files)
    uncompared = compare_supplied(metadata.distribution("papers-experiments-core"), True, name, data)
    assert uncompared
    assert all(member.startswith("expcore/examples/tiny_study/") for member in uncompared)


def test_k10_a_wheel_says_whether_it_came_from_an_index_which_decides_how_deep_it_is_read(site, monkeypatch):
    """K10: a wheel packed from an index install (no direct_url.json) is a release, read as it is; anything else is
    the author's own code, read with every view the checks have."""
    public = installed_distribution(site, "numlib", "1.4.2", {"numlib.py": b"X = 1\n"})
    vcs = json.dumps({"url": "https://code.example.org/toy", "vcs_info": {"vcs": "git", "commit_id": "c" * 40}})
    own = installed_distribution(site, "toy-platform", "2.0.0", CODE, direct_url=vcs)
    monkeypatch.setattr(wheels_module, "installed", lambda: {"numlib": public, "toy-platform": own})
    found = {w.name: w.from_index for w in vendor_wheels({"numlib": "1.4.2", "toy-platform": "2.0.0"})}
    assert found == {"numlib": True, "toy-platform": False}
    with pytest.raises(PackageError, match="not installed here"):
        vendor_wheels({"numlib": "1.5.0"})


def test_k10_the_make_environment_target_installs_exactly_the_vendored_wheels_offline(site, tmp_path):
    """K10: `make check` first makes a fresh environment beside the package and installs into it, from wheels/ alone
    (no index, hashes required), each wheel requirements.txt pins; an index the reader's pip is configured with is
    never asked."""
    name, data = repack(installed_distribution(site, "toy-platform", "2.0.0", CODE))
    wheel = Wheel("toy-platform", "2.0.0", name, data, from_index=False)
    package = tmp_path / "pkg"
    (package / "wheels").mkdir(parents=True)
    (package / wheel.path).write_bytes(wheel.data)
    (package / REQUIREMENTS).write_bytes(requirements([wheel]))
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    (package / "Makefile").write_bytes(makefile({"python": python, "distributions": {}}))
    for option in ("--no-index", "--no-deps", "--require-hashes", "--isolated", "PIP_CONFIG_FILE=/dev/null"):
        assert option in MAKEFILE
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


@pytest.mark.parametrize("channel", ["PIP_REQUIREMENT", "user configuration", "PIP_CONFIG_FILE"])
def test_k10_the_install_ignores_the_readers_pip_environment_and_configuration(site, tmp_path, channel):
    """K10: pip reads requirements from its environment and its configuration files, which could add a URL to fetch
    despite --no-index; the install runs isolated from both, so a requirement slipped in through either has no
    effect."""
    name, data = repack(installed_distribution(site, "toy-platform", "2.0.0", CODE))
    wheel = Wheel("toy-platform", "2.0.0", name, data, from_index=False)
    package = tmp_path / "pkg"
    (package / "wheels").mkdir(parents=True)
    (package / wheel.path).write_bytes(wheel.data)
    (package / REQUIREMENTS).write_bytes(requirements([wheel]))
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    (package / "Makefile").write_bytes(makefile({"python": python, "distributions": {}}))
    slipped = tmp_path / "slipped.txt"
    slipped.write_text("absent-distribution @ http://127.0.0.1:9/absent-1.0-py3-none-any.whl\n", encoding="utf-8")
    config = f"[global]\nrequirement = {slipped}\n[install]\nrequirement = {slipped}\n"
    environ = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")}
    if channel == "PIP_REQUIREMENT":
        environ["PIP_REQUIREMENT"] = str(slipped)
    elif channel == "user configuration":
        (tmp_path / "home" / ".config" / "pip").mkdir(parents=True)
        (tmp_path / "home" / ".config" / "pip" / "pip.conf").write_text(config, encoding="utf-8")
    else:
        (tmp_path / "pip.conf").write_text(config, encoding="utf-8")
        environ["PIP_CONFIG_FILE"] = str(tmp_path / "pip.conf")
    venv = tmp_path / "fresh"
    done = subprocess.run(
        ["make", "-C", str(package), "-f", "Makefile", "environment", f"PYTHON={sys.executable}", f"VENV={venv}"],
        capture_output=True,
        text=True,
        env=environ,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "absent" not in done.stdout + done.stderr
    shutil.rmtree(venv, ignore_errors=True)
