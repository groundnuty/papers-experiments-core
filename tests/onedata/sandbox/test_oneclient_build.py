"""Which oneclient served a Compose mount run (O65, `oneclient_build`): the version words and the GNU build id read in
the running sidecar, with the image's pin, and nothing for a run without a mount. The ELF files are invented
(fake_elf), the host is the in-memory one, and the shell script that reads the window of a file is run for real."""

import json
import shutil
import struct
import subprocess

import pytest
from fake_elf import elf
from fake_host import ONECLIENT_BUILD_ID
from sandbox_support import ONECLIENT_IMAGE_ID, make_spec, start_options

from expcore_onedata.sandbox import oneclient_build as OB
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.containers import ONECLIENT_BIN
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import TOKEN_FILE, DockerSandbox

needs_tools = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("sh", "tail", "head", "od")), reason="needs sh, tail, head and od"
)
BUILD_ID = bytes(range(20))
BINARY = elf(interp="/opt/invented/lib/ld.so", needed=("libc.so.6",), build_id=BUILD_ID)


def reader(data: bytes):
    """A reader of `data` by (offset, size), which is what the sidecar's script gives."""
    return lambda offset, size: data[offset : offset + size]


def recorded(world) -> list[tuple]:
    """Every docker command the host is given from here on, by its arguments."""
    given, original = [], world.host.docker

    def docker(*args, **kwargs):
        given.append(args)
        return original(*args, **kwargs)

    world.host.docker = docker
    return given


def start(world, level="mounted"):
    return DockerSandbox.start(world.host, world.s, make_spec(world, level), **start_options(world))


def probes_of(given: list[tuple]) -> list[tuple]:
    return [args for args in given if args[0] == "exec" and ONECLIENT_BIN in args]


# ---------------------------------------------------------------------------------------------------- the ELF reader


def test_o65_the_build_id_is_read_through_any_reader_as_it_is_from_a_file(tmp_path):
    """O65: the Apptainer backend reads the file on the node, the Docker one a window of the file in the sidecar; both
    run the one parser, so the same binary gives the same hex either way."""
    path = tmp_path / "oneclient"
    path.write_bytes(BINARY)
    assert OB.build_id_from(reader(BINARY)) == BUILD_ID.hex() == OB.build_id(path)


@pytest.mark.parametrize(
    "data",
    [b"", BINARY[:40], BINARY[:64], BINARY[: 64 + 56 * 3 - 1], BINARY[:-1], elf(), b"#!/bin/sh\n"],
    ids=["empty", "header cut", "no table", "table cut", "note cut", "no note", "no ELF"],
)
def test_o65_a_binary_that_shows_no_whole_note_has_no_build_id(data):
    """O65: a note cut short is not read as a shorter id (the Docker reader hands what the script printed, which a cut
    leaves short), and a file without a note has none."""
    assert OB.build_id_from(reader(data)) is None


def test_o65_a_reader_that_fails_gives_no_build_id():
    def failing(offset: int, size: int) -> bytes:
        raise OSError("the window could not be read")

    assert OB.build_id_from(failing) is None


def test_o65_a_header_that_claims_a_huge_table_is_not_read():
    """O65: the program-header table and a note segment are asked for only up to a bound, whatever the header says."""
    huge = bytearray(BINARY)
    struct.pack_into("<HH", huge, 54, 65535, 65535)
    asked: list[int] = []

    def counting(offset: int, size: int) -> bytes:
        asked.append(size)
        return bytes(huge[offset : offset + size])

    assert OB.build_id_from(counting) is None
    assert max(asked) <= OB.MAX_TABLE_BYTES


def test_o65_a_note_segment_claiming_more_than_a_bound_is_not_read():
    big = bytearray(BINARY)
    for n in range(struct.unpack_from("<H", big, 56)[0]):
        at = struct.unpack_from("<Q", big, 32)[0] + 56 * n
        if struct.unpack_from("<I", big, at)[0] == 4:  # PT_NOTE
            struct.pack_into("<Q", big, at + 32, OB.MAX_NOTES_BYTES + 1)
    asked: list[int] = []

    def counting(offset: int, size: int) -> bytes:
        asked.append(size)
        return bytes(big[offset : offset + size])

    assert OB.build_id_from(counting) is None
    assert max(asked) <= OB.MAX_TABLE_BYTES


# ---------------------------------------------------------------------------------------- the script in the sidecar


def run_slice(path, offset: int, size: int) -> CmdResult:
    argv = ["sh", "-c", OB.SLICE, OB.SLICE_NAME, str(offset), str(size), str(path)]
    done = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)
    return CmdResult(done.returncode, done.stdout, done.stderr)


@needs_tools
@pytest.mark.parametrize(
    "window", [(0, 64), (64, 56 * 4), (100, 1), (0, len(BINARY)), (len(BINARY) - 5, 100), (len(BINARY), 8)]
)
def test_o65_the_script_prints_a_window_of_the_file_as_hex_and_reads_no_more(tmp_path, window):
    """O65: `tail -c +N | head -c M | od` seeks, so a binary of any size is never read whole, and a window that runs
    past the end is short, not an error."""
    path = tmp_path / "oneclient"
    path.write_bytes(BINARY)
    offset, size = window
    done = run_slice(path, offset, size)
    assert done.returncode == 0
    assert OB.hex_bytes(done.stdout, size) == BINARY[offset : offset + size]


@needs_tools
def test_o65_the_whole_read_of_a_real_file_through_the_script_gives_the_build_id(tmp_path):
    path = tmp_path / "oneclient"
    path.write_bytes(BINARY)

    def through_the_script(offset: int, size: int) -> bytes:
        return OB.hex_bytes(run_slice(path, offset, size).stdout, size)

    assert OB.build_id_from(through_the_script) == BUILD_ID.hex()


@needs_tools
def test_o65_a_missing_file_prints_nothing_and_is_no_build_id(tmp_path):
    done = run_slice(tmp_path / "absent", 0, 64)
    assert OB.hex_bytes(done.stdout, 64) == b""
    assert (
        OB.build_id_from(lambda offset, size: OB.hex_bytes(run_slice(tmp_path / "absent", offset, size).stdout, size))
        is None
    )


@pytest.mark.parametrize(
    "text",
    ["zz\n", " 7f 4\n", "0x7f\n", " 7F 45\n", "7f45\n", " 7f 45 4c 46\nextra words\n", "\x00"],
    ids=["not hex", "odd digits", "prefix", "capitals", "no spaces", "words", "control"],
)
def test_o65_output_that_is_not_the_scripts_hex_is_refused(text):
    with pytest.raises(ValueError, match="hex"):
        OB.hex_bytes(text, 1024)


def test_o65_more_bytes_than_asked_for_are_refused():
    with pytest.raises(ValueError, match="more"):
        OB.hex_bytes(" 7f 45 4c 46\n", 3)


# ---------------------------------------------------------------------------------------------------- the sandbox


def test_o65_a_mount_run_records_the_oneclient_the_sidecar_ran(world):
    """O65: oneclient's own `--version` words, run in the sidecar, the binary's GNU build id, read from the sidecar's
    file, and the id of the image the sidecar runs; the keys of the Apptainer record where they apply, with `image`
    where that has `bundle_sha256`, and never a path or a name."""
    given = recorded(world)
    sbx = start(world)
    try:
        record = sbx.metadata()["mount"]["oneclient_build"]
        assert record == {"version": "25.0", "fuse": "3.2", "build_id": ONECLIENT_BUILD_ID, "image": ONECLIENT_IMAGE_ID}
        assert list(record) == ["version", "fuse", "build_id", "image"]
        assert "/" not in "".join(str(value) for value in record.values())
        assert json.loads(json.dumps(sbx.metadata()))["mount"]["oneclient_build"] == record
        assert all(arguments[1] == sbx.names.sidecar for arguments in probes_of(given))
        assert probes_of(given)[0][2:] == (ONECLIENT_BIN, "--version")
        assert len(probes_of(given)) >= 3  # the version, the header, the table, a note
    finally:
        sbx.stop()


def test_o65_the_sidecar_is_read_by_exec_and_never_copied_from(world):
    """O65: a `docker cp` from a running sidecar re-mounts its binds (O05): the reads are execs, and no verb that copies
    or creates a container is used for them."""
    given = recorded(world)
    sbx = start(world)
    try:
        assert {arguments[0] for arguments in given} & {"cp", "create"} == set()
        assert all(arguments[0] == "exec" for arguments in probes_of(given))
        assert not [a for a in given if a[0] == "run" and ONECLIENT_BIN in a and "--version" in a]
    finally:
        sbx.stop()


def test_o65_the_reads_come_after_the_host_s_copy_of_the_token_is_gone(world):
    """O65: the probe's docker calls neither lengthen the token file's life on the host (O05) nor the mount's wait."""
    left, original = [], world.host.docker

    def docker(*args, **kwargs):
        if args[0] == "exec" and ONECLIENT_BIN in args:
            left.append(list(world.s.work_root.rglob(TOKEN_FILE.name)))
        return original(*args, **kwargs)

    world.host.docker = docker
    sbx = start(world)
    try:
        assert left
        assert not any(left)
        assert sbx.metadata()["mount"]["wait_s"] >= 0
    finally:
        sbx.stop()


UNREAD_VERSION = {"version": None, "build_id": ONECLIENT_BUILD_ID}
UNREAD_ID = {"version": "25.0", "fuse": "3.2", "build_id": None}


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param({"oneclient_version": CmdResult(1, "", "boom")}, UNREAD_VERSION, id="version exits 1"),
        pytest.param({"oneclient_version": CmdResult(0, "oneclient 25.0\n")}, UNREAD_VERSION, id="version names none"),
        pytest.param(
            {"oneclient_version": CmdResult(0, "Oneclient: 25.0\n")},
            {"version": "25.0", "build_id": ONECLIENT_BUILD_ID},
            id="version without fuse",
        ),
        pytest.param({"oneclient_binary": CmdResult(1, "", "boom")}, UNREAD_ID, id="window exits 1"),
        pytest.param({"oneclient_binary": CmdResult(0, "not hex\n")}, UNREAD_ID, id="window is not hex"),
        pytest.param({"oneclient_binary": b""}, UNREAD_ID, id="no file"),
        pytest.param({"oneclient_binary": elf(interp="/x")}, UNREAD_ID, id="no note"),
        pytest.param({"oneclient_binary": b"#!/bin/sh\n"}, UNREAD_ID, id="no ELF"),
    ],
)
def test_o65_what_cannot_be_read_is_recorded_as_none_and_the_start_goes_on(world, change, expected):
    """O65: like the image's config file (`unread`, O36), a reading that fails is in the record as None, never a guess
    and never a refused start: the mount is already up and the sidecar's own start line is the evidence."""
    for name, value in change.items():
        setattr(world.host, name, value)
    sbx = start(world)
    try:
        record = sbx.metadata()["mount"]["oneclient_build"]
        assert record == {**expected, "image": ONECLIENT_IMAGE_ID}
    finally:
        sbx.stop()


def test_o65_a_probe_the_host_cannot_run_is_recorded_unread_and_the_start_goes_on(world):
    docker = world.host.docker

    def failing(*args, **kwargs):
        if args[0] == "exec" and ONECLIENT_BIN in args:
            raise SandboxError("docker exec failed (125)")
        return docker(*args, **kwargs)

    world.host.docker = failing
    sbx = start(world)
    try:
        assert sbx.metadata()["mount"]["oneclient_build"] == {
            "version": None, "build_id": None, "image": ONECLIENT_IMAGE_ID,
        }  # fmt: skip
    finally:
        sbx.stop()


@pytest.mark.parametrize("level", ["via-gate", "isolated"])
def test_o65_a_run_without_a_mount_is_unchanged(world, level):
    """O65: no data path, or the REST one, has no sidecar: no `mount` in its record, no new key anywhere and no command
    for oneclient."""
    given = recorded(world)
    sbx = start(world, level)
    try:
        meta = sbx.metadata()
        assert "mount" not in meta
        assert "oneclient_build" not in json.dumps(meta, default=str)
        assert not probes_of(given)
    finally:
        sbx.stop()
