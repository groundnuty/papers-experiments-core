# Provenance: new
"""Provision a space by storage import instead of an upload, and show it holds exactly its dataset (O41, #184).

A federation's owner can give Onedata 25.0 a directory that already holds a dataset, as an imported storage, instead of
uploading the files one by one through REST: the provider scans the directory and makes each entry in the space. The
federation makes the storage and the support (deploy/compose-federation does, `fedboot.py`); the core declares the
space so (`[space] import`, `spaces.py`) and checks the result here, through the space's provisioner
(`ImportedSpace`):

1. **`check_storage`**: the data provider's Onepanel shows the space supported by an imported storage, read-only as
   declared, in the `auto` mode, with continuous scans off. A continuous scan would scan again on its own, during a
   run, and with the detection of modifications on it claims a file changed on the storage whole on the data provider
   and takes its blocks from every other provider (op-worker f118ed9, `storage_import_engine.erl`,
   `maybe_update_file_location`; `fslogic_location.erl`, `update_imported_file_doc`), so it is refused. A manual
   import, file by file, is refused too: it has no scan to wait for.
2. **`await_scan`**: the scan's state (`GET .../storage-import/auto/info`) is read until it is `completed`, a later
   scan than the one counted before when re-scanning, with no failed file; `failed` or `aborted` fails at once, and the
   wait is bounded. The state is a report, never the evidence (O01): the listing and the block maps are.
3. **`verify`** (`SpaceCheck`, which checks an uploaded space the same way): on each provider named (the data provider
   by default), the space's listing holds exactly the manifest's paths, kinds and sizes, nothing more, on
   `consecutive_polls` reads in a row within the bound (O07); then every file of the data provider's listing is whole
   there by its block map (O01), as op-worker makes an imported file's location: one block `[0, size)`
   (`create_imported_file_doc`); and with `hashes`, every file read back through the data provider has the manifest's
   SHA-256.
4. **`rescan`**: forces a new scan (`client.force_scan`, gated as a mutation of the space) and records whether every
   file kept its id. With the detection of modifications and deletions off, a scan only adds what is new on the
   storage (`maybe_update_file`), so the ids stay; a file deleted from a read-only space comes back under a new id,
   since the delete removes its sync record and leaves the file on the storage (`fslogic_delete.erl`,
   `maybe_delete_storage_file`).

`provision` runs 1 to 3 and returns the record (version 1): the manifest's counts and SHA-256, the storage's flags,
the scan's state with the seconds waited and the reads taken, each provider's listing (its counts, the manifest's files
it lacks or holds at another size or kind, by position, `manifest.files[i]`, and how many entries it holds beyond the
manifest), the block maps' and the hashes' results, the seconds of each phase, and the reads retried (O29). Any
failure raises `ImportFailed` with that record so far. A message and a record name a provider by its position and a
file by its position in the manifest, never a path or a name (O12).

**What an imported space cannot take** (`docs/storage-import.md` has the sources): on a read-only storage, any write,
creation or replication onto the data provider (EROFS), so the provisioner refuses them and a progressive ingest is
refused (O27); a delete there removes the file from the space and leaves it on the storage; and a write or an eviction
that takes the data provider's blocks cannot be undone, since nothing is replicated back onto it. Reads, on-demand reads
and replications through the other providers, their evictions (the reset's, O01), QoS requirements and the transfer
views go on as in a space whose data were uploaded.
"""

from collections.abc import Sequence
from typing import Any

from expcore_onedata import consistency
from expcore_onedata.client import PlatformError
from expcore_onedata.config import Space
from expcore_onedata.importmanifest import Manifest, load_manifest, manifest_of_tree, write_manifest
from expcore_onedata.placement import file_fully_placed
from expcore_onedata.provision import Entry, Provisioner, ProvisionError

VERSION = 1
SCAN_STATES = ("enqueued", "running", "aborting", "completed", "failed", "aborted")  # onepanel's, auto import's info
SCAN_FAILED = ("failed", "aborted")
SCAN_COUNTERS = ("createdFiles", "modifiedFiles", "deletedFiles", "unmodifiedFiles", "failedFiles", "totalScans")
SCAN_POLL_S = 5.0  # between two reads of the scan's state


class ImportFailed(ProvisionError):
    """A space could not be shown to hold its dataset; `record` is what was recorded up to the failure."""

    def __init__(self, message: str, record: dict[str, Any]) -> None:
        super().__init__(message)
        self.record = record


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _table(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class SpaceCheck:
    """One space checked against its dataset's manifest through the space's provisioner: the listing on each provider
    named, the block maps and, when asked, the bytes on the data provider (`verify`). It serves an uploaded space as
    well as an imported one, so the two are checked by the same code (the live equivalence tests do)."""

    def __init__(self, provisioner: Provisioner, manifest: Manifest) -> None:
        self._p, self._manifest = provisioner, manifest
        self._retried = provisioner.reads_retried
        self._listings: dict[str, dict[str, Entry]] = {}  # provider -> path -> entry, the last listing read there
        self._record: dict[str, Any] = {
            "version": VERSION,
            "space": provisioner.space.position,
            "ok": False,
            "readonly": provisioner.read_only,
            "manifest": {
                "files": len(manifest.files),
                "directories": len(manifest.directories),
                "bytes": manifest.bytes,
                "sha256": manifest.digest(),
            },
            "storage": None,
            "scan": None,
            "listing": {},
            "blocks": None,
            "hashes": None,
            "seconds": {},
            "reads_retried": 0,
        }

    @property
    def space(self) -> Space:
        """The space checked."""
        return self._p.space.space

    @property
    def record(self) -> dict[str, Any]:
        """What has been recorded so far, a copy."""
        self._record["reads_retried"] = self._p.reads_retried - self._retried
        return {**self._record, "listing": dict(self._record["listing"]), "seconds": dict(self._record["seconds"])}

    def _fail(self, what: str) -> ImportFailed:
        self._record["ok"] = False  # whatever passed before: the record is the failing check's
        return ImportFailed(f"{self._p.where}{what}", self.record)

    def _list(self, provider: str) -> dict[str, Entry]:
        """Every entry of the space on a provider, by its path, each directory listed once, breadth first."""
        found: dict[str, Entry] = {}
        pending = [("", self._p.root())]
        while pending:
            prefix, directory = pending.pop(0)
            for name, entry in self._p.children(directory, provider).items():
                path = f"{prefix}{name}"
                found[path] = entry
                if entry.kind == "DIR":
                    pending.append((f"{path}/", entry.file_id))
        self._listings[provider] = found
        return found

    def listing(self, provider: str) -> dict[str, Entry]:
        """The space's entries on a provider by path, read now: what the equivalence checks compare."""
        return dict(self._list(provider))

    def _compare(self, provider: str) -> dict[str, Any]:
        """What a provider's listing holds against the manifest: its counts, the manifest's files it lacks or holds at
        another size or kind, its directories it lacks, and how many entries it holds beyond the manifest."""
        listed = self._list(provider)
        files = {f.path: (i, f) for i, f in enumerate(self._manifest.files)}
        directories = set(self._manifest.directories)
        missing, wrong_size, wrong_kind = [], [], []
        for path, (index, planned) in files.items():
            entry = listed.get(path)
            if entry is None:
                missing.append(self._manifest.label(index))
            elif entry.kind != "REG":
                wrong_kind.append(self._manifest.label(index))
            elif entry.size != planned.size:
                wrong_size.append(self._manifest.label(index))
        regular = [e for e in listed.values() if e.kind == "REG"]
        return {
            "files": len(regular),
            "directories": sum(1 for e in listed.values() if e.kind == "DIR"),
            "bytes": sum(e.size for e in regular),
            "missing": missing,
            "missing_directories": sum(1 for d in directories if d not in listed or listed[d].kind != "DIR"),
            "extra": sum(1 for path in listed if path not in files and path not in directories),
            "wrong_size": wrong_size,
            "wrong_kind": wrong_kind,
        }

    @staticmethod
    def _exact(facts: dict[str, Any]) -> bool:
        return not (
            facts["missing"]
            or facts["missing_directories"]
            or facts["extra"]
            or facts["wrong_size"]
            or facts["wrong_kind"]
        )

    def verify(
        self, *, timeout_s: float, providers: Sequence[str] | None = None, hashes: bool = False
    ) -> dict[str, Any]:
        """Show the space holds exactly the manifest on each provider named (the data provider by default), every
        file whole on the data provider by its block map, and with `hashes` the manifest's bytes; record and return the
        record, or raise ImportFailed with it."""
        holder, settings = self._p.settings.holder, self._p.settings
        named = list(dict.fromkeys(providers if providers is not None else [holder]))
        if not named or any(p not in self._p.space.support.providers for p in named):
            raise ValueError("a space is verified on providers that support it, at least one")
        if holder not in named:
            named.insert(0, holder)  # the block maps are the data provider's, so its listing is read first
        # A verification stands on its own: nothing an earlier one found is kept as this one's.
        self._record.update(ok=False, listing={}, blocks=None, hashes=None)
        began = self._p.clock()
        for provider in named:  # one bound for all: each provider waits only for what the ones before it left
            label = self._p.client.provider_label(provider)
            started = self._p.clock()
            result = consistency.converge(
                lambda p=provider: self._compare(p),
                self._exact,
                timeout_s=max(0.0, timeout_s - (started - began)),
                poll_s=settings.poll_s,
                consecutive=settings.consecutive_polls,
                clock=self._p.clock,
                sleep=self._p.sleep,
            )
            # The seconds from the verification's start to the first of the reads in a row that held the manifest.
            first = result.checks[-settings.consecutive_polls]["t_rel_s"] if result.converged else None
            seconds = None if first is None else round(started - began + first, 3)
            facts = result.value or {}
            self._record["listing"][label] = {**facts, "converged": result.converged, "seconds": seconds,
                                              "polls": len(result.checks)}  # fmt: skip
            if not result.converged:
                raise self._fail(
                    f"{label}: the space does not hold exactly the manifest after {timeout_s:g} s "
                    f"({len(facts.get('missing', []))} missing, {len(facts.get('wrong_size', []))} of another size, "
                    f"{len(facts.get('wrong_kind', []))} of another kind, {facts.get('extra', 0)} beyond it)"
                )
        self._record["seconds"]["listing"] = round(self._p.clock() - began, 3)
        self._check_blocks(holder)
        if hashes:
            self._check_hashes(holder)
        self._record["ok"] = True
        return self.record

    def _files_on(self, provider: str) -> list[tuple[int, Entry]]:
        listed = self._listings[provider]
        return [(i, listed[f.path]) for i, f in enumerate(self._manifest.files)]

    def _check_blocks(self, holder: str) -> None:
        """Every file of the data provider's listing is whole there by its block map (O01)."""
        began, files = self._p.clock(), self._files_on(holder)

        def whole(item: tuple[int, Entry]) -> bool:
            _, entry = item
            if entry.size == 0:
                return True  # an empty file has no block to hold
            return file_fully_placed(self._p.blocks(entry.file_id, holder, retry=self._p.retry), entry.size)

        try:
            results = consistency.bounded_map(whole, files, self._p.settings.concurrency)
        except PlatformError as error:
            raise self._fail(f"a block map on the data provider could not be read ({error})") from None
        short = [self._manifest.label(i) for (i, _), ok in zip(files, results, strict=True) if not ok]
        self._record["blocks"] = {"files": len(files), "whole": len(files) - len(short), "not_whole": short}
        self._record["seconds"]["blocks"] = round(self._p.clock() - began, 3)
        if short:
            raise self._fail(f"{len(short)} file(s) are not whole on the data provider by its block map")

    def _check_hashes(self, holder: str) -> None:
        """Every file read back through the data provider has the manifest's SHA-256 (O25: read by ranges, hashed)."""
        began, files = self._p.clock(), self._files_on(holder)

        def equal(item: tuple[int, Entry]) -> bool:
            index, entry = item
            planned = self._manifest.files[index]
            return self._p.read_file(entry.file_id, size=planned.size).sha256 == planned.sha256

        try:
            results = consistency.bounded_map(equal, files, self._p.settings.concurrency)
        except PlatformError as error:
            raise self._fail(f"a file could not be read back ({error})") from None
        differ = [self._manifest.label(i) for (i, _), ok in zip(files, results, strict=True) if not ok]
        self._record["hashes"] = {"files": len(files), "equal": len(files) - len(differ), "differ": differ}
        self._record["seconds"]["hashes"] = round(self._p.clock() - began, 3)
        if differ:
            raise self._fail(f"{len(differ)} file(s) do not have the manifest's SHA-256")


class ImportedSpace(SpaceCheck):
    """The import of one space's dataset, checked through the space's provisioner on its data provider's panel and its
    REST: the declared storage, the scan, and the space against the manifest (module docstring)."""

    def __init__(self, provisioner: Provisioner, manifest: Manifest) -> None:
        if provisioner.space.imported is None:
            raise ProvisionError(
                f"{provisioner.where}the space is not declared as held on an imported storage ([space] import); "
                "its data were uploaded, so there is no import to check"
            )
        super().__init__(provisioner, manifest)

    def _panel(self, path: str) -> Any:
        answer = self._p.client.panel_get(self._p.settings.holder, path, retry=self._p.retry)
        if not isinstance(answer, dict):
            raise self._fail("the data provider's panel answered something that is not a table")
        return answer

    # -- 1. the declared storage -----------------------------------------------------------------------------------
    def check_storage(self) -> dict[str, Any]:
        """Show, on the data provider's panel, that the space's support is an imported storage, read-only as declared,
        in the auto mode with continuous scans off; record and return its flags. ImportFailed otherwise."""
        details = self._panel(f"/provider/spaces/{self._p.space_id}")
        storage_id = details.get("storageId")
        if not isinstance(storage_id, str) or not storage_id:
            raise self._fail("the data provider's panel names no storage supporting the space")
        storage = self._panel(f"/provider/storages/{storage_id}")
        importing = _table(details.get("storageImport"))
        config = _table(importing.get("autoStorageImportConfig"))
        shown = {
            "imported": storage.get("importedStorage"),
            "readonly": storage.get("readonly"),
            "mode": importing.get("mode") if importing.get("mode") in ("auto", "manual") else None,
            "continuous_scan": config.get("continuousScan"),
            "detect_modifications": config.get("detectModifications"),
            "detect_deletions": config.get("detectDeletions"),
        }
        self._record["storage"] = shown
        if shown["imported"] is not True:
            raise self._fail("the data provider's storage of the space is not an imported storage, as declared")
        if shown["readonly"] is not self._p.read_only:
            raise self._fail("the data provider's imported storage's read-only is not as declared ([space] import)")
        if shown["mode"] == "manual":
            raise self._fail(
                "the space's import is manual, file by file, with no scan to wait for: declare an auto one"
            )
        if shown["mode"] != "auto":
            raise self._fail("the data provider's panel shows no auto import of the space")
        if shown["continuous_scan"] is not False:
            raise self._fail(
                "the space's import scans continuously, or does not say: a scan during a run can change what the "
                "space holds and take blocks from the other providers; set continuousScan false"
            )
        return dict(shown)

    # -- 2. the scan -----------------------------------------------------------------------------------------------
    def scan_info(self) -> dict[str, Any]:
        """One read of the space's auto import scan on the data provider's panel: its status and counters, checked."""
        answer = self._panel(f"/provider/spaces/{self._p.space_id}/storage-import/auto/info")
        status = answer.get("status")
        counters = {key: _count(answer.get(key)) for key in SCAN_COUNTERS}
        if status not in SCAN_STATES or any(value is None for value in counters.values()):
            raise self._fail("the import scan's state on the data provider's panel cannot be read")
        times = {key: _count(answer.get(key)) for key in ("start", "stop")}
        return {"status": status, **times, **counters}

    def await_scan(self, *, timeout_s: float, poll_s: float = SCAN_POLL_S, after: int = 0) -> dict[str, Any]:
        """Wait, bounded, until a scan later than the `after`-th has completed with no failed file; record and return
        its state with the seconds waited and the reads taken. A scan that failed or was aborted fails at once."""
        began, polls = self._p.clock(), 0
        while True:
            info = self.scan_info()
            polls += 1
            waited = round(self._p.clock() - began, 3)
            self._record["scan"] = {**info, "polls": polls, "waited_s": waited}
            if info["status"] in SCAN_FAILED:
                raise self._fail(f"the import scan ended {info['status']}")
            if info["status"] == "completed" and info["totalScans"] > after:
                if info["failedFiles"]:
                    raise self._fail(f"the import scan completed with {info['failedFiles']} failed file(s)")
                self._record["seconds"]["scan"] = waited
                return dict(self._record["scan"])
            if waited >= timeout_s:
                raise self._fail(f"the import scan did not complete within {timeout_s:g} s")
            self._p.sleep(poll_s)

    # -- the whole provisioning, and a re-scan -----------------------------------------------------------------------
    def provision(
        self, *, timeout_s: float, providers: Sequence[str] | None = None, hashes: bool = False
    ) -> dict[str, Any]:
        """Check the declared storage, wait for the scan and verify the space against the manifest; return the
        record, or raise ImportFailed with it."""
        self.check_storage()
        self.await_scan(timeout_s=timeout_s)
        return self.verify(timeout_s=timeout_s, providers=providers, hashes=hashes)

    def rescan(self, *, timeout_s: float, providers: Sequence[str] | None = None) -> dict[str, Any]:
        """Force a new scan, wait for it, verify the space again and record whether every file kept its id: `new_ids`
        names, by position, each manifest file whose id differs from the one it had before (or that had none)."""
        for key in ("ids_stable", "new_ids"):  # an earlier re-scan's, never this one's
            self._record.pop(key, None)
        before = self.scan_info()
        holder = self._p.settings.holder
        ids_before = {path: entry.file_id for path, entry in self._list(holder).items()}
        self._p.client.force_scan(holder, self.space, gate_retry=self._p.retry)
        self.await_scan(timeout_s=timeout_s, after=before["totalScans"])
        record = self.verify(timeout_s=timeout_s, providers=providers)
        after = self._listings[holder]
        new = [
            self._manifest.label(i)
            for i, f in enumerate(self._manifest.files)
            if ids_before.get(f.path) != after[f.path].file_id
        ]
        record.update(ids_stable=not new, new_ids=new)
        self._record.update(ids_stable=not new, new_ids=new)
        return record


__all__ = [
    "SCAN_STATES",
    "VERSION",
    "ImportFailed",
    "ImportedSpace",
    "Manifest",
    "SpaceCheck",
    "load_manifest",
    "manifest_of_tree",
    "write_manifest",
]
