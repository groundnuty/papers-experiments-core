#!/usr/bin/env python3
# Provenance: new
"""The relocatable oneclient bundle that the HPC host mount runs on a batch node, outside every cell
(`expcore_onedata.sandbox.apptainer.hostmount`), built from the pinned image the Compose federation's mount sidecar
runs, so that both backends run one oneclient build.

    python3 deploy/hpc-federation/oneclient_bundle.py --image onedata/oneclient:25.0 \\
        --id sha256:<64 hex digits> --out <directory> [--self-check]

1. **The image, read only through a scratch container.** The reference must name the id given (`docker image
   inspect`, which never pulls). A container of that id is created under a name of its own (`docker create --pull
   never`) and never started; its root is copied out of it as a tar stream (`docker cp <name>:/ -`); and that
   container alone is removed (`docker rm -v <name>`), whatever happens. Nothing else is started, pulled, tagged or
   removed.
2. **The closure, resolved inside the image only.** From the binary (`BINARY`), each `DT_NEEDED` name is looked up as
   the image's loader would: in the needing object's `DT_RUNPATH` (or its `DT_RPATH`, `$ORIGIN` expanded), then the
   directories of the image's `ld.so.conf`, then the loader's own (`TRUSTED`), every path resolved in the image's tree
   (an absolute link is the image's, never this host's). The modules glibc opens by name to resolve a host name
   (`DLOPENED`), which no `ldd` shows, are added with their closure, and so is the binary's `PT_INTERP`, the image's
   own loader. A name found nowhere, or a binary that is not a 64-bit x86-64 ELF object, fails the build.
3. **The bundle:** `bin/oneclient`, one regular file `lib/<name>` for each name (the loader as `lib/LOADER`), and
   `PROVENANCE.json` (the image's id, the binary's GNU build id, and each file's place in the image, size and
   SHA-256), as an uncompressed tar whose bytes depend on that content alone (sorted members, time 0, owner 0, fixed
   modes): two builds of one image give one SHA-256. Beside it, `<tar>.sha256` (as `sha256sum -c` reads it) and
   `<tar>.provenance.json` (the same, with the reference, the tar's SHA-256 and the self-check's result). That SHA-256
   is the pin `[sandbox.apptainer] oneclient_bundle_sha256` declares.

The bundle runs from wherever it is unpacked as `lib/LOADER --inhibit-cache --inhibit-rpath <every file of the bundle>
--library-path lib bin/oneclient ...` (`expcore_onedata.sandbox.apptainer.oneclient_bundle.Bundle.launcher`): the
image's libraries carry the DT_RPATH `/opt/oneclient/lib`, which glibc searches before `--library-path`, and a library
opened at run time would fall back to the node's `/etc/ld.so.cache`; with both inhibited, the loader and every library
are the image's, none the node's. `--self-check` runs that launcher here with an empty environment: `--list` must place
every library inside the unpacked bundle, `--version` must run with no object initialised from outside it
(`LD_DEBUG=files`), and its lines are recorded.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import os
import re
import secrets
import struct
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable, NamedTuple, Optional, Sequence

BINARY = "/opt/oneclient/bin/oneclient"  # the published image's binary (expcore_onedata.sandbox.containers)
LOADER = "ld-linux-x86-64.so.2"
DLOPENED = ("libnss_files.so.2", "libnss_dns.so.2")  # glibc opens these by name to resolve a host; ldd never lists them
TRUSTED = ("/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu", "/lib", "/usr/lib")
FORMAT = "expcore-oneclient-bundle/1"
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
NAME = re.compile(r"[A-Za-z0-9._+-]{1,128}\Z")  # a library's name as DT_NEEDED gives it: never a path
CONTAINER_PREFIX = "expcore-oneclient-bundle-"
FORBIDDEN_VERBS = frozenset({"start", "run", "exec", "pull", "rmi", "tag", "commit", "push", "kill", "restart"})
MAX_LINKS = 40
DOCKER_TIMEOUT_S = 1800
CHECK_TIMEOUT_S = 60
EXECUTABLE, READABLE = 0o755, 0o644

PT_LOAD, PT_DYNAMIC, PT_INTERP, PT_NOTE = 1, 2, 3, 4
DT_NULL, DT_NEEDED, DT_STRTAB, DT_RPATH, DT_RUNPATH = 0, 1, 5, 15, 29
EM_X86_64 = 62
NT_GNU_BUILD_ID = 3


class BuildError(Exception):
    """The bundle cannot be built as the module's docstring says; the message names what was wrong."""


# ---------------------------------------------------------------------------------------------------------- ELF
class Elf(NamedTuple):
    interp: str | None
    needed: tuple[str, ...]
    runpath: tuple[str, ...]
    rpath: tuple[str, ...]
    build_id: str | None


def parse_elf(data: bytes) -> Elf:
    """A 64-bit little-endian x86-64 ELF object's loader, needed names, search paths and GNU build id."""
    if data[:4] != b"\x7fELF":
        raise BuildError("not an ELF object")
    if data[4:6] != b"\x02\x01":
        raise BuildError("not a 64-bit little-endian ELF object")
    try:
        machine = struct.unpack_from("<H", data, 18)[0]
        phoff = struct.unpack_from("<Q", data, 32)[0]
        phentsize, phnum = struct.unpack_from("<HH", data, 54)
        if machine != EM_X86_64:
            raise BuildError("not an x86-64 ELF object")
        if phentsize != 56:
            raise BuildError("an ELF object whose program headers are not 56 bytes")
        segments = [struct.unpack_from("<IIQQQQQQ", data, phoff + n * phentsize) for n in range(phnum)]
        return _parse_segments(data, segments)
    except struct.error:
        raise BuildError("a truncated ELF object") from None


def _parse_segments(data: bytes, segments: list[tuple]) -> Elf:
    loads = [(s[2], s[3], s[5]) for s in segments if s[0] == PT_LOAD]  # (offset, vaddr, filesz)
    interp, build_id, dynamic = None, None, []
    for kind, _flags, offset, _vaddr, _paddr, filesz, _memsz, align in segments:
        if kind == PT_INTERP:
            interp = _cstring(data, offset)
        elif kind == PT_NOTE:
            build_id = build_id or _build_id(data[offset : offset + filesz], 8 if align == 8 else 4)
        elif kind == PT_DYNAMIC:
            dynamic = _dynamic(data[offset : offset + filesz])
    strtab = next((value for tag, value in dynamic if tag == DT_STRTAB), None)
    if strtab is None:
        return Elf(interp, (), (), (), build_id)
    base = _file_offset(loads, strtab)

    def strings(tag: int) -> list[str]:
        return [_cstring(data, base + value) for kind, value in dynamic if kind == tag]

    def paths(tag: int) -> tuple[str, ...]:
        return tuple(part for entry in strings(tag) for part in entry.split(":") if part)

    return Elf(interp, tuple(strings(DT_NEEDED)), paths(DT_RUNPATH), paths(DT_RPATH), build_id)


def _dynamic(raw: bytes) -> list[tuple[int, int]]:
    entries = []
    for start in range(0, len(raw) - 15, 16):
        tag, value = struct.unpack_from("<qQ", raw, start)
        if tag == DT_NULL:
            break
        entries.append((tag, value))
    return entries


def _file_offset(loads: list[tuple[int, int, int]], address: int) -> int:
    for offset, vaddr, filesz in loads:
        if vaddr <= address < vaddr + filesz:
            return offset + address - vaddr
    raise BuildError("an ELF object whose string table lies in no loaded segment")


def _cstring(data: bytes, offset: int) -> str:
    end = data.find(b"\0", offset)
    if offset >= len(data) or end < 0:
        raise BuildError("an ELF string runs past the end of its object")
    try:
        return data[offset:end].decode("utf-8")
    except UnicodeDecodeError:
        raise BuildError("an ELF string that is not UTF-8") from None


def _build_id(notes: bytes, align: int) -> str | None:
    at = 0
    while at + 12 <= len(notes):
        namesz, descsz, kind = struct.unpack_from("<III", notes, at)
        name_at = at + 12
        desc_at = name_at + _round(namesz, align)
        if kind == NT_GNU_BUILD_ID and notes[name_at : name_at + namesz] == b"GNU\0":
            return notes[desc_at : desc_at + descsz].hex()
        at = desc_at + _round(descsz, align)
    return None


def _round(size: int, align: int) -> int:
    return (size + align - 1) // align * align


# ---------------------------------------------------------------------------------------------------------- the image
def _normal(name: str) -> str:
    """A tar member's name as a path in the image, without its leading '/' or './' ('' is the root)."""
    return "/".join(part for part in name.split("/") if part not in ("", "."))


class Image:
    """The image's tree as `docker cp` gave it: every member by its path, read without extracting anything here."""

    def __init__(self, tar: tarfile.TarFile) -> None:
        self.tar = tar
        self.members: dict[str, tarfile.TarInfo] = {}
        for member in tar.getmembers():
            self.members[_normal(member.name)] = member

    def resolve(self, path: str) -> str | None:
        """The path with every link followed inside the image (an absolute target from the image's root, never this
        host's); None when a component is missing. A loop fails the build."""
        parts = [part for part in path.split("/") if part not in ("", ".")]
        done: list[str] = []
        hops = 0
        while parts:
            part = parts.pop(0)
            if part == "..":
                done = done[:-1]
                continue
            member = self.members.get("/".join([*done, part]))
            if member is None:
                return None
            if member.issym():
                hops += 1
                if hops > MAX_LINKS:
                    raise BuildError(f"/{path}: too many links in the image")
                if member.linkname.startswith("/"):
                    done = []
                parts = [p for p in member.linkname.split("/") if p not in ("", ".")] + parts
                continue
            done.append(part)
        return "/".join(done)

    def regular(self, path: str) -> tarfile.TarInfo | None:
        """The regular file a resolved path names (a hard link's data member), None when it is no regular file."""
        member = self.members.get(path)
        if member is not None and member.islnk():
            member = self.members.get(_normal(member.linkname))
        return member if member is not None and member.isreg() else None

    def read(self, path: str) -> bytes:
        member = self.regular(path)
        handle = None if member is None else self.tar.extractfile(member)
        if handle is None:
            raise BuildError(f"/{path} is not a regular file in the image")
        with handle:
            return handle.read()

    def search_dirs(self) -> list[str]:
        """The directories of the image's `/etc/ld.so.conf` and the files it includes, in order."""
        found: list[str] = []
        for line in self._conf_lines("etc/ld.so.conf", depth=0):
            if line not in found:
                found.append(line)
        return found

    def _conf_lines(self, path: str, depth: int) -> list[str]:
        resolved = self.resolve(path)
        if resolved is None or self.regular(resolved) is None or depth > 4:
            return []
        lines: list[str] = []
        for raw in self.read(resolved).decode("utf-8", "replace").splitlines():
            line = raw.split("#", 1)[0].strip()
            if line.startswith("include "):
                pattern = _normal(line[len("include ") :].strip())
                for name in sorted(n for n in self.members if fnmatch.fnmatchcase(n, pattern)):
                    lines.extend(self._conf_lines(name, depth + 1))
            elif line.startswith("/"):
                lines.append(line.rstrip("/"))
        return lines


# ---------------------------------------------------------------------------------------------------------- closure
class Entry(NamedTuple):
    name: str  # the file's name in the bundle, relative to its root
    source: str  # its path in the image
    size: int
    sha256: str
    mode: int


class Closure(NamedTuple):
    binary: Entry
    build_id: str | None
    libraries: tuple[Entry, ...]  # the loader first, then in the order the loader would load them


def _entry(image: Image, name: str, source: str, mode: int) -> tuple[Entry, Elf]:
    data = image.read(source)
    elf = parse_elf(data)
    return Entry(name, "/" + source, len(data), hashlib.sha256(data).hexdigest(), mode), elf


def _expand(directory: str, origin: str) -> str:
    expanded = directory.replace("${ORIGIN}", origin).replace("$ORIGIN", origin)
    if "$" in expanded:
        raise BuildError(f"a search path the build does not expand: {directory}")
    return expanded


def _lookup(image: Image, name: str, needer: str, elf: Elf, defaults: Sequence[str]) -> str:
    """Where the image's loader finds `name` for the object at `needer` (a path in the image)."""
    if not NAME.fullmatch(name):
        raise BuildError(f"/{needer} needs {name!r}, which is no plain library name")
    origin = "/" + str(PurePosixPath(needer).parent)
    own = [_expand(d, origin) for d in (elf.runpath or elf.rpath)]
    for directory in [*own, *defaults]:
        found = image.resolve(f"{directory}/{name}")
        if found is None or image.regular(found) is None:
            continue
        try:
            parse_elf(image.read(found))
        except BuildError:  # the loader skips a file it cannot load (another class or machine) and looks further
            continue
        return found
    raise BuildError(f"{name}, which /{needer} needs, is found nowhere in the image")


def closure(image: Image, binary: str = BINARY) -> Closure:
    """The binary, its loader and every library it loads, as the module's docstring says (step 2)."""
    resolved = image.resolve(binary)
    if resolved is None or image.regular(resolved) is None:
        raise BuildError(f"{binary} is not a regular file in the image")
    main, elf = _entry(image, "bin/oneclient", resolved, EXECUTABLE)
    if elf.interp is None or PurePosixPath(elf.interp).name != LOADER:
        raise BuildError(f"{binary}'s loader is not {LOADER}")
    loader = image.resolve(elf.interp)
    if loader is None or image.regular(loader) is None:
        raise BuildError(f"{binary}'s loader, {elf.interp}, is not in the image")
    defaults = [*image.search_dirs(), *TRUSTED]
    first, _ = _entry(image, f"lib/{LOADER}", loader, EXECUTABLE)
    found: dict[str, Entry] = {LOADER: first}
    # the start's loads first, breadth first as the loader goes; then what glibc opens later, with its own closure
    for phase in (elf.needed, DLOPENED):
        queue: list[tuple[str, Elf, tuple[str, ...]]] = [(resolved, elf, phase)]
        while queue:
            needer, needer_elf, names = queue.pop(0)
            for name in names:
                if name in found:
                    continue
                source = _lookup(image, name, needer, needer_elf, defaults)
                found[name], library = _entry(image, f"lib/{name}", source, READABLE)
                queue.append((source, library, library.needed))
    return Closure(main, elf.build_id, tuple(found.values()))


# ---------------------------------------------------------------------------------------------------------- the tar
def provenance(image_id: str, built: Closure) -> dict[str, object]:
    """What the bundle says of itself, inside the tar: never a path of this host, never the time of the build."""
    files = [built.binary, *built.libraries]
    return {
        "format": FORMAT,
        "image_id": image_id,
        "binary": BINARY,
        "build_id": built.build_id,
        "loader": f"lib/{LOADER}",
        "files": [{"path": e.name, "from": e.source, "size": e.size, "sha256": e.sha256} for e in files],
    }


def _info(name: str, size: int, mode: int, kind: bytes = tarfile.REGTYPE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.type = size, mode, kind
    info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
    return info


def pack(image: Image, built: Closure, image_id: str, target: Path) -> str:
    """Write the bundle's tar (step 3) and return its SHA-256."""
    record = (json.dumps(provenance(image_id, built), indent=1, sort_keys=True) + "\n").encode()
    entries = sorted([built.binary, *built.libraries], key=lambda e: e.name)
    with open(target, "xb") as raw, tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        tar.addfile(_info("PROVENANCE.json", len(record), READABLE), io.BytesIO(record))
        for directory in ("bin", "lib"):
            tar.addfile(_info(directory, 0, EXECUTABLE, tarfile.DIRTYPE))
            for entry in (e for e in entries if e.name.startswith(directory + "/")):
                data = image.read(entry.source.lstrip("/"))
                if hashlib.sha256(data).hexdigest() != entry.sha256:
                    raise BuildError(f"{entry.source} changed while the bundle was built")
                tar.addfile(_info(entry.name, len(data), entry.mode), io.BytesIO(data))
    return sha256_of(target)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------------------------------------- docker
Docker = Callable[[Sequence[str], Optional[io.BufferedWriter]], "subprocess.CompletedProcess[bytes]"]


def docker_cli(command: str = "docker") -> Docker:
    def run(argv: Sequence[str], stdout: io.BufferedWriter | None = None) -> subprocess.CompletedProcess[bytes]:
        if argv and argv[0] in FORBIDDEN_VERBS:  # the build never runs, pulls or removes anything else
            raise BuildError(f"docker {argv[0]} is never run by the bundle's build")
        sink = subprocess.PIPE if stdout is None else stdout
        return subprocess.run([command, *argv], stdout=sink, stderr=subprocess.PIPE, timeout=DOCKER_TIMEOUT_S)

    return run


def _out(done: subprocess.CompletedProcess[bytes], what: str) -> str:
    if done.returncode != 0:
        detail = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise BuildError(f"{what} failed: {detail[-1] if detail else 'exit ' + str(done.returncode)}")
    return (done.stdout or b"").decode("utf-8", "replace").strip()


def copy_root(docker: Docker, reference: str, image_id: str, target: Path) -> None:
    """The image's root as a tar stream, through a never-started scratch container (step 1)."""
    if not IMAGE_ID.fullmatch(image_id):
        raise BuildError("--id must be 'sha256:' and 64 lower-case hexadecimal digits")
    found = _out(docker(["image", "inspect", "--format", "{{.Id}}", reference], None), "the image's inspection")
    if found != image_id:
        raise BuildError(f"{reference} is the image {found}, not {image_id}: nothing was created")
    name = CONTAINER_PREFIX + secrets.token_hex(6)
    try:
        _out(docker(["create", "--pull", "never", "--name", name, image_id], None), "the scratch container's creation")
        inspect = ["container", "inspect", "--format", "{{.Image}} {{.State.Status}}", name]
        state = _out(docker(inspect, None), "its inspection")
        if state != f"{image_id} created":
            raise BuildError(f"the scratch container is not a never-started container of {image_id} ({state})")
        with open(target, "xb") as handle:
            _out(docker(["cp", f"{name}:/", "-"], handle), "the copy of its root")
    finally:
        done = docker(["rm", "-v", name], None)  # only this name, which nothing else can hold
        if done.returncode != 0 and b"No such container" not in (done.stderr or b""):
            raise BuildError(f"the scratch container {name} could not be removed: remove it by hand")


# ---------------------------------------------------------------------------------------------------------- check
LISTED = re.compile(r"(?:\S+ => )?(/\S+) \(0x[0-9a-f]+\)\Z")


def outside(listing: str, root: Path) -> list[str]:
    """The lines of the loader's `--list` that place a library outside `root`, or find none (the kernel's vDSO, a
    name with no path and no arrow, is no file)."""
    bad = []
    top = str(root.resolve())
    for raw in listing.splitlines():
        line = raw.strip()
        if not line or ("=>" not in line and not line.startswith("/")):
            continue
        match = LISTED.fullmatch(line)
        if match is None or os.path.commonpath([str(Path(match.group(1)).resolve()), top]) != top:
            bad.append(line)
    return bad


def launcher(bundle: Path) -> list[str]:
    """The words before oneclient's own arguments (the module's docstring), for a bundle unpacked at `bundle`."""
    lib = bundle / "lib"
    own = sorted(str(path) for path in [*lib.iterdir(), bundle / "bin" / "oneclient"])
    return [str(lib / LOADER), "--inhibit-cache", "--inhibit-rpath", ":".join(own), "--library-path", str(lib)]


INITIALISED = re.compile(r"calling init: (\S+)")


def self_check(bundle: Path, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> dict:
    """Run the unpacked bundle's launcher here with an empty environment (the module's docstring)."""
    words, binary = launcher(bundle), str(bundle / "bin" / "oneclient")
    common = {"capture_output": True, "text": True, "timeout": CHECK_TIMEOUT_S, "check": False}
    listed = run([*words, "--list", binary], env={}, **common)
    if listed.returncode != 0:
        raise BuildError(f"the loader's --list failed (exit {listed.returncode})")
    bad = outside(listed.stdout, bundle)
    if bad:
        raise BuildError("the launcher loads a library from outside the bundle: " + "; ".join(bad))
    version = run([*words, binary, "--version"], env={"LD_DEBUG": "files"}, **common)
    if version.returncode != 0:
        raise BuildError(f"oneclient --version failed (exit {version.returncode})")
    opened = INITIALISED.findall(version.stderr)
    foreign = [path for path in opened if outside(f"{path} (0x0)", bundle)]
    if not opened or foreign:
        raise BuildError("oneclient --version initialised an object from outside the bundle: " + "; ".join(foreign))
    return {"libraries": len(listed.stdout.splitlines()), "initialised": len(opened),
            "version": version.stdout.strip().splitlines()}  # fmt: skip


def _unpack(tar_path: Path, into: Path) -> None:
    with tarfile.open(tar_path) as tar:
        for member in tar.getmembers():
            target = into / member.name
            if member.isdir():
                target.mkdir(mode=0o755)
            else:
                handle = tar.extractfile(member)
                if handle is None:
                    raise BuildError(f"{member.name}: the bundle holds a member that is no regular file")
                target.write_bytes(handle.read())
                target.chmod(member.mode)


# ---------------------------------------------------------------------------------------------------------- main
def build(docker: Docker, reference: str, image_id: str, out: Path, check: bool = False) -> dict:
    """Steps 1 to 3 into `out`; the result's record is also written beside the tar."""
    out.mkdir(parents=True, exist_ok=True)
    stem = f"oneclient-bundle-{image_id.split(':')[1][:12]}"
    tar_path = out / f"{stem}.tar"
    if tar_path.exists():
        raise BuildError(f"{tar_path.name} is already in --out: a bundle is never overwritten")
    with tempfile.TemporaryDirectory(dir=out, prefix=".build-") as scratch:
        root = Path(scratch) / "image-root.tar"
        copy_root(docker, reference, image_id, root)
        with tarfile.open(root) as raw:
            image = Image(raw)
            built = closure(image)
            digest = pack(image, built, image_id, tar_path)
        result = {**provenance(image_id, built), "reference": reference, "tarball": tar_path.name}
        result["tarball_sha256"] = digest
        if check:
            unpacked = Path(scratch) / "bundle"
            unpacked.mkdir()
            _unpack(tar_path, unpacked)
            result["self_check"] = self_check(unpacked)
    (out / f"{stem}.tar.sha256").write_text(f"{digest}  {tar_path.name}\n", encoding="utf-8")
    (out / f"{stem}.tar.provenance.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--image", required=True, help="the image's reference, as the Compose sidecar names it")
    parser.add_argument("--id", required=True, help="the id it is pinned to: sha256:<64 hex digits>")
    parser.add_argument("--out", required=True, type=Path, help="the directory the tar and its records go to")
    parser.add_argument("--docker", default="docker", help="the docker command")
    parser.add_argument("--self-check", action="store_true", help="run the unpacked bundle's launcher here")
    args = parser.parse_args(argv)
    try:
        result = build(docker_cli(args.docker), args.image, args.id, args.out, args.self_check)
    except BuildError as error:
        print(f"oneclient_bundle: {error}", file=sys.stderr)
        return 1
    print(f"{result['tarball_sha256']}  {result['tarball']}  ({len(result['files'])} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
