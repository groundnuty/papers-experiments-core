# Provenance: new
"""A model's lease grant raised on an existing ledger, before any lease of the model is issued (C65).

Workers on a core that cannot be changed read a lease's grant from the ledger and take it as their whole budget, and
the ledger's pinned configuration is read by cores that predate this change (a sibling controller, the merge). So the
raise is kept where such a core does not look, and the configuration stays byte for byte as it was:

- the grants in force are the pinned ones, replaced by `OVERRIDES_KEY`, a meta row of its own holding model to
  micro-dollars, written by a raise and by nothing else (a ledger never raised has no such row);
- each model raised is an event of the log (`SET_GRANT`: model, old and new grant, reason, actor, the time as the
  event's), so `status` can show the history; a core that does not know the kind ignores it;
- the issue path reads the grants in force inside its own transaction, at every lease (`Ledger._issue`), so the grant
  written into each new lease row, which is what a worker reads, is the raised one. The fingerprint is untouched.

The rules, each refused (`GrantRefused`) with nothing written, the models of one call together or not at all:

- a grant is only raised: the new one must be above the one in force (a second raise is judged against the first);
- no lease of the model may ever have been issued, in any state, so no issued lease's accounting changes: from the
  first lease on, the grant of a model is pinned as it always was;
- the ledger needs a budget (without one a lease is granted nothing), the model needs runs in it and a grant above 0
  (a free model's is 0), and the new grant may not pass the campaign's ceiling or the model's own, as raised;
- the caps must still fit: the sum over capped models of cap times grant (raised ones counted at their new grant) may
  not pass the campaign's ceiling, or not every cap slot could hold a lease at once. A model with no cap is not
  counted: nothing bounds how many of its leases are out;
- the grant must be one a figure of dollars holds exactly (`config.check_grant_held_exactly`).

`set_in_file` takes the dispatcher's own lock for its one transaction, so it is refused while a controller holds the
ledger and no controller starts under it. That is the safe choice: a controller of a core that predates this change
ignores the raise and would keep issuing at the pinned grant, and nothing a controller leaves says which core it is
(`awareness` can tell a pause-aware core, not one that reads grants). The controller that holds the ledger next must
run a core that reads the raise: this one or later.
"""

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from expcore.campaign.elastic import caps as cap_table
from expcore.campaign.elastic import schema
from expcore.campaign.elastic.ceilings import effective as ceilings_in_force
from expcore.campaign.elastic.ceilings import text_of
from expcore.campaign.elastic.config import LedgerConfig, check_grant_held_exactly, usd_of
from expcore.campaign.elastic.records import GrantRaise, GrantRefused, LedgerError
from expcore.campaign.lock import CampaignLocked, DispatchLock

OVERRIDES_KEY = "grant_overrides_micro_usd"
SET_GRANT = "set_grant"


def overrides(db: sqlite3.Connection) -> dict[str, int]:
    """Each model's raised grant in micro-dollars (empty for a ledger never raised)."""
    row = db.execute("SELECT value FROM meta WHERE key=?", (OVERRIDES_KEY,)).fetchone()
    if row is None:
        return {}
    try:
        return {str(model): int(micro) for model, micro in json.loads(row[0]).items()}
    except (TypeError, ValueError, AttributeError):
        raise LedgerError(
            f"the meta row {OVERRIDES_KEY} cannot be read: it is not a mapping of models to micro-dollars"
        ) from None


def in_force(db: sqlite3.Connection, config: LedgerConfig) -> dict[str, int]:
    """Each model's lease grant in micro-dollars now: the pinned one, replaced by its raise. A ledger with no budget
    grants nothing, whatever a row says."""
    grants = {model: config.grant_micro(model) for model in config.grants_usd}
    if config.max_usd is not None:
        grants.update(overrides(db))
    return grants


def log(db: sqlite3.Connection) -> list[GrantRaise]:
    """Every raise, oldest first (empty for a ledger never raised)."""
    out = []
    for seq, at, detail in db.execute("SELECT seq, at, detail FROM events WHERE kind=? ORDER BY seq", (SET_GRANT,)):
        try:
            found = json.loads(detail)
            out.append(
                GrantRaise(
                    seq, at, found["model"], found["old_micro"], found["new_micro"], found["reason"], found["actor"]
                )
            )
        except (TypeError, ValueError, KeyError):
            raise LedgerError(
                f"event {seq} ({SET_GRANT}) holds no model, grants, reason and actor: it cannot be read"
            ) from None
    return out


def _check_one(db: sqlite3.Connection, model: str, old: int, new: int) -> None:
    if isinstance(new, bool) or not isinstance(new, int) or new < 1:
        raise ValueError(f"the new grant of {model} must be a whole number of micro-dollars from 1, got {new!r}")
    if db.execute("SELECT 1 FROM runs WHERE model=? LIMIT 1", (model,)).fetchone() is None:
        raise GrantRefused(f"the ledger has no runs of model {model} (no lease of it has a grant to raise)")
    if old == 0:
        raise GrantRefused(f"model {model} is free (a lease of it is granted 0): it has no grant to raise")
    leased = db.execute("SELECT id FROM leases WHERE model=? ORDER BY id LIMIT 1", (model,)).fetchone()
    if leased is not None:
        raise GrantRefused(
            f"model {model} has had lease {leased['id']} issued: its grant is pinned from the first lease, so that no "
            "issued lease's accounting changes"
        )
    if new <= old:
        raise GrantRefused(
            f"model {model}'s grant is {usd_of(old):g} USD: {usd_of(new):g} is not above it, and a grant is only raised"
        )
    try:
        check_grant_held_exactly(model, new)
    except ValueError as error:
        raise GrantRefused(str(error)) from None


def set_grants(
    db: sqlite3.Connection, config: LedgerConfig, now: float, wanted: Mapping[str, int], reason: str, actor: str
) -> list[GrantRaise]:
    """Raise the grants of the models in `wanted` (micro-dollars) in the caller's transaction and record each. Refused
    (`GrantRefused`, nothing written) as the module's docstring says."""
    reason, actor = text_of("reason", reason), text_of("actor", actor)
    if not wanted:
        raise ValueError("a raise names at least one model")
    if config.max_usd is None:
        raise GrantRefused("the ledger has no budget: a lease is granted nothing, so there is no grant to raise")
    current = in_force(db, config)
    ceilings = ceilings_in_force(db, config)
    raised: dict[str, tuple[int, int]] = {}
    for model, new in sorted(wanted.items()):
        old = current.get(model, 0)
        _check_one(db, model, old, new)
        for whose, ceiling in (
            ("the campaign's ceiling", ceilings.max_micro),
            (f"model {model}'s own ceiling", ceilings.per_model_micro.get(model)),
        ):
            if ceiling is not None and new > ceiling:
                raise GrantRefused(
                    f"model {model}'s new grant {usd_of(new):g} USD is above {whose}, {usd_of(ceiling):g} USD: no "
                    "lease of it could go out"
                )
        raised[model] = (old, new)
    after = {**current, **{model: new for model, (_, new) in raised.items()}}
    terms = [(cap, after.get(model, 0)) for model, cap in sorted(cap_table.effective(db, config).items())]
    total = sum(cap * grant for cap, grant in terms)
    if ceilings.max_micro is not None and total > ceilings.max_micro:
        sums = " + ".join(f"{cap} x {usd_of(grant):g}" for cap, grant in terms if grant)
        raise GrantRefused(
            f"the caps no longer fit the budget: {sums} = {usd_of(total):g} USD is over the campaign's ceiling of "
            f"{usd_of(ceilings.max_micro):g} USD, so not every cap slot could hold a lease at once; raise the ceiling "
            "or lower a cap first"
        )
    db.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (OVERRIDES_KEY, json.dumps({**overrides(db), **{m: new for m, (_, new) in raised.items()}}, sort_keys=True)),
    )
    for model, (old, new) in raised.items():
        schema.add_event(db, now, SET_GRANT, model=model, old_micro=old, new_micro=new, reason=reason, actor=actor)
    return log(db)[-len(raised) :]


def set_in_file(
    path: Path, wanted: Mapping[str, int], reason: str, actor: str, *, clock: Callable[[], float] = time.time
) -> list[GrantRaise]:
    """Raise grants in the ledger file at `path` under the dispatcher's own lock, in one transaction (the module's
    docstring). Refused (`GrantRefused`, nothing written) while a controller holds the ledger."""
    if not path.is_file():
        raise FileNotFoundError(f"{path}: no ledger there")
    lock = DispatchLock(path.parent)
    try:
        lock.acquire()
    except CampaignLocked as error:
        raise GrantRefused(
            f"a controller holds this ledger ({error}): one on a core that predates this change would ignore a raised "
            "grant and keep issuing at the pinned one, and none can be told from one that reads it. Stop the "
            "controller, raise the grants, and start it again on this commit or later"
        ) from None
    try:
        db = schema.connect(path)
        try:
            row = db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
            if row is None:
                raise ValueError(f"{path} holds no ledger configuration")
            config = LedgerConfig.from_json(row[0])
            with schema.transaction(db):
                return set_grants(db, config, clock(), wanted, reason, actor)
        finally:
            db.close()
    finally:
        lock.release()
