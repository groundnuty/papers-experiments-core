# Provenance: new
"""The study's spaces in platform.toml: one `[space]` with its `[support]`, or several `[[spaces]]` (O04, O09, O12).

A federation study has one space per data owner, each supported by its owner's provider, by the agent's local provider
and sometimes by a mirror. It declares them as an array of tables, and the one-space form stays as it was:

```toml
[[spaces]]                      # instead of [space] and [support], never beside them
label = "<label>"               # how code names the space; a message names it by its position, spaces[i]
name = "..."                    # name, id and prefixes as in [space]
id = "..."
prefixes = ["..."]
providers = ["<name>", ...]     # the providers declared to support this space (O04)
local = "<name>"                # the agent's own provider, one of these; the same in every entry (O03)
holder = "<name>"               # optional: this space's data provider, in place of [placement] holder (O09)
tokens = ["data_path", "tool_server"]  # optional: the token planes whose caveats reach this space; both by default
```

Each entry is a `StudySpace`. The one-space form reads as the single space `spaces[0]` with the label `space`. A
message names an entry by its position (`spaces[1]`), never by its label, name or id, as every table of named entries
in this layer is named (O12). Loading refuses a label, name or id given twice, a local provider that differs between
entries (the agent is pinned to one, O03), a data provider that does not support its space, and a token plane scoped to
no space.
"""

import hashlib
import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from expcore.config.loading import Section

PLANES = ("data_path", "tool_server")  # the token planes (`tokens.Plane`), by the values a space's `tokens` names
ONE_SPACE_LABEL = "space"  # the label of the one-space form's space: its table's name
LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
SPACE_KEYS = ("name", "id", "prefixes")
SUPPORT_KEYS = ("providers", "local")
SPACES_KEYS = ("label", *SPACE_KEYS, *SUPPORT_KEYS, "holder", "tokens")


@dataclass(frozen=True)
class Space:
    """A study space and the name prefixes a mutation may act on."""

    name: str
    id: str
    prefixes: tuple[str, ...]


@dataclass(frozen=True)
class Support:
    """The providers declared to support a space, and the agent's own."""

    providers: tuple[str, ...]
    local: str


@dataclass(frozen=True)
class StudySpace:
    """One declared space: the label code names it by, the space, the providers that support it, its data provider
    when it declares one, and the token planes whose caveats reach it. A message names it by `position`, never by its
    label or its name."""

    label: str
    position: str  # `spaces[i]`, its place in the file
    space: Space
    support: Support
    holder: str | None = None
    tokens: tuple[str, ...] = PLANES

    def digest(self) -> str:
        """The SHA-256 of everything that makes this the space it is, for the fingerprint: never a value itself."""
        text = json.dumps(
            {
                "label": self.label,
                "space": [self.space.name, self.space.id, list(self.space.prefixes)],
                "support": [list(self.support.providers), self.support.local],
                "holder": self.holder,
                "tokens": list(self.tokens),
            },
            sort_keys=True,
        )
        return hashlib.sha256(text.encode()).hexdigest()


def read_space(section: Section) -> Space:
    """Read a space's name, id and the prefixes the name must start with ([space], or an entry of [[spaces]])."""
    prefixes = section.strings("prefixes")
    if not prefixes or len(set(prefixes)) != len(prefixes):
        raise section.fail("prefixes", "must list at least one prefix, each once")
    name = section.string("name")
    if not name.startswith(prefixes):
        raise section.fail("name", "must start with one of the prefixes")
    return Space(name, section.string("id"), prefixes)


def read_support(section: Section, providers: Collection[str]) -> Support:
    """Read a support set ([support], or an entry of [[spaces]]) and check it against the declared providers."""
    names = section.strings("providers")
    if not names or len(set(names)) != len(names):
        raise section.fail("providers", "must list at least one provider, each once")
    for name in names:
        if name not in providers:
            raise section.fail("providers", "names a provider that is not in [providers]")
    local = section.string("local")
    if local not in names:
        raise section.fail("local", "must be one of the supporting providers")
    return Support(names, local)


def _one_space(root: Section, providers: Collection[str]) -> tuple[StudySpace, ...]:
    """Read the one-space form, [space] and [support], as the single space `spaces[0]`."""
    space_table = root.section("space")
    space_table.reject_unknown(SPACE_KEYS)
    space = read_space(space_table)
    support_table = root.section("support")
    support_table.reject_unknown(SUPPORT_KEYS)
    return (StudySpace(ONE_SPACE_LABEL, "spaces[0]", space, read_support(support_table, providers)),)


def _entry(entry: Section, position: str, providers: Collection[str]) -> StudySpace:
    """Read one entry of [[spaces]]; `entry` is known in messages by its position."""
    entry.reject_unknown(SPACES_KEYS)
    label = entry.string("label")
    if not LABEL.fullmatch(label):
        raise entry.fail("label", "must be letters, digits, '_' and '-', starting with a letter")
    support = read_support(entry, providers)
    holder = entry.optional_string("holder")
    if holder is not None and holder not in support.providers:
        raise entry.fail("holder", "must be one of the providers that support the space")
    tokens = entry.strings("tokens") if "tokens" in entry.data else PLANES
    if len(set(tokens)) != len(tokens) or any(plane not in PLANES for plane in tokens):
        raise entry.fail("tokens", f"must list token planes, each once; known: {', '.join(PLANES)}")
    return StudySpace(label, position, read_space(entry), support, holder, tuple(tokens))


def _several(root: Section, providers: Collection[str]) -> tuple[StudySpace, ...]:
    """Read [[spaces]]: at least one entry, no label, name or id twice, one local provider for all, every token plane
    scoped to some space."""
    raw: Any = root.data["spaces"]
    if not isinstance(raw, list) or not raw or not all(isinstance(item, Mapping) for item in raw):
        raise root.fail("spaces", "must be a non-empty array of tables, written [[spaces]]")
    entries = tuple(
        _entry(Section(item, root.file, f"spaces[{i}]"), f"spaces[{i}]", providers) for i, item in enumerate(raw)
    )
    for what, values in (
        ("label", [e.label for e in entries]),
        ("name", [e.space.name for e in entries]),
        ("id", [e.space.id for e in entries]),
    ):
        if len(set(values)) != len(values):
            raise root.fail("spaces", f"must give each space its own {what}")
    if len({e.support.local for e in entries}) != 1:
        raise root.fail("spaces", "must name the same local provider in every entry: the agent is pinned to one (O03)")
    for plane in PLANES:
        if not any(plane in e.tokens for e in entries):
            raise root.fail("spaces", f"must scope the {plane} plane's token to at least one space")
    return entries


def load_spaces(root: Section, providers: Collection[str]) -> tuple[StudySpace, ...]:
    """Read the spaces of platform.toml's root table in either form, and refuse a file that mixes the two."""
    if "spaces" not in root.data:
        return _one_space(root, providers)
    mixed = [key for key in ("space", "support") if key in root.data]
    if mixed:
        raise root.fail("spaces", f"cannot be declared beside [{mixed[0]}]; use one form or the other")
    return _several(root, providers)
