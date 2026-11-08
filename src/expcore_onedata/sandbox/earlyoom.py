# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/earlyoom.py, blob 5c1e57bbb7b8201ebb96325784b36744f9da5d27
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""earlyoom on the run host: its kills are host infrastructure, never a run's outcome (O17).

earlyoom is a userspace killer: it leaves no memory.events counter and no kernel line, and it signals SIGTERM first (at
`-m` TERM, then SIGKILL at the lower figure). So the exit code says nothing: a cell it hit exits 143 or 137, and when the victim is
the gate or oneclient the cell only sees a failed read, and may catch that and exit 0. And it picks the highest oom_score on the
whole host, so the victim may be in any container of any run. The evidence is its log and nothing else:

  * `read_kills`: every "sending <SIGNAL> to process" line of the journal in a window; None when the journal cannot be read
    (unknown, never "none"; a host without earlyoom has an empty, readable journal).
  * `attribute`: the kills of one run, by host PID, against the PIDs seen in that run's containers. /proc of the victim is gone
    after the kill, so the sandbox records its containers' `cgroup.procs` beforehand, once a second.
  * `killcause.earlyoom_cause`: the classification, infrastructure, which the harness retries.

The driver must not be a candidate at all. earlyoom's `--avoid` only subtracts 300 from a score and `--ignore` is the absolute
form, both on the comm name, and a driver is plain `python`: so a driver names itself (`name_this_driver`) and the host's earlyoom
has `--ignore ^<that name>$` (`[sandbox] earlyoom_comm`). Not oom_score_adj -1000, which needs root, is inherited by everything the driver forks and also
exempts it from the kernel's and memcap's OOM kills, leaving a runaway driver with no killer at all.
"""

from __future__ import annotations

import ctypes
import dataclasses
import logging
import re
from pathlib import Path

from expcore_onedata.sandbox.hostops import HostOps

log = logging.getLogger(__name__)

DEFAULTS_FILE = Path("/etc/default/earlyoom")
PR_SET_NAME = 15
_LINE = re.compile(
    r'sending (?P<sig>SIG[A-Z]+) to process (?P<pid>\d+) uid (?P<uid>\d+) "(?P<comm>[^"]*)": '
    r"oom_score (?P<score>-?\d+), oom_score_adj (?P<adj>-?\d+), VmRSS (?P<rss>\d+) MiB"
)


@dataclasses.dataclass(frozen=True)
class Kill:
    t: float  # epoch seconds, from the journal
    signal: str
    pid: int
    uid: int
    comm: str
    oom_score: int
    oom_score_adj: int
    rss_mib: int

    def record(self) -> dict:
        return dataclasses.asdict(self)


def parse_kills(journal_text: str) -> list[Kill]:
    """The kill lines of `journalctl -o short-unix` output. A dry run prints the same line, followed by a line saying it sent nothing:
    such a line is not a kill, but the service never runs dry, and its journal is the only source here."""
    out = []
    for line in journal_text.splitlines():
        m = _LINE.search(line)
        if not m:
            continue
        try:
            t = float(line.split(None, 1)[0])
        except (ValueError, IndexError):
            continue
        out.append(
            Kill(t, m["sig"], int(m["pid"]), int(m["uid"]), m["comm"], int(m["score"]), int(m["adj"]), int(m["rss"]))
        )
    return out


def read_kills(host: HostOps, since: float, until: float | None = None) -> list[Kill] | None:
    """Kills from `since` (epoch seconds) to `until`; None when the journal cannot be read."""
    p = host.run(
        ["journalctl", "-u", "earlyoom", "--no-pager", "-q", "-o", "short-unix", "--since", f"@{int(since)}"], 20
    )
    if p.returncode != 0:
        log.error("earlyoom: journalctl exited %s: %s", p.returncode, (p.stderr or "")[-200:])
        return None
    return [k for k in parse_kills(p.stdout) if k.t >= since and (until is None or k.t <= until)]


SPAN_GAP_S = (
    5.0  # not seen for this long at a one-second sampling: gone, so a later sighting is a new process with a reused PID
)


def note_pids(mine: dict[int, list[list[float]]], procs_text: str, now: float) -> None:
    """Record the PIDs of a `cgroup.procs` text: pid -> the spans [first seen, last seen] of its life in this run. Separate lives of a reused PID stay separate."""
    for pid in (int(x) for x in procs_text.split()):
        spans = mine.setdefault(pid, [])
        if spans and now - spans[-1][1] <= SPAN_GAP_S:
            spans[-1][1] = now
        else:
            spans.append([now, now])


def attribute(
    kills: list[Kill],
    pids_by_role: dict[str, dict[int, list[list[float]]]],
    *,
    before_s: float = 2.0,
    after_s: float = 30.0,
) -> list[dict]:
    """The kills of one run, each with the container it hit. A kill outside the spans of a PID's life in this run, with these margins for the sampling, hit
    whoever had that PID then, not this run. (The trailing margin is for a stalled sampler: to wrongly match inside it, the PID would have to be reused within 30 s, a
    wrap of pid_max, 4194304 on the run hosts.)"""
    out = []
    for k in kills:
        for role, pids in pids_by_role.items():
            if any(s[0] - before_s <= k.t <= s[1] + after_s for s in pids.get(k.pid, ())):
                out.append({**k.record(), "role": role})
                break
    return out


def unsampled_kills(
    kills: list[Kill], attributed: list[dict], since: float, until: float, uid: int | None
) -> list[dict]:
    """For a cell that died by a signal with no kill attributed by PID: a process born and killed between two samples never had its PID recorded. Kills of this uid in the
    cell's window that no PID here explains are charged to it. If one was another run's, the cost is one retry, against an earlyoom kill recorded as the agent's outcome."""
    known = {(k["pid"], k["t"]) for k in attributed}
    return [
        {**k.record(), "role": "agent (by uid and time: no PID sample)"}
        for k in kills
        if (k.pid, k.t) not in known and since - 1.0 <= k.t <= until + 1.0 and (uid is None or k.uid == uid)
    ]


def name_this_driver(comm: str) -> bool:
    """Give this process the comm name the host's earlyoom ignores. Main thread only: /proc/<pid>/comm is the main thread's name, and
    a thread's name is its own. False (and logged) when it did not take. `comm` is at most 15 bytes (the kernel's limit)."""
    import threading

    if threading.current_thread() is not threading.main_thread():
        log.error("name_this_driver must be called from the main thread")
        return False
    try:
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_NAME, comm.encode(), 0, 0, 0)
        return Path("/proc/self/comm").read_text().strip() == comm
    except (OSError, AttributeError) as e:
        log.error("name_this_driver failed: %r", e)
        return False


def active(host: HostOps) -> bool | None:
    """Whether the earlyoom service runs on this host; None when it is changing state or systemd cannot say."""
    p = host.run(["systemctl", "is-active", "earlyoom"], 10)
    if p.returncode == 127 or p.returncode == 124:
        return None
    state = p.stdout.strip()
    return True if state == "active" else False if state in ("inactive", "failed", "unknown") else None


def running_argv(host: HostOps) -> list[str] | None:
    """The arguments the running service was started with, which is what decides, not the settings file: that may have been edited since
    the last restart, or an override may ignore it. None when they cannot be read."""
    p = host.run(["systemctl", "show", "earlyoom", "-p", "MainPID", "--value"], 10)
    pid = p.stdout.strip()
    if p.returncode != 0 or not pid.isdigit() or pid == "0":
        return None
    try:
        return [a for a in host.read_text(f"/proc/{pid}/cmdline").split("\0") if a]
    except OSError:
        return None


def ignore_regex(argv: list[str]) -> str | None:
    """The value of `--ignore`, None without one."""
    return argv[argv.index("--ignore") + 1] if "--ignore" in argv and argv.index("--ignore") + 1 < len(argv) else None


# The sandbox's own processes are the only ones earlyoom may take, never the driver or `docker`. earlyoom has no way to name who may die, only who may not, and goes by
# oom_score. So every process of an agent's container has a high oom_score_adj, which outranks everything else while one exists, and the driver is out by
# name. Not `python3`: an agent's own `python3` must stay killable. The gate (root, comm `python`, which no name could tell from an agent's) and oneclient rank below every
# process of every agent, so they can only be picked with no agent process left on the host; and what they lose is earlyoom's, by role, infrastructure and retried.
BASE_PROTECTED = (
    "docker",
    "dockerd",
    "containerd",
    "containerd-shim",
    "ssh",
    "sshd",
    "sshd-session",
    "systemd",
    "(sd-pam)",
    "dbus-daemon",
    "tailscaled",
)


def protected_comms(comm: str) -> tuple[str, ...]:
    """Every process name the host's earlyoom must leave alone: the driver's and the host's own services."""
    return (comm, *BASE_PROTECTED)


def ignore_pattern(comm: str) -> str:
    """The `--ignore` value for the host's earlyoom service."""
    return f"^(dockerd|containerd.*|sshd.*|systemd.*|tailscaled|{re.escape(comm)}|dbus-daemon|.sd-pam.|docker|ssh)$"


def earlyoom_args(comm: str) -> str:
    """The arguments the host's earlyoom service needs so that it never takes the driver."""
    return f"-m 6,3 -r 3600 --ignore {ignore_pattern(comm)}"


def harness_protected(argv: list[str] | None, comm: str) -> bool:
    """The running service leaves every name above alone. Read from its own arguments, since a line can change on the way: `\\(sd-pam\\)` reached the service as `(sd-pam)`, a group that matches
    no such name. `--avoid` is no answer: it only subtracts from a score."""
    rx = ignore_regex(argv) if argv else None
    try:
        return bool(rx) and all(re.search(rx, c) for c in protected_comms(comm))
    except re.error:
        return False


def driver_exempt(argv: list[str] | None, comm: str) -> bool:
    rx = ignore_regex(argv) if argv else None
    try:
        return bool(rx) and re.search(rx, comm) is not None
    except re.error:
        return False


def require_driver_exempt(host: HostOps, comm: str, *, own_comm: str | None = None) -> dict:
    """For a driver that holds timed runs: where earlyoom runs, the running service must ignore this process's name, and this process
    must carry it. Refuses rather than discovering it when the campaign process is killed. A host without earlyoom passes."""
    state = active(host)
    if state is False:
        return {"earlyoom_active": False}
    argv = running_argv(host)
    named = (own_comm if own_comm is not None else host.read_text("/proc/self/comm").strip()) == comm
    if state is None or not driver_exempt(argv, comm) or not harness_protected(argv, comm) or not named:
        raise RuntimeError(
            f"earlyoom could kill this driver or the harness: service active: {state}, its arguments {argv}, this process named {comm}: "
            f'{named}. The running service needs EARLYOOM_ARGS="{earlyoom_args(comm)}" (set it in {DEFAULTS_FILE} and restart earlyoom)'
        )
    return {"earlyoom_active": True, "argv": argv}
