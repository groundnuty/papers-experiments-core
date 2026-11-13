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
import re
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from expcore_onedata.client import API_PROVIDER, AUTH_HEADER, POSIX_ABSENT
from expcore_onedata.config import PlatformConfig
from expcore_onedata.testing.world import render

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
    extra_args: Sequence[str] = (),
) -> dict[str, Any]:
    """The `[sandbox]` table of one mount level on the federation: `images` maps agent and oneclient to (name, id), and
    `extra_args` are further oneclient arguments (`[sandbox.mount] extra_args`, which refuses a TLS flag)."""
    mount: dict[str, Any] = {"path": MOUNT_PATH, "io_mode": "proxy"}  # the providers' storage is not the sidecar's
    if extra_args:
        mount["extra_args"] = list(extra_args)
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
        "mount": mount,
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


def absent(answer: httpx.Response) -> bool:
    """Whether a provider's answer says the entry is not there: a 404, or the 400 `posix` error with errno `enoent`
    that Onedata 25.0 gives for a file it does not know (the platform layer's `POSIX_ABSENT`). Any other 400, or a body
    of another shape, is not absence."""
    if answer.status_code == 404:
        return True
    status, code, errno = POSIX_ABSENT
    if answer.status_code != status:
        return False
    try:
        body = answer.json()
    except ValueError:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    return isinstance(details, dict) and error.get("id") == code and details.get("errno") == errno


class SpaceFiles:
    """Files in the study space, written through one provider's REST API as the admin; every one is deleted again."""

    def __init__(self, config: PlatformConfig, *, transport: httpx.BaseTransport | None = None) -> None:
        self._config = config
        self._http = httpx.Client(timeout=300, verify=config.tls.client_verify(), transport=transport)
        self._headers = {AUTH_HEADER: config.admin.read()}
        self.created: list[tuple[str, str]] = []  # (provider, file id), oldest first

    def _call(self, method: str, provider: str, path: str, **options: Any) -> httpx.Response:
        url = self._config.providers[provider].url + API_PROVIDER + path
        return self._http.request(method, url, headers=self._headers, **options)

    def _ok(self, answer: httpx.Response, what: str) -> httpx.Response:
        if answer.status_code >= 300:
            raise RuntimeError(f"{what} answered {answer.status_code}")  # never the body: it may name the space
        return answer

    def root(self, provider: str, space_id: str | None = None) -> str:
        """The root directory id of a space, as `provider` answers: `space_id`'s, else the only configured space's."""
        answer = self._call("GET", provider, f"/spaces/{space_id or self._config.space.id}")
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

    def remove_all(
        self,
        *,
        window_s: float = 60.0,
        poll_s: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> list[str]:
        """Delete every created entry, newest first, through the provider that made it, and show each gone (O11).

        A delete is sent again until the provider answers that the entry is gone (204, or `absent`) and a read of it
        answers that it is absent, for at most `window_s`: a directory whose files were made through another provider
        may be refused until their deletion reaches it. What is never shown gone is returned, by position."""
        failed = []
        for position, (provider, file_id) in reversed(list(enumerate(self.created))):
            deadline = clock() + window_s
            while True:
                deleted = self._call("DELETE", provider, f"/data/{file_id}")
                status = deleted.status_code
                gone = (status == 204 or absent(deleted)) and absent(self._call("GET", provider, f"/data/{file_id}"))
                if gone or clock() >= deadline:
                    break
                sleep(poll_s)
            if not gone:
                failed.append(f"entry {position}: not shown gone (its last delete answered {status})")
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


# -- one file of each of several spaces, read through one oneclient process (O36) -------------------------------------

SPACES = """
import errno, hashlib, json, os, subprocess, sys, time
files, deadline, wait = {files!r}, {deadline!r}, {wait!r}
child = (
    "import errno, hashlib, sys\\n"
    "digest, count = hashlib.sha256(), 0\\n"
    "try:\\n"
    "    with open(sys.argv[1], 'rb', buffering=0) as handle:\\n"
    "        while chunk := handle.read(1 << 20):\\n"
    "            digest.update(chunk)\\n"
    "            count += len(chunk)\\n"
    "except OSError as e:\\n"
    "    sys.stderr.write(errno.errorcode.get(e.errno, str(e.errno)))\\n"
    "    sys.exit(3)\\n"
    "print(count, digest.hexdigest())\\n"
)
results = {{}}
for label, (path, size, sha256) in files.items():
    entry = {{}}
    began = time.perf_counter()
    while True:  # the entry may reach the mount a little after it was written: wait for it, outside the read's deadline
        try:
            entry["size"] = os.stat(path).st_size
            break
        except FileNotFoundError:
            if time.perf_counter() - began > wait:
                entry["size"], entry["stat_error"] = None, "ENOENT"
                break
            time.sleep(1)
    entry["stat_seconds"] = round(time.perf_counter() - began, 3)
    if entry["size"] is None:
        entry["outcome"] = "error"
        results[label] = entry
        continue
    began = time.perf_counter()
    proc = subprocess.Popen([sys.executable, "-c", child, path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(timeout=deadline)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=10)
            killed = True
        except subprocess.TimeoutExpired:
            killed = False
        entry.update(outcome="blocked", seconds=round(time.perf_counter() - began, 3), killed=killed)
        results[label] = entry
        continue
    entry["seconds"] = round(time.perf_counter() - began, 3)
    if proc.returncode != 0:
        entry.update(outcome="error", error=err.decode(errors="replace")[-40:])
    else:
        count, digest = out.decode().split()
        entry["bytes"] = int(count)
        entry["matches"] = int(count) == size and digest == sha256
        entry["outcome"] = "read" if entry["matches"] else "mismatch"
    results[label] = entry
print(json.dumps(results))
"""


def spaces_cell(files: Mapping[str, tuple[str, int, str]], deadline_s: float, wait_s: float = 120.0) -> str:
    """For each file (label -> (path, size, the SHA-256 of its bytes)), once its entry has reached the mount (up to
    `wait_s`, outside the deadline), read it whole in a child process given `deadline_s` seconds. Each is `read`
    (its size and digest are the file's), `mismatch`, `error` with the errno's name, or `blocked` (the child was killed
    at the deadline), with its seconds. No byte of a file is printed. The files are read one after another by one
    agent, so one oneclient process serves every space (O36)."""
    return SPACES.format(files=dict(files), deadline=deadline_s, wait=wait_s)


def spaces_cell_timeout(files: int, deadline_s: float, wait_s: float = 120.0) -> float:
    """The cell's own limit: every file waited for to the end and read to its deadline and its kill's wait."""
    return files * (wait_s + deadline_s + 12) + 60


# -- can the sidecar account for what the agent read, per file? -------------------------------------------------------

ACCOUNTING = """
import errno, hashlib, json, os, time
untouched, repeat, offset, length = {untouched!r}, {repeat!r}, {offset!r}, {length!r}
out = {{"reads": []}}
try:
    out["untouched_size"] = os.stat(untouched).st_size
except OSError as e:
    out["untouched_size"], out["untouched_error"] = None, errno.errorcode.get(e.errno, str(e.errno))
digests = []
for _ in range(2):
    began = time.perf_counter()
    try:
        fd = os.open(repeat, os.O_RDONLY)
        try:
            data = os.pread(fd, length, offset)
        finally:
            os.close(fd)
        digests.append(hashlib.sha256(data).hexdigest())
        out["reads"].append({{"bytes": len(data), "seconds": round(time.perf_counter() - began, 4)}})
    except OSError as e:
        out["reads"].append({{"error": errno.errorcode.get(e.errno, str(e.errno))}})
out["same_bytes_twice"] = len(digests) == 2 and digests[0] == digests[1]
print(json.dumps(out))
"""


def accounting_cell(untouched: str, repeat: str, offset: int, length: int) -> str:
    """The agent's part of the accounting check: stat one file and never read it, and read one range of another twice
    (each time through its own open), so the sidecar's record can be held against a known pattern: a file never read,
    and a read repeated, which the kernel's page cache may answer without asking oneclient again."""
    return ACCOUNTING.format(untouched=untouched, repeat=repeat, offset=offset, length=length)


ONECLIENT_OPTION_WORDS = ("trace", "monitoring", "perf", "verbose", "metric", "statistic", "log")


def candidate_options(help_text: str) -> list[str]:
    """The long options in oneclient's `--help` whose name says it may record reads: a trace, monitoring, metrics,
    statistics, performance logging, verbosity or logging. Names only, sorted, each once."""
    names = re.findall(r"--([a-z][a-z0-9-]+)", help_text)
    return sorted({n for n in names if any(word in n for word in ONECLIENT_OPTION_WORDS)})


def file_guid(file_id: str) -> str | None:
    """Best effort: the file's uuid inside a REST file id, when the id is hex of an 8-byte header and then
    `guid#<uuid>#<space id>`; None when it is not. The encoding is not verified against a source, so a caller that
    finds no match by it also looks for the id itself and the file's name."""
    try:
        text = bytes.fromhex(file_id[16:]).decode("ascii")
    except ValueError:
        return None
    parts = text.split("#")
    return parts[1] if len(parts) >= 3 and parts[0] == "guid" and parts[1] else None


READS = """
import errno, json, os, time
path, reads, sequential = {path!r}, {reads!r}, {sequential!r}
out = []
fd = os.open(path, os.O_RDONLY)
try:
    if sequential:
        os.lseek(fd, reads[0][0], os.SEEK_SET)
    for offset, length in reads:
        began, got = time.perf_counter(), 0
        try:
            while got < length:
                chunk = os.read(fd, length - got) if sequential else os.pread(fd, length - got, offset + got)
                if not chunk:
                    break
                got += len(chunk)
            out.append({{"offset": offset, "length": length, "bytes": got,
                         "seconds": round(time.perf_counter() - began, 4)}})
        except OSError as e:
            out.append({{"offset": offset, "length": length, "error": errno.errorcode.get(e.errno, str(e.errno))}})
finally:
    os.close(fd)
print(json.dumps(out))
"""


def reads_cell(path: str, reads: Sequence[tuple[int, int]], *, sequential: bool = False) -> str:
    """Read the given (offset, length) ranges of one file through one descriptor: each by `pread`, or, `sequential`,
    one after the other by `read` from the first offset, as a program reading a file in order does (the kernel's
    read-ahead follows such a reader). Each read's bytes and seconds, or its errno's name; never the bytes."""
    return READS.format(path=path, reads=[list(r) for r in reads], sequential=sequential)


READER = """
import json, subprocess, sys
child = (
    "import os, random, sys\\n"
    "fd = os.open(sys.argv[1], os.O_RDONLY)\\n"
    "size, count = int(sys.argv[2]), 0\\n"
    "while True:\\n"
    "    os.pread(fd, 1 << 20, random.randrange(0, max(1, size - (1 << 20))))\\n"
    "    count += 1\\n"
    "    with open(sys.argv[3] + '.next', 'w') as handle:\\n"
    "        handle.write(str(count))\\n"
    "    os.replace(sys.argv[3] + '.next', sys.argv[3])\\n"  # whole, so a look never finds it empty
)
reader = subprocess.Popen([sys.executable, "-c", child, {path!r}, str({size!r}), {counter!r}],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          start_new_session=True)
print(json.dumps({{"pid": reader.pid}}))
"""


def reader_cell(path: str, size: int, counter: str) -> str:
    """Start a reader that outlives the cell: it reads 1 MiB at a random offset of the file, again and again, and
    writes how many reads it has done to `counter`, so the host sees whether it moves (a frozen reader does not)."""
    return READER.format(path=path, size=size, counter=counter)


def mentions(text: str, files: Mapping[str, Mapping[str, str | None]]) -> dict[str, dict[str, int]]:
    """For each file (label -> {form: value}), how many lines of `text` hold each non-empty form of it (its REST id,
    its uuid, its name); a form with no value is left out."""
    lines = text.splitlines()
    return {
        label: {form: sum(value in line for line in lines) for form, value in forms.items() if value}
        for label, forms in files.items()
    }
