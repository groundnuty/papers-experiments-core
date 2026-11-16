# Provenance: new
"""Hook (c)'s helper: the mount of a run on an HPC node, oneclient on the node outside every cell, watched (O60).

On a batch node the cells and oneclient run as one uid, with no root, no user namespace and FUSE without
`user_allow_other` (a mount is visible only to the uid that made it), so the Docker sidecar's guarantee that the agent
holds no token cannot be kept. The PI's ruling (16 Nov 2026): the mount path stays on HPC, with detection and report,
as for egress. `HostMount` is the `mounthook.MountHelper` that does it; `host_mount_factory` makes the factory the
sandbox takes (`mount_helper=`).

**`start()`**, before the gate and any cell, under `<run>/mount-guard/` (every directory 0700, none of them under any
path a cell binds; the cell binds only the mount point, `<run>/mount`, read-only at `[sandbox.mount] path`):

1. the token is written to `token/token` (0600), and oneclient is started on the node: `sh -c` reads that file into
   `ONECLIENT_ACCESS_TOKEN`, refuses an empty one, then becomes `env` without the variables that could set an option
   (`UNSET_ENV`), with `HOME` its own `oneclient-home/` and, with the platform's `ca_file`, `SSL_CERT_FILE` a copy of
   the bundle; then oneclient in the foreground with `-o ro` (no `allow_other`, which the node refuses), the declared
   force-I/O option, `--log-dir oneclient-log/`, `--config /dev/null`, `--disable-log-buffering` (#307), the
   provider's port and the study's extra arguments. The token is never in an argument or in the driver's environment;
   the binary is pinned by its SHA-256 (O13);
2. once the FUSE mount is up at the mount point (this uid's, read-only, no `allow_other`), the watcher
   (`expcore_onedata.sandbox.mountwatch`) is started on the token file, its directory and the mount, with the four
   private directories as the protected ones; then the token file is removed (verified) and the watcher, seeing the
   removal, opens the agent's window and says so: the start returns only then.

**`close_watch()`**, at the collection (once every cell's process is gone) or, at the latest, when the instrument
reads the log (`mount_watch_log()`) or at `stop()`: the watcher is stopped (SIGTERM: a last poll, `disarmed`, its end
line), its log read once and judged (`expcore.mountguard`), and oneclient's I/O mode read from its log (`iomode`).

**`stop()`**: the watch closed; the mount unmounted (`fusermount3 -u`, then oneclient stopped, then a lazy unmount if
the mount is still in the table); oneclient shown gone; the token file shown gone; any file of oneclient's log that
holds the token removed. Each step is tried and the stop raises `SandboxError` naming those that failed; a second call
does only what is left.

**What is refused:** a direct mount (its check needs a cell before the first exists, and a provider's storage is on
another node), a run without the token or the provider, an extra argument that sets the port (the provider's is
passed), and a binary that is not the pinned file.
"""

from __future__ import annotations

import os
import secrets
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.mountguard import read_log, snapshot, verdict_of
from expcore_onedata import tls as TLS
from expcore_onedata.sandbox import directio, iomode
from expcore_onedata.sandbox import mountwatch as MW
from expcore_onedata.sandbox.api import SandboxError, StartRefused
from expcore_onedata.sandbox.apptainer import helpers, layout
from expcore_onedata.sandbox.apptainer.mounthook import MountHelperFactory
from expcore_onedata.sandbox.apptainer.node import NodeOps
from expcore_onedata.sandbox.apptainer.settings import Pinned
from expcore_onedata.sandbox.containers import extra_args_record
from expcore_onedata.sandbox.hostops import MountTableError, describe_error
from expcore_onedata.sandbox.oneclient_args import (
    CONFIG_NULL,
    CONFIG_OPTION,
    DIRECT,
    DISABLE_LOG_BUFFERING,
    IO_MODE_FLAGS,
    LOG_DIR_OPTION,
    UNSET_ENV,
    abbreviates,
    mount_problem,
    short_letters,
)

MOUNT_WAIT_S = 60.0
ARMED_WAIT_S = 30.0
POLL_S = 0.1
STEP_S = 30.0
UNMOUNT = ("fusermount3", "-u")
ENV_ACCESS_TOKEN = "ONECLIENT_ACCESS_TOKEN"
# The wrapper's shell: read the token file named by its first argument into oneclient's variable, refuse an empty one,
# then become `env` with the rest. The value lives in oneclient's own environment, never in an argument.
HOST_START = (
    f't="$1"; shift; {ENV_ACCESS_TOKEN}="$(cat -- "$t")" && [ -n "${ENV_ACCESS_TOKEN}" ] || exit 97; '
    f'export {ENV_ACCESS_TOKEN}; exec env "$@"'
)
SHELL_NAME = "oneclient-host"
DIRECT_REFUSED = (
    '[sandbox.mount] io_mode "direct" is not available on the Apptainer backend: its check reads a file from a cell '
    "before any cell exists, and the provider's storage is on another node"
)
PORT_REFUSED = "[sandbox.mount] extra_args must not set the port: the host mount passes the provider's"
NOT_COLLECTED = "not_collected"


@dataclass(frozen=True)
class Layout:
    """The helper's files in the run's directory (the module's docstring)."""

    run_dir: Path

    @property
    def guard(self) -> Path:
        return self.run_dir / "mount-guard"

    @property
    def token_dir(self) -> Path:
        return self.guard / "token"

    @property
    def token_file(self) -> Path:
        return self.token_dir / "token"

    @property
    def log_dir(self) -> Path:
        return self.guard / "oneclient-log"

    @property
    def home_dir(self) -> Path:
        return self.guard / "oneclient-home"

    @property
    def watch_dir(self) -> Path:
        return self.guard / "watch"

    @property
    def watch_log(self) -> Path:
        return self.watch_dir / "mountwatch.jsonl"

    @property
    def mountpoint(self) -> Path:
        return self.run_dir / "mount"

    def protected(self) -> tuple[Path, ...]:
        """The directories no cell may reach: the token's, oneclient's log and home, the watcher's."""
        return (self.token_dir, self.log_dir, self.home_dir, self.watch_dir)


def host_options(settings: Any) -> list[str]:
    """The oneclient options the host mount sets, as a record keeps them (no path, no host): read-only without
    `allow_other`, the declared force-I/O option, no configuration file, unbuffered log lines, and `--insecure` when the
    platform's `[tls]` says so."""
    mount = settings.mount
    return [
        *("-o", "ro", IO_MODE_FLAGS[mount.io_mode], CONFIG_OPTION, CONFIG_NULL, DISABLE_LOG_BUFFERING),
        *(["--insecure"] if settings.tls.insecure else []),
    ]


def oneclient_argv(settings: Any, provider: Any, where: Layout, binary: Path, ca: Path | None) -> list[str]:
    """The wrapper's command line (the module's docstring, step 1)."""
    trust = [f"SSL_CERT_FILE={ca}"] if ca is not None else []
    return [
        *("sh", "-c", HOST_START, SHELL_NAME, str(where.token_file)),
        *(word for name in UNSET_ENV for word in ("-u", name)),
        f"HOME={where.home_dir}",
        *trust,
        *(str(binary), "-f", "-H", provider.host, "--port", str(provider.port)),
        *host_options(settings),
        *(LOG_DIR_OPTION, str(where.log_dir)),
        *settings.mount.extra_args,
        str(where.mountpoint),
    ]


def watcher_argv(python: str, where: Layout, nonce: str, oneclient: Any, poll_s: float) -> list[str]:
    return [
        *(python, "-I", "-m", "expcore_onedata.sandbox.mountwatch", "--log", str(where.watch_log)),
        *("--nonce", nonce, "--token", str(where.token_file), "--mountpoint", str(where.mountpoint)),
        *("--oneclient", f"{oneclient.pid}:{oneclient.start}", "--poll-s", str(poll_s)),
        *(word for path in where.protected() for word in ("--protect", str(path))),
    ]


def sets_port(argument: str) -> bool:
    return abbreviates(argument, "--port") or "P" in short_letters(argument)


def fuse_state(mountinfo: str, mountpoint: str, uid: int) -> str | None:
    """None when this uid's read-only FUSE mount without `allow_other` is at `mountpoint`; else what is wrong
    (`absent`, `not_ours`, `allow_other`, `writable`). The last mount at the point is the one a path reaches."""
    found = None
    for line in mountinfo.splitlines():
        head, sep, tail = line.partition(" - ")
        fields, rest = head.split(" "), tail.split(" ")
        if sep and len(fields) >= 6 and len(rest) >= 3 and fields[4] == mountpoint:
            found = (fields[5].split(","), rest[0], rest[2].split(","))
    if found is None or not found[1].startswith("fuse"):
        return "absent"
    options, _, supers = found
    if f"user_id={uid}" not in supers:
        return "not_ours"
    if "allow_other" in supers:
        return "allow_other"
    return None if "ro" in options else "writable"


def _own_mountinfo() -> str:
    with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as stream:
        return stream.read()


_PINNED: dict[layout.Identity, str] = {}
_PINNED_LOCK = threading.Lock()


def pin_binary(pin: Pinned) -> layout.Identity:
    """oneclient's binary by its identity once its SHA-256 is the pinned one (O13); `StartRefused` otherwise."""
    try:
        before = layout.identity(pin.path)
        with _PINNED_LOCK:
            digest = _PINNED.get(before) or layout.sha256_of(pin.path)
        if layout.identity(pin.path) != before:
            raise StartRefused("oneclient's binary changed while it was hashed")
    except OSError:
        raise StartRefused("oneclient's binary could not be read on this node") from None
    if digest != pin.sha256:
        raise StartRefused("oneclient's binary is not the file it is pinned to (its SHA-256 differs)")
    with _PINNED_LOCK:
        _PINNED[before] = digest
    return before


class HostMount:
    """One run's mount and its guard (the module's docstring)."""

    def __init__(
        self,
        node: NodeOps,
        settings: Any,
        spec: Any,
        run_dir: Path,
        *,
        oneclient: Pinned,
        python: str | None = None,
        unmount: Sequence[str] = UNMOUNT,
        poll_s: float = MW.POLL_S,
        mountinfo: Callable[[], str] = _own_mountinfo,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.node, self.s, self.spec, self.where = node, settings, spec, Layout(Path(run_dir))
        self._pin, self._python, self._unmount, self._poll_s = (
            oneclient,
            python or sys.executable,
            tuple(unmount),
            poll_s,
        )
        self._mountinfo, self._sleep, self._clock = mountinfo, sleep, clock
        self.nonce = secrets.token_hex(16)
        self.oneclient: helpers.Helper | None = None
        self.watch: helpers.Helper | None = None
        self._closed = False
        self._log: bytes | None = None
        self._snapshot: dict[str, Any] | None = None
        self._io_mode: dict[str, Any] | None = None
        self._meta: dict[str, Any] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------------------------------------ start
    def _validate(self) -> None:
        mount, spec = self.s.mount, self.spec
        if mount is None:
            raise StartRefused("a mount data path needs [sandbox.mount]")
        problem = mount_problem(mount.io_mode, mount.extra_args, mount.storages, mount.direct_check_file)
        if problem is not None:
            raise StartRefused(problem)
        if mount.io_mode == DIRECT:
            raise StartRefused(DIRECT_REFUSED)
        if any(sets_port(argument) for argument in mount.extra_args):
            raise StartRefused(PORT_REFUSED)
        if spec.token is None or spec.provider is None:
            raise StartRefused("a mount data path needs the data-path token and the agent's local provider")
        pin_binary(self._pin)

    def start(self) -> Path:
        self._validate()
        where = self.where
        for directory in (where.guard, *where.protected(), where.mountpoint):
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)  # whatever the driver's umask
        self._write_token()
        ca = TLS.write_private_copy(self.s.tls.bundle(), where.home_dir) if self.s.tls.ca_file is not None else None
        t0 = self._clock()
        argv = oneclient_argv(self.s, self.spec.provider, where, self._pin.path, ca)
        out, err = where.guard / "oneclient.stdout", where.guard / "oneclient.stderr"
        self.oneclient = helpers.Helper("oneclient", self.node.spawn(argv, stdout=out, stderr=err), out, err)
        self._wait_mounted()
        self._meta["wait_s"] = round(self._clock() - t0, 3)
        watched = watcher_argv(self._python, where, self.nonce, self.oneclient.process, self._poll_s)
        helpers.start(self.node, "mountwatch", watched, where.watch_dir, MW.READY, noted=self._noted,
                      sleep=self._sleep, clock=self._clock)  # fmt: skip
        self._remove_token()
        self._wait_armed()
        return where.mountpoint

    def _noted(self, helper: helpers.Helper) -> None:
        self.watch = helper

    def _write_token(self) -> None:
        handle = os.open(self.where.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(self.spec.token.reveal())

    def _remove_token(self) -> None:
        self.where.token_file.unlink(missing_ok=True)
        if os.path.lexists(self.where.token_file):
            raise SandboxError("the mount's token file could not be removed")

    def _wait_mounted(self) -> None:
        """Until this uid's read-only FUSE mount is at the mount point; `SandboxError` when oneclient ends first, the
        mount is not as it must be, or it does not appear in time."""
        until, state = self._clock() + MOUNT_WAIT_S, "absent"
        while True:
            try:
                state = fuse_state(self._mountinfo(), str(self.where.mountpoint), os.getuid())
            except OSError:
                state = "unread"
            if state is None:
                return
            if state not in ("absent", "unread"):
                raise SandboxError(f"the oneclient mount is not as the host mount makes it ({state})")
            process = self.oneclient.process
            if self.node.alive(process.pid, process.start) is False:
                status = self.node.exit_status(process)
                raise SandboxError(f"oneclient ended before its mount appeared (exit {status})")
            if self._clock() >= until:
                raise SandboxError("the oneclient mount did not appear")
            self._sleep(POLL_S)

    def _wait_armed(self) -> None:
        until = self._clock() + ARMED_WAIT_S
        while MW.ARMED not in helpers.output(self.watch).splitlines():
            if self.node.alive(self.watch.process.pid, self.watch.process.start) is False:
                raise SandboxError("the mount watcher ended before it saw the token's removal")
            if self._clock() >= until:
                raise SandboxError("the mount watcher did not see the token's removal in time")
            self._sleep(POLL_S)

    # ------------------------------------------------------------------------------------------------ the watch
    def close_watch(self) -> None:
        """Close the agent's window and read the watcher's log once (the module's docstring); safe to call again."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                watch = self.watch
                if watch is not None and self.node.alive(watch.process.pid, watch.process.start) is not False:
                    self._meta["watch_stop"] = helpers.stop(self.node, watch, sleep=self._sleep, clock=self._clock)
            finally:
                self._log = _read(self.where.watch_log) if self.watch is not None else None
                self._snapshot = snapshot(read_log(self._log, self.nonce))
                text = _read(self.where.log_dir / directio.LOG_FILE)
                declared = getattr(self.s.mount, "io_mode", None)
                self._io_mode = iomode.judge(declared, None if text is None else text.decode("utf-8", "replace"))

    def mount_watch_log(self) -> bytes | None:
        """The watcher's log (`expcore.mountguard.MountWatchSource`), its window closed first if still open: the
        executor reads it after the run's cells have ended (a cell alive at the close is a failed proof)."""
        self.close_watch()
        return self._log

    # ------------------------------------------------------------------------------------------------ stop
    def stop(self) -> None:
        errors: list[str] = []
        for what, step in (("watch", self.close_watch), ("unmount", self._unmount_once),
                           ("oneclient", self._stop_oneclient), ("mount", self._mount_gone),
                           ("token file", self._remove_token), ("oneclient log", self._scrub)):  # fmt: skip
            try:
                step()
            except Exception as error:  # every step is tried: the stop names each one that failed
                errors.append(f"{what} ({describe_error(error)})")
        if errors:
            raise SandboxError("the mount helper's stop is incomplete: " + "; ".join(errors))

    def _mounted(self) -> bool:
        try:
            return fuse_state(self._mountinfo(), str(self.where.mountpoint), os.getuid()) != "absent"
        except (OSError, MountTableError):
            return True  # a table that cannot be read shows nothing gone

    def _unmount_once(self) -> None:
        if self._mounted():
            self.node.run([*self._unmount, str(self.where.mountpoint)], STEP_S)

    def _stop_oneclient(self) -> None:
        process = None if self.oneclient is None else self.oneclient.process
        if process is not None and self.node.alive(process.pid, process.start) is not False:
            self._meta["oneclient_stop"] = helpers.stop(self.node, self.oneclient, sleep=self._sleep, clock=self._clock)

    def _mount_gone(self) -> None:
        if self._mounted():
            self.node.run([*self._unmount, "-z", str(self.where.mountpoint)], STEP_S)
        if self._mounted():
            raise SandboxError("the oneclient mount is still in the mount table")

    def _scrub(self) -> None:
        """Remove any file of oneclient's log that holds the token: a secret is never evidence (O05)."""
        token = self.spec.token.reveal().encode() if self.spec.token is not None else b""
        removed = 0
        if token and self.where.log_dir.is_dir():
            for path in self.where.log_dir.iterdir():
                data = _read(path)
                if data is not None and token in data:
                    path.unlink()
                    removed += 1
        self._meta["log_files_with_token"] = removed

    # ------------------------------------------------------------------------------------------------ record
    def metadata(self) -> dict[str, Any]:
        """The mount's facts for the run's record, never a secret, a path or a host: the I/O-mode reading (`io_mode`,
        taken when the watch closed), the options, the study's extra arguments by name, and the guard: its nonce, the
        run's verdict and the snapshot (`not_collected` before the watch closed)."""
        mount = self.s.mount
        declared, extra = getattr(mount, "io_mode", None), getattr(mount, "extra_args", ())
        with self._lock:
            taken = self._io_mode
            guard = (
                {"nonce": self.nonce, "verdict": verdict_of(self._snapshot), "snapshot": dict(self._snapshot)}
                if self._snapshot is not None
                else {"nonce": self.nonce, "verdict": NOT_COLLECTED, "snapshot": None}
            )
        return {
            "io_mode": taken or iomode.judge(declared, None, why_unread=iomode.NOT_COLLECTED),
            "oneclient": "node",
            "options": host_options(self.s) if declared in IO_MODE_FLAGS else [],
            "extra_args": extra_args_record(extra),
            "log_dir_from": "sandbox",
            "guard": guard,
            **{key: value for key, value in self._meta.items() if key in ("wait_s", "log_files_with_token")},
        }


def _read(path: Path) -> bytes | None:
    """A file's bytes, never following a link; None when it cannot be read."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        try:
            return handle.read()
        except OSError:
            return None


def host_mount_factory(oneclient: Pinned, **options: Any) -> MountHelperFactory:
    """The factory the Apptainer sandbox takes (`mount_helper=`), for oneclient's pinned binary on the node."""

    def factory(node: NodeOps, settings: Any, spec: Any, run_dir: Path) -> HostMount:
        return HostMount(node, settings, spec, run_dir, oneclient=oneclient, **options)

    return factory


__all__ = ["HostMount", "Layout", "fuse_state", "host_mount_factory", "host_options", "oneclient_argv"]
