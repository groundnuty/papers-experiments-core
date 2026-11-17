"""A space's support set as the zone lists it, compared with the layout's declaration (O35).

The layout declares each space's supporters (its owner and its mirrors), `facts.json` records them as `supported_by`,
and every later reader takes that declaration for the platform's own state: the link check's pairs, the reset's
occupancy stage, a study's sweeps. Nothing but `Bootstrap.verify_support` asks the zone, and what these functions do is
read its answer and say how it differs:

- `listed` reads the zone's answer for one space into the providers' layout labels and a count of the ids that map to
  no label (a provider the bootstrap did not give out);
- `extras` and `missing` give the differences, one message each, naming the space and the provider by its layout label
  and never by an id, a host or an address (an undeclared provider is only counted);
- `refusal` joins them into the one message up fails with, and `stamp` writes the time a set was read.

A support is never removed by up, so a difference is never repaired here: the remedy is in the message.
"""

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SupportRead:
    """One space's support set as the zone listed it: the providers' layout labels, sorted, and when it was read (UTC,
    to the second). Kept in the bootstrap's ids and written into the facts beside the declared set."""

    providers: tuple[str, ...]
    read_at: str


def stamp(seconds: float) -> str:
    """The UTC time of `seconds` since the epoch, to the second, as an ISO 8601 string ending in Z."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def listed(space: str, answer: Any, labels: Mapping[str, str]) -> tuple[tuple[str, ...], int]:
    """The providers the zone lists for a space: (their layout labels sorted, the number of ids that map to no label).

    `answer` is the zone's `GET /spaces/<id>/providers`; `labels` maps a provider's id to its layout label. An answer
    that is no object with a list of strings under `providers` is refused naming the space: a bare string would read as
    a set of characters, each an undeclared provider."""
    found = answer.get("providers") if isinstance(answer, dict) else None
    if not isinstance(found, list) or not all(isinstance(item, str) for item in found):
        raise RuntimeError(f"space {space}: the zone's answer is no list of provider ids")
    known = {labels[item] for item in found if item in labels}
    return tuple(sorted(known)), len({item for item in found if item not in labels})


def extras(space: str, declared: Sequence[str], held: Sequence[str], undeclared: int) -> list[str]:
    """What supports a space and is not declared for it: each provider by label, sorted, then the undeclared ones."""
    problems = [
        f"space {space}: supported by {label} but the platform doesn't declare it"
        for label in sorted(set(held) - set(declared))
    ]
    if undeclared == 1:
        problems.append(f"space {space}: supported by an undeclared provider but the platform doesn't declare it")
    elif undeclared:
        problems.append(
            f"space {space}: supported by {undeclared} undeclared providers but the platform doesn't declare them"
        )
    return problems


def missing(space: str, declared: Sequence[str], held: Sequence[str]) -> list[str]:
    """What a space declares and does not have: each provider by label, in the layout's order."""
    return [
        f"space {space}: declares {label} but it doesn't support the space" for label in declared if label not in held
    ]


def refusal(problems: Sequence[str]) -> str:
    """The message up fails with for these differences, and what to do about them: a support is never removed here."""
    return (
        "the federation's support sets differ from the layout's declaration: "
        + "; ".join(problems)
        + "; refused, since up never removes a support (down, then up, makes the federation anew)"
    )
