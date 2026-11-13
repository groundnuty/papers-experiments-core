# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/teardown.py, blob 9bd9d7eb81887f42865ed074642b06928be56a58
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Ordered teardown of a sandbox that holds a oneclient FUSE mount (a mount data path), for every path that ends one (O15).

Why: the VM froze on 1 Nov at 23:48Z while docker was killing a mount sandbox's agent container (`docker rm -f` at 23:47:59Z,
"failed to exit within 10s", the direct SIGKILL at 23:48:09Z, then nothing in the kernel log). The old `Sandbox.stop`
removed the agent container while the sidecar's FUSE mount was still bound into it (rslave), then SIGKILLed the sidecar,
and unmounted last. The order that works: stop the reader, unmount (lazily if it must), then the sidecar,
then the agent container.

`ordered_fuse_teardown` does that, every wait bounded and every step logged (one JSONL line per step, fsynced, so a freeze
in the middle of a teardown leaves its last step on disk):

  1. readers: SIGTERM, then SIGKILL, to every process of the agent container except its init and the init's `sleep`
     (found from the host through the container's cgroup: a `docker exec` fails at the pids limit). Best effort: a reader
     whose FUSE request is already with oneclient sleeps uninterruptibly (D) until oneclient answers or the connection
     ends (step 3); the survivors, their states and wait channels are logged, and the next step runs regardless.
  2. unmount, at the host's mount point (the sidecar's mount propagates there): `fusermount -u`, retried until a deadline,
     then the lazy `fusermount -uz`, then `umount -l`; checked in the host's mountinfo. That takes the host's and the sidecar's
     copies but NOT the agent's (measured on the VM, 2 Nov): docker bound the FUSE root itself at the agent's /data, a mount of
     its own and not one the host's unmount propagates to, so it alone keeps the connection alive. It is lazily unmounted in
     the agent's namespace through nsenter, and the sidecar's is checked the same way. The last resort: when the mount could not be released, or
     readers are still in D state after that deadline, those readers are recorded and the FUSE connection is aborted
     through fusectl, which ends every request waiting on it. If that fails too, the D-state PIDs stay on record and the
     order goes on (held, below, only if the mount itself is still live).
  3. the sidecar: `docker stop -t` (oneclient exits and closes /dev/fuse, which ends every request still waiting); if a
     reader is still alive or the sidecar still runs, the connection is aborted (again); then removed.
  4. the agent container: removed only now.

The rule: no container of the sandbox is killed while its FUSE mount is live. When the mount can be neither unmounted
nor aborted, the sidecar and the agent are left running and `fuse_safe` is False: the caller must not remove them
(`Sandbox.stop` then raises and the run's slot is quarantined), rather than SIGKILL them with the mount live.

The FUSE connection's `waiting` counter (requests in flight, /sys/fs/fuse/connections/N/waiting) is logged before the
steps: whether reads were in flight at the kill is the open question of the 1 Nov freeze.

What the host sampler sees (hostwatch.py): readers stuck in D, every abort and every held sandbox are also appended to
`<work_root>/_teardown/events.jsonl`, across processes; each sample carries the new ones and the running counts, and a held
sandbox or a failed abort closes the dispatch gate. The stuck tasks themselves also show in each sample's D-state list.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import json
import logging
import math
import os
import re
import time
from pathlib import Path

from expcore_onedata.sandbox import hostops
from expcore_onedata.sandbox.hostops import HostOps

log = logging.getLogger(__name__)


def _sudo(host: HostOps, *argv: str, timeout_s: float):
    """Run a privileged command within a time bound; its exit code is the caller's to read."""
    return host.run(list(argv), timeout_s, sudo=True)


def _read(host: HostOps, path: Path | str) -> str | None:
    """A host file's text; None when it cannot be read (unknown, never empty)."""
    try:
        return host.read_text(path)
    except OSError:
        return None


PROC = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")
FUSECTL = Path("/sys/fs/fuse/connections")
SIDECAR_SUFFIX = "-oneclient"
SIDECAR_MOUNT = "/mnt/oneclient"  # where the sidecar mounts (Sandbox._start_mount)
TEARDOWN_LOG_DIR = "_teardown"  # under the sandboxes' work root: <work_root>/_teardown/<sid>.jsonl
EVENTS_FILE = "events.jsonl"  # in the same directory: what the host sampler reads


@dataclasses.dataclass(frozen=True)
class Waits:
    reader_term_s: float = 5.0  # after SIGTERM, before SIGKILL
    reader_kill_s: float = 10.0  # after SIGKILL, before going on with the survivors
    after_abort_s: float = 2.0  # after an abort, for the readers it freed to go
    unmount_s: float = 10.0  # `fusermount -u` retried until then, then the lazy forms
    sidecar_stop_s: int = 10  # `docker stop -t`
    command_s: float = 20.0  # any one command
    probe_s: float = 5.0  # a diagnostic read (mountinfo, a fusectl counter)
    poll_s: float = 0.5


WAITS = Waits()


@dataclasses.dataclass
class TeardownResult:
    fuse_safe: bool  # the FUSE mount is gone (unmounted) or dead (aborted): the containers may now be removed
    removed: list[str]
    errors: list[str]
    log: list[dict]


class _StepLog:
    """Each step as a record: kept in memory, logged, and appended (flushed and fsynced) to a JSONL file when given."""

    def __init__(self, path: Path | None, ctx: dict):
        self.records: list[dict] = []
        self.ctx, self.t0, self._fh = ctx, time.monotonic(), None
        self._events = Path(path).parent / EVENTS_FILE if path is not None else None
        if path is not None:
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(path, "a")  # noqa: SIM115 -- held open for the whole teardown; closed in `close`
            except OSError as e:
                self.ctx = {**ctx, "log_file_error": hostops.describe_error(e)}

    def __call__(self, step: str, **kw) -> dict:
        rec = {
            "utc": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"),
            "t_s": round(time.monotonic() - self.t0, 3),
            "step": step,
            **self.ctx,
            **kw,
        }
        self.records.append(rec)
        log.info("teardown %s", json.dumps(rec, default=str))
        if self._fh is not None:
            try:
                self._fh.write(json.dumps(rec, default=str) + "\n")
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except OSError:
                pass
        return rec

    def event(self, step: str, **kw) -> dict:
        """A step the host sampler must see: logged as usual and appended to the shared events file (O_APPEND, one line,
        fsynced: several processes write it, and the last events before a freeze are the evidence)."""
        rec = self(step, **kw)
        if self._events is not None:
            try:
                with open(self._events, "a") as f:
                    f.write(json.dumps(rec, default=str) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as e:
                log.error("teardown: the events file could not be written: %s", hostops.describe_error(e))
        return rec

    def close(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()


# ------------------------------------------------------------------------------------------------ processes
UNKNOWN = "unknown"  # what `_stat` says of a process whose /proc state cannot be read or parsed: neither alive nor gone


def _stat_text(host: HostOps, pid: int) -> str | None:
    """The /proc/<pid>/stat line; None only when the process is gone, which only ENOENT on its /proc entry proves (O15).
    Any other failure (a PermissionError where /proc hides other users' processes, EIO, ESRCH in a race) raises
    `OSError`: the process may well be alive, and the caller must treat it as unknown."""
    try:
        return host.read_text(PROC / str(pid) / "stat")
    except FileNotFoundError:
        return None


def _gone(host: HostOps, pid: int) -> bool:
    """Whether a process is proved gone, and with it the hold its mount namespace kept on a copy of the FUSE mount: only
    ENOENT on its /proc entry proves that (O15). An entry that cannot be read is unknown, and unknown is never gone."""
    try:
        return _stat_text(host, pid) is None
    except OSError:
        return False


def _stat(host: HostOps, pid: int) -> dict | str | None:
    """State, parent, command name and start time of a process (/proc/<pid>/stat). None when it is gone (ENOENT) or a
    zombie (it holds no file and waits on no request); `UNKNOWN` when its entry cannot be read (a PermissionError, any
    other OSError) or parsed: it may be alive, so it is never taken for gone (O15)."""
    try:
        s = _stat_text(host, pid)
    except OSError:
        return UNKNOWN
    if s is None:
        return None
    close = s.rfind(")")
    rest = s[close + 2 :].split()
    if close < 0 or len(rest) < 20 or not rest[1].lstrip("-").isdigit():
        return UNKNOWN
    if rest[0] in ("Z", "X", "x"):
        return None
    return {
        "pid": pid,
        "state": rest[0],
        "ppid": int(rest[1]),
        "comm": s[s.find("(") + 1 : close],
        "start": rest[19],
    }


def _wchan(host: HostOps, pid: int, waits: Waits) -> str | None:
    """The wait channel, None when unknown. Without ptrace access the kernel answers "0" (the agent runs as another uid:
    measured on dockerd here, "0" without sudo and "futex_wait_queue" with), so "0" is read again through sudo."""
    path = PROC / str(pid) / "wchan"
    try:
        v = host.read_text(path).strip()
    except OSError:
        v = ""
    if v and v != "0":
        return v
    r = _sudo(host, "cat", str(path), timeout_s=waits.probe_s)
    v = r.stdout.strip() if r.returncode == 0 else ""
    return v if v and v != "0" else None


def _inspect(host: HostOps, name: str, waits: Waits) -> dict | None:
    """{"pid", "id", "running"} of a container; None when docker says it does not exist; {"unknown": True, ...} when docker
    cannot say (daemon trouble, a timeout): it may be running with its copy of the mount, so it is not treated as absent."""
    r = host.docker("inspect", name, check=False, timeout=waits.command_s)
    info = hostops.inspect_from(r)
    if info is None:
        if r.returncode == 1 and re.search(
            r"no such (object|container)", r.stderr.lower()
        ):  # not "no such file": a missing docker socket
            return None
        return {"pid": 0, "id": "", "running": None, "unknown": True}
    state = info.get("State") or {}
    return {"pid": int(state.get("Pid") or 0), "id": str(info.get("Id", "")), "running": bool(state.get("Running"))}


def _cgroup_procs(host: HostOps, cid: str) -> list[int] | None:
    for cand in (CGROUP_ROOT / "system.slice" / f"docker-{cid}.scope", CGROUP_ROOT / "docker" / cid):
        try:
            return [int(x) for x in host.read_text(cand / "cgroup.procs").split()]
        except (OSError, ValueError):
            continue
    return None


def init_sleep(host: HostOps, agent: dict) -> dict | None:
    """The init's `sleep infinity` of an agent container that has just started, by its pid and start time (/proc/<pid>/stat
    field 22): the one process of its cgroup besides the init, a child of the init named `sleep`. None while that is not
    what the cgroup holds (the init has not started it yet), or when it cannot be read (O30)."""
    procs = _cgroup_procs(host, agent["id"])
    others = [pid for pid in procs or [] if pid != agent["pid"]]
    if len(others) != 1:
        return None
    st = _stat(host, others[0])
    if not isinstance(st, dict) or st["ppid"] != agent["pid"] or st["comm"] != "sleep":
        return None
    return {"pid": st["pid"], "start": st["start"]}


def _ticks(start: str) -> float:
    return int(start) if start.isdigit() else math.inf


def _the_init_sleep(procs: list[dict], agent: dict) -> dict | None:
    """Which of the container's processes is its init's `sleep`: the one `agent["sleep"]` names by its pid and start
    time, as `init_sleep` read them when the container started. A process of the agent's can name itself `sleep`
    (/proc/self/comm is its own to write) and, orphaned, be reparented to the init; it cannot take that start time
    (O30). Without that record (the orphan reaper's teardown, `fuse_first`, knows nothing of the start) it is the
    init's child of that name that started first, which no process of the agent's can predate."""
    known = agent.get("sleep")
    if known is not None:
        return next((p for p in procs if (p["pid"], p["start"]) == (known["pid"], known["start"])), None)
    named = [p for p in procs if p["ppid"] == agent["pid"] and p["comm"] == "sleep"]
    return min(named, key=lambda p: _ticks(p["start"]), default=None)


def _readers(host: HostOps, agent: dict) -> list[dict] | None:
    """The agent container's processes except its init and the init's `sleep infinity` (`_the_init_sleep`; keeping those
    keeps the container, and with it its mount namespace, alive until step 4). None when the container's cgroup cannot
    be read, or the state of one of its processes cannot: it could be a reader as well as the init's `sleep`, so the
    list is unknown (O15)."""
    procs = _cgroup_procs(host, agent["id"])
    if procs is None:
        return None
    found = []
    for pid in procs:
        if pid == agent["pid"]:
            continue
        st = _stat(host, pid)
        if st == UNKNOWN:
            return None
        if isinstance(st, dict):
            found.append(st)
    spared = _the_init_sleep(found, agent)
    return [st for st in found if st is not spared]


def _alive(host: HostOps, procs: list[dict]) -> list[dict]:
    """The ones still alive (same pid AND start time: a reused pid is not a survivor). One whose state can no longer be
    read is kept as it was last seen, with `unknown` set: unknown is never gone (O15)."""
    out = []
    for p in procs:
        st = _stat(host, p["pid"])
        if st == UNKNOWN:
            out.append({**p, "unknown": True})
        elif isinstance(st, dict) and st["start"] == p["start"]:
            out.append(st)
    return out


def _brief(host: HostOps, procs: list[dict], waits: Waits) -> list[dict]:
    """For the log; the wait channel of the first few in D (it takes a sudo each, so it is read here, not while polling)."""
    out, asked = [], 0
    for p in procs[:32]:
        e = {k: p[k] for k in ("pid", "comm", "state")}
        if p["state"] == "D" and asked < 8:
            asked += 1
            e["wchan"] = _wchan(host, p["pid"], waits)
        out.append(e)
    return out


def _wait_gone(host: HostOps, procs: list[dict], seconds: float, poll: float) -> list[dict]:
    deadline = time.monotonic() + seconds
    while True:
        left = _alive(host, procs)
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(poll)


def _stop_readers(host: HostOps, agent: dict | None, waits: Waits, L: _StepLog) -> list[dict]:
    """Step 1. Returns the readers it signalled (the survivors are checked again later)."""
    if agent is None or not agent["running"] or agent["pid"] <= 0:
        L("readers", outcome="skipped", detail="no running agent container")
        return []
    procs = _readers(host, agent)
    if procs is None:
        L("readers", outcome="unknown", detail="the agent container's cgroup could not be read: readers not signalled")
        return []
    if not procs:
        L("readers", outcome="none", detail="no process besides init and its sleep")
        return []
    pids = [str(p["pid"]) for p in procs]
    r = _sudo(host, "kill", "-s", "TERM", *pids, timeout_s=waits.command_s)
    left = _wait_gone(host, procs, waits.reader_term_s, waits.poll_s)
    L(
        "readers",
        signal="TERM",
        signalled=len(procs),
        rc=r.returncode,
        left=len(left),
        left_procs=_brief(host, left, waits),
    )
    if left:
        r = _sudo(host, "kill", "-s", "KILL", *[str(p["pid"]) for p in left], timeout_s=waits.command_s)
        left = _wait_gone(host, left, waits.reader_kill_s, waits.poll_s)
        L(
            "readers",
            signal="KILL",
            rc=r.returncode,
            left=len(left),
            d_state=sum(p["state"] == "D" for p in left),
            left_procs=_brief(host, left, waits),
        )
    return procs


# ------------------------------------------------------------------------------------------------ the FUSE mount
def host_path(mountpoint: Path | str) -> str:
    """The path as the host's mountinfo shows it. Only the parent is resolved: resolving the mount point itself would
    stat the FUSE root, a request to the very daemon that may be stuck."""
    p = Path(mountpoint)
    return os.path.join(os.path.realpath(p.parent), p.name)


def _fuse_at(host: HostOps, mp: str, waits: Waits) -> list[dict] | None:
    """The FUSE mounts at `mp` in the host's mountinfo; None when it cannot be read in time (unknown, never "none")."""
    mi = _read(host, PROC / "self" / "mountinfo")
    try:
        return None if mi is None else [m for m in hostops.fuse_mounts(mi) if m["mountpoint"] == mp]
    except hostops.MountTableError:
        return None  # a table that is empty, cut off or malformed says nothing: unknown, never "none"


def _conn(dev: str) -> str | None:
    """The fusectl directory name of a FUSE mount's device ("0:N" -> "N")."""
    major, _, minor = dev.partition(":")
    return minor if major == "0" and minor.isdigit() else None


def _waiting(host: HostOps, devs: list[str], waits: Waits) -> dict:
    out = {}
    for d in devs:
        n = _conn(d)
        r = _sudo(host, "cat", str(FUSECTL / n / "waiting"), timeout_s=waits.probe_s) if n else None
        out[d] = int(r.stdout.strip()) if r is not None and r.returncode == 0 and r.stdout.strip().isdigit() else None
    return out


def _abort(host: HostOps, devs: list[str], waits: Waits) -> dict:
    """Abort each FUSE connection (every request ends with an error; the mount is dead). A connection whose fusectl entry
    is already gone counts as ended."""
    out = {}
    for d in devs:
        n = _conn(d)
        if n is None:
            out[d] = "unknown device"
            continue
        if not host.exists(FUSECTL / n):
            out[d] = "gone"
            continue
        r = _sudo(host, "sh", "-c", f"echo 1 > {FUSECTL / n / 'abort'}", timeout_s=waits.command_s)
        out[d] = "aborted" if r.returncode == 0 else f"failed rc {r.returncode}: {hostops.describe_stderr(r.stderr)}"
    return out


def _unmount_host(host: HostOps, mp: str, fuse0: list[dict] | None, waits: Waits, L: _StepLog) -> bool:
    """Step 2 at the host's mount point. True when no FUSE mount is left there (or a lazy unmount succeeded while the
    mount table could not be read)."""
    if fuse0 == []:
        L("unmount", outcome="none", detail="no FUSE mount at the mount point")
        return True
    deadline, attempts, r = time.monotonic() + waits.unmount_s, 0, None
    while True:
        r = _sudo(host, "fusermount", "-u", mp, timeout_s=waits.command_s)
        attempts += 1
        now = _fuse_at(host, mp, waits)
        if now == [] or (now is None and r.returncode == 0):
            L("unmount", outcome="unmounted", method="fusermount -u", attempts=attempts, verified=now == [])
            return True
        if time.monotonic() >= deadline:
            break
        time.sleep(waits.poll_s)
    L(
        "unmount",
        outcome="busy",
        method="fusermount -u",
        attempts=attempts,
        rc=r.returncode,
        stderr=hostops.stderr_digest(r.stderr),
    )
    for method, cmd in (("fusermount -uz", ["fusermount", "-uz", mp]), ("umount -l", ["umount", "-l", mp])):
        r = _sudo(host, *cmd, timeout_s=waits.command_s)
        now = _fuse_at(host, mp, waits)
        if now == [] or (now is None and r.returncode == 0):
            L("unmount", outcome="detached", method=method, rc=r.returncode, verified=now == [])
            return True
        L(
            "unmount",
            outcome="failed",
            method=method,
            rc=r.returncode,
            stderr=hostops.stderr_digest(r.stderr),
            fuse_left=None if now is None else len(now),
        )
    return False


def _ns_copies(host: HostOps, pid: int, waits: Waits) -> list[dict] | None:
    """The FUSE mounts in a container's own mount namespace; None when they cannot be read. Each is this sandbox's: the
    agent has no FUSE of its own (no capabilities, no /dev/fuse), and the sidecar mounts one thing."""
    r = _sudo(host, "cat", str(PROC / str(pid) / "mountinfo"), timeout_s=waits.probe_s)
    if r.returncode != 0:
        return None
    try:
        return hostops.fuse_mounts(r.stdout)
    except hostops.MountTableError:
        return None  # unknown, as when it cannot be read


def _unmount_namespaces(host: HostOps, targets: list[tuple[str, int]], waits: Waits, L: _StepLog) -> bool:
    """Step 2, in the agent's and the sidecar's own mount namespaces: the host's view looks the same whether or not the
    unmount reached them, and it does not reach the agent's (module docstring). True when no copy of the mount is known to be
    left. `-p` as well as `-m`: umount reads /proc/self/mountinfo, and in the container's /proc that resolves only from inside
    its PID namespace (with `-m` alone: "umount failed: No such file or directory", measured on the VM)."""
    clean = True
    for who, pid in targets:
        copies = _ns_copies(host, pid, waits)
        if copies is None:  # unknown is not clean, unless the process and so its namespace have gone (ENOENT only)
            gone = _gone(host, pid)
            L(
                "unmount_ns",
                who=who,
                outcome="gone" if gone else "unreadable",
                detail="the namespace's mountinfo could not be read",
            )
            clean = clean and gone
            continue
        if not copies:
            L("unmount_ns", who=who, outcome="clean")
            continue
        r = None
        for m in copies:
            r = _sudo(
                host, "nsenter", "-t", str(pid), "-m", "-p", "umount", "-l", m["mountpoint"], timeout_s=waits.command_s
            )
        again = _ns_copies(host, pid, waits)
        ok = again == [] or (again is None and _gone(host, pid))
        L(
            "unmount_ns",
            who=who,
            outcome="detached" if ok else "left",
            copies=[m["mountpoint"] for m in copies],
            verified=again == [],
            rc=r.returncode,
            stderr=hostops.stderr_digest(r.stderr),
        )
        clean = clean and ok
    return clean


# ------------------------------------------------------------------------------------------------ the sequence
def ordered_fuse_teardown(
    host: HostOps,
    agent: str | None,
    sidecar: str,
    mountpoint: Path | str,
    *,
    waits: Waits | None = None,
    log_path: Path | None = None,
    extra_readers: tuple[str, ...] | list[str] = (),
    agent_sleep: dict | None = None,
) -> TeardownResult:
    """Stop the readers, unmount, stop the sidecar, remove the agent container (module docstring). Never raises for a
    failed step: each is logged and recorded in `errors`, and the next step runs, except that no container is removed
    while the FUSE mount may still be live (`fuse_safe` False: the caller must leave the sidecar and the agent).
    `agent_sleep` is the agent's init's `sleep` as `init_sleep` identified it at the container's start, the one process
    the readers' step spares besides the init (`_the_init_sleep`)."""
    waits = waits or WAITS
    mp = host_path(mountpoint)
    L = _StepLog(log_path, {"sidecar": hostops.name_digest(sidecar)})
    errors: list[str] = []
    removed: list[str] = []
    try:
        ai = _inspect(host, agent, waits) if agent else None
        if ai is not None and agent_sleep is not None:
            ai = {**ai, "sleep": agent_sleep}
        si = _inspect(host, sidecar, waits)
        fuse0 = _fuse_at(host, mp, waits)
        targets = [(w, i["pid"]) for w, i in (("agent", ai), ("sidecar", si)) if i and i["running"] and i["pid"] > 0]
        unknown = [w for w, i in (("agent", ai), ("sidecar", si)) if i and i.get("unknown")]
        # the connections to end if it comes to that: from the namespaces too, because a retry after an earlier attempt
        # detached the host's copy sees none on the host while a copy may still live in a container
        devs = sorted(
            {m["dev"] for m in fuse0 or []}
            | {m["dev"] for _, pid in targets for m in _ns_copies(host, pid, waits) or []}
        )
        L(
            "begin",
            agent=hostops.name_digest(agent) if agent else None,
            mountpoint=hostops.name_digest(mp),
            agent_init_pid=ai and ai["pid"],
            agent_running=ai and ai["running"],
            sidecar_pid=si and si["pid"],
            sidecar_running=si and si["running"],
            fuse_mounts=None if fuse0 is None else len(fuse0),
            fuse_devs=devs,
            fuse_waiting=_waiting(host, devs, waits),
            containers_unknown=unknown,
        )

        # 1. the readers: first any other container that binds the mount (a control container: its reader is its init, so the container goes as a whole)
        for name in extra_readers:
            r = host.docker("stop", "-t", "2", name, check=False, timeout=2 + waits.command_s)
            after = _inspect(host, name, waits)
            L(
                "readers",
                who=hostops.name_digest(name),
                action="docker stop",
                rc=r.returncode,
                running_after=after and after["running"],
            )
            if after is not None and after["running"] is not False:
                unknown.append(name)
        readers = _stop_readers(host, ai, waits, L)

        # 2. the unmount: the host's mount point, then the containers' own namespaces
        unmounted = _unmount_host(host, mp, fuse0, waits, L)
        if unmounted and targets:
            unmounted = _unmount_namespaces(host, targets, waits, L)
        if unknown:  # docker could not say whether these run: a copy of the mount may be live in one, as with an unreadable namespace
            L(
                "unmount_ns",
                who=",".join(w if w in ("agent", "sidecar") else hostops.name_digest(w) for w in unknown),
                outcome="unknown",
                detail="docker could not say whether the container is running",
            )
            unmounted = False
        left = _alive(host, readers)
        stuck = [p for p in left if p["state"] == "D"]
        why = (["the mount could not be unmounted"] if not unmounted else []) + (
            [f"{len(stuck)} reader(s) still in D state after the unmount deadline"] if stuck else []
        )
        if stuck:
            L.event(
                "readers_stuck_d",
                count=len(stuck),
                pids=[p["pid"] for p in stuck],
                left_procs=_brief(host, stuck, waits),
                fuse_waiting=_waiting(host, devs, waits),
                unmounted=unmounted,
            )
        aborted = False
        if why:  # the last resort, before the sidecar's stop; sudo here is NOPASSWD: ALL, and a failure is recorded
            res = _abort(host, devs, waits)
            aborted = bool(res) and all(v in ("aborted", "gone") for v in res.values())
            left = _wait_gone(host, left, waits.after_abort_s, waits.poll_s) if aborted else _alive(host, readers)
            L.event(
                "abort",
                reason="; ".join(why),
                result=res,
                ok=aborted,
                readers_left=len(left),
                d_state=sum(p["state"] == "D" for p in left),
                left_procs=_brief(host, left, waits),
            )
        if not (unmounted or aborted):
            errors.append(
                "the FUSE mount could be neither unmounted nor aborted: the sidecar and the agent are left running"
            )
            L.event("held", outcome="containers left running", detail="no SIGKILL while the FUSE mount is live")
            return TeardownResult(False, removed, errors, L.records)

        # 3. the sidecar: a graceful stop, so oneclient closes /dev/fuse itself
        w = _waiting(host, devs, waits)
        r = host.docker(
            "stop",
            "-t",
            str(waits.sidecar_stop_s),
            sidecar,
            check=False,
            timeout=waits.sidecar_stop_s + waits.command_s,
        )
        after = _inspect(host, sidecar, waits)
        left = _alive(host, readers)
        L(
            "sidecar",
            action="docker stop",
            rc=r.returncode,
            stderr=hostops.stderr_digest(r.stderr) if r.returncode else None,
            fuse_waiting_before=w,
            sidecar_running_after=after and after["running"],
            readers_left=len(left),
            d_state=sum(p["state"] == "D" for p in left),
            left_procs=_brief(host, left, waits),
        )
        if devs and (left or (after and after["running"])):
            res = _abort(host, devs, waits)
            L.event(
                "abort",
                reason="readers left or the sidecar still running after its stop",
                result=res,
                ok=bool(res) and all(v in ("aborted", "gone") for v in res.values()),
            )
        try:
            hostops.remove_container(host, sidecar)
            removed.append(sidecar)
            L("sidecar", action="removed")
        except Exception as e:
            errors.append(f"the sidecar could not be removed ({type(e).__name__})")
            L("sidecar", action="remove failed", error=hostops.describe_error(e))

        # 4. the agent container, last
        if agent:
            left = _alive(host, readers)
            L(
                "agent",
                action="remove",
                readers_left=len(left),
                d_state=sum(p["state"] == "D" for p in left),
                left_procs=_brief(host, left, waits),
            )
            try:
                hostops.remove_container(host, agent)
                removed.append(agent)
                L("agent", action="removed")
            except Exception as e:
                errors.append(f"the agent could not be removed ({type(e).__name__})")
                L("agent", action="remove failed", error=hostops.describe_error(e))
        L("end", fuse_safe=True, unmounted=unmounted, aborted=aborted, errors=len(errors))
        return TeardownResult(True, removed, errors, L.records)
    finally:
        L.close()


def fuse_first(host: HostOps, names: list[str], *, waits: Waits | None = None, log_dir: Path | None = None) -> set[str]:
    """For a cleanup that removes containers by name without knowing the order (the orphan reaper, a lane's leftovers):
    run the ordered teardown for every oneclient sidecar among `names` (with its agent container, `<sidecar minus
    -oneclient>`) BEFORE the caller removes anything. Returns the names the caller must NOT remove: the sandboxes whose
    FUSE mount could be neither unmounted nor aborted, or whose mount point could not be found."""
    waits = waits or WAITS
    held: set[str] = set()
    for side in [n for n in names if n.endswith(SIDECAR_SUFFIX)]:
        agent = side[: -len(SIDECAR_SUFFIX)]
        info = hostops.inspect(host, side, timeout=waits.command_s)
        src = next(
            (m.get("Source", "") for m in (info or {}).get("Mounts") or [] if m.get("Destination") == SIDECAR_MOUNT), ""
        )
        if not src:
            log.error(
                "teardown: the mount point of a sidecar could not be found: it and its agent are left running (%s, %s)",
                hostops.name_digest(side),
                hostops.name_digest(agent),
            )
            held |= {side, agent}
            continue
        run_dir = Path(src).parent
        lp = (Path(log_dir) if log_dir is not None else run_dir.parent / TEARDOWN_LOG_DIR) / f"{run_dir.name}.jsonl"
        res = ordered_fuse_teardown(host, agent, side, Path(src), waits=waits, log_path=lp)
        if not res.fuse_safe:
            held |= {side, agent}
    return held
