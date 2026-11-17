"""The removal of an HPC federation's root (`down`, DESIGN.md section 5), which may hold read-only directories and lie
on a shared filesystem (NFS), where a file held open while it is removed becomes a silly-rename file (`.nfsXXXX`)
until its last close, and a directory holding one is not empty (seen live). Its file calls are arguments, so the tests
give it an NFS client's unlink.
"""

import errno
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Callable, List

from fedlayout import ROOT_MARKER

SILLY = re.compile(r"\.nfs[0-9A-Fa-f]+")  # an NFS client's silly-rename file: a removed file still held open
NOT_EMPTY = (errno.ENOTEMPTY, errno.EEXIST, errno.EBUSY)  # what rmdir says of a directory still holding one
SETTLE_S = 30.0  # the longest the root's removal waits, in all, for silly-rename files to go
POLL_S = 1.0


def silly(name: str) -> bool:
    """An NFS client's silly-rename file: what a file held open becomes when it is removed, until its last close."""
    return SILLY.fullmatch(name) is not None


def only_silly(directory: Path) -> bool:
    """Whether a directory (never through a link) holds nothing but silly-rename files, in it or in directories below
    it that hold nothing else; an empty one does."""
    for entry in os.scandir(directory):
        if entry.is_dir(follow_symlinks=False):
            if not only_silly(Path(entry.path)):
                return False
        elif not silly(entry.name):
            return False
    return True


def _remove_below(root: Path, keep: Path, unlink: Callable[[Any], None], rmdir: Callable[[Any], None]) -> List[str]:
    """One pass over the tree, bottom up, never through a link: every file and link removed but `keep`, then every
    directory. Returns the silly-rename files left, leaving their directories: a silly file is never removed here, as
    removing one that a process of another host holds would pull the file from under it; its holder's last close
    removes it."""
    left: List[str] = []
    for directory, subdirectories, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            path = Path(directory) / name
            if path == keep:
                continue
            if silly(name):
                left.append(str(path))
                continue
            unlink(path)
        for name in subdirectories:
            path = Path(directory) / name
            if path.is_symlink():
                unlink(path)
                continue
            try:
                rmdir(path)
            except OSError as error:
                if error.errno not in NOT_EMPTY or not only_silly(path):
                    raise
                left += [str(Path(d) / f) for d, _, names in os.walk(path) for f in names]
    # a file of the root held open when this pass removed it is a silly file now, which the pass's listing predates
    return sorted(set(left) | {entry.path for entry in os.scandir(root) if silly(entry.name)})


def settle_rmdir(root: Path, rmdir: Callable[[Any], None], sleep: Callable[[float], None], until: float,
                  clock: Callable[[], float]) -> None:  # fmt: skip
    """`rmdir` the root, waiting while it holds nothing but silly-rename files (their holders closing them) until
    `until`; never removes anything in it. RuntimeError at the bound, OSError for a root that holds anything else."""
    while True:
        try:
            rmdir(root)
            return
        except OSError as error:
            if error.errno not in NOT_EMPTY or not only_silly(root):
                raise
        if clock() >= until:
            held = sum(1 for _, _, names in os.walk(root) for _ in names)
            raise RuntimeError(
                f"the root still holds {held} NFS silly-rename file(s) after {SETTLE_S:.0f} s: files another process "
                "holds open; run down again once they are closed"
            )
        sleep(POLL_S)


def remove_tree(
    root: Path,
    *,
    unlink: Callable[[Any], None] = os.unlink,
    rmdir: Callable[[Any], None] = os.rmdir,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    keep_root: bool = False,
    marker_name: str = ROOT_MARKER,
) -> None:
    """Remove a federation's root, which may hold read-only directories and lie on a shared filesystem (NFS).

    Each directory (never a link, never through one) is made writable by its owner first; `chmod -R` would follow a
    link given as an argument. Then everything is removed bottom up but the root's marker, which goes last, so a root
    whose removal is cut short still reads as this federation's and a second `down` finishes it. A file another process
    holds open becomes a silly-rename file (`.nfsXXXX`) that goes at its last close: the removal waits for those, up to
    `SETTLE_S` in all, and then raises RuntimeError with the marker kept. Anything else that cannot be removed raises
    OSError, as before. With `keep_root` the directory and its marker (`marker_name`) are left, for the caller to
    remove: the provider storage's removal (`hpcstorage`) passes `/proc/self/fd/<n>` of a directory it holds open and
    has proved, so what is removed is that directory's contents whatever its path now names, and retires the
    directory itself by its parent's descriptor."""
    for directory, subdirectories, _ in os.walk(root, followlinks=False):
        for name in subdirectories:
            path = Path(directory) / name
            if not path.is_symlink():
                os.chmod(path, stat.S_IMODE(path.lstat().st_mode) | stat.S_IRWXU)
    os.chmod(root, stat.S_IRWXU)
    marker, until = root / marker_name, clock() + SETTLE_S
    while True:
        left = _remove_below(root, marker, unlink, rmdir)
        if not left:
            break
        if clock() >= until:
            raise RuntimeError(
                f"the root still holds {len(left)} NFS silly-rename file(s) after {SETTLE_S:.0f} s: files another "
                "process holds open; its marker is kept, so run down again once they are closed"
            )
        sleep(POLL_S)
    if keep_root:
        return
    if marker.exists() or marker.is_symlink():
        unlink(marker)
    settle_rmdir(root, rmdir, sleep, until, clock)
