"""fleetssh.py: the elastic fleet's controller reaching a REMOTE site's login node over ssh (#462 step 6, part 2;
README.md, "The elastic fleet's controller"). One campaign drives one site: the controller and its ledger stay on this
host's local disk, and each command the controller runs at the site is one ssh call to the login node.

- `SshSiteTransport.run(site, argv, timeout)` is the local transport's call over ssh: `argv` is the same argv the local
  transport runs (`SiteConfig.script`), quoted word by word into the one command ssh sends, run from the fleet's own
  directory with the site's `env_file` (site.env) sourced first. The options and the call's bound are `expcore.campaign.
  elastic.transport`'s (`ssh_argv`, `bounded_run`): host-key checking is never weakened, and a call past its bound is
  killed with its process group (`subprocess.TimeoutExpired`, a failed call, the site unread for the tick: unknown is
  never taken for ended). ssh's own failure (exit 255) says only that the call failed.
- `SshSiteTransport.pull(site, remote_dir, local_dir, digest)` is the copy back of what `shards.py collect --roots-only`
  wrote at the site: the manifest first (its SHA-256 must be the one collect printed, which is taken before any file
  it names is asked for), then each file it names, each checked against its size and SHA-256, into a directory beside
  `local_dir`, moved into place by one rename only when whole. `verify-copy` then runs on this host over that copy, and
  only then does `down` go on.
- The host or alias is the operator's; it appears in no result, log or error (the label names the site), and is
  removed from what a remote command printed.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Tuple

SSH_FAILED = 255  # ssh's own exit status: its message names the host, so it is never passed on
MANIFEST = "archives.sha256.json"  # `shards.ARCHIVES_MANIFEST`
FETCH_TIMEOUT_S = 600.0  # `elastic.transport.FETCH_TIMEOUT_S`: one file's pull, with time for its size added
MIN_BYTES_PER_S = 1 << 20  # the slowest pull the bound allows for: the timeout grows by a second per MiB
MAX_MANIFEST = 1 << 20
MAX_FILES = 8  # a root-only copy is a pair; this is the most a manifest may name before nothing is fetched
PLAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
HEX64 = re.compile(r"[0-9a-f]{64}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entries(manifest: Path) -> List[Tuple[str, int, str]]:
    """The files a root-only manifest names, as (name, bytes, sha256); ValueError for a form this pull will not follow
    (verify-copy checks the rest: it is the whole check, this only decides what is asked of the site)."""
    if manifest.stat().st_size > MAX_MANIFEST:
        raise ValueError("the manifest is too long")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    items = document.get("evidence") if isinstance(document, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_FILES:
        raise ValueError("the manifest names no evidence files, or too many")
    found: List[Tuple[str, int, str]] = []
    for item in items:
        name = item.get("name") if isinstance(item, dict) else None
        size, digest = (item.get("bytes"), item.get("sha256")) if isinstance(item, dict) else (None, None)
        if (not isinstance(name, str) or PLAIN_NAME.fullmatch(name) is None or name == MANIFEST
                or isinstance(size, bool) or not isinstance(size, int) or size < 0
                or not isinstance(digest, str) or HEX64.fullmatch(digest) is None
                or name in [n for n, _, _ in found]):  # fmt: skip
            raise ValueError("the manifest names a file this pull will not fetch")
        found.append((name, size, digest))
    return found


def redact(site: Any, text: str) -> str:
    """`text` with the site's host (and the host part of a `user@host`) replaced: for what is logged, printed or said,
    never for what is parsed."""
    host = getattr(site, "host", None)
    if not host:
        return text
    for name in sorted({host, host.rsplit("@", 1)[-1]}, key=len, reverse=True):
        text = text.replace(name, "<host>")
    return text


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class SshSiteTransport:
    """The controller's calls at a site whose `transport` is `ssh` (`SiteConfig.host`)."""

    def __init__(self, environ: Mapping[str, str], run: Optional[Callable[..., Any]] = None,
                 fetch_timeout_s: float = FETCH_TIMEOUT_S) -> None:  # fmt: skip
        from expcore.campaign.elastic.transport import bounded_run

        self.environ = dict(environ)
        self._run = run or bounded_run
        self.fetch_timeout_s = fetch_timeout_s

    def _said(self, site: Any, code: int, out: str, err: str) -> Tuple[int, str, str]:
        if code == SSH_FAILED:
            return code, "", "its ssh call failed"
        return code, out, redact(site, err)  # stdout is what the controller parses: it is never altered here

    def run(self, site: Any, argv: Any, timeout: float) -> Tuple[int, str, str]:
        from expcore.campaign.elastic.transport import remote_command, ssh_argv

        command = remote_command(list(argv), cwd=str(site.fleet), env_file=site.env_file)
        done = self._run([*ssh_argv(site.host), command], env=self.environ, stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, errors="replace", timeout=timeout, check=False)  # fmt: skip
        return self._said(site, done.returncode, done.stdout, done.stderr)

    def stream(self, site: Any, argv: Any, local: Path, timeout_s: Optional[float] = None) -> None:
        """The standard output of the command `argv` run at the site (as `elastic.transport.SshTransport.stream`: the
        site's own ssh, no prefix) into the new file `local`, within `timeout_s` (the fetch bound by default); raises
        `Unreachable` or `Missing` as a pull does."""
        from expcore.campaign.elastic.transport import Site, SshTransport, ssh_argv

        remote = Site(site.label, str(site.fleet), ssh=ssh_argv(site.host), python=())
        SshTransport(run=self._run, fetch_timeout_s=self.fetch_timeout_s).stream(remote, list(argv), local,
                                                                                timeout_s=timeout_s)  # fmt: skip

    def pull(self, site: Any, remote_dir: str, local_dir: Path, digest: str) -> Tuple[int, str, str]:
        """The copy at `remote_dir` of the site into `local_dir` (the module's docstring); (0, said, "") when it is here
        whole, else (1, "", why). A `local_dir` that already holds a copy with this manifest is not fetched again; one
        that holds another is never replaced."""
        from expcore.campaign.elastic.transport import Missing, Site, SshTransport, Unreachable, ssh_argv

        local_dir = Path(local_dir)
        reader = SshTransport(run=self._run, fetch_timeout_s=self.fetch_timeout_s)
        remote = Site(site.label, str(site.fleet), ssh=ssh_argv(site.host), python=())
        stage: Optional[Path] = None
        try:
            if local_dir.is_symlink():
                return 1, "", "this host's copy is a link: it is never followed"
            if local_dir.exists():
                held = local_dir / MANIFEST
                if held.is_file() and not held.is_symlink() and _sha256(held) == digest:
                    _fsync_dir(local_dir)  # a retry after a sync that failed: the copy is made durable before any down
                    _fsync_dir(local_dir.parent)
                    return 0, "its copy is already here", ""
                return 1, "", "this host holds another copy in its place: it is not replaced"
            local_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=f".{local_dir.name}.pull-", dir=str(local_dir.parent)))
            reader.fetch(remote, f"{remote_dir}/{MANIFEST}", stage / MANIFEST, timeout_s=self.fetch_timeout_s)
            if _sha256(stage / MANIFEST) != digest:
                return 1, "", "the manifest read there is not the one collect printed: nothing is fetched"
            for name, size, wanted in _entries(stage / MANIFEST):
                reader.fetch(remote, f"{remote_dir}/{name}", stage / name,
                             timeout_s=self.fetch_timeout_s + size / MIN_BYTES_PER_S)  # fmt: skip
                if (stage / name).stat().st_size != size or _sha256(stage / name) != wanted:
                    return 1, "", "a file read there is not the one the manifest names"
            _fsync_dir(stage)  # the copy's entries are durable before it is in place, and its place before any down
            os.rename(stage, local_dir)
            stage = None
            _fsync_dir(local_dir.parent)
            return 0, "its copy is fetched", ""
        except (Unreachable, subprocess.SubprocessError):
            return 1, "", "its ssh call failed or timed out"
        except Missing:
            return 1, "", "a file of the copy could not be read there"
        except (OSError, ValueError) as error:
            return 1, "", f"the copy could not be fetched ({type(error).__name__})"
        finally:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
