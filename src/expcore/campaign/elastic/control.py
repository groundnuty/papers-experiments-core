# Provenance: new
"""The elastic fleet's provisioning controller (#462 step 5, C43): each site's target, and the actions that reach it.

`decide` is pure: from what the ledger shows (`LedgerView`, built by `ledger_view`), what each site's read shows
(`SiteView`, its fleet's members as `Member`) and what each site declares (`SiteSettings`), it returns the targets and
an ordered list of actions, each the call an operator would make by hand: a proven end, a drain, a join, a retirement
(collect, then `fleet.py down --member`), an add (`fleet.py add K`). The deploy side runs them, or prints them.

- **Target.** The leases the ledger could issue now are `Σ over the current set's models of min(cap - slots, pending,
  what its budget still grants)`. Free federations (joined and idle, or on their way up) serve them first; the rest come
  up at the site with the most room, one at a time, each site within `parallel_ups` per add.
- **Room at a site** is the least of: its maximum less every member that may hold a job; its free whole nodes (idle
  only: under `--exclusive` a mixed node is not free) less 7 per up whose job has not started, over 7; its inodes, the
  95% line less the usage less one whole estimate per member whose data is not yet all there (not linked
  and verified), over the estimate; its declared allowance
  less the whole `--time` of every member made, over one more whole `--time`. A stalled queue (an up waiting past
  `queue_stall_s`), an add still running there, or a quota that could not be read leave no room.
- **Drain** comes once a joined federation's recorded end is closer than the lease bound (declared; by default the
  ledger's hard term, which fails closed), collect, KillWait and 5 minutes, and never after the end.
- **Retire** only what is drained or proven ended and holds no lease in flight: collect its root evidence, then down.
  Idle federations no cap lets work are drained once idle past their re-up cost, the least walltime left first,
  keeping a declared warm pool (none by default). A failed up is taken down with no collect.
- **Proof.** A job the site shows ended is proven `terminal`; a member the fleet's record shows down (its down verified
  the job gone) is proven `cancelled`. A site that cannot be read proves nothing and gets no action.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from expcore.campaign.elastic.controlinputs import (
    GONE,
    HOLDS_NOTHING,
    LedgerView,
    Member,
    SiteSettings,
    SiteView,
    ledger_view,
    read_only,
)
from expcore.campaign.elastic.records import LIVE, Federation

LINE_PERCENT = 95  # the inode guard's line (`hpcquota.LINE_PERCENT`)
__all__ = [
    "GONE",
    "Add",
    "Decision",
    "Drain",
    "Join",
    "LedgerView",
    "Member",
    "ProveEnded",
    "Retire",
    "SiteSettings",
    "SiteView",
    "decide",
    "ledger_label",
    "ledger_view",
    "read_only",
]


@dataclass(frozen=True)
class Add:
    site: str
    count: int
    reason: str


@dataclass(frozen=True)
class Join:
    site: str
    member: str
    label: str
    job_ids: tuple[str, ...]
    job_end_at: float
    kill_wait_s: float
    queue_wait_s: float | None
    reason: str


@dataclass(frozen=True)
class Drain:
    site: str
    member: str
    label: str
    reason: str


@dataclass(frozen=True)
class Retire:
    """Collect the member's root evidence (when `collect`), copy it back, then `down --member`; `label` is its ledger
    label, None for a member that never joined."""

    site: str
    member: str
    label: str | None
    root: str
    collect: bool
    reason: str


@dataclass(frozen=True)
class ProveEnded:
    site: str
    member: str
    label: str
    proof: str
    reason: str


Action = ProveEnded | Drain | Join | Retire | Add
ORDER = (ProveEnded, Drain, Join, Retire, Add)


@dataclass
class Decision:
    """One tick's decision: per site its target, room and limits; the actions in order; each up's queue wait."""

    at: float
    wanted: int
    targets: dict[str, dict] = field(default_factory=dict)
    actions: list[Action] = field(default_factory=list)
    waits: dict[str, list[dict]] = field(default_factory=dict)


def ledger_label(site: str, member: str) -> str:
    """A member's label in the ledger: every fleet has a `fed-1`, so the site's label goes in front."""
    return f"{site}-{member}"


def cutoff_s(settings: SiteSettings, ledger: LedgerView) -> float:
    """How long before its recorded end a federation is drained."""
    bound = ledger.hard_term_s if settings.lease_bound_s is None else settings.lease_bound_s
    return bound + settings.collect_s + settings.kill_wait_s + settings.margin_s


def queue_waits(now: float, view: SiteView) -> list[dict]:
    """Each member's queue wait, submit to running: done, or still waiting (so far)."""
    found = []
    for m in view.members:
        if m.status in HOLDS_NOTHING or m.submitted_at is None:
            continue
        if m.started_at is not None:
            found.append({"member": m.label, "queue_wait_s": m.started_at - m.submitted_at, "waiting": False})
        elif m.waiting:
            found.append({"member": m.label, "queue_wait_s": now - m.submitted_at, "waiting": True})
    return found


def decide(
    now: float,
    ledger: LedgerView,
    sites: Sequence[tuple[SiteSettings, SiteView]],
    busy: frozenset[str] = frozenset(),
) -> Decision:
    """The targets and the actions of one tick. `busy` holds the sites with an add running (`site`) and the members
    with a retirement running (`site/member`)."""
    labels = [s.label for s, _ in sites]
    if len(set(labels)) != len(labels) or any(s.label != v.label for s, v in sites):
        raise ValueError("each site is given once, its settings and its read under one label")
    decision = Decision(at=now, wanted=ledger.leases_wanted())
    free = 0
    rooms: dict[str, int] = {}
    for settings, view in sites:
        site_free, room = _site(now, ledger, settings, view, busy, decision)
        free += site_free
        rooms[settings.label] = room
    need = decision.wanted - free
    if need > 0:
        _add(need, rooms, {s.label: s.parallel_ups for s, _ in sites}, decision)
    elif need < 0:
        _retire_idle(now, -need, ledger, sites, decision)
    decision.actions.sort(key=lambda action: ORDER.index(type(action)))
    return decision


def _site(
    now: float, ledger: LedgerView, settings: SiteSettings, view: SiteView, busy: frozenset[str], decision: Decision
) -> tuple[int, int]:
    """One site's actions on its members, and (its free federations, its room for more)."""
    site = settings.label
    decision.waits[site] = [] if view.error is not None else queue_waits(now, view)
    if view.error is not None:
        decision.targets[site] = {"target": 0, "available": 0, "room": 0, "dark": view.error, "limits": {}}
        return 0, 0
    free = working = 0
    for m in view.members:
        if m.status == "taken" or f"{site}/{m.label}" in busy:
            continue
        fed = ledger.federations.get(ledger_label(site, m.label))
        if fed is None:
            free += _unjoined(now, ledger, settings, m, decision.actions)
        else:
            state = _joined(now, ledger, settings, m, fed, decision.actions)
            free += state == "free"
            working += state == "working"
    room, limits = _room(now, settings, view, busy)
    available = free + working
    decision.targets[site] = {"target": min(settings.max_federations, available + room), "available": available,
                              "room": room, "limits": limits}  # fmt: skip
    return free, room


def _unjoined(now: float, ledger: LedgerView, settings: SiteSettings, m: Member, acts: list) -> int:
    """A member the ledger does not hold: join it once ready, take it down when it cannot serve; 1 while it is on its
    way (an up in flight, or its data not yet verified) or joins now."""
    site = settings.label
    if m.status == "down":
        return 0
    if m.status in ("failed", "down-failed") or m.ended:
        acts.append(Retire(site, m.label, None, m.root, False, f"never joined: {m.status}, its job {m.state}"))
        return 0
    if m.status != "up" or not m.linked or not m.verified or m.waiting:
        return 1
    if m.ends_at is None or m.ends_at - cutoff_s(settings, ledger) <= now:
        acts.append(Retire(site, m.label, None, m.root, True, "too close to its job end to take a lease"))
        return 0
    wait = None if m.started_at is None or m.submitted_at is None else m.started_at - m.submitted_at
    label = ledger_label(site, m.label)
    acts.append(Join(site, m.label, label, m.jobs, m.ends_at, settings.kill_wait_s, wait, "up, linked and verified"))
    return 1


def _joined(now: float, ledger: LedgerView, settings: SiteSettings, m: Member, fed: Federation, acts: list) -> str:
    """A federation the ledger holds: prove its end, drain it, retire it; say whether it is `free` (idle, takes a
    lease), `working` (a lease in flight, not draining) or neither."""
    site, label = settings.label, fed.label
    held = ledger.holding.get(label)
    if m.status == "down":
        if fed.ended_at is None:
            acts.append(ProveEnded(site, m.label, label, "cancelled", "its down was verified by the fleet"))
        return "gone"
    if fed.ended_at is not None or fed.drained_at is not None:
        if held not in LIVE:
            why = "drained" if fed.ended_at is None else f"its job is proven ended ({fed.end_proof})"
            acts.append(Retire(site, m.label, label, m.root, True, f"{why}, and no lease in flight"))
        return "gone"
    if m.ended:
        acts.append(ProveEnded(site, m.label, label, "terminal", f"the site shows its job ended ({m.state})"))
        return "gone"
    cutoff = cutoff_s(settings, ledger)
    if m.status != "up":
        acts.append(Drain(site, m.label, label, f"its fleet status is {m.status}: it is taken down again"))
    elif held == "expired":
        acts.append(Drain(site, m.label, label, "its lease expired on a running job: it is replaced"))
    elif fed.job_end_at - cutoff <= now < fed.job_end_at:
        left = fed.job_end_at - now
        acts.append(Drain(site, m.label, label, f"{left:.0f} s to its job end, within the {cutoff:.0f} s cut-off"))
    elif now >= fed.job_end_at:
        return "gone"  # past its end: the site's read or the ledger's sweep proves it
    else:
        return "working" if held in LIVE else "free"
    return "working" if held in LIVE else "gone"


def _room(now: float, settings: SiteSettings, view: SiteView, busy: frozenset[str]) -> tuple[int, dict]:
    """How many more federations the site can take now (before `parallel_ups`), and each limit's own figure."""
    live = [m for m in view.members if m.status not in HOLDS_NOTHING]
    starting = [m for m in live if m.status in ("claimed", "made", "up") and m.waiting]
    unsettled = [m for m in live if not (m.linked and m.verified)]  # its files are not all on scratch yet
    per = view.nodes_per_federation
    limits: dict = {"maximum": settings.max_federations - len(live)}
    limits["nodes"] = max(view.idle_nodes - per * len(starting), 0) // per if per > 0 else 0
    limits.update(idle_nodes=view.idle_nodes, mixed_nodes=view.mixed_nodes, nodes_reserved=per * len(starting))
    if view.inodes_error is not None or (view.inodes_quota is None) != (view.inodes_used is None):
        limits["inodes"] = 0  # a quota that could not be read is never room
    elif view.inodes_quota is None:
        limits["inodes"] = None  # nothing on scratch: no inode limit
    else:
        line = view.inodes_quota * LINE_PERCENT // 100
        reserved = view.inode_estimate * len(unsettled)
        free_inodes = line - view.inodes_used - reserved
        estimate = max(view.inode_estimate, 1)  # an estimate of 0 still adds only while the usage is under the line
        limits["inodes"] = 0 if free_inodes < 0 else free_inodes // estimate
        limits["inodes_reserved"] = reserved
    whole = settings.rate_units_per_hour * view.time_s / 3600.0
    made = sum(1 for m in view.members if m.status != "taken")
    left = settings.allowance_units - made * whole
    limits["allowance"] = int(left // whole) if whole > 0 and left >= whole else 0
    limits["stalled"] = any(m.submitted_at is not None and now - m.submitted_at >= settings.queue_stall_s
                            for m in starting)  # fmt: skip
    limits["adding"] = settings.label in busy
    figures = [limits["maximum"], limits["nodes"], limits["allowance"]]
    if limits["inodes"] is not None:
        figures.append(limits["inodes"])
    room = 0 if limits["stalled"] or limits["adding"] else max(min(figures), 0)
    return room, limits


def _add(need: int, rooms: dict[str, int], parallel: dict[str, int], decision: Decision) -> None:
    """`need` more federations, one at a time to the site with the most room left (the first declared on a tie), each
    site taking at most its `parallel_ups` in one add."""
    given = dict.fromkeys(rooms, 0)
    order = list(rooms)
    for _ in range(need):
        open_ = [s for s in order if given[s] < parallel[s] and rooms[s] - given[s] > 0]
        if not open_:
            break
        best = max(open_, key=lambda s: (rooms[s] - given[s], -order.index(s)))
        given[best] += 1
    for site in order:
        if given[site]:
            why = f"{decision.wanted} lease(s) wanted, {need} federation(s) short; room here {rooms[site]}"
            decision.actions.append(Add(site, given[site], why))


def _retire_idle(
    now: float, surplus: int, ledger: LedgerView, sites: Sequence[tuple[SiteSettings, SiteView]], decision: Decision
) -> None:
    """Drain up to `surplus` joined federations idle past their re-up cost, the least walltime left first, each site
    keeping its warm pool."""
    drained = {a.label for a in decision.actions if isinstance(a, Drain | Retire | ProveEnded)}
    candidates = []
    for settings, view in sites:
        if view.error is not None:
            continue
        idle = []
        for m in view.members:
            label = ledger_label(settings.label, m.label)
            fed = ledger.federations.get(label)
            if fed is None or label in drained or fed.drained_at is not None or fed.ended_at is not None:
                continue
            if m.status != "up" or m.state != "RUNNING" or ledger.holding.get(label) is not None:
                continue
            since = ledger.idle_since.get(label, fed.joined_at)
            if now - since >= settings.retire_idle_s:
                idle.append((fed.job_end_at, label, settings, m))
        idle.sort(key=lambda item: (item[0], item[1]))
        kept = max(settings.warm_pool, 0)
        candidates += idle[: max(len(idle) - kept, 0)]
    candidates.sort(key=lambda item: (item[0], item[1]))
    for _, label, settings, m in candidates[:surplus]:
        why = f"idle past its re-up cost ({settings.retire_idle_s:.0f} s) with no cap to let it work"
        decision.actions.append(Drain(settings.label, m.label, label, why))
