"""fleetshare.py: several controllers of one study, one per ledger (campaign), sharing models, priority and sites
(C53; README.md, "Several ledgers, one controller each"). Scheduling only: nothing here writes a ledger.

The sites file's `[controller]` may name:
- `siblings = [<other ledgers' paths>]` with `shared_caps = { <model> = N, ... }` (together, and only with `archives`:
  the leases are issued by this loop's dispatcher). This ledger issues a lease of a shared model only while its own
  slots of it plus every sibling's stay below N (`expcore.campaign.elastic.siblings.SharedCaps`), as well as under its
  own cap. A sibling that cannot be read stops the model, fail closed. The siblings are read again before each issue.
- `defer_to = [<ledger paths>]`: while any of these ledgers has runs waiting for capacity (`siblings.waiting_of`), or
  cannot be read, this controller adds no federation and joins no member of a Compose site; members already joined
  keep serving until retired as before, and nothing is retired for priority.

A `[[site]]` may name `shared_site_file`, a JSON file on this host (`holders`) that every controller of that site
updates under a lock: its `max_federations` and `allowance_units` are then the site's totals across controllers
(`within`). Each tick, after the site reads, the controller locks the file, decides within what the other holders
leave, and rewrites its own entry (its members, its committed units, the Adds it just decided and any add still
running) before unlocking.

None of these keys: the controller is exactly the one before them.
"""

import contextlib
import dataclasses
import fcntl
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import fleetcompose

CONTROLLER_KEYS = ("siblings", "shared_caps", "defer_to")
SITE_KEYS = ("shared_site_file",)
FORMAT = "expcore-shared-site/1"
LOCK_TIMEOUT_S = 10.0
HOLDER_KEYS = ("members", "units", "max_federations", "allowance_units", "at")


@dataclass(frozen=True)
class Sharing:
    """What a sites file shares: the siblings and shared caps, the ledgers deferred to, each shared site's file."""

    siblings: Tuple[Path, ...] = ()
    shared_caps: Mapping[str, int] = field(default_factory=dict)
    defer_to: Tuple[Path, ...] = ()
    site_files: Mapping[str, Path] = field(default_factory=dict)


@dataclass
class State:
    """What the loop keeps across ticks: each site's add still running (its count), the siblings unread last."""

    adding: Dict[str, int] = field(default_factory=dict)
    unread: Tuple[str, ...] = ()


def _paths(value: Any, what: str) -> Tuple[Path, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"[controller] {what} must be a list of at least one ledger's absolute path")
    found = []
    for item in value:
        if not isinstance(item, str) or not os.path.isabs(item):
            raise ValueError(f"[controller] {what}: each must be an absolute path")
        found.append(Path(os.path.normpath(item)))
    if len({os.path.realpath(p) for p in found}) != len(found):
        raise ValueError(f"[controller] {what} names a ledger twice")
    return tuple(found)


def sharing_of(table: Mapping[str, Any], site_files: Mapping[str, Any], dispatched: bool) -> Optional[Sharing]:
    """The sharing keys of a sites file, checked (ValueError naming the key); None when it names none of them."""
    if not any(key in table for key in CONTROLLER_KEYS) and not site_files:
        return None
    if ("siblings" in table) != ("shared_caps" in table):
        raise ValueError("[controller] siblings and shared_caps go together")
    siblings = _paths(table["siblings"], "siblings") if "siblings" in table else ()
    caps = table.get("shared_caps", {})
    if not isinstance(caps, dict) or ("shared_caps" in table and not caps):
        raise ValueError("[controller] shared_caps must be a table of models and their shared caps")
    for model, cap in caps.items():
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            raise ValueError(f"[controller] shared_caps.{model} must be a whole number from 1")
    if caps and not dispatched:
        raise ValueError("[controller] shared_caps needs archives: the leases are issued by this loop's dispatcher")
    defer = _paths(table["defer_to"], "defer_to") if "defer_to" in table else ()
    files: Dict[str, Path] = {}
    for label, value in site_files.items():
        if not isinstance(value, str) or not os.path.isabs(value):
            raise ValueError(f"site {label}: shared_site_file must be an absolute path on this host")
        files[label] = Path(os.path.realpath(value))  # one lock file whatever link names it
    if len({os.path.realpath(p) for p in files.values()}) != len(files):
        raise ValueError("two sites name one shared_site_file: each site's file is its own")
    return Sharing(siblings, dict(caps), defer, files)


def check_ledger(sharing: Optional[Sharing], ledger: Path) -> None:
    """The controller's own ledger is never its own sibling, nor a ledger it defers to."""
    if sharing is None:
        return
    own = os.path.realpath(ledger)
    if any(os.path.realpath(p) == own for p in (*sharing.siblings, *sharing.defer_to)):
        raise ValueError("siblings and defer_to name other controllers' ledgers, never this one's own")


def open_ledger(path: Path, clock: Any, sharing: Optional[Sharing]) -> Any:
    """The ledger the controller holds: with shared caps, the one whose issues and caps hold to them."""
    from expcore.campaign.elastic import Ledger
    from expcore.campaign.elastic.siblings import SharedCaps, SharedLedger

    if sharing is None or not sharing.shared_caps:
        return Ledger.open(path, clock=clock)
    ledger = SharedLedger.open(path, clock=clock)
    ledger.shared = SharedCaps(sharing.siblings, sharing.shared_caps)
    return ledger


def open_reader(path: Path, sharing: Optional[Sharing]) -> Any:
    """A dry run's read of the ledger, with the shared caps folded into its caps."""
    from expcore.campaign.elastic.control import read_only
    from expcore.campaign.elastic.siblings import SharedCaps, SharedReader

    if sharing is None or not sharing.shared_caps:
        return read_only(path)
    reader = SharedReader(path)
    reader.shared = SharedCaps(sharing.siblings, sharing.shared_caps)
    return reader


# ------------------------------------------------------------------------------------------------ the site file
@contextlib.contextmanager
def locked(path: Path, timeout_s: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """The shared site file held by one controller at a time: an exclusive lock on `<file>.lock`, a file that is never
    replaced (a lock on the JSON itself would be lost when it is rewritten). TimeoutError past `timeout_s`."""
    descriptor = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"the shared site file's lock was not taken in {timeout_s:.0f} s") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(descriptor)


def _number(value: Any, whole: bool) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        if not math.isfinite(value) or value < 0:
            return False
    except OverflowError:  # an integer too large for a float
        return False
    return isinstance(value, int) if whole else True


def read_holders(path: Path) -> Dict[str, Dict[str, Any]]:
    """Every holder's entry of the shared site file; none for a file not made yet. ValueError for anything else."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = json.loads(text)
    holders = data.get("holders") if isinstance(data, dict) and data.get("format") == FORMAT else None
    if not isinstance(holders, dict):
        raise ValueError(f"it is not a file of {FORMAT}")
    for key, entry in holders.items():
        good = isinstance(entry, dict) and set(entry) == set(HOLDER_KEYS)
        if not good or not all(_number(entry[k], k in ("members", "max_federations")) for k in HOLDER_KEYS):
            raise ValueError(f"holder {key!r} is not an entry of {FORMAT}")
    return holders


def write_holders(path: Path, holders: Mapping[str, Mapping[str, Any]]) -> None:
    """The file rewritten whole and crash-safe: a draft beside it, flushed to disk, renamed over it, the directory
    flushed."""
    descriptor, draft = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"format": FORMAT, "holders": holders}, sort_keys=True, indent=1) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(draft, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(draft)
        raise
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def usage(settings: Any, view: Any, adding: int) -> Dict[str, Any]:
    """What a controller holds of a site, as `decide` counts it against the site's maximum and allowance: every member
    that may hold a job, and every member made times one whole `--time`; with the Adds on their way."""
    from expcore.campaign.elastic.control import HOLDS_NOTHING

    live = sum(1 for m in view.members if m.status not in HOLDS_NOTHING)
    made = sum(1 for m in view.members if m.status != "taken")
    whole = settings.rate_units_per_hour * view.time_s / 3600.0
    return {"members": live + adding, "units": (made + adding) * whole}


def within(settings: Any, holders: Mapping[str, Mapping[str, Any]], own: str) -> Tuple[Any, Dict[str, Any]]:
    """The site's settings for this controller: the least total any holder declares, less what every other holder
    holds, never below 0."""
    others = [entry for key, entry in holders.items() if key != own]
    maximum = min([settings.max_federations, *(e["max_federations"] for e in others)])
    allowance = min([settings.allowance_units, *(e["allowance_units"] for e in others)])
    members, units = sum(e["members"] for e in others), sum(e["units"] for e in others)
    left = dataclasses.replace(settings, max_federations=max(maximum - members, 0),
                               allowance_units=max(float(allowance - units), 0.0))  # fmt: skip
    return left, {"holders": len(others), "others_members": members, "others_units": units,
                  "max_federations": left.max_federations, "allowance_units": left.allowance_units}  # fmt: skip


def closed(settings: Any) -> Any:
    """A shared site whose file cannot be read or locked: no room this tick."""
    return dataclasses.replace(settings, max_federations=0, allowance_units=0.0)


# ------------------------------------------------------------------------------------------------ priority
def deferral(now: float, sharing: Sharing, views: Any) -> Dict[str, Any]:
    """Each ledger deferred to, read: its leases wanted, its free federations and its runs waiting; `deferred` when
    any waits or cannot be read."""
    from expcore.campaign.elastic.siblings import UNREADABLE, waiting_of

    shared = getattr(views, "shared", None)
    own = views.slots()
    found: Dict[str, Any] = {}
    deferred = False
    for path in sharing.defer_to:
        try:
            waiting = waiting_of(path, now, shared, own)
        except UNREADABLE as error:
            found[str(path)] = {"unreadable": f"{type(error).__name__}: {error}"}
            deferred = True
            continue
        found[str(path)] = {"wanted": waiting.wanted, "free": waiting.free, "waiting": waiting.count}
        deferred = deferred or waiting.count > 0
    return {"deferred": deferred, "ledgers": found}


def deferrable(action: Any, sites: Mapping[str, Any]) -> bool:
    """What priority holds back: an add, and a join of a Compose site's static member."""
    kind = type(action).__name__
    return kind == "Add" or (kind == "Join" and fleetcompose.is_compose(sites.get(action.site)))


def shown(action: Any) -> Dict[str, Any]:
    kind = type(action).__name__
    return {"action": kind, "site": action.site,
            **({"count": action.count} if kind == "Add" else {"member": action.member})}  # fmt: skip


# ------------------------------------------------------------------------------------------------ one tick
def decide(now: float, views: Any, reads: Sequence[Tuple[Any, Any]], busy: frozenset, sharing: Sharing,
           sites: Sequence[Any], ledger: Path, state: State, write: bool,
           say: Any = print) -> Tuple[Any, Any, Dict[str, Any]]:  # fmt: skip
    """`control.decide` within what is shared: the ledger's view (its caps with the siblings just read), the decision,
    and what the tick's log line adds: the shared caps read, the priority read and the actions it held back, each
    shared site's totals left. `write` (a live tick) rewrites this controller's entry of each shared site file under
    its lock; a dry run reads the files and writes nothing."""
    from expcore.campaign.elastic.control import decide as decide_sites
    from expcore.campaign.elastic.control import ledger_view

    extra: Dict[str, Any] = {}
    by_label = {site.label: site for site in sites}
    state.adding = {site: count for site, count in state.adding.items() if site in busy}
    shared = getattr(views, "shared", None)
    if shared is not None:
        shared.refresh()
        extra["shared_caps"] = shared.state()
        if shared.unread != state.unread:
            say(f"fleetshare: {len(shared.unread)} sibling ledger(s) not read: their shared models issue nothing"
                if shared.unread else "fleetshare: every sibling ledger read again")  # fmt: skip
            state.unread = shared.unread
    view = ledger_view(views)
    defer = deferral(now, sharing, views) if sharing.defer_to else None
    own = os.path.realpath(ledger)
    with contextlib.ExitStack() as stack:
        files: Dict[str, Optional[Dict[str, Dict[str, Any]]]] = {}
        totals: Dict[str, Dict[str, Any]] = {}
        for label, path in sorted(sharing.site_files.items(), key=lambda item: os.path.realpath(item[1])):
            if label not in by_label:
                continue
            try:
                if write:
                    stack.enter_context(locked(path))
                files[label] = read_holders(path)
            except (OSError, ValueError, TimeoutError) as error:
                files[label] = None
                totals[label] = {"error": f"{type(error).__name__}: {error}", "max_federations": 0}
        adjusted = []
        for settings, site_view in reads:
            holders = files.get(settings.label, {})
            if settings.label not in files:
                adjusted.append((settings, site_view))
            elif holders is None:
                adjusted.append((closed(settings), site_view))
            else:
                left, totals[settings.label] = within(settings, holders, own)
                adjusted.append((left, site_view))
        decision = decide_sites(now, view, adjusted, busy)
        if defer is not None:
            if defer["deferred"]:
                defer["held_back"] = [shown(a) for a in decision.actions if deferrable(a, by_label)]
                decision.actions = [a for a in decision.actions if not deferrable(a, by_label)]
            extra["defer"] = defer
        adds = {a.site: a.count for a in decision.actions if type(a).__name__ == "Add"}
        if write:
            state.adding.update(adds)
        for settings, site_view in reads:
            holders = files.get(settings.label)
            if not write or holders is None or site_view.error is not None:
                continue  # a dark site keeps its last entry: what it held is not known now
            entry = usage(settings, site_view, state.adding.get(settings.label, 0))
            holders = {**holders, own: {**entry, "max_federations": settings.max_federations,
                                        "allowance_units": float(settings.allowance_units), "at": now}}  # fmt: skip
            try:
                write_holders(sharing.site_files[settings.label], holders)
            except OSError as error:  # an add the other holders cannot see is never made
                totals[settings.label]["error"] = f"its entry was not written: {type(error).__name__}"
                decision.actions = [a for a in decision.actions
                                    if not (type(a).__name__ == "Add" and a.site == settings.label)]  # fmt: skip
                if settings.label in adds:
                    state.adding.pop(settings.label, None)
    if totals:
        extra["shared_sites"] = totals
    return view, decision, extra


__all__: List[str] = ["CONTROLLER_KEYS", "SITE_KEYS", "Sharing", "State", "decide", "sharing_of"]
