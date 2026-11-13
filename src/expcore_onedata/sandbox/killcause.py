# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/killcause.py, blob ce265cc4de09781c268b2a82175bf7e9b8220589
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Who caused a killed cell: pure functions over the cgroup's `memory.events` and the kernel log (no docker, no host access).

The exit code cannot tell a timeout from a memory kill from a host out-of-memory kill: all are 137. The facts that can:

* cgroup v2 `memory.events`: `oom` counts the times THIS cgroup was about to exceed its own `memory.max` and the OOM killer
  was invoked for it; `oom_kill` counts processes of this cgroup killed by ANY OOM killer, the host-wide one included.
  So `oom` +1 with `oom_kill` +1 is the agent's allocation hitting the container limit; `oom_kill` +1 with `oom` +0 is the
  host killing it.
* the kernel log line `oom-kill:constraint=CONSTRAINT_MEMCG|CONSTRAINT_NONE,...,oom_memcg=/...docker-<id>.scope,task_memcg=...`
  says the same (MEMCG with this container's id = its own limit; NONE = the host ran out), where the log can be read.

Docker's `OOMKilled` flag is sticky (it stays true after the first kill) and says nothing about which limit, so it is
recorded by the caller but never decides a category.

Categories: `agent` (the agent's own doing: a run outcome, never retried), `infrastructure` (retried), `unknown` (what the harness lost track of: unreadable counters, conflicting
evidence, a kill nothing attributes. Retried too, within the attempt limit, as the core's `unattributed_kill`; since that can discard an agent's own outcome, the export counts these
attempts per condition and model).

`time_limit` is the harness's own timer, never an inference: only a cell whose timer the harness recorded as fired (cells.py)
is one, whatever its elapsed time, since a cell killed by something else just before its limit has run as long. A failure of
docker itself without that flag is no time limit either: a dead container is infrastructure, anything else nothing attributes.

`kill_survived` is the harness's own record too (deadline.py, O34): a process of the cell outlived its kill at the cap
(a read waiting in the kernel on the FUSE mount), so the mount was aborted or the sandbox stopped. It overrides what the
counters say, since after an abort or a stop they describe the harness's doing, not the cell's.

One cause per run, in this order (kills can correlate with the condition, since a download may be charged to memory twice, so a cause must not depend on which
was looked at first): `kill_survived`, `earlyoom` (its journal, whatever the counters and the exit code), `container_dead`, `host_oom`, `memory_limit`, `time_limit`,
`killed_other`, `unknown`. An infrastructure cause ends the run and retries it; the agent's own cause, or an exception in its code, is its outcome and is kept. `run_cause`
applies the order to a run's cells.
"""

from __future__ import annotations

import re

KILL_SURVIVED = "kill_survived"
REASONS = (
    "memory_limit", "host_oom", "earlyoom", "container_dead", "time_limit", "killed_other", "unknown", KILL_SURVIVED,
)  # fmt: skip
CATEGORY = {
    "memory_limit": "agent",
    "time_limit": "agent",
    "host_oom": "infrastructure",
    "earlyoom": "infrastructure",
    "container_dead": "infrastructure",
    "killed_other": "unknown",
    "unknown": "unknown",
    KILL_SURVIVED: "infrastructure",
}


PRECEDENCE = (
    KILL_SURVIVED, "earlyoom", "container_dead", "host_oom", "memory_limit", "time_limit", "killed_other", "unknown",
)  # fmt: skip


def survived_cause(record: dict) -> dict:
    """A cell whose process outlived its kill at the cap (`deadline.enforce`'s record): infrastructure, whatever the
    counters, the container's state and the exit code say, with how its deadline ended."""
    return {
        "reason": KILL_SURVIVED,
        "category": CATEGORY[KILL_SURVIVED],
        "basis": f"a process of the cell outlived its kill at the cap; the deadline ended as {record.get('outcome')}",
        "deadline": record,
    }


def run_cause(reasons) -> str | None:
    """The one cause of a run with these cell causes, None with none. An unrecognised reason counts as `unknown`."""
    seen = {r if r in PRECEDENCE else "unknown" for r in reasons}
    return next((r for r in PRECEDENCE if r in seen), None)


def earlyoom_cause(kills: list[dict]) -> dict:
    """A cell of a run earlyoom hit (earlyoom.py): infrastructure whatever else the counters say, and whatever the cell's
    exit code, since it leaves no counter and no kernel line, and the victim may be another container of the run."""
    who = ", ".join(sorted({f"{k['comm']} ({k['role']})" for k in kills}))
    return {
        "reason": "earlyoom",
        "category": CATEGORY["earlyoom"],
        "basis": f"earlyoom's journal: {len(kills)} kill(s) of this run: {who}",
        "earlyoom_kills": kills,
    }


def parse_events(text: str) -> dict[str, int] | None:
    """`memory.events` content -> counters; None when it is not the expected `key value` lines."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 2 or not parts[1].isdigit():
            return None
        out[parts[0]] = int(parts[1])
    return out or None


_KLINE = re.compile(r"oom-kill:constraint=(?P<c>CONSTRAINT_\w+)(?P<rest>.*)")


def parse_kernel_oom(log: str, since_s: float | None = None) -> list[dict]:
    """The OOM-kill lines of a `dmesg` dump (raw `[seconds]` stamps): [{"t", "constraint", "text"}], those at or after
    `since_s` seconds of uptime when given."""
    out = []
    for line in log.splitlines():
        m = _KLINE.search(line)
        if not m:
            continue
        t = re.match(r"\s*\[\s*(\d+(?:\.\d+)?)\]", line)
        ts = float(t.group(1)) if t else None
        if since_s is not None and (ts is None or ts < since_s):
            continue
        mc = re.search(r"oom_memcg=(\S+?)(?:,|$|\s)", line)
        out.append(
            {"t": ts, "constraint": m.group("c"), "oom_memcg": mc.group(1) if mc else "", "text": line.strip()[:400]}
        )
    return out


def classify(
    before: dict[str, int] | None,
    after: dict[str, int] | None,
    *,
    container_running: bool | None,
    timer_fired: bool,
    timeout_s: float,
    kernel: list[dict] | None = None,
    container_id: str | None = None,
) -> dict:
    """The cause of a cell that the harness has evidence was killed. Returns {"reason", "category", "basis", ...deltas}.

    `timer_fired`: the harness's own timer ended the cell (recorded when it fired); the only way to `time_limit`, and how long
    the cell ran is no input at all (the caller records it). `kernel` is the OOM-kill lines of the cell's window (None: the log
    could not be read). Never guesses: conflicting or missing evidence gives `unknown`, with the evidence that was available in
    `basis`.
    """
    d = {}
    if before is not None and after is not None:
        d = {k: after.get(k, 0) - before.get(k, 0) for k in ("oom", "oom_kill")}
    # Kernel lines count only when they name THIS container's cgroup (its id in the line): another container's kill, or a
    # host kill of an unrelated process, says nothing about this cell. Without the container id no line can be attributed.
    mine = [k for k in (kernel or []) if container_id and container_id in k["text"]]
    # the limit that was hit is `oom_memcg`: this container's own cgroup = its own limit; an ancestor's limit (the container is only
    # the victim, `task_memcg`) or CONSTRAINT_NONE = a limit the agent's allocation did not set, so not the agent's outcome
    own = [k for k in mine if k["constraint"] == "CONSTRAINT_MEMCG" and container_id in k.get("oom_memcg", "")]
    host = [
        k
        for k in mine
        if k["constraint"] == "CONSTRAINT_NONE"
        or (k["constraint"] == "CONSTRAINT_MEMCG" and container_id not in k.get("oom_memcg", ""))
    ]
    facts = {
        "timer_fired": timer_fired,
        "events_delta": d or None,
        "kernel_lines": None if kernel is None else len(kernel),
        "kernel_memcg_own": len(own),
        "kernel_host": len(host),
    }

    def out(reason: str, basis: str) -> dict:
        return {"reason": reason, "category": CATEGORY[reason], "basis": basis, **facts}

    if container_running is False:  # a dead sandbox ends the run whatever else is on record: infrastructure, retried
        return out(
            "container_dead",
            "the container is no longer running"
            + (f" (OOM counters in the cell's window: {d})" if d and d.get("oom_kill") else ""),
        )
    if d.get("oom_kill", 0) > 0:
        if d.get("oom", 0) > 0:
            if host and not own:
                return out(
                    "unknown", "memory.events says the container limit, the kernel log says the host: conflicting"
                )
            if (
                host
            ):  # both happened in this window, each with its own line: the order decides, whichever the counters show
                return out(
                    "host_oom",
                    "the kernel log has both the container's limit and the host's killing a process of this container",
                )
            return out("memory_limit", "memory.events: oom and oom_kill rose" + (" (kernel log agrees)" if own else ""))
        if own and not host:
            return out("unknown", "memory.events says the host, the kernel log says the container limit: conflicting")
        return out(
            "host_oom",
            "memory.events: oom_kill rose without oom (not this cgroup's own limit)"
            + (" (kernel log agrees)" if host else ""),
        )
    if not d and kernel:  # counters unreadable: the kernel log alone
        if own and not host:
            return out("memory_limit", "kernel log: CONSTRAINT_MEMCG of this container (memory.events unreadable)")
        if host and not own:
            return out(
                "host_oom", "kernel log: CONSTRAINT_NONE killing a process of this container (memory.events unreadable)"
            )
        return out("unknown", "memory.events unreadable and the kernel log has no unambiguous line for this container")
    if timer_fired:  # recorded by the harness when its timer fired: never inferred from how long the cell ran
        return out(
            "time_limit",
            f"the harness's timer fired at the cell's limit of {timeout_s:g} s"
            + ("" if d else " (memory.events unreadable)"),
        )
    if not d and container_running is None:
        return out("unknown", "nothing could be read: memory.events, the container state, the kernel log")
    if not d:
        return out("unknown", "memory.events unreadable; the harness's timer did not fire")
    return out("killed_other", "no OOM kill counted, the container runs, the harness's timer did not fire")
