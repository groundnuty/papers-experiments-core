# Provenance: new
"""What the elastic ledger shows: its runs, leases, federations, events, charges, slots and estimates (C42).

Every view is read from the file, so a reopened ledger shows exactly what it showed before it closed.
"""

import json
import sqlite3
from typing import Any

from expcore.campaign.elastic.config import PRIOR_RUNS, LedgerConfig
from expcore.campaign.elastic.records import Demand, Federation, Lease, LedgerError, Run

_LEASE_SQL = "SELECT l.*, f.ended_at AS fed_ended FROM leases l JOIN federations f ON f.label = l.federation"


class LedgerViews:
    """The reads of a ledger open on `db` under `config`."""

    db: sqlite3.Connection
    config: LedgerConfig

    def _meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row[0]

    def committed_micro(self) -> tuple[int, dict[str, int]]:
        """What the budget counts: every lease's charge, in micro-dollars, in total and per model."""
        rows = self.db.execute("SELECT model, SUM(charge_micro) FROM leases GROUP BY model").fetchall()
        per_model = {model: int(total) for model, total in rows}
        return sum(per_model.values()), per_model

    def slots(self) -> dict[str, int]:
        """The cap slots held, per model: leases in flight, and expired ones whose holder is not proven ended."""
        rows = self.db.execute("SELECT model, COUNT(*) FROM leases WHERE slot=1 GROUP BY model").fetchall()
        return dict(rows)

    def estimate_s(self, model: str, condition: str) -> float:
        """A cell's expected lease time: the prior as `PRIOR_RUNS` runs, with every counted lease's time."""
        row = self.db.execute(
            "SELECT runs, total_s FROM estimates WHERE model=? AND condition=?", (model, condition)
        ).fetchone()
        runs, total = (0, 0.0) if row is None else (row[0], row[1])
        return (PRIOR_RUNS * self.config.prior_run_s + total) / (PRIOR_RUNS + runs)

    def _lease_view(self, lease_id: int) -> Lease:
        row = self.db.execute(f"{_LEASE_SQL} WHERE l.id=?", (lease_id,)).fetchone()
        if row is None:
            raise LedgerError(f"lease {lease_id} was never issued by this ledger")
        return self._as_lease(row)

    def _as_lease(self, row: Any) -> Lease:
        completion = None if row["completion"] is None else json.loads(row["completion"])
        return Lease(
            lease_id=row["id"],
            run_key=row["run_key"],
            model=row["model"],
            condition=row["condition"],
            federation=row["federation"],
            site=row["site"],
            fingerprint=self.config.fingerprint,
            attempt_base=row["attempt_base"],
            grant_micro=row["grant_micro"],
            issued_at=row["issued_at"],
            renewed_at=row["renewed_at"],
            counter=row["counter"],
            hard_term_at=row["hard_term_at"],
            state=row["state"],
            slot=bool(row["slot"]),
            charge_micro=row["charge_micro"],
            verdict=row["verdict"],
            completion_state=None if completion is None else completion["state"],
            proven=row["fed_ended"] is not None,
        )

    def lease(self, lease_id: int) -> Lease:
        return self._lease_view(lease_id)

    def leases(self) -> list[Lease]:
        return [self._as_lease(row) for row in self.db.execute(f"{_LEASE_SQL} ORDER BY l.id")]

    def run(self, run_key: str) -> Run:
        row = self.db.execute("SELECT * FROM runs WHERE run_key=?", (run_key,)).fetchone()
        if row is None:
            raise LedgerError(f"run {run_key} is not in the ledger")
        return Run(
            *(row[k] for k in ("run_key", "seq", "model", "condition", "set_index", "state", "charged", "lease_id"))
        )

    def runs(self) -> list[Run]:
        return [self.run(row[0]) for row in self.db.execute("SELECT run_key FROM runs ORDER BY seq")]

    def federations(self) -> list[Federation]:
        return [
            Federation(
                row["label"],
                row["site"],
                tuple(json.loads(row["job_ids"])),
                row["job_end_at"],
                row["kill_wait_s"],
                row["joined_at"],
                row["drained_at"],
                row["ended_at"],
                row["end_proof"],
            )
            for row in self.db.execute("SELECT * FROM federations ORDER BY joined_at, label")
        ]

    def events(self) -> list[dict[str, Any]]:
        """The ledger's log in its global sequence."""
        return [
            {**dict(row), "detail": None if row["detail"] is None else json.loads(row["detail"])}
            for row in self.db.execute("SELECT * FROM events ORDER BY seq")
        ]

    def demand(self) -> Demand:
        """The current set, its pending runs per model, and the cap slots held per model: what a controller's target
        reads (with `config.caps`)."""
        current = self.db.execute("SELECT MIN(set_index) FROM runs WHERE state='pending'").fetchone()[0]
        rows = self.db.execute(
            "SELECT model, COUNT(*) FROM runs WHERE state='pending' AND set_index=? GROUP BY model", (current,)
        ).fetchall()
        return Demand(set_index=current, pending=dict(rows), slots=self.slots())
