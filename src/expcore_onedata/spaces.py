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
holder = "<name>"               # this space's data provider, one of its providers: required, never [placement]'s (O09)
tokens = ["data_path", "tool_server"]  # optional: the token planes whose caveats reach this space; both by default
import = { readonly = true }    # optional: the data provider holds this space on an imported storage (below)
data_manifest = "data.json"     # optional: the manifest of the data the space holds (below)
```

Each entry is a `StudySpace`. The one-space form reads as the single space `spaces[0]` with the label `space`. A
message names an entry by its position (`spaces[1]`), never by its label, name or id, as every table of named entries
in this layer is named (O12). Loading refuses a label, name or id given twice, a local provider that differs between
entries (the agent is pinned to one, O03), an entry without its data provider or with one that does not support its
space, and a token plane scoped to no space. Whenever the `[[spaces]]` form is used, with one entry or several, its
entries are in the campaign fingerprint (`PlatformConfig.fingerprint`); the one-space form's space is in it by its
support, name and prefixes (`support`, `runsettings.support_record`).

**An imported storage** (`import`, in `[space]` or an entry of `[[spaces]]`; O41, `storageimport.py`). The space's data
provider holds its files on a storage Onedata imported them from (25.0's storage import), not on one they were written
to; `readonly` (required) says whether that storage is read-only, so that Onedata never writes to, changes or deletes
from it. A space without the key is a regular one, as every space was. The declaration is in the fingerprint: in an
entry's digest with `[[spaces]]`, and as `storage_import` in the one-space form, whose fingerprint stays as it was
without the key.

`shared` (optional, false by default; O59) says the storage's directory is imported by other federations too, so
nothing of this one may write to it: a shared import must be read-only, and `readonly = false` beside `shared = true`
is refused at load. It enters the fingerprint only when true, so no earlier declaration's fingerprint changes.

**The space's data identity** (`data_manifest`, in `[space]` or an entry of `[[spaces]]`; C06). A space is the same
experimental condition across federations when its declared identity and its data identity match. `data_manifest` names
the manifest of the data the space holds (`importmanifest`: every file with its size and SHA-256, every directory),
relative to the platform file or absolute; the loader reads and checks it, and the entry keeps its content hash
(`Manifest.digest()`), never the path. An entry that declares it has a digest of its declared identity (label, name,
prefixes, support, holder, token planes, import) and that hash, without the space's Onezone id, a deployment's handle
that every federation's `up` makes anew: two federations holding the same data give one digest, and one file different
gives another. An entry without it keeps the digest it always had, which holds the Onezone id, so its data being
unknown, two federations never give one digest, and a store started before the key existed resumes as it was. In the
one-space form, whose fingerprint holds nothing of its space, a declared manifest adds its content hash (`space_data`),
and without one that fingerprint is as it was. `legacy_digest()` is that id-bearing form of any entry, which a store
started before the entry declared its data may have recorded (`PlatformConfig.legacy_fingerprint`).
"""

import hashlib
import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.config.loading import ConfigError, Section

PLANES = ("data_path", "tool_server")  # the token planes (`tokens.Plane`), by the values a space's `tokens` names
ONE_SPACE_LABEL = "space"  # the label of the one-space form's space: its table's name
LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
SPACE_KEYS = ("name", "id", "prefixes", "import", "data_manifest")
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
class ImportedStorage:
    """How the data provider holds an imported space: on a storage Onedata imported the files from, read-only or not,
    and whether other federations import the same directory (`shared`, O59), in which case it is read-only."""

    readonly: bool
    shared: bool = False

    def record(self) -> dict[str, bool]:
        """What the fingerprint holds of it: `readonly`, and `shared` only when true, so that the digest of every
        declaration made before the key existed is what it was."""
        return {"readonly": self.readonly, **({"shared": True} if self.shared else {})}


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
    imported: ImportedStorage | None = None  # None: a regular storage, the data written to it (O41)
    data: str | None = None  # the content hash of the data manifest it declares (`data_manifest`), None without one

    def digest(self) -> str:
        """The SHA-256 of everything that makes this the space it is, for the fingerprint: never a value itself. With a
        data identity (`data_manifest`), its declared identity and that, without the Onezone id, so two federations
        holding the same data agree; without one, the form it always had (`legacy_digest`), the id included."""
        if self.data is None:
            return self.legacy_digest()
        facts = self._declared()
        facts["space"] = [self.space.name, list(self.space.prefixes)]
        facts["data"] = self.data
        return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()

    def legacy_digest(self) -> str:
        """The digest every entry had before a space could declare its data: the declared identity with the Onezone id.
        An imported storage adds a key, so the digest of a space without one is what it always was."""
        return hashlib.sha256(json.dumps(self._declared(), sort_keys=True).encode()).hexdigest()

    def _declared(self) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "label": self.label,
            "space": [self.space.name, self.space.id, list(self.space.prefixes)],
            "support": [list(self.support.providers), self.support.local],
            "holder": self.holder,
            "tokens": list(self.tokens),
        }
        if self.imported is not None:
            facts["import"] = self.imported.record()
        return facts


def parse_space(section: Section) -> Space:
    """Read a space's name, id and the prefixes the name must start with ([space], or an entry of [[spaces]])."""
    prefixes = section.strings("prefixes")
    if not prefixes or len(set(prefixes)) != len(prefixes):
        raise section.fail("prefixes", "must list at least one prefix, each once")
    name = section.string("name")
    if not name.startswith(prefixes):
        raise section.fail("name", "must start with one of the prefixes")
    return Space(name, section.string("id"), prefixes)


def parse_import(section: Section) -> ImportedStorage | None:
    """Read a space's optional `import` table ([space], or an entry of [[spaces]]): None when it has none."""
    if "import" not in section.data:
        return None
    value = section.data["import"]
    if not isinstance(value, Mapping):
        raise section.fail("import", "must be a table, written import = { readonly = true }")
    table = Section(value, section.file, f"{section.where}.import")
    table.reject_unknown(("readonly", "shared"))
    if "readonly" not in table.data:
        raise table.fail("readonly", "is required: say whether the imported storage is read-only")
    if not isinstance(table.data["readonly"], bool):
        raise table.fail("readonly", "must be true or false")
    shared = table.data.get("shared", False)
    if not isinstance(shared, bool):
        raise table.fail("shared", "must be true or false")
    if shared and not table.data["readonly"]:
        raise table.fail(
            "readonly",
            "must be true when 'shared' is true: other federations import this directory too, and nothing of this one "
            "may write to it",
        )
    return ImportedStorage(readonly=table.data["readonly"], shared=shared)


def parse_support(section: Section, providers: Collection[str]) -> Support:
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
    space = parse_space(space_table)
    support_table = root.section("support")
    support_table.reject_unknown(SUPPORT_KEYS)
    support = parse_support(support_table, providers)
    imported, data = parse_import(space_table), parse_data(space_table)
    return (StudySpace(ONE_SPACE_LABEL, "spaces[0]", space, support, imported=imported, data=data),)


def _entry(entry: Section, position: str, providers: Collection[str]) -> StudySpace:
    """Read one entry of [[spaces]]; `entry` is known in messages by its position."""
    entry.reject_unknown(SPACES_KEYS)
    label = entry.string("label")
    if not LABEL.fullmatch(label):
        raise entry.fail("label", "must be letters, digits, '_' and '-', starting with a letter")
    support = parse_support(entry, providers)
    holder = entry.string("holder")  # required: a default could make another owner's provider evictable (O09)
    if holder not in support.providers:
        raise entry.fail("holder", "must be one of the providers that support the space")
    tokens = entry.strings("tokens") if "tokens" in entry.data else PLANES
    if len(set(tokens)) != len(tokens) or any(plane not in PLANES for plane in tokens):
        raise entry.fail("tokens", f"must list token planes, each once; known: {', '.join(PLANES)}")
    data = parse_data(entry)
    return StudySpace(label, position, parse_space(entry), support, holder, tuple(tokens), parse_import(entry), data)


def parse_data(section: Section) -> str | None:
    """The content hash of the data manifest an entry declares (`data_manifest`), relative to the platform file or
    absolute; None without the key. A manifest that cannot be read or checked is refused naming the key, never the
    path (O12)."""
    if "data_manifest" not in section.data:
        return None
    from expcore_onedata.importmanifest import load_manifest  # it reaches config, which reads this module

    path = Path(section.file).parent / section.string("data_manifest")
    try:
        return load_manifest(path).digest()
    except ConfigError as error:  # the loader names the file first: that prefix, and the path anywhere, never shown
        said, prefix = str(error), f"{path}: "
        problem = said[len(prefix) :] if said.startswith(prefix) else "it cannot be read or checked"
        problem = problem.replace(str(path), "<data_manifest>")
        raise section.fail("data_manifest", f"is no manifest of a space's data ({problem})") from None


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
