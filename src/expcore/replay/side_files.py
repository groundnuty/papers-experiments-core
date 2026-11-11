# Provenance: new
"""Side files: per-run files a study writes beside each record, each named by its SHA-256 in that record (#109, R14).

A study declares them in study.toml, and `study.config` validates the declaration when the study loads:

    [[study.side_files]]
    pattern = "extent_ranges.json.gz"               # one file name, looked for beside each record
    hash_field = "instruments.extent.side_sha256"   # the record field, a dotted path, that holds the file's SHA-256

A pattern is one literal file name, never a glob, because each side file is paired with the one record in its own
directory: the attempt directory, `<digest>/try<N>/`, which holds one record. The replay check finds every file of a
declared name anywhere in the batch it parses, by the hashed-only patterns `patterns` gives (the name itself and
`*/<name>`), in the same walk that finds the records, so a side file is hashed into the check's inputs and never
replayed. `pair` then refuses, each as a failure of the check at stage `side_file`:

- named but missing: a record whose field holds a hash with no such file beside it;
- changed or swapped: a side file whose bytes do not have the hash its own record names;
- an orphan: a side file in a directory that holds no record, left from an earlier campaign or attempt;
- an absent field: a side file beside a record that names no hash for it (the field is absent or null);
- a malformed hash: a field that holds anything but 64 lower-case hexadecimal digits;
- an ambiguous pairing: a hash or a side file in a directory that holds more than one record.

A record that names no hash and has no such file beside it is fine: that run made no side file.

A scorer reads a side file's content only as the loader hands it over (#144): the bytes `read_side_file` read once,
whose SHA-256 the loader compared with the hash the record names, by declared pattern (`beside`), never a path. The
loader holds each side file whole in memory, so one larger than `MAX_SIDE_FILE_BYTES` is refused: the reader stops one
byte past the cap, and the check fails such a file at stage `side_file` too, so a check never passes a batch whose
analysis would refuse it.
"""

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

STAGE = "side_file"  # the stage of a check failure that a side file causes
SHA256 = re.compile(r"[0-9a-f]{64}")
# The most a side file may hold, 64 MiB: the loader reads each whole into memory to hand it to the scorers. It is read
# where it is used, never bound as a default, so the one value holds for the check, the analysis and re-scoring.
MAX_SIDE_FILE_BYTES = 64 * 1024 * 1024


class SideFileTooLarge(ValueError):
    """A side file holds more than MAX_SIDE_FILE_BYTES; the message says so and names the cap."""


@dataclass(frozen=True)
class SideFile:
    """One declared side file: its file name beside each record, and the record field that holds its SHA-256."""

    pattern: str
    hash_field: str

    def as_json(self) -> dict[str, str]:
        """The declaration as the replay check and PACKAGE.json record it."""
        return {"pattern": self.pattern, "hash_field": self.hash_field}


def declared(side_files: Sequence[SideFile]) -> list[dict[str, str]]:
    """The declarations as the replay check and PACKAGE.json record them, in the order study.toml gives."""
    return [side.as_json() for side in side_files]


def patterns(side_files: Sequence[SideFile]) -> list[str]:
    """The hashed-only patterns that find each declared name at the top of a batch and in any directory below it.

    `fnmatch` lets `*` cross a `/`, so `*/<name>` matches the name in every subdirectory and no longer name that only
    ends with it; a declared name holds no glob character (`study.config` refuses one).
    """
    return [found for side in side_files for found in (side.pattern, f"*/{side.pattern}")]


def is_side_file(rel: str, side_files: Sequence[SideFile]) -> bool:
    """Tell whether a path of the batch has a declared side file's name."""
    return PurePosixPath(rel).name in {side.pattern for side in side_files}


def too_large() -> str:
    """Why a side file over the cap is refused, naming the cap in force."""
    return (
        f"holds more than {MAX_SIDE_FILE_BYTES} bytes, the most a side file may hold (MAX_SIDE_FILE_BYTES): the "
        "analysis reads each side file whole into memory to hand it to the scorers"
    )


def read_side_file(path: str | os.PathLike[str]) -> bytes:
    """Read a side file once, whole, and return its bytes; raise SideFileTooLarge, having read at most one byte past
    the cap, for one that holds more than MAX_SIDE_FILE_BYTES."""
    limit = MAX_SIDE_FILE_BYTES
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise SideFileTooLarge(too_large())
    return data


def beside(
    records: Iterable[str], contents: Mapping[str, bytes], side_files: Sequence[SideFile]
) -> dict[str, dict[str, bytes]]:
    """The side files of each record, by declared pattern: the bytes in `contents` of each file of a declared name in
    the record's directory, an empty mapping for a record with none.

    `contents` maps each side file, by path, to the bytes the loader read; call this only once `pair` found nothing
    to refuse, so each of them has the hash the record beside it names.
    """
    held: dict[str, dict[str, bytes]] = {}
    for rel in records:
        directory = PurePosixPath(rel).parent
        found = {d.pattern: (directory / d.pattern).as_posix() for d in side_files}
        held[rel] = {pattern: contents[path] for pattern, path in found.items() if path in contents}
    return held


def named_hash(data: Mapping[str, Any], field: str) -> Any:
    """The value at a dotted path of a record's stored JSON; None when a part of the path is absent."""
    value: Any = data
    for part in field.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def pair(
    records: Mapping[str, Mapping[str, Any] | None],
    side: Mapping[str, str],
    side_files: Sequence[SideFile],
) -> list[tuple[str, str]]:
    """Pair every side file with the record beside it; return (path, problem) for each refusal, empty when all pair.

    `records` maps each record of the batch, by path, to its stored JSON, or to None when the record failed the check
    on its own (it is refused already, and a side file beside it is not an orphan). `side` maps each file of a
    declared name, by path, to the SHA-256 of its bytes. A problem of a record is reported against the record, an
    orphan against the side file.
    """
    sharing: dict[str, list[str]] = {}
    for rel in records:
        sharing.setdefault(PurePosixPath(rel).parent.as_posix(), []).append(rel)
    problems: list[tuple[str, str]] = []
    claimed: set[str] = set()
    for rel, data in sorted(records.items()):
        directory = PurePosixPath(rel).parent
        for declaration in side_files:
            beside = (directory / declaration.pattern).as_posix()
            if beside in side:
                claimed.add(beside)
            if data is not None:
                problem = _problem(data, declaration, side.get(beside), len(sharing[directory.as_posix()]))
                if problem is not None:
                    problems.append((rel, problem))
    problems += [
        (rel, "no record lies beside it to name it: a side file left from an earlier campaign or attempt; remove it")
        for rel in sorted(set(side) - claimed)
    ]
    return problems


def _problem(data: Mapping[str, Any], declaration: SideFile, digest: str | None, records_here: int) -> str | None:
    """What is wrong between one record and the side file of one declaration beside it, or None."""
    name, field = declaration.pattern, declaration.hash_field
    named = named_hash(data, field)
    if named is None and digest is None:
        return None  # the run made no side file
    if records_here > 1:
        return f"{records_here} records share its directory, so {name} cannot be paired with one of them"
    if named is None:
        return f"{name} lies beside the record, which names no hash for it at {field}"
    if not isinstance(named, str) or not SHA256.fullmatch(named):
        return f"{field} does not hold a SHA-256 of {name} (64 lower-case hexadecimal digits)"
    if digest is None:
        return f"the record names {name} by its hash at {field}, but no such file lies beside it"
    if digest != named:
        return (
            f"{name} beside the record does not have the SHA-256 the record names at {field}: it was changed after "
            "the run, or swapped with another record's"
        )
    return None
