# Provenance: new
"""The pre-campaign link check: every ordered (reading provider, source provider) pair transfers before any run (O35,
#221).

On the first real study's federation one provider's rtransfer link to a peer was stuck, with nothing logged and the
peer reachable by TCP and TLS, while every other pair transferred. Every read of that peer's files through the stuck
provider hung until its cell's bound, and the only symptom was an agent's hung read, which the study would have
scored. This check finds such a link before any run, and fails closed naming it.

**The pairs** are every ordered (reading provider, source) pair of two providers that support a declared space in
common (`linksettings.link_pairs`): the local provider reading from each source, and each source reading from the
local provider and from the other sources, n·(n-1) in all. Replication of a mirrored space moves data between any two
supporters, so a link that does not involve the local provider carries a study's reads too: on the live slot of 14 Nov
2026 a mirror's link to the local provider stuck after a third provider's outage, while every pair the local provider
reads through transferred and every check of those passed.
**The probe** of a pair is one small file that the check writes through the pair's source into the probe space of
`[link_check]`, a space no agent can see, and keeps: `expcore-link-probe-` and 16 hex digits of the SHA-256 of the
pair's key, `probe_bytes` long, with bytes derived from the same key (`linkprobe.py`). The key is the source's provider
id for the local provider's pairs, so their files are the ones the check always wrote, and the source's and the reader's
ids for any other reader: **one file per ordered pair**, because a provider that fetched a source's probe holds a copy,
which would serve another reader's fetch of the same file while its own link to the source is stuck. A provisioned
probe, never a file of the study's data: the study spaces are where agents work and their transfer listings are a
run's evidence, so the check touches nothing there, and a probe of known bytes lets the fetch be checked byte for
byte. It is written when it is missing (the Compose federation's `reset --clean` empties the probe space) or not the
probe's bytes, and kept otherwise, until a fetch of it fails: then its file is retired for good and the next check uses
another (below).

**Each pair**:

1. *Prepare.* The probe is found through the source, or written there; its bytes are read back through the source, which
   holds it (no transfer), and written again when they differ. Then, within `visible_timeout_s`, the reading provider
   must see it at its size and its block map, read through the reading provider, must show the source holding it whole
   and **no other provider holding any of it**. The whole block map is read, every provider the answer lists, not the
   providers of `[link_check]` alone, since a provider that is not one of them could serve the reading provider's read
   while the link under test is stuck. The pairs are prepared at most `WORKERS` at once. A listed provider that holds
   part of it is evicted, confirmed on its own block map within `evict_timeout_s`, when it is one of
   `[link_check].providers` (the client's gate admits an eviction through those and no others). One that is not,
   declared in platform.toml or not, fails the pair, named, and so does one whose holding cannot be read (a block map
   entry that does not parse is unknown, never empty): the check waits for it to read within `visible_timeout_s` and
   then fails, naming it. A provider the map does not list, or lists with no block, holds nothing. So the source alone
   holds the probe, and the reading provider has nowhere else to get it.
2. *Fetch.* One REST range read of the whole probe through the reading provider (`OnezoneClient.read_content`, bound to
   the probe space, so a file of any other space is refused before anything is sent), with `timeout_s` as its deadline
   and as the longest wait for an answer. It passes only when it ends in time with the probe's bytes. A read that times
   out, or that the reading provider answers with an error, is the pair's failure: "no transfer from providers[j] to
   providers[i] within N s", with the remedy. The fetches run at once, at most `WORKERS` together, so they take about
   one bound however many pairs there are. A fetch is never tried again in the same check, and **never of the same file
   in a later one**: a provider waits on its rtransfer synchronizer with no timeout (op-worker f118ed9,
   `gen_server2:call(..., infinity)`) and a later read of the same file range joins the same outstanding job until the
   synchronizer restarts it after 1,800 s, so a second read of that file would only wait on the same stuck job, even
   once the link works again. A failed probe is therefore **retired** (`linkprobe.ProbeFiles.retire`), and **a file id
   whose fetch ever failed is never read again**, whatever the file's name or generation and whether it still exists.
   The retirement is recorded first, as a marker file in the probe space (`expcore-link-retired-`, digests of the pair's
   key and of the retired file's id) written through the reading provider, which the next check lists the same way: no
   probe file a marker names is chosen. Then the next generation of the probe is written through the source, a new name
   and a new file id, and the failed file and the source's other probe files are deleted (a file with a stuck job may be
   deleted). The next check reads the new file, so a recovered link passes at once. When no probe file is left that a
   marker does not retire and a fresh one cannot be written, the pair fails closed, "no fresh probe for <source> to
   <local>: creation failed", and no retired file is read in its place. The deletions are best-effort: a retired file
   that stays is deleted by a later check of the pair that passes, and stays excluded until then; a marker is deleted
   once the source no longer lists its file, so the record is bounded by the retired files that remain. Only when the
   marker cannot be written and the failed file is not deleted is it not excluded for good: the failure says so, with a
   new file or without, and a later check may read it once more, retiring it again if it fails. The probe space is the
   check's alone, so no other reader has the file open.
3. *Evict afterwards*, only when every pair passed: each probe is evicted from its reading provider once, the eviction
   waited for and that provider's block map read. On Onedata 25.0 an eviction scheduled right after a fetch completes
   having evicted nothing (the same eviction about 30 s later works), so one that did not take is recorded, never
   retried and never a failure: the link was shown to work, and the next check's step 1 evicts the probe and confirms
   the local block map empty before it reads. When a pair fails the check answers at once, retiring its probe and
   evicting nothing.

**The remedy** of a stuck link is named with its failure: restart the reading provider's rtransfer link
(`rtransfer_config:restart_link()` in its op-worker), or restart that provider's container alone (`docker restart`,
never a Compose `down`). Waiting also clears it, after the synchronizer's 1,800 s.

**Fail closed.** A pair whose probe cannot be found, written, seen or made cold was not tested, and fails naming the
pair and why; so does every pair when platform.toml declares none of `[link_check]`; so does a pair whose probe a
provider outside `[link_check].providers` holds, or whose holding by any provider cannot be read, and a pair whose probe
files are all retired when a fresh one cannot be written. Unknown is never a pass.

**The record** (`LinkReport.to_json()`): each pair by position (`providers[i]`, never a name, O12; `local` is the
provider that reads, the agent's local provider or another), whether it passed,
the seconds and bytes of its fetch, whether its probe was written now, the evictions that made it cold and those that
evicted it afterwards, by transfer id, and whether that took; whether a failed probe was retired, and how many retired
files a passing check deleted and could not delete; the check's window on the host's epoch clock and every
transfer id it scheduled. The fetch itself is on-the-fly replication, which no transfer document records; the
evictions are documents of the probe space, never of a study space, so no study's transfer listing holds any of them,
and the ids and the window let a study set them apart in any other listing it reads.

**Where it runs.** `health.PlatformHealth` runs it at its first call, after the content-type check and the space, and
a failure holds every run until a re-check of the closed gate passes. It runs it again at every call after a failure,
and every `every_s` of the campaign (`[link_check]`, 600 s by default), so a link that sticks mid-campaign closes the
health gate: the run whose check found it is an infrastructure outcome, never scored, and the whole campaign is held
(the core's gate holds all dispatch, not one rights group's) until a check passes again. Periodic rather than per run:
each check costs a few seconds of eviction and block-map polls on 25.0, which every run would pay. A fault injector's
restore runs it for every ordered pair, not the restored provider's alone, and the restore fails while a pair does not
pass (`fault_serves.ServingProbe.links`): on the live slot a mirror's link to the local provider broke while a third
provider was down. `deploy/compose-federation`'s `up` runs it last and does not report the federation ready
until it passes.

**Its time.** The first check after the probe space was emptied writes the probes and waits for the reading providers
to see them (about 6 s on 25.0); every other check evicts the probes the last one left, a few seconds, fetches, and
evicts once more. The pairs are prepared, fetched and evicted at most `WORKERS` at once, so the time grows with the
number of pairs per `WORKERS`, not per pair: 20 pairs take about three rounds of what 2 took. A study keeps the
campaign's `health_timeout_s` above `visible_timeout_s + timeout_s +
evict_timeout_s` (150 s by default): a call that outlives it reads unhealthy, which closes the gate, or sets a run's
post-run check aside, though the links work.
"""

import dataclasses
import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from expcore.campaign.healthgate import Health
from expcore_onedata import consistency
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.linkprobe import (
    PROBE_PREFIX,
    NoFreshProbe,
    Probe,
    ProbeFiles,
    ProbeNotReady,
    probe_content,
    probe_name,
)
from expcore_onedata.linksettings import LinkCheckSettings, link_pairs
from expcore_onedata.placement import Range, file_fully_placed, held_bytes, provider_blocks
from expcore_onedata.readretry import ONE_ATTEMPT

VERSION = 1  # of the record
WORKERS = 8  # pairs prepared, fetched and evicted at once (a test sets it to 1: its fake clock is every thread's)
POLL_S = 2.0  # between reads of a block map or a transfer's record
EVICTION_PAUSE_S = 10.0  # before an eviction that took nothing is scheduled again
REMEDY = (
    "remedy: restart the rtransfer link on {local} (rtransfer_config:restart_link() in its op-worker) or restart "
    "{local}'s container alone (docker restart, never a compose down); it clears by itself only after about 1800 s"
)
Pair = tuple[str, str]  # (reading provider, source provider), by their names in platform.toml


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
    retired: bool | None = (
        None  # when the fetch failed: whether its probe's file is retired, so no later check reads it again
    )
    purged: int = 0  # the retired probes of the source that a passing check deleted
    unpurged: int = 0  # those it could not delete, which the next passing check deletes

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
            "retired": self.retired,
            "purged": self.purged,
            "unpurged": self.unpurged,
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


@dataclass(frozen=True)
class _Ready:
    """A pair whose probe is ready to be fetched: the probe, and the evictions that made it cold."""

    probe: Probe
    cooled: tuple[str, ...]


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
        self._names = {provider_id: name for name, provider_id in self._ids.items() if provider_id}
        self._files = ProbeFiles(client, settings, self._ids, config.spaces[0].support.local, clock, sleep)
        self._space = settings.space

    def _label(self, name: str) -> str:
        return self._client.provider_label(name)

    def _holdings(self, reader: str, file_id: str) -> tuple[dict[str, list[Range]], list[str]]:
        """What every provider of the probe space holds of the probe, as `reader`'s block map lists them: the blocks of
        each provider `[link_check]` names, and the labels of the listed providers whose blocks cannot be read. The
        whole map is read, not the configured providers alone: a provider that is not one of `[link_check].providers`
        (declared or not) cannot be evicted through the gate, so one that holds any of the probe fails the pair, named,
        and one whose holding cannot be read is never taken as empty."""
        answer = self._client.get(f"/data/{file_id}/distribution", provider=reader, retry=ONE_ATTEMPT)
        table = answer.get("distributionPerProvider") if isinstance(answer, Mapping) else None
        if not isinstance(table, Mapping):
            return {}, []  # an answer without its table shows no provider, so the check waits for one that does
        held: dict[str, list[Range]] = {}
        unreadable: list[str] = []
        for provider_id in table:
            name = self._names.get(provider_id)
            label = self._label(name) if name else self._foreign(provider_id)
            try:
                blocks = provider_blocks(answer, provider_id)
            except ValueError:
                unreadable.append(label)
                continue
            if name in self._settings.providers:
                held[name] = blocks
            elif held_bytes(blocks):
                raise ProbeNotReady(
                    f"{label} holds part of the probe and is not a provider of [link_check], so it can neither be "
                    "evicted nor left out of what the fetch reads from: list it in [link_check] providers (declare "
                    "it in [providers] first when it is not)"
                )
        return held, unreadable

    @staticmethod
    def _foreign(provider_id: str) -> str:
        """A provider platform.toml does not declare, by a digest of its id, never the id (O12)."""
        digest = hashlib.sha256(provider_id.encode()).hexdigest()[:8]
        return f"a provider platform.toml does not declare (id digest {digest})"

    def _make_cold(self, local: str, source: str, file_id: str) -> tuple[str, ...]:
        """Wait until the local block map shows the source holding the probe whole and every other provider of the probe
        space holding none of it, evicting any part another provider holds; return the evictions made, by transfer id.
        A holder that cannot be evicted, or whose holding cannot be read, never passes."""
        settings, began = self._settings, self._clock()
        made: list[str] = []
        evicted: set[str] = set()  # each provider is evicted from once, its retries inside `_evict`
        while True:
            try:
                held, unreadable = self._holdings(local, file_id)
            except PlatformError:
                held, unreadable = {}, []
            warm = [name for name, blocks in held.items() if name != source and held_bytes(blocks)]
            if (
                local in held
                and file_fully_placed(held.get(source, []), settings.probe_bytes)
                and not (warm or unreadable)
            ):
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
                if warm:
                    shown = f"{self._label(warm[0])} still holding part of it"
                elif unreadable:
                    shown = f"what {unreadable[0]} holds of it, readable"
                else:
                    shown = "the source holding it whole"
                raise ProbeNotReady(f"the block map of {self._label(local)} did not show {shown} in time")
            self._sleep(POLL_S)

    def prepare(self, pair: Pair) -> _Ready:
        """Make the pair's probe ready: written, seen by the local provider, held by the source alone."""
        local, source = pair
        probe = self._files.provision(source, local)
        consistency.wait_visible(
            self._client,
            local,
            probe.file_id,
            size=self._settings.probe_bytes,
            timeout_s=self._settings.visible_timeout_s,
            clock=self._clock,
            sleep=self._sleep,
            retry=ONE_ATTEMPT,
        )
        return _Ready(probe, self._make_cold(local, source, probe.file_id))

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
        """Read the whole probe through the reading provider, once, within `timeout_s`, and check its bytes."""
        local, source = pair
        names = self._label(local), self._label(source)
        bound, size = self._settings.timeout_s, self._settings.probe_bytes
        expected = hashlib.sha256(self._files.content(source, local)).hexdigest()
        began = self._clock()
        try:
            digest = self._files.digest(local, file_id, size)
        except PlatformError as error:
            remedy = REMEDY.format(local=names[0])
            reason = f"no transfer from {names[1]} to {names[0]} within {bound:g} s ({error}); {remedy}"
            return PairResult(*names, False, self._clock() - began, reason=reason, remedy=remedy)
        seconds = self._clock() - began
        if digest != expected:
            reason = f"the probe read from {names[1]} through {names[0]} is not the probe's bytes"
            return PairResult(*names, False, seconds, reason=reason)
        return PairResult(*names, True, seconds, size)

    def _settle(self, pair: Pair, ready: _Ready, result: PairResult) -> PairResult:
        """What follows a pair's fetch. A probe that passed is kept, and the retired probes of its source are deleted. A
        probe whose fetch failed is retired, recorded in the probe space, so no later check reads the file a stuck
        fetch may still be waiting on (`linkprobe.py`)."""
        local, source = pair
        if result.ok:
            deleted, undeleted = self._files.purge(source, ready.probe)
            return _with(result, purged=deleted, unpurged=undeleted)
        retired = self._files.retire(source, local, ready.probe)
        note = ""
        if not retired.ok:
            cause = retired.why or "not deleted"
            note = f"; its probe could not be retired ({cause}), so a later check may read it again"
        return _with(result, retired=retired.ok, reason=f"{result.reason}{note}", unpurged=retired.undeleted)

    def _prepared(self, pair: Pair) -> _Ready | PairResult:
        """The pair's probe made ready, or the failed result of a pair that could not be tested."""
        local, source = (self._label(name) for name in pair)
        try:
            return self.prepare(pair)
        except NoFreshProbe as error:
            return PairResult(local, source, False, reason=str(error))
        except (PlatformError, consistency.WaitTimeout, ProbeNotReady) as error:
            why = f"{source} -> {local} was not tested: its probe could not be made ready ({error})"
            return PairResult(local, source, False, reason=why)

    def run(self, pairs: Sequence[Pair], workers: int) -> tuple[PairResult, ...]:
        """Prepare every pair and fetch the ready ones, at most `workers` at once, settle each probe, and evict the
        probes afterwards when every pair passed."""
        ready: dict[Pair, _Ready] = {}
        results: dict[Pair, PairResult] = {}
        for pair, outcome in zip(pairs, consistency.bounded_map(self._prepared, list(pairs), workers), strict=True):
            if isinstance(outcome, _Ready):
                ready[pair] = outcome
            else:
                results[pair] = outcome
        fetched = consistency.bounded_map(
            lambda pair: self.fetch_probe(pair, ready[pair].probe.file_id), list(ready), workers
        )
        for pair, result in zip(ready, fetched, strict=True):
            made = _with(result, provisioned=ready[pair].probe.written, cooled=ready[pair].cooled)
            results[pair] = self._settle(pair, ready[pair], made)
        if ready and all(result.ok for result in results.values()):
            results = self._evict_after(pairs, ready, results, workers)
        return tuple(results[pair] for pair in pairs)

    def _evict_reader(self, reader: str, file_ids: list[str]) -> tuple[dict[str, bool], dict[str, list[str]]]:
        """Evict the probes a reading provider fetched, once: the next check confirms them cold."""
        try:
            return self._evict(reader, file_ids, again=False)
        except PlatformError:
            return dict.fromkeys(file_ids, False), {file_id: [] for file_id in file_ids}

    def _evict_after(
        self,
        pairs: Sequence[Pair],
        ready: dict[Pair, _Ready],
        results: dict[Pair, PairResult],
        workers: int,
    ) -> dict[Pair, PairResult]:
        """Evict every fetched probe from the provider that read it, each reading provider's together, the providers at
        most `workers` at once; record whether each took."""
        readers = list(dict.fromkeys(reader for reader, _ in ready))
        by_reader = [[ready[pair].probe.file_id for pair in ready if pair[0] == reader] for reader in readers]
        outcomes = consistency.bounded_map(
            lambda index: self._evict_reader(readers[index], by_reader[index]), list(range(len(readers))), workers
        )
        evicted = {file_id: done for found, _ in outcomes for file_id, done in found.items()}
        tids = {file_id: made for _, found in outcomes for file_id, made in found.items()}
        return {
            pair: _with(
                results[pair],
                evicted=evicted[ready[pair].probe.file_id],
                evictions=tuple(tids[ready[pair].probe.file_id]),
            )
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
    workers: int | None = None,
) -> LinkReport:
    """Check every ordered (reader, source) pair's link with a bounded fetch of its probe; never pass what was not
    shown.

    `clock` and `sleep` bound and pace the waits (a test's `Ticker`), `now` dates the window on the host's epoch clock,
    and `workers` (`WORKERS` by default) is how many pairs are prepared, fetched and evicted at once.
    A failure of the platform is the failing pair's; any other exception is a defect and propagates."""
    pairs = link_pairs(config.spaces, list(config.providers))
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
    results = _Check(config, client, settings, clock, sleep).run(pairs, WORKERS if workers is None else workers)
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
