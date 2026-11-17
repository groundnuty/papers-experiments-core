"""Invented ELF objects and oneclient bundles for the tests of the bundle's build and of its unpacking on a node.

`elf()` writes the smallest 64-bit little-endian ELF file the build reads: program headers for one loaded segment, an
optional `PT_INTERP`, a `PT_DYNAMIC` with `DT_NEEDED`, `DT_RUNPATH` or `DT_RPATH` names in its string table, and an
optional GNU build-id note. Nothing here runs; every name and byte is invented.
"""

from __future__ import annotations

import hashlib
import io
import json
import struct
import tarfile
from pathlib import Path

BASE = 0x400000
X86_64, AARCH64 = 62, 183
PT_LOAD, PT_DYNAMIC, PT_INTERP, PT_NOTE = 1, 2, 3, 4
DT_NULL, DT_NEEDED, DT_STRTAB, DT_RPATH, DT_RUNPATH = 0, 1, 5, 15, 29
LOADER = "ld-linux-x86-64.so.2"


def elf(
    *,
    interp: str | None = None,
    needed: tuple[str, ...] = (),
    runpath: str | None = None,
    rpath: str | None = None,
    build_id: bytes | None = None,
    machine: int = X86_64,
) -> bytes:
    """An ELF object as the module's docstring says."""
    strtab = bytearray(b"\0")

    def string(text: str) -> int:
        offset = len(strtab)
        strtab.extend(text.encode() + b"\0")
        return offset

    entries = [(DT_NEEDED, string(name)) for name in needed]
    entries += [(DT_RUNPATH, string(runpath))] if runpath is not None else []
    entries += [(DT_RPATH, string(rpath))] if rpath is not None else []
    note = b""
    if build_id is not None:
        note = struct.pack("<III", 4, len(build_id), 3) + b"GNU\0" + build_id + b"\0" * (-len(build_id) % 4)
    count = 2 + (interp is not None) + bool(note)
    data_at = 64 + 56 * count
    interp_bytes = (interp.encode() + b"\0") if interp is not None else b""
    strtab_at = data_at + len(interp_bytes)
    dynamic_at = strtab_at + len(strtab) + (-(strtab_at + len(strtab)) % 8)
    dynamic = b"".join(struct.pack("<qQ", tag, value) for tag, value in [*entries, (DT_STRTAB, BASE + strtab_at)])
    dynamic += struct.pack("<qQ", DT_NULL, 0)
    note_at = dynamic_at + len(dynamic)
    size = note_at + len(note)

    def header(kind: int, offset: int, filesz: int, align: int) -> bytes:
        return struct.pack("<IIQQQQQQ", kind, 4, offset, BASE + offset, BASE + offset, filesz, filesz, align)

    program = header(PT_LOAD, 0, size, 0x1000) + header(PT_DYNAMIC, dynamic_at, len(dynamic), 8)
    program += header(PT_INTERP, data_at, len(interp_bytes), 1) if interp is not None else b""
    program += header(PT_NOTE, note_at, len(note), 4) if note else b""
    ident = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    head = ident + struct.pack("<HHIQQQIHHHHHH", 3, machine, 1, 0, 64, 0, 0, 64, 56, count, 64, 0, 0)
    body = bytearray(head + program)
    body += interp_bytes + bytes(strtab)
    body += b"\0" * (dynamic_at - len(body))
    body += dynamic + note
    return bytes(body)


def bundle_members(build_id: bytes = b"\xab" * 20, libraries: tuple[str, ...] = ("libc.so.6",)) -> dict[str, bytes]:
    """A bundle's files as the build writes them: the binary, the loader, the libraries and the provenance record."""
    files = {
        "bin/oneclient": elf(interp=f"/opt/invented/lib/{LOADER}", needed=libraries, build_id=build_id),
        f"lib/{LOADER}": elf(),
        **{f"lib/{name}": elf() for name in libraries},
    }
    record = {"format": "expcore-oneclient-bundle/1", "build_id": build_id.hex(), "files": sorted(files)}
    return {"PROVENANCE.json": json.dumps(record).encode(), **files}


def write_tar(path: Path, members: dict[str, bytes], *, extra: list[tarfile.TarInfo] | None = None) -> str:
    """A tar of `members` (directories first, as the build writes them) and any `extra` members; its SHA-256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as tar:
        for directory in sorted({name.split("/")[0] for name in members if "/" in name}):
            info = tarfile.TarInfo(directory)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(info)
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755 if name.startswith("bin/") else 0o644
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info, io.BytesIO(b"x" * info.size) if info.isreg() else None)
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = ["AARCH64", "LOADER", "X86_64", "bundle_members", "elf", "write_tar"]
