# Provenance: new
"""`[link_check]` of platform.toml: the probe space of the pre-campaign link check, and its bounds (O35).

```toml
[link_check]                    # the pre-campaign link check (linkcheck.py)
name = "..."                    # the probe space: one no agent token reaches, never a declared study space
id = "..."
prefixes = ["..."]              # its name starts with one; only the check's requests, bound to it, reach it
providers = ["<name>", ...]     # the providers that support it: the local provider and every source of a pair
probe_bytes = 65536             # optional: the size of each probe file
timeout_s = 30                  # optional: the bound of each pair's fetch
visible_timeout_s = 60          # optional: the bound of the wait for a probe, and its block map, to show locally
evict_timeout_s = 60            # optional: the bound of an eviction of the probes from a provider, retries included
every_s = 600                   # optional: how often the health gate checks the links again during a campaign
```

**The pairs** (`link_pairs`) are the local provider and every source a run can fetch from: each provider that
supports a declared space, other than the local one, which is the same in every space (O03). The link between two
providers is theirs, whichever space a file lies in, so one probe space tests every pair.

**The probe space is never a study space.** Its probe files must be where no agent can see them: a space whose id or
name is a declared space's (`[space]`, or any `[[spaces]]` entry) is refused, since the agent's tokens are scoped to
the declared spaces. It is a space such as a federation's day-0 space, which no user of the agent is a member of
(`deploy/compose-federation`'s `agent_visible = false`). The client's gate admits a request in it only when the
request is bound to it (`OnezoneClient.mutate`'s `space`), and a name that a study space's prefix admits is refused,
so no study mutation can reach it. The check's transfers stay out of every study space's transfer listing.

**Every pair must be testable.** A probe space that the local provider, or a source, does not support is refused at
load, naming the provider by its position and the pair it leaves untested. Each refusal names the file and the key,
never a value (O12).
"""

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from expcore.config.loading import Section
from expcore_onedata.spaces import Space, StudySpace, parse_space

KEYS = (
    "name",
    "id",
    "prefixes",
    "providers",
    "probe_bytes",
    "timeout_s",
    "visible_timeout_s",
    "evict_timeout_s",
    "every_s",
)
POSITION = "[link_check]"
MAX_PROBE_BYTES = (1 << 20) - 1  # under 1 MiB: one on-the-fly window of 25.0, never a file another reader opens


@dataclass(frozen=True)
class LinkCheckSettings:
    """The probe space, the providers that support it, and the check's bounds."""

    space: Space
    providers: tuple[str, ...]
    probe_bytes: int = 65536
    timeout_s: float = 30.0
    visible_timeout_s: float = 60.0
    evict_timeout_s: float = 60.0
    every_s: float = 600.0


def link_pairs(spaces: Sequence[StudySpace], order: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """Every (local provider, source provider) pair a run can fetch through: the local provider, the same in every
    space, and each other provider that supports a declared space, once, in the order of `[providers]`."""
    if not spaces:
        return ()
    local = spaces[0].support.local
    sources = {name for entry in spaces for name in entry.support.providers if name != local}
    return tuple((local, name) for name in order if name in sources)


def _label(order: Sequence[str], name: str) -> str:
    return f"providers[{list(order).index(name)}]"


def _number(section: Section, key: str, default: float, minimum: float, *, integer: bool = False) -> float:
    """An optional finite number at or above `minimum`."""
    if key not in section.data:
        return default
    value = section.data[key]
    kind = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind) or not math.isfinite(value) or value < minimum:
        raise section.fail(key, f"must be {'an integer' if integer else 'a number'} of at least {minimum:g}")
    return value


def _providers(section: Section, declared: Collection[str]) -> tuple[str, ...]:
    names = section.strings("providers")
    if not names or len(set(names)) != len(names):
        raise section.fail("providers", "must list at least one provider, each once")
    if any(name not in declared for name in names):
        raise section.fail("providers", "names a provider that is not in [providers]")
    return names


def _not_a_study_space(section: Section, space: Space, spaces: Sequence[StudySpace]) -> None:
    for entry in spaces:
        for key, mine, theirs in (("id", space.id, entry.space.id), ("name", space.name, entry.space.name)):
            if mine == theirs:
                raise section.fail(
                    key,
                    f"is the {key} of {entry.position}, a declared study space, which the agent's tokens reach: the "
                    "probe space must be a space no agent can see, such as a federation's day-0 space",
                )
        if space.name.startswith(entry.space.prefixes):
            raise section.fail(
                "name",
                f"starts with a prefix of {entry.position}, so a study's own mutations could reach the probe space: "
                "give it a name no study prefix admits",
            )


def _every_pair_testable(
    section: Section, providers: tuple[str, ...], spaces: Sequence[StudySpace], order: Sequence[str]
) -> None:
    pairs = link_pairs(spaces, order)
    local = spaces[0].support.local
    if local not in providers:
        raise section.fail(
            "providers",
            f"leaves out the local provider {_label(order, local)}, through which every pair is read: the probe space "
            "must be supported by it",
        )
    for _, source in pairs:
        if source not in providers:
            where = next(e.position for e in spaces if source in e.support.providers)
            raise section.fail(
                "providers",
                f"leaves out {_label(order, source)}, a source of {where}, so the pair {_label(order, local)} <- "
                f"{_label(order, source)} cannot be tested: the probe space must be supported by it",
            )


def load_link_check(
    root: Section, providers: Mapping[str, object], spaces: Sequence[StudySpace]
) -> LinkCheckSettings | None:
    """Read `[link_check]` of platform.toml's root table, or None when there is none; refuse, naming the key, a probe
    space that is a declared study space and one that cannot test every pair."""
    if "link_check" not in root.data:
        return None
    section = Section(root.table("link_check"), root.file, POSITION)
    section.reject_unknown(KEYS)
    space = parse_space(section)
    order = list(providers)
    names = _providers(section, order)
    _not_a_study_space(section, space, spaces)
    _every_pair_testable(section, names, spaces, order)
    probe_bytes = int(_number(section, "probe_bytes", 65536, 1, integer=True))
    if probe_bytes > MAX_PROBE_BYTES:
        raise section.fail("probe_bytes", f"must be at most {MAX_PROBE_BYTES}, under 1 MiB: a probe is one small file")
    return LinkCheckSettings(
        space=space,
        providers=names,
        probe_bytes=probe_bytes,
        timeout_s=float(_number(section, "timeout_s", 30.0, 1)),
        visible_timeout_s=float(_number(section, "visible_timeout_s", 60.0, 1)),
        evict_timeout_s=float(_number(section, "evict_timeout_s", 60.0, 1)),
        every_s=float(_number(section, "every_s", 600.0, 60)),
    )


__all__ = ["MAX_PROBE_BYTES", "LinkCheckSettings", "link_pairs", "load_link_check"]
