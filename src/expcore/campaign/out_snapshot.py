# Provenance: new
"""Snapshots of a live campaign's out/ on a filesystem that outlives its host, and the restore of the newest good one
(O62).

A campaign whose store must live on a host's memory filesystem (an HPC node's tmpfs: SQLite needs locks a network
filesystem may not give, `deploy/hpc-federation/shards.py`) loses the whole of its out/ when the host is killed
outright. `take` copies it, while its dispatcher runs, into a directory of its own under a snapshots directory on the
shared filesystem, and `restore` puts a snapshot back for the next start.

**One consistent state: the store first, then runs/.** An attempt's row says 'running' before its run starts, its
record and marker are written in its directory when it ends, and only then does its row record the result (C04).
`take` copies the store FIRST, by the online backup over a read-only connection (`snapshot.take_snapshot`: one
committed state whatever the dispatcher is doing, its integrity checked, each attempt's directory named within the
batch), and `runs/` AFTER it. Every attempt the store copy holds began before the backup, and one it holds as ended
had written its record and marker before the backup, so the copy of `runs/` has them: the store never names a record
`runs/` lacks, and `take` checks it (every ended attempt's marker is in the copy). Copied the other way, an attempt
that ended between the two copies would be ended in the store and missing from `runs/`, and nothing would ever run it
again. An attempt that ends after the backup is 'running' in the copy, perhaps with its marker copied: a resume's
recovery commits it from the marker, or runs it again (C04). A directory of `runs/` that no row of the store copy
names is an attempt that began after the backup and is left out: the restored store would give that run's next
attempt the same directory and, its sequence being the copy's, the same attempt id, so a marker left there would read
as the next attempt's.

**Verified, then swapped in by rename.** A snapshot is written into a fresh directory, `.snap-<n>.part`, beside the
others: the store's copy and each file of `runs/`, hashed as copied. It is then checked: every ended attempt's marker
present, every file read back against its hash. Its manifest is written last, and the directory renamed to
`snap-<n>`. Only then are older snapshots removed, the newest `KEEP` kept, so the previous good one stays until the
new one is in place. A kill at any point leaves every snapshot that was whole as it was: a `.part` directory is never
restored, and the next `take` removes it.

**Restore.** `restore` copies one snapshot into a new out/, every file hashed as copied and compared with the
manifest (one missing, added or changed fails it), checks the store copy's integrity, and points its attempts'
directories at the new `runs/` (`snapshot.rebase_run_dirs`); one that fails leaves nothing behind. `snapshots` lists
them newest first, for the caller to try in turn.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from expcore.campaign.manifest import fingerprint_sha256
from expcore.campaign.markers import MARKER_NAME
from expcore.campaign.snapshot import rebase_run_dirs, take_snapshot

FORMAT = "expcore-out-snapshot/1"
MANIFEST = "snapshot.json"
STORE = "state.sqlite"  # the scheduler's STATE_FILE, under out/
RUNS = "runs"
KEEP = 2  # the newest snapshots kept: a damaged newest one falls back to the one before
ENDED = ("done", "infra_fail", "harness_error", "outage_discarded")  # states whose attempt wrote its marker (C04)
CHUNK = 1 << 20
_NAME = re.compile(r"snap-(\d{6,})")
_PART = re.compile(r"\.snap-(\d{6,})\.part")
_TRY = re.compile(r"try\d+")

Crash = Callable[[str], None]


class OutSnapshotError(RuntimeError):
    """A snapshot that cannot be taken, or does not verify; the message names the snapshot and what is wrong."""


def _never(point: str) -> None:
    """No kill: the test seam of `take` and `restore`."""


# ------------------------------------------------------------------------------------------------ taking one
def take(
    out: Path,
    into: Path,
    *,
    scratch: Path,
    facts: Mapping[str, Any],
    live: bool,
    now: str,
    crash: Crash = _never,
) -> Path:
    """Snapshot `out/` into a new `into/snap-<n>` and return it; `OutSnapshotError` when it cannot be, the snapshots
    already there left as they were.

    `scratch` is a directory on the host's own filesystem for the store's backup (SQLite is never opened on the shared
    one); `facts` go into the manifest as they are (the caller's: which shard, from which archive); `live` says a
    dispatcher holds the store (read in place, over a read-only connection) or none does (read from a private copy,
    so nothing is written beside it). `crash(point)` is called at `backup` (the store half copied), `runs` (one file
    of runs/ copied), `verified` and `rename` (before the rename) and `renamed` (before older ones are removed)."""
    into.mkdir(parents=True, exist_ok=True)
    for leftover in _parts(into):  # a take killed before: never restored, removed now
        shutil.rmtree(leftover, ignore_errors=True)
    number = 1 + max((_number(p) for p in (*snapshots(into), *_parts(into))), default=0)
    part = into / f".snap-{number:06d}.part"
    part.mkdir()
    scratch.mkdir(parents=True, exist_ok=True)
    backup = scratch / STORE
    try:
        take_snapshot(out / STORE, backup, allow_running=True, in_place=live)
        store = _copy(backup, part / STORE, lambda: crash("backup"))
        ended, named, campaign = _attempts(backup)
        files = _copy_runs(out / RUNS, part / RUNS, named, crash)
        missing = sorted(d for d in ended if f"{d}/{MARKER_NAME}" not in files)
        if missing:
            raise OutSnapshotError(
                f"the store copy names {len(missing)} ended attempt(s) whose marker the runs/ copy lacks "
                f"({missing[0]}): it would name a record runs/ does not hold"
            )
        _read_back(part, {STORE: store, **{f"{RUNS}/{name}": digest for name, digest in files.items()}})
        crash("verified")
        manifest = {"format": FORMAT, "number": number, "taken_utc": now, "facts": dict(facts), "campaign": campaign,
                    "store": store, "runs": files}  # fmt: skip
        _write_json(part / MANIFEST, manifest)
        _sync_dir(part)
        crash("rename")
        final = into / f"snap-{number:06d}"
        os.rename(part, final)
        _sync_dir(into)
    except Exception:
        shutil.rmtree(part, ignore_errors=True)
        raise
    finally:
        for name in (STORE, f"{STORE}-wal", f"{STORE}-shm"):
            (scratch / name).unlink(missing_ok=True)
    crash("renamed")
    for old in snapshots(into)[KEEP:]:
        shutil.rmtree(old, ignore_errors=True)
    return final


def snapshots(into: Path) -> list[Path]:
    """The snapshots in `into`, newest first; a `.part` directory is none."""
    try:
        found = [p for p in into.iterdir() if _NAME.fullmatch(p.name) and p.is_dir() and not p.is_symlink()]
    except FileNotFoundError:
        return []
    return sorted(found, key=_number, reverse=True)


def _parts(into: Path) -> list[Path]:
    return [p for p in into.iterdir() if _PART.fullmatch(p.name)]


def _number(path: Path) -> int:
    found = _NAME.fullmatch(path.name) or _PART.fullmatch(path.name)
    assert found is not None
    return int(found.group(1))


def _attempts(store: Path) -> tuple[set[str], set[str], str | None]:
    """The run directories (`<digest>/try<N>`) of the ended attempts and of all attempts of a store copy, and the hash
    of the campaign fingerprint it records."""
    connection = sqlite3.connect(f"file:{quote(str(store))}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT state, run_dir FROM attempts WHERE run_dir IS NOT NULL").fetchall()
        recorded = connection.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
    finally:
        connection.close()
    campaign = None if recorded is None else fingerprint_sha256(json.loads(recorded[0]))
    return {d for state, d in rows if state in ENDED}, {d for _, d in rows}, campaign


def _copy_runs(source: Path, target: Path, named: set[str], crash: Crash) -> dict[str, str]:
    """Copy each regular file under `source` to `target`, hashed as copied, but those of a run directory no attempt of
    the store copy names and every name that starts with a dot (a file being written in place); a file that goes
    away while it is copied is left out. Returns `{relative path: sha256}`."""
    files: dict[str, str] = {}
    target.mkdir()
    for base, dirs, names in os.walk(source):
        relative = PurePosixPath(Path(base).relative_to(source).as_posix())
        kept = []
        for d in sorted(dirs):
            inside = relative / d
            orphan = _TRY.fullmatch(d) and len(inside.parts) == 2 and inside.as_posix() not in named
            if not d.startswith(".") and not orphan:
                kept.append(d)
        dirs[:] = kept
        for name in sorted(names):
            if name.startswith("."):
                continue
            path = Path(base) / name
            if not path.is_file() or path.is_symlink():
                continue
            destination = target / relative / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                files[(relative / name).as_posix()] = _copy(path, destination)["sha256"]
            except FileNotFoundError:
                continue
            if len(files) == 1:
                crash("runs")
    return files


def _copy(source: Path, target: Path, halfway: Callable[[], None] | None = None) -> dict[str, Any]:
    """Copy one file whole into a new `target`, fsynced; `{"sha256", "bytes"}` of the bytes copied. `halfway` runs
    once the first chunk is written (a test's kill)."""
    digest, size = hashlib.sha256(), 0
    with open(source, "rb") as reader, open(target, "xb") as writer:
        while chunk := reader.read(CHUNK):
            digest.update(chunk)
            writer.write(chunk)
            size += len(chunk)
            if halfway is not None:
                writer.flush()
                halfway()
                halfway = None
        writer.flush()
        os.fsync(writer.fileno())
    return {"sha256": digest.hexdigest(), "bytes": size}


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _read_back(part: Path, expected: Mapping[str, Any]) -> None:
    """Read every file of a snapshot back against the hash it was copied with."""
    for name, digest in expected.items():
        want = digest["sha256"] if isinstance(digest, Mapping) else digest
        if _hash(part / name) != want:
            raise OutSnapshotError(f"{part.name}: {name} read back is not what was copied")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    with open(path, "x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _sync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# ------------------------------------------------------------------------------------------------ restoring one
def manifest_of(snapshot: Path) -> dict[str, Any]:
    """A snapshot's manifest; `OutSnapshotError` naming the snapshot when it is not one."""
    try:
        manifest = json.loads((snapshot / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise OutSnapshotError(f"{snapshot.name}: no readable manifest") from None
    store, runs = (manifest.get(k) if isinstance(manifest, dict) else None for k in ("store", "runs"))
    if manifest.get("format") != FORMAT or not isinstance(store, dict) or not isinstance(runs, dict):
        raise OutSnapshotError(f"{snapshot.name}: its manifest is not {FORMAT}")
    if not isinstance(manifest.get("facts"), dict):
        raise OutSnapshotError(f"{snapshot.name}: its manifest has no facts")
    return manifest


def restore(snapshot: Path, out: Path) -> dict[str, Any]:
    """Copy a snapshot into `out` (new) and return its manifest, every file hashed as copied and compared with it, the
    store copy's integrity checked and its attempts' directories pointed at `out/runs`; `OutSnapshotError` naming the
    snapshot and what failed, with nothing left at `out`."""
    manifest = manifest_of(snapshot)
    expected = {STORE: manifest["store"].get("sha256"), **{f"{RUNS}/{k}": v for k, v in manifest["runs"].items()}}
    found = {
        Path(base, name).relative_to(snapshot).as_posix()
        for base, _, names in os.walk(snapshot)
        for name in names
        if Path(base, name) != snapshot / MANIFEST
    }
    if found != set(expected):
        extra, gone = sorted(found - set(expected)), sorted(set(expected) - found)
        raise OutSnapshotError(
            f"{snapshot.name}: its files are not its manifest's (added {extra[:1]}, gone {gone[:1]})"
        )
    out.mkdir(parents=True)
    try:
        for name in sorted(expected):
            (out / name).parent.mkdir(parents=True, exist_ok=True)
            if _copy(snapshot / name, out / name)["sha256"] != expected[name]:
                raise OutSnapshotError(f"{snapshot.name}: {name} is not the file its manifest names")
        _integrity(snapshot, out / STORE)
        rebase_run_dirs(out / STORE, out / RUNS)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    return manifest


def _integrity(snapshot: Path, store: Path) -> None:
    try:
        connection = sqlite3.connect(store)
        try:
            verdict = connection.execute("PRAGMA integrity_check").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise OutSnapshotError(f"{snapshot.name}: its store cannot be read ({type(error).__name__})") from None
    if [tuple(row) for row in verdict] != [("ok",)]:
        raise OutSnapshotError(f"{snapshot.name}: its store fails its integrity check")


# ------------------------------------------------------------------------------------------------ progress
def ended_attempts(out: Path) -> int | None:
    """How many attempts under `out` have ended: the markers in `runs/` (each ended attempt writes one, before its row
    records the result, C04); None when there is no `runs/` yet. A change says a run ended since the last reading.
    The store is never opened for it: a read-only open of a store in WAL mode leaves its -shm and -wal beside it."""
    runs = out / RUNS
    if not runs.is_dir():
        return None
    try:
        return sum(1 for marker in runs.glob(f"*/try*/{MARKER_NAME}") if marker.is_file())
    except OSError:
        return None
