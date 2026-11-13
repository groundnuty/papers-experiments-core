# Provenance: new
"""The probe files of the link check (O35, #221): written through their source, found again, retired after a failure.

**A probe** is one small file per source in the `[link_check]` probe space: `expcore-link-probe-` and 16 hex digits of
the SHA-256 of the source's provider id, `probe_bytes` long, its bytes derived from the same id. It is a provisioned
file of known bytes, so a fetch can be checked byte for byte, and never a file of the study's data. It is written
through the source when it is missing (the Compose federation's `reset --clean` empties the probe space) or is not the
probe's bytes, and kept otherwise.

**Generations.** A probe has a generation, in its name: generation 0 is the name above, generation N is that name and
`-gN`. The probe a check uses is the highest generation the source's listing shows, so a probe with a higher
generation supersedes the lower ones with no state outside the probe space, whichever process runs the next check.

**A failed fetch retires its probe.** A provider that fetches a file's bytes waits on its rtransfer synchronizer with
no timeout, and a later read of the same file range joins the same outstanding job until the synchronizer restarts it
after 1,800 s (op-worker f118ed9). A read that timed out leaves that job behind, so the next check must never read the
same file. `retire` writes the next generation through the source, a new name and a new file id, and deletes the
failed file and any lower generation (a delete of a file with a stuck job is fine: nothing reads it). When the
successor cannot be written, the deletion alone retires the file; when neither takes, `retire` says so, and the next
check reads the same file once more and, if it fails, retires it again. A probe that passed is kept, and the retired
files a passing check finds in the listing are deleted then (`purge`), so none outlives a passing check of its pair.
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
ROOT_TRIES = 3  # reads of the probe space's root on a source, 2 s apart
_GENERATION = re.compile(r"-g([1-9][0-9]{0,8})")


class ProbeNotReady(RuntimeError):
    """A pair's probe could not be written, seen or made cold, so its link was not tested; the message holds labels."""


def probe_content(provider_id: str, size: int) -> bytes:
    """The bytes of a source's probe: SHA-256 blocks chained from the provider's id, `size` of them in all."""
    seed, out, counter = hashlib.sha256(provider_id.encode()).digest(), bytearray(), 0
    while len(out) < size:
        out += hashlib.sha256(seed + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:size])


def probe_name(provider_id: str, generation: int = 0) -> str:
    """The probe's file name: no provider's name, and the same whatever order platform.toml declares them in. A
    generation above 0 is a new name, so a retired probe's successor is never the file a stuck fetch is waiting on."""
    stem = PROBE_PREFIX + hashlib.sha256(provider_id.encode()).hexdigest()[:16]
    return stem if generation == 0 else f"{stem}-g{generation}"


def generation_of(name: str, provider_id: str) -> int | None:
    """The generation a file name has as the probe of a provider, or None when it is no probe name of that provider."""
    stem = probe_name(provider_id)
    if name == stem:
        return 0
    found = _GENERATION.fullmatch(name[len(stem) :]) if name.startswith(stem) else None
    return int(found.group(1)) if found else None


@dataclass(frozen=True)
class Probe:
    """A source's probe as one check uses it: its file, its generation, whether it was written now, and the retired
    probes (lower generations) of the source that the probe space still holds."""

    file_id: str
    generation: int
    written: bool
    older: tuple[str, ...] = ()


@dataclass(frozen=True)
class Retired:
    """What retiring a failed probe came to: whether it is retired (superseded or deleted), the lower generations
    deleted with it and those that could not be, and why the successor was not written when it was not."""

    ok: bool
    deleted: int
    undeleted: int
    why: str | None = None


class ProbeFiles:
    """The probes of the link check's sources, written and read through one client, in one probe space."""

    def __init__(
        self,
        client: OnezoneClient,
        settings: LinkCheckSettings,
        ids: dict[str, str],
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._client, self._settings, self._ids, self._clock, self._sleep = client, settings, ids, clock, sleep
        self._space = settings.space

    def _label(self, name: str) -> str:
        return self._client.provider_label(name)

    def content(self, source: str) -> bytes:
        return probe_content(self._ids[source], self._settings.probe_bytes)

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

    def _root(self, source: str) -> str:
        return consistency.space_root(
            self._client, source, self._space.id, tries=ROOT_TRIES, sleep=self._sleep, retry=ONE_ATTEMPT
        )

    def _probes(self, source: str, directory: str) -> dict[int, Entry]:
        """Every probe the source's listing of the probe space's root shows for the source, by generation."""
        found: dict[int, Entry] = {}
        params: dict[str, object] = {"limit": LISTING_LIMIT, "attribute": LISTING_ATTRIBUTES}
        while True:
            page = self._client.get(f"/data/{directory}/children", provider=source, params=params, retry=ONE_ATTEMPT)
            token = next_page_token(page)  # a page without its last-page marker is refused, never the end (O26)
            for item in page["children"]:
                entry = listing_entry(item)
                generation = generation_of(entry.name, self._ids[source])
                if generation is not None:
                    found[generation] = entry
            if token is None:
                return found
            params = {**params, "token": token}

    def _write(self, source: str, file_id: str, data: bytes) -> None:
        """Write the probe's bytes through the source, the file truncated first, so the source holds it whole, and read
        them back through the source, which holds them."""
        self._client.mutate(
            "PUT",
            f"/data/{file_id}/content",
            provider=source,
            content=data,
            content_type=OCTET_STREAM,
            space=self._space,
            gate_retry=ONE_ATTEMPT,
        )
        if self.digest(source, file_id, len(data)) != hashlib.sha256(data).hexdigest():
            raise ProbeNotReady(f"the probe read back through {self._label(source)} is not the bytes written")

    def _create(self, source: str, root: str, generation: int, data: bytes) -> str:
        made = self._client.mutate(
            "POST",
            f"/data/{root}/children",
            provider=source,
            params={"name": probe_name(self._ids[source], generation), "type": "REG"},
            space=self._space,
            gate_retry=ONE_ATTEMPT,
        )
        file_id = made.get("fileId") if isinstance(made, dict) else None
        if not isinstance(file_id, str) or not file_id:
            raise ProbeNotReady(f"{self._label(source)} answered the probe's creation with no file id")
        self._write(source, file_id, data)
        return file_id

    def provision(self, source: str) -> Probe:
        """Find the source's probe (its highest generation), or write one; read its bytes back through the source,
        which holds it, and write them again when they differ."""
        data = self.content(source)
        root = self._root(source)
        found = self._probes(source, root)
        if not found:
            return Probe(self._create(source, root, 0, data), 0, True)
        generation = max(found)
        entry = found[generation]
        older = tuple(found[g].file_id for g in sorted(found) if g < generation)
        written = (
            entry.size != len(data) or self.digest(source, entry.file_id, len(data)) != hashlib.sha256(data).hexdigest()
        )
        if written:
            self._write(source, entry.file_id, data)
        return Probe(entry.file_id, generation, written, older)

    def delete(self, source: str, file_ids: tuple[str, ...]) -> tuple[tuple[str, ...], int]:
        """Delete files of the probe space through the source; return the ids deleted and how many were not. A
        deletion that fails is counted, never an error: the file is retired either way, and a later check retries."""
        deleted: list[str] = []
        for file_id in file_ids:
            try:
                self._client.mutate(
                    "DELETE", f"/data/{file_id}", provider=source, space=self._space, gate_retry=ONE_ATTEMPT
                )
                deleted.append(file_id)
            except PlatformError:
                continue
        return tuple(deleted), len(file_ids) - len(deleted)

    def retire(self, source: str, probe: Probe) -> Retired:
        """Retire a probe whose fetch failed: write the next generation, a fresh file under a new name, then delete the
        failed file and every lower generation. It is retired when the successor was written or the failed file deleted;
        otherwise the next check reads it again."""
        why: str | None = None
        written = False
        try:
            self._create(source, self._root(source), probe.generation + 1, self.content(source))
            written = True
        except (PlatformError, consistency.WaitTimeout, ProbeNotReady) as error:
            why = str(error)
        deleted, undeleted = self.delete(source, (probe.file_id, *probe.older))
        return Retired(written or probe.file_id in deleted, len(deleted), undeleted, why)
