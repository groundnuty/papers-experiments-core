"""The fake federation's data API with what a complete reset meets: several data spaces, transfers that can be
cancelled, directory evictions, and concurrent callers. QoS requirements are `FakeData`'s (`add_qos`, `qos`,
`ignore_qos_deletes`, `qos_hidden_on`, `qos_replicates_to`).

`FakeReset` extends `fake_data.FakeData` with this provider call (25.0's REST description is the reference):

  DELETE /transfers/{tid}                      cancel a job that has not ended (204), or 400 for one that has

and with these changes to the calls it inherits:

- `add_data_space` adds a space with a root of its own, which `GET /spaces/{space}` answers on a provider and at which
  a file's parent links end, and the transfer listing of the space holds only the jobs on its files; `add_file`,
  `id_of`, `held`, `exists` and `add_qos` take `space=` (the study space by default);
- a job's record has the `spaceId` of its file's space, and a cancelled job reports `cancelled`, with no start or
  finish time (0) when it was cancelled while still waiting;
- an eviction scheduled on a directory evicts every file under it when it ends, unless `evictions_noop_first` makes
  it one that evicted nothing, as FakeData's file evictions are;
- every provider call is answered under one lock, so a bulk operation's threads never interleave inside the fake.

A test bends it through plain attributes: `ignore_cancels` (a cancel answers 204 and the job goes on),
`spawn_on_evict` (a provider id: an eviction that ends starts a replication of the same data into it, a job begun
while a reset runs), and `dir_stats_disabled` (a directory's distribution answers its physical sizes as failed, as a
space without directory statistics does).

A directory's distribution answers, per provider, the physical size of what that provider holds under it, as
`distributionPerStorageBackend.{storage}.physicalSize`, with `success` true. **That size lags a placement and not an
eviction**, as the build's does (op-worker f118ed9, the module docstring of `expcore_onedata.resetqos` traces it): bytes
that land show in it `blocks_flush_delay_s + dir_stats_flush_s x (L - 1)` seconds later, 10 + 5 x (L - 1) by default,
L being the parts of the file's path below the directory asked (the levels from the file's parent up to it); bytes
that leave show at once. A test sets the two attributes (both 0: no lag). Only changes made once the fake has been
called are dated: what a test puts in place before the first provider call, its setup, is old, and shows at once. A
change is dated on the fake's `clock` by the write to `blocks` (a replication's landing, a hook's assignment) and an
eviction by the removal of its entry; a list of blocks edited in place is not seen.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Callable

import httpx

from expcore_onedata.testing.fake_data import ROOT_ID, FakeData, Node, Transfer


def _held(blocks: list[list[int]] | None) -> int:
    return sum(n for _, n in blocks or [])


class _Placements(dict):
    """The fake's table of blocks, `(file id, provider id)` to `[offset, length]` lists, which dates its changes once
    `armed()` says so: each is kept as `(when, bytes held after it, whether it grew)`, so that `reported` can say how
    many bytes a directory's physical size shows at a later time. A growth shows `lag` seconds after it, a shrink (an
    eviction, `pop` and `del` included) at once. Before the first change of a key the entry is old: it shows as held."""

    def __init__(self, clock: Callable[[], float], armed: Callable[[], bool]) -> None:
        super().__init__()
        self._clock, self._armed = clock, armed
        self._changes: dict[tuple[str, str], list[tuple[float, int, bool]]] = {}

    def _note(self, key: tuple[str, str], now_held: int) -> None:
        if not self._armed():
            return
        before = _held(self.get(key))
        if now_held != before:
            changes = self._changes.setdefault(key, [(-math.inf, before, False)])
            changes.append((self._clock(), now_held, now_held > before))

    def __setitem__(self, key: tuple[str, str], value: list[list[int]]) -> None:
        self._note(key, _held(value))
        super().__setitem__(key, value)

    def __delitem__(self, key: tuple[str, str]) -> None:
        self._note(key, 0)
        super().__delitem__(key)

    def pop(self, key, *default):  # type: ignore[override]
        if key in self:
            self._note(key, 0)
        return super().pop(key, *default)

    def reported(self, key: tuple[str, str], lag: float) -> int:
        """The bytes of `key` a directory shows now, a growth showing `lag` seconds after it was made."""
        now, held = self._clock(), _held(self.get(key))
        for when, after, grew in reversed(self._changes.get(key, [])):
            if when + (lag if grew else 0.0) <= now:
                return min(held, after)
        return held


class FakeReset(FakeData):
    """FakeData with several data spaces, QoS requirements, cancels, directory evictions, a lock, and a directory
    size that lags a placement."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.space_roots: dict[str, str] = {self.SPACE_ID: ROOT_ID}
        self.ignore_cancels = False
        self.spawn_on_evict: str | None = None
        self.dir_stats_disabled = False
        self.blocks_flush_delay_s = 10.0  # the file location's flush after its first change (rel/files/app.config:511)
        self.dir_stats_flush_s = 5.0  # each directory level's collector flush (dir_stats_collector.erl:168)
        self._called = False
        placements = _Placements(lambda: self.clock(), lambda: self._called)
        placements.update(self.blocks)  # unhooked: what is there is old
        self.blocks = placements  # type: ignore[assignment]
        self._serve = threading.RLock()

    # -- setup helpers ---------------------------------------------------------
    def add_data_space(self, space_id: str, name: str, supports: list[str]) -> None:
        """Add a space with a root directory of its own, known to Onezone and supported by these provider ids."""
        self.add_space(space_id, name, supports)
        root = f"root-{space_id}"
        self.nodes[root] = Node(root, "", "DIR", None)
        self.space_roots[space_id] = root

    def _root_of(self, space: str | None) -> str:
        return self.space_roots[space or self.SPACE_ID]

    def space_of(self, file_id: str) -> str | None:
        """The id of the space an entry lies in, or None for one that is not there."""
        node = self.nodes.get(file_id)
        while node is not None and node.parent is not None:
            node = self.nodes.get(node.parent)
        return next((space for space, root in self.space_roots.items() if node and root == node.file_id), None)

    def add_file(self, path: str, content: bytes, *, holder: str | None = None, space: str | None = None) -> str:
        """Create a file at a path of a space (the study space by default); the holder has all its blocks."""
        holder = holder or self.PROVIDER_ID
        parent = self._root_of(space)
        *dirs, name = path.strip("/").split("/")
        for part in dirs:
            parent = self._child(parent, part) or self._create(parent, part, "DIR")
        file_id = self._create(parent, name, "REG")
        self.nodes[file_id].content = bytearray(content)
        if content:
            self.blocks[(file_id, holder)] = [[0, len(content)]]
        return file_id

    def id_of(self, path: str, space: str | None = None) -> str:
        """Return the id of a path of a space; raise KeyError when there is none."""
        node = self._root_of(space)
        for part in path.strip("/").split("/"):
            child = self._child(node, part)
            if child is None:
                raise KeyError(path)
            node = child
        return node

    def held(self, path: str, provider_id: str, space: str | None = None) -> list[list[int]]:
        """Return the blocks a provider holds of a file of a space, merged."""
        file_id = self.id_of(path, space)
        return sorted(self.blocks.get((file_id, provider_id), []))

    def exists(self, path: str, space: str | None = None) -> bool:
        try:
            self.id_of(path, space)
        except KeyError:
            return False
        return True

    def add_qos(self, path: str, *, space: str | None = None, expression: str = "anyStorage") -> str:  # type: ignore[override]
        """Put a requirement on an entry of a space, as an agent's QoS tool would, and return its id."""
        return self._new_qos(self.id_of(path, space), expression)

    # -- answers ---------------------------------------------------------------
    def respond(self, request: httpx.Request) -> httpx.Response:
        """Answer one call at a time, whatever thread sent it. The first call ends the setup: what changes after it is
        dated, for the lag of a directory's size."""
        with self._serve:
            self._called = True
            return super().respond(request)

    def occupancy_lag_s(self, levels: int) -> float:
        """Seconds a directory's physical size lags a placement of a file `levels` path parts below it."""
        return self.blocks_flush_delay_s + self.dir_stats_flush_s * (max(levels, 1) - 1)

    def _space_root(self, space: str) -> httpx.Response:
        if space in self.space_roots and space != self.SPACE_ID:
            return httpx.Response(200, json={"fileId": self.space_roots[space], "name": self.other_spaces[space]})
        return super()._space_root(space)

    def _provider_route(self, request: httpx.Request, provider: str, route: str) -> httpx.Response:
        """The reset's calls first, then everything FakeData answers."""
        method = request.method
        issued = self.issued.get(request.headers.get("X-Auth-Token", ""))
        readonly = issued is not None and any(c.get("type") == "data.readonly" for c in issued["caveats"])
        if self.enforce_readonly and readonly and method in ("POST", "DELETE"):
            return self._error(403, "forbidden")
        found = re.fullmatch(r"/transfers/([^/]+)", route)
        if found and method == "DELETE":
            return self._cancel(found.group(1))
        return super()._provider_route(request, provider, route)

    def _cancel(self, tid: str) -> httpx.Response:
        transfer = self.transfers.get(tid)
        if transfer is None:
            return self._error(404, "notFound")
        if transfer.ended:
            return self._error(400, "badValueNotAllowed")
        if not self.ignore_cancels:
            # A job cancelled while still scheduled is given no start or finish time, so it is listed last in `ended`
            # (op-worker f118ed9, replication_status.erl:264-271); one cancelled once running is stamped.
            if not transfer.waiting:
                transfer.started = transfer.started or transfer.scheduled
                transfer.finished = int(self.clock())
            transfer.ended, transfer.waiting, transfer.cancelled = True, False, True
        return httpx.Response(204)

    def _transfer(self, tid: str, provider: str) -> httpx.Response:
        """FakeData's record, with the space of the job's file and the cancelled state."""
        answer = super()._transfer(tid, provider)
        if answer.status_code != 200:
            return answer
        body = answer.json()
        transfer = self.transfers[tid]
        if "spaceId" not in self.transfer_omit:
            body["spaceId"] = self.space_of(transfer.file_id)
        if getattr(transfer, "cancelled", False):
            body["evictionStatus" if transfer.kind == "eviction" else "replicationStatus"] = "cancelled"
            body.update(bytesReplicated=0, filesReplicated=0, filesEvicted=0)
        return httpx.Response(200, json=body)

    def _list_transfers(self, space: str, query: httpx.QueryParams, provider: str) -> httpx.Response:
        """FakeData's listing, of the jobs on files of that space alone."""
        if space not in self.space_roots:
            return self._error(404, "notFound")
        if query.get("state") not in ("waiting", "ongoing", "ended"):
            return self._error(400, "badValueNotAllowed")
        self.listings += 1
        if self.transfer_list_body is not None:
            return httpx.Response(200, json=self.transfer_list_body)

        def here(tid: str, transfer: Transfer) -> bool:  # a job on a file since deleted is listed in the study space
            found = self.space_of(transfer.file_id)
            return found == space or (found is None and space == self.SPACE_ID)

        return self._transfer_page(query, provider, here)

    def _finish(self, transfer: Transfer) -> None:
        """An eviction of a directory evicts every file under it; anything else ends as FakeData's does."""
        if getattr(transfer, "cancelled", False):
            return
        node = self.nodes.get(transfer.file_id)
        if transfer.kind == "eviction" and self.spawn_on_evict is not None:
            tid = f"tid-{next(self._ids):04d}"
            now = int(self.clock())
            self.transfers[tid] = Transfer(
                "replication", transfer.file_id, self.spawn_on_evict, scheduled=now, started=now
            )
        if transfer.kind == "eviction" and node is not None and node.kind == "DIR":
            if self.evictions_noop_first > 0:  # completed, and nothing evicted
                self.evictions_noop_first -= 1
                transfer.noop = True
                return
            if self.evictions_remove_blocks:
                for file_id in self._files_under(node.file_id):
                    self.blocks.pop((file_id, transfer.provider_id), None)
                    self.pending.pop((file_id, transfer.provider_id), None)
            return
        super()._finish(transfer)

    def _distribution(self, node: Node) -> dict:
        """A file's as FakeData answers it; a directory's, each provider's physical size of what it holds under it."""
        if node.kind != "DIR":
            return super()._distribution(node)
        files = self._files_under(node.file_id)
        top = len(self._ancestry(node.file_id))
        lags = {f: self.occupancy_lag_s(len(self._ancestry(f)) - top) for f in files}
        per: dict = {}
        for provider in self.providers:
            held = sum(self.blocks.reported((f, provider), lags[f]) for f in files)
            backend = (
                {"success": False, "error": {"id": "dirStatsDisabledForSpace"}}
                if self.dir_stats_disabled
                else {"success": True, "physicalSize": held}
            )
            if provider not in self.omit_providers:
                per[provider] = {"success": True, "distributionPerStorageBackend": {"storage-1": backend}}
        return {"type": "DIR", "distributionPerProvider": per}

    def _files_under(self, directory: str) -> list[str]:
        found = []
        for node in [n for n in self.nodes.values() if n.parent == directory]:
            found += self._files_under(node.file_id) if node.kind == "DIR" else [node.file_id]
        return found


__all__ = ["FakeReset"]
