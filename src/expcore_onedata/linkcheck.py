# Provenance: new
"""The pre-campaign link check: every (local provider, source provider) pair transfers before any run (O35, #221).

On the first real study's federation one provider's rtransfer link to a peer was stuck, with nothing logged and the
peer reachable by TCP and TLS, while every other pair transferred. Every read of that peer's files through the stuck
provider hung until its cell's bound, and the only symptom was an agent's hung read, which the study would have
scored. This check finds such a link before any run, and fails closed naming it.

**The pairs** are the local provider and each source a run can fetch from (`linksettings.link_pairs`). **The probe**
of a source is one small file that the check writes through that source into the probe space of `[link_check]`, a
space no agent can see, and keeps: `expcore-link-probe-` and 16 hex digits of the SHA-256 of the source's provider id,
`probe_bytes` long, with bytes derived from the same id. A provisioned probe, never a file of the study's data: the
study spaces are where agents work and their transfer listings are a run's evidence, so the check touches nothing
there, and a probe of known bytes lets the fetch be checked byte for byte. It is written when it is missing (the
Compose federation's `reset --clean` empties the probe space) or not the probe's bytes, and kept otherwise.

**Each pair**:

1. *Prepare.* The probe is found through the source, or written there; its bytes are read back through the source,
   which holds it (no transfer), and written again when they differ. Then, within `visible_timeout_s`, the local
   provider must see it at its size and its block map, read through the local provider, must show the source holding
   it whole. Every other provider that block map shows holding part of it, the local one included, has that part
   evicted, confirmed on its own block map within `evict_timeout_s`. A provider the local block map does not list is
   one the local provider would not fetch from. So the source alone holds the probe, and the local provider has
   nowhere else to get it.
2. *Fetch.* One REST range read of the whole probe through the local provider (`OnezoneClient.read_content`, bound to
   the probe space, so a file of any other space is refused before anything is sent), with `timeout_s` as its deadline
   and as the longest wait for an answer. It passes only when it ends in time with the probe's bytes. A read that
   times out, or that the local provider answers with an error, is the pair's failure: "no transfer from providers[j]
   to providers[i] within N s", with the remedy. The fetches run at once, at most `WORKERS` together, so they take
   about one bound however many pairs there are. A fetch is never tried again in the same check: a provider waits on
   its rtransfer synchronizer with no timeout (op-worker f118ed9, `gen_server2:call(..., infinity)`) and restarts a
   stuck job only after 1,800 s, so a second read of the same file would only wait on the same stuck job. The probe
   space is the check's alone, so no other reader has the file open.
3. *Evict afterwards*, only when every pair passed: each probe is evicted from the local provider once, the eviction
   waited for and the local block map read. On Onedata 25.0 an eviction scheduled right after a fetch completes having
   evicted nothing (the same eviction about 30 s later works), so one that did not take is recorded, never retried and
   never a failure: the link was shown to work, and the next check's step 1 evicts the probe and confirms the local
   block map empty before it reads. When a pair fails the check answers at once and evicts nothing.

**The remedy** of a stuck link is named with its failure: restart the local provider's rtransfer link
(`rtransfer_config:restart_link()` in its op-worker), or restart that provider's container alone (`docker restart`,
never a Compose `down`). Waiting also clears it, after the synchronizer's 1,800 s.

**Fail closed.** A pair whose probe cannot be found, written, seen or made cold was not tested, and fails naming the
pair and why; so does every pair when platform.toml declares none of `[link_check]`. Unknown is never a pass.

**The record** (`LinkReport.to_json()`): each pair by position (`providers[i]`, never a name, O12), whether it passed,
the seconds and bytes of its fetch, whether its probe was written now, the evictions that made it cold and those that
evicted it afterwards, by transfer id, and whether that took; the check's window on the host's epoch clock and every
transfer id it scheduled. The fetch itself is on-the-fly replication, which no transfer document records; the
evictions are documents of the probe space, never of a study space, so no study's transfer listing holds any of them,
and the ids and the window let a study set them apart in any other listing it reads.

**Where it runs.** `health.PlatformHealth` runs it at its first call, after the content-type check and the space, and
a failure holds every run until a re-check of the closed gate passes. It runs it again at every call after a failure,
and every `every_s` of the campaign (`[link_check]`, 600 s by default), so a link that sticks mid-campaign closes the
health gate: the run whose check found it is an infrastructure outcome, never scored, and the whole campaign is held
(the core's gate holds all dispatch, not one rights group's) until a check passes again. Periodic rather than per run:
each check costs a few seconds of eviction and block-map polls on 25.0, which every run would pay. A fault injector's
restore runs it for every pair the restored provider is in, and the restore fails while a pair does not pass
(`fault_serves.ServingProbe.links`). `deploy/compose-federation`'s `up` runs it last and does not report the federation
ready until it passes.

**Its time.** The first check after the probe space was emptied writes the probes and waits for the local provider to
see them (about 6 s on 25.0); every other check evicts the probes the last one left, a few seconds, fetches, and
evicts once more. A study keeps the campaign's `health_timeout_s` above `visible_timeout_s + timeout_s +
evict_timeout_s` (150 s by default): a call that outlives it reads unhealthy, which closes the gate, or sets a run's
post-run check aside, though the links work.
"""

import dataclasses
import hashlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from expcore.campaign.healthgate import Health
from expcore_onedata import consistency
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.linksettings import LinkCheckSettings, link_pairs
from expcore_onedata.placement import file_fully_placed, held_bytes, provider_blocks
from expcore_onedata.provision import LISTING_ATTRIBUTES, LISTING_LIMIT, Entry, listing_entry, next_page_token
from expcore_onedata.readretry import ONE_ATTEMPT
from expcore_onedata.streamed import OCTET_STREAM

VERSION = 1  # of the record
PROBE_PREFIX = "expcore-link-probe-"
WORKERS = 8  # fetches at once
POLL_S = 2.0  # between reads of a block map or a transfer's record
EVICTION_PAUSE_S = 10.0  # before an eviction that took nothing is scheduled again
ROOT_TRIES = 3  # reads of the probe space's root on a source, 2 s apart
REMEDY = (
    "remedy: restart the rtransfer link on {local} (rtransfer_config:restart_link() in its op-worker) or restart "
    "{local}'s container alone (docker restart, never a compose down); it clears by itself only after about 1800 s"
)
Pair = tuple[str, str]  # (local provider, source provider), by their names in platform.toml


class ProbeNotReady(RuntimeError):
    """A pair's probe could not be written, seen or made cold, so its link was not tested; the message holds labels."""


@dataclass(frozen=True)
class PairResult:
    """What the check found for one pair; providers are named by their positions in platform.toml."""

    local: str
    source: str
    ok: bool
    seconds: float | None = None
    bytes: int | None = None
    provisioned: bool = False
    cooled: tuple[str, ...] = ()  # the evictions that made the probe cold before the fetch, by transfer id
    evicted: bool | None = None  # whether the eviction afterwards took; None when none was made
    evictions: tuple[str, ...] = ()  # the evictions afterwards, by transfer id
    reason: str | None = None
    remedy: str | None = None  # set when the fetch failed: the link needs it before the pair can pass

    def to_json(self) -> dict[str, Any]:
        return {
            "local": self.local,
            "source": self.source,
            "ok": self.ok,
            "seconds": None if self.seconds is None else round(self.seconds, 3),
            "bytes": self.bytes,
            "provisioned": self.provisioned,
            "cooled_by": list(self.cooled),
            "evicted": self.evicted,
            "evicted_by": list(self.evictions),
            "reason": self.reason,
            "remedy": self.remedy,
        }


@dataclass(frozen=True)
class LinkReport:
    """The verdict of one link check, with every pair's result and the check's window on the host's epoch clock."""

    ok: bool
    detail: str
    pairs: tuple[PairResult, ...] = ()
    started: float | None = None
    ended: float | None = None

    @property
    def health(self) -> Health:
        return Health(self.ok, self.detail)

    def to_json(self) -> dict[str, Any]:
        transfers = sorted({tid for pair in self.pairs for tid in (*pair.cooled, *pair.evictions)})
        return {
            "version": VERSION,
            "ok": self.ok,
            "detail": self.detail,
            "window": {"start_epoch_s": self.started, "end_epoch_s": self.ended},
            "transfers": transfers,
            "pairs": [pair.to_json() for pair in self.pairs],
        }


def probe_content(provider_id: str, size: int) -> bytes:
    """The bytes of a source's probe: SHA-256 blocks chained from the provider's id, `size` of them in all."""
    seed, out, counter = hashlib.sha256(provider_id.encode()).digest(), bytearray(), 0
    while len(out) < size:
        out += hashlib.sha256(seed + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:size])


def probe_name(provider_id: str) -> str:
    """The probe's file name: no provider's name, and the same whatever order platform.toml declares them in."""
    return PROBE_PREFIX + hashlib.sha256(provider_id.encode()).hexdigest()[:16]


class _Check:
    """One run of the check over the pairs, through one client, on one clock."""

    def __init__(
        self,
        config: PlatformConfig,
        client: OnezoneClient,
        settings: LinkCheckSettings,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._client, self._settings, self._clock, self._sleep = client, settings, clock, sleep
        self._ids = {name: provider.id or "" for name, provider in config.providers.items()}
        self._space = settings.space

    def _label(self, name: str) -> str:
        return self._client.provider_label(name)

    # -- the probe -------------------------------------------------------------------------------------------------
    def _find(self, source: str, directory: str, name: str) -> Entry | None:
        params: dict[str, Any] = {"limit": LISTING_LIMIT, "attribute": LISTING_ATTRIBUTES}
        while True:
            page = self._client.get(f"/data/{directory}/children", provider=source, params=params, retry=ONE_ATTEMPT)
            token = next_page_token(page)  # a page without its last-page marker is refused, never the end (O26)
            for item in page["children"]:
                entry = listing_entry(item)
                if entry.name == name:
                    return entry
            if token is None:
                return None
            params = {**params, "token": token}

    def _write(self, source: str, file_id: str, data: bytes) -> None:
        """Write the probe's bytes through the source, the file truncated first, so the source holds it whole."""
        self._client.mutate(
            "PUT",
            f"/data/{file_id}/content",
            provider=source,
            content=data,
            content_type=OCTET_STREAM,
            space=self._space,
            gate_retry=ONE_ATTEMPT,
        )

    def _digest(self, provider: str, file_id: str, size: int) -> str:
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

    def _provision(self, source: str) -> tuple[str, bool]:
        """Find the source's probe, or write it; read its bytes back through the source, which holds it, and write
        them again when they differ. Return its id and whether it was written now."""
        data = probe_content(self._ids[source], self._settings.probe_bytes)
        expected = hashlib.sha256(data).hexdigest()
        root = consistency.space_root(
            self._client, source, self._space.id, tries=ROOT_TRIES, sleep=self._sleep, retry=ONE_ATTEMPT
        )
        entry = self._find(source, root, probe_name(self._ids[source]))
        if entry is None:
            made = self._client.mutate(
                "POST",
                f"/data/{root}/children",
                provider=source,
                params={"name": probe_name(self._ids[source]), "type": "REG"},
                space=self._space,
                gate_retry=ONE_ATTEMPT,
            )
            file_id = made.get("fileId") if isinstance(made, dict) else None
            if not isinstance(file_id, str) or not file_id:
                raise ProbeNotReady(f"{self._label(source)} answered the probe's creation with no file id")
            self._write(source, file_id, data)
            written = True
        else:
            file_id, written = entry.file_id, False
            if entry.size != len(data) or self._digest(source, file_id, len(data)) != expected:
                self._write(source, file_id, data)
                written = True
        if written and self._digest(source, file_id, len(data)) != expected:
            raise ProbeNotReady(f"the probe read back through {self._label(source)} is not the bytes written")
        return file_id, written

    def _held(self, reader: str, file_id: str) -> dict[str, list[tuple[int, int]]]:
        """The blocks of the probe each provider of the probe space holds, as `reader`'s block map lists them; a
        provider the map does not list is left out."""
        answer = self._client.get(f"/data/{file_id}/distribution", provider=reader, retry=ONE_ATTEMPT)
        held: dict[str, list[tuple[int, int]]] = {}
        for name in self._settings.providers:
            try:
                held[name] = provider_blocks(answer, self._ids[name])
            except ValueError:
                continue
        return held

    def _make_cold(self, local: str, source: str, file_id: str) -> tuple[str, ...]:
        """Wait until the local block map shows the source holding the probe whole and every other provider holding
        none of it, evicting any part another provider holds; return the evictions made, by transfer id."""
        settings, began = self._settings, self._clock()
        made: list[str] = []
        evicted: set[str] = set()  # each provider is evicted from once, its retries inside `_evict`
        while True:
            try:
                held = self._held(local, file_id)
            except PlatformError:
                held = {}
            warm = [name for name, blocks in held.items() if name != source and held_bytes(blocks)]
            if local in held and file_fully_placed(held.get(source, []), settings.probe_bytes) and not warm:
                return tuple(made)
            for name in (name for name in warm if name not in evicted):
                done, tids = self._evict(name, [file_id])
                made += tids[file_id]
                evicted.add(name)
                if not done[file_id]:
                    raise ProbeNotReady(
                        f"the probe could not be evicted from {self._label(name)} within "
                        f"{settings.evict_timeout_s:g} s, so the local provider could fetch it from there"
                    )
            if self._clock() - began >= settings.visible_timeout_s + (settings.evict_timeout_s if evicted else 0.0):
                shown = f"{self._label(warm[0])} still holding part of it" if warm else "the source holding it whole"
                raise ProbeNotReady(f"the block map of {self._label(local)} did not show {shown} in time")
            self._sleep(POLL_S)

    def prepare(self, pair: Pair) -> tuple[str, bool, tuple[str, ...]]:
        """Make the pair's probe ready: written, seen by the local provider, held by the source alone."""
        local, source = pair
        file_id, written = self._provision(source)
        consistency.wait_visible(
            self._client,
            local,
            file_id,
            size=self._settings.probe_bytes,
            timeout_s=self._settings.visible_timeout_s,
            clock=self._clock,
            sleep=self._sleep,
            retry=ONE_ATTEMPT,
        )
        return file_id, written, self._make_cold(local, source, file_id)

    # -- evictions -------------------------------------------------------------------------------------------------
    def _empty_on(self, provider: str, file_id: str) -> bool:
        """Whether `provider`'s own block map shows it holding none of the probe; unknown is not empty."""
        try:
            answer = self._client.get(f"/data/{file_id}/distribution", provider=provider, retry=ONE_ATTEMPT)
            return held_bytes(provider_blocks(answer, self._ids[provider])) == 0
        except (PlatformError, ValueError):
            return False

    def _schedule_eviction(self, provider: str, file_id: str) -> str:
        body = {
            "type": "eviction",
            "evictingProviderId": self._ids[provider],
            "dataSourceType": "file",
            "fileId": file_id,
        }
        answer = self._client.mutate(
            "POST", "/transfers", provider=provider, json=body, space=self._space, gate_retry=ONE_ATTEMPT
        )
        tid = answer.get("transferId") if isinstance(answer, dict) else None
        if not isinstance(tid, str) or not tid:
            raise PlatformError(f"{self._label(provider)}: an eviction's answer holds no transfer id")
        return tid

    def _evict(
        self, provider: str, file_ids: Sequence[str], *, again: bool = True
    ) -> tuple[dict[str, bool], dict[str, list[str]]]:
        """Evict the probes from a provider, confirmed on its own block map; with `again`, schedule again what it still
        holds after `EVICTION_PAUSE_S`, within `evict_timeout_s`. Return, per probe, whether it was evicted and the
        transfers."""
        bound, began = self._settings.evict_timeout_s, self._clock()
        pending, tids = list(file_ids), {file_id: [] for file_id in file_ids}
        while pending:
            scheduled = {file_id: self._schedule_eviction(provider, file_id) for file_id in pending}
            for file_id, tid in scheduled.items():
                tids[file_id].append(tid)
            consistency.wait_transfers(
                self._client,
                provider,
                scheduled.values(),
                timeout_s=max(0.0, bound - (self._clock() - began)),
                poll_s=POLL_S,
                clock=self._clock,
                sleep=self._sleep,
                retry=ONE_ATTEMPT,
            )
            pending = [file_id for file_id in pending if not self._empty_on(provider, file_id)]
            if not again or not pending or self._clock() - began + EVICTION_PAUSE_S >= bound:
                break
            self._sleep(EVICTION_PAUSE_S)
        return {file_id: file_id not in pending for file_id in file_ids}, tids

    # -- the fetch -------------------------------------------------------------------------------------------------
    def fetch_probe(self, pair: Pair, file_id: str) -> PairResult:
        """Read the whole probe through the local provider, once, within `timeout_s`, and check its bytes."""
        local, source = pair
        names = self._label(local), self._label(source)
        bound, size = self._settings.timeout_s, self._settings.probe_bytes
        expected = hashlib.sha256(probe_content(self._ids[source], size)).hexdigest()
        began = self._clock()
        try:
            digest = self._digest(local, file_id, size)
        except PlatformError as error:
            remedy = REMEDY.format(local=names[0])
            reason = f"no transfer from {names[1]} to {names[0]} within {bound:g} s ({error}); {remedy}"
            return PairResult(*names, False, self._clock() - began, reason=reason, remedy=remedy)
        seconds = self._clock() - began
        if digest != expected:
            reason = f"the probe read from {names[1]} through {names[0]} is not the probe's bytes"
            return PairResult(*names, False, seconds, reason=reason)
        return PairResult(*names, True, seconds, size)

    def run(self, pairs: Sequence[Pair], workers: int) -> tuple[PairResult, ...]:
        """Prepare every pair, fetch the ready ones at once, and evict the probes afterwards when every pair passed."""
        ready: dict[Pair, tuple[str, bool, tuple[str, ...]]] = {}
        results: dict[Pair, PairResult] = {}
        for pair in pairs:
            try:
                ready[pair] = self.prepare(pair)
            except (PlatformError, consistency.WaitTimeout, ProbeNotReady) as error:
                local, source = (self._label(name) for name in pair)
                why = f"{source} -> {local} was not tested: its probe could not be made ready ({error})"
                results[pair] = PairResult(local, source, False, reason=why)
        fetched = consistency.bounded_map(lambda pair: self.fetch_probe(pair, ready[pair][0]), list(ready), workers)
        for pair, result in zip(ready, fetched, strict=True):
            results[pair] = _with(result, provisioned=ready[pair][1], cooled=ready[pair][2])
        if ready and all(result.ok for result in results.values()):
            results = self._evict_after(pairs, ready, results)
        return tuple(results[pair] for pair in pairs)

    def _evict_after(
        self,
        pairs: Sequence[Pair],
        ready: dict[Pair, tuple[str, bool, tuple[str, ...]]],
        results: dict[Pair, PairResult],
    ) -> dict[Pair, PairResult]:
        """Evict every fetched probe from the local provider, the same for every pair; record whether each took."""
        local = pairs[0][0]
        file_ids = [ready[pair][0] for pair in ready]
        try:
            evicted, tids = self._evict(local, file_ids, again=False)  # once: the next check confirms it cold
        except PlatformError:
            evicted, tids = dict.fromkeys(file_ids, False), {file_id: [] for file_id in file_ids}
        return {
            pair: _with(results[pair], evicted=evicted[ready[pair][0]], evictions=tuple(tids[ready[pair][0]]))
            if pair in ready
            else results[pair]
            for pair in pairs
        }


def _with(result: PairResult, **changes: Any) -> PairResult:
    return dataclasses.replace(result, **changes)


def check_links(
    config: PlatformConfig,
    client: OnezoneClient,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    workers: int = WORKERS,
    involving: str | None = None,
) -> LinkReport:
    """Check every (local, source) pair's link with a bounded fetch of its probe; never pass what was not shown.

    `clock` and `sleep` bound and pace the waits (a test's `Ticker`), `now` dates the window on the host's epoch clock.
    With `involving` (a provider's name), only the pairs it is in: every pair when it is the local provider, its own
    when it is a source, as after its restore. A failure of the platform is the failing pair's; any other exception is a
    defect and propagates."""
    pairs = tuple(p for p in link_pairs(config.spaces, list(config.providers)) if involving in (None, *p))
    started = now()
    if not pairs:
        detail = "no provider pair to check: every declared space is supported by the local provider alone"
        return LinkReport(True, detail, (), started, now())
    settings = config.link_check
    if settings is None:
        detail = (
            f"{len(pairs)} provider pair(s) a run can fetch through, and platform.toml declares no [link_check] probe "
            "space to test their links with, so none was tested"
        )
        return LinkReport(False, detail, (), started, now())
    results = _Check(config, client, settings, clock, sleep).run(pairs, workers)
    failed = [result for result in results if not result.ok]
    if failed:
        detail = "a provider link is stuck or untested: " + "; ".join(str(result.reason) for result in failed)
    else:
        timed = ", ".join(f"{r.source}->{r.local} {r.seconds:.2f} s" for r in results)  # the gate keeps the detail
        detail = (
            f"each of the {len(results)} provider pair(s) fetched its probe within {settings.timeout_s:g} s ({timed})"
        )
    return LinkReport(not failed, detail, results, started, now())


__all__ = [
    "PROBE_PREFIX",
    "REMEDY",
    "LinkReport",
    "PairResult",
    "ProbeNotReady",
    "check_links",
    "probe_content",
    "probe_name",
]
