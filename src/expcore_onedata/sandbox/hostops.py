# Provenance: new
"""Everything the sandbox does to its host goes through `HostOps`, so a test supplies a fake and touches no machine.

`HostOps` is the injectable interface: eleven primitives (run docker, run a bounded command, read a host file, and a few
host effects such as a quota-limited directory, a certificate authority and an unmount). `DockerHost` implements it
with `docker` and the host's own tools; a test double implements the same protocol. Every function of the package
that reaches the host takes a `HostOps` as its first argument; none of them imports `subprocess` or reads `/proc`,
`/sys` or the kernel log itself.

Secrets never travel in an argument list: `docker` takes an `env` mapping for them, and a failure is logged with the
size and SHA-256 of the tool's own stderr, never its text and never the environment.
"""

import errno
import hashlib
import ipaddress
import json
import logging
import os
import platform
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from expcore_onedata.sandbox.api import SandboxError

log = logging.getLogger(__name__)

DOCKER_TIMEOUT_S = 120.0
TIMED_OUT = 124  # the exit code GNU timeout uses, and what a bounded command that ran out of time reports


@dataclass(frozen=True)
class CmdResult:
    """A finished command. A command that ran out of time has `returncode` 124 and says so on stderr."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


@runtime_checkable
class HostOps(Protocol):
    """The host as the sandbox needs it; an implementation raises `SandboxError` for what no result can report."""

    def docker(
        self,
        *args: str,
        check: bool = True,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
    ) -> CmdResult:
        """Run `docker <args>`. With `check`, a non-zero exit raises; `env` is added to the environment."""
        ...

    def run(self, argv: Sequence[str], timeout_s: float, *, sudo: bool = False, input: str | None = None) -> CmdResult:
        """Run a command within `timeout_s`, behind the privilege command when `sudo`. An exit code never raises."""
        ...

    def read_text(self, path: str | Path) -> str:
        """Read a host file (`/proc`, `/sys`, the kernel's files); raise `OSError` when it cannot be read."""
        ...

    def readlink(self, path: str | Path) -> str:
        """Return a symbolic link's target; raise `OSError` when it cannot be read."""
        ...

    def exists(self, path: str | Path) -> bool:
        """Whether a path exists on the host."""
        ...

    def process_alive(self, pid: int) -> bool | None:
        """Whether a process exists: False only for the kernel's "no such process", None when it cannot be told."""
        ...

    def make_quota_dir(self, image: Path, mountpoint: Path, size_bytes: int, uid: int) -> None:
        """Make `mountpoint` a directory capped at `size_bytes` and writable by `uid` (a loop-mounted disk)."""
        ...

    def make_ca(self, directory: Path, names: Sequence[str]) -> None:
        """Write `ca.pem` and a `<name>.pem` / `<name>.key` pair for each name into `directory`."""
        ...

    def unmount(self, path: Path) -> None:
        """Unmount `path` if it is mounted; raise `SandboxError` when it stays mounted."""
        ...

    def image_id(self, reference: str) -> str:
        """Resolve an image reference (tag or id) to its immutable id, `sha256:...`; raise when it has none."""
        ...

    def host_facts(self) -> dict[str, Any]:
        """Facts about the host for the run's record: kernel, docker version. No names or addresses."""
        ...


def stderr_digest(text: str | bytes | None) -> dict[str, Any]:
    """What a log line or a record keeps of a tool's stderr: how many bytes it was and their SHA-256, never the text.

    The tool's own words may name an image, a path or an address from the configuration (O12), so they are not copied
    anywhere; two failures with the same digest are the same words."""
    data = text if isinstance(text, bytes) else (text or "").encode("utf-8", "replace")
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def name_digest(name: object) -> str:
    """A container's name or a host path as a log keeps it: twelve hex digits of its SHA-256. The name holds the lease
    key and the path the work directory, both configured values (O12); whoever holds the name can hash it to match."""
    return hashlib.sha256(str(name).encode("utf-8", "replace")).hexdigest()[:12]


def describe_error(error: BaseException) -> str:
    """An exception for a log line or a record, without anything it may have been given. The sandbox's own errors are
    written to name no configured value; any other holds its class and, for an OS error, the errno's name, never the
    path or the text the system attached."""
    if isinstance(error, SandboxError):
        return f"{type(error).__name__}: {error}"
    if isinstance(error, OSError) and error.errno:
        return f"{type(error).__name__} ({errno.errorcode.get(error.errno, error.errno)})"
    return type(error).__name__


def describe_stderr(text: str | bytes | None) -> str:
    """`stderr_digest` as the words of a log line or a message."""
    digest = stderr_digest(text)
    return f"stderr {digest['bytes']} bytes, sha256 {digest['sha256']}"


def _text(value: str | bytes | None) -> str:
    """Text of a captured stream, which a timed-out process may leave as bytes or None."""
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


class DockerHost:
    """The real host: `docker` on the path, and, for privileged steps, `sudo_command` in front of the command."""

    def __init__(self, *, sudo_command: Sequence[str] = ("sudo", "-n")) -> None:
        self._sudo = tuple(sudo_command)

    def _run(
        self, argv: Sequence[str], timeout_s: float, *, env: Mapping[str, str] | None = None, input: str | None = None
    ) -> CmdResult:
        merged = None if env is None else {**os.environ, **env}
        try:
            done = subprocess.run(
                list(argv),
                input=input,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                env=merged,
                check=False,
            )
        except subprocess.TimeoutExpired as expired:
            return CmdResult(TIMED_OUT, _text(expired.stdout), f"timed out after {timeout_s:g}s")
        except OSError as error:
            return CmdResult(127, "", f"{argv[0]} could not be run: {error.strerror or type(error).__name__}")
        return CmdResult(done.returncode, done.stdout, done.stderr)

    def docker(
        self,
        *args: str,
        check: bool = True,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
    ) -> CmdResult:
        result = self._run(["docker", *args], DOCKER_TIMEOUT_S if timeout is None else timeout, env=env, input=input)
        if check and result.returncode != 0:
            # the tool's own words may name an image, a container or a path from the configuration: neither the log
            # nor the exception (which reaches a record) carries them, only the subcommand, the exit code and the
            # size and SHA-256 of the stderr
            log.error(
                "docker %s failed (%s): %s", args[0] if args else "", result.returncode, describe_stderr(result.stderr)
            )
            raise SandboxError(f"docker {args[0] if args else ''} failed ({result.returncode})")
        return result

    def run(self, argv: Sequence[str], timeout_s: float, *, sudo: bool = False, input: str | None = None) -> CmdResult:
        return self._run([*(self._sudo if sudo else ()), *argv], timeout_s, input=input)

    def read_text(self, path: str | Path) -> str:
        return Path(path).read_text(encoding="utf-8", errors="replace")

    def readlink(self, path: str | Path) -> str:
        return os.readlink(path)

    def exists(self, path: str | Path) -> bool:
        return Path(path).exists()

    def process_alive(self, pid: int) -> bool | None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # it exists, as someone else's
        except OSError:
            return None
        return True

    def _must(self, argv: Sequence[str], timeout_s: float, *, sudo: bool = True) -> None:
        result = self.run(argv, timeout_s, sudo=sudo)
        if result.returncode != 0:
            log.error("%s failed (%s): %s", argv[0], result.returncode, describe_stderr(result.stderr))
            raise SandboxError(f"{argv[0]} failed ({result.returncode})")

    def make_quota_dir(self, image: Path, mountpoint: Path, size_bytes: int, uid: int) -> None:
        mountpoint.mkdir(parents=True, exist_ok=True)
        self._must(["truncate", "-s", str(size_bytes), str(image)], 60, sudo=False)
        self._must(["mkfs.ext4", "-q", "-F", str(image)], 120)
        self._must(["mount", "-o", "loop,nosuid,nodev", str(image), str(mountpoint)], 60)
        self._must(["chown", f"{uid}:{uid}", str(mountpoint)], 30)

    def _openssl(self, line: str, **paths: Path | str) -> None:
        """Run `openssl` with a space-separated line whose `{name}` fields are filled from `paths`."""
        words = [word.format(**{k: str(v) for k, v in paths.items()}) for word in line.split()]
        self._must(["openssl", *words], 60, sudo=False)

    def make_ca(self, directory: Path, names: Sequence[str]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        ca_key, ca_pem = directory / "ca.key", directory / "ca.pem"
        self._openssl(
            "req -x509 -newkey rsa:2048 -nodes -keyout {key} -out {pem} -days 2 -subj /CN=sandbox-run-ca"
            " -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign",
            key=ca_key,
            pem=ca_pem,
        )
        for name in names:
            key, csr, pem, ext = (directory / f"{name}.{suffix}" for suffix in ("key", "csr", "pem", "ext"))
            self._openssl(
                "req -newkey rsa:2048 -nodes -keyout {key} -out {csr} -subj /CN={cn}", key=key, csr=csr, cn=name
            )
            ext.write_text(
                f"subjectAltName=DNS:{name}\nbasicConstraints=CA:FALSE\n"
                "keyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n",
                encoding="utf-8",
            )
            self._openssl(
                "x509 -req -in {csr} -CA {ca} -CAkey {cakey} -CAcreateserial -out {pem} -days 2 -extfile {ext}",
                csr=csr,
                ca=ca_pem,
                cakey=ca_key,
                pem=pem,
                ext=ext,
            )

    def unmount(self, path: Path) -> None:
        if str(path) not in mounted_points(self.read_text("/proc/self/mountinfo")):
            return
        for argv in (["umount", str(path)], ["umount", "-l", str(path)]):
            self.run(argv, 60, sudo=True)
            if str(path) not in mounted_points(self.read_text("/proc/self/mountinfo")):
                return
        log.error("%s is still mounted after an unmount and a lazy unmount", path)
        raise SandboxError("a mount is still mounted after an unmount and a lazy unmount")

    def image_id(self, reference: str) -> str:
        found = self.docker("image", "inspect", "-f", "{{.Id}}", reference, check=False, timeout=30)
        value = found.stdout.strip()
        if found.returncode != 0 or not value.startswith("sha256:"):
            raise SandboxError("an image could not be resolved to an id")
        return value

    def host_facts(self) -> dict[str, Any]:
        version = self.docker("version", "--format", "{{.Server.Version}}", check=False, timeout=30)
        return {"kernel": platform.release(), "docker_server": version.stdout.strip() or None}


# ------------------------------------------------------------------------------------------------ mount tables
_OCTAL = re.compile(r"\\([0-7]{3})")
_MOUNTINFO_ID = re.compile(r"\d+")
_MOUNTINFO_DEV = re.compile(r"\d+:\d+")
_MOUNTINFO_TAG = re.compile(r"[a-z_]+(?::\S+)?")  # an optional field: shared:N, master:N, propagate_from:N, unbindable


class MountTableError(SandboxError):
    """A mount table that is empty, cut off or not mountinfo at all: what it says is unknown, never "no mounts"."""


def _unescape(field: str) -> str:
    """Undo mountinfo's octal escapes (a space is `\\040`)."""
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), field)


def _mountinfo_row(line: str) -> dict[str, str] | None:
    """One mountinfo line, or None when it is not whole. Fields are separated by single spaces (a space in a path is
    `\\040`): an id, a parent id, `major:minor`, a root, an absolute mount point, the mount options, any optional fields
    (`tag[:value]`), the `-` separator, then exactly the file system type, the source and the superblock options. The
    source may be empty, as the kernel prints a mount made without one; the type and the super options never are."""
    head, sep, tail = line.partition(" - ")
    fields, rest = head.split(" "), tail.split(" ")
    whole = (
        sep
        and len(fields) >= 6
        and _MOUNTINFO_ID.fullmatch(fields[0])
        and _MOUNTINFO_ID.fullmatch(fields[1])
        and _MOUNTINFO_DEV.fullmatch(fields[2])
        and fields[3]
        and fields[4].startswith("/")
        and fields[5]
        and all(_MOUNTINFO_TAG.fullmatch(tag) for tag in fields[6:])
        and len(rest) == 3
        and rest[0]
        and rest[2]
    )
    if not whole:
        return None
    return {
        "id": fields[0],
        "mountpoint": _unescape(fields[4]),
        "dev": fields[2],
        "root": _unescape(fields[3]),
        "fstype": rest[0],
        "source": rest[1],
    }


def parse_mountinfo(mountinfo: str) -> list[dict[str, str]]:
    """Every mount of a mountinfo table, strictly: `{"id", "mountpoint", "dev", "root", "fstype", "source"}` each.

    A table that cannot be shown whole is unknown, and a caller that reads "no mount of mine" from it would free a
    resource that is still mounted (C19). So an empty table, any line that is not whole (`_mountinfo_row`: a line cut
    before its superblock options is not), or a table without the root mount (the sanity marker: every real table has
    one, a cut-off one may not) raises `MountTableError`.
    """
    if not mountinfo.strip():
        raise MountTableError("the mount table is empty")
    out = []
    for number, line in enumerate(mountinfo.splitlines(), 1):
        row = _mountinfo_row(line)
        if row is None:
            raise MountTableError(f"line {number} of the mount table does not parse")
        out.append(row)
    if not any(m["mountpoint"] == "/" for m in out):
        raise MountTableError("the mount table has no root mount: it may be cut off")
    return out


def mounted_points(mountinfo: str) -> set[str]:
    """Every mount point in a mountinfo table; `MountTableError` when the table cannot be shown whole."""
    return {m["mountpoint"] for m in parse_mountinfo(mountinfo)}


def fuse_mounts(mountinfo: str) -> list[dict[str, str]]:
    """The FUSE mounts of a mountinfo table: `{"mountpoint", "dev", "source"}` each (`dev` is `major:minor`)."""
    return [
        {"mountpoint": m["mountpoint"], "dev": m["dev"], "source": m["source"]}
        for m in parse_mountinfo(mountinfo)
        if m["fstype"].startswith("fuse")
    ]


def is_fuse_mounted(mountinfo: str, mountpoint: str) -> bool:
    """Whether a FUSE filesystem is mounted at `mountpoint`."""
    return any(m["mountpoint"] == mountpoint for m in fuse_mounts(mountinfo))


def mounts_under(mountinfo: str, root: str | Path) -> list[str]:
    """Mount points inside `root`, deepest first."""
    base = str(root).rstrip("/") + "/"
    return sorted((m for m in mounted_points(mountinfo) if m.startswith(base)), key=lambda m: (-m.count("/"), m))


# ------------------------------------------------------------------------------------------------ docker helpers
def inspect_from(result: CmdResult) -> dict[str, Any] | None:
    """The first object of a `docker inspect` answer; None when the command failed or printed something else."""
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None


def inspect(host: HostOps, name: str, *, timeout: float = 30.0) -> dict[str, Any] | None:
    """`docker inspect` of a container as a dictionary; None when docker reports no such object or cannot be read."""
    return inspect_from(host.docker("inspect", name, check=False, timeout=timeout))


def container_exists(host: HostOps, name: str) -> bool | None:
    """Whether a container (any state) exists: True/False, None when docker could not say."""
    result = host.docker("ps", "-a", "-q", "--filter", f"name=^{name}$", check=False, timeout=30)
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def remove_container(host: HostOps, name: str) -> None:
    """Remove a container with its volumes and verify it is gone; raise `SandboxError` when it is not."""
    host.docker("rm", "-f", "-v", name, check=False, timeout=60)
    if container_exists(host, name) is not False:
        log.error("container %s is not shown gone after its removal", name_digest(name))
        raise SandboxError("a container is not shown gone after its removal")


def network_exists(host: HostOps, name: str) -> bool | None:
    """Whether a docker network exists: True/False, None when docker could not say."""
    result = host.docker("network", "ls", "-q", "--filter", f"name=^{name}$", check=False, timeout=30)
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def remove_network(host: HostOps, name: str) -> None:
    """Remove a network and verify it is gone (a leaked network keeps its subnet); raise `SandboxError` otherwise."""
    host.docker("network", "rm", name, check=False, timeout=60)
    if network_exists(host, name) is not False:
        log.error("network %s is not shown gone after its removal", name_digest(name))
        raise SandboxError("a network is not shown gone after its removal")


def strict_networks(values: Sequence[str]) -> list[ipaddress.IPv4Network]:
    """Parse address ranges as networks in strict mode (no host bits set); raise `ValueError`, never echoing a value."""
    out = []
    for position, value in enumerate(values):  # a range is a configured value: it is named by its position
        try:
            net = ipaddress.ip_network(value, strict=True)
        except ValueError:
            raise ValueError(f"entry {position}: is not a valid IPv4 network with no host bits set") from None
        if not isinstance(net, ipaddress.IPv4Network):
            raise ValueError(f"entry {position}: must be an IPv4 network")
        out.append(net)
    return out
