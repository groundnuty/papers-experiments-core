"""The read-log channel on the fake node (O91): the collector as a helper, and the client a script in a cell imports.

By default the collector is emulated by what it does (the way the fake node emulates the egress collector): it writes
its start line when it is spawned, a record line for each record a cell sends (`FakeReadLog.record`), and its end line
on SIGTERM; a SIGKILL leaves no end line. With `FakeNode.real_readlog = True` the node starts the REAL collector
program (the harness's own `LocalNode.spawn`, a process of this uid in a session of its own), and a cell's client
sends to its real Unix socket, so the log is what the collector wrote.

A cell is not run: its program is not executed (the fake node never does). So the client a script in the cell would
import is loaded here from the file the cell's bind holds (`client_of`), its fixed socket path (`/expcore-readlog/sock`,
which exists only inside a cell) replaced by the bind's source: the path the collector bound on the node. Every
name and address is invented.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from expcore_onedata.readlog import paths
from expcore_onedata.sandbox.apptainer.node import LocalNode, Spawned
from expcore_onedata.sandbox.hostops import CmdResult

if TYPE_CHECKING:
    from fake_node import Cell, FakeNode, FakeProc

SCRIPT = "readlog-collector.py"  # the name of the run's copy of the collector (the apptainer backend's)
READY = "readlog collector ready"
CANARY_SAID = "readlog canary sent"  # what the backend's canary program prints once the client returned


@dataclass
class FakeReadLog:
    """The emulated collector: the log it writes, the nonce and expected sender it was told, and how many execs there
    were when it was started (a collector must be up before any cell)."""

    log: Path
    nonce: str
    proc: FakeProc
    peer_uid: int
    execs_before: int
    seq: int = 0
    records: int = 0

    def write(self, event: str, **fields: Any) -> None:
        line = dict(fields, event=event, seq=self.seq, wall=time.time(), mono=time.monotonic())
        with open(self.log, "a", encoding="ascii") as handle:
            handle.write(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n")
        self.seq += 1

    def record(self, rec: dict[str, Any], *, pid: int = 77, uid: int | None = None) -> None:
        """A record as the collector writes it for a datagram of this sender."""
        self.write("record", peer={"pid": pid, "uid": self.peer_uid if uid is None else uid}, rec=rec)
        self.records += 1


def is_collector(argv: list[str]) -> bool:
    return any(a.endswith(SCRIPT) for a in argv)


def spawn(node: FakeNode, argv: list[str], stdout: Path, stderr: Path, environ: tuple[str, ...]) -> Spawned:
    """Start the collector: the real program when the node is told to, else the emulation."""
    sock_dir, log, nonce, uid = Path(argv[-4]), Path(argv[-3]), argv[-2], int(argv[-1])
    info = os.lstat(sock_dir)
    assert info.st_mode & 0o777 == 0o700, "the read-log collector's socket directory is owner-only"
    assert not os.listdir(sock_dir), "the read-log collector's socket directory is empty"
    assert not log.exists(), "the read-log collector's log exists before it starts"
    if node.real_readlog:
        if node.real_node is None:
            node.real_node = LocalNode()
        env = dict(item.split("=", 1) for item in environ)
        started = node.real_node.spawn(argv, stdout=stdout, stderr=stderr, env=env)
        node.real_helpers.add((started.pid, started.start))
        return started
    proc = node._new_proc(node.own_pidns(), "python3", cmdline=tuple(argv), environ=environ, role="readlog",
                          ignores=node.helpers_ignore)  # fmt: skip
    collector = FakeReadLog(log, nonce, proc, uid, len(node.execs))
    collector.write("start", format=paths.FORMAT, nonce=nonce, pid=proc.pid, uid=os.getuid(), peer_uid=uid,
                    max_bytes=paths.MAX_LOG_BYTES)  # fmt: skip
    node.readlogs.append(collector)
    stdout.write_text(READY + "\n")
    return Spawned(proc.pid, proc.start)


def signalled(node: FakeNode, proc: FakeProc, sig: int) -> None:
    """The emulated collector's reaction to a signal."""
    if sig in proc.ignores and sig != signal.SIGKILL:
        return
    collector = next(c for c in node.readlogs if c.proc is proc)
    if sig == signal.SIGTERM:
        collector.write("end", records=collector.records, malformed=0, unwritten=0, cap_reached=False)
    proc.alive, proc.exit_status = False, 0 if sig == signal.SIGTERM else 128 + sig


def client_of(cell: Cell) -> ModuleType:
    """The client module as the cell's bind holds it, its socket the bind's source: what a script in the cell imports
    by the explicit path, so a cell that has no such bind has no client."""
    folder, sockets = cell.source(paths.CLIENT_DIR), cell.source(paths.SOCKET_DIR)
    assert folder is not None, "the cell has no read-log client bind"
    assert sockets is not None, "the cell has no read-log socket bind"
    spec = importlib.util.spec_from_file_location(paths.CLIENT_MODULE, folder / paths.CLIENT_NAME)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SOCKET = str(sockets / paths.SOCKET_NAME)  # the one place a test differs from a cell: the path
    return module


def canary(node: FakeNode, cell: Cell) -> CmdResult:
    """The backend's canary program in a cell: the client sends the canary record; it says so once the client returned,
    and a failure is the program's traceback and exit status 1."""
    if node.real_readlog:
        try:
            client_of(cell).log_canary()
        except Exception as error:  # the program's own traceback: any failure of the client
            return CmdResult(1, "", f"{type(error).__name__}: {error}\n")
    else:
        node.readlog_collector().record({"v": paths.LINE_VERSION, "op": "canary", "item": paths.CANARY_ITEM})
    return CmdResult(0, CANARY_SAID + "\n")
