# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/scripted/metrics.py, blob bc25531c7aac74a09ff74c730fceabd8119cd2c5
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: icsoc-2026-code-agent @ c1e76c2, harness/verifier.py:39-45,132-171 (retry resets, never a real answer)
#   merged: icsoc-2026-code-agent @ c1e76c2, harness/verifier.py:290-302 (a provider's physical size from the block map)
#   merged: onedata-mcp @ 0cc17ce, benchmark/fixture_runner.py:269-281 (the transfer listing read to its last page)
#   changes: see git history of this file from its import commit
"""Platform probes as instruments: file distribution, transfer records and bytes per provider pair (O08).

`PlatformProbes` is an `expcore.plugin.Instrument`. It reads the platform at the start of a run and again at its end, and
`finish` returns one snapshot that goes into the run record under the namespace `platform_probes`. A scorer reads that
snapshot and never the live platform (R13). The snapshot holds, for the files the study names:

- each tracked provider's block map before and after (merged blocks, held bytes);
- the record of every transfer the provisioner scheduled (`transfers`: state and counters as the provider reports them);
- the record of every transfer of the space that started in the run's window, whoever started it: the harness, the
  agent through the tool plane, or a QoS requirement (`space_transfers`: its type, source and target by position
  label, state, counters, and schedule, start and end times; `transferlog.py` says how it is read and where it fails
  closed). An unreadable or unconfirmed listing is `probe_ok` false with a reason and no records, never no transfers;
- the bytes moved between each pair of providers, from the growth of a provider's block map, attributed to a source
  only where exactly one other provider held those bytes at the start. Bytes that two or more providers held, and
  bytes no other provider held, are counted apart and attributed to nobody. No pair is ever derived from a transfer's
  status (O01);
- per tracked receiving provider, its gained bytes split (`on_demand`): `transfer_bytes_in`, the byte counters of the
  jobs in the window that replicate into it, and `on_demand_lower_bound`, the gained bytes less those, floored at zero
  and only a lower bound (`transferwindow.py` names the conditions); None when the block maps or the listing are unknown.

**`gained_bytes` is placement gained, not a net change and not bytes on the wire.** It is the bytes of the blocks a
provider's block map (`GET /data/{id}/distribution`) shows at run end that it did not show at run start; what the map
showed at the start and no longer does is `lost_bytes`, kept apart, so replacing 100 bytes with 100 others gains 100
and loses 100. Bytes fetched and evicted again during the run, bytes read through a provider that did not keep them,
and bytes moved more than once leave no trace in it, and it says nothing of what crossed the network.

**A probe that fails is recorded as failed and its value is `None`, never guessed (O08).** `run_probe` sends the read
once when the provider answers, whatever it answers (a 404 is an answer); it retries only a transport failure (no
answer came back), a bounded number of times. Anything that depends on a failed probe is `None` in the snapshot and the
snapshot says it is incomplete, so a scorer cannot mistake a missing reading for a zero.

The probes run outside the agent's time, before the loop starts and after it ends. The per-container counters of the
earlier study (network interfaces, cgroups, gate logs) belong to the sandbox and are not here.
"""

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.placement import Range, held_bytes, merged, uncovered
from expcore_onedata.provision import Listings, Provisioner
from expcore_onedata.transferlog import MAX_LISTED, PAGE_LIMIT, TransferLog
from expcore_onedata.transferwindow import Window, split_gained

NAMESPACE = "platform_probes"
VERSION = 2
BLOCKS_SHOWN = 2000  # the most merged blocks recorded per file and provider; the count is always recorded


@dataclass(frozen=True)
class ProbeResult:
    """What a probe found: the value when it worked, and otherwise why not; the value of a failed probe is None."""

    ok: bool
    value: Any
    attempts: int
    status: int | None = None
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Return the result without its value, as plain JSON."""
        return {"probe_ok": self.ok, "attempts": self.attempts, "status": self.status, "error": self.error}


def run_probe(
    read: Callable[[], Any],
    *,
    attempts: int = 3,
    backoff_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> ProbeResult:
    """Run a read-only probe: one request when the provider answers, up to `attempts` when no answer comes back.

    A real HTTP answer, a 404 or a 500 included, is final: it is recorded as the probe's failure and not retried. A
    transport failure (a reset, a refusal, a timeout) is retried with `backoff_s` between tries, and after the last the
    probe is failed with no value. Anything else the read raises is a defect and propagates.
    """
    if attempts < 1:
        raise ValueError("a probe makes at least one attempt")
    for attempt in range(1, attempts + 1):
        try:
            return ProbeResult(True, read(), attempt)
        except PlatformError as error:
            if not error.transport:
                answered = f"the provider answered {error.status}" if error.status else "the answer was unusable"
                return ProbeResult(False, None, attempt, status=error.status, error=answered)
            if attempt == attempts:
                return ProbeResult(False, None, attempt, error=f"no answer after {attempts} attempt(s)")
            sleep(backoff_s)
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns


def _intersection(first: Iterable[Range], second: Iterable[Range]) -> list[Range]:
    """Return the parts of `first` that `second` also holds."""
    first = merged(first)
    return merged(uncovered(uncovered(second, first), first))


def _split_by_holders(gained: list[Range], holders: Mapping[str, list[Range]]) -> tuple[dict[str, int], int, int]:
    """Count the gained bytes held by exactly one source, by two or more, and by none; the first is per source."""
    points = sorted(
        {edge for off, length in gained for edge in (off, off + length)}
        | {edge for held in holders.values() for off, length in held for edge in (off, off + length)}
    )
    sole: dict[str, int] = {}
    shared = unheld = 0
    for start, end in pairwise(points):
        if not any(off <= start and end <= off + length for off, length in gained):
            continue
        covering = [
            s for s, held in holders.items() if any(off <= start and end <= off + length for off, length in held)
        ]
        if len(covering) == 1:
            sole[covering[0]] = sole.get(covering[0], 0) + end - start
        elif covering:
            shared += end - start
        else:
            unheld += end - start
    return sole, shared, unheld


def bytes_by_pair(before: Mapping[str, list[Range]], after: Mapping[str, list[Range]]) -> dict[str, Any]:
    """Describe how each provider's holdings changed, and which provider the new bytes came from, from block maps alone.

    For each destination the bytes it gained are split by who held them at the start: exactly one other provider (the
    source of those bytes), two or more (`ambiguous_bytes`), or none (`unattributed_bytes`). Bytes it lost are `lost`.
    """
    out: dict[str, Any] = {}
    for destination, now in after.items():
        was = before.get(destination, [])
        gained = merged(uncovered(was, now))
        others = {s: _intersection(gained, held) for s, held in before.items() if s != destination}
        sole, shared, unheld = _split_by_holders(gained, others)
        out[destination] = {
            "gained_bytes": held_bytes(gained),
            "from": sole,
            "ambiguous_bytes": shared,
            "unattributed_bytes": unheld,
            "lost_bytes": held_bytes(uncovered(now, was)),
        }
    return out


def _blocks_json(result: ProbeResult) -> dict[str, Any]:
    """Return one block-map reading as JSON: the blocks (capped) and the bytes, or the failure with nulls."""
    if not result.ok:
        return {**result.to_json(), "held_bytes": None, "block_count": None, "blocks": None, "blocks_truncated": None}
    blocks = merged(result.value)
    return {
        **result.to_json(),
        "held_bytes": held_bytes(blocks),
        "block_count": len(blocks),
        "blocks": [list(b) for b in blocks[:BLOCKS_SHOWN]],
        "blocks_truncated": len(blocks) > BLOCKS_SHOWN,
    }


class PlatformProbes:
    """The platform's instrument: the files' block maps before and after a run, the transfer records, bytes per pair."""

    namespace = NAMESPACE

    def __init__(
        self,
        client: OnezoneClient,
        provisioner: Provisioner,
        files: Mapping[str, str],
        providers: Sequence[str],
        *,
        transfers: Callable[[], Iterable[tuple[str, str, str]]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        page_limit: int = PAGE_LIMIT,
        max_listed: int = MAX_LISTED,
    ) -> None:
        """Track `files` (label to space-relative path) on `providers`; transfers default to the provisioner's log.

        `now` is the epoch clock the run's window is read on. `page_limit` ids are asked for per page of the space's
        transfer listing, and past `max_listed` distinct ids the listing is unknown.
        """
        self._client, self._provisioner = client, provisioner
        # One instrument per space: with several declared, each has a namespace of its own (`platform_probes_space1`
        # for spaces[1]) and its snapshot names its space by position; one space keeps the namespace it always had.
        several = bool(provisioner.where)  # a position starts every message only when several spaces are declared
        index = provisioner.space.position.removeprefix("spaces[").removesuffix("]")
        self.namespace = f"{NAMESPACE}_space{index}" if several else NAMESPACE
        self._space = provisioner.space.position if several else None
        positions: dict[str, int] = {}
        for index, path in enumerate(files.values()):  # one file tracked twice would count its gained bytes twice
            if path in positions:
                raise ValueError(f"files[{positions[path]}] and files[{index}] name the same path; track a file once")
            positions[path] = index
        self._files, self._providers = dict(files), list(providers)
        self._before_ids: dict[str, str] = {}
        self._transfers = transfers if transfers is not None else provisioner.transfers
        self._sleep, self._now = sleep, now
        self._log = TransferLog(
            client,
            provisioner,
            self._providers,
            self._probe,
            sleep=sleep,
            now=now,
            page_limit=page_limit,
            max_listed=max_listed,
        )
        self._before: dict[str, dict[str, ProbeResult]] | None = None
        self._started: float | None = None

    def _probe(self, read: Callable[[], Any]) -> ProbeResult:
        settings = self._provisioner.settings
        return run_probe(read, attempts=settings.probe_attempts, backoff_s=settings.probe_backoff_s, sleep=self._sleep)

    def _file_id(self, path: str, listings: Listings) -> ProbeResult:
        """Resolve a path to its entry; a path that is no longer there is a failed probe, not an exception.

        The read does not wait: the provisioner's wait for a new space root would retry a real 404 inside one attempt,
        out of sight of the probe (O08). The only retry is the probe's own, of a network reset, and it is counted.
        `listings` is the reading's cache: a directory listed for one tracked file is not listed again for the next,
        and a listing that failed is not in it, so the next file's probe reads it afresh.
        """

        def read() -> Any:
            try:
                return self._provisioner.resolve(path, wait=False, listings=listings)
            except KeyError:
                raise PlatformError("the tracked file is not in the space", status=404) from None

        return self._probe(read)

    def _read_maps(self) -> tuple[dict[str, ProbeResult], dict[str, dict[str, ProbeResult]]]:
        """Read every tracked file's entry, listing each directory once, and each tracked provider's block map of it."""
        entries: dict[str, ProbeResult] = {}
        maps: dict[str, dict[str, ProbeResult]] = {}
        listings: Listings = {}
        for label, path in self._files.items():
            entries[label] = entry = self._file_id(path, listings)
            maps[label] = {}
            for provider in self._providers:
                if entry.ok:
                    maps[label][provider] = self._probe(
                        lambda p=provider, e=entry.value: self._provisioner.blocks(e.file_id, p)
                    )
                else:
                    maps[label][provider] = ProbeResult(False, None, 0, entry.status, "the file could not be resolved")
        return entries, maps

    def start(self, identity: Mapping[str, Any]) -> None:
        """Read the block maps before the run; `identity` is not used, since the snapshot is of the platform's state."""
        self._started = self._now()
        entries, self._before = self._read_maps()
        self._before_ids = {label: entry.value.file_id for label, entry in entries.items() if entry.ok}

    def _transfer_records(self) -> list[dict[str, Any]]:
        records = []
        for provider, tid, kind in self._transfers():
            found = self._probe(lambda p=provider, t=tid: self._client.get(f"/transfers/{t}", provider=p))
            body = found.value if found.ok and isinstance(found.value, dict) else {}
            records.append(
                {
                    "provider": provider,
                    "transfer_id": tid,
                    "kind": kind,
                    **found.to_json(),
                    "state": next(
                        (body[k] for k in ("transferStatus", "replicationStatus", "evictionStatus") if k in body), None
                    ),
                    "bytes_replicated": body.get("bytesReplicated"),
                    "files_replicated": body.get("filesReplicated"),
                    "files_evicted": body.get("filesEvicted"),
                    "files_failed": body.get("filesFailed"),
                }
            )
        return records

    def finish(self) -> dict[str, Any]:
        """Read the block maps again and return the snapshot; everything that rests on a failed probe is None."""
        entries, after = self._read_maps()
        window = None if self._started is None else Window(self._started, self._now())
        before = self._before or {}
        files: dict[str, Any] = {}
        pair_totals: dict[str, dict[str, Any]] = {}
        complete = self._before is not None
        counted: dict[str, str] = {}  # file id -> the label its bytes were counted under, so no file counts twice
        for label in self._files:
            size = entries[label].value.size if entries[label].ok else None
            file_id = entries[label].value.file_id if entries[label].ok else None
            if file_id is not None and file_id in counted:
                files[label] = {"size": size, "entry": entries[label].to_json(), "duplicate_of": counted[file_id]}
                continue
            if file_id is not None:
                counted[file_id] = label
            readings = {
                p: {
                    "before": _blocks_json(before[label][p]) if label in before else None,
                    "after": _blocks_json(after[label][p]),
                }
                for p in self._providers
            }
            # The same file at both ends: a path replaced by another file during the run, or one whose identity was not
            # read at either end, gives no gained bytes that can be set against its transfers.
            same_file = file_id is not None and self._before_ids.get(label) == file_id
            usable = (
                same_file
                and label in before
                and all(before[label][p].ok and after[label][p].ok for p in self._providers)
            )
            complete = complete and usable
            pairs = (
                bytes_by_pair(
                    {p: merged(before[label][p].value) for p in self._providers},
                    {p: merged(after[label][p].value) for p in self._providers},
                )
                if usable
                else None
            )
            if pairs:
                for destination, row in pairs.items():
                    total = pair_totals.setdefault(
                        destination,
                        {"gained_bytes": 0, "from": {}, "ambiguous_bytes": 0, "unattributed_bytes": 0, "lost_bytes": 0},
                    )
                    for key in ("gained_bytes", "ambiguous_bytes", "unattributed_bytes", "lost_bytes"):
                        total[key] += row[key]
                    for source, n in row["from"].items():
                        total["from"][source] = total["from"].get(source, 0) + n
            files[label] = {"size": size, "entry": entries[label].to_json(), "providers": readings, "pairs": pairs}
        transfers = self._transfer_records()
        file_ids = {entries[label].value.file_id: label for label in self._files if entries[label].ok}
        harness = {tid for _, tid, _ in self._transfers()}
        log = self._log.read(window, file_ids, harness)
        ok = complete and all(t["probe_ok"] for t in transfers) and log.section["probe_ok"]
        gained = {p: row["gained_bytes"] for p, row in pair_totals.items()}
        return {
            "version": VERSION,
            **({} if self._space is None else {"space": self._space}),  # its position, with several spaces only
            "ok": ok,
            "providers": list(self._providers),
            "seconds": None if self._started is None else round(self._now() - self._started, 3),
            "files": files,
            "transfers": transfers,
            "space_transfers": log.section,
            "pairs": pair_totals if complete else None,
            "on_demand": (
                split_gained(
                    gained, log.inside, self._provisioner.provider_named, flagged=log.flagged, everywhere=log.everywhere
                )
                if complete and log.inside is not None
                else None
            ),
        }
