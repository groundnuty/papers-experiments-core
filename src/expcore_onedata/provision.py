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

- **Placement is what the block map says** (O01). A replication job that reports `completed` is a report; `replicate`
  returns only when `GET /data/{id}/distribution` on the receiving provider covers the whole file on consecutive polls,
  and raises `NotPlaced` with the shortfall otherwise. An eviction is confirmed the same way, by the block map showing
  the provider holding nothing of the file.
- **The data provider's own copy is never evicted** (O09): a request to evict from the provider `[placement] holder`
  names is refused before anything is sent, by name and by id.
- **Teardown removes only what the change log says this provisioner created** (O09). A file is deleted by its logged id;
  a directory only when nothing but logged, already deleted entries was in it; anything else is reported as left, and
  the report's `ok` is False. Nothing outside the log is touched, and no token or user is revoked.
- **The waits are bounded** (O07, `consistency.py`) and happen before or after a run, never in the agent's time.

The change log is a JSON-lines file: one line per creation and per deletion with the path, the id and the time. It holds
no token and no credential.
"""

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expcore_onedata import consistency
from expcore_onedata.client import OnezoneClient, OutOfScope, PlatformError, answered_absent
from expcore_onedata.config import PlatformConfig
from expcore_onedata.placement import Range, held_bytes, provider_blocks, uncovered
from expcore_onedata.rangemap import PlacementSettings, safe_path

LISTING_LIMIT = 1000
_SEGMENT = re.compile(r"[A-Za-z0-9_=-]{1,512}")  # the charset of a file id the client accepts
LISTING_ATTRIBUTES = ["name", "file_id", "size", "type"]  # Onedata 25 names attributes in snake case
SHORT_FILES_SHOWN = 50


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


class Provisioner:
    """Creates, places and removes study data in the configured space, and confirms each step by the block map."""

    def __init__(
        self,
        client: OnezoneClient,
        config: PlatformConfig,
        settings: PlacementSettings,
        *,
        provider_ids: Mapping[str, str],
        log: Path | str,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if settings.holder not in config.providers:
            raise ProvisionError("the data provider named in the settings is not a declared provider")
        self._client, self._config, self._settings = client, config, settings
        self._ids = dict(provider_ids)
        self._log = Path(log)
        self._clock, self._sleep = clock, sleep
        self._verified_name: str | None = None
        self._root: str | None = None

    @property
    def settings(self) -> PlacementSettings:
        """The bounds and the data provider this provisioner works to."""
        return self._settings

    # -- scope -----------------------------------------------------------------
    def verify_space(self) -> str:
        """Read the space's live name and refuse one that differs from the configuration or lies outside the prefixes.

        The name is Onezone's, read by the client (`space_name`, read again on every call). The client's own gate refuses every
        mutation whose target space lies outside the prefixes; this adds that the space is the configured one.
        """
        try:
            name = self._client.space_name(self._config.space.id)
        except OutOfScope:
            raise Refused("the configured space is unknown to Onezone; nothing was changed") from None
        if not name.startswith(self._config.space.prefixes):
            raise Refused("the space's live name is outside the configured prefixes; nothing was changed")
        if name != self._config.space.name:
            raise Refused("the space's live name is not the configured one; nothing was changed")
        self._verified_name = name
        return name

    def _verified(self) -> None:
        """Verify the space once, before this provisioner first changes anything."""
        if self._verified_name is None:
            self.verify_space()

    def provider_id(self, name: str) -> str:
        """Return a declared provider's Onezone id, or refuse when it is unknown."""
        if name not in self._config.providers:
            raise Refused("the provider is not declared")
        if name not in self._ids:
            raise Refused("the provider's id is unknown: declare it in platform.toml or resolve it from the space view")
        return self._ids[name]

    def provider_named(self, provider_id: str) -> str | None:
        """Return the declared provider whose Onezone id this is, or None when no declared provider has it."""
        return next((name for name, known in self._ids.items() if known == provider_id), None)

    @property
    def space_id(self) -> str:
        """The configured space's id."""
        return self._config.space.id

    def root(self, *, wait: bool = True) -> str:
        """Return the space's root directory id on the data provider.

        With `wait` (provisioning) a root that answers "no such file" is waited out, within bounds (O07). Without it (an
        instrument's read, O08) there is one request and no wait: a real answer, a 404 included, is raised as it came.
        """
        if self._root is None:
            self._root = consistency.space_root(
                self._client,
                self._settings.holder,
                self._config.space.id,
                **({} if wait else {"tries": 1}),
                sleep=self._sleep,
            )
        return self._root

    # -- the change log --------------------------------------------------------
    def _write(self, action: str, **fields: Any) -> None:
        """Append one line to the change log; fail loudly, since an unlogged creation could never be torn down."""
        self._log.parent.mkdir(parents=True, exist_ok=True)
        line = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "action": action, **fields}
        with open(self._log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
            handle.flush()

    def logged(self) -> list[dict[str, Any]]:
        """Return the creations the log holds that no later deletion line cancels, oldest first."""
        if not self._log.exists():
            return []
        created: dict[str, dict[str, Any]] = {}
        for text in self._log.read_text(encoding="utf-8").splitlines():
            if not text.strip():
                continue
            line = json.loads(text)
            if line.get("action") == "create":
                created[line["id"]] = line
            elif line.get("action") == "delete":
                created.pop(line["id"], None)
        return list(created.values())

    # -- listing and creation --------------------------------------------------
    def children(self, directory_id: str, provider: str | None = None) -> dict[str, Entry]:
        """Return every entry of a directory by name, following the pages to the end."""
        provider = provider or self._settings.holder
        entries: dict[str, Entry] = {}
        params: dict[str, Any] = {"limit": LISTING_LIMIT, "attribute": LISTING_ATTRIBUTES}
        while True:
            page = self._client.get(f"/data/{_segment(directory_id)}/children", provider=provider, params=params)
            if not isinstance(page, dict) or not isinstance(page.get("children"), list):
                raise PlatformError("a listing answer holds no 'children'")
            for item in page["children"]:
                entry = listing_entry(item)
                entries[entry.name] = entry
            if page.get("isLast", True):
                return entries
            token = page.get("nextPageToken")
            if not token:
                raise PlatformError("a listing is not on its last page but names no next page")
            params = {**params, "token": token}

    def resolve(self, path: str, provider: str | None = None, *, wait: bool = True) -> Entry:
        """Walk a space-relative path down from the root and return its entry; raise KeyError naming the part missing.

        `wait` is as for `root`: an instrument passes False so that no real answer is retried inside its read.
        """
        node = Entry(self.root(wait=wait), "", 0, "DIR")
        for part in safe_path(path):
            found = self.children(node.file_id, provider).get(part)
            if found is None:
                raise KeyError(f"no entry {part!r} on the way down the path")
            node = found
        return node

    def make_dir(self, path: str) -> str:
        """Create every directory of a path that is missing, log each creation, and return the last one's id."""
        parent = self.root()
        for part in safe_path(path):
            existing = self.children(parent).get(part)
            if existing is not None:
                if existing.kind != "DIR":
                    raise ProvisionError("a part of the path exists and is not a directory")
                parent = existing.file_id
                continue
            parent = self._create(parent, part, "DIR", path)
        return parent

    def _create(self, parent_id: str, name: str, kind: str, path: str) -> str:
        self._verified()
        answer = self._client.mutate(
            "POST",
            f"/data/{_segment(parent_id)}/children",
            provider=self._settings.holder,
            params={"name": name, "type": kind},
        )
        file_id = answer.get("fileId") if isinstance(answer, dict) else None
        if not isinstance(file_id, str) or not file_id:
            raise PlatformError("a creation answer holds no file id")
        self._write("create", id=file_id, kind=kind, path=path)
        return file_id

    def put_file(self, path: str, data: bytes) -> str:
        """Create a file with this content on the data provider, in blocks, and verify its size; return its id.

        A file already there is refused, since replacing it would break the rule that teardown touches only what the
        log says was created.
        """
        *directory, name = safe_path(path)
        parent = self.make_dir("/".join(directory)) if directory else self.root()
        if name in self.children(parent):
            raise ProvisionError("the file already exists; this provisioner will not overwrite or adopt it")
        file_id = self._create(parent, name, "REG", path)
        self._verified()
        for offset in range(0, len(data), self._settings.chunk_bytes):
            self._client.mutate(
                "PUT",
                f"/data/{_segment(file_id)}/content",
                provider=self._settings.holder,
                params={"offset": offset},
                content=data[offset : offset + self._settings.chunk_bytes],
                timeout_s=self._settings.request_timeout_s,
            )
        size = self._client.get(f"/data/{_segment(file_id)}", provider=self._settings.holder).get("size")
        if size != len(data):
            raise ProvisionError("the size read back after an upload is not the size uploaded")
        return file_id

    # -- block maps ------------------------------------------------------------
    def blocks(self, file_id: str, provider: str, *, reader: str | None = None) -> list[Range]:
        """Return the blocks a provider holds of a file, read from the block map as the `reader` provider sees it."""
        answer = self._client.get(f"/data/{_segment(file_id)}/distribution", provider=reader or provider)
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
        provider's block map.
        """
        self._verified()
        return self._client.read_range(
            f"/data/{_segment(file_id)}/content",
            provider=provider,
            offset=offset,
            length=length,
            timeout_s=self._settings.request_timeout_s,
        )

    def holds(self, provider: str, file_ids: Iterable[str]) -> int:
        """Return how many bytes `provider` holds of these files together, by the block map."""
        return sum(held_bytes(self.blocks(fid, provider)) for fid in file_ids)

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

    def require_empty(self, provider: str, file_ids: list[str]) -> Confirmation:
        """Wait for `provider` to hold nothing of these files, by consecutive block-map polls; raise NotPlaced if not."""
        result = consistency.converge(
            lambda: self.holds(provider, file_ids),
            lambda held: held == 0,
            timeout_s=self._settings.settle_timeout_s,
            poll_s=self._settings.poll_s,
            consecutive=self._settings.consecutive_polls,
            clock=self._clock,
            sleep=self._sleep,
        )
        report = PlacementReport(len(file_ids), int(result.value or 0), 0, 0, ())
        if not result.converged:
            raise NotPlaced(f"the provider still holds {report.held_bytes} B of the files after the bound", report)
        return Confirmation(True, report, len(result.checks), 0.0)

    # -- transfers -------------------------------------------------------------
    def _schedule(self, kind: str, file_id: str, provider: str) -> str:
        field_name = "replicatingProviderId" if kind == "replication" else "evictingProviderId"
        self._verified()
        answer = self._client.mutate(
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
            raise ProvisionError("an empty file has nothing to place")
        wanted = {entry.file_id: [(0, entry.size)]}
        began = self._clock()
        tid = self._schedule("replication", entry.file_id, to_provider)
        done = consistency.require_ended(
            consistency.wait_transfers(
                self._client,
                to_provider,
                [tid],
                timeout_s=self._settings.transfer_timeout_s,
                poll_s=self._settings.poll_s,
                clock=self._clock,
                sleep=self._sleep,
            ),
            "replication",
        )
        confirmation = self.confirm_placed(to_provider, wanted)
        if not confirmation.placed:
            raise NotPlaced(
                f"the transfer reported {done.states()} but the block map does not cover the file "
                f"({confirmation.report.bytes_short} B short)",
                confirmation.report,
            )
        return {
            "transfer_states": done.states(),
            "placement": confirmation.report.to_json(),
            "seconds": round(self._clock() - began, 3),
        }

    def evict(self, path: str, from_provider: str) -> dict[str, Any]:
        """Evict a file from a provider and return a record once its block map there is empty; never from the holder."""
        if from_provider == self._settings.holder or (
            from_provider in self._ids and self._ids[from_provider] == self._ids.get(self._settings.holder)
        ):
            raise Refused("refusing to evict the data provider's own copy; nothing was sent")
        self._verified()
        entry = self.resolve(path)
        tid = self._schedule("eviction", entry.file_id, from_provider)
        done = consistency.require_ended(
            consistency.wait_transfers(
                self._client,
                from_provider,
                [tid],
                timeout_s=self._settings.transfer_timeout_s,
                poll_s=self._settings.poll_s,
                clock=self._clock,
                sleep=self._sleep,
            ),
            "eviction",
        )
        confirmation = self.require_empty(from_provider, [entry.file_id])
        return {"transfer_states": done.states(), "empty": confirmation.placed}

    # -- teardown --------------------------------------------------------------
    def _gone(self, file_id: str) -> bool:
        """Tell whether the data provider has no such entry: it answered a 404, or Onedata 25.0's 400 posix enoent
        (`answered_absent`). A read that worked is "no"; any other failure, another 400 or an unreadable body among
        them, raises, so the poll never counts as absence."""
        try:
            self._client.get(f"/data/{_segment(file_id)}", provider=self._settings.holder)
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
        is deleted only when everything in it is an entry this teardown has confirmed gone.
        """
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
                self._client.mutate("DELETE", f"/data/{_segment(fid)}", provider=self._settings.holder)
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
