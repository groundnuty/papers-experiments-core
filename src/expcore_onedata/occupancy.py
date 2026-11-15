# Provenance: new
"""What each provider holds of a space: its files' block maps, summed, and the space root's counter beside them (O01,
O08, O46).

**The verdict comes from the files.** The space root's distribution (`GET /data/{root}/distribution`) gives, per
provider, a physical size from the space's directory statistics (Oneprovider 25.0's REST description,
`DataDirDistribution.physicalSize`, "actual amount of storage consumed on a given storage backend by all regular files
in the directory's subtree"). **That counter is wrong in both directions, for good or for seconds** (op-worker
f118ed9): after an eviction that emptied a space's only regular file (depth 3, 43,096,327 B) at a mirror, the root's
distribution, asked of either provider, went on crediting the mirror 1,048,576 B, exactly 1 MiB, more than 23 minutes
later, while the file's distribution showed the mirror physical 0 and blocks `[]`, the mirror's posix storage held the
file with 0 B allocated, and nothing else held bytes (no trash entry, no hidden file, no transfer active or waiting);
an earlier 64 KiB read at offset 8 MiB had placed one 1 MiB block in the file's middle. The other way, a placement of
1,572,864 B took 9.534 s (34 polls) to show in the counter, which read 0 meanwhile (the Day-0 facts of 13 Nov). So the
counter is never the sole verdict: each provider that can hold the space's bytes, every provider of the space other
than the data provider unless the caller declares it never does (`never_holds`), has the block map of each regular
file of the space read **there**, whatever the counter says, and the sum is what it holds. The counter is read beside
them for two uses: a provider declared never to hold whose counter reads 0 has no file read (the only fast path), and
each provider whose files were read and whose counter differs from their sum, higher or lower, gives an event
(`COUNTER_DISAGREES`) with both figures.

**Which files.** Every regular file (`REG`) and hard link (`LNK`) any provider's listing showed the reset's QoS
scan, both scans when it ran two (`Listing`, `resetqos.Found.listing`), is read at every such provider, so a file one
provider's listing missed is still read there, and the files cost no listing of their own; a provider that answers it
has no such file (`client.answered_absent`) holds none of it. A hard link shares its target's blocks under its own id,
so it is counted on its own: a provider whose files hold more than allowed with bytes in a hard link is refused with
that said, since a link and its target may count the same bytes twice (fail-closed). Nothing but the reset acts on the
space between its stages (`consistency.guard`), so a file created after the scan is not looked for. What no listing
shows, the trash and a deleted file still open (the space's tmp directory), is not read; the counter may count it
(the trash's, not the tmp directory's, by the source), and the event then shows the gap.

**Bounded.** The reads go `[placement] concurrency` at a time, at most `READS_MAX` in one reading, its calls together,
within `READ_BOUND_S`, looked at after every batch; past either the reading fails, closed, with the figures, never
falling back on the counter.

The counter is read only once no transfer runs on any provider (the caller makes sure, `reset.py`). Anything unknown
raises, never a zero: a provider the answer leaves out or marks failed, a backend that failed (directory statistics
turned off for the space answers so), a size that is not a whole number of bytes, or a block map that cannot be read.
A provider no declaration fits that supports the space is refused too, whatever its counter says: there is no
declared provider to read its files on.
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from expcore_onedata import consistency
from expcore_onedata.client import PlatformError, answered_absent
from expcore_onedata.placement import held_bytes, merged
from expcore_onedata.provision import Provisioner, ProvisionError
from expcore_onedata.readretry import ONE_ATTEMPT, ReadRetry
from expcore_onedata.transferrecord import Unreadable

COUNTER_DISAGREES = "occupancy_counter_disagrees"  # the event of a counter that is not its files' sum
READS_MAX = 200_000  # block-map reads one reading may make, every provider together
READ_BOUND_S = 1800.0  # the longest one reading's block-map reads may take
ROUND = 16  # reads sent per worker between two looks at the clock
SHOWN = 5  # files a refusal names per provider

Label = Callable[[str], str]


def _physical(entry: Any) -> int:
    """Sum one provider's physical size over its storage backends; raise Unreadable unless every part is known."""
    if not isinstance(entry, Mapping) or entry.get("success") is not True:
        raise Unreadable("the space root's distribution has no physical size for a provider: it is unknown")
    backends = entry.get("distributionPerStorageBackend")
    if not isinstance(backends, Mapping) or not backends:
        raise Unreadable("the space root's distribution has no storage backend for a provider: it is unknown")
    total = 0
    for backend in backends.values():
        known = isinstance(backend, Mapping) and backend.get("success") is True
        size = backend.get("physicalSize") if known else None
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise Unreadable("a storage backend's physical size of the space is unknown (directory statistics off?)")
        total += size
    return total


def physical_sizes(
    provisioner: Provisioner,
    providers: Sequence[str],
    *,
    retry: ReadRetry = ONE_ATTEMPT,
    undeclared_ok: bool = True,
) -> dict[str, int]:
    """Return each declared provider's physical bytes of the provisioner's space by the counter, from one read of its
    root's distribution on the data provider; `providers` must all be in the answer. Raise Unreadable for anything
    unknown, and ProvisionError when an undeclared provider holds bytes, or, without `undeclared_ok`, is in the answer
    at all. The read that timed out is tried again under `retry`, the policy of a caller that reads it as plumbing (the
    reset); by default it is one attempt (O29)."""
    root = provisioner.root()
    answer = provisioner.client.get(f"/data/{root}/distribution", provider=provisioner.settings.holder, retry=retry)
    per = answer.get("distributionPerProvider") if isinstance(answer, Mapping) else None
    if not isinstance(per, Mapping):
        raise Unreadable("the space root's distribution answer has no 'distributionPerProvider' table")
    sizes: dict[str, int] = {}
    for provider_id, entry in per.items():
        name = provisioner.provider_named(provider_id)
        size = _physical(entry)
        if name is None:
            if size:
                raise ProvisionError("a provider no declaration fits holds bytes of the space")
            if not undeclared_ok:
                raise ProvisionError(
                    "a provider no declaration fits supports the space: its files cannot be read there, and the "
                    "directory statistics alone are no verdict"
                )
            continue
        sizes[name] = size
    missing = [provisioner.client.provider_label(p) for p in providers if p not in sizes]
    if missing:
        raise Unreadable(f"the space root's distribution leaves out {', '.join(missing)}: what it holds is unknown")
    return sizes


@dataclass(frozen=True)
class Listing:
    """The ids of the regular files (`REG`) and of the hard links (`LNK`) of the space that any provider's listing
    showed the reset's QoS scan: what the occupancy reads at every provider that can hold the space's bytes (O46)."""

    regular: frozenset[str] = frozenset()
    links: frozenset[str] = frozenset()

    def __or__(self, other: "Listing") -> "Listing":
        return Listing(self.regular | other.regular, self.links | other.links)

    def ids(self) -> list[str]:
        """Every id to read, sorted: a file's position in this list is how a message names it."""
        return sorted(self.regular | self.links)


@dataclass(frozen=True)
class Reading:
    """One reading of the occupancy: the counter per provider, the bytes each file's block map holds at each
    provider whose files were read (file id to bytes), and which of the ids are hard links."""

    counter: dict[str, int]
    files: dict[str, dict[str, int]]
    links: frozenset[str] = frozenset()
    carried: dict[str, int] = field(default_factory=dict)  # per provider, the files carried as empty, not read

    @property
    def reads(self) -> int:
        """The block maps this reading read: its files less those a reset's shortcut carried as empty."""
        return sum(len(held) for held in self.files.values()) - sum(self.carried.values())

    def held(self, provider: str) -> int:
        """What a provider holds of the space by its files, 0 for one whose files were not read (the fast path)."""
        return sum(self.files.get(provider, {}).values())

    def events(self, label: Label) -> list[dict[str, Any]]:
        """A `COUNTER_DISAGREES` event per provider whose files were read and whose counter is not their sum."""
        return [
            {"event": COUNTER_DISAGREES, "provider": label(q), "counter_bytes": self.counter[q], "files_bytes": held}
            for q in self.files
            if q in self.counter and (held := self.held(q)) != self.counter[q]
        ]

    def facts(self, label: Label) -> dict[str, Any]:
        """The record's figures: the counter (`physical_bytes`, every provider), the files' sums, the reads, the
        events; and the files carried as empty (`files_carried`) when a shortcut carried any."""
        facts = {
            "physical_bytes": {label(q): n for q, n in self.counter.items()},
            "files_bytes": {label(q): self.held(q) for q in self.files},
            "files_read": self.reads,
            "events": self.events(label),
        }
        return {**facts, "files_carried": sum(self.carried.values())} if self.carried else facts


class OccupancyExceeded(ProvisionError):
    """A provider's files hold more of the space than the reset allows. `facts` is what the failed stage records."""

    def __init__(self, message: str, facts: dict[str, Any]) -> None:
        super().__init__(message)
        self.facts = facts


@dataclass
class Budget:
    """What one reading has spent of its bounds: when it began, and the block-map reads it has made, every call
    together. Mutable on purpose: the calls of one reading share it."""

    began: float
    reads: int = field(default=0)


def read_files(
    p: Provisioner, wanted: Mapping[str, Sequence[str]], label: Label, budget: Budget
) -> dict[str, dict[str, int]]:
    """The bytes each file's block map holds at each provider, read there, `concurrency` at a time; raise
    ProvisionError, closed and with the figures, when the reading's reads would pass `READS_MAX` or its time
    `READ_BOUND_S`, which is looked at after every batch, the last included."""
    pairs = [(file_id, provider, i) for provider, ids in wanted.items() for i, file_id in enumerate(ids)]
    where = ", ".join(f"{label(q)}: {len(ids)} file(s)" for q, ids in wanted.items())
    if budget.reads + len(pairs) > READS_MAX:
        raise ProvisionError(
            f"the occupancy's per-file reading needs {budget.reads + len(pairs)} block-map read(s), past its budget of "
            f"{READS_MAX} ({where}); the directory statistics alone are no verdict"
        )
    budget.reads += len(pairs)
    step = max(1, p.settings.concurrency) * ROUND
    held: dict[str, dict[str, int]] = {provider: {} for provider in wanted}  # filled in place: the reading it returns
    for start in range(0, len(pairs), step):
        chunk = pairs[start : start + step]
        sizes = consistency.bounded_map(lambda key: _held(p, key, label), chunk, p.settings.concurrency)
        for (file_id, provider, _), size in zip(chunk, sizes, strict=True):
            held[provider][file_id] = size
        if p.clock() - budget.began > READ_BOUND_S:
            raise ProvisionError(
                f"the occupancy's per-file reading had read {start + len(chunk)} of {len(pairs)} block map(s) past its "
                f"bound of {READ_BOUND_S:.0f} s ({where}); the directory statistics alone are no verdict"
            )
    return held


def _held(p: Provisioner, key: tuple[str, str, int], label: Label) -> int:
    """The bytes one file's block map holds at one provider, read there. A map that cannot be read, an error or a
    missing provider in the answer included, is unknown and raises, naming the provider and the file's position: it
    never counts as 0."""
    file_id, provider, index = key
    try:
        return held_bytes(merged(p.blocks(file_id, provider, retry=p.retry)))
    except PlatformError as error:
        if answered_absent(error):
            return 0  # the provider has no such file, as one another listing showed but this one has not met yet
        raise Unreadable(
            f"the block map of files[{index}] at {label(provider)} is unknown ({error}): the occupancy's per-file "
            "reading has no verdict, and the directory statistics alone are none"
        ) from None


def holdings(
    p: Provisioner,
    listing: Listing,
    never_holds: Collection[str],
    *,
    providers: Sequence[str],
    label: Label,
    retry: ReadRetry = ONE_ATTEMPT,
    only: Mapping[str, Collection[str]] | None = None,
) -> Reading:
    """Read the occupancy: the block map of every listed file at each provider of the space other than the data
    provider and those in `never_holds`, then the counter (`physical_sizes` over `providers`, an undeclared provider in
    it refused), then the files of every other provider but those in `never_holds` whose counter reads an explicit 0:
    one the counter leaves out, or credits with bytes, is read too. One budget covers the reading. `only`, from a
    reset's shortcut (`resetshortcut.py`), names for some of the first providers the ids to read there; every other
    listed id is carried as holding nothing (`Reading.carried`), and a provider it does not name is read whole."""
    holder, ids, budget = p.settings.holder, listing.ids(), Budget(p.clock())
    support = [q for q in p.space.support.providers if q != holder]
    first = [q for q in support if q not in never_holds]
    narrowed = only or {}
    wanted = {q: [i for i in ids if i in narrowed[q]] if q in narrowed else ids for q in first}
    files = read_files(p, wanted, label, budget)
    carried = {q: len(ids) - len(wanted[q]) for q in first if q in narrowed}
    for q in carried:
        files[q].update((i, 0) for i in ids if i not in narrowed[q])  # in place: the reading's own table
    counter = physical_sizes(p, providers, retry=retry, undeclared_ok=False)
    skipped = {q for q in never_holds if counter.get(q) == 0}  # the only fast path: a 0 read, never a gap
    rest = [q for q in dict.fromkeys([*support, *counter]) if q != holder and q not in files and q not in skipped]
    reading = {**files, **read_files(p, dict.fromkeys(rest, ids), label, budget)}
    return Reading(counter, reading, listing.links, carried)


def judge(reading: Reading, allowed: Mapping[str, int], label: Label) -> None:
    """Raise OccupancyExceeded when a provider's files hold more of the space than `allowed` gives it (nothing by
    default), stating per provider the bytes its files hold, the figure allowed, the counter, and the files that hold
    bytes, each by its position among the files read there, sorted by id, never by a name or a path."""
    over = [q for q in reading.files if reading.held(q) > allowed.get(q, 0)]
    if not over:
        return
    details: dict[str, Any] = {}
    parts: list[str] = []
    for q in over:
        files = {f"files[{i}]": n for i, (_, n) in enumerate(sorted(reading.files[q].items())) if n}
        held, bound, counter = reading.held(q), allowed.get(q, 0), reading.counter.get(q)
        linked = sum(n for file_id, n in reading.files[q].items() if file_id in reading.links)
        details[label(q)] = {
            "files_bytes": held,
            "allowed_bytes": bound,
            "counter_bytes": counter,
            "links_bytes": linked,
            "files": files,
        }
        shown = ", ".join(f"{name} {n} bytes" for name, n in list(files.items())[:SHOWN])
        more = f", and {len(files) - SHOWN} more" if len(files) > SHOWN else ""
        links = (
            f"; {linked} bytes of it are in hard link(s), each counted on its own, so a link and its target may count "
            "the same bytes twice"
            if linked
            else ""
        )
        parts.append(
            f"{label(q)} holds {held} bytes of the space in {len(files)} file(s), beyond what the reset allows "
            f"({bound} bytes; the space root's directory statistics read {counter} bytes): {shown}{more}{links}"
        )
    raise OccupancyExceeded("; ".join(parts), {**reading.facts(label), "over": details})


__all__ = [
    "COUNTER_DISAGREES",
    "READS_MAX",
    "READ_BOUND_S",
    "Budget",
    "Listing",
    "OccupancyExceeded",
    "Reading",
    "holdings",
    "judge",
    "physical_sizes",
    "read_files",
]
