# Provenance: new
"""The file channel between the orchestrating host and a federation's node worker (#462 step 3, C44).

Compute nodes cannot reach the orchestrator, and the orchestrator reaches a site only through its login node, so a
lease goes out, and its heartbeats and its end come back, as files on the site's shared file system. Each federation
has one channel directory, `<channel root>/<label>/`, that holds:

- `inbox/`: what the orchestrator sends. `lease-<id>.json` is a lease (`expcore-elastic-lease/1`: the federation, the
  site and the kernel's lease file as `lease`) or, under the same name, a tombstone (`cancelled`: the lease was taken
  back before its file landed). `drain.json` asks the worker to take no new lease (the login watchdog writes it).
- `outbox/`: what the worker sends. `heartbeat.json` (`expcore-elastic-heartbeat/1`: the lease and its counter),
  `completion-<id>.json` (`expcore-elastic-completion/1`), `return-<id>.json` (`expcore-elastic-return/1`, a lease
  that began no attempt) and `exited.json` (the worker stopped after a drain).
- `archives/lease-<id>.out.tar`: the lease store's `out/`, whose SHA-256 its completion names.
- `worker/`: the worker's own state (the last lease id it took, and `running.json` while a lease runs).
- `places/lease-<id>/`: what a lease leaves for a controller that must settle it from what survived its federation
  (C46): `snapshots/` (the lease store's `out/`, snapshotted while it runs, `expcore.campaign.out_snapshot`) and
  `spend.jsonl` (the run's spend journal). The worker never removes them.

**The rules, the fault relay's (`expcore_onedata.fault_relay`).** Every file is written whole: a draft beside it (a
name starting with a dot, which no reader takes), fsynced, then renamed into place, and the directory fsynced. A lease
file and its tombstone are created under the one name with `link(2)`, which fails when the name exists, so of a late
delivery and a cancel exactly one lands, and a name once written is never replaced. Every file is read only after a
fresh listing of its directory shows its exact name (`os.scandir`), never by a lookup of the path first: an NFS client
caches a failed lookup for up to 30 s, and the listing revalidates the directory. A file that does not parse, or is
not of its format, is not read at all.

The channel root is a site setting. The design note keeps completions and archives in the shared results directory,
which `down` never removes; a root there keeps them for the copy back.
"""

import contextlib
import datetime as dt
import json
import os
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from expcore.campaign.elastic.records import Lease

LEASE_FORMAT = "expcore-elastic-lease/1"
HEARTBEAT_FORMAT = "expcore-elastic-heartbeat/1"
COMPLETION_FORMAT = "expcore-elastic-completion/1"
RETURN_FORMAT = "expcore-elastic-return/1"
DRAIN_FORMAT = "expcore-elastic-drain/1"
EXITED_FORMAT = "expcore-elastic-exited/1"
CONTACT_FORMAT = "expcore-elastic-contact/1"
KERNEL_LEASE_SCHEMA = "expcore-lease/1"  # `run_lease.LEASE_SCHEMA`, the kernel's own file

INBOX, OUTBOX, ARCHIVES, WORKER, PLACES = "inbox", "outbox", "archives", "worker", "places"
HEARTBEAT, DRAIN, EXITED, CONTACT = "heartbeat.json", "drain.json", "exited.json", "contact.json"
LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")  # the ledger's operator labels: never a path or a host name
_NAMED = re.compile(r"(lease|completion|return)-(\d{12})\.json")
_TIME = "%Y-%m-%dT%H:%M:%SZ"


def lease_name(lease_id: int) -> str:
    return f"lease-{lease_id:012d}.json"


def place_name(lease_id: int) -> str:
    """The directory of a lease's place under `places/`."""
    return f"lease-{lease_id:012d}"


def completion_name(lease_id: int) -> str:
    return f"completion-{lease_id:012d}.json"


def return_name(lease_id: int) -> str:
    return f"return-{lease_id:012d}.json"


def archive_name(lease_id: int) -> str:
    return f"lease-{lease_id:012d}.out.tar"


def named_id(name: str, kind: str) -> int | None:
    """The lease id a channel file's name carries, for a file of `kind` (lease, completion, return); None otherwise."""
    found = _NAMED.fullmatch(name)
    return int(found.group(2)) if found and found.group(1) == kind and int(found.group(2)) > 0 else None


def federation_dir(root: str | Path, label: str) -> Path:
    return Path(root) / label


def make_dirs(channel: Path) -> None:
    """Create a channel's directories, each 0700, where they are missing."""
    for name in ("", INBOX, OUTBOX, ARCHIVES, WORKER):
        (channel / name).mkdir(mode=0o700, parents=True, exist_ok=True)


def fsync_dir(directory: Path) -> None:
    """Make a rename or a link in `directory` durable."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _draft(path: Path, data: bytes) -> Path:
    """A fsynced draft beside `path`, 0600, under a dotted name no reader takes."""
    draft = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    fd = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            draft.unlink()
        raise
    return draft


def _encoded(record: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(record), sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def write_json(path: Path, record: Mapping[str, Any]) -> None:
    """Write one JSON file whole: a draft, fsynced, renamed over `path`, the directory fsynced."""
    draft = _draft(path, _encoded(record))
    try:
        os.replace(draft, path)
    except BaseException:
        with contextlib.suppress(OSError):
            draft.unlink()
        raise
    fsync_dir(path.parent)


def create_json(path: Path, record: Mapping[str, Any]) -> bool:
    """Create `path` whole, only if no file has the name (`link(2)`): True when this call created it."""
    draft = _draft(path, _encoded(record))
    try:
        os.link(draft, path)
    except FileExistsError:
        return False
    finally:
        with contextlib.suppress(OSError):
            draft.unlink()
    fsync_dir(path.parent)
    return True


def listing(directory: Path) -> set[str]:
    """The names a fresh listing of `directory` shows, drafts left out. Raises OSError when it cannot be listed."""
    with os.scandir(directory) as entries:
        return {entry.name for entry in entries if not entry.name.startswith(".")}


def read_listed(directory: Path, names: set[str], name: str) -> dict[str, Any] | None:
    """One JSON object of a regular file of ours that the listing `names` showed; None when it is not listed, not a
    regular file of this user, or not one JSON object."""
    if name not in names:
        return None
    path = directory / name
    try:
        seen = path.lstat()
        if not path.is_file() or path.is_symlink() or seen.st_uid != os.getuid():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def utc_text(seconds: float) -> str:
    """Seconds since the epoch as the kernel's lease time, rounded down to the second."""
    return dt.datetime.fromtimestamp(int(seconds), tz=dt.UTC).strftime(_TIME)


def kernel_lease(lease: Lease) -> dict[str, Any]:
    """The kernel's lease file (`run_lease.read_lease`, `expcore-lease/1`) for a ledger lease: its run, its id, its
    attempt base (the run's charged attempts in its earlier leases, the ledger's one count), its grant in micro-dollars,
    the campaign's fingerprint hash, and its hard term rounded down to the second."""
    return {
        "schema": KERNEL_LEASE_SCHEMA,
        "run_key": lease.run_key,
        "lease_id": lease.lease_id,
        "attempt_base": lease.attempt_base,
        "grant_micro_usd": lease.grant_micro,
        "campaign_sha256": lease.fingerprint,
        "expires_utc": utc_text(lease.hard_term_at),
    }


def envelope(lease: Lease) -> dict[str, Any]:
    """A lease file of the inbox: who it is for, and the kernel's lease."""
    return {"format": LEASE_FORMAT, "federation": lease.federation, "site": lease.site, "lease": kernel_lease(lease)}


def tombstone(label: str, site: str, lease_id: int) -> dict[str, Any]:
    """The file that takes a lease's name in the inbox when the lease is cancelled before its file landed."""
    return {"format": LEASE_FORMAT, "federation": label, "site": site, "cancelled": lease_id}


def is_tombstone(record: Mapping[str, Any]) -> bool:
    return record.get("format") == LEASE_FORMAT and "cancelled" in record and "lease" not in record


__all__ = [
    "ARCHIVES",
    "COMPLETION_FORMAT",
    "CONTACT",
    "CONTACT_FORMAT",
    "DRAIN",
    "DRAIN_FORMAT",
    "EXITED",
    "EXITED_FORMAT",
    "HEARTBEAT",
    "HEARTBEAT_FORMAT",
    "INBOX",
    "LABEL",
    "LEASE_FORMAT",
    "OUTBOX",
    "PLACES",
    "RETURN_FORMAT",
    "WORKER",
    "archive_name",
    "completion_name",
    "create_json",
    "envelope",
    "federation_dir",
    "fsync_dir",
    "is_tombstone",
    "kernel_lease",
    "lease_name",
    "listing",
    "make_dirs",
    "named_id",
    "place_name",
    "read_listed",
    "return_name",
    "tombstone",
    "utc_text",
    "write_json",
]
