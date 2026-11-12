"""The baseline content of the spaces: bring each space back to exactly its synthetic files, written through its owner,
read every one back, and replicate one file to the study space's first mirror so it has an ended transfer to read.

A provider finishes deleting a directory after the call returns, and with canonical storage paths a new directory of
the same name has the same path on storage, so the old one's late removal can take the new one's files. Reset
therefore never deletes and makes again a directory of the same name: a baseline directory is kept and emptied file by
file (the large listing directory is kept whole when it is intact), and any other entry is removed, a directory of
another name included. Every baseline file is then read back through the writer, and a file whose bytes differ is
written again, for a bounded number of rounds.

A space declared `baseline = false` has no baseline files: it is brought back to empty, everything in it removed, and
nothing is written into it. A study space with none has no replicated file either, so it gets no transfer record.
"""

import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath
from typing import Any

from fedboot import Bootstrap
from fedlayout import LISTING_DIR, REPLICATED_FILE, BaselineFile, baseline_plan
from fedrest import PROVIDER_API, RestError, bearer
from fedspec import Service, Spec

WORKERS = 16
PAGE = 1000
WRITE_ROUNDS = 3
TRANSFER_DONE = {"completed"}
TRANSFER_FAILED = {"failed", "cancelled", "aborted"}


def parent_of(path: str) -> str:
    """The directory a baseline path lies in, relative to the space root; '' for the root itself."""
    return "/".join(PurePosixPath(path).parts[:-1])


def top_directories(files: Iterable[BaselineFile]) -> list[str]:
    """The baseline's directories, each directly under the root; a deeper one is refused."""
    names = sorted({parent_of(f.path) for f in files} - {""})
    if any("/" in name for name in names):
        raise ValueError("a baseline directory must lie directly under the space root")
    return names


def next_page_token(page: Any) -> str | None:
    """A children page's token for the next page, or None on the last page.

    The end of a listing is `isLast` true, and only that: on Onedata 25.0 the last page still carries a token, and a
    page the directory's last entries fill says `isLast` false and is followed by an empty last page
    (`day0/2026-11-12-federation-facts-listing-end.md`). A page without a list of children or a boolean `isLast`, or
    one that is not the last and gives no token, may be cut, so it is refused, never taken as the end."""
    if not isinstance(page, dict) or not isinstance(page.get("children"), list):
        raise RuntimeError("a listing page holds no list of children")
    last = page.get("isLast")
    if not isinstance(last, bool):
        raise RuntimeError("a listing page has no boolean isLast, its last-page marker, so it may be cut")
    if last:
        return None
    token = page.get("nextPageToken")
    if not isinstance(token, str) or not token:
        raise RuntimeError("a listing page that is not the last names no next page")
    return token


def writer_of(spec: Spec, space: str) -> Service:
    """The provider a space's baseline is written through: its owner."""
    return spec.provider(spec.space(space).owner)


def intact(found: dict[str, dict[str, Any]], expected: dict[str, int]) -> bool:
    """A directory holds exactly the expected regular files, each of its size."""
    return set(found) == set(expected) and all(
        found[path].get("type") == "REG" and found[path].get("size") == size for path, size in expected.items()
    )


class Content:
    """Bring the spaces' content back to the baseline."""

    def __init__(self, boot: Bootstrap) -> None:
        self.boot = boot
        self.spec = boot.layout.spec
        self.plan = baseline_plan(self.spec)

    def root(self, service: Service, space: str) -> str:
        sid = self.boot.ids.spaces[space]
        return self.boot.provider(service, "GET", f"/spaces/{sid}", "space")["fileId"]

    def children(self, service: Service, directory: str) -> list[dict[str, Any]]:
        """Every entry of a directory, page by page to the page that says it is the last (`next_page_token`)."""
        entries: list[dict[str, Any]] = []
        params: dict[str, Any] = {"limit": PAGE, "attribute": ["file_id", "name", "type", "size"]}
        while True:
            page = self.boot.provider(service, "GET", f"/data/{directory}/children", "children", params=params)
            token = next_page_token(page)
            entries += page["children"]
            if token is None:
                return entries
            params = {**params, "token": token}

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
        service, files = writer_of(self.spec, space), self.plan[space]
        removed, directories, ids = self.clear(service, self.root(service, space), files)
        if not files:
            return removed, ids
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

    def replicate(self, file_id: str, target: str, timeout_s: float) -> None:
        """Replicate a file from the data provider to `target` and wait for the transfer to end."""
        body = {
            "type": "replication",
            "dataSourceType": "file",
            "fileId": file_id,
            "replicatingProviderId": self.boot.ids.providers[target],
        }
        holder = self.spec.provider(self.spec.holder)
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
        """Bring every space's content back to its baseline; return how many entries each space lost. A study space
        with no mirror has nothing to replicate to, and one declared `baseline = false` has no file to replicate, so
        neither has a transfer record."""
        removed = {}
        for declared in self.spec.spaces:
            removed[declared.name], ids = self.settle(declared.name)
            if declared.name == self.spec.study_space and declared.mirrors and declared.baseline:
                self.replicate(ids[REPLICATED_FILE], declared.mirrors[0], timeout_s)
        return removed
