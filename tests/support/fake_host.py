"""A fake host for the sandbox: docker's containers and networks, mounts, host files and processes, all in memory.

It implements `HostOps` (expcore_onedata.sandbox.hostops) and records every effect in `calls`, in order, so a test can
assert the order of a teardown. Work that needs a real directory (the sandbox's own files) uses the test's `tmp_path`.
The copy helper of a copy out of the sandbox (`copyhelper.py`, run with this interpreter) and `chmod` run for real, as
this user and never under sudo, on the test's own directories, so a test sees what the helper does with a link, a FIFO
or an odd name; a test that wants another answer wraps `run`. `chown` only records (this user cannot give a file to
root), and `rm -rf` makes read-only directories writable before it removes them, as root needs not. `docker pause`
freezes a container: its cgroup says `frozen 1` until `unpause`, and nothing can be run in it meanwhile. Names, images
and addresses are invented. The fake knows only the docker
commands the package uses; anything else fails loudly, so a new command shows up as a test failure and not as a silent
pass.

A cell runs under the completion bootstrap (`completion.py`, O55), and the fake plays its part: it checks the program it
was given against the size and hash the argv names, hands the test's `exec_handler` the program as the cell's stdin,
and adds the completion record to both streams of what the handler returns, with its exit status, as the bootstrap
would after the program's exit. `exec_delivers` stands for a stdin that loses bytes on the way (the bootstrap then
records a source that is not whole, and runs nothing); `exec_input_error` for a write of the program that `docker exec`
cut short; a handler that returns `Unrecorded` stands for an exec whose record never came (the stream ended first, or
docker reported an exit it never saw).
"""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fake_elf import elf

from expcore_onedata.sandbox import completion
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.cellresolv import PROBE as RESOLV_PROBE
from expcore_onedata.sandbox.cellresolv import RESOLV_CONF
from expcore_onedata.sandbox.containers import ONECLIENT_BIN
from expcore_onedata.sandbox.hostops import CmdResult, apply_output_cap, encode_input
from expcore_onedata.sandbox.inputs import PROBE_MARK
from expcore_onedata.sandbox.oneclient_build import SLICE, SLICE_NAME

VALUED = {
    "--name", "--network", "--label", "-l", "--user", "-u", "--cpus", "--memory", "--memory-swap", "--pids-limit",
    "--ulimit", "--tmpfs", "--shm-size", "-v", "--volume", "--mount", "-e", "--env", "--add-host", "--cap-add",
    "--cap-drop", "--security-opt", "--entrypoint", "--oom-score-adj", "--device", "-w", "--workdir", "--hostname",
}  # fmt: skip
REPEATED = {"--label", "-l", "-v", "--volume", "--mount", "-e", "--env", "--add-host", "--cap-add", "--cap-drop"}
BOOT_ID = "0f0f0f0f-1111-2222-3333-444455556666"
CERTIFI_BUNDLE = "/usr/lib/python3/site-packages/certifi/cacert.pem"  # where the fake agent image keeps certifi's
PID_NS = "pid:[4026531836]"
ONECLIENT_CONFIG_PATH = "/etc/invented/oneclient.conf"  # where the fake oneclient image keeps the file its help names
ONECLIENT_CONFIG_SHA256 = hashlib.sha256(b"an invented oneclient configuration file\n").hexdigest()
ONECLIENT_VERSION_TEXT = "Oneclient: 25.0\nFUSE library: 3.2\n"  # what the fake oneclient image's `--version` prints
ONECLIENT_BUILD_ID = "ab" * 20  # the GNU build id of the fake oneclient image's binary
# the /etc/resolv.conf the engine makes for a container on a user-defined network, with the host's `search` line copied
ENGINE_RESOLV = "search host-search.invented.test\nnameserver 127.0.0.11\noptions ndots:0\n"


class Unrecorded(CmdResult):
    """What `docker exec` returned for a cell, as it is: no completion record is added (O55)."""


def is_signal_helper(argv: Sequence[str]) -> bool:
    """The root half of a signal (`killhelper.py`), run as `PYTHON -I -c SOURCE TERM|KILL` with its request on stdin."""
    return bool(argv) and argv[0] == sys.executable and "-c" in argv and argv[-1] in ("TERM", "KILL")


def is_snapshot_helper(argv: Sequence[str]) -> bool:
    """The root half of the Docker diagnosis (`procsnap.py`, O97), run as `PYTHON -I -B -c SOURCE` with its request on
    stdin: the program is the helper's own source, which is what tells it from any other `-c` command."""
    from expcore_onedata.sandbox import sudoproc  # not before it exists: the old code never calls the helper

    return bool(argv) and argv[0] == sys.executable and "-c" in argv and sudoproc.PROGRAM in argv


def bootstrap_of(argv: Sequence[str]) -> tuple[str, int, str, str] | None:
    """The nonce, size, hash and interpreter of a cell's bootstrap argv (`completion.Completion.argv`), or None."""
    if completion.BOOTSTRAP not in argv:
        return None
    at = list(argv).index(completion.BOOTSTRAP)
    nonce, size, digest, python = argv[at + 1 : at + 5]
    return nonce, int(size), digest, python


def recorded(result: CmdResult, nonce: str, record: dict[str, Any]) -> CmdResult:
    """`result` with the completion record the bootstrap writes on both streams after the program's exit."""
    line = completion.record_line(nonce, record)
    return CmdResult(result.returncode, result.stdout + line, result.stderr + line, input_error=result.input_error)


@dataclass
class FakeContainer:
    name: str
    id: str
    image: str
    options: dict[str, Any]
    command: list[str]
    labels: dict[str, str]
    networks: dict[str, str] = field(default_factory=dict)
    running: bool = True
    pid: int = 0
    exit_code: int = 0
    oom_killed: bool = False
    logs: str = ""
    started_at: str = "2026-01-01T00:00:00Z"
    paused: bool = False
    init_child: int = 0  # the `sleep` docker-init started, in a container run with `--init ... sleep infinity`

    def mount_specs(self) -> list[str]:
        return [*self.options.get("-v", []), *self.options.get("--mount", [])]

    def hostname(self) -> str:
        """What `hostname` prints in the container: `--hostname`, else the engine's own choice, its short id."""
        return self.options.get("--hostname") or self.id[:12]

    def file(self, path: str) -> str | None:
        """What a cell of the container reads at `path`: the text of a file bound there when the container started, and
        for `/etc/resolv.conf` with no bind the engine's own (`ENGINE_RESOLV`); None for any other path."""
        bound = self.options.get("_files", {})
        if path in bound:
            return bound[path]
        return ENGINE_RESOLV if path == RESOLV_CONF else None


def _mounts_json(c: FakeContainer) -> list[dict[str, str]]:
    """The container's bind mounts as `docker inspect` lists them: source and destination."""
    out = []
    for spec in c.options.get("--mount", []):
        fields = dict(part.split("=", 1) for part in spec.split(",") if "=" in part)
        out.append({"Source": fields.get("src", ""), "Destination": fields.get("dst", "")})
    for spec in c.options.get("-v", []):
        parts = spec.split(":")
        out.append({"Source": parts[0], "Destination": parts[1] if len(parts) > 1 else ""})
    return out


def bound_files(options: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Each bind of `docker run`'s options, `-v` or `--mount type=bind`, as (host path, path in the container)."""
    out = []
    for spec in options.get("-v", []):
        source, destination, *_ = spec.split(":")
        out.append((source, destination))
    for spec in options.get("--mount", []):
        fields = dict(part.split("=", 1) for part in spec.split(",") if "=" in part)
        if fields.get("type") == "bind":
            out.append((fields["src"], fields["dst"]))
    return out


def writable(path: str) -> None:
    """Make every directory under `path` writable by this user, as root needs not, so that it can be removed."""
    for top, dirs, _ in os.walk(path):
        for name in [".", *dirs]:
            full = os.path.join(top, name)
            if not os.path.islink(full):
                os.chmod(full, 0o700)


def _config_env(c: FakeContainer) -> list[str]:
    """The container's environment as docker keeps it in its configuration, which `docker inspect` shows: each `-e
    NAME=value`, and each `-e NAME` with the value the docker client's environment held for it."""
    client = c.options.get("_env") or {}
    named = c.options.get("-e", [])
    return [e if "=" in e else f"{e}={client[e]}" for e in named if "=" in e or e in client]


def _inspect_json(c: FakeContainer) -> dict[str, Any]:
    return {
        "Id": c.id,
        "Name": "/" + c.name,
        "Image": c.image,
        "Config": {
            "Labels": c.labels,
            "Env": _config_env(c),
            "Entrypoint": [c.options["--entrypoint"]] if "--entrypoint" in c.options else None,
            "Cmd": c.command,
        },
        "State": {
            "Status": ("paused" if c.paused else "running") if c.running else "exited",
            "Running": c.running,
            "Paused": c.paused,
            "Pid": c.pid if c.running else 0,
            "ExitCode": c.exit_code,
            "OOMKilled": c.oom_killed,
            "StartedAt": c.started_at,
            "FinishedAt": "0001-01-01T00:00:00Z" if c.running else "2026-01-01T00:10:00Z",
        },
        "Mounts": _mounts_json(c),
        "NetworkSettings": {"Networks": {n: {"IPAddress": ip} for n, ip in c.networks.items()}},
    }


def _parse_run(args: Sequence[str]) -> tuple[dict[str, Any], str, list[str]]:
    """Split `docker run` arguments into options, image and command."""
    options: dict[str, Any] = {}
    i = 0
    while i < len(args) and args[i].startswith("-"):
        flag = args[i]
        if flag in VALUED:
            value = args[i + 1]
            i += 2
            if flag in REPEATED:
                options.setdefault(flag, []).append(value)
            else:
                options[flag] = value
        else:
            options[flag] = True
            i += 1
    return options, args[i], list(args[i + 1 :])


READINGS = {"os": {"id": "invented", "version_id": "9.9"}, "glibc": "glibc 2.99", "kernel": "9.99.0-invented"}


class FakeHost:
    """An in-memory host. Set `images`, `files`, `mounts` and the handlers before the code under test runs."""

    def __init__(self, work_root: Path | None = None) -> None:
        self.lock = threading.RLock()
        self.work_root = work_root
        self.readings: Any = READINGS  # what `host_readings` answers (O87); a test sets another, or `readings_error`
        self.readings_error: Exception | None = None
        self.images: dict[str, str] = {}
        self.containers: dict[str, FakeContainer] = {}
        self.networks: dict[str, dict[str, Any]] = {}
        self.mounts: dict[str, str] = {}  # mount point -> filesystem type
        self.files: dict[str, str] = {
            "/proc/sys/kernel/random/boot_id": BOOT_ID + "\n",
            "/proc/uptime": "1000.00 900.00\n",
        }
        self.links = {"/proc/self/ns/pid": PID_NS}
        self.alive: set[int] = set()
        # processes a signal does not end: a read waiting in the kernel on a FUSE mount, which takes its kill only when
        # it returns (O34); a test releases one by removing it from here and from /proc
        self.unkillable: set[int] = set()
        self.init_starts_sleep = True  # docker-init starts the `sleep` of a `--init ... sleep infinity` container
        self.calls: list[tuple] = []
        self.routes = "default via 192.0.2.1 dev eth0\n"
        self.dmesg = ""
        self.next_pid = 5000
        self.sudo_commands: list[list[str]] = []
        self.exec_handler: Callable[[FakeContainer, list[str], str | None], CmdResult] = self._default_exec
        self.exec_delivers: Callable[[bytes], bytes] | None = None  # what of a cell's program reaches the container
        self.exec_input_error: str | None = None  # a write of a cell's program that docker exec cut short
        self.journal = ""
        self.run_handlers: dict[str, Callable[[list[str]], CmdResult]] = {}
        self.fail_remove: set[str] = set()
        self.fail_run: set[str] = set()
        self.fail_network_create: list[str] = []
        self.stuck_mounts: set[str] = set()
        self.denied: set[str] = set()  # files whose read raises PermissionError, as /proc does with hidepid
        self.mount_ids: dict[str, int] = {}  # a mount keeps its id when the directory above it is renamed
        self._next_mount_id = 40  # an unmounted path that is mounted again is a new mount, with an id of its own
        # the agent container's own mount table (`/proc/<pid>/mountinfo` of its init, read as root): the shares bound
        # into it with `rslave` and the FUSE mounts on them, as the kernel's mount propagation shows them there (see
        # `agent_mountinfo`). On by default, since the start reads it (`DockerSandbox._wait_agent_mount`); a test may
        # turn it off, and `ns_override` gives a table (by pid) in place of this one
        self.ns_mountinfo = True
        self.ns_override: dict[int, str] = {}
        # the real kernel lists one FUSE mount at the share twice in the host's table: the share's bind is a peer of the
        # volume mount under it, so a mount stacked on the bind is propagated to that peer as well (measured on the
        # study VM: two entries, one device). On by default, so no test assumes one entry; a test may turn it off
        self.fuse_peer_entry = True
        # the kernel hands a freed device number to the next mount, depending on how soon the dead superblock is
        # released (measured live: one restore came up on the dead mount's device number, another on a new one). With
        # this on, a path keeps the device number it first had across unmounts and remounts, while its mount id is a new
        # one each time, as the kernel's is
        self.reuse_freed_devices = False
        self._device_of: dict[str, int] = {}
        # the bytes of a process's memory (`/proc/<pid>/task/<tid>/mem`), by that path, for the diagnosis's root half
        self.proc_bytes: dict[str, bytes] = {}
        self.fusectl_state = "writable"  # what the root half's check of the connection's abort file answers
        self.one_shots: list[FakeContainer] = []  # the `--rm` containers that were run, as they were labelled
        self.stuck_one_shots = False  # a `--rm` container that docker does not remove: it stays, running
        self.oneclient_images: set[str] = set()
        # what a oneclient container answers the sandbox's two reads of its binary (`oneclient_build`, O65): its
        # `--version`, and the file at ONECLIENT_BIN (bytes, or the whole result the read gets); the test's
        # `exec_handler` never sees either
        self.oneclient_version = CmdResult(0, ONECLIENT_VERSION_TEXT)
        self.oneclient_binary: bytes | CmdResult = elf(
            interp="/opt/invented/lib/ld.so", needed=("libc.so.6",), build_id=bytes.fromhex(ONECLIENT_BUILD_ID)
        )
        self.pause_without_freezing = False  # a `docker pause` that answers 0 but leaves the cgroup unfrozen
        # an engine that writes its own /etc/resolv.conf whatever a bind names, so a cell reads `ENGINE_RESOLV`
        self.engine_resolv_wins = False
        self.signal_handlers: dict[str, Callable[[FakeContainer], None]] = {}  # a signal the process handles and lives
        self.base_bundle = "BASE BUNDLE\n"
        # what the agent image's interpreter answers the sandbox's question of where its certifi bundle is
        self.certifi_probe = CmdResult(0, f"certifi:{CERTIFI_BUNDLE}\n")
        self.add_process(__import__("os").getpid(), start=777)

    @staticmethod
    def reading_resolv(handler: Callable[[FakeContainer, list[str], str | None], CmdResult]):
        """`handler` for every program but the start's read of /etc/resolv.conf (O56), which the fake answers from what
        the container holds, as `_default_exec` does: for a test whose handler answers the other start probes alone."""

        def answering(container: FakeContainer, argv: list[str], stdin: str | None) -> CmdResult:
            if stdin == RESOLV_PROBE:
                return FakeHost._default_exec(container, argv, stdin)
            return handler(container, argv, stdin)

        return answering

    @staticmethod
    def _default_exec(container: FakeContainer, argv: list[str], stdin: str | None) -> CmdResult:
        """The agent's code does nothing, and the read-only probes are refused as a read-only filesystem refuses them:
        the root's and the mount's write, and every change to a read-only input, which the agent can still read and
        run."""
        if stdin == RESOLV_PROBE:  # a cell reads its /etc/resolv.conf back and says the hash of the bytes
            seen = (container.file(RESOLV_CONF) or "").encode()
            return CmdResult(0, f"resolv {hashlib.sha256(seen).hexdigest()}\n")
        if stdin and PROBE_MARK in stdin:
            refused = dict.fromkeys(("write", "beside", "mkdir", "rename", "chmod", "delete"), 30)
            return CmdResult(0, json.dumps({"read": "done", "run": True, **refused, "work": "done"}) + "\n")
        if stdin and "open(" in stdin and "'w'" in stdin:
            return CmdResult(0, "blocked 30\n")
        return CmdResult(0)

    # ---------------------------------------------------------------- setup helpers
    def add_image(self, reference: str, image_id: str) -> None:
        self.images[reference] = image_id
        self.images[image_id] = image_id

    def add_process(self, pid: int, *, start: int = 1) -> None:
        self.alive.add(pid)
        self.files[f"/proc/{pid}/stat"] = f"{pid} (python) S " + " ".join(["0"] * 18) + f" {start} 0\n"

    def add_cgroup(self, container: "FakeContainer", pids: Sequence[int], *, events: str | None = None) -> str:
        """Give a container a cgroup directory (the first layout docker uses) with these processes."""
        directory = f"/sys/fs/cgroup/system.slice/docker-{container.id}.scope"
        self.files[f"{directory}/cgroup.procs"] = "".join(f"{pid}\n" for pid in pids)
        if events is not None:
            self.files[f"{directory}/memory.events"] = events
        return directory

    def add_stat(self, pid: int, comm: str, ppid: int, *, state: str = "S", start: int = 1) -> None:
        """Put a process in /proc: its state, parent and start time, in the field positions the kernel uses."""
        self.alive.add(pid)
        self.files[f"/proc/{pid}/stat"] = f"{pid} ({comm}) {state} {ppid} " + " ".join(["0"] * 17) + f" {start}\n"

    def add_threads(self, container: "FakeContainer", pid: int, threads: dict[int, str]) -> None:
        """Give a process threads, `{tid: state}`: each has its own `task/<tid>/stat`, and the container's cgroup lists
        the thread ids (`cgroup.threads`, as cgroup v2 does) beside the processes."""
        directory = f"/sys/fs/cgroup/system.slice/docker-{container.id}.scope"
        listed = self.files.get(f"{directory}/cgroup.threads", "")
        for tid, state in threads.items():
            self.files[f"/proc/{pid}/task/{tid}/stat"] = f"{tid} (python) {state} 1 " + " ".join(["0"] * 17) + " 30\n"
            listed += f"{tid}\n"
        self.files[f"{directory}/cgroup.threads"] = listed

    def containers_with(self, label: str, value: str | None = None) -> list[FakeContainer]:
        return [c for c in self.containers.values() if label in c.labels and value in (None, c.labels[label])]

    def calls_of(self, kind: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == kind]

    # ---------------------------------------------------------------- HostOps: files and processes
    def mountinfo(self) -> str:
        lines = ["1 0 8:1 / / rw - ext4 /dev/root rw"]  # every real table has the root mount; the probe checks for it
        for path, kind in self.mounts.items():
            mount_id = self._mount_id(path)
            device = self._device(path, mount_id)
            lines.append(f"{mount_id} 25 {device} / {path} rw - {kind} source rw")  # device 100 for the first
            if self.fuse_peer_entry and kind.startswith("fuse"):  # the peer's copy: another id, the same device
                lines.append(f"{mount_id + 5000} 24 {device} / {path} rw - {kind} source rw")
        return "\n".join(lines) + "\n"

    def _device(self, path: str, mount_id: int) -> str:
        """The device a mount at `path` with `mount_id` shows: its own number, or the path's first when freed numbers
        are reused (`reuse_freed_devices`)."""
        first = self._device_of.setdefault(path, mount_id)
        return f"0:{60 + (first if self.reuse_freed_devices else mount_id)}"

    def _mount_id(self, path: str) -> int:
        if path not in self.mount_ids:
            self.mount_ids[path] = self._next_mount_id
            self._next_mount_id += 1
        return self.mount_ids[path]

    def agent_mountinfo(self, pid: int) -> str | None:
        """The mount table of the container whose init is `pid`, as `ns_mountinfo` makes it; None when it is off or
        the pid is no container's."""
        if pid in self.ns_override:
            return self.ns_override[pid]
        container = next((c for c in self.containers.values() if c.pid == pid), None)
        if not self.ns_mountinfo or container is None:
            return None
        lines = ["1 0 8:1 / / ro - overlay overlay ro"]
        for spec in container.mount_specs():
            fields = dict(part.split("=", 1) for part in spec.split(",") if "=" in part)
            src, dst = fields.get("src", ""), fields.get("dst", "")
            if "rslave" not in spec:
                continue
            stacked = container.options.get("_stacked", {}).get(dst)
            if stacked is not None:
                # bound while a FUSE mount was stacked on the share: the bind is a clone of that mount, which no later
                # mount or unmount of the share on the host reaches; only a detach inside the namespace removes it
                if dst not in container.options.get("_detached", set()):
                    lines.append(f"{stacked + 100} 25 {self._device(src, stacked)} / {dst} ro - fuse.oneclient s ro")
            elif self.mounts.get(src, "").startswith("fuse"):
                # bound from the bare shared share: the host's mounts on it propagate in, and its unmounts do too
                mount_id = self._mount_id(src)
                lines.append(f"{mount_id + 100} 25 {self._device(src, mount_id)} / {dst} ro - fuse.oneclient s ro")
        return "\n".join(lines) + "\n"

    def relocate_mounts(self, old: str, new: str) -> None:
        """What renaming a directory does to the mounts under it: they move with it, with the same id and device."""
        for path in [p for p in self.mounts if p == old or p.startswith(old + "/")]:
            moved = new + path[len(old) :]
            self.mount_ids[moved] = self.mount_ids.pop(path, None) or self._mount_id(moved)
            self.mounts[moved] = self.mounts.pop(path)

    def read_text(self, path: str | Path) -> str:
        path = str(path)
        if path in self.denied:
            raise PermissionError(13, "Permission denied", path)
        if path == "/proc/self/mountinfo":
            return self.mountinfo()
        if path in self.files:
            return self.files[path]
        named = re.fullmatch(r"/proc/(\d+)/cmdline", path)
        if named and int(named[1]) in self.alive:  # a process that was given none has the command line `proc-<pid>`
            return f"proc-{named[1]}\0"
        raise FileNotFoundError(path)

    def readlink(self, path: str | Path) -> str:
        if str(path) in self.links:
            return self.links[str(path)]
        raise FileNotFoundError(str(path))

    def exists(self, path: str | Path) -> bool:
        return str(path) in self.files or Path(path).exists()

    def process_alive(self, pid: int) -> bool | None:
        return pid in self.alive

    # ---------------------------------------------------------------- HostOps: host effects
    def make_quota_dir(self, image: Path, mountpoint: Path, size_bytes: int, uid: int) -> None:
        with self.lock:
            self.calls.append(("quota_dir", str(mountpoint), size_bytes, uid))
            mountpoint.mkdir(parents=True, exist_ok=True)
            self.mounts[str(mountpoint)] = "ext4"

    def make_ca(self, directory: Path, names: Sequence[str]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "ca.pem").write_text("RUN CA\n", encoding="utf-8")
        for name in names:
            (directory / f"{name}.pem").write_text("CERT\n", encoding="utf-8")
            (directory / f"{name}.key").write_text("KEY\n", encoding="utf-8")
        self.calls.append(("make_ca", tuple(names)))

    def unmount(self, path: Path) -> None:
        with self.lock:
            self.calls.append(("unmount", str(path)))
            if str(path) in self.stuck_mounts:
                raise SandboxError(
                    "a mount is still mounted after an unmount and a lazy unmount"
                )  # as DockerHost words it
            self.mounts.pop(str(path), None)

    def image_id(self, reference: str) -> str:
        if reference not in self.images:
            raise SandboxError("an image could not be resolved to an id")
        return self.images[reference]

    def host_facts(self) -> dict[str, Any]:
        return {"kernel": "test-kernel", "docker_server": "0.0"}

    def host_readings(self) -> dict[str, Any]:
        if self.readings_error is not None:
            raise self.readings_error
        return json.loads(json.dumps(self.readings))

    # ---------------------------------------------------------------- HostOps: run
    def run(self, argv: Sequence[str], timeout_s: float, *, sudo: bool = False, input: str | None = None) -> CmdResult:
        argv = list(argv)
        with self.lock:
            self.calls.append(("run", tuple(argv), sudo))
            if sudo:
                self.sudo_commands.append(argv)
            if is_snapshot_helper(argv):  # run for real, on a tree made of this fake's /proc
                return self._snapshot_helper(input)
            if is_signal_helper(argv):  # never run for real: its pids are this fake's, and the real /proc is the host's
                return self._signal_helper(argv, input)
            handler = self.run_handlers.get(argv[0])
            if handler is not None:
                return handler(argv)
            if argv[0] == sys.executable and "-c" in argv:  # the copy helper, run for real
                return self.real(argv, input)
            return self._run_default(argv)

    def _snapshot_helper(self, request: str | None) -> CmdResult:
        """The root half of the diagnosis (`procsnap.py`, O97) played against this fake's /proc: a tree is made of the
        files, links and memory the fake holds for its processes, and the helper's own code reads it, so what comes back
        is what the real one would say of such a process."""
        from expcore_onedata.sandbox import procsnap

        with tempfile.TemporaryDirectory() as made:
            root = Path(made)
            for path, text in self.files.items():
                if re.match(r"/proc/\d+/", path):
                    target = root / path[len("/proc/") :]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text, encoding="utf-8")
            for path, data in self.proc_bytes.items():
                target = root / path[len("/proc/") :]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            for path, destination in self.links.items():
                if re.match(r"/proc/\d+/", path):
                    target = root / path[len("/proc/") :]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(destination)
            old, procsnap.PROC_ROOT = procsnap.PROC_ROOT, str(root)
            try:
                out, err = io.StringIO(), io.StringIO()
                code = procsnap.main([], io.StringIO(request or ""), out, err)
            finally:
                procsnap.PROC_ROOT = old
        return CmdResult(code, out.getvalue(), err.getvalue())

    def _signal_helper(self, argv: list[str], request: str | None) -> CmdResult:
        """The root half of a signal (`killhelper.py`, O72) played against this fake's /proc: each target is signalled
        only while it is the process the listing saw (its start tick and the digest of its command line), and what
        was signalled is then run as the plain `kill -s SIGNAL PID...` the tests read, through `run_handlers["kill"]`
        when one is set (its failure is the whole command's, as a failed `sudo` is). The answer is the helper's own,
        an outcome for every target."""
        results, matched = [], []
        for target in json.loads(request or "{}").get("targets", []):
            outcome = self._identity_outcome(target["pid"], target["start"], target["cmd_sha256"])
            results.append({"pid": target["pid"], "outcome": outcome})
            if outcome == "signalled":
                matched.append(target["pid"])
        if matched:
            plain = ["kill", "-s", argv[-1], *map(str, matched)]
            self.calls.append(("run", tuple(plain), True))
            self.sudo_commands.append(plain)
            handler = self.run_handlers.get("kill")
            done = handler(plain) if handler is not None else self._run_default(plain)
            if done.returncode != 0:
                return CmdResult(done.returncode, "", done.stderr)
        return CmdResult(0, json.dumps({"results": results}))

    def _identity_outcome(self, pid: int, start: str, digest: str | None) -> str:
        from expcore_onedata.sandbox import procsignal  # not before it exists: the old code never calls the helper

        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            return "refused"
        if digest is None:
            return "unreadable"
        try:
            stat = self.read_text(f"/proc/{pid}/stat")
            command = self.read_text(f"/proc/{pid}/cmdline")
        except FileNotFoundError:
            return "gone"
        except OSError:
            return "unreadable"
        fields = stat[stat.rfind(")") + 2 :].split()
        if fields[19] != start:
            return "replaced"
        if fields[0] in ("Z", "X"):
            return "gone"
        return "signalled" if procsignal.digest_of(command) == digest else "changed"

    def _run_default(self, argv: list[str]) -> CmdResult:
        match argv[0]:
            case "ip":
                return CmdResult(0, self.routes)
            case "journalctl":
                return CmdResult(0, self.journal)
            case "sh" if "fusectl-state" in argv:  # the check of the connection's abort file (`dockerdiag`, O97)
                return CmdResult(0, self.fusectl_state + "\n")
            case "chmod":
                return self.real(argv)
            case "chown":  # this user cannot give a file to root: the command is recorded, and nothing changes
                return CmdResult(0)
            case "dmesg":
                return CmdResult(0, self.dmesg)
            case "cat":
                named = re.fullmatch(r"/proc/(\d+)/mountinfo", argv[1])
                text = self.agent_mountinfo(int(named[1])) if named else None
                text = text if text is not None else self.files.get(argv[1])
                return CmdResult(0, text) if text is not None else CmdResult(1, "", "No such file")
            case "kill":
                for pid in argv[argv.index("-s") + 2 :] if "-s" in argv else argv[1:]:
                    if int(pid) in self.unkillable:
                        continue
                    self.alive.discard(int(pid))
                    self.files.pop(f"/proc/{pid}/stat", None)
                return CmdResult(0)
            case "mount" if "--bind" in argv:
                self.mounts[argv[-1]] = "bind"
                return CmdResult(0)
            case "fusermount" | "umount":
                target = argv[-1]
                if target in self.stuck_mounts:
                    return CmdResult(1, "", "busy")
                self.mounts.pop(target, None)
                self.mount_ids.pop(target, None)  # mounted again, it is another mount
                return CmdResult(0)
            case "rm" if "-rf" in argv:
                writable(argv[-1])  # root removes what is read-only; this user must make it writable first
                shutil.rmtree(argv[-1], ignore_errors=True)
                return CmdResult(0)
            case "rm" if "-f" in argv:  # one file; a missing one is not an error, as with the real command
                try:
                    Path(argv[-1]).unlink(missing_ok=True)
                except OSError:  # a directory: `rm -f` without `-r` refuses it, and says so
                    return CmdResult(1, "", f"cannot remove '{argv[-1]}': Is a directory")
                return CmdResult(0)
            case "rmdir":  # what `rmdir` does: an empty directory only
                try:
                    Path(argv[-1]).rmdir()
                except OSError as e:
                    return CmdResult(1, "", f"failed to remove '{argv[-1]}': {e.strerror}")
                return CmdResult(0)
            case "mkdir":
                Path(argv[-1]).mkdir(parents=True, exist_ok=True)
                return CmdResult(0)
            case "mv":
                shutil.move(argv[-2], argv[-1])
                self.relocate_mounts(argv[-2], argv[-1])
                return CmdResult(0)
            case "nsenter" if "umount" in argv:  # `nsenter -t PID -m -p umount -l PATH`: a detach inside the namespace
                owner = next((c for c in self.containers.values() if c.pid == int(argv[argv.index("-t") + 1])), None)
                if owner is not None:
                    owner.options.setdefault("_detached", []).append(argv[-1])
                return CmdResult(0)
            case "mount" | "nsenter" | "sh" | "test" | "truncate":
                return CmdResult(0)
        raise AssertionError(f"the fake host does not know the command {argv[0]!r}")

    @staticmethod
    def real(argv: list[str], input: str | None = None) -> CmdResult:
        """Run a command for real, as this user and never under sudo, on the test's own directories."""
        done = subprocess.run(
            argv,
            input=input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        return CmdResult(done.returncode, done.stdout, done.stderr)

    # ---------------------------------------------------------------- HostOps: docker
    def docker(
        self,
        *args: str,
        check: bool = True,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
        output_cap: int | None = None,
        marker: bytes | None = None,
    ) -> CmdResult:
        with self.lock:
            self.calls.append(("docker", args[0], args[1:] if args[0] not in ("exec", "run") else ()))
            # a result is cut as the real host's runner cuts it, so a test sees what a cell's caller would
            result = apply_output_cap(self._docker(list(args), env, input), output_cap, marker)
        if check and result.returncode != 0:
            raise SandboxError(f"docker {args[0]} failed ({result.returncode}): {result.stderr.strip()[-300:]}")
        return result

    def _docker(self, args: list[str], env: Mapping[str, str] | None, stdin: str | None) -> CmdResult:
        command, rest = args[0], args[1:]
        if command == "image":
            ref = rest[-1]
            return CmdResult(0, self.images[ref] + "\n") if ref in self.images else CmdResult(1, "", "No such image")
        if command == "version":
            return CmdResult(0, "0.0\n")
        if command == "run":
            return self._run_container(rest, env)
        if command == "exec":
            return self._exec(rest, stdin)
        if command in ("rm", "stop", "inspect", "ps", "logs", "kill", "pause", "unpause"):
            return getattr(self, f"_{command}")(rest)
        if command == "network":
            return getattr(self, f"_net_{rest[0]}")(rest[1:])
        raise AssertionError(f"the fake host does not know 'docker {command}'")

    def _find(self, ref: str) -> FakeContainer | None:
        return self.containers.get(ref) or next((c for c in self.containers.values() if c.id.startswith(ref)), None)

    def _run_container(self, args: list[str], env: Mapping[str, str] | None) -> CmdResult:
        options, image, command = _parse_run(args)
        if options.get("--rm"):  # a one-shot: its output is what the test configured, nothing is kept
            one_shot = FakeContainer(
                name=options["--name"], id=f"{abs(hash(options['--name'])) % 10**12:012x}" + "1" * 52,
                image=image, options=options, command=command,
                labels=dict(item.split("=", 1) for item in options.get("--label", [])),
            )  # fmt: skip
            self.one_shots.append(one_shot)
            if self.stuck_one_shots:
                self.containers[one_shot.name] = one_shot
            if command[:1] == ["-c"] and "certifi" in command[1]:
                return self.certifi_probe
            return CmdResult(0, self.base_bundle if options.get("--entrypoint") == "cat" else "oneclient 1.0\n")
        name = options["--name"]
        if name in self.fail_run:
            return CmdResult(125, "", "cannot start the container")
        if name in self.containers:
            return CmdResult(125, "", f"Conflict. The container name {name} is already in use")
        for spec in options.get("--mount", []):  # unlike `-v`, which makes a directory of it, `--mount` refuses
            fields = dict(part.split("=", 1) for part in spec.split(",") if "=" in part)
            if fields.get("type") == "bind" and not os.path.lexists(fields.get("src", "")):
                # docker's own words name the path; the real `HostOps.docker` digests them, so the fake's carry none
                return CmdResult(125, "", 'invalid mount config for type "bind": bind source path does not exist')
        labels = dict(item.split("=", 1) for item in options.get("--label", []))
        self.next_pid += 1
        container = FakeContainer(
            name=name, id=f"{abs(hash(name)) % 10**12:012x}" + "0" * 52, image=self.images.get(image, image),
            options=options, command=command, labels=labels, pid=self.next_pid,
        )  # fmt: skip
        if container.image in self.oneclient_images:
            self._mount_oneclient(container)
        net = options.get("--network", "bridge")
        container.networks[net] = f"10.1.0.{len(self.containers) + 2}"
        if any("gate" in word for word in command):
            container.logs = "gate ready\n"
        self.alive.add(container.pid)
        self.containers[name] = container
        self.add_stat(container.pid, "tini", 1)
        if options.get("--init") and command == ["sleep", "infinity"] and self.init_starts_sleep:
            self.next_pid += 1
            container.init_child = self.next_pid
            self.add_stat(container.init_child, "sleep", container.pid, start=2)
        procs = [container.pid, *([container.init_child] if container.init_child else [])]
        self.add_cgroup(container, procs, events="low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
        container.options["_env"] = dict(env or {})
        container.options[
            "_stacked"
        ] = {  # destination: the mount id of the FUSE mount the bind cloned, if there was one
            fields["dst"]: self._mount_id(fields["src"])
            for spec in options.get("--mount", [])
            if "rslave" in spec
            for fields in [dict(part.split("=", 1) for part in spec.split(",") if "=" in part)]
            if self.mounts.get(fields.get("src", ""), "").startswith("fuse")
        }
        container.options["_detached"] = []  # destinations detached from inside the namespace (a list: dumped as JSON)
        container.options["_files"] = {  # what each file bound in held when the container started, by destination
            destination: Path(source).read_text(encoding="utf-8")
            for source, destination in bound_files(options)
            if Path(source).is_file()
        }
        if self.engine_resolv_wins:
            container.options["_files"].pop(RESOLV_CONF, None)
        return CmdResult(0, container.id + "\n")

    def _mount_oneclient(self, container: FakeContainer) -> None:
        container.logs = f"expcore-oneclient-config sha256={ONECLIENT_CONFIG_SHA256} path={ONECLIENT_CONFIG_PATH}\n"
        for spec in container.options.get("--mount", []):
            fields = dict(part.split("=", 1) for part in spec.split(",") if "=" in part)
            if fields.get("dst") == "/mnt/oneclient":
                self.mounts[fields["src"]] = "fuse.oneclient"

    def _exec(self, args: list[str], stdin: str | None) -> CmdResult:
        i = 0
        while args[i].startswith("-"):
            i += 2 if args[i] in ("-u", "-w", "-e") else 1
        container = self.containers.get(args[i])
        if container is None or not container.running:
            return CmdResult(126, "", f"Error response from daemon: container {args[i]} is not running")
        if container.paused:
            return CmdResult(126, "", f"Error response from daemon: container {args[i]} is paused")
        if container.image in self.oneclient_images and (read := self._oneclient_read(args[i + 1 :])) is not None:
            return read
        boot = bootstrap_of(args[i + 1 :])
        if boot is None:
            return self.exec_handler(container, args[i + 1 :], stdin)
        return self._run_cell(container, args[i + 1 :], stdin or "", *boot)

    def _oneclient_read(self, argv: list[str]) -> CmdResult | None:
        """The answer to one of the sandbox's reads of its sidecar's oneclient, as `od -An -v -tx1` of the window prints
        it (a missing file prints nothing); None for any other command."""
        if argv == [ONECLIENT_BIN, "--version"]:
            return self.oneclient_version
        if len(argv) != 7 or argv[:4] != ["sh", "-c", SLICE, SLICE_NAME] or argv[6] != ONECLIENT_BIN:
            return None
        if isinstance(self.oneclient_binary, CmdResult):
            return self.oneclient_binary
        offset, size = int(argv[4]), int(argv[5])
        window = self.oneclient_binary[offset : offset + size]
        return CmdResult(
            0, "".join(" " + " ".join(f"{b:02x}" for b in window[n : n + 16]) + "\n" for n in range(0, len(window), 16))
        )

    def _run_cell(self, container: FakeContainer, argv: list[str], stdin: str, nonce: str, size: int, digest: str,
                  python: str) -> CmdResult:  # fmt: skip
        """A cell under the completion bootstrap (O55): the program checked whole, run by the handler, and recorded."""
        sent = encode_input(stdin)
        got = self.exec_delivers(sent) if self.exec_delivers is not None else sent
        whole = len(got) == size and hashlib.sha256(got).hexdigest() == digest
        if not whole:  # the bootstrap runs nothing and says what arrived
            record = {"source_bytes": len(got), "source_whole": False, "exit_status": None}
            result = recorded(CmdResult(completion.NOT_RUN_EXIT), nonce, record)
        else:
            ran = self.exec_handler(container, argv, got.decode("utf-8", "replace"))
            if isinstance(ran, Unrecorded):
                result = CmdResult(ran.returncode, ran.stdout, ran.stderr, input_error=ran.input_error)
            else:
                status = 128 - ran.returncode if ran.returncode < 0 else ran.returncode
                result = recorded(ran, nonce, {"source_bytes": size, "source_whole": True, "exit_status": status})
        if self.exec_input_error is not None:
            result = CmdResult(result.returncode, result.stdout, result.stderr, input_error=self.exec_input_error)
        return result

    def _cgroup_events(self, container: FakeContainer, frozen: bool) -> None:
        self.files[f"/sys/fs/cgroup/system.slice/docker-{container.id}.scope/cgroup.events"] = (
            f"populated 1\nfrozen {int(frozen)}\n"
        )

    def _pause(self, args: list[str]) -> CmdResult:
        container = self._find(args[-1])
        if container is None or not container.running:
            return CmdResult(1, "", "No such container, or it is not running")
        container.paused = True
        if not self.pause_without_freezing:
            self._cgroup_events(container, True)
        self.calls.append(("paused", container.name))
        return CmdResult(0, container.name + "\n")

    def _unpause(self, args: list[str]) -> CmdResult:
        container = self._find(args[-1])
        if container is None:
            return CmdResult(1, "", "No such container")
        container.paused = False
        self._cgroup_events(container, False)
        self.calls.append(("unpaused", container.name))
        return CmdResult(0, container.name + "\n")

    def _rm(self, args: list[str]) -> CmdResult:
        target = args[-1]
        container = self._find(target)
        if container is None:
            return CmdResult(1, "", f"No such container: {target}")
        if container.name in self.fail_remove:
            return CmdResult(1, "", "removal failed")
        self.calls.append(("removed", container.name))
        self.alive.discard(container.pid)
        for path, kind in list(self.mounts.items()):
            if kind == "fuse.oneclient" and any(f"src={path}" in m for m in container.mount_specs()):
                pass  # a removed sidecar leaves its mount behind: the teardown must have unmounted it first
        del self.containers[container.name]
        return CmdResult(0, container.name + "\n")

    def _stop(self, args: list[str]) -> CmdResult:
        container = self._find(args[-1])
        if container is None:
            return CmdResult(1, "", "No such container")
        container.running = False
        self.alive.discard(container.pid)
        self.calls.append(("stopped", container.name))
        return CmdResult(0, container.name + "\n")

    def _kill(self, args: list[str]) -> CmdResult:
        container = self._find(args[-1])
        if container is None:
            return CmdResult(1, "", "No such container")
        sig = args[args.index("-s") + 1] if "-s" in args else "KILL"
        self.calls.append(("signalled", container.name, sig))
        handled = self.signal_handlers.get(sig)
        if handled is not None:  # the process handles this signal: it acts on it and goes on running
            handled(container)
            return CmdResult(0, container.name + "\n")
        container.running, container.exit_code = False, 0
        self.alive.discard(container.pid)
        return CmdResult(0, container.name + "\n")

    def _inspect(self, args: list[str]) -> CmdResult:
        found = [self._find(a) for a in args]
        if any(c is None for c in found):
            return CmdResult(1, "[]", "No such object")
        return CmdResult(0, json.dumps([_inspect_json(c) for c in found if c]))

    def _logs(self, args: list[str]) -> CmdResult:
        container = self._find(args[-1])
        return CmdResult(0, container.logs) if container else CmdResult(1, "", "No such container")

    def _ps(self, args: list[str]) -> CmdResult:
        wanted = [args[i + 1] for i, a in enumerate(args) if a == "--filter"]
        out = [c.id[:12] for c in self.containers.values() if all(self._matches(c.name, c.labels, f) for f in wanted)]
        return CmdResult(0, "\n".join(out) + ("\n" if out else ""))

    @staticmethod
    def _matches(name: str, labels: Mapping[str, str], flt: str) -> bool:
        kind, _, value = flt.partition("=")
        if kind == "name":
            return name == value.strip("^$")
        if kind == "label":
            key, _, want = value.partition("=")
            return key in labels and (not want or labels[key] == want)
        return True

    def _net_create(self, args: list[str]) -> CmdResult:
        name, subnet = args[-1], args[args.index("--subnet") + 1]
        import ipaddress

        if self.fail_network_create:
            return CmdResult(1, "", self.fail_network_create.pop(0))
        wanted = ipaddress.ip_network(subnet)
        if any(ipaddress.ip_network(n["subnet"]).overlaps(wanted) for n in self.networks.values()):
            return CmdResult(1, "", "Error response from daemon: Pool overlaps with other one on this address space")
        labels = dict(a.split("=", 1) for i, a in enumerate(args) if i and args[i - 1] == "--label")
        self.networks[name] = {
            "id": f"{abs(hash(name)) % 10**12:012x}",
            "subnet": subnet,
            "labels": labels,
            "args": args,
        }
        self.calls.append(("network_created", name))
        return CmdResult(0, name + "\n")

    def _net_ls(self, args: list[str]) -> CmdResult:
        wanted = [args[i + 1] for i, a in enumerate(args) if a == "--filter"]
        out = [n["id"] for name, n in self.networks.items() if all(self._matches(name, n["labels"], f) for f in wanted)]
        return CmdResult(0, "\n".join(out) + ("\n" if out else ""))

    def _net_inspect(self, args: list[str]) -> CmdResult:
        found = [(name, n) for name, n in self.networks.items() if name in args or n["id"] in args]
        data = [
            {"Id": n["id"], "Name": name, "Labels": n["labels"], "IPAM": {"Config": [{"Subnet": n["subnet"]}]}}
            for name, n in found
        ]
        return CmdResult(0, json.dumps(data))

    def _net_rm(self, args: list[str]) -> CmdResult:
        if args[-1] not in self.networks:
            return CmdResult(1, "", "No such network")
        del self.networks[args[-1]]
        self.calls.append(("network_removed", args[-1]))
        return CmdResult(0)

    def _net_connect(self, args: list[str]) -> CmdResult:
        container = self._find(args[-1])
        if container is None or args[-2] not in self.networks:
            return CmdResult(1, "", "No such network or container")
        container.networks[args[-2]] = f"10.2.0.{len(container.networks) + 1}"
        return CmdResult(0)
