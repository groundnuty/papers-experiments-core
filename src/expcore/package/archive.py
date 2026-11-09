# Provenance: new
"""The checksum manifest and the deterministic archive of a replication package.

- `SHA256SUMS` lists every file of the package but itself, one `<sha256>  <path>` line each, sorted by path: the format
  `sha256sum -c` reads, and `verify_sums` checks it without any tool.
- The archive is a gzip-compressed tar whose members are the files under one top directory, sorted by name, each with
  modification time 0, owner and group 0 with no names, and mode 0644; the gzip header carries no time and no file name.
  It is written to a temporary file beside the target and renamed into place after the stream is closed, so a reader
  never sees half an archive. Two builds of the same files are byte-identical (K06).
"""

import gzip
import hashlib
import io
import os
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path

SUMS_FILE = "SHA256SUMS"
MEMBER_MODE = 0o644
MEMBER_MTIME = 0
COMPRESS_LEVEL = 9


def render_sums(files: Mapping[str, bytes]) -> bytes:
    """The manifest of the files, sorted by path, in the format `sha256sum -c` reads."""
    lines = [f"{hashlib.sha256(files[path]).hexdigest()}  {path}\n" for path in sorted(files) if path != SUMS_FILE]
    return "".join(lines).encode("utf-8")


def parse_sums(text: str) -> dict[str, str]:
    """Read a manifest into {path: sha256}; raise ValueError naming the first line that is not `<sha256>  <path>`."""
    entries: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        digest, separator, path = line.partition("  ")
        if not separator or len(digest) != 64 or set(digest) - set("0123456789abcdef") or not path or path in entries:
            raise ValueError(f"{SUMS_FILE}:{number}: expected '<sha256>  <path>', each path once")
        entries[path] = digest
    return entries


def verify_sums(root: Path, ignored_parts: frozenset[str] = frozenset({"__pycache__"})) -> list[str]:
    """Check every file below the root against its manifest; return one problem per file, empty when all match.

    A listed file that is missing or whose bytes differ is a problem, and so is a file the manifest does not list
    (caches named in `ignored_parts` aside): the package holds exactly what its manifest says.
    """
    root = Path(root)
    try:
        listed = parse_sums((root / SUMS_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"{SUMS_FILE}: missing, so no file can be verified"]
    except ValueError as error:
        return [str(error)]
    if not listed:
        return [f"{SUMS_FILE}: lists no file"]
    present = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if (p.is_file() or p.is_symlink()) and not ignored_parts.intersection(p.relative_to(root).parts)
    }
    problems = []
    for path, digest in sorted(listed.items()):
        target = root / path
        if path not in present:
            problems.append(f"{path}: listed in {SUMS_FILE} but missing")
        elif target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            problems.append(f"{path}: its bytes differ from {SUMS_FILE}")
    problems += [f"{path}: not listed in {SUMS_FILE}" for path in sorted(present - set(listed) - {SUMS_FILE})]
    return problems


def _tar_bytes(top: str, files: Mapping[str, bytes]) -> bytes:
    """The uncompressed tar of the files under one top directory, members sorted and stripped of time and owner."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(files):
            info = tarfile.TarInfo(f"{top}/{path}")
            info.size, info.mtime, info.mode = len(files[path]), MEMBER_MTIME, MEMBER_MODE
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(files[path]))
    return buffer.getvalue()


def write_archive(target: Path, top: str, files: Mapping[str, bytes]) -> Path:
    """Write the deterministic archive of the files to `target` by rename after the stream closes; return it."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=COMPRESS_LEVEL, mtime=0) as packed:
                packed.write(_tar_bytes(top, files))
            raw.flush()
            os.fsync(raw.fileno())
        os.chmod(name, MEMBER_MODE)
        os.replace(name, target)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return target
