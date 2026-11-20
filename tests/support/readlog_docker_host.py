"""The read-log collector's container on the fake host: a stand-in that writes the log the collector would, or the REAL
collector process (`expcore_onedata/readlog/collector.py`) with the container's paths mapped to the host's binds.

`Collector(host)` installs itself on a `FakeHost` instance (the fake's own methods keep working for every other
container). Stand-in mode writes, at SIGTERM, a log in the collector's format (`readlog_logs`) of the records a test
put in `records`; real mode starts the collector as `python -I -B collector.py <socket dir> <log> <nonce> <uid>` (the
options the sandbox's own command carries) on the bound host directories, and the test's own process is the agent (the
canary through the real client, at the agent's bound socket directory), so the real socket, the real client and the
real reader are in play.

Knobs a test sets before the start: `ready` (the container says it is ready), `exits_before_ready`, `canary` (the
canary cell's answer, stand-in mode), `ignores_term`, `complete` (a TERM writes an end line), `writes_log` (the
stand-in leaves a log at all), `end_on_kill` (a kill leaves an end line), `term_loses_race`. `execs` holds each
`docker exec` the fake saw, with its arguments (the user the cell ran as among them).
"""

import contextlib
import io
import os
import select
import signal
import subprocess
import sys
from pathlib import Path

from fake_host import FakeContainer, FakeHost, _parse_run, bound_files
from readlog_logs import encode, lines

from expcore_onedata.readlog import paths
from expcore_onedata.sandbox import containers as C
from expcore_onedata.sandbox.hostops import CmdResult

LOG = "readlog.jsonl"


class Collector:
    def __init__(self, host: FakeHost, *, real: bool = False) -> None:
        self.host, self.real = host, real
        self.ready = True
        self.exits_before_ready = False
        self.canary = CmdResult(0, "canary sent\n")
        self.ignores_term = False
        self.complete = True
        self.writes_log = True
        self.end_on_kill = False  # a collector that needed the kill and still left an end line
        self.term_loses_race = False  # it ends by itself just before the harness's TERM, which then fails
        self.records: list[dict] = []
        self.execs: list[tuple[list[str], str | None]] = []
        self.container: FakeContainer | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self._binds: dict[str, str] = {}
        self._run, self._kill, self._exec, self._rm = host._run_container, host._kill, host._exec, host._rm
        self._handler = host.exec_handler
        host._run_container, host._kill, host._exec, host._rm = self.run_container, self.kill, self.exec, self.rm
        host.exec_handler = self.handler

    # ---------------------------------------------------------------- the host's view
    @property
    def log_path(self) -> Path:
        return Path(self._binds[C.READLOG_LOG_DIR]) / LOG

    @property
    def socket_path(self) -> str:
        return str(Path(self._binds[C.READLOG_SOCKET_DIR]) / paths.SOCKET_NAME)

    def agent_binds(self) -> dict[str, str]:
        """The agent container's binds by destination (the one that has the channel's two targets)."""
        for container in self.host.containers.values():
            binds = {dst: src for src, dst in bound_files(container.options)}
            if paths.SOCKET_DIR in binds:
                return binds
        raise AssertionError("no container has the read-log socket directory")

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)
        if self.proc is not None:
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream is not None:
                    stream.close()

    # ---------------------------------------------------------------- docker run of the collector
    def run_container(self, args: list[str], env) -> CmdResult:
        done = self._run(args, env)
        options, _, command = _parse_run(args)
        name = options.get("--name", "")
        if done.returncode != 0 or not name.endswith(C.READLOG_SUFFIX):
            return done
        container = self.container = self.host.containers[name]
        self._binds = {dst: src for src, dst in bound_files(options)}
        if self.exits_before_ready:
            container.running, container.exit_code, container.logs = False, 1, ""
        elif self.real:
            self._spawn(container, command)
        else:
            container.logs = C.READLOG_READY + "\n" if self.ready else ""
        return done

    def _spawn(self, container: FakeContainer, command: list[str]) -> None:
        """The real collector, with the container's paths turned into the host directories bound there."""
        nonce, uid = command[-2], command[-1]
        options = command[: command.index(f"{C.READLOG_PROGRAM_DIR}/{C.READLOG_PROGRAM_NAME}")]  # `-I -B`, as built
        argv = [
            sys.executable, *options, str(Path(self._binds[C.READLOG_PROGRAM_DIR]) / C.READLOG_PROGRAM_NAME),
            self._binds[C.READLOG_SOCKET_DIR], str(self.log_path), nonce, uid,
        ]  # fmt: skip
        self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ready, _, _ = select.select([self.proc.stdout], [], [], 30)
        said = self.proc.stdout.readline() if ready else b""
        if C.READLOG_READY.encode() not in said:
            self.proc.kill()
            _, err = self.proc.communicate()
            container.running, container.exit_code, container.logs = False, 2, err.decode("utf-8", "replace")
            return
        container.logs = C.READLOG_READY + "\n"

    # ---------------------------------------------------------------- signals
    def kill(self, args: list[str]) -> CmdResult:
        container = self.host._find(args[-1])
        if container is None or container is not self.container or not container.running:
            return self._kill(args)
        sig = args[args.index("-s") + 1] if "-s" in args else "KILL"
        self.host.calls.append(("signalled", container.name, sig))
        if sig == "TERM" and self.ignores_term:
            return CmdResult(0, container.name + "\n")
        if sig == "TERM" and self.term_loses_race:  # it has just ended on someone else's TERM: nothing to signal
            self._write_log(end=True, uid=int(container.command[-1]), nonce=container.command[-2])
            container.running = False
            self.host.alive.discard(container.pid)
            return CmdResult(1, "", f"container {container.name} is not running")
        if self.proc is not None:
            self.proc.send_signal(signal.SIGTERM if sig == "TERM" else signal.SIGKILL)
            container.exit_code = self.proc.wait(timeout=30)
        elif self.writes_log:
            end = self.complete if sig == "TERM" else self.end_on_kill
            self._write_log(end=end, uid=int(container.command[-1]), nonce=container.command[-2])
        container.running = False
        self.host.alive.discard(container.pid)
        return CmdResult(0, container.name + "\n")

    def _write_log(self, *, end: bool, uid: int, nonce: str) -> None:
        """The log the stand-in leaves: the canary, the test's records, and an end line when it was stopped."""
        data = encode(lines(self.records, nonce=nonce, peer_uid=uid, end=end))
        self.log_path.write_bytes(data)
        self.log_path.chmod(0o600)

    def rm(self, args: list[str]) -> CmdResult:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)
        return self._rm(args)

    # ---------------------------------------------------------------- the cells
    def exec(self, args: list[str], stdin: str | None) -> CmdResult:
        self.execs.append((list(args), stdin))
        return self._exec(args, stdin)

    def handler(self, container: FakeContainer, argv: list[str], stdin: str | None) -> CmdResult:
        if stdin and "log_canary" in stdin:
            return self._real_canary(stdin) if self.real else self.canary
        return self._handler(container, argv, stdin)

    def _real_canary(self, source: str) -> CmdResult:
        """The harness's canary cell run for real, in this process, with the agent's two targets at the host's binds:
        the real client sends the first record to the real collector's socket."""
        binds = self.agent_binds()
        client = str(Path(binds[paths.CLIENT_DIR]) / paths.CLIENT_NAME)
        socket_path = str(Path(binds[paths.SOCKET_DIR]) / paths.SOCKET_NAME)
        source = source.replace(paths.CLIENT, client)
        source = source.replace("client.log_canary()", f"client.SOCKET = {socket_path!r}\nclient.log_canary()")
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                exec(compile(source, "<canary>", "exec"), {})
        except Exception as e:  # the cell's own failure: its traceback is what its stderr would hold
            return CmdResult(1, out.getvalue(), repr(e))
        return CmdResult(0, out.getvalue())


def agent_uid_is_ours(settings):
    """`settings` with the declared agent uid this process's own: a send from the test is then a send from the agent."""
    import dataclasses

    return dataclasses.replace(settings, agent_uid=os.getuid())
