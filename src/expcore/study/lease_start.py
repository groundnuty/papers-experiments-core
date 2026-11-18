# Provenance: new
"""The start of a run taken on a lease (C41, step 2 of #462): its one run, its grant, and every refusal first.

`run STUDY --lease FILE` runs the one run a lease names (`campaign.run_lease`) in a store of its own. Everything here
is decided before the store is opened, from the study and the hash of the campaign's fingerprint, so a refused lease
leaves no store behind:

- the campaign: the lease was issued for the SHA-256 of one fingerprint (`manifest.fingerprint_sha256`), the one
  `fingerprint_of` makes of the study here, before the form of an earlier core or a platform's earlier form is applied
  to it (a lease store is always new). A lease for another campaign is refused, naming both digests' first characters;
- the term: a lease whose expiry has come is refused (a ledger expires a lease by its own clock; this is the node's own
  check at the start, never a replacement for it);
- the run: it must be a run of the plan; the store holds it and no other, at its place in the whole plan;
- the attempt limit: the lease's charged attempts count against `[scheduler] max_attempts`, so a run that has used
  every attempt gets no lease run. The limit is the ledger's; this keeps the kernel from running past it;
- the money: **the grant is the run's whole budget.** The store's gate is `Budget(max_usd=grant)` and nothing of the
  campaign's own: no per-model ceiling and no release file or step (the ledger holds those, and a release would take
  a run past its grant). A campaign with no `[budget]` is held to the grant all the same. The per-run bound stays: the
  gate starts a run only when the grant covers what one attempt can cost (`attempt_bounds.bound_usd`), probes and
  top-ups come out of the same grant, and a grant below one run's reservation holds the run before any attempt. A
  grant of 0 is for a model that costs nothing, run with no gate as by-model shards do; a priced model is refused with
  it, since a budget of 0 holds every run. A model with no price that is not free is refused, since no grant can pay
  for it;
- the caps: one run of the model's unit. The harness-error stop and a unit's pause are derived from what this store's
  attempts did, as before; the ledger takes them across the leases.
"""

import dataclasses
import datetime as dt
from collections.abc import Mapping

from expcore.campaign.gate import Budget
from expcore.campaign.manifest import Manifest
from expcore.campaign.run_lease import LeaseError, RunLease
from expcore.campaign.settings import SchedulerConfig
from expcore.campaign.shard_budget import reservation_usd
from expcore.campaign.shard_run import ShardStart
from expcore.study.config import StudyConfig


def lease_start(
    study: StudyConfig,
    settings: SchedulerConfig,
    lease: RunLease,
    *,
    campaign_sha256: str,
    environment: Mapping[str, str],
    now: dt.datetime,
) -> tuple[Manifest, SchedulerConfig, ShardStart]:
    """A lease's one-run manifest, its settings with the grant as the budget, and the start its store pins.

    `campaign_sha256` is the hash of this campaign's fingerprint as `fingerprint_of` made it; `now` is a time with a
    timezone. Raises LeaseError (a ShardError) for every refusal of the module's docstring, before anything is written.
    """
    lease.check_campaign(campaign_sha256)
    if lease.expired(now):
        raise LeaseError(f"{lease.name} expired at {lease.expires_utc}")
    rows = [row for row in study.manifest.rows if row.key.text == lease.run_key]
    if not rows:
        raise LeaseError(f"{lease.name}: its run is not a run of this campaign's plan")
    lease.check_attempts_left(settings.max_attempts)
    model = str(rows[0].key.factors["model"])
    try:
        reserved = reservation_usd(study.models.resolve(model), study.campaign.agent.limits)
    except ValueError as error:  # a priced model with no context window: nothing bounds its runs
        raise LeaseError(f"model {model}: {error}") from None
    if reserved is None:
        raise LeaseError(
            f"model {model} has no price and is not marked free, so what its run reserves is unknown and no grant "
            "can pay for it; give it a price, or mark it free"
        )
    if reserved > 0 and lease.grant_micro_usd == 0:
        raise LeaseError(
            f"{lease.name} grants 0 USD, and a run of model {model} reserves {reserved:.6f} USD: a budget of 0 holds "
            "every run, so a priced model needs a grant above 0"
        )
    caps = {study.campaign.endpoints[model].unit: 1}
    budget = Budget(max_usd=lease.grant_usd) if lease.grant_micro_usd > 0 else None
    shares = {
        "unit_caps": caps,
        "budget": None if budget is None else {"max_usd": lease.grant_usd, "per_model_usd": {}},
    }
    run = dataclasses.replace(settings, unit_caps=caps, budget=budget)
    manifest = Manifest(study.manifest.order_seed, (rows[0],))
    return manifest, run, ShardStart(lease, dict(environment), shares)


__all__ = ["lease_start"]
