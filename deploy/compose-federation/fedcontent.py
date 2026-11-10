"""The baseline content of the spaces: bring each space back to exactly its synthetic files, read every one back, and
replicate one file so the study space has an ended transfer to read.

A provider finishes deleting a directory after the call returns, and with canonical storage paths a new directory of
the same name has the same path on storage, so the old one's late removal can take the new one's files. Reset
therefore never deletes and makes again a directory of the same name: a baseline directory is kept and emptied file by
file (the large listing directory is kept whole when it is intact), and any other entry is removed, a directory of
another name included. Every baseline file is then read back through the writer, and a file whose bytes differ is
written again, for a bounded number of rounds.
"""

import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath
from typing import Any

from fedboot import Bootstrap
from fedlayout import (
    HOLDER,
    LISTING_DIR,
    PROVIDERS,
    REPLICATED_FILE,
    SPACES,
    STUDY_SPACE,
    BaselineFile,
    Service,
    baseline,
)
from fedrest import PROVIDER_API, RestError, bearer

WORKERS = 16
PAGE = 1000
WRITE_ROUNDS = 3
TRANSFER_DONE = {"completed"}
TRANSFER_FAILED = {"failed", "cancelled", "aborted"}


def provider(name: str) -> Service:
    return next(service for service in PROVIDERS if service.name == name)


def parent_of(path: str) -> str:
    """The directory a baseline path lies in, relative to the space root; '' for the root itself."""
    return "/".join(PurePosixPath(path).parts[:-1])


def top_directories(files: Iterable[BaselineFile]) -> list[str]:
    """The baseline's directories, each directly under the root; a deeper one is refused."""
    names = sorted({parent_of(f.path) for f in files} - {""})
    if any("/" in name for name in names):
        raise ValueError("a baseline directory must lie directly under the space root")
    return names


def writer_of(space: str) -> Service:
    """The provider a space's baseline is written through: the holder, or the first supporter of a space it lacks."""
    supporters = SPACES[space]
    return provider(HOLDER if HOLDER in supporters else supporters[0])


def intact(found: dict[str, dict[str, Any]], expected: dict[str, int]) -> bool:
    """A directory holds exactly the expected regular files, each of its size."""
    return set(found) == set(expected) and all(
        found[path].get("type") == "REG" and found[path].get("size") == size for path, size in expected.items()
    )


class Content:
    """Bring the spaces' content back to the baseline."""

    def __init__(self, boot: Bootstrap) -> None:
        self.boot = boot

    def root(self, service: Service, space: str) -> str:
        sid = self.boot.ids.spaces[space]
        return self.boot.provider(service, "GET", f"/spaces/{sid}", "space")["fileId"]

    def children(self, service: Service, directory: str) -> list[dict[str, Any]]:
        """Every entry of a directory, page by page."""
        entries: list[dict[str, Any]] = []
        params: dict[str, Any] = {"limit": PAGE, "attribute": ["file_id", "name", "type", "size"]}
        while True:
            page = self.boot.provider(service, "GET", f"/data/{directory}/children", "children", params=params)
            entries += page["children"]
            if page.get("isLast", True):
                return entries
            params = {**params, "token": page["nextPageToken"]}

    def remove(self, service: Service, file_ids: Sequence[str]) -> None:
        with ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(lambda i: self.boot.provider(service, "DELETE", f"/data/{i}", "remove"), file_ids))

    def make(self, service: Service, parent: str, name: str, kind: str) -> str:
        params = {"name": name, "type": kind}
        return self.boot.provider(service, "POST", f"/data/{parent}/children", "create", params=params)["fileId"]

    def clear(self, service: Service, root: str, files: Sequence[BaselineFile]) -> tuple[int, dict[str, str], dict]:
        """Remove what is not baseline; return the count removed, the directories' ids by path and the files kept."""
        tops = top_directories(files)
        directories, kept, removed = {"": root}, {}, []
        for entry in self.children(service, root):
            if entry["type"] == "DIR" and entry["name"] in tops:
                directories[entry["name"]] = entry["file_id"]
            else:
                removed.append(entry["file_id"])
        for name in tops:
            if name not in directories:
                directories[name] = self.make(service, root, name, "DIR")
                continue
            inside = {f"{name}/{e['name']}": e for e in self.children(service, directories[name])}
            expected = {f.path: f.size for f in files if parent_of(f.path) == name}
            if name == LISTING_DIR and intact(inside, expected):
                kept.update({path: inside[path]["file_id"] for path in expected})  # read back below like the rest
            else:
                removed += [e["file_id"] for e in inside.values()]
        self.remove(service, removed)
        return len(removed), directories, kept

    def write(self, service: Service, parent: str, item: BaselineFile) -> str | None:
        """Create one file and write its content; return its id, or None (with nothing left) when a step failed."""
        file_id = None
        try:
            file_id = self.make(service, parent, PurePosixPath(item.path).name, "REG")
            octets = {"Content-Type": "application/octet-stream"}
            self.boot.provider(
                service, "PUT", f"/data/{file_id}/content", "content", content=item.content(), headers=octets
            )
            return file_id
        except RestError:
            if file_id is not None:
                self.remove(service, [file_id])
            return None

    def read_back(self, service: Service, file_id: str) -> bytes | None:
        url = f"{service.url}{PROVIDER_API}/data/{file_id}/content"
        try:
            headers = bearer(self.boot.admin_token())
            return self.boot.rest.call("GET", url, label=f"{service.name} content", headers=headers).content
        except RestError:
            return None

    def differing(self, service: Service, files: Sequence[BaselineFile], ids: dict[str, str]) -> list[BaselineFile]:
        """The baseline files that are missing or whose bytes, read back, are not theirs."""
        with ThreadPoolExecutor(WORKERS) as pool:
            read = pool.map(lambda f: f.path in ids and self.read_back(service, ids[f.path]) == f.content(), files)
            return [f for f, ok in zip(files, read, strict=True) if not ok]

    def settle(self, space: str) -> tuple[int, dict[str, str]]:
        """Bring one space to its baseline; return the count of entries removed and the files' ids by path."""
        service, files = writer_of(space), baseline(space)
        removed, directories, ids = self.clear(service, self.root(service, space), files)
        pending = [f for f in files if f.path not in ids]
        for _ in range(WRITE_ROUNDS):
            with ThreadPoolExecutor(WORKERS) as pool:
                made = pool.map(lambda f: (f.path, self.write(service, directories[parent_of(f.path)], f)), pending)
                ids.update({path: file_id for path, file_id in made if file_id is not None})
            pending = self.differing(service, files, ids)
            if not pending:
                return removed, ids
            self.remove(service, [ids.pop(f.path) for f in pending if f.path in ids])
        raise RuntimeError(f"{len(pending)} baseline file(s) of {space} could not be written in {WRITE_ROUNDS} rounds")

    def replicate(self, file_id: str, timeout_s: float) -> None:
        """Replicate a file from the holder to the next supporter of the study space and wait for the transfer."""
        target = next(name for name in SPACES[STUDY_SPACE] if name != HOLDER)
        body = {
            "type": "replication",
            "dataSourceType": "file",
            "fileId": file_id,
            "replicatingProviderId": self.boot.ids.providers[target],
        }
        holder = provider(HOLDER)
        tid = self.boot.provider(holder, "POST", "/transfers", "transfer", json=body)["transferId"]
        began = time.monotonic()
        while time.monotonic() - began < timeout_s:
            record = self.boot.provider(holder, "GET", f"/transfers/{tid}", "transfer")
            state = record.get("transferStatus") or record.get("replicationStatus")
            if state in TRANSFER_DONE:
                return
            if state in TRANSFER_FAILED:
                raise RuntimeError(f"the baseline replication ended {state}")
            time.sleep(2)
        raise TimeoutError("the baseline replication did not end in time")

    def reset(self, timeout_s: float) -> dict[str, int]:
        """Bring every space's content back to its baseline; return how many entries each space lost."""
        removed = {}
        for space in SPACES:
            removed[space], ids = self.settle(space)
            if space == STUDY_SPACE:
                self.replicate(ids[REPLICATED_FILE], timeout_s)
        return removed
