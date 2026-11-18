# Provenance: new
"""Everything the Apptainer backend does to its node goes through `NodeOps`, so a test supplies a fake (O56).

`LocalNode` is the real node: `apptainer` run through the bounded runner (`hostops.run_bounded`) with an environment it
names whole, helper processes (the data gate, the egress collector) spawned in sessions of their own, the node's
`/proc` read for this uid's processes, and signals sent through a pidfd only to the process that holds a pid with the
start time the caller knows, never to one that took the pid over (the bundle's `supervise.py` does the same).

A kernel that answers ENOSYS to `pidfd_open` (Linux before 5.3: an EL8 4.18 kernel has `pidfd_send_signal` and no
`pidfd_open`) has no descriptor to hold a process by. Only then `LocalNode.signal` takes the identity of its target from
what the target is to this process: a child it spawned is signalled while the kernel still shows it an unreaped child
of its own (`waitid`), another process of this uid while its real and effective uid and its start tick, read last, are
as named. A process `/proc` hides that is no child of ours (hidepid, a setuid starter) is not signalled: unknown, as it
is with a pidfd. `LocalNode._signal_without_pidfd` states each identity and the window that remains. A kernel with
`pidfd_open` runs the pidfd path, which the fallback does not touch.

There is no privilege: everything runs as the driver's uid, the cells too. `run` takes the `sudo` flag the shared copy
and removal helpers pass (`copyout`, `tokenfiles`) and runs the command as this uid all the same (`facts.py`).
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import logging
import os
import platform
import re
import signal
import subprocess
import threading
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from expcore_onedata.sandbox import hostfacts
from expcore_onedata.sandbox import killcause as KC
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.apptainer import loopback
from expcore_onedata.sandbox.apptainer import procs as P
from expcore_onedata.sandbox.cellhost import OwnNames
from expcore_onedata.sandbox.hostops import CmdResult, DockerHost, mounts_under, run_bounded

log = logging.getLogger(__name__)
SYS_PIDFD_SEND_SIGNAL, SYS_PIDFD_OPEN = 424, 434
PROC = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")
V1_MEMORY = "memory"  # the memory controller's directory under the root on a cgroup v1 host
V1_UNLIMITED = 1 << 62  # v1 shows no limit as a value near 2**63 (9223372036854771712 on the kernels seen)
VERSION_S = 20.0
BOOT_ID = PROC / "sys" / "kernel" / "random" / "boot_id"
BOOT_ID_FORM = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


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
        is gone or another process, None when that cannot be told (never sent then). Where the kernel has no
        `pidfd_open`, see `LocalNode._signal_without_pidfd`."""
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
        job). On a cgroup v1 host `events` holds `max` (the limit-holder's `memory.failcnt`) and `oom_kill` (the count
        in `memory.oom_control` of this process's own cgroup, where the kernel counts a kill of a process in it, not
        of the limit-holder), each only when it could be read."""
        ...

    def own_cgroup(self) -> str | None:
        """This process's own memory cgroup path (v2's, or on a v1 host the memory controller's), where the cells it
        starts are charged, hidden processes and all; None when it cannot be read. Held to tell which processes it
        counts, never recorded (it names a job)."""
        ...

    def cgroup_anon(self, path: str) -> int | None:
        """The anonymous bytes of the cgroup at `path` (`own_cgroup`'s): every process in it and below, v2's `anon` of
        its `memory.stat`, on v1 its `total_rss` (`rss` when there is none); None when it cannot be read."""
        ...

    def hidden_children(self, pid: int, start: str) -> list[int] | None:
        """The children of the process that started at `start` that `/proc` does not show as this uid's while they
        exist (`procs.hidden_children`); [] when the process is gone or another holds its pid, None when its
        children cannot be read while it lives."""
        ...

    def own_affinity(self) -> list[int] | None:
        """The cores this process may run on (its CPU affinity, sorted): the step's cores, from which a cell's pin is
        taken (O70); None when they cannot be read."""
        ...

    def task_affinity(self, pid: int, start: str) -> frozenset[int] | None:
        """The union of the cores every task of the process that started at `start` may run on (`Cpus_allowed_list`
        of each task's status: a thread may set its own); the empty set when the process is gone or another holds its
        pid; None when a living task's cores cannot be read."""
        ...

    def node_facts(self, apptainer: Sequence[str]) -> dict[str, Any]:
        """Facts of the node for a record: its kernel and the version of `apptainer` (the command words). No name
        and no address."""
        ...

    def boot_id(self) -> str | None:
        """This boot of the node: the kernel's random per-boot UUID (`/proc/sys/kernel/random/boot_id`), which names
        neither the node nor its site; None when it cannot be read. What the work disk's kept proof is for (O71)."""
        ...

    def own_names(self) -> OwnNames:
        """The node's own host name and its fully qualified form, as it reports them (O73): for the gate's scan for node
        names alone, never a record or a message. A form it cannot read, or not in a short bound, is left out and its
        reason is in `unread`."""
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


def _memory_on_v1_in(text: str) -> bool:
    """Whether the memory controller is on a cgroup v1 hierarchy, from this process's `/proc/self/cgroup`: a line lists
    `memory` among its controllers, and the v2 path (a hybrid host has both) shows no `memory.stat` under the root. A
    host whose controller is on v2 has no such line."""
    if P.cgroup_path(text, v1=True) is None:
        return False
    unified = P.cgroup_path(text)
    if unified is None:
        return True
    try:
        _read(CGROUP_ROOT.joinpath(*[p for p in unified.split("/") if p]) / "memory.stat")
    except OSError:
        return True
    return False


def _memory_on_v1() -> bool:
    """`_memory_on_v1_in` of this process's own `/proc/self/cgroup`; False when it cannot be read (the v2 reading)."""
    try:
        return _memory_on_v1_in(_read(PROC / "self" / "cgroup"))
    except OSError:
        return False


def _v1_count(path: Path, key: str | None = None) -> int | None:
    """A v1 memory file's number: the whole file (`memory.limit_in_bytes`), or `key`'s line (`memory.oom_control`); None
    when it cannot be read or is no number."""
    try:
        text = _read(path)
    except OSError:
        return None
    if key is not None:
        return P.stat_value(text, key)
    return int(text.strip()) if text.strip().isdigit() else None


def _cgroup_memory_v1(own: str) -> dict[str, Any] | None:
    """`LocalNode.cgroup_memory` on a v1 host, from the memory controller's files (v2's `memory.max`, `memory.peak` and
    `memory.events` are `memory.limit_in_bytes`, `memory.max_usage_in_bytes` and, of its counters, `memory.failcnt` as
    `max`). The walk goes up from the process's own cgroup to the nearest one with a limit, skipping a level whose limit
    is near 2**63 (none) or unreadable. A kill is counted in the cgroup of the process the kernel killed, not in the one
    whose limit it hit, so `oom_kill` is the own cgroup's (cells inherit it); a kernel whose `memory.oom_control` has no
    such line leaves it out, never a guess."""
    parts = [p for p in own.split("/") if p]
    root = CGROUP_ROOT / V1_MEMORY
    oom_kill = _v1_count(root.joinpath(*parts) / "memory.oom_control", "oom_kill")
    for depth in range(len(parts) + 1):
        here = root.joinpath(*parts[: len(parts) - depth])
        limit = _v1_count(here / "memory.limit_in_bytes")
        if limit is None or limit >= V1_UNLIMITED:
            continue
        counters = {"max": _v1_count(here / "memory.failcnt"), "oom_kill": oom_kill}
        events = {name: count for name, count in counters.items() if count is not None}
        return {"depth": depth, "max_bytes": limit, "events": events or None,
                "peak_bytes": _v1_count(here / "memory.max_usage_in_bytes")}  # fmt: skip
    return None


def _start_of(pid: int) -> str | None:
    """The start time of `pid` from `/proc`, or None when its directory is not there, not whole or cannot be read."""
    try:
        return _live_start(pid)
    except OSError:
        return None


def _live_start(pid: int) -> str | None:
    """The start time of `pid`, or None when the process is gone (no entry, the kernel's ESRCH, or an entry that is not
    whole); an `OSError` when its `stat` is there and cannot be read: a live process that is unreadable is not gone."""
    try:
        parsed = P.parse_stat(_read(PROC / str(pid) / "stat"))
    except (FileNotFoundError, ProcessLookupError):
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


def _uids(pid: int) -> tuple[int, int] | None:
    """The real and effective uid of `pid` from its `status`, or None when that cannot be read: the process is gone,
    `/proc` hides it from this uid (hidepid, a setuid program's process), or the file is not whole."""
    try:
        return P.parse_uids(_read(PROC / str(pid) / "status"))
    except OSError:
        return None


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
        with self._lock:  # the poll reaps: `_signal_without_pidfd` holds this lock from its `waitid` to its kill
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
        v1 = _memory_on_v1()  # one kind of path for every process, the own cgroup's (`own_cgroup`)
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
                cgroup = P.cgroup_path(_read(base / "cgroup"), v1=v1)
            found.append(
                P.Proc(
                    int(name), start, ppid, state, comm, pidns, P.parse_rss(status), P.tags_in(cmdline, environ, tags),
                    P.parse_anon(status), cgroup, P.parse_threads(status),
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
        except OSError as error:
            if error.errno == errno.ENOSYS:  # the kernel has no pidfd_open: the one failure the fallback answers
                return self._signal_without_pidfd(pid, start, sig)
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

    def _signal_without_pidfd(self, pid: int, start: str, sig: int) -> bool | None:
        """`signal` where `pidfd_open` answers ENOSYS, the one answer that leads here (any other refusal is unknown, as
        it was, and a kernel that has the call never enters this method). Nothing holds the process by a descriptor
        across the checks, so what identifies the target is what it is to this process, and the answers are those of
        `signal`: True when sent, False when gone or another process, None when that cannot be told, nothing sent.

        * **A child this node spawned** (the data gate, the helpers: `_children`, by pid and start): signalled only
          while this node has not reaped it (its `Popen` has no `returncode`: the entry stays after the reap, and its
          pid may be another child's by then) and `waitid(P_PID, pid, WEXITED | WNOHANG | WNOWAIT)` shows it a child of
          ours that is still running. `ChildProcessError` (reaped) is False, and so is a child that has ended and is not
          reaped yet (a zombie: nothing can receive a signal there, and one that reads as sent would let the gate's
          run-start mark pass as signalled). A pid is free to be reused only once its parent has waited for it, so no
          other process can hold it while this holds, and `/proc` is not consulted. The node's lock is held from the
          check to the `kill` and by `_reaped`, whose poll is the only thing here that reaps a child, so no thread of
          this process reaps it between the two. No PID-reuse window remains.
        * **Any other process** (a cell's process, a holder, a helper another driver recorded: no child of ours, and
          `/proc` shows them): signalled only if its real and its effective uid, read from `/proc/<pid>/status`, are
          both this process's uid, and then if its start tick (field 22 of `stat`), read last, is the one named, with
          `os.kill` at once; the tick is read again after the signal, and one that changed is not reported as sent
          (None). A process of another uid, or at another tick, is False and gets nothing. What remains is the window
          between the last read of the tick and the kill (two system calls): the process could end and another
          process of this uid take its pid, which the kill would then reach. That takes a cycle of the whole PID space
          inside that gap; a pidfd, which holds the process itself, closes it, and this does not.
        * **A process `/proc` does not show us that is no child of ours** (hidepid hides a setuid starter, and a
          process that made itself non-dumpable): its identity cannot be read, so it is not signalled, and the answer
          is None, as it is with a pidfd (`signal`, `_start_of`); False once no process holds the pid. No parent is
          signalled in its place: a signal to another process is no signal to the target. Every caller reads None as
          not sent and fails closed: a cell past its cap that cannot be signalled is listed again, outlives the
          deadline's grace and ends as `survived_kill` with the sandbox's stop ordered; a memory kill is recorded with
          `signalled` 0; the gate's run-start mark is unconfirmed and its served bytes unknown; a helper or a holder
          that cannot be signalled cannot be shown gone, and the teardown fails.
        """
        with self._lock:
            held = self._children.get(pid)
            if held is not None and held[1] == start:
                if held[0].returncode is not None:
                    return False  # reaped by this node: the pid may be another child's now
                try:
                    ended = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                except ChildProcessError:
                    return False  # reaped: the pid may be anyone's now
                if ended is not None:
                    return False  # ended, not reaped yet: a zombie takes no signal
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    return False
                except PermissionError:
                    return None
                return True
        return self._signal_by_tick(pid, start, sig)

    def _signal_by_tick(self, pid: int, start: str, sig: int) -> bool | None:
        """The second and third kinds of target of `_signal_without_pidfd`: a process that is no child of ours."""
        uids = _uids(pid)
        if uids is None:  # gone, or hidden from this uid: only a process that is there can be hidden
            return False if _exists(pid) is False else None
        if uids != (os.getuid(), os.getuid()):
            return False
        try:
            seen = _live_start(pid)  # the last read before the kill: it follows at once
        except OSError:
            return None  # there, and unreadable: not known to be the one asked for
        if seen is None:
            return None  # hidden, or gone since the uids were read
        if seen != start:
            return False
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return False
        except PermissionError:
            return None
        after = _start_of(pid)  # gone (None) is how a kill that worked usually reads
        if after is not None and after != start:
            log.warning(
                "pid %d shows start tick %s after a signal to %s: it may have been taken over", pid, after, start
            )
            return None
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
        found = self._own_memory_cgroup()
        if found is None:
            return None
        own, v1 = found
        if v1:
            return _cgroup_memory_v1(own)
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
        found = self._own_memory_cgroup()
        return None if found is None else found[0]

    def _own_memory_cgroup(self) -> tuple[str, bool] | None:
        """This process's memory cgroup path and whether it is on a v1 hierarchy; None when it cannot be read."""
        try:
            text = _read(PROC / "self" / "cgroup")
        except OSError:
            return None
        v1 = _memory_on_v1_in(text)
        path = P.cgroup_path(text, v1=v1)
        return None if path is None else (path, v1)

    def cgroup_anon(self, path: str) -> int | None:
        parts = [p for p in path.split("/") if p]
        try:
            return P.stat_value(_read(CGROUP_ROOT.joinpath(*parts) / "memory.stat"), "anon")
        except OSError:
            pass  # not a v2 cgroup under the root: a v1 memory cgroup's path is read under the memory controller's
        try:
            text = _read(CGROUP_ROOT.joinpath(V1_MEMORY, *parts) / "memory.stat")
        except OSError:
            return None
        total = P.stat_value(text, "total_rss")  # this cgroup and everything below, as v2's `anon` counts
        return total if total is not None else P.stat_value(text, "rss")

    def hidden_children(self, pid: int, start: str) -> list[int] | None:
        try:
            here = _live_start(pid)
        except OSError:
            return None  # there, and unreadable: never taken for gone
        if here != start:  # gone, hidden itself (its parent's children name it), or another process now
            return []
        return P.hidden_children(lambda: _children(pid), _shown_ours, _exists)

    def own_affinity(self) -> list[int] | None:
        try:
            return sorted(os.sched_getaffinity(0)) or None
        except (AttributeError, OSError):
            return None

    def task_affinity(self, pid: int, start: str) -> frozenset[int] | None:
        try:
            here = _live_start(pid)
        except OSError:
            return None  # there, and unreadable: never taken for gone
        if here != start:  # gone, hidden, or another process now
            return frozenset()
        tasks = PROC / str(pid) / "task"
        try:
            tids = os.listdir(tasks)
        except FileNotFoundError:
            return frozenset()
        except OSError:
            return None
        found: set[int] = set()
        for tid in tids:
            try:
                cores = P.parse_cpus_allowed(_read(tasks / tid / "status"))
            except FileNotFoundError:
                continue  # the thread ended between the listing and the read
            except OSError:
                return None
            if cores is None:
                return None
            found |= cores
        return frozenset(found)

    def node_facts(self, apptainer: Sequence[str]) -> dict[str, Any]:
        done = self.apptainer([*apptainer, "--version"], VERSION_S)
        version = done.stdout.strip() if done.returncode == 0 else None
        return {"kernel": platform.release(), "apptainer": version[:80] if version else None}

    def boot_id(self) -> str | None:
        try:
            found = _read(BOOT_ID).strip()
        except OSError:
            return None
        return found if BOOT_ID_FORM.fullmatch(found) else None

    def own_names(self) -> OwnNames:
        return loopback.own_names()

    def node_readings(self) -> dict[str, Any]:
        return hostfacts.readings()

    def tool_server_readings(self) -> dict[str, Any]:
        return self.node_readings()  # the tool server is a process of this driver's, on this node


__all__ = ["LocalNode", "NodeOps", "Spawned"]
