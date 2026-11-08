# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, scripts/run_pilot.py:2067-2096
"""One dispatcher per store (C07).

A second dispatcher on the same store would run the same runs twice and treat the first one's running attempts as stale
at its own recovery. The dispatcher takes an exclusive, non-blocking `flock` on a file beside the store before it opens
it; a second process gets `CampaignLocked` and exits. The kernel drops the lock when its holder dies, however it dies,
so a killed dispatcher leaves nothing to clean up.

`flock` is unreliable on network file systems (NFS before a recent kernel, some Ceph and FUSE mounts): two hosts may
both be granted the lock. The lock still works for processes on one host, so a campaign on such a file system starts
with a warning, and a file system that refuses `flock` altogether is refused.
"""

import fcntl
import os
import warnings
from pathlib import Path
from types import TracebackType

LOCK_NAME = "campaign.lock"
NETWORK_FS_TYPES = frozenset({"nfs", "nfs4", "ceph", "cifs", "smb3", "smbfs", "lustre"})


class CampaignLocked(RuntimeError):
    """Another dispatcher holds the lock of this store, or the file system cannot lock."""


class NetworkFilesystemWarning(UserWarning):
    """The lock file is on a network file system, where `flock` may grant the lock to two hosts."""


def filesystem_type(path: Path, mounts: Path = Path("/proc/mounts")) -> str | None:
    """Return the type of the file system holding `path` (the longest mount point above it), or None if unknown."""
    try:
        lines = mounts.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    target = os.path.realpath(path)
    best, best_type = "", None
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        point = fields[1].replace("\\040", " ")
        inside = target == point or target.startswith(point.rstrip("/") + "/")
        if inside and len(point) >= len(best):
            best, best_type = point, fields[2]
    return best_type


def is_network_fs(fs_type: str | None) -> bool:
    """Tell whether a file system type is one where `flock` is not reliable across hosts."""
    return bool(fs_type) and (fs_type in NETWORK_FS_TYPES or str(fs_type).startswith("fuse.ceph"))


class DispatchLock:
    """The exclusive lock of one store's directory; a context manager that releases it on exit.

    `mounts` is where the file system types are read from, so a test can name a table of its own.
    """

    def __init__(self, root: Path, mounts: Path = Path("/proc/mounts")) -> None:
        self.path = Path(root) / LOCK_NAME
        self._mounts = mounts
        self._fd: int | None = None

    def acquire(self) -> "DispatchLock":
        """Take the lock or raise CampaignLocked; warn when the directory is on a network file system."""
        if self._fd is not None:
            raise RuntimeError("this dispatcher already holds the lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fs_type = filesystem_type(self.path.parent, self._mounts)
        if is_network_fs(fs_type):
            warnings.warn(
                f"{self.path} is on a {fs_type} file system, where flock may grant the lock to two hosts: "
                "run one dispatcher per store, on one host",
                NetworkFilesystemWarning,
                stacklevel=2,
            )
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise CampaignLocked(f"another dispatcher holds {self.path}: one dispatcher per store") from None
        except OSError as error:
            os.close(fd)
            raise CampaignLocked(f"cannot lock {self.path}: the file system does not support flock ({error})") from None
        self._fd = fd
        return self

    def release(self) -> None:
        """Release the lock; releasing twice does nothing."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @property
    def held(self) -> bool:
        """Tell whether this object holds the lock."""
        return self._fd is not None

    def __enter__(self) -> "DispatchLock":
        return self.acquire()

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()
