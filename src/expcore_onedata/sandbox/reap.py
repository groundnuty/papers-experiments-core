# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/reap.py, blob f307e1314b01cdf4baea5e32794405139e8bbb77
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Reap what a dead process left of one run's sandbox: containers and networks (netpool.reap_orphans), then what only this does, its loop mounts and directory.

Called from two places, so that a dead run's cell never keeps reading fr under a slot user that is now another run's: the slot-user lease, before it lets a
key go to a new holder when the last holder died without releasing it (dataset_76.harness.slot_lease, on either host), and the remote host's reaper, as soon as a
worker's death is seen (dataset_76.campaign.remote). No run output is deleted without the operator's word, and a dead attempt's disks hold its only record (the lead, 2 Nov;
lane-common): the attempt's output dir, trace and gate log are copied out first and the copy checked by file count and bytes, then what goes is logged, and only then do
the mounts and scratch go. A copy or a log that fails keeps the disks, as does anything still there (a FUSE mount that could not be released keeps its containers, by
design), and the caller tries again: the run stays counted, and the status alarms."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


REAPED = "remote_reaped.jsonl"    # what was removed, written before it is removed
YOUNG_S = 120.0                   # a directory this new, with no container, may be a live start's: not touched yet


def sid_prefix(run_id_key: str) -> str:
    """The sandbox directory name's prefix for a run's key (`RunId.key()`, or a scripted plan's), as dataset_76.sandbox.Sandbox names it."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", run_id_key)[:60] + "-"


def work_root() -> Path:
    """Where `Sandbox` puts its directories: `SandboxConfig.work_root`, which nothing overrides by environment (an `STUDY_VAR_88` read here sent the reaper
    to a root no sandbox lives in, where "nothing there" read as gone)."""
    from .config import SandboxConfig
    return Path(SandboxConfig.work_root)


def mounts_under(x: Path) -> list[Path]:
    """Mount points inside a sandbox directory, deepest first (/proc/self/mountinfo, its octal escapes undone)."""
    out = []
    pre = str(x) + "/"
    with open("/proc/self/mountinfo") as f:
        for line in f:
            mp = line.split()[4].encode().decode("unicode_escape")
            if mp.startswith(pre):
                out.append(Path(mp))
    return sorted(set(out), key=lambda p: len(p.parts), reverse=True)


class CollectFailed(RuntimeError):
    pass


def _facts(H: Any, path: Path, *depth: str) -> tuple[int, int]:
    """(files, bytes) under `path`, read as root: an agent's files need not be readable by us."""
    r = H.sudo_bounded("find", str(path), *depth, "-type", "f", "-printf", "%s\\n", timeout_s=120.0)
    if r.returncode != 0:
        raise CollectFailed(f"listing {path}: {str(r.stderr).strip()[:200]}")
    sizes = [int(s) for s in r.stdout.split()]
    return len(sizes), sum(sizes)


def _sudo_refused(r: Any) -> bool:
    """sudo's own failures (a password asked for, an unknown user, a broken audit plugin) exit 1 as well, the code of a `test -d` that says no, but write `sudo: ...`
    to stderr, which `test` never does. Its note that it cannot resolve the host name is on every call, and is not a refusal."""
    return any(x.startswith("sudo:") and "unable to resolve host" not in x for x in str(r.stderr).splitlines())


def _copy(H: Any, what: str, src: Path, dst: Path, *depth: str) -> dict:
    """Copy a directory's files out and check them. `depth` keeps to its top level (the gate's spool is not a record)."""
    t = H.sudo_bounded("test", "-d", str(src), timeout_s=30.0)
    if t.returncode == 1 and not _sudo_refused(t):
        return {"what": what, "absent": True}          # `test` says it is not a directory: the attempt never got that far, nothing to keep, and not a failure
    if t.returncode != 0:                              # a timeout (124), a command that could not start (127), a sudo that refused (exit 1 too): nothing is known about it
        raise CollectFailed(f"testing {src}: exit {t.returncode} {str(t.stderr).strip()[:200]}")
    dst.mkdir(parents=True, exist_ok=True)
    if depth:
        listed = H.sudo_bounded("find", str(src), *depth, "-type", "f", timeout_s=120.0)
        if listed.returncode != 0:
            raise CollectFailed(f"listing {src}: {str(listed.stderr).strip()[:200]}")
        cmds = [("cp", f, str(dst)) for f in listed.stdout.split("\n") if f]
    else:
        cmds = [("cp", "-r", f"{src}/.", str(dst))]
    for c in cmds + [("chown", "-R", f"{os.getuid()}:{os.getgid()}", str(dst))]:
        r = H.sudo_bounded(*c, timeout_s=600.0)
        if r.returncode != 0:
            raise CollectFailed(f"{c[0]} of {what}: {str(r.stderr).strip()[:200]}")
    want, got = _facts(H, src, *depth), _facts(H, dst, *depth)
    if want != got:
        raise CollectFailed(f"{what}: {want} files and bytes there, {got} copied")
    return {"what": what, "files": got[0], "bytes": got[1]}


def collect(x: Path, keep: Path, H: Any) -> list[dict]:
    """What a killed attempt did, into its record: its disks mounted if they no longer are (after a reboot), then the output dir, the trace and the gate log, each checked."""
    mounted = set(mounts_under(x))
    for img, mnt in ((x / "work.img", x / "work"), (x / "trace.img", x / "trace")):
        if img.exists() and mnt not in mounted:
            mnt.mkdir(exist_ok=True)
            r = H.sudo_bounded("mount", "-o", "loop,ro", str(img), str(mnt), timeout_s=60.0)
            if r.returncode != 0:
                raise CollectFailed(f"mounting {img.name}: {str(r.stderr).strip()[:200]}")
    return [_copy(H, "output", x / "work" / "output", keep / "output"), _copy(H, "trace", x / "trace", keep / "trace"),
            _copy(H, "gate log", x / "gatelog", keep / "gatelog", "-maxdepth", "1")]


def reap_dirs(log_dir: Path, root: Path, prefix: str, H: Any, dir_label: str, young_s: float = YOUNG_S) -> bool:
    """True when no directory of this prefix is left. Only with no container of them left, and none younger than `young_s` (nothing yet shows whose it is)."""
    try:
        dirs = [x for x in Path(root).iterdir() if x.name.startswith(prefix) and x.is_dir()]
    except OSError as e:                   # also a root that is missing (an unmounted volume, a wrong path): "nothing there" is not "gone"
        print(f"reap: the sandbox root {root} cannot be read, so nothing is shown gone: {e}", file=sys.stderr)
        return False
    now = time.time()
    for x in dirs:
        ps = H.docker("ps", "-a", "-q", "--filter", f"label={dir_label}={x}", check=False)
        if ps.returncode != 0:             # a docker that did not answer lists no containers: that is not "none left"
            print(f"reap: docker ps failed ({ps.returncode}), so {x} is not shown clear of containers: {str(ps.stderr).strip()[:200]}", file=sys.stderr)
            return False
        if ps.stdout.split() or now - x.stat().st_mtime < young_s:
            return False
    for x in dirs:
        keep = Path(log_dir) / "reaped" / x.name
        try:
            keep.mkdir(parents=True, exist_ok=True)
            kept = collect(x, keep, H)
            sizes = {f.name: f.stat().st_blocks * 512 for f in x.iterdir() if f.is_file() and not f.is_symlink()}
            with open(Path(log_dir) / REAPED, "a") as f:
                f.write(json.dumps({"t": time.time(), "dir": str(x), "kept": kept, "allocated_bytes": sizes, "mounts": [str(m) for m in mounts_under(x)]}) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except (OSError, CollectFailed) as e:
            print(f"reap: not collected, so its disks stay: {x}: {e}", file=sys.stderr)
            return False
        try:
            for m in mounts_under(x):
                H.unmount(m)
            H.sudo_bounded("rm", "-rf", "--one-file-system", str(x), timeout_s=300.0)
        except Exception as e:  # noqa: BLE001 - a umount or rm that failed or hung: not gone, tried again (they are bounded: see hostops)
            print(f"reap: {x}: {type(e).__name__}: {e}", file=sys.stderr)
            return False
        if x.exists():                     # verified, not assumed: a mount left behind keeps the directory
            return False
    return True


def reap_run(run_id_key: str, *, log_dir: Path, root: Path | None = None, young_s: float = YOUNG_S) -> bool:
    """The whole of it for one run: its containers and networks, then its mounts and directories. True when shown gone."""
    from . import hostops as H, netpool
    netpool.reap_orphans()
    return reap_dirs(log_dir, work_root() if root is None else root, sid_prefix(run_id_key), H, netpool.DIR, young_s)
