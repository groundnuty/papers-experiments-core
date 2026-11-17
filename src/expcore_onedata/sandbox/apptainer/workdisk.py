# Provenance: new
"""`/work` as a file system of its own on the Apptainer backend: an ext3 image of `work_disk_bytes` (O71).

Docker's `/work` is a loop-mounted ext4 disk of `work_disk_bytes` (`hostops.make_quota_dir`): a write past it fails
with ENOSPC and nothing is killed. A batch node gives an unprivileged user no loop mount and no root. So with
`[sandbox.apptainer] work_disk = "image"` the run makes an ext3 image file, and every cell binds a directory of it at
`/work` (`bind`: `-B <image>:/work:image-src=/work`). Apptainer's documentation gives that form for a directory of an
image and says an ext3 image is mounted read/write. Apptainer mounts it itself: with the kernel's driver where the
site's `apptainer.conf` allows `setuid-mount extfs`, with fuse2fs otherwise (its documentation says so; the node
decides, and the start's proof below is what is relied on).

* **The image** (`make`): `<run>/work/disk.img`, sparse, of `work_disk_bytes` rounded down to 2 MiB, so the file's
  allocated size, which the watch still walks against the cap, never reads over it (huge pages included). The node's
  `mkfs.ext3 -d` lays it out from a tree holding one directory, `work`, of the driver's uid, and that directory is what
  the cell sees as `/work`: the driver's, as Docker gives its disk's root to the agent. The image's root and its
  `lost+found` (root's, 0700, so the uid could not list them) stay outside the cell's `/work`.
* **The proof** (`PROBE`, `judge`, `CHECK`, `judge_check`): before the agent's first cell, a cell writes zeros into
  `/work` until a write fails, at most one chunk past the image's size. The start goes on only when the write failed
  with ENOSPC, past half the image's size and at most at its size; anything else refuses it: no bound, a bound that
  came only at `fsync` (the write itself reported success), another error. The probe allocates the whole image on the
  node's tmpfs, and the blocks ext3 frees stay allocated in the file, so the image is then made again by the same
  command, the probed one's memory given back. Before that, the probe leaves a small file (`COPY_PROBE`), and the
  collection's own path (`collect`, below) must copy it out byte for byte, or the start fails: a run whose outputs
  could not be collected is never paid for. A last cell shows the image made again a file system of its own (not
  `/tmp`'s) of at most its size, and names its mount type. The fill runs once per driver per boot of the node per
  configuration: its proof is kept (`workproof.py`), and a later start checks the invariants instead (the copy, the
  image made again, its superblock as the host reads it, and the size and mount type a cell sees).
* **The collection** (`ImageCopyHost`, `collect`): the host cannot read an ext3 image without privilege, so
  `copyout.copy_tree` runs as for Docker, with its copy helper in a one-shot exec of the image that binds the run's
  disk at `/work` and a staging directory of the run's at `EXPORT`. The listing is the image's own, so every refusal
  is Docker's (a link, a special file, a hard link out of the output, another file system, more than the work disk
  holds), and nothing is copied before they are checked. Each exec mounts the image anew, so the copy's device may
  not be the listing's: the helper is given the listing's device and compares each object with the device the root
  has in its own exec (`WRAPPER`), its inode, kind and size unchanged. Each exec runs after the image's identity is
  checked (O13) and binds no preload. It carries a tag of its own (`COPY_TAG`), never the run's: the watch, which
  kills every process carrying the run's tag past a bound, would take the harness's copy for the agent's when the
  agent's last cell left `/tmp` past its size, and fail the collection.
* **What holds it** (`end_holders`): at the teardown and in a reaper, every process of the uid whose command line
  names the image, or that carries the run's `COPY_TAG`, is killed and shown gone, or the step fails: a cell's
  `apptainer` that was given up on, a FUSE server Apptainer left, a copy whose driver died. Its mount is in the cell's
  mount namespace, never under the run's directory.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import shutil
import signal
import struct
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox import copyout
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.apptainer.node import NodeOps
from expcore_onedata.sandbox.hostops import CmdResult, describe_stderr

log = logging.getLogger(__name__)

IMAGE_NAME = "disk.img"  # in the run's `work/`, which the watch walks
TREE = "/work"  # the image's directory every cell binds at /work
CELL_WORK = "/work"
EXPORT = "/expcore-export"  # where a collection's exec binds the run's staging directory
COPY_TAG = "EXPCORE_WORK_COPY"  # a collection exec's environment: `<COPY_TAG>=<sid>`, which the watch never matches
ALIGN = 2 << 20  # the image's size is a multiple of this: a huge page of tmpfs, so its allocation stays within it
MKFS_S = 300.0
VERSION_S = 20.0
PROBE_BASE_S = 60.0
PROBE_RATE = 16 << 20  # bytes a second the probe's timeout allows for its fill, past its base
PROBE_CHUNK = 1 << 20
GONE_WAIT_S, GONE_POLL_S = 10.0, 0.1
PROBE_HEAD = "# expcore work disk probe (O71)\n"
CHECK_HEAD = "# expcore work disk check (O71)\n"
COPY_PROBE = ".expcore-copy-probe"  # the file the probe leaves in /work for the start's copy out of the image
COPY_MARK = b"expcore work disk copy probe (O71)\n"
PROBE = PROBE_HEAD + (
    "import errno, os\n"
    "path, limit, chunk = '/work/.expcore-disk-probe', {limit}, bytes({chunk})\n"
    "written, verdict = 0, 'unbounded'\n"
    "fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)\n"
    "try:\n"
    "    while written < limit:\n"
    "        try:\n"
    "            written += os.write(fd, chunk)\n"
    "        except OSError as e:\n"
    "            verdict = 'enospc' if e.errno == errno.ENOSPC else 'errno-%d' % e.errno\n"
    "            break\n"
    "    if verdict == 'unbounded':\n"
    "        try:\n"
    "            os.fsync(fd)\n"
    "        except OSError as e:\n"
    "            verdict = 'deferred-%d' % e.errno\n"
    "finally:\n"
    "    os.close(fd)\n"
    "    os.unlink(path)\n"
    "with open('/work/{copy}', 'xb') as handle:\n"
    "    handle.write({mark!r})\n"
    "print(verdict, written)\n"
)
CHECK = CHECK_HEAD + (
    "import os\n"
    "fs = os.statvfs('/work')\n"
    "kind = '?'\n"
    "try:\n"
    "    for line in open('/proc/self/mountinfo', encoding='utf-8', errors='replace'):\n"
    "        head, sep, tail = line.partition(' - ')\n"
    "        if sep and head.split()[4] == '/work':\n"
    "            kind = tail.split()[0]\n"
    "except (OSError, IndexError):\n"
    "    kind = '?'\n"
    "print(fs.f_blocks * fs.f_frsize, int(os.stat('/work').st_dev != os.stat('/tmp').st_dev), kind)\n"
)
MARK_HEAD = "# expcore work disk copy mark (O71)\n"
MARK = MARK_HEAD + "with open('/work/{copy}', 'xb') as handle:\n    handle.write({mark!r})\nprint('marked')\n"
SUPERBLOCK_AT, EXT_MAGIC = 1024, 0xEF53
FSTYPE = re.compile(r"[A-Za-z0-9._+-]{1,64}")
_WRAPPER_BODY = (
    "import io, json, os, sys\n"
    "request = json.loads(sys.stdin.read())\n"
    "listed = request.pop('listed_device', None)\n"
    "if listed is not None:\n"
    "    fd = os.open(request['root'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)\n"
    "    try:\n"
    "        here = os.fstat(fd).st_dev\n"
    "    finally:\n"
    "        os.close(fd)\n"
    "    request['ancestors'] = [[here if a[0] == listed else a[0], *a[1:]] for a in request['ancestors']]\n"
    "    request['expect'] = [[here if e[0] == listed else e[0], *e[1:]] for e in request['expect']]\n"
    "sys.stdin = io.StringIO(json.dumps(request))\n"
    "exec(compile(HELPER, 'copyhelper.py', 'exec'), {'__name__': '__main__'})\n"
)


def image_path(run_dir: Path) -> Path:
    return run_dir / "work" / IMAGE_NAME


def image_size(work_disk_bytes: int) -> int:
    """The image's size for a work disk of `work_disk_bytes`: rounded down to `ALIGN`, never over it."""
    return work_disk_bytes // ALIGN * ALIGN


def bind(run_dir: Path) -> AV.Bind:
    """Every cell's `/work`: the image's `work` directory, read/write."""
    return AV.Bind(image_path(run_dir), CELL_WORK, True, image_src=TREE)


def make(node: NodeOps, run_dir: Path, size: int, mkfs: Sequence[str]) -> None:
    """Make the run's image afresh (the module's docstring): a sparse file of `size` bytes laid out by `mkfs -d` from a
    tree of one directory of the driver's; `SandboxError` naming what failed, never a path."""
    image, tree = image_path(run_dir), run_dir / "disk-tree"
    image.unlink(missing_ok=True)
    if os.path.lexists(tree):
        shutil.rmtree(tree)
    tree.mkdir(mode=0o700)
    (tree / TREE.strip("/")).mkdir()
    (tree / TREE.strip("/")).chmod(0o755)
    descriptor = os.open(image, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.ftruncate(descriptor, size)
    finally:
        os.close(descriptor)
    try:
        done = node.run([*mkfs, "-q", "-F", "-d", str(tree), str(image)], MKFS_S)
    finally:
        shutil.rmtree(tree)
    if done.returncode != 0:
        log.error("mkfs of the work disk's image failed (%s): %s", done.returncode, describe_stderr(done.stderr))
        raise SandboxError(f"the work disk's image could not be made (mkfs exit {done.returncode})")


def mkfs_version(node: NodeOps, mkfs: Sequence[str]) -> str | None:
    """The first line `mkfs -V` writes (e2fsprogs' version), for the run's record; None when it says none."""
    done = node.run([*mkfs, "-V"], VERSION_S)
    lines = (done.stderr + done.stdout).strip().splitlines()
    return lines[0][:80] if done.returncode == 0 and lines else None


def probe_source(size: int) -> str:
    """The probe's program for an image of `size` bytes: it writes at most one chunk past it."""
    return PROBE.format(limit=size + PROBE_CHUNK, chunk=PROBE_CHUNK, copy=COPY_PROBE, mark=COPY_MARK)


def probe_timeout_s(size: int) -> float:
    return PROBE_BASE_S + size / PROBE_RATE


def judge(stdout: str, exit_code: int, size: int) -> int | None:
    """The bytes `/work` held when a write was refused with ENOSPC, or None when the probe did not show that bound at
    the size: only `enospc <bytes>`, its exit 0, the bytes past half the size and at most the size, is a proof."""
    words = stdout.split()
    if exit_code != 0 or len(words) != 2 or words[0] != "enospc" or not words[1].isdigit():
        return None
    held = int(words[1])
    return held if size // 2 < held <= size else None


def judge_check(stdout: str, exit_code: int, size: int) -> tuple[int, str | None] | None:
    """The size of the file system at `/work` and its mount type (None when the cell could not name it), when it is
    one of its own (not `/tmp`'s) past half of `size` and at most `size`; None otherwise."""
    words = stdout.split()
    if exit_code != 0 or len(words) != 3 or not words[0].isdigit() or words[1] != "1":
        return None
    if words[2] != "?" and not FSTYPE.fullmatch(words[2]):
        return None
    total = int(words[0])
    return (total, None if words[2] == "?" else words[2]) if size // 2 < total <= size else None


def mark_source() -> str:
    """A cell's program that leaves the copy probe's file in `/work` without a fill (a start that relies on a kept
    proof, `workproof.py`)."""
    return MARK.format(copy=COPY_PROBE, mark=COPY_MARK)


def superblock(image: Path) -> dict[str, Any] | None:
    """The image's ext superblock as the host reads it, its file never written: its block size, its size in bytes and
    its feature words (compat, incompat, ro_compat); None when it is no ext file system (its magic) or cannot be
    read. Offsets as dumpe2fs shows them (checked on e2fsprogs 1.47.0)."""
    try:
        with open(image, "rb") as handle:
            handle.seek(SUPERBLOCK_AT)
            raw = handle.read(1024)
    except OSError:
        return None
    if len(raw) < 1024 or struct.unpack_from("<H", raw, 56)[0] != EXT_MAGIC:
        return None
    count, log_size = struct.unpack_from("<I", raw, 4)[0], struct.unpack_from("<I", raw, 24)[0]
    if log_size > 6:
        return None
    block = 1024 << log_size
    return {"block_bytes": block, "bytes": count * block, "features": list(struct.unpack_from("<III", raw, 92))}


@functools.cache
def wrapper_source() -> str:
    """The copy helper (`copyhelper.py`, unchanged) behind a few lines that move the listing's device to the one the
    root has in this exec (the module's docstring); run as `python -I -c <source> list|copy`, its request on stdin."""
    return f"HELPER = {copyout.HELPER.read_text(encoding='utf-8')!r}\n{_WRAPPER_BODY}"


def listed_device(listing: str) -> int | None:
    """The device of a listing's root (its first record), None when it does not show one."""
    try:
        return int(listing.split("\0", 1)[0].split(" ", 5)[2].split(":", 1)[0])
    except (IndexError, ValueError):
        return None


class ImageCopyHost:
    """`copyout.copy_tree`'s host for the image (the module's docstring): its copy helper in a one-shot exec of the
    image (`copy_helper`); every other command, the removal of a refused copy, on the node as the driver (`run`)."""

    def __init__(
        self,
        node: NodeOps,
        *,
        argv_of: Callable[[Sequence[str]], list[str]],
        python: str,
        before_exec: Callable[[], None],
        image: Path,
        staging: Path,
    ) -> None:
        self._node, self._argv_of, self._python, self._before_exec = node, argv_of, python, before_exec
        self._image, self._staging = image, staging
        self._listed: int | None = None

    def exists(self, path: str | Path) -> bool:
        return os.path.exists(self._image) if str(path) == CELL_WORK else os.path.exists(path)

    def run(self, argv: Sequence[str], timeout_s: float, *, sudo: bool = False, input: str | None = None) -> CmdResult:
        return self._node.run(argv, timeout_s, input=input)

    def copy_helper(self, operation: str, request: dict, timeout_s: float) -> CmdResult:
        """Run the copy helper's `operation` in an exec of the image: a copy's destination, a path of the staging
        directory, is named as the exec binds it, and the listing's device goes with it."""
        self._before_exec()
        if operation == "copy":
            dest = Path(request["dest"])
            if not dest.is_relative_to(self._staging):
                raise SandboxError("a copy out of the work disk must go to the run's staging directory")
            request = {**request, "dest": str(Path(EXPORT) / dest.relative_to(self._staging)),
                       "listed_device": self._listed}  # fmt: skip
        argv = self._argv_of([self._python, "-I", "-c", wrapper_source(), operation])
        done = self._node.apptainer(argv, timeout_s, input=json.dumps(request))
        if operation == "list":
            self._listed = listed_device(done.stdout) if done.returncode == 0 else None
        return done


def collect(
    host: ImageCopyHost, staging: Path, dest: Path, outputs: Sequence[str], *, max_bytes: int
) -> dict[str, dict[str, int] | None]:
    """Copy the named outputs out of the image into `staging/work` (`copyout.copy_tree` through `host`), then move each
    one copied to its place under `dest`, as Docker's collection lays them out; raises as `copy_tree` does
    (`UnsafeOutput` before anything is copied)."""
    tree = staging / "work"
    copied = copyout.copy_tree(host, Path(CELL_WORK), [(rel, tree / rel) for rel in outputs], max_bytes=max_bytes)
    tops: list[tuple[str, ...]] = []
    for parts in sorted({copyout.output_parts(rel) for rel in outputs if copied[rel] is not None}, key=len):
        if not any(parts[: len(top)] == top for top in tops):  # one inside another came with it
            tops.append(parts)
    for parts in tops:
        target = dest.joinpath(*parts)
        if os.path.lexists(target):
            raise SandboxError("a copy out of the sandbox would be merged into what its destination already holds")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(tree.joinpath(*parts), target)
    return copied


def copy_env(sid: str) -> dict[str, str]:
    """A collection exec's environment: its own tag (the module's docstring)."""
    return {COPY_TAG: sid}


def end_holders(
    node: NodeOps,
    image: Path,
    sid: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Kill every process of the uid whose command line names `image` or that carries the run's `COPY_TAG`, listing
    again until a fresh listing shows none (one may start another while it is killed), within `GONE_WAIT_S`: how many
    were found. A process once found is waited for until it is shown gone, whatever a later listing can read of its
    command line; a zombie holds nothing and is left to its parent. `SandboxError` when they cannot be listed or one is
    not shown gone at the deadline (unknown is never gone)."""
    tags = {str(image), f"{COPY_TAG}={sid}"}
    until, found = clock() + GONE_WAIT_S, {}
    while True:
        listing = node.processes(tuple(tags))
        if listing is None:
            raise SandboxError("the processes that may hold the run's work disk could not be listed")
        listed = {(p.pid, p.start): p for p in listing}
        found |= {(p.pid, p.start): p for p in listing if p.tags & tags and p.state != "Z"}
        left = [
            proc for key, proc in found.items()
            if (key in listed and listed[key].state != "Z") or (key not in listed and node.alive(*key) is not False)
        ]  # fmt: skip
        if not left:
            return len(found)
        if clock() >= until:
            raise SandboxError("a process that holds the run's work disk could not be shown gone")
        for proc in left:
            node.signal(proc.pid, proc.start, signal.SIGKILL)
        sleep(GONE_POLL_S)


__all__ = [
    "CHECK",
    "CHECK_HEAD",
    "COPY_MARK",
    "COPY_PROBE",
    "COPY_TAG",
    "EXPORT",
    "IMAGE_NAME",
    "MARK_HEAD",
    "PROBE",
    "PROBE_CHUNK",
    "PROBE_HEAD",
    "ImageCopyHost",
    "bind",
    "collect",
    "copy_env",
    "end_holders",
    "image_path",
    "image_size",
    "judge",
    "judge_check",
    "make",
    "mark_source",
    "mkfs_version",
    "probe_source",
    "probe_timeout_s",
    "superblock",
    "wrapper_source",
]
