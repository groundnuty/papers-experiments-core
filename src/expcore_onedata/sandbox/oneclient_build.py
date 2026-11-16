# Provenance: new
"""Which oneclient served a mount run: what a run records of it under `metadata()["mount"]["oneclient_build"]` (O65).

Both backends record the same facts about the binary: `version` and `fuse` (oneclient's own `--version` words; `fuse`
only when it prints one) and `build_id` (the GNU build-id note of the binary, since oneclient prints no build id). Each
adds the fact that fixes what it ran: the Apptainer backend's host mount `bundle_sha256`, the pin of the bundle it
unpacked (`apptainer.hostmount`), the Docker sandbox's `image`, the id of the sidecar's image. A run without a mount has
no `oneclient_build`, and no sandbox adds the key anywhere else.

**`version_of`** and **`build_id`** are the parts both share. `build_id_from` is the ELF parser: it asks a reader for
`(offset, size)` windows, so it reads a file on a node (`build_id`) or a window of a file in a container the same way,
and asks only for the header, the program-header table and the note segments, each up to a bound.

**`sidecar_build`**, at the end of the Docker sandbox's mount start, reads both in the *running* sidecar by `docker
exec`, never by `docker cp` (a copy from a running container re-mounts its binds, O05): `oneclient --version`, and the
windows of `ONECLIENT_BIN` the parser asks for, each by `SLICE` (`tail | head | od`, which seeks, so a binary of any
size is never read whole, and prints plain hex, since the host's output is text). It runs after the host's copy of the
token is gone and the mount is up, so it neither lengthens the first nor the mount's wait. Its `image` is the sidecar
image's id, which the start has already shown to be the pinned one (O13). A reading that fails is None in the record
(`version`, `build_id`), logged without anything the tool said, and never refuses the start: the mount is already up,
as with the image's config file (`unread`, O36). Nothing the exec prints is recorded but what `version_of` and
`hex_bytes` accept.
"""

from __future__ import annotations

import logging
import re
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.containers import ONECLIENT_BIN
from expcore_onedata.sandbox.hostops import HostOps, describe_error, describe_stderr

log = logging.getLogger(__name__)

ReadAt = Callable[
    [int, int], bytes
]  # (offset, size) -> the bytes there; short at the end of the file; OSError if unreadable

VERSION = re.compile(r"Oneclient: ([0-9A-Za-z.+~_-]{1,64})")
FUSE = re.compile(r"FUSE library: ([0-9A-Za-z.+~_-]{1,64})")
ELF64_LE = b"\x7fELF\x02\x01"
ELF_HEADER_BYTES = 64
PROGRAM_HEADER_BYTES = 56  # one Elf64_Phdr
PT_NOTE = 4
MAX_TABLE_BYTES = 1 << 16  # the program-header table is asked for up to this
MAX_NOTES_BYTES = 1 << 20  # and a note segment up to this
MAX_OFFSET = 1 << 40  # a file offset the sidecar's script is asked for
PROBE_TIMEOUT_S = 30.0  # of each docker call that reads the sidecar
VERSION_CAP_BYTES = 4096
# The window of a file in the sidecar, as hex: `sh -c SLICE SLICE_NAME <offset> <size> <path>`. `tail -c +N` seeks to a
# byte (N counts from 1) and `head -c` ends the read, so only the window is read whatever the file's size; a missing
# file prints nothing. coreutils' own tools, which the sidecar's entrypoint already uses (`sha256sum`, `cut`, `head`).
SLICE = 'tail -c "+$(($1 + 1))" -- "$3" | head -c "$2" | od -An -v -tx1'
SLICE_NAME = "oneclient-read"
HEX_BYTE = re.compile(r"[0-9a-f]{2}")


def version_of(returncode: int, stdout: str) -> dict[str, str]:
    """oneclient's own words for its version, from `--version`; `SandboxError` when they are not there."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    found = VERSION.fullmatch(lines[0]) if returncode == 0 and lines else None
    if found is None:
        raise SandboxError(f"oneclient's --version did not name its version (exit {returncode})")
    fuse = next((m.group(1) for m in map(FUSE.fullmatch, lines[1:]) if m), None)
    return {"version": found.group(1), **({"fuse": fuse} if fuse is not None else {})}


def build_id(path: Path) -> str | None:
    """The GNU build id of a 64-bit little-endian ELF file, from its notes; None when it has none or is no such file."""
    try:
        with open(path, "rb") as handle:

            def read_at(offset: int, size: int) -> bytes:
                handle.seek(offset)
                return handle.read(size)

            return build_id_from(read_at)
    except OSError:
        return None


def build_id_from(read_at: ReadAt) -> str | None:
    """The GNU build id of a 64-bit little-endian ELF file read through `read_at`, as hex; None when the file has none,
    is no such file, is cut short where the id would be, or cannot be read. Only the header, the program-header table
    (at most `MAX_TABLE_BYTES`) and each note segment (at most `MAX_NOTES_BYTES`) are asked for, and a window that
    comes back shorter than asked is no window: a note cut short is never read as a shorter id."""
    try:
        head = read_at(0, ELF_HEADER_BYTES)
        if len(head) != ELF_HEADER_BYTES or head[:6] != ELF64_LE:
            return None
        phoff = struct.unpack_from("<Q", head, 32)[0]
        phentsize, phnum = struct.unpack_from("<HH", head, 54)
        if phentsize < PROGRAM_HEADER_BYTES or phentsize * phnum > MAX_TABLE_BYTES:
            return None
        table = read_at(phoff, phentsize * phnum)
        if len(table) != phentsize * phnum:
            return None
        for n in range(phnum):
            kind, _, offset, _, _, filesz, _, align = struct.unpack_from("<IIQQQQQQ", table, n * phentsize)
            if kind == PT_NOTE and filesz <= MAX_NOTES_BYTES:
                notes = read_at(offset, filesz)
                found = _note(notes, 8 if align == 8 else 4) if len(notes) == filesz else None
                if found is not None:
                    return found
    except (OSError, struct.error):
        return None
    return None


def _note(notes: bytes, align: int) -> str | None:
    at = 0
    while at + 12 <= len(notes):
        namesz, descsz, kind = struct.unpack_from("<III", notes, at)
        name_at = at + 12
        desc_at = name_at + (namesz + align - 1) // align * align
        if kind == 3 and notes[name_at : name_at + namesz] == b"GNU\0":  # NT_GNU_BUILD_ID
            return notes[desc_at : desc_at + descsz].hex() or None
        at = desc_at + (descsz + align - 1) // align * align
    return None


def hex_bytes(text: str, limit: int) -> bytes:
    """The bytes `SLICE` printed (`od -An -v -tx1`: two lowercase hex digits each, blanks between): `ValueError` for any
    other word, and for more than `limit` bytes."""
    words = text.split()
    if not all(HEX_BYTE.fullmatch(word) for word in words):
        raise ValueError("the window is not the hex of bytes")
    if len(words) > limit:
        raise ValueError("the window holds more bytes than were asked for")
    return bytes.fromhex("".join(words))


def version_args(container: str) -> list[str]:
    """`docker` arguments that run oneclient's `--version` in the running sidecar."""
    return ["exec", container, ONECLIENT_BIN, "--version"]


def slice_args(container: str, offset: int, size: int) -> list[str]:
    """`docker` arguments that print `size` bytes of the sidecar's binary from `offset`, as hex (`SLICE`)."""
    return ["exec", container, "sh", "-c", SLICE, SLICE_NAME, str(offset), str(size), ONECLIENT_BIN]


def _window(host: HostOps, container: str, offset: int, size: int) -> bytes:
    """One window of the sidecar's binary; `OSError` when it cannot be had (the reason is logged, never the text)."""
    if not 0 <= offset < MAX_OFFSET or not 0 <= size <= MAX_NOTES_BYTES:
        raise OSError("a window outside the bounds")
    try:
        found = host.docker(
            *slice_args(container, offset, size), check=False, timeout=PROBE_TIMEOUT_S, output_cap=4 * size + 64
        )
    except SandboxError as error:
        log.error("the sidecar's oneclient binary could not be read: %s", describe_error(error))
        raise OSError("docker could not run the read") from None
    if found.returncode != 0 or (found.stdout_count is not None and found.stdout_count.cut):
        log.error(
            "the sidecar's oneclient binary could not be read (exit %s, %s)",
            found.returncode,
            describe_stderr(found.stderr),
        )
        raise OSError("the read failed")
    try:
        return hex_bytes(found.stdout, size)
    except ValueError as error:
        log.error("the sidecar's oneclient binary was read as something else than hex: %s", error)
        raise OSError("the read is not hex") from None


def _version(host: HostOps, container: str) -> dict[str, str]:
    try:
        found = host.docker(
            *version_args(container), check=False, timeout=PROBE_TIMEOUT_S, output_cap=VERSION_CAP_BYTES
        )
        return version_of(found.returncode, found.stdout)
    except SandboxError as error:  # `version_of`'s words or the host's; neither names a configured value
        log.error("oneclient's --version could not be read in the sidecar: %s", describe_error(error))
        return {}


def sidecar_build(host: HostOps, container: str, image_id: str) -> dict[str, Any]:
    """The `oneclient_build` of a mount run on the Docker sandbox (the module's docstring): `version`, `fuse` when
    oneclient prints it, `build_id`, `image`. `version` and `build_id` are None when they could not be read; nothing
    here raises for a failed reading."""
    return {
        "version": None,
        **_version(host, container),
        "build_id": build_id_from(lambda offset, size: _window(host, container, offset, size)),
        "image": image_id,
    }


__all__ = ["build_id", "build_id_from", "hex_bytes", "sidecar_build", "version_of"]
