# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/netpool.py, blob 67c5ce2d1adecb72abd5dc7fe9f3a643bbfb36d4
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Sandbox networks from a dedicated address range, and the networks a killed process left behind.

Docker's default address pools give this host about 30 networks in all, and every sandbox needs one: a few leaked
sandboxes and a campaign at concurrency 16 exhaust them ("all predefined address pools have been fully subnetted", 1 Nov).
So each sandbox network gets an explicit /28 from a dedicated range (default 10.200.0.0/13: 32,768 networks, checked
against this host's routes and addresses on 1 Nov; set `STUDY_VAR_89` or `SandboxConfig.subnet_pool` to change it).
Explicit subnets never draw on Docker's default pools.

Concurrent starts in different processes: each takes candidates in a random rotation of the range, skipping subnets
already in use, and lets Docker refuse an overlap (the daemon checks it atomically); a refusal moves on to the next
candidate, at most `MAX_ATTEMPTS` times.

Exit paths: `Sandbox.stop` removes the network and every container it started or tried to start (normal end, an
exception, a failed start). A process killed outright cannot; so its networks and containers carry three labels, the
sandbox marker, the owner (boot id, PID namespace, pid and process start time) and the run directory, and
`reap_orphans` removes the containers and networks whose owner is provably gone: in this boot and this PID namespace
with no such process, or from an earlier boot (an M sandbox's sidecar and agent in the ordered teardown, see teardown.py). An owner it cannot judge (another namespace, an unreadable /proc) is
left alone, and so is anything without all three labels. It deletes no files: a killed run's directory and loop mounts
stay until its owner's lane removes them.
"""
from __future__ import annotations

import ipaddress
import json
import os
import random
import re
import subprocess
import threading
from pathlib import Path

from . import hostops as H

_REAP_LOCK = threading.Lock()   # an M orphan's ordered teardown takes tens of seconds; starts run from several worker threads

POOL_ENV = "STUDY_VAR_89"
DEFAULT_POOL = "10.16.0.0/13"
PREFIX = 28                 # 16 addresses: the gate and the agent need two
MAX_ATTEMPTS = 64
LABEL, OWNER, DIR = "org.dataset_76.sandbox", "org.dataset_76.owner", "org.dataset_76.dir"
OVERLAP = "Pool overlaps"


def default_pool() -> str:
    return os.environ.get(POOL_ENV, DEFAULT_POOL)


# ------------------------------------------------------------------------------------------------ owners
def _start_time(pid: int) -> str | None:
    """The process's start time in clock ticks since boot (/proc/<pid>/stat field 22): with the pid, it names one
    process, so a reused pid is not mistaken for the owner. None if there is no such process; an unreadable entry
    raises (unknown is not dead)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    except ProcessLookupError:
        return None
    return stat.rsplit(")", 1)[1].split()[19]   # fields after the command name start at field 3


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _pid_ns() -> str:
    return os.readlink("/proc/self/ns/pid").strip("pid:[]")


def owner_id(pid: int | None = None) -> str:
    pid = os.getpid() if pid is None else pid
    st = _start_time(pid)
    if st is None:
        raise H.SandboxError(f"no process {pid}")
    return f"{_boot_id()}/{_pid_ns()}/{pid}/{st}"


_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _no_such_process(pid: int) -> bool:
    """Absent from /proc is not proof (hidepid hides other users' processes): only the kernel's ESRCH is."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False                         # it exists, as someone else's
    return False


def owner_gone(owner: str) -> bool:
    """True only when the owner is provably gone: an earlier boot, or this boot and PID namespace with no process of
    that pid (ESRCH) or one with another start time. False for a live owner, another namespace, a malformed label, an
    unreadable or hidden /proc entry."""
    m = re.fullmatch(rf"({_UUID})/(\d+)/(\d+)/(\d+)", owner or "")
    if not m:
        return False
    boot, ns, pid, start = m.groups()
    try:
        if boot != _boot_id():
            return True                      # no process outlives a reboot
        if ns != _pid_ns():
            return False                     # its pids are not ours to look up
        seen = _start_time(int(pid))
        if seen is None:
            return _no_such_process(int(pid))
        return seen != start
    except OSError:
        return False


def owner_alive(owner: str) -> bool:
    return not owner_gone(owner)


def labels(sandbox_dir: Path) -> list[str]:
    """`docker run` / `docker network create` arguments that mark a resource as this process's sandbox."""
    return ["--label", f"{LABEL}=1", "--label", f"{OWNER}={owner_id()}", "--label", f"{DIR}={sandbox_dir}"]


# ------------------------------------------------------------------------------------------------ subnets
def subnets(pool: str, prefix: int = PREFIX) -> list[ipaddress.IPv4Network]:
    return list(ipaddress.ip_network(pool).subnets(new_prefix=prefix))


def docker_bridges() -> set[str]:
    """The host interfaces of Docker's bridge networks: their routes are Docker's, which refuses overlaps among them."""
    ids = H.docker("network", "ls", "-q", "--filter", "driver=bridge", check=False).stdout.split()
    if not ids:
        return set()
    out = H.docker("network", "inspect", "-f", '{{.Id}} {{index .Options "com.docker.network.bridge.name"}}', *ids,
                   check=False).stdout
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if parts:
            names.add(parts[1] if len(parts) > 1 and parts[1] != "<no" else "br-" + parts[0][:12])
    return names


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


def check_pool(pool: str) -> None:
    """Refuse a range that this host already routes: sandbox networks inside it would shadow real destinations."""
    out = subprocess.run(["ip", "-4", "route", "show", "table", "all"], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise H.SandboxError(f"cannot read the host's routes to check the subnet pool: {out.stderr.strip()[:200]}")
    # our own sandbox networks add no host route (inhibit_ipv4), so any overlap is someone else's
    hits = route_overlaps(pool, out.stdout, docker_bridges())
    if hits:
        raise H.SandboxError(f"subnet pool {pool} overlaps this host's routes: {hits[:3]}; set {POOL_ENV}")


def used_subnets() -> list[ipaddress.IPv4Network]:
    ids = H.docker("network", "ls", "-q", check=False).stdout.split()
    if not ids:
        return []
    out = H.docker("network", "inspect", "-f", "{{json .IPAM.Config}}", *ids, check=False).stdout
    used = []
    for line in out.splitlines():
        for cfg in json.loads(line or "null") or []:
            try:
                n = ipaddress.ip_network(cfg.get("Subnet", ""), strict=False)
            except ValueError:
                continue
            if n.version == 4:
                used.append(n)
    return used


def create(name: str, *, pool: str | None = None, prefix: int = PREFIX, attempts: int = MAX_ATTEMPTS,
           extra: list[str] | None = None, rng: random.Random | None = None) -> tuple[str, int]:
    """Create the internal network `name` on a free /`prefix` of the pool: (subnet, attempts used).

    Raises SandboxError when no candidate was accepted within `attempts`; nothing is left behind then."""
    pool = pool or default_pool()
    check_pool(pool)
    cands = subnets(pool, prefix)
    start = (rng or random.SystemRandom()).randrange(len(cands))
    used = used_subnets()
    tried = 0
    for k in range(len(cands)):
        cand = cands[(start + k) % len(cands)]
        if any(cand.overlaps(u) for u in used):
            continue
        tried += 1
        r = H.docker("network", "create", "--internal", "--opt", "com.docker.network.bridge.inhibit_ipv4=true",
                     "--subnet", str(cand), *(extra or []), name, check=False)
        if r.returncode == 0:
            return str(cand), tried
        if OVERLAP not in r.stderr:
            raise H.SandboxError(f"docker network create {name} failed: {r.stderr.strip()[:300]}")
        used.append(cand)            # taken meanwhile by another process: the next candidate
        if tried >= attempts:
            break
    raise H.SandboxError(f"no free /{prefix} in {pool} for {name} after {tried} attempts")


# ------------------------------------------------------------------------------------------------ orphans
def reap_orphans() -> dict:
    """Remove the containers and networks of sandboxes whose owner is provably gone (`owner_gone`). Only resources
    carrying all three labels are considered; containers first, so that the network can go."""
    out = {"containers": [], "networks": [], "held": []}
    if not _REAP_LOCK.acquire(blocking=False):       # a concurrent start is reaping: this one need not wait on it
        return {**out, "skipped": "another thread of this process is reaping"}
    try:
        return _reap_locked(out)
    finally:
        _REAP_LOCK.release()


def _reap_locked(out: dict) -> dict:
    full = ["--filter", f"label={LABEL}=1", "--filter", f"label={OWNER}", "--filter", f"label={DIR}"]
    ps = H.docker("ps", "-a", *full, "--format", '{{.ID}}\t{{.Names}}\t{{.Label "' + OWNER + '"}}', check=False)
    if ps.returncode != 0:         # a docker that did not answer lists nothing: that is not "none left", and the caller that must know sees `skipped`
        return {**out, "skipped": f"docker ps failed ({ps.returncode}): {str(ps.stderr).strip()[:200]}"}
    rows = ps.stdout
    gone = []
    for line in rows.splitlines():
        cid, name, owner = (line.split("\t") + ["", ""])[:3]
        if cid and owner_gone(owner):
            gone.append((cid, name))
    # an M sandbox's sidecar may still hold a live FUSE mount: the readers stop, the mount goes, then the sidecar and the
    # agent (teardown.fuse_first); a sandbox whose mount could not be released is left running, never SIGKILLed with it
    from . import teardown
    held = teardown.fuse_first([n for _, n in gone])
    for cid, name in gone:
        if name in held:
            out["held"].append(name)
        elif H.docker("rm", "-f", cid, check=False).returncode == 0:
            out["containers"].append(cid)
    nets = H.docker("network", "ls", "-q", *full, check=False).stdout.split()
    for nid in nets:
        info = H.docker("network", "inspect", "-f", '{{.Name}}\t{{index .Labels "' + OWNER + '"}}', nid, check=False)
        name, _, owner = info.stdout.strip().partition("\t")
        if name and owner_gone(owner) and H.docker("network", "rm", nid, check=False).returncode == 0:
            out["networks"].append(name)
    return out
