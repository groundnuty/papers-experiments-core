# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/infradeath.py, blob 6b81a8e443952540e4a1f77041a617ee343e6343
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Did a gate or a sidecar of a run die during it? Pure functions over `docker inspect` and the cgroup's `oom_kill` count (no docker, no host access).

The rule (O17): any gate or sidecar death during a run (an OOM kill, a container exit, a restart) makes that attempt an infrastructure failure,
retried and never an agent outcome, in every condition. It is decided from the container's own state, checked at collection, and never from what the agent saw fail:
the agent may not have touched the gate after it died, or may have caught the error.

What is compared, per container, between the state recorded when it came up (the baseline) and the state at collection:

* `Status` is not `running` (exited, dead, paused, restarting, or the container is gone): it is not there to serve the run. `ExitCode` and `OOMKilled` say how it went.
* `Id` differs: another container under the same name.
* `RestartCount` rose: Docker's restart policy brought it back. The sandbox sets no policy, so this is a second line.
* `StartedAt` or `FinishedAt` differ from the baseline: it was stopped and started again (`docker restart`, a daemon restart), which `RestartCount` does not count and `Status` hides.
* `OOMKilled` is true now and was not at the baseline. Docker's flag is sticky, so it only counts when it is new (killcause.py has the same rule for the agent's container).
* the cgroup's `oom_kill` rose: a process of the container was killed by an OOM killer while the container went on running (a child of the gate or of oneclient), which Docker's
  flag and `Status` do not show. Unreadable at the start or now is not zero: it is reported, as `killcause` reports unreadable counters.

A state that cannot be read at collection is a problem too (not known is not clean). A healthy container gives no problem.

Whose doing was it? A death is infrastructure only if agent code did not cause it. `cause_of` gives each death one cause from a fixed vocabulary:

* `host_pressure` (infrastructure): earlyoom's journal names the kill, or the OOM kill came from the host (the kernel log's CONSTRAINT_NONE or an ancestor's limit, or the cgroup's
  `oom_kill` rising with its own `oom` not rising: killcause.py's reading).
* `component_defect` (infrastructure): the gate hit its OWN memory limit (whatever the agent asked of it, it must not die), or a component ended by itself (an exit code of its
  own, or a crash signal).
* `own_limit_agent` (the AGENT's outcome, not retried): the oneclient sidecar hit its own memory limit. a mount's reads are charged to it, as a REST client's memory is charged to the agent's.
* `undetermined` (infrastructure, retried and listed): anything else, e.g. a SIGKILL or SIGTERM from outside with no OOM counter, no kernel line and no earlyoom record (`docker kill`, `docker stop`,
  `docker restart`, a daemon restart: a host or operator action, which the agent cannot reach), a restart whose earlier stop left no cause, a limit that the evidence cannot name,
  a state that could not be read. A replay of the agent's code is never part of this.

The caller turns the infrastructure deaths into `InfraError("sandbox", ...)` (kinds.py), and records every death, the agent's included, in the run's metadata and its `infra.json`.
"""

from __future__ import annotations

import json
from typing import Any

NEVER = "0001-01-01T00:00:00Z"  # Docker's value for a time that has not happened (FinishedAt of a container that never stopped)

HOST_PRESSURE, COMPONENT_DEFECT, OWN_LIMIT_AGENT, UNDETERMINED = (
    "host_pressure",
    "component_defect",
    "own_limit_agent",
    "undetermined",
)
CAUSES = (HOST_PRESSURE, COMPONENT_DEFECT, OWN_LIMIT_AGENT, UNDETERMINED)  # the fixed vocabulary of a death's cause
INFRASTRUCTURE = (
    HOST_PRESSURE,
    COMPONENT_DEFECT,
    UNDETERMINED,
)  # retried; the order is the run's one cause when several containers died: the specific before undetermined
CRASH_EXITS = (132, 134, 135, 136, 139)  # 128 + SIGILL, SIGABRT, SIGBUS, SIGFPE, SIGSEGV: a process that crashed itself


def parse_inspect(text: str) -> dict[str, Any] | None:
    """`docker inspect <container>` output -> the fields compared here; None when it is not what Docker prints for one container."""
    try:
        doc = json.loads(text)
        d = doc[0]
        s = d["State"]
        cid, status = d["Id"], s["Status"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    if not isinstance(cid, str) or not cid or not isinstance(status, str):
        return None
    return {
        "id": cid[:64],
        "status": status,
        "running": s.get("Running") is True,
        "restarting": s.get("Restarting") is True,
        "paused": s.get("Paused") is True,
        "oom_killed": s.get("OOMKilled") is True,
        "exit_code": s.get("ExitCode"),
        "restart_count": int(d.get("RestartCount") or 0),
        "started_at": s.get("StartedAt"),
        "finished_at": s.get("FinishedAt"),
        "error": str(s.get("Error") or "")[:200],
        "oom": None,
        "oom_kill": None,
    }  # `oom` and `oom_kill`: the cgroup's counters, which the caller reads and sets


def problems(
    role: str, condition: str, start: dict | None, end: dict | None, read_error: str | None = None
) -> list[str]:
    """What is wrong with a gate or sidecar between its baseline `start` and now `end` (None: it could not be read, `read_error` says why). [] when it ran throughout.
    Each item names the role, the condition and the evidence."""
    who = f"{role} (condition {condition})"
    if end is None:
        return [f"{who}: its state could not be read at collection ({read_error or 'no reason given'})"]
    if start is None:
        return [f"{who}: no state was recorded when it came up, so nothing can show it ran throughout"]
    out: list[str] = []
    if end["id"] != start["id"]:
        out.append(f"{who}: another container now holds the name (id {start['id'][:12]} then {end['id'][:12]})")
    if end["status"] != "running":
        out.append(
            f"{who}: not running at collection (status {end['status']}, exit code {end['exit_code']}, OOMKilled {str(end['oom_killed']).lower()}, "
            f"finished {end['finished_at']}" + (f", error {end['error']!r}" if end["error"] else "") + ")"
        )
    elif end["restarting"]:
        out.append(f"{who}: restarting at collection")
    if end["restart_count"] > start["restart_count"]:
        out.append(
            f"{who}: restarted {end['restart_count'] - start['restart_count']} time(s) by Docker (RestartCount {start['restart_count']} then {end['restart_count']})"
        )
    if end["started_at"] != start["started_at"]:
        out.append(
            f"{who}: started again during the run (StartedAt {start['started_at']} then {end['started_at']}, last exit code {end['exit_code']})"
        )
    elif end["finished_at"] != start["finished_at"] and end["status"] == "running":
        out.append(
            f"{who}: stopped during the run (FinishedAt {start['finished_at']} then {end['finished_at']}, exit code {end['exit_code']})"
        )
    if end["oom_killed"] and not start["oom_killed"]:
        out.append(
            f"{who}: OOM-killed during the run (Docker's OOMKilled was false at the start and is true now, exit code {end['exit_code']})"
        )
    a, b = start.get("oom_kill"), end.get("oom_kill")
    if a is not None and b is not None and b > a:
        out.append(
            f"{who}: the cgroup's oom_kill rose by {b - a} ({a} then {b}): a process of the container was killed by an OOM killer"
        )
    if not out and (a is None or b is None):
        out.append(
            f"{who}: the cgroup's oom_kill counter could not be read (at the start {a}, now {b}): not known whether a process of it was killed"
        )
    return out


def _kernel_verdict(kernel: list[dict] | None, cid: str) -> tuple[str | None, int, int]:
    """(verdict, own lines, host lines) of the kernel's OOM-kill lines that name this container. Own: CONSTRAINT_MEMCG with this container's cgroup as the limit that was hit. Host:
    CONSTRAINT_NONE, or a limit of an ancestor cgroup (the container only the victim). Both, or neither: no verdict."""
    mine = [k for k in kernel or [] if cid and cid in k.get("text", "")]
    own = [k for k in mine if k["constraint"] == "CONSTRAINT_MEMCG" and cid in k.get("oom_memcg", "")]
    host = [
        k
        for k in mine
        if k["constraint"] == "CONSTRAINT_NONE"
        or (k["constraint"] == "CONSTRAINT_MEMCG" and cid not in k.get("oom_memcg", ""))
    ]
    return ("own" if own and not host else "host" if host and not own else None), len(own), len(host)


def cause_of(
    role: str,
    start: dict | None,
    end: dict | None,
    read_error: str | None = None,
    *,
    kernel: list[dict] | None = None,
    earlyoom: bool = False,
) -> dict:
    """The one cause of a death of a gate or sidecar: {"cause", "category" ("agent" | "infrastructure"), "basis"}. Call it only for a container with problems.

    `kernel`: the OOM-kill lines of the run's window (killcause.parse_kernel_oom), None when the log could not be read. `earlyoom`: its journal named a kill of this role's process.
    Never guesses: evidence that cannot name the cause gives `undetermined`, which is retried and listed."""

    def out(cause: str, basis: str) -> dict:
        return {"cause": cause, "category": "agent" if cause == OWN_LIMIT_AGENT else "infrastructure", "basis": basis}

    if earlyoom:
        return out(HOST_PRESSURE, "earlyoom's journal names a kill of this container's process")
    if end is None or start is None:
        return out(
            UNDETERMINED,
            f"its state could not be read ({read_error or 'no state was recorded when it came up'}): not known how it ended",
        )
    cid = end["id"]
    a_oom, b_oom, a_kill, b_kill = start.get("oom"), end.get("oom"), start.get("oom_kill"), end.get("oom_kill")
    d_kill = (b_kill - a_kill) if a_kill is not None and b_kill is not None else None
    d_oom = (b_oom - a_oom) if a_oom is not None and b_oom is not None else None
    k, n_own, n_host = _kernel_verdict(kernel, cid)
    flag = end["oom_killed"] and not start["oom_killed"]
    if flag or (d_kill or 0) > 0 or n_own or n_host:
        # an OOM kill: which limit was hit. The counters say it while the cgroup is there (oom and oom_kill rose: its own limit; oom_kill without oom: the host's killer), the kernel log
        # after it is gone. Docker's flag says only that a kill happened. Counters and log must agree; both host and own lines, or no line and no counter, name nothing.
        c = None if not d_kill else ("own" if (d_oom or 0) > 0 else "host")
        if n_own and n_host:
            return out(
                UNDETERMINED,
                "an OOM kill, and the kernel log has both this container's own limit and the host's killing a process of it",
            )
        if c and k and c != k:
            return out(
                UNDETERMINED,
                f"an OOM kill: the cgroup's counters say the {c} limit, the kernel log the {k} one: conflicting",
            )
        verdict = c or k
        if verdict is None:
            return out(
                UNDETERMINED,
                "an OOM kill ("
                + ("Docker's OOMKilled is newly true" if flag else "the cgroup's oom_kill rose")
                + ") whose limit cannot be established: "
                f"no counter delta that names it ({d_oom}, {d_kill}) and {'no line naming this container in the kernel log' if kernel is not None else 'the kernel log could not be read'}",
            )
        src = "the cgroup's counters" if c else "the kernel log"
        if verdict == "host":
            return out(HOST_PRESSURE, f"killed by the host's OOM killer, not at this container's own limit ({src})")
        if role == "gate":
            return out(
                COMPONENT_DEFECT,
                f"the gate hit its own memory limit ({src}), e.g. its spool: a defect of the component whatever the agent asked of it",
            )
        if role == "sidecar":
            return out(
                OWN_LIMIT_AGENT,
                f"the sidecar hit its own memory limit ({src}): a mount's reads are charged to it, so it is the agent's outcome",
            )
        return out(UNDETERMINED, f"the {role} hit its own memory limit ({src}), and nothing says whose allowance it is")
    if end["id"] != start["id"]:
        return out(UNDETERMINED, "another container holds the name: nothing says how the first ended")
    if end["status"] in ("paused", "restarting", "created"):
        return out(UNDETERMINED, f"the container is {end['status']}: nothing says how or why")
    if end["status"] != "running":
        code = end["exit_code"]
        if isinstance(code, int) and (code in CRASH_EXITS or 1 <= code < 128):
            return out(
                COMPONENT_DEFECT,
                f"the {role} ended by itself with exit code {code}"
                + (" (a crash signal)" if code in CRASH_EXITS else "")
                + ": no OOM kill, no host kill on record",
            )
        # 0 is what a SIGTERM handler or a plain stop gives; 137 and 143 are SIGKILL and SIGTERM, which `docker kill` and `docker stop` send as much as a killer does
        return out(
            UNDETERMINED,
            f"ended with exit code {code} and no OOM counter, no kernel OOM line and no earlyoom record: a signal sent from outside (docker kill, stop, restart: a host or "
            "operator action, which agent code cannot do) or a host kill that left nothing",
        )
    if end["started_at"] != start["started_at"] or end["restart_count"] > start["restart_count"]:
        return out(
            UNDETERMINED,
            "it was stopped and started again (docker restart, a restart policy, a daemon restart): its state keeps no reliable record of why it stopped",
        )
    return out(UNDETERMINED, "a problem with its state or its OOM counter, and no death shape that names a cause")


def run_cause(causes) -> str | None:
    """The one infrastructure cause of a run whose containers died with these causes (the specific before `undetermined`); None when none of them is infrastructure."""
    seen = set(causes)
    return next((c for c in INFRASTRUCTURE if c in seen), None)


def attempt_cause(deaths) -> str | None:
    """The one cause an attempt carries in the campaign's store: the infrastructure cause when any death was infrastructure (the attempt is retried whatever else died), else the agent's
    own-limit cause, else None (no container died). `deaths`: the per-container death records of the run's metadata."""
    deaths = [d for d in deaths or [] if isinstance(d, dict)]
    return run_cause(d.get("cause") for d in deaths) or next(
        (d["cause"] for d in deaths if d.get("cause") == OWN_LIMIT_AGENT), None
    )


def infra_detail(deaths) -> str | None:
    """The detail of the InfraError for the infrastructure deaths among `deaths`, led by a fixed marker that the campaign's export parses (`container_death_cause`); None when none."""
    infra = [d for d in deaths or [] if isinstance(d, dict) and d.get("category") == "infrastructure"]
    if not infra:
        return None
    return f"{MARKER}{run_cause(d['cause'] for d in infra)}): " + "; ".join(
        f"{p} [cause {d['cause']}: {d['basis']}]" for d in infra for p in d["problems"]
    )


MARKER = "container death (cause="  # what `infra_detail` starts with, so an attempt's stored detail says what it was; the cause itself is stored per attempt (campaign store: container_death_cause)
