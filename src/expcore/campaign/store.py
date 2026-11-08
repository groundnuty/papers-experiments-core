# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/store.py, blob 8e7c6cbfd0a274d8ae91a5da4f3a167cc184aef6
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Durable campaign state in SQLite (WAL, synchronous=FULL).

Kill safety: a transaction either commits or does not. An attempt is inserted
as 'running' before the run starts and moved to its final state in one
transaction together with the run's status. A process killed in between leaves
a 'running' row; the next campaign process (holding the exclusive lock, so
nothing else is running) turns every such row into 'interrupted' and re-runs
the run with the SAME infra attempt number. Interrupted attempts are kept but
never count toward the 3-attempt infra budget: nobody observed an infra fault.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from .manifest import ManifestRow

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  run_key TEXT PRIMARY KEY, seq INTEGER NOT NULL, block TEXT NOT NULL,
  instance_id TEXT, option TEXT, layout TEXT, model TEXT, seed INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',
  exclude_from_analysis INTEGER NOT NULL DEFAULT 0);  -- pending|done|infra_exhausted|harness_error (back to pending when the campaign next opens)
CREATE TABLE IF NOT EXISTS attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_key TEXT NOT NULL, attempt INTEGER NOT NULL,
  state TEXT NOT NULL,  -- running|done|infra_fail|interrupted|harness_error
  slot TEXT, started_utc TEXT, ended_utc TEXT, wall_s REAL, pid INTEGER,
  infra_kind TEXT, infra_detail TEXT, termination TEXT,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
  cache_creation_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,  -- parts of input_tokens
  bytes_into_context INTEGER DEFAULT 0, bytes_moved INTEGER, run_dir TEXT,
  post_health_ok INTEGER, post_health_detail TEXT, slot_identity TEXT,
  usage_complete INTEGER DEFAULT 1, lease_wait_s REAL, container_death_cause TEXT);
CREATE INDEX IF NOT EXISTS attempts_run ON attempts(run_key);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL, set_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, model TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS unit_health(
  unit TEXT PRIMARY KEY, fails INTEGER NOT NULL, down INTEGER NOT NULL, pause_until_utc TEXT, model TEXT);
CREATE TABLE IF NOT EXISTS endpoint_windows(
  id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT, down_utc TEXT, up_utc TEXT, reason TEXT, parent TEXT);
"""


def _utc(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


class PinMismatch(RuntimeError):
    def __init__(self, key: str, pinned: str, now: str):
        super().__init__(f"{key} is pinned to {pinned} in this campaign, but is now {now}: refusing to resume with a different one "
                         "(re-pin only with the explicit operator flag)")
        self.key, self.pinned, self.now = key, pinned, now


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None,
                                  timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(runs)")]
        for col in ("cache_creation_tokens", "cache_read_tokens"):   # databases created before prompt caching
            if col not in [r[1] for r in self.db.execute("PRAGMA table_info(attempts)")]:
                self.db.execute(f"ALTER TABLE attempts ADD COLUMN {col} INTEGER DEFAULT 0")
        for col in ("reason", "parent"):
            if col not in [r[1] for r in self.db.execute("PRAGMA table_info(endpoint_windows)")]:
                self.db.execute(f"ALTER TABLE endpoint_windows ADD COLUMN {col} TEXT")
        if "model" not in [r[1] for r in self.db.execute("PRAGMA table_info(unit_health)")]:
            self.db.execute("ALTER TABLE unit_health ADD COLUMN model TEXT")
        have = [r[1] for r in self.db.execute("PRAGMA table_info(attempts)")]
        for col, typ in (("post_health_ok", "INTEGER"), ("post_health_detail", "TEXT")):
            if col not in have:  # each column on its own: a kill between the two must be repairable
                self.db.execute(f"ALTER TABLE attempts ADD COLUMN {col} {typ}")
        if "slot_identity" not in have:  # what the gate knew of the slot's checkpoint and fingerprint when the run ended
            self.db.execute("ALTER TABLE attempts ADD COLUMN slot_identity TEXT")
        for col, typ in (("usage_complete", "INTEGER DEFAULT 1"), ("lease_wait_s", "REAL")):
            if col not in have:
                self.db.execute(f"ALTER TABLE attempts ADD COLUMN {col} {typ}")
        if "container_death_cause" not in have:      # the cause of a gate or sidecar death in the attempt (dataset_76.sandbox.infradeath.CAUSES); NULL: none died
            self.db.execute("ALTER TABLE attempts ADD COLUMN container_death_cause TEXT")
        if "exec_host" not in have:          # two run hosts (hosts.py): where the attempt executed
            self.db.execute("ALTER TABLE attempts ADD COLUMN exec_host TEXT")
        if "deferred_utc" not in cols:           # amendment 24 (#2, 20:48Z): a run's first deferral by M's or D's cap
            self.db.execute("ALTER TABLE runs ADD COLUMN deferred_utc TEXT")
        if "deferred_why" not in cols:           # and why: M_cap, D_cap or D_disk, reported apart (science, #34, 21:14Z)
            self.db.execute("ALTER TABLE runs ADD COLUMN deferred_why TEXT")
        if "exclude_from_analysis" not in cols:  # state written before the calibration block existed
            self.db.execute("ALTER TABLE runs ADD COLUMN exclude_from_analysis INTEGER NOT NULL DEFAULT 0")

    # --- campaign-level facts that must not change under a resume (the pinned sandbox image id) -------------------
    def get_meta(self, key: str) -> str | None:
        r = self.q("SELECT value FROM meta WHERE key=?", (key,))
        return r[0]["value"] if r else None

    def put_meta(self, key: str, value: str, now: str) -> None:
        """Upsert a campaign-level fact that the status process reads (not a pin: it may change)."""
        self._tx(lambda: self.db.execute(
            "INSERT INTO meta(key,value,set_utc) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, set_utc=excluded.set_utc",
            (key, value, now)))

    def update_meta(self, key: str, fn: Callable[[str | None], tuple[str | None, list[tuple[str, str | None, str]], Any]], now: str) -> Any:
        """Read, change and write one campaign-level fact, with its events, in ONE transaction (BEGIN IMMEDIATE): `fn(current text or None)` returns (the new text or None to leave
        it, events, a result for the caller). Two writers of one fact, the dispatcher and a command run from a shell, never overwrite each other's step."""
        def go():
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            new, events, result = fn(row["value"] if row else None)
            if new is not None:
                self.db.execute("INSERT INTO meta(key,value,set_utc) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, set_utc=excluded.set_utc", (key, new, now))
            for kind, model, detail in events:
                self.db.execute("INSERT INTO events(ts,kind,model,detail) VALUES(?,?,?,?)", (now, kind, model, detail))
            return result
        return self._tx(go)

    def put_meta_many(self, facts: dict[str, str], now: str, events: Iterable[tuple[str, str | None, str]] = ()) -> None:
        """Upsert several campaign-level facts, and append events (kind, model, detail), in ONE transaction: facts that stand or fall together (a stop's
        count and its time) are never split by a kill, which would leave the one without the other."""
        def go():
            for key, value in facts.items():
                self.db.execute(
                    "INSERT INTO meta(key,value,set_utc) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, set_utc=excluded.set_utc",
                    (key, value, now))
            for kind, model, detail in events:
                self.db.execute("INSERT INTO events(ts,kind,model,detail) VALUES(?,?,?,?)", (now, kind, model, detail))
        self._tx(go)

    def pin(self, key: str, value: str, now: str, repin: bool = False) -> str:
        """Record `value` under `key` at the first start; later it must be the same value. A different one is refused
        (ImagePinMismatch) unless `repin` is set (an explicit operator decision), which replaces it and logs an event.
        Returns the value in force."""
        def go():
            cur = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if cur is None:
                self.db.execute("INSERT INTO meta(key,value,set_utc) VALUES(?,?,?)", (key, value, now))
                return value
            if cur["value"] == value:
                return value
            if not repin:
                raise PinMismatch(key, cur["value"], value)
            self.db.execute("UPDATE meta SET value=?, set_utc=? WHERE key=?", (value, now, key))
            self.db.execute("INSERT INTO events(ts,kind,model,detail) VALUES(?,?,?,?)",
                            (now, "repin", None, f"{key}: {cur['value']} -> {value} (operator flag)"))
            return value
        return self._tx(go)

    def close(self) -> None:
        with self._lock:        # a thread inside a statement while the connection closes under it segfaults the process; after this, a late write raises instead
            self.db.close()

    def _tx(self, fn):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                r = fn()
                self.db.execute("COMMIT")
                return r
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, tuple(args)).fetchall()

    # --- manifest and recovery -------------------------------------------------
    def sync_manifest(self, rows: list[ManifestRow]) -> int:
        """Insert missing rows and refresh seq/flag of existing ones (idempotent); status is kept."""
        def go():
            n = 0
            for r in rows:
                c = self.db.execute(
                    "INSERT OR IGNORE INTO runs(run_key,seq,block,instance_id,option,layout,model,seed,exclude_from_analysis)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (r.run_key, r.seq, r.block, r.run.instance_id, r.run.option.value,
                     r.run.layout.value, r.run.model, r.run.seed, int(r.exclude_from_analysis)))
                n += c.rowcount
                # keep order and flag current with the manifest; status is never touched
                self.db.execute("UPDATE runs SET seq=?, exclude_from_analysis=? WHERE run_key=?",
                                (r.seq, int(r.exclude_from_analysis), r.run_key))
            return n
        return self._tx(go)

    def recover(self, now: str) -> int:
        """Call only while holding the campaign lock: every 'running' row is stale."""
        def go():
            c = self.db.execute(
                "UPDATE attempts SET state='interrupted', ended_utc=? WHERE state='running'", (now,))
            return c.rowcount
        n = self._tx(go)
        if n:
            self.event(now, "recovered_interrupted", None, f"{n} attempt(s)")
        return n

    # --- attempts --------------------------------------------------------------
    def pending(self, blocks: list[str] | None = None) -> list[sqlite3.Row]:
        rows = self.q("SELECT * FROM runs WHERE status='pending' ORDER BY seq")
        return [r for r in rows if blocks is None or r["block"] in blocks]

    def infra_attempts(self, run_key: str) -> int:
        """The attempts charged against `max_attempts`. A key rejection (the key's fault, which holds the model) is an infra failure that is never charged; nor is a post-run check that failed during an Onedata outage (healthgate.py)."""
        return self.q("SELECT COUNT(*) c FROM attempts WHERE run_key=? AND state='infra_fail' AND COALESCE(infra_kind,'') NOT IN ('key_rejected','onedata_outage')",
                      (run_key,))[0]["c"]

    def begin_attempt(self, run_key: str, attempt: int, slot: str, started: str, pid: int,
                      run_dir: str) -> int:
        def go():
            if self.db.execute("SELECT status FROM runs WHERE run_key=?", (run_key,)).fetchone()[0] != "pending":
                raise RuntimeError(f"{run_key} is not pending")
            if self.db.execute("SELECT 1 FROM attempts WHERE run_key=? AND state='running'",
                               (run_key,)).fetchone():
                raise RuntimeError(f"{run_key} already has a running attempt")
            c = self.db.execute(
                "INSERT INTO attempts(run_key,attempt,state,slot,started_utc,pid,run_dir)"
                " VALUES(?,?,?,?,?,?,?)", (run_key, attempt, "running", slot, started, pid, run_dir))
            return c.lastrowid
        return self._tx(go)

    def backend_slots(self) -> dict[str, tuple[str, str, str, str | None]]:
        """run_key -> (model, option, layout, slot of its latest attempt, any state); runs without
        an attempt are absent."""
        return {r["run_key"]: (r["model"], r["option"], r["layout"], r["slot"]) for r in self.q(
            "SELECT run_key,model,option,layout,slot FROM attempts a JOIN runs USING(run_key)"
            " WHERE a.id=(SELECT MAX(id) FROM attempts b WHERE b.run_key=a.run_key)")}

    def requeue_harness_errors(self, keys: set[str], now: str) -> list[sqlite3.Row]:
        """Every run of `keys` whose status is 'harness_error' goes back to 'pending': the same row, run_key and seed, its attempts kept (they are the
        record of what failed). A run lost to our own bug is not an outcome (science, #34, 2 Nov 18:35Z). One transaction: no run changes status without
        its event. Returns the runs, with the attempt that failed last, for the caller's log. A run of a block no longer in the manifest keeps its state."""
        def go():
            back = [r for r in self.db.execute(
                "SELECT r.run_key, r.model, r.option, (SELECT COUNT(*) FROM attempts a WHERE a.run_key=r.run_key AND a.state='harness_error') n,"
                " (SELECT infra_detail FROM attempts a WHERE a.run_key=r.run_key AND a.state='harness_error' ORDER BY id DESC LIMIT 1) last_detail"
                " FROM runs r WHERE r.status='harness_error' ORDER BY r.seq").fetchall() if r["run_key"] in keys]
            for r in back:
                self.db.execute("UPDATE runs SET status='pending' WHERE run_key=? AND status='harness_error'", (r["run_key"],))
                self.db.execute("INSERT INTO events(ts,kind,model,detail) VALUES(?,?,?,?)",
                                (now, "harness_error_requeued", r["model"],
                                 f"{r['run_key']}: pending again from the same row and seed; {r['n']} harness-error attempt(s) kept, the last: {(r['last_detail'] or '')[:200]}"))
            return back
        return self._tx(go)

    def set_deferred(self, run_key: str, ts: str, why: str | None = None) -> None:
        """Amendment 24: the run's FIRST deferral by its host's M or D cap or disk bound, and why (a later one moves neither)."""
        self._tx(lambda: self.db.execute("UPDATE runs SET deferred_utc=?, deferred_why=? WHERE run_key=? AND deferred_utc IS NULL", (ts, why, run_key)))

    def counted_starts(self, run_key: str | None = None) -> dict[str, str]:
        """run key -> when it counts as started (science, #34, 21:33Z; amendment 24): the earliest of its first started attempt (state != 'unstarted', as
        export._in_cohort reads it) and its deferral. A retry, or a re-queued harness_error run, is the same run restarting: it never moves this."""
        where, args = ("WHERE run_key=?", (run_key,)) if run_key is not None else ("", ())
        out: dict[str, str] = {}

        def note(k: str, ts: str | None) -> None:
            if ts and (k not in out or _utc(ts) < _utc(out[k])):
                out[k] = ts
        for r in self.q(f"SELECT run_key, started_utc FROM attempts {where} {'AND' if where else 'WHERE'} state != 'unstarted' AND started_utc IS NOT NULL", args):
            note(r["run_key"], r["started_utc"])
        for r in self.q(f"SELECT run_key, deferred_utc FROM runs {where} {'AND' if where else 'WHERE'} deferred_utc IS NOT NULL", args):
            note(r["run_key"], r["deferred_utc"])
        return out

    def deferred(self, run_key: str) -> str | None:
        r = self.q("SELECT deferred_utc FROM runs WHERE run_key=?", (run_key,))
        return r[0]["deferred_utc"] if r else None

    def run_cell(self, run_key: str) -> tuple[str, str, str]:
        r = self.q("SELECT model,option,layout FROM runs WHERE run_key=?", (run_key,))[0]
        return r["model"], r["option"], r["layout"]

    def save_unit(self, unit: str, fails: int, down: bool, pause_until_utc: str | None,
                  model: str | None = None) -> None:
        self._tx(lambda: self.db.execute(
            "INSERT INTO unit_health(unit,fails,down,pause_until_utc,model) VALUES(?,?,?,?,?)"
            " ON CONFLICT(unit) DO UPDATE SET fails=excluded.fails, down=excluded.down,"
            " pause_until_utc=excluded.pause_until_utc, model=excluded.model",
            (unit, fails, int(down), pause_until_utc, model)))

    def load_units(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM unit_health")

    def set_usage(self, attempt_id: int, **cols: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in cols)
        self._tx(lambda: self.db.execute(f"UPDATE attempts SET {sets} WHERE id=?", (*cols.values(), attempt_id)))

    def running_attempts(self) -> list[sqlite3.Row]:
        return self.q("SELECT a.id, a.run_key, a.run_dir, a.slot, r.block, r.model, r.exclude_from_analysis"
                      " FROM attempts a JOIN runs r USING(run_key) WHERE a.state='running'")

    def finish_attempt(self, attempt_id: int, run_key: str, state: str, run_status: str,
                       ended: str, wall_s: float, **cols: Any) -> None:
        """One transaction: the attempt's final state and the run's status."""
        def go():
            sets = ["state=?", "ended_utc=?", "wall_s=?"] + [f"{k}=?" for k in cols]
            self.db.execute(f"UPDATE attempts SET {','.join(sets)} WHERE id=? AND state='running'",
                            (state, ended, wall_s, *cols.values(), attempt_id))
            self.db.execute("UPDATE runs SET status=? WHERE run_key=?", (run_status, run_key))
        self._tx(go)

    # --- events and endpoint windows ------------------------------------------
    def event(self, ts: str, kind: str, model: str | None, detail: str) -> None:
        self._tx(lambda: self.db.execute(
            "INSERT INTO events(ts,kind,model,detail) VALUES(?,?,?,?)", (ts, kind, model, detail)))

    def set_model_up(self, model: str, up: bool, now: str, reason: str | None = None,
                     parent: str | None = None) -> None:
        """Open or close the model's down-window; writes only on a change."""
        is_open = bool(self.q("SELECT 1 FROM endpoint_windows WHERE model=? AND up_utc IS NULL", (model,)))
        if up != is_open:
            return  # up and no open window, or down and already open: nothing to record

        def go():
            if up:
                self.db.execute("UPDATE endpoint_windows SET up_utc=? WHERE model=? AND up_utc IS NULL",
                                (now, model))
            else:
                self.db.execute("INSERT INTO endpoint_windows(model,down_utc,reason,parent) VALUES(?,?,?,?)",
                                (model, now, reason, parent))
        self._tx(go)


def unstarted_streaks(store: Store, stop_after: int) -> dict[str, int]:
    """Per option, the runs in a row refused a sandbox for want of a slot user. An unreadable record stops dispatch (fail closed), under the name "*"."""
    try:
        return {str(k): int(v) for k, v in json.loads(store.get_meta("unstarted_streaks") or "{}").items()}
    except (ValueError, TypeError, AttributeError):
        return {"*": stop_after}


def harness_error_kind(detail: str | None) -> str:
    """The kind of a harness error: the exception's type name, the part of the attempt's detail (`<Type>: <message>`) before its first colon. A remote run reports every
    error as `RemoteHarnessError: <Type>: <message>` (hosts.HostRouter): its kind is the inner type, so two remote bugs of different types never pool (science, #34, 3 Nov)."""
    head, _, rest = (detail or "").partition(":")
    head = head.strip()
    if head == "RemoteHarnessError":
        inner = rest.split(":", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", inner):
            return inner
    return head or "unknown"


def harness_errors_all_time(store: Store) -> dict[str, dict[str, int]]:
    """Every harness-error attempt in the store, per kind and per option, whatever was released or re-queued since (the report, not the stop's count)."""
    out: dict[str, dict[str, int]] = {}
    for r in store.q("SELECT r.option, a.infra_detail FROM attempts a JOIN runs r USING(run_key) WHERE a.state='harness_error'"):
        per = out.setdefault(harness_error_kind(r["infra_detail"]), {})
        per[r["option"]] = per.get(r["option"], 0) + 1
    return out
