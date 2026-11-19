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
- **Prepare.** A site that declares a prepare command (`prepare_timeout_s`) has each member that is up, linked and
  running, and not yet verified, run it once (`Prepare`): the study's ingest and verify-data, which the core never
  reads. It joins only when the member then reads verified. A prepare that exited non-zero or timed out, one that
  exited 0 and left the data unverified, and one found started and never ended with no run of this controller's (it
  died: the controller is not the one that started it) retire the member unjoined, through the retirement path (a
  member already verified when it finds one in flight is joined). The replacement is the usual add.
- **Proof.** A job the site shows ended is proven `terminal`; a member the fleet's record shows down (its down verified
  the job gone) is proven `cancelled`. A site that cannot be read proves nothing and gets no action.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from expcore.campaign.elastic.controlinputs import (
    GONE,
    HOLDS_NOTHING,
    WAITING,
    LedgerView,
    Member,
    SiteSettings,
    SiteView,
    environment_digest,
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
    "Prepare",
    "ProveEnded",
    "Retire",
    "Settle",
    "SiteSettings",
    "SiteView",
    "decide",
    "environment_digest",
    "ledger_label",
    "ledger_view",
    "prepare_key",
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
class Prepare:
    """Run the site's prepare command for a member that is up, linked and running with its data not verified: once,
    and one at a time for a member (`prepare_key` is its busy key)."""

    site: str
    member: str
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
    label, None for a member that never joined. `abort_prepare` first ends a prepare found started and never ended."""

    site: str
    member: str
    label: str | None
    root: str
    collect: bool
    reason: str
    abort_prepare: bool = False


@dataclass(frozen=True)
class ProveEnded:
    site: str
    member: str
    label: str
    proof: str
    reason: str


@dataclass(frozen=True)
class Settle:
    """A lost lease, not yet settled: its spend is read from its dead federation's call log (`root`, the surviving
    root, when the site still shows one) and settled in the ledger; a lease whose log cannot be read stays at its
    grant."""

    site: str
    member: str
    label: str
    lease_id: int
    root: str | None
    reason: str


Action = ProveEnded | Settle | Drain | Join | Prepare | Retire | Add
ORDER = (ProveEnded, Settle, Drain, Join, Prepare, Retire, Add)


@dataclass
class Decision:
    """One tick's decision: per site its target, room and limits; the actions in order; each up's queue wait."""

    at: float
    wanted: int
    targets: dict[str, dict] = field(default_factory=dict)
    actions: list[Action] = field(default_factory=list)
    waits: dict[str, list[dict]] = field(default_factory=dict)
    relays: dict[str, list[dict]] = field(default_factory=dict)  # per site: each relay found gone, its last heartbeat


def ledger_label(site: str, member: str) -> str:
    """A member's label in the ledger: every fleet has a `fed-1`, so the site's label goes in front."""
    return f"{site}-{member}"


def prepare_key(site: str, member: str) -> str:
    """The busy key of a member's prepare in flight: not the member's own (`site/member`, a retirement's), which would
    stop the member counting as on its way."""
    return f"prepare:{site}/{member}"


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
    """The targets and the actions of one tick. `busy` holds the sites with an add running (`site`), the members with
    a retirement running (`site/member`) and the members with a prepare running (`prepare_key`)."""
    labels = [s.label for s, _ in sites]
    if len(set(labels)) != len(labels) or any(s.label != v.label for s, v in sites):
        raise ValueError("each site is given once, its settings and its read under one label")
    decision = Decision(at=now, wanted=ledger.leases_wanted())
    memory = login_budgets(sites)
    free = 0
    rooms: dict[str, int] = {}
    for settings, view in sites:
        site_free, room = _site(now, ledger, settings, view, busy, decision, memory[settings.label])
        free += site_free
        rooms[settings.label] = room
    _settle(now, ledger, sites, decision)
    need = decision.wanted - free
    if need > 0:
        shared = {label: (key, room) for label, (key, room, _) in memory.items()}
        _add(need, rooms, {s.label: s.parallel_ups for s, _ in sites}, shared, decision)
    elif need + ledger.reopenable < 0:
        # keep a federation for each exhausted run a pending settle may reopen: settle first, then retire
        _retire_idle(now, -need - ledger.reopenable, ledger, sites, decision)
    decision.actions.sort(key=lambda action: ORDER.index(type(action)))
    return decision


def _site(
    now: float,
    ledger: LedgerView,
    settings: SiteSettings,
    view: SiteView,
    busy: frozenset[str],
    decision: Decision,
    memory: tuple[str, int | None, dict],
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
            free += _unjoined(now, ledger, settings, m, decision.actions, busy)
        else:
            state = _joined(now, ledger, settings, m, fed, decision.actions)
            free += state == "free"
            working += state == "working"
    room, limits = _room(now, settings, view, busy, memory)
    available = free + working
    decision.targets[site] = {"target": min(settings.max_federations, available + room), "available": available,
                              "room": room, "limits": limits}  # fmt: skip
    return free, room


def _unjoined(
    now: float, ledger: LedgerView, settings: SiteSettings, m: Member, acts: list, busy: frozenset[str] = frozenset()
) -> int:
    """A member the ledger does not hold: join it once ready, take it down when it cannot serve; 1 while it is on its
    way (an up in flight, or its data not yet verified) or joins now."""
    site = settings.label
    if m.status == "down":
        return 0
    if prepare_key(site, m.label) in busy:
        return 1  # a prepare in flight here: nothing else is decided for the member until it ends (a no-op elsewhere)
    if m.status in ("failed", "down-failed") or m.ended or m.broken:
        acts.append(Retire(site, m.label, None, m.root, False, f"never joined: {m.status}, its job {m.state}"))
        return 0
    if m.status == "up" and m.linked and m.relay == "gone":
        acts.append(Retire(site, m.label, None, m.root, True, relay_gone(m)))
        return 0
    if m.status != "up" or not m.linked or m.waiting:
        return 1
    if settings.prepare_timeout_s is None:
        if not m.verified:
            return 1
    else:
        held = _prepared(settings, m, acts)
        if held is not None:
            return held
    if m.ends_at is None or m.ends_at - cutoff_s(settings, ledger) <= now:
        acts.append(Retire(site, m.label, None, m.root, True, "too close to its job end to take a lease"))
        return 0
    wait = None if m.started_at is None or m.submitted_at is None else m.started_at - m.submitted_at
    label = ledger_label(site, m.label)
    acts.append(Join(site, m.label, label, m.jobs, m.ends_at, settings.kill_wait_s, wait, "up, linked and verified"))
    return 1


def _prepared(settings: SiteSettings, m: Member, acts: list) -> int | None:
    """A member up, linked and running at a site that declares a prepare command: 1 while its data is on its way (a
    prepare is to start), 0 when it is retired unjoined, None when it may join (verified, with no prepare against it).
    A prepare in flight here was handled before this (`_unjoined`)."""
    site = settings.label
    if m.prepare == "failed":  # the marker does not redeem a prepare that failed
        acts.append(Retire(site, m.label, None, m.root, True, "never joined: its prepare failed or timed out"))
        return 0
    if m.prepare == "running":  # started and never ended, and no run of ours: the controller that started it is gone
        if m.verified:
            return None
        why = "never joined: its prepare was in flight when its controller died, and its data is not verified"
        acts.append(Retire(site, m.label, None, m.root, True, why, True))
        return 0
    if m.verified:
        return None
    if m.prepare == "ok":
        why = "never joined: its prepare exited 0 and its data is not verified"
        acts.append(Retire(site, m.label, None, m.root, True, why))
        return 0
    acts.append(Prepare(site, m.label, "up and linked, its data not verified"))
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
    if now >= fed.job_end_at:
        return "gone"  # past its recorded end: never drained now; the site's read or the ledger's sweep proves it
    cutoff = cutoff_s(settings, ledger)
    if m.status != "up":
        acts.append(Drain(site, m.label, label, f"its fleet status is {m.status}: it is taken down again"))
    elif m.broken:
        acts.append(Drain(site, m.label, label, f"its jobs are {m.state}: one ended or is ending beside another"))
    elif held == "expired":
        acts.append(Drain(site, m.label, label, "its lease expired on a running job: it is replaced"))
    elif m.relay == "gone":
        acts.append(Drain(site, m.label, label, relay_gone(m)))
    elif fed.job_end_at - cutoff <= now < fed.job_end_at:
        left = fed.job_end_at - now
        acts.append(Drain(site, m.label, label, f"{left:.0f} s to its job end, within the {cutoff:.0f} s cut-off"))
    else:
        return "working" if held in LIVE else "free"
    return "working" if held in LIVE else "gone"


def relay_gone(m: Member) -> str:
    """The record of a fault relay found gone: no outage can be injected on its federation without it."""
    beat = "none read" if m.relay_beat_at is None else f"{m.relay_beat_at:.0f}"
    return f"relay gone, no exit status (its last heartbeat at {beat}): outage injection cannot run without it"


def login_load(settings: SiteSettings, view: SiteView) -> dict:
    """What one fleet takes of its login node's memory, in MB: the relays beating there, each up and each verify-data
    in flight (every member up, linked and not yet verified: a prepare in flight is one)."""
    live = [m for m in view.members if m.status not in HOLDS_NOTHING]
    relays = sum(1 for m in live if m.relay == "here")
    ups = sum(1 for m in live if m.status in ("claimed", "made"))
    verifying = sum(1 for m in live if m.status == "up" and m.linked and not m.verified)
    load = relays * settings.relay_mb + ups * settings.up_mb + verifying * settings.verify_mb
    return {"load_mb": load, "relays": relays, "ups": ups, "verifying": verifying}


def login_budgets(sites: Sequence[tuple[SiteSettings, SiteView]]) -> dict[str, tuple[str, int | None, dict]]:
    """Per site: its login node's key, how many more federations (an up now, a relay after) that login node's memory
    takes, shared by every site on it (one cgroup), and the figures; None where no site on it declares a cap. A site
    on it that could not be read leaves no room: its use is unknown."""
    groups: dict[str, list[tuple[SiteSettings, SiteView]]] = {}
    for settings, view in sites:
        groups.setdefault(settings.login_node or settings.label, []).append((settings, view))
    found: dict[str, tuple[str, int | None, dict]] = {}
    for key, members in groups.items():
        caps = [s.login_memory_mb for s, _ in members if s.login_memory_mb is not None]
        if not caps:
            room, detail = None, {}
        else:
            reserve = max(s.login_reserve_mb for s, _ in members)
            each = max(s.relay_mb + s.up_mb for s, _ in members)
            loads = [login_load(s, v) for s, v in members if v.error is None]
            used = reserve + sum(load["load_mb"] for load in loads)
            free = min(caps) - used
            dark = len(loads) < len(members)
            room = 0 if dark or free < each else int(free // each)
            detail = {"login_node": key, "cap_mb": min(caps), "used_mb": used, "sites": len(members), "dark": dark,
                      **{k: sum(load[k] for load in loads) for k in ("relays", "ups", "verifying")}}  # fmt: skip
        for settings, _ in members:
            found[settings.label] = (key, room, detail)
    return found


def _room(
    now: float, settings: SiteSettings, view: SiteView, busy: frozenset[str], memory: tuple[str, int | None, dict]
) -> tuple[int, dict]:
    """How many more federations the site can take now (before `parallel_ups`), and each limit's own figure."""
    live = [m for m in view.members if m.status not in HOLDS_NOTHING]
    # every up whose job has not started, whatever its record says (a failed one may still hold a waiting job)
    starting = [m for m in live
                if (m.status in ("claimed", "made") and m.state is None) or m.state in WAITING or m.broken]  # fmt: skip
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
                            for m in starting if m.waiting)  # fmt: skip
    limits["adding"] = settings.label in busy
    limits["login_memory"], limits["login_memory_mb"] = memory[1], memory[2]
    figures = [limits["maximum"], limits["nodes"], limits["allowance"]]
    figures += [limits[key] for key in ("inodes", "login_memory") if limits[key] is not None]
    room = 0 if limits["stalled"] or limits["adding"] else max(min(figures), 0)
    return room, limits


def _settle(now: float, ledger: LedgerView, sites: Sequence[tuple[SiteSettings, SiteView]], decision: Decision) -> None:
    """A `Settle` for each lost lease not yet settled, but one whose job was cancelled less than KillWait ago (it may
    still be spending), and each relay found gone, per site."""
    views = {settings.label: view for settings, view in sites}
    for settings, view in sites:
        decision.relays[settings.label] = [
            {"member": m.label, "relay": "gone, no exit status", "last_beat_at": m.relay_beat_at}
            for m in view.members
            if m.relay == "gone" and m.status not in HOLDS_NOTHING
        ]
    if ledger.left_micro is None:
        return  # no budget: no lease holds a charge to settle
    for lease_id, label, state in ledger.lost:
        fed = ledger.federations.get(label)
        if fed is None or fed.site not in views:
            continue
        if state == "expired" and fed.end_proof == "cancelled" and now < (fed.ended_at or now) + fed.kill_wait_s:
            continue
        member = label[len(fed.site) + 1 :] if label.startswith(f"{fed.site}-") else label
        view = views[fed.site]
        found = next((m for m in view.members if m.label == member and m.status != "down"), None)
        same = found is not None and set(found.jobs) == set(fed.job_ids)  # the member holding the jobs it joined with
        root = found.root if view.error is None and same else None
        why = f"lease {lease_id} is lost ({state}, its holder proven ended) and still charged its grant"
        decision.actions.append(Settle(fed.site, member, label, lease_id, root, why))


def _add(
    need: int,
    rooms: dict[str, int],
    parallel: dict[str, int],
    shared: dict[str, tuple[str, int | None]],
    decision: Decision,
) -> None:
    """`need` more federations, one at a time to the site with the most room left (the first declared on a tie), each
    site taking at most its `parallel_ups` in one add, and the sites on one login node together at most what its
    memory takes."""
    given = dict.fromkeys(rooms, 0)
    order = list(rooms)
    taken: dict[str, int] = {}
    for _ in range(need):
        open_ = [
            s
            for s in order
            if given[s] < parallel[s]
            and rooms[s] - given[s] > 0
            and (shared[s][1] is None or taken.get(shared[s][0], 0) < shared[s][1])
        ]
        if not open_:
            break
        best = max(open_, key=lambda s: (rooms[s] - given[s], -order.index(s)))
        given[best] += 1
        taken[shared[best][0]] = taken.get(shared[best][0], 0) + 1
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
            if now >= fed.job_end_at:
                continue  # past its recorded end: never drained
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
