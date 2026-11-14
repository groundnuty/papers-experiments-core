# Provenance: new
"""A fake federation whose data provider holds the study space on an imported storage, with that provider's Onepanel
(O41, #184): what `storageimport.py` reads and sends, as Onedata 25.0's sources describe it.

`FakeImportData` is a `FakeData` whose data provider (`importing`, alpha by default) supports the study space on an
imported POSIX storage. The storage's content is a dictionary (`storage`, path to bytes, and `storage_dirs`), which a
test fills with `put_on_storage` and `put_dir_on_storage` before a scan. A scan makes every storage entry the space does
not hold yet, each file with a new id and its whole block map on the importing provider, which is how op-worker f118ed9
imports a file (`storage_import_engine.erl`, `import_file_unsafe`; `fslogic_location.erl`, `create_imported_file_doc`):
the other providers show the entries after their visibility delay, as for a file written through the importing one.
With the detection of changes off, as the core's federations set it, a scan never changes or deletes what the space
holds, so neither does this one.

Models, besides `FakeData`'s calls:

  GET  /api/v3/onepanel/provider/spaces/{sid}                           storageId, storageImport (mode, auto config)
  GET  /api/v3/onepanel/provider/storages/{id}                          name, type, importedStorage, readonly,
                                                                        mountPoint
  GET  /api/v3/onepanel/provider/spaces/{sid}/storage-import/auto/info  the scan's status and counters; a scan stays
                                                                        enqueued, then running, over `scan_polls`
                                                                        reads, then imports and ends as
                                                                        `scan_outcome` (with `scan_failed_files`)
  POST /api/v3/onepanel/provider/spaces/{sid}/storage-import/auto/force-start   a new scan

Each panel call is recorded in `panel_calls` as (provider id, method, route); `panel_status` makes every panel call
answer that status. A read-only storage (`readonly`) refuses, on the importing provider, a creation and a write of
content with 400 `posix` `erofs`, and a delete there removes the entry from the space and leaves it on the storage, so a
later scan imports it again under a new id (`fslogic_delete.erl`, `maybe_delete_storage_file`); a writable one takes
both, and a delete there also takes the file off the storage.

Does not model: the scan's speed, LUMA's owners, modes and times, a scan with the detection of changes on, manual
registration, or the writes through another provider that take the importing provider's blocks from it.
"""

import re
from typing import Any

import httpx

from expcore_onedata.testing.fake_data import ROOT_ID, FakeData, Node
from expcore_onedata.testing.world import BETA_ID, SPACE_NAME, add_users

PANEL = "/api/v3/onepanel"
STORAGE_ID = "storage-imported-0001"
MOUNT_POINT = "/volumes/import/study"


class FakeImportData(FakeData):
    """`FakeData` whose data provider holds the study space on an imported storage, with its Onepanel.

    Models: the imported storage's content, a scan that makes what the space lacks with whole block maps on the
    importing provider, the panel's support, storage and scan reads and its force-start, and a read-only storage's
    refusals and deletes (the module docstring has each).

    Does not model: the scan's speed, LUMA's owners, modes and times, change detection, manual registration, or a
    write through another provider that takes the importing provider's blocks.
    """

    def __init__(self, *, readonly: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.importing = self.PROVIDER_ID  # the provider whose support is the imported storage
        self.readonly = readonly  # what the panel shows, and whether the importing provider refuses writes
        self.imported_storage = True  # what the panel shows of the storage
        self.mode = "auto"  # the support's import mode, as the panel shows it
        # its auto config, as the panel shows it: the core's federations turn all three off
        self.auto_config = {"continuousScan": False, "detectModifications": False, "detectDeletions": False}
        self.storage: dict[str, bytes] = {}  # the imported storage's files, by path in the space
        self.storage_dirs: set[str] = set()  # its directories, an empty one included
        self.scan_polls = 1  # info reads a scan stays enqueued or running before it imports and ends
        self.scan_outcome = "completed"  # how the next scan ends: "completed", "failed" or "aborted"
        self.scan_failed_files = 0  # the failed files a scan reports
        self.panel_status: int | None = None  # every panel call answers this status instead, when set
        self.panel_calls: list[tuple[str, str, str]] = []
        self._scan: dict[str, Any] = {"status": "completed", "totalScans": 0, **dict.fromkeys(_COUNTERS, 0)}
        self._scan.update(start=0, stop=0)
        self._polls_left = 0

    # -- the storage -----------------------------------------------------------------------------------------------
    def put_on_storage(self, path: str, content: bytes) -> None:
        """Put a file on the imported storage, its directories implied; a scan imports it."""
        self.storage[path.strip("/")] = bytes(content)

    def put_dir_on_storage(self, path: str) -> None:
        """Put a directory on the imported storage, an empty one included."""
        self.storage_dirs.add(path.strip("/"))

    def drop_from_storage(self, path: str) -> None:
        """Take a file off the imported storage before a scan."""
        del self.storage[path.strip("/")]

    def remove_from_space(self, path: str) -> None:
        """What a REST delete through the importing provider does: the entry leaves the space; on a read-only storage
        the file stays there, on a writable one it goes too."""
        with self._lock:
            self._delete(self.id_of(path))
            if not self.readonly:
                self.storage.pop(path.strip("/"), None)

    def _storage_tree(self) -> list[tuple[str, str]]:
        """Every entry of the storage as (path, kind), each directory before what lies in it."""
        directories = set(self.storage_dirs)
        for path in self.storage:
            parts = path.split("/")
            directories.update("/".join(parts[:depth]) for depth in range(1, len(parts)))
        entries = [(d, "DIR") for d in directories] + [(f, "REG") for f in self.storage]
        return sorted(entries, key=lambda entry: (entry[0].count("/"), entry[0]))

    def _run_scan(self) -> None:
        """Import what the space does not hold yet; count what was made and what was there already."""
        created = unmodified = 0
        for path, kind in self._storage_tree():
            if self.exists(path):
                unmodified += 1
                continue
            created += 1
            if kind == "DIR":
                self._make_dir(path)
            else:
                self.add_file(path, self.storage[path], holder=self.importing)
        self._scan.update(
            status=self.scan_outcome,
            stop=int(self.clock()),
            createdFiles=created,
            unmodifiedFiles=unmodified,
            failedFiles=self.scan_failed_files,
            totalScans=self._scan["totalScans"] + 1,
        )

    def _make_dir(self, path: str) -> None:
        parent = ROOT_ID
        for part in path.split("/"):
            parent = self._child(parent, part) or self._create(parent, part, "DIR", self.importing)

    def start_scan(self) -> None:
        """Enqueue a scan, as an auto support does for the initial one and a force-start for a later one."""
        with self._lock:
            self._scan.update(status="enqueued", start=int(self.clock()), stop=0)
            self._scan.update(dict.fromkeys(_COUNTERS, 0))
            self._polls_left = self.scan_polls

    def _scan_info(self) -> dict[str, Any]:
        """The scan's state, moved on by this read: enqueued, then running, then imported and ended."""
        if self._scan["status"] in ("enqueued", "running"):
            self._polls_left -= 1
            if self._polls_left <= 0:
                self._run_scan()
            else:
                self._scan["status"] = "running"
        return dict(self._scan)

    # -- the panel -----------------------------------------------------------------------------------------------------
    def respond(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.startswith(PANEL):
            return super().respond(request)
        route = request.url.path.removeprefix(PANEL)
        provider = self.provider_of(request.url.host)
        with self._lock:
            self.panel_calls.append((provider or "?", request.method, route))
            if provider is None:
                return self._error(404, "notFound")
            if request.headers.get("X-Auth-Token") != self.admin_token:
                return self._error(401, "unauthorized")
            if self.panel_status is not None:
                return self._error(self.panel_status, "forbidden")
            return self._panel_route(request.method, route, provider)

    def _panel_route(self, method: str, route: str, provider: str) -> httpx.Response:
        space = self.SPACE_ID
        if method == "GET" and route == f"/provider/spaces/{space}":
            return httpx.Response(200, json=self._space_details(provider))
        if method == "GET" and route == f"/provider/storages/{STORAGE_ID}" and provider == self.importing:
            return httpx.Response(200, json=self._storage_details())
        if provider != self.importing:
            return self._error(404, "notFound")
        if method == "GET" and route == f"/provider/spaces/{space}/storage-import/auto/info":
            return httpx.Response(200, json=self._scan_info())
        if method == "POST" and route == f"/provider/spaces/{space}/storage-import/auto/force-start":
            self.start_scan()
            return httpx.Response(204)
        return self._error(404, "notFound")

    def _space_details(self, provider: str) -> dict[str, Any]:
        details: dict[str, Any] = {"id": self.SPACE_ID, "name": self.space_name}
        if provider != self.importing:
            return {**details, "storageId": "storage-posix-0001"}
        return {
            **details,
            "storageId": STORAGE_ID,
            "storageImport": {"mode": self.mode, "autoStorageImportConfig": dict(self.auto_config)},
        }

    def _storage_details(self) -> dict[str, Any]:
        return {
            "id": STORAGE_ID,
            "name": "import-study",
            "type": "posix",
            "importedStorage": self.imported_storage,
            "readonly": self.readonly,
            "mountPoint": MOUNT_POINT,
        }

    # -- the read-only storage -------------------------------------------------------------------------------------
    def _provider_route(self, request: httpx.Request, provider: str, route: str) -> httpx.Response:
        writing = request.method in ("POST", "PUT") and re.fullmatch(r"/data/[^/]+/(children|content)", route)
        if self.readonly and provider == self.importing and writing:
            return self._error(400, "posix", "erofs")
        deleted = self._deleted_path(request, route)
        answer = super()._provider_route(request, provider, route)
        if deleted is not None and not self.readonly and answer.status_code == 204:
            self.storage.pop(deleted, None)
        return answer

    def _deleted_path(self, request: httpx.Request, route: str) -> str | None:
        """The space path of the file a DELETE names, read before the delete takes it."""
        found = re.fullmatch(r"/data/([^/]+)", route)
        if request.method != "DELETE" or found is None or found.group(1) not in self.nodes:
            return None
        parts: list[str] = []
        node: Node | None = self.nodes[found.group(1)]
        while node is not None and node.parent is not None:
            parts.append(node.name)
            node = self.nodes.get(node.parent)
        return "/".join(reversed(parts))


_COUNTERS = ("createdFiles", "modifiedFiles", "deletedFiles", "unmodifiedFiles", "failedFiles")


def make_import_federation(*, clock: Any = None, readonly: bool = True, **kwargs: Any) -> FakeImportData:
    """The federation `make_federation` returns, its data provider alpha holding the study space on an imported
    storage (`readonly` or not); beta is known and does not support the space until a test adds it. The storage is
    empty and no scan has run: a test puts the dataset on the storage and starts the initial scan (`start_scan`)."""
    federation = FakeImportData(space_name=SPACE_NAME, readonly=readonly, **kwargs)
    if clock is not None:
        federation.clock = clock
    federation.add_provider(BETA_ID, "beta.test", supports=False)
    add_users(federation)
    return federation


__all__ = ["MOUNT_POINT", "PANEL", "STORAGE_ID", "FakeImportData", "make_import_federation"]
