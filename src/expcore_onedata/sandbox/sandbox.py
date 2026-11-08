# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/sandbox.py, blob 6e2fc30132ac38e62270de4bf334f100e1d85d40
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""One sandboxed run: container lifecycle, exec, collect, metadata.

Per run (all named dataset-168-<id>):
  * a loop-mounted ext4 disk of the option's size, mounted at /work (the home
    and working directory of the unprivileged user; ENOSPC beyond the quota);
  * an INTERNAL docker network (no route out) holding the agent container and
    the gate container; the gate is the agent's only network peer;
  * the gate (network enforcement, see gate.py / gatepolicy.py);
  * M and M+: a oneclient sidecar that owns the mount and the token, exported
    read-only to the agent through bind propagation; the agent container has
    neither the token, nor FUSE, nor a route to any Onedata host;
  * the agent container: read-only root, all capabilities dropped,
    no-new-privileges, fixed cpus/memory/pids.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from dataset_76.records import Layout, Option
from dataset_76.sandbox import earlyoom as EO
from dataset_76.sandbox import hostops as H
from dataset_76.sandbox import infradeath as ID
from dataset_76.sandbox import killcause as KC
from dataset_76.sandbox import netpool
from dataset_76.sandbox import teardown as TD
from dataset_76.sandbox import termination as TERM
from dataset_76.sandbox.config import (
    AGENT_OOM_SCORE_ADJ,
    DataConfig,
    SandboxConfig,
    dataset_location,
    host_alias_args,
    study_env,
    validate_host_aliases,
    validate_knowledge,
)
from dataset_76.sandbox.gatepolicy import REST_OPTIONS
from dataset_76.sandbox.oneclient_log import storage_detection as oneclient_storage_detection
from dataset_76.sandbox.oneclient_log import summarize as summarize_oneclient_log
from dataset_76.sandbox.route import observed_route

log = logging.getLogger(__name__)
ERROR_LOG = (
    "instrument-error.log"  # what sitecustomize leaves when the instrument's import fails, and nothing else does
)
LOADED_RECORD_REQUIRED = True  # the tracer of feat/34-tracer-batch writes it: a traced run without one must not pass (test_run_evidence checks this agrees with the tree)
CERTIFI_PATH = "/usr/local/lib/python3.12/site-packages/certifi/cacert.pem"
SIGNAL_EXITS = (137, 143, -9, -15)  # a cell that ended by SIGKILL or SIGTERM, what earlyoom sends
CAP_MARGIN_S = 1.0  # past `timeout`'s own kill inside the container, for what Docker takes to start it
START_WITHIN_S = (
    60.0  # a process killed later than this had long since imported the tracer, or never will, on the most loaded host
)
QUIESCE_ROUNDS = (
    5  # a forking worker can start a child between the listing and the kill: listed again until none is new
)
PINNED = [
    "numpy",
    "scipy",
    "pandas",
    "xarray",
    "netCDF4",
    "h5netcdf",
    "h5py",
    "fsspec",
    "onedatarestfsspec",
    "onedatafilerestclient",
]


@dataclasses.dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    timed_out: bool = False
    kill: dict | None = (
        None  # set when the cell ended with a kill or a timeout: the cause (dataset_76.sandbox.killcause)
    )


PERF_FLUSH_WAIT_S = 5  # > oneclient's 4 s flush interval of the perf CSV (day0/evidence/q1b_q2_source.md)
GATE_STOP_WAIT_S = 3.0  # how long `collect` waits for a gate that was sent SIGTERM; the gate's own teardown budget is 1 s (gate.py TEARDOWN_BUDGET_S)
GATE_STOP_CALL_MIN_S = 0.5  # the least a docker call of that wait is given, however little of the wait is left
GATE_STOP_LOGS_S = 10.0  # reading the exited gate's output for a write failure
PERF_LOG_DIR = "/var/log/oneclient"  # inside the sidecar; a host directory the agent container never mounts


def effective_log_dir(oc_args: list[str], default: str = "/tmp/oneclient/0") -> str:
    """The log directory oneclient was actually started with: the last --log-dir / -l in its arguments, else the default."""
    out = default
    for i, a in enumerate(oc_args):
        if a in ("--log-dir", "-l") and i + 1 < len(oc_args):
            out = oc_args[i + 1]
        elif a.startswith("--log-dir="):
            out = a.split("=", 1)[1]
    return out


def oneclient_sidecar_args(
    provider_host: str, io: list[str], tls_mode: str, extra: list[str], perf_log: bool = False
) -> list[str]:
    """Arguments after the image name, with `--entrypoint env`.

    The stock onedata/oneclient image sets ONECLIENT_INSECURE=1 and oneclient reads --insecure from it, so the
    default entrypoint would run unverified whatever we pass. `env -u ONECLIENT_INSECURE` removes it, and
    `--insecure` is passed only when the mode is "insecure". `-f` (foreground) is what the image's run.sh adds.
    """
    if tls_mode not in ("verify", "insecure"):
        raise H.SandboxError(f"unknown oneclient_tls {tls_mode!r}")
    # reject -i, --insecure and clusters of value-less short flags that contain i (letters inside values are fine)
    if any(
        a in ("-i", "--insecure")
        or (a.startswith("-") and not a.startswith("--") and "i" in a[1:] and set(a[1:]) <= set("fdivh"))
        for a in extra
    ):
        raise H.SandboxError(
            "TLS verification is set by oneclient_tls only; do not pass -i/--insecure in oneclient_extra_args"
        )
    return [
        "-u",
        "ONECLIENT_INSECURE",
        "/opt/oneclient/bin/oneclient",
        "-f",
        "-H",
        provider_host,
        "-o",
        "ro,allow_other",
        *io,
        *(["--insecure"] if tls_mode == "insecure" else []),
        *(["--log-dir", PERF_LOG_DIR, "--log-read-write-perf"] if perf_log else []),
        *extra,
        "/mnt/oneclient",
    ]


def summarize_gate_log(lines: list[str]) -> dict:
    """Content transfers seen by the gate so far (D: each is a whole-file transfer, see gate.py)."""
    recs = [json.loads(x) for x in lines if x.strip()]
    content = [r for r in recs if r.get("tag") == "content" and r.get("decision") == "allow"]
    ok = [r for r in content if r.get("status") in (200, 206)]  # 206: T range reads
    errors = gate_errors(recs)
    # the bytes whatever the request's final status (Astra's Sandbox #8): 10 bytes of a failed transfer moved, and a whole file fetched before the delivery to the sandbox failed was fetched
    return {
        "content_requests": len(ok),
        "whole_file_transfers": sum(
            1 for r in content if "upstream_bytes" in r
        ),  # D only; the gate sets it once the whole file has arrived
        "upstream_bytes": sum(r.get("upstream_bytes", 0) for r in content),
        "bytes_to_sandbox": sum(r.get("bytes", 0) for r in content),
        "peak_spool_bytes": max([r.get("spool_peak_bytes", 0) for r in recs] or [0]),
        "refused_range_requests": sum(1 for r in recs if r.get("decision") == "deny" and r.get("range")),
        "errors": errors,
        # validity flags (science, #7): only T, condition_a and D have a gate, so an error the gate made itself is
        # one only those options can see; an upstream one is the platform's, which every option meets in some form
        "agent_saw_gate_error": errors["gate_5xx"] + errors["gate_resets"] > 0,
        "agent_saw_upstream_error": errors["upstream_5xx"] + errors["upstream_resets"] > 0,
    }


def _docker_failed(rc: int, err: str) -> bool:
    """`docker exec`, or the `timeout` under it, could not run the cell, as they say themselves on stderr. Not an exit code or words alone: a cell may exit 126, or print "the
    scheduler is not running" in its traceback, and either is its own outcome, which reading as a kill would now retry."""
    if rc == 0 or not any(
        marker in err for marker in ("Error response from daemon", "OCI runtime", "docker: ", "timeout: ")
    ):
        return False
    return rc in (125, 126) or "is not running" in err or "No such container" in err


def gate_log_unknown(why: str) -> dict:
    """No gate log to read. Unknown, never zero errors: `_agent_seen_errors` and the analysis read None as not recorded."""
    return {
        "unavailable": why,
        "content_requests": None,
        "whole_file_transfers": None,
        "upstream_bytes": None,
        "bytes_to_sandbox": None,
        "peak_spool_bytes": None,
        "refused_range_requests": None,
        "errors": None,
        "agent_saw_gate_error": None,
        "agent_saw_upstream_error": None,
    }


def gate_errors(recs: list[dict]) -> dict:
    """Every 5xx and every reset the client got, by origin (the gate's own, or the platform's), and the gate's retries.

    From the gate log's `error_origin` / `client_saw` fields: a 5xx is an HTTP status the client received, a reset is a
    connection the gate ended mid-response (a body already begun cannot carry an error status). Policy refusals are 4xx
    and counted apart; they are the enforcement, not errors."""
    out = {
        "gate_5xx": 0,
        "gate_resets": 0,
        "upstream_5xx": 0,
        "upstream_resets": 0,
        "connection_errors_gate": 0,
        "connection_errors_client": 0,
        "client_request_errors": 0,
        "upstream_retries": 0,
        "upstream_stale": 0,
        "policy_refusals": 0,
    }
    for r in recs:
        if r.get("event") in ("connection_error", "tcp_forward_error"):
            origin, seen = r.get("error_origin"), r.get("client_saw") in ("5xx", "reset")
            if origin == "upstream":
                out["upstream_resets"] += seen  # the platform side failed: the client lost its connection
            elif origin == "gate":
                out["connection_errors_gate"] += 1  # diagnostic, whether or not a client noticed
                out["gate_resets"] += seen  # the flag counts only what a client saw
            else:
                out["connection_errors_client"] += 1
            continue
        if r.get("event") == "request_error":  # a request the parser refused: the client's own
            out["client_request_errors"] += 1
            continue
        origin, saw = r.get("error_origin"), r.get("client_saw")
        if origin in ("gate", "upstream") and saw in ("5xx", "reset"):
            out[f"{origin}_{'5xx' if saw == '5xx' else 'resets'}"] += 1
        out["upstream_retries"] += int(r.get("upstream_retries") or 0)
        out["upstream_stale"] += int(r.get("upstream_stale") or 0)
        if r.get("decision") == "deny":
            out["policy_refusals"] += 1
    return out


_PENDING_CLEANUP: list[Sandbox] = []
_PENDING_LOCK = threading.Lock()  # guards the list; the campaign starts sandboxes from several worker threads
_DRAIN_LOCK = threading.Lock()  # one drain at a time; a concurrent start skips instead of waiting on docker


def _register_pending(sbx: Sandbox) -> None:
    with _PENDING_LOCK:
        if sbx not in _PENDING_CLEANUP:
            _PENDING_CLEANUP.append(sbx)


def cleanup_pending() -> int:
    """Retry the cleanup of sandboxes whose start failed and whose cleanup was incomplete (daemon trouble at the time).
    Returns how many are still not clean. Called at every start and at exit; safe from several threads at once."""
    if not _DRAIN_LOCK.acquire(blocking=False):  # another thread is draining: its result is as good as ours
        with _PENDING_LOCK:
            return len(_PENDING_CLEANUP)
    try:
        with _PENDING_LOCK:
            todo = list(_PENDING_CLEANUP)
        for sbx in todo:
            try:
                sbx.stop()
            except BaseException:
                continue
            with _PENDING_LOCK:
                if sbx in _PENDING_CLEANUP:
                    _PENDING_CLEANUP.remove(sbx)
        with _PENDING_LOCK:
            return len(_PENDING_CLEANUP)
    finally:
        _DRAIN_LOCK.release()


import atexit  # noqa: E402

atexit.register(cleanup_pending)  # a last attempt when the process ends normally


_SAMPLED_PIDS: set[int] = (
    set()
)  # every PID any sandbox of this driver has seen, for telling another run's earlyoom victim from one nobody saw
_NOT_TORN_DOWN: dict[str, int] = {}  # run key -> how many of its sandboxes are not verifiably gone
_NOT_TORN_DOWN_LOCK = threading.Lock()


def _note_up(run_key: str, delta: int) -> None:
    with _NOT_TORN_DOWN_LOCK:
        n = _NOT_TORN_DOWN.get(run_key, 0) + delta
        if n > 0:
            _NOT_TORN_DOWN[run_key] = n
        else:
            _NOT_TORN_DOWN.pop(run_key, None)


_TO_RETRY: dict[str, list] = {}  # run key -> how to try again what would not go: each call raises until it is gone


def note_unfinished(run_key: str, retry) -> None:
    with _NOT_TORN_DOWN_LOCK:
        if retry not in _TO_RETRY.setdefault(run_key, []):
            _TO_RETRY[run_key].append(retry)


def retry_teardown(run_key: str) -> bool:
    """Try again what would not go. True when nothing of the run's is left: the only end of its slot user's quarantine (real_sandbox.leased_token)."""
    with _NOT_TORN_DOWN_LOCK:
        todo = list(_TO_RETRY.get(run_key, []))
    for retry in todo:
        try:
            retry()
        except BaseException as e:
            log.warning("run %s: its teardown is still not finished: %r", run_key, e)
            continue
        with _NOT_TORN_DOWN_LOCK:
            _TO_RETRY[run_key].remove(retry)
    return not survives(run_key)


def survives(run_key: str | None) -> bool:
    """A sandbox of this run is not verifiably gone: a failed start or stop left it, or a teardown is holding it with its FUSE mount live. It may still be using the run's
    token, so the run's slot user must not go to another run. Any path, any thread: the slot lease asks here when it ends."""
    with _NOT_TORN_DOWN_LOCK:
        return bool(run_key) and _NOT_TORN_DOWN.get(run_key, 0) > 0


def _device_of(path) -> int:
    """The device a path is on (st_dev): a directory whose device differs from its parent's is a mount point. `os.path.ismount` would lstat `path/..` and
    answer False for one it was not allowed to look at, which is a wrong answer and not an unknown."""
    return os.stat(path).st_dev


class StartRefused(H.SandboxError, ValueError):
    """`Sandbox.start` refused before any container, network or mount exists: a rejected config or a moved image tag. No sandbox started, so the
    run used no attempt. (A ValueError too, for the callers that already catch the config errors.) A start that fails after creating something
    is not this: it raises its own error, and the run used an attempt."""


class Sandbox:
    def __init__(self):
        raise TypeError("use Sandbox.start()")

    # ------------------------------------------------------------------ start
    @classmethod
    def start(
        cls,
        run_key: str,
        option: Option | str,
        layout: Layout | str,
        data_config: DataConfig,
        config: SandboxConfig | None = None,
    ) -> Sandbox:
        TERM.raise_if_requested()  # a SIGTERM reached this process: nothing new starts (see termination.py)
        self = object.__new__(cls)
        if config is None or config.limits is None:
            raise ValueError(
                "Sandbox.start needs SandboxConfig(limits=Limits.for_dataset(s1_bytes, s2_bytes)): "
                "there is no default quota, it must come from the real dataset sizes"
            )
        try:
            config.limits.validate()
            validate_host_aliases(config.host_aliases)  # before any container exists
            if not config.image or not config.pinned_image_id.startswith("sha256:"):
                raise ValueError(
                    "SandboxConfig needs image (a tag or id) and pinned_image_id ('sha256:...', from pin_image): "
                    "the sandbox image is pinned by id, there is no default tag"
                )
        except ValueError as e:
            raise StartRefused(str(e)) from e
        resolved = H.image_id(config.image)
        if (
            resolved != config.pinned_image_id
        ):  # the tag was moved (or the wrong image pinned): refuse, never run another image
            raise StartRefused(
                f"image {config.image!r} now resolves to {resolved}, but {config.pinned_image_id} is pinned"
            )
        self._image = (
            resolved  # every container below is started by id, so a retag between this check and the run cannot matter
        )
        self.cfg = config
        self.dc = data_config
        self.option = Option(option)
        self.layout = Layout(layout)
        self.run_key = run_key
        self.sid = re.sub(r"[^A-Za-z0-9_.-]", "_", run_key)[:60] + "-" + uuid.uuid4().hex[:8]
        self.name = f"dataset-168-{self.sid}"
        self.dir = Path(self.cfg.work_root) / self.sid
        self._mounts: list[Path] = []
        self._containers: list[str] = []
        self._net: str | None = None
        self._cells = 0
        self._cell_log: list[dict] = []
        self._meta: dict = {}
        self._stopped = True  # nothing to clean up until _start has begun
        validate_knowledge(data_config.knowledge)
        if not re.fullmatch(r"[\x21-\x7e]{1,200}", run_key):  # it becomes an HTTP header value
            raise ValueError(f"run_key {run_key!r}: 1-200 printable ASCII characters without spaces")
        self._stopped = False
        _note_up(self.run_key, +1)  # until `stop` has verified everything gone
        # earlier failed starts of THIS process whose cleanup could not finish (daemon trouble then): retry now
        self._meta["pending_cleanups_left"] = cleanup_pending()
        try:
            # what killed processes left behind (theirs only: all three labels, owner provably gone); never fails the start
            self._meta["reaped_orphans"] = netpool.reap_orphans()
        except Exception as e:
            self._meta["reaped_orphans"] = {"error": repr(e)[:300]}
        try:
            self._start()
            self._bytes_at_start = self._read_byte_counters()  # after the mount is up, as the scripted runs read theirs
            self._note_markers_at_start()
            TERM.register(self)  # before the check: a SIGTERM after it finds this sandbox in the list
            TERM.raise_if_requested()  # one that came during the start: stopped below, never handed out
        except BaseException as exc:
            try:
                self.stop()
            except BaseException as e:  # incl. KeyboardInterrupt: whatever interrupted the cleanup, it stays retryable
                _register_pending(self)  # first of all: retried by cleanup_pending()
                for x in (exc, e):  # and reachable on whichever exception propagates, to retry stop()
                    try:
                        x.sandbox = self
                    except Exception:
                        pass
                try:
                    import sys

                    print(
                        f"sandbox {self.name}: cleanup after a failed start was incomplete: {e}",
                        file=sys.stderr,
                        flush=True,
                    )
                except Exception:
                    pass
                if not isinstance(e, Exception):  # an interrupt of the cleanup itself is not swallowed
                    raise
            raise
        return self

    def _start(self) -> None:
        cfg, dc, opt = self.cfg, self.dc, self.option
        rest = opt.value in REST_OPTIONS
        self.dir.mkdir(parents=True, exist_ok=False)
        started = time.time()
        self._started_epoch = started
        self._meta["oneclient_perf_log"] = False  # only an M/M+ sidecar started with the option sets it
        self._meta.update(
            option=opt.value,
            layout=self.layout.value,
            run_key=self.run_key,
            host_aliases=dict(sorted(cfg.host_aliases.items())),
            host=H.host_facts(),
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        )

        # 1. caches: fresh container, disk, fsspec instance and (M) oneclient process+mount; the host-wide drop is opt-in
        cache = {"fresh_container": True, "fresh_disk": True}
        if cfg.drop_host_caches:
            r = H.sudo("sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches", check=False)
            if r.returncode != 0:  # a warm cache would invalidate the run: do not start it
                raise H.SandboxError("host page cache could not be dropped: " + r.stderr.strip()[:200])
            cache["host_page_cache_dropped"] = True
        else:
            cache["host_page_cache_dropped"] = False  # explicit opt-out (tests, or the operator's choice)
        disk = cfg.limits.disk_bytes(opt)
        work = self.dir / "work"
        self._mounts.append(work)  # registered before the mount (unmounting what never got mounted is harmless)
        H.make_quota_dir(self.dir / "work.img", work, disk, cfg.agent_uid, usable=True)
        trace = self.dir / "trace"
        self._mounts.append(trace)
        H.make_quota_dir(
            self.dir / "trace.img", trace, cfg.limits.agent_trace_bytes, cfg.agent_uid
        )  # science #34, 18:31Z: 1 GiB, not the 256 MiB allowance

        # 2. hosts the sandbox may name
        hosts: dict[str, dict] = {}
        if rest:
            zone = dc.onezone_host
            hosts[zone] = {"role": "onezone", **dc.upstream.get(zone, {"addr": zone, "port": 443, "tls": True})}
            for ph in dc.provider_hosts if dc.provider_hosts is not None else self._discover_providers():
                hosts[ph] = {"role": "oneprovider", **dc.upstream.get(ph, {"addr": ph, "port": 443, "tls": True})}
        tls_hosts = list(hosts)
        if rest:
            self._meta["provider_route"] = observed_route(
                hosts, None
            )  # what the gate is configured with, not the setting

        # 3. network, gate
        tls = self.dir / "tls"
        H.make_ca(tls, tls_hosts or ["gate.invalid"])
        (self.dir / "gatelog").mkdir()
        (self.dir / "gatelog").chmod(0o777)  # the gate runs as root without DAC_OVERRIDE
        gate_cfg = {
            "option": opt.value,
            "run_key": self.run_key,
            "page_limit": cfg.page_limit,
            "verify_ssl": dc.verify_ssl,
            "log": "/var/gate/requests.jsonl",
            "tls_dir": "/etc/gate/tls",
            "hosts": hosts or {"gate.invalid": {"role": "none", "addr": "127.0.0.1"}},
            "tcp_forwards": [{"listen": k["port"], "host": k["host"], "port": k["port"]} for k in dc.knowledge],
        }
        (self.dir / "gate.json").write_text(json.dumps(gate_cfg))
        self._net = self.name + "-net"
        # inhibit_ipv4: the host gets no address on this bridge, so there is no gateway to reach
        subnet, tries = netpool.create(self._net, pool=cfg.subnet_pool, extra=netpool.labels(self.dir))
        self._meta["sandbox_network"] = {"subnet": subnet, "attempts": tries, "pool": cfg.subnet_pool}
        gate = self.name + "-gate"
        self._containers.append(
            gate
        )  # registered before it is created: an interrupt cannot leave one stop() does not know
        H.docker(
            "run",
            "-d",
            "--name",
            gate,
            "--network",
            "bridge",
            *netpool.labels(self.dir),
            "--add-host",
            "host.docker.internal:host-gateway",
            *host_alias_args(cfg.host_aliases),
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_BIND_SERVICE",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--user",
            "0",
            "--pids-limit",
            "512",
            "--memory",
            "1g",
            "-v",
            f"{self.dir / 'tls'}:/etc/gate/tls:ro",
            "-v",
            f"{self.dir / 'gate.json'}:/etc/gate/gate.json:ro",
            "-v",
            f"{self.dir / 'gatelog'}:/var/gate",
            "--entrypoint",
            "python",
            self._image,
            "/opt/dataset_76/gate/gate.py",
            "/etc/gate/gate.json",
        )
        H.docker("network", "connect", self._net, gate)
        self._wait_gate(gate)
        self.watch_container(
            "gate", gate
        )  # its state as the run begins: the baseline the check at collection compares with (infradeath.py)
        self._note_pids()  # from here on, and not only once the agent is up: the gate or oneclient can be a victim during the start
        gate_ip = H.docker(
            "inspect", "-f", f'{{{{(index .NetworkSettings.Networks "{self._net}").IPAddress}}}}', gate
        ).stdout.strip()
        if not gate_ip:
            raise H.SandboxError("gate has no IP on the sandbox network")

        # 4. M / M+: the mount sidecar
        mount_src = None
        if not rest:
            if opt == Option.condition_d:
                if not callable(dc.replicate):
                    raise H.SandboxError(
                        "M+ needs DataConfig.replicate (the scripted replication, #12); refusing to run M as M+"
                    )
                dc.replicate()
            mount_src = self._start_mount()
            if self._meta.get("mount_kind") == "oneclient":  # the stub mount of the local tests has no sidecar
                self.watch_container("sidecar", self.name + TD.SIDECAR_SUFFIX)
            self._note_pids()

        # 5. the agent container
        lim = cfg.limits
        args = [
            "run",
            "-d",
            "--init",
            "--oom-score-adj",
            str(AGENT_OOM_SCORE_ADJ),
            "--name",
            self.name,
            "--network",
            self._net,
            *netpool.labels(self.dir),
            "--user",
            f"{cfg.agent_uid}:{cfg.agent_uid}",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            str(lim.cpus),
            "--memory",
            str(lim.container_memory_bytes),
            "--memory-swap",
            str(lim.container_memory_bytes),
            "--pids-limit",
            str(lim.pids),
            "--ulimit",
            f"nofile={lim.nofile}:{lim.nofile}",
            "--tmpfs",
            f"/tmp:size={lim.tmp_bytes},mode=1777",
            "--shm-size",
            str(lim.shm_bytes),
            "-v",
            f"{trace}:/trace",
            "-v",
            f"{work}:/work",
            "-v",
            f"{tls / 'ca.pem'}:/etc/dataset_76/ca.pem:ro",
            # the shipped client calls requests' session.send(), which ignores REQUESTS_CA_BUNDLE and uses
            # certifi's bundle: replace it, in this container only, by certifi + the run CA
            "-v",
            f"{self._trust_bundle(tls)}:{CERTIFI_PATH}:ro",
        ]
        names = list(tls_hosts) + [k["name"] for k in dc.knowledge]
        for n in names:
            args += ["--add-host", f"{n}:{gate_ip}"]
        # the agent reaches every provider through the gate (its name maps to the gate above), so an alias for a name it
        # already has is NOT added: a second entry would put the LAN address beside the gate's. Any other alias is passed.
        args += host_alias_args(cfg.host_aliases, skip=set(names))
        self._meta["dataset_location"] = dataset_location(dc, opt, self.layout)
        env = {"STUDY_VAR_61": cfg.instrument_module, **study_env(dc, opt, self.layout)}
        if cfg.instruments_dir:
            args += ["-v", f"{Path(cfg.instruments_dir).resolve()}:/opt/dataset_76/instruments:ro"]
            env["PYTHONPATH"] = "/opt/dataset_76/instruments:/opt/dataset_76/hooks"
        if rest:
            env["ONEDATA_ONEZONE_HOST"] = dc.onezone_host
            env["ONEDATA_VERIFY_SSL"] = "true"
            if dc.token:
                env["ONEDATA_TOKEN"] = dc.token  # the read-only dataset token: the agent's own credential
        else:
            args += ["--mount", f"type=bind,src={mount_src},dst={dc.mount_path},readonly,bind-propagation=rslave"]
        denv = dict(os.environ)
        for k, v in env.items():
            args += ["-e", k]
            denv[k] = v
        args += [self._image, "sleep", "infinity"]
        self._containers.append(self.name)
        H.docker(*args, env=denv)

        # 6. evidence
        self._meta.update(self._evidence(cache, disk, rest))
        self._check_capacity()
        self._start_sampler()

    # ---------------------------------------------------------------- helpers
    def _check_capacity(self) -> None:
        """Measure what the agent can really write, from inside the container, and refuse to run when it could
        hold a wholesale copy of the smaller layout (D: the work disk must instead hold 1.2 x S2)."""
        probe = (
            "import json, os\n"
            "print(json.dumps({p: (lambda v: v.f_bavail * v.f_frsize)(os.statvfs(p)) "
            "for p in ('/work', '/tmp', '/trace', '/dev/shm')}))"
        )
        r = self.exec(probe, 60, _record=False)
        if r.exit_code != 0:
            raise H.SandboxError("capacity probe failed: " + r.stderr[-300:])
        cap = json.loads(r.stdout)
        lim = self.cfg.limits
        self._meta["writable_capacity_bytes"] = cap
        if self.option == Option.D:
            if cap["/work"] < 1.2 * lim.s2_size_bytes:
                raise H.SandboxError(f"D work disk offers {cap['/work']} B, below 1.2 x S2")
            return
        total, smallest = sum(cap.values()), min(lim.s1_size_bytes, lim.s2_size_bytes)
        if total >= smallest:
            raise H.SandboxError(
                f"writable capacity {total} B (work, /tmp, trace, /dev/shm) could hold a wholesale "
                f"copy of the smaller layout ({smallest} B)"
            )

    def _discover_providers(self) -> list[str]:
        """Ask Onezone, from the host, which providers support the space (their hosts the gate may serve)."""
        import requests

        dc = self.dc
        u = dc.upstream.get(dc.onezone_host)
        base = (
            f"{'https' if u.get('tls', True) else 'http'}://{u['addr']}:{u.get('port', 443)}"
            if u
            else f"https://{dc.onezone_host}"
        )
        r = requests.post(
            f"{base}/api/v3/onezone/tokens/infer_access_token_scope",
            json={"token": dc.token},
            verify=dc.verify_ssl,
            timeout=30,
        )
        r.raise_for_status()
        scope = r.json()["dataAccessScope"]
        pids = {p for s in scope["spaces"].values() if s["name"] == dc.space_name for p in s["supports"]}
        if not pids:
            raise H.SandboxError(f"space {dc.space_name!r} not in the token's scope")
        return sorted(scope["providers"][p]["domain"] for p in pids)

    def _oneshot(self, entrypoint: str, image: str, *cmd: str, check: bool = True):
        """A short-lived `docker run` with a name of its own, registered for cleanup BEFORE it is created: an interrupt
        or a timeout between creation and exit cannot leave an anonymous container that stop() does not know."""
        name = f"{self.name}-once-{uuid.uuid4().hex[:6]}"
        self._containers.append(name)
        return H.docker(
            "run",
            "--rm",
            "--name",
            name,
            *netpool.labels(self.dir),
            *host_alias_args(self.cfg.host_aliases),
            "--entrypoint",
            entrypoint,
            image,
            *cmd,
            check=check,
        )

    def _trust_bundle(self, tls: Path) -> Path:
        base = self._oneshot("cat", self._image, CERTIFI_PATH).stdout
        out = tls / "trust-bundle.pem"
        out.write_text(base + "\n" + (tls / "ca.pem").read_text())
        return out

    def _wait_gate(self, gate: str) -> None:
        for _ in range(100):
            if "gate ready" in H.docker("logs", gate, check=False).stdout:
                return
            if H.docker("inspect", "-f", "{{.State.Running}}", gate).stdout.strip() != "true":
                raise H.SandboxError("gate exited: " + H.docker("logs", gate, check=False).stderr[-500:])
            time.sleep(0.1)
        raise H.SandboxError("gate did not become ready")

    def _start_mount(self) -> Path:
        dc, cfg = self.dc, self.cfg
        share = self.dir / "mount"
        share.mkdir()
        if dc.mount_stub_dir:
            self._meta["mount_kind"] = "stub-dir (TEST ONLY, not oneclient)"
            return Path(dc.mount_stub_dir).resolve()
        # the mount point must be a shared mount so the sidecar's FUSE mount propagates out
        self._mounts.append(share)
        H.sudo("mount", "--bind", str(share), str(share))
        H.sudo("mount", "--make-rshared", str(share))
        side = self.name + "-oneclient"
        io = {"direct": ["--force-direct-io"], "proxy": ["--force-proxy-io"]}.get(dc.io_mode or "", [])
        if dc.io_mode not in (None, "direct", "proxy"):
            raise H.SandboxError(f"unknown io_mode {dc.io_mode!r}")
        if dc.oneclient_tls not in ("verify", "insecure"):
            raise H.SandboxError(f"unknown oneclient_tls {dc.oneclient_tls!r}")
        oc_args = oneclient_sidecar_args(
            dc.oneclient_provider_host or (dc.provider_hosts or [""])[0],
            io,
            dc.oneclient_tls,
            dc.oneclient_extra_args,
            dc.oneclient_perf_log,
        )
        self._meta["oneclient_perf_log"] = bool(dc.oneclient_perf_log)
        perf_mount = []
        if dc.oneclient_perf_log:  # the CSV goes to a host directory that only the sidecar mounts
            (self.dir / "perflog").mkdir()
            perf_mount = ["-v", f"{self.dir / 'perflog'}:{PERF_LOG_DIR}"]
        # recorded from what is actually passed, not from the setting
        self._meta["oneclient_tls"] = "insecure" if "--insecure" in oc_args else "verify"
        self._meta["oneclient_args"] = [a for a in oc_args if a != dc.oneclient_token]
        self._meta["provider_route"] = observed_route(None, oc_args)
        self._meta["oneclient_log_dir"] = effective_log_dir(oc_args)  # where the sidecar really writes its log
        env = dict(os.environ, ONECLIENT_ACCESS_TOKEN=dc.oneclient_token)
        cmd = [
            "run",
            "-d",
            "--name",
            side,
            "--network",
            "bridge",
            *netpool.labels(self.dir),
            "--cpus",
            str(cfg.limits.cpus),
            "--memory",
            str(cfg.limits.container_memory_bytes),
            "--memory-swap",
            str(cfg.limits.container_memory_bytes),
            "--pids-limit",
            str(cfg.limits.pids),
            "--cap-add",
            "SYS_ADMIN",
            "--device",
            "/dev/fuse",
            "--security-opt",
            "apparmor:unconfined",
            *host_alias_args(cfg.host_aliases),
            "--tmpfs",
            "/root/.local:size=1g",
            "-e",
            "ONECLIENT_ACCESS_TOKEN",
            "--mount",
            f"type=bind,src={share},dst=/mnt/oneclient,bind-propagation=rshared",
            *perf_mount,
            "--entrypoint",
            "env",
            cfg.oneclient_image,
            *oc_args,
        ]
        self._containers.append(side)
        t_start = time.monotonic()  # M's fixed set-up cost: from starting the sidecar to the mount being there
        H.docker(*cmd, env=env)
        for _ in range(300):  # wait until something is mounted
            if H.is_fuse_mounted(Path("/proc/self/mountinfo").read_text(), str(share)):
                break
            if H.docker("inspect", "-f", "{{.State.Running}}", side).stdout.strip() != "true":
                raise H.SandboxError("oneclient sidecar exited: " + H.docker("logs", side, check=False).stderr[-500:])
            time.sleep(0.2)
        else:
            raise H.SandboxError("oneclient mount did not appear")
        self._mount_wait_s = round(time.monotonic() - t_start, 3)
        self._meta["mount_kind"] = "oneclient"
        info = H.docker("inspect", "-f", "{{.State.StartedAt}} {{.State.Pid}} {{.Id}} {{.Image}}", side).stdout.split()
        self._meta["mount_process"] = {"started_at": info[0], "host_pid": int(info[1]), "container_id": info[2][:12]}
        # what the RUNNING sidecar uses (a tag moved after the start cannot change it); recorded, not pinned
        self._meta["oneclient_image_id"] = info[3]
        self._meta["oneclient_version"] = self._oneshot(
            "/opt/oneclient/bin/oneclient", info[3], "--version", check=False
        ).stdout.strip()
        return share

    def _evidence(self, cache: dict, disk: int, rest: bool) -> dict:
        probe = (
            "import json,os,sys,importlib.metadata as m;"
            "v={p:m.version(p) for p in %r};"
            "i=os.environ.get('STUDY_VAR_61','');e='/trace/%s';"
            "print(json.dumps(dict(py=sys.version.split()[0],versions=v,uid=os.getuid(),"
            "work=sorted(os.listdir('/work')),trace=sorted(os.listdir('/trace')),"
            "home_cache=sorted(os.listdir(os.path.expanduser('~'))),"
            "instrument=dict(module=i,loaded=i in sys.modules,writing=os.path.exists('/trace/ok.'+str(os.getpid())),error=open(e).read()[-500:] if os.path.exists(e) else None)"
            ")))"
        ) % (PINNED, ERROR_LOG)
        r = self.exec(probe, 60, _record=False)
        if r.exit_code != 0:
            raise H.SandboxError("evidence probe failed: " + r.stderr[-300:])
        ev = json.loads(r.stdout)
        # the positive record (science's ruling on Tracer #6): sitecustomize swallows a failed import and leaves only a log, so a traced run must show that the
        # instrument loaded, here in the sandbox before any agent turn, and `instrument_state` again at the end. No record is not a clean record.
        inst = {"configured": bool(self.cfg.instruments_dir), **ev["instrument"]}
        self._meta["instrument"] = inst
        if inst["configured"] and (not inst["loaded"] or inst["error"]):
            raise H.SandboxError(
                f"the instrument did not load ({inst['module']}): {inst['error'] or 'not imported'}"[:600]
            )
        if (
            inst["configured"] and not inst["writing"]
        ):  # imported, and its record is going nowhere: a read-only /trace swallows even the error log
            raise H.SandboxError(f"the instrument loaded and left no marker in /trace ({inst['module']})"[:600])
        cache.update(
            work_dir_at_start=ev["work"],
            trace_dir_at_start=ev["trace"],
            home_at_start=ev["home_cache"],
            fsspec_cache_dirs_present=False,
            oneclient_cache="fresh sidecar container with empty tmpfs" if not rest else None,
        )
        img = self._image
        lim = self.cfg.limits
        return {
            "image": self.cfg.image,
            "image_id": img,
            "python": ev["py"],
            "versions": ev["versions"],
            "agent_uid": ev["uid"],
            "io_mode": self.dc.io_mode if not rest else None,
            "limits": {
                "cpus": lim.cpus,
                "memory_bytes": lim.container_memory_bytes,
                "pids": lim.pids,
                "agent_memory_bytes": lim.agent_memory_bytes,
                "tracer_headroom_bytes": lim.tracer_headroom_bytes,
                "nofile": lim.nofile,
                "disk_bytes": disk,
                "trace_bytes": lim.agent_trace_bytes,
            },
            "gate": {
                "policy_option": self.option.value,
                "page_limit": self.cfg.page_limit if self.option == Option.condition_c else None,
            },
            "cache_clear": cache,
            "condition_d_replication": self.option == Option.condition_d and callable(self.dc.replicate),
        }

    # ------------------------------------------------------------------- API
    # ---- why was a cell killed? (read from the host, before any teardown) -----------------------------------------
    def _cgroup_dir(self) -> Path | None:
        """The agent container's cgroup (v2) directory on the host, or None if it cannot be found."""
        cached = getattr(self, "_cg_dir", None)
        if cached is not None and Path(cached, "memory.events").is_file():
            return Path(cached)
        try:
            cid = H.docker("inspect", "-f", "{{.Id}}", self.name).stdout.strip()
        except Exception:
            return None
        self._cid = cid
        for cand in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope", f"/sys/fs/cgroup/docker/{cid}"):
            if Path(cand, "memory.events").is_file():
                self._cg_dir = cand
                return Path(cand)
        return None

    def _other_cgroup_dir(self, container: str) -> Path | None:
        """The cgroup directory of another container of this sandbox (gate, sidecar); None when it cannot be found."""
        try:
            cid = H.docker("inspect", "-f", "{{.Id}}", container).stdout.strip()
        except Exception:
            return None
        for cand in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope", f"/sys/fs/cgroup/docker/{cid}"):
            if Path(cand, "memory.events").is_file():
                return Path(cand)
        return None

    @staticmethod
    def _cgroup_totals(d: Path | None) -> dict:
        """{cpu_s, peak_bytes, oom_kills} of a container's cgroup since it started; None for what could not be read (never zero)."""
        out = {"cpu_s": None, "peak_bytes": None, "oom_kills": None}
        if d is None:
            return out
        try:
            for line in (d / "cpu.stat").read_text().splitlines():
                k, _, v = line.partition(" ")
                if k == "usage_usec" and v.strip().isdigit():
                    out["cpu_s"] = round(int(v) / 1e6, 3)
        except Exception:
            pass
        try:
            out["peak_bytes"] = int((d / "memory.peak").read_text().strip())
        except Exception:
            pass
        try:
            ev = KC.parse_events((d / "memory.events").read_text())
            out["oom_kills"] = None if ev is None else int(ev.get("oom_kill", 0))
        except Exception:
            pass
        return out

    def container_stats(self) -> dict:
        """Per container of this run (agent, gate, and for M/M+ the oneclient sidecar): CPU seconds, peak memory, OOM kills, read
        from each cgroup before teardown (same shape as the scripted runs' `containers`)."""
        fz = self.__dict__.get("_gate_frozen")
        out = {
            "agent": self._cgroup_totals(self._cgroup_dir()),
            "gate": fz["stats"] if fz is not None else self._cgroup_totals(self._other_cgroup_dir(self.name + "-gate")),
        }
        if self.option.value not in REST_OPTIONS:
            out["sidecar"] = self._cgroup_totals(self._other_cgroup_dir(self.name + "-oneclient"))
        return out

    @staticmethod
    def _read_stat(d: Path) -> dict[str, int] | None:
        try:
            return {
                k: int(v) for k, v in (ln.split() for ln in (d / "memory.stat").read_text().splitlines() if ln.strip())
            }
        except Exception:
            return None

    def _memory_facts(self) -> dict:
        """Counters of the agent container's cgroup: the memory.events counters, peak and limit. None where unreadable.
        `memory_peak_bytes` is the cgroup's high-water mark and INCLUDES file cache and tmpfs (/tmp, /dev/shm); the sampled
        `memory_anon_peak_bytes` (anon + shmem, see MemSampler) is the better measure of what the agent's code allocated."""
        out = {"oom_kill_count": None, "memory_peak_bytes": None, "memory_max_bytes": None, "events": None}
        d = self._cgroup_dir()
        if d is None:
            return out
        try:
            out["events"] = KC.parse_events((d / "memory.events").read_text())
            out["oom_kill_count"] = None if out["events"] is None else int(out["events"].get("oom_kill", 0))
        except Exception:
            pass
        for key, fn in (("memory_peak_bytes", "memory.peak"), ("memory_max_bytes", "memory.max")):
            try:
                v = (d / fn).read_text().strip()
                out[key] = None if v == "max" else int(v)
            except Exception:
                pass
        return out

    MEM_SAMPLE_PERIOD_S = 1.0

    def _sample_memory(self) -> None:
        """One sample of the cgroup's anonymous memory plus shared/tmpfs memory (what the agent's code allocated, without the
        reclaimable file cache); keeps the maximum and the stat at that moment."""
        d = self._cgroup_dir()
        st = self._read_stat(d) if d is not None else None
        if st is None:
            return
        v = st.get("anon", 0) + st.get("shmem", 0)
        if v > getattr(self, "_anon_peak", -1):
            self._anon_peak = v
            self._stat_at_anon_peak = {k: st.get(k) for k in ("anon", "file", "shmem", "kernel", "sock")}

    # ---- did the gate or the sidecar survive the run? (science's ruling, 2 Nov: container state at collection, never the agent's errors; infradeath.py) ----
    @staticmethod
    def _oom_counters_of(cid: str) -> dict[str, int] | None:
        """{oom, oom_kill} of a container's cgroup, read by its id; None when it cannot be read (never zero). An exited container's cgroup is gone."""
        for cand in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope", f"/sys/fs/cgroup/docker/{cid}"):
            try:
                ev = KC.parse_events(Path(cand, "memory.events").read_text())
            except OSError:
                continue
            if ev is not None:
                return {"oom": int(ev.get("oom", 0)), "oom_kill": int(ev.get("oom_kill", 0))}
        return None

    def _infra_read(self, container: str, timeout: float = 30) -> tuple[dict | None, str | None]:
        """(state, None) of a container from `docker inspect` and its cgroup, or (None, why) when docker cannot say. `timeout`: how long the docker call may take (a timeout is read as
        "cannot say"); the default is `_docker_t`'s."""
        r = H._docker_t("inspect", container, timeout=timeout)
        if r.returncode != 0:
            return None, f"docker inspect exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}"
        st = ID.parse_inspect(r.stdout)
        if st is None:
            return None, f"docker inspect printed something unreadable: {r.stdout.strip()[:120]!r}"
        c = self._oom_counters_of(st["id"])
        st["oom"], st["oom_kill"] = (c["oom"], c["oom_kill"]) if c else (None, None)
        return st, None

    def watch_container(self, role: str, container: str) -> None:
        """Register a container of this run that is not the agent's (the gate, the oneclient sidecar, or any other the run depends on) and record its state now as the baseline.
        It must be running: one that is not, or whose state cannot be read, is a failed start. A registered container is removed by `stop` with the others."""
        st, why = self._infra_read(container)
        if st is None:
            raise H.SandboxError(f"the {role} ({container}) could not be inspected when it came up: {why}")
        if st["status"] != "running":
            raise H.SandboxError(
                f"the {role} ({container}) is not running when the run begins (status {st['status']}, exit code {st['exit_code']})"
            )
        watched = self.__dict__.setdefault("_watched", {})
        if not watched:
            self._infra_uptime0 = self._uptime()  # the kernel log is read from here when a death needs its limit named
        if role in watched:
            raise ValueError(f"a {role} is already registered")
        watched[role] = {"container": container, "start": st}
        if container not in self._containers:
            self._containers.append(container)

    def infra_container_check(self) -> dict:
        """The state of every registered container now against its baseline, and the cause of each death (infradeath.cause_of; amendment 24, science, #2): {"containers": {role: {container,
        start, end, read_error, problems, death}}, "deaths": [{role, container, option, cause, category, basis, problems}], "death": [the infrastructure deaths' problems],
        "death_cause": the run's one infrastructure cause or None}. Read from the host while the containers are up, never from the agent's output. A container with no problem ran throughout."""
        out: dict = {}
        found: list[tuple[str, str, list[str], dict | None, dict | None, str | None]] = []
        for role, w in self.__dict__.get("_watched", {}).items():
            fz = self.__dict__.get("_gate_frozen") if role == "gate" else None
            end, why = (
                (fz["end"], None) if fz is not None else self._infra_read(w["container"])
            )  # a gate we stopped ourselves: as it was when the signal was sent
            probs = ID.problems(role, self.option.value, w["container"], w["start"], end, why)
            out[role] = {
                "container": w["container"],
                "start": w["start"],
                "end": end,
                "read_error": why,
                "problems": probs,
                "death": None,
            }
            if probs:
                found.append((role, w["container"], probs, w["start"], end, why))
        deaths: list[dict] = []
        if found:
            kernel = self._kernel_oom(
                getattr(self, "_infra_uptime0", None)
            )  # once, and only for a run that has a death to explain; None: unreadable
            hit = {k.get("role") for k in self._meta.get("earlyoom_kills") or [] if isinstance(k, dict)}
            for role, container, probs, start, end, why in found:
                c = ID.cause_of(role, start, end, why, kernel=kernel, earlyoom=role in hit)
                d = {"role": role, "container": container, "option": self.option.value, **c, "problems": probs}
                deaths.append(d)
                out[role]["death"] = d
        infra = [d for d in deaths if d["category"] == "infrastructure"]
        return {
            "containers": out,
            "deaths": deaths,
            "death": [p for d in infra for p in d["problems"]],
            "death_cause": ID.run_cause(d["cause"] for d in infra),
        }

    def _container_roles(self) -> dict[str, str]:
        roles = {"agent": self.name, "gate": self.name + "-gate"}
        if self.option.value not in REST_OPTIONS and self._meta.get("mount_kind") == "oneclient":
            roles["sidecar"] = self.name + "-oneclient"
        return roles

    def _note_pids(self) -> None:
        try:
            self._sample_pids()
        except Exception:
            pass

    def _sample_pids(self) -> None:
        """Host PIDs in each of this run's containers, kept for the whole run: what earlyoom's log names is gone from /proc by the time
        anyone reads it, so this is how a kill is traced to its run (dataset_76.sandbox.earlyoom)."""
        dirs = self.__dict__.setdefault("_pid_cgroups", {})
        seen = self.__dict__.setdefault("_seen_pids", {})
        now = time.time()
        for role, container in self._container_roles().items():
            if role not in dirs:
                d = self._cgroup_dir() if role == "agent" else self._other_cgroup_dir(container)
                if d is None:
                    continue
                dirs[role] = d
            try:
                EO.note_pids(seen.setdefault(role, {}), (dirs[role] / "cgroup.procs").read_text(), now)
                _SAMPLED_PIDS.update(seen[role])
            except (OSError, ValueError):
                pass  # the container has gone; what was seen stays

    def _earlyoom_scan(self) -> list[dict]:
        """The earlyoom kills of this run so far, kept in the metadata. An unreadable journal is recorded as unknown, never as none."""
        if getattr(self, "_started_epoch", None) is None:
            return []
        self._sample_pids()  # what a cell that has just ended left behind
        kills = EO.read_kills(self._started_epoch)
        self._meta["earlyoom_log_readable"] = kills is not None
        prev = self._meta.setdefault("earlyoom_kills", [])
        if kills is None:  # not known: what is already on record stays
            return prev
        self._journal_kills = kills
        found = prev + [k for k in EO.attribute(kills, getattr(self, "_seen_pids", {})) if k not in prev]
        self._meta["earlyoom_kills"] = found
        # for the audit, deciding nothing: kills of this uid in the window that match no PID seen here, usually another run's
        self._meta["earlyoom_other_kills_of_this_uid"] = sum(k.uid == self.cfg.agent_uid for k in (kills or [])) - sum(
            k["uid"] == self.cfg.agent_uid for k in found
        )
        # of those, the ones no sandbox here ever saw: a process born and killed between two samples. It may be this run's, and nothing tells: retried, not assumed clean
        known = {(k["pid"], k["t"]) for k in found}
        self._meta["earlyoom_unsampled_kills_of_this_uid"] = [
            k.record()
            for k in kills
            if k.uid == self.cfg.agent_uid and (k.pid, k.t) not in known and k.pid not in _SAMPLED_PIDS
        ]
        return found

    def _start_sampler(self) -> None:
        self._anon_peak, self._stat_at_anon_peak = -1, None
        self._sampler_stop = threading.Event()

        def loop():
            while not self._sampler_stop.is_set():
                try:
                    self._sample_memory()
                    self._sample_pids()
                except Exception:
                    pass
                self._sampler_stop.wait(self.MEM_SAMPLE_PERIOD_S)

        self._sampler = threading.Thread(target=loop, daemon=True, name=f"memsample-{self.name}")
        self._sampler.start()

    def _kernel_oom(self, uptime_before: float | None) -> list[dict] | None:
        """OOM-kill lines of the kernel log since `uptime_before` (host uptime in seconds), None if the log cannot be read."""
        if uptime_before is None:
            return None
        try:
            r = H.sudo("dmesg", check=False, timeout=20)
            if r.returncode != 0:
                return None
            return KC.parse_kernel_oom(r.stdout, since_s=uptime_before - 1.0)
        except Exception:
            return None

    @staticmethod
    def _uptime() -> float | None:
        try:
            return float(Path("/proc/uptime").read_text().split()[0])
        except Exception:
            return None

    def _kill_facts(
        self, before: dict | None, duration_s: float, timeout_s: float, uptime_before: float | None = None
    ) -> dict:
        """The facts about a cell that ended with a kill (137) or a timeout (124), read from the host before any teardown,
        and the classification (dataset_76.sandbox.killcause): `reason` is memory_limit | host_oom | container_dead | time_limit |
        killed_other | unknown, `category` agent | infrastructure | unknown. Docker's sticky OOMKilled flag is recorded, not used."""
        facts = self._memory_facts()
        try:
            st = (
                H.docker("inspect", "-f", "{{.State.OOMKilled}}|{{.State.Status}}|{{.State.ExitCode}}", self.name)
                .stdout.strip()
                .split("|")
            )
            container = {
                "docker_oom_killed": st[0] == "true",
                "container_status": st[1],
                "container_exit_code": int(st[2]),
            }
        except Exception:
            container = {"docker_oom_killed": None, "container_status": None, "container_exit_code": None}
        running = None if container["container_status"] is None else container["container_status"] == "running"
        need_log = facts["events"] is None or (facts["events"] or {}).get("oom_kill", 0) > (before or {}).get(
            "oom_kill", 0
        )
        kernel = self._kernel_oom(uptime_before) if need_log else None
        cls = KC.classify(
            before,
            facts["events"],
            container_running=running,
            duration_s=duration_s,
            timeout_s=timeout_s,
            kernel=kernel,
            container_id=getattr(self, "_cid", None),
        )
        events_before = None if before is None else dict(before)
        return {
            **container,
            **{k: v for k, v in facts.items() if k != "events"},
            "events_before": events_before,
            "events_after": facts["events"],
            "duration_s": duration_s,
            "timeout_s": timeout_s,
            "oom_kill_count_before": None if before is None else before.get("oom_kill"),
            "oom_killed": (
                True
                if cls["reason"] in ("memory_limit", "host_oom")
                else None
                if facts["events"] is None and container["docker_oom_killed"] is None
                else False
            ),
            **cls,
        }

    def _agent_processes(self) -> list[dict] | None:
        """Every process in the agent's container except its init and the init's `sleep`, from the host; [] when the container has stopped or gone, None when that cannot be read."""
        info = TD._inspect(self.name, TD.WAITS)
        if info is None:
            return []
        if info.get("unknown"):
            return None
        return TD._readers(info) if info["running"] else []

    def quiesce(self, reason: str = "end of run") -> dict:
        """Kill everything the agent left running, before any evidence is collected: a worker started with `start_new_session` is outside `timeout`'s group, and could go
        on computing and changing outputs after the cap. The container stays up, since its cgroup's counters classify the kills and M's mount has its own teardown in `stop`;
        only its init and the init's `sleep` remain. `killed` None: the processes could not be listed, so nothing is known. `left`: what had not gone 10 s after SIGKILL
        (see `quiet`). It never raises."""
        state = {"reason": reason, "killed": None, "left": None}
        try:
            killed, left, seen = 0, [], set()
            for _ in range(QUIESCE_ROUNDS):
                procs = self._agent_processes()
                if procs is None:
                    killed = None
                    break
                left = procs
                fresh = [p for p in procs if (p["pid"], p["start"]) not in seen]
                if not fresh:
                    break
                H.sudo_bounded("kill", "-s", "KILL", *[str(p["pid"]) for p in fresh], timeout_s=20.0)
                seen.update((p["pid"], p["start"]) for p in fresh)
                killed += len(fresh)
                left = TD._wait_gone(fresh, 10.0, 0.2)
            else:
                left = self._agent_processes()
                killed = None if left is None else killed
            if killed is not None:
                state.update(killed=killed, left=[{k: p[k] for k in ("pid", "comm", "state")} for p in left[:16]])
        except Exception as e:
            state.update(killed=None, left=None, error=repr(e)[:200])
        self._meta.setdefault("quiesced", []).append(state)
        return state

    @staticmethod
    def quiet(state: dict) -> bool:
        """Nothing of the agent's can run any more. A process in D (a read of a mount that has gone slow) or a zombie has SIGKILL pending or is dead: it returns to no user code,
        so it changes nothing, and counting it would retry only M's runs at their cap."""
        return state["killed"] is not None and not [p for p in state["left"] if p["state"] not in ("D", "Z")]

    def exec(self, code: str, timeout_s: float, *, _record: bool = True) -> ExecResult:
        """Run Python source in the container as the unprivileged user (cwd /work). The only time limit is `timeout_s`, which the
        caller sets (the harness: the run's remaining time cap); there is no default, so no hidden per-cell limit can creep back.
        Its clock starts here, not when Docker gets round to starting the process, so what comes before the launch does not extend the allowance. When it runs out,
        every process of the agent's container is killed, a detached worker included, whether or not it held the cell's pipes; the container and the run's evidence stay.

        One cause per cell, whatever the exit code (killcause.py has the precedence): the cgroup's counters are read after every cell, because a killed worker may leave
        a parent that exits 1, or 0; earlyoom's journal likewise. A cell that printed bytes that are not UTF-8 is the agent's own outcome: they are replaced, never an exception."""
        if not timeout_s >= 1:
            raise ValueError("timeout_s must be at least 1 second (GNU timeout treats 0 as no limit)")
        entered = time.monotonic()
        before = (
            (self.__dict__.get("_events_seen") or self._memory_facts()["events"]) if _record else None
        )  # a kill between two cells is charged to the next
        up0 = self._uptime() if _record else None
        remaining = timeout_s - (time.monotonic() - entered)
        # the link between a cell and its start records (agreed with the tracer and the labeller): children inherit it, and the probes have none. In every run, traced or not.
        tag = ["-e", f"STUDY_VAR_37={self._cells + 1}"] if _record else []
        limit = f"{max(remaining, 1.0):.3f}"
        cmd = [
            "docker",
            "exec",
            "-i",
            *tag,
            "-u",
            f"{self.cfg.agent_uid}:{self.cfg.agent_uid}",
            "-w",
            "/work",
            self.name,
            "timeout",
            "-s",
            "KILL",
            limit,
            "python",
            "-",
        ]
        t0, wall0 = time.monotonic(), time.time()
        pre_exec = t0 - entered
        cap = threading.Timer(max(0.0, remaining) + CAP_MARGIN_S, self._kill_at_the_cap)
        cap.daemon = True
        started = gave_up = False
        try:
            if remaining < 1:  # used up before it could start
                rc, out, err, gave_up = 124, "", "the cell was not started: its time ran out during preparation", True
            else:
                if _record:  # the probes kill nothing of the agent's
                    cap.start()
                    started = True
                try:
                    p = subprocess.run(
                        cmd,
                        input=code,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=remaining + 30,
                    )
                    rc, out, err = p.returncode, p.stdout, p.stderr
                except subprocess.TimeoutExpired as e:
                    rc, out, err, gave_up = (
                        124,
                        (e.stdout or b"").decode("utf-8", "replace")
                        if isinstance(e.stdout, bytes)
                        else (e.stdout or ""),
                        "docker exec did not return",
                        True,
                    )
            used = time.monotonic() - entered
            # 124 is the harness's own only when it says so: `timeout -s KILL` exits 137, so otherwise the agent's code chose it
            res = ExecResult(
                out,
                err,
                rc,
                time.monotonic() - t0,
                timed_out=gave_up or (rc in (137, -9) and used >= timeout_s - KC.AT_THE_LIMIT_S),
            )
            if _record:
                self._record_cell(code, res, before, up0, used, pre_exec, timeout_s, gave_up, wall0, limit)
        finally:  # past the post-processing: a worker that outlived its cell must not go on computing through a slow journal read at the cap
            if started:
                cap.cancel()
        return res

    def _record_cell(
        self,
        code: str,
        res: ExecResult,
        before: dict | None,
        up0: float | None,
        used: float,
        pre_exec: float,
        timeout_s: float,
        gave_up: bool,
        wall0: float,
        limit: str,
    ) -> None:
        rc, err, dur = res.exit_code, res.stderr, res.duration_s
        self._cells += 1
        entry = {
            "cell": self._cells,
            "code": code,
            "exit_code": rc,
            "duration_s": dur,
            "pre_exec_s": round(pre_exec, 3),
            "timed_out": res.timed_out,
        }
        try:
            events = self._memory_facts()["events"]
        except Exception:
            events = None
        killed = (events or {}).get("oom_kill", 0) > (before or {}).get("oom_kill", 0)
        if (
            events is None
        ):  # not known whether anything of this cell was killed: the run's end routes it, and the agent's result stays as it is
            self._meta.setdefault("oom_counters_unreadable_cells", []).append(self._cells)
        # a kill or a timeout, an OOM kill of any process of the container, or a failure that looks like a dead sandbox (docker's own 125/126, or its "not running" text):
        # record why, now, before anything is torn down
        # the `docker exec` client itself died: a signal kills it (or it exits 0 on SIGTERM, Docker 29.1), and neither reaches the process in the container, which goes on holding its memory
        client_lost = rc < 0 or (not gave_up and self._cell_still_running(limit))
        if client_lost:
            self.quiesce("the docker client was killed")
        if gave_up or rc in (137, -9) or client_lost or killed or _docker_failed(rc, err):
            # when the harness gave up, the cell had all the time it could have: `used` may be less, with no time left to start it in, and that must not read as a kill nothing explains
            entry["kill"] = res.kill = self._kill_facts(before, timeout_s if gave_up else used, timeout_s, up0)
            self._meta.setdefault("cell_kills", []).append({"cell": self._cells, **entry["kill"]})
            self._meta["oom_killed"] = bool(self._meta.get("oom_killed")) or entry["kill"]["oom_killed"] is True
        self._events_seen = events
        # whatever the exit code: earlyoom's victim may be another container of the run, and the cell may survive that
        try:
            ek = self._earlyoom_scan()
        except Exception as e:
            ek = []
            self._meta["earlyoom_log_readable"] = False
            self._meta["earlyoom_scan_error"] = repr(e)[:200]
        new = ek[self.__dict__.get("_earlyoom_seen", 0) :]
        if not new and (rc in SIGNAL_EXITS or client_lost):
            new = [
                k
                for k in EO.unsampled_kills(
                    self.__dict__.get("_journal_kills", []), ek, wall0, time.time(), self.cfg.agent_uid
                )
                if k["pid"] not in _SAMPLED_PIDS
            ]
            if (
                not new and client_lost
            ):  # earlyoom's victim may be the client: it has the driver's uid, and no container's cgroup
                new = [
                    k
                    for k in EO.unsampled_kills(
                        self.__dict__.get("_journal_kills", []), ek, wall0, time.time(), os.getuid()
                    )
                    if k["comm"] == "docker"
                ]
            if new:
                self._meta["earlyoom_kills"] = ek = [*ek, *new]
        if new:
            self._earlyoom_seen = len(ek)
            facts = entry.get("kill") or self._kill_facts(before, timeout_s if gave_up else used, timeout_s, up0)
            entry["kill"] = res.kill = {**facts, **KC.earlyoom_cause(new), "oom_killed": True}
            if self._meta.get("cell_kills") and self._meta["cell_kills"][-1]["cell"] == self._cells:
                self._meta["cell_kills"][-1] = {"cell": self._cells, **entry["kill"]}
            else:
                self._meta.setdefault("cell_kills", []).append({"cell": self._cells, **entry["kill"]})
            self._meta["oom_killed"] = True
        if res.kill:  # from what caused it, not from an exit code that a memory kill shares
            res.timed_out = entry["timed_out"] = gave_up or res.kill["reason"] == "time_limit"
        # a Python process ran to its own end, or was killed long after it began, so the tracer must have started in it. One that never launched, or was killed at once, may have died before the import
        entry["expects_start"] = (
            not gave_up and rc not in (125, 126, 127) and (rc not in SIGNAL_EXITS or used >= START_WITHIN_S)
        )
        if res.timed_out:  # now, and not only from the timer: a worker that closed the cell's pipes let `docker exec` return, and must not run on until the run's end
            self.quiesce("time cap")
        self._cell_log.append(entry)

    def _cell_still_running(self, limit: str) -> bool:
        """This cell's own `timeout` is still in the container, though `docker exec` has returned: its exit never lets that happen. So the client went, and the exit code says nothing.
        Matched on its whole command line, with this cell's limit, so an agent's own `timeout` is never taken for it. Host reads only."""
        want = [b"timeout", b"-s", b"KILL", limit.encode(), b"python", b"-"]
        try:
            pids = (self._cgroup_dir() / "cgroup.procs").read_text().split()
        except Exception:
            return False
        for pid in pids:
            try:
                if Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:-1] == want:
                    return True
            except OSError:
                continue
        return False

    def _kill_at_the_cap(self) -> None:
        """In a thread, when a cell's time runs out: `timeout` inside the container ends the cell's own process group, and this ends the rest. It never raises."""
        try:
            self.quiesce("time cap")
        except Exception as e:
            log.error("sandbox %s: killing its processes at the cap failed: %r", self.name, e)

    def run_shell(self, command: str, timeout_s: float = 60) -> ExecResult:
        """Run a shell command as the unprivileged user (used by the enforcement tests)."""
        cmd = [
            "docker",
            "exec",
            "-u",
            f"{self.cfg.agent_uid}:{self.cfg.agent_uid}",
            "-w",
            "/work",
            self.name,
            "timeout",
            "-s",
            "KILL",
            str(int(timeout_s)),
            "sh",
            "-c",
            command,
        ]
        t0 = time.monotonic()
        p = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_s + 30
        )
        return ExecResult(p.stdout, p.stderr, p.returncode, time.monotonic() - t0)

    def collect(self, dest_dir: str | Path) -> dict:
        """Stop whatever of the agent's still runs, then copy out the run's evidence. A copy that fails raises, and the sandbox's disks are then kept when it stops: a source is
        never deleted after a failed copy. So does an agent that could not be shown stopped: its outputs may still be changing."""
        try:
            state = self.quiesce("before collection")
            if not self.quiet(state):
                raise H.SandboxError(
                    f"the agent's processes could not be shown stopped before collection: {state}"[:500]
                )
            return self._collect(dest_dir)
        except BaseException:
            self.keep_evidence = True
            raise

    def _collect(self, dest_dir: str | Path) -> dict:
        """Copy out output/report.json, output/dataset-3.nc, the code cells, the trace dir and the gate log."""
        dest = Path(dest_dir)
        (dest / "output").mkdir(parents=True, exist_ok=True)
        got = {}
        for f in ("report.json", "dataset-3.nc"):
            r = H.docker("cp", f"{self.name}:/work/output/{f}", str(dest / "output" / f), check=False)
            got[f] = r.returncode == 0
            if r.returncode != 0 and not re.search(
                r"could not find the file|no such file or directory", r.stderr, re.I
            ):  # absent is the agent's outcome; a failed copy, or "No such container", is not
                raise H.SandboxError(f"copying {f} failed: {r.stderr.strip()[:200]}")
        (dest / "cells").mkdir(exist_ok=True)
        for c in self._cell_log:
            (dest / "cells" / f"cell_{c['cell']:03d}.py").write_text(
                c["code"], encoding="utf-8", errors="surrogatepass"
            )  # an agent's string, from JSON, may hold one
        (dest / "cells" / "cells.jsonl").write_text("".join(json.dumps(c) + "\n" for c in self._cell_log))
        # (docker cp cannot read tmpfs mounts, so /trace is a quota disk we can read from the host)
        (dest / "trace").mkdir(exist_ok=True)
        # checked: an empty collected trace would label a correctly computed run `omitting`, with zero coverage
        for what, r in (
            ("copying the trace", H.sudo("cp", "-r", f"{self.dir / 'trace'}/.", str(dest / "trace"), check=False)),
            (
                "owning the copied trace",
                H.sudo("chown", "-R", f"{os.getuid()}:{os.getgid()}", str(dest / "trace"), check=False),
            ),
        ):
            if r.returncode != 0:
                raise H.SandboxError(f"{what} failed: {r.stderr.strip()[:200]}")
        held = (self._meta.get("instrument") or {}).get("trace_files")
        if held and not any(
            f.is_file() and f.stat().st_size for f in (dest / "trace").rglob("*")
        ):  # the copy said it worked: the record says otherwise
            raise H.SandboxError(
                f"the trace held {held} file(s) with content at the end of the run, and none came across"
            )
        got["trace"] = True
        self.observe_io_mode()
        text = None
        try:  # oneclient's own log (token redacted), next to the run's other outputs. Not readable: `sidecar_errors` says unavailable, which the harness routes
            text = self.sidecar_log_text()
        except Exception as e:
            got["oneclient_log"] = False
            got["oneclient_log_error"] = repr(e)[:200]
        if text is not None:
            (dest / "oneclient_log").mkdir(exist_ok=True)
            tok = self.dc.oneclient_token
            (dest / "oneclient_log" / "oneclient.INFO").write_text(text.replace(tok, "<token>") if tok else text)
            got["oneclient_log"] = True
        pl = self.dir / "perflog"
        if pl.exists():  # oneclient's read/write performance CSV (the option is off by default)
            time.sleep(
                PERF_FLUSH_WAIT_S
            )  # the logger flushes about every 4 s: wait it out so the last rows are in the file
            (dest / "oneclient_perf").mkdir(exist_ok=True)
            files = sorted(
                H.sudo("find", str(pl), "-maxdepth", "1", "-name", "read_write_perf-*.csv", check=False).stdout.split(
                    "\n"
                )
            )
            for r in [H.sudo("cp", f, str(dest / "oneclient_perf"), check=False) for f in files if f] + [
                H.sudo("chown", "-R", f"{os.getuid()}:{os.getgid()}", str(dest / "oneclient_perf"), check=False)
            ]:  # argument lists, no shell: paths may contain spaces
                if r.returncode != 0:
                    raise H.SandboxError(f"copying oneclient's perf log failed: {r.stderr.strip()[:200]}")
            got["oneclient_perf"] = bool(list((dest / "oneclient_perf").glob("read_write_perf-*.csv")))
        self._stop_gate_for_collection()  # last before the log: the gate writes its teardown lines as it exits (the agent is stopped and every other copy is made)
        gl = self.dir / "gatelog" / "requests.jsonl"
        if gl.exists():
            shutil.copy(gl, dest / "gate_requests.jsonl")
        got["gate_log"] = gl.exists()
        if (
            "gate_stop" in self._meta
        ):  # the REST options: how the stop went, for the callers whose metadata() came before collect (the harness) or who keep only this (faults)
            got["gate_stop"] = dict(self._meta["gate_stop"])
        return got

    IO_PROBE = (
        "import json, os, sys\n"
        "root, n = sys.argv[1], int(sys.argv[2])\n"
        "paths = [root] if not os.path.isdir(root) else [e.path for _, e in zip(range(n), os.scandir(root))]\n"
        "cnt = {}\n"
        "for p in paths:\n"
        "    try:\n"
        "        v = os.getxattr(p, 'org.onedata.access_type').decode()\n"
        "    except OSError:\n"
        "        v = 'error'\n"
        "    cnt[v] = cnt.get(v, 0) + 1\n"
        "print(json.dumps(cnt))\n"
    )

    def observe_io_mode(self, sample: int = 200) -> str | None:
        """M/M+: the I/O mode oneclient actually selected, read from the xattr org.onedata.access_type of the
        dataset files (a file reads "unknown" until it has been read once, so this is called after the run;
        `collect` does). Recorded in metadata as io_mode_observed (+ the counts); the mode is not forced."""
        if self.option.value in REST_OPTIONS or not self.dc.mount_path:
            return None
        loc = self._meta["dataset_location"].replace(self.dc.mount_path.rstrip("/"), "/mnt/oneclient", 1)
        r = H.docker("exec", self.name + "-oneclient", "python3", "-c", self.IO_PROBE, loc, str(sample), check=False)
        if r.returncode != 0:  # e.g. the stand-in mount of the local tests has no sidecar
            self._meta["io_mode_observed"] = None
            return None
        counts = json.loads(r.stdout.strip().splitlines()[-1])
        seen = [k for k in counts if k in ("proxy", "direct")]
        self._meta["io_observed_after_cells"] = self._cells
        self._meta["io_mode_counts"] = counts
        self._meta["io_mode_observed"] = seen[0] if len(seen) == 1 else ("mixed" if seen else "unknown")
        return self._meta["io_mode_observed"]

    ONECLIENT_DEFAULT_LOG_DIR = "/tmp/oneclient/0"

    def sidecar_log_text(self) -> str | None:
        """oneclient's own log (INFO holds every level) read from outside the agent; None when there is no sidecar
        (REST options, the stand-in mount); raises SandboxError when the sidecar exists but its log cannot be read."""
        if self._meta.get("mount_kind") != "oneclient":
            return None
        logdir = self._meta.get("oneclient_log_dir") or self.ONECLIENT_DEFAULT_LOG_DIR
        r = H._docker_t("exec", self.name + "-oneclient", "cat", f"{logdir}/oneclient.INFO", timeout=60)
        if r.returncode != 0:
            raise H.SandboxError(f"cannot read the oneclient log: {r.stderr.strip()[:200]}")
        return r.stdout

    def _sidecar_log_checked(self) -> tuple[str | None, str | None]:
        """(token-free log text, None), (None, None) when there is no sidecar, or (None, reason) when it cannot be read."""
        try:
            text = self.sidecar_log_text()
        except Exception as e:
            msg = repr(e)
            return None, (msg.replace(self.dc.oneclient_token, "<token>") if self.dc.oneclient_token else msg)[:200]
        if text is None:
            return None, None
        return (text.replace(self.dc.oneclient_token, "<token>") if self.dc.oneclient_token else text), None

    def sidecar_errors(self) -> dict | None:
        """Warnings and errors of the oneclient sidecar by origin (see oneclient_log), or {"unavailable": reason}: an
        unreadable log is reported as unknown, never as zero errors. None when the option has no sidecar."""
        text, reason = self._sidecar_log_checked()
        if reason is not None:
            return {"unavailable": reason}
        return None if text is None else summarize_oneclient_log(text)

    def m_setup(self) -> dict:
        """M's fixed per-run set-up cost, measured and never subtracted from any wall time. `mount_wait_s`: sidecar start to the
        FUSE mount appearing (time.monotonic). `storage_detection_s`: oneclient's own storage access type detection, which runs at
        the first file access and is read from its log (see oneclient_log.storage_detection). Each is None with a reason, never 0."""
        mw = getattr(self, "_mount_wait_s", None)
        text, reason = self._sidecar_log_checked()
        if text is not None:
            det = oneclient_storage_detection(text)
        else:
            why = f"oneclient log unavailable: {reason}" if reason else "stub mount: no oneclient sidecar"
            det = {"storage_detection_s": None, "none_reason": why}
        return {
            "mount_wait_s": mw,
            "mount_wait_none_reason": None if mw is not None else "stub mount: no oneclient sidecar",
            "storage_detection_s": det["storage_detection_s"],
            "storage_detection_none_reason": det["none_reason"],
            "storage_detection": det,
        }

    def trace_usage(self) -> dict:
        """How much of the /trace volume the run used, from the host, while the volume is still mounted (the runner reads it after the agent loop and before collect and stop; science,
        #34, 18:31Z). `trace_used_bytes` = (f_blocks - f_bfree) * f_frsize of the volume, `trace_size_bytes` = f_blocks * f_frsize. None for both, with `trace_usage_reason`, where it
        cannot be said: never 0, never the host's own filesystem (a directory that lost its mount answers statvfs with the numbers of the disk under it, so it must be a mount and the
        size of the volume made). It never raises."""
        trace = self.dir / "trace"
        want = self.cfg.limits.agent_trace_bytes

        def unknown(why: str) -> dict:
            return {"trace_used_bytes": None, "trace_size_bytes": None, "trace_usage_reason": why[:300]}

        if self.__dict__.get("_stopped", True):
            return unknown("the sandbox was stopped: /trace is no longer mounted")
        try:
            if _device_of(trace) == _device_of(trace.parent):
                return unknown(f"{trace} is not a mount: its numbers would be the host's")
            v = os.statvfs(trace)
        except OSError as e:
            return unknown(f"statvfs of {trace} failed: {e}")
        size, used = v.f_blocks * v.f_frsize, (v.f_blocks - v.f_bfree) * v.f_frsize
        if (
            not 0.9 * want <= size <= want
        ):  # the ext4 overhead takes a little of the image; much less, or more, is another filesystem
            return unknown(f"the filesystem at {trace} is {size} B, which does not match the {want} B volume made")
        return {"trace_used_bytes": used, "trace_size_bytes": size, "trace_usage_reason": None}

    def record(self, **fields) -> None:
        """Add fields to the run record (additive; used by the factory for token records and the pinned image id)."""
        clash = set(fields) & set(self._meta)
        if clash:
            raise ValueError(f"metadata fields already set: {sorted(clash)}")
        self._meta.update(fields)

    def instrument_state(self) -> dict:
        """From the host, at the end of a run: the instrument's error log (agent code cannot stop it being read here), and how many files of its trace hold anything. None where
        that could not be read, with `state_error`: an error log that is there and cannot be read counts as an error, never as none."""
        trace = self.dir / "trace"
        found = H.sudo_bounded("find", str(trace), "-type", "f", "-printf", "%s %P\n", timeout_s=20.0)
        if found.returncode != 0:
            return {
                "error_at_end": None,
                "trace_files": None,
                "start_markers": None,
                "state_error": f"the trace could not be listed: {found.stderr.strip()[:200]}",
            }
        files = [line.split(" ", 1) for line in found.stdout.splitlines() if " " in line]
        names = [name for size, name in files if size != "0"]
        # one per process the tracer started in: empty files. Not those of the harness's own probes at the start, which would pass for cells
        markers = sum(
            name.startswith("ok.") and name not in self.__dict__.get("_markers_at_start", ()) for _, name in files
        )
        error = None
        if ERROR_LOG in names:
            r = H.sudo_bounded("cat", str(trace / ERROR_LOG), timeout_s=20.0)
            error = (
                r.stdout[-500:]
                if r.returncode == 0 and r.stdout
                else f"the instrument's error log could not be read: {r.stderr.strip()[:200]}"
            )
        return {
            "error_at_end": error,
            "trace_files": sum(n != ERROR_LOG for n in names),
            "start_markers": markers,
            **self._loaded_records(trace, [n for _, n in files]),
        }

    @staticmethod
    def _loaded_records(trace: Path, names: list[str]) -> dict:
        """The tracer's `instrument-loaded.<pid>.json`, each one whole: how many, and the first that does not parse or says `ok: false`. One per interpreter at most, and not one per
        process at least: a helper killed during the install has none, and ran no agent code."""
        recs = [n for n in names if n.startswith("instrument-loaded.") and n.endswith(".json")]
        bad = None
        for n in recs:
            r = H.sudo_bounded("cat", str(trace / n), timeout_s=20.0)
            try:
                ok = json.loads(r.stdout)["ok"] is True if r.returncode == 0 else False
            except (ValueError, KeyError, TypeError):
                ok = False
            if not ok:
                bad = f"{n}: {r.stdout.strip()[:200] or r.stderr.strip()[:200]}"
                break
        return {"loaded_records": len(recs), "loaded_record_bad": bad, "loaded_record_required": LOADED_RECORD_REQUIRED}

    def _read_byte_counters(self) -> dict:
        """Each container's own network-namespace counters (scripted/metrics.py: "Nothing is derived from another counter"), each read on its own: None for one that cannot be read."""
        from dataset_76.scripted import metrics as M

        gate, side = self.name + "-gate", self.name + "-oneclient"
        read = {
            "agent_eth0": lambda: M.iface_counters(self.name)["eth0"],
            "gate_upstream": lambda: M.iface_by_mac(M.iface_counters(gate), M.bridge_mac(gate)),
        }
        if self._meta.get("mount_kind") == "oneclient":
            read["sidecar_bridge"] = lambda: M.iface_by_mac(M.iface_counters(side), M.bridge_mac(side))
        out: dict = {}
        for name, fn in read.items():
            try:
                out[name] = fn()
            except Exception as e:
                out[name] = None
                log.warning("sandbox %s: the %s byte counter could not be read: %r", self.name, name, e)
        return out

    def bytes_moved(self) -> dict:
        """What the run moved, in the names the scripted runs use. `between_sites_rx` (science's ruling, 2 Nov): the gate's upstream rx under T, condition_a and D, the sidecar's rx under
        M, the two places traffic crosses to the provider; `registered_rx` (parameters.md, a sensitivity): the agent's `eth0` under T, condition_a and D, downstream of the gate, and the
        sidecar's under M. Each counts everything on its wire, framing and listing calls included (results/bytes-counters). None where not known, never 0: no retry. It never raises."""
        start, now = self.__dict__.get("_bytes_at_start") or {}, self._read_byte_counters() if not self._stopped else {}
        out: dict = {}
        for name in set(start) | set(now):
            a, b = start.get(name), now.get(name)
            d = None if a is None or b is None else {"rx": b["rx"] - a["rx"], "tx": b["tx"] - a["tx"]}
            out[name] = (
                None if d is None or d["rx"] < 0 or d["tx"] < 0 else d
            )  # less than at the start: a container that restarted, so the count is lost
        rx = lambda name: (out.get(name) or {}).get("rx")
        mount = self.option.value not in REST_OPTIONS
        return {
            **out,
            "between_sites_rx": rx("sidecar_bridge") if mount else rx("gate_upstream"),
            "registered_rx": rx("sidecar_bridge") if mount else rx("agent_eth0"),
        }

    def _note_markers_at_start(self) -> None:
        if (self._meta.get("instrument") or {}).get("configured"):
            r = H.sudo_bounded(
                "find", str(self.dir / "trace"), "-maxdepth", "1", "-name", "ok.*", "-printf", "%P\n", timeout_s=20.0
            )
            if r.returncode != 0:
                raise H.SandboxError(f"the trace could not be listed at the start: {r.stderr.strip()[:200]}")
            self._markers_at_start = frozenset(r.stdout.split())

    def _cells_completed(self) -> int:
        """Cells that ran to their own end. Each was a Python process this harness launched, so each must have left a start marker; a kill may come before the tracer's import has."""
        return sum(bool(c.get("expects_start")) for c in self._cell_log)

    def _stop_gate_for_collection(self) -> None:
        """SIGTERM the gate and wait up to GATE_STOP_WAIT_S for it to exit, so that it writes its `teardown` close line for every client connection still open (gate.py `teardown`)
        before its log is copied. `collect` calls it after the agent is stopped and every other copy is made, just before the gate log's: `docker rm -f` in `stop` is a SIGKILL, which
        leaves no line. Only the REST options, whose gate carries the agent's requests. Never raises, and never costs the collection: whatever happens is recorded in `gate_stop`.

        A gate that has exited because we stopped it must not read as one that died (science's ruling of 2 Nov, #2): `infra_container_check`, `_gate_log_problem` and `container_stats`
        would find an exited container, and the scripted runner and the confirmation take `metadata()` after `collect`. So the gate's state is read first, while it runs, and kept
        (`_gate_frozen`) for those readers, but only if it then exits with code 0, what its SIGTERM handler gives, with status `exited`, and with StartedAt, RestartCount and id as in the
        snapshot (a restart between the snapshot and the signal, whose replacement exits 0, is not hidden). The kept log check is the one made before the signal, joined by the check
        for a write failure, run again on the exited container: a teardown line that could not be written is printed as GATE LOG FAILED and the gate still exits 0. A gate that was not
        running before, one that ends any other way, and one that does not exit in time are left as they are for the checks to find. Each docker call of the wait is bounded by the time
        left, and never by less than 0.5 s. An old gate image, whose PID 1 ignores SIGTERM, costs the wait and writes no teardown line."""
        if self.__dict__.get("_gate_stop_done") or self.option.value not in REST_OPTIONS:
            return
        self._gate_stop_done = True
        gate = self.name + "-gate"
        note = self._meta["gate_stop"] = {"signalled": False}
        try:
            end, why = self._infra_read(gate)
            if end is None or end["status"] != "running" or end["restarting"]:
                note["skipped"] = f"the gate was not running ({why or end['status']})"
                return
            frozen = {
                "end": end,
                "log_problem": self._gate_log_problem(),
                "stats": self._cgroup_totals(self._other_cgroup_dir(gate)),
            }
            r = H._docker_t("kill", "-s", "TERM", gate)
            if r.returncode != 0:
                note["error"] = f"docker kill exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}"
                return
            note["signalled"] = True
            t0 = time.monotonic()
            after = None
            while True:
                left = GATE_STOP_WAIT_S - (time.monotonic() - t0)
                st, _ = self._infra_read(
                    gate, timeout=max(GATE_STOP_CALL_MIN_S, left)
                )  # `_docker_t`'s 30 s would be the wait's, not 3
                if st is not None and st["status"] != "running":
                    after = st
                    break
                if time.monotonic() - t0 >= GATE_STOP_WAIT_S:
                    break
                time.sleep(0.1)
            note["waited_s"] = round(time.monotonic() - t0, 2)
            note["exited"] = after is not None
            if after is not None:
                note["exit_code"] = after["exit_code"]
                kept = (
                    after["exit_code"] == 0
                    and after["status"] == "exited"
                    and after["id"] == end["id"]
                    and not after["oom_killed"]
                    and after["started_at"] == end["started_at"]
                    and after["restart_count"] == end["restart_count"]
                )
                note["kept"] = kept
                if kept:
                    frozen["log_problem"] = frozen["log_problem"] or self._gate_output_problem(
                        gate, timeout=GATE_STOP_LOGS_S
                    )  # a write failure while it stopped
                    self._gate_frozen = frozen
                    note["log_problem"] = frozen[
                        "log_problem"
                    ]  # in the note too: a caller whose metadata() came before collect never reads `_gate_frozen`
        except Exception as e:
            note["error"] = repr(e)[:200]

    def _gate_log_problem(self) -> str | None:
        """Why this gate's log cannot stand for the whole run: a gate that died, or was restarted, leaves a log that stops short; one that could not write logs it to its stderr
        and carries on (`_log`). None: it ran throughout and wrote everything. Only while the containers are up. A gate that `collect` stopped with SIGTERM and that exited with
        code 0 is read as it was when the signal was sent (`_stop_gate_for_collection`)."""
        fz = self.__dict__.get("_gate_frozen")
        if fz is not None:
            return fz["log_problem"]
        gate = self.name + "-gate"
        r = H._docker_t("inspect", "-f", "{{.State.Running}}|{{.State.ExitCode}}|{{.RestartCount}}", gate)
        st = r.stdout.strip().split("|")
        if r.returncode != 0 or len(st) != 3:
            return f"the gate's state could not be read: {r.stderr.strip()[:200]}"
        if st[0] != "true" or st[2] != "0":
            return f"the gate did not run throughout: running={st[0]}, exit code {st[1]}, restarts {st[2]}"
        return self._gate_output_problem(gate)

    @staticmethod
    def _gate_output_problem(gate: str, timeout: float = 30) -> str | None:
        """The part of `_gate_log_problem` that finds a write failure: the gate's own output, which says GATE LOG FAILED when `_log` could not write a line (and went on). Not readable
        is a problem too: not known is not clean. Also run on a gate that has exited, which is where a failed teardown line shows."""
        r = H._docker_t("logs", gate, timeout=timeout)
        if r.returncode != 0:
            return f"the gate's own output could not be read: {r.stderr.strip()[:200]}"
        if "GATE LOG FAILED" in r.stdout + r.stderr:
            return "the gate could not write its log"
        return None

    def _note_kill_after_the_last_cell(self, events: dict | None) -> None:
        """An OOM kill with no later cell to charge it to: a detached worker that outlived the last cell. Between two cells, the next cell's before-and-after finds it."""
        seen = self.__dict__.get("_events_seen") or {"oom": 0, "oom_kill": 0}
        if events is None or events.get("oom_kill", 0) <= seen.get("oom_kill", 0):
            return
        kill = self._kill_facts(seen, 0.0, 1.0)
        self._meta.setdefault("cell_kills", []).append({"cell": self._cells, "after_last_cell": True, **kill})
        self._meta["oom_killed"] = bool(self._meta.get("oom_killed")) or kill["oom_killed"] is True
        self._events_seen = events

    def metadata(self) -> dict:
        if not getattr(self, "_stopped", True):
            if (self._meta.get("instrument") or {}).get("configured"):
                try:
                    self._meta["instrument"].update(
                        self.instrument_state(), cells=self._cells, cells_completed=self._cells_completed()
                    )
                except Exception as e:
                    self._meta["instrument"].update(error_at_end=None, trace_files=None, state_error=repr(e)[:200])
            try:
                self._earlyoom_scan()  # also what hit the run between cells
            except Exception as e:
                self._meta["earlyoom_log_readable"] = False
                self._meta["earlyoom_scan_error"] = repr(e)[:200]
        # the harness takes this snapshot before collect(): observe the I/O mode now, after the cells that have run
        # (before the first read a file's mode is "unknown"), and again whenever more cells have run since
        if (
            self._cells
            and not self._stopped
            and self._meta.get("io_observed_after_cells") != self._cells
            and self.option.value not in REST_OPTIONS
            and self._meta.get("mount_kind") == "oneclient"
        ):
            self.observe_io_mode()
        if (
            not self._stopped
        ):  # the container's own counters, read before teardown: an OOM kill of any process is recorded
            mem = self._memory_facts()
            self._meta["memory_peak_bytes"], self._meta["memory_max_bytes"] = (
                mem["memory_peak_bytes"],
                mem["memory_max_bytes"],
            )
            self._meta["oom_kill_count"] = mem["oom_kill_count"]
            self._meta["memory_events"] = mem["events"]
            self._note_kill_after_the_last_cell(mem["events"])
            try:
                self._meta["containers"] = self.container_stats()
            except Exception as e:
                self._meta["containers"] = {"error": repr(e)[:200]}
            try:  # a gate or sidecar that died, restarted or was OOM-killed: infrastructure, whatever the agent saw (the harness routes `infra_death`)
                chk = self.infra_container_check()
                self._meta["infra_containers"], self._meta["container_deaths"] = chk["containers"], chk["deaths"]
                self._meta["infra_death"], self._meta["infra_death_cause"] = chk["death"], chk["death_cause"]
            except Exception as e:
                msg = f"the state of the gate and the sidecar could not be checked: {e!r}"[:300]
                self._meta["infra_containers"] = {"error": repr(e)[:200]}
                self._meta["container_deaths"] = [
                    {
                        "role": "gate or sidecar",
                        "container": None,
                        "option": self.option.value,
                        "cause": ID.UNDETERMINED,
                        "category": "infrastructure",
                        "basis": "the check itself failed",
                        "problems": [msg],
                    }
                ]
                self._meta["infra_death"], self._meta["infra_death_cause"] = [msg], ID.UNDETERMINED
            try:
                self._sample_memory()  # a last sample, so the maximum includes the end of the run
            except Exception:
                pass
            ap = getattr(self, "_anon_peak", -1)
            self._meta["memory_anon_peak_bytes"] = ap if ap >= 0 else None
            self._meta["memory_stat_at_anon_peak"] = getattr(self, "_stat_at_anon_peak", None)
            self._meta["memory_peak_note"] = (
                "memory_peak_bytes is the cgroup high-water mark and includes file cache and tmpfs; "
                "memory_anon_peak_bytes is the maximum of sampled anon+shmem (1 s period)"
            )
            if mem["oom_kill_count"]:
                self._meta["oom_killed"] = True
            self._meta.setdefault("oom_killed", False if mem["oom_kill_count"] is not None else None)
        meta = json.loads(json.dumps(self._meta, default=str))
        gl = self.dir / "gatelog" / "requests.jsonl"
        if self.option.value not in REST_OPTIONS:
            meta["gate_transfers"] = gate_log_unknown("a mount option has no REST data path")
        else:
            try:
                problem = None if self._stopped else self._gate_log_problem()
                meta["gate_transfers"] = (
                    gate_log_unknown(problem) if problem else summarize_gate_log(gl.read_text().splitlines())
                )
            except OSError as e:
                meta["gate_transfers"] = gate_log_unknown(f"the gate's log could not be read: {type(e).__name__}")
        if self.option.value not in REST_OPTIONS:
            se = self.sidecar_errors()
            meta["sidecar_errors"] = se
            # True/False only when the log was read; "agent_saw_sidecar_error" needs the agent's own errno record (the tracer)
            # errors the agent's own request caused (ENOENT, EROFS, EACCES, EPERM, EISDIR on the FUSE line) do not count
            meta["sidecar_error_seen"] = (
                (se["total"] - se["fuse_caused_by_agent"] > 0) if se and "total" in se else None
            )
            meta["agent_saw_sidecar_error"] = None
            meta["m_setup"] = self.m_setup()
        return meta

    def stop(self) -> None:
        # one teardown at a time: the scripted MemoryGuard stops the sandbox from its own thread while the main thread's
        # exec returns and its finally-block stops it too; the second caller waits, then finds it stopped. A SIGTERM cannot
        # cut it short (termination.py): that would leave what it exists to remove, and quarantine a slot for nothing
        with TERM.sigterm_deferred(), self.__dict__.setdefault("_stop_lock", threading.Lock()):
            self._stop_locked()

    def _stop_locked(self) -> None:
        if getattr(self, "_stopped", True):
            return
        errors: list[str] = []
        complete = False
        ev = getattr(self, "_sampler_stop", None)
        if ev is not None:
            ev.set()

        def step(fn, *a, **kw):  # every step runs whatever an earlier one raised (a timeout, a daemon error)
            try:
                fn(*a, **kw)
            except Exception as e:
                errors.append(f"{getattr(fn, '__name__', fn)}: {e!r}"[:300])

        try:
            held: set[str] = set()
            name = getattr(self, "name", None)
            side = f"{name}{TD.SIDECAR_SUFFIX}" if name else None
            if side is not None and side in self._containers:
                # a oneclient sidecar was started (or tried): M and M+, also after a failed start. The readers stop, the
                # FUSE mount goes, then the sidecar, then the agent; no container is killed with the mount live
                cfg = getattr(self, "cfg", None)
                lp = (
                    Path(cfg.work_root) / TD.TEARDOWN_LOG_DIR / f"{self.sid}.jsonl"
                    if cfg is not None and getattr(self, "sid", None)
                    else None
                )
                res = TD.ordered_fuse_teardown(
                    name if name in self._containers else None,
                    side,
                    self.dir / "mount",
                    waits=getattr(self, "teardown_waits", TD.WAITS),
                    log_path=lp,
                    extra_readers=list(self.__dict__.get("extra_reader_containers", ())),
                )
                self.teardown_log = res.log
                errors.extend(res.errors)
                if not res.fuse_safe:
                    held = {name, side}
            for c in reversed(self._containers):
                if c not in held:
                    step(H.remove_container, c)
            if held:  # the network and the mounts cannot go while those two live: all of it is left for a retry
                errors.append(f"left running with a FUSE mount that could not be released: {sorted(held)}")
            else:
                if self._net:
                    step(H.remove_network, self._net)  # a leaked network keeps its subnet
                for m in reversed(self._mounts):
                    step(H.unmount, m)
                if self.__dict__.get("keep_evidence"):  # the copy failed: the disks hold the only record of the run
                    kept = Path(self.cfg.work_root) / "_kept_evidence"
                    step(H.sudo_bounded, "mkdir", "-p", str(kept), timeout_s=20.0)
                    step(H.sudo_bounded, "mv", str(self.dir), str(kept / self.sid), timeout_s=60.0)
                    self._meta["evidence_kept_at"] = self.evidence_kept_at = str(kept / self.sid)
                    log.error("sandbox %s: a copy failed, its disks are kept at %s", self.name, kept / self.sid)
                else:
                    step(H.sudo_bounded, "rm", "-rf", "--one-file-system", str(self.dir), timeout_s=300.0)
                if self.dir.exists():  # verified, not assumed: a mount left behind keeps the directory
                    errors.append(f"run directory {self.dir} still exists")
            complete = not errors
        finally:
            # stopped only when everything is verifiably gone: an interrupt (Ctrl-C) or a failed step leaves it retryable
            self._stopped = complete
            if complete:
                TERM.unregister(self)
                _note_up(getattr(self, "run_key", ""), -1)
        if errors:
            note_unfinished(getattr(self, "run_key", ""), self.stop)
            raise H.SandboxError("sandbox cleanup incomplete: " + "; ".join(errors))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.stop()
