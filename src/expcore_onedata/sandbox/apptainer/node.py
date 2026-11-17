# Provenance: new
"""Everything the Apptainer backend does to its node goes through `NodeOps`, so a test supplies a fake (O56).

`LocalNode` is the real node: `apptainer` run through the bounded runner (`hostops.run_bounded`) with an environment it
names whole, helper processes (the data gate, the egress collector) spawned in sessions of their own, the node's
`/proc` read for this uid's processes, and signals sent through a pidfd only to the process that holds a pid with the
start time the caller knows, never to one that took the pid over (the bundle's `supervise.py` does the same).

There is no privilege: everything runs as the driver's uid, the cells too. `run` takes the `sudo` flag the shared copy
and removal helpers pass (`copyout`, `tokenfiles`) and runs the command as this uid all the same (`facts.py`).
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import platform
import signal
import subprocess
import threading
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from expcore_onedata.sandbox import killcause as KC
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.apptainer import hostfacts, loopback
from expcore_onedata.sandbox.apptainer import procs as P
from expcore_onedata.sandbox.hostops import CmdResult, DockerHost, mounts_under, run_bounded

SYS_PIDFD_SEND_SIGNAL, SYS_PIDFD_OPEN = 424, 434
PROC = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")
VERSION_S = 20.0


@dataclass(frozen=True)
class Spawned:
    """A process by its pid and its start time in clock ticks after boot: together they name it uniquely."""

    pid: int
    start: str

    def record(self) -> dict[str, Any]:
        return {"pid": self.pid, "start": self.start}


@runtime_checkable
class NodeOps(Protocol):
    """The node as the Apptainer backend needs it; an implementation raises `OSError` only where it says so."""

    def apptainer(
        self,
        argv: Sequence[str],
        timeout_s: float,
        *,
        input: str | None = None,
        output_cap: int | None = None,
        marker: bytes | None = None,
    ) -> CmdResult:
        """Run an `apptainer` command line within `timeout_s` (`run_bounded`'s bounds, caps and markers), in an
        environment of the node's `PATH`, `HOME` and `LC_ALL` alone: no `APPTAINER*` variable of the driver's."""
        ...

    def run(self, argv: Sequence[str], timeout_s: float, *, sudo: bool = False, input: str | None = None) -> CmdResult:
        """Run a command within `timeout_s` as this uid; `sudo` is taken and ignored (there is no privilege)."""
        ...

    def exists(self, path: str | Path) -> bool:
        """Whether a path exists on the node (the shared copy helper asks it of the run's working directory)."""
        ...

    def mounts_under(self, root: Path) -> list[str] | None:
        """The mount points inside `root` in this process's mount table, deepest first; None when the table cannot be
        shown whole (unknown, never "no mount")."""
        ...

    def spawn(
        self, argv: Sequence[str], *, stdout: Path, stderr: Path, env: Mapping[str, str] | None = None
    ) -> Spawned:
        """Start a helper process in a session of its own, stdin from /dev/null, its streams to two new files (made
        0600, refused if they exist), its environment the node's `PATH`, `HOME` and `LC_ALL` and `env` (the run's tag,
        so a reaper finds it even before the run's record names it); raise `OSError` when it cannot be started."""
        ...

    def exit_status(self, process: Spawned) -> int | None:
        """A spawned helper's exit status once it has ended (128 + N for a signal N), None while it runs or unknown."""
        ...

    def processes(self, tags: Collection[str] = ()) -> list[P.Proc] | None:
        """Every process of this uid that `/proc` shows, with which of `tags` each holds; None when `/proc` could not
        be listed (unknown, never none)."""
        ...

    def signal(self, pid: int, start: str, sig: int) -> bool | None:
        """Send `sig` to `pid` only if it is still the process that started at `start`: True when sent, False when it
        is gone or another process, None when that cannot be told."""
        ...

    def alive(self, pid: int, start: str) -> bool | None:
        """Whether the process that started at `start` still holds `pid` (a zombie of ours is reaped first): None when
        that cannot be told."""
        ...

    def identity(self) -> Spawned:
        """This process, by its pid and start time: the owner a run's record names."""
        ...

    def own_pidns(self) -> str | None:
        """This process's PID namespace (`pid:[<inode>]`): a cell's is another."""
        ...

    def free_port(self) -> int:
        """A TCP port on 127.0.0.1 that was free when asked (another process may take it before it is bound)."""
        ...

    def make_ca(self, directory: Path, names: Sequence[str]) -> None:
        """Write `ca.pem`, `ca.key` and a `<name>.pem` / `<name>.key` pair for each name into `directory`."""
        ...

    def cgroup_memory(self) -> dict[str, Any] | None:
        """The memory cgroup that bounds this process (the nearest with a limit): its `max_bytes`, `events`,
        `peak_bytes` and `depth` above this process's own; None when it cannot be read. Never its path (it names a
        job)."""
        ...

    def own_cgroup(self) -> str | None:
        """This process's own cgroup v2 path, where the cells it starts are charged, hidden processes and all; None on
        a v1 or hybrid host or when it cannot be read. Held to tell which processes it counts, never recorded (it
        names a job)."""
        ...

    def cgroup_anon(self, path: str) -> int | None:
        """The `anon` bytes of the `memory.stat` of the cgroup at `path` (v2: every process in it and below); None when
        it cannot be read."""
        ...

    def hidden_children(self, pid: int, start: str) -> list[int] | None:
        """The children of the process that started at `start` that `/proc` does not show as this uid's while they
        exist (`procs.hidden_children`); [] when the process is gone or another holds its pid, None when its
        children cannot be read while it lives."""
        ...

    def node_facts(self, apptainer: Sequence[str]) -> dict[str, Any]:
        """Facts of the node for a record: its kernel and the version of `apptainer` (the command words). No name
        and no address."""
        ...

    def node_readings(self) -> dict[str, Any]:
        """The node's OS release (ID, VERSION_ID), glibc version and kernel release (`hostfacts.readings`, O87): files
        read as text and system values asked, nothing executed; a reading that fails is `unknown` with its reason."""
        ...

    def tool_server_readings(self) -> dict[str, Any]:
        """The same readings of the host the run's tool server runs on. The server is the driver's own child, so on
        the node it is the node's own; a node that places it elsewhere answers for that host."""
        ...


def _syscall(number: int, *args: Any) -> int:
    """A raw Linux system call through libc's `syscall`, for one this Python was built without; `OSError` with the
    call's errno when it fails."""
    libc = ctypes.CDLL(None, use_errno=True)
    found = libc.syscall(number, *args)
    if found < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return int(found)


def pidfd_open(pid: int) -> int:
    """`pidfd_open(2)`: `os.pidfd_open` where this Python has it, else the system call itself (434 in Linux's generic
    table, which every architecture shares for calls this new). A Python built elsewhere (a standalone build) may lack
    it although the node's kernel has it."""
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    return _syscall(SYS_PIDFD_OPEN, ctypes.c_int(pid), ctypes.c_uint(0))


def pidfd_send_signal(fd: int, sig: int) -> None:
    """`pidfd_send_signal(2)`, likewise (424)."""
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, sig)
        return
    _syscall(SYS_PIDFD_SEND_SIGNAL, ctypes.c_int(fd), ctypes.c_int(int(sig)), None, ctypes.c_uint(0))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _start_of(pid: int) -> str | None:
    """The start time of `pid` from `/proc`, or None when its directory is not there or not whole."""
    try:
        parsed = P.parse_stat(_read(PROC / str(pid) / "stat"))
    except OSError:
        return None
    return None if parsed is None else parsed[3]


def _children(pid: int) -> set[int] | None:
    """Every child of `pid`'s threads (`/proc/<pid>/task/<tid>/children`); empty when the process is gone (or hidden:
    its own parent's children name it then), None when a living thread's children cannot be read."""
    tasks = PROC / str(pid) / "task"
    try:
        tids = os.listdir(tasks)
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    found: set[int] = set()
    for tid in tids:
        try:
            listed = P.parse_children(_read(tasks / tid / "children"))
        except FileNotFoundError:
            if (tasks / tid).exists():  # a living thread without the file: the kernel does not keep it
                return None
            continue  # the thread ended between the listing and the read
        except OSError:
            return None
        if listed is None:
            return None
        found |= listed
    return found


def _shown_ours(pid: int) -> bool:
    """Whether `/proc` shows `pid` as a process of this uid (a non-dumpable one's entry is root's, or missing)."""
    try:
        return (PROC / str(pid)).stat().st_uid == os.getuid()
    except OSError:
        return False


def _exists(pid: int) -> bool | None:
    """Whether a process holds `pid`, whatever `/proc` shows of it; None when that cannot be told."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


class LocalNode:
    """The node the driver runs on. Helper processes it spawned are reaped by it, so a helper that ended is never a
    zombie that `/proc` shows as alive."""

    def __init__(self) -> None:
        self._children: dict[int, tuple[subprocess.Popen[bytes], str]] = {}  # pid -> the child and its start
        self._lock = threading.Lock()

    def _apptainer_env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/"),
            "LC_ALL": "C",
        }

    def apptainer(
        self,
        argv: Sequence[str],
        timeout_s: float,
        *,
        input: str | None = None,
        output_cap: int | None = None,
        marker: bytes | None = None,
    ) -> CmdResult:
        return run_bounded(
            argv, timeout_s, env=self._apptainer_env(), inherit_env=False, input=input, output_cap=output_cap,
            marker=marker,
        )  # fmt: skip

    def run(self, argv: Sequence[str], timeout_s: float, *, sudo: bool = False, input: str | None = None) -> CmdResult:
        return run_bounded(argv, timeout_s, input=input)

    def exists(self, path: str | Path) -> bool:
        return Path(path).exists()

    def mounts_under(self, root: Path) -> list[str] | None:
        try:
            return mounts_under(_read(PROC / "self" / "mountinfo"), root)
        except (OSError, SandboxError):
            return None

    def spawn(
        self, argv: Sequence[str], *, stdout: Path, stderr: Path, env: Mapping[str, str] | None = None
    ) -> Spawned:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        out = os.open(stdout, flags, 0o600)
        try:
            err = os.open(stderr, flags, 0o600)
            try:
                child = subprocess.Popen(
                    list(argv), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True,
                    env={**self._apptainer_env(), **(env or {})}, close_fds=True,
                )  # fmt: skip
            finally:
                os.close(err)
        finally:
            os.close(out)
        start = _start_of(child.pid)  # it cannot have been reaped: only this object reaps it, and has not yet
        if start is None:  # /proc hides it: it is no helper of ours to keep
            with contextlib.suppress(OSError):
                child.kill()
            child.wait()
            raise OSError("a helper process could not be identified")
        with self._lock:
            self._children[child.pid] = (child, start)
        return Spawned(child.pid, start)

    def _reaped(self, pid: int, start: str) -> int | None:
        """A child of ours, the one that started at `start`, that has ended, reaped now: its exit status; None while
        it runs, or when that pid with that start was no child of ours (another process may hold the pid now)."""
        with self._lock:
            found = self._children.get(pid)
        if found is None or found[1] != start:
            return None
        code = found[0].poll()
        return None if code is None else (128 - code if code < 0 else code)

    def exit_status(self, process: Spawned) -> int | None:
        return self._reaped(process.pid, process.start)

    def processes(self, tags: Collection[str] = ()) -> list[P.Proc] | None:
        uid = os.getuid()
        try:
            entries = [name for name in os.listdir(PROC) if name.isdigit()]
        except OSError:
            return None
        found = []
        for name in entries:
            base = PROC / name
            try:
                if base.stat().st_uid != uid:
                    continue
                parsed = P.parse_stat(_read(base / "stat"))
                if parsed is None:
                    continue
                comm, state, ppid, start = parsed
                status = _read(base / "status")
            except OSError:  # it ended while it was read: not there
                continue
            try:
                pidns = P.namespace(os.readlink(base / "ns" / "pid"))
            except OSError:
                pidns = None
            cmdline = environ = b""
            cgroup = None
            with contextlib.suppress(OSError):
                cmdline = (base / "cmdline").read_bytes()
            with contextlib.suppress(OSError):
                environ = (base / "environ").read_bytes()
            with contextlib.suppress(OSError):
                cgroup = P.cgroup_path(_read(base / "cgroup"))
            found.append(
                P.Proc(
                    int(name), start, ppid, state, comm, pidns, P.parse_rss(status), P.tags_in(cmdline, environ, tags),
                    P.parse_anon(status), cgroup,
                )
            )  # fmt: skip
        return found

    def signal(self, pid: int, start: str, sig: int) -> bool | None:
        if pid < 1:
            return False
        try:
            fd = pidfd_open(pid)
        except ProcessLookupError:
            return False
        except OSError:
            return None
        try:
            now = _start_of(pid)
            if now is None:
                return None  # hidden, or gone between the open and the read: not known to be the one asked for
            if now != start:
                return False
            pidfd_send_signal(fd, sig)
        except ProcessLookupError:
            return False
        except PermissionError:
            return None
        finally:
            os.close(fd)
        return True

    def alive(self, pid: int, start: str) -> bool | None:
        if self._reaped(pid, start) is not None:
            return False
        now = _start_of(pid)
        if now is not None:
            return now == start
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return None
        return None  # a pid that exists and that /proc hides: not known to be ours

    def identity(self) -> Spawned:
        start = _start_of(os.getpid())
        if start is None:
            raise OSError("this process's own start time could not be read")
        return Spawned(os.getpid(), start)

    def own_pidns(self) -> str | None:
        try:
            return P.namespace(os.readlink(PROC / "self" / "ns" / "pid"))
        except OSError:
            return None

    def free_port(self) -> int:
        return loopback.free_port()

    def make_ca(self, directory: Path, names: Sequence[str]) -> None:
        DockerHost(sudo_command=()).make_ca(directory, names)  # openssl only, as this uid

    def cgroup_memory(self) -> dict[str, Any] | None:
        try:
            own = P.cgroup_path(_read(PROC / "self" / "cgroup"))
        except OSError:
            return None
        if own is None:
            return None
        parts = [p for p in own.split("/") if p]
        for depth in range(len(parts) + 1):
            here = CGROUP_ROOT.joinpath(*parts[: len(parts) - depth])
            try:
                limit = _read(here / "memory.max").strip()
            except OSError:
                continue
            if limit == "max":
                continue
            try:
                events = KC.parse_events(_read(here / "memory.events"))
            except OSError:
                events = None
            try:
                peak = int(_read(here / "memory.peak").strip())
            except (OSError, ValueError):
                peak = None
            return {"depth": depth, "max_bytes": int(limit) if limit.isdigit() else None, "events": events,
                    "peak_bytes": peak}  # fmt: skip
        return None

    def own_cgroup(self) -> str | None:
        try:
            return P.cgroup_path(_read(PROC / "self" / "cgroup"))
        except OSError:
            return None

    def cgroup_anon(self, path: str) -> int | None:
        parts = [p for p in path.split("/") if p]
        try:
            return P.stat_value(_read(CGROUP_ROOT.joinpath(*parts) / "memory.stat"), "anon")
        except OSError:
            return None

    def hidden_children(self, pid: int, start: str) -> list[int] | None:
        if _start_of(pid) != start:  # gone, hidden itself (its parent's children name it), or another process now
            return []
        return P.hidden_children(lambda: _children(pid), _shown_ours, _exists)

    def node_facts(self, apptainer: Sequence[str]) -> dict[str, Any]:
        done = self.apptainer([*apptainer, "--version"], VERSION_S)
        version = done.stdout.strip() if done.returncode == 0 else None
        return {"kernel": platform.release(), "apptainer": version[:80] if version else None}

    def node_readings(self) -> dict[str, Any]:
        return hostfacts.readings()

    def tool_server_readings(self) -> dict[str, Any]:
        return self.node_readings()  # the tool server is a process of this driver's, on this node


__all__ = ["LocalNode", "NodeOps", "Spawned"]
