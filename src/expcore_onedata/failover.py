# Provenance: new
"""Whether a mirror is asked before the owner of a space, when the owner is taken down (O98).

An owner outage takes down the provider that holds a space's data, for the whole run, and the space's mirrors are meant
to serve its reads meanwhile. They do so only when op-worker asks a mirror first. Its replica finder sorts the holders
of a file's blocks by provider id, bytewise, and the first holder takes every block it holds, without asking whether it
is connected; a disconnected error is thrown back and never retried on another holder. Provider ids are random at
registration, so a read fails over to a mirror only when some mirror's id sorts below the owner's, and otherwise every
read waits on the stopped owner. That order is a fact of one federation that the harness can read, so it is checked
and never assumed.

**What is in scope.** Only the spaces whose data provider (`holder`, `PlatformConfig.space_holders`) is one of the
providers a declared owner outage takes down, and that another provider supports. A federation with no declared owner
outage has nothing in scope: `orders` gives the empty tuple, and a caller then does nothing, writes nothing and says
nothing. A provider that holds no space, or one that only mirrors, is no owner, and its outage is not checked.

**What holds.** The supporters of a space, the owner and its mirrors, are ordered by id as bytes (UTF-8, no case
folding), as Erlang compares binaries. `SpaceOrder.mirror_first` holds when the first of them is a mirror, so with
several mirrors it holds when any one of them sorts below the owner. A supporter whose id is unknown, or two with one
id, gives no order: that is `FailoverError`, never a pass. Two ids are never equal on a platform the loader accepted
(`[providers]` refuses a repeated id), so there are no ties to break.

**Names (O12).** A space is named by its position, `spaces[i]`, and a provider by its position in the platform file,
`providers[i]`, in every message and in the record. No provider's name, no space's name or label and no id is ever
written: the order is told by labels alone.

**The record.** `record_text` is the content of `failover-order.json` in a federation's root, which `up` writes once
the order holds, only for the spaces in scope (`hpcfailover` in deploy/hpc-federation). The injector (`faults`) does not
read it: it checks again, from the platform's current ids, before it takes the owner down.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from expcore_onedata.config import PlatformConfig

FILE = "failover-order.json"  # in the federation's root; written only when a space is in scope and its order holds
VERSION = "expcore-failover-order/1"
FIRST_IS_OWNER = "the owner's provider id sorts before every mirror's, so op-worker would ask the stopped owner first"


class FailoverError(ValueError):
    """The order of a space's supporters cannot be told, or does not hold. The message holds labels only."""


class FailoverRefused(FailoverError):
    """The first supporter of at least one space in scope, by provider id, is its owner. `failed` are those spaces."""

    def __init__(self, failed: Sequence["SpaceOrder"]) -> None:
        self.failed = tuple(failed)
        super().__init__(f"failover order: {'; '.join(order.sentence() for order in self.failed)}")


@dataclass(frozen=True)
class SpaceOrder:
    """One space in scope: its position label, its owner's and its mirrors' provider labels, and every supporter's
    label in the order op-worker asks them (provider id, bytewise)."""

    space: str
    owner: str
    mirrors: tuple[str, ...]
    order: tuple[str, ...]

    @property
    def mirror_first(self) -> bool:
        """Whether a mirror is asked before the owner."""
        return self.order[0] != self.owner

    def sentence(self) -> str:
        """Why the order fails for this space, or the order as it is; labels only."""
        mirrors = ", ".join(self.mirrors)
        if self.mirror_first:
            return f"{self.space}: {self.order[0]} is asked first (owner {self.owner}; mirrors {mirrors})"
        return f"{self.space}: {FIRST_IS_OWNER} (owner {self.owner}; mirrors {mirrors})"

    def record(self) -> dict[str, Any]:
        return {
            "space": self.space,
            "owner": self.owner,
            "mirrors": list(self.mirrors),
            "order": list(self.order),
            "mirror_first": self.mirror_first,
        }


def ordered(ids: dict[str, str | None]) -> tuple[str, ...]:
    """The labels of `ids` (label to provider id) in the order op-worker asks them: the ids as UTF-8 bytes, ascending.
    FailoverError for a missing id or one given twice: that is no order, and none is guessed."""
    missing = [label for label, found in ids.items() if not isinstance(found, str) or not found]
    if missing:
        raise FailoverError(
            f"failover order: the provider id of {', '.join(missing)} is not known, so no order is told"
        )
    keys = {label: found.encode("utf-8") for label, found in ids.items() if found}
    if len(set(keys.values())) != len(keys):
        raise FailoverError("failover order: two supporters of a space have one provider id, so no order is told")
    return tuple(sorted(keys, key=keys.__getitem__))


def orders(config: PlatformConfig, owners: Iterable[str]) -> tuple[SpaceOrder, ...]:
    """The order of every space in scope, in the platform's order of spaces: a space whose data provider is one of
    `owners` (provider names of this platform, the providers an owner outage takes down) and that another provider
    supports. Empty when `owners` is empty or none of them holds a space with a mirror. FailoverError when an owner is
    no provider of the platform, or when a supporter's id is not known."""
    taken = tuple(owners)
    if not taken:
        return ()
    unknown = [name for name in taken if name not in config.providers]
    if unknown:
        raise FailoverError(f"failover order: {len(unknown)} outage target(s) are no provider of the platform")
    targets = {config.provider_label(name) for name in taken}
    ids = {config.provider_label(name): provider.id for name, provider in config.providers.items()}
    holders = config.space_holders()
    found = []
    for entry in config.spaces:
        owner = holders.get(entry.space.name)
        if owner not in targets:
            continue
        mirrors = tuple(label for label in map(config.provider_label, entry.support.providers) if label != owner)
        if mirrors:
            found.append(
                SpaceOrder(entry.position, owner, mirrors, ordered({label: ids[label] for label in (owner, *mirrors)}))
            )
    return tuple(found)


def admit(config: PlatformConfig, owners: Iterable[str]) -> tuple[SpaceOrder, ...]:
    """`orders`, refused unless a mirror is asked first in every space in scope: the spaces in scope with their order,
    which is empty when none is. FailoverRefused names every space that fails, FailoverError one that has no order."""
    found = orders(config, owners)
    failed = [order for order in found if not order.mirror_first]
    if failed:
        raise FailoverRefused(failed)
    return found


def record_text(found: Sequence[SpaceOrder]) -> str:
    """The content of `failover-order.json`: sorted keys, an indent of 2 and a final newline."""
    document = {"version": VERSION, "spaces": [order.record() for order in found]}
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def read_record(text: str) -> dict[str, Any]:
    """The record as `record_text` wrote it, checked: its version, and each space's labels, order and `mirror_first`
    (which must say a mirror is first, as only such a record is ever written). FailoverError for anything else."""
    try:
        document = json.loads(text)
    except ValueError:
        raise FailoverError("failover order: the record is no JSON") from None
    spaces = document.get("spaces") if isinstance(document, dict) else None
    if not isinstance(document, dict) or document.get("version") != VERSION or not isinstance(spaces, list):
        raise FailoverError("failover order: the record is not of this version")
    for space in spaces:
        if not _well_formed(space):
            raise FailoverError("failover order: a space of the record is malformed")
    return document


def _well_formed(space: Any) -> bool:
    if not isinstance(space, dict) or set(space) != {"space", "owner", "mirrors", "order", "mirror_first"}:
        return False
    owner, mirrors, order = space["owner"], space["mirrors"], space["order"]
    if not isinstance(space["space"], str) or not isinstance(owner, str):
        return False
    if not all(isinstance(label, list) and all(isinstance(item, str) for item in label) for label in (mirrors, order)):
        return False
    return sorted(order) == sorted([owner, *mirrors]) and space["mirror_first"] is True and order[0] != owner
