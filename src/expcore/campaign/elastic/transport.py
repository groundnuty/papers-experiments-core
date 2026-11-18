# Provenance: new
"""How the dispatcher reaches a site's login node: one ssh call per site per tick, and one per archive pulled (C44).

`SshTransport.exchange` runs the exchange verb (`elastic.exchange`) with the request on standard input and takes its
whole standard output as one JSON reply: a call that fails, times out, exits non-zero, or prints anything but one reply
of the format for that site is `Unreachable`, never a partial reading. `fetch` copies one archive (`cat`) into a local
file; ssh's own failures (exit 255, a timeout) are `Unreachable`, and a remote file that cannot be read is `Missing`.
Paths and the verb are passed as one shell-quoted command, the only form ssh carries.

`LocalTransport` runs the same two calls on this host, for a site whose login node is the dispatcher's own (step 6):
the verb as a child process and the archive as a copy, with the failures the same two classes.

`ssh_argv` and `remote_command` are the one place an ssh call is built (the controller's own site commands use them
too): the options are fixed and never weaken host-key checking, the host comes after `--`, every word of the remote
command is `shlex.quote`d, and a call is bounded by `bounded_run`, which kills the call's whole process group when its
bound passes. A site's host or alias is the operator's (their ssh config resolves it); it never reaches a log, an error
or a record, which say the site's label only.

Live-unproven: these calls were run against a fake ssh only.
"""

import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expcore.campaign.elastic.exchange import FORMAT, MODULE

SSH_FAILED = 255  # ssh's own exit status when it cannot connect or the connection drops
EXCHANGE_TIMEOUT_S = 120.0
FETCH_TIMEOUT_S = 600.0
MAX_REPLY = 1 << 26
# never a setting that weakens host-key checking (no StrictHostKeyChecking, no UserKnownHostsFile, no accept-new)
SSH_OPTIONS = (
    "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3"
)  # fmt: skip
HOST = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._@:%\[\]-]*")  # an ssh destination or alias, never an option or a space


class Unreachable(OSError):
    """The site's login node was not reached, or its answer was not whole: the site was not read."""


class Missing(OSError):
    """The login node answered, and the file asked for could not be read there."""


@dataclass(frozen=True)
class Site:
    """A site: its label, its channel root on the shared file system, the ssh argv that reaches its login node, and
    the interpreter there that has the core."""

    name: str
    root: str
    ssh: tuple[str, ...] = field(default=(), repr=False)  # holds the host: never shown
    python: tuple[str, ...] = ("python3", "-I", "-B")


def ssh_argv(host: str) -> tuple[str, ...]:
    """The ssh argv that reaches `host`, ready for one remote command word after it; ValueError for a host that could
    be read as an option or split by a shell."""
    if not isinstance(host, str) or HOST.fullmatch(host) is None:
        raise ValueError("a site's host is an ssh destination or alias: letters, digits and . _ - @ : only")
    return ("ssh", *SSH_OPTIONS, "--", host)


def remote_command(argv: Sequence[str], *, cwd: str | None = None, env_file: str | None = None) -> str:
    """The one string ssh sends to the login shell: `argv` with every word quoted, and, when given, first a `cd` into
    `cwd` and the sourcing of `env_file` (site.env, its variables exported), each only if the one before succeeded, so
    a command never runs from the wrong directory or without its site."""
    words = shlex.join(argv)
    if cwd is None and env_file is None:
        return words
    steps = [f"cd {shlex.quote(cwd)}"] if cwd is not None else []
    if env_file is not None:
        steps.append(f"set -a && . {shlex.quote(env_file)} && set +a")
    return " && ".join([*steps, f"exec {words}"])


def bounded_run(argv: Sequence[str], *, check: bool = False, timeout: float | None = None, input: Any = None,
                capture_output: bool = False, stdout: Any = None, stderr: Any = None, stdin: Any = None,
                text: bool = False, **popen: Any) -> subprocess.CompletedProcess:  # fmt: skip
    """`subprocess.run` that runs the call in a session of its own and, when its bound passes, kills the whole group (an
    ssh with a proxy command or a stuck child included) before raising `subprocess.TimeoutExpired`: a hang is a failed
    call, and no pipe it held keeps this one waiting."""
    if capture_output:
        stdout = stderr = subprocess.PIPE
    if input is not None:
        stdin = subprocess.PIPE
    process = subprocess.Popen(list(argv), stdin=stdin, stdout=stdout, stderr=stderr, text=text,
                               start_new_session=True, **popen)  # fmt: skip
    try:
        out, err = process.communicate(input, timeout=timeout)
    except BaseException as error:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
        if isinstance(error, subprocess.TimeoutExpired):
            raise subprocess.TimeoutExpired(list(argv), timeout) from None
        raise
    return subprocess.CompletedProcess(list(argv), process.returncode, out, err)


Run = Callable[..., subprocess.CompletedProcess]


class SshTransport:
    """The real transport, through `run` (`bounded_run`: a call past its bound is killed with its process group)."""

    def __init__(
        self,
        run: Run = bounded_run,
        exchange_timeout_s: float = EXCHANGE_TIMEOUT_S,
        fetch_timeout_s: float = FETCH_TIMEOUT_S,
    ) -> None:
        self._run = run
        self._timeout_s = exchange_timeout_s
        self._fetch_s = fetch_timeout_s

    def _call(self, site: Site, remote: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if not site.ssh:
            raise Unreachable(f"site {site.name} has no ssh command")
        try:
            return self._run([*site.ssh, shlex.join(remote)], check=False, **kwargs)
        except (OSError, subprocess.SubprocessError) as error:
            raise Unreachable(f"site {site.name}: {type(error).__name__}") from None

    def exchange(self, site: Site, request: dict[str, Any]) -> dict[str, Any]:
        done = self._call(
            site,
            [*site.python, "-m", MODULE],
            input=json.dumps(request, sort_keys=True, allow_nan=False).encode("utf-8"),
            capture_output=True,
            timeout=self._timeout_s,
        )
        if done.returncode != 0:
            raise Unreachable(f"site {site.name}: the exchange exited {done.returncode}")
        if len(done.stdout) > MAX_REPLY:
            raise Unreachable(f"site {site.name}: the reply is too long")
        try:
            reply = json.loads(done.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise Unreachable(f"site {site.name}: the reply is not one JSON document") from None
        if not isinstance(reply, dict) or reply.get("format") != FORMAT or reply.get("site") != site.name:
            raise Unreachable(f"site {site.name}: the reply is not an exchange reply for this site")
        return reply

    def fetch(self, site: Site, remote: str, local: Path, *, timeout_s: float | None = None) -> None:
        """Copy `remote` into `local` (a new file), within `timeout_s` (the transport's fetch bound by default);
        Unreachable or Missing leave no `local` behind."""
        try:
            with local.open("xb") as sink:
                done = self._call(
                    site,
                    ["cat", "--", remote],
                    stdout=sink,
                    stderr=subprocess.DEVNULL,
                    timeout=self._fetch_s if timeout_s is None else timeout_s,
                )
                sink.flush()
                os.fsync(sink.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                local.unlink()
            raise
        if done.returncode != 0:
            with contextlib.suppress(OSError):
                local.unlink()
            if done.returncode == SSH_FAILED:
                raise Unreachable(f"site {site.name}: the pull's ssh failed")
            raise Missing(f"site {site.name}: the archive could not be read there (exit {done.returncode})")


class LocalTransport(SshTransport):
    """The same calls run here, not over ssh (`site.python`, the verb's interpreter, is this host's): a site whose login
    node is where the dispatcher runs. A call runs from the directory the site names in `cwds` (its fleet's own 0700
    directory), never from the dispatcher's."""

    def __init__(self, cwds: Mapping[str, str] | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("run", subprocess.run)  # a local call is not given a session of its own
        super().__init__(**kwargs)
        self._cwds = dict(cwds or {})

    def _call(self, site: Site, remote: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
        cwd = self._cwds.get(site.name)
        try:
            return self._run(list(remote), check=False, **({"cwd": cwd} if cwd else {}), **kwargs)
        except (OSError, subprocess.SubprocessError) as error:
            raise Unreachable(f"site {site.name}: {type(error).__name__}") from None


class RoutedTransport(LocalTransport):
    """Each site as it is reached: over ssh when its `ssh` is set (a remote site), else here. One campaign drives one
    site, but a sites file may name a local one beside a remote one."""

    def __init__(self, cwds: Mapping[str, str] | None = None, *, ssh_run: Run = bounded_run, **kwargs: Any) -> None:
        super().__init__(cwds, **kwargs)
        self._ssh = SshTransport(run=ssh_run, exchange_timeout_s=self._timeout_s, fetch_timeout_s=self._fetch_s)

    def _call(self, site: Site, remote: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if site.ssh:
            return self._ssh._call(site, remote, **kwargs)
        return super()._call(site, remote, **kwargs)


__all__ = [
    "LocalTransport",
    "Missing",
    "RoutedTransport",
    "Site",
    "SshTransport",
    "Unreachable",
    "bounded_run",
    "remote_command",
    "ssh_argv",
]
