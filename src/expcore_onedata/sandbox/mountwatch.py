# Provenance: new
"""The host's watcher of a mount run on an HPC node: every access to the mount's token file and its directory, any
tracer on oneclient, and the proof that each cell is isolated from both, logged where no cell can write (O60).

    python -I -B -m expcore_onedata.sandbox.mountwatch --log <file> --nonce <hex> --token <file> --mountpoint <dir>
        --oneclient <pid>:<start> --protect <dir> [--protect <dir> ...] [--poll-s 0.05]

The host (`apptainer.hostmount`) starts it once oneclient's wrapper has read the token file into oneclient's
environment and the mount is up, and before any cell; it runs as the driver's uid, outside every cell, with no
privilege. It:

- sets inotify watches (every event) on the token file and on its directory, writes its start line and oneclient's
  (as `--oneclient` names it: its pid and start time), and prints `mount watch ready`;
- opens the agent's window itself (`armed`, and prints `mount watch armed`) once both watches have seen the token file
  removed: the host removes it once the mount is up, so that removal is the canary that shows both watches live, and
  every event after it is counted as the agent's (inotify names no process);
- every `--poll-s`: reads the queued events, samples oneclient's `TracerPid` (a line when it changes; a `sampling`
  line when oneclient's status can no longer be read: `gone`, or `unreadable`), and polls this uid's processes for the
  run's cells (`mountproof.CellPoll`), writing a proof line for each;
- on SIGTERM or SIGINT, which the host sends once every cell has ended: reads what is queued, polls one last time (a
  cell of the run's still alive is a failed proof), writes `disarmed` and its end line, and exits 0.

The log (`expcore.mountguard.log` reads it) is created 0600 and refused if it exists; it is JSON lines numbered from
0 with `wall` and `mono` readings. It names no path and no token: an event's entry is `self`, `token` or `other`. Past
`MAX_LINES` lines, or when a write fails, a line is counted in the end line's `unwritten` and not written; the reader
takes any as damage. The watcher refuses to start (exit 2, nothing logged) when it cannot read its own `NSpid`.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import select
import signal
import struct
import sys
import time
from collections.abc import Sequence
from typing import Any

from expcore_onedata.sandbox import mountproof as MP
from expcore_onedata.sandbox.hostops import MountTableError

FORMAT = "expcore-mountwatch/1"
READY, ARMED = "mount watch ready", "mount watch armed"
MAX_LINES = 1_000_000
POLL_S = 0.05
EVENT = struct.Struct("iIII")  # wd, mask, cookie, len
IN_ALL_EVENTS = 0x00000FFF
IN_NONBLOCK, IN_CLOEXEC = os.O_NONBLOCK, os.O_CLOEXEC
IN_Q_OVERFLOW = 0x00004000
IN_DELETE, IN_DELETE_SELF = 0x00000200, 0x00000400
CANARY = {("token", "self", IN_DELETE_SELF), ("dir", "token", IN_DELETE)}  # the host's removal, seen by each watch
MASKS = (
    (0x00000001, "IN_ACCESS"), (0x00000002, "IN_MODIFY"), (0x00000004, "IN_ATTRIB"), (0x00000008, "IN_CLOSE_WRITE"),
    (0x00000010, "IN_CLOSE_NOWRITE"), (0x00000020, "IN_OPEN"), (0x00000040, "IN_MOVED_FROM"),
    (0x00000080, "IN_MOVED_TO"), (0x00000100, "IN_CREATE"), (0x00000200, "IN_DELETE"), (0x00000400, "IN_DELETE_SELF"),
    (0x00000800, "IN_MOVE_SELF"), (0x00002000, "IN_UNMOUNT"), (0x00008000, "IN_IGNORED"), (0x40000000, "IN_ISDIR"),
)  # fmt: skip
YAMA = "/proc/sys/kernel/yama/ptrace_scope"


def mask_names(mask: int) -> list[str]:
    return [name for bit, name in MASKS if mask & bit]


class Inotify:
    """inotify through the C library: watches by target name, events read without blocking."""

    def __init__(self) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)
        fd = self._libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        self.fd, self.targets = fd, {}

    def watch(self, path: str, target: str) -> bool:
        wd = self._libc.inotify_add_watch(self.fd, os.fsencode(path), IN_ALL_EVENTS)
        if wd < 0:
            return False
        self.targets[wd] = target
        return True

    def read(self) -> list[tuple[str | None, int, bytes]]:
        """Every queued event as (target, mask, name); target None for a queue overflow."""
        out: list[tuple[str | None, int, bytes]] = []
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                return out
            offset = 0
            while offset + EVENT.size <= len(data):
                wd, mask, _, length = EVENT.unpack_from(data, offset)
                name = data[offset + EVENT.size : offset + EVENT.size + length].rstrip(b"\0")
                offset += EVENT.size + length
                # every watch is the token's or its directory's; an event of no known watch is counted as one
                out.append((None if mask & IN_Q_OVERFLOW else self.targets.get(wd, "dir"), mask, name))


class Log:
    """The watcher's log: numbered JSON lines, each written whole and at once."""

    def __init__(self, path: str) -> None:
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        self.seq, self.unwritten = 0, 0

    def write(self, event: str, **fields: Any) -> None:
        if self.seq >= MAX_LINES and event != "end":
            self.unwritten += 1
            return
        line = dict(fields, event=event, seq=self.seq, wall=time.time(), mono=time.monotonic())
        data = (json.dumps(line, sort_keys=True) + "\n").encode()
        try:
            written = os.write(self._fd, data)
        except OSError:
            written = -1
        if written != len(data):
            self.unwritten += 1  # a short or failed write leaves a damaged line: the reader finds it
            return
        self.seq += 1


class Sampler:
    """oneclient's `TracerPid`, sampled; it stops at the first status it cannot read."""

    def __init__(self, proc: MP.ProcView, pid: int, start: int, log: Log) -> None:
        self._proc, self._pid, self._start, self._log = proc, pid, start, log
        self.attached, self.stopped, self.samples = False, False, 0

    def sample(self) -> None:
        if self.stopped:
            return
        try:
            same = MP.start_time(self._proc.read(f"/proc/{self._pid}/stat")) == self._start
            text = self._proc.read(f"/proc/{self._pid}/status") if same else ""
        except (FileNotFoundError, ProcessLookupError):
            same, text = False, ""
        except OSError:
            self._stop("unreadable")
            return
        if not same:
            self._stop("gone")
            return
        tracer = MP.status_field(text, "TracerPid")
        if tracer is None or not tracer.isdigit():
            self._stop("unreadable")
            return
        self.samples += 1
        if (tracer != "0") != self.attached:
            self.attached = tracer != "0"
            self._log.write("tracer", attached=self.attached)

    def _stop(self, why: str) -> None:
        self.stopped = True
        self._log.write("sampling", state="stopped", why=why)


def _ptrace_scope() -> int | None:
    try:
        with open(YAMA, encoding="ascii") as stream:
            value = stream.read().strip()
    except OSError:
        return None
    return int(value) if value in ("0", "1", "2", "3") else None


class Watcher:
    """One run's watcher (the module's docstring); `proc` and `mountinfo` are the host's, injectable for tests."""

    def __init__(self, args: argparse.Namespace, proc: MP.ProcView, own_status: str, mountinfo: str) -> None:
        depth = MP.nspid(own_status)
        uid = MP.real_uid(own_status)
        if depth is None or uid is None:
            raise SystemExit("mountwatch: ERROR: this process's NSpid or uid cannot be read")
        pid, _, start = args.oneclient.partition(":")
        if not pid.isdigit() or not start.isdigit():
            raise SystemExit("mountwatch: ERROR: --oneclient takes <pid>:<start time>")
        self.log = Log(args.log)
        self.inotify = Inotify()
        self.token_name = os.fsencode(os.path.basename(args.token))
        watches = {
            "dir": self.inotify.watch(os.path.dirname(os.path.abspath(args.token)), "dir"),
            "token": self.inotify.watch(args.token, "token"),
        }
        try:
            devs = MP.proc_devs(mountinfo)
        except MountTableError:
            devs = frozenset()
        mountpoint = os.path.realpath(args.mountpoint)
        dev = MP.fuse_dev(mountinfo, mountpoint) if devs else None
        protected = tuple(MP.protected_at(path, mountinfo) for path in args.protect)
        self.host = MP.Host(len(depth), uid, devs, dev, mountpoint, protected, int(pid))
        self.log.write("start", format=FORMAT, nonce=args.nonce, pid=os.getpid(), uid=uid, nspid_depth=len(depth),
                       ptrace_scope=_ptrace_scope(), watches=watches, mount_seen=dev is not None)  # fmt: skip
        oc = proc_status(proc, int(pid), int(start))
        self.log.write("oneclient", pid=int(pid), seen=oc is not None, nspid_depth=oc)
        self.sampler = Sampler(proc, int(pid), int(start), self.log)
        self.cells = MP.CellPoll(proc, self.host, self._proof)
        self.armed, self.deleted = False, set()

    def _proof(self, label: str, ok: bool, problems: list[str], cell: dict[str, int]) -> None:
        self.log.write("proof", label=label, ok=ok, problems=problems, cell=cell)

    def events(self) -> None:
        for target, mask, name in self.inotify.read():
            if target is None:
                self.log.write("overflow")
                continue
            entry = "self" if not name else ("token" if name == self.token_name else "other")
            self.log.write("inotify", target=target, mask=mask_names(mask), entry=entry)
            self.deleted |= {(t, e, bit) for t, e, bit in CANARY if (t, e) == (target, entry) and mask & bit}
        if not self.armed and self.deleted == CANARY:
            self.armed = True
            self.log.write("armed")
            print(ARMED, flush=True)

    def tick(self) -> None:
        self.events()
        self.sampler.sample()
        self.cells.poll()

    def close(self) -> None:
        self.events()
        self.sampler.sample()
        self.cells.close()
        self.events()
        if self.armed:
            self.log.write("disarmed")
        self.log.write("end", lines=self.log.seq, samples=self.sampler.samples, polls=self.cells.polls,
                       polls_failed=self.cells.failed, unwritten=self.log.unwritten)  # fmt: skip


def proc_status(proc: MP.ProcView, pid: int, start: int) -> int | None:
    """oneclient's depth when it is the process named (pid and start time), else None."""
    try:
        if MP.start_time(proc.read(f"/proc/{pid}/stat")) != start:
            return None
        values = MP.nspid(proc.read(f"/proc/{pid}/status"))
    except OSError:
        return None
    return None if values is None else len(values)


def parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mountwatch")
    for name in ("--log", "--nonce", "--token", "--mountpoint", "--oneclient"):
        parser.add_argument(name, required=True)
    parser.add_argument("--protect", action="append", required=True)
    parser.add_argument("--poll-s", type=float, default=POLL_S)
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse(sys.argv[1:] if argv is None else argv)
    proc = MP.HostProc()
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(True))
    signal.signal(signal.SIGINT, lambda *_: stop.append(True))
    watcher = Watcher(args, proc, proc.read("/proc/self/status"), proc.read("/proc/self/mountinfo"))
    print(READY, flush=True)
    while not stop:
        select.select([watcher.inotify.fd], [], [], args.poll_s)
        watcher.tick()
    watcher.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
