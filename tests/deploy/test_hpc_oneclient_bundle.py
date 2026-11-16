"""The build of the relocatable oneclient bundle (deploy/hpc-federation/oneclient_bundle.py), on invented images: the
closure resolved inside the image only, the Docker discipline of its one scratch container, and a tar whose bytes
depend on its content alone. Docker is a fake; every ELF object is invented (fake_elf)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
from fake_elf import AARCH64, LOADER, elf

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "oneclient_bundle.py"
IMAGE_ID = "sha256:" + "0123456789abcdef" * 4
OTHER_ID = "sha256:" + "fedcba9876543210" * 4
REFERENCE = "invented/oneclient@" + IMAGE_ID


@pytest.fixture(scope="module")
def ob():
    """The script as a module, loaded from its file: nothing is put on `sys.path`."""
    spec = importlib.util.spec_from_file_location("oneclient_bundle_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def image_tar(entries: dict, *, mtime: int = 1_700_000_000) -> bytes:
    """An image root as `docker cp <name>:/ -` streams it (names from '/'): bytes are files, str a link's target."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, value in entries.items():
            info = tarfile.TarInfo("/" + name)
            info.mtime = mtime
            if isinstance(value, str):
                info.type, info.linkname = tarfile.SYMTYPE, value
                tar.addfile(info)
            else:
                info.size, info.mode = len(value), 0o755
                tar.addfile(info, io.BytesIO(value))
    return raw.getvalue()


def oneclient_image(**changes) -> dict:
    """An image laid out as the published one: the package's own loader, RUNPATH, an absolute link, and the two NSS
    modules glibc opens by name in the loader's default directory, one of them needing a library of its own."""
    files = {
        "opt/oneclient/bin/oneclient": elf(
            interp=f"/opt/oneclient/lib/{LOADER}",
            needed=("liba.so.1", LOADER),
            runpath="$ORIGIN/../lib",
            build_id=b"\x5a" * 20,
        ),
        "opt/oneclient/lib/ld-2.31.so": elf(),
        f"opt/oneclient/lib/{LOADER}": "ld-2.31.so",
        "opt/oneclient/lib/liba.so.1": "/opt/oneclient/lib/real/liba.so.1.0",  # absolute: the image's, never the host's
        "opt/oneclient/lib/real/liba.so.1.0": elf(needed=("libb.so.2",), rpath="/opt/oneclient/lib"),
        "opt/oneclient/lib/libb.so.2": elf(),
        "lib": "usr/lib",
        "usr/lib/x86_64-linux-gnu/libnss_files.so.2": elf(),
        "usr/lib/x86_64-linux-gnu/libnss_dns.so.2": elf(needed=("libresolv.so.2",)),
        "usr/lib/x86_64-linux-gnu/libresolv.so.2": elf(),
    }
    files.update(changes)
    return {name: value for name, value in files.items() if value is not None}


class FakeDocker:
    """`docker` as the build calls it: one image, its root streamed by `cp`; every call recorded."""

    def __init__(self, root: bytes, *, image_id: str = IMAGE_ID, state: str = "created", cp_fails: bool = False):
        self.root, self.image_id, self.state, self.cp_fails = root, image_id, state, cp_fails
        self.calls: list[list[str]] = []

    def __call__(self, argv, stdout=None):
        self.calls.append(list(argv))
        ok = subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv[:2] == ["image", "inspect"]:
            ok.stdout = self.image_id.encode()
        elif argv[:2] == ["container", "inspect"]:
            ok.stdout = f"{self.image_id} {self.state}".encode()
        elif argv[0] == "cp":
            if self.cp_fails:
                return subprocess.CompletedProcess(argv, 1, b"", b"Error: copy failed")
            stdout.write(self.root)
        return ok

    def verbs(self) -> list[str]:
        return [call[0] if call[0] not in ("image", "container") else " ".join(call[:2]) for call in self.calls]


def opened(path: Path) -> tarfile.TarFile:
    return tarfile.open(fileobj=io.BytesIO(path.read_bytes()))


# ------------------------------------------------------------------------------------------------------- ELF
def test_an_elf_object_s_loader_needed_names_runpath_and_build_id_are_read(ob):
    data = elf(interp="/x/" + LOADER, needed=("liba.so.1", "libb.so.2"), runpath="$ORIGIN/../lib:/y",
               build_id=b"\x01\x02\x03\x04")  # fmt: skip
    found = ob.parse_elf(data)
    assert found.interp == "/x/" + LOADER
    assert found.needed == ("liba.so.1", "libb.so.2")
    assert found.runpath == ("$ORIGIN/../lib", "/y")
    assert found.build_id == "01020304"


def test_an_object_of_another_machine_or_no_elf_at_all_is_refused(ob):
    with pytest.raises(ob.BuildError, match="x86-64"):
        ob.parse_elf(elf(machine=AARCH64))
    with pytest.raises(ob.BuildError, match="not an ELF"):
        ob.parse_elf(b"#!/bin/sh\n")


# ------------------------------------------------------------------------------------------------------- closure
def closure_of(ob, entries: dict):
    with tarfile.open(fileobj=io.BytesIO(image_tar(entries))) as tar:
        return ob.closure(ob.Image(tar))


def test_the_closure_follows_runpath_origin_and_absolute_links_inside_the_image(ob):
    """O64: an absolute link is resolved from the image's root, never this host's; $ORIGIN is the needing object's."""
    built = closure_of(ob, oneclient_image())
    sources = {entry.name: entry.source for entry in built.libraries}
    assert sources["lib/liba.so.1"] == "/opt/oneclient/lib/real/liba.so.1.0"
    assert sources[f"lib/{LOADER}"] == "/opt/oneclient/lib/ld-2.31.so"  # the image's own loader, through its link
    assert built.binary.source == "/opt/oneclient/bin/oneclient"
    assert built.build_id == "5a" * 20


def test_a_library_found_through_an_rpath_is_in_the_closure(ob):
    sources = {entry.name: entry.source for entry in closure_of(ob, oneclient_image()).libraries}
    assert sources["lib/libb.so.2"] == "/opt/oneclient/lib/libb.so.2"


def test_the_modules_glibc_opens_by_name_are_added_with_their_own_closure(ob):
    """O64: ldd never lists the NSS modules a host lookup opens; without them the node's own would be opened."""
    names = {Path(entry.name).name for entry in closure_of(ob, oneclient_image()).libraries}
    assert {"libnss_files.so.2", "libnss_dns.so.2", "libresolv.so.2"} <= names


def test_a_needed_name_found_nowhere_in_the_image_fails_the_build(ob):
    with pytest.raises(ob.BuildError, match=r"libb\.so\.2.*nowhere"):
        closure_of(ob, oneclient_image(**{"opt/oneclient/lib/libb.so.2": None}))


def test_a_candidate_of_another_machine_is_skipped_for_the_next_directory(ob):
    """As the loader does: a file it cannot load is passed over, and the search goes on."""
    entries = oneclient_image(**{"opt/oneclient/lib/libb.so.2": elf(machine=AARCH64),
                                 "usr/lib/x86_64-linux-gnu/libb.so.2": elf()})  # fmt: skip
    sources = {entry.name: entry.source for entry in closure_of(ob, entries).libraries}
    assert sources["lib/libb.so.2"] == "/usr/lib/x86_64-linux-gnu/libb.so.2"


def test_the_directories_of_the_image_s_ld_so_conf_and_its_includes_are_searched(ob):
    entries = oneclient_image(**{
        "opt/oneclient/lib/libb.so.2": None,
        "etc/ld.so.conf": b"include /etc/ld.so.conf.d/*.conf\n",
        "etc/ld.so.conf.d/extra.conf": b"# a comment\n/opt/extra/lib\n",
        "opt/extra/lib/libb.so.2": elf(),
    })  # fmt: skip
    sources = {entry.name: entry.source for entry in closure_of(ob, entries).libraries}
    assert sources["lib/libb.so.2"] == "/opt/extra/lib/libb.so.2"


def test_a_binary_whose_loader_is_not_the_x86_64_loader_is_refused(ob):
    entries = oneclient_image(**{"opt/oneclient/bin/oneclient": elf(interp="/lib/ld-musl-x86_64.so.1")})
    with pytest.raises(ob.BuildError, match="loader"):
        closure_of(ob, entries)


# ------------------------------------------------------------------------------------------------------- docker
def test_the_image_is_read_only_through_one_never_started_container_removed_by_its_own_name(ob, tmp_path):
    """O64: created by id with --pull never, checked never started, copied from, then removed by its own name."""
    docker = FakeDocker(image_tar(oneclient_image()))
    ob.copy_root(docker, REFERENCE, IMAGE_ID, tmp_path / "root.tar")
    assert docker.verbs() == ["image inspect", "create", "container inspect", "cp", "rm"]
    create = docker.calls[1]
    assert create[:3] == ["create", "--pull", "never"]
    assert create[-1] == IMAGE_ID
    name = create[create.index("--name") + 1]
    assert name.startswith(ob.CONTAINER_PREFIX)
    assert docker.calls[3] == ["cp", f"{name}:/", "-"]
    assert docker.calls[4] == ["rm", "-v", name]
    assert not ({"start", "run", "exec", "pull", "rmi", "tag"} & {word for call in docker.calls for word in call})


def test_the_container_is_removed_even_when_the_copy_fails(ob, tmp_path):
    docker = FakeDocker(b"", cp_fails=True)
    with pytest.raises(ob.BuildError, match="copy"):
        ob.copy_root(docker, REFERENCE, IMAGE_ID, tmp_path / "root.tar")
    assert docker.verbs()[-1] == "rm"


def test_a_reference_that_names_another_image_is_refused_before_anything_is_created(ob, tmp_path):
    docker = FakeDocker(b"", image_id=OTHER_ID)
    with pytest.raises(ob.BuildError, match="nothing was created"):
        ob.copy_root(docker, REFERENCE, IMAGE_ID, tmp_path / "root.tar")
    assert docker.verbs() == ["image inspect"]


def test_a_container_that_is_not_a_never_started_one_of_the_image_is_refused_and_removed(ob, tmp_path):
    docker = FakeDocker(b"", state="running")
    with pytest.raises(ob.BuildError, match="never-started"):
        ob.copy_root(docker, REFERENCE, IMAGE_ID, tmp_path / "root.tar")
    assert "cp" not in docker.verbs()
    assert docker.verbs()[-1] == "rm"


def test_the_docker_command_refuses_every_verb_that_would_run_pull_or_remove_anything_else(ob):
    run = ob.docker_cli("/nonexistent/docker")
    for verb in ("start", "run", "exec", "pull", "rmi", "tag"):
        with pytest.raises(ob.BuildError, match="never run"):
            run([verb, "x"])


# ------------------------------------------------------------------------------------------------------- the tar
def test_two_builds_of_one_image_give_one_sha256_whatever_the_image_s_times(ob, tmp_path):
    """O64: the pin is the tar's SHA-256: a rebuild of the same image must not change it (no time, owner, order)."""
    first = ob.build(FakeDocker(image_tar(oneclient_image(), mtime=1)), REFERENCE, IMAGE_ID, tmp_path / "a")
    second = ob.build(FakeDocker(image_tar(oneclient_image(), mtime=2)), REFERENCE, IMAGE_ID, tmp_path / "b")
    assert first["tarball_sha256"] == second["tarball_sha256"]
    tar_a = tmp_path / "a" / first["tarball"]
    assert tar_a.read_bytes() == (tmp_path / "b" / second["tarball"]).read_bytes()
    assert hashlib.sha256(tar_a.read_bytes()).hexdigest() == first["tarball_sha256"]


def test_the_tar_holds_regular_files_and_two_directories_with_fixed_times_owners_and_modes(ob, tmp_path):
    result = ob.build(FakeDocker(image_tar(oneclient_image())), REFERENCE, IMAGE_ID, tmp_path)
    with opened(tmp_path / result["tarball"]) as tar:
        members = tar.getmembers()
    assert [m.name for m in members if m.isdir()] == ["bin", "lib"]
    assert all(m.isreg() or m.isdir() for m in members)
    assert {(m.mtime, m.uid, m.gid, m.uname, m.gname) for m in members} == {(0, 0, 0, "", "")}
    modes = {m.name: m.mode for m in members}
    assert modes["bin/oneclient"] == modes[f"lib/{LOADER}"] == 0o755
    assert modes["lib/liba.so.1"] == modes["PROVENANCE.json"] == 0o644
    assert [m.name for m in members] == sorted(m.name for m in members)


def test_the_provenance_names_the_image_id_and_every_file_s_source_and_sha256_never_a_host_path(ob, tmp_path):
    result = ob.build(FakeDocker(image_tar(oneclient_image())), REFERENCE, IMAGE_ID, tmp_path)
    with opened(tmp_path / result["tarball"]) as tar:
        inside = json.loads(tar.extractfile("PROVENANCE.json").read())
        data = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isreg()}
    assert inside["image_id"] == IMAGE_ID
    assert inside["build_id"] == "5a" * 20
    assert {f["path"] for f in inside["files"]} == set(data) - {"PROVENANCE.json"}
    assert all(hashlib.sha256(data[f["path"]]).hexdigest() == f["sha256"] for f in inside["files"])
    assert str(tmp_path) not in json.dumps(inside)
    assert "reference" not in inside
    beside = json.loads((tmp_path / f"{result['tarball']}.provenance.json").read_text())
    assert beside["reference"] == REFERENCE
    assert beside["tarball_sha256"] == result["tarball_sha256"]
    check = (tmp_path / f"{result['tarball']}.sha256").read_text()
    assert check == f"{result['tarball_sha256']}  {result['tarball']}\n"


def test_a_tar_already_in_the_output_directory_is_never_overwritten(ob, tmp_path):
    ob.build(FakeDocker(image_tar(oneclient_image())), REFERENCE, IMAGE_ID, tmp_path)
    docker = FakeDocker(image_tar(oneclient_image()))
    with pytest.raises(ob.BuildError, match="never overwritten"):
        ob.build(docker, REFERENCE, IMAGE_ID, tmp_path)
    assert docker.calls == []


# ------------------------------------------------------------------------------------------------------- self-check
def test_the_loader_s_listing_flags_a_library_outside_the_bundle_or_not_found(ob, tmp_path):
    root = tmp_path / "bundle"
    listing = "\n".join([
        "\tlinux-vdso.so.1 (0x00007ffd0000)",
        f"\t/opt/oneclient/lib/{LOADER} => {root}/lib/{LOADER} (0x00007f0001)",
        f"\tliba.so.1 => {root}/lib/liba.so.1 (0x00007f0002)",
        "\tlibz.so.1 => /usr/lib64/libz.so.1 (0x00007f0003)",
        "\tlibq.so.1 => not found",
    ])  # fmt: skip
    bad = ob.outside(listing, root)
    assert bad == ["libz.so.1 => /usr/lib64/libz.so.1 (0x00007f0003)", "libq.so.1 => not found"]


def test_the_launcher_inhibits_the_cache_and_every_file_s_rpath(ob, tmp_path):
    """O64: the image's libraries carry an RPATH that glibc searches before --library-path, and a module opened later
    would fall back to the node's cache: both are switched off for every file of the bundle."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "bin").mkdir()
    for name in ("lib/" + LOADER, "lib/liba.so.1", "bin/oneclient"):
        (tmp_path / name).write_bytes(b"")
    words = ob.launcher(tmp_path)
    assert words[:3] == [str(tmp_path / "lib" / LOADER), "--inhibit-cache", "--inhibit-rpath"]
    assert set(words[3].split(":")) == {str(tmp_path / n) for n in ("lib/" + LOADER, "lib/liba.so.1", "bin/oneclient")}
    assert words[4:] == ["--library-path", str(tmp_path / "lib")]


def test_the_self_check_refuses_an_object_initialised_from_outside_the_bundle(ob, tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "bin").mkdir()

    def run(argv, env, **_):
        if "--list" in argv:
            return subprocess.CompletedProcess(argv, 0, f"\tliba.so.1 => {tmp_path}/lib/liba.so.1 (0x1)\n", "")
        debug = f"  12:\tcalling init: {tmp_path}/lib/liba.so.1\n  12:\tcalling init: /usr/lib64/libnss_sss.so.2\n"
        return subprocess.CompletedProcess(argv, 0, "Oneclient: 25.0\n", debug)

    with pytest.raises(ob.BuildError, match="libnss_sss"):
        ob.self_check(tmp_path, run)
