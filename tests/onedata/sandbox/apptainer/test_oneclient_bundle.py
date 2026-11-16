"""oneclient's bundle on a node (`apptainer.oneclient_bundle`): unpacked for a run from the bytes that are hashed, only
what a bundle may hold, its launcher, and the version and build id a run records. Every bundle here is invented
(fake_elf); nothing runs."""

import os
import stat
import tarfile

import pytest
from fake_elf import LOADER, bundle_members, elf, write_tar

from expcore_onedata.sandbox.api import SandboxError, StartRefused
from expcore_onedata.sandbox.apptainer import oneclient_bundle as OB
from expcore_onedata.sandbox.apptainer.settings import Pinned


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "run" / "oneclient-bundle"
    path.mkdir(parents=True, mode=0o700)
    return path


def pinned_tar(tmp_path, members=None, **options) -> Pinned:
    path = tmp_path / "files" / "oneclient-bundle.tar"
    return Pinned(path, write_tar(path, bundle_members() if members is None else members, **options))


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def link(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = tarfile.SYMTYPE, "/usr/lib64/libc.so.6"
    return info


def test_the_bundle_is_unpacked_as_private_files_and_only_the_binary_and_loader_are_executable(tmp_path, root):
    bundle = OB.unpack(pinned_tar(tmp_path), root)
    assert set(bundle.files) == {"PROVENANCE.json", "bin/oneclient", f"lib/{LOADER}", "lib/libc.so.6"}
    assert bundle.binary.read_bytes() == bundle_members()["bin/oneclient"]
    assert {mode(root / "bin"), mode(root / "lib")} == {0o700}
    assert mode(bundle.binary) == mode(root / "lib" / LOADER) == 0o500
    assert mode(root / "lib" / "libc.so.6") == mode(root / "PROVENANCE.json") == 0o400


def test_a_tar_that_is_not_the_pinned_file_is_refused_and_nothing_of_it_is_left(tmp_path, root):
    """O65: only the pinned bytes run; an unpacked copy of any other file is removed before the refusal."""
    pin = pinned_tar(tmp_path)
    with pytest.raises(StartRefused, match="pinned"):
        OB.unpack(Pinned(pin.path, "0" * 64), root)
    assert os.listdir(root) == []


def test_bytes_after_the_archive_s_end_are_hashed_too(tmp_path, root):
    """O65: the pin covers the whole file, not only what tar reads: a file with something appended is another."""
    pin = pinned_tar(tmp_path)
    with open(pin.path, "ab") as handle:
        handle.write(b"appended after the end-of-archive blocks\n")
    with pytest.raises(StartRefused, match="pinned"):
        OB.unpack(pin, root)
    assert os.listdir(root) == []


@pytest.mark.parametrize(
    "extra",
    [link("lib/libz.so.1"), link("bin/oneclient-link")],
    ids=["a link among the libraries", "a link beside the binary"],
)
def test_a_pinned_tar_that_holds_a_link_is_refused(tmp_path, root, extra):
    """O65: even pinned, a link would let the loader open a file outside the bundle."""
    with pytest.raises(StartRefused, match="no regular file"):
        OB.unpack(pinned_tar(tmp_path, extra=[extra]), root)
    assert os.listdir(root) == []


@pytest.mark.parametrize("name", ["lib/../escape.so", "lib/deeper/libx.so.1", "etc/passwd", "/abs/libx.so", "bin/sh"])
def test_a_path_the_bundle_does_not_hold_is_refused(tmp_path, root, name):
    members = {**bundle_members(), name: elf()}
    with pytest.raises(StartRefused, match="does not hold"):
        OB.unpack(pinned_tar(tmp_path, members), root)
    assert os.listdir(root) == []
    assert not (root.parent / "escape.so").exists()


def test_a_name_twice_is_refused(tmp_path, root):
    twice = tarfile.TarInfo("lib/libc.so.6")
    twice.size = 3
    with pytest.raises(StartRefused, match="twice"):
        OB.unpack(pinned_tar(tmp_path, extra=[twice]), root)


def test_a_directory_named_twice_is_refused_too(tmp_path, root):
    again = tarfile.TarInfo("lib")
    again.type = tarfile.DIRTYPE
    with pytest.raises(StartRefused, match="twice"):
        OB.unpack(pinned_tar(tmp_path, extra=[again]), root)
    assert os.listdir(root) == []


def test_a_bundle_without_the_binary_is_refused(tmp_path, root):
    members = {name: data for name, data in bundle_members().items() if name != "bin/oneclient"}
    with pytest.raises(StartRefused, match="no bin/oneclient"):
        OB.unpack(pinned_tar(tmp_path, members), root)


def test_a_file_that_cannot_be_read_is_refused(tmp_path, root):
    with pytest.raises(StartRefused, match="could not be read"):
        OB.unpack(Pinned(tmp_path / "absent.tar", "0" * 64), root)


def test_the_bundle_goes_only_into_an_empty_directory_whose_path_holds_no_colon(tmp_path, root):
    (root / "left-over").write_text("x")
    with pytest.raises(StartRefused, match="empty"):
        OB.unpack(pinned_tar(tmp_path), root)
    colon = tmp_path / "a:b"
    colon.mkdir()
    with pytest.raises(StartRefused, match="':'"):
        OB.unpack(pinned_tar(tmp_path), colon)


def test_the_launcher_inhibits_the_cache_and_every_rpath_of_the_bundle_s_files(tmp_path, root):
    """O65: the image's libraries carry an RPATH glibc searches before --library-path, and a module opened at run
    time would fall back to the node's cache: both are off for every file the bundle holds."""
    bundle = OB.unpack(pinned_tar(tmp_path), root)
    words = bundle.launcher()
    assert words[:3] == [str(root / "lib" / LOADER), "--inhibit-cache", "--inhibit-rpath"]
    expected = {str(root / name) for name in ("bin/oneclient", f"lib/{LOADER}", "lib/libc.so.6")}
    assert set(words[3].split(":")) == expected
    assert words[4:] == ["--library-path", str(root / "lib"), str(root / "bin" / "oneclient")]


def test_oneclient_s_version_and_fuse_version_are_read_from_its_own_words():
    assert OB.version_of(0, "Oneclient: 25.0\nFUSE library: 3.2\n") == {"version": "25.0", "fuse": "3.2"}
    assert OB.version_of(0, "Oneclient: 25.0\n") == {"version": "25.0"}


@pytest.mark.parametrize(("code", "out"), [(1, "Oneclient: 25.0\n"), (0, ""), (0, "oneclient 25.0\n"),
                                           (0, "Oneclient: 25.0; rm -rf /\n")])  # fmt: skip
def test_a_version_that_is_not_oneclient_s_own_line_fails_the_start(code, out):
    with pytest.raises(SandboxError, match="did not name its version"):
        OB.version_of(code, out)


def test_the_binary_s_gnu_build_id_is_read_from_its_note(tmp_path):
    path = tmp_path / "oneclient"
    path.write_bytes(elf(interp="/x/" + LOADER, build_id=bytes(range(20))))
    assert OB.build_id(path) == bytes(range(20)).hex()
    path.write_bytes(elf())
    assert OB.build_id(path) is None
    path.write_bytes(b"#!/bin/sh\n")
    assert OB.build_id(path) is None
