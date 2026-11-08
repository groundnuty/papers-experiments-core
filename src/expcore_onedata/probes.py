# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/scripted/metrics.py, blob bc25531c7aac74a09ff74c730fceabd8119cd2c5
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Per-run measures: network counters per container, gate-log counts, oneclient's perf log, host state.

Bytes moved (#10 Q6, parameters.md): each container's own network-namespace counters. Three are recorded because they
are different things in T and D: the agent's eth0 (the frozen method) sees only the gate; the gate's upstream interface
sees the traffic between sites; the gate log's `upstream_bytes` is the file bytes of whole-file transfers. M: the
oneclient sidecar's eth0. Nothing is derived from another counter.
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import time
from pathlib import Path

from ..sandbox import hostops as H

_IFACES = ('for i in /sys/class/net/*; do n=$(basename "$i"); [ "$n" = lo ] && continue; '
           'echo "$n $(cat "$i/address") $(cat "$i/statistics/rx_bytes") $(cat "$i/statistics/tx_bytes")"; done')


def parse_ifaces(text: str) -> dict[str, dict]:
    out = {}
    for line in text.strip().splitlines():
        name, mac, rx, tx = line.split()
        out[name] = {"mac": mac.lower(), "rx": int(rx), "tx": int(tx)}
    return out


def iface_counters(container: str) -> dict[str, dict]:
    return parse_ifaces(H.docker("exec", container, "sh", "-c", _IFACES).stdout)


def bridge_mac(container: str) -> str:
    """MAC of the container's interface on docker's `bridge` network (its way out); raises if it has none."""
    nets = json.loads(H.docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", container).stdout)
    mac = (nets.get("bridge") or {}).get("MacAddress")
    if not mac:
        raise H.SandboxError(f"{container}: no MAC on the bridge network; networks {sorted(nets)}")
    return mac.lower()


def iface_by_mac(counters: dict[str, dict], mac: str) -> dict:
    hits = [c for c in counters.values() if c["mac"] == mac]
    if len(hits) != 1:
        raise H.SandboxError(f"expected one interface with MAC {mac}, found {sorted(counters)}")
    return hits[0]


def delta(after: dict, before: dict) -> dict:
    return {"rx": after["rx"] - before["rx"], "tx": after["tx"] - before["tx"]}


RETRY_BEHAVIOUR = ("the shipped client (onedatafilerestclient 25.0.0) never retries: a requests.Session with the default "
                   "adapter (max_retries 0) and no retry code, so any failed request fails the run; the scripted "
                   "pipeline adds none. The only automatic retry is the gate's: one replacement upstream connection when "
                   "a kept one is found closed with no byte of an answer received, logged as upstream_reconnects.")


def summarize_gate_requests(lines: list[str]) -> dict:
    """Requests as the gate saw them: the client-side request count of the REST options, and per request type the
    hard errors (non-2xx), the upstream timeouts and failures behind them, and the gate's stale-connection reconnects."""
    tags: dict[str, dict] = {}
    statuses: dict[str, int] = {}
    total = denied = ranged = 0
    for line in lines:
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("event") == "conn_close":
            continue          # how a client connection ended (gate.py `_peer_gone`), not a request: it has no status and would count as a non-2xx. The gate's other events still count, as ever
        total += 1
        tag = r.get("tag") or "untagged"
        t = tags.setdefault(tag, {"requests": 0, "bytes": 0, "upstream_bytes": 0, "pages": 0, "non_2xx": 0,
                                  "upstream_errors": 0, "upstream_timeouts": 0, "reconnects": 0})
        t["requests"] += 1
        t["bytes"] += int(r.get("bytes") or 0)
        t["upstream_bytes"] += int(r.get("upstream_bytes") or 0)
        t["pages"] += int(r.get("pages") or 0)
        t["reconnects"] += int(r.get("upstream_reconnects") or 0)
        key = str(r.get("status"))
        statuses[key] = statuses.get(key, 0) + 1
        if not key.startswith("2"):
            t["non_2xx"] += 1
        if r.get("error"):
            t["upstream_errors"] += 1
            if "timed out" in r["error"].lower() or "timeout" in r["error"].lower():
                t["upstream_timeouts"] += 1
        if r.get("decision") == "deny":
            denied += 1
        if r.get("range"):
            ranged += 1
    errors = sum(n for s_, n in statuses.items() if not s_.startswith("2"))
    return {"requests": total, "denied": denied, "non_2xx": errors, "with_range": ranged,
            "upstream_errors": sum(t["upstream_errors"] for t in tags.values()),
            "upstream_timeouts": sum(t["upstream_timeouts"] for t in tags.values()),
            "reconnects": sum(t["reconnects"] for t in tags.values()),
            "by_tag": tags, "by_status": statuses}


def parse_perf_csv(text: str) -> dict:
    """oneclient `--log-read-write-perf`: Time,File,Class,Operation,Offset,Size,Duration [us] (#10 Q1b).

    FsLogic reads are what reached the client from FUSE; ReadCache prefetches are what it asked the provider for."""
    out: dict[str, dict] = {}
    files = set()
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if len(row) != 7 or row[0] == "Time":
            continue
        _, path, cls, op, _off, size, dur = row
        o = out.setdefault(f"{cls}.{op}", {"count": 0, "bytes": 0, "duration_us": 0})
        o["count"] += 1
        o["bytes"] += int(size)
        o["duration_us"] += int(dur)
        files.add(path)
    return {"by_operation": out, "files": len(files)}


def parse_cgroup(text: str) -> dict:
    """{cpu_s, peak_bytes, oom_kills} from `cat cpu.stat memory.peak memory.events` of a cgroup. What a file did not
    give is None (unknown), never zero."""
    out = {"cpu_s": None, "peak_bytes": None, "oom_kills": None}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "usage_usec" and parts[1].isdigit():
            out["cpu_s"] = int(parts[1]) / 1e6
        elif len(parts) == 2 and parts[0] == "oom_kill" and parts[1].isdigit():
            out["oom_kills"] = int(parts[1])
        elif len(parts) == 1 and parts[0].isdigit():        # memory.peak is a bare number
            out["peak_bytes"] = int(parts[0])
    return out


CGROUP_FILES = ("/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory.events")


def cgroup(container: str) -> dict:
    """CPU seconds, peak memory and OOM kills of this container since it started (science, #9: per run, per container).
    All None when it cannot be read (it may be gone)."""
    r = H.docker("exec", container, "cat", *CGROUP_FILES, check=False)
    return parse_cgroup(r.stdout)


def container_stats(before: dict, after: dict) -> dict:
    """role -> {cpu_s over the run, peak_bytes, oom_kills}; a missing reading stays None."""
    out = {}
    for role, a in after.items():
        b = before.get(role) or {}
        cpu = a["cpu_s"] - b["cpu_s"] if a.get("cpu_s") is not None and b.get("cpu_s") is not None else None
        out[role] = {"cpu_s": None if cpu is None else round(cpu, 3), "peak_bytes": a.get("peak_bytes"), "oom_kills": a.get("oom_kills")}
    return out


def meminfo_available_gib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024 ** 2
    raise RuntimeError("no MemAvailable in /proc/meminfo")


def disk_free_gib(path: Path | str) -> float:
    return shutil.disk_usage(path).free / 1024 ** 3


def nic_rx(nic: str = "ens3") -> int | None:
    try:
        return int(Path(f"/sys/class/net/{nic}/statistics/rx_bytes").read_text())
    except OSError:
        return None


def other_sandboxes(own_prefix: str) -> list[str]:
    """Sandboxes of other lanes running now (agent containers only), so contention is on the record."""
    names = H.docker("ps", "--format", "{{.Names}}", check=False).stdout.split()
    return sorted(n for n in names if n.startswith("dataset-168-") and not n.startswith(own_prefix)
                  and not n.endswith(("-gate", "-oneclient")))


def host_state(work_root: Path | str, own_prefix: str) -> dict:
    return {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "load1": round(os.getloadavg()[0], 2),
            "load5": round(os.getloadavg()[1], 2), "mem_available_gib": round(meminfo_available_gib(), 1),
            "disk_free_gib": round(disk_free_gib(work_root), 1), "other_sandboxes": len(other_sandboxes(own_prefix)),
            "ens3_rx": nic_rx()}
