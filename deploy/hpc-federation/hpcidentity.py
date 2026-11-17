"""A federation's render identity (O74): what `up` rendered, as hashes alone, in `render-identity.json` in the root.

Two federations rendered from one template, with the same pins and the same data, are one platform, and a shard's step
must be able to tell that the federation it runs against is the one a plan expects: the same template, the same pins,
the same platform fingerprint. The record holds, as canonical JSON with sorted keys (mode 0600):

- `version`: `VERSION`, the record's own format.
- `template_sha256`: the SHA-256 of the platform template as the renderer read it, before it was rendered.
- `platform_fingerprint_sha256`: the SHA-256 of the canonical JSON of the rendered file's
  `PlatformConfig.fingerprint()`, hashed as the core hashes a fingerprint (`expcore.campaign.manifest`). It holds the
  platform's declared identity and its spaces' data identity, never an id the federation made, so two federations of
  one template and one set of data have one (C06); it holds the sandbox's pins too, which is why a pin that changed
  changes it as well.
- the SHA-256 of each pin the rendered platform holds (`PINS`): the agent's SIF, the egress shim, the egress collector
  and oneclient's bundle. They are the very values `up` filled in and checked (`hpcpins`: the site's declared hashes,
  each file measured against its own in the preflight, and the bundle's own collector hashed here), never read again.
  A pin the template does not name is not in the record: no entry is ever empty or null.
- `study_artifacts_tree_sha256` (`ARTIFACTS`): when the site declares `HPC_STUDY_ARTIFACTS`, the directory of the
  study's own artifacts (its tool server's clone, its venv, its manifests), the SHA-256 of that directory's tree as
  `tree_sha256` takes it. `up` takes it once, in its preflight on the login node before anything is submitted, and the
  core's phase, here or in a step on a node, is handed the value from the state file. A federation with no such
  directory has no such key: it is never empty or null.

It holds no address, id, job, node, path, user or secret: only hashes and the version, so it can travel with a
study's results (`shards.py` copies it into `out/study-state/`) and be committed beside a study as the record a plan
expects. `shards.py` runs alone on a node and keeps its own copy of the names below (a test compares the two).

**The tree's digest** (`tree_sha256`) is the SHA-256 of one canonical listing: the bytes `TREE_VERSION` and a NUL, then
one record for every entry of the tree, sorted by the entry's relative path as bytes (the whole path: `a-b` is before
`a/b`), with no record for the directory itself. A regular file is `F NUL path NUL size NUL sha256 NUL`, a directory
`D NUL path NUL`, a symbolic link `L NUL path NUL target NUL`; the paths are raw bytes, `/`-separated, and the sizes
and hashes are ASCII, so no field holds a NUL and the listing reads back one way. Mode, owner, time and the
directory's own path are not in it, so a copy of the tree elsewhere has the digest of the original, as long as its
files hold no path of their own (a venv's scripts name the venv, and are then another tree).

A link is recorded by its target string and never followed: a venv's `bin/python` is a link to an interpreter outside
the tree, which `up` can neither hash nor refuse, so the digest holds where the link points and not what is there. A
special file (a FIFO, a socket, a device) is refused, as is a root that is not a directory of its own. The tree is read
below one held descriptor with no link followed (`O_NOFOLLOW`, entries classified by `lstat` before any is opened, the
opened one checked to be the one classified) and written never: it may be read-only. Every refusal is a `TreeError`
that names the path relative to the directory, escaped, and never an absolute path.
"""

import errno
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from fedfacts import write_private

FILE = "render-identity.json"
VERSION = "expcore-render-identity/1"
# The pins, each key with the placeholder of the file it pins and the placeholder of its hash (`hpcpins.PLACEHOLDERS`,
# `hpcpins.OWN`): a pin is in the record when the template names either.
PINS = {
    "sandbox_sif_sha256": ("hpc_agent_sif", "hpc_agent_sif_sha"),
    "egress_shim_sha256": ("hpc_egress_shim", "hpc_egress_shim_sha"),
    "egress_collector_sha256": ("hpc_egress_collector", "hpc_egress_collector_sha"),
    "oneclient_bundle_sha256": ("hpc_oneclient_bundle", "hpc_oneclient_bundle_sha"),
}
ARTIFACTS = "study_artifacts_tree_sha256"  # in the record only when the site declares HPC_STUDY_ARTIFACTS
KEYS = ("version", "template_sha256", "platform_fingerprint_sha256", *PINS, ARTIFACTS)
HEX_DIGITS = frozenset("0123456789abcdef")
TREE_VERSION = "expcore-artifacts-tree/1"  # the tree digest's own format: the listing's first bytes
MAX_DEPTH = 128  # directories one below another; a walk holds one descriptor for each
CHUNK = 1 << 20


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint_sha256_of(platform: Path) -> str:
    """The SHA-256 of the canonical fingerprint of the rendered platform file, as the core hashes one (the same
    function, over `load_platform(...).fingerprint()`, which a campaign's own load of the file gives)."""
    from expcore.campaign.manifest import fingerprint_sha256
    from expcore_onedata.config import load_platform

    return fingerprint_sha256(load_platform(platform).fingerprint())


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX_DIGITS


def build(template: str, values: Mapping[str, str], platform: Path) -> Dict[str, str]:
    """The record of the platform file just rendered at `platform` from `template` with `values` (what `write_facts`
    filled: the pins `hpcpins.values` gave, and the artifacts directory's digest `up` took, when the site declared
    one). Raises RuntimeError naming a pin, or the artifacts digest, whose value is no SHA-256."""
    tree = values.get(ARTIFACTS)
    if tree is not None and not is_sha256(tree):
        raise RuntimeError(f"{ARTIFACTS}: the digest of the study's artifacts is no SHA-256: no render identity")
    record = {
        "version": VERSION,
        "template_sha256": sha256_text(template),
        "platform_fingerprint_sha256": fingerprint_sha256_of(platform),
    }
    for key, (file_name, pin_name) in PINS.items():
        if f"@{file_name}@" not in template and f"@{pin_name}@" not in template:
            continue
        value = values.get(pin_name)
        if not is_sha256(value):
            raise RuntimeError(f"{pin_name}: the template names it and its value is no SHA-256: no render identity")
        record[key] = value
    if tree is not None:
        record[ARTIFACTS] = tree
    return record


class TreeError(RuntimeError):
    """A tree that cannot be digested: the message names a path relative to the directory (or the directory itself),
    escaped, and never an absolute path."""


def shown(path: bytes) -> str:
    """A path of the tree as a message may hold it: printable ASCII, so a newline or a byte that is no text in a name
    is escaped."""
    return os.fsdecode(path).encode("unicode_escape").decode("ascii")


def why(error: OSError) -> str:
    return errno.errorcode.get(error.errno or 0, type(error).__name__)


def open_below(directory_fd: int, name: str, flags: int, seen: os.stat_result, path: bytes) -> int:
    """`name` below the held directory, opened read-only with no link followed and never blocking, and shown to be the
    entry `seen` (an `lstat` of it) classified: another one put there since is refused."""
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | flags, dir_fd=directory_fd
        )
    except OSError as error:
        raise TreeError(f"{shown(path)} cannot be opened ({why(error)})") from None
    held = os.fstat(descriptor)
    if (held.st_dev, held.st_ino) != (seen.st_dev, seen.st_ino):
        os.close(descriptor)
        raise TreeError(f"{shown(path)} changed while it was read")
    return descriptor


def hash_file(descriptor: int, path: bytes) -> Tuple[int, str]:
    """The size and SHA-256 of the file `descriptor` holds, read once whole and closed; refused when it changed (its
    size or its mtime) while it was read. Read-only: a descriptor of `open_below`."""
    digest, size = hashlib.sha256(), 0
    try:
        before = os.fstat(descriptor)
        while chunk := os.read(descriptor, CHUNK):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
    except OSError as error:
        raise TreeError(f"{shown(path)} cannot be read ({why(error)})") from None
    finally:
        os.close(descriptor)
    if size != after.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise TreeError(f"{shown(path)} changed while it was read")
    return size, digest.hexdigest()


def walk(directory_fd: int, prefix: bytes, depth: int, entries: List[Tuple[bytes, bytes]]) -> None:
    """Add a record to `entries` for every entry below the held directory (`prefix`, its path from the root, empty for
    the root), as (path, record), descending into each directory it holds."""
    if depth > MAX_DEPTH:
        raise TreeError(f"{shown(prefix)} is nested deeper than {MAX_DEPTH} directories")
    try:
        names = os.listdir(directory_fd)
    except OSError as error:
        raise TreeError(f"{shown(prefix) or 'the directory'} cannot be listed ({why(error)})") from None
    for name in names:
        path = prefix + b"/" + os.fsencode(name) if prefix else os.fsencode(name)
        try:
            seen = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise TreeError(f"{shown(path)} cannot be read ({why(error)})") from None
        if stat.S_ISDIR(seen.st_mode):
            entries.append((path, b"D\0" + path + b"\0"))
            child = open_below(directory_fd, name, os.O_DIRECTORY, seen, path)
            try:
                walk(child, path, depth + 1, entries)
            finally:
                os.close(child)
        elif stat.S_ISREG(seen.st_mode):
            size, sha = hash_file(open_below(directory_fd, name, 0, seen, path), path)
            entries.append((path, b"F\0" + path + b"\0" + str(size).encode() + b"\0" + sha.encode() + b"\0"))
        elif stat.S_ISLNK(seen.st_mode):
            try:
                target = os.fsencode(os.readlink(name, dir_fd=directory_fd))
            except OSError as error:
                raise TreeError(f"{shown(path)} cannot be read ({why(error)})") from None
            entries.append((path, b"L\0" + path + b"\0" + target + b"\0"))
        else:
            raise TreeError(f"{shown(path)} is a special file: the tree may hold files, directories and links only")


def tree_sha256(root: Path) -> str:
    """The digest of the tree below `root` (the module's docstring: the canonical listing): 64 lower-case hex digits.
    `TreeError` when `root` is not a directory of its own (a link is refused, never followed), or on a special file, a
    directory or file that cannot be read, one that changed while it was read, or nesting past `MAX_DEPTH`."""
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise TreeError(
            f"the directory cannot be opened ({why(error)}): it must exist and be a directory of its own, not a link"
        ) from None
    entries: List[Tuple[bytes, bytes]] = []
    try:
        walk(root_fd, b"", 0, entries)
    finally:
        os.close(root_fd)
    digest = hashlib.sha256(TREE_VERSION.encode("ascii") + b"\0")
    for _, record in sorted(entries):
        digest.update(record)
    return digest.hexdigest()


def canonical(record: Mapping[str, str]) -> str:
    """The record's text: canonical JSON, sorted keys, no spaces, one newline."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def write(root: Path, record: Mapping[str, str]) -> Path:
    """Write the record into the federation's root, mode 0600 and never through a link; its path."""
    path = root / FILE
    write_private(path, canonical(record))
    return path
