# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/scripted/provision.py, blob 1969c9f03ed263e249b7cba8ec4ee353d329ef03
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: icsa-2027-experiments @ 6fc798cf, day0/scripts/odsafe.py:1-17,33-55 (a teardown that finds files left changes nothing)
#   merged: icsa-2027-experiments @ 6fc798cf, day0/scripts/odclean.py:237 (DELETE of a file by its id)
#   changes: see git history of this file from its import commit
"""Study data in the space: create, replicate, evict, confirm and tear down, scoped to the configured prefixes (O01, O09).

`Provisioner` is the only writer of study data. Every request that can change state goes through
`OnezoneClient.mutate` (or `read_range`) with the space's name, so a name outside `[space] prefixes` sends nothing, and
before it first acts the provisioner reads the space's live name from Onezone and refuses one that differs from the
configured name or lies outside the prefixes. On top of that gate:

- **A large file is never held whole** (O25). `upload` sends a local file a chunk at a time, skips a target that
  already holds its bytes, resumes one that holds a prefix, and reports success only once the size and SHA-256 read
  back match; `read_file` reads a file back by streamed ranges, hashing as it goes. Every write carries a content type.
- **A listing ends only on its last-page marker** (O26, `next_page_token`): a page without one is refused.
- **Placement is what the block map says** (O01). A replication job that reports `completed` is a report; `replicate`
  returns only when `GET /data/{id}/distribution` on the receiving provider covers the whole file on consecutive polls,
  and raises `NotPlaced` with the shortfall otherwise. An eviction is confirmed the same way, by the block map showing
  the provider holding nothing of the file.
- **The data provider's own copy is never evicted** (O09): a request to evict from the provider `[placement] holder`
  names is refused before anything is sent, by name and by id.
- **Teardown removes only what the change log says this provisioner created** (O09). A file is deleted by its logged id;
  a directory only when nothing but logged, already deleted entries was in it; anything else is reported as left, and
  the report's `ok` is False. Nothing outside the log is touched, and no token or user is revoked.
- **A space held on a read-only imported storage is never written** (O41, `[space] import`): every request that would
  create, write or delete a file or a directory there, through any provider, and a replication onto the data
  provider, is refused before it is sent (`read_only`). Reads, replications onto other providers and evictions from
  them go on as in any space; `storageimport.py` verifies such a space against its dataset's manifest.
- **The waits are bounded** (O07, `consistency.py`) and happen before or after a run, never in the agent's time.
- **A plumbing read that timed out is tried again, boundedly and in the log** (O29, `readretry.py`): the listings, the
  size and attribute reads, `read_file`'s metadata read, the visibility wait, the polls of a transfer's record that end
  an operation (`replicate`, `evict`), the mutation gate's reads and a range read's or a streamed read's gate, which
  find the target's space (`verify_space` too), and the block-map reads outside a poll (`blocks(retry=)`, which the
  placement's own reads pass), three attempts with pauses of 2 s and 5 s. `reads_retried` counts the retries. Never
  retried: a mutation, a streamed or range read itself, a block map read as a probe reads it (one attempt unless the
  caller names a policy), and what is read inside a poll that already counts a failed read as a poll that did not match.
  An instrument's own read (`resolve(wait=False)`) is one attempt.

The change log is a JSON-lines file: one line per creation and per deletion with the path, the id and the time. It holds
no token and no credential.
"""

import dataclasses
import functools
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expcore_onedata import consistency
from expcore_onedata.client import OnezoneClient, OutOfScope, PlatformError, answered_absent
from expcore_onedata.config import PlatformConfig, StudySpace
from expcore_onedata.placement import Range, held_bytes, provider_blocks, uncovered
from expcore_onedata.rangemap import PlacementSettings, safe_path
from expcore_onedata.readretry import ONE_ATTEMPT, ReadRetry
from expcore_onedata.streamed import OCTET_STREAM, Digest, Upload, chunks, media_type

LISTING_LIMIT = 1000
_SEGMENT = re.compile(r"[A-Za-z0-9_=-]{1,512}")  # the charset of a file id the client accepts
LISTING_ATTRIBUTES = ["name", "file_id", "size", "type"]  # Onedata 25 names attributes in snake case
SHORT_FILES_SHOWN = 50
# An eviction scheduled right after a provider fetched the bytes on the fly completes on Onedata 25.0 with
# filesEvicted 0 and removes nothing, whichever provider schedules it; the same eviction about 30 s later works. So
# while the block map still shows bytes after an eviction has ended, it is scheduled again after a pause, at most this
# many times in all; each check waits this long for the map to show the provider empty.
EVICTION_ATTEMPTS = 5
EVICTION_RETRY_PAUSE_S = 10.0
EVICTION_CHECK_S = 10.0

Listings = dict[tuple[str, str], dict[str, "Entry"]]  # (provider, directory id) -> its entries, for one operation


class ProvisionError(RuntimeError):
    """Provisioning refused or could not confirm what it did; the message holds no id, URL or name of the federation."""


class Refused(ProvisionError):
    """A request was refused before anything was sent: outside the prefixes, or against the data provider's own copy."""


class NotPlaced(ProvisionError):
    """The block map does not show what a transfer or a read was meant to place."""

    def __init__(self, message: str, report: "PlacementReport | None" = None) -> None:
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class Entry:
    """A file or directory as a listing shows it."""

    file_id: str
    name: str
    size: int
    kind: str


@dataclass(frozen=True)
class PlacementReport:
    """How much of what was wanted a provider holds, by its block map."""

    files: int
    held_bytes: int
    files_short: int
    bytes_short: int
    short_file_ids: tuple[str, ...]

    @property
    def fully_placed(self) -> bool:
        """True when nothing is short and at least one file was asked about."""
        return self.files > 0 and self.files_short == 0

    def to_json(self) -> dict[str, Any]:
        """Return the report as plain JSON."""
        return {
            "files": self.files,
            "held_bytes": self.held_bytes,
            "files_short": self.files_short,
            "bytes_short": self.bytes_short,
            "short_file_ids": list(self.short_file_ids),
            "fully_placed": self.fully_placed,
        }


@dataclass(frozen=True)
class Confirmation:
    """The outcome of waiting for the block map to show a placement: the last report and how the polls went."""

    placed: bool
    report: PlacementReport
    polls: int
    seconds: float


@dataclass(frozen=True)
class TeardownReport:
    """What a teardown deleted and what it found left: each leftover with its path and the reason."""

    deleted: tuple[str, ...] = ()
    left: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """True when everything the log says was created is gone, as the data provider reads it (#114)."""
        return not self.left

    def raise_if_left(self) -> None:
        """Raise ProvisionError naming how many entries remain, so a caller cannot read leftovers as success."""
        if self.left:
            raise ProvisionError(f"teardown left {len(self.left)} entr(ies) behind; see the report")


def resolve_provider_ids(config: PlatformConfig) -> dict[str, str]:
    """Map each declared provider's name to the id Onezone gives it; platform.toml always declares it."""
    return {provider.name: provider.id for provider in config.providers.values() if provider.id is not None}


def _segment(identifier: str) -> str:
    """Return an opaque id as a path segment, unescaped; refuse one that would need escaping.

    The client refuses a mutation path with an escape in it (it could be read as another path), so an id is never
    percent-encoded here: one outside the plain charset of a file id is refused at the source instead of reaching the
    client as a path it would reject.
    """
    if not isinstance(identifier, str) or not _SEGMENT.fullmatch(identifier):
        raise PlatformError("an id is not a plain path segment")
    return identifier


def listing_entry(item: Any) -> Entry:
    """Read one entry of a children listing; refuse one without an id, a size or a name."""
    fid = item.get("file_id") or item.get("fileId") if isinstance(item, dict) else None
    if not fid or not isinstance(item.get("name"), str) or item.get("size") is None:
        raise PlatformError("a listing entry has no file id, name or size")
    return Entry(str(fid), item["name"], int(item["size"]), str(item.get("type") or ""))


def _hashing(blocks: Iterable[tuple[int, bytes]], hasher: Any) -> Iterator[tuple[int, bytes]]:
    """Pass `(offset, bytes)` blocks on, adding each to `hasher` first, so the bytes sent are the bytes hashed."""
    for offset, block in blocks:
        hasher.update(block)
        yield offset, block


def next_page_token(page: Any) -> str | None:
    """Read a children page's paging (O26): None when it is the listing's last page, else the next page's token.

    Onedata's API says a page may be shorter than its limit and still not be the last, and that `isLast` is the only
    valid sign that every entry has been listed. On 25.0 that was measured, in
    `deploy/compose-federation/day0/2026-11-12-federation-facts-listing-end.md`: every page carries `nextPageToken`,
    `isLast` and `children`; `isLast` is a boolean, false before the last page and true on it; the token is a non-empty
    string on every page, the last included; a page the directory's last entries fill is followed by an empty page with
    `isLast` true; and the page after a last page answers 400 `posix` `eagain`. So the end is `isLast` true alone,
    whatever the token, and an empty last page is no error. A page without `children`, without a boolean `isLast`, or not the last and without a token,
    is refused (PlatformError), never taken for the end of the listing: a listing cut there would pass for a whole
    directory, and a file on a later page for a missing one.
    """
    if not isinstance(page, dict) or not isinstance(page.get("children"), list):
        raise PlatformError("a listing answer holds no 'children'")
    last = page.get("isLast")
    if not isinstance(last, bool):
        raise PlatformError("a listing page has no last-page marker (a boolean isLast), so it may be cut: refused")
    if last:
        return None
    token = page.get("nextPageToken")
    if not isinstance(token, str) or not token:
        raise PlatformError("a listing is not on its last page but names no next page")
    return token


class Provisioner:
    """Creates, places and removes study data in one configured space, and confirms each step by the block map.

    `space` is the label of the space it acts in (`[[spaces]]`); without one it is the only space, and a configuration
    that declares several refuses an unnamed provisioner. A `[[spaces]]` entry's `holder` is its data provider; the
    one-space form's is `[placement] holder`. Either way it must support the space, or the provisioner is refused. Every
    mutation it sends is bound to its space: the client refuses one whose target lies in any other space, whatever its
    prefixes (`OnezoneClient.mutate`'s `space`). With several spaces every message starts with the space's position
    (`spaces[i]`), never its label or name.
    """

    def __init__(
        self,
        client: OnezoneClient,
        config: PlatformConfig,
        settings: PlacementSettings,
        *,
        provider_ids: Mapping[str, str],
        log: Path | str,
        space: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        entry = config.space_named(space)
        where = f"{entry.position}: " if len(config.spaces) > 1 else ""
        holder = entry.holder if config.spaces_table else settings.holder  # [[spaces]] requires its own (spaces.py)
        if holder is None or holder not in config.providers:
            raise ProvisionError(f"{where}the data provider named in the settings is not a declared provider")
        if holder not in entry.support.providers:
            raise ProvisionError(f"{where}the data provider does not support the space; nothing was sent")
        self._client, self._config, self._entry, self._where = client, config, entry, where
        self._settings = settings if holder == settings.holder else dataclasses.replace(settings, holder=holder)
        self._ids = dict(provider_ids)
        self._log = Path(log)
        self._log_lock = threading.Lock()
        self._role: str | None = None  # the role that claimed the log through this provisioner (`claim_log`)
        self._clock, self._sleep = clock, sleep
        self._retry = ReadRetry(clock=clock, sleep=sleep)  # this provisioner's plumbing reads, and their count (O29)
        self._verified_name: str | None = None
        self._root: str | None = None
        self._visible: set[tuple[str, str]] = set()  # (provider, file id) seen at its size there (`await_visible`)

    @property
    def settings(self) -> PlacementSettings:
        """The bounds and the data provider this provisioner works to: the space's own holder when it declares one."""
        return self._settings

    @property
    def space(self) -> StudySpace:
        """The declared space this provisioner acts in."""
        return self._entry

    @property
    def where(self) -> str:
        """The start of every message: the space's position and a colon when several spaces are declared, else empty."""
        return self._where

    @property
    def client(self) -> OnezoneClient:
        """The client every request of this provisioner goes through."""
        return self._client

    @property
    def read_only(self) -> bool:
        """Whether the space's data provider holds it on a read-only imported storage (`[space] import`, O41)."""
        return self._entry.imported is not None and self._entry.imported.readonly

    def _refuse_read_only(self, what: str) -> None:
        """Refuse, before anything is sent, a request a read-only imported storage cannot take (O41)."""
        if self.read_only:
            raise Refused(
                f"{self._where}the space is held on a read-only imported storage, so {what} is refused; nothing was sent"
            )

    def mutate(self, method: str, path: str, **options: Any) -> Any:
        """Send a mutation through the client's gate, bound to this provisioner's space: one whose target lies in any
        other declared space is refused before it is sent, even under a shared prefix (O09). The gate's own reads, which
        find the target's space, are tried again when they time out, under this provisioner's policy (O29); the mutation
        is sent once.

        In a space held on a read-only imported storage every change of a file's content or of the tree
        (`/data/...`), through any provider, is refused unsent (O41): op-worker answers a creation or a write there
        EROFS, a delete there removes the file from the space and leaves it on the storage, and a write through another
        provider takes the data provider's blocks from it for good, since nothing can be replicated back onto a
        read-only storage (op-worker f118ed9, `test_distributed/readonly_storage_test_SUITE.erl`)."""
        if path.startswith("/data/"):
            self._refuse_read_only(f"a {method} of a file or directory")
        return self._client.mutate(method, path, space=self._entry.space, gate_retry=self._retry, **options)

    @property
    def retry(self) -> ReadRetry:
        """The policy for this provisioner's plumbing reads (three attempts, 2 s and 5 s apart), which the reset and
        the ingest that work through it pass to their own reads too, so one count says how many were retried (O29)."""
        return self._retry

    @property
    def reads_retried(self) -> int:
        """How many reads that timed out were tried again under `retry`, for the record of what this provisioner did."""
        return self._retry.retries

    def clock(self) -> float:
        """The provisioner's clock (monotonic seconds, or a test's)."""
        return self._clock()

    def sleep(self, seconds: float) -> None:
        """The provisioner's sleep (or a test's, which only moves its clock)."""
        self._sleep(seconds)

    # -- scope -----------------------------------------------------------------
    def verify_space(self) -> str:
        """Read the space's live name and refuse one that differs from the configuration or lies outside the prefixes.

        The name is Onezone's, read by the client (`space_name`, read again on every call, and again when it timed out,
        under this provisioner's policy: O29). The client's own gate refuses every mutation whose target space lies
        outside the prefixes; this adds that the space is the configured one.
        """
        space = self._entry.space
        try:
            name = self._client.space_name(space.id, retry=self._retry)
        except OutOfScope:
            raise Refused(f"{self._where}the configured space is unknown to Onezone; nothing was changed") from None
        if not name.startswith(space.prefixes):
            raise Refused(f"{self._where}the space's live name is outside the configured prefixes; nothing was changed")
        if name != space.name:
            raise Refused(f"{self._where}the space's live name is not the configured one; nothing was changed")
        self._verified_name = name
        return name

    def _verified(self) -> None:
        """Verify the space once, before this provisioner first changes anything."""
        if self._verified_name is None:
            self.verify_space()

    def provider_id(self, name: str) -> str:
        """Return a declared provider's Onezone id, or refuse when it is unknown."""
        if name not in self._config.providers:
            raise Refused(f"{self._where}the provider is not declared")
        if name not in self._ids:
            raise Refused(
                f"{self._where}the provider's id is unknown: declare it in platform.toml or resolve it from the space view"
            )
        return self._ids[name]

    def provider_named(self, provider_id: str) -> str | None:
        """Return the declared provider whose Onezone id this is, or None when no declared provider has it."""
        return next((name for name, known in self._ids.items() if known == provider_id), None)

    @property
    def space_id(self) -> str:
        """The id of the space this provisioner acts in."""
        return self._entry.space.id

    def root(self, *, wait: bool = True) -> str:
        """Return the space's root directory id on the data provider.

        With `wait` (provisioning) a root that answers "no such file" is waited out, within bounds (O07). Without it (an
        instrument's read, O08) there is one request and no wait: a real answer, a 404 included, is raised as it came.
        """
        if self._root is None:
            self._root = consistency.space_root(
                self._client,
                self._settings.holder,
                self._entry.space.id,
                **({} if wait else {"tries": 1}),
                sleep=self._sleep,
                retry=self._retry if wait else ONE_ATTEMPT,
            )
        return self._root

    # -- the change log --------------------------------------------------------
    def _write(self, action: str, **fields: Any) -> None:
        """Append one line to the change log; fail loudly, since an unlogged creation could never be torn down."""
        self._log.parent.mkdir(parents=True, exist_ok=True)
        line = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "action": action, **fields}
        with self._log_lock, open(self._log, "a", encoding="utf-8") as handle:  # a bulk eviction writes from threads
            handle.write(json.dumps(line, sort_keys=True) + "\n")
            handle.flush()

    def _lines(self) -> list[dict[str, Any]]:
        if not self._log.exists():
            return []
        return [json.loads(text) for text in self._log.read_text(encoding="utf-8").splitlines() if text.strip()]

    def logged(self) -> list[dict[str, Any]]:
        """Return the creations the log holds that no later deletion line cancels, oldest first."""
        created: dict[str, dict[str, Any]] = {}
        for line in self._lines():
            if line.get("action") == "create":
                created[line["id"]] = line
            elif line.get("action") == "delete":
                created.pop(line["id"], None)
        return list(created.values())

    def claim_log(self, role: str) -> None:
        """Reserve the change log for one role that needs a log of its own, such as a progressive ingest, whose restore
        takes what the log says was created for its own and whose restored files a teardown over the log would delete.

        The claim is a line of the log, so another provisioner on the same file, or a later process, sees it too, and
        `teardown` refuses a claimed log; each creation this provisioner logs after it carries the role. Claiming again
        for the same role is harmless (one study's runs, one after another). ProvisionError, with nothing written, when
        the log holds another role's claim or a creation without the role, which another user made."""
        lines = self._lines()
        claims = {line.get("role") for line in lines if line.get("action") == "claim"}
        foreign = [line for line in lines if line.get("action") == "create" and line.get("role") != role]
        if claims - {role} or foreign:
            raise ProvisionError(
                f"{self._where}the change log is shared with another user; a {role} needs a provisioner and a change "
                "log of its own"
            )
        if not claims:
            self._write("claim", role=role)
        self._role = role

    def _claimed_by(self) -> str | None:
        return next((line.get("role") for line in self._lines() if line.get("action") == "claim"), None)

    # -- listing and creation --------------------------------------------------
    def children(
        self, directory_id: str, provider: str | None = None, *, listings: Listings | None = None, retry: bool = True
    ) -> dict[str, Entry]:
        """Return every entry of a directory by name, following the pages to the end.

        `listings` is one operation's cache: a directory it holds is not listed again, and one listed whole is added to
        it, so an operation over many files lists each directory once. A listing that fails adds nothing. A page read
        that timed out is tried again under the provisioner's policy (O29), unless `retry` is False, an instrument's
        read."""
        provider = provider or self._settings.holder
        if listings is not None and (provider, directory_id) in listings:
            return listings[(provider, directory_id)]
        entries = self._list_pages(directory_id, provider, self._retry if retry else ONE_ATTEMPT)
        if listings is not None:
            listings[(provider, directory_id)] = entries
        return entries

    def _list_pages(self, directory_id: str, provider: str, reads: ReadRetry) -> dict[str, Entry]:
        """List one directory on a provider, following the pages to the end, each page read under `reads`."""
        entries: dict[str, Entry] = {}
        params: dict[str, Any] = {"limit": LISTING_LIMIT, "attribute": LISTING_ATTRIBUTES}
        while True:
            page = self._client.get(
                f"/data/{_segment(directory_id)}/children", provider=provider, params=params, retry=reads
            )
            token = next_page_token(page)
            for item in page["children"]:
                entry = listing_entry(item)
                entries[entry.name] = entry
            if token is None:
                return entries
            params = {**params, "token": token}

    def resolve(
        self, path: str, provider: str | None = None, *, wait: bool = True, listings: Listings | None = None
    ) -> Entry:
        """Walk a space-relative path down from the root and return its entry; raise KeyError naming the part missing.

        `wait` is as for `root`: an instrument passes False so that no real answer is retried inside its read, and no
        read that timed out either (O29): its listings are one attempt each.
        `listings` is the operation's cache of directory listings (`children`), shared by every path it resolves.
        """
        node = Entry(self.root(wait=wait), "", 0, "DIR")
        for part in safe_path(path):
            found = self.children(node.file_id, provider, listings=listings, retry=wait).get(part)
            if found is None:
                raise KeyError(f"no entry {part!r} on the way down the path")
            node = found
        return node

    def resolve_many(self, paths: Iterable[str], provider: str | None = None) -> dict[str, Entry]:
        """Resolve many space-relative paths with one listing of each directory on their way, never one per path."""
        listings: Listings = {}
        return {path: self.resolve(path, provider, listings=listings) for path in dict.fromkeys(paths)}

    def walk(self, directory_id: str | None = None, provider: str | None = None) -> Iterator[tuple[Entry, bool]]:
        """Yield every entry under a directory (the space's root by default), each directory listed once, with
        whether it is a leaf: a file, or a directory with nothing in it. Directories are entered breadth first."""
        listings: Listings = {}
        pending = [directory_id or self.root()]
        while pending:
            current = pending.pop(0)
            for entry in self.children(current, provider, listings=listings).values():
                if entry.kind == "DIR":
                    inside = self.children(entry.file_id, provider, listings=listings)
                    yield entry, not inside
                    pending.append(entry.file_id)
                else:
                    yield entry, True
            listings.pop((provider or self._settings.holder, current), None)  # entered: no longer needed

    def make_dir(self, path: str) -> str:
        """Create every directory of a path that is missing, log each creation, and return the last one's id."""
        parent = self.root()
        for part in safe_path(path):
            existing = self.children(parent).get(part)
            if existing is not None:
                if existing.kind != "DIR":
                    raise ProvisionError(f"{self._where}a part of the path exists and is not a directory")
                parent = existing.file_id
                continue
            parent = self._create(parent, part, "DIR", path)
        return parent

    def _create(self, parent_id: str, name: str, kind: str, path: str) -> str:
        self._verified()
        answer = self.mutate(
            "POST",
            f"/data/{_segment(parent_id)}/children",
            provider=self._settings.holder,
            params={"name": name, "type": kind},
        )
        file_id = answer.get("fileId") if isinstance(answer, dict) else None
        if not isinstance(file_id, str) or not file_id:
            raise PlatformError("a creation answer holds no file id")
        self._write("create", id=file_id, kind=kind, path=path, **({} if self._role is None else {"role": self._role}))
        return file_id

    def put_file(self, path: str, data: bytes, *, content_type: str = OCTET_STREAM) -> str:
        """Create a file with this content on the data provider, in blocks, and verify its size; return its id.

        A file already there is refused, since replacing it would break the rule that teardown touches only what the
        log says was created. Every write carries `content_type`, since Onedata 25.0 answers 415 to one without. A
        file too large to hold in memory goes through `upload`, which also skips, resumes and verifies by hash.
        """
        media = media_type(content_type)
        *directory, name = safe_path(path)
        parent = self.make_dir("/".join(directory)) if directory else self.root()
        if name in self.children(parent):
            raise ProvisionError(
                f"{self._where}the file already exists; this provisioner will not overwrite or adopt it"
            )
        file_id = self._create(parent, name, "REG", path)
        step = self._settings.chunk_bytes
        self._write_chunks(file_id, ((at, data[at : at + step]) for at in range(0, len(data), step)), media)
        if self._size_of(file_id) != len(data):
            raise ProvisionError(f"{self._where}the size read back after an upload is not the size uploaded")
        return file_id

    def upload(
        self, path: str, source: Path | str, *, content_type: str = OCTET_STREAM, stop: int | None = None
    ) -> Upload:
        """Upload a local file to a path of the space on the data provider, never holding it whole; return what was
        done (O25). With `stop`, only the local file's first `stop` bytes are its content here: the target ends as
        that prefix, verified by its hash, and a later upload with a larger stop, or none, resumes from it.

        The file is read and sent `chunk_bytes` at a time, each chunk a PUT at its offset through the client's mutation
        gate (the space's name is checked first, and the file's space before every write), carrying `content_type`.
        What the target already holds decides what is sent; its bytes are read back by `read_file`, hashed as they
        come:

        - nothing: the file is created, logged, and sent whole (`created`);
        - the same size and SHA-256 as the local file: nothing is sent (`skipped`). Nothing changes, so a file another
          made is skipped too, and stays out of the log;
        - a strict prefix of the local file, in a file the log says this provisioner created: the rest is sent from
          where the target ends, by offset writes (`resumed`);
        - anything else in a file it created: the file is written again from its first byte, the first write without
          an offset, which truncates it (`restarted`);
        - anything else in a file it did not create: refused before anything is written, as `put_file` refuses (O09).

        Success is reported only once the size and the SHA-256 read back equal those of the bytes read from the local
        file; otherwise ProvisionError. A file that changes while it is uploaded fails that check.
        """
        media, source = media_type(content_type), Path(source)
        size = source.stat().st_size  # a local file that is not there is refused before anything is asked
        if stop is not None:
            if isinstance(stop, bool) or not isinstance(stop, int) or not 1 <= stop <= size:
                raise ProvisionError(f"{self._where}an upload's stop is a whole number of bytes within the local file")
            size = stop
        self._verified()
        *directory, name = safe_path(path)
        parent = self.make_dir("/".join(directory)) if directory else self.root()
        existing = self.children(parent).get(name)
        if existing is None:
            action, start, hasher = "created", 0, hashlib.sha256()
            file_id = self._create(parent, name, "REG", path)
        else:
            action, start, hasher = self._compare(existing, source, size)
            file_id = existing.file_id
            if action == "skipped":
                return Upload(file_id, Digest(size, hasher.hexdigest()), action, 0)
        # The caller's stop, never the size taken above: without one, a local file appended to meanwhile is read to its
        # new end and fails the size check below instead of being cut to its first bytes and reported done.
        blocks = _hashing(chunks(source, self._settings.chunk_bytes, start=start, stop=stop), hasher)
        sent = self._write_chunks(file_id, blocks, media, truncate=action == "restarted")
        expected = Digest(start + sent, hasher.hexdigest())
        if expected.size != size:
            raise ProvisionError(f"{self._where}the local file changed size while it was uploaded")
        if self._size_of(file_id) != size:
            raise ProvisionError(f"{self._where}the size read back after an upload is not the size of the bytes sent")
        if self.read_file(file_id, size=size) != expected:
            raise ProvisionError(
                f"{self._where}the content read back after an upload does not have the SHA-256 of the bytes sent"
            )
        return Upload(file_id, expected, action, sent)

    def _compare(self, existing: Entry, source: Path, size: int) -> tuple[str, int, Any]:
        """Decide how an upload treats a target that exists: return the action, the offset to send from, and the hash of
        the local bytes before that offset. Resuming or restarting writes into the file, so it needs the log to say
        this provisioner created it."""
        if existing.kind != "REG":
            raise ProvisionError(f"{self._where}the path exists and is not a regular file; nothing was written")
        hasher = hashlib.sha256()
        if existing.size <= size:
            held = self.read_file(existing.file_id, size=existing.size)
            for _, block in chunks(source, self._settings.chunk_bytes, stop=existing.size):
                hasher.update(block)
            if held.sha256 == hasher.hexdigest():
                if existing.size == size:
                    return "skipped", size, hasher
                self._require_created(existing.file_id)
                return "resumed", existing.size, hasher
        self._require_created(existing.file_id)
        return "restarted", 0, hashlib.sha256()

    def _require_created(self, file_id: str) -> None:
        if file_id not in {line["id"] for line in self.logged()}:
            raise ProvisionError(
                f"{self._where}the file exists with other content and this provisioner did not create it; it will not "
                "overwrite or adopt it, and nothing was written"
            )

    def _write_chunks(
        self, file_id: str, blocks: Iterable[tuple[int, bytes]], media: str, *, truncate: bool = False
    ) -> int:
        """Write `(offset, bytes)` blocks to a file's content on the data provider, each a PUT through the gate; return
        the bytes sent. With `truncate`, the first write goes without an offset, which truncates the file (with an
        empty body when there is no block)."""
        self._verified()
        sent = 0
        for offset, block in blocks:
            if truncate and sent == 0 and offset != 0:
                raise ProvisionError(f"{self._where}a truncating write starts at the file's first byte")
            self._put(file_id, block, None if truncate and sent == 0 else offset, media)
            sent += len(block)
        if truncate and sent == 0:
            self._put(file_id, b"", None, media)
        return sent

    def _put(self, file_id: str, block: bytes, offset: int | None, media: str) -> None:
        """One write of content, bound to this provisioner's space: at `offset`, or without one, which truncates the file
        first. A body of `fresh_connection_bytes` or more goes on a connection of its own (#186)."""
        self.mutate(
            "PUT",
            f"/data/{_segment(file_id)}/content",
            provider=self._settings.holder,
            params=None if offset is None else {"offset": offset},
            content=block,
            timeout_s=self._settings.request_timeout_s,
            content_type=media,
            fresh=len(block) >= self._settings.fresh_connection_bytes,  # a large body never rides a reused connection
        )

    def _size_of(self, file_id: str) -> Any:
        """The size a file's attributes on the data provider give now."""
        answer = self._client.get(f"/data/{_segment(file_id)}", provider=self._settings.holder, retry=self._retry)
        return answer.get("size") if isinstance(answer, dict) else None

    def read_file(
        self,
        file_id: str,
        *,
        provider: str | None = None,
        dest: Path | str | None = None,
        size: int | None = None,
        deadline_s: float | None = None,
    ) -> Digest:
        """Read a file's whole content through a provider (the data provider unless named), hashing it as it comes,
        and write it to `dest` when one is given; return its size and SHA-256 (O25).

        The read is the client's `read_content`: by ranges of `chunk_bytes`, each gated as a range read and streamed,
        so the file is never held. It is bounded in bytes by `size`, or else by the size the file's attributes give,
        which the content must match exactly; and in time by `deadline_s` (the placement's `transfer_timeout_s` unless
        given), no wait for the platform lasting longer than `request_timeout_s`. Every range is bound to this
        provisioner's space, as a mutation is. Through a provider other than the data provider it first waits until the
        file is visible there (`await_visible`). With `dest`, the bytes go to `<dest>.part`, which replaces `dest` only
        once the read is whole and is removed when it fails.
        """
        self._verified()
        provider = provider or self._settings.holder
        deadline = self._settings.transfer_timeout_s if deadline_s is None else deadline_s
        self.await_visible(provider, [file_id])
        if size is None:
            answer = self._client.get(
                f"/data/{_segment(file_id)}", provider=provider, retry=self._retry, deadline_s=deadline
            )
            size = answer.get("size") if isinstance(answer, dict) else None
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise PlatformError("a file's attributes hold no size, so its read has no bound")
        read = functools.partial(
            self._client.read_content,
            f"/data/{_segment(file_id)}/content",
            provider=provider,
            size=size,
            chunk_bytes=self._settings.chunk_bytes,
            deadline_s=deadline,
            timeout_s=self._settings.request_timeout_s,
            space=self._entry.space,
            gate_retry=self._retry,
        )
        if dest is None:
            return read()
        target = Path(dest)
        partial = target.with_name(target.name + ".part")
        try:
            with open(partial, "wb") as handle:
                found = read(sink=handle.write)
            os.replace(partial, target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return found

    # -- visibility ------------------------------------------------------------
    def await_visible(self, provider: str, file_ids: Iterable[str]) -> float:
        """Wait, bounded, until a provider sees each file at its size on the data provider; return the seconds waited.

        A file written through the data provider is not visible through another at once (Onedata 25.0 answers it with
        a 404 or 400 posix enoent for some seconds), so a read or a transfer through that provider waits here first
        (`consistency.wait_visible`, bounded by `VISIBLE_TIMEOUT_S`; past it, `NotVisible` names the provider by its
        label, and any other error fails closed). The data provider, the writer, needs no wait, and a provider and file
        seen once are not asked again. Instruments never call this: a probe records what it reads (O08)."""
        if provider == self._settings.holder:
            return 0.0
        waited = 0.0
        for file_id in file_ids:
            if (provider, file_id) in self._visible:
                continue
            size = self._client.get(  # with `name`, which a deleted entry refuses: `size` alone may read a tombstone
                f"/data/{_segment(file_id)}",
                provider=self._settings.holder,
                params={"attribute": list(consistency.VISIBLE_ATTRIBUTES)},
                retry=self._retry,
            ).get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise PlatformError("the data provider's answer for a file's size holds no size")
            waited += consistency.wait_visible(
                self._client,
                provider,
                _segment(file_id),
                size=size,
                clock=self._clock,
                sleep=self._sleep,
                retry=self._retry,
            )
            self._visible.add((provider, file_id))
        return round(waited, 3)

    # -- block maps ------------------------------------------------------------
    def blocks(
        self, file_id: str, provider: str, *, reader: str | None = None, retry: ReadRetry = ONE_ATTEMPT
    ) -> list[Range]:
        """Return the blocks a provider holds of a file, read from the block map as the `reader` provider sees it.

        One attempt unless the caller names a policy: the probes record what the map shows and the polls that judge a
        placement count a failed read as a poll that did not match, so a stall there stays the stall it is. A caller
        whose failure would end its operation (the placement's read before it begins) passes the provisioner's
        (`retry`), and a read that timed out is tried again (O29)."""
        answer = self._client.get(f"/data/{_segment(file_id)}/distribution", provider=reader or provider, retry=retry)
        try:
            return provider_blocks(answer, self.provider_id(provider))
        except ValueError as error:
            raise PlatformError(f"the block map is unreadable ({error})") from None

    def placement_report(self, provider: str, wanted: Mapping[str, list[Range]]) -> PlacementReport:
        """For file id -> ranges that `provider` must hold: how many bytes it holds of them and what is missing."""
        held = 0
        short: dict[str, int] = {}
        for file_id, ranges in wanted.items():
            blocks = self.blocks(file_id, provider)
            held += held_bytes(blocks)
            gaps = uncovered(blocks, ranges)
            if gaps:
                short[file_id] = sum(length for _, length in gaps)
        return PlacementReport(
            len(wanted), held, len(short), sum(short.values()), tuple(sorted(short))[:SHORT_FILES_SHOWN]
        )

    def read_through(self, provider: str, file_id: str, offset: int, length: int) -> int:
        """Read a range of a file through a provider, which fetches what it lacks; return how many bytes were read."""
        return len(self.fetch(provider, file_id, offset, length))

    def fetch(self, provider: str, file_id: str, offset: int, length: int) -> bytes:
        """Read a range of a file through a provider, which fetches what it lacks; return the bytes.

        This is a range read and never a transfer job (O02). It is gated like a mutation, because it changes the
        provider's block map. The provider must see the file first (`await_visible`).
        """
        self._verified()
        self.await_visible(provider, [file_id])
        return self._client.read_range(
            f"/data/{_segment(file_id)}/content",
            provider=provider,
            offset=offset,
            length=length,
            timeout_s=self._settings.request_timeout_s,
            space=self._entry.space,
            gate_retry=self._retry,
        )

    def holds(self, provider: str, file_ids: Iterable[str], *, retry: ReadRetry = ONE_ATTEMPT) -> int:
        """Return how many bytes `provider` holds of these files together, by the block map, each read as `blocks`
        reads it under `retry`."""
        return sum(held_bytes(self.blocks(fid, provider, retry=retry)) for fid in file_ids)

    def confirm_placed(self, provider: str, wanted: Mapping[str, list[Range]]) -> Confirmation:
        """Wait, within the bound, for the block map to show every wanted range on `provider` on consecutive polls."""
        began = self._clock()
        result = consistency.converge(
            lambda: self.placement_report(provider, wanted),
            lambda report: report.fully_placed,
            timeout_s=self._settings.settle_timeout_s,
            poll_s=self._settings.poll_s,
            consecutive=self._settings.consecutive_polls,
            clock=self._clock,
            sleep=self._sleep,
        )
        report = result.value if result.value is not None else PlacementReport(len(wanted), 0, len(wanted), 0, ())
        return Confirmation(result.converged, report, len(result.checks), round(self._clock() - began, 3))

    def require_empty(self, provider: str, file_ids: list[str], *, timeout_s: float | None = None) -> Confirmation:
        """Wait for `provider` to hold nothing of these files, by consecutive block-map polls; raise NotPlaced if not.

        The wait is bounded by `timeout_s`, the settings' `settle_timeout_s` unless given."""
        result = consistency.converge(
            lambda: self.holds(provider, file_ids),
            lambda held: held == 0,
            timeout_s=self._settings.settle_timeout_s if timeout_s is None else timeout_s,
            poll_s=self._settings.poll_s,
            consecutive=self._settings.consecutive_polls,
            clock=self._clock,
            sleep=self._sleep,
        )
        report = PlacementReport(len(file_ids), int(result.value or 0), 0, 0, ())
        if not result.converged:
            raise NotPlaced(
                f"{self._where}the provider still holds {report.held_bytes} B of the files after the bound", report
            )
        return Confirmation(True, report, len(result.checks), 0.0)

    # -- transfers -------------------------------------------------------------
    def schedule_transfer(self, kind: str, file_id: str, provider: str) -> str:
        """Schedule a replication to, or an eviction from, a provider of a file (or of a directory, which covers every
        file under it), through that provider once it sees the entry (`await_visible`), through the mutation gate; log
        it and return the transfer's id. Its end is a report, never placement (O01): a caller confirms by the block
        map."""
        field_name = "replicatingProviderId" if kind == "replication" else "evictingProviderId"
        if kind == "replication" and provider == self._settings.holder:
            self._refuse_read_only("a replication onto the data provider")  # op-worker answers it EROFS
        self._verified()
        self.await_visible(provider, [file_id])
        answer = self.mutate(
            "POST",
            "/transfers",
            provider=provider,
            json={"type": kind, field_name: self.provider_id(provider), "dataSourceType": "file", "fileId": file_id},
        )
        tid = answer.get("transferId") if isinstance(answer, dict) else None
        if not isinstance(tid, str) or not tid:
            raise PlatformError("a transfer answer holds no transfer id")
        self._write("transfer", id=tid, kind=kind, file=file_id, provider=provider)
        return tid

    def transfers(self) -> list[tuple[str, str, str]]:
        """Return `(provider, transfer id, kind)` of every transfer the log says this provisioner scheduled."""
        if not self._log.exists():
            return []
        lines = (json.loads(t) for t in self._log.read_text(encoding="utf-8").splitlines() if t.strip())
        return [(x["provider"], x["id"], x["kind"]) for x in lines if x.get("action") == "transfer"]

    def replicate(self, path: str, to_provider: str) -> dict[str, Any]:
        """Replicate a whole file to a provider and return a record once its block map there covers the file (O01).

        The transfer's own state is recorded and never believed: when the job reports `completed` and the map does not
        cover the file, this raises `NotPlaced`.
        """
        self._verified()
        entry = self.resolve(path)
        if entry.size < 1:
            raise ProvisionError(f"{self._where}an empty file has nothing to place")
        wanted = {entry.file_id: [(0, entry.size)]}
        began = self._clock()
        tid = self.schedule_transfer("replication", entry.file_id, to_provider)
        done = consistency.require_ended(
            consistency.wait_transfers(
                self._client,
                to_provider,
                [tid],
                timeout_s=self._settings.transfer_timeout_s,
                poll_s=self._settings.poll_s,
                clock=self._clock,
                sleep=self._sleep,
                retry=self._retry,
            ),
            "replication",
        )
        confirmation = self.confirm_placed(to_provider, wanted)
        if not confirmation.placed:
            raise NotPlaced(
                f"{self._where}the transfer reported {done.states()} but the block map does not cover the file "
                f"({confirmation.report.bytes_short} B short)",
                confirmation.report,
            )
        return {
            "transfer_states": done.states(),
            "placement": confirmation.report.to_json(),
            "seconds": round(self._clock() - began, 3),
        }

    def refuse_holder(self, from_provider: str) -> None:
        """Refuse, before anything is sent, to evict from the data provider: named by its name, by another name that
        has its id, or by its id itself (O09)."""
        holder_id = self._ids.get(self._settings.holder)
        if (
            from_provider == self._settings.holder
            or (from_provider in self._ids and self._ids[from_provider] == holder_id)
            or (holder_id is not None and from_provider == holder_id)
        ):
            raise Refused(f"{self._where}refusing to evict the data provider's own copy; nothing was sent")

    def check_eviction(self, from_provider: str) -> None:
        """Refuse, before anything is sent, a bulk eviction or a reset's from a provider that is the data provider
        (`refuse_holder`), is not declared, or does not support this provisioner's space."""
        self.refuse_holder(from_provider)
        if from_provider not in self._config.providers:
            raise Refused(f"{self._where}refusing to evict from a provider that is not declared; nothing was sent")
        if from_provider not in self._entry.support.providers:
            raise Refused(f"{self._where}refusing to evict from a provider that does not support the space")

    def evict(self, path: str, from_provider: str) -> dict[str, Any]:
        """Evict a file from a provider and return a record once its block map there is empty; never from the holder.

        The eviction is scheduled through that provider, with `evictingProviderId` naming it, as the federation's own
        reset does. A transfer that ended is never the evidence (O01): while the provider's block map still shows bytes,
        the eviction is scheduled again after `EVICTION_RETRY_PAUSE_S`, up to `EVICTION_ATTEMPTS` in all, and after the
        last the whole settle bound is waited before `NotPlaced` refuses it. The record says how many attempts it took.
        Many files go through `eviction.evict_ids` or `eviction.evict_tree`, which verify them together under the same
        retry rule.
        """
        self.refuse_holder(from_provider)
        self._verified()
        entry = self.resolve(path)
        for attempt in range(1, EVICTION_ATTEMPTS + 1):
            tid = self.schedule_transfer("eviction", entry.file_id, from_provider)
            done = consistency.require_ended(
                consistency.wait_transfers(
                    self._client,
                    from_provider,
                    [tid],
                    timeout_s=self._settings.transfer_timeout_s,
                    poll_s=self._settings.poll_s,
                    clock=self._clock,
                    sleep=self._sleep,
                    retry=self._retry,
                ),
                "eviction",
            )
            if attempt == EVICTION_ATTEMPTS:  # the last: wait the whole settle bound, then refuse (NotPlaced)
                confirmation = self.require_empty(from_provider, [entry.file_id])
                break
            try:
                confirmation = self.require_empty(from_provider, [entry.file_id], timeout_s=EVICTION_CHECK_S)
                break
            except NotPlaced:  # completed, and the block map still holds bytes: never evicted; schedule it again
                self._sleep(EVICTION_RETRY_PAUSE_S)
        return {"transfer_states": done.states(), "empty": confirmation.placed, "attempts": attempt}

    # -- teardown --------------------------------------------------------------
    def _gone(self, file_id: str) -> bool:
        """Tell whether the data provider has no such entry: it answered a 404, or Onedata 25.0's 400 posix enoent
        (`answered_absent`). A read that worked is "no"; any other failure, another 400 or an unreadable body among
        them, raises, so the poll never counts as absence."""
        try:
            self._client.get(f"/data/{_segment(file_id)}", provider=self._settings.holder, retry=ONE_ATTEMPT)
        except PlatformError as error:
            if answered_absent(error):
                return True
            raise
        return False

    def _confirm_gone(self, file_id: str) -> bool:
        """Tell whether the data provider shows the entry absent on consecutive polls, within the bound.

        It stops at once when it does. The absence is the data provider's, the one entries are created and deleted
        on, and says nothing of the other providers or of storage, where a directory's delete may still be under way
        (#114): nothing here creates a name again after its teardown, since every scratch name is new."""
        return consistency.converge(
            lambda: self._gone(file_id),
            bool,
            timeout_s=self._settings.settle_timeout_s,
            poll_s=self._settings.poll_s,
            consecutive=self._settings.consecutive_polls,
            clock=self._clock,
            sleep=self._sleep,
        ).converged

    def teardown(self) -> TeardownReport:
        """Delete exactly the entries the log says were created, newest first, and report anything left (O09, O11).

        A deletion is logged only once the entry's absence is confirmed, so an entry whose DELETE answered success and
        that is still there keeps its creation record and a later teardown deletes and checks it again. A directory
        is deleted only when everything in it is an entry this teardown has confirmed gone. A change log a role has
        claimed (`claim_log`) is refused with nothing sent: it holds files that role restores, not files to remove.
        """
        claimed = self._claimed_by()
        if claimed is not None:
            raise ProvisionError(
                f"{self._where}the change log is a {claimed}'s; a teardown over it would delete the files it restores, "
                "so nothing was deleted"
            )
        self._verified()
        deleted: list[str] = []
        left: list[dict[str, Any]] = []
        removed: set[str] = set()
        for line in reversed(self.logged()):
            fid = line["id"]
            if line["kind"] == "DIR":
                try:
                    strangers = [e for e in self.children(fid).values() if e.file_id not in removed]
                except PlatformError as error:
                    if not answered_absent(error):
                        raise
                    strangers = []  # the directory is already gone, so it holds nothing; its absence is checked below
                if strangers:
                    left.append({"path": line["path"], "reason": "holds entries this provisioner did not create"})
                    continue
            try:
                self.mutate("DELETE", f"/data/{_segment(fid)}", provider=self._settings.holder)
            except PlatformError as error:
                if not answered_absent(error):  # already gone is confirmed below; any other refusal leaves it
                    left.append({"path": line["path"], "reason": "the delete was refused or failed"})
                    continue
            if not self._confirm_gone(fid):
                left.append({"path": line["path"], "reason": "still listed after its delete"})
                continue
            self._write("delete", id=fid, path=line["path"])
            removed.add(fid)
            deleted.append(fid)
        return TeardownReport(tuple(deleted), tuple(left))
