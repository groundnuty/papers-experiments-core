"""The per-run reset: every owner keeps its spaces' data, and every mirror is emptied of its replicas, verified on the
block maps.

    uv run python deploy/compose-federation/federation.py [--layout PATH] reset --replicas

`reset --clean` brings the deployment back to its baseline and removes whatever a run ingested; this reset removes no
file. For every space it lists each regular file through the owner and reads the block maps, and it refuses, evicting
nothing in any space, unless every owner holds each of its files whole: a file only a mirror holds is an original, not
a replica. It then schedules one eviction per mirror holding anything, of the space's root directory, on that mirror,
and waits for it to end. The result is accepted only from the block maps: every mirror holding none of each file, and
the owner still holding each whole, on two sweeps in a row. A transfer's own state is never the evidence (Onedata 25.0
reports `completed` for an eviction that removed nothing), and the verdict on a block map is the platform layer's own
(`expcore_onedata.placement`), so an answer that does not list a provider is unknown, never empty.

`evict_replicas(boot, timeout_s)` is the narrow interface a study's per-run reset shares with this command. The platform
layer's per-run reset (#118: many files evicted in one call and verified together on the block maps, one listing per
operation) is to replace the directory eviction and the sweep below behind that signature, by calling the layer's
eviction (the successor of `Provisioner.evict` and `require_empty`) instead of the providers' REST interfaces directly.
QoS requirements and running transfers, which #118 also resets, are not touched here. The layout's `probe_space` is
left alone: each link probe in it is held by the provider that wrote it, its owner or not, and the link check evicts
the copies it fetched itself (#221).
"""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from fedboot import Bootstrap
from fedcontent import Content
from fedrest import RestError
from fedspec import Service, Space

from expcore_onedata.placement import Range, file_fully_placed, held_bytes, provider_blocks

WORKERS = 16
CONSECUTIVE = 2  # clean sweeps in a row before a space counts as reset
POLL_S = 2.0
STATE_FIELDS = ("transferStatus", "evictionStatus", "replicationStatus")
ENDED = {"completed", "skipped"}
FAILED = {"failed", "cancelled", "aborted"}


@dataclass(frozen=True)
class File:
    file_id: str
    path: str
    size: int


@dataclass
class Sweep:
    """One reading of every file's block maps: files the owner does not hold whole, and what each mirror holds."""

    owner_short: int = 0
    held: dict[str, int] = field(default_factory=dict)  # bytes, by mirror
    unknown: dict[str, int] = field(default_factory=dict)  # readings that were not answers, by mirror

    @property
    def clean(self) -> bool:
        return not self.owner_short and not any(self.held.values()) and not any(self.unknown.values())

    def describe(self) -> str:
        held = ", ".join(f"{name} {held} B" for name, held in self.held.items()) or "no mirror"
        unknown = sum(self.unknown.values())
        return f"mirrors hold {held}; {unknown} reading(s) unknown; {self.owner_short} file(s) not whole at the owner"


@dataclass(frozen=True)
class Evicted:
    """What the per-run reset did to one space."""

    files: int
    evicted_from: tuple[str, ...]
    held_before: dict[str, int]
    sweeps: int
    seconds: float

    def __str__(self) -> str:
        before = ", ".join(f"{name} {held} B" for name, held in self.held_before.items()) or "nothing (no mirror)"
        evicted = f"evicted from {', '.join(self.evicted_from)}" if self.evicted_from else "nothing to evict"
        return (
            f"{self.files} file(s); mirrors held {before}; {evicted}; "
            f"verified empty on {self.sweeps} sweep(s), {self.seconds:.0f} s"
        )


class Replicas:
    """Evict what the mirrors hold of each space, and prove it on the block maps."""

    def __init__(self, boot: Bootstrap) -> None:
        self.boot = boot
        self.spec = boot.layout.spec
        self.content = Content(boot)

    def files(self, owner: Service, space: str) -> list[File]:
        """Every regular file of the space, walked through its owner."""
        pending, found = [("", self.content.root(owner, space))], []
        while pending:
            prefix, directory = pending.pop()
            for entry in self.content.children(owner, directory):
                path = prefix + entry["name"]
                if entry["type"] == "DIR":
                    pending.append((path + "/", entry["file_id"]))
                elif entry["type"] == "REG":
                    found.append(File(entry["file_id"], path, int(entry.get("size") or 0)))
        return found

    def blocks(self, reader: Service, file_id: str, holder: str) -> list[Range] | None:
        """The blocks `holder` has of a file, as `reader` answers; None when that is unknown."""
        try:
            answer = self.boot.provider(reader, "GET", f"/data/{file_id}/distribution", "distribution")
            return provider_blocks(answer, self.boot.ids.providers[holder])
        except (RestError, ValueError):
            return None

    def sweep(self, files: list[File], owner: Service, mirrors: tuple[Service, ...]) -> Sweep:
        """Read every file's block map: the owner's own, and each mirror's own."""

        def read(file: File) -> tuple[File, list[Range] | None, dict[str, list[Range] | None]]:
            mine = {mirror.name: self.blocks(mirror, file.file_id, mirror.name) for mirror in mirrors}
            return file, self.blocks(owner, file.file_id, owner.name), mine

        with ThreadPoolExecutor(WORKERS) as pool:
            readings = list(pool.map(read, files))
        sweep = Sweep(held={m.name: 0 for m in mirrors}, unknown={m.name: 0 for m in mirrors})
        for file, whole, mine in readings:
            if whole is None or not file_fully_placed(whole, file.size):
                sweep.owner_short += 1
            for name, blocks in mine.items():
                if blocks is None:
                    sweep.unknown[name] += 1
                else:
                    sweep.held[name] += held_bytes(blocks)
        return sweep

    def evict(self, mirror: Service, space: str, directory: str, deadline: float) -> None:
        """Evict a directory, recursively, from a mirror, and wait for the transfer to end."""
        body = {
            "type": "eviction",
            "evictingProviderId": self.boot.ids.providers[mirror.name],
            "dataSourceType": "file",
            "fileId": directory,
        }
        tid = self.boot.provider(mirror, "POST", "/transfers", "eviction", json=body)["transferId"]
        while True:
            record = self.boot.provider(mirror, "GET", f"/transfers/{tid}", "transfer")
            state = next((record[f] for f in STATE_FIELDS if record.get(f)), None)
            if state in ENDED:
                return
            if state in FAILED:
                raise RuntimeError(f"the eviction of {space} from {mirror.name} ended {state}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"the eviction of {space} from {mirror.name} did not end in time")
            time.sleep(POLL_S)

    def reset_space(self, space: Space, files: list[File], before: Sweep, timeout_s: float) -> Evicted:
        """Evict from every mirror that holds anything of the space, then wait for clean sweeps in a row."""
        began = time.monotonic()
        deadline = began + timeout_s
        owner = self.spec.provider(space.owner)
        mirrors = tuple(self.spec.provider(name) for name in space.mirrors)
        targets = tuple(m for m in mirrors if before.held[m.name] or before.unknown[m.name])
        if targets:
            directory = self.content.root(owner, space.name)
            for mirror in targets:
                self.evict(mirror, space.name, directory, deadline)
        sweeps, in_row = 0, 0
        while True:
            now = self.sweep(files, owner, mirrors)
            sweeps += 1
            in_row = in_row + 1 if now.clean else 0
            if in_row >= CONSECUTIVE:
                seconds = time.monotonic() - began
                return Evicted(len(files), tuple(m.name for m in targets), before.held, sweeps, seconds)
            if time.monotonic() > deadline:
                raise RuntimeError(f"{space.name} was not verified empty at its mirrors in time: {now.describe()}")
            time.sleep(POLL_S)


def evict_replicas(boot: Bootstrap, timeout_s: float) -> dict[str, Evicted]:
    """The per-run reset: refuse unless every owner holds each of its files whole, then evict every mirror's replicas
    and verify them gone; return what was done, by space. No file is removed and no owner's block is evicted."""
    replicas = Replicas(boot)
    spec = replicas.spec
    spaces = [space for space in spec.spaces if space.name != spec.probe_space]  # its probes are each source's own
    found = {}
    for space in spaces:
        owner = spec.provider(space.owner)
        files = replicas.files(owner, space.name)
        before = replicas.sweep(files, owner, tuple(spec.provider(name) for name in space.mirrors))
        if before.owner_short:
            raise RuntimeError(
                f"{before.owner_short} file(s) of {space.name} are not held whole by its owner {owner.name}, so a "
                "mirror may hold the only copy; nothing was evicted in any space"
            )
        found[space.name] = (files, before)
    return {space.name: replicas.reset_space(space, *found[space.name], timeout_s=timeout_s) for space in spaces}
