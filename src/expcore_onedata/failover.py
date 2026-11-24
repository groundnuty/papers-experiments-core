# Provenance: new
"""Whether a failover holder is asked before the owner of a space, when the owner is taken down (O98).

An owner outage takes down the provider that holds a space's data, for the whole run, and the providers that hold the
space's replica are meant to serve its reads meanwhile. They do so only when op-worker asks one of them first. Its
replica finder sorts the holders of a file's blocks by provider id, bytewise, and the first holder takes every block it
holds, without asking whether it is connected; a disconnected error is thrown back and never retried on another holder.
Provider ids are random at registration, so a read fails over to another holder only when some holder's id sorts below
the owner's, and otherwise every read waits on the stopped owner. That order is a fact of one federation that the
harness can read, so it is checked and never assumed.

**What is in scope.** Only the spaces whose data provider (`holder`, `PlatformConfig.space_holders`) is one of the
providers a declared owner outage takes down, and that declare their failover holders (below). A federation with no
declared owner outage has nothing in scope: `orders` gives the empty tuple, and a caller then does nothing, writes
nothing and says nothing. A provider that holds no space, or one that only supports, is no owner, and its outage is not
checked. A space that declares no failover holder is out of scope of `orders`: nothing says where its reads go.

**Which providers count.** A supporter is never assumed to hold blocks. The agent's local provider supports every space
(the platform's `local`) and holds none of its data, since it is evicted at every reset: it would sort first, change
nothing for the failover, and let an owner stop that stalls pass. Only the providers the platform file declares as the
space's `failover` holders count (`StudySpace.failover`, in `spaces.py`: the providers holding the space's whole
replica, each a supporter, never the owner and never the local provider), and the order is over the owner and those
alone, whatever else supports the space. The declaration is in the platform file, so the campaign fingerprint holds it
(the digest of the entry): a member with another declaration is another experiment. The one-space form `[space]`
cannot declare one and is out of scope. `require_declared` is the fail-closed half: a space an owner holds that another
provider supports and that declares none is `FailoverError`, so that a platform which declares failover holders for some
spaces cannot leave another of the owner's spaces unchecked in silence.

**What holds.** The owner of a space and its failover holders are ordered by id as bytes (UTF-8, no case folding), as
Erlang compares binaries. `SpaceOrder.mirror_first` holds when the first of them is a failover holder, so with several
it holds when any one of them sorts below the owner. A provider whose id is unknown, or two with one id, gives no order:
that is `FailoverError`, never a pass. Two ids are never equal on a platform the loader accepted (`[providers]` refuses
a repeated id), so there are no ties to break.

**Names (O12).** A space is named by its position, `spaces[i]`, and a provider by its position in the platform file,
`providers[i]`, in every message and in the record. No provider's name, no space's name or label and no id is ever
written: the order is told by labels alone.

**The record.** `record_text` is the content of `failover-order.json` in a federation's root, which `up` writes once
the order holds, only for the spaces in scope (`hpcfailover` in deploy/hpc-federation); its `mirrors` are the spaces'
failover holders. It is evidence of what `up` admitted, which the login node's identity read reports
(`fleetidentity.py`), and nothing decides by it: the injector (`faults`) checks again, with `admit_stop`, from the
platform file's ids, holders and failover declarations as its configuration was loaded, before it admits the federation
or takes the owner down, and reads no record, since the platform file is the authority and is fingerprinted.
"""

import json
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from expcore_onedata.config import PlatformConfig
from expcore_onedata.spaces import StudySpace

FILE = "failover-order.json"  # in the federation's root; written only when a space is in scope and its order holds
VERSION = "expcore-failover-order/1"
SPACE_LABEL = re.compile(r"spaces\[(0|[1-9][0-9]*)\]")  # a position in the platform's spaces, never a name
PROVIDER_LABEL = re.compile(r"providers\[(0|[1-9][0-9]*)\]")  # a position in its providers, never a name
FIRST_IS_OWNER = "the owner's provider id sorts before every mirror's, so op-worker would ask the stopped owner first"


class FailoverError(ValueError):
    """The order of a space's owner and failover holders cannot be told, or does not hold. Messages hold labels only."""


class FailoverRefused(FailoverError):
    """The first of the owner and the failover holders of at least one space in scope, by provider id, is the owner.
    `failed` are those spaces."""

    def __init__(self, failed: Sequence["SpaceOrder"]) -> None:
        self.failed = tuple(failed)
        super().__init__(f"failover order: {'; '.join(order.sentence() for order in self.failed)}")


@dataclass(frozen=True)
class SpaceOrder:
    """One space in scope: its position label, its owner's and its failover holders' provider labels (`mirrors`), and
    the labels of the owner and those holders in the order op-worker asks them (provider id, bytewise)."""

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
    `owners` (provider names of this platform, the providers an owner outage takes down) and that declares its failover
    holders (`StudySpace.failover`; see "Which providers count"), ordered over the owner and those holders alone, in the
    file's order of the space's providers. Empty when `owners` is empty or none of them holds such a space.
    FailoverError when an owner is no provider of the platform, when a declared holder is no other supporter of its
    space (a platform built in code; the loader refuses it), or when an id of a provider ordered is not known."""
    taken = tuple(owners)
    if not taken:
        return ()
    unknown = [name for name in taken if name not in config.providers]
    if unknown:
        raise FailoverError(f"failover order: {len(unknown)} outage target(s) are no provider of the platform")
    targets = {config.provider_label(name) for name in taken}
    ids = {config.provider_label(name): provider.id for name, provider in config.providers.items()}
    found = []
    for entry, owner, others in _held_by(config, targets):
        if entry.failover:
            holders = _failover_holders(config, entry, others)
            found.append(
                SpaceOrder(entry.position, owner, holders, ordered({label: ids[label] for label in (owner, *holders)}))
            )
    return tuple(found)


def _held_by(config: PlatformConfig, targets: set[str]) -> Iterator[tuple[StudySpace, str, tuple[str, ...]]]:
    """Each space (an entry of the platform's spaces) whose data provider is one of the `targets` (provider labels),
    with that label and the labels of the other providers that support it, in the platform's order of spaces."""
    holders = config.space_holders()
    for entry in config.spaces:
        owner = holders.get(entry.space.name)
        if owner in targets:
            yield (
                entry,
                owner,
                tuple(label for label in map(config.provider_label, entry.support.providers) if label != owner),
            )


def _failover_holders(config: PlatformConfig, entry: StudySpace, others: Sequence[str]) -> tuple[str, ...]:
    """The labels of the providers `entry` declares to hold its replica, in the file's order of its providers.
    FailoverError when one is no other supporter of the space (the loader refuses that; a platform built in code may
    not): no order is guessed."""
    declared = {config.provider_label(name) for name in entry.failover if name in config.providers}
    holders = tuple(label for label in others if label in declared)
    if len(holders) != len(entry.failover):
        raise FailoverError(
            f"failover order: {entry.position}: a declared failover holder is no other supporter of the space in the "
            "platform file, so no order is told"
        )
    return holders


def require_declared(config: PlatformConfig, owners: Iterable[str]) -> None:
    """FailoverError unless every space that one of `owners` (provider names of this platform) holds and another
    provider supports declares its failover holders: nothing else says where such a space's reads go when its data
    provider is down, and counting its supporters would count the local provider, which holds nothing. The message names
    the spaces by position. Nothing for owners that hold no such space."""
    targets = {config.provider_label(name) for name in owners if name in config.providers}
    missing = [entry.position for entry, _, others in _held_by(config, targets) if others and not entry.failover]
    if missing:
        raise FailoverError(
            f"failover order: {', '.join(missing)}: declares no failover holder, so the order of the owner's outage "
            "cannot be told"
        )


def admit(config: PlatformConfig, owners: Iterable[str]) -> tuple[SpaceOrder, ...]:
    """`orders`, refused unless a failover holder is asked first in every space in scope: the spaces in scope with their
    order, which is empty when none is. FailoverRefused names every space that fails, FailoverError one that has no
    order."""
    found = orders(config, owners)
    failed = [order for order in found if not order.mirror_first]
    if failed:
        raise FailoverRefused(failed)
    return found


def admit_stop(config: PlatformConfig, owner: str) -> tuple[SpaceOrder, ...]:
    """The check before a stop of the provider `owner` (a name in `config`), which the injector makes from the ids,
    holders and failover declarations it was built with, its platform file's, which is fingerprinted: `admit` over
    `owner`. A platform that declares failover holders for no space has nothing in scope, as before O98, and nothing is
    checked. Where any space declares them, a space `owner` holds that another provider supports and that declares none
    is refused (`require_declared`): the stop of a provider that holds no such space is no owner stop. FailoverError
    when `owner` is no provider of the platform, when such a space declares none, or when an order is not known;
    FailoverRefused when the order does not hold."""
    if owner not in config.providers:
        return admit(config, (owner,))  # FailoverError: it is no provider of the platform
    if any(entry.failover for entry in config.spaces):
        require_declared(config, (owner,))
    return admit(config, (owner,))


def record_text(found: Sequence[SpaceOrder]) -> str:
    """The content of `failover-order.json`: sorted keys, an indent of 2 and a final newline."""
    document = {"version": VERSION, "spaces": [order.record() for order in found]}
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def read_record(text: str) -> dict[str, Any]:
    """The record as `record_text` wrote it, checked: exactly its two keys and its version, at least one space (an
    admission with none writes no record), and each space's labels, order and `mirror_first` (which must say a mirror is
    first, as only such a record is ever written), the spaces by ascending position, and all the orders consistent with
    one order of the providers (`consistent`): the ids are one set, so no two spaces can ask for opposite orders of two
    providers. A label's number is any non-negative integer with no leading zero. The login node's reader of the same
    file (`fleetidentity.failover_record_ok`) refuses the same records. FailoverError for anything else."""
    try:
        document = json.loads(text, object_pairs_hook=_once)
    except (ValueError, RecursionError):
        raise FailoverError("failover order: the record is no JSON") from None
    spaces = document.get("spaces") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "spaces"}
        or document["version"] != VERSION
        or not isinstance(spaces, list)
        or not spaces
    ):
        raise FailoverError("failover order: the record is not of this version")
    for space in spaces:
        if not _well_formed(space):
            raise FailoverError("failover order: a space of the record is malformed")
    positions = [_position(space["space"]) for space in spaces]  # each label was checked above
    if any(before >= after for before, after in pairwise(positions)):
        raise FailoverError("failover order: the spaces of the record are not in the platform's order")
    if not consistent([space["order"] for space in spaces]):
        raise FailoverError("failover order: the orders of the record contradict each other, so no ids give them")
    return document


def _position(label: str) -> tuple[int, str]:
    """The number in a label `spaces[<digits>]` that was checked, as a key: digits with no leading zero order by length
    and then as text, and no `int` is made of them, whose conversion of a very long digit string raises."""
    digits = label[len("spaces[") : -1]
    return len(digits), digits


def consistent(orders: Sequence[Sequence[str]]) -> bool:
    """Whether one order of the providers holds in every one of `orders` (each a list of distinct provider labels): the
    union of each order's adjacent pairs, first before second, has no cycle. The provider ids are one set whatever the
    space, so two spaces cannot order two providers oppositely."""
    after: dict[str, set[str]] = {}
    waiting: dict[str, int] = {}
    for order in orders:
        for label in order:
            after.setdefault(label, set())
            waiting.setdefault(label, 0)
        for first, second in pairwise(order):
            if second not in after[first]:
                after[first].add(second)
                waiting[second] += 1
    ready = [label for label, count in waiting.items() if count == 0]
    done = 0
    while ready:
        label = ready.pop()
        done += 1
        for later in after[label]:
            waiting[later] -= 1
            if waiting[later] == 0:
                ready.append(later)
    return done == len(waiting)


def _once(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object read with no key twice: what a reader that keeps the last would see is not what the file says."""
    found = dict(pairs)
    if len(found) != len(pairs):
        raise ValueError("a key twice")
    return found


def _well_formed(space: Any) -> bool:
    if not isinstance(space, dict) or set(space) != {"space", "owner", "mirrors", "order", "mirror_first"}:
        return False
    label, owner, mirrors, order = space["space"], space["owner"], space["mirrors"], space["order"]
    if not isinstance(label, str) or not SPACE_LABEL.fullmatch(label) or not isinstance(owner, str):
        return False
    if not isinstance(mirrors, list) or not mirrors or not isinstance(order, list):
        return False
    supporters = [owner, *mirrors]
    if not all(isinstance(item, str) and PROVIDER_LABEL.fullmatch(item) for item in [*supporters, *order]):
        return False
    return (
        len(set(supporters)) == len(supporters)
        and sorted(order) == sorted(supporters)
        and space["mirror_first"] is True
        and order[0] != owner
    )
