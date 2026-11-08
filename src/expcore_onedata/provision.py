# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/scripted/provision.py, blob 1969c9f03ed263e249b7cba8ec4ee353d329ef03
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Spaces this lane owns, on the federation: create, upload, verify, replicate, evict, tear down.

Production safety: only `study_space_prefix_2_*` and `study_space_prefix_4_*` spaces are created or changed (refused otherwise); every
change goes to the change log next to the results. Never restarts or reconfigures anything. A transfer's reported status
is never taken as evidence (dataset_77 onedata-issues 0003, 0006): placement always comes from the block map.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

import requests

from . import placement as P

OWN_PREFIXES = ("study_space_prefix_2_", "study_space_prefix_4_", "study_space_prefix_6_")
CHUNK = 64 << 20
# A stalled block (science, #34, 2 Nov 11:37Z). fr's op_worker can stop taking a write for 60 to 300 s, on either path. `requests` applies its connect timeout to
# writes, so the pair below sets both. The write's limit covers the whole 64 MiB block, so this needs a path well above 9 Mbit/s per stream: fr's is, and the WAN
# anchor's (2 to 15 Mbit/s) is not, so it is M+'s setup that asks for this (`stall_retry`).
BLOCK_TRIES = 5
BLOCK_STALL_S = 60.0
BLOCK_READ_S = 300.0
S1_DIR, S2_DIR, S2_NAME = "dataset_15", "dataset_16", "dataset-2.nc"
TERMINAL = ("completed", "failed", "cancelled", "skipped")


def check_own(name: str) -> None:
    if not name.startswith(OWN_PREFIXES):
        raise ValueError(f"refusing to act on space {name!r}: this lane owns only {OWN_PREFIXES}")


def check_space(o, provider: str, space_id: str, root_id: str, recorded: dict[str, str]) -> str:
    """Before acting on a space named by a file: its live name is one of this lane's, its root is the one recorded, and
    each recorded id (relative path -> id) is what a listing down from that root finds. Documented calls only: a
    file's attributes carry no space id (checked on fr), and the id's own encoding is the provider's internal format."""
    name = o.oz("GET", f"/spaces/{space_id}").json().get("name", "")
    check_own(name)
    if o.space_root(provider, space_id) != root_id:
        raise ValueError(f"{name}: the recorded root is not this space's root")
    for rel, fid in recorded.items():
        node = root_id
        for part in rel.strip("/").split("/"):
            entry = children(o, provider, node).get(part)
            if entry is None:
                raise ValueError(f"{name}: no {rel!r} under the space root")
            node = entry["fileId"]
        if node != fid:
            raise ValueError(f"{name}: the recorded id of {rel!r} is not the one in the space")
    return name


def read_manifest(path: Path | str) -> dict[str, str]:
    """`<sha256>  <name>` lines (#3): name -> digest."""
    out = {}
    for line in Path(path).read_text().splitlines():
        digest, name = line.split("  ", 1)
        out[name] = digest
    return out


def children(o, provider: str, dir_id: str) -> dict[str, dict]:
    """All entries of a directory, following pagination to the end."""
    out: dict[str, dict] = {}
    token = None
    while True:
        # Onedata 25 names attributes in snake case (`fileId` is refused with 400 badValueNotAllowed, checked on fr)
        params = {"limit": 1000, "attribute": ["name", "file_id", "size", "type"]}
        if token:
            params["token"] = token
        j = o.op(provider, "GET", f"/data/{dir_id}/children", params=params).json()
        for c in j["children"]:
            fid = c.get("file_id") or c.get("fileId")
            if not fid or c.get("size") is None:
                raise RuntimeError(f"listing entry without file id or size: {sorted(c)}")
            out[c["name"]] = {"name": c["name"], "fileId": fid, "size": int(c["size"]), "type": c.get("type")}
        if j.get("isLast", True):
            return out
        token = j.get("nextPageToken")
        if not token:
            raise RuntimeError("listing: not the last page but no nextPageToken")


class BlockStalls:
    """What the upload hit, for the setup's record. Several files upload at once."""

    def __init__(self):
        self._mu, self.events = threading.Lock(), []

    def add(self, name: str, off: int, seconds: float, what: str) -> None:
        with self._mu:
            self.events.append({"file": name, "offset": off, "seconds": round(seconds, 1), "what": what})

    def summary(self) -> dict:
        with self._mu:
            return {"stalled_blocks": len({(e["file"], e["offset"]) for e in self.events}), "stalls": len(self.events),
                    "stall_seconds": round(sum(e["seconds"] for e in self.events), 1), "stall_events": self.events[:50]}


def _put_block(o, provider: str, fid: str, name: str, off: int, buf: bytes, stalls: BlockStalls) -> None:
    """One block. A stalled one goes again on a new connection, up to BLOCK_TRIES: the same bytes at the same offset, so a late write of the stalled one changes
    nothing, and the hash check after the upload still decides whether the file is right. One that does not recover stops the setup."""
    for attempt in range(1, BLOCK_TRIES + 1):
        t0 = time.time()
        try:
            o.op(provider, "PUT", f"/data/{fid}/content", params={"offset": off}, data=buf, timeout=(BLOCK_STALL_S, BLOCK_READ_S),
                 headers={"content-type": "application/octet-stream"}, **({"new_connection": True} if attempt > 1 else {}))
            return
        except (requests.ConnectionError, requests.Timeout) as e:
            stalls.add(name, off, time.time() - t0, type(e).__name__)
    raise RuntimeError(f"{name}: the block at offset {off} stalled {BLOCK_TRIES} times; it did not recover on a new connection")


def put_file(o, provider: str, parent: str, path: Path, existing: dict[str, dict], stalls: BlockStalls | None = None) -> bool:
    """One file, in 64 MiB blocks, as #3 uploaded them; a file already there with the right size is left alone. With `stalls`, a stalled block goes again."""
    size = path.stat().st_size
    cur = existing.get(path.name)
    if cur is not None:
        if int(cur["size"]) == size:
            return False
        raise RuntimeError(f"{path.name} exists with another size; refusing to overwrite")
    fid = o.op(provider, "POST", f"/data/{parent}/children", params={"name": path.name, "type": "REG"}).json()["fileId"]
    with open(path, "rb") as f:
        off = 0
        while off < size:
            buf = f.read(CHUNK)
            if stalls is not None:
                _put_block(o, provider, fid, path.name, off, buf, stalls)
            else:
                o.op(provider, "PUT", f"/data/{fid}/content", params={"offset": off}, data=buf, timeout=900,
                     headers={"content-type": "application/octet-stream"})
            off += len(buf)
    got = int(o.op(provider, "GET", f"/data/{fid}").json()["size"])
    if got != size:
        raise RuntimeError(f"{path.name}: size after upload {got} != {size}")
    return True


def upload_dataset(o, provider: str, root_id: str, s1_dir: Path, s2_file: Path, *, workers: int = 8, stall_retry: bool = False) -> dict:
    """s1z/ and s2z/dataset-2.nc under the space root. Resumes: present files of the right size are skipped."""
    top = children(o, provider, root_id)
    def mkdir(name: str) -> str:
        if name in top:
            return top[name]["fileId"]
        return o.op(provider, "POST", f"/data/{root_id}/children", params={"name": name, "type": "DIR"}).json()["fileId"]
    task_a, task_b = mkdir(S1_DIR), mkdir(S2_DIR)
    ex1, ex2 = children(o, provider, task_a), children(o, provider, task_b)
    files = sorted(Path(s1_dir).glob("*.nc"))
    t0 = time.time()
    stalls = BlockStalls() if stall_retry else None
    with cf.ThreadPoolExecutor(workers) as pool:
        wrote = sum(pool.map(lambda p: put_file(o, provider, task_a, p, ex1, stalls), files))
    task_s = time.time()
    w2 = put_file(o, provider, task_b, Path(s2_file), ex2, stalls)
    return {"s1_written": wrote, "s1_present": len(files) - wrote, "s1_seconds": round(task_s - t0, 1),
            "s2_written": w2, "s2_seconds": round(time.time() - task_s, 1), "s1_dir_id": task_a, "s2_dir_id": task_b, **(stalls.summary() if stalls else {})}


def sha_remote(o, provider: str, fid: str, size: int) -> str:
    h = hashlib.sha256()
    for off in range(0, size, CHUNK):
        r = o.op(provider, "GET", f"/data/{fid}/content", timeout=900, expect=(200, 206),
                 headers={"Range": f"bytes={off}-{min(off + CHUNK, size) - 1}"})
        h.update(r.content)
    return h.hexdigest()


def verify_files(o, provider: str, entries: dict[str, dict], manifest: dict[str, str], *, workers: int = 8,
                 retries: int = 2) -> dict:
    """Download and hash each listed file from the provider, against #3's digests. A retried read is counted."""
    retried = [0]

    def one(name: str) -> tuple[str, str]:
        c = entries[name]
        for attempt in range(retries + 1):
            try:
                return name, sha_remote(o, provider, c["fileId"], int(c["size"]))
            except Exception:  # noqa: BLE001 - a dropped read is retried and counted; a third failure propagates
                retried[0] += 1
                if attempt == retries:
                    raise
                time.sleep(2)
        raise AssertionError

    names = sorted(entries)
    t0 = time.time()
    with cf.ThreadPoolExecutor(workers) as pool:
        got = dict(pool.map(one, names))
    bad = sorted(n for n in names if got[n] != manifest.get(n))
    return {"files_checked": len(names), "mismatches": bad, "ok": not bad and bool(names), "seconds": round(time.time() - t0, 1),
            "retried_reads": retried[0]}


# ------------------------------------------------------------------------------------- transfers
def schedule_replication(o, on_provider: str, file_id: str, to_provider_id: str) -> str:
    r = o.op(on_provider, "POST", "/transfers", json={"type": "replication", "replicatingProviderId": to_provider_id,
                                                      "dataSourceType": "file", "fileId": file_id}, expect=(201,))
    return r.json()["transferId"]


def schedule_eviction(o, on_provider: str, file_id: str, from_provider_id: str, *, holder_id: str) -> str:
    """Evict from `from_provider_id`. The provider holding the source data must never be the target."""
    if from_provider_id == holder_id:
        raise ValueError("refusing to evict the data provider's own copy")
    r = o.op(on_provider, "POST", "/transfers", json={"type": "eviction", "evictingProviderId": from_provider_id,
                                                      "dataSourceType": "file", "fileId": file_id}, expect=(201,))
    return r.json()["transferId"]


def transfer_state(j: dict) -> str:
    return j.get("transferStatus") or j.get("replicationStatus") or j.get("evictionStatus") or "unknown"


def wait_transfers(o, on_provider: str, tids: Iterable[str], *, timeout_s: float, poll_s: float = 2.0,
                   clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Poll until each transfer reports a terminal state, or `timeout_s`. A report only: placement is checked apart."""
    pending = set(tids)
    final: dict[str, dict] = {}
    t0 = clock()
    while pending and clock() - t0 < timeout_s:
        for tid in sorted(pending):
            j = o.op(on_provider, "GET", f"/transfers/{tid}").json()
            if transfer_state(j) in TERMINAL:
                final[tid] = j
                pending.discard(tid)
        if pending:
            sleep(poll_s)
    states: dict[str, int] = {}
    for j in final.values():
        states[transfer_state(j)] = states.get(transfer_state(j), 0) + 1
    return {"seconds": round(clock() - t0, 3), "states": states, "timed_out": sorted(pending),
            "bytes_replicated": sum(int(j.get("bytesReplicated") or 0) for j in final.values()),
            "files_replicated": sum(int(j.get("filesReplicated") or 0) for j in final.values()),
            "files_evicted": sum(int(j.get("filesEvicted") or 0) for j in final.values()),
            "files_failed": sum(int(j.get("filesFailed") or 0) for j in final.values())}


def blocks_of(o, reader: str, file_id: str, provider_id: str) -> list[tuple[int, int]]:
    return P.provider_blocks(o.op(reader, "GET", f"/data/{file_id}/distribution").json(), provider_id)


def placement_report(o, reader: str, provider_id: str, wanted: dict[str, list[tuple[int, int]]],
                     *, workers: int = 8) -> dict:
    """For file id -> ranges that must be held by `provider_id`: what is missing, and how much it holds of them."""
    def one(item):
        fid, ranges = item
        blocks = blocks_of(o, reader, fid, provider_id)
        return fid, P.held_bytes(blocks), P.uncovered(blocks, ranges)

    with cf.ThreadPoolExecutor(workers) as pool:
        rows = list(pool.map(one, wanted.items()))
    short = {fid: gaps for fid, _, gaps in rows if gaps}
    return {"files": len(rows), "held_bytes": sum(h for _, h, _ in rows), "files_short": len(short),
            "bytes_short": sum(ln for gaps in short.values() for _, ln in gaps),
            "short_file_ids": sorted(short)[:50], "fully_placed": not short and bool(rows)}


def holds_nothing(o, reader: str, provider_id: str, file_ids: Iterable[str], *, workers: int = 8) -> dict:
    """The forbidden check's other half: the agent-site provider holds no block of these files."""
    ids = list(file_ids)

    def one(fid: str) -> int:
        return P.held_bytes(blocks_of(o, reader, fid, provider_id))

    with cf.ThreadPoolExecutor(workers) as pool:
        held = list(pool.map(one, ids))
    return {"files": len(ids), "held_bytes": sum(held), "files_holding": sum(1 for h in held if h), "empty": not any(held) and bool(ids)}


# ------------------------------------------------------------------------------------- space lifecycle
class AdminOD:
    """`od.OD` that re-mints its admin token before it expires: M+ campaigns outlast one token. Host side only."""

    def __init__(self, od_module, refresh_s: float = 3 * 3600):
        self._od, self._refresh = od_module, refresh_s
        self._o = od_module.OD()
        self._born = time.time()

    def __getattr__(self, name):
        if time.time() - self._born > self._refresh:
            self._o.token = self._od.mint_token()
            self._born = time.time()
        return getattr(self._o, name)


def create_space(o, od_safe, name: str, providers: list[str], size_bytes: int) -> str:
    """A new space of this lane, supported by exactly `providers`. Every step goes to the change log (day0's `od`)."""
    check_own(name)
    sid = od_safe.create_space_owned(o, name)
    try:
        for p in providers:
            o.support(sid, p, size_bytes)
            o.wait_supported(sid, p)
    except BaseException:
        # A space this process created and never asked any provider to support holds nothing anywhere: remove it, so a failure before the first support
        # request does not leave an empty space whose id the caller never got. One whose support request was sent (even if it failed) is NOT touched
        # here: it is refused by the teardown rule and stays for the operator; the space id is in the change log.
        never = getattr(o, "never_supported", None)
        if callable(never) and never(sid) is True:
            ok, res = od_safe.safe_teardown(o, sid, providers)
            print(f"setup failed before any support request; space {sid} ({name}) removed: ok={ok} {res}", file=sys.stderr, flush=True)
        raise
    return sid


def space_root(o, provider: str, sid: str, *, tries: int = 30, wait_s: float = 2.0) -> str:
    """Support is eventually consistent (dataset_77 0004): the root can answer late on a fresh space."""
    for attempt in range(tries):
        try:
            return o.space_root(provider, sid)
        except Exception:  # noqa: BLE001 - retried; the last failure propagates
            if attempt == tries - 1:
                raise
            time.sleep(wait_s)
    raise AssertionError


def teardown(o, od_safe, sid: str, providers: list[str]) -> dict:
    """Tear the space down by the operator's rule (2 Nov): `safe_teardown` deletes its files through Onedata and shows them gone (the root lists
    empty, `/volumes/posix/<spaceId>` empty or missing on every supporting provider) before it revokes a support or deletes the space. A space
    with files left, one already deleted, or one whose disk cannot be checked comes back `ok: False` with `results["refusal"]` (space id, provider,
    size seen) and nothing revoked or deleted; the caller must not read it as torn down."""
    r = o.oz("GET", f"/spaces/{sid}", expect=None)
    name = r.json().get("name", "") if r.status_code == 200 else ""       # not 200: the guard in safe_teardown says why, and deletes nothing
    if name:
        check_own(name)
    ok, res = od_safe.safe_teardown(o, sid, providers)
    return {"ok": ok, "results": res, "name": name}
