# Provenance: new
"""What the elastic ledger shows: its runs, leases, federations, events, charges, slots and estimates (C42).

Every view is read from the file, so a reopened ledger shows exactly what it showed before it closed.
"""

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from expcore.campaign.elastic import caps as cap_table
from expcore.campaign.elastic import declarations, pauses, schema
from expcore.campaign.elastic.ceilings import effective, history
from expcore.campaign.elastic.config import PRIOR_RUNS, LedgerConfig
from expcore.campaign.elastic.records import (
    HELD,
    CapChange,
    Ceiling,
    Ceilings,
    Declaration,
    Demand,
    Federation,
    Lease,
    LedgerError,
    OperatorPause,
    Run,
    Settlement,
    Status,
)
from expcore.campaign.elastic.settling import read as read_settlements

_LEASE_SQL = "SELECT l.*, f.ended_at AS fed_ended FROM leases l JOIN federations f ON f.label = l.federation"
# The meta key of the environment pinned at the first join: one for the campaign, or with declared platforms (C50) one
# for each, under this prefix and the platform's SHA-256.
ENVIRONMENT_KEY = "environment"
ENVIRONMENT_PREFIX = ENVIRONMENT_KEY + ":"


def environment_key(platform: str | None) -> str:
    """Where the first join pins the environment: the campaign's one key, or the platform's own (C50)."""
    return ENVIRONMENT_KEY if platform is None else ENVIRONMENT_PREFIX + platform


class LedgerViews:
    """The reads of a ledger open on `db` under `config`."""

    db: sqlite3.Connection
    config: LedgerConfig

    def _meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row[0]

    def platform_environments(self) -> dict[str, str]:
        """Each declared platform's environment digest as its first join pinned it (C50); empty without a set or before
        a join on it."""
        rows = self.db.execute("SELECT key, value FROM meta WHERE key LIKE ?", (ENVIRONMENT_PREFIX + "%",))
        return {key[len(ENVIRONMENT_PREFIX) :]: value for key, value in rows}

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
        return self._as_lease(row, declarations.lease_sets(self.db, lease_id))

    def _as_lease(self, row: Any, sets: Mapping[int, Any]) -> Lease:
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
            declared=sets.get(row["id"]),
        )

    def lease(self, lease_id: int) -> Lease:
        return self._lease_view(lease_id)

    def leases(self) -> list[Lease]:
        sets = declarations.lease_sets(self.db)
        return [self._as_lease(row, sets) for row in self.db.execute(f"{_LEASE_SQL} ORDER BY l.id")]

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
        reads (with `caps()`). A paused model's runs still count (C48): its federations stay up through the pause, so
        its leases go out the moment it is declared."""
        current = self.db.execute("SELECT MIN(set_index) FROM runs WHERE state='pending'").fetchone()[0]
        rows = self.db.execute(
            "SELECT model, COUNT(*) FROM runs WHERE state='pending' AND set_index=? GROUP BY model", (current,)
        ).fetchall()
        return Demand(set_index=current, pending=dict(rows), slots=self.slots())

    def ceilings(self) -> Ceilings:
        """The dollar ceilings in force: the pinned ones, or as raised (`raise_ceiling`)."""
        return effective(self.db, self.config)

    def ceiling_history(self) -> list[Ceiling]:
        """Every raise of a ceiling, oldest first, with its time, reason and actor."""
        return history(self.db)

    def caps(self) -> dict[str, int]:
        """The caps in force: the pinned ones, or as changed (`set_cap`)."""
        return cap_table.effective(self.db, self.config)

    def cap_history(self) -> list[CapChange]:
        """Every change of a cap, oldest first, with its time, reason and actor."""
        return cap_table.history(self.db)

    def declaration_log(self) -> list[Declaration] | None:
        """Every declaration, oldest first, with its time, reason and actor; None for a ledger made before the log."""
        return declarations.history(self.db)

    def paused(self) -> dict[str, tuple[str | None, ...]]:
        """Each model paused by values its leases' runs saw undeclared, with those values (C48)."""
        return declarations.paused(self.db)

    def operator_pauses(self) -> dict[str, OperatorPause]:
        """Each model an operator has paused and not resumed, with the pause that holds it (C55)."""
        return pauses.held(self.db)

    def pause_log(self) -> list[OperatorPause]:
        """Every operator pause and resume, oldest first, with its time, reason and actor (C55)."""
        return pauses.log(self.db)

    def settlements(self) -> list[Settlement]:
        """Every lost lease whose spend was settled, by lease id, with its time and source."""
        return read_settlements(self.db)

    def status(self) -> Status:
        """The ceiling and its history, what is committed and left, and what unsettled leases lock as their grants,
        read in one transaction so an operator's raise from another process cannot split it."""
        if self.db.in_transaction:
            return self._status()
        self.db.execute("BEGIN")
        try:
            return self._status()
        finally:
            self.db.execute("COMMIT")

    def _status(self) -> Status:
        in_force = self.ceilings()
        total, per_model = self.committed_micro()
        marks = ", ".join("?" * len(HELD))
        locked = self.db.execute(
            f"SELECT l.model, l.state, SUM(l.charge_micro) FROM leases l WHERE l.state IN ({marks})"
            " AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.lease_id = l.id) GROUP BY l.model, l.state",
            HELD,
        ).fetchall()
        by_state: dict[str, int] = {}
        by_model: dict[str, int] = {}
        for model, state, amount in locked:
            by_state[state] = by_state.get(state, 0) + int(amount)
            by_model[model] = by_model.get(model, 0) + int(amount)
        return Status(
            ceiling_micro=in_force.max_micro,
            committed_micro=total,
            remaining_micro=None if in_force.max_micro is None else in_force.max_micro - total,
            locked_micro=sum(by_state.values()),
            locked_by_state=by_state,
            locked_by_model=by_model,
            model_ceiling_micro=dict(in_force.per_model_micro),
            model_remaining_micro={m: c - per_model.get(m, 0) for m, c in in_force.per_model_micro.items()},
            ceilings=tuple(self.ceiling_history()),
            caps=self.caps(),
            cap_changes=tuple(self.cap_history()),
            declared=declarations.declared_by_model(self.db),
            declarations=None if (logged := self.declaration_log()) is None else tuple(logged),
            paused=self.paused(),
            operator_pauses=self.operator_pauses(),
            pause_log=tuple(self.pause_log()),
        )


class LedgerReader(LedgerViews):
    """A ledger file read without its dispatcher lock, for an operator's `status` while a dispatcher runs: the views
    only, over one connection that writes nothing."""

    def __init__(self, path: Path) -> None:
        self.db = schema.connect(path)
        try:
            pinned = self._meta("config")
            if pinned is None:
                raise ValueError(f"{path} holds no ledger configuration")
            self.config = LedgerConfig.from_json(pinned)
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()
