# Provenance: new
"""Hook (c)'s helper: the mount of a run on an HPC node, oneclient on the node outside every cell, watched (O60).

On a batch node the cells and oneclient run as one uid, with no root, no user namespace and FUSE without
`user_allow_other` (a mount is visible only to the uid that made it), so the Docker sidecar's guarantee that the agent
holds no token cannot be kept. The PI's ruling (16 Nov 2026): the mount path stays on HPC, with detection and report,
as for egress. `HostMount` is the `mounthook.MountHelper` that does it; `host_mount_factory` makes the factory the
sandbox takes (`mount_helper=`).

**`start()`**, before the gate and any cell, under `<run>/mount-guard/` (every directory 0700, none of them under any
path a cell binds; the cell binds only the mount point, `<run>/mount`, read-only at `[sandbox.mount] path`):

1. oneclient's bundle, the tar `[sandbox.apptainer] oneclient_bundle` pins, is unpacked into `oneclient-bundle/` from
   the bytes that are hashed (`oneclient_bundle.unpack`: another file refuses the start), and its own `--version` and
   the binary's GNU build id are read for the record. The token is written to `token/token` (0600), and oneclient is
   started on the node through the bundle's launcher (the image's loader and libraries, with no RPATH and no loader
   cache consulted): `sh -c` reads that file into `ONECLIENT_ACCESS_TOKEN`, refuses an empty one, then becomes `env`
   without the variables that could set an option (`UNSET_ENV`), with `HOME` its own `oneclient-home/` and, with the
   platform's `ca_file`, `SSL_CERT_FILE` a copy of the CA bundle; then oneclient in the foreground with `-o ro` (no
   `allow_other`, which the node refuses), the declared force-I/O option, `--log-dir oneclient-log/`, `--config
   /dev/null`, `--disable-log-buffering` (#307), the provider's port and the study's extra arguments. The token is
   never in an argument or in the driver's environment;
2. once the FUSE mount is up at the mount point (this uid's, read-only, no `allow_other`), the watcher
   (`expcore_onedata.sandbox.mountwatch`) is started on the token file, its directory and the mount, with the five
   private directories as the protected ones; then the token file is removed (verified) and the watcher, seeing the
   removal, opens the agent's window and says so: the start returns only then.

**`close_watch()`**, at the collection (once every cell's process is gone) or, at the latest, when the instrument
reads the log (`mount_watch_log()`) or at `stop()`: the watcher is stopped (SIGTERM: a last poll, `disarmed`, its end
line), its log read once and judged (`expcore.mountguard`), and oneclient's I/O mode read from its log (`iomode`).

**What the sandbox reads of it** (O17, O18): `infra_state()`, oneclient with its mount and the watcher by role, each
as it is now or as it was just before the harness stopped it (`apptainer.infra`), and `oneclient_log()`, oneclient's
INFO log, which the driver may read at any time: the watcher does not watch the log directory.

**`stop()`**: the watch closed; the mount unmounted (`fusermount3 -u`, then oneclient stopped, then a lazy unmount if
the mount is still in the table); oneclient shown gone; the token file shown gone; any file of oneclient's log that
holds the token removed; the bundle's copy removed. Each step is tried and the stop raises `SandboxError` naming those
that failed; a second call does only what is left.

**What is refused:** a direct mount (its check needs a cell before the first exists, and a provider's storage is on
another node), a run without the token or the provider, an extra argument that sets the port (the provider's is
passed), a platform that declares no bundle, and a bundle that is not the pinned file.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
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
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.apptainer import helpers
from expcore_onedata.sandbox.apptainer import oneclient_bundle as OB
from expcore_onedata.sandbox.apptainer.infra import process_state
from expcore_onedata.sandbox.apptainer.mounthook import MountHelperFactory
from expcore_onedata.sandbox.apptainer.node import NodeOps
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

log = logging.getLogger(__name__)

MOUNT_WAIT_S = 60.0
ARMED_WAIT_S = 30.0
POLL_S = 0.1
STEP_S = 30.0
UNMOUNT = ("fusermount3", "-u")
LAZY = "-uz"  # the lazy unmount, as the reaper's (`runs`): the mount detached even while a process holds it
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
NO_BUNDLE = (
    "a mount data path on the Apptainer backend needs [sandbox.apptainer] oneclient_bundle: the pinned oneclient the "
    "host mount runs on the node (deploy/hpc-federation/README.md)"
)
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
    def bundle_dir(self) -> Path:
        return self.guard / "oneclient-bundle"

    @property
    def watch_log(self) -> Path:
        return self.watch_dir / "mountwatch.jsonl"

    @property
    def mountpoint(self) -> Path:
        return self.run_dir / "mount"

    def protected(self) -> tuple[Path, ...]:
        """The directories no cell may reach: the token's, oneclient's log, home and bundle, the watcher's."""
        return (self.token_dir, self.log_dir, self.home_dir, self.watch_dir, self.bundle_dir)


def host_options(settings: Any) -> list[str]:
    """The oneclient options the host mount sets, as a record keeps them (no path, no host): read-only without
    `allow_other`, the declared force-I/O option, no configuration file, unbuffered log lines, and `--insecure` when the
    platform's `[tls]` says so."""
    mount = settings.mount
    return [
        *("-o", "ro", IO_MODE_FLAGS[mount.io_mode], CONFIG_OPTION, CONFIG_NULL, DISABLE_LOG_BUFFERING),
        *(["--insecure"] if settings.tls.insecure else []),
    ]


def clean_env(where: Layout) -> list[str]:
    """`env`'s words before oneclient: the variables that could set an option unset, `HOME` oneclient's own."""
    return [*(word for name in UNSET_ENV for word in ("-u", name)), f"HOME={where.home_dir}"]


def probe_env(where: Layout) -> list[str]:
    """`env -i`'s variables for the bundle's `--version`: those a spawned helper gets (`NodeOps.spawn`: the node's
    `PATH`, `LC_ALL=C`) with oneclient's own `HOME`, and nothing of the driver's environment besides."""
    return [f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}", "LC_ALL=C", f"HOME={where.home_dir}"]


def oneclient_argv(settings: Any, provider: Any, where: Layout, launcher: Sequence[str], ca: Path | None) -> list[str]:
    """The wrapper's command line (the module's docstring, step 1); `launcher` runs the bundle's oneclient."""
    trust = [f"SSL_CERT_FILE={ca}"] if ca is not None else []
    return [
        *("sh", "-c", HOST_START, SHELL_NAME, str(where.token_file)),
        *clean_env(where),
        *trust,
        *(*launcher, "-f", "-H", provider.host, "--port", str(provider.port)),
        *host_options(settings),
        *(LOG_DIR_OPTION, str(where.log_dir)),
        *settings.mount.extra_args,
        str(where.mountpoint),
    ]


def watcher_argv(python: str, where: Layout, nonce: str, oneclient: Any, poll_s: float) -> list[str]:
    return [
        *(python, "-I", "-B", "-m", "expcore_onedata.sandbox.mountwatch", "--log", str(where.watch_log)),
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


def bundle_pin(settings: Any) -> Any:
    """The platform's pin of oneclient's bundle, None when it declares none."""
    return getattr(getattr(settings, "apptainer", None), "oneclient_bundle", None)


class HostMount:
    """One run's mount and its guard (the module's docstring)."""

    def __init__(
        self,
        node: NodeOps,
        settings: Any,
        spec: Any,
        run_dir: Path,
        *,
        python: str | None = None,
        unmount: Sequence[str] = UNMOUNT,
        poll_s: float = MW.POLL_S,
        mountinfo: Callable[[], str] = _own_mountinfo,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.node, self.s, self.spec, self.where = node, settings, spec, Layout(Path(run_dir))
        self._python, self._unmount, self._poll_s = python or sys.executable, tuple(unmount), poll_s
        self._mountinfo, self._sleep, self._clock = mountinfo, sleep, clock
        self.nonce = secrets.token_hex(16)
        # The run's tag (the run directory is named by the sandbox's id): a reaper of a driver killed outright finds
        # oneclient and the watcher by it, as it finds the gate and the collector (`runs.reap`).
        self._tag = {AV.RUN_TAG: self.where.run_dir.name}
        self.bundle: OB.Bundle | None = None
        self._build: dict[str, Any] | None = None  # the oneclient that serves the run: version, build id, pin
        self.oneclient: helpers.Helper | None = None
        self.watch: helpers.Helper | None = None
        self._closed = False
        self._log: bytes | None = None
        self._snapshot: dict[str, Any] | None = None
        self._io_mode: dict[str, Any] | None = None
        self._meta: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._before: dict[str, dict[str, Any]] = {}  # a role's state just before the harness stopped it (O17)

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
        if bundle_pin(self.s) is None:
            raise StartRefused(NO_BUNDLE)

    def start(self) -> Path:
        self._validate()
        where = self.where
        for directory in (where.guard, *where.protected(), where.mountpoint):
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)  # whatever the driver's umask
        self.bundle = OB.unpack(bundle_pin(self.s), where.bundle_dir)
        self._build = self._read_build(self.bundle)
        self._write_token()
        ca = TLS.write_private_copy(self.s.tls.bundle(), where.home_dir) if self.s.tls.ca_file is not None else None
        t0 = self._clock()
        argv = oneclient_argv(self.s, self.spec.provider, where, self.bundle.launcher(), ca)
        out, err = where.guard / "oneclient.stdout", where.guard / "oneclient.stderr"
        spawned = self.node.spawn(argv, stdout=out, stderr=err, env=self._tag)
        self.oneclient = helpers.Helper("oneclient", spawned, out, err)
        self._wait_mounted()
        self._meta["wait_s"] = round(self._clock() - t0, 3)
        watched = watcher_argv(self._python, where, self.nonce, self.oneclient.process, self._poll_s)
        helpers.start(self.node, "mountwatch", watched, where.watch_dir, MW.READY, noted=self._noted, env=self._tag,
                      sleep=self._sleep, clock=self._clock)  # fmt: skip
        self._remove_token()
        self._wait_armed()
        return where.mountpoint

    def _read_build(self, bundle: OB.Bundle) -> dict[str, Any]:
        """What the run records of its oneclient: its own `--version`, run here, the binary's build id, and the pin.

        The probe runs in the environment the mount's oneclient gets (`probe_env`), never the driver's: a site's
        `LD_PRELOAD` or `LD_AUDIT` there would load the node's code into the probe alone."""
        ran = self.node.run(["env", "-i", *probe_env(self.where), *bundle.launcher(), "--version"], STEP_S)
        return {
            **OB.version_of(ran.returncode, ran.stdout),
            "build_id": OB.build_id(bundle.binary),
            "bundle_sha256": bundle_pin(self.s).sha256,
        }

    def token_paths(self) -> list[Path]:
        """The token's file, which the sandbox lists in the run's record before the start writes it (O60)."""
        return [self.where.token_file]

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
                if watch is not None:  # its last state of the run: the stop that follows is the harness's own
                    self._before.setdefault("mount_watch", process_state(self.node, watch.process))
                if watch is not None and self.node.alive(watch.process.pid, watch.process.start) is not False:
                    self._meta["watch_stop"] = helpers.stop(self.node, watch, sleep=self._sleep, clock=self._clock)
            finally:
                self._log = _read(self.where.watch_log) if self.watch is not None else None
                self._snapshot = snapshot(read_log(self._log, self.nonce))
                text = _read_log_link(self.where.log_dir, directio.LOG_FILE)
                declared = getattr(self.s.mount, "io_mode", None)
                self._io_mode = iomode.judge(declared, None if text is None else text.decode("utf-8", "replace"))

    def mount_watch_log(self) -> bytes | None:
        """The watcher's log (`expcore.mountguard.MountWatchSource`), its window closed first if still open: the
        executor reads it after the run's cells have ended (a cell alive at the close is a failed proof)."""
        self.close_watch()
        return self._log

    # ------------------------------------------------------------------------------------------------ stop
    # ------------------------------------------------------------------------------------------------ its state
    def _mount_state(self) -> dict[str, Any]:
        process = self.oneclient.process
        end = process_state(self.node, process)
        try:
            end["mount"] = fuse_state(self._mountinfo(), str(self.where.mountpoint), os.getuid())
        except (OSError, MountTableError):
            end["mount"] = "unread"
        return end

    def infra_state(self) -> dict[str, dict[str, Any]]:
        """oneclient with its mount (`mount`) and the guard's watcher (`mount_watch`), by role, for the sandbox's
        `infra_check` (`apptainer.infra`, O17): each one's process (`start`) and its state (`end`), now, or just before
        the harness stopped it (`stopped_by_harness`). Only what has started is listed. The live state is read before
        the note is looked for, and the note is taken before every stop, so a stop that lands meanwhile is found by its
        note, never taken for a death."""
        found: dict[str, dict[str, Any]] = {}
        for role, helper, now in (("mount", self.oneclient, self._mount_state),
                                  ("mount_watch", self.watch, None)):  # fmt: skip
            if helper is None:
                continue
            live = now() if now is not None else process_state(self.node, helper.process)  # first, then the note
            with self._lock:
                before = self._before.get(role)
            found[role] = {"start": helper.process.record(), "end": before or live}
            if before is not None:
                found[role]["stopped_by_harness"] = True
        return found

    def oneclient_log(self) -> bytes | None:
        """oneclient's INFO log, through glog's link to it in its log directory, as it is now; None when it cannot be
        read. The read is the driver's, of a directory no cell binds, which the guard's watcher does not watch (it
        watches the token file and its directory alone), so it may be read during the run (O18, O60)."""
        return _read_log_link(self.where.log_dir, directio.LOG_FILE)

    def stop(self) -> None:
        if self.oneclient is not None:  # its last state of the run, before the stop's own unmount and signals
            with self._lock:
                if "mount" not in self._before:
                    try:
                        self._before["mount"] = self._mount_state()
                    except Exception as error:  # unknown, never clean: and the stop's steps run all the same
                        self._before["mount"] = {"running": None, "exit_status": None, "mount": "unread"}
                        log.error("oneclient's state before the stop could not be read: %s", describe_error(error))
        errors: list[str] = []
        for what, step in (("watch", self.close_watch), ("unmount", self._unmount_once),
                           ("oneclient", self._stop_oneclient), ("mount", self._mount_gone),
                           ("token file", self._remove_token), ("oneclient log", self._scrub),
                           ("bundle", self._remove_bundle)):  # fmt: skip
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
            self.node.run([self._unmount[0], LAZY, str(self.where.mountpoint)], STEP_S)
        if self._mounted():
            raise SandboxError("the oneclient mount is still in the mount table")

    def _remove_bundle(self) -> None:
        """The bundle's copy, once oneclient is shown gone: 100 MB of a run's directory that is no evidence."""
        process = None if self.oneclient is None else self.oneclient.process
        if process is not None and self.node.alive(process.pid, process.start) is not False:
            raise SandboxError("oneclient is not shown gone, so its bundle is kept")
        if self.where.bundle_dir.is_dir():
            shutil.rmtree(self.where.bundle_dir)

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
        self._meta["log_files_scrubbed"] = removed

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
            "oneclient_build": dict(self._build) if self._build is not None else None,
            "options": host_options(self.s) if declared in IO_MODE_FLAGS else [],
            "extra_args": extra_args_record(extra),
            "log_dir_from": "sandbox",
            "guard": guard,
            **{key: value for key, value in self._meta.items() if key in ("wait_s", "log_files_scrubbed")},
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


def _read_log_link(directory: Path, name: str) -> bytes | None:
    """oneclient's INFO log through glog's link to it: followed only to a regular file of the same directory, then read
    without following anything further; None otherwise."""
    try:
        target = (directory / name).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if target.parent != directory.resolve():
        return None
    return _read(target)


def host_mount_factory(*binary: Any, **options: Any) -> MountHelperFactory:
    """The factory the Apptainer sandbox takes (`mount_helper=`). Its oneclient is the platform's bundle,
    `[sandbox.apptainer] oneclient_bundle`, whose SHA-256 the campaign fingerprint holds: a binary given here is
    refused, since it would serve runs the fingerprint does not tell apart."""
    if binary or "oneclient" in options:
        raise TypeError(
            "host_mount_factory() takes no oneclient: declare its bundle as [sandbox.apptainer] oneclient_bundle and "
            "oneclient_bundle_sha256 (deploy/hpc-federation/README.md), so the fingerprint holds it"
        )

    def factory(node: NodeOps, settings: Any, spec: Any, run_dir: Path) -> HostMount:
        return HostMount(node, settings, spec, run_dir, **options)

    return factory


__all__ = ["HostMount", "Layout", "clean_env", "fuse_state", "host_mount_factory", "host_options", "oneclient_argv"]
