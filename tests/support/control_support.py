"""Helpers for the elastic controller's tests: simulated sites (their fleet members, nodes, inodes and allowance) and a
ledger's view, built by hand so each test says only what it changes."""

import dataclasses
from collections.abc import Mapping, Sequence

from expcore.campaign.elastic import Demand, Federation
from expcore.campaign.elastic.control import LedgerView, Member, SiteSettings, SiteView

NOW = 1_000_000.0
HOUR = 3600.0
SITES = ("site-a", "site-b")


def settings(label: str = "site-a", **changes) -> SiteSettings:
    """A site allowing 7 federations, billing 47 units an hour against a very high allowance, bringing 4 up at once."""
    fields = {
        "label": label,
        "max_federations": 7,
        "allowance_units": 1e12,
        "rate_units_per_hour": 47.0,
        "kill_wait_s": 180.0,
        "parallel_ups": 4,
        "lease_bound_s": 1800.0,
    }
    fields.update(changes)
    return SiteSettings(**fields)


def member(label: str, state: str | None = "RUNNING", status: str = "up", **changes) -> Member:
    """A member that is up, linked and verified, its jobs running since an hour ago and ending in 10 hours."""
    fields = {
        "label": label,
        "status": status,
        "root": f"/fleet/{label}/root",
        "jobs": (f"j-{label}",),
        "state": state,
        "submitted_at": NOW - HOUR - 40.0,
        "started_at": NOW - HOUR if state not in (None, "PENDING") else None,
        "ends_at": NOW + 10 * HOUR,
        "linked": status == "up",
        "verified": status == "up",
    }
    fields.update(changes)
    return Member(**fields)


def site(label: str = "site-a", members: Sequence[Member] = (), **changes) -> SiteView:
    """A readable site with 70 idle whole nodes (10 federations' worth), inodes for 6 more federations, a 7-node
    federation of 17,650 files and a 12 h `--time`."""
    fields = {
        "label": label,
        "error": None,
        "idle_nodes": 70,
        "mixed_nodes": 300,
        "nodes_per_federation": 7,
        "time_s": 12 * HOUR,
        "inodes_used": 826_221,
        "inodes_quota": 1_000_000,
        "inode_estimate": 17_650,
        "members": tuple(members),
    }
    fields.update(changes)
    return SiteView(**fields)


def joined(label: str, site_label: str = "site-a", end_in_s: float = 10 * HOUR, **changes) -> Federation:
    """The ledger's record of a federation that joined an hour ago."""
    fields = {
        "label": f"{site_label}-{label}",
        "site": site_label,
        "job_ids": (f"j-{label}",),
        "job_end_at": NOW + end_in_s,
        "kill_wait_s": 180.0,
        "joined_at": NOW - HOUR,
        "drained_at": None,
        "ended_at": None,
        "end_proof": None,
    }
    fields.update(changes)
    return Federation(**fields)


def ledger(
    pending: Mapping[str, int] | None = None,
    slots: Mapping[str, int] | None = None,
    caps: Mapping[str, int] | None = None,
    federations: Sequence[Federation] = (),
    holding: Mapping[str, str] | None = None,
    idle_since: Mapping[str, float] | None = None,
    grants: Mapping[str, int] | None = None,
    left: int | None = None,
    left_per_model: Mapping[str, int] | None = None,
    in_flight: int | None = None,
) -> LedgerView:
    """A ledger's view: the current set's pending runs and held slots per model, the caps, the federations that joined,
    the state of each one's held lease, and the budget (none by default: `left` is what `max_usd` leaves, in
    micro-dollars, `left_per_model` what each ceiling leaves, `grants` each model's grant)."""
    pending = dict(pending or {})
    slots = dict(slots or {})
    holding = dict(holding or {})
    live = sum(1 for state in holding.values() if state in ("issued", "sent"))
    return LedgerView(
        demand=Demand(set_index=0 if pending else None, pending=pending, slots=slots),
        caps=dict(caps or {}),
        grants_micro=dict(grants or {}),
        left_micro=left,
        left_per_model_micro=dict(left_per_model or {}),
        in_flight=live if in_flight is None else in_flight,
        federations={f.label: f for f in federations},
        holding=holding,
        idle_since=dict(idle_since or {}),
        hard_term_s=7200.0,
    )


def replace(view: SiteView, **changes) -> SiteView:
    return dataclasses.replace(view, **changes)
