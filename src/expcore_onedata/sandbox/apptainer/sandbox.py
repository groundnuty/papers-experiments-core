# Provenance: new
"""One sandboxed run on an HPC batch node: setuid Apptainer for the cells, the core's gate and the egress collector as
helper processes on the node, over an injectable `NodeOps` (O56).

`ApptainerSandbox` is the platform layer's `Sandbox` (`api.py`), and also what `SandboxPlatform` (`provider.py`) and the
code tool call of a sandbox beyond that protocol: `start`/`begin`/`abandon`, `exec`, `mark_run_start`, `collect`,
`stop`, `metadata`, `record`, `read_only_inputs`, `settle_deadline`, `gate_refusals`, `served_bytes`,
`mount_io_mode`, `session_kinds`, `session_ends`, `teardown_left`, `keep_evidence`, `stopped`; for the egress
instrument (`expcore.egress.EgressLog`) `egress_log`, `egress_declaration` and `egress_nonce`; and for the process
visibility instrument (`expcore.visibility.ProcessVisibility`) `process_visibility`.

Per run, under `work_root/<sid>` (owner-only, on the node's tmpfs), recorded first in `work_root/_runs/<sid>.json`
(`runs.py`), so a driver killed outright is reaped by the next start and its key held by the probe:

* `work/`, `tmp/` and `shm/`: the cell's `/work`, `/tmp` and `/dev/shm`, kept across cells, bounded by the watch;
* the egress collector (`egress-collector.py`, a pinned copy), started first, on a fresh 0700 socket directory that
  every cell binds read-only at `/hpcop-egress`; the pinned egress shim, preloaded in every cell through a bound
  `/etc/ld.so.preload`; a canary connect from inside a cell opens the collector's log before anything else runs, and
  proves the watch sees a cell's processes, or the start fails;
* REST: the core's gate on 127.0.0.1 (`gaterun.py`), with the token in its memory alone; the cell resolves the zone's
  and the provider's names to the loopback and trusts the run's CA; its `ONEDATA_TOKEN` is the placeholder (O23);
* a mount: oneclient on the node, outside every cell, by the helper of hook (c) (`mounthook.py`, `hostmount.py`); a
  mount run is refused at its start without the helper, without `[sandbox.mount]`, and by the helper itself for a
  direct mount (HPC mounts are proxy-only until the direct-I/O comparison passes);
* no name server: every cell binds a `resolv.conf` that names none and an `nsswitch.conf` whose hosts are `files`, so
  the C library resolves the names of `/etc/hosts` alone: the federation's, as Docker's `--add-host` gives them
  (`facts.py`, `dns_none`);
* every cell one `apptainer exec --containall` (`cells.py`, `argv.py`), the root of the image and every bind but
  `/work`, `/tmp` and `/dev/shm` read-only, proved at the start as on Docker, with the read-only inputs (O20).

What departs from the Docker sandbox is recorded in every run's metadata (`facts.py`).
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import secrets
import signal
import stat
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from expcore.config.loading import ConfigError
from expcore.egress import Declaration, declaration
from expcore_onedata import tls as TLS
from expcore_onedata.fault_drain import GATE, MOUNT
from expcore_onedata.sandbox import deadline as DL
from expcore_onedata.sandbox import gatelog, gatepolicy, inputs, kinds, tokenfiles
from expcore_onedata.sandbox import termination as TERM
from expcore_onedata.sandbox.api import DataPathMode, ExecResult, ReadOnlyInput, SandboxError, SandboxSpec, StartRefused
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.apptainer import facts, gaterun, helpers, layout, loopback, runs
from expcore_onedata.sandbox.apptainer.cells import AppCellRunner
from expcore_onedata.sandbox.apptainer.collect import Collection
from expcore_onedata.sandbox.apptainer.mounthook import MOUNT_REFUSED, MountHelper, MountHelperFactory
from expcore_onedata.sandbox.apptainer.node import NodeOps
from expcore_onedata.sandbox.apptainer.teardown import Teardown
from expcore_onedata.sandbox.apptainer.watch import Watch, spaces_of
from expcore_onedata.sandbox.cells import OUTPUT_CAP_BYTES
from expcore_onedata.sandbox.completion import ExecIncomplete
from expcore_onedata.sandbox.containers import (
    CA_PATH,
    CERTIFI_PROBE,
    ENV_CA_BUNDLES,
    ENV_TOKEN,
    ENV_VERIFY_SSL,
    ENV_ZONE_HOST,
    certifi_bundle,
    trust_targets,
)
from expcore_onedata.sandbox.hostops import describe_error, describe_stderr
from expcore_onedata.sandbox.registry import DEFAULT, Registry
from expcore_onedata.sandbox.sandbox import READ_ONLY_PROBE, RUN_KEY, RUN_START_POLLS
from expcore_onedata.sandbox.settings import SandboxSettings

log = logging.getLogger(__name__)

COLLECTOR_READY = "egress collector ready"
GATE_ATTEMPTS = 3  # a free port another process took before the gate bound it: tried again with another
PROBE_S = 60.0
CANARY = (
    "import socket, time\n"
    "try:\n"
    "    socket.create_connection(('127.0.0.1', {port}), 10).close()\n"
    "    print('canary open')\n"
    "except OSError as e:\n"
    "    print('canary ' + type(e).__name__)\n"
    "time.sleep({hold:.3f})\n"
)


class ApptainerSandbox:
    """The `Sandbox` of the platform layer on an HPC batch node. Build it with `start`; stop it with `stop`."""

    def __init__(
        self,
        node: NodeOps,
        settings: SandboxSettings,
        spec: SandboxSpec,
        *,
        registry: Registry = DEFAULT,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        config_sha256: str | None = None,
        output_cap_bytes: int = OUTPUT_CAP_BYTES,
        mount_helper: MountHelperFactory | None = None,
        **docker_only: Any,  # the Docker sandbox's options a platform may pass to every backend (waits, sampling)
    ) -> None:
        self.node, self.s, self.spec, self.registry = node, settings, spec, registry
        self.app = settings.apptainer
        self._sleep, self._clock, self._output_cap, self._mount_factory = sleep, clock, output_cap_bytes, mount_helper
        key = re.sub(r"[^A-Za-z0-9_.-]", "_", spec.lease_key or spec.run_key)[:48]
        self.sid = f"{key}-{uuid.uuid4().hex[:8]}"
        self.dir = settings.work_root / self.sid
        self.run_tag = f"{AV.RUN_TAG}={self.sid}"
        self.egress_nonce = secrets.token_hex(16)
        self._meta: dict[str, Any] = {
            "run_key": spec.run_key,
            "data_path": spec.mode.value,
            "listing": spec.listing if spec.mode is DataPathMode.REST else None,
            "backend": self.app.record() if self.app is not None else {"backend": "apptainer"},
            "infrastructure_facts": facts.record(mount_refused=self._mount_refused()),
            "images": {},
            "tls": settings.tls.record(),
            "certifi": None,
            "cell_deadline": {"grace_s": settings.kill_grace_s, "margin_s": DL.margin_s(settings.kill_grace_s)},
        }
        if config_sha256:
            self._meta["platform_config_sha256"] = config_sha256
        self._meta_lock = threading.RLock()
        self._phase = threading.Lock()
        self._agent_began, self._steps_running, self._refusals_seen = False, 0, 0
        self._stopped, self._stop_lock = True, threading.Lock()
        self._abandoned = threading.Event()
        self._created, self._created_lock = 0, threading.Lock()
        self._cells: AppCellRunner | None = None
        self._watch: Watch | None = None
        self._helpers: dict[str, helpers.Helper] = {}
        self._mount: MountHelper | None = None
        self._token_files: list[Path] = []
        self._ca_bytes: bytes | None = None
        self._ca_copy: Path | None = None
        self._inputs: tuple[ReadOnlyInput, ...] = ()
        self._input_copies: list[tuple[Path, str]] = []
        self._image_identity: layout.Identity | None = None
        self._binds: list[AV.Bind] = []
        self._env: dict[str, str] = {}
        self._gate_port: int | None = None
        self._canary_port: int | None = None
        self._ends: dict[str, float] = {}
        self._started = time.time()
        self.keep_evidence = False
        self.collection = Collection(self)
        self.teardown = Teardown(self)

    # ------------------------------------------------------------------------------------------------ start
    @classmethod
    def start(cls, node: NodeOps, settings: SandboxSettings, spec: SandboxSpec, **options: Any) -> ApptainerSandbox:
        """Check the spec and pin the image, then build the sandbox; a failure after anything exists stops it first."""
        self = cls(node, settings, spec, **options)
        self.begin()
        return self

    def abandon(self) -> None:
        """Ask a start running in another thread to stop creating things: it cleans up what it made and raises."""
        self._abandoned.set()

    def _checkpoint(self) -> None:
        if self._abandoned.is_set():
            raise SandboxError("the start was abandoned")

    def register_creation(self) -> None:
        with self._created_lock:
            self._created += 1

    def begin(self) -> None:
        """The blocking half of `start`: check, pin, build. A failure after anything exists stops it first."""
        TERM.raise_if_requested()
        self._validate()
        self._checkpoint()
        self._stopped = False
        self._meta["pending_cleanups_left"] = self.registry.drain()
        self._meta["reaped_orphans"] = runs.reap(self.node, self.s.work_root, own=self.sid)  # never raises
        try:
            self._build()
            self._checkpoint()
            TERM.register(self)
            TERM.raise_if_requested()
        except BaseException as exc:
            try:
                self.stop(force=self._abandoned.is_set())
            except Exception as error:
                self.registry.add_pending(self)
                exc.sandbox = self  # type: ignore[attr-defined]
                log.error("cleanup after a failed start was incomplete: %s", describe_error(error))
            raise

    def _mount_refused(self) -> bool:
        """Whether a mount data path is refused here: no helper is given or the platform declares no `[sandbox.mount]`.
        The run's facts list `mount_refused` exactly when this holds; the helper's own refusals (a direct mount, a run
        without a token) come after it, at the helper's start."""
        return self._mount_factory is None or self.s.mount is None

    def _validate(self) -> None:
        sp, s = self.spec, self.s
        if self.app is None:
            raise StartRefused('the Apptainer sandbox needs [sandbox] backend = "apptainer" and [sandbox.apptainer]')
        if not RUN_KEY.fullmatch(sp.run_key):
            raise StartRefused("the run key must be 1 to 200 printable ASCII characters without spaces")
        if (sp.token is None) != (sp.mode is DataPathMode.NONE):
            raise StartRefused("a data path takes the data-path token, and no data path takes none")
        if sp.mode is DataPathMode.MOUNT and self._mount_refused():
            raise StartRefused(MOUNT_REFUSED)
        if sp.mode is not DataPathMode.NONE and sp.provider is None:
            raise StartRefused("a data path needs the agent's local provider")
        if sp.mode is DataPathMode.REST and sp.zone is None:
            raise StartRefused("a REST data path needs the zone")
        if sp.mode is DataPathMode.REST and s.federation is not None and s.federation.zone is None:
            raise StartRefused("[sandbox.federation] names no zone, which the REST data path's gate serves (O69)")
        if sp.mode is DataPathMode.REST and s.placeholder_token == sp.token.reveal():
            raise StartRefused("[sandbox] placeholder_token is the run's data-path token: the agent may hold none")
        if sp.listing not in gatepolicy.LISTINGS or (
            sp.mode is not DataPathMode.REST and sp.listing != gatepolicy.PAGED
        ):
            raise StartRefused("the listing mode must be paged or complete, and complete is for a REST data path only")
        if sp.mode is not DataPathMode.NONE and s.tls.ca_file is not None:
            try:
                self._ca_bytes = s.tls.bundle()
            except ConfigError as error:
                raise StartRefused(str(error)) from None
        if self.app.gate_options.get(gaterun.PEER_CHECK_KEY) is True and not gaterun.PEER_CHECK.is_file():
            raise StartRefused("gate_options asks for the gate's peer-uid check, and this core's gate does not have it")
        self._inputs = inputs.validate(sp.inputs, inputs.taken_paths(s), storages=inputs.storage_paths(s))
        layout.check_work_root(s.work_root, os.getuid())
        self._image_identity = layout.pin_image(self.app.image)  # O13, before anything exists
        self._meta["images"]["agent"] = f"sha256:{self.app.image.sha256}"

    def image_unchanged(self) -> None:
        """Before every exec: the image is still the file that was hashed (O13); `SandboxError` otherwise."""
        try:
            same = layout.identity(self.app.image.path) == self._image_identity
        except OSError:
            same = False
        if not same:
            raise SandboxError("the agent's image changed on the node since it was pinned")

    # ------------------------------------------------------------------------------------------------ the record
    def run_record(self) -> None:
        """Write the run's record (`runs.py`): its lease, its owner, its helpers and its token files, as they are."""
        runs.write(self.s.work_root, {
            "sid": self.sid,
            "lease": self.spec.lease_key,
            "owner": self.node.identity().record(),
            "helpers": [h.record() for h in self._helpers.values()],
            "token_files": [str(p.relative_to(self.dir)) for p in self._token_files],
        })  # fmt: skip

    def _note_helper(self, helper: helpers.Helper) -> None:
        self.register_creation()
        self._helpers[helper.role] = helper
        self.run_record()

    # ------------------------------------------------------------------------------------------------ build
    def _build(self) -> None:
        self.run_record()  # before anything exists: whatever is made from here, a reaper or a probe can find
        self.register_creation()
        self.dir.mkdir(mode=0o700)
        for name in ("work", "tmp", "shm", "cell", "egress", "egress-sock"):
            (self.dir / name).mkdir(mode=0o700)
        cell = self.dir / "cell"
        layout.copy_pinned(self.app.egress_shim, cell / "egresslog.so", "the egress shim")
        layout.copy_pinned(self.app.egress_collector, self.dir / "egress" / "egress-collector.py",
                           "the egress collector")  # fmt: skip
        for n, pin in enumerate(self.app.preload):
            layout.copy_pinned(pin, cell / f"preload-{n}.so", f"preload library {n}")
        for n, bind in enumerate(self.app.binds):
            layout.copy_pinned(bind.file, cell / f"bind-{n}", f"bound file {n}")
        if self._ca_bytes is not None:
            self._ca_copy = TLS.write_private_copy(self._ca_bytes, self.dir)
        self._input_copies = self._snapshot_inputs()
        self._started = time.time()
        self._meta.update(host=self.node.node_facts(self.app.apptainer),
                          started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))  # fmt: skip
        self._checkpoint()
        self._start_collector()
        self._checkpoint()
        share = self._start_mount() if self.spec.mode is DataPathMode.MOUNT else None
        self._checkpoint()
        if self.spec.mode is DataPathMode.REST:
            self._start_gate()
        self._checkpoint()
        self._lay_out_cell(share)
        self._start_cells()
        self._canary()
        self._verify_read_only()
        self._verify_inputs()
        self._new_refusals()  # whatever the start's own steps met is not the agent's first cell's

    def _host_python(self) -> str:
        return self.app.host_python or sys.executable

    def _start_collector(self) -> None:
        egress = self.dir / "egress"
        argv = [self._host_python(), "-I", str(egress / "egress-collector.py"), str(self.dir / "egress-sock"),
                str(egress / "egress.jsonl"), self.egress_nonce]  # fmt: skip
        helpers.start(self.node, "collector", argv, egress, COLLECTOR_READY, noted=self._note_helper,
                      env={AV.RUN_TAG: self.sid}, sleep=self._sleep, clock=self._clock)  # fmt: skip

    def _start_mount(self) -> Path:
        """Hook (c): the mount's helper, started before any cell; the node directory bound at the mount path."""
        self.register_creation()
        self._mount = self._mount_factory(self.node, self.s, self.spec, self.dir)
        # before they exist: the record lists them, so a reaper removes them first, and every teardown too (O60); each
        # must be a file of the run's directory, as the record names it relative to it and a reaper removes it there
        paths = [Path(os.path.normpath(path)) for path in self._mount.token_paths()]
        real = Path(os.path.realpath(self.dir))
        outside = [p for p in paths if p == self.dir or not p.is_relative_to(self.dir)]
        # through a link, even one that stays in the run: the reaper never follows one (`runs.remove_inside`)
        linked = [p for p in paths if p not in outside and Path(os.path.realpath(p)) != real / p.relative_to(self.dir)]
        if outside or linked:
            raise SandboxError("the mount helper named a token file outside the run's directory or through a link")
        self._token_files = [*self._token_files, *paths]
        self.run_record()
        return self._mount.start()

    def _served(self) -> dict[str, dict[str, Any]]:
        """The names the gate serves, each with the URL's host and port it dials (`gaterun.hosts`):
        `[sandbox.federation]`'s when the platform declares them (O69), else the URLs' own hosts. The cell sees these
        names and no other."""
        declared = self._declared()
        return gaterun.hosts(self.spec.zone, self.spec.provider, declared)

    def _declared(self) -> tuple[str, str] | None:
        """`[sandbox.federation]`'s zone and provider names in lower case, as the gate matches `Host` and SNI (O69);
        None when the platform declares none."""
        names = self.s.federation
        if names is None:
            return None
        if names.zone is None:
            raise SandboxError("[sandbox.federation] names no zone, which the REST data path's gate serves")
        return names.zone.lower(), names.local_provider.lower()

    def _zone_name(self) -> str:
        """The zone's name as the cell is given it (`ONEDATA_ONEZONE_HOST`): the declared one, else its URL's host."""
        declared = self._declared()
        return declared[0] if declared is not None else self.spec.zone.host

    def _start_gate(self) -> None:
        sp = self.spec
        served = self._served()
        tls_dir, gate_dir = self.dir / "tls", self.dir / "gate"
        tls_dir.mkdir(mode=0o700)
        gate_dir.mkdir(mode=0o700)
        self.node.make_ca(tls_dir, list(served))
        (tls_dir / "ca.key").unlink(missing_ok=True)  # the leaves are made: nothing signs with it again
        log_path = gate_dir / "requests.jsonl"
        log_path.touch(mode=0o600)  # there from the start: a log that is missing is unreadable, never empty
        token = self.dir / gaterun.TOKEN_FILE
        for attempt in range(1, GATE_ATTEMPTS + 1):
            self._gate_port = self.node.free_port()
            config = gate_dir / f"gate-{attempt}.json"
            gaterun.write_config(config, gaterun.config(
                run_key=sp.run_key, tls=self.s.tls, served=served, listing=sp.listing, directory=gate_dir,
                tls_dir=tls_dir, token_file=token, port=self._gate_port, upstream_ca=self._ca_copy,
                options=self.app.gate_options, scan_names=sp.node_names,
            ))  # fmt: skip
            self._token_files = [token]  # before it exists: every teardown removes it, whatever fails from here
            self.run_record()
            gaterun.write_token(token, sp.token.reveal())
            try:
                helpers.start(self.node, "gate", gaterun.argv(self._host_python(), config), gate_dir,
                              gaterun.READY, noted=self._note_helper, env={AV.RUN_TAG: self.sid}, sleep=self._sleep,
                              clock=self._clock)  # fmt: skip
            except SandboxError:
                self._remove_token_files()
                said = helpers.output(self._helpers["gate"])
                self._retire_gate()
                if attempt < GATE_ATTEMPTS and "Address already in use" in said:
                    continue
                log.error("the gate did not start: %s", describe_stderr(said))
                raise SandboxError("the gate did not start") from None
            break
        self._remove_token_files()  # the gate read it before it said ready; it keeps it in memory alone
        asked = json.loads(config.read_text(encoding="utf-8")).get(gaterun.PEER_CHECK_KEY) is True
        # in effect only when asked of a gate that has it, which refuses to start when it cannot make it (#317)
        self._meta["gate"] = {"port": self._gate_port, "hosts": len(served),
                              "names": "declared" if self.s.federation is not None else "urls",  # whose, not which
                              "peer_uid_check": asked and gaterun.PEER_CHECK.is_file()}  # fmt: skip

    def _retire_gate(self) -> None:
        """A gate that failed to start: shown gone before another is tried (a stop that cannot show it raises)."""
        gate = self._helpers["gate"]
        helpers.stop(self.node, gate, sleep=self._sleep, clock=self._clock)  # raises, and stays for the teardown
        del self._helpers["gate"]
        os.rename(gate.stdout, gate.stdout.with_suffix(f".failed-{gate.process.pid}"))
        os.rename(gate.stderr, gate.stderr.with_suffix(f".failed-{gate.process.pid}"))
        self.run_record()

    def _remove_token_files(self) -> None:
        """Remove the run's file of the token, verified (`tokenfiles.remove`): a file that stays raises, naming its
        place in the run's directory and its kind, never a content."""
        found = [item for path in self._token_files if (item := tokenfiles.remove(self.node, path, self.dir))]
        if stuck := tokenfiles.stuck(found):
            raise SandboxError(f"{len(stuck)} of the run's copies of the data-path token could not be removed")

    def token_files_left(self) -> bool:
        return any(os.path.lexists(path) for path in self._token_files)

    def _probe_argv(self, command: Sequence[str]) -> list[str]:
        """A one-shot exec of the image for the harness's own reads: `/work` alone bound, no shim, no egress."""
        return AV.exec_argv(self.app.apptainer, self.app.image.path, [AV.Bind(self.dir / "work", "/work", True)], {},
                            command)  # fmt: skip

    def _trust_bundles(self) -> dict[str, Path]:
        """Each declared trust path and the image's certifi bundle (O23), with the run's CA appended."""
        found = self.node.apptainer(self._probe_argv([self.app.python, "-c", CERTIFI_PROBE]), PROBE_S)
        if found.returncode != 0:
            raise SandboxError(
                f"the agent image did not say where its certifi bundle is ({describe_stderr(found.stderr)})"
            )
        try:
            certifi = certifi_bundle(found.stdout)
        except ValueError:
            raise SandboxError("the agent image's answer about its certifi bundle is not one path") from None
        self._meta["certifi"] = "not_in_image" if certifi is None else "run_ca_added"
        ca = (self.dir / "tls" / "ca.pem").read_text(encoding="utf-8")
        out = {}
        for n, path in enumerate(trust_targets(self.s.trust_paths, certifi)):
            base = self.node.apptainer(self._probe_argv(["cat", path]), PROBE_S)
            if base.returncode != 0:
                raise SandboxError(f"trust path {n} could not be read in the agent image")
            target = self.dir / "cell" / f"trust-{n}.pem"
            layout.write_text(target, base.stdout + "\n" + ca)
            out[path] = target
        return out

    def _lay_out_cell(self, share: Path | None) -> None:
        """The cell's files, its binds and its environment (`argv.py`)."""
        cell, sp = self.dir / "cell", self.spec
        rest = sp.mode is DataPathMode.REST
        served = self._served() if rest else {}
        names = list(served)
        preloads = [f"{AV.PRELOAD_DIR}/{n}.so" for n in range(len(self.app.preload))]
        layout.write_text(cell / "hosts", AV.hosts_text(names))
        layout.write_text(cell / "resolv.conf", AV.RESOLV_TEXT)
        layout.write_text(cell / "nsswitch.conf", AV.NSSWITCH_TEXT)
        layout.write_text(cell / "ld.so.preload", AV.preload_text([*preloads, AV.SHIM_TARGET]))
        binds = [
            AV.Bind(self.dir / "work", "/work", True), AV.Bind(self.dir / "tmp", "/tmp", True),
            AV.Bind(self.dir / "shm", "/dev/shm", True), AV.Bind(cell / "hosts", "/etc/hosts"),
            AV.Bind(cell / "resolv.conf", AV.RESOLV_CONF), AV.Bind(cell / "nsswitch.conf", AV.NSSWITCH_CONF),
            AV.Bind(self.dir / "egress-sock", AV.EGRESS_DIR), AV.Bind(cell / "egresslog.so", AV.SHIM_TARGET),
            AV.Bind(cell / "ld.so.preload", "/etc/ld.so.preload"),
            *(AV.Bind(cell / f"preload-{n}.so", target) for n, target in enumerate(preloads)),
            *(AV.Bind(cell / f"bind-{n}", b.target) for n, b in enumerate(self.app.binds)),
        ]  # fmt: skip
        env = {AV.RUN_TAG: self.sid}
        if rest:
            layout.write_text(cell / "ca.pem", (self.dir / "tls" / "ca.pem").read_text(encoding="utf-8"))
            layout.write_text(cell / "gate.json", AV.gate_info(self._gate_port, names))
            binds += [AV.Bind(cell / "ca.pem", CA_PATH), AV.Bind(cell / "gate.json", AV.GATE_INFO)]
            binds += [AV.Bind(bundle, path) for path, bundle in self._trust_bundles().items()]
            env |= dict.fromkeys(ENV_CA_BUNDLES, CA_PATH) | {ENV_ZONE_HOST: self._zone_name(), ENV_VERIFY_SSL: "true"}
            env |= {ENV_TOKEN: self.s.placeholder_token, AV.GATE_PORT_ENV: str(self._gate_port)}
        if share is not None:
            binds.append(AV.Bind(share, self.s.mount.path))
        binds += [AV.Bind(copy, target) for copy, target in self._input_copies]
        if not all(AV.bindable(b.source) for b in binds):
            raise SandboxError("a path the cell binds holds ':', ',' or white space")
        self._binds, self._env = binds, env

    def cell_argv(self, command: Sequence[str]) -> list[str]:
        return AV.exec_argv(self.app.apptainer, self.app.image.path, self._binds, self._env, command)

    def _start_cells(self) -> None:
        limits, own = self.s.limits, self.node.own_pidns()
        if own is None:  # the driver's and the helpers' namespace could not be told from a cell's
            raise SandboxError("the driver's own PID namespace could not be read")
        self._watch = Watch(
            self.node, run_tag=self.run_tag, own_pidns=own, memory_bytes=limits.memory_bytes,
            spaces=spaces_of(self.dir, {"work": limits.work_disk_bytes, "tmp": limits.tmp_bytes,
                                        "shm": limits.shm_bytes}),
            memory_poll_s=self.app.memory_poll_s, disk_poll_s=self.app.disk_poll_s,
            hidden_margin_bytes=self.app.hidden_memory_margin_bytes,
        )  # fmt: skip
        self._watch.start()
        escalation = DL.Escalation(self.s.kill_grace_s, _no_mount, sleep=self._sleep, clock=self._clock)
        self._cells = AppCellRunner(
            self.node, self._watch, self.cell_argv, python=self.app.python, settings=self.s, meta=self._meta,
            registry=self.registry, started_epoch=self._started, escalation=escalation, stop=self._stop_at_deadline,
            meta_lock=self._meta_lock, output_cap_bytes=self._output_cap, before_exec=self.image_unchanged,
            sleep=self._sleep, clock=self._clock,
        )  # fmt: skip

    def _canary(self) -> None:
        """The egress canary (`expcore.egress`): a connect from inside a cell to a listener of the harness's, the
        first record of the collector's log, so a shim that never loaded leaves a log that reads as unknown. The cell
        holds on long enough for the watch to find its namespace: a cell whose processes the harness cannot see could
        not be bounded, and the start fails."""
        with loopback.listener() as port:
            self._canary_port = port
            hold = max(1.0, 5 * self.app.memory_poll_s)
            done = self._cells.run(CANARY.format(port=self._canary_port, hold=hold), PROBE_S, record=False)
        said = done.stdout.strip()
        seen = self._cells.last_watch.get("seen") is True
        self._meta["egress"] = {"nonce": self.egress_nonce, "declaration": self.egress_declaration().to_json(),
                                "canary": said == "canary open", "supervised": seen}  # fmt: skip
        if done.exit_code != 0 or said != "canary open":
            log.error("the egress canary said %r (%s)", said, describe_stderr(done.stderr))
            raise SandboxError("the egress canary could not connect from inside a cell")
        if not seen:
            raise SandboxError(
                "the harness cannot see a cell's processes: its time and memory bounds could not be kept"
            )

    def _verify_read_only(self) -> None:
        r = self._cells.run(READ_ONLY_PROBE.format(path="/ro-probe"), PROBE_S, record=False)
        if r.exit_code != 0 or r.stdout.strip() != f"blocked {errno.EROFS}":
            log.error("read-only probe of the root said %r (%s)", r.stdout.strip(), describe_stderr(r.stderr))
            raise SandboxError("the root is not read-only for the agent")
        self._meta["read_only_verified"] = {"root": True}

    def _snapshot_inputs(self) -> list[tuple[Path, str]]:
        """Copy each read-only input into the run's directory, hash it, and take every write bit away (O20)."""
        if not self._inputs:
            return []
        base = self.dir / "inputs"
        base.mkdir(mode=0o700)
        copies, recorded = [], []
        for n, item in enumerate(self._inputs):
            copy = base / str(n)
            try:
                found = inputs.snapshot(item.source, copy)
            except (OSError, inputs.InputUnsafe) as error:
                log.error("read-only input %d could not be copied: %s", n, describe_error(error))
                raise SandboxError(f"read-only input {n} could not be copied into the run's directory") from None
            if item.sha256 is not None and found["sha256"] != item.sha256:
                raise SandboxError(f"read-only input {n} is no longer the content it was declared with")
            for root, dirs, files in os.walk(copy):
                for name in (*dirs, *files):
                    path = os.path.join(root, name)
                    if not os.path.islink(path):
                        os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~0o222)
            copy.chmod(stat.S_IMODE(copy.stat().st_mode) & ~0o222)
            recorded.append({"path": item.target, **found})
            copies.append((copy, item.target))
        self._meta["read_only_inputs"] = recorded
        return copies

    @property
    def read_only_inputs(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._meta.get("read_only_inputs", [])]

    def _verify_inputs(self) -> None:
        recorded = self._meta.get("read_only_inputs", [])
        for n, ((copy, target), item, found) in enumerate(zip(self._input_copies, self._inputs, recorded, strict=True)):
            name, runnable = inputs.probe_targets(copy)
            r = self._cells.run(inputs.probe_source(target, name, runnable), PROBE_S, record=False)
            why = inputs.judge(r.stdout, runnable is not None)
            if why is not None:
                raise SandboxError(f"read-only input {n} is not read-only for the agent: {why}")
            for what, root in (("copy", copy), ("source", item.source)):
                try:
                    same = inputs.digest(root)["sha256"] == found["sha256"]
                except (OSError, inputs.InputUnsafe):
                    same = False
                if not same:
                    raise SandboxError(f"read-only input {n} changed between its hash and its mount ({what})")
        if self._inputs:
            self._meta["read_only_verified"]["inputs"] = True

    # ------------------------------------------------------------------------------------------------ the API
    def exec(self, code: str, timeout_s: float, *, record: bool = True) -> ExecResult:
        """Run Python source in a cell (`cells.AppCellRunner.run`), with the gate's refusals that came while it ran.
        `record=False` is a step of the harness's own, refused once the run's start is marked or a cell began (O24).
        An exec not known to have completed that no timeout or kill explains raises `ExecIncomplete` (O55)."""
        self._refuse_if_ended()
        if self._cells is None or self._stopped:
            raise SandboxError("the sandbox is not running")
        self._enter(record)
        try:
            try:
                res = self._cells.run(code, timeout_s, record=record)
            except ExecIncomplete as incomplete:
                incomplete.result.gate_refusals = self._new_refusals()
                if not record:
                    self._record_step(incomplete.result, incomplete=incomplete.cause)
                raise
            res.gate_refusals = self._new_refusals()
            if not record:
                self._record_step(res)
        finally:
            if not record:
                with self._phase:
                    self._steps_running -= 1
        return res

    def _enter(self, record: bool) -> None:
        with self._phase:
            if record:
                self._agent_began = True
                return
            if "run_start" in self._meta or self._agent_began:
                raise SandboxError(
                    "a harness step runs only before the agent's run: its start is marked or a cell began"
                )
            self._steps_running += 1

    def _record_step(self, res: ExecResult, *, incomplete: str | None = None) -> None:
        steps = self._meta.setdefault("harness_steps", [])
        steps.append({
            "step": len(steps) + 1, "status": res.status, "exit_code": res.exit_code, "duration_s": res.duration_s,
            "timed_out": res.timed_out, "bound": res.bound, "gate_refusals": list(res.gate_refusals),
            **({"output_cut": res.output_cut} if res.output_cut else {}), "exec_check": res.exec_check,
            **({"kill": res.kill} if res.kill else {}), **({"incomplete": incomplete} if incomplete else {}),
        })  # fmt: skip

    def settle_deadline(self) -> dict[str, Any] | None:
        return self._cells.settle() if self._cells is not None else None

    def _refuse_if_ended(self) -> None:
        if self._cells is None:
            return
        with self._meta_lock:
            record = self._cells.ended
        if record is not None:
            raise kinds.run_ended_error(record)

    def _stop_at_deadline(self) -> dict[str, Any]:
        try:
            self.stop()
        except SandboxError as error:
            log.error("the sandbox could not be stopped at a cell's deadline: %s", describe_error(error))
            return {"ok": False, "error": "the sandbox's teardown was incomplete"}
        return {"ok": True}

    def mark_run_start(self) -> dict[str, Any]:
        """Mark where the agent's run begins (O24): a REST gate is signalled (SIGUSR1) and the mark read back from its
        log; without a gate nothing is signalled. Refused once a cell began, while a harness step runs, after the
        collection stopped the gate, and a second time."""
        with self._phase:
            if self._cells is None or self._stopped:
                raise SandboxError("the sandbox is not running")
            if "run_start" in self._meta:
                raise SandboxError("the run's start is already marked")
            if self._agent_began:
                raise SandboxError("the run's start must be marked before the agent's first cell begins")
            if self._steps_running:
                raise SandboxError("the run's start cannot be marked while a harness step runs")
            if "gate_stop" in self._meta:
                raise SandboxError("the run's start cannot be marked after the collection stopped the gate")
            if self.spec.mode is not DataPathMode.REST:
                self._meta["run_start"] = {"gate": False}
                return dict(self._meta["run_start"])
            note = self._meta["run_start"] = {"gate": True, "signalled": False, "confirmed": False}
            gate = self._helpers["gate"].process
            note["signalled"] = self.node.signal(gate.pid, gate.start, signal.SIGUSR1) is True
            for _ in range(RUN_START_POLLS if note["signalled"] else 0):
                lines = self.gate_log_lines()
                if lines is not None and gatelog.run_start_marks(lines):
                    note["confirmed"] = True
                    break
                self._sleep(0.1)
            return dict(note)

    def served_bytes(self) -> dict[str, Any]:
        """The bytes the agent read through its data path (O22): as the Docker sandbox counts them."""
        path = {"data_path": self.spec.mode.value, "listing": self._meta.get("listing"),
                "run_start_marked": "run_start" in self._meta}  # fmt: skip
        caps = self.s.served_caps
        if self.spec.mode is DataPathMode.MOUNT:
            return path | gatelog.served_unknown("mount_not_through_gate", caps=caps)
        if self.spec.mode is DataPathMode.NONE:
            return path | gatelog.served_nothing(caps)
        return path | self._meta.get("served_bytes", gatelog.served_unknown("gate_not_collected", caps=caps))

    def gate_refusals(self) -> list[str]:
        lines = self.gate_log_lines()
        return [gatelog.UNREADABLE] if lines is None else gatelog.refusals(lines)

    def _new_refusals(self) -> tuple[str, ...]:
        lines = self.gate_log_lines()
        if lines is None:
            return (gatelog.UNREADABLE,)
        seen = gatelog.refusals(lines)
        new, self._refusals_seen = tuple(seen[self._refusals_seen :]), len(seen)
        return new

    def gate_log_lines(self) -> list[str] | None:
        """The gate log's lines; empty without a gate, None when it cannot be read (never following a link, O19)."""
        if self.spec.mode is not DataPathMode.REST:
            return []
        try:
            descriptor = os.open(self.dir / "gate" / "requests.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise OSError(errno.EINVAL, "the gate log is not a regular file")
                return handle.read().decode("utf-8").splitlines()
        except (OSError, ValueError) as error:
            log.error("the gate log could not be read: %s", describe_error(error))
            return None

    def egress_declaration(self) -> Declaration:
        """The run's egress declaration (`expcore.egress`): the gate's endpoint, the canary's, the gate's names, and
        whether the data path is the agent's own HTTP to the gate."""
        rest = self.spec.mode is DataPathMode.REST
        gate = [(AV.LOOPBACK, self._gate_port)] if rest and self._gate_port else []
        names = list(self._served()) if rest else []  # the names the agent's code was given, never the URLs' (O69)
        return declaration(gate, (AV.LOOPBACK, self._canary_port or 1), hosts=names, network_calls=rest)

    def egress_log(self) -> bytes | None:
        """The collector's log as the collection read it once the collector had stopped; None before, or when it
        could not be read (`expcore.egress.EgressSource`)."""
        return self.collection.egress_bytes

    def process_visibility(self) -> dict[str, Any] | None:
        """What the watch saw of the processes `/proc` hides (`expcore.visibility.ProcessVisibility`'s source): read
        once the run's cells have ended; None before the watch exists."""
        return None if self._watch is None else self._watch.visibility()

    def mount_watch_log(self) -> bytes | None:
        """The mount guard's log (`expcore.mountguard.MountWatchSource`), its window closed first (O60); None without a
        mount helper that keeps one. Read after the run's cells have ended: the collection does, and so may the
        instrument."""
        read = getattr(self._mount, "mount_watch_log", None)
        return None if read is None else read()

    @property
    def mount_guard_nonce(self) -> str | None:
        """The nonce of the mount guard's watcher (`expcore.mountguard.MountGuard`), once the helper exists."""
        return getattr(self._mount, "nonce", None)

    def mount_io_mode(self) -> dict[str, Any]:
        if self.spec.mode is not DataPathMode.MOUNT or self._mount is None:
            return {"mount": False}
        return {"mount": True, **self._mount.metadata().get("io_mode", {})}

    def record(self, **fields: Any) -> None:
        with self._meta_lock:
            self._meta.update(fields)

    def note_end(self, kind: str) -> None:
        with self._meta_lock:
            self._ends.setdefault(kind, time.monotonic())

    def session_kinds(self) -> tuple[str, ...]:
        return {DataPathMode.MOUNT: (MOUNT,), DataPathMode.REST: (GATE,)}.get(self.spec.mode, ())

    def session_ends(self) -> dict[str, float]:
        with self._meta_lock:
            return dict(self._ends)

    @property
    def stopped(self) -> bool:
        return self._stopped

    def collect(self, dest_dir: str | Path, outputs: Sequence[str] = ()) -> dict[str, Any]:
        """Stop what of the agent's still runs, then copy out the run's evidence and the named files of /work
        (`collect.Collection`)."""
        self.settle_deadline()
        self._refuse_if_ended()
        try:
            return self.collection.run(Path(dest_dir), outputs)
        except BaseException:
            self.keep_evidence = True
            raise

    def metadata(self) -> dict[str, Any]:
        """The facts of the run for its record. Never a secret: the token is in none of them."""
        with self._meta_lock:
            meta = json.loads(json.dumps(self._meta, default=str))
        if self._cells is not None:
            meta["cells"] = len(self._cells.cells)
        if self._watch is not None:
            meta["watch"] = self._watch.summary()
            meta["infrastructure_facts"]["memory"] = meta["watch"]["memory"]  # never `within` unless the check held
        if self.spec.mode is DataPathMode.REST:
            lines = self.gate_log_lines()
            meta["gate_log"] = gatelog.unknown("the gate log could not be read") if lines is None else (
                gatelog.summarize(lines)
            )  # fmt: skip
        if self._mount is not None:
            meta["mount"] = self._mount.metadata()
        meta["served_bytes"] = self.served_bytes()
        counted = meta.pop("rest_node_name_hits", None)
        if self.spec.node_names and self.spec.mode is DataPathMode.REST:  # O73: scanned, so never silently absent
            meta["infrastructure_facts"]["rest_node_name_hits"] = counted or gatelog.node_name_hits_unknown(
                gatelog.GATE_NOT_COLLECTED
            )
        return meta

    # ------------------------------------------------------------------------------------------------ stop
    def stop(self, *, force: bool = False) -> None:
        """Tear down in order and verify (`teardown.Teardown`); raise `SandboxError` when anything is not shown gone.
        Idempotent; a SIGTERM cannot cut it short; a cell's deadline under way is waited for first (O34)."""
        if self._cells is not None:
            self._cells.wait_decided()
        with TERM.sigterm_deferred(), self._stop_lock:
            self.teardown.run(force)

    def teardown_left(self) -> dict[str, Any]:
        return self.teardown.left()

    def __enter__(self) -> ApptainerSandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _no_mount(timeout_s: float) -> dict[str, Any]:
    """The deadline's abort: there is no FUSE mount of the harness's to abort, so a survivor orders the stop."""
    return {"ok": None, "why": "the run has no FUSE mount of the sandbox's own"}


__all__ = ["ApptainerSandbox"]
