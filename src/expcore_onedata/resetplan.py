# Provenance: new
"""A reset planner's reading of a space: one recursive listing for the file list and the QoS scan, and each file's
holdings at every provider that can hold the space's bytes (O33, O46).

A study's planner names in the reset's eviction plan only the files a mirror holds, since an eviction job per id it
named would flood the transfer listing, which never shrinks (O32). To know them it read every file's block map at each
mirror after a tree walk of its own, and the reset's QoS scan then listed the space again: for the large space of the
first study's live reset, 30,680 block maps (106 s) and 96 listing pages (103 s). `plan_scan` makes both with one
listing:

- **The listing:** the recursive listing of the space root on every provider the reset reads (`resetqos.by_listing`,
  the QoS scan the reset itself makes, each provider's listing read at once), with each regular file's path.
- **The holdings:** each regular file's block map read at each provider that can hold the space's bytes, `[placement]
  concurrency` at a time, within `occupancy.READS_MAX` and `occupancy.READ_BOUND_S` (`occupancy.read_files`, the
  occupancy stage's own reads). Those are every provider of the space but the data provider and the ones the caller
  declares never hold the space's bytes (`never_holds`, as `SpaceReset.run` takes it). The space root's counter never
  skips one: it under-counts (a placement read 0 for 9.534 s, O46).

`Holdings` gives each file's bytes at each provider read, by file id, the files' paths, and `plan()`, the eviction plan
of the held files; the caller picks what to evict. **Given to the reset** (`SpaceReset.run(planned=...)`, or
`ResetPlan.planned`), its scan is the qos stage's first scan, so the space is listed once: the reset refuses one of
another space or of other providers, before any stage, makes the second scan (after a removal) itself, and records
`scan: "given"` with the scan's age (`scan_age_s`). A requirement added between the planner's scan and the reset is
missed, as one added just after the reset's own first scan would be; the planner reads right before the reset, between
runs, so the window is the planner's own reading.
"""

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from expcore_onedata import occupancy, resetqos

if TYPE_CHECKING:  # the reset imports this module's type; only its interface is used here
    from expcore_onedata.reset import SpaceReset


@dataclass(frozen=True)
class Holdings:
    """What a planner read: the QoS scan (with each regular file's path), each file's bytes at each provider read
    (provider to file id to bytes), the provisioner's clock when the listing began, and the seconds it all took."""

    scan: resetqos.Found
    held: dict[str, dict[str, int]]
    read_at: float
    seconds: float

    def held_paths(self, provider: str) -> list[str]:
        """The paths of the files a provider holds any bytes of, sorted; none for a provider not read."""
        return sorted(self.scan.paths[file_id] for file_id, n in self.held.get(provider, {}).items() if n)

    def plan(self) -> dict[str, list[str]]:
        """The eviction plan of the held files: each provider read that holds any, to their paths."""
        return {provider: paths for provider in self.held if (paths := self.held_paths(provider))}

    def facts(self, label: Callable[[str], str]) -> dict[str, Any]:
        """The figures, every provider by its position: the files listed, per provider read the files read, the files
        held and the bytes, the reads of the scan, and the seconds."""
        per = {
            label(q): {
                "files_read": len(held),
                "held_files": sum(1 for n in held.values() if n),
                "held_bytes": sum(held.values()),
            }
            for q, held in self.held.items()
        }
        return {
            "files": len(self.scan.paths),
            "providers": per,
            "reads": dict(self.scan.reads),
            "seconds": self.seconds,
        }


def plan_scan(reset: "SpaceReset", *, never_holds: Collection[str] = ()) -> Holdings:
    """List the space once on every provider the reset reads, as its QoS scan does, and read each regular file's
    block map at each provider that can hold the space's bytes: every provider of the space but the data provider and
    those in `never_holds`. Refuse a `never_holds` that names the data provider or a provider that does not support the
    space, before anything is sent."""
    p, providers = reset.provisioner, reset.providers
    if isinstance(never_holds, str) or not all(isinstance(q, str) for q in never_holds):
        raise ValueError("never_holds is a collection of provider names")
    for provider in never_holds:
        p.check_eviction(provider)
    p.verify_space()
    began = p.clock()
    found = resetqos.by_listing(p, providers)
    every = frozenset().union(*found.regular.values())
    can_hold = [q for q in p.space.support.providers if q != p.settings.holder and q not in never_holds]
    wanted = {q: sorted(found.regular.get(q, every)) for q in can_hold}
    held = occupancy.read_files(p, wanted, p.client.provider_label)
    return Holdings(found, held, began, round(p.clock() - began, 3))


def given_scan(reset: "SpaceReset", holdings: Holdings) -> resetqos.Found:
    """The scan of a planner's reading, refused unless it is of the reset's space, by the listing, over the providers
    the reset reads."""
    scan, p = holdings.scan, reset.provisioner
    if scan.space != p.space_id or "listings" not in scan.reads:  # the space's Onezone id, never its position
        raise ValueError("a planner's reading given to a reset is of its own space, made by the recursive listing")
    # the scan's tables are keyed by provider names: each name must stand for the same provider, by its Onezone id
    if set(scan.providers) != {(provider, p.provider_id(provider)) for provider in reset.providers}:
        raise ValueError("a planner's reading given to a reset lists the providers the reset reads, no others")
    return scan


__all__ = ["Holdings", "given_scan", "plan_scan"]
