# Provenance: new
"""When the reset's quiet check runs, and the files it covers beyond a removed requirement's (O33, #209).

A removed requirement names the files a QoS replication may still be placing, but an agent that may manage QoS can add
a requirement and remove it before the run ends. Its replication creates no transfer document and no requirement is
left, so at reset time nothing shows that one is landing, and it can land after the eviction. So the quiet check does
not wait for a requirement to be found. **It runs, and why is recorded**, in this order:

- `requirements`: the reset removed a requirement of the run's own;
- `rights`: the caller says the run's condition declares the right to manage QoS (`qos_rights=True`, from
  `config.Condition.manages_qos`);
- `default`: the caller did not say (`qos_rights=None`), which fails safe.

It is skipped only for a caller that says the run had no QoS right (`qos_rights=False`) when the reset found no
requirement. The reason is judged at reset time, and the record gives it with the quiet stage's seconds.

**What it covers.** The occupancy is read again after the quiet wait, as before, and so are the block maps of the
files a removed requirement applied to; now also those of every regular file a provider other than the data provider
held when the run ended, read before the eviction (`held_at_end`, one block-map read per file of the space on each
such provider, the tree listed once), since the eviction empties them and the maps after it show a replication that
puts them back. A file that was not yet held shows only in the occupancy.

**How long it waits.** With a removed requirement the files a replication may reach are known, so the wait is twice the
occupancy's lag for the deepest of those and of the held ones (`resetqos.quiet_wait_s`). With none to name them, a run
with QoS rights, or one that did not say, waits for the deepest regular file of the space: a late replication could
reach any. The wait is 20 s for files directly under the root, 30 s one directory down, and 10 s more for each
further level.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from expcore_onedata import resetqos
from expcore_onedata.placement import held_bytes
from expcore_onedata.provision import Provisioner

REQUIREMENTS, RIGHTS, DEFAULT = "requirements", "rights", "default"  # the reasons the quiet check runs, in order


def quiet_reason(removed: int, qos_rights: bool | None) -> str | None:
    """Why the quiet check runs, or None when it is skipped: `removed` requirements were removed at the reset, and the
    caller said whether the run's condition declares the right to manage QoS (`None`: it did not say)."""
    if removed:
        return REQUIREMENTS
    if qos_rights is None:
        return DEFAULT
    return RIGHTS if qos_rights else None


@dataclass(frozen=True)
class Held:
    """The regular files a provider other than the data provider held when the run ended, each with its depth (the
    parts of its path below the root), and the depth of the deepest regular file of the space."""

    files: Mapping[str, int]
    deepest: int


def held_at_end(p: Provisioner, providers: Sequence[str]) -> Held:
    """Read, on each provider other than the data provider, the block map of every regular file of the space and keep
    the files it holds bytes of. The listing is read once per provider. The caller makes sure no transfer runs before
    it reads any map (`SpaceReset._require_quiet`)."""
    held: dict[str, int] = {}
    deepest = 0
    for provider in (q for q in providers if q != p.settings.holder):
        _, leaves = resetqos.tree(p, provider)
        files = {file_id: depth for file_id, kind, depth in leaves if kind != "DIR"}
        deepest = max([deepest, *files.values()])
        maps = resetqos.read_maps(p, files, [provider])
        held.update({file_id: files[file_id] for (file_id, _), blocks in maps.items() if held_bytes(blocks)})
    return Held(held, deepest)


def quiet_depth(affected: Mapping[str, int], held: Held, qos_rights: bool | None) -> int:
    """The depth the quiet wait is for: the deepest affected or held file, and, unless the caller said the run had no
    QoS right, the deepest file of the space, which a replication nothing names could still reach."""
    depths = [*affected.values(), *held.files.values(), *([held.deepest] if qos_rights is not False else [])]
    return max(depths, default=1)


__all__ = ["DEFAULT", "REQUIREMENTS", "RIGHTS", "Held", "held_at_end", "quiet_depth", "quiet_reason"]
