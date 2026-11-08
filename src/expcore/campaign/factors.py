# Provenance: new
"""The factor schema: what varies in a study, how a run is identified, and the plan that crosses the factors.

A study declares factors with levels, an identity order and named conditions in `study.toml`:

    [factors.data_path]
    levels = ["mount", "api"]

    [identity]
    order = ["task", "data_path", "model", "seed"]

    [conditions.mount]
    data_path = "mount"

`task`, `model` and `seed` are built in. A condition assigns every declared factor and none of the built-in ones.
A run key is the factor values in the identity order, joined canonically; its SHA-256 names files and store rows.
Two runs share a key only if they agree on every factor, and two conditions never assign the same levels, so
conditions never share a key (C18). A plan expands conditions x tasks x models x seeds and refuses a repeated
value, so keys are unique by construction (R11).
"""

import hashlib
import itertools
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any
from urllib.parse import quote

from expcore.config.loading import Section, load_toml

BUILTIN_FACTORS = ("task", "model", "seed")
# A factor name becomes part of a SQL column name in the store, so it is held to a plain identifier.
_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_SEPARATOR = "|"


class PlanError(ValueError):
    """A plan input is unusable: an empty list, a repeated value, or a value of the wrong type."""


@dataclass(frozen=True)
class RunKey:
    """The identity of one run: factor names and values in the identity order, with the text and digest they give.

    `text` is `name=value` pairs joined by `|` with every value percent-encoded, so no value can contain the
    separator and two different value tuples never give the same text. `digest` is the SHA-256 of the text.
    """

    names: tuple[str, ...]
    values: tuple[str | int, ...]
    text: str = field(init=False, compare=False, repr=False)
    digest: str = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        """Check the values against the names and derive the text and the digest."""
        if len(self.names) != len(self.values) or len(set(self.names)) != len(self.names):
            raise PlanError("a run key needs one value for each distinct factor name")
        text = _SEPARATOR.join(f"{n}={quote(str(v), safe='')}" for n, v in zip(self.names, self.values, strict=True))
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "digest", hashlib.sha256(text.encode("utf-8")).hexdigest())

    def __str__(self) -> str:
        """Return the canonical text."""
        return self.text

    @property
    def factors(self) -> dict[str, str | int]:
        """Return the factor values by name, in the identity order."""
        return dict(zip(self.names, self.values, strict=True))

    @cached_property
    def factors_json(self) -> str:
        """Return the factor values as the JSON text the store keeps."""
        return json.dumps(self.factors, separators=(",", ":"), ensure_ascii=False)


class FactorSchema:
    """The declared factors, the identity order and the named conditions of one study, checked as a whole.

    Every problem names the file and the key it came from. The constructor is the one place that validates, so a
    schema built in code is held to the same rules as one loaded from `study.toml`.
    """

    def __init__(
        self,
        declared: Mapping[str, Sequence[str]],
        order: Sequence[str],
        conditions: Mapping[str, Mapping[str, str]],
        *,
        file: str | os.PathLike[str] = "<schema>",
    ) -> None:
        """Validate and keep copies of the declared levels, the identity order and the conditions."""
        self.file = str(file)
        self.declared: dict[str, tuple[str, ...]] = {name: tuple(levels) for name, levels in declared.items()}
        self.order = tuple(order)
        self.conditions: dict[str, dict[str, str]] = {name: dict(c) for name, c in conditions.items()}
        self._check_factors()
        self._check_order()
        self._check_conditions()
        self._by_levels = {tuple(c[f] for f in self.declared): name for name, c in self.conditions.items()}

    # --- validation ---------------------------------------------------------------------------------------------

    def _check_factors(self) -> None:
        """Refuse a badly named, built-in or level-less factor."""
        for name, levels in self.declared.items():
            where = Section({"levels": list(levels)}, self.file, f"[factors.{name}]")
            if name in BUILTIN_FACTORS:
                raise Section({}, self.file, "[factors]").fail(name, "is built in and cannot be declared")
            if not _NAME_RE.fullmatch(name):
                raise Section({}, self.file, "[factors]").fail(
                    name, "must start with a lower-case letter and use lower-case letters, digits and underscores"
                )
            if not levels or not all(isinstance(x, str) and x for x in levels):
                raise where.fail("levels", "must be a non-empty array of non-empty strings")
            repeated = sorted({x for x in levels if levels.count(x) > 1})
            if repeated:
                raise where.fail("levels", f"repeats {', '.join(map(repr, repeated))}")

    def _check_order(self) -> None:
        """Require the identity order to name every factor, built in or declared, exactly once."""
        where = Section({}, self.file, "[identity]")
        everything = [*BUILTIN_FACTORS, *self.declared]
        unknown = [name for name in self.order if name not in everything]
        if unknown:
            raise where.fail("order", f"names unknown factor {unknown[0]!r}; the factors are: {', '.join(everything)}")
        repeated = sorted({name for name in self.order if self.order.count(name) > 1})
        if repeated:
            raise where.fail("order", f"repeats factor {repeated[0]!r}")
        omitted = [name for name in everything if name not in self.order]
        if omitted:
            raise where.fail("order", f"omits factor {omitted[0]!r}; every factor must appear once")

    def _check_conditions(self) -> None:
        """Require at least one condition, each assigning every declared factor a declared level, none alike."""
        if not self.conditions:
            raise Section({}, self.file, "[conditions]").fail(None, "needs at least one condition")
        seen: dict[tuple[str, ...], str] = {}
        for name, assignment in self.conditions.items():
            where = Section(assignment, self.file, f"[conditions.{name}]")
            for factor, level in assignment.items():
                if factor in BUILTIN_FACTORS:
                    raise where.fail(factor, "is built in; a condition assigns declared factors only")
                if factor not in self.declared:
                    raise where.fail(
                        factor, f"is not a declared factor; declared: {', '.join(self.declared) or 'none'}"
                    )
                if level not in self.declared[factor]:
                    raise where.fail(
                        factor, f"has unknown level {level!r}; levels of {factor!r}: {', '.join(self.declared[factor])}"
                    )
            for factor in self.declared:
                if factor not in assignment:
                    raise where.fail(factor, "is missing; a condition assigns every declared factor")
            levels = tuple(assignment[f] for f in self.declared)
            if levels in seen:
                raise where.fail(None, f"duplicates condition {seen[levels]!r}: both assign the same levels")
            seen[levels] = name

    # --- keys and plans -----------------------------------------------------------------------------------------

    def key(self, condition: str, task: str, model: str, seed: int) -> RunKey:
        """Return the run key of one task, model and seed under a named condition."""
        if condition not in self.conditions:
            raise PlanError(f"unknown condition {condition!r}; the conditions are: {', '.join(self.conditions)}")
        for label, value in (("task", task), ("model", model)):
            if not isinstance(value, str) or not value:
                raise PlanError(f"{label} must be a non-empty string")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise PlanError("seed must be an integer")
        values = {"task": task, "model": model, "seed": seed, **self.conditions[condition]}
        return RunKey(self.order, tuple(values[name] for name in self.order))

    def expand(self, tasks: Iterable[str], models: Iterable[str], seeds: Iterable[int]) -> list[RunKey]:
        """Return the key of every condition x task x model x seed, refusing an empty list or a repeated value."""
        tasks, models, seeds = _distinct("task", tasks), _distinct("model", models), _distinct("seed", seeds)
        keys = [
            self.key(condition, task, model, seed)
            for condition, task, model, seed in itertools.product(self.conditions, tasks, models, seeds)
        ]
        if len({k.text for k in keys}) != len(keys):  # unreachable while conditions differ; kept as the last guard
            raise PlanError("the plan holds a repeated run key")
        return keys

    def condition_of(self, key: RunKey) -> str:
        """Return the name of the condition a key belongs to; refuse a key whose levels fit none."""
        values = key.factors
        try:
            return self._by_levels[tuple(str(values[f]) for f in self.declared)]
        except KeyError:
            raise PlanError(f"run key {key.text!r} fits no condition of the schema") from None

    # --- identity of the schema itself --------------------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """Return the schema as plain data, the form that is fingerprinted and pinned."""
        return {
            "factors": {name: list(levels) for name, levels in self.declared.items()},
            "order": list(self.order),
            "conditions": {name: dict(c) for name, c in self.conditions.items()},
        }

    @cached_property
    def digest(self) -> str:
        """Return the SHA-256 of the schema's canonical JSON."""
        text = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _distinct(label: str, values: Iterable[Any]) -> list[Any]:
    """Return the values as a list, refusing an empty list, a repeated value and (for seeds) a non-integer."""
    out = list(values)
    if not out:
        raise PlanError(f"the plan needs at least one {label}")
    repeated = [v for v in dict.fromkeys(out) if out.count(v) > 1]
    if repeated:
        raise PlanError(f"{label} {repeated[0]!r} appears more than once in the plan")
    return out


def _table(parent: Section, key: str, where: str) -> Section:
    """Return a required sub-table of `parent` named `where` in messages, as TOML writes the table."""
    return Section(parent.section(key).data, parent.file, where)


def load_factor_schema(path: str | os.PathLike[str]) -> FactorSchema:
    """Read the factors, the identity order and the conditions from a study file; other tables are not this loader's."""
    top = Section(load_toml(path), path, "top level")
    declared = {}
    if "factors" in top.data:
        factors = _table(top, "factors", "[factors]")
        for name in factors.data:
            table = _table(factors, name, f"[factors.{name}]")
            table.reject_unknown(["levels"])
            if "levels" not in table.data:
                raise table.fail("levels", "is required")
            declared[name] = table.strings("levels")
    order = _table(top, "identity", "[identity]").strings("order")
    conditions = {}
    all_conditions = _table(top, "conditions", "[conditions]")
    for name in all_conditions.data:
        section = _table(all_conditions, name, f"[conditions.{name}]")
        conditions[name] = {factor: section.string(factor) for factor in section.data}
    return FactorSchema(declared, order, conditions, file=path)
