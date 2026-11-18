# Provenance: new
"""The merge of an elastic campaign by run (#462 step 4, C45): the checks, merge.json `expcore-merge/2`, the layout.

`python -m expcore merge OUT STORE_OUT... --ledger LEDGER` takes the campaign's ledger and the `out/` of lease stores
(`expcore-shard/5`, one run each, `campaign.run_lease`) and writes one batch under `OUT`:

    OUT/runs/                                     the records of each run's counted lease, as one batch
    OUT/leases/<lease id>/campaign-snapshot.sqlite the snapshot of each counted lease's store
    OUT/evidence/<lease id>/campaign-snapshot.sqlite the snapshot of every other lease store given (never analysed)
    OUT/ledger-snapshot.sqlite                    the ledger, as the merge read it (`campaign.ledger_read`)
    OUT/merge.json                                `expcore-merge/2`: what was merged, and each snapshot's SHA-256

**What a merge by run refuses, naming the lease or the run and the reason** (`check_by_run`):

1. a ledger whose tables are not what its events give (`ledger_read`), or whose runs are not the study's plan;
2. two stores of one lease; a store of a lease the ledger never issued, or of a cancelled or live lease; a store
   whose pinned lease names another run, another campaign or another grant than the ledger's; a store that holds any
   run but its own;
3. stores of another campaign (the fingerprint, the core first of all, `merge.check_fingerprints`) or another
   environment (`merge.check_environment`), or than the one the ledger pinned at its first join (`environment_digest`
   of the stores' identity); a campaign whose fingerprint is not the one the ledger was made for;
4. **not exactly one counted lease per run:** a run of the plan that is not terminal in the ledger (it has no counted
   lease yet; a run the ledger ended as infra_exhausted for its lost leases has none, and is listed as an exhausted
   run once none of its leases is unsettled, see 6), a run whose counted lease's store is not given (its archive is
   the run's only record), a counted store whose run ended otherwise than the ledger says; a store of a counted or
   superseded lease that is not given (the ledger verified its archive, so it is evidence to keep);
5. spend: every lease is counted at what the ledger charges it (its measured spend once its completion verified,
   the spend a settlement gave a lost lease, else its grant), and the leases together must stay within the ceilings
   in force (`max_usd` and each model's, as pinned or raised); a store given must agree with its lease's charge: the
   settled spend exactly (and the settled charged attempts), else no more than the charge, never above its grant, and
   never unknown under a budget;
6. a run the ledger holds as `infra_exhausted` while any of its leases is still charged its grant unsettled (in
   flight, expired, reported or lost): a settle, a return or a late completion can show fewer charged attempts and
   reopen it (`elastic.attempts`), so it is not final.

Then each counted store passes the replay check (`study.merge_by_run`), and its records are exactly those of its
attempts (`require_records`): none it does not account for, and the record of every attempt it holds as done.
Today's checks hold for every store given, so a store of another campaign or environment is refused even when it is
only evidence.

**Evidence.** A lease store that is not its run's counted lease is kept under `evidence/` and in merge.json's
`evidence`, with why (`reason`): `superseded` (its completion came from a lease that was no longer the run's latest),
`earlier` (it counted, and its run went back to pending and was leased again), `lost` (its archive never verified),
`expired` or `returned`. Its spend is counted; its records are never in `runs/`, never checked as the batch, never
analysed. A store of a lost or expired lease that still holds a running attempt (its federation was lost mid-run) is
settled by `recover` in the merge's private copy as an infrastructure loss (`study.merge_by_run`), its running attempt
read at its bound (`study.merge_by_run.dead_store_spend` is the same read, for a controller that settles the lease
in the ledger). A lost or expired lease with no store given is counted at what the ledger charges it.

**merge.json** (`by_run_layout.MERGE_BY_RUN_SCHEMA`, written and read back by `campaign.merged_by_run`) holds
`fingerprint`, `rule`, `environment_identity`, `ledger_sha256`, `runs` (how many the plan holds), `leases` (each
counted lease in plan order: `lease_id`, `run_key`, `federation`, `site`, `started_utc`, `snapshot_sha256`,
`spend_usd`), `evidence` (in lease order: the same and `reason`; `started_utc` and `snapshot_sha256` None with no
store) and `spend`
(`total_usd`, `per_model_usd`, `max_usd`, `per_model_ceiling_usd`). Federations and sites appear as the operator's
labels the ledger holds. The check and the analysis rebuild it from the snapshots, the ledger snapshot and the study's
plan through these checks, and refuse a file that differs.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from expcore.campaign.elastic.config import micro_up, usd_of
from expcore.campaign.elastic.records import HELD
from expcore.campaign.lease_facts import LeaseStore
from expcore.campaign.ledger_read import TERMINAL, LeaseRow, LedgerFacts, RunRow
from expcore.campaign.manifest import Manifest, fingerprint_sha256
from expcore.campaign.merge import (
    MergeRefused,
    check_environment,
    check_fingerprints,
    sha256_of,
)
from expcore.campaign.selection_ledger import Pricing

_EVIDENCE_REASONS = {"counted": "earlier", "superseded": "superseded", "lost": "lost", "expired": "expired"}
_EVIDENCE_REASONS |= {"returned": "returned", "reported": "superseded"}
_KEPT = ("counted", "superseded")  # a lease whose archive the ledger verified: its store is evidence to keep


# --- the checks ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """A lease that is not its run's counted lease, and its store when one was given."""

    lease: LeaseRow
    reason: str
    store: LeaseStore | None


@dataclass(frozen=True)
class LostRun:
    """A run the ledger ended as infra_exhausted for its lost leases, with no counted lease and so no store: every one
    of its leases (none of them unsettled) and the run as the ledger holds it. It is listed as exhausted, never
    analysed; its placement is its last lease's."""

    run: RunRow
    leases: tuple[LeaseRow, ...]

    @property
    def last(self) -> LeaseRow:
        return self.leases[-1]


@dataclass(frozen=True)
class ByRun:
    """A merge by run that passed the checks: each run's counted store in plan order, the evidence, and the runs the
    ledger exhausted for their lost leases, in plan order."""

    counted: tuple[LeaseStore, ...]
    evidence: tuple[Evidence, ...]
    ledger: LedgerFacts
    lost: tuple[LostRun, ...] = ()


def check_by_run(ledger: LedgerFacts, stores: Sequence[LeaseStore], plan: Manifest, pricing: Pricing | None) -> ByRun:
    """Run checks 1 to 6 of the module docstring; raise MergeRefused for the first fault."""
    refuse_reopenable(ledger)
    refuse_contradicted(ledger)
    if not stores:
        raise MergeRefused("a merge by run takes the store of every run's counted lease; none was given")
    by_lease = _by_lease(stores)
    for store in stores:
        _check_binding(store, ledger)
    ordered = sorted(stores, key=lambda store: store.lease.lease_id)
    check_fingerprints(ordered)
    if fingerprint_sha256(ordered[0].fingerprint) != ledger.config.fingerprint:
        raise MergeRefused(
            f"{ordered[0].name}: ran the campaign with fingerprint sha256 "
            f"{fingerprint_sha256(ordered[0].fingerprint)[:12]}, and the ledger is the one of "
            f"{ledger.config.fingerprint[:12]}"
        )
    check_environment(ordered)
    _check_ledger_environment(ordered[0], ledger)
    counted, lost = _counted(ledger, by_lease, plan)
    evidence = _evidence(ledger, by_lease, {store.lease.lease_id for store in counted})
    for store in ordered:
        _check_store_spend(store, ledger)
    _check_spend(ledger, counted, evidence)
    return ByRun(tuple(counted), tuple(evidence), ledger, tuple(lost))


def refuse_contradicted(ledger: LedgerFacts) -> None:
    """5: refuse a ledger that recorded a lease's settled or granted charge as below what later evidence showed it spent
    (`settle_contradicted`, `cap_failed`): the ledger keeps the lower charge, so the lease would be counted below its
    spend."""
    if ledger.contradicted:
        lease = min(ledger.contradicted)
        raise MergeRefused(
            f"the ledger recorded that lease {lease} spent more than it is charged (a settlement contradicted, or a "
            "cap that failed): its charge is below its spend, so the campaign's spend cannot be counted from it"
        )


def refuse_reopenable(ledger: LedgerFacts) -> None:
    """6: refuse a ledger in which a run is `infra_exhausted` while one of its leases is still charged its grant with
    no settlement. The ledger charges a lost lease one attempt before its real count is known, and a settle, a return
    or a late completion that shows fewer charged attempts reopens the run (`elastic.attempts`), so such a run is not
    final: it is exhausted only once every one of its leases is settled, counted or otherwise ended."""
    for key, run in ledger.runs.items():
        unsettled = ledger.unsettled(key) if run.state == "infra_exhausted" else []
        if unsettled:
            raise MergeRefused(
                f"run {key} is infra_exhausted in the ledger, but its lease {unsettled[0].lease_id} is "
                f"{unsettled[0].state} and not settled: a settle, a return or a late completion can show fewer charged "
                "attempts and reopen the run, so it is not final; settle its leases and merge again"
            )


def environment_digest(identity: Mapping[str, str]) -> str:
    """The environment a federation joins the ledger with (`elastic.Ledger.join`): the SHA-256 of its environment
    identity's canonical JSON, the identity a lease store pins (`META_ENVIRONMENT`)."""
    text = json.dumps(dict(identity), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_ledger_environment(store: LeaseStore, ledger: LedgerFacts) -> None:
    """3: the stores' environment identity is the one the ledger pinned at its first join."""
    if ledger.environment != environment_digest(store.environment):
        raise MergeRefused(
            f"{store.name}: ran against an environment whose identity has digest "
            f"{environment_digest(store.environment)[:12]}, and the ledger pinned "
            f"{str(ledger.environment)[:12]} at its first join: another environment than the ledger's"
        )


def require_records(stores: Sequence[LeaseStore], records: Mapping[str, int | None]) -> None:
    """The records of a batch merged by run (by path relative to its `runs/`, each with the lease id it names, None
    for a record that names none) are exactly the counted stores': each lies where an attempt of one of them lies and
    names that store's lease when it names one, and every attempt a store holds as done has its record. Attempt
    numbers can repeat across a run's leases, so a superseded or lost lease's record can lie where the counted one's
    belongs: the lease id it names tells it apart. A record of another lease, or one a store does not account for, is
    refused, and so is a counted run without its record."""
    expected = {rel: state for store in stores for rel, state in store.attempts.items()}
    owner = {rel: store for store in stores for rel in store.attempts}
    found = set(records)
    stray = sorted(found - set(expected))
    if stray:
        raise MergeRefused(f"{stray[0]}: a record no attempt of a counted lease's store accounts for")
    foreign = sorted(rel for rel in found if records[rel] != owner[rel].lease.lease_id)
    if foreign:
        named = "no lease" if records[foreign[0]] is None else f"lease {records[foreign[0]]}"
        raise MergeRefused(f"{foreign[0]}: a record naming {named}, where {owner[foreign[0]].name} keeps its own")
    missing = sorted(rel for rel, state in expected.items() if state == "done" and rel not in found)
    if missing:
        raise MergeRefused(f"{missing[0]}: the record of a counted lease's final attempt is missing")


def _by_lease(stores: Sequence[LeaseStore]) -> dict[int, LeaseStore]:
    held: dict[int, LeaseStore] = {}
    for store in stores:
        if store.lease.lease_id in held:
            raise MergeRefused(f"{store.name} is given twice: give each lease's store once")
        held[store.lease.lease_id] = store
    return held


def _check_binding(store: LeaseStore, ledger: LedgerFacts) -> None:
    """2: the store's pinned lease is one the ledger issued, for this run, campaign and grant, and it holds its run."""
    row = ledger.leases.get(store.lease.lease_id)
    if row is None:
        raise MergeRefused(f"{store.name}: the ledger never issued lease {store.lease.lease_id}")
    if row.run_key != store.lease.run_key:
        raise MergeRefused(f"{store.name}: the ledger issued lease {row.lease_id} for run {row.run_key}")
    if store.lease.campaign_sha256 != ledger.config.fingerprint:
        raise MergeRefused(f"{store.name}: its lease was issued for another campaign than the ledger's")
    if store.lease.grant_micro_usd != row.grant_micro:
        raise MergeRefused(
            f"{store.name}: its lease grants {store.lease.grant_micro_usd} micro-dollars, the ledger {row.grant_micro}"
        )
    if row.state in ("cancelled", "issued", "sent"):
        raise MergeRefused(f"{store.name}: the ledger holds lease {row.lease_id} as {row.state}, so no store is its")
    if set(store.runs) != {store.lease.run_key}:
        others = sorted(set(store.runs) - {store.lease.run_key})
        raise MergeRefused(f"{store.name}: holds {others[0] if others else 'no run'}, not exactly its own run")


def _counted(
    ledger: LedgerFacts, by_lease: Mapping[int, LeaseStore], plan: Manifest
) -> tuple[list[LeaseStore], list[LostRun]]:
    """1 and 4: the ledger's runs are the plan's, each terminal, and its counted lease's store is given and agrees; a
    run the ledger exhausted for its lost leases, every lease of it settled or ended (`refuse_reopenable`), has none
    and is returned apart, to be listed as exhausted."""
    planned = {row.key.text: row.seq for row in plan.rows}
    held = {key: run.seq for key, run in ledger.runs.items()}
    if held != planned:
        extra = sorted(set(held) - set(planned)) or sorted(key for key in held if held[key] != planned.get(key))
        missing = sorted(set(planned) - set(held))
        named = f"run {extra[0]} is not the plan's" if extra else f"run {missing[0]} of the plan is not in it"
        raise MergeRefused(f"the ledger's runs are not the study's plan: {named}")
    _check_models(ledger, plan)
    counted, lost = [], []
    for key in sorted(planned, key=planned.__getitem__):
        run = ledger.runs[key]
        lease_id = ledger.counted(key)
        if lease_id is None and run.state == "infra_exhausted":
            if run.charged < ledger.config.max_attempts:
                raise MergeRefused(
                    f"run {key} is infra_exhausted in the ledger with {run.charged} charged attempts, below its "
                    f"{ledger.config.max_attempts}, and no counted lease: its exhaustion is not what its leases give"
                )
            lost.append(LostRun(run, tuple(r for r in ledger.leases_of(key) if r.state != "cancelled")))
            continue
        if lease_id is None and run.state in TERMINAL:
            raise MergeRefused(
                f"run {key} is {run.state} in the ledger with no counted lease (its latest, lease {run.lease_id}, is "
                f"{ledger.leases[run.lease_id].state if run.lease_id is not None else 'none'})"
            )
        if lease_id is None:
            raise MergeRefused(
                f"run {key} is {run.state} in the ledger: a campaign is merged by run once every run has its "
                "counted lease"
            )
        store = by_lease.get(lease_id)
        if store is None:
            raise MergeRefused(
                f"run {key}: the store of its counted lease {lease_id} is not given; its records are the run's only "
                "ones, so the merge cannot go on without them"
            )
        if store.status.get(key) != run.state:
            raise MergeRefused(
                f"{store.name}: its run is {store.status.get(key)} in its store and {run.state} in the ledger"
            )
        counted.append(store)
    return counted, lost


def _check_models(ledger: LedgerFacts, plan: Manifest) -> None:
    """1: each ledger run, and each lease of it, names the model the plan's run has, so spend is held to its own
    model's ceiling."""
    models = {row.key.text: str(row.key.factors["model"]) for row in plan.rows}
    for key, run in ledger.runs.items():
        if run.model != models[key]:
            raise MergeRefused(f"run {key} is of model {models[key]} in the plan and {run.model} in the ledger")
    for lease in ledger.leases.values():
        if lease.model != models[lease.run_key]:
            raise MergeRefused(
                f"lease {lease.lease_id} names model {lease.model}, and its run is of {models[lease.run_key]}"
            )


def _evidence(ledger: LedgerFacts, by_lease: Mapping[int, LeaseStore], counted: set[int]) -> list[Evidence]:
    """Every lease but the counted ones and the cancelled, with its reason; a kept lease's store must be given."""
    found = []
    for lease in sorted(ledger.leases.values(), key=lambda row: row.lease_id):
        if lease.lease_id in counted or lease.state == "cancelled":
            continue
        reason = _EVIDENCE_REASONS.get(lease.state)
        if reason is None or (lease.state == "reported" and lease.verdict != "superseded"):
            raise MergeRefused(f"lease {lease.lease_id} of run {lease.run_key} is {lease.state} in the ledger")
        store = by_lease.get(lease.lease_id)
        if store is None and lease.state in _KEPT:
            raise MergeRefused(
                f"lease {lease.lease_id} of run {lease.run_key} is {lease.state} in the ledger, which verified its "
                "archive, and its store is not given: give it, as the evidence it is"
            )
        found.append(Evidence(lease, reason, store))
    return found


def _check_spend(ledger: LedgerFacts, counted: Sequence[LeaseStore], evidence: Sequence[Evidence]) -> None:
    """5: every lease's spend within its grant, and together within the ceilings in force (the ledger's `max_usd` and
    each model's, as pinned or as raised since)."""
    totals = spend_micro(ledger, counted, evidence)
    ceilings = ledger.ceilings
    if ceilings.max_micro is None:
        return
    if totals[None] > ceilings.max_micro:
        raise MergeRefused(
            f"the leases spent USD {usd_of(totals[None])} together, above the campaign's ceiling of USD "
            f"{usd_of(ceilings.max_micro)}"
        )
    for model, ceiling in sorted(ceilings.per_model_micro.items()):
        if totals.get(model, 0) > ceiling:
            raise MergeRefused(
                f"the leases of model {model} spent USD {usd_of(totals[model])} together, above its ceiling of USD "
                f"{usd_of(ceiling)}"
            )


def spend_micro(
    ledger: LedgerFacts, counted: Sequence[LeaseStore], evidence: Sequence[Evidence]
) -> dict[str | None, int | None]:
    """Every lease's spend in micro-dollars as the ledger charges it, summed in total (under None) and per model: its
    measured spend once its completion verified, the spend a settlement gave it, else its grant. The ledger's money is
    the one count; a store given is evidence held to it (`_check_store_spend`)."""
    rows = [ledger.leases[store.lease.lease_id] for store in counted] + [item.lease for item in evidence]
    unbounded = ledger.ceilings.max_micro is None  # no budget: a grant is 0, no bound on what an unsettled lease spent
    totals: dict[str | None, int | None] = {None: 0}
    for row in rows:
        unknown = unbounded and row.state in HELD and row.lease_id not in ledger.settlements
        for scope in (None, row.model):
            have = totals.get(scope, 0)
            totals[scope] = None if have is None or unknown else have + row.charge_micro
    return totals


def _check_store_spend(store: LeaseStore, ledger: LedgerFacts) -> None:
    """5: a store's own spend agrees with what the ledger charges its lease: exactly what a settlement gave it (and the
    charged attempts it gave, when it gave them), else no more than the ledger charges. A spend unknown under a budget
    is refused."""
    row = ledger.leases[store.lease.lease_id]
    micro = store_micro(store, row)
    if micro is None:
        if ledger.ceilings.max_micro is not None:
            raise MergeRefused(f"{store.name}: its spend is unknown, and the campaign has a budget")
        return
    settled = ledger.settlements.get(row.lease_id)
    if settled is not None and not store.complete:
        raise MergeRefused(
            f"{store.name}: the ledger settled its lease at USD {usd_of(settled.spend_micro)}, but its store's run had "
            "not ended, so it cannot show all its lease spent and no settled figure from it is safe"
        )
    if settled is not None and micro != settled.spend_micro:
        raise MergeRefused(
            f"{store.name}: the ledger settled its lease at USD {usd_of(settled.spend_micro)}, and its store reads USD "
            f"{usd_of(micro)}: the settled value is not this store's"
        )
    if settled is not None and settled.charged is not None and settled.charged != store.charged:
        raise MergeRefused(
            f"{store.name}: the ledger settled its lease with {settled.charged} charged attempts, and its store holds "
            f"{store.charged}"
        )
    if micro > row.charge_micro:
        raise MergeRefused(
            f"{store.name}: its store spent USD {usd_of(micro)}, more than the ledger charges its lease "
            f"(USD {usd_of(row.charge_micro)})"
        )


def store_micro(store: LeaseStore, row: LeaseRow) -> int | None:
    """A store's spend in micro-dollars; refused above its grant, when the grant held it (a grant of 0 has no gate)."""
    spent = None if store.spend is None else store.spend.total
    if spent is None:
        return None
    micro = micro_up(spent)
    if row.grant_micro and micro > row.grant_micro:
        raise MergeRefused(f"{store.name}: spent USD {spent}, above its grant of USD {usd_of(row.grant_micro)}")
    return micro


__all__ = ["LeaseStore", "MergeRefused", "sha256_of"]
