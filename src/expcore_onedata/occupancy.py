# Provenance: new
"""Each provider's physical size of a space: the bytes its storage holds of the whole space (O01, O08).

`GET /data/{root}/distribution` of a space's root directory answers, per provider, the physical size of the space on
each of its storage backends (Oneprovider 25.0's REST description, `DataDirDistribution`: `physicalSize`, "actual
amount of storage consumed on a given storage backend by all regular files in the directory's subtree"). It also has
the logical and virtual sizes, which are never read here: they are directory statistics that lag behind the data.

The physical size is read only once no transfer runs on any provider (the caller makes sure, `reset.py`): read then,
it matches what the providers hold. Anything else is unknown and raises, never a zero: a provider the answer leaves
out or marks failed, a backend that failed (directory statistics turned off for the space answers so), or a size that
is not a whole number of bytes. A provider no declaration fits that holds bytes of the space is refused too.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from expcore_onedata.provision import Provisioner, ProvisionError
from expcore_onedata.transferrecord import Unreadable


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


def physical_sizes(provisioner: Provisioner, providers: Sequence[str]) -> dict[str, int]:
    """Return each declared provider's physical bytes of the provisioner's space, from one read of its root's
    distribution on the data provider; `providers` must all be in the answer. Raise Unreadable for anything unknown,
    and ProvisionError when an undeclared provider holds bytes."""
    root = provisioner.root()
    answer = provisioner.client.get(f"/data/{root}/distribution", provider=provisioner.settings.holder)
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
            continue
        sizes[name] = size
    missing = [provisioner.client.provider_label(p) for p in providers if p not in sizes]
    if missing:
        raise Unreadable(f"the space root's distribution leaves out {', '.join(missing)}: what it holds is unknown")
    return sizes


__all__ = ["physical_sizes"]
