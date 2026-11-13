# Provenance: new
"""The reset's QoS requirements: found, the run's own removed, and the files they touched settled and kept quiet (O33).

Sources are op-worker `f118ed9` (paths below are its own) and ctool `8ae7baf` (`ctool:`), read for #209.

**Finding them.** The build has no listing of a space's requirements: QoS's REST routes are `POST /qos_requirements`,
`GET` and `DELETE /qos_requirements/{qid}`, the audit log, `GET /data/{id}/qos/summary` and an expression's evaluation
(`src/http/rest/routes/qos_rest_routes.erl:31-101`), and a QoS replication creates no transfer document
(`src/modules/qos/qos_traverse.erl:298-305,332-338`), so the transfer listing cannot show one either. `[placement]
qos_scan` chooses how the reset finds them:

- `summaries` (the default): on every provider, the space is walked, each directory listed once, and the summary of
  the root and of every leaf is read; a leaf's summary holds the requirements on it and on every ancestor
  (`file_middleware_get_handler.erl:493-500`), so together they hold every requirement. One read per leaf per
  provider: about 52,000 reads for a large tree (#209).
- `listing`: on the data provider, one recursive listing of the space root, `GET /data/{root}/files` with
  `attributes` `fileId`, `path`, `type` and `effQosInheritancePath`, `include_directories=true` and `limit`, its pages
  followed by `token` until `nextPageToken` is null (`basic_file_operations_rest_routes.erl:59-70`; the parameters,
  query or JSON body, `file_middleware_get_handler.erl:124-138`, `rest_handler.erl:313-325`; the answer, `files`,
  `inaccessiblePaths` and `nextPageToken`, `file_rest_translator.erl:92-97`, with no `isLast` whatever the REST
  description says; `path` is relative to the root, itself `"."`, `recursive_listing.erl:486-498,576-584`; the trash
  is never listed, `trash_dir.erl:98-101`). A listing that could not enter a path (`inaccessiblePaths`) is incomplete
  and fails closed. An entry whose `effQosInheritancePath` is `direct` or `directAndAncestor` has a requirement on
  itself (`src/modules/datastore/models/qos/file_qos.erl:262-291`); only those entries' summaries are read, and each
  requirement's record then names its entry (`fileId`, `qos_middleware_plugin.erl:241-245,300-312`).
  **Two facts it rests on are not verified live:** that the listing takes `include_directories` (the code does,
  `file_middleware_get_handler.erl:131,404`; the REST description omits it), and that a requirement added through one
  provider is found through another once dbsync settles (each provider's QoS view is built from the synced requirement
  documents, `src/modules/dbsync/dbsync_events.erl:129`, `src/modules/qos/qos_logic.erl:65-70`). The gated live tests
  in `tests/live/test_contract.py` (O33) check both; until they have passed on the federation, `summaries` stays the
  default.

**The run's own.** A requirement present at the run's start (`SpaceReset.qos_requirements()`, the caller's baseline)
is kept; every other one is the run's and is removed. The regular files at or under a removed requirement's entry are
the affected files.

**Settled, then quiet.** A QoS replication has no transfer to wait for, and one still running when its requirement is
removed can go on placing bytes. So after the removal the affected files' block maps, on every provider, must stay
the same for `SETTLE_STABLE_S`, read every `SETTLE_POLL_S`, within `SETTLE_BOUND_S`, or the reset fails. After the
eviction and the occupancy they are read again, and once more after `quiet_wait_s`, together with the occupancy: a
provider holding more of an affected file than before the wait fails the reset, and so does a provider other than the
data provider holding more of the space than at the occupancy stage. That check does not wait for a requirement to be
found: an agent can add one and remove it before the run ends, leaving the reset none to see while its replication is
still landing, so it runs for every run that declares QoS rights, and by default, and covers the files a local
provider held when the run ended as well (`resetheld.py`). The block maps decide: a file's distribution
shows its provider's in-memory location at once. A replication that lands between the eviction's verification and
the first of these readings is already in that reading, so the maps show no growth; the occupancy read after the wait,
set against the occupancy stage's, is what catches it. The occupancy is otherwise a second sign, and a zero there is
never taken alone as nothing having landed, because a directory's physical size lags a placement:

- a placement's size change is held until the file location's flush, `blocks_flush_delay` after its first change
  (10 s, `rel/files/app.config:511`; `fslogic_cache.erl:524-530,706-715`), and only then reported to the parent
  directory's statistics (`dir_size_stats.erl:218-229`);
- each directory above the parent gets it when the parent's collector flushes, 5 s after its first unflushed change
  (`FLUSH_INTERVAL_MILLIS`, `dir_stats_collector.erl:168,905-918,1037-1057`);
- so the space root's physical size reflects a placement at most 10 + 5 x (L - 1) s later, L being the levels from the
  file's parent up to the space root, the parts of the file's path (`occupancy_lag_s`). An eviction flushes at once
  (`fslogic_location_cache.erl:345-354`) and lags only the 5 s per level above the parent.

The quiet wait is twice that lag for the deepest affected file (`quiet_wait_s`), so the occupancy read after it has
had time to show a placement that landed when the wait began. Another study measured 9.5 s for a directory after a
placement and 0.05 s after an eviction, which this fits. A space with directory statistics off answers no physical
size at all, and the occupancy stage already fails on it.
"""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from expcore_onedata import consistency
from expcore_onedata.client import PlatformError, answered_absent
from expcore_onedata.placement import Range, held_bytes, merged
from expcore_onedata.provision import Listings, Provisioner, ProvisionError
from expcore_onedata.readretry import ONE_ATTEMPT
from expcore_onedata.transferrecord import Unreadable

_PLAIN_ID = re.compile(r"[A-Za-z0-9_=-]{1,512}")  # a requirement or entry id the client accepts as one path segment
LISTING_PAGE = 1000  # entries a page of the recursive listing asks for (the build takes 1 to 10,000)
DIRECT = frozenset({"direct", "directAndAncestor"})  # an `effQosInheritancePath` with a requirement on the entry itself
INHERITANCE = frozenset({"none", "ancestor", *DIRECT})
SETTLE_STABLE_S = 3.0  # seconds the affected files' block maps must stay the same after the removal
SETTLE_POLL_S = 0.5  # seconds between two readings of them
SETTLE_BOUND_S = 60.0  # the longest they are read before the reset fails
# A directory's physical size after a placement: the file location's flush, then each level's collector flush. Both
# from op-worker f118ed9: `blocks_flush_delay` in rel/files/app.config:511, `FLUSH_INTERVAL_MILLIS` in
# src/modules/fslogic/dir_stats_collector.erl:168 (the module docstring traces the path).
BLOCKS_FLUSH_DELAY_S = 10.0
DIR_STATS_FLUSH_S = 5.0
QUIET_WAIT_FACTOR = 2.0  # the quiet wait is this many times the occupancy's longest lag for the deepest affected file


def occupancy_lag_s(depth: int) -> float:
    """The longest the space root's physical size lags a placement on a file `depth` path parts below the root."""
    return BLOCKS_FLUSH_DELAY_S + DIR_STATS_FLUSH_S * (max(depth, 1) - 1)


def quiet_wait_s(depth: int) -> float:
    """The quiet re-check's wait for affected files at most `depth` path parts below the root."""
    return QUIET_WAIT_FACTOR * occupancy_lag_s(depth)


@dataclass(frozen=True)
class Found:
    """What a scan found: each requirement with the provider that showed it first, the regular files each applies to,
    each by its id with its depth (the parts of its path below the root), and how many reads of each kind it made."""

    where: dict[str, str]
    files: dict[str, dict[str, int]]
    reads: dict[str, int] = field(default_factory=dict)


def _summary(p: Provisioner, file_id: str, provider: str) -> set[str]:
    """The ids of the QoS requirements on an entry and its ancestors, from its summary on one provider."""
    answer = p.client.get(f"/data/{file_id}/qos/summary", provider=provider, retry=p.retry)
    requirements = answer.get("requirements") if isinstance(answer, dict) else None
    if not isinstance(requirements, Mapping) or not all(isinstance(q, str) for q in requirements):
        raise Unreadable("a QoS summary holds no table of requirements keyed by id")
    if not all(_plain(q) for q in requirements):  # an id becomes a path segment of the next calls
        raise Unreadable("a QoS summary holds a requirement id that is not one plain path segment")
    return set(requirements)


def _plain(value: str) -> bool:
    """Whether an id is one plain path segment, as the client accepts one."""
    return _PLAIN_ID.fullmatch(value) is not None


def tree(p: Provisioner, provider: str) -> tuple[str, list[tuple[str, str, int]]]:
    """The space root on one provider and every leaf under it, a file or an empty directory, as `(id, kind, depth)`:
    the tree `Provisioner.walk` enters, breadth first and each directory listed once, with each entry's depth."""
    root = consistency.space_root(p.client, provider, p.space_id, sleep=p.sleep, retry=p.retry)
    listings: Listings = {}
    found: list[tuple[str, str, int]] = []
    pending = [(root, 0)]
    while pending:
        current, depth = pending.pop(0)
        for entry in p.children(current, provider, listings=listings).values():
            if entry.kind != "DIR":
                found.append((entry.file_id, entry.kind, depth + 1))
                continue
            if not p.children(entry.file_id, provider, listings=listings):
                found.append((entry.file_id, entry.kind, depth + 1))
            pending.append((entry.file_id, depth + 1))
        listings.pop((provider, current), None)  # entered: no longer needed
    return root, found


def by_summaries(p: Provisioner, providers: Sequence[str]) -> Found:
    """Every requirement any provider's summaries show: on each provider, the root's and every leaf's."""
    where: dict[str, str] = {}
    files: dict[str, dict[str, int]] = {}
    read = 0
    for provider in providers:
        root, leaves = tree(p, provider)
        entries = [root, *(file_id for file_id, _, _ in leaves)]
        summaries = consistency.bounded_map(lambda f, q=provider: _summary(p, f, q), entries, p.settings.concurrency)
        for qid in sorted(set().union(*summaries)):
            where.setdefault(qid, provider)
        for (file_id, kind, depth), held in zip(leaves, summaries[1:], strict=True):
            for qid in held if kind != "DIR" else ():
                files.setdefault(qid, {})[file_id] = depth
        read += len(entries)
    return Found(where, {q: files.get(q, {}) for q in where}, {"summaries": read})


@dataclass(frozen=True)
class _Listed:
    """One entry of the recursive listing: its id, its path relative to the space root ("." for the root), whether
    it is a directory, and its `effQosInheritancePath`."""

    file_id: str
    path: str
    directory: bool
    inheritance: str


def _listing_page(p: Provisioner, provider: str, root: str, token: str | None) -> tuple[list[_Listed], str | None]:
    """One page of the space root's recursive listing with the QoS attributes; its entries and the next token. The
    first page sends no token at all: a null one would be read as the text "null"."""
    params: dict[str, Any] = {
        "attributes": ["fileId", "path", "type", "effQosInheritancePath"],
        "include_directories": "true",
        "limit": LISTING_PAGE,
        **({"token": token} if token else {}),
    }
    answer = p.client.get(f"/data/{root}/files", provider=provider, params=params, retry=p.retry)
    return _entries_of(answer)


def _entries_of(answer: Any) -> tuple[list[_Listed], str | None]:
    """Read one page of the recursive listing; refuse one not in its shape, never take it for the end (O26), and
    refuse a listing that could not enter a path, which is incomplete."""
    if not isinstance(answer, dict) or not isinstance(answer.get("files"), list):
        raise Unreadable("a page of the recursive listing has no list of files")
    if "nextPageToken" not in answer:
        raise Unreadable("a page of the recursive listing has no nextPageToken, the marker of its last page")
    token = answer["nextPageToken"]
    if token is not None and (not isinstance(token, str) or not token):
        raise Unreadable("a page of the recursive listing has a nextPageToken that is neither null nor a token")
    inaccessible = answer.get("inaccessiblePaths", [])
    if not isinstance(inaccessible, list) or inaccessible:
        raise Unreadable("the recursive listing could not enter every path (inaccessiblePaths): it is incomplete")
    entries = []
    for item in answer["files"]:
        file_id, path = (item.get("fileId"), item.get("path")) if isinstance(item, dict) else (None, None)
        inheritance, kind = (item.get("effQosInheritancePath"), item.get("type")) if isinstance(item, dict) else (0, 0)
        if not isinstance(file_id, str) or not _plain(file_id) or not isinstance(path, str) or not path:
            raise Unreadable("an entry of the recursive listing has no plain fileId or no path")
        if inheritance not in INHERITANCE or not isinstance(kind, str):
            raise Unreadable("an entry of the recursive listing has no effQosInheritancePath or no type")
        entries.append(_Listed(file_id, path.strip("/") or ".", kind == "DIR", inheritance))
    return entries, token


def _requirement_entry(p: Provisioner, qid: str, provider: str) -> str:
    """The id of the entry a requirement is on, from its record's `fileId`."""
    record = p.client.get(f"/qos_requirements/{qid}", provider=provider, retry=p.retry)
    file_id = record.get("fileId") if isinstance(record, dict) else None
    if not isinstance(file_id, str) or not file_id:
        raise Unreadable("a QoS requirement's record names no fileId")
    return file_id


def _under(path: str, top: str) -> bool:
    return top == "." or path == top or path.startswith(f"{top}/")


def listed(p: Provisioner, provider: str) -> tuple[dict[str, _Listed], int]:
    """Every entry of the space root's recursive listing on `provider`, by id, and how many pages were read."""
    root = consistency.space_root(p.client, provider, p.space_id, sleep=p.sleep, retry=p.retry)
    entries: dict[str, _Listed] = {}
    token: str | None = None
    pages = 0
    while True:
        page, token = _listing_page(p, provider, root, token)
        pages += 1
        entries.update((e.file_id, e) for e in page)
        if token is None:
            return entries, pages


def by_listing(p: Provisioner, provider: str) -> Found:
    """Every requirement on the space, from one recursive listing on `provider`, the summaries of the entries a
    requirement is on, and each requirement's record."""
    entries, pages = listed(p, provider)
    hits = [e.file_id for e in entries.values() if e.inheritance in DIRECT]
    found = consistency.bounded_map(lambda f: _summary(p, f, provider), hits, p.settings.concurrency)
    qids = sorted(set().union(*found))
    on = consistency.bounded_map(lambda q: _requirement_entry(p, q, provider), qids, p.settings.concurrency)
    files: dict[str, dict[str, int]] = {}
    for qid, entry_id in zip(qids, on, strict=True):
        top = entries.get(entry_id)
        if top is None:
            raise Unreadable("a QoS requirement is on an entry the recursive listing does not hold")
        under = (e for e in entries.values() if not e.directory and _under(e.path, top.path))
        files[qid] = {e.file_id: len(e.path.split("/")) for e in under}
    reads = {"listing_pages": pages, "summaries": len(hits), "records": len(qids)}
    return Found(dict.fromkeys(qids, provider), files, reads)


def scan(p: Provisioner, providers: Sequence[str]) -> Found:
    """Find every requirement of the space as `[placement] qos_scan` says."""
    if p.settings.qos_scan == "listing":
        return by_listing(p, p.settings.holder)
    return by_summaries(p, providers)


def _gone(p: Provisioner, key: tuple[str, str]) -> bool:
    """Whether a removed requirement answers on one provider that it is not there (`answered_absent`: a 404, or
    25.0's 400 posix enoent); any other failure, another 400 included, propagates and does not count."""
    provider, qid = key
    try:
        p.client.get(f"/qos_requirements/{qid}", provider=provider, retry=ONE_ATTEMPT)
    except PlatformError as error:
        if answered_absent(error):
            return True
        raise
    return False


def remove(p: Provisioner, providers: Sequence[str], baseline: Iterable[str]) -> tuple[dict[str, Any], dict[str, int]]:
    """Find every requirement, remove each one not in `baseline` through the gate, and show them gone everywhere: each
    answers that it is not there on every provider, and a second scan finds none but the baseline's. Return the
    stage's facts and the affected files, those the removed requirements applied to, each with its depth."""
    kept = frozenset(baseline)
    found = scan(p, providers)
    added = {qid: where for qid, where in found.where.items() if qid not in kept}
    facts: dict[str, Any] = {"scan": p.settings.qos_scan, "found": len(found.where)}
    facts["kept"] = len(found.where) - len(added)
    affected = {file_id: depth for qid in added for file_id, depth in found.files[qid].items()}
    for qid, provider in added.items():
        p.mutate("DELETE", f"/qos_requirements/{qid}", provider=provider)
    reads = dict(found.reads)
    if added:
        settings = p.settings
        gone = consistency.converge_each(
            [(provider, qid) for provider in providers for qid in added],
            lambda key: _gone(p, key),
            bool,
            timeout_s=settings.settle_timeout_s,
            poll_s=settings.poll_s,
            consecutive=settings.consecutive_polls,
            workers=settings.concurrency,
            clock=p.clock,
            sleep=p.sleep,
        )
        if not gone.converged:
            raise ProvisionError(f"{len(gone.pending)} QoS requirement reading(s) still answer after the removals")
        again = scan(p, providers)
        left = [qid for qid in again.where if qid not in kept]
        if left:
            raise ProvisionError(f"the scan after the removals still finds {len(left)} QoS requirement(s) not kept")
        reads = {k: reads.get(k, 0) + again.reads.get(k, 0) for k in reads.keys() | again.reads.keys()}
    facts.update(removed=len(added), affected_files=len(affected), reads=reads)
    return facts, affected


Maps = dict[tuple[str, str], tuple[Range, ...]]  # (file id, provider) -> the merged blocks that provider holds


def read_maps(p: Provisioner, files: Iterable[str], providers: Sequence[str]) -> Maps:
    """Each provider's block map of each file, read on that provider, under the provisioner's policy."""
    keys = [(file_id, provider) for file_id in sorted(files) for provider in providers]
    maps = consistency.bounded_map(
        lambda key: tuple(merged(p.blocks(key[0], key[1], retry=p.retry))), keys, p.settings.concurrency
    )
    return dict(zip(keys, maps, strict=True))


def settle(p: Provisioner, files: Mapping[str, int], providers: Sequence[str]) -> dict[str, Any]:
    """Read the affected files' block maps every `SETTLE_POLL_S` until they have stayed the same for
    `SETTLE_STABLE_S`; raise ProvisionError when they are still changing after `SETTLE_BOUND_S` of waiting. The bound
    counts the waits alone, never the reads: a requirement on a large directory makes one reading of every file's map
    on every provider take long, and that is no change."""
    if not files:
        return {"files": 0, "polls": 0}
    last, since, polls, waited = read_maps(p, files, providers), p.clock(), 1, 0.0
    while p.clock() - since < SETTLE_STABLE_S:
        if waited >= SETTLE_BOUND_S:
            raise ProvisionError(
                f"the block maps of {len(files)} file(s) a removed QoS requirement applied to were still changing "
                f"after {SETTLE_BOUND_S:.0f} s of waiting"
            )
        p.sleep(SETTLE_POLL_S)
        waited += SETTLE_POLL_S
        current = read_maps(p, files, providers)
        polls += 1
        if current != last:
            last, since = current, p.clock()
    return {"files": len(files), "polls": polls}


def grown(before: Maps, after: Maps) -> list[tuple[str, str]]:
    """The (file, provider) pairs whose block map holds bytes after that it did not hold before."""
    return [key for key, now in after.items() if held_bytes(merged([*before.get(key, ()), *now])) > _held(before, key)]


def _held(maps: Maps, key: tuple[str, str]) -> int:
    return held_bytes(list(maps.get(key, ())))


def quiet(
    p: Provisioner,
    files: Mapping[str, int],
    providers: Sequence[str],
    occupancy: Callable[[], Mapping[str, int]],
    before: Mapping[str, int],
    *,
    depth: int,
) -> dict[str, Any]:
    """Read the block maps of `files`, wait `quiet_wait_s(depth)`, and read the maps and the occupancy again; raise
    ProvisionError when a provider holds more of a file than before the wait, or a provider other than the data
    provider holds more of the space than at the occupancy stage (`before`). It runs whatever `files` holds: with none
    the occupancy alone is read again, since a replication that has placed nothing yet shows in no block map."""
    wait = quiet_wait_s(depth)
    first = read_maps(p, files, providers)
    p.sleep(wait)
    second = read_maps(p, files, providers)
    sizes = occupancy()
    more = grown(first, second)
    if more:
        where = ", ".join(sorted({p.client.provider_label(provider) for _, provider in more}))
        raise ProvisionError(
            f"{len(more)} block map(s) of files held when the run ended or a removed QoS requirement applied to grew "
            f"after the eviction, on {where}"
        )
    over = [q for q, size in sizes.items() if q != p.settings.holder and size > before.get(q, 0)]
    if over:
        where = ", ".join(p.client.provider_label(q) for q in over)
        raise ProvisionError(f"{where} hold(s) more of the space after the quiet wait than at the occupancy stage")
    physical = {p.client.provider_label(q): n for q, n in sizes.items()}
    return {"files": len(files), "waited_s": wait, "physical_bytes": physical}


__all__ = [
    "BLOCKS_FLUSH_DELAY_S",
    "DIR_STATS_FLUSH_S",
    "SETTLE_BOUND_S",
    "SETTLE_STABLE_S",
    "Found",
    "occupancy_lag_s",
    "quiet",
    "quiet_wait_s",
    "remove",
    "scan",
    "settle",
    "tree",
]
