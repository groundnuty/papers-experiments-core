"""The fake federation's data API with what a complete reset meets: several data spaces, QoS requirements, transfers
that can be cancelled, directory evictions, and concurrent callers.

`FakeReset` extends `fake_data.FakeData` with these provider calls (25.0's REST description is the reference):

  POST   /qos_requirements                     a requirement on a file or a directory (body fileId, expression)
  GET    /qos_requirements/{qid}               the requirement: qosRequirementId, fileId, expression, status
  DELETE /qos_requirements/{qid}               remove it (204; once gone, answered as `missing_answer`)
  GET    /data/{id}/qos/summary                the requirements on the entry and its ancestors, keyed by id
  DELETE /transfers/{tid}                      cancel a job that has not ended (204), or 400 for one that has

and with these changes to the calls it inherits:

- `add_data_space` adds a space with a root of its own, which `GET /spaces/{space}` answers on a provider and at which
  a file's parent links end, and the transfer listing of the space holds only the jobs on its files; `add_file`,
  `id_of`, `held` and `exists` take `space=` (the study space by default);
- a job's record has the `spaceId` of its file's space, and a cancelled job reports `cancelled`, with no start or
  finish time (0) when it was cancelled while still waiting;
- an eviction scheduled on a directory evicts every file under it that the provider holds when it ends, unless
  `evictions_noop_first` makes it one that evicted nothing, as FakeData's file evictions are, or `qos_covered` names a
  file under it, which keeps its blocks; its record counts every file under the directory in `filesProcessed` and
  the files it removed in `filesEvicted`, so a file the provider did not hold, or one cancelled per file, is processed
  and not evicted, with `filesFailed` 0, as op-worker counts them;
- every provider call is answered under one lock, so a bulk operation's threads never interleave inside the fake.

A test bends it through plain attributes: `ignore_cancels` (a cancel answers 204 and the job goes on),
`ignore_qos_deletes` (a removal answers 204 and the requirement stays), `qos_spawns` (adding a requirement starts a
replication of its file into this provider id, as a requirement that selects that provider's storage would),
`qos_hidden_on` (requirement id to the provider ids that have not seen it yet: its summary there leaves it out and
reading it answers as a requirement that is not there, `missing_answer`), `spawn_on_evict` (a provider id: an eviction
that ends starts a replication of the same data into it, a job begun while a reset runs), and `dir_stats_disabled` (a
directory's distribution answers its physical sizes as failed, as a space without directory statistics does).

A directory's distribution answers, per provider, the physical size of what that provider holds under it, as
`distributionPerStorageBackend.{storage}.physicalSize`, with `success` true.
"""

from __future__ import annotations

import json
import re
import threading

import httpx

from expcore_onedata.testing.fake_data import ROOT_ID, FakeData, Node, Transfer


class FakeReset(FakeData):
    """FakeData with several data spaces, QoS requirements, cancels, directory evictions and a lock."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.space_roots: dict[str, str] = {self.SPACE_ID: ROOT_ID}
        self.qos: dict[str, dict] = {}  # requirement id -> its record
        self.ignore_cancels = False
        self.ignore_qos_deletes = False
        self.qos_spawns: str | None = None
        self.qos_hidden_on: dict[str, set[str]] = {}
        self.spawn_on_evict: str | None = None
        self.dir_stats_disabled = False
        self._serve = threading.RLock()
        self._qos_ids = 0

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

    def add_qos(self, path: str, *, space: str | None = None, expression: str = "anyStorage") -> str:
        """Put a requirement on an entry, as an agent's QoS tool would, and return its id."""
        return self._new_qos(self.id_of(path, space), expression)

    def _new_qos(self, file_id: str, expression: str) -> str:
        self._qos_ids += 1
        qid = f"qos-{self._qos_ids:04d}"
        self.qos[qid] = {"qosRequirementId": qid, "fileId": file_id, "expression": expression, "replicasNum": 1}
        if self.qos_spawns is not None and self.nodes[file_id].kind == "REG":
            tid = f"tid-{next(self._ids):04d}"
            now = int(self.clock())
            self.transfers[tid] = Transfer("replication", file_id, self.qos_spawns, scheduled=now, started=now)
        return qid

    # -- answers ---------------------------------------------------------------
    def respond(self, request: httpx.Request) -> httpx.Response:
        """Answer one call at a time, whatever thread sent it."""
        with self._serve:
            return super().respond(request)

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
        if route == "/qos_requirements" and method == "POST":
            sent = json.loads(request.content or b"{}")
            if sent.get("fileId") not in self.nodes or not sent.get("expression"):
                return self._error(400, "badValueNotAllowed")
            return httpx.Response(201, json={"qosRequirementId": self._new_qos(sent["fileId"], sent["expression"])})
        found = re.fullmatch(r"/qos_requirements/([^/]+)", route)
        if found:
            return self._qos_requirement(method, found.group(1), provider)
        found = re.fullmatch(r"/data/([^/]+)/qos/summary", route)
        if found and method == "GET":
            return self._qos_summary(found.group(1), provider)
        found = re.fullmatch(r"/transfers/([^/]+)", route)
        if found and method == "DELETE":
            return self._cancel(found.group(1))
        return super()._provider_route(request, provider, route)

    def _qos_requirement(self, method: str, qid: str, provider: str) -> httpx.Response:
        record = self.qos.get(qid)
        if record is None or provider in self.qos_hidden_on.get(qid, set()):
            return self._answer(self.missing_answer)  # a 404, or 25.0's 400 posix enoent (`answer_missing_as_25`)
        if method == "GET":
            return httpx.Response(200, json={**record, "status": "fulfilled"})
        if method == "DELETE":
            if not self.ignore_qos_deletes:
                del self.qos[qid]
            return httpx.Response(204)
        return self._error(404, "notFound")

    def _qos_summary(self, file_id: str, provider: str) -> httpx.Response:
        if file_id not in self.nodes:
            return self._error(404, "notFound")
        chain, node = set(), self.nodes.get(file_id)
        while node is not None:
            chain.add(node.file_id)
            node = self.nodes.get(node.parent) if node.parent else None
        requirements = {
            qid: "fulfilled"
            for qid, r in self.qos.items()
            if r["fileId"] in chain and provider not in self.qos_hidden_on.get(qid, set())
        }
        return httpx.Response(200, json={"requirements": requirements, "status": "fulfilled"})

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
            body.update(bytesReplicated=0, filesReplicated=0, filesEvicted=0, filesProcessed=0)
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
            files = self._files_under(node.file_id)
            transfer.processed = len(files)
            if self.evictions_noop_first > 0:  # completed, and nothing evicted
                self.evictions_noop_first -= 1
                transfer.noop = True
                return
            keys = [(f, transfer.provider_id) for f in files]
            held = [key for key in keys if self.blocks.get(key) and key not in self.qos_covered]
            transfer.evicted = len(held) if self.evictions_remove_blocks else len(files)  # a lying job claims them all
            if self.evictions_remove_blocks:
                for key in (k for k in keys if k not in self.qos_covered):
                    self.blocks.pop(key, None)
                    self.pending.pop(key, None)
            return
        super()._finish(transfer)

    def _distribution(self, node: Node) -> dict:
        """A file's as FakeData answers it; a directory's, each provider's physical size of what it holds under it."""
        if node.kind != "DIR":
            return super()._distribution(node)
        files = self._files_under(node.file_id)
        per: dict = {}
        for provider in self.providers:
            held = sum(n for f in files for _, n in self.blocks.get((f, provider), []))
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
