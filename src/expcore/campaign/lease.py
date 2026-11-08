# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/slot_lease.py, blob d67105e5208d8b0bda15861bf1773d21ce12d628
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Lease pool of the per-slot Onedata users (#8, #11): a run takes a free slot user OF ITS OWN OPTION.

The audit log names the Onezone user of every line, so two runs that overlap must hold different users, and the
user must tell the option (config/slot_map.json: {slot key: {user, option}}). A lease is an exclusive `flock` on one
file per slot under LEASE_DIR:

- safe across threads (each acquire opens its own file description, and flock locks conflict between them) and across
  processes (the campaign and a scripted queue side by side);
- the kernel drops the lock when the holder dies, so a killed process frees its slot by itself;
- released on every exit path of this module's users (context manager, `release()`, garbage collection, process exit).

There is no free-slot fallback to another option and none to the admin user: with no free slot the caller WAITS, and the
wait is recorded (`wait_s`; an append to `waits.jsonl`; a marker file while it lasts, which the status view alarms on),
because too few users would otherwise silently lower the throughput. A wait is never part of a run's measured time.

The word "slot" in the scheduler means a model endpoint (dataset_76.harness.slots); here it is an Onedata slot user.
"""
from __future__ import annotations

import dataclasses
import fcntl
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from dataset_76.faults.matcher import RunInterval, load_slot_map

from .slot_users import slot_name

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
SLOT_MAP = REPO / "config" / "slot_map.json"
LEASE_DIR = Path("/mnt/volume2/dataset-80/slot-leases")
WAIT_LOG = "waits.jsonl"
QUARANTINE_LOG = "quarantines.jsonl"      # one line per quarantine ("q") and per end of one ("free"); durable across restarts
DEAD_LOG = "dead_holders.jsonl"   # one line per reap of a holder that died without releasing its key
DEAD_RETRY_S = 30.0               # a key whose dead holder's sandbox was not shown gone is tried again this often
POLL_S = 0.25
WAIT_LOG_MIN_S = 0.5         # an acquisition slower than this is logged even if no marker was needed


class NoSlotForOption(RuntimeError):
    """The slot map has no user for this option (e.g. M+, which is scripted-only and keeps its own path)."""


class SlotWaitTimeout(RuntimeError):
    pass


class SlotQuarantined(RuntimeError):
    """Every slot user of this option is in quarantine: a wait would end only when a teardown is proven, so the run is refused before any sandbox starts.
    It uses no attempt, and repeated refusals stop dispatch (science, #2, 2 Nov 08:15Z; `Campaign._is_unstarted`)."""


NO_SLOT_USER = (NoSlotForOption, SlotWaitTimeout, SlotQuarantined)     # the refusals: no sandbox started, for want of a slot user


def _option_value(option: Any) -> str:
    return str(getattr(option, "value", option))


def _username(key: str, option: str) -> str:
    n = key.rsplit("-", 1)[-1]
    if key != f"{option}-{n}" or not n.isdigit():
        raise ValueError(f"slot key {key!r} is not <option>-<n> for option {option!r}")
    return slot_name(option, int(n))


_DEAD_RETRY: dict = {}           # (lease dir, key) -> monotonic time of the next reap


def _owner() -> str | None:
    """This process, as the sandbox's containers name it (netpool.owner_id: boot, namespace, pid and start time), so a reused pid is no holder."""
    try:
        from dataset_76.sandbox import netpool
        return netpool.owner_id()
    except Exception:  # noqa: BLE001 - no note of an owner: as a lease always was
        return None


_QUARANTINED: set = set()        # leases in quarantine, held by reference until their teardown is proven
QUARANTINE_RETRY_S = 60.0


class Lease:
    """One held slot user. `start`/`end` are epoch seconds bracketing the use of the slot's tokens: `start` is taken
    once the slot is held (after any wait), `end` when it is released."""

    def __init__(self, fh, key: str, option: str, username: str, user_id: str, wait_s: float, start: float, lease_dir: Path | None = None):
        self._fh, self._dir = fh, lease_dir
        import threading
        self._mu, self._q_start, self._prover = threading.Lock(), None, None
        self.slot, self.option, self.username, self.user_id = key, option, username, user_id
        self.wait_s, self.start, self.end = wait_s, start, None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def release(self) -> None:
        with self._mu:
            if self._fh is None or self._q_start is not None:      # a quarantine ends only when the teardown is proven (or with the holder's death)
                return
            self.end = time.time()
            self._free_locked()                  # also logs the end of a quarantine, whatever path ends it

    def quarantine(self, until_epoch: float | None = None, proven: Callable[[], bool] | None = None) -> None:
        """Stop counting the use (`end` is set) but KEEP the slot held: a sandbox that could not be torn down may still
        use the token, and another run must not get the same user meanwhile. `until_epoch` is the token's expiry, on record and ending nothing: a mounted oneclient may read past it
        (the lead's ruling, 2 Nov). Freed when `proven()` first says the teardown is done, asked by a daemon thread every QUARANTINE_RETRY_S; with none, or never, when the process dies."""
        with self._mu:
            if self._fh is None:                                   # released already
                return
            if self._q_start is not None:                          # already quarantined: once only, but a later caller may know how to prove the end
                self._prove_later(proven)
                return
            if self.end is None:
                self.end = time.time()
            log.error("slot %s quarantined: its sandbox could not be torn down", self.slot)
            self._q_start = time.time()
            _QUARANTINED.add(self)                  # keeps the lock's file open even when the run's objects are garbage collected
            _quarantine_event(self._dir, {"ev": "q", "slot": self.slot, "option": self.option, "start": self._q_start,
                                          "until": until_epoch, "pid": os.getpid()})
            self._prove_later(proven)

    def _prove_later(self, proven: Callable[[], bool] | None) -> None:
        if proven is None or self._prover is not None:
            return
        import threading

        def wait():
            while self._fh is not None:
                try:
                    if proven():
                        self._free()
                        return
                except BaseException as e:  # noqa: BLE001 -- not proven; the slot stays held
                    log.error("slot %s: checking its teardown failed: %r", self.slot, e)
                time.sleep(QUARANTINE_RETRY_S)
        self._prover = threading.Thread(target=wait, daemon=True, name=f"quarantine-{self.slot}")
        self._prover.start()

    def _free(self) -> None:
        with self._mu:
            self._free_locked()

    def _free_locked(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:                                 # released in order: the next holder has nothing to reap (`SlotPool._dead_holder_cleared`)
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps({"released": time.time(), "slot": self.slot}))
                fh.flush()
            except OSError:
                pass
            if self._q_start is not None:
                _QUARANTINED.discard(self)
                _quarantine_event(self._dir, {"ev": "free", "slot": self.slot, "start": self._q_start, "end": time.time()})
                self._q_start = None
            try:
                fh.close()
            except OSError:
                pass

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def __del__(self):                        # last resort; the process exit and a kill free it too
        try:
            self.release()
        except Exception:  # noqa: BLE001
            pass

    def record(self) -> dict[str, Any]:
        """`onedata_slot` of the run record: {slot, user_id, start, end}; `end` is None while the lease is held."""
        return {"slot": self.slot, "user_id": self.user_id, "start": self.start, "end": self.end}

    def interval(self, run_key: str) -> RunInterval:
        if self.end is None:
            raise RuntimeError("the lease is still held: it has no end yet")
        return RunInterval(run_key, self.option, self.slot, self.start, self.end)


def _quarantine_event(d: Path | None, e: dict) -> None:
    """Append one line (a single write(2) on O_APPEND); a pool with no directory (a bare Lease in a test) logs nothing."""
    if d is None:
        return
    try:
        Path(d).mkdir(parents=True, exist_ok=True)
        fd = os.open(Path(d) / QUARANTINE_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (json.dumps(e) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        log.exception("could not append to %s", QUARANTINE_LOG)


def allowed_keys() -> set[str] | None:
    """Two run hosts (dataset_76/campaign/hosts.py): this host's share of the slot users, from STUDY_VAR_95 (comma-separated keys), set by
    the campaign on its host and by the remote runner on the other. A lease is an flock, which does not cross hosts, so each host may
    lease only its own share; the campaign refuses a key on two hosts. Unset: every user, as on one host."""
    v = os.environ.get("STUDY_VAR_95")
    if v is None:
        return None
    return {k for k in v.split(",") if k}


def reserved_keys() -> set[str]:
    """The campaign's pool only: slot keys held back for a process beside it (fault calibration, the validation set) while that process is up, so no agent run
    leases a user the process's attribution windows read (dataset_76.harness.reservation). Looked at on each use, so a process that starts is honoured at the next
    look and a key is back as soon as its process is gone. Empty when the campaign set no reservation (and in the process itself)."""
    from .reservation import excluded_keys
    return excluded_keys()


class SlotPool:
    def __init__(self, slot_map_path: Path = SLOT_MAP, lease_dir: Path = LEASE_DIR, *, poll_s: float = POLL_S,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 reap_dead: Callable[[dict], bool] | None = None, reap_log_dir: Callable[[str], Path | None] | None = None):
        self.slot_map_path, self.dir = Path(slot_map_path), Path(lease_dir)
        self.poll_s, self._sleep, self._clock = poll_s, sleep, clock
        self._reap_dead = reap_dead or self._reap_dead_default
        self._reap_log_dir = reap_log_dir            # where a dead holder's record goes: its attempt's run dir, when the caller knows it; else this pool's dir

    # the map is re-read on every attempt: users added while a run waits are used at once
    def slots_of(self, option: str) -> list[tuple[str, str]]:
        """[(slot key, user id)] of this option, in key order."""
        for attempt in range(5):                 # the map may be rewritten (non-atomically) while a run waits
            try:
                m = load_slot_map(self.slot_map_path)
                break
            except ValueError:               # includes a half-written JSON file; a really broken map still raises
                if attempt == 4:
                    raise
                self._sleep(0.05)
        only = allowed_keys()
        held = reserved_keys()
        return sorted((k, s.user) for k, s in m.items() if s.option == option and (only is None or k in only) and k not in held)

    def _all_quarantined(self, slots: list[tuple[str, str]]) -> bool:
        try:
            return {k for k, _ in slots} <= quarantined_slots(self.dir)
        except (OSError, KeyError):                      # not known to be quarantined: the run waits, as any other, and the status report says it cannot read the pool
            log.exception("could not read %s", QUARANTINE_LOG)
            return False

    def _lock_path(self, key: str) -> Path:
        return self.dir / f"{key}.lock"

    def _flock(self, key: str):
        """The key's lock file with its flock taken, or None when a live holder has it."""
        self.dir.mkdir(parents=True, exist_ok=True)
        fh = open(self._lock_path(key), "a+")           # a+: never truncates a holder's note; O_CLOEXEC by default
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None
        return fh

    def _try(self, key: str):
        fh = self._flock(key)
        if fh is None:
            return None
        if not self._dead_holder_cleared(key, fh):
            fh.close()
            return None
        return fh

    def _dead_holder_cleared(self, key: str, fh) -> bool:
        """The kernel drops a dead process's lock at once, but its sandbox may go on reading fr under this user's token. So a key whose last holder died without
        releasing it goes to nobody until that sandbox is shown gone (the lead, 2 Nov, from lane-dress's rehearsal); this holds the key's lock meanwhile. A
        reap that fails is tried again after DEAD_RETRY_S, and the key counts as busy until then."""
        try:
            fh.seek(0)
            note = json.loads(fh.read() or "{}")
        except (OSError, ValueError):
            return True                          # the note is unreadable: nothing names a holder
        owner = note.get("owner") if isinstance(note, dict) else None
        if not owner or "released" in note:
            return True
        from dataset_76.sandbox import netpool
        if not netpool.owner_gone(owner):
            return True
        retry = _DEAD_RETRY.get((str(self.dir), key), 0.0)
        if self._clock() < retry:
            return False
        try:
            gone = bool(self._reap_dead(note))
        except Exception:  # noqa: BLE001 - not shown gone
            log.exception("reaping the dead holder of %s", key)
            gone = False
        if gone:
            _DEAD_RETRY.pop((str(self.dir), key), None)
            try:                                 # cleared: nothing left for the next look to reap
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps({"released": time.time(), "slot": key, "reaped_owner": owner}))
                fh.flush()
            except OSError:
                pass
        else:
            _DEAD_RETRY[(str(self.dir), key)] = self._clock() + DEAD_RETRY_S
        try:
            with open(self.dir / DEAD_LOG, "a") as f:
                f.write(json.dumps({"t": time.time(), "slot": key, "run_key": note.get("run_key"), "owner": owner, "reaped": gone}) + "\n")
        except OSError:
            pass
        return gone

    def _reap_dead_default(self, note: dict) -> bool:
        run_key = note.get("run_key")
        if not run_key:                          # a lease taken with no run named (the oracle's) has no sandbox directory this can find; its containers still go
            from dataset_76.sandbox import netpool
            got = netpool.reap_orphans()
            return not got.get("held") and "skipped" not in got       # one it left running (a mount not released), or did not look at, is not shown gone
        from dataset_76.sandbox import reap
        return reap.reap_run(run_key, log_dir=(self._reap_log_dir(run_key) if self._reap_log_dir else None) or self.dir)

    def sweep_dead_holders(self) -> list[str]:
        """Reap what a dead holder's lease left (at a campaign's start, then each minute while one is stuck), rather than wait for each key's next holder.
        Returns the keys still held back: a key whose lock is free but whose dead holder's sandbox is not shown gone. A key whose lock is busy has a LIVE
        holder (a run of this process, in the minute loop) and is not one of them: counting it would count every run in flight twice."""
        only = allowed_keys()
        out = []
        for k in sorted(load_slot_map(self.slot_map_path)):
            if only is not None and k not in only:
                continue
            if (self.dir / f"{k}.lock").exists():
                fh = self._flock(k)
                if fh is None:
                    continue
                try:
                    if not self._dead_holder_cleared(k, fh):
                        out.append(k)
                finally:
                    fh.close()
        return out

    def acquire(self, option: Any, *, run_key: str | None = None, timeout_s: float | None = None, refuse_quarantined: bool = False) -> Lease:
        """A free slot user of exactly this option; waits (and records the wait) while there is none. With `refuse_quarantined`, raises SlotQuarantined
        once none can come free without a teardown being proven; ordinary contention still waits."""
        opt = _option_value(option)
        t0 = self._clock()
        marker = None
        try:
            while True:
                slots = self.slots_of(opt)
                if not slots:
                    raise NoSlotForOption(f"no slot user for option {opt!r} in {self.slot_map_path}")
                for key, uid in slots:
                    fh = self._try(key)
                    if fh is not None:
                        wait = self._clock() - t0
                        start = time.time()
                        try:
                            fh.seek(0)
                            fh.truncate()
                            fh.write(json.dumps({"pid": os.getpid(), "run_key": run_key, "since": start, "slot": key, "owner": _owner()}))
                            fh.flush()
                        except OSError:
                            pass                      # the note is for people; the lock is the truth
                        lease = Lease(fh, key, opt, _username(key, opt), uid, wait, start, self.dir)
                        if marker is not None or wait >= WAIT_LOG_MIN_S:      # any contention is logged, however short
                            self._log_wait(opt, run_key, wait)
                        return lease
                if refuse_quarantined and self._all_quarantined(slots):
                    raise SlotQuarantined(f"no usable {opt} slot user: all {len(slots)} are in quarantine until a teardown is proven")
                if marker is None:
                    marker = self._waiting_marker(opt, run_key)
                    log.warning("no free %s slot user: waiting (run %s)", opt, run_key)
                if timeout_s is not None and self._clock() - t0 >= timeout_s:
                    raise SlotWaitTimeout(f"no free {opt} slot user after {timeout_s:g} s")
                self._sleep(self.poll_s)
        finally:
            if marker is not None:
                marker[0].close()
                marker[1].unlink(missing_ok=True)

    # ---- the visible side of waiting
    def _waiting_marker(self, option: str, run_key: str | None):
        d = self.dir / "waiting"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{option}.{os.getpid()}.{uuid.uuid4().hex[:8]}.json"
        fh = open(p, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)           # held while waiting: a dead waiter's file reads as stale
        fh.write(json.dumps({"option": option, "run_key": run_key, "since": time.time(), "pid": os.getpid()}))
        fh.flush()
        return fh, p

    def _log_wait(self, option: str, run_key: str | None, wait_s: float) -> None:
        try:
            line = json.dumps({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "option": option,
                               "run_key": run_key, "wait_s": round(wait_s, 3)}) + "\n"
            fd = os.open(self.dir / WAIT_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode())             # one write(2) on O_APPEND: lines of several processes never interleave
            finally:
                os.close(fd)
        except OSError:
            log.exception("could not append to %s", WAIT_LOG)

    def status(self) -> dict[str, dict[str, Any]]:
        """Per option: slots, busy (probed by a lock attempt), live waiters, and the logged waits."""
        return pool_status(self.dir, self.slot_map_path)


def pool_status(lease_dir: Path = LEASE_DIR, slot_map_path: Path = SLOT_MAP, since: float | None = None) -> dict[str, dict[str, Any]]:
    d = Path(lease_dir)
    out: dict[str, dict[str, Any]] = {}
    for k, s in sorted(load_slot_map(slot_map_path).items()):
        o = out.setdefault(s.option, {"slots": 0, "busy": 0, "waiting": 0, "waits": 0, "wait_s_total": 0.0, "wait_s_max": 0.0,
                                                     "quarantined": 0, "quarantines": 0, "quarantine_slot_hours": 0.0})
        o["slots"] += 1
        p = d / f"{k}.lock"
        if p.exists():
            with open(p, "a+") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    o["busy"] += 1
    w = d / "waiting"
    if w.is_dir():
        for p in w.iterdir():
            try:
                with open(p, "a+") as fh:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                        continue                         # not held: a dead waiter's leftover
                    except OSError:
                        pass
                opt = p.name.split(".", 1)[0]
                if opt in out:
                    out[opt]["waiting"] += 1
            except OSError:
                continue
    _fold_quarantines(d, out, since=since)
    for o in out.values():
        o["free"] = o["slots"] - o["busy"]                 # a quarantined slot is busy (its lock is held), so it is not free
        o["usable"] = o["slots"] - o["quarantined"]        # slots a run can ever get while the quarantines last
    wl = d / WAIT_LOG
    if wl.exists():
        for line in wl.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("option") in out:
                o = out[e["option"]]
                o["waits"] += 1
                o["wait_s_total"] += float(e["wait_s"])
                o["wait_s_max"] = max(o["wait_s_max"], float(e["wait_s"]))
    return out


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError):
        return True
    return True


def _fold_quarantines(d: Path, out: dict, now: float | None = None, since: float | None = None) -> None:
    """From quarantines.jsonl: per option the cumulative count and slot-hours lost, and the quarantines in force now.
    A quarantine ends at its "free" line; with none it ended when its holder died (it then ends at its last sign of life,
    unknown: counted to the token expiry or to now, whichever is earlier -- an upper bound, said so in the report) or is
    still in force (holder alive, token not yet expired). In force = the holder is alive and the slot's lock is held."""
    now = time.time() if now is None else now
    ev = _quarantine_log(d)
    freed = {(e["slot"], e["start"]): e["end"] for e in ev if e.get("ev") == "free"}
    for e in ev:
        if e.get("ev") != "q" or e.get("option") not in out:
            continue
        o = out[e["option"]]
        key = (e["slot"], e["start"])
        counted = since is None or float(e["start"]) >= since      # cumulative totals: this campaign's quarantines only
        until = e.get("until")
        if key in freed:
            end = freed[key]
        else:
            end = now if until is None else min(float(until), now)
            if _in_force(d, e):
                o["quarantined"] += 1                              # in force now, whichever campaign made it
                end = now
        if counted:
            o["quarantines"] += 1
            o["quarantine_slot_hours"] += max(0.0, end - float(e["start"])) / 3600.0


def _quarantine_log(d: Path) -> list[dict]:
    p = Path(d) / QUARANTINE_LOG
    if not p.exists():
        return []
    ev = []
    for line in p.read_text().splitlines():
        try:
            ev.append(json.loads(line))
        except ValueError:
            continue
    return ev


def _in_force(d: Path, q: dict) -> bool:
    return _pid_alive(q.get("pid")) and _lock_held(Path(d) / f"{q['slot']}.lock")


def quarantined_slots(d: Path) -> set[str]:
    """The slots in a quarantine in force now, whichever process holds it, by the rule the status report counts (`_fold_quarantines`): no "free" line,
    the holder alive, the slot's lock held. A dead holder's last line refuses nobody."""
    ev = _quarantine_log(d)
    freed = {(e["slot"], e["start"]) for e in ev if e.get("ev") == "free"}
    return {e["slot"] for e in ev if e.get("ev") == "q" and (e["slot"], e["start"]) not in freed and _in_force(d, e)}


def _lock_held(path: Path) -> bool:
    if not path.exists():
        return False
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except OSError:
            return True


# ---- the pipeline's side: intervals from run records
def interval_from(run_key: str, option: str, onedata_slot: Mapping[str, Any]) -> RunInterval:
    """`RunInterval(run_key, option, slot, start, end)` from a run record's `onedata_slot`; loud on a missing or
    open interval (a run without one cannot be attributed, and must not be dropped silently)."""
    try:
        slot, start, end = onedata_slot["slot"], onedata_slot["start"], onedata_slot["end"]
    except (KeyError, TypeError):
        raise ValueError(f"{run_key}: no complete onedata_slot in the run record") from None
    if not isinstance(slot, str) or not slot or start is None or end is None or not float(end) >= float(start):
        raise ValueError(f"{run_key}: onedata_slot is incomplete or inverted: {dict(onedata_slot)}")
    return RunInterval(run_key, option, slot, float(start), float(end))


def run_interval(record: Mapping[str, Any]) -> RunInterval:
    """From a `run_record.json` dict (the harness) -- run key and option from its `run`, the rest from extras."""
    r = record["run"]
    key = f"{r['instance_id']}__{r['option']}__{r['layout']}__{r['model']}__s{r['seed']}"
    return interval_from(key, str(r["option"]), (record.get("extras") or {}).get("onedata_slot") or {})


def run_intervals(records: Iterable[Mapping[str, Any]]) -> list[RunInterval]:
    return [run_interval(r) for r in records]
