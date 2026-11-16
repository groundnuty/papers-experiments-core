"""The fake HPC node (`fake_node.FakeNode`) with the host mount's two processes emulated: oneclient and its watcher.

oneclient's wrapper (`sh -c HOST_START ...`) is emulated by what it does: it reads the token file its argv names (kept
in `oneclient_tokens`) and the FUSE mount appears at the last argument in `mountinfo()`, this uid's, read-only, without
`allow_other`; SIGTERM ends it and the mount. `fusermount3 -u` ends the mount too, unless `stuck_mount`.

The watcher (`-m expcore_onedata.sandbox.mountwatch`) writes its start and oneclient lines in the real format and says
it is ready; once the token file is gone (looked at whenever the harness asks whether a process is alive, as the real
one is woken by inotify) it writes the canary and `armed`; `touch()` writes an access in the window and `cell()` a
proof; SIGTERM writes `disarmed` and the end line. Every name and path is invented or the test's own.
"""

from __future__ import annotations

import json
import os
import signal
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fake_node import OWN_NS, FakeNode, FakeProc

from expcore_onedata.sandbox.apptainer.hostmount import SHELL_NAME
from expcore_onedata.sandbox.apptainer.node import Spawned
from expcore_onedata.sandbox.hostops import CmdResult

FUSE_DEV = "0:99999"
BASE_TABLE = "1 0 8:1 / / rw shared:1 - ext4 /dev/invented rw\n2 1 0:20 / /proc rw shared:2 - proc proc rw\n"


class Watch:
    def __init__(self, argv: list[str], stdout: Path, proc: FakeProc) -> None:
        get = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}
        self.log, self.nonce, self.token, self.stdout, self.proc = (
            Path(get["--log"]),
            get["--nonce"],
            Path(get["--token"]),
            stdout,
            proc,
        )
        self.argv, self.seq, self.armed = argv, 0, False
        self.protect = [argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "--protect"]

    def write(self, event: str, **fields: Any) -> None:
        line = dict(fields, event=event, seq=self.seq, wall=time.time(), mono=time.monotonic())
        with open(self.log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
        self.seq += 1

    def inotify(self, target: str, mask: str, entry: str = "self") -> None:
        self.write("inotify", target=target, mask=[mask], entry=entry)


class FakeMountNode(FakeNode):
    """The fake node with oneclient and the mount watcher (the module's docstring)."""

    def __init__(self) -> None:
        super().__init__()
        self.mounted_at: str | None = None
        self.oneclient_tokens: list[str] = []
        self.oneclient_argv: list[str] | None = None
        self.oneclient_fails = False
        self.mount_never = False
        self.stuck_mount = False
        self.watches: list[Watch] = []
        self.unmounts: list[list[str]] = []
        self.spawn_env: list[dict[str, str]] = []

    # ------------------------------------------------------------------------------------------------ the mount
    def mountinfo(self) -> str:
        line = ""
        if self.mounted_at is not None:
            owner = f"user_id={os.getuid()},group_id={os.getgid()}"
            line = f"3 1 {FUSE_DEV} / {self.mounted_at} ro,nosuid shared:3 - fuse.oneclient oneclient rw,{owner}\n"
        return BASE_TABLE + line

    def spawn(self, argv: Sequence[str], *, stdout: Path, stderr: Path, env: dict[str, str] | None = None) -> Spawned:
        if list(argv[:2]) == ["sh", "-c"] and SHELL_NAME in argv:
            os.close(os.open(stdout, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            os.close(os.open(stderr, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            self.spawned.append(list(argv))
            self.spawn_env.append(dict(env or {}))
            return self._spawn_oneclient(list(argv))
        if "expcore_onedata.sandbox.mountwatch" in argv:
            os.close(os.open(stdout, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            os.close(os.open(stderr, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            self.spawned.append(list(argv))
            self.spawn_env.append(dict(env or {}))
            return self._spawn_watch(list(argv), stdout)
        return super().spawn(argv, stdout=stdout, stderr=stderr, env=env)

    def _spawn_oneclient(self, argv: list[str]) -> Spawned:
        self.oneclient_argv = argv
        self.oneclient_tokens.append(Path(argv[4]).read_text(encoding="utf-8"))
        proc = self._new_proc(OWN_NS, "oneclient", cmdline=tuple(argv), role="oneclient")
        if self.oneclient_fails:
            proc.alive, proc.exit_status = False, 1
        elif not self.mount_never:
            self.mounted_at = argv[-1]
        return Spawned(proc.pid, proc.start)

    def _spawn_watch(self, argv: list[str], stdout: Path) -> Spawned:
        proc = self._new_proc(OWN_NS, "python3", cmdline=tuple(argv), role="mountwatch")
        watch = Watch(argv, stdout, proc)
        assert not watch.log.exists(), "the watcher's log exists before it starts"
        assert watch.token.exists(), "the watcher starts while the token file is there"
        assert self.mounted_at is not None, "the watcher starts once the mount is up"
        watch.write("start", format="expcore-mountwatch/1", nonce=watch.nonce, pid=proc.pid, uid=os.getuid(),
                    nspid_depth=1, ptrace_scope=1, watches={"dir": True, "token": True}, mount_seen=True)  # fmt: skip
        watch.write("oneclient", pid=99, seen=True, nspid_depth=1)
        self.watches.append(watch)
        stdout.write_text("mount watch ready\n")
        return Spawned(proc.pid, proc.start)

    def watch(self) -> Watch:
        return self.watches[-1]

    def touch(self) -> None:
        """An access to the token's directory in the window."""
        self.watch().inotify("dir", "IN_OPEN")

    def cell(self, ok: bool = True) -> None:
        """A proof of a cell the watcher caught."""
        problems = [] if ok else ["same_pid_namespace"]
        self.watch().write("proof", label="cell", ok=ok, problems=problems,
                           cell={"pid": 4242, "start_time": 777, "nspid_depth": 2})  # fmt: skip

    def alive(self, pid: int, start: str) -> bool | None:
        for watch in self.watches:
            if watch.proc.alive and not watch.armed and not watch.token.exists():
                watch.inotify("token", "IN_DELETE_SELF")
                watch.inotify("dir", "IN_DELETE", "token")
                watch.write("armed")
                watch.armed = True
                with open(watch.stdout, "a", encoding="utf-8") as handle:
                    handle.write("mount watch armed\n")
        return super().alive(pid, start)

    def signal(self, pid: int, start: str, sig: int) -> bool | None:
        proc = self._find(pid, start)
        if proc is not None and proc.alive and proc.role in ("oneclient", "mountwatch"):
            self.signals.append((pid, sig))
            if proc.role == "mountwatch" and sig == signal.SIGTERM:
                watch = next(w for w in self.watches if w.proc is proc)
                if watch.armed:
                    watch.write("disarmed")
                watch.write("end", lines=watch.seq, samples=3, polls=5, polls_failed=0, unwritten=0)
            if proc.role == "oneclient" and not self.stuck_mount:
                self.mounted_at = None
            proc.alive, proc.exit_status = False, 0 if sig == signal.SIGTERM else 128 + sig
            return True
        return super().signal(pid, start, sig)

    def run(self, argv: Sequence[str], timeout_s: float, *, sudo: bool = False, input: str | None = None) -> CmdResult:
        if argv and argv[0] == "fusermount3":
            self.unmounts.append(list(argv))
            if not self.stuck_mount:
                self.mounted_at = None
            return CmdResult(0, "", "")
        return super().run(argv, timeout_s, sudo=sudo, input=input)
