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
chunk_bytes = 67108864          # the largest single read or write
probe_attempts = 3              # tries at a read that fails with a network reset (O08)
probe_backoff_s = 1.0

[[placement.map]]               # a row (inline instead of `map = ...`); top-level [[map]] in the study's file
file = "dir/file.bin"           # relative to the space root
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expcore.config.loading import ConfigError, Section, load_toml
from expcore_onedata.config import PlatformConfig
from expcore_onedata.placement import Range, merged

PART = re.compile(r"[^/\x00-\x1f\\]{1,255}")


@dataclass(frozen=True)
class PlacementSettings:
    """The bounds and the data provider of placement."""

    holder: str
    poll_s: float = 2.0
    transfer_timeout_s: float = 3600.0
    settle_timeout_s: float = 300.0
    consecutive_polls: int = 2
    request_timeout_s: float = 900.0
    chunk_bytes: int = 64 << 20
    probe_attempts: int = 3
    probe_backoff_s: float = 1.0


@dataclass(frozen=True)
class RangeRow:
    """One file on one receiving provider: the merged ranges that must be there."""

    file: str
    provider: str
    ranges: tuple[Range, ...]


@dataclass(frozen=True)
class RangeMap:
    """The declared map: at most one row per file and provider."""

    rows: tuple[RangeRow, ...] = field(default_factory=tuple)

    def for_provider(self, provider: str) -> tuple[RangeRow, ...]:
        """Return the rows whose receiving provider is the one named."""
        return tuple(row for row in self.rows if row.provider == provider)

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
    """Validate the rows of a map; the receiving provider must be declared and must not be the data provider."""
    if not isinstance(rows, list):
        raise ConfigError(f"{file}: {where}: must be an array of tables")
    parsed: list[RangeRow] = []
    for number, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise ConfigError(f"{file}: {where}: row {number} must be a table")
        section = Section(row, file, f"{where} row {number}")
        section.reject_unknown(("file", "provider", "ranges"))
        path, provider = section.string("file"), section.string("provider")
        try:
            safe_path(path)
        except ValueError as error:
            raise section.fail("file", str(error)) from None
        if provider not in config.providers:
            raise section.fail("provider", "must be a provider declared in [providers]")
        if provider == holder:
            raise section.fail("provider", "must not be the data provider, whose copy is the source of every range")
        parsed.append(RangeRow(path, provider, _ranges(section)))
    return parsed


def build_map(rows: list[RangeRow], file: Path) -> RangeMap:
    """Join the rows into a map; a file and provider named twice is refused, since the later row would win silently."""
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if (row.file, row.provider) in seen:
            raise ConfigError(f"{file}: [placement]: the map names one file on one provider more than once")
        seen.add((row.file, row.provider))
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
            "probe_attempts",
            "probe_backoff_s",
        )
    )
    holder = table.string("holder")
    if holder not in config.providers:
        raise table.fail("holder", "must be a provider declared in [providers]")
    settings = PlacementSettings(
        holder=holder,
        poll_s=_number(table, "poll_s", 2.0, minimum=0.001),
        transfer_timeout_s=_number(table, "transfer_timeout_s", 3600.0, minimum=0),
        settle_timeout_s=_number(table, "settle_timeout_s", 300.0, minimum=0),
        consecutive_polls=_number(table, "consecutive_polls", 2, minimum=2, integer=True),
        request_timeout_s=_number(table, "request_timeout_s", 900.0, minimum=1),
        chunk_bytes=_number(table, "chunk_bytes", 64 << 20, minimum=1, integer=True),
        probe_attempts=_number(table, "probe_attempts", 3, minimum=1, integer=True),
        probe_backoff_s=_number(table, "probe_backoff_s", 1.0, minimum=0),
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
