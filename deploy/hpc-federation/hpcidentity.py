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
- the SHA-256 of each pin the rendered platform holds (`PINS`): the agent's SIF, the egress shim, the egress collector,
  oneclient's bundle and the 443 mapper's preload (`gateport_sha256`, the REST layout's). They are the very values `up`
  filled in and checked (`hpcpins`: the site's declared hashes, each file measured against its own in the preflight, and
  the bundle's own collector hashed here), never read again. A pin the template does not name is not in the record: no
  entry is ever empty or null.
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
directory's own path are not in it, so a copy of the tree elsewhere has the digest of the original.

**Site-neutral (`expcore-artifacts-tree/2`).** One study's directory built from one recipe at two sites' paths is one
tree: what an install writes of the place it was made in is not its code. So a file's bytes, and a link's target, are
hashed with each spelling of the directory's own path (its real path and the path it was given by, as below) replaced
by `ROOT_PLACEHOLDER`, and a file's size is that of what is hashed: a venv's scripts (their shebangs, `VIRTUAL_ENV`),
`pyvenv.cfg`, an editable install's `.pth` and the interpreter's `_sysconfigdata` are then the same at every path. And
the bookkeeping that only restates other entries or the install's place and time is left out of the listing: a
`*.dist-info` directory's `RECORD`, `direct_url.json` and `uv_cache.json`, and everything below a `.git` directory but
its `HEAD`, which names the clone's commit (the clone's working tree is in whole). Every other byte is in as it is, so
a changed file of the code, the venv or the interpreter is another digest. A link that is left out is still checked.

A link is recorded by its target string and never followed, so the digest holds where a link points and not what is
there: what a link points at must therefore be in the tree, where its own entry pins it. **The directory is
self-contained** (the study's interpreter is installed in it and its venv is built on it), and a link whose resolved
target leaves it is refused, because a change of what it points at would pass the shard check. A link is inside when
it resolves, as the kernel resolves it, to a place in the tree:

- the link's own path is walked from the root one component at a time; a component that is a link of the tree (the
  walk found them all) is replaced by its target, a relative one read from the directory the link is in, an absolute
  one from the root once the root's own path is stripped; `..` goes up one directory (through whatever the walk has
  reached, a link to a parent included: the dots are never collapsed as text), and one at the root is an escape;
- an absolute target is inside only when it begins, component by component and never as a string prefix (`/a/bc` is
  not inside `/a/b`), with the root's real path or with the path the root was given by (a parent of the root may be a
  link, and a venv built on the directory holds the path it was built by); `..` before that path is reached is no
  match, since a parent there may be a link too;
- the walk never reads or opens what is outside the root: it works on the tree's own entries alone, so a target that
  does not exist outside, or a FIFO, is refused all the same and never touched;
- a path may pass through at most `MAX_LINK_HOPS` (40, Linux's own limit) links, the link itself the first: a longer
  chain, and a loop, are refused;
- a link that dangles inside the tree (its target resolves to a name the tree does not hold) is allowed and recorded as
  any other: a file that appears where it points is a change of the tree, so the digest moves; a link to the root
  itself, or to a directory above it inside the tree, is inside.

A special file (a FIFO, a socket, a device) is refused, as is a root that is not a directory of its own. The tree is
read below one held descriptor with no link followed (`O_NOFOLLOW`, entries classified by `lstat` before any is
opened, the opened one checked to be the one classified) and written never: it may be read-only. Every refusal is a
`TreeError` that names the path relative to the directory, escaped, and never an absolute path (nor a link's target,
which may be one).
"""

import errno
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

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
    "gateport_sha256": ("hpc_gateport", "hpc_gateport_sha"),
}
ARTIFACTS = "study_artifacts_tree_sha256"  # in the record only when the site declares HPC_STUDY_ARTIFACTS
KEYS = ("version", "template_sha256", "platform_fingerprint_sha256", *PINS, ARTIFACTS)
HEX_DIGITS = frozenset("0123456789abcdef")
TREE_VERSION = "expcore-artifacts-tree/2"  # the tree digest's own format: the listing's first bytes
ROOT_PLACEHOLDER = b"@study_artifacts@"  # what the tree's own path is in a file's bytes and a link's target (v2)
# The install's bookkeeping that restates other entries or the place it was made in, left out of the listing (v2): a
# distribution's list of its files' hashes, where it was installed from and uv's cache key, in a `*.dist-info`.
BOOKKEEPING = frozenset({b"RECORD", b"direct_url.json", b"uv_cache.json"})
MAX_DEPTH = 128  # directories one below another; a walk holds one descriptor for each
MAX_LINK_HOPS = 40  # the links one path may pass through, the link itself the first: Linux's own limit (MAXSYMLINKS)
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


def neutral_part(data: bytes, root: "Optional[re.Pattern[bytes]]", keep: int) -> Tuple[bytes, bytes]:
    """The bytes of `data` that can be given out now, each match of the root's spellings `root` replaced by
    `ROOT_PLACEHOLDER`, and the last bytes kept back (at most `keep`, the longest spelling's length less one) since a
    spelling may continue in the next read: a match that starts before them is whole in `data`. A whole-file
    `root.sub`, read in any chunks."""
    if root is None:
        return b"", data
    cut = len(data) - keep
    if cut <= 0:
        return data, b""
    parts, position = [], 0
    for found in root.finditer(data):
        if found.start() >= cut:
            break
        parts += [data[position : found.start()], ROOT_PLACEHOLDER]
        position = found.end()
    end = max(position, cut)
    parts.append(data[position:end])
    return data[end:], b"".join(parts)


def hash_file(
    descriptor: int, path: bytes, root: "Optional[re.Pattern[bytes]]" = None, keep: int = 0
) -> Tuple[int, str]:
    """The size and SHA-256 of the file `descriptor` holds, read once whole and closed, with every spelling of the
    tree's own path (`root`, `root_pattern`) replaced by `ROOT_PLACEHOLDER` first: the size is that of what is hashed.
    Refused when it changed (its size or its mtime) while it was read. Read-only: a descriptor of `open_below`."""
    digest, size, read, pending = hashlib.sha256(), 0, 0, b""
    try:
        before = os.fstat(descriptor)
        while chunk := os.read(descriptor, CHUNK):
            read += len(chunk)
            pending, ready = neutral_part(pending + chunk, root, keep)
            digest.update(ready)
            size += len(ready)
        after = os.fstat(descriptor)
    except OSError as error:
        raise TreeError(f"{shown(path)} cannot be read ({why(error)})") from None
    finally:
        os.close(descriptor)
    if read != after.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise TreeError(f"{shown(path)} changed while it was read")
    rest = pending if root is None else root.sub(ROOT_PLACEHOLDER, pending)
    digest.update(rest)
    return size + len(rest), digest.hexdigest()


def left_out(path: bytes) -> bool:
    """Whether the entry at `path` is install bookkeeping the listing leaves out (v2): `BOOKKEEPING` in a `*.dist-info`
    directory, and everything below a `.git` directory but its `HEAD`, which names the commit (the clone's working
    tree is in whole; its index, logs, refs and config hold where and when it was cloned)."""
    parts = path.split(b"/")
    if len(parts) >= 2 and parts[-2].endswith(b".dist-info") and parts[-1] in BOOKKEEPING:
        return True
    if b".git" in parts[:-1]:
        return parts[parts.index(b".git") + 1 :] != [b"HEAD"]
    return False


def walk(
    directory_fd: int,
    prefix: bytes,
    depth: int,
    entries: List[Tuple[bytes, bytes]],
    links: Dict[bytes, bytes],
    root: "Optional[re.Pattern[bytes]]" = None,
    keep: int = 0,
) -> None:
    """Add a record to `entries` for every entry below the held directory (`prefix`, its path from the root, empty for
    the root) but those `left_out`, as (path, record), descending into each directory it holds, and each link to
    `links` (its path and its target), every one, so a link left out is still checked. A file's bytes and a link's
    target are in with the tree's own path (`root`) replaced. Both are filled in place: they are the walk's result."""
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
        kept = not left_out(path)
        if stat.S_ISDIR(seen.st_mode):
            if kept:
                entries.append((path, b"D\0" + path + b"\0"))
            child = open_below(directory_fd, name, os.O_DIRECTORY, seen, path)
            try:
                walk(child, path, depth + 1, entries, links, root, keep)
            finally:
                os.close(child)
        elif stat.S_ISREG(seen.st_mode):
            if kept:
                size, sha = hash_file(open_below(directory_fd, name, 0, seen, path), path, root, keep)
                entries.append((path, b"F\0" + path + b"\0" + str(size).encode() + b"\0" + sha.encode() + b"\0"))
        elif stat.S_ISLNK(seen.st_mode):
            try:
                target = os.fsencode(os.readlink(name, dir_fd=directory_fd))
            except OSError as error:
                raise TreeError(f"{shown(path)} cannot be read ({why(error)})") from None
            if kept:
                shown_target = target if root is None else root.sub(ROOT_PLACEHOLDER, target)
                entries.append((path, b"L\0" + path + b"\0" + shown_target + b"\0"))
            links[path] = target
        else:
            raise TreeError(f"{shown(path)} is a special file: the tree may hold files, directories and links only")


def components(path: bytes) -> List[bytes]:
    """The parts of `path` that name something: no empty one (a doubled or a trailing `/`), no `.`; a `..` stays."""
    return [part for part in path.split(b"/") if part not in (b"", b".")]


def root_spellings(root: Path) -> List[Tuple[bytes, ...]]:
    """The ways an absolute link may write the root's own path, as components: its real path and, when that differs,
    the path `root` was given by (plain, absolute: a parent of the root may be a link on a shared filesystem, and a
    venv built on the directory holds the path it was built by)."""
    spellings = [tuple(components(os.fsencode(os.path.realpath(root))))]
    given = os.fsencode(root)
    if given.startswith(b"/"):
        parts = tuple(components(given))
        if b".." not in parts and parts not in spellings:
            spellings.append(parts)
    return spellings


def root_pattern(spellings: Sequence[Tuple[bytes, ...]]) -> Tuple["Optional[re.Pattern[bytes]]", int]:
    """The pattern of the root's spellings as a file or a link writes them (`/` and its components joined, longest
    first), and how many bytes a read keeps back for one that continues in the next (`neutral_part`); (None, 0) for
    the filesystem's root, which has no spelling to replace."""
    texts = sorted({b"/" + b"/".join(parts) for parts in spellings if parts}, key=len, reverse=True)
    if not texts:
        return None, 0
    return re.compile(b"|".join(re.escape(text) for text in texts)), len(texts[0]) - 1


def below_root(target: bytes, spellings: Sequence[Tuple[bytes, ...]]) -> Optional[List[bytes]]:
    """The components of the absolute `target` after the root's own path, or None when it does not begin with one of
    its spellings. The comparison is by whole components: `/a/bc` does not begin with `/a/b`; and a `..` before the
    root's path is reached matches nothing (a parent there might be a link)."""
    parts = components(target)
    for spelling in spellings:
        if tuple(parts[: len(spelling)]) == spelling:
            return parts[len(spelling) :]
    return None


def check_link(path: bytes, links: Mapping[bytes, bytes], spellings: Sequence[Tuple[bytes, ...]]) -> None:
    """Raise `TreeError` unless the link at `path` (a key of `links`: path to target, every link the walk found)
    resolves to a place in the tree. `path` is walked from the root as the kernel walks it: a component that is a link
    is replaced by its target (a relative one is read from the directory the link is in, an absolute one from the
    root, `below_root`), `..` goes up one and an escape is one that goes above the root; the dots are never collapsed
    as text first, since `d/..` is not where it started when `d` is a link to somewhere else. Nothing outside the root
    is read: the tree's own entries are all it consults. A path that passes through more than `MAX_LINK_HOPS` links is
    refused (a loop is a chain with no end)."""
    leaves = f"{shown(path)} is a link that leaves the directory: the tree must hold what its links point at"
    here: List[bytes] = []  # the directory reached so far, from the root
    ahead = components(path)[::-1]  # still to walk, the next one last: a stack, spliced in place when a link is met
    hops = 0
    while ahead:
        part = ahead.pop()
        if part == b"..":
            if not here:
                raise TreeError(leaves)
            here.pop()
            continue
        target = links.get(b"/".join([*here, part]))
        if target is None:  # a directory, a file, or a name the tree does not hold
            here.append(part)
            continue
        hops += 1
        if hops > MAX_LINK_HOPS:
            raise TreeError(
                f"{shown(path)} is a link that takes more than {MAX_LINK_HOPS} links to follow: a loop or a chain"
            )
        if target.startswith(b"/"):
            inside = below_root(target, spellings)
            if inside is None:
                raise TreeError(leaves)
            here = []
            ahead.extend(inside[::-1])
        else:
            ahead.extend(components(target)[::-1])


def tree_sha256(root: Path) -> str:
    """The digest of the tree below `root` (the module's docstring: the canonical listing): 64 lower-case hex digits.
    `TreeError` when `root` is not a directory of its own (a link is refused, never followed), on a special file, a
    directory or file that cannot be read, one that changed while it was read, nesting past `MAX_DEPTH`, or a link
    whose target leaves the tree or that takes more than `MAX_LINK_HOPS` links to follow (`check_link`; the first by
    path is named)."""
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise TreeError(
            f"the directory cannot be opened ({why(error)}): it must exist and be a directory of its own, not a link"
        ) from None
    entries: List[Tuple[bytes, bytes]] = []
    links: Dict[bytes, bytes] = {}
    spellings = root_spellings(root)
    pattern, keep = root_pattern(spellings)
    try:
        walk(root_fd, b"", 0, entries, links, pattern, keep)
    finally:
        os.close(root_fd)
    for path in sorted(links):
        check_link(path, links, spellings)
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
