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

**What holds.** The owner of a space and its mirrors are ordered by id as bytes (UTF-8, no case folding), as Erlang
compares binaries. `SpaceOrder.mirror_first` holds when the first of them is a mirror, so with several mirrors it holds
when any one of them sorts below the owner. A supporter whose id is unknown, or two with one id, gives no order: that is
`FailoverError`, never a pass. Two ids are never equal on a platform the loader accepted (`[providers]` refuses a
repeated id), so there are no ties to break.

**Which providers are mirrors.** A mirror is a provider the study designates to hold the space's replicas, and op-worker
asks only the holders of a file's blocks: a provider that supports the space and holds nothing (the agent's local
provider, say) sorts first and changes nothing for the failover, so counting it would pass an owner stop that stalls.
Where the caller knows the designated mirrors (`orders`' `declared`, by space and provider label: `up` has the layout's
`Space.mirrors`, and the record lists them), the order is over the owner and those mirrors alone and every other
supporter is ignored; a declared mirror that is no other supporter of the space in the file, or one declared twice, is
`FailoverError`. A space `declared` lists with no mirror is out of scope. Where it knows none (a Compose federation, a
test), every supporter other than the owner counts, as it always did.

**Names (O12).** A space is named by its position, `spaces[i]`, and a provider by its position in the platform file,
`providers[i]`, in every message and in the record. No provider's name, no space's name or label and no id is ever
written: the order is told by labels alone.

**The record.** `record_text` is the content of `failover-order.json` in a federation's root, which `up` writes once
the order holds, only for the spaces in scope (`hpcfailover` in deploy/hpc-federation), its `mirrors` the declared ones.
The injector (`faults`) checks again, from the ids and holders its configuration was loaded with, before it admits the
federation or takes the owner down. A root that holds the record is an HPC member's, and `admit_stop` then takes each
space's mirrors from it, read strictly (`read_record`; a record that is unreadable, or whose labels do not resolve in
the platform file, is `FailoverError`: the order is never assumed) and against the platform's current ids. A space the
owner holds that another provider supports and that the record does not list is `FailoverError` too: no mirror was
declared for it, and counting every supporter there would let one that holds nothing (the local provider) pass the stop.
A root with none (a Compose federation, a test) is checked as before, over every supporter.
"""

import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from expcore_onedata.config import PlatformConfig
from expcore_onedata.spaces import StudySpace

FILE = "failover-order.json"  # in the federation's root; written only when a space is in scope and its order holds
VERSION = "expcore-failover-order/1"
SPACE_LABEL = re.compile(r"spaces\[(0|[1-9][0-9]*)\]")  # a position in the platform's spaces, never a name
PROVIDER_LABEL = re.compile(r"providers\[(0|[1-9][0-9]*)\]")  # a position in its providers, never a name
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


def orders(
    config: PlatformConfig, owners: Iterable[str], declared: Mapping[str, Sequence[str]] | None = None
) -> tuple[SpaceOrder, ...]:
    """The order of every space in scope, in the platform's order of spaces: a space whose data provider is one of
    `owners` (provider names of this platform, the providers an owner outage takes down) and that another provider
    supports. Empty when `owners` is empty or none of them holds a space with a mirror.

    `declared` maps a space's position label to the labels of the providers declared its mirrors (see "Which providers
    are mirrors"): a space it lists is ordered over its owner and those mirrors alone, whatever else supports it, and is
    out of scope when it lists none; a space it does not list is ordered over every other supporter. None: every space
    is. FailoverError when an owner is no provider of the platform, when a declared mirror is no other supporter of its
    space in the file (or is declared twice), or when an id of a provider ordered is not known."""
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
        wanted = None if declared is None else declared.get(entry.position)
        mirrors = others if wanted is None else _declared_mirrors(entry.position, others, wanted)
        if mirrors:
            found.append(
                SpaceOrder(entry.position, owner, mirrors, ordered({label: ids[label] for label in (owner, *mirrors)}))
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


def _declared_mirrors(position: str, others: Sequence[str], wanted: Sequence[str]) -> tuple[str, ...]:
    """The providers among `others` (a space's other supporters) that are declared its mirrors (`wanted`), in the file's
    order. FailoverError when a declared mirror is not one of `others`, or is declared twice: no order is guessed."""
    kept = tuple(label for label in others if label in wanted)
    if len(kept) != len(wanted):
        raise FailoverError(
            f"failover order: {position}: a declared mirror is no other supporter of the space in the platform file, "
            "or is declared twice, so no order is told"
        )
    return kept


def admit(
    config: PlatformConfig, owners: Iterable[str], declared: Mapping[str, Sequence[str]] | None = None
) -> tuple[SpaceOrder, ...]:
    """`orders`, refused unless a mirror is asked first in every space in scope: the spaces in scope with their order,
    which is empty when none is. FailoverRefused names every space that fails, FailoverError one that has no order."""
    found = orders(config, owners, declared)
    failed = [order for order in found if not order.mirror_first]
    if failed:
        raise FailoverRefused(failed)
    return found


def admit_stop(config: PlatformConfig, owner: str, root: Path) -> tuple[SpaceOrder, ...]:
    """The check before a stop of the provider `owner` (a name in `config`), which the injector makes from the ids and
    holders it was built with: `admit` over the mirrors of the federation's root. A root that holds the record
    `failover-order.json` (an HPC member's, written by `up`) gives each space it lists the mirrors it declares, read
    strictly and against the platform's current ids, and must list every space `owner` holds that another provider
    supports; every space of a root with none is over all its supporters. Nothing of `root` is read unless `owner` holds
    a space that another provider supports (a stop of anything else is no owner stop). FailoverError when the record is
    unreadable, any space of it does not resolve in the platform file, it does not list such a space of `owner`, or the
    order is not known; FailoverRefused when the order does not hold."""
    if owner not in config.providers:
        return admit(config, (owner,))  # FailoverError: it is no provider of the platform
    supported = [entry.position for entry, _, others in _held_by(config, {config.provider_label(owner)}) if others]
    if not supported:
        return ()
    record = _record_in(root)
    if record is None:
        return admit(config, (owner,))
    declared = _recorded_mirrors(config, owner, record)
    unlisted = [position for position in supported if position not in declared]
    if unlisted:
        raise FailoverError(
            f"failover order: {', '.join(unlisted)}: the record declares no mirror of this space, so no order is told"
        )
    return admit(config, (owner,), declared)


def _record_in(root: Path) -> dict[str, Any] | None:
    """The record `up` left in `root`, read as `read_record` reads it; None when there is none. FailoverError for one
    that cannot be read, however it fails: a record that might say something else is no ground to take a stop on."""
    try:
        raw = (root / FILE).read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise FailoverError("failover order: the record cannot be read, so no order is told") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise FailoverError("failover order: the record is no JSON") from None
    return read_record(text)


def _recorded_mirrors(config: PlatformConfig, owner: str, record: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """The mirrors the record declares for each of its spaces that `owner` holds. Every space of the record is resolved
    in the platform file first, whoever holds it, since one that does not resolve shows the record is not of this
    platform file (an earlier one's, or another's): FailoverError for a position the file has no space at, a data
    provider other than the record's owner, or a provider label that is none of the file's providers."""
    target = config.provider_label(owner)
    labels = {config.provider_label(name) for name in config.providers}
    entries = {entry.position: entry for entry in config.spaces}
    holders = config.space_holders()
    found = {}
    for space in record["spaces"]:
        entry = entries.get(space["space"])
        held = holders.get(entry.space.name) if entry is not None else None
        if held != space["owner"] or any(label not in labels for label in (space["owner"], *space["mirrors"])):
            raise FailoverError(
                f"failover order: {space['space']} of the record does not resolve in the platform file, so no order "
                "is told"
            )
        if space["owner"] == target:
            found[space["space"]] = tuple(space["mirrors"])
    return found


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
