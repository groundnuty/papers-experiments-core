"""A space's support set as the zone lists it, compared with the layout's declaration (O35).

The layout declares each space's supporters (its owner and its mirrors), `facts.json` records them as `supported_by`,
and every later reader takes that declaration for the platform's own state: the link check's pairs, the reset's
occupancy stage, a study's sweeps. Only `Bootstrap.ensure_support` (before it supports a space) and
`Bootstrap.verify_support` (after) ask the zone for it, and what these functions do is read its answer and say how it
differs:

- `listed` reads the zone's answer for one space into the providers' layout labels and a count of the ids that map to
  no label (a provider the bootstrap did not give out);
- `extras` and `missing` give the differences, one message each, naming the space and the provider by its layout label
  and never by an id, a host or an address (an undeclared provider is only counted);
- `refusal` joins them into the one message up fails with, and `stamp` writes the time a set was read;
- `check_rendered_support` holds the rendered `platform.toml` to what the zone listed: the link check's pairs, the
  import verification and a campaign read the support sets of THAT file, whose template declares them itself, so a
  template that leaves a supporter out would leave its pairs untested and one that adds a provider would test links
  the space does not have.

A support is never removed by up, so a difference is never repaired here: the remedy is in the message.
"""

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fedlayout import Layout


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
        + "; refused: up never removes a support, so remove an extra one in the zone, or run down, then up, to make "
        "the federation anew"
    )


def rendered_differences(space: str, declared: Sequence[str], measured: Sequence[str], undeclared: int) -> list[str]:
    """How a rendered file's support set for a space differs from the zone's (`measured`), one message each.

    `declared` is the file's, as layout labels, and `undeclared` counts its providers whose id is no provider of the
    federation. What the zone lists and the file leaves out comes first, then what the file declares and the space
    does not have."""
    problems = [
        f"space {space}: supported by {label} but platform.toml doesn't declare it"
        for label in sorted(set(measured) - set(declared))
    ]
    problems += [
        f"space {space}: platform.toml declares {label} but it doesn't support the space"
        for label in sorted(set(declared) - set(measured))
    ]
    if undeclared == 1:
        problems.append(f"space {space}: platform.toml declares an undeclared provider that doesn't support the space")
    elif undeclared:
        problems.append(
            f"space {space}: platform.toml declares {undeclared} undeclared providers that don't support the space"
        )
    return problems


def check_rendered_support(layout: Layout, facts: Mapping[str, Any]) -> None:
    """Refuse a rendered platform file whose support set for a space is not the set the zone listed (O35).

    `facts` are the ones the file was rendered from: each space's id and measured set (`supported_by_measured`, from
    `Bootstrap.verify_support`) and each provider's id. The file is loaded as the platform layer reads it, without its
    credentials or manifests (which are no concern of this check); its spaces are matched to the layout's by id and its
    providers to the federation's by id, whatever the template calls them, and the refusal names them by layout label,
    one message per difference, never an id. A space of the file that is no space of the federation, or a facts record
    with no measured set (a bootstrap that never read one), is not checked here."""
    measured = {n: s["supported_by_measured"] for n, s in facts["spaces"].items() if "supported_by_measured" in s}
    if not measured:
        return
    from expcore.config.loading import ConfigError
    from expcore_onedata.config import load_platform

    try:
        config = load_platform(layout.platform, require_secrets=False, require_manifests=False)
    except ConfigError as error:
        raise RuntimeError(f"the rendered platform file is refused: {error}") from None
    space_of = {space["id"]: name for name, space in facts["spaces"].items()}
    label_of = {provider["id"]: name for name, provider in facts["providers"].items()}
    problems: list[str] = []
    for entry in config.spaces:
        name = space_of.get(entry.space.id)
        if name not in measured:
            continue
        ids = [config.providers[provider].id for provider in entry.support.providers]
        known = [label_of[found] for found in ids if found in label_of]
        problems += rendered_differences(name, known, measured[name], sum(1 for found in ids if found not in label_of))
    if problems:
        raise RuntimeError(
            "the rendered platform.toml's support sets differ from what the zone listed: "
            + "; ".join(problems)
            + "; refused: the link check and a campaign read the file's sets, so declare the space's supporters in the "
            "template as the layout does"
        )
