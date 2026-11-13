# Provenance: new
"""Everything the sandbox does to its host goes through `HostOps`, so a test supplies a fake and touches no machine.

`HostOps` is the injectable interface: eleven primitives (run docker, run a bounded command, read a host file, and a few
host effects such as a quota-limited directory, a certificate authority and an unmount). `DockerHost` implements it
with `docker` and the host's own tools; a test double implements the same protocol. Every function of the package
that reaches the host takes a `HostOps` as its first argument; none of them imports `subprocess` or reads `/proc`,
`/sys` or the kernel log itself.

Secrets never travel in an argument list: `docker` takes an `env` mapping for them, and a failure is logged with the
size and SHA-256 of the tool's own stderr, never its text and never the environment.

Every command runs through `run_bounded`, which bounds it twice. In time (O41): a command past its limit is killed, and
the wait for it to end after the kill is bounded too, since a process in an uninterruptible sleep (a read of a FUSE
mount whose provider is gone) cannot die and `subprocess.run` waits for it for ever, holding the caller and with it
a whole teardown. In memory (O42): with an `output_cap` the first bytes of each stream are kept and the rest is read,
counted and dropped, so a command that writes without end is never blocked on a full pipe and never held whole;
`Counted` says how many bytes it wrote and how many were kept.
"""

import codecs
import contextlib
import errno
import hashlib
import ipaddress
import json
import logging
import os
import platform
import re
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from expcore_onedata.sandbox.api import SandboxError

log = logging.getLogger(__name__)

DOCKER_TIMEOUT_S = 120.0
TIMED_OUT = 124  # the exit code GNU timeout uses, and what a bounded command that ran out of time reports


KILL_WAIT_S = 10.0  # how long a command's end is waited for after its kill: one that does not die is given up on (O41)
READER_JOIN_S = 5.0  # how long the readers of a command's output are waited for once it has ended or was given up on
READ_BYTES = 1 << 16  # what one read of a command's output takes: the most held at once beside the kept part


@dataclass(frozen=True)
class Counted:
    """What one output stream of a command held when a cap was asked for (O42): the bytes it wrote and the bytes kept of
    them, always the first ones and a whole number of characters."""

    produced: int
    kept: int

    @property
    def cut(self) -> bool:
        return self.produced > self.kept

    def record(self) -> dict[str, int]:
        return {"produced": self.produced, "kept": self.kept}


@dataclass(frozen=True)
class CmdResult:
    """A finished command. A command that ran out of time has `returncode` 124 and says so on stderr. With an output
    cap, `stdout_count` and `stderr_count` say how much each stream wrote and how much of it is in the text (O42); they
    are None when no cap was asked for."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    stdout_count: Counted | None = None
    stderr_count: Counted | None = None


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
        output_cap: int | None = None,
    ) -> CmdResult:
        """Run `docker <args>`. With `check`, a non-zero exit raises; `env` is added to the environment. With
        `output_cap`, each of stdout and stderr keeps at most that many bytes (a whole number of characters) and the
        result says how many each wrote (`CmdResult.stdout_count`, O42)."""
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


def _check_cap(cap: object) -> int | None:
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 1):
        raise ValueError("an output cap is a whole number of bytes, at least 1")
    return cap


def _stream_text(data: bytes, produced: int, cap: int | None) -> tuple[str, Counted | None]:
    """The text of one captured stream: `data` is its first bytes, `produced` how many it wrote. Decoded as UTF-8 with
    bad bytes replaced and line ends made universal (`\r\n` and `\r` are `\n`), as `subprocess` did in text mode; a cut
    never ends inside a character, so the bytes kept are those of the characters the text holds."""
    cut = cap is not None and produced > len(data)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    text = decoder.decode(data, final=not cut)
    kept = len(data) - len(decoder.getstate()[0]) if cut else len(data)
    return text.replace("\r\n", "\n").replace("\r", "\n"), (None if cap is None else Counted(produced, kept))


def apply_output_cap(result: CmdResult, cap: int | None) -> CmdResult:
    """`result` as `run_bounded` would have returned it under `cap`: each stream cut to its first `cap` bytes, whole
    characters, and counted. For a host that does not run a process (the in-memory one of the tests); None leaves the
    result as it is."""
    cap = _check_cap(cap)
    if cap is None:
        return result
    parts = []
    for text in (result.stdout, result.stderr):
        raw = text.encode("utf-8", "replace")
        parts.append(_stream_text(raw[:cap], len(raw), cap))
    (out, out_count), (err, err_count) = parts
    return CmdResult(result.returncode, out, err, out_count, err_count)


class _Pipe(threading.Thread):
    """Reads one stream of a command to its end, keeping its first `cap` bytes (all of them without a cap) and counting
    the rest, so what is held is bounded whatever the command writes. `produced` rises before `kept` does, so a
    reader of both, `kept` first, never sees more kept than produced."""

    def __init__(self, stream: Any, cap: int | None) -> None:
        super().__init__(daemon=True, name="command-output")
        self._stream, self._cap = stream, cap
        self.kept = bytearray()
        self.produced = 0

    def run(self) -> None:
        try:
            while chunk := self._stream.read1(READ_BYTES):
                self.produced += len(chunk)
                room = len(chunk) if self._cap is None else self._cap - len(self.kept)
                if room > 0:
                    self.kept += chunk[:room]
        except (OSError, ValueError):  # the stream was closed under it: what it read stands
            pass

    def snapshot(self) -> tuple[bytes, int]:
        data = bytes(self.kept)
        return data, max(self.produced, len(data))


def _feed(stream: Any, data: bytes) -> None:
    """Write a command's input from a thread of its own, so that output being read never waits for it."""
    try:
        stream.write(data)
    except (OSError, ValueError):  # the command ended or closed its stdin before reading all of it
        pass
    finally:
        with contextlib.suppress(OSError, ValueError):
            stream.close()


def run_bounded(
    argv: Sequence[str],
    timeout_s: float,
    *,
    env: Mapping[str, str] | None = None,
    input: str | None = None,
    output_cap: int | None = None,
) -> CmdResult:
    """Run a command within `timeout_s` and return its output as text; never raise for what the command did.

    A command past its time is killed, and its end is waited for at most `KILL_WAIT_S` more: one that does not die (a
    process in uninterruptible sleep) is left behind, and the result is the timeout all the same (returncode 124, stderr
    `timed out after Ns`, stdout what it had written; O41). A program that cannot be run is 127. With `output_cap`, each
    of stdout and stderr keeps at most that many bytes and the rest is read and dropped, and `stdout_count` and
    `stderr_count` say what was written and kept (O42). `env` is added to the environment."""
    cap = _check_cap(output_cap)
    merged = None if env is None else {**os.environ, **env}
    try:
        child = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged,
        )
    except OSError as error:
        return CmdResult(127, "", f"{argv[0]} could not be run: {error.strerror or type(error).__name__}")
    out, err = _Pipe(child.stdout, cap), _Pipe(child.stderr, cap)
    readers = (out, err)
    for reader in readers:
        reader.start()
    if input is not None:
        threading.Thread(
            target=_feed, args=(child.stdin, input.encode("utf-8", "replace")), daemon=True, name="command-input"
        ).start()
    timed_out = False
    try:
        try:
            child.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            child.kill()
            try:
                child.wait(timeout=KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                log.error("a command did not end %g s after its kill: it is left, and its caller goes on", KILL_WAIT_S)
    except BaseException:  # an interrupt: nothing is left running behind the caller
        with contextlib.suppress(OSError):
            child.kill()
        raise
    for reader in readers:
        reader.join(READER_JOIN_S)
    if not any(
        reader.is_alive() for reader in readers
    ):  # nothing reads them any more: closed, not left to the collector
        for stream in (child.stdout, child.stderr):
            with contextlib.suppress(OSError, ValueError):
                stream.close()
    (out_data, out_n), (err_data, err_n) = out.snapshot(), err.snapshot()
    stdout, out_count = _stream_text(out_data, out_n, cap)
    stderr, err_count = _stream_text(err_data, err_n, cap)
    if timed_out:
        return CmdResult(TIMED_OUT, stdout, f"timed out after {timeout_s:g}s", out_count, None)
    return CmdResult(child.returncode, stdout, stderr, out_count, err_count)


class DockerHost:
    """The real host: `docker` on the path, and, for privileged steps, `sudo_command` in front of the command."""

    def __init__(self, *, sudo_command: Sequence[str] = ("sudo", "-n")) -> None:
        self._sudo = tuple(sudo_command)

    def _run(
        self,
        argv: Sequence[str],
        timeout_s: float,
        *,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
        output_cap: int | None = None,
    ) -> CmdResult:
        """Every command of this host, bounded in time and, when asked, in the output kept (`run_bounded`)."""
        return run_bounded(argv, timeout_s, env=env, input=input, output_cap=output_cap)

    def docker(
        self,
        *args: str,
        check: bool = True,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
        output_cap: int | None = None,
    ) -> CmdResult:
        result = self._run(
            ["docker", *args],
            DOCKER_TIMEOUT_S if timeout is None else timeout,
            env=env,
            input=input,
            output_cap=output_cap,
        )
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
        log.error("a mount (%s) is still mounted after an unmount and a lazy unmount", name_digest(path))  # O12
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


IDENTITY_FIELDS = ("id", "dev", "source", "fstype")


def mount_identity(mount: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    """What a mount is wherever it is now (C19): the id the kernel gave it, its device, its source and its file system
    type, as mountinfo shows them (`parse_mountinfo`). Renaming or moving the directory above it changes none of them.
    None unless all four are there (a source may be empty, as the kernel prints one made without it): a recorded mount
    registered before it was made, or one recorded with part of its identity, cannot be found by it anywhere."""
    if any(mount.get(field) is None for field in IDENTITY_FIELDS) or not all(
        str(mount[field]) for field in ("id", "dev", "fstype")
    ):
        return None
    return (str(mount["id"]), str(mount["dev"]), str(mount["source"]), str(mount["fstype"]))


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
