"""The fake federation with its providers' data API: files, block maps, range reads, transfers and their delays.

`FakeData` adds to `FakeFederation` the calls of `/api/v3/oneprovider`, chosen by the request's host:

  GET    /spaces/{space}                        the root directory of the space (ENOENT for `root_enoent_polls` calls)
  GET    /data/{id}                             attributes, `parent_id` among them; asked for a path, 400 as in 25.0
  GET    /data/{id}/children                    paged by `limit` and `token`
  POST   /data/{id}/children?name&type          create a file or a directory
  PUT    /data/{id}/content?offset              write bytes; the writing provider holds them
  GET    /data/{id}/content  (Range)            206; a provider lacking the bytes fetches them on the fly
  GET    /data/{id}/distribution                the block map of every provider
  DELETE /data/{id}                             remove a file or a directory with what is under it
  POST   /transfers, GET /transfers/{id}        replication, eviction and migration jobs
  GET    /spaces/{space}/transfers              the ids of the space's jobs in one state, paged by `limit`, `page_token`

and, from Onezone, `GET /spaces/{space}` (the live name). Time is counted in calls, never in seconds, so a test never
waits. A test bends the fake through plain attributes:

- `fetch_lag`: polls of the file's block map before bytes fetched by a range read or a replication show in it;
- `onthefly_places`: False makes a range read answer without ever adding a block (a provider that does not cache);
- `transfers_place_blocks`: False makes a replication end as `completed` with no block placed (the O01 fake);
- `evictions_remove_blocks`: False makes an eviction end as `completed` and leave the blocks; `evictions_noop_first`
  does that, with `filesEvicted` 0, for the next N evictions only;
- `onthefly_unit`: a range read through a provider lacking the bytes places at least this many from its offset;
- `transfer_flip_after`: polls of a transfer before it ends; `transfer_visible_after`: polls before it is found at all,
  each answered `transfer_unseen_answer` (a 404 by default); a fresh space root answers `root_enoent_answer` (a 404 of
  posix enoent by default) for `root_enoent_polls` reads;
- `resets`: a list of `[substring, count]` or `[substring, count, provider id]`: the next `count` provider calls whose
  route contains the substring (to that provider, when named) fail with a connection reset;
- `fail_deletes`, `ignore_deletes`: every DELETE answers 500, or answers 204 and removes nothing;
- `enforce_readonly`: False lets a token with the read-only caveat write (a broken enforcement); `readonly_answer` is
  how such a write is refused, 403 by default, `(400, "posix", "eacces")` as Onedata 25.0 answers it;
- `unseen`, `unseen_after_create`: a provider that sees a new file only after so many reads of it, each answered
  `unseen_answer` (a 404 by default), as a second provider of 25.0 does for some seconds after a write elsewhere;
- `missing_answer`: how a file or directory that is not there is answered, a 404 by default; `answer_missing_as_25`
  makes it 400 posix enoent and keeps a deleted entry's `parent_id` readable, as 25.0 does;
- a content write (`PUT /data/{id}/content`) without `Content-Type: application/octet-stream` is a 415, as in 25.0;
- `statuses`: a list of `[substring, count, status]` likewise answering with that HTTP status.
- `content_type`: the Content-Type of a file's content, read whole or by range (None: the header is left out).
- `clock`: the epoch seconds a job's schedule, start and finish times are taken from (a test binds its own clock);
- `transfer_omit`: field names a job's record leaves out; `transfer_list_body`: an answer that replaces the listing's;
- `provider_view`: `(job id, provider id)` to what that provider says of the job where it differs: `state` (the list
  it is in, and its record's status) and record fields such as `bytesReplicated`;
- `listing_lag`: job id to how many listing answers, counted from the first, leave the job out (its record is read
  at once, as a real provider's is while its listing lags);
- every data-API answer has a `Date` header of `clock` moved by `provider_skew` (provider id to seconds ahead,
  negative when behind); `date_override` (provider id to a header value, or None for none) replaces it;

`add_transfer` makes a job that no request to `/transfers` made, as a QoS requirement or another client does, with the
times and the state a test gives it.

`provider_calls` lists `(provider id, method, route)` of every provider call, so a test can assert what was sent where.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from email.utils import formatdate

import httpx
from fake_federation import FakeFederation
from fake_onedata import Request

PROVIDER = "/api/v3/oneprovider"
ROOT_ID = "root-0001"


@dataclass
class Node:
    """A file or a directory of the space."""

    file_id: str
    name: str
    kind: str  # "REG" or "DIR"
    parent: str | None
    content: bytearray = field(default_factory=bytearray)


@dataclass
class Transfer:
    """A replication, eviction or migration job, when it was scheduled, started and ended, and how often it was asked
    about. `provider_id` is the replicating provider (replication, migration) or the evicting one (eviction);
    `source_id` is a migration's evicting provider."""

    kind: str
    file_id: str
    provider_id: str
    asked: int = 0
    ended: bool = False
    source_id: str | None = None
    scheduled: int = 0
    started: int = 0
    finished: int = 0
    waiting: bool = False
    bytes_replicated: int | None = None  # the counter the record reports, when a test scripts it
    noop: bool = False  # an eviction that completed and evicted nothing (`evictions_noop_first`)

    @property
    def replicates(self) -> bool:
        return self.kind in ("replication", "migration")

    @property
    def state(self) -> str:
        return "ended" if self.ended else "waiting" if self.waiting else "ongoing"


def _merge(blocks: list[list[int]]) -> list[list[int]]:
    """Merge overlapping or adjacent [offset, length] blocks."""
    merged: list[list[int]] = []
    for off, length in sorted(blocks):
        if merged and off <= merged[-1][0] + merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], off + length - merged[-1][0])
        else:
            merged.append([off, length])
    return merged


class FakeData(FakeFederation):
    """Onezone plus the data API of every provider it reports."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.nodes: dict[str, Node] = {ROOT_ID: Node(ROOT_ID, "", "DIR", None)}
        self.blocks: dict[tuple[str, str], list[list[int]]] = {}
        self.pending: dict[tuple[str, str], list[list[int]]] = {}  # [offset, length, polls left]
        self.transfers: dict[str, Transfer] = {}
        self.provider_calls: list[tuple[str, str, str]] = []
        self.fetch_lag = 0
        self.onthefly_places = True
        self.transfers_place_blocks = True
        self.evictions_remove_blocks = True
        self.evictions_noop_first = 0  # the next N evictions complete with filesEvicted 0 and remove nothing, as 25.0's
        # do when scheduled right after a provider fetched the bytes on the fly
        self.onthefly_unit: int | None = (
            None  # a range read places [offset, offset + max(length, unit)), clipped to the
        )
        # file, as 25.0 does with 1 MiB; None places exactly the range read
        self.transfer_flip_after = 1
        self.transfer_visible_after = 0
        self.root_enoent_polls = 0
        # A provider that sees a new file only after some reads: (provider id, file id) -> reads of it answered as
        # not there; `unseen_after_create` (provider id -> reads) sets that for every file made from then on.
        self.unseen: dict[tuple[str, str], int] = {}
        self.unseen_after_create: dict[str, int] = {}
        self.unseen_answer: tuple[int, str | None, str | None] = (404, "notFound", None)
        self.root_enoent_answer: tuple[int, str | None, str | None] = (404, "posix", "enoent")  # a fresh root's
        self.transfer_unseen_answer: tuple[int, str | None, str | None] = (404, "notFound", None)  # a job not seen yet
        self.page_size = 1000
        self.id_suffix = ""  # appended to every file id made from now on, for ids with characters like "="
        self.fail_deletes = False  # every DELETE answers 500
        self.omit_blocks = False  # the block map's storage backends carry no "blocks" field
        self.omit_providers: set[str] = set()  # provider ids the block map leaves out (a partial answer)
        self.ignore_deletes = False  # every DELETE answers 204 and removes nothing
        self.enforce_readonly = True  # a mutation made with a token that has the read-only caveat is refused
        # how that refusal is answered: (status, error id, errno); Onedata 25.0 answers (400, "posix", "eacces")
        self.readonly_answer: tuple[int, str, str | None] = (403, "forbidden", None)
        # how a file or directory that is not there is answered: (status, error id, errno), an error id of None being a
        # body that is no error object; see `answer_missing_as_25`
        self.missing_answer: tuple[int, str | None, str | None] = (404, "notFound", None)
        self.deleted_parents: dict[str, str | None] | None = None  # a deleted entry's parent link, while it is kept
        self.resets: list[list] = []
        self.statuses: list[list] = []
        self.content_type: str | None = "application/octet-stream"  # of a file's content, whole or by range
        self.clock = lambda: 0.0  # epoch seconds of a job's schedule, start and finish times
        self.transfer_omit: set[str] = set()  # fields a job's record leaves out
        self.transfer_list_body: dict | None = None  # replaces every answer of the listing
        self.provider_view: dict[tuple[str, str], dict] = {}  # (job, provider id) -> its listed state, record fields
        self.listing_lag: dict[str, int] = {}  # job id -> listing answers it is left out of, from the first one
        self.provider_skew: dict[str, float] = {}  # provider id -> seconds its clock runs ahead (negative: behind)
        self.date_override: dict[str, str | None] = {}  # provider id -> the Date header it sends (None: none)
        self.listings = 0  # the listing answers given
        self._ids = itertools.count(1)
        self._root_polls = 0

    # -- setup helpers ---------------------------------------------------------
    def provider_of(self, host: str) -> str | None:
        """Return the id of the provider that answers for a host."""
        return next((i for i, p in self.providers.items() if p["domain"] == host), None)

    def add_file(self, path: str, content: bytes, *, holder: str | None = None) -> str:
        """Create a file at a path of the space (directories on the way as needed); the holder has all its blocks."""
        holder = holder or self.PROVIDER_ID
        parent = ROOT_ID
        *dirs, name = path.strip("/").split("/")
        for part in dirs:
            parent = self._child(parent, part) or self._create(parent, part, "DIR")
        file_id = self._create(parent, name, "REG")
        self.nodes[file_id].content = bytearray(content)
        if content:
            self.blocks[(file_id, holder)] = [[0, len(content)]]
        return file_id

    def id_of(self, path: str) -> str:
        """Return the id of a path of the space; raise KeyError when there is none."""
        node = ROOT_ID
        for part in path.strip("/").split("/"):
            child = self._child(node, part)
            if child is None:
                raise KeyError(path)
            node = child
        return node

    def held(self, path: str, provider_id: str) -> list[list[int]]:
        """Return the blocks a provider holds of a file, merged."""
        return _merge([list(b) for b in self.blocks.get((self.id_of(path), provider_id), [])])

    def exists(self, path: str) -> bool:
        """Tell whether a path of the space exists."""
        try:
            self.id_of(path)
        except KeyError:
            return False
        return True

    def add_transfer(
        self,
        kind: str,
        path: str,
        provider_id: str,
        *,
        scheduled: float,
        source_id: str | None = None,
        ended: bool = True,
        waiting: bool = False,
        finished: float | None = None,
        bytes_replicated: int | None = None,
    ) -> str:
        """Make a job no request to `/transfers` made (a QoS requirement's, another client's) and return its id; one
        that has ended has placed or removed its blocks, and finished at `finished` (default: when it was scheduled)."""
        tid = f"tid-{next(self._ids):04d}"
        self.transfers[tid] = Transfer(
            kind,
            self.id_of(path),
            provider_id,
            source_id=source_id,
            scheduled=int(scheduled),
            started=0 if waiting else int(scheduled),
            waiting=waiting,
            bytes_replicated=bytes_replicated,
        )
        if ended:
            self.end_transfer(tid, at=scheduled if finished is None else finished)
        return tid

    def end_transfer(self, tid: str, *, at: float | None = None) -> None:
        """End a job now (or at `at`): a replication places the file's blocks, an eviction removes them."""
        transfer = self.transfers[tid]
        transfer.ended, transfer.waiting = True, False
        transfer.started = transfer.started or transfer.scheduled
        transfer.finished = int(self.clock() if at is None else at)
        self._finish(transfer)

    # -- tree ------------------------------------------------------------------
    def _child(self, parent: str, name: str) -> str | None:
        return next((n.file_id for n in self.nodes.values() if n.parent == parent and n.name == name), None)

    def _create(self, parent: str, name: str, kind: str) -> str:
        file_id = f"fid-{next(self._ids):04d}{self.id_suffix}"
        self.nodes[file_id] = Node(file_id, name, kind, parent)
        if kind == "REG":  # a provider that sees a file sees the directories above it, made before it
            for provider_id, reads in self.unseen_after_create.items():
                self.unseen[(provider_id, file_id)] = reads
        return file_id

    def _delete(self, file_id: str) -> None:
        for child in [n.file_id for n in self.nodes.values() if n.parent == file_id]:
            self._delete(child)
        if self.deleted_parents is not None:
            self.deleted_parents[file_id] = self.nodes[file_id].parent
        del self.nodes[file_id]
        for table in (self.blocks, self.pending):
            for key in [k for k in table if k[0] == file_id]:
                del table[key]

    # -- answers ---------------------------------------------------------------
    @staticmethod
    def _error(status: int, error_id: str, errno: str | None = None) -> httpx.Response:
        details = {"errno": errno} if errno is not None else {}
        return httpx.Response(status, json={"error": {"id": error_id, "details": details}})

    def answer_missing_as_25(self, answer: tuple[int, str | None, str | None] = (400, "posix", "enoent")) -> None:
        """Answer what is not there as Onedata 25.0 does: 400 posix enoent instead of a 404, while a deleted entry's
        `parent_id` still answers its old parent (both read on the Compose federation). `answer` replaces the 400."""
        self.missing_answer = answer
        self.deleted_parents = {}

    def _missing(self, file_id: str, query: httpx.QueryParams, method: str) -> httpx.Response:
        """A file or directory that is not there: `missing_answer`, except a deleted entry's kept parent link."""
        kept = self.deleted_parents or {}
        if method == "GET" and file_id in kept and query.get("attribute") == "parent_id":
            return httpx.Response(200, json={"parent_id": kept[file_id]})
        return self._answer(self.missing_answer)

    def _answer(self, answer: tuple[int, str | None, str | None]) -> httpx.Response:
        """An error answer from (status, error id, errno); an error id of None is a body that is no error object."""
        status, error_id, errno = answer
        if error_id is None:
            return httpx.Response(status, text="<html><body>Bad Request</body></html>")
        return self._error(status, error_id, errno)

    def respond(self, request: httpx.Request) -> httpx.Response:
        """Answer a provider call here and anything else as the federation does."""
        if not request.url.path.startswith(PROVIDER):
            return super().respond(request)
        route = request.url.path.removeprefix(PROVIDER)
        provider = self.provider_of(request.url.host)
        with self._lock:
            self.provider_calls.append((provider or "?", request.method, route))
            self.requests.append(
                Request(
                    request.method,
                    request.url.path,
                    dict(request.url.params),
                    request.headers.get("Range"),
                    request.headers.get("X-Auth-Token"),
                    request.content,
                )
            )
            for fault in self.resets:
                if fault[1] > 0 and fault[0] in route and (len(fault) < 3 or fault[2] == provider):
                    fault[1] -= 1
                    raise httpx.ConnectError("connection reset")
            for fault in self.statuses:
                if fault[1] > 0 and fault[0] in route:
                    fault[1] -= 1
                    return self._error(fault[2], "internalServerError")
        if provider is None:
            return self._error(404, "notFound")
        if route == "/configuration":
            return httpx.Response(200, json={"version": "25.0.0", "build": "test", "name": provider})
        token = request.headers.get("X-Auth-Token")
        if token != self.admin_token and not self._accepts(token):
            return self._error(401, "unauthorized")
        return self._dated(self._provider_route(request, provider, route), provider)

    def _dated(self, response: httpx.Response, provider: str) -> httpx.Response:
        """Give an answer the Date header of the provider's clock: the fake's clock moved by the provider's skew, in
        whole seconds as HTTP has it, or the override a test set (None: no header at all)."""
        if provider in self.date_override:
            if self.date_override[provider] is not None:
                response.headers["Date"] = self.date_override[provider]
            return response
        response.headers["Date"] = formatdate(self.clock() + self.provider_skew.get(provider, 0.0), usegmt=True)
        return response

    def _admin_route(self, method: str, route: str, sent: dict) -> httpx.Response:
        """Onezone's calls that need the admin credential: a provider's online flag, then what the federation has."""
        found = re.fullmatch(r"/providers/([^/]+)", route)
        if method == "GET" and found:
            entry = self.providers.get(found.group(1))
            if entry is None:
                return self._error(404, "notFound")
            return httpx.Response(200, json={"online": entry["online"], "name": entry["name"]})
        return super()._admin_route(method, route, sent)

    def _provider_route(self, request: httpx.Request, provider: str, route: str) -> httpx.Response:
        method, query = request.method, request.url.params
        issued = self.issued.get(request.headers.get("X-Auth-Token", ""))
        if (
            self.enforce_readonly
            and issued is not None
            and method in ("POST", "PUT", "DELETE")
            and any(c.get("type") == "data.readonly" for c in issued["caveats"])
        ):
            return self._error(*self.readonly_answer)
        found = re.fullmatch(r"/spaces/([^/]+)", route)
        if found and method == "GET":
            return self._space_root(found.group(1))
        found = re.fullmatch(r"/data/([^/]+)(/children|/content|/distribution)?", route)
        if found:
            node = self.nodes.get(found.group(1))
            lag = (provider, found.group(1))
            if node is not None and self.unseen.get(lag, 0) > 0:  # this provider does not see the new file yet
                self.unseen[lag] -= 1
                return self._answer(self.unseen_answer)
            if node is None:
                return self._missing(found.group(1), query, method)
            part = found.group(2) or ""
            if part == "" and method == "GET":
                if "path" in query.get_list("attribute"):  # Onedata 25.0 has no path attribute ([L1] in client.py)
                    return self._error(400, "badValueNotAllowed")
                return httpx.Response(200, json=self._attributes(node))
            if part == "" and method == "DELETE":
                if self.fail_deletes:
                    return self._error(500, "internalServerError")
                if not self.ignore_deletes:
                    self._delete(node.file_id)
                return httpx.Response(204)
            if part == "/children" and method == "GET":
                return self._list(node, query)
            if part == "/children" and method == "POST":
                return self._make(node, query)
            if part == "/content" and method == "PUT":
                if request.headers.get("Content-Type") != "application/octet-stream":
                    return httpx.Response(415)  # 25.0 refuses a raw body that does not say it is one
                return self._write(node, provider, int(query.get("offset", "0")), request.content)
            if part == "/content" and method == "GET":
                return self._read(node, provider, request.headers.get("Range"))
            if part == "/distribution" and method == "GET":
                return httpx.Response(200, json=self._distribution(node))
        if route == "/transfers" and method == "POST":
            return self._schedule(request)
        found = re.fullmatch(r"/spaces/([^/]+)/transfers", route)
        if found and method == "GET":
            return self._list_transfers(found.group(1), query, provider)
        found = re.fullmatch(r"/transfers/([^/]+)", route)
        if found and method == "GET":
            return self._transfer(found.group(1), provider)
        return self._error(404, "notFound")

    def _space_root(self, space: str) -> httpx.Response:
        if space != self.SPACE_ID:
            return self._error(404, "notFound")
        self._root_polls += 1
        if self._root_polls <= self.root_enoent_polls:
            return self._answer(self.root_enoent_answer)
        return httpx.Response(200, json={"fileId": ROOT_ID, "name": self.space_name})

    def _attributes(self, node: Node) -> dict:
        """A file's attributes as Onedata 25.0 names them; `parent_id` is its parent's id, null for a space's root."""
        return {
            "fileId": node.file_id,
            "file_id": node.file_id,
            "name": node.name,
            "type": node.kind,
            "size": len(node.content),
            "parent_id": node.parent,
        }

    def add_tree(self, root_id: str, path: str) -> str:
        """Make a tree that is no part of the study space: a root of its own, with no parent, and a path below it;
        return the id of the path's last entry. It stands for a file of a space this configuration does not know."""
        self.nodes[root_id] = Node(root_id, "", "DIR", None)
        parent = root_id
        for part in path.strip("/").split("/"):
            parent = self._child(parent, part) or self._create(parent, part, "DIR")
        return parent

    def _list(self, node: Node, query: httpx.QueryParams) -> httpx.Response:
        if node.kind != "DIR":
            return self._error(400, "posix")
        children = sorted((n for n in self.nodes.values() if n.parent == node.file_id), key=lambda n: n.name)
        start = int(query.get("token", "0"))
        limit = min(int(query.get("limit", self.page_size)), self.page_size)
        page = children[start : start + limit]
        last = start + limit >= len(children)
        body: dict = {"children": [self._attributes(c) for c in page], "isLast": last}
        if not last:
            body["nextPageToken"] = str(start + limit)
        return httpx.Response(200, json=body)

    def _make(self, parent: Node, query: httpx.QueryParams) -> httpx.Response:
        name, kind = query.get("name"), query.get("type")
        if parent.kind != "DIR" or not name or kind not in ("REG", "DIR"):
            return self._error(400, "badValueNotAllowed")
        if self._child(parent.file_id, name) is not None:
            return self._error(409, "posix")
        return httpx.Response(201, json={"fileId": self._create(parent.file_id, name, kind)})

    def _write(self, node: Node, provider: str, offset: int, body: bytes) -> httpx.Response:
        end = offset + len(body)
        if len(node.content) < end:  # a write past the end pads with zeros, as a sparse file does
            node.content.extend(b"\0" * (end - len(node.content)))
        node.content[offset:end] = body
        if body:
            held = self.blocks.setdefault((node.file_id, provider), [])
            held.append([offset, len(body)])
            self.blocks[(node.file_id, provider)] = _merge(held)
        return httpx.Response(204)

    def _read(self, node: Node, provider: str, header: str | None) -> httpx.Response:
        found = re.fullmatch(r"bytes=(\d+)-(\d+)", header or "")
        typed = {} if self.content_type is None else {"Content-Type": self.content_type}
        if found is None:
            return httpx.Response(200, content=bytes(node.content), headers=typed)
        first, last = int(found.group(1)), min(int(found.group(2)), len(node.content) - 1)
        if first > last:
            return self._error(416, "rangeNotSatisfiable")
        length = last - first + 1
        if not self._holds(node.file_id, provider, first, length) and self.onthefly_places:
            placed = (
                length
                if self.onthefly_unit is None
                else min(max(length, self.onthefly_unit), len(node.content) - first)
            )
            self.pending.setdefault((node.file_id, provider), []).append([first, placed, self.fetch_lag])
            self._promote(node.file_id, provider)
        return httpx.Response(
            206,
            content=bytes(node.content[first : last + 1]),
            headers={"Content-Range": f"bytes {first}-{last}/{len(node.content)}", **typed},
        )

    def _holds(self, file_id: str, provider: str, offset: int, length: int) -> bool:
        return any(
            o <= offset and offset + length <= o + n for o, n in _merge(self.blocks.get((file_id, provider), []))
        )

    def _promote(self, file_id: str, provider: str) -> None:
        """Move the pending blocks whose lag has run out into the block map."""
        waiting = self.pending.get((file_id, provider), [])
        due = [p for p in waiting if p[2] <= 0]
        self.pending[(file_id, provider)] = [p for p in waiting if p[2] > 0]
        if due:
            held = self.blocks.setdefault((file_id, provider), [])
            held.extend([o, n] for o, n, _ in due)
            self.blocks[(file_id, provider)] = _merge(held)

    def _distribution(self, node: Node) -> dict:
        per: dict = {}
        for provider in self.providers:
            for waiting in self.pending.get((node.file_id, provider), []):
                waiting[2] -= 1
            self._promote(node.file_id, provider)
            held = self.blocks.get((node.file_id, provider), [])
            backend: dict = {"physicalSize": sum(n for _, n in held)}
            if not self.omit_blocks:
                backend["blocks"] = [list(b) for b in held]
            if provider not in self.omit_providers:
                per[provider] = {
                    "virtualSize": len(node.content),
                    "distributionPerStorageBackend": {"storage-1": backend},
                }
        return {"type": node.kind, "distributionPerProvider": per}

    def _schedule(self, request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content or b"{}")
        kind = sent.get("type")
        provider = sent.get("evictingProviderId" if kind == "eviction" else "replicatingProviderId")
        source = sent.get("evictingProviderId") if kind == "migration" else None
        if (
            kind not in ("replication", "eviction", "migration")
            or sent.get("fileId") not in self.nodes
            or provider is None
            or (kind == "migration" and source is None)
        ):
            return self._error(400, "badValueNotAllowed")
        tid = f"tid-{next(self._ids):04d}"
        now = int(self.clock())
        self.transfers[tid] = Transfer(kind, sent["fileId"], provider, source_id=source, scheduled=now, started=now)
        return httpx.Response(201, json={"transferId": tid})

    def _list_transfers(self, space: str, query: httpx.QueryParams, provider: str) -> httpx.Response:
        if space != self.SPACE_ID:
            return self._error(404, "notFound")
        if query.get("state") not in ("waiting", "ongoing", "ended"):
            return self._error(400, "badValueNotAllowed")
        self.listings += 1
        if self.transfer_list_body is not None:
            return httpx.Response(200, json=self.transfer_list_body)
        ids = [
            tid
            for tid, t in self.transfers.items()
            if self._view(tid, provider).get("state", t.state) == query["state"]
            and self.listings > self.listing_lag.get(tid, 0)
        ]
        start = int(query.get("page_token") or "0")
        end = start + int(query.get("limit", "100"))
        token = str(end) if end < len(ids) else None
        return httpx.Response(200, json={"transfers": ids[start:end], "nextPageToken": token})

    def _transfer(self, tid: str, provider: str) -> httpx.Response:
        transfer = self.transfers.get(tid)
        if transfer is None:
            return self._error(404, "notFound")
        transfer.asked += 1
        if transfer.asked <= self.transfer_visible_after:
            return self._answer(self.transfer_unseen_answer)
        if not transfer.ended and transfer.asked > self.transfer_visible_after + self.transfer_flip_after:
            self.end_transfer(tid)
        state = "completed" if transfer.ended else "scheduled" if transfer.waiting else "active"
        size = len(self.nodes[transfer.file_id].content) if transfer.file_id in self.nodes else 0
        moved = transfer.bytes_replicated if transfer.bytes_replicated is not None else size
        answer = {
            "evictionStatus" if transfer.kind == "eviction" else "replicationStatus": state,
            "type": transfer.kind,
            "fileId": transfer.file_id,
            "replicatingProviderId": transfer.provider_id if transfer.replicates else None,
            "evictingProviderId": transfer.provider_id if transfer.kind == "eviction" else transfer.source_id,
            "scheduleTime": transfer.scheduled,
            "startTime": transfer.started,
            "finishTime": transfer.finished,
            "bytesReplicated": moved if transfer.ended and transfer.replicates else 0,
            "filesReplicated": 1 if transfer.ended and transfer.replicates else 0,
            "filesEvicted": 1 if transfer.ended and transfer.kind != "replication" and not transfer.noop else 0,
            "filesFailed": 0,
        }
        view = self._view(tid, provider)
        if "state" in view:  # this provider lists the job in another state, and its record says so
            status = "evictionStatus" if transfer.kind == "eviction" else "replicationStatus"
            answer[status] = {"ended": "completed", "ongoing": "active", "waiting": "scheduled"}[view["state"]]
        answer.update({k: v for k, v in view.items() if k != "state"})
        return httpx.Response(200, json={k: v for k, v in answer.items() if k not in self.transfer_omit})

    def _view(self, tid: str, provider: str) -> dict:
        """What one provider says of a job where it differs from the job itself: its listed state, record fields."""
        return self.provider_view.get((tid, provider), {})

    def _finish(self, transfer: Transfer) -> None:
        key = (transfer.file_id, transfer.provider_id)
        if transfer.replicates and self.transfers_place_blocks and transfer.file_id in self.nodes:
            size = len(self.nodes[transfer.file_id].content)
            self.pending.setdefault(key, []).append([0, size, self.fetch_lag])
            self._promote(*key)
        evicted = (transfer.file_id, transfer.source_id) if transfer.kind == "migration" else key
        if transfer.kind == "eviction" and self.evictions_noop_first > 0:  # completed, and nothing evicted
            self.evictions_noop_first -= 1
            transfer.noop = True
            return
        if transfer.kind != "replication" and self.evictions_remove_blocks:
            self.blocks.pop(evicted, None)
            self.pending.pop(evicted, None)


__all__ = ["PROVIDER", "ROOT_ID", "FakeData"]
