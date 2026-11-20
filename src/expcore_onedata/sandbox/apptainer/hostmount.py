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

**Generations (O97):** the mount of a run that is never restored is generation 1, as it always was. After a cell's
deadline aborted the FUSE connection and the survivors were the agent's, `abort()` frees the cell and `restore()`
makes the next generation (`mountgen`): the watch of the old one closed, its oneclient stopped, the dead mount
unmounted, then a new oneclient and its own watcher, armed, at the same mount point. `mount_watch_logs()` is every
generation's watcher log in order, `generation_oneclient_logs()` every generation's oneclient log, and `connection()`
the live connection's device and fusectl state. Whatever is read of all of them (the guard's snapshot, the I/O-mode
reading) is read of every one.

**What is refused:** a direct mount (its check needs a cell before the first exists, and a provider's storage is on
another node), a run without the token or the provider, an extra argument that sets the port (the provider's is
passed), a platform that declares no bundle, and a bundle that is not the pinned file.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import stat
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore import mountguard as MG
from expcore.mountguard import read_log, snapshot, verdict_of
from expcore_onedata import tls as TLS
from expcore_onedata.sandbox import directio, iomode
from expcore_onedata.sandbox import fusectl as FC
from expcore_onedata.sandbox import mountwatch as MW
from expcore_onedata.sandbox.api import SandboxError, StartRefused
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.apptainer import helpers
from expcore_onedata.sandbox.apptainer import mountabort as MA
from expcore_onedata.sandbox.apptainer import oneclient_bundle as OB
from expcore_onedata.sandbox.apptainer.infra import process_state
from expcore_onedata.sandbox.apptainer.mountgen import Generation, suffix
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
STOPPING = "the mount is being stopped"
# what the I/O-mode reading says of a mount of several generations while nothing judges them together (`_worst_io`)
GENERATIONS_UNJUDGED = "the mount has more than one generation, and no reading of them together is available"


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
        return self.watch_log_of(1)

    def watch_log_of(self, generation: int) -> Path:
        """A generation's watcher log: generation 1's is the log of a run that is never restored."""
        return self.watch_dir / f"mountwatch{suffix(generation)}.jsonl"

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
        *(*launcher, "-f", "-H", provider.dial, "--port", str(provider.port)),  # O73: the mapped address if it has one
        *host_options(settings),
        *(LOG_DIR_OPTION, str(where.log_dir)),
        *settings.mount.extra_args,
        str(where.mountpoint),
    ]


def watcher_argv(
    python: str, where: Layout, nonce: str, oneclient: Any, poll_s: float, log: Path | None = None
) -> list[str]:
    """The watcher's command line; `log` is its generation's log (`Layout.watch_log_of`), generation 1's by default."""
    return [
        *(python, "-I", "-B", "-m", "expcore_onedata.sandbox.mountwatch", "--log", str(log or where.watch_log)),
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
        fusectl_root: Path = FC.FUSECTL,
    ) -> None:
        self.node, self.s, self.spec, self.where = node, settings, spec, Layout(Path(run_dir))
        self._python, self._unmount, self._poll_s = python or sys.executable, tuple(unmount), poll_s
        self._mountinfo, self._sleep, self._clock = mountinfo, sleep, clock
        self._fusectl_root = Path(fusectl_root)
        self.nonce = secrets.token_hex(16)
        # The run's tag (the run directory is named by the sandbox's id): a reaper of a driver killed outright finds
        # oneclient and the watcher by it, as it finds the gate and the collector (`runs.reap`).
        self._tag = {AV.RUN_TAG: self.where.run_dir.name}
        self.bundle: OB.Bundle | None = None
        self._build: dict[str, Any] | None = None  # the oneclient that serves the run: version, build id, pin
        self._ca: Path | None = None  # the run's private copy of the CA bundle: the same for every generation
        self._gens: list[Generation] = [Generation(1)]  # the live generation is the last
        self._closing = False  # a stop began: no restore writes the token file or starts a process from then on
        self._finalized = False  # the guard's window is closed and the run's snapshot and I/O-mode reading are taken
        self._snapshot: dict[str, Any] | None = None
        self._io_mode: dict[str, Any] | None = None
        self._meta: dict[str, Any] = {}
        self._lock = threading.RLock()

    @property
    def _live(self) -> Generation:
        return self._gens[-1]

    @property
    def oneclient(self) -> helpers.Helper | None:
        """The live generation's oneclient."""
        return self._live.oneclient

    @property
    def watch(self) -> helpers.Helper | None:
        """The live generation's watcher."""
        return self._live.watch

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
        if self.s.tls.ca_file is not None:
            self._ca = TLS.write_private_copy(self.s.tls.bundle(), where.home_dir)
        self._launch(self._live)
        return where.mountpoint

    def _launch(self, gen: Generation) -> None:
        """The module's docstring, `start`'s steps 1 and 2, for one generation: oneclient started on the token file
        already written, its mount awaited, then the generation's own watcher, and the token file removed once the
        watcher is armed. The streams and the watcher's log are the generation's own (`mountgen.suffix`)."""
        where, tail = self.where, suffix(gen.number)
        t0 = self._clock()
        argv = oneclient_argv(self.s, self.spec.provider, where, self.bundle.launcher(), self._ca)
        out, err = where.guard / f"oneclient{tail}.stdout", where.guard / f"oneclient{tail}.stderr"
        spawned = self.node.spawn(argv, stdout=out, stderr=err, env=self._tag)
        gen.oneclient = helpers.Helper("oneclient", spawned, out, err)
        self._wait_mounted(gen)
        gen.wait_s = round(self._clock() - t0, 3)
        if gen.number == 1:
            self._meta["wait_s"] = gen.wait_s
        watched = watcher_argv(self._python, where, self.nonce, gen.oneclient.process, self._poll_s,
                               log=where.watch_log_of(gen.number))  # fmt: skip
        self._refuse_if_stopping()
        helpers.start(self.node, f"mountwatch{tail}", watched, where.watch_dir, MW.READY,
                      noted=lambda helper: setattr(gen, "watch", helper), env=self._tag, sleep=self._sleep,
                      clock=self._clock)  # fmt: skip
        self._remove_token()
        self._wait_armed(gen)

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

    def _write_token(self) -> None:
        handle = os.open(self.where.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(self.spec.token.reveal())

    def _remove_token(self) -> None:
        self.where.token_file.unlink(missing_ok=True)
        if os.path.lexists(self.where.token_file):
            raise SandboxError("the mount's token file could not be removed")

    def _wait_mounted(self, gen: Generation) -> None:
        """Until this uid's read-only FUSE mount is at the mount point, and the generation has its connection's device
        and fusectl state (`connection`); `SandboxError` when oneclient ends first, the mount is not as it must be, it
        does not appear in time, or a stop began."""
        until, state, table = self._clock() + MOUNT_WAIT_S, "absent", ""
        while True:
            self._refuse_if_stopping()
            try:
                table = self._mountinfo()
                state = fuse_state(table, str(self.where.mountpoint), os.getuid())
            except OSError:
                state = "unread"
            if state is None:
                gen.device = FC.fuse_device(table, str(self.where.mountpoint))
                gen.fusectl = FC.state(self._fusectl_root, None if gen.device is None else FC.conn(gen.device))
                return
            if state not in ("absent", "unread"):
                raise SandboxError(f"the oneclient mount is not as the host mount makes it ({state})")
            process = gen.oneclient.process
            if self.node.alive(process.pid, process.start) is False:
                status = self.node.exit_status(process)
                raise SandboxError(f"oneclient ended before its mount appeared (exit {status})")
            if self._clock() >= until:
                raise SandboxError("the oneclient mount did not appear")
            self._sleep(POLL_S)

    def _wait_armed(self, gen: Generation) -> None:
        until = self._clock() + ARMED_WAIT_S
        while MW.ARMED not in helpers.output(gen.watch).splitlines():
            self._refuse_if_stopping()
            if self.node.alive(gen.watch.process.pid, gen.watch.process.start) is False:
                raise SandboxError("the mount watcher ended before it saw the token's removal")
            if self._clock() >= until:
                raise SandboxError("the mount watcher did not see the token's removal in time")
            self._sleep(POLL_S)

    # ------------------------------------------------------------------------------------------------ the watch
    def _close_generation(self, gen: Generation) -> None:
        """Close one generation's watch: its watcher stopped (SIGTERM: a last poll, `disarmed`, its end line) and its
        log read once. Its last state is noted first, so the stop is never read as a death. Safe to call again."""
        with self._lock:
            if gen.closed:
                return
            gen.closed = True
            try:
                watch = gen.watch
                if watch is not None:  # its last state of the run: the stop that follows is the harness's own
                    gen.before.setdefault("mount_watch", process_state(self.node, watch.process))
                if watch is not None and self.node.alive(watch.process.pid, watch.process.start) is not False:
                    self._meta["watch_stop"] = helpers.stop(self.node, watch, sleep=self._sleep, clock=self._clock)
            finally:
                gen.log = _read(self.where.watch_log_of(gen.number)) if gen.watch is not None else None

    def close_watch(self) -> None:
        """Close the agent's window of the live generation (every earlier one was closed by its restore), and take what
        is read of all of them: the guard's snapshot over every generation's log and the I/O-mode reading over every
        generation's oneclient log (the module's docstring). Safe to call again."""
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
            try:
                self._close_generation(self._live)
            finally:
                self._judge()

    def _judge(self) -> None:
        """The snapshot of the generations' logs and the I/O-mode reading of their oneclient logs, taken once."""
        live = self._live
        if live.oneclient is not None and live.oneclient_log is None:
            live.oneclient_log = _read_log_link(self.where, directio.LOG_FILE)
        self._snapshot = _guard_snapshot([gen.log for gen in self._gens], self.nonce)
        declared = getattr(self.s.mount, "io_mode", None)
        readings = [
            iomode.judge(declared, None if gen.oneclient_log is None else gen.oneclient_log.decode("utf-8", "replace"))
            for gen in self._gens
        ]
        self._io_mode = _worst_io(readings, declared)

    def mount_watch_logs(self) -> list[bytes | None]:
        """Every generation's watcher log in order (None for one that could not be read), the live window closed first:
        the executor reads them after the run's cells have ended (a cell alive at the close is a failed proof)."""
        self.close_watch()
        return [gen.log for gen in self._gens]

    def mount_watch_log(self) -> bytes | None:
        """The watcher's log (`expcore.mountguard.MountWatchSource`) of a mount with one generation. None for a mount
        that was restored: one log is not the whole run's, and the plural form (`mount_watch_logs`) is."""
        logs = self.mount_watch_logs()
        return logs[0] if len(logs) == 1 else None

    def generation_oneclient_logs(self) -> list[bytes | None]:
        """oneclient's INFO log of every generation in order, None for one that could not be read: an earlier one as it
        was when its generation ended (read before the next started: glog's link moves to the newest), the live one as
        it is now."""
        with self._lock:
            earlier = [gen.oneclient_log for gen in self._gens[:-1]]
            live = self._live.oneclient_log
        return [*earlier, live if live is not None else _read_log_link(self.where, directio.LOG_FILE)]

    def connection(self) -> dict[str, Any]:
        """The live generation's FUSE connection: `device` (its "major:minor" in the mount table, None before the mount
        is up) and `fusectl` (`writable`, `absent` or `not_writable`: whether the owner may abort it, None before the
        mount is up). Read from what the generation recorded when its mount came up, never from the mount."""
        live = self._live
        return {"device": live.device, "fusectl": live.fusectl}

    def abort(self, timeout_s: float) -> dict[str, Any]:
        """Free what waits on the live connection, within `timeout_s` (`mountabort`): the deadline's abort. oneclient's
        state is noted first as the harness's, so its end after the abort is never read as a death; only the freeing
        action runs here, and the unmount and oneclient's stop are the restore's. The record is kept in the
        generation's metadata. Never raises."""
        gen = self._live
        if gen.oneclient is None:
            record = MA.not_attempted()
        else:
            try:
                self._note_mount(gen)
                record = MA.abort(
                    self.node,
                    gen.oneclient.process,
                    connection=None if gen.device is None else FC.conn(gen.device),
                    state=gen.fusectl,
                    root=self._fusectl_root,
                    identity_ok=lambda: self._identity_ok(gen),
                    timeout_s=timeout_s,
                    sleep=self._sleep,
                    clock=self._clock,
                )
            except Exception as error:  # `mountabort.abort` does not raise; the note before it may
                log.error("the abort of the mount could not be made: %s", describe_error(error))
                record = {"ok": False, "method": None, "waiting_before": None, "waiting_after": None,
                          "abort_s": 0.0, "why": MA.RAISED}  # fmt: skip
        with self._lock:
            gen.abort = dict(record)
        return record

    def restore(self) -> dict[str, Any]:
        """Make the next generation of the mount after `abort` freed the cell (the module's docstring): the old
        generation's watch closed (its log read, `disarmed`), its oneclient stopped and its log kept, the dead mount
        unmounted until the mount table shows it gone, then the token file written again, a new oneclient with the same
        command line mounted at the same point, and the generation's own watcher (a log of its own, the run's nonce)
        armed on the token file's removal. `{"ok": True, "generation": n, "wait_s": ...}`. A failure removes the token
        file and raises `SandboxError` with a fixed phrase (never an error's text): the run is then ended by the caller,
        and `stop()` ends whatever of the new generation started. The caller is the sandbox's `_restore_host_mount`,
        under its restore lock, and a stop that begins meanwhile ends it at its next step (`refuse_restores`)."""
        step = "closing the old watch"
        try:
            with self._lock:
                self._refuse_restore()
                old = self._live
            self._note_mount(old)
            self._close_generation(old)
            step = "stopping the old oneclient"
            self._stop_generation(old)
            old.oneclient_log = _read_log_link(self.where, directio.LOG_FILE)  # before glog's link moves to the new one
            step = "unmounting the dead mount"
            self._unmount_once()
            self._mount_gone()
            step = "starting the new mount"
            new = Generation(old.number + 1)
            with self._lock:  # the flag and the token file under one lock: none is written once a stop began
                self._refuse_restore()
                self._gens.append(new)  # live from now: a stop finds whatever of it starts
                self._write_token()
            self._launch(new)
        except Exception as error:
            self._fail_restore(error, step)
        return {"ok": True, "generation": new.number, "wait_s": new.wait_s}

    def _refuse_restore(self) -> None:
        """`SandboxError` when no restore may begin or go on: a stop began, the guard's window is closed (the run's
        snapshot is taken, and a generation made after it would be missing from it), or no oneclient ever started."""
        self._refuse_if_stopping()
        with self._lock:
            if self._finalized:
                raise SandboxError("the mount's guard is closed: no restore can be watched")
            if self._live.oneclient is None:
                raise SandboxError("the mount never started: nothing to restore")

    def _fail_restore(self, error: Exception, step: str) -> None:
        """The restore failed: the token file removed, then `SandboxError` naming the reason. A reason that is not
        already one of ours is the step, never the error's text, which can carry a path."""
        reason = str(error) if isinstance(error, SandboxError) else f"the mount could not be restored while {step}"
        try:
            self._remove_token()
        except SandboxError:
            raise SandboxError(f"{reason}, and its token file could not be removed") from error
        if isinstance(error, SandboxError):
            raise error
        raise SandboxError(reason) from error

    def _identity_ok(self, gen: Generation) -> bool:
        """Whether the mount table, read now, shows this uid's read-only FUSE mount at the mount point on the device
        the generation's mount came up with: the connection's identity, since its number is reused after an unmount.
        Read from the table alone: nothing is statted on a mount that may be wedged."""
        try:
            table = self._mountinfo()
            mountpoint = str(self.where.mountpoint)
            return (
                fuse_state(table, mountpoint, os.getuid()) is None and FC.fuse_device(table, mountpoint) == gen.device
            )
        except (OSError, MountTableError):
            return False

    def refuse_restores(self) -> None:
        """A stop is under way or about to be: from now on no restore writes the token file or starts a process, and one
        that is running ends at its next step. Called by the sandbox's stop, which then waits for the restore."""
        with self._lock:
            self._closing = True

    def _refuse_if_stopping(self) -> None:
        with self._lock:
            if self._closing:
                raise SandboxError(STOPPING)

    # ------------------------------------------------------------------------------------------------ stop
    # ------------------------------------------------------------------------------------------------ its state
    def _mount_state(self, gen: Generation) -> dict[str, Any]:
        end = process_state(self.node, gen.oneclient.process)
        try:
            end["mount"] = fuse_state(self._mountinfo(), str(self.where.mountpoint), os.getuid())
        except (OSError, MountTableError):
            end["mount"] = "unread"
        return end

    def _note_mount(self, gen: Generation) -> None:
        """The oneclient's state just before the harness ends it (an abort, the stop), once: so what follows is never
        read as a death (`infra_check`, O17); its mount's state is unknown, never clean, when it cannot be read."""
        if gen.oneclient is None:
            return
        with self._lock:
            if "mount" in gen.before:
                return
            try:
                gen.before["mount"] = self._mount_state(gen)
            except Exception as error:  # unknown, never clean: and what follows runs all the same
                gen.before["mount"] = {"running": None, "exit_status": None, "mount": "unread"}
                log.error("oneclient's state before its end could not be read: %s", describe_error(error))

    def infra_state(self) -> dict[str, dict[str, Any]]:
        """oneclient with its mount (`mount`) and the guard's watcher (`mount_watch`), by role, for the sandbox's
        `infra_check` (`apptainer.infra`, O17): each one's process (`start`) and its state (`end`), now, or just before
        the harness stopped it (`stopped_by_harness`). Only what has started is listed. The live state is read before
        the note is looked for, and the note is taken before every stop, so a stop that lands meanwhile is found by its
        note, never taken for a death. These roles are the live generation's: an earlier generation of a restored mount
        is listed beside them as `mount_<g>` and `mount_watch_<g>`, each as it was just before the harness ended it."""
        found: dict[str, dict[str, Any]] = {}
        with self._lock:
            gens = list(self._gens)
        for gen in gens:
            live = gen is gens[-1]
            tail = "" if live else f"_{gen.number}"
            for role, helper, now in (("mount", gen.oneclient, self._mount_state if live else None),
                                      ("mount_watch", gen.watch, None)):  # fmt: skip
                if helper is None:
                    continue
                state = (
                    now(gen) if now is not None else process_state(self.node, helper.process)
                )  # first, then the note
                with self._lock:
                    before = gen.before.get(role)
                found[role + tail] = {"start": helper.process.record(), "end": before or state}
                if before is not None:
                    found[role + tail]["stopped_by_harness"] = True
        return found

    def oneclient_log(self) -> bytes | None:
        """oneclient's INFO log, through glog's link to it in its log directory, as it is now, with the logs of the
        generations before it in front (each read when its generation ended); None when any of them cannot be read, or
        when a link lies on the way to it (`_read_log_link`, #362). The read is the driver's, of a directory no cell
        binds, which the guard's watcher does not watch (it watches the token file and its directory alone), so it may
        be read during the run (O18, O60)."""
        logs = self.generation_oneclient_logs()
        return None if any(text is None for text in logs) else b"".join(logs)  # type: ignore[arg-type]

    def stop(self) -> None:
        self.refuse_restores()
        self._note_mount(self._live)  # its last state of the run, before the stop's own unmount and signals
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
        """Every generation's oneclient that still runs (an earlier one ended with its restore)."""
        for gen in list(self._gens):
            self._stop_generation(gen)

    def _stop_generation(self, gen: Generation) -> None:
        process = None if gen.oneclient is None else gen.oneclient.process
        if process is not None and self.node.alive(process.pid, process.start) is not False:
            self._meta["oneclient_stop"] = helpers.stop(self.node, gen.oneclient, sleep=self._sleep, clock=self._clock)

    def _mount_gone(self) -> None:
        if self._mounted():
            self.node.run([self._unmount[0], LAZY, str(self.where.mountpoint)], STEP_S)
        if self._mounted():
            raise SandboxError("the oneclient mount is still in the mount table")

    def _remove_bundle(self) -> None:
        """The bundle's copy, once oneclient is shown gone: 100 MB of a run's directory that is no evidence."""
        for gen in list(self._gens):
            process = None if gen.oneclient is None else gen.oneclient.process
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
            generations = [gen.record() for gen in self._gens]
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
            "fusectl": self._gens[0].fusectl,  # the first generation's: whether this node let the owner abort (O97)
            "remounts": len(generations) - 1,
            "generations": generations,
            **{key: value for key, value in self._meta.items() if key in ("wait_s", "log_files_scrubbed")},
        }


def _guard_snapshot(logs: Sequence[bytes | None], nonce: str) -> dict[str, Any]:
    """The mount guard's snapshot over every generation's watcher log: `expcore.mountguard.aggregate` where it exists
    (resolved when called), which for one log gives that log's snapshot. Without it one log reads as it always did and
    several are unknown, never clean: a restored run is not read as if its later generations were not there."""
    real = getattr(MG, "aggregate", None)
    if real is not None:
        return real(list(logs), nonce)
    return snapshot(read_log(logs[0] if len(logs) == 1 else None, nonce))


def _worst_io(readings: Sequence[dict[str, Any]], declared: object) -> dict[str, Any]:
    """The worst of the generations' I/O-mode readings: `iomode.worst` where it exists (resolved when called), which
    for one reading is that reading. Without it one reading stands and several are not agreement."""
    real = getattr(iomode, "worst", None)
    if real is not None:
        return real(list(readings))
    if len(readings) == 1:
        return readings[0]
    return iomode.judge(declared, None, why_unread=GENERATIONS_UNJUDGED)


def _read(path: str | Path, *, dir_fd: int | None = None) -> bytes | None:
    """A file's bytes, never following a link at its name (in the directory `dir_fd` holds, when given); None when it
    cannot be read."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        try:
            return handle.read()
        except OSError:
            return None


def _read_log_link(where: Layout, name: str) -> bytes | None:
    """oneclient's INFO log through glog's link to it, with no link followed on the way (#362). The path is walked from
    the run's directory, held as a descriptor: each directory down to the log's is opened from the one before it with
    `O_NOFOLLOW` (as `runs.remove_inside` walks one), so a link in any of them, `mount-guard` and `oneclient-log`
    included, or one put there after the run began, makes the log unreadable. No cell can reach these directories
    (`Layout.protected`), so this is defence in depth. In the last directory the log's own name is followed once, by
    `readlink`, and only to a bare file name of that directory (glog writes its link relative, so); a target with a
    directory part, an absolute one or a chain is not followed. The file is then read with `O_NOFOLLOW`, so it is a link
    at no point. None when anything on the way is a link or is not there, or the file cannot be read."""
    try:
        names = where.log_dir.relative_to(where.run_dir).parts
    except ValueError:
        return None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = None
    try:
        descriptor = os.open(where.run_dir, flags)
        for part in names:
            inner = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = inner
        if stat.S_ISLNK(os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode):
            name = os.readlink(name, dir_fd=descriptor)
            if name in ("", ".", "..") or os.sep in name:
                return None
        return _read(name, dir_fd=descriptor)
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


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
