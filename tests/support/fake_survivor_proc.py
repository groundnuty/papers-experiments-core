"""A fake `/proc` for the survivors' diagnostics (`expcore_onedata.sandbox.apptainer.stuckread.ProcReader`), in memory.

`FakeProcReader.process(...)` makes a process with a mount table, a working directory and open files (`fd`, `maps`),
and its tasks (`task`); the per-process files (`fd/N`, `fdinfo/N`, `mountinfo`, `cwd`, `maps`) are read through any
task's directory, as a real `/proc` shares them between a process's threads, and a zombie leader has no entries of its
own.
`memory` holds what a task's memory has at an address (`mem_at`). Every name asked is kept in `names`, so a test can
show which files the reader opened, and `stall` makes a name's read block until the test lets it go.

Everything is invented: the pids, the spaces, the files and the mount table.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

DATA_DEVICE = "0:99"
MOUNTS = (
    "1 0 8:1 / / ro - squashfs /dev/loop0 ro\n"
    "20 1 0:21 / /proc rw - proc proc rw\n"
    "30 1 8:2 /run/work /work rw - ext3 /dev/vda rw\n"
    "31 1 8:2 /run/tmp /tmp rw - ext3 /dev/vda rw\n"
    "32 1 0:22 / /dev/shm rw - tmpfs tmpfs rw\n"
    "33 1 8:3 /inputs /opt/inputs ro - ext3 /dev/vdb ro\n"
    f"40 1 {DATA_DEVICE} / /data ro - fuse.oneclient oneclient ro,user_id=1000\n"
)
DATA_MOUNT, WORK_MOUNT, TMP_MOUNT, ROOT_MOUNT = 40, 30, 31, 1
SHARED = re.compile(r"(fd/\d+|fdinfo/\d+|mountinfo|cwd|maps)")
NAME = re.compile(r"(\d+)(?:/task/(\d+))?/(.+)")


def stat_line(tid: int, comm: str, state: str, start: str) -> str:
    """A `stat` line whose fields `procs.parse_stat` reads: comm, state, parent and, as the 22nd field, the start."""
    return f"{tid} ({comm}) {state} 1 " + " ".join(["0"] * 17) + f" {start} 0 0\n"


def syscall_text(number: int, *args: int) -> str:
    """The text of a `syscall` file: the number, six arguments in hex, a stack pointer and a counter."""
    padded = [*args, *([0] * (6 - len(args)))]
    return f"{number} " + " ".join(hex(a) for a in padded) + " 0x7ffd1000 0x7f110000\n"


@dataclass
class FakeProcess:
    pid: int
    start: str
    comm: str
    state: str
    mounts: str
    cwd: str
    tasks: dict[int, dict[str, str]] = field(default_factory=dict)
    fds: dict[int, tuple[str, str]] = field(default_factory=dict)  # number: (link target, fdinfo text)
    mapped: list[str] = field(default_factory=list)
    memory: dict[int, dict[int, bytes]] = field(default_factory=dict)  # tid: {address: bytes}
    hidden: bool = False  # `/proc/<pid>/stat` cannot be read
    root: str = "/"  # the process's root directory: a chroot's links carry its prefix, which its mount table does not

    def task(
        self, tid: int, *, state: str = "D", wchan: str = "0", syscall: str | None = None, comm: str | None = None
    ):
        self.tasks[tid] = {
            "stat": stat_line(tid, comm or self.comm, state, self.start),
            "wchan": wchan,
            "syscall": syscall if syscall is not None else "running\n",
        }
        return self

    def fd(self, number: int, target: str, *, mnt_id: int, pos: int = 0, flags: str = "0100000"):
        self.fds[number] = (target, f"pos:\t{pos}\nflags:\t{flags}\nmnt_id:\t{mnt_id}\nino:\t77\n")
        return self

    def maps(self, *paths: str):
        self.mapped.extend(paths)
        return self

    def mem_at(self, tid: int, address: int, text: str):
        self.memory.setdefault(tid, {})[address] = text.encode()
        return self

    def maps_text(self) -> str:
        return "".join(f"7f0000000000-7f0000001000 r--p 00000000 00:63 12 {path}\n" for path in self.mapped) + (
            "7ffd00000000-7ffd00021000 rw-p 00000000 00:00 0 [stack]\n"
        )


class FakeProcReader:
    """The view the reader is given (the module's docstring)."""

    def __init__(self) -> None:
        self.processes: dict[int, FakeProcess] = {}
        self.names: list[str] = []
        self.stall: dict[str, threading.Event] = {}

    def process(self, pid: int, *, start: str = "5000", comm: str = "python3", state: str = "D", cwd: str = "/work",
                mounts: str = MOUNTS) -> FakeProcess:  # fmt: skip
        self.processes[pid] = FakeProcess(pid, start, comm, state, mounts, cwd)
        return self.processes[pid]

    def _note(self, name: str) -> None:
        self.names.append(name)
        gate = self.stall.get(name)
        if gate is not None:
            gate.wait(30)

    def _find(self, name: str) -> tuple[FakeProcess | None, int | None, str]:
        found = NAME.fullmatch(name)
        if found is None:
            return None, None, name
        pid = int(found.group(1))
        return self.processes.get(pid), (int(found.group(2)) if found.group(2) else None), found.group(3)

    def listdir(self, name: str) -> list[str] | None:
        self._note(name)
        found = re.fullmatch(r"(\d+)/(task|task/\d+/fd)", name)
        if found is None or int(found.group(1)) not in self.processes:
            return None
        process = self.processes[int(found.group(1))]
        if found.group(2) == "task":
            return [str(tid) for tid in process.tasks]
        return [str(number) for number in process.fds]

    def read(self, name: str, limit: int) -> bytes | None:
        self._note(name)
        process, tid, tail = self._find(name)
        if process is None or (process.hidden and tid is None):
            return None
        if tid is None:  # `<pid>/stat`
            return (
                stat_line(process.pid, process.comm, process.state, process.start).encode()[:limit]
                if tail == "stat"
                else None
            )
        if tid not in process.tasks:
            return None
        if SHARED.fullmatch(tail):
            shared = self._shared(process, tail)
            return None if shared is None else shared[:limit]  # a real read stops at its limit
        text = process.tasks[tid].get(tail)
        return None if text is None else text.encode()[:limit]

    def _shared(self, process: FakeProcess, tail: str) -> bytes | None:
        if tail == "mountinfo":
            return process.mounts.encode()
        if tail == "maps":
            return process.maps_text().encode()
        if tail.startswith("fdinfo/"):
            held = process.fds.get(int(tail.split("/")[1]))
            return None if held is None else held[1].encode()
        return None

    def readlink(self, name: str) -> str | None:
        self._note(name)
        process, tid, tail = self._find(name)
        if process is None or tid not in process.tasks:
            return None
        if tail == "root":
            return process.root
        if tail == "cwd":
            return process.cwd
        if tail.startswith("fd/"):
            held = process.fds.get(int(tail.split("/")[1]))
            return None if held is None else held[0]
        return None

    def read_at(self, name: str, offset: int, size: int) -> bytes | None:
        self._note(name)
        process, tid, tail = self._find(name)
        if process is None or tid is None or tail != "mem":
            return None
        data = process.memory.get(tid, {}).get(offset)
        return None if data is None else data[:size]
