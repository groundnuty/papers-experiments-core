# Provenance: new
"""What the elastic controller reads (#462 step 5, C43): each site's declared settings (`SiteSettings`),
one read of a site and its fleet's members (`SiteView`, `Member`), and the ledger's view (`LedgerView`, `ledger_view`),
from the ledger the controller drives or from a read-only view of it that takes no lock (`read_only`, for a dry run
beside the process that drives the ledger).
"""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from expcore.campaign.elastic.records import LIVE, Demand, Federation
from expcore.campaign.elastic.views import LedgerReader, LedgerViews

RE_UP_S = 210.0 + 180.0 + 375.0  # an up (median about 3.5 min), a down (about 3 min) and an ingest (375 s), measured
STALL_S = 900.0
COLLECT_S = 60.0
MARGIN_S = 300.0
WAITING = ("PENDING", "CONFIGURING", "REQUEUED", "RESIZING")  # `hpcslurm.WAITING_STATES`
GONE = "GONE"  # Slurm no longer knows the job, or its id now names another job: it ended
BROKEN = "BROKEN"  # some of a member's jobs ended or are ending and some are not: not proven ended, not usable
# The states that prove a job ended (`hpcslurm.ENDED_STATES`, and GONE): a member is ended only when every job is
ENDED = ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY", "DEADLINE", "PREEMPTED",
         "BOOT_FAIL", GONE)  # fmt: skip
HOLDS_NOTHING = ("taken", "down")  # members that can hold no job (`fleetrecord`; a claimed one is an up in flight)
ENDING_EVENTS = ("report", "return", "cancel", "expire", "lost")  # a lease's events after which its federation is free
RELAY_MB = 40.0  # a fault relay's share of the login node's memory: about 28 MB measured idle, with margin
UP_MB = 256.0  # an up in flight on the login node (declared, not measured)
VERIFY_MB = 1024.0  # a verify-data in flight (declared: one at about 3.9 GB was killed before it streamed)
LOGIN_RESERVE_MB = 512.0  # the rest of what the account runs there: the controller, the dispatcher's ssh sessions
RELAY_STATES = ("none", "here", "elsewhere", "gone")  # no relay; beating here; beating on another login node; gone
PREPARE_STATES = ("running", "ok", "failed")  # a member's prepare as its site's record shows it (no record: None)
NODE_ROOMS = ("idle", "queue")  # `SiteSettings.node_room`: the node limit counts idle nodes, or trusts the queue (C56)


def _positive(name: str, value: float, zero: bool = False) -> float:
    bad = isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value)
    if bad or value < 0 or (value == 0 and not zero):
        raise ValueError(f"{name} must be a {'non-negative' if zero else 'positive'} finite number, got {value!r}")
    return float(value)


def _count(name: str, value: int, least: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < least:
        raise ValueError(f"{name} must be a whole number of at least {least}, got {value!r}")
    return value


@dataclass(frozen=True)
class SiteSettings:
    """What a site declares: its operator's label (never a host or a site's name), its maximum of federations, its
    billing allowance and rate (declared: no code reads a grant), KillWait, ups at once, the warm pool kept idle, the
    idle time past which a federation retires, the queue wait that stalls the site, the drain's lease bound (None:
    the ledger's hard term), collect time and margin, and the login node's memory budget: the account's cap there
    (None: not declared, no budget), each fault relay's share, a reserve for each up and each verify-data in flight,
    and one for everything else the account runs there; `login_node` names the login node the site's commands run on
    (sites that name one share one budget: one cgroup), None for one of its own; `prepare_timeout_s` bounds the
    site's prepare command (the study's ingest and verify-data of a member that has come up: the deploy side holds the
    command), None when the site has none. A prepare in flight takes `verify_mb`: it is the verify-data in flight the
    budget already counts for every member that is up, linked and not yet verified. `node_room` (C56) is how the site's
    node figures limit its room: `idle` (the default) counts the free whole nodes, as an `--exclusive` site needs;
    `queue` is for a site whose jobs share nodes (`HPC_EXCLUSIVE_NODES` 0): Slurm starts them on mixed nodes, so no
    node count is a capacity and none binds, and one up waiting in the queue at a time stands in its place."""

    label: str
    max_federations: int
    allowance_units: float
    rate_units_per_hour: float
    kill_wait_s: float = 180.0
    parallel_ups: int = 1
    warm_pool: int = 0
    retire_idle_s: float = RE_UP_S
    queue_stall_s: float = STALL_S
    lease_bound_s: float | None = None
    collect_s: float = COLLECT_S
    margin_s: float = MARGIN_S
    login_memory_mb: float | None = None
    relay_mb: float = RELAY_MB
    up_mb: float = UP_MB
    verify_mb: float = VERIFY_MB
    login_reserve_mb: float = LOGIN_RESERVE_MB
    login_node: str | None = None
    prepare_timeout_s: float | None = None
    node_room: str = "idle"

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label or len(self.label) > 40:
            raise ValueError("a site's label must be 1 to 40 characters")
        if not all(c.isdigit() or ("a" <= c <= "z") or c == "-" for c in self.label) or self.label[0] == "-":
            raise ValueError(f"site label {self.label!r} must be [a-z0-9-], not starting with '-'")
        _count("max_federations", self.max_federations, 0)
        _count("parallel_ups", self.parallel_ups, 1)
        _count("warm_pool", self.warm_pool, 0)
        _positive("allowance_units", self.allowance_units, zero=True)
        _positive("rate_units_per_hour", self.rate_units_per_hour)
        for name in ("kill_wait_s", "retire_idle_s", "queue_stall_s", "collect_s", "margin_s"):
            _positive(name, getattr(self, name), zero=True)
        if self.lease_bound_s is not None:
            _positive("lease_bound_s", self.lease_bound_s)
        if self.login_memory_mb is not None:
            _positive("login_memory_mb", self.login_memory_mb)
        for name in ("relay_mb", "up_mb", "verify_mb", "login_reserve_mb"):
            _positive(name, getattr(self, name), zero=True)
        if self.login_node is not None and (not isinstance(self.login_node, str) or not self.login_node):
            raise ValueError("login_node must be a non-empty operator label")
        if self.prepare_timeout_s is not None:
            _positive("prepare_timeout_s", self.prepare_timeout_s)
        if not isinstance(self.node_room, str) or self.node_room not in NODE_ROOMS:
            raise ValueError(f"node_room must be one of {', '.join(NODE_ROOMS)}, got {self.node_room!r}")
        if self.relay_mb + self.up_mb <= 0:
            raise ValueError("relay_mb and up_mb cannot both be 0: a federation takes some of the login node")


@dataclass(frozen=True)
class Member:
    """One member of a site's fleet, as the site's read shows it: its fleet label and record status, its root, its
    held jobs and their state (RUNNING when all run; an ended state or `GONE` only when every one ended; `BROKEN` when
    some ended or are ending and others not; else a waiting state while any waits; None before any is submitted),
    the earliest submit, the latest start and the earliest recorded end (seconds since the epoch), whether its up
    finished (linked) and whether every ingested space is verified; and its fault relay: `none` (its up recorded none
    serving), `here` (beating, a process of this login node), `elsewhere` (beating, on another login node), `gone`
    (its up recorded it serving and no live heartbeat is left), with its last heartbeat; and its prepare, as the site's
    record of it shows: None (never started), `running` (started and not ended), `ok` (exit 0) or `failed` (a non-zero
    exit, the timeout, or aborted). `unswept` (C54): a down of it could not sweep a node of it (its state says so), so
    the node may still hold the operator's key files, and a down run again, its job gone, would remove the root without
    that sweep: only an operator takes it down."""

    label: str
    status: str
    root: str
    jobs: tuple[str, ...] = ()
    state: str | None = None
    submitted_at: float | None = None
    started_at: float | None = None
    ends_at: float | None = None
    linked: bool = False
    verified: bool = False
    relay: str = "none"
    relay_beat_at: float | None = None
    prepare: str | None = None
    unswept: bool = False

    def __post_init__(self) -> None:
        if self.relay not in RELAY_STATES:
            raise ValueError(f"a member's relay is one of {', '.join(RELAY_STATES)}, not {self.relay!r}")
        if self.prepare is not None and self.prepare not in PREPARE_STATES:
            raise ValueError(f"a member's prepare is one of {', '.join(PREPARE_STATES)} or none, not {self.prepare!r}")

    @property
    def waiting(self) -> bool:
        return self.state is None or self.state in WAITING

    @property
    def ended(self) -> bool:
        """Every held job is shown ended: the end is proven."""
        return self.state in ENDED

    @property
    def broken(self) -> bool:
        """Neither waiting, running nor proven ended (a job ended beside one that still runs, one still ending, or a
        state not read): unusable, and its end not proven."""
        return self.state is not None and not self.waiting and self.state != "RUNNING" and not self.ended


@dataclass(frozen=True)
class SiteView:
    """One read of a site: `error` names why it could not be read (the site is dark), else its free whole nodes (idle)
    and mixed ones (reported only), a federation's nodes, `--time` and inode estimate, the account's inode usage and
    quota (None: nothing on scratch), why the quota could not be read, and its fleet's members."""

    label: str
    error: str | None = None
    idle_nodes: int = 0
    mixed_nodes: int = 0
    nodes_per_federation: int = 7
    time_s: float = 0.0
    inodes_used: int | None = None
    inodes_quota: int | None = None
    inode_estimate: int = 0
    inodes_error: str | None = None
    members: tuple[Member, ...] = ()


@dataclass(frozen=True)
class LedgerView:
    """What the controller reads of the ledger: the current set's demand, the caps, the budget (grants and what
    `max_usd` and each ceiling leave, in micro-dollars; `left_micro` None: no budget), the leases in flight, the
    federations by label, the state of each federation's lease holding a slot, since when each holds no lease, and the
    hard term."""

    demand: Demand
    caps: Mapping[str, int]
    grants_micro: Mapping[str, int]
    left_micro: int | None
    left_per_model_micro: Mapping[str, int]
    in_flight: int
    federations: Mapping[str, Federation]
    holding: Mapping[str, str]
    idle_since: Mapping[str, float]
    hard_term_s: float
    lost: tuple[tuple[int, str, str], ...] = ()  # (lease id, federation, state) of each lost lease not yet settled
    money: Mapping[str, object] = field(default_factory=dict)  # the ledger's `status()`, for the log and the status
    reopenable: int = 0  # runs exhausted by a loss's conservative charge whose lost lease a settle may still reopen

    def leases_wanted(self) -> int:
        """How many more leases the ledger could issue now: each model of the current set below its cap with runs
        pending, as many as its budget still grants, the models taken in name order against the shared total."""
        left = self.left_micro
        wanted = 0
        for model in sorted(self.demand.pending):
            cap = self.caps.get(model)
            room = self.demand.pending[model]
            if cap is not None:
                room = min(room, cap - self.demand.slots.get(model, 0))
            grant = self.grants_micro.get(model, 0)
            if left is not None and grant > 0:
                ceiling = self.left_per_model_micro.get(model)
                room = min(room, left // grant, room if ceiling is None else ceiling // grant)
                room = max(room, 0)
                left -= room * grant
            wanted += max(room, 0)
        return wanted


def environment_digest(identity: Mapping[str, str]) -> str:
    """The environment a federation joins the ledger with: the SHA-256 of its environment identity's canonical JSON
    (keys sorted, no spaces), the identity a lease store pins, as the merge by run (step 4) computes it to check every
    store against the ledger's pinned environment."""
    if not isinstance(identity, Mapping) or not identity:
        raise ValueError("an environment identity is a non-empty mapping of names to values")
    text = json.dumps(dict(identity), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ledger_view(views: LedgerViews) -> LedgerView:
    """What the controller reads of a ledger: `demand`, the caps in force (`caps()`), the grants of its configuration,
    `status()` (what the ceilings in force leave, and what unsettled leases lock), `federations`, `leases` (which lease
    holds each federation's slot, how many are in flight, which are lost), `settlements` and `events` (since when each
    federation holds no lease: its join, or the last event that freed it)."""
    config = views.config
    demand = views.demand()
    leases = views.leases()
    status = views.status()
    federations = {federation.label: federation for federation in views.federations()}
    owner = {lease.lease_id: lease.federation for lease in leases}
    idle = {label: federation.joined_at for label, federation in federations.items()}
    for event in views.events():
        label = owner.get(event["lease_id"])
        if event["kind"] in ENDING_EVENTS and label is not None:
            idle[label] = max(idle[label], event["at"])
    settled = {settlement.lease_id for settlement in views.settlements()}
    lost = tuple(
        (lease.lease_id, lease.federation, lease.state)
        for lease in leases
        if lease.lease_id not in settled and (lease.state == "lost" or (lease.state == "expired" and lease.proven))
    )
    # An exhausted run is not final while a lost lease of it is unsettled: a settle that shows fewer attempts reopens
    # it. Only a ledger with a budget settles, so only there may such a run come back.
    exhausted = {run.run_key for run in views.runs() if run.state == "infra_exhausted"}
    lost_ids = {lease_id for lease_id, _, _ in lost}
    reopenable = len({lease.run_key for lease in leases if lease.lease_id in lost_ids and lease.run_key in exhausted})
    money = {"ceiling_micro": status.ceiling_micro, "committed_micro": status.committed_micro,
             "remaining_micro": status.remaining_micro, "locked_micro": status.locked_micro,
             "locked_by_state": dict(status.locked_by_state)}  # fmt: skip
    return LedgerView(
        demand=demand,
        caps=views.caps(),
        grants_micro={model: config.grant_micro(model) for model in demand.pending},
        left_micro=status.remaining_micro,
        left_per_model_micro=dict(status.model_remaining_micro),
        in_flight=sum(1 for lease in leases if lease.state in LIVE),
        federations=federations,
        holding={lease.federation: lease.state for lease in leases if lease.slot},
        idle_since=idle,
        hard_term_s=config.hard_term_s,
        lost=lost,
        money=money,
        reopenable=reopenable if status.remaining_micro is not None else 0,
    )


def read_only(path: Path) -> LedgerReader:
    """The ledger at `path`, read without its lock (`LedgerReader`), for a dry run beside the process that drives it."""
    return LedgerReader(path)
