"""A fake HPC node whose cell has a reader in the kernel that no signal ends, until the connection ends (O97).

`stuck` is a cell handler: it makes the cell's second process unkillable and leaves it, as a read of a stopped
provider's file does. `free` is what the kernel does when the connection ends, by the fusectl write or by oneclient's
death (a test says which: `free_by_fusectl` for the write, and the node does it on the signal): the reader returns and
dies of its pending SIGKILL. Everything is invented.
"""

import time

from fake_mount_node import FakeMountNode

from expcore_onedata.sandbox.hostops import CmdResult


class ReaderNode(FakeMountNode):
    """The fake node whose cell has a reader in the kernel that no signal ends, until the connection ends: the abort
    (the fusectl write, or oneclient's death) frees it, as the kernel does."""

    def __init__(self) -> None:
        super().__init__()
        self.keep_mount_on_death = True
        self.reader = None
        self.freed_by: list[str] = []

    def stuck(self, cell) -> CmdResult:
        self.reader = cell.procs[1]
        self.reader.unkillable = True
        deadline = time.monotonic() + 10.0
        while cell.procs[0].alive and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
        return CmdResult(137, "", "")

    def free(self, how: str) -> None:
        with self.lock:
            if self.reader is not None and self.reader.alive:
                self.reader.unkillable, self.reader.alive, self.reader.exit_status = False, False, 137
                self._end_namespace_if_empty(self.reader.pidns)
                self.freed_by.append(how)

    def signal(self, pid, start, sig):
        proc = self._find(pid, start)
        sent = super().signal(pid, start, sig)
        if proc is not None and proc.role == "oneclient":
            self.free("daemon_death")  # the kernel ends the connection when the daemon's descriptors close
        return sent
