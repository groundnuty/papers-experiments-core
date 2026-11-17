# Provenance: new
"""What a run of the Apptainer backend left on the node, recorded before it exists, reaped, and proved gone (O56, C19).

Each sandbox writes `<work_root>/_runs/<sid>.json` before it creates anything (crash-safe: a draft, fsynced, renamed),
and again as each helper starts: the lease, the driver that owns it (pid and start time), each helper process (pid and
start time) and the run's token files by their place in its directory. Its stop removes the record last, once
everything it names is shown gone. So a driver killed outright leaves a record, and:

* the next start on the same work root **reaps** every record whose owner is shown gone (`reap`): it SIGKILLs the
  helpers it names and every process that carries the run's tag (a mount's oneclient included), removes the token
  files first, kills every process whose command line names the run's work disk image (O71, `workdisk.end_holders`),
  unmounts whatever is still mounted in the run's directory (`fusermount3 -u`, then `-uz`), then removes the
  directory, and drops the record once all of it is gone. A record whose owner lives, or whose owner cannot be
  told gone, is never touched;
* the lease's **probe** (`ApptainerProbe.resource_gone`) says a key's sandbox is gone only when no record names the
  key, no run directory of the key is under the work root and no process carries a run tag of the key; anything it
  cannot read is not gone.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from expcore.campaign.lease import process_gone
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.apptainer.argv import RUN_TAG
from expcore_onedata.sandbox.apptainer.node import NodeOps
from expcore_onedata.sandbox.apptainer.workdisk import end_holders, image_path

log = logging.getLogger(__name__)

RUNS = "_runs"
FORMAT = "expcore-apptainer-run/1"
SID = re.compile(r"[A-Za-z0-9_.-]{1,64}-[0-9a-f]{8}")
UNMOUNT, LAZY_UNMOUNT = ("fusermount3", "-u"), ("fusermount3", "-uz")  # an unprivileged user's own FUSE mounts
UNMOUNT_S = 30.0


def record_path(work_root: Path, sid: str) -> Path:
    return work_root / RUNS / f"{sid}.json"


def write(work_root: Path, document: Mapping[str, Any]) -> None:
    """Write a run's record crash-safely; `SandboxError` when it cannot be written (nothing is created without it)."""
    target = record_path(work_root, str(document["sid"]))
    try:
        target.parent.mkdir(mode=0o700, exist_ok=True)
        draft = target.with_suffix(".tmp")
        with open(draft, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"format": FORMAT, **document}, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(draft, target)
    except OSError as error:
        raise SandboxError(f"the run's record could not be written ({type(error).__name__})") from None


def drop(work_root: Path, sid: str) -> None:
    record_path(work_root, sid).unlink(missing_ok=True)


def read_all(work_root: Path) -> list[dict[str, Any]] | None:
    """Every run record under the work root; None when one cannot be read or is not a record (unknown)."""
    base = work_root / RUNS
    if not base.exists():
        return []
    found = []
    try:
        for path in sorted(base.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            if not (isinstance(document, dict) and document.get("format") == FORMAT and SID.fullmatch(
                str(document.get("sid", ""))
            )):  # fmt: skip
                return None
            found.append(document)
    except (OSError, ValueError):
        return None
    return found


def remove_tree(path: Path) -> None:
    """Remove a run's directory, read-only copies included: every directory in it (never a link) is made writable
    first, so `rmtree` can empty it; nothing outside it is followed or changed. A tree that stays raises `OSError`."""
    if not os.path.lexists(path):
        return
    for root, dirs, _ in os.walk(path, topdown=True, followlinks=False):
        for name in (root, *(os.path.join(root, d) for d in dirs)):
            info = os.lstat(name)
            if stat.S_ISDIR(info.st_mode):
                os.chmod(name, stat.S_IMODE(info.st_mode) | stat.S_IRWXU)
    shutil.rmtree(path)


def _tagged(node: NodeOps, sid: str) -> list[Any] | None:
    tag = f"{RUN_TAG}={sid}"
    found = node.processes((tag,))
    return None if found is None else [p for p in found if tag in p.tags]


def _unmount_all(node: NodeOps, run_dir: Path) -> None:
    """Unmount every mount left inside a dead run's directory, deepest first: `fusermount3 -u`, then `-uz` (lazy) as a
    last resort, once its daemon is gone; `SandboxError` unless the table then shows none (unknown is never none)."""
    found = node.mounts_under(run_dir)
    if found is None:
        raise SandboxError("the mount table could not be read")
    for mountpoint in found:
        for argv in (UNMOUNT, LAZY_UNMOUNT):
            node.run([*argv, mountpoint], UNMOUNT_S)
            still = node.mounts_under(run_dir)
            if still is not None and mountpoint not in still:
                break
    left = node.mounts_under(run_dir)
    if left is None or left:
        raise SandboxError("a dead driver's run still has a mount in its directory")


def reap(node: NodeOps, work_root: Path, *, own: str) -> dict[str, Any]:
    """Reap the runs whose driver is shown gone (the module's docstring); `own` is the starting sandbox's id. Counts
    and errors, never a name. It never raises."""
    out: dict[str, Any] = {"runs": 0, "processes": 0, "left": 0}
    records = read_all(work_root)
    if records is None:
        out["error"] = "a run record could not be read"
        return out
    for document in records:
        sid, owner = document["sid"], document.get("owner") or {}
        if sid == own or node.alive(int(owner.get("pid", 0)), str(owner.get("start", ""))) is not False:
            continue
        try:
            killed = _reap_one(node, work_root, document)
            out["runs"] += 1
            out["processes"] += killed
        except (OSError, SandboxError) as error:
            out["left"] += 1
            log.error("a dead driver's run could not be reaped: %s", type(error).__name__)
    return out


def inside(run_dir: Path, relative: object) -> Path | None:
    """A record's token path as a file of the run's directory, or None for one that is not: absolute, or leaving it
    by `..`. A reaper reads the record from disk, so it never removes a path the record names outside the run."""
    if not isinstance(relative, str) or not relative or os.path.isabs(relative):
        return None
    path = Path(os.path.normpath(run_dir / relative))
    return path if path != run_dir and path.is_relative_to(run_dir) else None


def remove_inside(run_dir: Path, path: Path) -> bool:
    """Remove a file of the run's directory without following a link anywhere: each directory from the run's down is
    opened with `O_NOFOLLOW`, and the file is unlinked in the last one (an unlink never follows its last name). False
    when a directory on the way is a link or not a directory, so nothing outside the run is ever removed; True when
    the file is gone or was never there."""
    names = path.relative_to(run_dir).parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(run_dir, flags)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        for name in names[:-1]:
            inner = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = inner
        os.unlink(names[-1], dir_fd=fd)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def _reap_one(node: NodeOps, work_root: Path, document: Mapping[str, Any]) -> int:
    sid, killed = str(document["sid"]), 0
    run_dir = work_root / sid
    for relative in document.get("token_files", []):  # first, before any step that can fail: a secret is never left
        path = inside(run_dir, relative)  # for a later reap (O60: the mount's token, when the unmount fails)
        if path is not None and not remove_inside(run_dir, path):
            log.error("a token file of a dead driver's run is under a link or no directory: not removed")
    for helper in document.get("helpers", []):
        if node.signal(int(helper["pid"]), str(helper["start"]), signal.SIGKILL) is True:
            killed += 1
    tagged = _tagged(node, sid)
    if tagged is None:
        raise SandboxError("the node's processes could not be listed")
    killed += sum(1 for p in tagged if node.signal(p.pid, p.start, signal.SIGKILL) is True)
    states = [node.alive(int(h["pid"]), str(h["start"])) for h in document.get("helpers", [])]
    left = _tagged(node, sid)
    if any(state is not False for state in states) or left is None or left:  # unknown is never gone
        raise SandboxError("a dead driver's helper or cell is not shown gone")
    if os.path.lexists(image_path(run_dir)):  # a FUSE server of its work disk, say (O71): never left holding it
        end_holders(node, image_path(run_dir))
    _unmount_all(node, run_dir)  # a dead oneclient's FUSE mount, say (hook c): never removed from under a mount
    remove_tree(run_dir)
    drop(work_root, sid)
    return killed


class ApptainerProbe:
    """The `ResourceProbe` half that proves a key's sandbox gone on this backend (the module's docstring)."""

    def __init__(
        self,
        node: NodeOps,
        work_root: Path,
        *,
        holder_gone: Callable[[str], bool | None] = process_gone,
        starting: Callable[[str], bool] = lambda key: False,
    ) -> None:
        self._node, self._root, self._holder_gone, self._starting = node, work_root, holder_gone, starting

    def holder_gone(self, owner: str) -> bool | None:
        return self._holder_gone(owner)

    def resource_gone(self, key: str, note: Mapping[str, Any]) -> bool:
        """True only when nothing started with this key is left; False on anything else, a failed read included."""
        if self._starting(key):
            return False
        records = read_all(self._root)
        if records is None or any(r.get("lease") == key for r in records):
            return False
        pattern = re.compile(re.escape(key) + r"-[0-9a-f]{8}")
        try:
            if any(pattern.fullmatch(entry.name) for entry in os.scandir(self._root)):
                return False
        except OSError:
            return False
        found = self._node.processes((f"{RUN_TAG}={key}-",))
        return found is not None and not any(p.tags for p in found)


__all__ = ["ApptainerProbe", "read_all", "reap", "record_path", "remove_tree", "write"]
