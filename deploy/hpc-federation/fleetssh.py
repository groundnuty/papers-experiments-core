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
- `SshSiteTransport.pull_places(site, remote_dir, local_dir)` is the copy back of a member's channel places
  (`<channel>/<label>/places/`, the stores the worker leaves for a lease that is lost): for each `lease-<id>/` there,
  the newest `KEEP` snapshots (each a tar of the snapshot, extracted with the `data` filter) and `spend.jsonl`, into a
  directory beside `local_dir`, moved into place by one rename only when whole. A directory that is not there, and a
  place without snapshots or a journal, are nothing to pull; a call that fails or times out is a failed pull (a place
  is never taken for one with nothing in it), so the retirement stops before `down`, as an evidence pull that fails.
- The host or alias is the operator's; it appears in no result, log or error (the label names the site), and is
  removed from what a remote command printed.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
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
PLACE_NAME = re.compile(r"lease-[0-9]{12}")  # `elastic.channel.place_name`: a place's directory under `places/`
SNAPSHOT_NAME = re.compile(r"snap-[0-9]{6,}")  # `out_snapshot`: one snapshot's directory
LIST_TIMEOUT_S = 120.0  # one listing or test of a directory at the site


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


def site_ssh(site: Any) -> Tuple[str, ...]:
    """The site's ssh argv: its host, through its own ssh config file when it names one (`ssh_config`)."""
    from expcore.campaign.elastic.transport import ssh_argv

    return ssh_argv(site.host, getattr(site, "ssh_config", None))


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
        from expcore.campaign.elastic.transport import remote_command

        command = remote_command(list(argv), cwd=str(site.fleet), env_file=site.env_file)
        done = self._run([*site_ssh(site), command], env=self.environ, stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, errors="replace", timeout=timeout, check=False)  # fmt: skip
        return self._said(site, done.returncode, done.stdout, done.stderr)

    def stream(self, site: Any, argv: Any, local: Path, timeout_s: Optional[float] = None) -> None:
        """The standard output of the command `argv` run at the site (as `elastic.transport.SshTransport.stream`: the
        site's own ssh, no prefix) into the new file `local`, within `timeout_s` (the fetch bound by default); raises
        `Unreachable` or `Missing` as a pull does."""
        from expcore.campaign.elastic.transport import Site, SshTransport

        remote = Site(site.label, str(site.fleet), ssh=site_ssh(site), python=())
        SshTransport(run=self._run, fetch_timeout_s=self.fetch_timeout_s).stream(remote, list(argv), local,
                                                                                timeout_s=timeout_s)  # fmt: skip

    def pull(self, site: Any, remote_dir: str, local_dir: Path, digest: str) -> Tuple[int, str, str]:
        """The copy at `remote_dir` of the site into `local_dir` (the module's docstring); (0, said, "") when it is here
        whole, else (1, "", why). A `local_dir` that already holds a copy with this manifest is not fetched again; one
        that holds another is never replaced."""
        from expcore.campaign.elastic.transport import Missing, Site, SshTransport, Unreachable

        local_dir = Path(local_dir)
        reader = SshTransport(run=self._run, fetch_timeout_s=self.fetch_timeout_s)
        remote = Site(site.label, str(site.fleet), ssh=site_ssh(site), python=())
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

    def _printed(self, site: Any, argv: List[str]) -> str:
        """What the plain command `argv` printed at the site (as `stream` runs it: no `cd`, no site.env, which these
        reads need none of); a command that exits non-zero raises `Missing`, a failed call `Unreachable`."""
        scratch = Path(tempfile.mkdtemp(prefix="expcore-list-"))  # 0700, ours
        try:
            self.stream(site, argv, scratch / "said", LIST_TIMEOUT_S)
            return (scratch / "said").read_text(encoding="utf-8", errors="replace")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def exists(self, site: Any, flag: str, path: str) -> bool:
        """Whether `path` is there (`test -d` or `-f` at the site). The answer is what the command prints, `yes` or
        `no`, never a status: a failed call, and any other answer, raises, and is no answer."""
        from expcore.campaign.elastic.transport import Unreachable

        said = self._printed(site, ["sh", "-c", 'if test "$1" "$2"; then echo yes; else echo no; fi', "sh", flag, path])
        if said.strip() not in ("yes", "no"):
            raise Unreachable("the site's answer is not whole")
        return said.strip() == "yes"

    def names(self, site: Any, directory: str, pattern: "re.Pattern[str]") -> List[str]:
        """The names in `directory` at the site that `pattern` matches, sorted; none when it is not there."""
        if not self.exists(site, "-d", directory):
            return []
        return sorted(n for n in self._printed(site, ["ls", "-1", "--", directory]).split("\n") if pattern.fullmatch(n))

    def _pull_place(self, site: Any, there: str, place: Path, scratch: Path) -> None:
        """One lease's place into `place` (new): its newest snapshots and its journal, as `locate` reads them."""
        from expcore.campaign.elastic.snapshots import SNAPSHOTS
        from expcore.campaign.out_snapshot import KEEP
        from expcore.campaign.run_lease import LEASE_JOURNAL

        place.mkdir()
        newest = sorted(self.names(site, f"{there}/{SNAPSHOTS}", SNAPSHOT_NAME), key=lambda n: int(n[5:]),
                        reverse=True)[:KEEP]  # fmt: skip
        for name in newest:
            tar = scratch / f"{name}.tar"
            self.stream(site, ["tar", "-C", f"{there}/{SNAPSHOTS}/{name}", "-cf", "-", "."], tar)
            with tarfile.open(tar, "r:") as archive:
                archive.extractall(place / SNAPSHOTS / name, filter="data")
            tar.unlink()
        if self.exists(site, "-f", f"{there}/{LEASE_JOURNAL}"):
            self.stream(site, ["cat", "--", f"{there}/{LEASE_JOURNAL}"], place / LEASE_JOURNAL)

    def pull_places(self, site: Any, remote_dir: str, local_dir: Path) -> Tuple[int, str, str]:
        """The places under `remote_dir` of the site into `local_dir` (the module's docstring); (0, said, "") when they
        are here whole or there are none, else (1, "", why). A `local_dir` that is already here is a retry's earlier
        pull, whole by its rename: it is not fetched again."""
        from expcore.campaign.elastic.transport import Missing, Unreachable

        local_dir = Path(local_dir)
        stage: Optional[Path] = None
        try:
            if local_dir.is_symlink():
                return 1, "", "this host's places are a link: it is never followed"
            if local_dir.exists():
                return 0, "its places are already here", ""
            names = self.names(site, remote_dir, PLACE_NAME)
            if not names:
                return 0, "it has no places", ""
            stage = Path(tempfile.mkdtemp(prefix=f".{local_dir.name}.pull-", dir=str(local_dir.parent)))
            (stage / "tmp").mkdir()
            (stage / "places").mkdir()
            for name in names:
                self._pull_place(site, f"{remote_dir}/{name}", stage / "places" / name, stage / "tmp")
            shutil.rmtree(stage / "tmp")
            _fsync_dir(stage / "places")  # the places are in their directory before any down
            os.rename(stage / "places", local_dir)
            _fsync_dir(local_dir.parent)
            return 0, f"its {len(names)} place(s) are fetched", ""
        except (Unreachable, subprocess.SubprocessError):
            return 1, "", "its ssh call failed or timed out"
        except Missing:
            return 1, "", "a file of a place could not be read there"
        except (OSError, tarfile.TarError, ValueError) as error:
            return 1, "", f"the places could not be fetched ({type(error).__name__})"
        finally:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
