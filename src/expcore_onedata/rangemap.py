# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/scripted/s2ranges.py, blob 37fc53e2da12e927b555b235388dc7c5eee4f61c
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""The declared range map, and the `[placement]` settings (O02).

Onedata's transfer jobs replicate whole files, so a study that wants a byte range of a file on a provider places it by
reading those bytes through that provider, which fetches them on the fly (`ranges.py`). Which ranges, of which files, on
which providers is data, and it is declared: the source study's fixed layout is gone. A map is a list of rows, each a
file (a path under the space root), the receiving provider (a name declared in platform.toml) and its ranges
(`[offset, length]` in bytes). It sits inline in platform.toml or in a study's own file that platform.toml names:

```toml
[placement]
holder = "<provider>"           # the data provider: it holds the source copy, and nothing ever evicts it (O09)
map = "ranges.toml"             # optional: a file (relative to this one) of top-level [[map]] rows, or the rows below
poll_s = 2.0                    # seconds between polls of a transfer or a block map
transfer_timeout_s = 3600       # the longest a transfer is waited for
settle_timeout_s = 300          # the longest a block map is waited on to show a placement
consecutive_polls = 2           # polls in a row that must agree before a placement is believed (2 or more, O07)
request_timeout_s = 900         # one block-sized read or write
chunk_bytes = 16777216          # the largest single read or write: 16 MiB, the largest size measured never to stall (#186)
fresh_connection_bytes = 16777216  # a content write of at least this many bytes goes on a new connection (#186)
probe_attempts = 3              # tries at a read that fails with a network reset (O08)
probe_backoff_s = 1.0
transfer_settle_s = 30.0        # the wait between two observations of the transfer listing: at least its lag (O07)
transfer_settle_polls = 4       # the most observations of the listing before it counts as not settled (2 or more)
clock_skew_allowance_s = 5.0    # added to the providers' clock offsets that widen the run's window (O08)
concurrency = 16                # the most requests a bulk operation has in flight at once (eviction, block maps, reset)
qos_scan = "listing"            # how the reset finds QoS requirements: "listing" or "summaries" (a read per leaf, O33)
qos_fulfil_timeout_s = 120      # the longest the reset waits for a run's QoS requirement to be fulfilled before deleting it

[[placement.map]]               # a row (inline instead of `map = ...`); top-level [[map]] in the study's file
file = "dir/file.bin"           # relative to the space root
space = "<label>"               # the [[spaces]] entry the file lies in: required with several spaces, else optional
provider = "<provider>"         # the receiving provider
ranges = [[0, 4096], [8192, 1024]]
```

Every message names the file and the key and never echoes a value. `trace_reads` derives a row's ranges from a reader:
the study runs its own code on a local copy and the map records every byte that code read.
"""

import io
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from expcore.config.loading import ConfigError, Section, load_toml
from expcore_onedata.config import PlatformConfig
from expcore_onedata.placement import Range, merged
from expcore_onedata.spaces import ONE_SPACE_LABEL

PART = re.compile(r"[^/\x00-\x1f\\]{1,255}")
# 16 MiB, the largest chunk with no stall in any run of the bisection of #186 (`day0/2026-11-12-federation-facts-upload-
# and-stalls.md`): on the 25.0 development build a chunk of 32 or 64 MiB stalled on a reused keep-alive connection (3 of
# 289 reused PUTs, 0 of 336 on new ones) and 16 MiB passed every run either way. A content write at or above the
# threshold, which with these defaults is every full chunk, is sent on a new connection (`OnezoneClient.mutate`).
DEFAULT_CHUNK_BYTES = 16 << 20
DEFAULT_FRESH_CONNECTION_BYTES = 16 << 20
# How the reset finds a space's QoS requirements (O33): `listing` (the default), one recursive listing with
# `effQosInheritancePath` and a summary per entry a requirement is on, or `summaries`, a summary per leaf on every
# provider. The listing became the default after the 14 Nov 2026 live slot showed it takes `include_directories` and
# finds a requirement added through another provider (`resetqos.py`); it is in no fingerprint, so a study's store
# does not depend on it.
QOS_SCANS = ("summaries", "listing")
DEFAULT_QOS_SCAN = "listing"


@dataclass(frozen=True)
class PlacementSettings:
    """The bounds and the data provider of placement."""

    holder: str
    poll_s: float = 2.0
    transfer_timeout_s: float = 3600.0
    settle_timeout_s: float = 300.0
    consecutive_polls: int = 2
    request_timeout_s: float = 900.0
    chunk_bytes: int = DEFAULT_CHUNK_BYTES
    probe_attempts: int = 3
    probe_backoff_s: float = 1.0
    transfer_settle_s: float = 30.0  # at least the lag of a provider's transfer listing behind a new job
    transfer_settle_polls: int = 4
    clock_skew_allowance_s: float = 5.0  # beyond the providers' estimated clock offsets
    concurrency: int = 16  # the named bound on a bulk operation's requests in flight (eviction, block maps, reset)
    qos_scan: str = DEFAULT_QOS_SCAN  # how the reset finds QoS requirements (`resetqos.py`); see QOS_SCANS
    qos_fulfil_timeout_s: float = 120.0  # the reset's wait for a requirement to be fulfilled (`resetremove.py`)
    # Last, so a caller that passes the settings positionally keeps the order it had before #196 (O25): every field
    # added later goes at the end of the class.
    fresh_connection_bytes: int = DEFAULT_FRESH_CONNECTION_BYTES  # a write this large: a new connection

    def reset_settings(self) -> dict[str, Any]:
        """The settings the core records at a start and compares at a resume, `StudyPlugin(reset_settings=...)` (C35).

        Every field after its default, as `placement.<name>`: numbers and text only. They are in no fingerprint, since
        they change how placement and the reset work and not what an agent sees, so a campaign resumed after one
        changed (or after a default changed under a file that never declared it) says so. The data provider's name is
        left out, since the record travels with the export and the replication package, which name no provider. The
        map is not here either: it says what is placed and where, not how, and it is no setting.
        """
        return {f"placement.{item.name}": getattr(self, item.name) for item in fields(self) if item.name != "holder"}


@dataclass(frozen=True)
class RangeRow:
    """One file on one receiving provider: the merged ranges that must be there, and the label of the space the file
    lies in (`[[spaces]]`; the only space's label when the row names none)."""

    file: str
    provider: str
    ranges: tuple[Range, ...]
    space: str = ONE_SPACE_LABEL


@dataclass(frozen=True)
class RangeMap:
    """The declared map: at most one row per file and provider."""

    rows: tuple[RangeRow, ...] = field(default_factory=tuple)

    def for_provider(self, provider: str) -> tuple[RangeRow, ...]:
        """Return the rows whose receiving provider is the one named."""
        return tuple(row for row in self.rows if row.provider == provider)

    def for_space(self, label: str) -> tuple[RangeRow, ...]:
        """Return the rows whose file lies in the space the label names."""
        return tuple(row for row in self.rows if row.space == label)

    def files(self) -> tuple[str, ...]:
        """Return the distinct files the map names, in the order of first appearance."""
        return tuple(dict.fromkeys(row.file for row in self.rows))

    def total_bytes(self) -> int:
        """Return the bytes the map asks to be placed, over all rows."""
        return sum(length for row in self.rows for _, length in row.ranges)


@dataclass(frozen=True)
class Placement:
    """The settings and the map `load_placement` read."""

    settings: PlacementSettings
    map: RangeMap


def safe_path(path: str) -> tuple[str, ...]:
    """Split a space-relative path into its parts; raise ValueError for an absolute, empty or climbing one."""
    parts = tuple(path.split("/")) if isinstance(path, str) and path else ()
    if not parts or any(p in ("", ".", "..") or not PART.fullmatch(p) for p in parts):
        raise ValueError("a path must be relative to the space root, with plain names and no '.' or '..'")
    return parts


def _number(section: Section, key: str, default: float, *, minimum: float, integer: bool = False) -> Any:
    """Read an optional finite number at or above a minimum."""
    value = section.data.get(key, default)
    kind = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind) or not math.isfinite(value) or value < minimum:
        raise section.fail(key, f"must be {'an integer' if integer else 'a number'} of at least {minimum:g}")
    return value


def _ranges(section: Section) -> tuple[Range, ...]:
    """Read a row's ranges: a non-empty array of `[offset, length]` pairs of non-negative and positive integers."""
    raw = section.data.get("ranges")
    if not isinstance(raw, list) or not raw:
        raise section.fail("ranges", "must be a non-empty array of [offset, length] pairs")
    pairs: list[Range] = []
    for item in raw:
        ok = (
            isinstance(item, list)
            and len(item) == 2
            and all(isinstance(n, int) and not isinstance(n, bool) for n in item)
        )
        if not ok or item[0] < 0 or item[1] < 1:
            raise section.fail(
                "ranges", "each must be [offset, length], an offset of 0 or more and a length of 1 or more"
            )
        pairs.append((item[0], item[1]))
    return tuple(merged(pairs))


def parse_rows(rows: Any, file: Path, where: str, config: PlatformConfig, holder: str) -> list[RangeRow]:
    """Validate the rows of a map; the receiving provider must be declared and must not be the data provider of the
    row's space (its own holder, or `holder`). With several spaces a row names its space by its label."""
    if not isinstance(rows, list):
        raise ConfigError(f"{file}: {where}: must be an array of tables")
    parsed: list[RangeRow] = []
    for number, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise ConfigError(f"{file}: {where}: row {number} must be a table")
        section = Section(row, file, f"{where} row {number}")
        section.reject_unknown(("file", "provider", "ranges", "space"))
        path, provider = section.string("file"), section.string("provider")
        label = section.optional_string("space")
        try:
            entry = config.space_named(label)
        except ConfigError:
            raise section.fail(
                "space", "must be the label of a declared space; with several spaces it is required"
            ) from None
        try:
            safe_path(path)
        except ValueError as error:
            raise section.fail("file", str(error)) from None
        if provider not in config.providers:
            raise section.fail("provider", "must be a provider declared in [providers]")
        if provider == (entry.holder or holder):
            raise section.fail("provider", "must not be the data provider, whose copy is the source of every range")
        parsed.append(RangeRow(path, provider, _ranges(section), entry.label))
    return parsed


def build_map(rows: list[RangeRow], file: Path) -> RangeMap:
    """Join the rows into a map; a file and provider named twice is refused, since the later row would win silently."""
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        if (row.space, row.file, row.provider) in seen:
            raise ConfigError(f"{file}: [placement]: the map names one file on one provider more than once")
        seen.add((row.space, row.file, row.provider))
    return RangeMap(tuple(rows))


def _map_rows(table: Section, file: Path, config: PlatformConfig, holder: str) -> list[RangeRow]:
    """Read the rows of `map`: the rows themselves, or the path of a study's file that holds them."""
    raw = table.data.get("map", [])
    if isinstance(raw, str):
        study_file = (file.parent / raw).resolve()
        study = Section(load_toml(study_file), study_file, "the map file")
        study.reject_unknown(("map",))
        return parse_rows(study.data.get("map", []), study_file, "[[map]]", config, holder)
    if isinstance(raw, list):
        return parse_rows(raw, file, "[[placement.map]]", config, holder)
    raise table.fail("map", "must be the path of a file, or the rows themselves as [[placement.map]]")


def load_placement(path: str | os.PathLike[str], config: PlatformConfig) -> Placement:
    """Read `[placement]` of a platform.toml (and the study's map file it names) against its loaded configuration.

    Raises ConfigError naming the file and the key; the table is required, because placement acts on a data provider
    that nothing else declares.
    """
    file = Path(path).resolve()
    root = Section(load_toml(file), file, "platform.toml")
    table = root.section("placement")
    table.reject_unknown(
        (
            "holder",
            "map",
            "poll_s",
            "transfer_timeout_s",
            "settle_timeout_s",
            "consecutive_polls",
            "request_timeout_s",
            "chunk_bytes",
            "fresh_connection_bytes",
            "probe_attempts",
            "probe_backoff_s",
            "transfer_settle_s",
            "transfer_settle_polls",
            "clock_skew_allowance_s",
            "concurrency",
            "qos_scan",
            "qos_fulfil_timeout_s",
        )
    )
    holder = table.string("holder")
    if holder not in config.providers:
        raise table.fail("holder", "must be a provider declared in [providers]")
    qos_scan = table.optional_string("qos_scan") or DEFAULT_QOS_SCAN
    if qos_scan not in QOS_SCANS:
        raise table.fail("qos_scan", f"must be one of {', '.join(QOS_SCANS)}")
    settings = PlacementSettings(
        holder=holder,
        poll_s=_number(table, "poll_s", 2.0, minimum=0.001),
        transfer_timeout_s=_number(table, "transfer_timeout_s", 3600.0, minimum=0),
        settle_timeout_s=_number(table, "settle_timeout_s", 300.0, minimum=0),
        consecutive_polls=_number(table, "consecutive_polls", 2, minimum=2, integer=True),
        request_timeout_s=_number(table, "request_timeout_s", 900.0, minimum=1),
        chunk_bytes=_number(table, "chunk_bytes", DEFAULT_CHUNK_BYTES, minimum=1, integer=True),
        fresh_connection_bytes=_number(
            table, "fresh_connection_bytes", DEFAULT_FRESH_CONNECTION_BYTES, minimum=1, integer=True
        ),
        probe_attempts=_number(table, "probe_attempts", 3, minimum=1, integer=True),
        probe_backoff_s=_number(table, "probe_backoff_s", 1.0, minimum=0),
        transfer_settle_s=_number(table, "transfer_settle_s", 30.0, minimum=0),
        transfer_settle_polls=_number(table, "transfer_settle_polls", 4, minimum=2, integer=True),
        clock_skew_allowance_s=_number(table, "clock_skew_allowance_s", 5.0, minimum=0),
        concurrency=_number(table, "concurrency", 16, minimum=1, integer=True),
        qos_scan=qos_scan,
        qos_fulfil_timeout_s=_number(table, "qos_fulfil_timeout_s", 120.0, minimum=0),
    )
    rows = _map_rows(table, file, config, holder)
    return Placement(settings, build_map(rows, file))


class TracingFile(io.RawIOBase):
    """A read-only file object that records every `(offset, length)` actually read."""

    def __init__(self, path: Path | str) -> None:
        super().__init__()
        self._file = open(path, "rb")  # noqa: SIM115 - closed by close()
        self.reads: list[Range] = []

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._file.seek(offset, whence)

    def tell(self) -> int:
        return self._file.tell()

    def readinto(self, buffer: Any) -> int:
        position = self._file.tell()
        count = self._file.readinto(buffer)
        if count:
            self.reads.append((position, count))
        return count

    def close(self) -> None:
        self._file.close()
        super().close()


def trace_reads(path: Path | str, reader: Callable[[TracingFile], object]) -> list[Range]:
    """Run `reader` on a traced copy of a local file and return the merged ranges it read.

    The study passes its own reading code, so the map holds exactly the bytes that code needs and nothing is computed
    from the file's internals that could drift from the reader.
    """
    traced = TracingFile(path)
    try:
        reader(traced)
        return merged(traced.reads)
    finally:
        traced.close()
