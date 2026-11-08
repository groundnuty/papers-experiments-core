# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/teardown.py, blob 9bd9d7eb81887f42865ed074642b06928be56a58
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Ordered teardown of a sandbox that holds a oneclient FUSE mount (M, M+), for every path that ends one.

Why: the VM froze on 1 Nov at 23:48Z while docker was killing an M sandbox's agent container (`docker rm -f` at 23:47:59Z,
"failed to exit within 10s", the direct SIGKILL at 23:48:09Z, then nothing in the kernel log). The old `Sandbox.stop`
removed the agent container while the sidecar's FUSE mount was still bound into it (rslave), then SIGKILLed the sidecar,
and unmounted last. Science's order (#2, 2 Nov 00:33Z): stop the reader, unmount (lazily if it must), then the sidecar,
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

import dataclasses
import datetime as dt
import json
import logging
import os
import re
import time
from pathlib import Path

from . import hostops as H

log = logging.getLogger(__name__)

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
                self._fh = open(path, "a")
            except OSError as e:
                self.ctx = {**ctx, "log_file_error": repr(e)[:200]}

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
                log.error("teardown: the events file could not be written: %r", e)
        return rec

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass


# ------------------------------------------------------------------------------------------------ processes
def _stat(pid: int) -> dict | None:
    """State, parent, command name and start time of a process (/proc/<pid>/stat); None when it is gone or a zombie."""
    try:
        s = (PROC / str(pid) / "stat").read_text()
    except (OSError, ValueError):
        return None
    rest = s[s.rfind(")") + 2 :].split()
    if len(rest) < 20 or rest[0] in ("Z", "X", "x"):
        return None
    return {
        "pid": pid,
        "state": rest[0],
        "ppid": int(rest[1]),
        "comm": s[s.find("(") + 1 : s.rfind(")")],
        "start": rest[19],
    }


def _wchan(pid: int, waits: Waits) -> str | None:
    """The wait channel, None when unknown. Without ptrace access the kernel answers "0" (the agent runs as another uid:
    measured on dockerd here, "0" without sudo and "futex_wait_queue" with), so "0" is read again through sudo."""
    path = PROC / str(pid) / "wchan"
    try:
        v = path.read_text().strip()
    except OSError:
        v = ""
    if v and v != "0":
        return v
    r = H.sudo_bounded("cat", str(path), timeout_s=waits.probe_s)
    v = r.stdout.strip() if r.returncode == 0 else ""
    return v if v and v != "0" else None


def _inspect(name: str, waits: Waits) -> dict | None:
    """{"pid", "id", "running"} of a container; None when docker says it does not exist; {"unknown": True, ...} when docker
    cannot say (daemon trouble, a timeout): it may be running with its copy of the mount, so it is not treated as absent."""
    r = H.run_bounded(["docker", "inspect", "-f", "{{.State.Pid}}|{{.Id}}|{{.State.Running}}", name], waits.command_s)
    parts = r.stdout.strip().split("|")
    if r.returncode != 0 or len(parts) != 3 or not parts[0].isdigit():
        if r.returncode == 1 and re.search(
            r"no such (object|container)", r.stderr.lower()
        ):  # not "no such file": a missing docker socket
            return None
        return {"pid": 0, "id": "", "running": None, "unknown": True}
    return {"pid": int(parts[0]), "id": parts[1], "running": parts[2] == "true"}


def _cgroup_procs(cid: str) -> list[int] | None:
    for cand in (CGROUP_ROOT / "system.slice" / f"docker-{cid}.scope", CGROUP_ROOT / "docker" / cid):
        try:
            return [int(x) for x in (cand / "cgroup.procs").read_text().split()]
        except (OSError, ValueError):
            continue
    return None


def _readers(agent: dict) -> list[dict] | None:
    """The agent container's processes except its init and the init's `sleep infinity` (keeping those keeps the container,
    and with it its mount namespace, alive until step 4). None when the container's cgroup cannot be read."""
    procs = _cgroup_procs(agent["id"])
    if procs is None:
        return None
    out = []
    for pid in procs:
        st = _stat(pid)
        if st is None or pid == agent["pid"] or (st["ppid"] == agent["pid"] and st["comm"] == "sleep"):
            continue
        out.append(st)
    return out


def _alive(procs: list[dict]) -> list[dict]:
    """The ones still alive (same pid AND start time: a reused pid is not a survivor)."""
    out = []
    for p in procs:
        st = _stat(p["pid"])
        if st is not None and st["start"] == p["start"]:
            out.append(st)
    return out


def _brief(procs: list[dict], waits: Waits) -> list[dict]:
    """For the log; the wait channel of the first few in D (it takes a sudo each, so it is read here, not while polling)."""
    out, asked = [], 0
    for p in procs[:32]:
        e = {k: p[k] for k in ("pid", "comm", "state")}
        if p["state"] == "D" and asked < 8:
            asked += 1
            e["wchan"] = _wchan(p["pid"], waits)
        out.append(e)
    return out


def _wait_gone(procs: list[dict], seconds: float, poll: float) -> list[dict]:
    deadline = time.monotonic() + seconds
    while True:
        left = _alive(procs)
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(poll)


def _stop_readers(agent: dict | None, waits: Waits, L: _StepLog) -> list[dict]:
    """Step 1. Returns the readers it signalled (the survivors are checked again later)."""
    if agent is None or not agent["running"] or agent["pid"] <= 0:
        L("readers", outcome="skipped", detail="no running agent container")
        return []
    procs = _readers(agent)
    if procs is None:
        L("readers", outcome="unknown", detail="the agent container's cgroup could not be read: readers not signalled")
        return []
    if not procs:
        L("readers", outcome="none", detail="no process besides init and its sleep")
        return []
    pids = [str(p["pid"]) for p in procs]
    r = H.sudo_bounded("kill", "-s", "TERM", *pids, timeout_s=waits.command_s)
    left = _wait_gone(procs, waits.reader_term_s, waits.poll_s)
    L("readers", signal="TERM", signalled=len(procs), rc=r.returncode, left=len(left), left_procs=_brief(left, waits))
    if left:
        r = H.sudo_bounded("kill", "-s", "KILL", *[str(p["pid"]) for p in left], timeout_s=waits.command_s)
        left = _wait_gone(left, waits.reader_kill_s, waits.poll_s)
        L(
            "readers",
            signal="KILL",
            rc=r.returncode,
            left=len(left),
            d_state=sum(p["state"] == "D" for p in left),
            left_procs=_brief(left, waits),
        )
    return procs


# ------------------------------------------------------------------------------------------------ the FUSE mount
def host_path(mountpoint: Path | str) -> str:
    """The path as the host's mountinfo shows it. Only the parent is resolved: resolving the mount point itself would
    stat the FUSE root, a request to the very daemon that may be stuck."""
    p = Path(mountpoint)
    return os.path.join(os.path.realpath(p.parent), p.name)


def _fuse_at(mp: str, waits: Waits) -> list[dict] | None:
    """The FUSE mounts at `mp` in the host's mountinfo; None when it cannot be read in time (unknown, never "none")."""
    mi = H.read_text_bounded(PROC / "self" / "mountinfo", waits.probe_s)
    return None if mi is None else [m for m in H.fuse_mounts(mi) if m["mountpoint"] == mp]


def _conn(dev: str) -> str | None:
    """The fusectl directory name of a FUSE mount's device ("0:N" -> "N")."""
    major, _, minor = dev.partition(":")
    return minor if major == "0" and minor.isdigit() else None


def _waiting(devs: list[str], waits: Waits) -> dict:
    out = {}
    for d in devs:
        n = _conn(d)
        r = H.sudo_bounded("cat", str(FUSECTL / n / "waiting"), timeout_s=waits.probe_s) if n else None
        out[d] = int(r.stdout.strip()) if r is not None and r.returncode == 0 and r.stdout.strip().isdigit() else None
    return out


def _abort(devs: list[str], waits: Waits) -> dict:
    """Abort each FUSE connection (every request ends with an error; the mount is dead). A connection whose fusectl entry
    is already gone counts as ended."""
    out = {}
    for d in devs:
        n = _conn(d)
        if n is None:
            out[d] = "unknown device"
            continue
        if not (FUSECTL / n).exists():
            out[d] = "gone"
            continue
        r = H.sudo_bounded("sh", "-c", f"echo 1 > {FUSECTL / n / 'abort'}", timeout_s=waits.command_s)
        out[d] = "aborted" if r.returncode == 0 else f"failed rc {r.returncode}: {r.stderr.strip()[:120]}"
    return out


def _unmount_host(mp: str, fuse0: list[dict] | None, waits: Waits, L: _StepLog) -> bool:
    """Step 2 at the host's mount point. True when no FUSE mount is left there (or a lazy unmount succeeded while the
    mount table could not be read)."""
    if fuse0 == []:
        L("unmount", outcome="none", detail="no FUSE mount at the mount point")
        return True
    deadline, attempts, r = time.monotonic() + waits.unmount_s, 0, None
    while True:
        r = H.sudo_bounded("fusermount", "-u", mp, timeout_s=waits.command_s)
        attempts += 1
        now = _fuse_at(mp, waits)
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
        stderr=r.stderr.strip()[-200:],
    )
    for method, cmd in (("fusermount -uz", ["fusermount", "-uz", mp]), ("umount -l", ["umount", "-l", mp])):
        r = H.sudo_bounded(*cmd, timeout_s=waits.command_s)
        now = _fuse_at(mp, waits)
        if now == [] or (now is None and r.returncode == 0):
            L("unmount", outcome="detached", method=method, rc=r.returncode, verified=now == [])
            return True
        L(
            "unmount",
            outcome="failed",
            method=method,
            rc=r.returncode,
            stderr=r.stderr.strip()[-200:],
            fuse_left=None if now is None else len(now),
        )
    return False


def _ns_copies(pid: int, waits: Waits) -> list[dict] | None:
    """The FUSE mounts in a container's own mount namespace; None when they cannot be read. Each is this sandbox's: the
    agent has no FUSE of its own (no capabilities, no /dev/fuse), and the sidecar mounts one thing."""
    r = H.sudo_bounded("cat", str(PROC / str(pid) / "mountinfo"), timeout_s=waits.probe_s)
    return None if r.returncode != 0 else H.fuse_mounts(r.stdout)


def _unmount_namespaces(targets: list[tuple[str, int]], waits: Waits, L: _StepLog) -> bool:
    """Step 2, in the agent's and the sidecar's own mount namespaces: the host's view looks the same whether or not the
    unmount reached them, and it does not reach the agent's (module docstring). True when no copy of the mount is known to be
    left. `-p` as well as `-m`: umount reads /proc/self/mountinfo, and in the container's /proc that resolves only from inside
    its PID namespace (with `-m` alone: "umount failed: No such file or directory", measured on the VM)."""
    clean = True
    for who, pid in targets:
        copies = _ns_copies(pid, waits)
        if copies is None:  # unknown is not clean, unless the process and so its namespace have gone
            gone = _stat(pid) is None
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
            r = H.sudo_bounded(
                "nsenter", "-t", str(pid), "-m", "-p", "umount", "-l", m["mountpoint"], timeout_s=waits.command_s
            )
        again = _ns_copies(pid, waits)
        ok = again == [] or (again is None and _stat(pid) is None)
        L(
            "unmount_ns",
            who=who,
            outcome="detached" if ok else "left",
            copies=[m["mountpoint"] for m in copies],
            verified=again == [],
            rc=r.returncode,
            stderr=r.stderr.strip()[-200:],
        )
        clean = clean and ok
    return clean


# ------------------------------------------------------------------------------------------------ the sequence
def ordered_fuse_teardown(
    agent: str | None,
    sidecar: str,
    mountpoint: Path | str,
    *,
    waits: Waits | None = None,
    log_path: Path | None = None,
    extra_readers: tuple[str, ...] | list[str] = (),
) -> TeardownResult:
    """Stop the readers, unmount, stop the sidecar, remove the agent container (module docstring). Never raises for a
    failed step: each is logged and recorded in `errors`, and the next step runs, except that no container is removed
    while the FUSE mount may still be live (`fuse_safe` False: the caller must leave the sidecar and the agent)."""
    waits = waits or WAITS
    mp = host_path(mountpoint)
    L = _StepLog(log_path, {"sidecar": sidecar})
    errors: list[str] = []
    removed: list[str] = []
    try:
        ai = _inspect(agent, waits) if agent else None
        si = _inspect(sidecar, waits)
        fuse0 = _fuse_at(mp, waits)
        targets = [(w, i["pid"]) for w, i in (("agent", ai), ("sidecar", si)) if i and i["running"] and i["pid"] > 0]
        unknown = [w for w, i in (("agent", ai), ("sidecar", si)) if i and i.get("unknown")]
        # the connections to end if it comes to that: from the namespaces too, because a retry after an earlier attempt
        # detached the host's copy sees none on the host while a copy may still live in a container
        devs = sorted(
            {m["dev"] for m in fuse0 or []} | {m["dev"] for _, pid in targets for m in _ns_copies(pid, waits) or []}
        )
        L(
            "begin",
            agent=agent,
            mountpoint=mp,
            agent_init_pid=ai and ai["pid"],
            agent_running=ai and ai["running"],
            sidecar_pid=si and si["pid"],
            sidecar_running=si and si["running"],
            fuse_mounts=None if fuse0 is None else len(fuse0),
            fuse_devs=devs,
            fuse_waiting=_waiting(devs, waits),
            containers_unknown=unknown,
        )

        # 1. the readers: first any other container that binds the mount (an M control: its reader is its init, so the container goes as a whole)
        for name in extra_readers:
            r = H.run_bounded(["docker", "stop", "-t", "2", name], 2 + waits.command_s)
            after = _inspect(name, waits)
            L("readers", who=name, action="docker stop", rc=r.returncode, running_after=after and after["running"])
            if after is not None and after["running"] is not False:
                unknown.append(name)
        readers = _stop_readers(ai, waits, L)

        # 2. the unmount: the host's mount point, then the containers' own namespaces
        unmounted = _unmount_host(mp, fuse0, waits, L)
        if unmounted and targets:
            unmounted = _unmount_namespaces(targets, waits, L)
        if unknown:  # docker could not say whether these run: a copy of the mount may be live in one, as with an unreadable namespace
            L(
                "unmount_ns",
                who=",".join(unknown),
                outcome="unknown",
                detail="docker could not say whether the container is running",
            )
            unmounted = False
        left = _alive(readers)
        stuck = [p for p in left if p["state"] == "D"]
        why = (["the mount could not be unmounted"] if not unmounted else []) + (
            [f"{len(stuck)} reader(s) still in D state after the unmount deadline"] if stuck else []
        )
        if stuck:
            L.event(
                "readers_stuck_d",
                count=len(stuck),
                pids=[p["pid"] for p in stuck],
                left_procs=_brief(stuck, waits),
                fuse_waiting=_waiting(devs, waits),
                unmounted=unmounted,
            )
        aborted = False
        if why:  # the last resort, before the sidecar's stop; sudo here is NOPASSWD: ALL, and a failure is recorded
            res = _abort(devs, waits)
            aborted = bool(res) and all(v in ("aborted", "gone") for v in res.values())
            left = _wait_gone(left, waits.after_abort_s, waits.poll_s) if aborted else _alive(readers)
            L.event(
                "abort",
                reason="; ".join(why),
                result=res,
                ok=aborted,
                readers_left=len(left),
                d_state=sum(p["state"] == "D" for p in left),
                left_procs=_brief(left, waits),
            )
        if not (unmounted or aborted):
            errors.append(
                f"FUSE mount at {mp} could be neither unmounted nor aborted: {sidecar} and {agent} left running"
            )
            L.event("held", outcome="containers left running", detail="no SIGKILL while the FUSE mount is live")
            return TeardownResult(False, removed, errors, L.records)

        # 3. the sidecar: a graceful stop, so oneclient closes /dev/fuse itself
        w = _waiting(devs, waits)
        r = H.run_bounded(
            ["docker", "stop", "-t", str(waits.sidecar_stop_s), sidecar], waits.sidecar_stop_s + waits.command_s
        )
        after = _inspect(sidecar, waits)
        left = _alive(readers)
        L(
            "sidecar",
            action="docker stop",
            rc=r.returncode,
            stderr=r.stderr.strip()[-200:] if r.returncode else "",
            fuse_waiting_before=w,
            sidecar_running_after=after and after["running"],
            readers_left=len(left),
            d_state=sum(p["state"] == "D" for p in left),
            left_procs=_brief(left, waits),
        )
        if devs and (left or (after and after["running"])):
            res = _abort(devs, waits)
            L.event(
                "abort",
                reason="readers left or the sidecar still running after its stop",
                result=res,
                ok=bool(res) and all(v in ("aborted", "gone") for v in res.values()),
            )
        try:
            H.remove_container(sidecar)
            removed.append(sidecar)
            L("sidecar", action="removed")
        except Exception as e:
            errors.append(f"remove {sidecar}: {e!r}"[:300])
            L("sidecar", action="remove failed", error=repr(e)[:200])

        # 4. the agent container, last
        if agent:
            left = _alive(readers)
            L(
                "agent",
                action="remove",
                readers_left=len(left),
                d_state=sum(p["state"] == "D" for p in left),
                left_procs=_brief(left, waits),
            )
            try:
                H.remove_container(agent)
                removed.append(agent)
                L("agent", action="removed")
            except Exception as e:
                errors.append(f"remove {agent}: {e!r}"[:300])
                L("agent", action="remove failed", error=repr(e)[:200])
        L("end", fuse_safe=True, unmounted=unmounted, aborted=aborted, errors=len(errors))
        return TeardownResult(True, removed, errors, L.records)
    finally:
        L.close()


def fuse_first(names: list[str], *, waits: Waits | None = None, log_dir: Path | None = None) -> set[str]:
    """For a cleanup that removes containers by name without knowing the order (the orphan reaper, a lane's leftovers):
    run the ordered teardown for every oneclient sidecar among `names` (with its agent container, `<sidecar minus
    -oneclient>`) BEFORE the caller removes anything. Returns the names the caller must NOT remove: the sandboxes whose
    FUSE mount could be neither unmounted nor aborted, or whose mount point could not be found."""
    waits = waits or WAITS
    held: set[str] = set()
    for side in [n for n in names if n.endswith(SIDECAR_SUFFIX)]:
        agent = side[: -len(SIDECAR_SUFFIX)]
        r = H.run_bounded(
            [
                "docker",
                "inspect",
                "-f",
                '{{range .Mounts}}{{if eq .Destination "' + SIDECAR_MOUNT + '"}}{{.Source}}{{end}}{{end}}',
                side,
            ],
            waits.command_s,
        )
        src = r.stdout.strip() if r.returncode == 0 else ""
        if not src:
            log.error("teardown: the mount point of %s could not be found: it and %s are left running", side, agent)
            held |= {side, agent}
            continue
        run_dir = Path(src).parent
        lp = (Path(log_dir) if log_dir is not None else run_dir.parent / TEARDOWN_LOG_DIR) / f"{run_dir.name}.jsonl"
        res = ordered_fuse_teardown(agent, side, Path(src), waits=waits, log_path=lp)
        if not res.fuse_safe:
            held |= {side, agent}
    return held
