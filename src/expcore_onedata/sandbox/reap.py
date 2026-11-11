# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/reap.py, blob f307e1314b01cdf4baea5e32794405139e8bbb77
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see the history of this file from its import commit
"""Reap what a dead process left of one run's sandbox: containers and networks (netpool.reap_orphans), then what only
this does, its loop mounts and directory. The one part a start runs for itself is `reap_token_files`: the files of the
data-path token that a killed run left, which nothing else removes until its directory is reaped.

Called by an operator tool, or by whatever frees a lease key whose last holder died without releasing it, so that a dead
run's cell never keeps reading under a user that is now another run's. No run output is deleted without the operator's
word, and a dead attempt's disks hold its only record: the files the caller names (`outputs`, relative to the working
directory) and the gate log are copied out first and the copy checked by file count and bytes, then what goes is logged,
and only then do the mounts and scratch go. The log and the record name a directory by its id and the SHA-256 of its
name, and a mount by its place in the directory, never by a configured value (O12). A copy or a log that fails keeps the disks, as does anything still there (a
FUSE mount that could not be released keeps its containers, by design), and the caller tries again.

Every host effect goes through the `HostOps` passed in.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import time
from collections.abc import Sequence
from pathlib import Path

from expcore_onedata.sandbox import copyout, hostops, netpool
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.hostops import HostOps, describe_error, describe_stderr
from expcore_onedata.sandbox.settings import SandboxSettings

log = logging.getLogger(__name__)

REAPED = "reaped.jsonl"  # what was removed, written before it is removed
YOUNG_S = 120.0  # a directory this new, with no container, may be a live start's: not touched yet
TOKEN_FILE_GRACE_S = 600.0  # several times what a healthy start holds its file for: a docker call and the mount's wait
SID_DIRECTORY = re.compile(r".+-[0-9a-f]{8}")  # the name of a run's directory: its key, a dash, the eight hex of its id


class CollectFailed(SandboxError):
    """A kept copy of an attempt's evidence could not be made or checked; its message names no configured value."""


def sid_pattern(key: str) -> re.Pattern[str]:
    """The names of the sandbox directories of a lease key or a run key, as `DockerSandbox` names them (key, id)."""
    return re.compile(re.escape(re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:48]) + r"-[0-9a-f]{8}")


def _named(x: Path) -> dict[str, str]:
    """A sandbox directory as a log or a record names it (O12): its name holds the lease key or run key and its path the
    work root, both configured values, so by the id that ends its name and the SHA-256 of the name, to match on."""
    return {"id": x.name.rsplit("-", 1)[-1], "sha256": hashlib.sha256(x.name.encode("utf-8", "replace")).hexdigest()}


def mounts_under(host: HostOps, x: Path) -> list[Path]:
    """Mount points inside a sandbox directory, deepest first."""
    return [Path(m) for m in hostops.mounts_under(host.read_text("/proc/self/mountinfo"), x)]


def _sudo(host: HostOps, *argv: str, timeout_s: float):
    return host.run(list(argv), timeout_s, sudo=True)


def _kept(what: str, copied: dict[str, int] | None) -> dict:
    """An entry of the record: what was copied, its files and bytes, or that it was absent."""
    return {"what": what, "absent": True} if copied is None else {"what": what, **copied}


def collect(host: HostOps, x: Path, keep: Path, outputs: Sequence[str], *, max_bytes: int | None = None) -> list[dict]:
    """What a killed attempt did, into its record: its disk mounted if it no longer is (after a reboot), then the named
    outputs and the gate log, copied as root under the rule of `copyout.py` (O19) and checked file by file against the
    listing. An output or log the attempt never got to is absent, which is not a failure; one holding a link, a special
    file or more bytes than the working disk (`max_bytes`, else the size of its disk image) refuses the collection, so
    the disks stay for an operator."""
    work = x / "work"
    image = x / "work.img"
    if image.exists() and work not in set(mounts_under(host, x)):
        work.mkdir(exist_ok=True)
        r = _sudo(host, "mount", "-o", "loop,ro", str(image), str(work), timeout_s=60.0)
        if r.returncode != 0:
            raise CollectFailed(f"mounting the working disk: {describe_stderr(r.stderr)}")
    if max_bytes is None and image.exists():
        max_bytes = image.stat().st_size  # the disk's own size: what an output on it can hold, unless it is sparse
    copied = copyout.copy_tree(host, work, [(rel, keep / "work" / rel) for rel in outputs], max_bytes=max_bytes)
    log_copy = copyout.copy_tree(host, x / "gatelog", [(".", keep / "gatelog")], max_bytes=None)  # the gate's own
    return [*(_kept(rel, copied[rel]) for rel in outputs), _kept("gate log", log_copy["."])]


def reap_dirs(
    host: HostOps,
    log_dir: Path,
    root: Path,
    names: re.Pattern[str],
    dir_label: str,
    outputs: Sequence[str] = (),
    young_s: float = YOUNG_S,
    *,
    max_bytes: int | None = None,
) -> bool:
    """True when no directory of these names is left. Only with no container of them left, and none younger than
    `young_s` (nothing yet shows whose it is). `max_bytes` bounds what an output may hold (see `collect`)."""
    try:
        dirs = [x for x in Path(root).iterdir() if names.fullmatch(x.name) and x.is_dir()]
    except OSError as e:  # a root that is missing (an unmounted volume, a wrong path): "nothing there" is not "gone"
        log.error("reap: the sandbox root cannot be read, so nothing is shown gone: %s", e.strerror)
        return False
    now = time.time()
    for x in dirs:
        ps = host.docker("ps", "-a", "-q", "--filter", f"label={dir_label}={x}", check=False)
        if ps.returncode != 0:  # a docker that did not answer lists no containers: that is not "none left"
            named = _named(x)
            log.error(
                "reap: docker ps failed (%s), so directory %s (sha256 %s) is not shown clear of containers",
                ps.returncode,
                named["id"],
                named["sha256"],
            )
            return False
        if ps.stdout.split() or now - x.stat().st_mtime < young_s:
            return False
    return all(_reap_one(host, log_dir, x, outputs, max_bytes) for x in dirs)


def _reap_one(host: HostOps, log_dir: Path, x: Path, outputs: Sequence[str], max_bytes: int | None) -> bool:
    keep = Path(log_dir) / "reaped" / x.name
    try:
        keep.mkdir(parents=True, exist_ok=True)
        kept = collect(host, x, keep, outputs, max_bytes=max_bytes)
        sizes = {f.name: f.stat().st_blocks * 512 for f in x.iterdir() if f.is_file() and not f.is_symlink()}
        with open(Path(log_dir) / REAPED, "a") as f:
            record = {"t": time.time(), "dir": _named(x), "kept": kept, "allocated_bytes": sizes}
            mounts = [str(m.relative_to(x)) for m in mounts_under(host, x)]  # by their place in it, never the path
            f.write(json.dumps(record | {"mounts": mounts}) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except (OSError, SandboxError) as e:
        log.error("reap: a run directory was not collected, so its disks stay: %s", describe_error(e))
        return False
    try:
        for m in mounts_under(host, x):
            host.unmount(m)
        _sudo(host, "rm", "-rf", "--one-file-system", str(x), timeout_s=300.0)
    except Exception as e:
        log.error("reap: a run directory could not be handled: %s", describe_error(e))
        return False
    return not x.exists()  # verified, not assumed: a mount left behind keeps the directory


def reap_token_files(
    host: HostOps, root: Path, relatives: Sequence[Path], *, grace_s: float = TOKEN_FILE_GRACE_S
) -> dict[str, int]:
    """Remove the files of the data-path token that a run killed outright left on the host: it wrote one and died before
    it removed it (`DockerSandbox._write_token_file`), and nothing else removes it until `reap_run` reaches its directory.

    Only a regular file at one of `relatives` inside a directory of `root` that is named as a sandbox names its own
    (key, dash, eight hex digits), and only once it is `grace_s` old, several times what a healthy start keeps its
    file for (one docker call and the mount's or the gate's wait): a younger one may be a start in another process
    that has not yet run its container, and removing the file of a start that did stall past the grace makes that
    start fail, never pass on a missing token. A link is never followed (`lstat`), a file anywhere else is not touched. Returns the
    count removed and the count found and not removed, which is all a record may say of them (O12)."""
    removed = left = 0
    if not Path(root).is_dir():  # a root nothing has run on yet
        return {"removed": 0, "left": 0}
    cutoff = time.time() - grace_s
    for run_dir in sorted(Path(root).iterdir()):
        if not SID_DIRECTORY.fullmatch(run_dir.name) or run_dir.is_symlink() or not run_dir.is_dir():
            continue
        for relative in relatives:
            path = run_dir / relative
            try:
                facts = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as e:  # one that cannot be examined is not shown gone
                log.error("reap: a token file could not be examined: %s", describe_error(e))
                left += 1
                continue
            if not stat.S_ISREG(facts.st_mode) or facts.st_mtime > cutoff:
                continue
            _sudo(host, "rm", "-f", str(path), timeout_s=20.0)
            if os.path.lexists(path):  # verified, not assumed
                named = _named(run_dir)
                log.error(
                    "reap: a token file of directory %s (sha256 %s) could not be removed", named["id"], named["sha256"]
                )
                left += 1
            else:
                removed += 1
    return {"removed": removed, "left": left}


def reap_run(
    host: HostOps,
    settings: SandboxSettings,
    key: str,
    *,
    log_dir: Path,
    outputs: Sequence[str] = (),
    young_s: float = YOUNG_S,
) -> bool:
    """The whole of it for one lease key or run key: its containers and networks, then its mounts and directories.
    True when shown gone."""
    labels = netpool.Labels(settings.label_namespace)
    netpool.reap_orphans(host, labels)
    return reap_dirs(
        host,
        log_dir,
        settings.work_root,
        sid_pattern(key),
        labels.dir,
        outputs,
        young_s,
        max_bytes=settings.limits.work_disk_bytes,
    )
