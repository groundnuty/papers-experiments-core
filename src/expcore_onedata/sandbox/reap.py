# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/reap.py, blob f307e1314b01cdf4baea5e32794405139e8bbb77
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see the history of this file from its import commit
"""Reap what a dead process left of one run's sandbox: containers and networks (netpool.reap_orphans), then what only
this does, its loop mounts and directory.

Called by an operator tool, or by whatever frees a lease key whose last holder died without releasing it, so that a dead
run's cell never keeps reading under a user that is now another run's. No run output is deleted without the operator's
word, and a dead attempt's disks hold its only record: the files the caller names (`outputs`, relative to the working
directory) and the gate log are copied out first and the copy checked by file count and bytes, then what goes is logged,
and only then do the mounts and scratch go. A copy or a log that fails keeps the disks, as does anything still there (a
FUSE mount that could not be released keeps its containers, by design), and the caller tries again.

Every host effect goes through the `HostOps` passed in.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Sequence
from pathlib import Path

from expcore_onedata.sandbox import hostops, netpool
from expcore_onedata.sandbox.hostops import HostOps
from expcore_onedata.sandbox.settings import SandboxSettings

log = logging.getLogger(__name__)

REAPED = "reaped.jsonl"  # what was removed, written before it is removed
YOUNG_S = 120.0  # a directory this new, with no container, may be a live start's: not touched yet


class CollectFailed(RuntimeError):
    pass


def sid_pattern(key: str) -> re.Pattern[str]:
    """The names of the sandbox directories of a lease key or a run key, as `DockerSandbox` names them (key, id)."""
    return re.compile(re.escape(re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:48]) + r"-[0-9a-f]{8}")


def mounts_under(host: HostOps, x: Path) -> list[Path]:
    """Mount points inside a sandbox directory, deepest first."""
    return [Path(m) for m in hostops.mounts_under(host.read_text("/proc/self/mountinfo"), x)]


def _sudo(host: HostOps, *argv: str, timeout_s: float):
    return host.run(list(argv), timeout_s, sudo=True)


def _facts(host: HostOps, path: Path) -> tuple[int, int]:
    """(files, bytes) under `path`, read as root: an agent's files need not be readable by us."""
    r = _sudo(host, "find", str(path), "-type", "f", "-printf", "%s\\n", timeout_s=120.0)
    if r.returncode != 0:
        raise CollectFailed(f"listing {path}: {str(r.stderr).strip()[:200]}")
    sizes = [int(s) for s in r.stdout.split()]
    return len(sizes), sum(sizes)


def _sudo_refused(r) -> bool:
    """sudo's own failures (a password asked for, an unknown user) exit 1 as well, the code of a `test -d` that says no,
    but write `sudo: ...` to stderr, which `test` never does."""
    return any(x.startswith("sudo:") and "unable to resolve host" not in x for x in str(r.stderr).splitlines())


def _copy(host: HostOps, what: str, src: Path, dst: Path) -> dict:
    """Copy a directory or file out as root and check it by file count and bytes."""
    t = _sudo(host, "test", "-e", str(src), timeout_s=30.0)
    if t.returncode == 1 and not _sudo_refused(t):
        return {"what": what, "absent": True}  # the attempt never got that far: nothing to keep, and not a failure
    if t.returncode != 0:  # a timeout, a command that could not start, a sudo that refused: nothing is known
        raise CollectFailed(f"testing {src}: exit {t.returncode} {str(t.stderr).strip()[:200]}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    for argv in (("cp", "-r", str(src), str(dst)), ("chown", "-R", f"{os.getuid()}:{os.getgid()}", str(dst))):
        r = _sudo(host, *argv, timeout_s=600.0)
        if r.returncode != 0:
            raise CollectFailed(f"{argv[0]} of {what}: {str(r.stderr).strip()[:200]}")
    want, got = _facts(host, src), _facts(host, dst)
    if want != got:
        raise CollectFailed(f"{what}: {want} files and bytes there, {got} copied")
    return {"what": what, "files": got[0], "bytes": got[1]}


def collect(host: HostOps, x: Path, keep: Path, outputs: Sequence[str]) -> list[dict]:
    """What a killed attempt did, into its record: its disk mounted if it no longer is (after a reboot), then the named
    outputs and the gate log, each checked."""
    work = x / "work"
    if (x / "work.img").exists() and work not in set(mounts_under(host, x)):
        work.mkdir(exist_ok=True)
        r = _sudo(host, "mount", "-o", "loop,ro", str(x / "work.img"), str(work), timeout_s=60.0)
        if r.returncode != 0:
            raise CollectFailed(f"mounting the working disk: {str(r.stderr).strip()[:200]}")
    kept = [_copy(host, rel, work / rel, keep / "work" / rel) for rel in outputs]
    return [*kept, _copy(host, "gate log", x / "gatelog", keep / "gatelog")]


def reap_dirs(
    host: HostOps,
    log_dir: Path,
    root: Path,
    names: re.Pattern[str],
    dir_label: str,
    outputs: Sequence[str] = (),
    young_s: float = YOUNG_S,
) -> bool:
    """True when no directory of these names is left. Only with no container of them left, and none younger than
    `young_s` (nothing yet shows whose it is)."""
    try:
        dirs = [x for x in Path(root).iterdir() if names.fullmatch(x.name) and x.is_dir()]
    except OSError as e:  # a root that is missing (an unmounted volume, a wrong path): "nothing there" is not "gone"
        log.error("reap: the sandbox root cannot be read, so nothing is shown gone: %s", e.strerror)
        return False
    now = time.time()
    for x in dirs:
        ps = host.docker("ps", "-a", "-q", "--filter", f"label={dir_label}={x}", check=False)
        if ps.returncode != 0:  # a docker that did not answer lists no containers: that is not "none left"
            log.error("reap: docker ps failed (%s), so %s is not shown clear of containers", ps.returncode, x.name)
            return False
        if ps.stdout.split() or now - x.stat().st_mtime < young_s:
            return False
    return all(_reap_one(host, log_dir, x, outputs) for x in dirs)


def _reap_one(host: HostOps, log_dir: Path, x: Path, outputs: Sequence[str]) -> bool:
    keep = Path(log_dir) / "reaped" / x.name
    try:
        keep.mkdir(parents=True, exist_ok=True)
        kept = collect(host, x, keep, outputs)
        sizes = {f.name: f.stat().st_blocks * 512 for f in x.iterdir() if f.is_file() and not f.is_symlink()}
        with open(Path(log_dir) / REAPED, "a") as f:
            record = {"t": time.time(), "dir": x.name, "kept": kept, "allocated_bytes": sizes}
            f.write(json.dumps(record | {"mounts": [str(m) for m in mounts_under(host, x)]}) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except (OSError, CollectFailed) as e:
        log.error("reap: not collected, so its disks stay (%s): %s", x.name, e)
        return False
    try:
        for m in mounts_under(host, x):
            host.unmount(m)
        _sudo(host, "rm", "-rf", "--one-file-system", str(x), timeout_s=300.0)
    except Exception as e:
        log.error("reap: %s: %s: %s", x.name, type(e).__name__, e)
        return False
    return not x.exists()  # verified, not assumed: a mount left behind keeps the directory


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
    return reap_dirs(host, log_dir, settings.work_root, sid_pattern(key), labels.dir, outputs, young_s)
