"""Helpers for the tests of `expcore_onedata.sandbox.mountproof`: a host as the watcher learned it, a poll that records
the proof lines it writes, and a node of invented processes (`fake_proc`)."""

from fake_proc import FUSE_DEV, HOST_MOUNTPOINT, HOST_PROC, UID, FakeProc, host_process

from expcore_onedata.sandbox import mountproof as MP

ONECLIENT = 99
GUARD = MP.Protected("0:30", "/run/guard/token")
HOST_ROOT_MOUNT = ("8:1", "/")  # `fake_proc.HOST_ROOT`: the device and root of the host's `/` mount
WATCHER_MNT_NS = "mnt:[4026531841]"


def host(**kw) -> MP.Host:
    base = {"depth": 1, "uid": UID, "proc_devs": frozenset({HOST_PROC[0]}), "fuse_dev": FUSE_DEV,
            "mountpoint": HOST_MOUNTPOINT, "protected": (GUARD,), "oneclient": ONECLIENT}  # fmt: skip
    return MP.Host(**{**base, **kw})


class Proofs(list):
    """The proof lines written, as (label, ok, problems, cell); `details` holds each one's extra keys (None for a
    passing proof, which has none)."""

    def __init__(self) -> None:
        super().__init__()
        self.details: list = []

    def write(self, label, ok, problems, cell, detail=None) -> None:
        self.append((label, ok, problems, cell))
        self.details.append(detail)


def polled(proc: FakeProc, **kw):
    """Poll once; the proofs written."""
    proofs = Proofs()
    poll = MP.CellPoll(proc, host(**kw), proofs.write)
    poll.poll()
    return poll, proofs


def node(*procs) -> FakeProc:
    return FakeProc().add(host_process(ONECLIENT), *procs)
