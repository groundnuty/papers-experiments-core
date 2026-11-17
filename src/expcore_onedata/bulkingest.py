# Provenance: new
"""A manifest's files ingested into a space as its sole writer, through one listings cache (O67).

`Provisioner.upload` lists the target's directory whole for every file, so n files into one directory cost n listings
that grow with it: quadratic for a flat dataset (15,340 files in one directory is about 125,000 listing pages). Given
one cache (`listings`), the uploads list each directory once and add every entry they make, which is linear. A cache
is right only while nothing else writes into the space, so this module is the one operation that passes one, on two
conditions that make a stale cache harmless:

- **It holds the federation alone.** `ingest_hold` takes the federation's lock (`admission.FederationLock`, beside the
  fault journal) exclusively, with the purpose `ingest` and the space's label in its holder record, for as long as the
  ingest runs: no run, no tool outside a campaign, no injector and no other ingest is admitted meanwhile (the health
  gate names it "an ingest"), and it is refused while the fault journal holds an unsettled injection. `ingest_files`
  refuses to start unless the running context holds that hold for the provisioner's own federation. A platform without
  `[faults]` has no such lock, and its ingest is refused. A writer that takes no admission (a person, another
  program with the federation's credentials) is outside the hold: the final listing is what shows its effect.
- **It ends with a fresh listing compared with the manifest.** The root is listed again without the cache, every level
  of it, and every manifest file must be there as a regular file of its declared size, every directory on the way to
  one must be a directory, and nothing else may be under the root. Any difference fails the ingest, naming each path
  (`IngestMismatch`), so a wrong skip, a lost file or another writer's file shows at the end, never as success.

**Before each upload, the source is checked** against the manifest: its size and its SHA-256 (a full read), so bytes
that are not the manifest's are never sent; the upload then verifies what the space holds by its hash, as every upload
does (O25). A source is a local path or a `streamed.Slice` (a member of an uncompressed tar, `streamed.tar_members`).

The manifest's paths are relative to `root`, a directory of the space (the whole space when empty: then nothing else
may be in the space at all). A path held twice, a file that is also a directory of another path, and a path that is
not plain and relative are refused before anything is sent.
"""

import contextlib
import hashlib
import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expcore_onedata.admission import (
    INGEST,
    Admission,
    AdmissionRefused,
    FederationLock,
    current_admission,
    fault_settings_of,
    federation_of,
    require_same_federation,
)
from expcore_onedata.config import PlatformConfig
from expcore_onedata.provision import Entry, Listings, Provisioner, ProvisionError
from expcore_onedata.rangemap import safe_path
from expcore_onedata.streamed import Digest, Slice, chunks

SHOWN = 10  # the most paths a mismatch names of each kind; the counts are whole
_SHA256 = re.compile(r"[0-9a-f]{64}")
_READ_BYTES = 1 << 20  # the source check's read size


class IngestRefused(ProvisionError):
    """The ingest cannot start: the federation is not held alone, or the manifest cannot be a tree. Nothing was sent."""


class IngestMismatch(ProvisionError):
    """A source, or the space's listing at the end, differs from the manifest; the message names each path."""


@dataclass(frozen=True)
class ManifestFile:
    """One file of the manifest: its path under the ingest's root, its source, and its declared size and SHA-256."""

    path: str
    source: Path | Slice
    size: int
    sha256: str


@dataclass(frozen=True)
class IngestReport:
    """What the ingest did: one record per file in the manifest's order (`index`, `path`, `size`, `action`,
    `sent_bytes`, `check_s`, `upload_s`), the seconds of the uploads and of the final listing, and what it found."""

    root: str
    files: tuple[dict[str, Any], ...]
    upload_s: float
    listing: dict[str, Any] = field(default_factory=dict)

    def actions(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.files:
            counts[record["action"]] = counts.get(record["action"], 0) + 1
        return counts

    def to_json(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "files": list(self.files),
            "upload_s": self.upload_s,
            "listing": dict(self.listing),
            "actions": self.actions(),
        }


@contextlib.contextmanager
def ingest_hold(config: PlatformConfig, *, label: str, timeout_s: float | None = None) -> Iterator[Admission]:
    """Hold the federation of `config` alone for an ingest into the space `label`, as the running context's admission:
    its lock exclusively, with the purpose `ingest` and the label in the holder record (`FederationLock.held`). Waits
    at most `timeout_s` (FederationBusy when another holds it). A platform without `[faults]` has no lock: refused."""
    settings = fault_settings_of(config)
    if settings is None:
        raise IngestRefused(
            "the platform has no [faults] table, so its federation has no lock an ingest could hold alone"
        )
    with FederationLock(settings).held(exclusive=True, timeout_s=timeout_s, target=label, purpose=INGEST) as held:
        yield held


def ingest_files(
    provisioner: Provisioner,
    files: Sequence[ManifestFile],
    *,
    root: str = "",
    on_file: Callable[[dict[str, Any]], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> IngestReport:
    """Ingest `files` under `root` in the provisioner's space, as its sole writer; return the report.

    Refused (`IngestRefused`, nothing sent) unless the running context holds the provisioner's federation alone for an
    ingest (`ingest_hold`), and when the manifest cannot be a tree. Each source is checked against the manifest and
    uploaded through one listings cache; `on_file` is called with each file's record once it is in. The ingest ends
    with a fresh listing of the root compared with the manifest (`check_listing`). `IngestMismatch` names what
    differs."""
    _require_sole_writer(provisioner.config)
    prefix = _checked(files, root)
    cache: Listings = {}
    records: list[dict[str, Any]] = []
    began = clock()
    for index, item in enumerate(files):
        started = clock()
        _check_source(item)
        checked = clock()
        done = provisioner.upload(f"{prefix}{item.path}", item.source, listings=cache)
        ended = clock()
        if done.digest != Digest(item.size, item.sha256):
            raise IngestMismatch(
                f"{item.path}: the space holds {done.digest.size} bytes of another hash after its upload"
            )
        record = {
            "index": index,
            "path": item.path,
            "size": item.size,
            "action": done.action,
            "sent_bytes": done.sent_bytes,
            "check_s": checked - started,
            "upload_s": ended - checked,
        }
        records.append(record)
        if on_file is not None:
            on_file(record)
    upload_s = clock() - began
    listing = check_listing(provisioner, files, root=root, clock=clock)
    return IngestReport(root, tuple(records), upload_s, listing)


def check_listing(
    provisioner: Provisioner,
    files: Sequence[ManifestFile],
    *,
    root: str = "",
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """List `root` afresh, every level, without a cache, and compare it with the manifest: every file there as a
    regular file of its size, every directory on the way a directory, nothing else. Return what was found (counts and
    seconds); raise `IngestMismatch` naming every kind of difference."""
    began = clock()
    prefix = _checked(files, root)
    try:
        top = provisioner.resolve(prefix.rstrip("/")) if prefix else Entry(provisioner.root(), "", 0, "DIR")
    except KeyError:
        raise IngestMismatch(f"the ingest's root {root!r} is not in the space") from None
    if top.kind != "DIR":
        raise IngestMismatch(f"the ingest's root {root!r} is not a directory")
    found: dict[str, Entry] = {}
    pending = [("", top.file_id)]
    while pending:
        under, directory = pending.pop()
        for name, entry in provisioner.children(directory).items():
            path = f"{under}{name}"
            found[path] = entry
            if entry.kind == "DIR":
                pending.append((f"{path}/", entry.file_id))
    wanted = {item.path: item.size for item in files}
    folders = _directories(wanted)
    differences = {
        "missing": [p for p in wanted if p not in found],
        "not a regular file": [p for p in wanted if p in found and found[p].kind != "REG"],
        "of another size": [
            f"{p} (size {found[p].size}, declared {wanted[p]})"
            for p in wanted
            if p in found and found[p].kind == "REG" and found[p].size != wanted[p]
        ],
        "not a directory": [p for p in folders if p in found and found[p].kind != "DIR"],
        "not in the manifest": sorted(p for p in found if p not in wanted and p not in folders),
    }
    said = [
        f"{len(paths)} {kind}: {', '.join(paths[:SHOWN])}{' ...' if len(paths) > SHOWN else ''}"
        for kind, paths in differences.items()
        if paths
    ]
    if said:
        raise IngestMismatch(
            f"the space's listing under {root or 'its root'!r} differs from the manifest: " + "; ".join(said)
        )
    return {
        "files": sum(1 for e in found.values() if e.kind == "REG"),
        "directories": sum(1 for e in found.values() if e.kind == "DIR"),
        "seconds": clock() - began,
    }


def _require_sole_writer(config: PlatformConfig) -> None:
    settings = fault_settings_of(config)
    if settings is None:
        raise IngestRefused(
            "the platform has no [faults] table, so its federation has no lock an ingest could hold alone"
        )
    held = current_admission()
    if held is None or not held.exclusive:
        raise IngestRefused(
            "an ingest runs only while this context holds its federation alone (ingest_hold): its listings cache is "
            "right only for the space's sole writer; nothing was sent"
        )
    if (held.record or {}).get("purpose") != INGEST:
        raise IngestRefused("the federation is held alone for another purpose than an ingest; nothing was sent")
    try:
        require_same_federation(held, federation_of(settings))
    except AdmissionRefused:
        raise IngestRefused("the hold is for another federation than the provisioner's; nothing was sent") from None


def _checked(files: Sequence[ManifestFile], root: str) -> str:
    """The root as a prefix of the paths (`""` or `"a/b/"`), once the manifest is shown to be a tree."""
    if not files:
        raise IngestRefused("the manifest holds no file to ingest")
    try:
        prefix = "/".join(safe_path(root)) + "/" if root else ""
        for item in files:
            safe_path(item.path)
    except ValueError:
        raise IngestRefused("a manifest path, or the root, is not a plain path relative to the space") from None
    seen: set[str] = set()
    for item in files:
        if item.path in seen:
            raise IngestRefused(f"{item.path}: the manifest holds the path twice")
        seen.add(item.path)
        size = item.size
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or not _SHA256.fullmatch(item.sha256):
            raise IngestRefused(f"{item.path}: the manifest's size or SHA-256 is not one")
    clash = sorted(seen & _directories(seen))
    if clash:
        raise IngestRefused(f"{clash[0]}: the manifest holds it as a file and as a directory of another path")
    return prefix


def _directories(paths: Any) -> set[str]:
    """Every directory on the way to the paths, relative to the root."""
    found: set[str] = set()
    for path in paths:
        parts = path.split("/")[:-1]
        found.update("/".join(parts[: n + 1]) for n in range(len(parts)))
    return found


def _check_source(item: ManifestFile) -> None:
    """Read the source whole and refuse it unless it is the manifest's size and SHA-256 (nothing of it is sent)."""
    size = item.source.size if isinstance(item.source, Slice) else Path(item.source).stat().st_size
    hasher, read = hashlib.sha256(), 0
    if size == item.size:
        for _, block in chunks(item.source, _READ_BYTES):
            hasher.update(block)
            read += len(block)
    if size != item.size or read != item.size or hasher.hexdigest() != item.sha256:
        raise IngestMismatch(f"{item.path}: its source is not the manifest's bytes (size or SHA-256); nothing was sent")


__all__ = [
    "IngestMismatch",
    "IngestRefused",
    "IngestReport",
    "ManifestFile",
    "check_listing",
    "ingest_files",
    "ingest_hold",
]
