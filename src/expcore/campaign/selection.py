# Provenance: new
"""One campaign run in batches chosen by factor levels (C35): a selection shard, `expcore-shard/4`.

A study that runs some of its conditions now and the rest later (stages early and mid now, late and last next week)
keeps one campaign: the study, its factor schema and its fingerprint stay whole, and each batch is a **selection** of
the plan's runs by the levels of declared factors, given on the command line, never in `campaign.toml`:

    python -m expcore run STUDY --select stage=early,mid --shard-label first

`--select FACTOR=L1,L2` keeps the runs whose value of the factor is one of the levels; given for several factors, a
run is kept when every factor matches. Only declared factors are selected on (the built-in task, model and seed are
not): a selection is a batch of conditions. A selection is a shard of the same campaign under the rule
`RULE_SELECTION`, recorded in its store, its snapshot, its export and a merge exactly as a shard's spec is, and the
merge (#168) joins the selections of a campaign as it joins its shards (`campaign.merge_selection`).

Unlike a shard of `expcore-shard/2` or `/3`, **a pair crosses selections**: an early-late contrast pairs a run of the
first batch with one of the second, by task, model and seed, after the merge. That is the point of a selection, and
the analysis pairs the merged runs as it pairs one driver's.

The spec (`SelectionSpec`) holds the label, the selection and whether the batches run one after another
(`--sequential`, always declared, never assumed). Its place in a merge is not part of it: the merge numbers the
selections 1..N by their canonical selection text (`numbered`), so every reader of the snapshots numbers them alike.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import InitVar, dataclass, field, replace
from typing import TYPE_CHECKING, Any

from expcore.campaign.shard import ShardError, ShardSpec, is_default_label

if TYPE_CHECKING:
    from expcore.campaign.factors import FactorSchema

RULE_SELECTION = "expcore-shard/4: a declared selection of factor levels"
"""The rule of a selection shard; kept with every selection spec, never accepted by `ShardSpec`."""

_LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_LABEL_WIDE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_FACTOR = re.compile(r"[a-z][a-z0-9_]{0,31}")
_SPEC_KEYS = {"rule", "label", "select", "sequential"}


@dataclass(frozen=True)
class Selection:
    """The levels kept of each selected factor: factors in name order, each with its levels, none repeated."""

    levels: tuple[tuple[str, tuple[str, ...]], ...]

    def __post_init__(self) -> None:
        """Refuse a selection no batch can have."""
        names = [entry[0] for entry in self.levels]
        if not names:
            raise ShardError("--select names at least one factor and its levels, such as --select stage=early,mid")
        if len(set(names)) != len(names):
            repeated = sorted({name for name in names if names.count(name) > 1})
            raise ShardError(f"--select names factor {repeated[0]!r} twice: give its levels once, comma-separated")
        for name, levels in self.levels:
            if not isinstance(name, str) or not _FACTOR.fullmatch(name):
                raise ShardError(f"--select names {name!r}, which is no factor name")
            if not levels or not all(isinstance(level, str) and level for level in levels):
                raise ShardError(f"--select {name}= names no level; give one or more, comma-separated")
            if len(set(levels)) != len(levels):
                raise ShardError(f"--select {name}= repeats a level")
        if names != sorted(names):
            raise ShardError("a selection keeps its factors in name order")

    @classmethod
    def parse(cls, texts: Sequence[str]) -> Selection:
        """Read `FACTOR=L1,L2` arguments of the command line (syntax only; `checked` holds them to the schema)."""
        parsed: list[tuple[str, tuple[str, ...]]] = []
        for text in texts:
            name, equals, levels = text.strip().partition("=")
            if not equals or not name:
                raise ShardError(f"--select takes FACTOR=LEVEL[,LEVEL...], such as stage=early,mid, not {text!r}")
            parsed.append((name.strip(), tuple(level.strip() for level in levels.split(","))))
        return cls(tuple(sorted(parsed, key=lambda entry: entry[0])))

    def checked(self, schema: FactorSchema) -> Selection:
        """This selection held to the study's schema: every factor declared, every level one of its own, kept in the
        schema's order of levels. Raises ShardError naming the unknown factor or level and what is known."""
        out = []
        for name, levels in self.levels:
            if name not in schema.declared:
                known = ", ".join(schema.declared) or "none"
                raise ShardError(
                    f"--select names factor {name!r}, which the study does not declare (declared factors: {known}); "
                    "a selection is made on declared factors only"
                )
            declared = schema.declared[name]
            unknown = [level for level in levels if level not in declared]
            if unknown:
                raise ShardError(
                    f"--select {name}= names level {unknown[0]!r}, which factor {name!r} does not have "
                    f"(its levels: {', '.join(declared)})"
                )
            out.append((name, tuple(level for level in declared if level in levels)))
        return Selection(tuple(out))

    def selects(self, factors: Mapping[str, Any]) -> bool:
        """Whether a run, by its factor values, is in this selection."""
        return all(str(factors.get(name)) in levels for name, levels in self.levels)

    @property
    def text(self) -> str:
        """The selection as the command line writes it: `stage=early,mid`, factors joined by a space."""
        return " ".join(f"{name}={','.join(levels)}" for name, levels in self.levels)

    def to_json(self) -> dict[str, list[str]]:
        """The selection as the store, the export and a merge keep it: factor to its levels."""
        return {name: list(levels) for name, levels in self.levels}

    @classmethod
    def from_json(cls, data: Any) -> Selection:
        """Read a selection back, refusing any other form."""
        if not isinstance(data, Mapping) or not all(isinstance(v, list) for v in data.values()):
            raise ShardError("a selection is a mapping of factor names to lists of levels")
        return cls(tuple(sorted(((str(k), tuple(v)) for k, v in data.items()), key=lambda entry: entry[0])))


@dataclass(frozen=True)
class SelectionSpec:
    """A selection shard: the operator's `label`, the `selection`, whether it is `sequential`, and the rule.

    `index` and `count` are its place in a merge, assigned there (`numbered`) and never stored: a selection does not
    know how many batches its campaign has when it runs. The label is held to the shard label's form.
    """

    label: str
    selection: Selection
    sequential: bool = False
    rule: str = RULE_SELECTION
    index: int | None = field(default=None, compare=False)
    count: int | None = field(default=None, compare=False)
    wide_label: InitVar[bool] = False

    def __post_init__(self, wide_label: bool) -> None:
        """Refuse a spec no selection can have."""
        if self.rule != RULE_SELECTION:
            raise ShardError(f"a selection shard is made under {RULE_SELECTION!r}, not {self.rule!r}")
        if not isinstance(self.sequential, bool):
            raise ShardError("a selection's sequential setting is true or false")
        if not isinstance(self.label, str) or not (_LABEL_WIDE if wide_label else _LABEL).fullmatch(self.label):
            raise ShardError(
                "a selection's label (--shard-label) is 1 to 64 lower-case letters, digits or '-', starting with a "
                "letter or digit, with no '.', so that a host name or an address is not used by accident "
                "(--shard-label-wide also allows '.' and '_')"
            )

    @property
    def name(self) -> str:
        """How a message names the selection: its label and its levels, never a path."""
        return f"selection {self.label} ({self.selection.text})"

    def at(self, index: int, count: int) -> SelectionSpec:
        """This selection numbered `index` of `count` in a merge."""
        return replace(self, index=index, count=count, wide_label=True)

    def to_json(self) -> dict[str, Any]:
        """The spec as the store, the export and a merge keep it."""
        return {
            "rule": self.rule,
            "label": self.label,
            "select": self.selection.to_json(),
            "sequential": self.sequential,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SelectionSpec:
        """Read a spec back, refusing missing or extra keys; a label of the wide form is read too."""
        if not isinstance(data, Mapping) or set(data) != _SPEC_KEYS:
            raise ShardError("a selection spec has exactly the keys rule, label, select and sequential")
        selection = Selection.from_json(data["select"])
        return cls(data["label"], selection, data["sequential"], data["rule"], wide_label=True)


Spec = ShardSpec | SelectionSpec


def spec_from_json(data: Any) -> Spec:
    """A stored spec, a shard's or a selection's, by its rule."""
    if isinstance(data, Mapping) and data.get("rule") == RULE_SELECTION:
        return SelectionSpec.from_json(data)
    return ShardSpec.from_json(data)


def describe(data: Mapping[str, Any]) -> str:
    """How a message names a stored spec (its JSON): `shard 2 of 4 ('host-b')` or `selection first (stage=early)`."""
    if data.get("rule") == RULE_SELECTION:
        try:
            return SelectionSpec.from_json(data).name
        except ShardError:
            return f"selection {data.get('label')!r}"
    return f"shard {data.get('index')} of {data.get('count')} ({data.get('label')!r})"


def resume_hint(data: Mapping[str, Any]) -> str:
    """The options that resume the selection a store recorded, as a command line gives them."""
    spec = SelectionSpec.from_json(data)
    options = " ".join(f"--select {name}={','.join(levels)}" for name, levels in spec.selection.levels)
    wide = "" if is_default_label(spec.label) else " --shard-label-wide"
    sequential = " --sequential (with the same --after)" if spec.sequential else ""
    return f"{options} --shard-label {spec.label}{wide}{sequential}"


def canonical(selection: Selection) -> str:
    """The selection's canonical text, by which a merge numbers selections."""
    return json.dumps(selection.to_json(), sort_keys=True, separators=(",", ":"))


__all__ = [
    "RULE_SELECTION",
    "Selection",
    "SelectionSpec",
    "Spec",
    "canonical",
    "describe",
    "resume_hint",
    "spec_from_json",
]
