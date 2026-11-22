"""A host's `/proc` in memory, for the mount watcher's proofs (`expcore_onedata.sandbox.mountproof.ProcView`).

Every device, path and pid is invented. A process is its `stat`, `status` (uid and `NSpid`), `mountinfo` and PID
namespace links (PID, and mount namespace and root where a test sets them); a mount table is built from `(dev, root,
mountpoint, fstype)` rows, with the root mount first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

UID = 1000
HOST_ROOT = ("8:1", "/", "/", "ext4")
HOST_PROC = ("0:20", "/", "/proc", "proc")
HOST_TMP = ("0:30", "/", "/tmp", "tmpfs")
FUSE_DEV = "0:55"
HOST_MOUNTPOINT = "/tmp/run/mount"
CELL_PATH = "/data"


def table(*rows: tuple[str, str, str, str]) -> str:
    """A mountinfo text of these rows."""
    return "".join(
        f"{n} 1 {dev} {root} {point} rw shared:{n} - {fstype} {fstype} rw\n"
        for n, (dev, root, point, fstype) in enumerate(rows, 20)
    )


HOST_TABLE = table(HOST_ROOT, HOST_PROC, HOST_TMP, (FUSE_DEV, "/", HOST_MOUNTPOINT, "fuse.oneclient"))


def cell_table(*extra: tuple[str, str, str, str]) -> str:
    """A cell's mount table: its image, its own `/proc`, the run's mount at the cell's path, its `/work`."""
    return table(
        ("0:70", "/", "/", "overlay"),
        ("0:71", "/", "/proc", "proc"),
        (FUSE_DEV, "/", CELL_PATH, "fuse.oneclient"),
        ("0:30", "/run/work", "/work", "tmpfs"),
        *extra,
    )


@dataclass
class P:
    pid: int
    start: int
    nspid: tuple[int, ...]
    mountinfo: str = HOST_TABLE
    uid: int = UID
    ns: str | None = "pid:[4026531836]"
    tracer: int = 0
    unreadable_mounts: bool = False
    mnt_ns: str | None = None  # `ns/mnt`; None is a link that cannot be read
    root: str | None = None  # `root`; None likewise


@dataclass
class FakeProc:
    procs: dict[int, P] = field(default_factory=dict)
    listing_fails: bool = False
    own_mnt_ns: str | None = None  # the watcher's own `/proc/self/ns/mnt`; None cannot be read

    def add(self, *procs: P) -> FakeProc:
        for proc in procs:
            self.procs[proc.pid] = proc
        return self

    def pids(self):
        if self.listing_fails:
            raise PermissionError("listing refused")
        return list(self.procs)

    def _proc(self, path: str) -> tuple[P, str]:
        parts = path.split("/")
        proc = self.procs.get(int(parts[2]))
        if proc is None:
            raise FileNotFoundError(path)
        return proc, "/".join(parts[3:])

    def read(self, path: str) -> str:
        proc, what = self._proc(path)
        if what == "stat":
            return f"{proc.pid} (a (b) c) S 1 " + " ".join(["0"] * 17) + f" {proc.start} 0 0\n"
        if what == "status":
            ns, uid = "\t".join(str(v) for v in proc.nspid), "\t".join([str(proc.uid)] * 4)
            return f"Name:\tx\nUid:\t{uid}\nTracerPid:\t{proc.tracer}\nNSpid:\t{ns}\n"
        if what == "mountinfo":
            if proc.unreadable_mounts:
                raise PermissionError(path)
            return proc.mountinfo
        raise FileNotFoundError(path)

    def readlink(self, path: str) -> str:
        if path == "/proc/self/ns/mnt":
            if self.own_mnt_ns is None:
                raise PermissionError(path)
            return self.own_mnt_ns
        proc, what = self._proc(path)
        link = {"ns/pid": proc.ns, "ns/mnt": proc.mnt_ns, "root": proc.root}.get(what)
        if link is None:
            raise PermissionError(path)
        return link


def host_process(pid: int, **kw) -> P:
    return P(pid, 1000 + pid, (pid,), **kw)


def cell_process(pid: int, inner: int = 1, *, ns: str = "pid:[4026532100]", mountinfo: str | None = None, **kw) -> P:
    return P(pid, 1000 + pid, (pid, inner), mountinfo=cell_table() if mountinfo is None else mountinfo, ns=ns, **kw)
