# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/netpool.py, blob 67c5ce2d1adecb72abd5dc7fe9f3a643bbfb36d4
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see the history of this file from its import commit
"""Sandbox networks from a dedicated address range, and the networks a killed process left behind (O14).

Docker's default address pools give a host about 30 networks in all, and every sandbox needs one: a few leaked
sandboxes and a campaign at concurrency 16 exhaust them ("all predefined address pools have been fully subnetted").
So each sandbox network gets an explicit small subnet from the pools `[sandbox.network]` names, each a network in strict
mode (checked when the file is loaded) and each checked against the host's routes before use. Explicit subnets never
draw on Docker's default pools.

Concurrent starts in different processes: each takes candidates in a random rotation of the range, skipping subnets
already in use, and lets Docker refuse an overlap (the daemon checks it atomically); a refusal moves on to the next
candidate, at most `max_attempts` times.

Exit paths: `stop` of a sandbox removes the network and every container it started or tried to start (normal end, an
exception, a failed start). A process killed outright cannot; so its networks and containers carry four labels, the
sandbox marker, the owner (boot id, PID namespace, pid and process start time), the run directory and the lease key,
and `reap_orphans` removes the containers and networks whose owner is provably gone: in this boot and this PID
namespace with no such process, or from an earlier boot (a mount sandbox's sidecar and agent in the ordered teardown,
see teardown.py). An owner it cannot judge (another namespace, an unreadable /proc) is left alone, and so is anything
without all of the first three labels. It deletes no files: a killed run's directory and loop mounts stay until its
owner's lane removes them.

Every host effect goes through the `HostOps` passed in; the process facts (boot id, start time, liveness) too.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import random
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.hostops import HostOps, describe_stderr, strict_networks

log = logging.getLogger(__name__)
_REAP_LOCK = (
    threading.Lock()
)  # a mount orphan's ordered teardown takes tens of seconds; starts run from several threads

DEFAULT_PREFIX = 28  # 16 addresses: the gate and the agent need two
MAX_ATTEMPTS = 64
OVERLAP = "Pool overlaps"


@dataclass(frozen=True)
class Labels:
    """The docker labels that mark a resource as one process's sandbox, in the deployment's label namespace."""

    namespace: str

    @property
    def sandbox(self) -> str:
        return f"{self.namespace}.sandbox"

    @property
    def owner(self) -> str:
        return f"{self.namespace}.owner"

    @property
    def dir(self) -> str:
        return f"{self.namespace}.dir"

    @property
    def lease(self) -> str:
        return f"{self.namespace}.lease"

    def args(self, host: HostOps, sandbox_dir: Path, lease_key: str | None = None) -> list[str]:
        """`docker run` / `docker network create` arguments that mark a resource as this process's sandbox."""
        pairs = [(self.sandbox, "1"), (self.owner, owner_id(host)), (self.dir, str(sandbox_dir))]
        if lease_key is not None:
            pairs.append((self.lease, lease_key))
        return [arg for key, value in pairs for arg in ("--label", f"{key}={value}")]

    def filters(self) -> list[str]:
        """`--filter` arguments that select resources carrying the first three labels."""
        return [
            "--filter",
            f"label={self.sandbox}=1",
            "--filter",
            f"label={self.owner}",
            "--filter",
            f"label={self.dir}",
        ]


# ------------------------------------------------------------------------------------------------ owners
def _start_time(host: HostOps, pid: int) -> str | None:
    """The process's start time in clock ticks since boot (/proc/<pid>/stat field 22): with the pid, it names one
    process, so a reused pid is not mistaken for the owner. None if there is no such process; an unreadable entry
    raises (unknown is not dead)."""
    try:
        stat = host.read_text(f"/proc/{pid}/stat")
    except FileNotFoundError:
        return None
    return stat.rsplit(")", 1)[1].split()[19]  # fields after the command name start at field 3


def _boot_id(host: HostOps) -> str:
    return host.read_text("/proc/sys/kernel/random/boot_id").strip()


def _pid_ns(host: HostOps) -> str:
    return host.readlink("/proc/self/ns/pid").strip("pid:[]")


def owner_id(host: HostOps, pid: int | None = None) -> str:
    """The identity of a process, stable for its life and never reused: boot, PID namespace, pid, start time."""
    pid = os.getpid() if pid is None else pid
    st = _start_time(host, pid)
    if st is None:
        raise SandboxError(f"no process {pid}")
    return f"{_boot_id(host)}/{_pid_ns(host)}/{pid}/{st}"


_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def owner_gone(host: HostOps, owner: str) -> bool:
    """True only when the owner is provably gone: an earlier boot, or this boot and PID namespace with no process of
    that pid (the kernel's ESRCH) or one with another start time. False for a live owner, another namespace, a malformed
    label, an unreadable or hidden /proc entry."""
    m = re.fullmatch(rf"({_UUID})/(\d+)/(\d+)/(\d+)", owner or "")
    if not m:
        return False
    boot, ns, pid, start = m.groups()
    try:
        if boot != _boot_id(host):
            return True  # no process outlives a reboot
        if ns != _pid_ns(host):
            return False  # its pids are not ours to look up
        seen = _start_time(host, int(pid))
        if seen is None:
            return host.process_alive(int(pid)) is False  # absent from /proc is not proof: only ESRCH is
        return seen != start
    except OSError:
        return False


def owner_alive(host: HostOps, owner: str) -> bool:
    return not owner_gone(host, owner)


# ------------------------------------------------------------------------------------------------ subnets
def subnets(pools: Sequence[str], prefix: int = DEFAULT_PREFIX) -> list[ipaddress.IPv4Network]:
    """Every subnet of the given size in the pools (each a strict network)."""
    return [sub for net in strict_networks(pools) for sub in net.subnets(new_prefix=prefix)]


def _json(text: str) -> list:
    try:
        data = json.loads(text or "null")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def docker_bridges(host: HostOps) -> set[str]:
    """The host interfaces of Docker's bridge networks: their routes are Docker's, which refuses overlaps among them."""
    ids = host.docker("network", "ls", "-q", "--filter", "driver=bridge", check=False).stdout.split()
    if not ids:
        return set()
    info = _json(host.docker("network", "inspect", *ids, check=False).stdout)
    return {
        (n.get("Options") or {}).get("com.docker.network.bridge.name") or "br-" + str(n.get("Id", ""))[:12]
        for n in info
    }


def route_overlaps(pool: str, route_text: str, ignore_devs: set[str] = frozenset()) -> list[str]:
    """Routes (from `ip -4 route show table all`) whose destination overlaps the pool, other than those on the given
    devices (Docker's own bridges, by name). `default`, broadcast and similar entries cannot collide with the pool."""
    net = ipaddress.ip_network(pool)
    hits = []
    for line in route_text.splitlines():
        parts = line.split()
        dev = parts[parts.index("dev") + 1] if "dev" in parts[:-1] else ""
        if dev and dev in ignore_devs:
            continue
        if not parts or parts[0] in ("default", "broadcast", "local", "unreachable", "prohibit", "blackhole", "throw"):
            if not (parts and parts[0] == "local" and len(parts) > 1):
                continue
            dest = parts[1]
        else:
            dest = parts[0]
        try:
            d = ipaddress.ip_network(dest, strict=False)
        except ValueError:
            continue
        if d.version == 4 and d.overlaps(net):
            hits.append(line.strip())
    return hits


def check_pool(host: HostOps, pool: str) -> None:
    """Refuse a range that this host already routes: sandbox networks inside it would shadow real destinations."""
    out = host.run(["ip", "-4", "route", "show", "table", "all"], 30)
    if out.returncode != 0:
        raise SandboxError("the host's routes could not be read to check the subnet pool")
    # our own sandbox networks add no host route (inhibit_ipv4), so any overlap is someone else's
    hits = route_overlaps(pool, out.stdout, docker_bridges(host))
    if hits:
        raise SandboxError(
            f"a subnet pool overlaps this host's routes ({len(hits)} found); set [sandbox.network] pools"
        )


def used_subnets(host: HostOps) -> list[ipaddress.IPv4Network]:
    ids = host.docker("network", "ls", "-q", check=False).stdout.split()
    if not ids:
        return []
    used = []
    for net in _json(host.docker("network", "inspect", *ids, check=False).stdout):
        for cfg in (net.get("IPAM") or {}).get("Config") or []:
            try:
                n = ipaddress.ip_network(cfg.get("Subnet", ""), strict=False)
            except ValueError:
                continue
            if n.version == 4:
                used.append(n)
    return used


def create(
    host: HostOps,
    name: str,
    *,
    pools: Sequence[str],
    prefix: int = DEFAULT_PREFIX,
    attempts: int = MAX_ATTEMPTS,
    extra: list[str] | None = None,
    rng: random.Random | None = None,
) -> tuple[str, int]:
    """Create the internal network `name` on a free /`prefix` of the pools: (subnet, attempts used).

    Raises SandboxError when no candidate was accepted within `attempts`; nothing is left behind then."""
    for pool in pools:
        check_pool(host, pool)
    cands = subnets(pools, prefix)
    start = (rng or random.SystemRandom()).randrange(len(cands))
    used = used_subnets(host)
    tried = 0
    for k in range(len(cands)):
        cand = cands[(start + k) % len(cands)]
        if any(cand.overlaps(u) for u in used):
            continue
        tried += 1
        r = host.docker(
            *("network", "create", "--internal", "--opt", "com.docker.network.bridge.inhibit_ipv4=true"),
            *("--subnet", str(cand), *(extra or []), name),
            check=False,
        )
        if r.returncode == 0:
            return str(cand), tried
        if OVERLAP not in r.stderr:
            log.error("docker network create failed: %s", describe_stderr(r.stderr))
            raise SandboxError("docker network create failed")
        used.append(cand)  # taken meanwhile by another process: the next candidate
        if tried >= attempts:
            break
    raise SandboxError(f"no free /{prefix} in the pools after {tried} attempts")


# ------------------------------------------------------------------------------------------------ orphans
def reap_orphans(host: HostOps, labels: Labels) -> dict:
    """Remove the containers and networks of sandboxes whose owner is provably gone (`owner_gone`). Only resources
    carrying all three labels are considered; containers first, so that the network can go."""
    out: dict = {"containers": [], "networks": [], "held": []}
    if not _REAP_LOCK.acquire(blocking=False):  # a concurrent start is reaping: this one need not wait on it
        return {**out, "skipped": "another thread of this process is reaping"}
    try:
        return _reap_locked(host, labels, out)
    finally:
        _REAP_LOCK.release()


def _reap_locked(host: HostOps, labels: Labels, out: dict) -> dict:
    ps = host.docker("ps", "-a", "-q", *labels.filters(), check=False)
    if ps.returncode != 0:  # a docker that did not answer lists nothing: that is not "none left"
        return {**out, "skipped": f"docker ps failed ({ps.returncode}): {describe_stderr(ps.stderr)}"}
    gone = []
    ids = ps.stdout.split()
    for info in _json(host.docker("inspect", *ids, check=False).stdout) if ids else []:
        owner = ((info.get("Config") or {}).get("Labels") or {}).get(labels.owner, "")
        if info.get("Id") and owner_gone(host, owner):
            gone.append((info["Id"], str(info.get("Name", "")).lstrip("/")))
    # a mount sandbox's sidecar may still hold a live FUSE mount: the readers stop, the mount goes, then the sidecar and
    # the agent (teardown.fuse_first); a sandbox whose mount could not be released is left running, never SIGKILLed with it
    from expcore_onedata.sandbox import teardown

    held = teardown.fuse_first(host, [name for _, name in gone])
    for cid, name in gone:
        if name in held:
            out["held"].append(name)
        elif host.docker("rm", "-f", cid, check=False).returncode == 0:
            out["containers"].append(cid)
    net_ids = host.docker("network", "ls", "-q", *labels.filters(), check=False).stdout.split()
    for net in _json(host.docker("network", "inspect", *net_ids, check=False).stdout) if net_ids else []:
        owner = (net.get("Labels") or {}).get(labels.owner, "")
        name = net.get("Name", "")
        if name and owner_gone(host, owner) and host.docker("network", "rm", name, check=False).returncode == 0:
            out["networks"].append(name)
    return out
