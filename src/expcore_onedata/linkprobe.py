# Provenance: new
"""The probe files of the link check (O35, #221): written through their source, found again, retired after a failure.

**A probe** is one small file per pair (a reading provider and a source) in the `[link_check]` probe space:
`expcore-link-probe-` and 16 hex digits of the SHA-256 of the pair's **key**, `probe_bytes` long, its bytes derived from
the same key. The key (`probe_key`) is the source's provider id for a pair the agent's local provider reads, so those
probe files are the check's original ones, and the source's and the reader's ids for any other reader. **One file per
pair, never one per source:** a provider that fetched a source's probe then holds a copy, and a second reader of the
same file could be served by that copy while its own link to the source is stuck, a false pass. It is a provisioned
file of known bytes, so a fetch can be checked byte for byte, and never a file of the study's data. It is written
through the source when it is missing (the Compose federation's `reset --clean` empties the probe space) or is not the
probe's bytes, at offset 0, and kept otherwise; a probe file longer than the probe's bytes gives way to the next
generation, since only a write without an offset could shorten it and none is ever sent (O44).

**Generations.** A probe has a generation, in its name: generation 0 is the name above, generation N is that name and
`-gN`. The probe a check uses is the highest generation the source's listing shows among the files not retired (below),
so a probe with a higher generation supersedes the lower ones with no state outside the probe space, whichever
process runs the next check.

**A failed fetch retires its file, for good.** A provider that fetches a file's bytes waits on its rtransfer
synchronizer with no timeout, and a later read of the same file range joins the same outstanding job until the
synchronizer restarts it after 1,800 s (op-worker f118ed9). A read that timed out leaves that job behind, so **a file
id whose fetch ever failed is never read again by the check**, whatever its name or generation and whether the file
still exists. The module keeps no file of its own, so the record of retired ids is kept in the probe space, as
**markers**: `retire` writes one empty file per retired file id, `expcore-link-retired-`, 16 hex digits of the SHA-256
of the pair's key and 16 of the SHA-256 of the retired file id (a digest, never the id), through the reading provider:
it answered the failed fetch, its writes do not depend on the source's, and it lists at once what it wrote.
Provisioning lists the markers through the reading provider and leaves out of its choice every probe file whose id one
of them names; a marker of another pair, whose key differs, is never read as this pair's and never dropped by it. When
no probe is left, it writes a fresh one
under the next generation above every file the source lists; when that fails too the pair fails closed, "no fresh
probe for <source> to <local>: creation failed", and no retired file is read.

`retire` writes the record first, then the next generation through the source (a new name and a new file id), then
deletes the failed file and every other probe file the source holds (a delete of a file with a stuck job is fine:
nothing reads it). The deletions are best-effort and a later check retries them: a probe that passed purges the
retired files it finds listed (`purge`), and a retired id stays excluded for as long as its file exists. A record is
dropped once its file is gone: provisioning deletes every marker whose file the source's listing no longer shows, so
the record is bounded by the retired files that remain. Only when the record cannot be written and the failed file (with
the lower generations no failure retired) is not deleted is the failed file not excluded for good, and `retire` says so,
a successor or not: a later check may read it again and, if it fails, retires it again.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass

from expcore_onedata import consistency
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.linksettings import LinkCheckSettings
from expcore_onedata.provision import LISTING_ATTRIBUTES, LISTING_LIMIT, Entry, listing_entry, next_page_token
from expcore_onedata.readretry import ONE_ATTEMPT
from expcore_onedata.streamed import OCTET_STREAM

PROBE_PREFIX = "expcore-link-probe-"
RETIRED_PREFIX = "expcore-link-retired-"
ROOT_TRIES = 3  # reads of the probe space's root on a provider, 2 s apart
_GENERATION = re.compile(r"-g([1-9][0-9]{0,8})")
_DIGEST = re.compile(r"-([0-9a-f]{16})")
_EXISTS = 409  # the answer to a creation of a name that is already there


class ProbeNotReady(RuntimeError):
    """A pair's probe could not be written, seen or made cold, so its link was not tested; the message holds labels."""


class NoFreshProbe(ProbeNotReady):
    """A pair has no probe that may be read and a fresh one could not be written; no retired file is read in its
    place. The message is the pair's reason, with labels."""


def _digest(text: str) -> str:
    """16 hex digits of the SHA-256 of a provider's id or a file's id: a name that holds neither."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def probe_key(source_id: str, reader_id: str, local_id: str) -> str:
    """What the files of the pair `reader` <- `source` take their names and bytes from: the source's provider id when
    the reader is the agent's local provider (the probe files the check always had), else the source's and the reader's
    ids, so that no two readers of a source fetch the same file."""
    return source_id if reader_id == local_id else f"{source_id}>{reader_id}"


def probe_content(key: str, size: int) -> bytes:
    """The bytes of a pair's probe: SHA-256 blocks chained from the pair's key, `size` of them in all."""
    seed, out, counter = hashlib.sha256(key.encode()).digest(), bytearray(), 0
    while len(out) < size:
        out += hashlib.sha256(seed + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:size])


def probe_name(key: str, generation: int = 0) -> str:
    """The probe's file name: no provider's name, and the same whatever order platform.toml declares them in. A
    generation above 0 is a new name, so a retired probe's successor is never the file a stuck fetch is waiting on."""
    stem = PROBE_PREFIX + _digest(key)
    return stem if generation == 0 else f"{stem}-g{generation}"


def generation_of(name: str, key: str) -> int | None:
    """The generation a file name has as the probe of a pair, or None when it is no probe name of that pair."""
    stem = probe_name(key)
    if name == stem:
        return 0
    found = _GENERATION.fullmatch(name[len(stem) :]) if name.startswith(stem) else None
    return int(found.group(1)) if found else None


def retired_name(key: str, file_id: str) -> str:
    """The marker of a retired probe file: the pair's key and the file id, each as a digest. The id is what is retired,
    not the probe's name, since a deleted name is written again under a new id, which is a new file."""
    return f"{RETIRED_PREFIX}{_digest(key)}-{_digest(file_id)}"


def retired_digest(name: str, key: str) -> str | None:
    """The digest of the file id a marker of a pair's probes holds, or None when the name is no such marker."""
    stem = RETIRED_PREFIX + _digest(key)
    found = _DIGEST.fullmatch(name[len(stem) :]) if name.startswith(stem) else None
    return found.group(1) if found else None


@dataclass(frozen=True)
class Probe:
    """A source's probe as one check uses it: its file, its generation, whether it was written now, the source's other
    probe files that the probe space still holds (retired ones, and lower generations that no failure retired), those
    of them with no marker, and the highest generation the source lists, retired files included."""

    file_id: str
    generation: int
    written: bool
    others: tuple[str, ...] = ()
    unmarked: tuple[str, ...] = ()
    newest: int = 0


@dataclass(frozen=True)
class Retired:
    """What retiring a failed probe came to: whether the failed file is out of use for good (recorded as retired, or
    deleted with every lower generation no failure retired), the files deleted with it and those that could not be,
    and why the rest was not done when it was not. A successor alone is not retirement: it holds the failed file back
    only while it is the highest generation."""

    ok: bool
    deleted: int
    undeleted: int
    why: str | None = None


class ProbeFiles:
    """The probes of the link check's pairs, written and read through one client, in one probe space. `home` is the
    agent's local provider, whose pairs keep the source-keyed probe files (`probe_key`)."""

    def __init__(
        self,
        client: OnezoneClient,
        settings: LinkCheckSettings,
        ids: dict[str, str],
        home: str,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._client, self._settings, self._ids, self._clock, self._sleep = client, settings, ids, clock, sleep
        self._home = home
        self._space = settings.space

    def _label(self, name: str) -> str:
        return self._client.provider_label(name)

    def key(self, source: str, reader: str) -> str:
        """What the pair's probe files are named and made from (`probe_key`)."""
        return probe_key(self._ids[source], self._ids[reader], self._ids[self._home])

    def content(self, source: str, reader: str) -> bytes:
        return probe_content(self.key(source, reader), self._settings.probe_bytes)

    def digest(self, provider: str, file_id: str, size: int) -> str:
        """The SHA-256 of the probe as read through `provider`, bounded by `timeout_s`; bound to the probe space, so a
        file of any other space is refused before anything is sent."""
        timeout = self._settings.timeout_s
        return self._client.read_content(
            f"/data/{file_id}/content",
            provider=provider,
            size=size,
            chunk_bytes=size,
            deadline_s=timeout,
            timeout_s=timeout,
            clock=self._clock,
            space=self._space,
            gate_retry=ONE_ATTEMPT,
        ).sha256

    def _root(self, provider: str) -> str:
        return consistency.space_root(
            self._client, provider, self._space.id, tries=ROOT_TRIES, sleep=self._sleep, retry=ONE_ATTEMPT
        )

    def _children(self, provider: str, directory: str) -> list[Entry]:
        """Every entry of the probe space's root as `provider` lists it."""
        found: list[Entry] = []
        params: dict[str, object] = {"limit": LISTING_LIMIT, "attribute": LISTING_ATTRIBUTES}
        while True:
            page = self._client.get(f"/data/{directory}/children", provider=provider, params=params, retry=ONE_ATTEMPT)
            token = next_page_token(page)  # a page without its last-page marker is refused, never the end (O26)
            found += [listing_entry(item) for item in page["children"]]
            if token is None:
                return found
            params = {**params, "token": token}

    def _probes(self, key: str, entries: list[Entry]) -> dict[int, Entry]:
        """The probes of a pair among a source's listing, by generation."""
        found: dict[int, Entry] = {}
        for entry in entries:
            generation = generation_of(entry.name, key)
            if generation is not None:
                found[generation] = entry
        return found

    def _write(self, provider: str, file_id: str, data: bytes) -> None:
        """Write the probe's bytes through `provider` at offset 0, over a file no longer than them, so it holds them
        whole, and read them back through it, which holds them. No write goes without an offset, which would truncate
        the file (O44): a longer file is replaced by the next generation instead (`provision`)."""
        self._client.mutate(
            "PUT",
            f"/data/{file_id}/content",
            provider=provider,
            params={"offset": 0},
            content=data,
            content_type=OCTET_STREAM,
            space=self._space,
            gate_retry=ONE_ATTEMPT,
        )
        if self.digest(provider, file_id, len(data)) != hashlib.sha256(data).hexdigest():
            raise ProbeNotReady(f"the probe read back through {self._label(provider)} is not the bytes written")

    def _create(self, provider: str, root: str, name: str, data: bytes | None) -> str:
        """Create a file of the probe space's root through `provider`, `data` its content (a marker has none)."""
        made = self._client.mutate(
            "POST",
            f"/data/{root}/children",
            provider=provider,
            params={"name": name, "type": "REG"},
            space=self._space,
            gate_retry=ONE_ATTEMPT,
        )
        file_id = made.get("fileId") if isinstance(made, dict) else None
        if not isinstance(file_id, str) or not file_id:
            raise ProbeNotReady(f"{self._label(provider)} answered a file's creation with no file id")
        if data is not None:
            self._write(provider, file_id, data)
        return file_id

    def _retired(self, key: str, local: str, probes: dict[int, Entry]) -> set[str]:
        """The file ids among the pair's probes that a marker of the pair, read through the reading provider, retires.
        A marker of the pair whose file the source's listing no longer shows is deleted: its file is gone, so the record
        is bounded. A marker of another pair is not this pair's to read or to drop."""
        marks: dict[str, Entry] = {}
        for entry in self._children(local, self._root(local)):
            digest = retired_digest(entry.name, key)
            if digest is not None:
                marks[digest] = entry
        listed = {_digest(entry.file_id): entry.file_id for entry in probes.values()}
        gone = tuple(mark.file_id for digest, mark in marks.items() if digest not in listed)
        if gone:
            self.delete(local, gone)  # a marker that stays is found again, and dropped, by the next check
        return {file_id for digest, file_id in listed.items() if digest in marks}

    def provision(self, source: str, local: str) -> Probe:
        """Find the source's probe, its highest generation among the files no marker retires, or write one under the
        next generation above every file listed; read its bytes back through the source, which holds it, and write them
        again when they differ. When no probe may be read and a fresh one cannot be written, the pair is not tested.
        `local` is the provider that reads the pair, the agent's local one or another."""
        key = self.key(source, local)
        data = self.content(source, local)
        root = self._root(source)
        probes = self._probes(key, self._children(source, root))
        retired = self._retired(key, local, probes)
        usable = {g: entry for g, entry in probes.items() if entry.file_id not in retired}
        newest = max(probes, default=-1)
        if not usable:
            try:
                file_id = self._create(source, root, probe_name(key, newest + 1), data)
            except (PlatformError, ProbeNotReady) as error:
                pair = f"{self._label(source)} to {self._label(local)}"
                after = "; every probe file of the source is retired and none is read" if probes else ""
                raise NoFreshProbe(f"no fresh probe for {pair}: creation failed ({error}){after}") from error
            return Probe(file_id, newest + 1, True, tuple(e.file_id for e in probes.values()), (), newest + 1)
        generation = max(usable)
        entry = usable[generation]
        if entry.size > len(data):  # only a truncating write could shorten it, and none is sent (O44)
            every = tuple(e.file_id for _, e in sorted(probes.items()))
            file_id = self._create(source, root, probe_name(key, newest + 1), data)
            return Probe(file_id, newest + 1, True, every, tuple(f for f in every if f not in retired), newest + 1)
        others = tuple(e.file_id for _, e in sorted(probes.items()) if e.file_id != entry.file_id)
        unmarked = tuple(file_id for file_id in others if file_id not in retired)
        written = (
            entry.size != len(data) or self.digest(source, entry.file_id, len(data)) != hashlib.sha256(data).hexdigest()
        )
        if written:
            self._write(source, entry.file_id, data)
        return Probe(entry.file_id, generation, written, others, unmarked, newest)

    def delete(self, provider: str, file_ids: tuple[str, ...]) -> tuple[tuple[str, ...], int]:
        """Delete files of the probe space through `provider`; return the ids deleted and how many were not. A deletion
        that fails is counted, never an error: a retired file stays excluded whether or not it is deleted, and a later
        check retries."""
        deleted: list[str] = []
        for file_id in file_ids:
            try:
                self._client.mutate(
                    "DELETE", f"/data/{file_id}", provider=provider, space=self._space, gate_retry=ONE_ATTEMPT
                )
                deleted.append(file_id)
            except PlatformError:
                continue
        return tuple(deleted), len(file_ids) - len(deleted)

    def purge(self, source: str, probe: Probe) -> tuple[int, int]:
        """Delete the other probe files of a probe that passed, the retired ones and the superseded; return how many
        were deleted and how many were not, which the next check that passes deletes."""
        deleted, undeleted = self.delete(source, probe.others)
        return len(deleted), undeleted

    def _record(self, source: str, local: str, probe: Probe) -> tuple[bool, tuple[str, ...]]:
        """Write the markers that retire a failed probe, and the lower generations no failure retired, through the local
        provider; return whether the failed file's was written and what went wrong. A marker already there is a
        record already made."""
        causes: list[str] = []
        try:
            root = self._root(local)
        except (PlatformError, consistency.WaitTimeout) as error:
            return False, (f"no record of it written: {error}",)
        recorded = False
        key = self.key(source, local)
        for file_id in (probe.file_id, *probe.unmarked):
            try:
                self._create(local, root, retired_name(key, file_id), None)
            except (PlatformError, ProbeNotReady) as error:
                if getattr(error, "status", None) != _EXISTS:
                    causes.append(f"no record of it written: {error}")
                    continue
            recorded = recorded or file_id == probe.file_id
        return recorded, tuple(causes)

    def retire(self, source: str, local: str, probe: Probe) -> Retired:
        """Retire a probe whose fetch failed, so its file id is never read again: record it (a marker through the local
        provider, which answered), write the next generation, a fresh file under a new name, through the source, then
        delete the failed file and the source's other probe files, best-effort. It is retired once recorded, or once it
        and every lower generation without a record are deleted; otherwise a later check may read it again."""
        recorded, causes = self._record(source, local, probe)
        try:
            name = probe_name(self.key(source, local), probe.newest + 1)
            self._create(source, self._root(source), name, self.content(source, local))
        except (PlatformError, consistency.WaitTimeout, ProbeNotReady) as error:
            causes = (*causes, f"no successor written: {error}")
        deleted, undeleted = self.delete(source, (probe.file_id, *probe.others))
        gone = set(deleted)
        out_of_use = recorded or (probe.file_id in gone and set(probe.unmarked) <= gone)
        return Retired(out_of_use, len(deleted), undeleted, "; ".join(causes) or None)
