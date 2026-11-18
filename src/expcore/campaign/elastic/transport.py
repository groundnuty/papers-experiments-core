# Provenance: new
"""How the dispatcher reaches a site's login node: one ssh call per site per tick, and one per archive pulled (C44).

`SshTransport.exchange` runs the exchange verb (`elastic.exchange`) with the request on standard input and takes its
whole standard output as one JSON reply: a call that fails, times out, exits non-zero, or prints anything but one reply
of the format for that site is `Unreachable`, never a partial reading. `fetch` copies one archive (`cat`) into a local
file; ssh's own failures (exit 255, a timeout) are `Unreachable`, and a remote file that cannot be read is `Missing`.
Paths and the verb are passed as one shell-quoted command, the only form ssh carries.

`LocalTransport` runs the same two calls on this host, for a site whose login node is the dispatcher's own (step 6):
the verb as a child process and the archive as a copy, with the failures the same two classes.

Live-unproven: the site's ssh options (a control master, `BatchMode`, the login alias) are step 6's; these calls were
run against a fake ssh only.
"""

import contextlib
import json
import os
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.campaign.elastic.exchange import FORMAT, MODULE

SSH_FAILED = 255  # ssh's own exit status when it cannot connect or the connection drops
EXCHANGE_TIMEOUT_S = 120.0
FETCH_TIMEOUT_S = 600.0
MAX_REPLY = 1 << 26


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
    ssh: tuple[str, ...] = ()
    python: tuple[str, ...] = ("python3", "-I", "-B")


Run = Callable[..., subprocess.CompletedProcess]


class SshTransport:
    """The real transport, through `run` (`subprocess.run`)."""

    def __init__(self, run: Run = subprocess.run, exchange_timeout_s: float = EXCHANGE_TIMEOUT_S) -> None:
        self._run = run
        self._timeout_s = exchange_timeout_s

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

    def fetch(self, site: Site, remote: str, local: Path) -> None:
        """Copy `remote` into `local` (a new file); Unreachable or Missing leave no `local` behind."""
        try:
            with local.open("xb") as sink:
                done = self._call(
                    site, ["cat", "--", remote], stdout=sink, stderr=subprocess.DEVNULL, timeout=FETCH_TIMEOUT_S
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
        super().__init__(**kwargs)
        self._cwds = dict(cwds or {})

    def _call(self, site: Site, remote: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
        cwd = self._cwds.get(site.name)
        try:
            return self._run(list(remote), check=False, **({"cwd": cwd} if cwd else {}), **kwargs)
        except (OSError, subprocess.SubprocessError) as error:
            raise Unreachable(f"site {site.name}: {type(error).__name__}") from None


__all__ = ["LocalTransport", "Missing", "Site", "SshTransport", "Unreachable"]
