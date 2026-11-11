"""What the live test of the mount path (tests/live/test_mount_path.py) needs, apart, so the offline tests check it.

- `derive_platform`: the federation's own platform.toml has no `[sandbox]` table (its images are pinned on each host,
  and its address pool is the host's own), so the test writes a copy with one into a directory of its own: every path
  of the original made absolute, and a `[sandbox]` table whose `[sandbox.federation]` puts the oneclient sidecar on the
  federation's network. Nothing is written beside the original.
- `SpaceFiles`: the files the test reads, written into the study space through a chosen provider's REST API as the
  admin, and deleted again. The platform layer's provisioner cannot write on Onedata 25.0 (its mutation gate reads a
  file attribute that release does not have), so the test writes them itself.
- The code its cells run inside the agent: each prints one line of JSON with outcomes and numbers, never the bytes it
  read, an address or a name.

Every name here is invented or public: the image names are the published ones.
"""

import hashlib
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
from platform_support import render

from expcore_onedata.client import API_PROVIDER, AUTH_HEADER
from expcore_onedata.config import PlatformConfig

AGENT_IMAGE = "python:3.12-slim"  # has python, `timeout` and `sleep`, which the agent's container needs
ONECLIENT_IMAGE = "onedata/oneclient:25.0"  # the client of the federation's release
FED_NETWORK = "expcore-fed_fed"  # the default layout's network: its project name and the project's one network, `fed`
LEVEL = "mounted"  # the invented level of the data_path factor that maps to the mount
LEASE_KEY = "live-mount"  # labels the sandbox's containers, network and mounts, so the probe can show them gone
LABEL_NAMESPACE = "org.expcore.live"
MOUNT_PATH = "/data"
PATH_KEYS = (("credentials", "admin", "file"), ("credentials", "users", "directory"), ("tls", "ca_file"))


def pattern(label: str, size: int) -> bytes:
    """Deterministic bytes with no zero byte, so a zero read back is never data."""
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += bytes(b or 1 for b in hashlib.sha256(f"{label}:{counter}".encode()).digest())
        counter += 1
    return bytes(out[:size])


def federation_network(facts: Mapping[str, Any]) -> str:
    """The docker network of the federation `facts` (its facts.json) describes: its Compose project's one network."""
    project = (facts.get("layout") or {}).get("project")
    return f"{project}_fed" if project else FED_NETWORK


def sandbox_table(
    *,
    work_root: Path,
    images: Mapping[str, tuple[str, str]],
    pool: str,
    network: str,
    local: str,
    local_address: str,
) -> dict[str, Any]:
    """The `[sandbox]` table of one mount level on the federation: `images` maps agent and oneclient to (name, id)."""
    return {
        "work_root": str(work_root),
        "name_prefix": "fedsbx",
        "label_namespace": LABEL_NAMESPACE,
        "agent_uid": 10001,
        "python": "python",
        "sudo": ["sudo", "-n"],
        "images": {role: {"name": name, "id": image_id} for role, (name, image_id) in images.items()},
        "network": {"pools": [pool], "prefix": 28, "max_attempts": 64},
        "limits": {
            "cpus": 1.0,
            "memory_bytes": 2 << 30,
            "pids": 256,
            "nofile": 1024,
            "tmp_bytes": 256 << 20,
            "shm_bytes": 64 << 20,
            "work_disk_bytes": 256 << 20,
        },
        "mount": {"path": MOUNT_PATH, "io_mode": "proxy"},  # the providers' storage is not shared with the sidecar
        "data_paths": {LEVEL: "mount"},
        "federation": {"network": network, "providers": {local: local_address}},
    }


def derive_platform(source: Path, out_dir: Path, sandbox: Mapping[str, Any]) -> Path:
    """Write `source` with `sandbox` as its `[sandbox]` table into `out_dir/platform.toml`, its paths made absolute."""
    data = tomllib.loads(source.read_text(encoding="utf-8"))
    if "sandbox" in data:
        raise ValueError("the platform file already has a [sandbox] table: give the federation's own platform.toml")
    base = source.resolve().parent
    for keys in PATH_KEYS:
        table = data
        for key in keys[:-1]:
            table = table.get(key, {})
        if keys[-1] in table:
            table[keys[-1]] = str((base / table[keys[-1]]).resolve())
    data["sandbox"] = dict(sandbox)
    out = out_dir / "platform.toml"
    out.write_text(render(data), encoding="utf-8")
    out.chmod(0o600)
    return out


class SpaceFiles:
    """Files in the study space, written through one provider's REST API as the admin; every one is deleted again."""

    def __init__(self, config: PlatformConfig) -> None:
        self._config = config
        self._http = httpx.Client(timeout=300, verify=config.tls.client_verify())
        self._headers = {AUTH_HEADER: config.admin.read()}
        self.created: list[tuple[str, str]] = []  # (provider, file id), oldest first

    def _call(self, method: str, provider: str, path: str, **options: Any) -> httpx.Response:
        url = self._config.providers[provider].url + API_PROVIDER + path
        return self._http.request(method, url, headers=self._headers, **options)

    def _ok(self, answer: httpx.Response, what: str) -> httpx.Response:
        if answer.status_code >= 300:
            raise RuntimeError(f"{what} answered {answer.status_code}")  # never the body: it may name the space
        return answer

    def root(self, provider: str) -> str:
        """The space's root directory id, as `provider` answers."""
        answer = self._call("GET", provider, f"/spaces/{self._config.space.id}")
        return self._ok(answer, "the space").json()["fileId"]

    def make(self, provider: str, parent: str, name: str, kind: str) -> str:
        """Create a directory or an empty regular file under `parent` through `provider`; return its id."""
        answer = self._call("POST", provider, f"/data/{parent}/children", params={"name": name, "type": kind})
        file_id = self._ok(answer, "a creation").json()["fileId"]
        self.created.append((provider, file_id))
        return file_id

    def seen(self, provider: str, file_id: str) -> bool:
        """Whether `provider` knows the entry yet (a new entry reaches the other providers after a while)."""
        return self._call("GET", provider, f"/data/{file_id}").status_code == 200

    def write(self, provider: str, file_id: str, offset: int, data: bytes) -> None:
        """Write `data` at `offset` through `provider`; an empty write past the end sets the file's size."""
        headers = {**self._headers, "Content-Type": "application/octet-stream"}
        url = self._config.providers[provider].url + API_PROVIDER + f"/data/{file_id}/content"
        self._ok(self._http.put(url, headers=headers, params={"offset": offset}, content=data), "a write")

    def remove_all(self) -> list[str]:
        """Delete every created entry, newest first, through the provider that made it; what failed, by position."""
        failed = []
        for position, (provider, file_id) in reversed(list(enumerate(self.created))):
            answer = self._call("DELETE", provider, f"/data/{file_id}")
            if answer.status_code not in (204, 404):
                failed.append(f"entry {position}: the delete answered {answer.status_code}")
        self.created.clear()
        return failed

    def close(self) -> None:
        self._http.close()


# -- the cells: code run inside the agent, each printing one line of JSON ----------------------------------------------

CONFINEMENT = """
import errno, json, os, socket
mount, space = {mount!r}, {space!r}
out = {{}}
entries = sorted(os.listdir(mount))
out["entries"] = len(entries)
out["only_the_space"] = entries == [space]
lines = [line.split() for line in open("/proc/self/mountinfo") if line.split()[4] == mount]
out["mount_lines"] = len(lines)
out["mount_read_only"] = bool(lines) and all("ro" in line[5].split(",") for line in lines)
out["fstypes"] = sorted({{line[line.index("-") + 1] for line in lines}})
try:
    open(os.path.join(mount, space, "ro-probe"), "w").close()
    out["write_in_the_space"] = "WRITABLE"
except OSError as e:
    out["write_in_the_space"] = errno.errorcode.get(e.errno, str(e.errno))


def reach(host, port):
    try:
        socket.create_connection((host, port), timeout=3).close()
        return "connected"
    except socket.gaierror:
        return "unresolved"
    except TimeoutError:
        return "timeout"
    except OSError as e:
        return errno.errorcode.get(e.errno, "error")


out["by_name"] = [reach(name, 443) for name in {names!r}]
out["by_address"] = [reach(address, 443) for address in {addresses!r}]
out["loopback_ports"] = [reach("127.0.0.1", port) for port in {ports!r}]
out["dev_fuse"] = os.path.exists("/dev/fuse")
out["cap_eff_zero"] = int(next(l.split()[1] for l in open("/proc/self/status") if l.startswith("CapEff")), 16) == 0
print(json.dumps(out))
"""


def confinement_cell(
    mount: str, space: str, names: Sequence[str], addresses: Sequence[str], ports: Sequence[int]
) -> str:
    """What the agent sees of the mount, and whether it reaches the federation by any other way: by the providers'
    names on the federation's network, by the addresses of the network's containers, or at the host's published ports
    through its own loopback. Every outcome is a word (`connected`, `unresolved`, an errno's name), never an address."""
    return CONFINEMENT.format(mount=mount, space=space, names=list(names), addresses=list(addresses), ports=list(ports))


READ = """
import errno, hashlib, json, os, time
path, wait = {path!r}, {wait!r}
out = {{}}
began = time.perf_counter()
while True:
    try:
        out["size"] = os.stat(path).st_size
        break
    except FileNotFoundError:
        if time.perf_counter() - began > wait:
            out["error"] = "ENOENT"
            break
        time.sleep(1)
out["appeared_after_s"] = round(time.perf_counter() - began, 3)
if "error" not in out:
    digest, count, t0 = hashlib.sha256(), 0, time.perf_counter()
    try:
        with open(path, "rb", buffering=0) as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
                count += len(chunk)
        out.update(read=count, sha256=digest.hexdigest(), seconds=round(time.perf_counter() - t0, 3))
    except OSError as e:
        out["error"] = errno.errorcode.get(e.errno, str(e.errno))
print(json.dumps(out))
"""


def read_cell(path: str, wait_s: float = 60.0) -> str:
    """Read a whole file through the mount, once it appears there: its size, the bytes read, their SHA-256 and the
    seconds, or the errno's name."""
    return READ.format(path=path, wait=wait_s)


UNWRITTEN = """
import errno, json, os, subprocess, sys, time
files, prefix, size, deadline = {files!r}, {prefix!r}, {size!r}, {deadline!r}
reads = [("unwritten", prefix, 4096), ("across_prefix_end", prefix - 16, 32), ("whole", 0, size)]
child = (
    "import errno, os, sys\\n"
    "try:\\n"
    "    fd = os.open(sys.argv[1], os.O_RDONLY)\\n"
    "    sys.stdout.buffer.write(os.pread(fd, int(sys.argv[3]), int(sys.argv[2])))\\n"
    "except OSError as e:\\n"
    "    sys.stderr.write(errno.errorcode.get(e.errno, str(e.errno)))\\n"
    "    sys.exit(3)\\n"
)


def written(label, offset):
    import hashlib
    out, counter = bytearray(), 0
    while len(out) < prefix:
        out += bytes(b or 1 for b in hashlib.sha256((label + ":" + str(counter)).encode()).digest())
        counter += 1
    return bytes(out[offset:prefix]) if offset < prefix else b""


def classify(label, data, offset, length):
    expected = written(label, offset)
    head, tail = data[: len(expected)], data[len(expected) :]
    outcome = "short" if len(data) < length else ("zeros" if not tail.strip(b"\\0") else "nonzero")
    return {{
        "outcome": outcome,
        "bytes": len(data),
        "prefix_matches": head == expected[: len(head)],
        "unwritten_all_zero": (not tail.strip(b"\\0")) if tail else None,
    }}


results = {{}}
for label, (path, pattern_label) in files.items():
    entry = {{}}
    began = time.perf_counter()
    try:
        entry["size"] = os.stat(path).st_size
    except OSError as e:
        entry["size"], entry["stat_error"] = None, errno.errorcode.get(e.errno, str(e.errno))
    entry["stat_seconds"] = round(time.perf_counter() - began, 3)
    blocked = False
    for name, offset, length in reads:
        if blocked:
            entry[name] = {{"outcome": "not_tried", "why": "an earlier read of this file blocked"}}
            continue
        began = time.perf_counter()
        proc = subprocess.Popen([sys.executable, "-c", child, path, str(offset), str(length)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            data, err = proc.communicate(timeout=deadline)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=10)
                killed = True
            except subprocess.TimeoutExpired:
                killed = False
            entry[name] = {{"outcome": "blocked", "seconds": round(time.perf_counter() - began, 3), "killed": killed}}
            blocked = True
            continue
        seconds = round(time.perf_counter() - began, 3)
        if proc.returncode != 0:
            entry[name] = {{"outcome": "error", "error": err.decode(errors="replace")[-40:], "seconds": seconds}}
        else:
            entry[name] = {{**classify(pattern_label, data, offset, length), "seconds": seconds}}
    results[label] = entry
print(json.dumps(results))
"""
UNWRITTEN_READS = 3  # the reads of each file in `UNWRITTEN`


def unwritten_cell(files: Mapping[str, tuple[str, str]], prefix: int, size: int, deadline_s: float) -> str:
    """For each file (label -> (path, the label its prefix's bytes were made from by `pattern`)) at its final size
    `size` with only its first `prefix` bytes written: its size, then a read of 4096 unwritten bytes, one of 32
    across the prefix's end and one of the whole file, each in a child process given `deadline_s` seconds. Each read
    is classified (`zeros`, `nonzero`, `short`, `error` with the errno's name, `blocked`), with its seconds and whether
    the prefix's bytes it returned are the ones written; after a read that blocked, the file's later reads are
    `not_tried`."""
    return UNWRITTEN.format(files=dict(files), prefix=prefix, size=size, deadline=deadline_s)


def unwritten_cell_timeout(files: int, deadline_s: float) -> float:
    """The cell's own limit: every read blocking to its deadline and its kill's wait, with a minute to spare."""
    return files * UNWRITTEN_READS * (deadline_s + 12) + 60
