# Provenance: new
"""The reset's QoS requirements: found, and the files the run's own touched settled and kept quiet (O33).

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
- `listing`: on **every provider that supports the space**, one recursive listing of the space root, `GET
  /data/{root}/files` with `attributes` `fileId`, `path`, `type` and `effQosInheritancePath`,
  `include_directories=true` and `limit`, its pages followed by `token` until `nextPageToken` is null
  (`basic_file_operations_rest_routes.erl:59-70`; the parameters, query or JSON body,
  `file_middleware_get_handler.erl:124-138`, `rest_handler.erl:313-325`; the answer, `files`, `inaccessiblePaths` and
  `nextPageToken`, `file_rest_translator.erl:92-97`, with no `isLast` whatever the REST description says; `path` is
  relative to the root, itself `"."`, `recursive_listing.erl:486-498,576-584`; the trash is never listed,
  `trash_dir.erl:98-101`). A listing that could not enter a path (`inaccessiblePaths`) is incomplete and fails closed.
  An entry whose `effQosInheritancePath` is `direct` or `directAndAncestor` has a requirement on itself
  (`src/modules/datastore/models/qos/file_qos.erl:262-291`); only those entries' summaries are read, each on the
  provider whose listing marked the entry, and each requirement's record then names its entry (`fileId`,
  `qos_middleware_plugin.erl:241-245,300-312`), read on the provider whose summary showed it. **Every provider is
  listed because a requirement is visible at once only on the provider it was added through:** an agent that adds one
  through one provider just before the reset leaves the others without it until dbsync delivers it, so a listing of the
  data provider alone can find none, and the removal and the second scan are then skipped. The entries of the
  listings are united by id. Two providers can mark one entry and hold different requirements on it, so its summary is
  read on each provider that marks it. For N supporting providers the scan makes, besides each provider's read of the
  space root, N x ceil(E / `LISTING_PAGE`) page reads for E entries, one summary per entry per provider that marks it
  (a handful), and one record per requirement; the record gives `listings`, `listing_pages`, `summaries` and `records`.
  **A fact it rests on is not verified live:** that the listing takes `include_directories` (the code does,
  `file_middleware_get_handler.erl:131,404`; the REST description omits it). The gated live tests in
  `tests/live/test_contract.py` (O33) check it, and also that a requirement added through one provider is found
  through another once dbsync settles (each provider's QoS view is built from the synced requirement documents,
  `src/modules/dbsync/dbsync_events.erl:129`, `src/modules/qos/qos_logic.erl:65-70`), which the reset itself no longer
  needs to find a requirement; until the first has passed on the federation, `summaries` stays the default.

**The run's own** are removed by `resetremove.py`: each waited on until it is fulfilled or impossible on every
provider, deleted, and its deletion waited on until every provider has processed it. The regular files at or under a
removed requirement's entry are the affected files.

**Settled, then quiet.** A QoS replication has no transfer to wait for, and a cancel can still place the blocks it had
in flight. So after the removal the affected files' block maps, on every provider, must stay the same for
`SETTLE_STABLE_S`, read every `SETTLE_POLL_S`, within `SETTLE_BOUND_S`, or the reset fails. After the eviction and the
occupancy the quiet stage reads, at one instant T, the block maps of the files the eviction plan evicted, on the
providers it evicted them from, and of the files a removed requirement applied to, on every provider. Each may hold no
more than it should: nothing where the eviction verified it empty, what the settle saw elsewhere. Then it waits
`quiet_wait_s` from T and reads the occupancy: no provider other than the data provider may hold more of the space than
at the occupancy stage. That check does not wait for a requirement to be found: an agent can add one and remove it
before the run ends, and a cancel that missed leaves one file replicating with no requirement for the reset to see
(the residual, `resetremove.py`), so it runs unless the caller knows that no run since the last verified reset had the
right to manage QoS, and covers the files the eviction plan evicts as well (`resetheld.py`), never a read of the whole
space.

**What the quiet stage proves, exactly: no replica of the plan's files, and no occupancy growth, had landed by T.** T is
the instant the last block map was read. The block maps decide for the plan's files (a file's distribution shows its
provider's in-memory location at once), read against what the eviction and the settle left, so a replica that landed
between the eviction's verification and its map's reading is in the map. The occupancy, read at T + the lag + a margin,
shows every placement that landed by T, whichever file it is on, plan or not, since a directory's physical size lags a
placement by at most that lag; so growth in it is a landing before T, one on a file after its map was read included,
and a zero there is never taken alone as nothing having landed, because it lags. **It does not prove that nothing lands
after T.** A placement after T may or may not show in the occupancy read, so a replication still running at T can place
its bytes later and the stage still passes; nothing read at the end of a wait can exclude it. The occupancy lags a
placement:

- a placement's size change is held until the file location's flush, `blocks_flush_delay` after its first change
  (10 s, `rel/files/app.config:511`; `fslogic_cache.erl:524-530,706-715`), and only then reported to the parent
  directory's statistics (`dir_size_stats.erl:218-229`);
- each directory above the parent gets it when the parent's collector flushes, 5 s after its first unflushed change
  (`FLUSH_INTERVAL_MILLIS`, `dir_stats_collector.erl:168,905-918,1037-1057`);
- so the space root's physical size reflects a placement at most 10 + 5 x (L - 1) s later, L being the levels from the
  file's parent up to the space root, the parts of the file's path (`occupancy_lag_s`). An eviction flushes at once
  (`fslogic_location_cache.erl:345-354`) and lags only the 5 s per level above the parent.

The quiet wait is that lag for the deepest file it must cover plus `QUIET_MARGIN_S` (`quiet_wait_s`): the deepest
regular file of the space, as the scan saw it, since a replication nothing names could reach any file, or the deepest
affected file when the caller knows the run could set no requirement. So the occupancy is read when every placement
that landed by T shows in it. Another study measured 9.5 s for a directory after a placement and 0.05 s after an
eviction, which this fits. A space with directory statistics off answers no physical size at all, and the occupancy
stage already fails on it.
"""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from expcore_onedata import consistency
from expcore_onedata.placement import Range, held_bytes, merged
from expcore_onedata.provision import Listings, Provisioner, ProvisionError
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
QUIET_MARGIN_S = 5.0  # seconds added to the lag: one collector flush, for what the bound does not count (an assumption)


def occupancy_lag_s(depth: int) -> float:
    """The longest the space root's physical size lags a placement on a file `depth` path parts below the root."""
    return BLOCKS_FLUSH_DELAY_S + DIR_STATS_FLUSH_S * (max(depth, 1) - 1)


def quiet_wait_s(depth: int) -> float:
    """The quiet stage's wait from T to the occupancy read, for files at most `depth` path parts below the root: the
    lag, by which every placement that landed by T shows, and `QUIET_MARGIN_S`."""
    return occupancy_lag_s(depth) + QUIET_MARGIN_S


@dataclass(frozen=True)
class Found:
    """What a scan found: each requirement with the provider that showed it first, the regular files each applies to,
    each by its id with its depth (the parts of its path below the root), how many reads of each kind it made, and the
    depth of the deepest regular file of the space it saw (0 when it saw none), which costs no read of its own."""

    where: dict[str, str]
    files: dict[str, dict[str, int]]
    reads: dict[str, int] = field(default_factory=dict)
    deepest: int = 0


def _summary(p: Provisioner, file_id: str, provider: str) -> set[str]:
    """The ids of the QoS requirements on an entry and its ancestors, from its summary on one provider."""
    return requirements_of(p.client.get(f"/data/{file_id}/qos/summary", provider=provider, retry=p.retry))


def requirements_of(answer: Any) -> set[str]:
    """The requirement ids a summary answer holds; refuse one not in its shape, or an id that is not plain."""
    requirements = answer.get("requirements") if isinstance(answer, dict) else None
    if not isinstance(requirements, Mapping) or not all(isinstance(q, str) for q in requirements):
        raise Unreadable("a QoS summary holds no table of requirements keyed by id")
    if not all(plain_id(q) for q in requirements):  # an id becomes a path segment of the next calls
        raise Unreadable("a QoS summary holds a requirement id that is not one plain path segment")
    return set(requirements)


def plain_id(value: str) -> bool:
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
    read, deepest = 0, 0
    for provider in providers:
        root, leaves = tree(p, provider)
        deepest = max([deepest, *(depth for _, kind, depth in leaves if kind != "DIR")])
        entries = [root, *(file_id for file_id, _, _ in leaves)]
        summaries = consistency.bounded_map(lambda f, q=provider: _summary(p, f, q), entries, p.settings.concurrency)
        for qid in sorted(set().union(*summaries)):
            where.setdefault(qid, provider)
        for (file_id, kind, depth), held in zip(leaves, summaries[1:], strict=True):
            for qid in held if kind != "DIR" else ():
                files.setdefault(qid, {})[file_id] = depth
        read += len(entries)
    return Found(where, {q: files.get(q, {}) for q in where}, {"summaries": read}, deepest)


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
        if not isinstance(file_id, str) or not plain_id(file_id) or not isinstance(path, str) or not path:
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


def by_listing(p: Provisioner, providers: Sequence[str]) -> Found:
    """Every requirement any of `providers` shows, from the recursive listing of each, the summaries of the entries a
    requirement is on (each read on the provider whose listing marked the entry), and each requirement's record (read
    on the provider whose summary showed it first).

    A requirement is at once visible on the provider it was added through, and reaches the others through dbsync, so
    a listing of one provider can miss one the reset needs to remove. The entries of the listings are united, by id:
    an entry a provider marks `direct` or `directAndAncestor` has its summary read on that provider, and on each other
    provider that marks it so, since two providers can hold different requirements on one entry. The calls it makes:
    one listing of the space root per provider (a read of its root and `listing_pages` pages of `LISTING_PAGE`
    entries), one summary per entry per provider that marks it, one record per requirement."""
    listings = [(provider, *listed(p, provider)) for provider in providers]
    entries: dict[str, _Listed] = {}
    hits: list[tuple[str, str]] = []  # (entry id, the provider that marks it as having a requirement on itself)
    for provider, found, _ in listings:
        for entry in found.values():
            entries.setdefault(entry.file_id, entry)
            if entry.inheritance in DIRECT:
                hits.append((entry.file_id, provider))
    summaries = consistency.bounded_map(lambda hit: _summary(p, hit[0], hit[1]), hits, p.settings.concurrency)
    where: dict[str, str] = {}
    for (_, provider), held in zip(hits, summaries, strict=True):
        for qid in sorted(held):
            where.setdefault(qid, provider)
    qids = sorted(where)
    on = consistency.bounded_map(lambda q: _requirement_entry(p, q, where[q]), qids, p.settings.concurrency)
    files: dict[str, dict[str, int]] = {}
    for qid, entry_id in zip(qids, on, strict=True):
        top = entries.get(entry_id)
        if top is None:
            raise Unreadable("a QoS requirement is on an entry the recursive listing does not hold")
        under = (e for e in entries.values() if not e.directory and _under(e.path, top.path))
        files[qid] = {e.file_id: len(e.path.split("/")) for e in under}
    reads = {
        "listings": len(listings),
        "listing_pages": sum(pages for *_, pages in listings),
        "summaries": len(hits),
        "records": len(qids),
    }
    deepest = max((len(e.path.split("/")) for e in entries.values() if not e.directory), default=0)
    return Found(where, files, reads, deepest)


def scan(p: Provisioner, providers: Sequence[str]) -> Found:
    """Find every requirement of the space, on every provider that supports it, as `[placement] qos_scan` says."""
    if p.settings.qos_scan == "listing":
        return by_listing(p, providers)
    return by_summaries(p, providers)


Pair = tuple[str, str]  # (file id, provider)
Maps = dict[Pair, tuple[Range, ...]]  # (file id, provider) -> the merged blocks that provider holds


def read_pairs(p: Provisioner, pairs: Iterable[Pair]) -> Maps:
    """The block map of each (file, provider) pair, read on that provider, under the provisioner's policy."""
    keys = list(dict.fromkeys(pairs))
    maps = consistency.bounded_map(
        lambda key: tuple(merged(p.blocks(key[0], key[1], retry=p.retry))), keys, p.settings.concurrency
    )
    return dict(zip(keys, maps, strict=True))


def read_maps(p: Provisioner, files: Iterable[str], providers: Sequence[str]) -> Maps:
    """Each provider's block map of each file."""
    return read_pairs(p, [(file_id, provider) for file_id in sorted(files) for provider in providers])


def settle(p: Provisioner, files: Mapping[str, int], providers: Sequence[str]) -> tuple[dict[str, Any], Maps]:
    """Read the affected files' block maps every `SETTLE_POLL_S` until they have stayed the same for
    `SETTLE_STABLE_S`; raise ProvisionError when they are still changing after `SETTLE_BOUND_S` of waiting. Return the
    stage's facts and the maps it ended on, which the quiet stage holds the files to. The bound counts the waits alone,
    never the reads: a requirement on a large directory makes one reading of every file's map on every provider take
    long, and that is no change."""
    if not files:
        return {"files": 0, "polls": 0}, {}
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
    return {"files": len(files), "polls": polls}, last


def grown(before: Maps, after: Maps) -> list[Pair]:
    """The (file, provider) pairs whose block map holds bytes after that it did not hold before."""
    return [key for key, now in after.items() if held_bytes(merged([*before.get(key, ()), *now])) > _held(before, key)]


def _held(maps: Maps, key: Pair) -> int:
    return held_bytes(list(maps.get(key, ())))


def quiet(
    p: Provisioner,
    expected: Maps,
    occupancy: Callable[[], Mapping[str, int]],
    before: Mapping[str, int],
    *,
    depth: int,
) -> dict[str, Any]:
    """Read the block map of each (file, provider) pair of `expected`, wait `quiet_wait_s(depth)` from T, the instant
    the last was read, and read the occupancy. Raise ProvisionError when a map holds more than `expected` says it should
    (nothing, for a pair the eviction emptied; what the settle saw, for a file a removed requirement applied to), or
    when a provider other than the data provider holds more of the space than at the occupancy stage (`before`). The
    occupancy is read whatever `expected` holds, none included: a replication on a file nothing here names shows in no
    block map read, and only the occupancy shows it.

    What this proves, exactly: no replica of the plan's files, and no occupancy growth, had landed by T. The occupancy
    read at T + the lag + a margin shows every placement that landed by T. A landing after T is not proved absent: it
    may not show in the occupancy yet, and no map is read again."""
    maps = read_pairs(p, expected)
    more = grown(expected, maps)
    if more:
        where = ", ".join(sorted({p.client.provider_label(provider) for _, provider in more}))
        raise ProvisionError(
            f"{len(more)} block map(s) of files the eviction plan covered or a removed QoS requirement applied to "
            f"hold more than the eviction or the settle left them, on {where}: a replica had landed by the quiet stage"
        )
    wait = quiet_wait_s(depth)
    p.sleep(wait)
    sizes = occupancy()
    over = [q for q, size in sizes.items() if q != p.settings.holder and size > before.get(q, 0)]
    if over:
        where = ", ".join(p.client.provider_label(q) for q in over)
        raise ProvisionError(f"{where} hold(s) more of the space after the quiet wait than at the occupancy stage")
    physical = {p.client.provider_label(q): n for q, n in sizes.items()}
    files = {file_id for file_id, _ in expected}
    return {"files": len(files), "block_maps": len(maps), "waited_s": wait, "physical_bytes": physical}


__all__ = [
    "BLOCKS_FLUSH_DELAY_S",
    "DIR_STATS_FLUSH_S",
    "QUIET_MARGIN_S",
    "SETTLE_BOUND_S",
    "SETTLE_STABLE_S",
    "Found",
    "occupancy_lag_s",
    "plain_id",
    "quiet",
    "quiet_wait_s",
    "requirements_of",
    "scan",
    "settle",
    "tree",
]
