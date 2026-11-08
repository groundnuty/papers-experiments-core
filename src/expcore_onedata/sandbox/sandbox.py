# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/sandbox.py, blob 6e2fc30132ac38e62270de4bf334f100e1d85d40
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see the history of this file from its import commit
"""One sandboxed run: container lifecycle, exec, collect, metadata, over an injectable `HostOps`.

Per run, named `<prefix>-<id>` where the id starts with the lease key when there is one:
  * a loop-mounted disk of the configured size, mounted at /work (the home and working directory of the unprivileged
    user; ENOSPC beyond the quota);
  * an INTERNAL docker network from the dedicated pool (no route out) holding the agent container, and in REST mode the
    gate container, the agent's only network peer (O14);
  * REST data path: the data gate (gate.py, gatepolicy.py), whose files are mounted from this package, read-only;
  * mount data path: a oneclient sidecar that owns the mount and the token, exported read-only to the agent through bind
    propagation; the agent has neither the token, nor FUSE, nor a route to any host;
  * no data path: neither;
  * the agent container: read-only root, all capabilities dropped, no-new-privileges, fixed cpus, memory and pids.

The data path is read-only in every condition (O05). The token is examined by `tokens.py` before it gets here; the gate
passes only reads; the mount and the root filesystem are read-only, and `start` proves the last two by trying to write
and requiring the refusal, so a sandbox whose root or mount is writable is never handed out. Images are started by the
resolved id they were pinned to, and a moved tag refuses the start before anything is created (O13). Teardown runs on
every path that ends a sandbox, in order, and is verified (O15).
"""

from __future__ import annotations

import errno
import ipaddress
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox import gatelog, hostops, netpool
from expcore_onedata.sandbox import infradeath as ID
from expcore_onedata.sandbox import teardown as TD
from expcore_onedata.sandbox import termination as TERM
from expcore_onedata.sandbox.api import DataPathMode, ExecResult, SandboxError, SandboxSpec, StartRefused
from expcore_onedata.sandbox.cells import CellRunner
from expcore_onedata.sandbox.containers import (
    ENV_ACCESS_TOKEN,
    ENV_TOKEN,
    ONECLIENT_LOG_DIR,
    Names,
    agent_run_args,
    effective_log_dir,
    gate_run_args,
    sidecar_command,
    sidecar_run_args,
)
from expcore_onedata.sandbox.hostops import HostOps, describe_error, describe_stderr, stderr_digest
from expcore_onedata.sandbox.observe import Observer
from expcore_onedata.sandbox.oneclient_log import summarize as summarize_oneclient_log
from expcore_onedata.sandbox.registry import DEFAULT, Registry
from expcore_onedata.sandbox.settings import SandboxSettings

log = logging.getLogger(__name__)

RUN_KEY = re.compile(r"[\x21-\x7e]{1,200}")  # it becomes an HTTP header value at the gate
GATE_FILES = Path(__file__).resolve().parent  # gate.py and gatepolicy.py are mounted from here into the gate container
GATE_STOP_WAIT_S = 3.0  # how long `collect` waits for a gate that was sent SIGTERM; the gate's own budget is 1 s
MOUNT_WAIT_S = 60.0
READ_ONLY_PROBE = (
    "try:\n"
    "    open({path!r}, 'w').close()\n"
    "except OSError as e:\n"
    "    print('blocked', e.errno)\n"
    "else:\n"
    "    print('WRITABLE')\n"
)


class DockerSandbox:
    """The `Sandbox` of the platform layer, over docker. Build it with `start`; stop it with `stop` (or `with`)."""

    def __init__(
        self,
        host: HostOps,
        settings: SandboxSettings,
        spec: SandboxSpec,
        *,
        registry: Registry = DEFAULT,
        waits: TD.Waits | None = None,
        sample_period_s: float | None = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        config_sha256: str | None = None,
    ) -> None:
        self.host, self.s, self.spec, self.registry = host, settings, spec, registry
        self.waits, self._sample_period, self._sleep = waits or TD.WAITS, sample_period_s, sleep
        key = spec.lease_key or re.sub(r"[^A-Za-z0-9_.-]", "_", spec.run_key)[:48]
        self.sid = f"{key}-{uuid.uuid4().hex[:8]}"
        self.names = Names.make(settings.name_prefix, self.sid)
        self.dir = settings.work_root / self.sid
        self.labels = netpool.Labels(settings.label_namespace)
        self.obs = Observer(host, settings.agent_uid)
        self._mounts: list[Path] = []
        self._containers: list[str] = []
        self._net: str | None = None
        self._watched: dict[str, dict[str, Any]] = {}
        self._meta: dict[str, Any] = {"run_key": spec.run_key, "data_path": spec.mode.value, "images": {}}
        if config_sha256:  # the configuration is named by its hash in a record, never by a value in it (O12)
            self._meta["platform_config_sha256"] = config_sha256
        self._stopped, self._stop_lock = True, threading.Lock()
        self._sampler_stop = threading.Event()
        self._cells: CellRunner | None = None
        self._log_dir = ONECLIENT_LOG_DIR
        self.keep_evidence = False
        self._uptime0: float | None = None
        self.teardown_log: list[dict] = []

    # ------------------------------------------------------------------------------------------------ start
    @classmethod
    def start(cls, host: HostOps, settings: SandboxSettings, spec: SandboxSpec, **options: Any) -> DockerSandbox:
        """Check the spec and pin the images, then build the sandbox; a failure after anything exists stops it first."""
        TERM.raise_if_requested()  # a SIGTERM reached this process: nothing new starts
        self = cls(host, settings, spec, **options)
        self._validate()
        self._pin_images()  # a moved tag refuses the start (O13), before anything exists
        self._stopped = False  # from here, `stop` has something to verify
        self._meta["pending_cleanups_left"] = self.registry.drain()
        try:
            self._meta["reaped_orphans"] = netpool.reap_orphans(host, self.labels)  # never fails the start
        except Exception as e:
            self._meta["reaped_orphans"] = {"error": describe_error(e)}
        try:
            self._build()
            TERM.register(self)  # before the check: a SIGTERM after it finds this sandbox in the list
            TERM.raise_if_requested()  # one that came during the start: stopped below, never handed out
        except BaseException as exc:
            self._cleanup_after(exc)
            raise
        return self

    def _validate(self) -> None:
        sp, s = self.spec, self.s
        if not RUN_KEY.fullmatch(sp.run_key):
            raise StartRefused("the run key must be 1 to 200 printable ASCII characters without spaces")
        if (sp.token is None) != (sp.mode is DataPathMode.NONE):
            raise StartRefused("a data path takes the data-path token, and no data path takes none")
        if sp.mode is not DataPathMode.NONE and sp.provider is None:
            raise StartRefused("a data path needs the agent's local provider")
        if sp.mode is DataPathMode.REST and sp.zone is None:
            raise StartRefused("a REST data path needs the zone")
        if sp.mode is DataPathMode.MOUNT and (s.mount is None or s.oneclient is None):
            raise StartRefused("a mount data path needs [sandbox.mount] and the oneclient image")

    def _images(self) -> dict[str, Any]:
        s, mode = self.s, self.spec.mode
        images = {"agent": s.agent}
        if mode is DataPathMode.REST:
            images["gate"] = s.gate
        if mode is DataPathMode.MOUNT:
            images["oneclient"] = s.oneclient
        return images

    def _pin_images(self) -> None:
        """Every image the sandbox starts must resolve to the id it is pinned to; never run another image (O13)."""
        for role, ref in self._images().items():
            resolved = self.host.image_id(ref.name)
            if resolved != ref.id:
                raise StartRefused(f"the {role} image no longer resolves to the id it is pinned to")
            self._meta["images"][role] = resolved

    def _cleanup_after(self, exc: BaseException) -> None:
        """Stop what a failed start left. A cleanup that fails too is registered for the next start, and reachable on the
        exception, so whoever holds a lease on it can try again."""
        try:
            self.stop()
        except Exception as e:
            self.registry.add_pending(self)
            exc.sandbox = self  # type: ignore[attr-defined]
            log.error("cleanup after a failed start was incomplete: %s", describe_error(e))

    # ------------------------------------------------------------------------------------------------ build
    def _build(self) -> None:
        s, _sp, mode = self.s, self.spec, self.spec.mode
        self.dir.mkdir(parents=True, exist_ok=False)
        self._started = time.time()
        self._meta.update(host=self.host.host_facts(), started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        work = self.dir / "work"
        self._mounts.append(work)  # registered before the mount (unmounting what never got mounted is harmless)
        self.host.make_quota_dir(self.dir / "work.img", work, s.limits.work_disk_bytes, s.agent_uid)
        hosts = self._gate_hosts() if mode is DataPathMode.REST else {}
        if mode is DataPathMode.REST:
            self._prepare_gate(hosts)
        subnet, tries = self._create_network()
        self._meta["network"] = {  # the position of the pool, never an address or the pool itself (O12)
            "pool": self._pool_position(subnet),
            "prefix": s.network.prefix,
            "attempts": tries,
        }
        gate_ip = self._start_gate() if mode is DataPathMode.REST else None
        share = self._start_mount() if mode is DataPathMode.MOUNT else None
        self._start_agent(work, share, gate_ip, list(hosts))
        roles = {"agent": self.names.agent} | ({"gate": self.names.gate} if gate_ip else {})
        if share:
            roles["sidecar"] = self.names.sidecar
        self._cells = CellRunner(self.host, self.obs, self.names, s, self._meta, self.registry, roles, self._started)
        self._verify_read_only()
        self._cells.sample_pids()
        self._start_sampler()

    def _gate_hosts(self) -> dict[str, dict[str, Any]]:
        """The zone and the agent's pinned local provider: the gate serves these two and nothing else (O03, O05)."""
        zone, provider = self.spec.zone, self.spec.provider
        return {
            ep.host: {"role": role, "addr": ep.host, "port": ep.port, "tls": ep.tls}
            for role, ep in (("onezone", zone), ("oneprovider", provider))
            if ep is not None
        }

    def _prepare_gate(self, hosts: dict[str, dict[str, Any]]) -> None:
        self.host.make_ca(self.dir / "tls", list(hosts))
        (self.dir / "gatelog").mkdir()
        (self.dir / "gatelog").chmod(0o777)  # the gate runs as root without DAC_OVERRIDE
        config = {
            "run_key": self.spec.run_key,
            "verify_ssl": True,
            "log": "/var/gate/requests.jsonl",
            "tls_dir": "/etc/gate/tls",
            "hosts": hosts,
        }
        (self.dir / "gate.json").write_text(json.dumps(config), encoding="utf-8")

    def _pool_position(self, subnet: str) -> int | None:
        """Which pool of `[sandbox.network] pools`, by position in the file, a subnet was taken from."""
        net = ipaddress.ip_network(subnet)
        pools = hostops.strict_networks(self.s.network.pools)
        return next((i for i, pool in enumerate(pools) if net.subnet_of(pool)), None)

    def _label_args(self) -> list[str]:
        return self.labels.args(self.host, self.dir, self.spec.lease_key)

    def _create_network(self) -> tuple[str, int]:
        net = self.s.network
        self._net = self.names.net
        return netpool.create(
            self.host,
            self._net,
            pools=net.pools,
            prefix=net.prefix,
            attempts=net.max_attempts,
            extra=self._label_args(),
        )

    def _start_gate(self) -> str:
        names, s = self.names, self.s
        self._containers.append(
            names.gate
        )  # registered before it is created: an interrupt cannot leave one `stop` misses
        self.host.docker(*gate_run_args(s, names, self._label_args(), self.dir, GATE_FILES, s.gate.id))
        self.host.docker("network", "connect", names.net, names.gate)
        self._wait_gate()
        self.watch_container("gate", names.gate)  # its state as the run begins: the baseline the check compares with
        info = hostops.inspect(self.host, names.gate) or {}
        ip = ((info.get("NetworkSettings") or {}).get("Networks") or {}).get(names.net, {}).get("IPAddress", "")
        if not ip:
            raise SandboxError("the gate has no address on the sandbox network")
        return ip

    def _wait_gate(self) -> None:
        for _ in range(100):
            logs = self.host.docker("logs", self.names.gate, check=False)
            if "gate ready" in logs.stdout:
                return
            if (hostops.inspect(self.host, self.names.gate) or {}).get("State", {}).get("Running") is not True:
                log.error("the gate exited: %s", describe_stderr(logs.stderr))
                raise SandboxError("the gate exited before it was ready")
            self._sleep(0.1)
        raise SandboxError("the gate did not become ready")

    def _start_mount(self) -> Path:
        """Start the oneclient sidecar on a shared mount point; the token goes in its environment, never an argument."""
        s, sp, names = self.s, self.spec, self.names
        share = self.dir / "mount"
        share.mkdir()
        self._mounts.append(share)
        self.host.run(["mount", "--bind", str(share), str(share)], 60, sudo=True)
        self.host.run(["mount", "--make-rshared", str(share)], 60, sudo=True)  # so the sidecar's mount propagates out
        host_name = sp.provider.host
        command = sidecar_command(host_name, s)
        self._log_dir = effective_log_dir(command)
        self._meta["mount"] = {  # the keys that set it, never a path (O12)
            "tls": s.mount.tls,
            "io_mode": s.mount.io_mode,
            "log_dir_from": "sandbox.mount.extra_args" if self._log_dir != ONECLIENT_LOG_DIR else "default",
        }
        self._containers.append(names.sidecar)
        t0 = time.monotonic()
        args = sidecar_run_args(s, names, self._label_args(), share, s.oneclient.id, host_name)
        self.host.docker(*args, env={ENV_ACCESS_TOKEN: sp.token.reveal()})
        deadline = time.monotonic() + MOUNT_WAIT_S
        while not hostops.is_fuse_mounted(self.host.read_text("/proc/self/mountinfo"), str(share)):
            if (hostops.inspect(self.host, names.sidecar) or {}).get("State", {}).get("Running") is not True:
                logs = self.host.docker("logs", names.sidecar, check=False)
                raise SandboxError(f"the oneclient sidecar exited ({describe_stderr(logs.stderr)})")
            if time.monotonic() > deadline:
                raise SandboxError("the oneclient mount did not appear")
            self._sleep(0.2)
        self._meta["mount"]["wait_s"] = round(time.monotonic() - t0, 3)
        self.watch_container("sidecar", names.sidecar)
        return share

    def _trust_bundles(self) -> dict[str, Path]:
        """For each configured path of a CA bundle in the agent image: the image's own bundle plus the run's CA."""
        out: dict[str, Path] = {}
        ca = (self.dir / "tls" / "ca.pem").read_text(encoding="utf-8")
        for n, path in enumerate(self.s.trust_paths):
            name = f"{self.names.agent}-once-{uuid.uuid4().hex[:6]}"
            self._containers.append(name)  # registered before it is created
            base = self.host.docker("run", "--rm", "--name", name, "--entrypoint", "cat", self.s.agent.id, path).stdout
            bundle = self.dir / "tls" / f"trust-{n}.pem"
            bundle.write_text(base + "\n" + ca, encoding="utf-8")
            out[path] = bundle
        return out

    def _start_agent(self, work: Path, share: Path | None, gate_ip: str | None, gate_hosts: list[str]) -> None:
        s, sp = self.s, self.spec
        rest = sp.mode is DataPathMode.REST
        args, env = agent_run_args(
            s, self.names, self._label_args(), image_id=s.agent.id, mode=sp.mode, work=work, share=share,
            gate_ip=gate_ip, gate_hosts=gate_hosts, ca=self.dir / "tls" / "ca.pem" if rest else None,
            trust_bundles=self._trust_bundles() if rest else {}, zone_host=sp.zone.host if rest and sp.zone else None,
            with_token=rest,
        )  # fmt: skip
        if rest:
            env[ENV_TOKEN] = sp.token.reveal()
        self._containers.append(self.names.agent)
        self.host.docker(*args, env=env)

    def _verify_read_only(self) -> None:
        """Prove the root and the mount refuse a write, from inside the agent, and refuse to hand out a sandbox that does not."""
        paths = {"root": "/ro-probe"}
        if self.spec.mode is DataPathMode.MOUNT:
            paths["mount"] = f"{self.s.mount.path}/ro-probe"
        proved: dict[str, bool] = {}
        for what, path in paths.items():
            r = self._cells.run(READ_ONLY_PROBE.format(path=path), 60, record=False)
            said = r.stdout.strip()
            if r.exit_code != 0 or said != f"blocked {errno.EROFS}":
                log.error("read-only probe of the %s said %r (%s)", what, said, describe_stderr(r.stderr))
                raise SandboxError(f"the {what} is not read-only for the agent")
            proved[what] = True
        self._meta["read_only_verified"] = proved

    # ------------------------------------------------------------------------------------------------ watching
    def watch_container(self, role: str, container: str) -> None:
        """Register a container of this run that is not the agent's and record its state now as the baseline. It must be
        running: one that is not, or whose state cannot be read, is a failed start."""
        st, why = self.obs.state(container)
        if st is None:
            raise SandboxError(f"the {role} could not be inspected when it came up ({why})")
        if st["status"] != "running":
            raise SandboxError(f"the {role} is not running when the run begins (status {st['status']})")
        if not self._watched:
            self._uptime0 = self.obs.uptime()  # the kernel log is read from here when a death needs its limit named
        self._watched[role] = {"container": container, "start": st}
        if container not in self._containers:
            self._containers.append(container)

    def infra_container_check(self) -> dict[str, Any]:
        """The state of every registered container now against its baseline, and the cause of each death. Read from the
        host while the containers are up, never from the agent's output. A container with no problem ran throughout."""
        found, containers = [], {}
        for role, w in self._watched.items():
            end, why = self.obs.state(w["container"])
            probs = ID.problems(role, self.spec.mode.value, w["start"], end, why)
            containers[role] = {
                "start": w["start"],
                "end": end,
                "problems": probs,
            }  # by role: a name holds the lease key
            if probs:
                found.append((role, probs, w["start"], end, why))
        deaths: list[dict] = []
        if found:
            kernel = self.obs.kernel_oom(self._uptime0)
            hit = {k.get("role") for k in self._meta.get("earlyoom_kills") or [] if isinstance(k, dict)}
            for role, probs, start, end, why in found:
                cause = ID.cause_of(role, start, end, why, kernel=kernel, earlyoom=role in hit)
                deaths.append(
                    {
                        "role": role,
                        "condition": self.spec.mode.value,
                        **cause,
                        "problems": probs,
                    }
                )
        infra = [d for d in deaths if d["category"] == "infrastructure"]
        return {"containers": containers, "deaths": deaths, "death": [p for d in infra for p in d["problems"]]}

    def _start_sampler(self) -> None:
        if not self._sample_period:
            return

        def loop() -> None:
            while not self._sampler_stop.is_set():
                try:
                    self._cells.sample_pids()
                except Exception as e:
                    log.debug("sampling failed: %s", describe_error(e))
                self._sampler_stop.wait(self._sample_period)

        threading.Thread(target=loop, daemon=True, name=f"sample-{self.names.agent}").start()

    # ------------------------------------------------------------------------------------------------ the API
    def exec(self, code: str, timeout_s: float) -> ExecResult:
        """Run Python source as the unprivileged user. See `CellRunner.run`."""
        if self._cells is None or self._stopped:
            raise SandboxError("the sandbox is not running")
        return self._cells.run(code, timeout_s)

    @property
    def stopped(self) -> bool:
        return self._stopped

    def record(self, **fields: Any) -> None:
        """Add facts to the metadata (the lease, the token records: never a value)."""
        self._meta.update(fields)

    def collect(self, dest_dir: str | Path, outputs: Sequence[str] = ()) -> dict[str, Any]:
        """Stop whatever of the agent's still runs, then copy out the run's evidence and the named files of /work.

        A copy that fails raises, and the sandbox's disks are then kept when it stops: a source is never deleted after a
        failed copy. An agent that could not be shown stopped also fails the collection: its outputs may still change."""
        try:
            state = self._cells.quiesce("before collection")
            if not CellRunner.quiet(state):
                log.error("processes left before collection: %s", state)
                raise SandboxError("the agent's processes could not be shown stopped before collection")
            return self._collect(Path(dest_dir), outputs)
        except BaseException:
            self.keep_evidence = True
            raise

    def _copy_out(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        r = self.host.run(["cp", "-r", "--no-preserve=ownership", str(src), str(dst)], 600, sudo=True)
        owner = self.host.run(["chown", "-R", f"{os.getuid()}:{os.getgid()}", str(dst)], 60, sudo=True)
        if r.returncode != 0 or owner.returncode != 0:
            log.error("copying out failed: %s", describe_stderr(r.stderr or owner.stderr))
            raise SandboxError("copying a file out of the sandbox failed")

    def _collect(self, dest: Path, outputs: Sequence[str]) -> dict[str, Any]:
        for rel in outputs:
            if rel.startswith("/") or ".." in Path(rel).parts:
                raise SandboxError("an output to collect must be a relative path inside the working directory")
        dest.mkdir(parents=True, exist_ok=True)
        got = {}
        for rel in outputs:
            src = self.dir / "work" / rel
            got[rel] = src.exists()
            if got[rel]:
                self._copy_out(src, dest / "work" / rel)
        self._meta["infra_check"] = check = self.infra_container_check()  # while everything runs
        result: dict[str, Any] = {"outputs": got, "deaths": check["deaths"]}
        if self.spec.mode is DataPathMode.REST:
            self._stop_gate()
            log_file = self.dir / "gatelog" / "requests.jsonl"
            self._copy_out(log_file, dest / "gate-requests.jsonl")
            result["gate_log"] = "gate-requests.jsonl"
        (dest / "metadata.json").write_text(json.dumps(self.metadata(), indent=1, default=str), encoding="utf-8")
        return result

    def _stop_gate(self) -> None:
        """SIGTERM the gate and wait for it to exit, so that it writes its close line for every client connection still
        open before its log is copied (`docker rm -f` is a SIGKILL and leaves no line). Never costs the collection."""
        note = self._meta["gate_stop"] = {"signalled": False}
        try:
            r = self.host.docker("kill", "-s", "TERM", self.names.gate, check=False, timeout=30)
            note["signalled"] = r.returncode == 0
            deadline = time.monotonic() + GATE_STOP_WAIT_S
            while note["signalled"] and time.monotonic() < deadline:
                state = (hostops.inspect(self.host, self.names.gate) or {}).get("State", {})
                if state.get("Running") is False:
                    note["exit_code"] = state.get("ExitCode")
                    break
                self._sleep(0.1)
            logs = self.host.docker("logs", self.names.gate, check=False, timeout=30)
            note["log_write_failed"] = "GATE LOG FAILED" in logs.stderr + logs.stdout
        except Exception as e:
            note["error"] = describe_error(e)

    def sidecar_errors(self) -> dict[str, Any] | None:
        """Warnings and errors of the oneclient sidecar by origin, or `{"unavailable": reason}`; None without a sidecar."""
        if self.spec.mode is not DataPathMode.MOUNT:
            return None
        r = self.host.docker(
            "exec", self.names.sidecar, "cat", f"{self._log_dir}/oneclient.INFO", check=False, timeout=60
        )
        if r.returncode != 0:
            return {"unavailable": "the oneclient log could not be read", "stderr": stderr_digest(r.stderr)}
        token = self.spec.token.reveal() if self.spec.token else ""
        return summarize_oneclient_log(r.stdout.replace(token, "<token>") if token else r.stdout)

    def metadata(self) -> dict[str, Any]:
        """The facts of the run for its record. Never a secret: tokens are not in it, and the sidecar's log is summarised."""
        meta = json.loads(json.dumps(self._meta, default=str))
        if not self._stopped and self._cells is not None:
            meta["cells"] = len(self._cells.cells)
            meta["memory"] = {k: v for k, v in self.obs.memory_facts(self.names.agent).items() if k != "events"}
            meta["containers"] = {r: self.obs.totals(c) for r, c in self._cells.roles.items()}
            check = self.infra_container_check()
            meta["container_deaths"], meta["infra_death"] = check["deaths"], check["death"]
        if self.spec.mode is DataPathMode.REST:
            file = self.dir / "gatelog" / "requests.jsonl"
            try:
                meta["gate"] = gatelog.summarize(file.read_text(encoding="utf-8").splitlines())
            except OSError as e:
                meta["gate"] = gatelog.unknown(f"the gate log could not be read: {e.strerror}")
        meta["sidecar_log"] = self.sidecar_errors() if not self._stopped else None
        return meta

    # ------------------------------------------------------------------------------------------------ stop
    def stop(self) -> None:
        """Tear down in order and verify; raise `SandboxError` when anything is not gone. Safe to call again (idempotent).

        One teardown at a time: the sampler's thread and the driver's `finally` may both stop; the second waits, then
        finds it stopped. A SIGTERM cannot cut it short (termination.py): that would leave what it exists to remove."""
        with TERM.sigterm_deferred(), self._stop_lock:
            self._stop_locked()

    def _step(self, errors: list[str], fn: Callable[..., Any], *a: Any) -> None:
        """Every step runs whatever an earlier one raised (a timeout, a daemon error); a failure is recorded."""
        try:
            fn(*a)
        except Exception as e:
            log.error("teardown step %s failed: %s", getattr(fn, "__name__", "step"), describe_error(e))
            errors.append(f"a teardown step failed ({type(e).__name__})")

    def _stop_locked(self) -> None:
        if self._stopped:
            return
        errors: list[str] = []
        complete = False
        self._sampler_stop.set()
        try:
            held: set[str] = set()
            if self.names.sidecar in self._containers:
                # a oneclient sidecar was started (or tried), also after a failed start: the readers stop, the FUSE
                # mount goes, then the sidecar, then the agent; no container is killed with the mount live (O15)
                log_path = self.s.work_root / TD.TEARDOWN_LOG_DIR / f"{self.sid}.jsonl"
                agent = self.names.agent if self.names.agent in self._containers else None
                res = TD.ordered_fuse_teardown(
                    self.host, agent, self.names.sidecar, self.dir / "mount", waits=self.waits, log_path=log_path
                )
                self.teardown_log = res.log
                errors.extend(res.errors)
                if not res.fuse_safe:
                    held = {self.names.agent, self.names.sidecar}
            for c in reversed(self._containers):
                if c not in held:
                    self._step(errors, hostops.remove_container, self.host, c)
            if held:  # the network and the mounts cannot go while those two live: all of it is left for a retry
                errors.append("the sidecar and the agent are left running with a FUSE mount that could not be released")
            else:
                self._remove_the_rest(errors)
            complete = not errors
        finally:
            self._stopped = (
                complete  # stopped only when everything is verifiably gone; an interrupt leaves it retryable
            )
            if complete:
                TERM.unregister(self)
        if errors:
            raise SandboxError("sandbox cleanup incomplete: " + "; ".join(errors))

    def _remove_the_rest(self, errors: list[str]) -> None:
        if self._net:
            self._step(errors, hostops.remove_network, self.host, self._net)  # a leaked network keeps its subnet
        for m in reversed(self._mounts):
            self._step(errors, self.host.unmount, m)
        if self.keep_evidence:  # the copy failed: the disks hold the only record of the run
            kept = self.s.work_root / "_kept_evidence"
            self._step(errors, lambda: self.host.run(["mkdir", "-p", str(kept)], 20, sudo=True))
            self._step(errors, lambda: self.host.run(["mv", str(self.dir), str(kept / self.sid)], 60, sudo=True))
            self._meta["evidence_kept"] = {"under": "sandbox.work_root", "directory_id": self.sid.rsplit("-", 1)[-1]}
        else:
            self._step(errors, lambda: self.host.run(["rm", "-rf", "--one-file-system", str(self.dir)], 300, sudo=True))
        if (
            self.dir.exists() and not self.keep_evidence
        ):  # verified, not assumed: a mount left behind keeps the directory
            errors.append("the run directory still exists")

    def __enter__(self) -> DockerSandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


__all__ = ["DockerSandbox"]
