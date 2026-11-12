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
"""

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.inputs import PROBE_MARK

VALUED = {
    "--name", "--network", "--label", "-l", "--user", "-u", "--cpus", "--memory", "--memory-swap", "--pids-limit",
    "--ulimit", "--tmpfs", "--shm-size", "-v", "--volume", "--mount", "-e", "--env", "--add-host", "--cap-add",
    "--cap-drop", "--security-opt", "--entrypoint", "--oom-score-adj", "--device", "-w", "--workdir",
}  # fmt: skip
REPEATED = {"--label", "-l", "-v", "--volume", "--mount", "-e", "--env", "--add-host", "--cap-add", "--cap-drop"}
BOOT_ID = "0f0f0f0f-1111-2222-3333-444455556666"
CERTIFI_BUNDLE = "/usr/lib/python3/site-packages/certifi/cacert.pem"  # where the fake agent image keeps certifi's
PID_NS = "pid:[4026531836]"


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

    def mount_specs(self) -> list[str]:
        return [*self.options.get("-v", []), *self.options.get("--mount", [])]


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


class FakeHost:
    """An in-memory host. Set `images`, `files`, `mounts` and the handlers before the code under test runs."""

    def __init__(self, work_root: Path | None = None) -> None:
        self.lock = threading.RLock()
        self.work_root = work_root
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
        self.calls: list[tuple] = []
        self.routes = "default via 192.0.2.1 dev eth0\n"
        self.dmesg = ""
        self.next_pid = 5000
        self.sudo_commands: list[list[str]] = []
        self.exec_handler: Callable[[FakeContainer, list[str], str | None], CmdResult] = self._default_exec
        self.journal = ""
        self.run_handlers: dict[str, Callable[[list[str]], CmdResult]] = {}
        self.fail_remove: set[str] = set()
        self.fail_run: set[str] = set()
        self.fail_network_create: list[str] = []
        self.stuck_mounts: set[str] = set()
        self.denied: set[str] = set()  # files whose read raises PermissionError, as /proc does with hidepid
        self.mount_ids: dict[str, int] = {}  # a mount keeps its id when the directory above it is renamed
        self.one_shots: list[FakeContainer] = []  # the `--rm` containers that were run, as they were labelled
        self.stuck_one_shots = False  # a `--rm` container that docker does not remove: it stays, running
        self.oneclient_images: set[str] = set()
        self.pause_without_freezing = False  # a `docker pause` that answers 0 but leaves the cgroup unfrozen
        self.signal_handlers: dict[str, Callable[[FakeContainer], None]] = {}  # a signal the process handles and lives
        self.base_bundle = "BASE BUNDLE\n"
        # what the agent image's interpreter answers the sandbox's question of where its certifi bundle is
        self.certifi_probe = CmdResult(0, f"certifi:{CERTIFI_BUNDLE}\n")
        self.add_process(__import__("os").getpid(), start=777)

    @staticmethod
    def _default_exec(container: FakeContainer, argv: list[str], stdin: str | None) -> CmdResult:
        """The agent's code does nothing, and the read-only probes are refused as a read-only filesystem refuses them:
        the root's and the mount's write, and every change to a read-only input, which the agent can still read and
        run."""
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

    def containers_with(self, label: str, value: str | None = None) -> list[FakeContainer]:
        return [c for c in self.containers.values() if label in c.labels and value in (None, c.labels[label])]

    def calls_of(self, kind: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == kind]

    # ---------------------------------------------------------------- HostOps: files and processes
    def mountinfo(self) -> str:
        lines = ["1 0 8:1 / / rw - ext4 /dev/root rw"]  # every real table has the root mount; the probe checks for it
        for path, kind in self.mounts.items():
            mount_id = self.mount_ids.setdefault(path, 40 + len(self.mount_ids))
            lines.append(f"{mount_id} 25 0:{60 + mount_id} / {path} rw - {kind} source rw")  # device 100 for the first
        return "\n".join(lines) + "\n"

    def relocate_mounts(self, old: str, new: str) -> None:
        """What renaming a directory does to the mounts under it: they move with it, with the same id and device."""
        for path in [p for p in self.mounts if p == old or p.startswith(old + "/")]:
            moved = new + path[len(old) :]
            self.mount_ids[moved] = self.mount_ids.pop(path, 40 + len(self.mount_ids))
            self.mounts[moved] = self.mounts.pop(path)

    def read_text(self, path: str | Path) -> str:
        path = str(path)
        if path in self.denied:
            raise PermissionError(13, "Permission denied", path)
        if path == "/proc/self/mountinfo":
            return self.mountinfo()
        if path in self.files:
            return self.files[path]
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

    # ---------------------------------------------------------------- HostOps: run
    def run(self, argv: Sequence[str], timeout_s: float, *, sudo: bool = False, input: str | None = None) -> CmdResult:
        argv = list(argv)
        with self.lock:
            self.calls.append(("run", tuple(argv), sudo))
            if sudo:
                self.sudo_commands.append(argv)
            handler = self.run_handlers.get(argv[0])
            if handler is not None:
                return handler(argv)
            if argv[0] == sys.executable and "-c" in argv:  # the copy helper, run for real
                return self.real(argv, input)
            return self._run_default(argv)

    def _run_default(self, argv: list[str]) -> CmdResult:
        match argv[0]:
            case "ip":
                return CmdResult(0, self.routes)
            case "journalctl":
                return CmdResult(0, self.journal)
            case "chmod":
                return self.real(argv)
            case "chown":  # this user cannot give a file to root: the command is recorded, and nothing changes
                return CmdResult(0)
            case "dmesg":
                return CmdResult(0, self.dmesg)
            case "cat":
                text = self.files.get(argv[1])
                return CmdResult(0, text) if text is not None else CmdResult(1, "", "No such file")
            case "kill":
                for pid in argv[argv.index("-s") + 2 :] if "-s" in argv else argv[1:]:
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
                return CmdResult(0)
            case "rm" if "-rf" in argv:
                writable(argv[-1])  # root removes what is read-only; this user must make it writable first
                shutil.rmtree(argv[-1], ignore_errors=True)
                return CmdResult(0)
            case "rm" if "-f" in argv:  # one file; a missing one is not an error, as with the real command
                Path(argv[-1]).unlink(missing_ok=True)
                return CmdResult(0)
            case "mkdir":
                Path(argv[-1]).mkdir(parents=True, exist_ok=True)
                return CmdResult(0)
            case "mv":
                shutil.move(argv[-2], argv[-1])
                self.relocate_mounts(argv[-2], argv[-1])
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
    ) -> CmdResult:
        with self.lock:
            self.calls.append(("docker", args[0], args[1:] if args[0] not in ("exec", "run") else ()))
            result = self._docker(list(args), env, input)
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
        self.add_cgroup(container, [container.pid], events="low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
        container.options["_env"] = dict(env or {})
        container.options["_files"] = {  # what each file bound in held when the container started, by destination
            spec.split(":")[1]: Path(spec.split(":")[0]).read_text(encoding="utf-8")
            for spec in options.get("-v", [])
            if Path(spec.split(":")[0]).is_file()
        }
        return CmdResult(0, container.id + "\n")

    def _mount_oneclient(self, container: FakeContainer) -> None:
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
        return self.exec_handler(container, args[i + 1 :], stdin)

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
