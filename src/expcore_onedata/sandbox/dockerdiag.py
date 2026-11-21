# Provenance: new
"""The diagnosis of a Docker run's survivors, as the cell tracker gives it to the deadline (O97).

At a cell's deadline, before the mount is aborted, the deadline asks its tracker what each survivor is blocked on
(`deadline.Diagnosing`). On Docker the agent runs as another user, so its processes' `/proc` entries are read through
the host's privilege: one call to the root half reads every survivor whole (`sudoproc.SudoProcReader`,
`procsnap.py`), and the reading itself is the one an Apptainer node makes, `stuckread.diagnose`, over that reader. The
answer is the same shape, so the same verdict (`attribution`) is made of it on both backends: the record's capped
`survivors` and `connection`, and `reading`, every survivor read whole, with the `pid` and `start` the deadline listed
it by.

What the reading needs of a Docker run is its cell's fixed targets (`stuck.Roles`): the data mount at the path the
agent sees it, on the device the run's mount has in the host's table now (a restore makes a new device, so it is read
when the deadline fires, never at the start), `/work`, `/tmp`, `/dev/shm` and the read-only inputs at the paths they
are bound at. Nothing a process wrote is read, and nothing of the target: the same uid on the same wedged connection
would wait too (`stuckread`). The agent's processes run on this host, so the syscall tables are the ones of this
machine, which `stuckread` checks (`stuck.X86_64`); on another one no call is resolved, and nothing is the agent's.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from expcore_onedata.sandbox import teardown as TD
from expcore_onedata.sandbox.apptainer import stuck as S
from expcore_onedata.sandbox.apptainer import stuckread
from expcore_onedata.sandbox.hostops import HostOps
from expcore_onedata.sandbox.sudoproc import SudoProcReader

SNAPSHOT_SHARE = 0.5  # of the time given, at most, for the one call that reads every survivor
# the abort file of the run's connection, as root sees it: is it there, and can it be written
FUSECTL_STATE = (
    'if [ -e "$1" ]; then if [ -w "$1" ]; then echo writable; else echo not_writable; fi; else echo absent; fi'
)
FUSECTL_STATE_NAME = "fusectl-state"  # the shell's $0


class DockerDiagnosis:
    """What a Docker run's tracker reads its survivors with: `diagnosis(left, timeout_s)`."""

    def __init__(
        self,
        host: HostOps,
        *,
        waits: TD.Waits,
        mountpoint: Callable[[], str],
        mount_path: str,
        inputs: Sequence[str] = (),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Take the host, the sandbox's waits, the run's mount point on the host (read each time: it is the share the
        mount is on), the path the agent sees the data mount at, the paths its read-only inputs are bound at, and the
        clock the time given is counted by."""
        self._host, self._waits, self._mountpoint = host, waits, mountpoint
        self._mount_path, self._inputs, self._clock = mount_path, tuple(inputs), clock

    def __call__(self, left: Sequence[Mapping[str, Any]], timeout_s: float) -> dict[str, Any] | None:
        """What the survivors in `left` are blocked on, within `timeout_s`; None when there is nothing to read or no
        time. One call reads every survivor, then the shared reading is made over it."""
        if not left or not timeout_s > 0:
            return None
        began = self._clock()
        view = SudoProcReader.snapshot(self._host, left, timeout_s * SNAPSHOT_SHARE, self._clock)
        spent = self._clock() - began
        until = began + timeout_s
        return stuckread.diagnose(
            view, left, timeout_s - spent, self.roles(), connection=lambda: self.connection(until - self._clock())
        )

    def _device(self) -> str | None:
        """The device of the run's mount in the host's table now; None when there is no FUSE mount at the share, or its
        entries are on more than one device, or the table cannot be read. The table lists one mount more than once (the
        share's bind is a peer of the volume under it), so it is the device that must be one, not the entry."""
        found = TD._fuse_at(self._host, self._mountpoint(), self._waits)
        devices = {m["dev"] for m in found or []}
        return next(iter(devices)) if len(devices) == 1 else None

    def roles(self) -> S.Roles:
        """The cell's fixed targets (`stuck.Roles`): with the device, only a FUSE mount on it at the data path is the
        data mount; with none known, any FUSE mount there is (the agent has no FUSE of its own)."""
        return S.Roles(data=self._mount_path, device=self._device(), inputs=self._inputs)

    def connection(self, budget_s: float | None = None) -> dict[str, Any] | None:
        """Whether the run's connection has an abort file the root user can write, `{"fusectl": ...}` for the record;
        None when it cannot be told, or when `budget_s`, the time the diagnosis has left, is spent: the check never
        takes a reading that was complete past its time. The abort is made in the sidecar, which mounts fusectl itself
        where it is absent."""
        if budget_s is not None and budget_s <= 0:
            return None
        device = self._device()
        number = None if device is None else TD._conn(device)
        if number is None:
            return None
        done = TD._sudo(
            self._host,
            "sh",
            "-c",
            FUSECTL_STATE,
            FUSECTL_STATE_NAME,
            str(TD.FUSECTL / number / "abort"),
            timeout_s=self._waits.probe_s if budget_s is None else min(self._waits.probe_s, budget_s),
        )
        state = done.stdout.strip() if done.returncode == 0 else ""
        return {"fusectl": state} if state in ("writable", "absent", "not_writable") else None


__all__ = ["DockerDiagnosis"]
