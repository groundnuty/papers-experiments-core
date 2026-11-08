# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/slot_lease.py, blob d67105e5208d8b0bda15861bf1773d21ce12d628
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Exclusive leases on shared mutable resources, with quarantine (C19).

A resource a run mutates, or must not share with another run, is held under a lease for as long as the run uses it:
a platform user whose audit log must name one run, a writable tool-server token, a provider whose state the run
changes. A **key** names one such resource and a **group** is the set of keys one kind of run may take (the platform
declares them; nothing here knows what a key is). A lease is an exclusive `flock` on one file per key:

- safe across threads (each acquire opens its own file description, and flock locks conflict between them) and across
  processes (a campaign and a script beside it);
- dropped by the kernel when the holder dies, however it dies, so a killed process frees its key by itself;
- **but not re-granted at once.** The holder's sandbox may outlive it and go on using the resource. A key whose last
  holder did not release it goes to nobody until `ResourceProbe.resource_gone` has answered True for it (the dead-holder
  rule). That holds whether the holder is shown gone, or its liveness cannot be determined; only a holder the probe
  shows alive (and so not holding the lock it let go of) leaves nothing to reap. A probe that cannot show the resource
  gone, by saying so or by failing, quarantines the key: it is held back and asked again after `dead_retry_s`.
- written crash-safely: the note in the lock file is recovery state, not decoration. It is written with a flush and an
  fsync, and a note that cannot be written fails the acquisition: the key is released, nothing is started and the error
  is raised. An empty note in a lock file that existed before this acquisition, and a note that cannot be read, name a
  holder that died between taking the key and writing it, or mid-write: they are indeterminate and the key needs the
  same proof, never "a key never used". Only a lock file this very acquisition created is a key never used.

A teardown that cannot be proven **quarantines** the resource: the lease stays held, its use ends, and the key goes
to nobody until the teardown is proven (`LeasePool.settle_quarantines` asks the proof). When every key of a group is
quarantined, `acquire` raises `AllQuarantined` before waiting: the run is left unstarted and uses no attempt.

There is no fallback to another group and none to a shared principal: with no free key the caller waits, and the wait
is logged (`waits.jsonl`) with the run, because too few keys would otherwise silently lower the throughput. The wait is
recorded on every path, a refusal and a timeout included, with its outcome: a run that waited ten minutes and was then
left unstarted is ten minutes of queue time the analysis would otherwise lose. A wait for a lease is time spent on
infrastructure, never the agent's: `acquire_under` runs it inside `infrastructure_wait("lease_queue")` of whichever
timer or call context is handed in. The loop's clock starts only when the loop does, so a lease taken before
`record_attempt` is outside the agent's time and outside the record's clock; a provider that takes its lease inside a call, through the call context, has the wait recorded per kind.
"""

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

WAIT_LOG = "waits.jsonl"
QUARANTINE_LOG = "quarantines.jsonl"  # one line per quarantine ("q") and per end of one ("free")
DEAD_LOG = "dead_holders.jsonl"  # one line per look at a holder that died without releasing its key
POLL_S = 0.25
DEAD_RETRY_S = 30.0  # a key whose dead holder's resource was not shown gone is tried again this often
QUARANTINE_RETRY_S = 60.0  # a quarantine's proof is asked again this often
WAIT_LOG_MIN_S = 0.5  # an acquisition slower than this is logged even if no contention was seen
_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class LeaseRefused(RuntimeError):
    """No lease was granted and nothing was started: the run uses no attempt."""

    wait_s: float = 0.0  # the seconds the caller waited before the refusal; set by `LeasePool.acquire`


class NoResources(LeaseRefused):
    """The platform declares no key for this group."""


class LeaseTimeout(LeaseRefused):
    """No key came free within the time the caller allowed."""


class AllQuarantined(LeaseRefused):
    """Every key of the group is quarantined or held back for a dead holder: a wait would end only when a teardown is
    proven, so the run is refused before anything starts."""


_OUTCOMES = {NoResources: "no_resources", AllQuarantined: "all_quarantined", LeaseTimeout: "timeout"}


class ResourceProbe(Protocol):
    """What the platform can say about a holder and the resource it used. Both questions must fail safe."""

    def holder_gone(self, owner: str) -> bool | None:
        """Tell whether the process named by `owner` (as `LeasePool` was told to write it) is gone for good.

        True: shown gone. False: shown alive. None, or an exception: cannot be told, which is treated like gone, since
        only a holder shown alive leaves nothing to reap.
        """
        ...

    def resource_gone(self, key: str, note: Mapping[str, Any]) -> bool:
        """Tell whether everything the holder started with this key is shown gone: its sandbox, its processes, its
        mounts. `note` is what the holder wrote in the lock file (it may be empty when the file was unreadable).

        Only True shows it. Anything else, an exception included, keeps the key quarantined.
        """
        ...


class WaitSource(Protocol):
    """An `AgentTimer` or a call context: anything that can mark a wait on infrastructure."""

    def infrastructure_wait(self, kind: str) -> AbstractAsyncContextManager[None]:
        """Return the context manager that marks a wait of the given kind."""
        ...


def process_owner() -> str:
    """Name this process so that a reused pid is not mistaken for it: boot id, pid and start time (Linux `/proc`)."""
    pid = os.getpid()
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        started = _start_time(pid)
    except OSError:
        return f"pid:{pid}"
    return f"{boot}:{pid}:{started}"


def process_gone(owner: str) -> bool | None:
    """Tell whether the process `process_owner` named is gone: another boot, no such pid, or the pid reused.

    True: it is gone, which is not a proof that what it started is (the teardown still needs one). False: it is alive.
    None: this function cannot read the owner or the process table, so it cannot say.
    """
    parts = owner.split(":")
    if len(parts) == 2 and parts[0] == "pid":
        try:
            os.kill(int(parts[1]), 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False  # there is such a process, which is not ours to signal
        except (ValueError, OSError):
            return None
        return False
    if len(parts) != 3:
        return None
    boot, pid, started = parts
    try:
        if boot != Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip():
            return True
    except OSError:
        return None  # no process table to look in at all
    try:
        return _start_time(int(pid)) != started
    except FileNotFoundError:
        return True  # this boot has no /proc/<pid>: the process is gone
    except (ValueError, IndexError, OSError):
        return None


def _start_time(pid: int) -> str:
    """Return the start time field of a process; raise FileNotFoundError (an OSError) when there is no such process."""
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    return stat.rsplit(")", 1)[1].split()[19]  # the 22nd field overall, after the command in parentheses


def _append_line(path: Path, entry: dict[str, Any]) -> None:
    """Append one JSON line with a single write(2) on O_APPEND, so lines of several processes never interleave."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        log.exception("could not append to %s", path)


class Lease:
    """One held key. `start` and `end` are epoch seconds bracketing the use of the resource: `start` is taken once the
    key is held (after any wait), `end` when the use ends (release, or the quarantine that keeps the key)."""

    def __init__(self, handle: Any, key: str, group: str, run_key: str | None, wait_s: float, start: float):
        self._handle = handle
        self._mutex = threading.Lock()
        self.key, self.group, self.run_key = key, group, run_key
        self.wait_s, self.start, self.end = wait_s, start, None
        self.quarantined = False

    @property
    def held(self) -> bool:
        """Tell whether the key is still held (a quarantined lease is)."""
        return self._handle is not None

    def release(self) -> None:
        """End the use and free the key; a quarantined lease frees only through `LeasePool.settle_quarantines`."""
        with self._mutex:
            if self._handle is None or self.quarantined:
                return
            self.end = time.time()
            self._free_locked()

    def _quarantine(self) -> bool:
        """End the use but keep the key held; False if it was released already or already quarantined."""
        with self._mutex:
            if self._handle is None or self.quarantined:
                return False
            self.quarantined = True
            if self.end is None:
                self.end = time.time()
            return True

    def _free(self) -> None:
        with self._mutex:
            self._free_locked()

    def _free_locked(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:  # released in order: the next holder has nothing to reap
            _write_note(handle, {"released": time.time(), "key": self.key})
        except OSError:
            log.exception(
                "could not note the release of %s: the next holder will need a proof the resource is gone", self.key
            )
        with contextlib.suppress(OSError):
            handle.close()

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def __del__(self) -> None:  # last resort; process exit and a kill free the key too
        with contextlib.suppress(Exception):
            self.release()

    def record(self) -> dict[str, Any]:
        """Return the lease as a record keeps it: key, group, the seconds waited for it, start and end (None while held)."""
        return {"key": self.key, "group": self.group, "wait_s": self.wait_s, "start": self.start, "end": self.end}


def _write_note(handle: Any, note: Mapping[str, Any]) -> None:
    """Replace the note in a lock file, flushed and fsynced; raise OSError if it cannot be written.

    The note is recovery state: it is how a later holder tells a key released in order from one a dead holder left. A
    note that did not reach the disk is a note that was not written, so every error is the caller's to handle.
    """
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(note, sort_keys=True))
    handle.flush()
    os.fsync(handle.fileno())


class LeasePool:
    """The leases of one campaign directory.

    `resolve(group)` returns the keys the group may take; it is called again on every attempt, so a key added while a
    run waits is used at once. `owner` is how this process is written into a note (`process_owner()` by default) and
    `probe` answers for holders and resources. `clock` and `sleep` are injectable so a test never really waits.
    """

    def __init__(
        self,
        directory: Path | str,
        resolve: Callable[[str], Sequence[str]],
        probe: ResourceProbe,
        *,
        owner: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_s: float = POLL_S,
        dead_retry_s: float = DEAD_RETRY_S,
        quarantine_retry_s: float = QUARANTINE_RETRY_S,
    ):
        self.dir = Path(directory)
        self._resolve, self._probe = resolve, probe
        self._owner = owner if owner is not None else process_owner()
        self._clock, self._sleep, self._poll_s = clock, sleep, poll_s
        self._dead_retry_s, self._quarantine_retry_s = dead_retry_s, quarantine_retry_s
        self._mutex = threading.Lock()
        self._dead_retry: dict[str, float] = {}  # key -> the clock time before which it is not looked at again
        self._quarantined: dict[str, tuple[Lease, Callable[[], bool], float]] = {}  # key -> lease, proof, next look

    # --- keys -----------------------------------------------------------------------------------------------------

    def keys_of(self, group: str) -> list[str]:
        """Return the group's keys in order; refuse a key that is not a plain file name."""
        keys = list(self._resolve(group))
        for key in keys:
            if not _KEY_RE.fullmatch(key):
                raise ValueError(f"lease key {key!r} must be letters, digits, dot, dash and underscore")
        return keys

    def unusable(self) -> set[str]:
        """Return the keys no run can get until a teardown is proven: quarantined here, or held back for a dead holder
        whose resource could not be shown gone."""
        now = self._clock()
        with self._mutex:
            return set(self._quarantined) | {k for k, t in self._dead_retry.items() if t > now}

    def held_back(self) -> int:
        """Return how many keys are held by quarantine: they count against a concurrency cap like a running attempt."""
        with self._mutex:
            return len(self._quarantined)

    # --- granting -------------------------------------------------------------------------------------------------

    def _lock_path(self, key: str) -> Path:
        return self.dir / f"{key}.lock"

    def _flock(self, key: str) -> Any:
        """Return the key's lock file with its flock taken, or None when a live holder has it."""
        self.dir.mkdir(parents=True, exist_ok=True)
        # a+: never truncates a holder's note. The handle outlives this call when the lock is taken, so no `with`.
        handle = open(self._lock_path(key), "a+", encoding="utf-8")  # noqa: SIM115
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        return handle

    def _holder_alive(self, owner: str) -> bool:
        """Tell whether the probe shows the holder alive. Gone, unknown and a failing probe are all "not shown alive"."""
        try:
            return self._probe.holder_gone(owner) is False
        except Exception:
            log.exception("looking for the holder %s", owner)
            return False

    def _dead_holder_cleared(self, key: str, handle: Any, *, fresh: bool = False) -> bool:
        """Tell whether the key may go to a new holder, given that its flock is ours.

        The kernel drops a dead process's lock at once, but its resource may go on being used. So a key whose note does
        not say it was released goes to nobody until `resource_gone` answers True: when the holder is shown gone, when
        its liveness cannot be determined, and when the note is empty (in a file that was there before) or unreadable. A
        holder the probe shows alive does not hold the lock it let go of; nothing is left to reap. `fresh` says that this
        acquisition created the lock file, so an empty note is a key never used.
        """
        handle.seek(0)
        text = handle.read()
        note: dict[str, Any] = {}
        if text.strip():
            try:
                parsed = json.loads(text)
                note = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                note = {}  # unreadable: a holder that died mid-write is a dead holder that must be shown gone
        elif fresh:
            return True  # a key never used
        if "released" in note:
            return True
        owner = note.get("owner")
        if owner and self._holder_alive(str(owner)):
            return True
        now = self._clock()
        with self._mutex:
            if now < self._dead_retry.get(key, 0.0):
                return False
        try:
            gone = self._probe.resource_gone(key, note) is True
        except Exception:
            log.exception("looking for the resource of the dead holder of %s", key)
            gone = False
        with self._mutex:
            if gone:
                self._dead_retry.pop(key, None)
            else:
                self._dead_retry[key] = now + self._dead_retry_s
        if gone:
            try:
                _write_note(handle, {"released": time.time(), "key": key, "reaped_owner": owner})
            except OSError:
                log.exception("could not note that the resource of %s was reaped; the proof is asked again", key)
        _append_line(
            self.dir / DEAD_LOG,
            {"t": time.time(), "key": key, "run_key": note.get("run_key"), "owner": owner, "gone": gone},
        )
        return gone

    def _try_key(self, key: str) -> Any:
        fresh = not self._lock_path(key).exists()
        handle = self._flock(key)
        if handle is None:
            return None
        if not self._dead_holder_cleared(key, handle, fresh=fresh):
            handle.close()
            return None
        return handle

    def _grant(self, handle: Any, key: str, group: str, run_key: str | None, wait_s: float) -> Lease:
        start = time.time()
        note = {"owner": self._owner, "pid": os.getpid(), "run_key": run_key, "since": start, "key": key}
        try:
            _write_note(handle, note)
        except BaseException:
            # Without its note a holder that dies leaves a key nobody can tell from a free one. Nothing was started:
            # drop the lock (closing the file does), leave the empty or partial note to read as indeterminate, raise.
            with contextlib.suppress(OSError):
                handle.close()
            raise
        return Lease(handle, key, group, run_key, wait_s, start)

    def try_acquire(self, group: str, run_key: str | None = None) -> Lease | None:
        """Take a free key of the group now, or return None when every one is busy.

        Raises NoResources when the group has no key and AllQuarantined when none can come free without a proof.
        """
        keys = self._keys_or_refuse(group)
        for key in keys:
            with self._mutex:
                if key in self._quarantined:
                    continue
            handle = self._try_key(key)
            if handle is not None:
                return self._grant(handle, key, group, run_key, 0.0)
        if set(keys) <= self.unusable():
            raise AllQuarantined(f"no usable key in group {group!r}: all {len(keys)} are quarantined or held back")
        return None

    def _keys_or_refuse(self, group: str) -> list[str]:
        keys = self.keys_of(group)
        if not keys:
            raise NoResources(f"the platform declares no key for group {group!r}")
        return keys

    def acquire(self, group: str, *, run_key: str | None = None, timeout_s: float | None = None) -> Lease:
        """Take a free key of the group, waiting while there is none, and log the wait.

        Raises NoResources, AllQuarantined (as soon as no key can come free without a teardown being proven, so a
        run is not left waiting for ever) and LeaseTimeout. The seconds waited are measured on every path, so no queue
        time is lost to the throughput analysis: they are `Lease.wait_s` for a lease, `LeaseRefused.wait_s` for a
        refusal, and a line of `waits.jsonl` either way (a granted lease only when it waited or was contended).
        """
        began = self._clock()
        contended = False
        try:
            while True:
                lease = self.try_acquire(group, run_key)
                if lease is not None:
                    lease.wait_s = self._clock() - began
                    if contended or lease.wait_s >= WAIT_LOG_MIN_S:
                        self._log_wait(group, run_key, lease.wait_s, "granted", lease.key)
                    return lease
                contended = True
                if timeout_s is not None and self._clock() - began >= timeout_s:
                    raise LeaseTimeout(f"no free key in group {group!r} after {timeout_s:g} s")
                self._sleep(self._poll_s)
        except LeaseRefused as refusal:
            refusal.wait_s = self._clock() - began
            self._log_wait(group, run_key, refusal.wait_s, _OUTCOMES.get(type(refusal), "refused"), None)
            raise

    def _log_wait(self, group: str, run_key: str | None, wait_s: float, outcome: str, key: str | None) -> None:
        _append_line(
            self.dir / WAIT_LOG,
            {
                "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "group": group,
                "run_key": run_key,
                "wait_s": round(wait_s, 3),
                "outcome": outcome,
                "key": key,
            },
        )

    async def acquire_under(
        self, waits: WaitSource, group: str, *, run_key: str | None = None, timeout_s: float | None = None
    ) -> Lease:
        """Take a key inside `infrastructure_wait("lease_queue")` of a timer or a call context, off the event loop.

        The wait is marked whether or not it is long, so it is never part of the agent's time; the lease carries the
        seconds it waited.
        """
        async with waits.infrastructure_wait("lease_queue"):
            return await asyncio.to_thread(lambda: self.acquire(group, run_key=run_key, timeout_s=timeout_s))

    def finish(self, lease: Lease) -> bool:
        """End a lease after its run: free the key if the probe shows what the run started gone, else quarantine it.

        An unproven teardown keeps the key held until the probe can show it (C19). Return True when the key was freed.
        """
        note = {"key": lease.key, "run_key": lease.run_key, "group": lease.group}

        def gone() -> bool:
            return bool(self._probe.resource_gone(lease.key, note))

        try:
            shown = gone()
        except Exception:
            log.exception("looking for the resource behind %s", lease.key)
            shown = False
        if shown:
            lease.release()
            return True
        self.quarantine(lease, gone)
        return False

    # --- quarantine -----------------------------------------------------------------------------------------------

    def quarantine(self, lease: Lease, proven: Callable[[], bool]) -> None:
        """End the lease's use but keep its key held until `proven()` first says the teardown is done.

        Use it when a sandbox could not be torn down: it may still use the resource, so no other run may get the key.
        `proven` is asked again by `settle_quarantines`; a proof that raises is not a proof.
        """
        if not lease._quarantine():
            return
        log.error("lease %s quarantined: its teardown could not be proven", lease.key)
        with self._mutex:
            self._quarantined[lease.key] = (lease, proven, self._clock() + self._quarantine_retry_s)
        _append_line(self.dir / QUARANTINE_LOG, {"ev": "q", "key": lease.key, "start": lease.end, "pid": os.getpid()})

    def settle_quarantines(self) -> list[str]:
        """Ask the proof of each quarantine that is due; free the keys whose teardown is proven and return them."""
        now = self._clock()
        with self._mutex:
            due = [(k, entry) for k, entry in self._quarantined.items() if entry[2] <= now]
        freed = []
        for key, (lease, proven, _) in due:
            try:
                done = bool(proven())
            except Exception:
                log.exception("proving the teardown behind %s", key)
                done = False
            with self._mutex:
                if done:
                    self._quarantined.pop(key, None)
                else:
                    self._quarantined[key] = (lease, proven, now + self._quarantine_retry_s)
            if done:
                lease._free()
                freed.append(key)
                _append_line(self.dir / QUARANTINE_LOG, {"ev": "free", "key": key, "end": time.time()})
        return freed

    def sweep_dead_holders(self, groups: Iterable[str]) -> list[str]:
        """Look at every key of the groups for a dead holder's leftovers, at a campaign's start and again while any is
        stuck. Return the keys still held back: free to lock, but the holder's resource is not shown gone. A key
        whose lock is busy has a live holder and is not one of them."""
        out = []
        for key in dict.fromkeys(k for g in groups for k in self.keys_of(g)):
            if not self._lock_path(key).exists():
                continue
            handle = self._flock(key)
            if handle is None:
                continue
            try:
                if not self._dead_holder_cleared(key, handle):
                    out.append(key)
            finally:
                handle.close()
        return out
