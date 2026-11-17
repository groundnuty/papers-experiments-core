"""A fake HPC node for the Apptainer sandbox: `apptainer`, the helper processes and `/proc`, all in memory.

It implements `NodeOps` (expcore_onedata.sandbox.apptainer.node). Every `apptainer exec` is parsed back into its binds,
its environment, its image and its command, and kept in `execs`, so a test reads exactly what a cell was given. A cell
runs under the completion bootstrap (O55) as on the fake docker host (`fake_host`): the program is checked against the
size and hash its argv names, handed to `cell_handler`, and the record added to both streams; `exec_delivers` stands
for a stdin that loses bytes and a handler that returns `Unrecorded` for an exec whose record never came.

While a cell runs, its processes are in the process table in a PID namespace of their own, carrying the run's tag in
their environment and the nonce in their command line, as `apptainer exec --containall` would make them; the cell waits
until the watch has listed the table twice (`wait_for_watch`), so a watch's thread sees every cell without timing.

The helpers are emulated by what they do: the gate reads its token file when it starts (`gate_tokens`), says it is
ready, writes its run-start mark on SIGUSR1 and its teardown line on SIGTERM; the collector writes its start line, a
record for each connect a cell makes through `connect`, and its end line on SIGTERM. Everything else a test needs to
break (a gate that never gets ready, a helper that ignores its signals, an unreadable /proc) is a flag here. Every
name and address is invented.

A work disk image (O71) is made by an emulated `mkfs` (`mkfs_calls`): the image file is left as the backend made it, and
what the image holds is a backing directory beside it (`FakeImage`), which a cell bound to it reads and writes through
`Cell.work_dir`, `Cell.write_work` refusing a write past what it holds with ENOSPC. The start's probe and check of it
are answered from the image (`disk_probe_answer`, `disk_check_answer` override), and the collection's copy helper,
run in one-shot execs of the image, runs for real against the backing directory.

The driver's cgroup (`CGROUP`) charges the anonymous memory of every live process in it, hidden or not, plus
`cgroup_slack` (Apptainer's own hidden starter, the kernel's batching). A process made with `hidden=True` is what a
non-dumpable process is under `hidepid`: `/proc` never lists it, its memory is charged all the same, its `parent`'s
children name it, and it ends when the last visible process of its namespace does (the namespace's init exits).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import signal
import struct
import sys
import threading
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fake_host import Unrecorded, bootstrap_of, recorded

from expcore_onedata.sandbox import completion
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.apptainer import workdisk
from expcore_onedata.sandbox.apptainer.node import LocalNode, Spawned
from expcore_onedata.sandbox.apptainer.procs import Proc
from expcore_onedata.sandbox.containers import CERTIFI_PROBE
from expcore_onedata.sandbox.hostops import CmdResult, apply_output_cap, encode_input, run_bounded
from expcore_onedata.sandbox.sandbox import READ_ONLY_PROBE

OWN_NS = "pid:[4026531836]"
CGROUP = "/invented.slice/job_1/step_0/user/task_0"  # the driver's own, where its cells are charged
CERTIFI = "/usr/lib/invented/certifi/cacert.pem"
IMAGE_BUNDLE = "-----BEGIN CERTIFICATE-----\ninvented image bundle\n-----END CERTIFICATE-----\n"
CANARY_MARK = "canary open"


@dataclass
class FakeProc:
    pid: int
    start: str
    pidns: str
    comm: str
    environ: tuple[str, ...] = ()
    cmdline: tuple[str, ...] = ()
    rss: int | None = 1 << 20
    state: str = "S"
    alive: bool = True
    role: str = "cell"
    exit_status: int | None = None
    ignores: frozenset[int] = frozenset()  # signals it does not end on
    unkillable: bool = False  # a process in the kernel that no signal ends (a read of a dead FUSE mount)
    anon: int | None = None  # its RssAnon, the rss when None
    cgroup: str | None = CGROUP
    hidden: bool = False  # non-dumpable under hidepid: never listed, charged all the same
    parent: int | None = None  # the pid whose children name it
    threads: int | None = 1  # its tasks (`Threads:`); None: the status file has no such line
    cores: frozenset[int] | None = None  # the cores its tasks may run on; None: every core of the node

    @property
    def anon_bytes(self) -> int:
        return (self.rss or 0) if self.anon is None else self.anon

    def proc(self, tags: Collection[str]) -> Proc:
        found = frozenset(
            t for t in tags if any(e.startswith(t) for e in self.environ) or any(t in a for a in self.cmdline)
        )
        return Proc(self.pid, self.start, 1, self.state, self.comm, self.pidns, self.rss, found, self.anon_bytes,
                    self.cgroup, self.threads)  # fmt: skip


@dataclass
class Exec:
    """One `apptainer exec`, parsed back."""

    argv: list[str]
    flags: list[str]
    binds: dict[str, tuple[str, bool]]  # target -> (source, writable)
    env: dict[str, str]
    image: str
    command: list[str]
    stdin: str | None
    nofile: int | None = None  # the `prlimit --nofile=N:N` in front of a cell's `apptainer` (O70)
    cpus: list[int] | None = None  # the `taskset -c` in front of it
    images: dict[str, str] = field(default_factory=dict)  # target -> image-src, for a bind of an image's directory


@dataclass
class FakeImage:
    """A work disk image the fake's mkfs made: its size, and the directory that stands for what it holds."""

    size: int
    backing: Path

    @property
    def holds(self) -> int:
        """What a disk of this size holds, past its metadata and its reserved blocks."""
        return self.size * 9 // 10

    def used(self) -> int:
        return sum(p.lstat().st_size for p in self.backing.rglob("*") if p.is_file() and not p.is_symlink())


@dataclass
class Cell:
    """What a cell's handler is given: the node, the exec, the program that arrived and the cell's processes."""

    node: FakeNode
    exec: Exec
    program: str
    nonce: str
    pidns: str
    procs: list[FakeProc] = field(default_factory=list)

    def source(self, target: str) -> Path | None:
        found = self.exec.binds.get(target)
        return None if found is None else Path(found[0])

    def image(self) -> FakeImage | None:
        """The image whose directory is the cell's /work, None when /work is a directory of the node's."""
        if "/work" not in self.exec.images:
            return None
        return self.node.images[str(self.source("/work"))]

    def work_dir(self) -> Path:
        """Where the cell's /work is on the host: the run's directory, or the image's backing directory."""
        image = self.image()
        if image is None:
            return self.source("/work")
        return image.backing / self.exec.images["/work"].strip("/")

    def write_work(self, rel: str, size: int) -> int:
        """Write `size` bytes to /work/<rel> as a cell would: 0, or ENOSPC (nothing written) past the image's room."""
        image = self.image()
        if image is not None and image.used() + size > image.holds:
            return errno.ENOSPC
        (self.work_dir() / rel).write_bytes(b"\0" * size)
        return 0


@dataclass
class Collector:
    log: Path
    nonce: str
    proc: FakeProc
    seq: int = 0
    records: int = 0
    lines: list[str] = field(default_factory=list)

    def write(self, event: str, **fields: Any) -> None:
        line = dict(fields, event=event, seq=self.seq, wall=time.time(), mono=time.monotonic())
        with open(self.log, "a", encoding="ascii") as handle:
            handle.write(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n")
        self.seq += 1

    def connect(self, address: str, port: int, *, pid: int = 77, result: int = 0) -> None:
        base = {"pid": pid, "fd": 3, "time": "1.000000000", "type": "tcp", "family": "inet", "address": address,
                "port": port, "dropped": 0}  # fmt: skip
        peer = {"pid": pid, "uid": os.getuid()}
        self.write("record", peer=peer, rec={**base, "op": "connect", "result": None})
        self.write("record", peer=peer, rec={**base, "op": "result", "result": result})
        self.records += 2


@dataclass
class Gate:
    config: dict[str, Any]
    proc: FakeProc


class FakeNode:
    """The fake node (the module's docstring)."""

    def __init__(self, *, own_process: bool = False) -> None:
        """`own_process`: the driver is this process, by its pid and start time (`LocalNode.identity`), and a process
        the fake did not make is answered from the host's /proc (`LocalNode.alive`), as on a real node; so a run record
        left by a driver killed outright in another process names a driver that is gone, which the next start's reaper
        takes. Otherwise the driver is one invented process, alive on every fake node."""
        self.lock = threading.RLock()
        self.procs: dict[int, FakeProc] = {}
        self._pid, self._ns, self._port = 7000, 4026532000, 41000
        self.execs: list[Exec] = []
        self.spawned: list[list[str]] = []
        self.cell_handler: Callable[[Cell], CmdResult] = self.default_cell
        self.exec_delivers: Callable[[bytes], bytes] | None = None
        self.exec_input_error: str | None = None
        self.wait_for_watch = True
        self.listed = 0
        self.proc_unreadable = False
        self.gate_tokens: list[str] = []
        self.gates: list[Gate] = []
        self.collectors: list[Collector] = []
        self.gate_busy = 0  # this many gate starts fail as if their port were taken
        self.gate_never_ready = False
        self.helpers_ignore: frozenset[int] = frozenset()
        self.cell_left: list[FakeProc] = []  # processes a cell leaves behind (set by a handler)
        self.cgroup: dict[str, Any] = {"depth": 0, "max_bytes": 1 << 34, "events": {"oom": 0, "oom_kill": 0},
                                       "peak_bytes": 0}  # fmt: skip
        self.cgroup_path: str | None = CGROUP  # None: a v1 host
        self.cgroup_slack = 0  # anonymous bytes the cgroup charges that no process holds
        self.cgroup_unreadable = False
        self.children_unreadable = False
        self.cores = [0, 1, 2, 3]  # the driver's own affinity: the step's cores (`own_affinity`)
        self.step_set_unreadable = False
        self.affinity_unreadable = False  # a task's `Cpus_allowed_list` cannot be read
        self.starter_nofile: int | None = None  # a starter that sets the cell's hard open-file limit itself
        self.starter_cpus: frozenset[int] | None = None  # a starter that leaves the cell on these cores whatever
        self.canary_limits_missing = False  # a canary whose second line never comes
        self.host = LocalNode() if own_process else None
        self.owner = self.host.identity() if self.host is not None else Spawned(999, "4242")
        self.procs[self.owner.pid] = FakeProc(self.owner.pid, self.owner.start, OWN_NS, "driver", role="driver")
        self.signals: list[tuple[int, int]] = []
        self.mounts: set[str] = set()  # mount points in the driver's mount table (a mount's oneclient, say)
        self.busy_mounts: set[str] = set()  # those only a lazy unmount takes
        self.unmounts: list[list[str]] = []
        self.mount_table_unreadable = False
        self.images: dict[str, FakeImage] = {}  # by the image file's path (O71)
        self.mkfs_calls: list[list[str]] = []
        self.mkfs_fails = False
        self.disk_probe_answer: str | None = None  # what the start's probe of a work disk says, if not the image's
        self.disk_check_answer: str | None = None
        self.copy_helper_result: CmdResult | None = None  # what every copy exec of a work disk returns, if not its own
        self.boot: str | None = "00000000-0000-4000-8000-000000000001"  # this boot of the node (`boot_id`)
        self.mkfs_superblock = True  # whether the fake's mkfs writes an ext superblock into the image

    # ------------------------------------------------------------------------------------------------ processes
    def _new_proc(self, pidns: str, comm: str, **kw: Any) -> FakeProc:
        with self.lock:
            self._pid += 1
            proc = FakeProc(self._pid, str(100000 + self._pid), pidns, comm, **kw)
            self.procs[proc.pid] = proc
            return proc

    def new_namespace(self) -> str:
        with self.lock:
            self._ns += 1
            return f"pid:[{self._ns}]"

    def processes(self, tags: Collection[str] = ()) -> list[Proc] | None:
        with self.lock:
            self.listed += 1
            if self.proc_unreadable:
                return None
            return [p.proc(tags) for p in self.procs.values() if p.alive and not p.hidden]

    def own_cgroup(self) -> str | None:
        return self.cgroup_path

    def cgroup_anon(self, path: str) -> int | None:
        with self.lock:
            if self.cgroup_unreadable:
                return None
            inside = [p for p in self.procs.values() if p.alive and p.cgroup is not None and
                      (p.cgroup == path or p.cgroup.startswith(path + "/"))]  # fmt: skip
            return sum(p.anon_bytes for p in inside) + self.cgroup_slack

    def hidden_children(self, pid: int, start: str) -> list[int] | None:
        with self.lock:
            proc = self._find(pid, start)
            if proc is None or not proc.alive or proc.hidden:
                return []
            if self.children_unreadable:
                return None
            return sorted(p.pid for p in self.procs.values() if p.alive and p.hidden and p.parent == pid)

    def own_affinity(self) -> list[int] | None:
        return None if self.step_set_unreadable else sorted(self.cores)

    def task_affinity(self, pid: int, start: str) -> frozenset[int] | None:
        with self.lock:
            proc = self._find(pid, start)
            if proc is None or not proc.alive or proc.hidden:
                return frozenset()
            if self.affinity_unreadable:
                return None
            return proc.cores if proc.cores is not None else frozenset(self.cores)

    def _find(self, pid: int, start: str) -> FakeProc | None:
        proc = self.procs.get(pid)
        return proc if proc is not None and proc.start == start else None

    def signal(self, pid: int, start: str, sig: int) -> bool | None:
        with self.lock:
            proc = self._find(pid, start)
            if proc is None or not proc.alive:
                return False
            self.signals.append((pid, sig))
        if proc.role == "gate":
            self._gate_signal(proc, sig)
        elif proc.role == "collector":
            self._collector_signal(proc, sig)
        elif not proc.unkillable and (sig == signal.SIGKILL or sig not in proc.ignores):
            proc.alive, proc.exit_status = False, 128 + sig
            self._end_namespace_if_empty(proc.pidns)
        return True

    def _end_namespace_if_empty(self, pidns: str) -> None:
        """A cell's namespace ends with its init, when its last visible process is gone: its hidden ones with it."""
        with self.lock:
            if pidns == OWN_NS or any(p.alive and not p.hidden and p.pidns == pidns for p in self.procs.values()):
                return
            for p in self.procs.values():
                if p.pidns == pidns and p.hidden:
                    p.alive = False

    def alive(self, pid: int, start: str) -> bool | None:
        with self.lock:
            if self.host is not None and pid not in self.procs:
                return self.host.alive(pid, start)
            proc = self._find(pid, start)
            return proc is not None and proc.alive

    def exit_status(self, process: Spawned) -> int | None:
        proc = self._find(process.pid, process.start)
        return None if proc is None or proc.alive else proc.exit_status

    def identity(self) -> Spawned:
        return self.owner

    def own_pidns(self) -> str | None:
        return OWN_NS

    def free_port(self) -> int:
        with self.lock:
            self._port += 1
            return self._port

    def make_ca(self, directory: Path, names: Sequence[str]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "ca.pem").write_text("-----BEGIN CERTIFICATE-----\ninvented run CA\n-----END CERTIFICATE-----\n")
        (directory / "ca.key").write_text("invented CA key\n")
        for name in names:
            (directory / f"{name}.pem").write_text(f"invented leaf of {name}\n")
            (directory / f"{name}.key").write_text(f"invented key of {name}\n")

    def cgroup_memory(self) -> dict[str, Any] | None:
        return json.loads(json.dumps(self.cgroup))

    def node_facts(self, apptainer: Sequence[str]) -> dict[str, Any]:
        return {"kernel": "invented", "apptainer": "apptainer version invented"}

    def boot_id(self) -> str | None:
        return self.boot

    def run(self, argv: Sequence[str], timeout_s: float, *, sudo: bool = False, input: str | None = None) -> CmdResult:
        if argv and argv[0] == "fusermount3":
            return self._fusermount(list(argv))
        if argv and Path(argv[0]).name in ("mkfs.ext3", "mke2fs"):
            return self._mkfs(list(argv))
        return run_bounded(argv, timeout_s, input=input)

    def _mkfs(self, argv: list[str]) -> CmdResult:
        """`mkfs.ext3 -V`, or `mkfs.ext3 -q -F -d <tree> <image>`: the image's backing directory made from the tree."""
        self.mkfs_calls.append(argv)
        if argv[1:] == ["-V"]:
            return CmdResult(0, "", "mke2fs 1.47.0 (invented)\n\tUsing EXT2FS Library version 1.47.0\n")
        if self.mkfs_fails:
            return CmdResult(1, "", "mkfs.ext3: invented failure\n")
        image, tree = Path(argv[-1]), Path(argv[argv.index("-d") + 1])
        backing = image.with_name(image.name + ".d")
        if backing.exists():
            shutil.rmtree(backing)
        shutil.copytree(tree, backing, symlinks=True)
        size = image.stat().st_size
        self.images[str(image)] = FakeImage(size, backing)
        if self.mkfs_superblock:  # what workdisk.superblock reads: blocks of 4096, ext's magic, ext3's features
            raw = bytearray(1024)
            struct.pack_into("<I", raw, 4, size // 4096)
            struct.pack_into("<I", raw, 24, 2)
            struct.pack_into("<H", raw, 56, 0xEF53)
            struct.pack_into("<III", raw, 92, 0x3C, 0x2, 0x3)
            with open(image, "r+b") as handle:
                handle.seek(1024)
                handle.write(bytes(raw))
        return CmdResult(0, "")

    def exists(self, path: str | Path) -> bool:
        return Path(path).exists()

    def mounts_under(self, root: Path) -> list[str] | None:
        if self.mount_table_unreadable:
            return None
        base = str(root).rstrip("/") + "/"
        return sorted((m for m in self.mounts if m.startswith(base)), key=lambda m: (-m.count("/"), m))

    def _fusermount(self, argv: list[str]) -> CmdResult:
        """`fusermount3 -u` unmounts a mount whose daemon is gone; one in `busy_mounts` goes only with `-uz`."""
        self.unmounts.append(list(argv))
        mountpoint = argv[-1]
        if mountpoint in self.mounts and (argv[1] == "-uz" or mountpoint not in self.busy_mounts):
            self.mounts.discard(mountpoint)
            return CmdResult(0)
        return CmdResult(1, "", "fusermount3: failed to unmount: Device or resource busy\n")

    # ------------------------------------------------------------------------------------------------ helpers
    def spawn(self, argv: Sequence[str], *, stdout: Path, stderr: Path, env: dict[str, str] | None = None) -> Spawned:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        os.close(os.open(stdout, flags, 0o600))
        os.close(os.open(stderr, flags, 0o600))
        self.spawned.append(list(argv))
        environ = tuple(f"{k}={v}" for k, v in (env or {}).items())
        if any(a.endswith("gate.py") for a in argv):
            return self._spawn_gate(list(argv), stdout, stderr, environ)
        if any(a.endswith("egress-collector.py") for a in argv):
            return self._spawn_collector(list(argv), stdout, environ)
        raise AssertionError(f"the fake node spawns only the gate and the collector, not {argv[:3]}")

    def _spawn_gate(self, argv: list[str], stdout: Path, stderr: Path, environ: tuple[str, ...]) -> Spawned:
        config = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        proc = self._new_proc(OWN_NS, "python3", cmdline=tuple(argv), environ=environ, role="gate",
                              ignores=self.helpers_ignore)  # fmt: skip
        self.gates.append(Gate(config, proc))
        if self.gate_busy:
            self.gate_busy -= 1
            stderr.write_text("OSError: [Errno 98] Address already in use\n")
            proc.alive, proc.exit_status = False, 1
            return Spawned(proc.pid, proc.start)
        self.gate_tokens.append(Path(config["token_file"]).read_text(encoding="utf-8"))
        if not self.gate_never_ready:
            stdout.write_text("gate ready\n")
        return Spawned(proc.pid, proc.start)

    def _gate_signal(self, proc: FakeProc, sig: int) -> None:
        gate = next(g for g in self.gates if g.proc is proc)
        if sig == signal.SIGUSR1:
            with open(gate.config["log"], "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": "run_start", "before": []}) + "\n")
            return
        if sig in proc.ignores and sig != signal.SIGKILL:
            return
        if sig == signal.SIGTERM:
            with open(gate.config["log"], "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": "teardown_complete"}) + "\n")
        proc.alive, proc.exit_status = False, 0 if sig == signal.SIGTERM else 128 + sig

    def _spawn_collector(self, argv: list[str], stdout: Path, environ: tuple[str, ...]) -> Spawned:
        sock_dir, log, nonce = Path(argv[-3]), Path(argv[-2]), argv[-1]
        info = os.lstat(sock_dir)
        assert info.st_mode & 0o777 == 0o700, "the collector's socket directory is owner-only"
        assert not os.listdir(sock_dir), "the collector's socket directory is empty"
        assert not log.exists(), "the collector's log exists before it starts"
        proc = self._new_proc(OWN_NS, "python3", cmdline=tuple(argv), environ=environ, role="collector",
                              ignores=self.helpers_ignore)  # fmt: skip
        collector = Collector(log, nonce, proc)
        collector.write("start", format="hpcop-egress/1", nonce=nonce, pid=proc.pid, uid=os.getuid())
        self.collectors.append(collector)
        stdout.write_text("egress collector ready\n")
        return Spawned(proc.pid, proc.start)

    def _collector_signal(self, proc: FakeProc, sig: int) -> None:
        if sig in proc.ignores and sig != signal.SIGKILL:
            return
        collector = next(c for c in self.collectors if c.proc is proc)
        if sig == signal.SIGTERM:
            collector.write("end", records=collector.records, malformed=0, unwritten=0)
        proc.alive, proc.exit_status = False, 0 if sig == signal.SIGTERM else 128 + sig

    def collector(self) -> Collector:
        return self.collectors[-1]

    # ------------------------------------------------------------------------------------------------ apptainer
    def apptainer(
        self,
        argv: Sequence[str],
        timeout_s: float,
        *,
        input: str | None = None,
        output_cap: int | None = None,
        marker: bytes | None = None,
    ) -> CmdResult:
        parsed = parse_exec(list(argv), input)
        self.execs.append(parsed)
        boot = bootstrap_of(parsed.command)
        result = self._one_shot(parsed) if boot is None else self._run_cell(parsed, input or "", *boot)
        return apply_output_cap(result, output_cap, marker)

    def _one_shot(self, parsed: Exec) -> CmdResult:
        if parsed.command[1:5] == ["-I", "-B", "-c", workdisk.wrapper_source()]:
            return self._copy_helper(parsed)
        if parsed.command[-1] == CERTIFI_PROBE:
            return CmdResult(0, f"certifi:{CERTIFI}\n")
        if parsed.command[0] == "cat":
            return CmdResult(0, IMAGE_BUNDLE) if parsed.command[1] == CERTIFI else CmdResult(1, "", "no such file")
        raise AssertionError(f"the fake node runs no one-shot {parsed.command[:2]}")

    def _copy_helper(self, parsed: Exec) -> CmdResult:
        """The collection's copy helper in an exec of the image: run for real, its /work the image's backing directory
        and the staging directory where the exec binds it. While it runs, a process of its namespace carries the exec's
        environment, and the watch is given time to list it; one the watch killed makes the exec's exit 137."""
        environ = tuple(f"{k}={v}" for k, v in parsed.env.items())
        proc = self._new_proc(self.new_namespace(), "python3", environ=environ, cmdline=tuple(parsed.command[:4]))
        try:
            if self.wait_for_watch:
                seen, until = self.listed, time.monotonic() + 0.3
                while self.listed < seen + 10 and time.monotonic() < until:
                    time.sleep(0.005)
            if not proc.alive:
                return CmdResult(137, "", "")
            return self._copy_helper_run(parsed) if self.copy_helper_result is None else self.copy_helper_result
        finally:
            proc.alive = False

    def _copy_helper_run(self, parsed: Exec) -> CmdResult:
        image = self.images[parsed.binds["/work"][0]]
        export = parsed.binds[workdisk.EXPORT][0]
        request = json.loads(parsed.stdin or "{}")
        if request.get("root") == "/work":
            request["root"] = str(image.backing / parsed.images["/work"].strip("/"))
        if str(request.get("dest", "")).startswith(workdisk.EXPORT + "/"):
            request["dest"] = export + request["dest"][len(workdisk.EXPORT) :]
        argv = [sys.executable, "-I", "-B", "-c", parsed.command[4], parsed.command[5]]
        return run_bounded(argv, 120, input=json.dumps(request))

    def _run_cell(self, parsed: Exec, stdin: str, nonce: str, size: int, digest: str, python: str) -> CmdResult:
        sent = encode_input(stdin)
        got = self.exec_delivers(sent) if self.exec_delivers is not None else sent
        if not (len(got) == size and hashlib.sha256(got).hexdigest() == digest):
            record = {"source_bytes": len(got), "source_whole": False, "exit_status": None}
            result = recorded(CmdResult(completion.NOT_RUN_EXIT), nonce, record)
        else:
            cell = self._enter(parsed, got.decode("utf-8", "replace"), nonce)
            try:
                ran = self.cell_handler(cell)
            finally:
                self._leave(cell)
            if isinstance(ran, Unrecorded):
                result = CmdResult(ran.returncode, ran.stdout, ran.stderr, input_error=ran.input_error)
            else:
                status = 128 - ran.returncode if ran.returncode < 0 else ran.returncode
                result = recorded(ran, nonce, {"source_bytes": size, "source_whole": True, "exit_status": status})
        if self.exec_input_error is not None:
            result = CmdResult(result.returncode, result.stdout, result.stderr, input_error=self.exec_input_error)
        return result

    def _enter(self, parsed: Exec, program: str, nonce: str) -> Cell:
        """The cell's processes appear in a namespace of their own; the watch lists them twice before it goes on."""
        pidns = self.new_namespace()
        env = tuple(f"{k}={v}" for k, v in parsed.env.items())
        cores = None if parsed.cpus is None else frozenset(parsed.cpus)
        cores = self.starter_cpus if self.starter_cpus is not None else cores
        top = self._new_proc(pidns, "timeout", environ=env, cmdline=tuple(parsed.command), cores=cores)
        worker = self._new_proc(pidns, "python3", environ=env, cmdline=(parsed.command[4], "-"), cores=cores)
        cell = Cell(self, parsed, program, nonce, pidns, [top, worker])
        if self.wait_for_watch:
            seen, until = self.listed, time.monotonic() + 5.0
            while self.listed < seen + 2 and time.monotonic() < until:
                time.sleep(0.005)
        return cell

    def _leave(self, cell: Cell) -> None:
        """The exec returns: its processes end, but for those a handler left behind (`cell_left`)."""
        with self.lock:
            for proc in cell.procs:
                if proc not in self.cell_left and not proc.unkillable:
                    proc.alive = False
            self._end_namespace_if_empty(cell.pidns)

    def default_cell(self, cell: Cell) -> CmdResult:
        """The harness's own programs, as a cell would answer them; any other program prints nothing and exits 0."""
        if cell.program.startswith("import os, resource, socket, time"):  # the egress canary
            port = int(cell.program.split("'127.0.0.1', ")[1].split(")")[0])
            self.collector().connect("127.0.0.1", port)
            return CmdResult(0, CANARY_MARK + "\n" + ("" if self.canary_limits_missing else self.limits_line(cell)))
        if cell.program == READ_ONLY_PROBE.format(path="/ro-probe"):
            return CmdResult(0, "blocked 30\n")
        if cell.program.startswith(workdisk.PROBE_HEAD):  # a write past the work disk's size
            image = cell.image()
            said = f"enospc {image.holds}" if image is not None else f"unbounded {1 << 30}"
            if image is not None:  # the file it leaves for the start's copy out of the image
                (cell.work_dir() / workdisk.COPY_PROBE).write_bytes(workdisk.COPY_MARK)
            return CmdResult(0, (said if self.disk_probe_answer is None else self.disk_probe_answer) + "\n")
        if cell.program.startswith(workdisk.MARK_HEAD):  # the copy probe's file, on a start that relies on a proof
            (cell.work_dir() / workdisk.COPY_PROBE).write_bytes(workdisk.COPY_MARK)
            return CmdResult(0, "marked\n")
        if cell.program.startswith(workdisk.CHECK_HEAD):  # the work disk's size, whether it is /tmp's, its type
            image = cell.image()
            said = f"{image.holds} 1 ext3" if image is not None else f"{1 << 40} 0 tmpfs"
            return CmdResult(0, (said if self.disk_check_answer is None else self.disk_check_answer) + "\n")
        return CmdResult(0, "")

    def limits_line(self, cell: Cell) -> str:
        """What the canary's second line says in a cell: the open-file limit and the cores the cell was given, from the
        exec's `prlimit` and `taskset` as the kernel would apply them, unless the starter overrides them."""
        hard = self.starter_nofile or cell.exec.nofile or 1048576
        cores = sorted(self.starter_cpus or cell.exec.cpus or self.cores)
        return f"limits {hard} {hard} {','.join(str(c) for c in cores)}\n"


def _wrapper(head: list[str]) -> tuple[list[str], int | None, list[int] | None]:
    """The `prlimit` and `taskset` words in front of a cell's `apptainer` (`limits.prefix`), parsed; anything else in
    front of it fails loudly. `(the rest, the file limit, the cores)`."""
    nofile = cores = None
    if head[:1] == ["prlimit"]:
        assert head[1].startswith("--nofile="), f"the prefix's limit: {head[1]}"
        soft, _, hard = head[1].removeprefix("--nofile=").partition(":")
        assert soft == hard, f"a hard and soft limit alike: {head[1]}"
        assert soft.isdigit(), f"a number: {head[1]}"
        assert head[2] == "--", f"the prefix's separator: {head[2]}"
        nofile, head = int(soft), head[3:]
        if head[:2] == ["taskset", "-c"]:
            cores, head = [int(c) for c in head[2].split(",")], head[3:]
    assert head[:1] == ["apptainer"], f"an unexpected word in front of the exec: {head[:3]}"
    return head, nofile, cores


def parse_exec(argv: list[str], stdin: str | None) -> Exec:
    """An `apptainer exec` command line back into its parts; anything the backend does not write fails loudly."""
    at = argv.index("exec")
    head, nofile, cpus = _wrapper(argv[: at + 1])
    flags, rest = head, argv[at + 1 :]
    expected = ["--containall", "--cleanenv", "--no-home", "--no-mount", AV.NO_MOUNT, "--pwd", "/work"]
    assert rest[: len(expected)] == expected, f"the exec's flags: {rest[: len(expected)]}"
    rest = rest[len(expected) :]
    binds: dict[str, tuple[str, bool]] = {}
    images: dict[str, str] = {}
    env: dict[str, str] = {}
    while rest and rest[0] in ("-B", "--env"):
        option, value, rest = rest[0], rest[1], rest[2:]
        if option == "-B":
            parts = value.split(":")
            assert len(parts) in (2, 3), f"a bind: {value}"
            image = len(parts) == 3 and parts[2].startswith("image-src=/")
            assert len(parts) == 2 or parts[2] == "ro" or image, f"a bind's option: {value}"
            assert parts[1] not in binds, f"a target bound twice: {parts[1]}"
            binds[parts[1]] = (parts[0], len(parts) == 2 or image)
            if image:
                images[parts[1]] = parts[2][len("image-src=") :]
        else:
            name, _, text = value.partition("=")
            env[name] = text
    return Exec(argv, flags, binds, env, rest[0], rest[1:], stdin, nofile, cpus, images)


__all__ = ["CANARY_MARK", "CGROUP", "Cell", "Exec", "FakeImage", "FakeNode", "FakeProc", "Unrecorded", "parse_exec"]
