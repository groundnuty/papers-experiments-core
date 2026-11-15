# Provenance: new
"""Which entries of the link check's probe space are the check's own, and which probe is a pair's current one.

A study's Day-0 or reset check of the probe space has to leave out exactly the files the link check writes (`linkprobe`)
and nothing else, and has to tell a pair's current probe from the leftovers the check has not yet purged. This module
reads that from a listing's names and file ids alone, with no provider id and no call to the platform.

**The two name shapes**, matched whole, in lower-case hex and with no other text before, after or between:

- a probe, `expcore-link-probe-<16 hex>`, and `-g<N>` after it for generation N from 1, with no leading zero and no
  upper bound, since `probe_name` has none and the check writes whatever generation comes next (no `-g0`). The 16 hex
  digits, the SHA-256 of the pair's key, are the pair's **stem**;
- a retirement marker, `expcore-link-retired-<16 hex>-<16 hex>`: the pair's stem, then the SHA-256 of the retired file
  id.

Any other name is `other`, near misses (a wrong number of digits, upper case, `-g0`, a suffix) included.

**The current probe of a pair is the highest generation among its files not retired**, a file being retired when a
marker of its stem holds the digest of its file id (the id, never the name: a name written again is a new file). Every
other probe of the pair, a lower generation or a retired one, is superseded, and a pair whose every probe is retired
has no current one. This is the choice `linkprobe.ProbeFiles.provision` makes, and it reads a generation as
`linkprobe.generation_of` does, which stops at nine digits. A probe named with ten or more (the writer has no bound, but
a billion failed fetches of one pair is out of reach) is the check's own file and never `current`, since `provision`
does not look at it, and no `purge` deletes it; it is `superseded`, with every other probe that is not current.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import NamedTuple

from expcore_onedata.linkprobe import _GENERATION, PROBE_PREFIX, RETIRED_PREFIX, _digest
from expcore_onedata.provision import Entry

_STEM = r"(?P<stem>[0-9a-f]{16})"
_PROBE = re.compile(rf"{re.escape(PROBE_PREFIX)}{_STEM}(?P<suffix>-g[1-9][0-9]*)?")
_MARKER = re.compile(rf"{re.escape(RETIRED_PREFIX)}{_STEM}-(?P<file>[0-9a-f]{{16}})")


class ListedFile(NamedTuple):
    """An entry of a listing: its name and its file id."""

    name: str
    file_id: str


class CurrentProbe(NamedTuple):
    """The current probe of one pair: the pair's stem, the probe's name and file id, and its generation."""

    stem: str
    name: str
    file_id: str
    generation: int


@dataclass(frozen=True)
class LinkCheckEntries:
    """A probe space's entries sorted by what they are to the link check, each tuple in the listing's order.

    `own` is every entry of the two name shapes, and is `current`'s files, `superseded` and `markers` together.
    `current` holds one probe per pair that has one, ordered by stem. `superseded` holds the probes of lower or retired
    generations, `markers` the retirement markers (whether or not their file is still listed), and `other` every entry
    that is none of these, for the study to judge."""

    own: tuple[ListedFile, ...]
    current: tuple[CurrentProbe, ...]
    superseded: tuple[ListedFile, ...]
    markers: tuple[ListedFile, ...]
    other: tuple[ListedFile, ...]


def is_link_check_entry(name: str) -> bool:
    """Whether a file name is one of the two shapes the link check writes: a probe or a retirement marker."""
    return _PROBE.fullmatch(name) is not None or _MARKER.fullmatch(name) is not None


def _readable_generation(suffix: str | None) -> int | None:
    """The generation a probe name's `-g<N>` suffix gives as `linkprobe.generation_of` reads it (no suffix is generation
    0), or None for one it does not read: the writer's names past nine digits. No integer is made of those."""
    if suffix is None:
        return 0
    found = _GENERATION.fullmatch(suffix)
    return int(found.group(1)) if found else None


def _listed(listing: Mapping[str, Entry] | Iterable[Entry | tuple[str, str]]) -> list[ListedFile]:
    """The listing's entries as name and file id, a pair given twice once. Refuse an item that is neither an `Entry` nor
    a `(name, file_id)` pair of strings, an entry with no file id, and a name given two file ids."""
    items = listing.values() if isinstance(listing, Mapping) else listing
    found: dict[str, ListedFile] = {}
    for item in items:
        if isinstance(item, Entry):
            entry = ListedFile(item.name, item.file_id)
        elif isinstance(item, tuple) and len(item) == 2 and all(isinstance(part, str) for part in item):
            entry = ListedFile(*item)
        else:
            raise TypeError(f"a listing entry is an Entry or a (name, file_id) pair of strings, not {item!r}")
        if not entry.file_id:
            raise ValueError(f"the listing entry {entry.name!r} has no file id")
        if found.setdefault(entry.name, entry) != entry:
            raise ValueError(f"the listing gives {entry.name!r} two file ids; a directory lists a name once")
    return list(found.values())


def link_check_entries(listing: Mapping[str, Entry] | Iterable[Entry | tuple[str, str]]) -> LinkCheckEntries:
    """Sort a probe space's listing into the link check's own entries and the others.

    `listing` is the probe space's entries as the core's listing calls return them: `Entry` objects (`provision.Entry`,
    whose `name` and `file_id` are read), as a list or as the dict of them by name that `Provisioner.children` returns,
    or `(name, file_id)` pairs. The entries are one directory's, so a name is listed once. No provider id is needed: the
    stem in a name groups a pair's probes and markers."""
    files = _listed(listing)
    probes: dict[str, list[tuple[int, ListedFile]]] = {}
    retired: dict[str, set[str]] = {}
    for file in files:
        if (probe := _PROBE.fullmatch(file.name)) is not None:
            if (generation := _readable_generation(probe["suffix"])) is not None:
                probes.setdefault(probe["stem"], []).append((generation, file))
        elif (mark := _MARKER.fullmatch(file.name)) is not None:
            retired.setdefault(mark["stem"], set()).add(mark["file"])
    current = []
    for stem, found in sorted(probes.items()):
        marked = retired.get(stem, set())
        usable = [(generation, file) for generation, file in found if _digest(file.file_id) not in marked]
        if usable:
            generation, file = max(usable, key=lambda candidate: candidate[0])
            current.append(CurrentProbe(stem, file.name, file.file_id, generation))
    chosen = {probe.name for probe in current}
    own = tuple(file for file in files if is_link_check_entry(file.name))
    return LinkCheckEntries(
        own=own,
        current=tuple(current),
        superseded=tuple(f for f in own if _PROBE.fullmatch(f.name) and f.name not in chosen),
        markers=tuple(f for f in own if _MARKER.fullmatch(f.name)),
        other=tuple(file for file in files if not is_link_check_entry(file.name)),
    )
