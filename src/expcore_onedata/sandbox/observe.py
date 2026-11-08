# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/sandbox/sandbox.py:630-910 (cgroup reads, state, kill facts)
#   changes: the reads go through HostOps and the facts are gathered by one class
"""What the host can say about the containers of one sandbox: cgroup counters, container state, the kernel log (O17).

Every read goes through `HostOps`, so a test serves the cgroup files and the kernel log from memory. Nothing here
decides a cause: it gathers the facts that `killcause`, `infradeath` and `earlyoom` classify. An unreadable counter is
None, never zero, so a classification built on it says "unknown" and not "nothing happened".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from expcore_onedata.sandbox import earlyoom as EO
from expcore_onedata.sandbox import infradeath as ID
from expcore_onedata.sandbox import killcause as KC
from expcore_onedata.sandbox.hostops import HostOps, inspect

CGROUP_ROOT = "/sys/fs/cgroup"


def cgroup_candidates(cid: str) -> tuple[str, str]:
    """Where docker puts a container's cgroup (v2): under system.slice with the systemd driver, else under docker."""
    return f"{CGROUP_ROOT}/system.slice/docker-{cid}.scope", f"{CGROUP_ROOT}/docker/{cid}"


class Observer:
    """The cgroup and state reads of one sandbox's containers, by container name."""

    def __init__(self, host: HostOps, agent_uid: int) -> None:
        self.host = host
        self.agent_uid = agent_uid
        self._dirs: dict[str, str] = {}

    # ------------------------------------------------------------------------------------------------ files
    def _read(self, path: str) -> str | None:
        try:
            return self.host.read_text(path)
        except OSError:
            return None

    def cgroup_dir(self, container: str) -> str | None:
        """The container's cgroup directory on the host, or None when it cannot be found."""
        cached = self._dirs.get(container)
        if cached is not None and self.host.exists(f"{cached}/memory.events"):
            return cached
        info = inspect(self.host, container)
        if info is None:
            return None
        cid = str(info.get("Id", ""))
        for cand in cgroup_candidates(cid):
            if self.host.exists(f"{cand}/memory.events"):
                self._dirs[container] = cand
                return cand
        return None

    def totals(self, container: str) -> dict[str, Any]:
        """{cpu_s, peak_bytes, oom_kills} of a container's cgroup since it started; None for what could not be read."""
        out: dict[str, Any] = {"cpu_s": None, "peak_bytes": None, "oom_kills": None}
        d = self.cgroup_dir(container)
        if d is None:
            return out
        for line in (self._read(f"{d}/cpu.stat") or "").splitlines():
            key, _, value = line.partition(" ")
            if key == "usage_usec" and value.strip().isdigit():
                out["cpu_s"] = round(int(value) / 1e6, 3)
        peak = (self._read(f"{d}/memory.peak") or "").strip()
        out["peak_bytes"] = int(peak) if peak.isdigit() else None
        events = KC.parse_events(self._read(f"{d}/memory.events") or "")
        out["oom_kills"] = None if events is None else int(events.get("oom_kill", 0))
        return out

    def memory_facts(self, container: str) -> dict[str, Any]:
        """The memory.events counters, peak and limit of a container's cgroup. None where unreadable."""
        out: dict[str, Any] = {
            "oom_kill_count": None,
            "memory_peak_bytes": None,
            "memory_max_bytes": None,
            "events": None,
        }
        d = self.cgroup_dir(container)
        if d is None:
            return out
        out["events"] = KC.parse_events(self._read(f"{d}/memory.events") or "")
        out["oom_kill_count"] = None if out["events"] is None else int(out["events"].get("oom_kill", 0))
        for key, name in (("memory_peak_bytes", "memory.peak"), ("memory_max_bytes", "memory.max")):
            value = (self._read(f"{d}/{name}") or "").strip()
            out[key] = int(value) if value.isdigit() else None
        return out

    def anon_memory(self, container: str) -> int | None:
        """Anonymous plus shared memory now (what the agent's code allocated, without reclaimable cache)."""
        d = self.cgroup_dir(container)
        text = self._read(f"{d}/memory.stat") if d else None
        if text is None:
            return None
        stat = {k: int(v) for k, v in (line.split() for line in text.splitlines() if line.strip())}
        return stat.get("anon", 0) + stat.get("shmem", 0)

    def procs(self, container: str) -> str | None:
        """The `cgroup.procs` text of a container: its host PIDs."""
        d = self.cgroup_dir(container)
        return self._read(f"{d}/cgroup.procs") if d else None

    # ------------------------------------------------------------------------------------------------ state
    def oom_counters(self, cid: str) -> dict[str, int] | None:
        """{oom, oom_kill} of a container's cgroup by its id; None when it cannot be read. An exited one's is gone."""
        for cand in cgroup_candidates(cid):
            events = KC.parse_events(self._read(f"{cand}/memory.events") or "")
            if events is not None:
                return {"oom": int(events.get("oom", 0)), "oom_kill": int(events.get("oom_kill", 0))}
        return None

    def state(self, container: str, timeout: float = 30) -> tuple[dict | None, str | None]:
        """(state, None) of a container from `docker inspect` and its cgroup, or (None, why) when docker cannot say."""
        r = self.host.docker("inspect", container, check=False, timeout=timeout)
        if r.returncode != 0:
            return None, f"docker inspect exited {r.returncode}"
        st = ID.parse_inspect(r.stdout)
        if st is None:
            return None, "docker inspect printed something unreadable"
        counters = self.oom_counters(st["id"])
        st["oom"], st["oom_kill"] = (counters["oom"], counters["oom_kill"]) if counters else (None, None)
        return st, None

    # ------------------------------------------------------------------------------------------------ kernel
    def uptime(self) -> float | None:
        text = self._read("/proc/uptime")
        try:
            return float(text.split()[0]) if text else None
        except (ValueError, IndexError):
            return None

    def kernel_oom(self, uptime_before: float | None) -> list[dict] | None:
        """OOM-kill lines of the kernel log since `uptime_before` seconds of uptime; None if the log cannot be read."""
        if uptime_before is None:
            return None
        result = self.host.run(["dmesg"], 20, sudo=True)
        if result.returncode != 0:
            return None
        return KC.parse_kernel_oom(result.stdout, since_s=uptime_before - 1.0)

    def earlyoom_kills(self, since: float) -> list[EO.Kill] | None:
        """The host's out-of-memory killer's kills since an epoch time; None when its journal cannot be read."""
        return EO.read_kills(self.host, since)

    # ------------------------------------------------------------------------------------------------ kills
    def kill_facts(
        self,
        agent: str,
        before: Mapping[str, int] | None,
        duration_s: float,
        timeout_s: float,
        uptime_before: float | None,
    ) -> dict[str, Any]:
        """The facts about a cell that ended with a kill or a timeout, read from the host before any teardown, and the
        classification (killcause): `reason` is memory_limit, host_oom, container_dead, time_limit, killed_other or
        unknown, and `category` agent, infrastructure or unknown. Docker's sticky OOMKilled flag is recorded, not used.
        """
        facts = self.memory_facts(agent)
        info = inspect(self.host, agent)
        state = (info or {}).get("State") or {}
        container = {
            "docker_oom_killed": state.get("OOMKilled") if info else None,
            "container_status": state.get("Status") if info else None,
            "container_exit_code": state.get("ExitCode") if info else None,
        }
        running = None if container["container_status"] is None else container["container_status"] == "running"
        events = facts["events"]
        need_log = events is None or events.get("oom_kill", 0) > (before or {}).get("oom_kill", 0)
        kernel = self.kernel_oom(uptime_before) if need_log else None
        cls = KC.classify(
            dict(before) if before is not None else None,
            events,
            container_running=running,
            duration_s=duration_s,
            timeout_s=timeout_s,
            kernel=kernel,
            container_id=str((info or {}).get("Id", "")) or None,
        )
        oom_killed = (
            True
            if cls["reason"] in ("memory_limit", "host_oom")
            else None
            if events is None and container["docker_oom_killed"] is None
            else False
        )
        return {
            **container,
            **{k: v for k, v in facts.items() if k != "events"},
            "events_before": None if before is None else dict(before),
            "events_after": events,
            "duration_s": duration_s,
            "timeout_s": timeout_s,
            "oom_kill_count_before": None if before is None else before.get("oom_kill"),
            "oom_killed": oom_killed,
            **cls,
        }
