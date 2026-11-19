# Provenance: new
"""The campaign's ledger for an elastic fleet (#462 step 1, C42): its runs, the leases on them, and who may count.

One SQLite file per campaign on the orchestrating host (`schema`), held by one dispatcher lock (`campaign.lock`).
Federations join with the campaign's fingerprint and an environment pinned at the first join, and take one run at a
time on a lease (`issue`, `choice`). A site reading (`observe_site`) brings heartbeats, completions, returns and jobs
shown ended; the sweep expires what it must.

- **Fencing.** The lease id is the token. A completion counts only if it comes from its run's latest lease and the run
  is not done; any other is `superseded`: kept, its spend counted, never scored. The rule reads lease order and never
  outcomes, and the events' sequence records every step of it. The run counts as done only once its archive is
  verified (`verify_archive`); one that never verifies is `lost` and its run goes back to pending.
- **Expiry.** A lease expires when its site was read and its counter stood still for `stale_s` of reads, when it is
  past its hard term, or when its site went unread for `site_out_s`. After a restart no lease expires until its site
  is read, save by the site's own `site_out_s`, counted from the reopen. Expiry puts the run back to pending at once.
- **The dead-holder rule.** An expired lease keeps its cap slot and its grant until its holder's job is proven ended:
  a terminal state or the controller's cancel (`prove_ended`), or the recorded job end plus KillWait once its site was
  read after that moment (so any completion it left is collected first) or its site is out, as `campaign.lease`
  holds back a key whose holder may still be live. Proven ended with no completion, the lease stays
  charged its grant, the bound of what it can have spent. A completion or a return read later settles it.
- **Budget.** A lease is issued only if its grant fits what is left of `max_usd` and of its model's ceiling after the
  charge of every lease. A lease is charged its grant until its spend is settled, so with every lease spending its
  whole grant the budget still holds.
- **Caps.** A model's cap on its leases in flight or held back is the pinned one until an operator changes it, up or
  down, mid-campaign (`caps`); a lower cap revokes nothing and only stops new leases until the model is under it.
- **Declared fingerprints.** A model's fingerprints are declared in an append-only log, never in the fingerprint
  (`declarations`, C48): each lease carries its model's set at issue, its run judges its replies against it, and a
  model whose lease's run saw a value its set does not declare is paused (no new lease; leases out run on) until every
  value seen is declared.
- **Settling and raising.** A lost lease (`settle`) is charged the spend read from its dead federation instead of its
  grant, and the difference goes back to the budget (`settling`). A ceiling is raised mid-campaign, recorded, never
  lowered or set below what is committed, and never part of the fingerprint (`ceilings`); `status` shows the money,
  the grants locked in unsettled leases and the ceilings' history.
"""

import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from expcore.campaign.elastic import attempts, caps, ceilings, declarations, schema, settling
from expcore.campaign.elastic.choice import InFlight, Pending, choose
from expcore.campaign.elastic.config import LedgerConfig, LedgerRun, check_plan, micro_up
from expcore.campaign.elastic.records import (
    LIVE,
    PROOFS,
    CapChange,
    Ceiling,
    Completion,
    CompletionConflict,
    Declaration,
    JoinRefused,
    Lease,
    LedgerError,
    Return,
    Settlement,
    SettleRefused,
    SiteReading,
    Wait,
)
from expcore.campaign.elastic.views import LedgerViews
from expcore.campaign.lock import DispatchLock

_LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")  # an operator's label, as a shard's: never a host name or an address


class Ledger(LedgerViews):
    """One campaign's ledger, open under its dispatcher lock. `clock` gives seconds since the epoch."""

    def __init__(self, path: Path, lock: DispatchLock, clock: Callable[[], float]) -> None:
        self.path = path
        self._lock = lock
        self._clock = clock
        try:
            self.db = schema.connect(path)
        except BaseException:
            lock.release()
            raise
        try:
            pinned = self._meta("config")
            if pinned is None:
                raise ValueError(f"{path} holds no ledger configuration")
            self._config = LedgerConfig.from_json(pinned)
        except BaseException:
            self.close()
            raise
        self._fingerprint = self.config.fingerprint
        self._opened_at = clock()
        self._last_read: dict[str, float] = {}  # site -> when it was last read since this open; never persisted

    @property
    def config(self) -> LedgerConfig:
        """The configuration pinned at creation; read-only."""
        return self._config

    @classmethod
    def create(
        cls,
        path: Path,
        runs: list[LedgerRun],
        config: LedgerConfig,
        clock: Callable[[], float],
        declared: Mapping[str, Iterable[str]] | None = None,
    ) -> "Ledger":
        """Create a ledger for the runs and the configuration, with each model's first declared fingerprints
        (`declared`, models.toml's `accepted_fingerprints`; a model not named starts with none), and open it."""
        check_plan(runs, config)
        first = declarations.first_rows(declared or {}, {r.model for r in runs}, clock())
        lock = DispatchLock(path.parent).acquire()
        try:
            rows = [(r.run_key, r.seq, r.model, r.condition, r.set_index) for r in runs]
            schema.create_file(path, rows, {"config": config.to_json()}, first)
        except BaseException:
            lock.release()
            raise
        return cls(path, lock, clock)

    @classmethod
    def open(cls, path: Path, clock: Callable[[], float]) -> "Ledger":
        """Open an existing ledger: every lease, grant and slot comes back from the file."""
        lock = DispatchLock(path.parent).acquire()
        return cls(path, lock, clock)

    def close(self) -> None:
        """Close the file and release the lock."""
        try:
            self.db.close()
        finally:
            self._lock.release()

    # --- transactions -----------------------------------------------------------------------------------------

    def _transact(self, fn: Callable[[], Any]) -> Any:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = fn()
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return result

    def _event(self, now: float, kind: str, *, run_key=None, lease_id=None, federation=None, **detail: Any) -> None:
        self.db.execute(
            "INSERT INTO events(at, kind, run_key, lease_id, federation, detail) VALUES(?,?,?,?,?,?)",
            (now, kind, run_key, lease_id, federation, json.dumps(detail, sort_keys=True) if detail else None),
        )

    # --- federations ------------------------------------------------------------------------------------------

    def join(
        self,
        label: str,
        *,
        site: str,
        job_ids: tuple[str, ...],
        job_end_at: float,
        fingerprint: str,
        environment: str,
        kill_wait_s: float,
    ) -> None:
        """Record a federation whose `up` finished; refuse another fingerprint or environment, or a label in use."""
        for what, name in (("federation label", label), ("site label", site)):
            if not isinstance(name, str) or not _LABEL.fullmatch(name):
                raise ValueError(f"{what} {name!r} must be 1 to 64 of [a-z0-9-], starting with a letter or digit")
        if not job_ids or not all(isinstance(j, str) and j for j in job_ids):
            raise ValueError("job_ids must name at least one job")
        for what, value in (("job_end_at", job_end_at), ("kill_wait_s", kill_wait_s)):
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{what} must be a finite number of seconds, not below zero, got {value!r}")
        if not isinstance(environment, str) or not environment:
            raise ValueError("environment must be the federation's environment identity")

        def go() -> None:
            if fingerprint != self._fingerprint:
                raise JoinRefused(f"federation {label}: its fingerprint {fingerprint!r} is not the campaign's")
            pinned = self._meta("environment")
            if pinned is not None and environment != pinned:
                raise JoinRefused(
                    f"federation {label}: its environment {environment!r} is not the one pinned at the first join"
                )
            if self.db.execute("SELECT 1 FROM federations WHERE label=?", (label,)).fetchone():
                raise JoinRefused(f"federation {label} has already joined: a label is used once")
            now = self._clock()
            if pinned is None:
                self.db.execute("INSERT INTO meta(key, value) VALUES('environment', ?)", (environment,))
            self.db.execute(
                "INSERT INTO federations(label, site, job_ids, job_end_at, kill_wait_s, joined_at) VALUES(?,?,?,?,?,?)",
                (label, site, json.dumps(list(job_ids)), float(job_end_at), float(kill_wait_s), now),
            )
            self._event(now, "join", federation=label, site=site, job_end_at=job_end_at)

        self._transact(go)

    def drain(self, label: str) -> None:
        """The federation takes no new lease from now on; a lease it holds runs to its end and counts as any other."""

        def go() -> None:
            if self._federation_row(label)["drained_at"] is not None:
                return
            now = self._clock()
            self.db.execute("UPDATE federations SET drained_at=? WHERE label=?", (now, label))
            self._event(now, "drain", federation=label)

        self._transact(go)

    def prove_ended(self, label: str, proof: str) -> None:
        """The federation's job is shown ended (`proof`: terminal, or cancelled by the controller): its held leases
        let go of their slots, and a lease still in flight on it expires first."""
        if proof not in PROOFS:
            raise ValueError(f"a job's end is proven by one of {', '.join(PROOFS)}, not {proof!r}")
        self._transact(lambda: self._prove(self._federation_row(label)["label"], proof, self._clock()))

    def _federation_row(self, label: str) -> Any:
        row = self.db.execute("SELECT * FROM federations WHERE label=?", (label,)).fetchone()
        if row is None:
            raise LedgerError(f"federation {label!r} has not joined")
        return row

    def _prove(self, label: str, proof: str, now: float) -> None:
        if self.db.execute("SELECT ended_at FROM federations WHERE label=?", (label,)).fetchone()[0] is not None:
            return
        self.db.execute("UPDATE federations SET ended_at=?, end_proof=? WHERE label=?", (now, proof, label))
        for row in self.db.execute("SELECT * FROM leases WHERE federation=? AND slot=1", (label,)).fetchall():
            if row["state"] in LIVE:
                self._expire(row, now, "ended")
            self.db.execute("UPDATE leases SET slot=0 WHERE id=?", (row["id"],))
            # proven ended with no completion: the loss is one attempt
            attempts.lose(self.db, self.config, now, row["id"])
        self._event(now, "proven_ended", federation=label, proof=proof)

    # --- leasing ----------------------------------------------------------------------------------------------

    def issue(self, label: str) -> Lease | Wait:
        """Lease the next run to the federation (`choice`), written `issued`; or say why it waits."""
        return self._transact(lambda: self._issue(label, self._clock()))

    def _issue(self, label: str, now: float) -> Lease | Wait:
        fed = self._federation_row(label)
        if fed["ended_at"] is not None:
            return Wait(f"federation {label} has ended ({fed['end_proof']})")
        if fed["drained_at"] is not None:
            return Wait(f"federation {label} is draining")
        held = self.db.execute("SELECT id, state FROM leases WHERE federation=? AND slot=1", (label,)).fetchone()
        if held is not None:
            return Wait(f"federation {label} holds lease {held['id']} ({held['state']})")
        current = self.db.execute("SELECT MIN(set_index) FROM runs WHERE state='pending'").fetchone()[0]
        if current is None:
            return Wait("no run is pending")
        pending = [
            Pending(r["run_key"], r["seq"], r["model"], r["condition"])
            for r in self.db.execute("SELECT * FROM runs WHERE state='pending' AND set_index=?", (current,))
        ]
        in_flight = [
            InFlight(r["model"], r["condition"], r["issued_at"])
            for r in self.db.execute("SELECT * FROM leases WHERE state IN ('issued', 'sent')")
        ]
        chosen = choose(
            current,
            pending,
            in_flight,
            self.slots(),
            self.committed_micro(),
            self.config,
            self.estimate_s,
            now,
            self.ceilings(),
            self.caps(),
            declarations.paused(self.db),
        )
        if isinstance(chosen, Wait):
            return chosen
        run = self.db.execute("SELECT * FROM runs WHERE run_key=?", (chosen.run_key,)).fetchone()
        grant = self.config.grant_micro(run["model"])
        cursor = self.db.execute(
            "INSERT INTO leases(run_key, federation, site, model, condition, attempt_base, grant_micro, issued_at,"
            " renewed_at, hard_term_at, state, charge_micro) VALUES(?,?,?,?,?,?,?,?,?,?,'issued',?)",
            (
                run["run_key"],
                label,
                fed["site"],
                run["model"],
                run["condition"],
                run["charged"],
                grant,
                now,
                now,
                now + self.config.hard_term_s,
                grant,
            ),
        )
        lease_id = cursor.lastrowid
        declarations.record_set(self.db, lease_id, run["model"])  # the set its run is judged by (C48)
        self.db.execute("UPDATE runs SET state='leased', lease_id=? WHERE run_key=?", (lease_id, run["run_key"]))
        self._event(now, "issue", run_key=run["run_key"], lease_id=lease_id, federation=label, grant_micro=grant)
        return self._lease_view(lease_id)

    def mark_sent(self, lease_id: int) -> None:
        """The lease's file landed in its federation's inbox."""

        def go() -> None:
            row = self._lease_row(lease_id)
            if row["state"] == "sent":
                return
            if row["state"] != "issued":
                raise LedgerError(f"lease {lease_id} is {row['state']}, so it cannot be marked sent")
            self.db.execute("UPDATE leases SET state='sent' WHERE id=?", (lease_id,))
            self._event(self._clock(), "sent", run_key=row["run_key"], lease_id=lease_id)

        self._transact(go)

    def _landed(self, lease_id: int, now: float) -> None:
        """A heartbeat shows the lease's file landed and its worker took it: an issued lease is sent, and can no
        longer be cancelled."""
        row = self._lease_row(lease_id)
        if row["state"] == "issued":
            self.db.execute("UPDATE leases SET state='sent' WHERE id=?", (lease_id,))
            self._event(now, "sent", run_key=row["run_key"], lease_id=lease_id, by="heartbeat")

    def cancel(self, lease_id: int) -> None:
        """An issued lease whose file never landed: nothing ran, so nothing is charged and its run goes back."""

        def go() -> None:
            row = self._lease_row(lease_id)
            if row["state"] != "issued":
                raise LedgerError(f"lease {lease_id} is {row['state']}: only an issued lease can be cancelled")
            if row["counter"] is not None:
                raise LedgerError(f"lease {lease_id} has a heartbeat: its file landed, so it cannot be cancelled")
            now = self._clock()
            self.db.execute(
                "UPDATE leases SET state='cancelled', slot=0, charge_micro=0, ended_at=? WHERE id=?", (now, lease_id)
            )
            attempts.requeue(self.db, self.config, row["run_key"])
            self._event(now, "cancel", run_key=row["run_key"], lease_id=lease_id)

        self._transact(go)

    def _lease_row(self, lease_id: int) -> Any:
        row = self.db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
        if row is None:
            raise LedgerError(f"lease {lease_id} was never issued by this ledger")
        return row

    # --- reading a site ---------------------------------------------------------------------------------------

    def observe_site(self, site: str, reading: SiteReading) -> None:
        """Apply one read of a site, all or nothing: completions first (collect before requeue), then returns,
        heartbeats and jobs shown ended, then the sweep. A lease or federation of another site is refused."""
        now = self._clock()
        read = {**self._last_read, site: now}

        def go() -> None:
            for lease_id in sorted(reading.lease_ids()):
                lease_site = self._lease_row(lease_id)["site"]
                if lease_site != site:
                    raise LedgerError(f"lease {lease_id} is at site {lease_site}, not at site {site}")
            for label in reading.ended:
                if self._federation_row(label)["site"] != site:
                    raise LedgerError(f"federation {label} is not at site {site}")
            for completion in reading.completions:
                self._report(completion, now)
            for returned in reading.returns:
                self._return(returned, now)
            for lease_id, counter in reading.heartbeats.items():
                self.db.execute(
                    "UPDATE leases SET counter=?, renewed_at=? WHERE id=? AND state IN ('issued', 'sent')"
                    " AND (counter IS NULL OR counter < ?)",
                    (counter, now, lease_id, counter),
                )
                self._landed(lease_id, now)
            for label, proof in reading.ended.items():
                self._prove(label, proof, now)
            self._sweep(now, read)

        self._transact(go)
        self._last_read = read

    def _report(self, completion: Completion, now: float) -> None:
        row = self._lease_row(completion.lease_id)
        if row["completion"] is not None:
            if json.loads(row["completion"]) == completion.as_dict():
                return  # the same file read again
            raise CompletionConflict(f"lease {row['id']} already has another completion: they differ")
        if row["state"] in ("returned", "cancelled"):
            raise CompletionConflict(f"lease {row['id']} was {row['state']}: a completion of it cannot be read")
        declarations.see(self.db, now, row, completion.undeclared)  # whatever its verdict (C48)
        run = self.db.execute("SELECT * FROM runs WHERE run_key=?", (row["run_key"],)).fetchone()
        counts = run["lease_id"] == row["id"] and run["state"] != "done"
        verdict = "counts" if counts else "superseded"
        self.db.execute(
            "UPDATE leases SET state='reported', slot=0, verdict=?, completion=?, reported_at=? WHERE id=?",
            (verdict, json.dumps(completion.as_dict(), sort_keys=True), now, row["id"]),
        )
        if counts:
            self.db.execute("UPDATE runs SET state='leased' WHERE run_key=?", (row["run_key"],))
        self._event(now, "report", run_key=row["run_key"], lease_id=row["id"], verdict=verdict, was=row["state"])

    def _return(self, returned: Return, now: float) -> None:
        row = self._lease_row(returned.lease_id)
        spend = micro_up(returned.spend_usd)
        charge = settling.charge_of(self.db, row["id"], spend)
        if row["state"] == "returned" and row["charge_micro"] == charge:
            return
        if row["state"] not in (*LIVE, "expired"):
            raise CompletionConflict(f"lease {row['id']} is {row['state']}, so it cannot be returned")
        declarations.see(self.db, now, row, returned.undeclared)  # its admission probe's (C48)
        self.db.execute(
            "UPDATE leases SET state='returned', slot=0, charge_micro=?, ended_at=? WHERE id=?",
            (charge, now, row["id"]),
        )
        if charge < spend:
            settling.contradicted(self.db, now, row, spend, charge)
        # it never started: its loss, if counted, is taken back
        attempts.unstarted(self.db, self.config, now, row["id"])
        if row["state"] in LIVE:
            attempts.requeue(self.db, self.config, row["run_key"])
        self._event(now, "return", run_key=row["run_key"], lease_id=row["id"], was=row["state"])

    def verify_archive(self, lease_id: int, sha256: str | None) -> str:
        """Settle a completion read: the archive pulled and hashed to `sha256` (None: it never arrived). A match
        replaces the grant with the measured spend and, for a counting completion, applies the status rule across
        leases; anything else is `lost`, still charged its grant. Return the lease's state."""
        return self._transact(lambda: self._verify(lease_id, sha256, self._clock()))

    def _verify(self, lease_id: int, sha256: str | None, now: float) -> str:
        row = self._lease_row(lease_id)
        if row["state"] in ("counted", "superseded", "lost"):
            return row["state"]
        if row["state"] != "reported":
            raise LedgerError(f"lease {lease_id} is {row['state']}: no completion of it was read")
        completion = json.loads(row["completion"])
        counts = row["verdict"] == "counts"
        if sha256 is None or sha256 != completion["archive_sha256"]:
            self.db.execute("UPDATE leases SET state='lost', ended_at=? WHERE id=?", (now, lease_id))
            if counts:
                attempts.requeue(self.db, self.config, row["run_key"])
            attempts.lose(self.db, self.config, now, lease_id, completion["charged"] if counts else 0)
            self._event(now, "lost", run_key=row["run_key"], lease_id=lease_id, counted=counts)
            return "lost"
        state = "counted" if counts else "superseded"
        measured = micro_up(completion["spend_usd"])
        charge = settling.charge_of(self.db, lease_id, measured)
        self.db.execute(
            "UPDATE leases SET state=?, charge_micro=?, ended_at=? WHERE id=?", (state, charge, now, lease_id)
        )
        if charge < measured:
            settling.contradicted(self.db, now, row, measured, charge)
        if counts:
            run = self.db.execute("SELECT * FROM runs WHERE run_key=?", (row["run_key"],)).fetchone()
            charged = run["charged"] + completion["charged"] - row["charged_added"]  # its loss was counted for one
            status = self._status_after(completion["state"], charged)
            self.db.execute("UPDATE runs SET state=?, charged=? WHERE run_key=?", (status, charged, row["run_key"]))
            self.db.execute("UPDATE leases SET charged_added=? WHERE id=?", (completion["charged"], lease_id))
            if status == "pending" and run["charged"] >= self.config.max_attempts:
                # `_report` put the run back to leased, but it had no attempt left: it was exhausted; this reopens it
                attempts.log_reopen(
                    self.db, self.config, now, row, completion["charged"], run["charged"], "its late completion"
                )
            self.db.execute(
                "INSERT INTO estimates(model, condition, runs, total_s) VALUES(?,?,1,?) ON CONFLICT(model, condition)"
                " DO UPDATE SET runs = runs + 1, total_s = total_s + excluded.total_s",
                (row["model"], row["condition"], row["reported_at"] - row["issued_at"]),
            )
        self._event(now, state, run_key=row["run_key"], lease_id=lease_id, outcome=completion["state"])
        return state

    def _status_after(self, state: str, charged: int) -> str:
        """Today's status rule (`Store._status_after`) applied across leases."""
        if state in ("done", "harness_error"):
            return state
        if state == "infra_fail" and charged >= self.config.max_attempts:
            return "infra_exhausted"
        return "pending"

    # --- expiry -----------------------------------------------------------------------------------------------

    def sweep(self) -> None:
        """Prove ended the jobs past their recorded end plus KillWait, and expire the leases that must expire."""
        now = self._clock()
        self._transact(lambda: self._sweep(now, self._last_read))

    def _site_out(self, now: float, last: float | None) -> bool:
        """A site unread for `site_out_s`, counted from the reopen when it was not read since."""
        return now - (self._opened_at if last is None else last) >= self.config.site_out_s

    def _sweep(self, now: float, read: dict[str, float]) -> None:
        # A job past its end plus KillWait is proven ended once its site was read after that moment, so the reading
        # collected any completion it left first, or once its site is out.
        for fed in self.db.execute("SELECT * FROM federations WHERE ended_at IS NULL").fetchall():
            gone = fed["job_end_at"] + fed["kill_wait_s"]
            last = read.get(fed["site"])
            if now >= gone and ((last is not None and last >= gone) or self._site_out(now, last)):
                self._prove(fed["label"], "walltime", now)
        cfg = self.config
        for row in self.db.execute("SELECT * FROM leases WHERE state IN ('issued', 'sent')").fetchall():
            last = read.get(row["site"])
            if self._site_out(now, last):
                self._expire(row, now, "site_out")
            elif last is None:
                continue  # not read since the reopen: nothing else expires it yet
            elif now >= row["hard_term_at"]:
                self._expire(row, now, "hard_term")
            elif last - row["renewed_at"] >= cfg.stale_s:
                self._expire(row, now, "stale")

    def _expire(self, row: Any, now: float, reason: str) -> None:
        """The lease expires and its run goes back to pending at once; its slot and grant stay held."""
        self.db.execute("UPDATE leases SET state='expired', ended_at=? WHERE id=?", (now, row["id"]))
        attempts.requeue(self.db, self.config, row["run_key"])
        self._event(
            now, "expire", run_key=row["run_key"], lease_id=row["id"], federation=row["federation"], reason=reason
        )

    # --- money ------------------------------------------------------------------------------------------------

    def settle(
        self, lease_id: int, spend_micro_usd: int, source: str, *, attempts_charged: int | None = None
    ) -> Settlement:
        """Replace a lost lease's grant with the spend read from its dead federation (`source`: `root` or `pulled`),
        and, when the log gives them, the lease's charged attempts (`attempts_charged`) for the one its loss counted.

        Only a lease expired with its holder proven ended, or one whose archive never verified, is settled; one
        already settled is refused unless the values are the same (then nothing changes), and a spend above the grant
        is recorded as a failed cap and refused (`SettleRefused`). The difference goes back to the budget."""
        settling.check(lease_id, spend_micro_usd, source, attempts_charged)
        outcome = self._transact(
            lambda: settling.apply(
                self.db, self.config, self._clock(), lease_id, spend_micro_usd, source, attempts_charged
            )
        )
        if isinstance(outcome, str):
            raise SettleRefused(outcome)  # the failure was committed first
        return outcome

    def raise_ceiling(self, new_max_micro_usd: int, reason: str, actor: str) -> Ceiling:
        """Raise the campaign's dollar ceiling to `new_max_micro_usd`, recorded with the time, `reason` and `actor`.
        Never lowers it or sets it below what is committed (`CeilingRefused`); the fingerprint does not change."""
        return self._transact(
            lambda: ceilings.raise_ceiling(self.db, self.config, self._clock(), new_max_micro_usd, reason, actor)
        )

    def set_cap(self, model: str, new_cap: int, reason: str, actor: str) -> CapChange:
        """Set a model's cap on the leases it holds at once, up or down, recorded with the time, `reason` and `actor`.
        It takes effect at the next `issue`; no lease already issued is revoked (`CapRefused` for a model with no runs
        here or a cap it already has)."""
        return self._transact(lambda: caps.set_cap(self.db, self.config, self._clock(), model, new_cap, reason, actor))

    def declare(self, model: str, fingerprint: str | None, reason: str, actor: str) -> Declaration:
        """Declare a fingerprint of a model, or "none" (`fingerprint` None), recorded with the time, `reason` and
        `actor`; the next `issue` reads it, and a model it was the last undeclared value of is no longer paused.
        `DeclareRefused` for a model with no runs here, a value already declared, or a ledger with no log."""
        return self._transact(lambda: declarations.declare(self.db, self._clock(), model, fingerprint, reason, actor))

    def raise_model_ceiling(self, model: str, new_max_micro_usd: int, reason: str, actor: str) -> Ceiling:
        """`raise_ceiling` for a model that has a ceiling of its own, never below what the model has committed."""
        if not isinstance(model, str) or not model:
            raise ValueError(f"a model's ceiling needs the model's key, got {model!r}")
        return self._transact(
            lambda: ceilings.raise_ceiling(self.db, self.config, self._clock(), new_max_micro_usd, reason, actor, model)
        )
