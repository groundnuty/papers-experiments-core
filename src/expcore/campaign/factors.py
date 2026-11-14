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

A condition may also declare which infrastructure failures are its treatment:

    [conditions.provider-down]
    data_path = "api"
    infrastructure_is_treatment = ["endpoint"]

Normally an attempt that ends as infrastructure is set aside and run again, because the fault says nothing about the
agent. When a condition's manipulation is the fault itself (a provider that is down), running again until the run
looks healthy would remove the effect being measured. Under such a condition an attempt that ends as infrastructure
of a listed kind is a scored final attempt: the store marks it as treatment, never runs it again and does not charge
it against the infrastructure limit. Other kinds under the same condition are set aside and run again as usual. The
kinds are checked against the registry in `expcore.errors` (the core's, a platform's, a study's own), and
the declaration is part of the schema, so changing it between a start and a resume is refused (C06). A kind the
registry marks as not treatable (`tool_plane_integrity` and any a platform registered so) is refused when the
schema is loaded, naming the file and the key: a run that failed an integrity check of the harness says nothing about
the manipulation, so it can never be scored as the condition's fault. The scheduler refuses, at its start, a health
or identity kind a condition declares as treatment for the same reason (`refused_treatment`).

A kind may be declared with the target the condition manipulates, as a table beside the plain names:

    [conditions.provider-down]
    data_path = "api"
    infrastructure_is_treatment = [{ kind = "provider_outage", target = "providers[1]" }]

The target is a position label from the platform's configuration, never a configured name. Then only a failure of that
kind that names that target, while the run's fault injector held the target down in the attempt, is the condition's
treatment; another target's failure, a failure that names none and an injection that did not happen are set aside
(`Outcome.is_treatment`, record version 3). A plain name declares the kind with no target: any failure of the kind is
the treatment, whatever it names, except for a kind that is a fault on one target (`expcore.errors.targeted_kinds`,
registered with `requires_target=True`, such as `provider_outage`). That kind is refused by its plain name, naming the
file, the condition and the kind, so a schema cut to the kind names alone cannot be built for it and no other target's
failure is ever scored as the manipulation (C34). The targets are part of the schema too (`treatment_targets`).

A condition may run exclusive, with no fault of its own:

    [conditions.matched-control]
    data_path = "api"
    exclusive = true

An outage run holds a shared platform (a federation) exclusively, so it never shares it with another run; its control
shares it with whatever else runs. `exclusive = true` asks the platform's run admission to hold it exclusively for this
condition's runs too, so a study can run a matched control under the same isolation. It must be `true` or `false`; a
campaign whose platform gives no run admission refuses a condition that declares it (`expcore.campaign.scheduler`).
The setting is part of the schema, and a schema that declares no condition exclusive has the digest it had before the
setting existed.

A condition may also name the tasks it runs on:

    [conditions.chain-c]
    data_path = "download"
    tasks = ["task-a", "task-b"]

By default a condition runs on every task of the plan. A chain design, where an arm is run on a subset, names the
subset. Each listed task must be a task of the plan, which is checked when the plan is expanded, and the plan expands
a condition over its own tasks only. The list is part of the schema, so changing it between a start and a resume is
refused, while adding tasks to a condition is growth (C06, C08).
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
from expcore.errors import TARGET_LABEL, treatment_vocabulary

BUILTIN_FACTORS = ("task", "model", "seed")
TREATMENT_KEY = "infrastructure_is_treatment"
TASKS_KEY = "tasks"
EXCLUSIVE_KEY = "exclusive"
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
        treatment: Mapping[str, Sequence[str | Mapping[str, str]]] | None = None,
        tasks: Mapping[str, Sequence[str]] | None = None,
        exclusive: Mapping[str, bool] | None = None,
        file: str | os.PathLike[str] = "<schema>",
    ) -> None:
        """Validate and keep copies of the declared levels, the identity order, the conditions and their settings.

        `treatment` maps a condition to the infrastructure kinds that are its treatment, each a kind's name or a table
        `{kind, target}` naming the target the condition manipulates, and `tasks` maps a condition to the tasks it runs
        on; a condition not in `tasks` runs on every task, and `exclusive` maps a condition to whether its runs hold the
        platform exclusively (see the module docstring).
        """
        self.file = str(file)
        self.declared: dict[str, tuple[str, ...]] = {name: tuple(levels) for name, levels in declared.items()}
        self.order = tuple(order)
        self.conditions: dict[str, dict[str, str]] = {name: dict(c) for name, c in conditions.items()}
        self._check_factors()
        self._check_order()
        self._check_conditions()
        self.treatment_targets: dict[str, dict[str, str]] = {name: {} for name in self.conditions}
        self.treatment_kinds: dict[str, tuple[str, ...]] = self._check_treatment(treatment or {})
        self.condition_tasks: dict[str, tuple[str, ...] | None] = self._check_condition_tasks(tasks or {})
        self.exclusive: dict[str, bool] = self._check_exclusive(exclusive or {})
        self._by_levels = {tuple(c[f] for f in self.declared): name for name, c in self.conditions.items()}

    # --- validation ---------------------------------------------------------------------------------------------

    def _check_factors(self) -> None:
        """Refuse a badly named, built-in or level-less factor."""
        for name, levels in self.declared.items():
            where = Section({"levels": list(levels)}, self.file, f"[factors.{name}]")
            if name in BUILTIN_FACTORS:
                raise Section({}, self.file, "[factors]").fail(name, "is built in and cannot be declared")
            if name in (TREATMENT_KEY, TASKS_KEY, EXCLUSIVE_KEY):
                raise Section({}, self.file, "[factors]").fail(name, "is reserved for a condition's setting")
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
                check_condition_key(where, factor, self.declared)
                where.string(factor)  # then the value: a non-empty string, named by its type when it is not one
                if level not in self.declared[factor]:
                    raise where.fail(
                        factor, f"has unknown level {level!r}; levels of {factor!r}: {', '.join(self.declared[factor])}"
                    )
            for factor in self.declared:
                if factor not in assignment:
                    levels = ", ".join(self.declared[factor])
                    raise where.fail(
                        factor, f"is missing; a condition assigns every declared factor; levels of {factor!r}: {levels}"
                    )
            levels = tuple(assignment[f] for f in self.declared)
            if levels in seen:
                raise where.fail(None, f"duplicates condition {seen[levels]!r}: both assign the same levels")
            seen[levels] = name

    def _check_treatment(self, declared: Mapping[str, Sequence[str | Mapping[str, str]]]) -> dict[str, tuple[str, ...]]:
        """Check each declared treatment against the registry of kinds; return every condition's sorted kinds.

        A table entry's target goes into `treatment_targets`, by condition and kind.
        """
        out: dict[str, tuple[str, ...]] = dict.fromkeys(self.conditions, ())
        # one view for the whole schema: a scope leaving cannot split two reads
        known, untreatable, targeted = treatment_vocabulary()
        for name, entries in declared.items():
            if name not in self.conditions:
                raise Section({}, self.file, "[conditions]").fail(name, f"declares {TREATMENT_KEY} but is no condition")
            where = Section({}, self.file, f"[conditions.{name}]")
            kinds, targets = self._treatment_entries(where, tuple(entries))
            for kind in kinds:
                if kind not in known:
                    treatable = [k for k in known if k not in untreatable]
                    raise where.fail(
                        TREATMENT_KEY,
                        f"names unknown infrastructure kind {kind!r}; treatable kinds: {', '.join(treatable)}; "
                        "a platform's kinds are known once study.toml lists its module in [study] platforms, and a "
                        "study declares its own in [study.kinds]",
                    )
                if kind in untreatable:
                    raise where.fail(
                        TREATMENT_KEY,
                        f"names infrastructure kind {kind!r}, which can never be a treatment: {untreatable[kind]}",
                    )
            repeated = sorted({k for k in kinds if kinds.count(k) > 1})
            if repeated:
                raise where.fail(TREATMENT_KEY, f"repeats {', '.join(map(repr, repeated))}")
            for kind in kinds:
                if kind in targeted and kind not in targets:
                    raise where.fail(
                        TREATMENT_KEY,
                        f"declares infrastructure kind {kind!r} with no target; a condition whose treatment is "
                        f"{kind!r} must say which target it manipulates, or any target's failure would be scored as "
                        f'its treatment: write {{ kind = "{kind}", target = "providers[1]" }}, the position label of '
                        "that target in the platform's configuration",
                    )
            out[name] = tuple(sorted(kinds))
            self.treatment_targets[name] = dict(sorted(targets.items()))
        return out

    @staticmethod
    def _treatment_entries(
        where: Section, entries: tuple[str | Mapping[str, str], ...]
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        """Split a condition's treatment entries into its kinds and the targets the table entries name."""
        kinds: list[str] = []
        targets: dict[str, str] = {}
        for entry in entries:
            if isinstance(entry, str) and entry:
                kinds.append(entry)
                continue
            if not isinstance(entry, Mapping) or set(entry) != {"kind", "target"}:
                raise where.fail(
                    TREATMENT_KEY, "must be an array of infrastructure kinds, each a name or a table {kind, target}"
                )
            kind, target = entry["kind"], entry["target"]
            if not isinstance(kind, str) or not kind:
                raise where.fail(TREATMENT_KEY, "a table entry's kind must be an infrastructure kind")
            if not isinstance(target, str) or not TARGET_LABEL.fullmatch(target):
                raise where.fail(
                    TREATMENT_KEY,
                    "a table entry's target must be a position label such as 'providers[1]', never a configured name",
                )
            kinds.append(kind)
            targets[kind] = target
        return tuple(kinds), targets

    def refused_treatment(self, kinds: Mapping[str, str]) -> tuple[str, str] | None:
        """Return the first (condition, kind) whose declared treatment is one of `kinds`, or None.

        `kinds` maps a kind to the reason it cannot be a treatment here. The registry's untreatable kinds are
        refused when the schema is built; a campaign adds the kinds it charges its own failures to (the scheduler's
        health and identity kinds), which are untreatable for that campaign.
        """
        for condition, declared in self.treatment_kinds.items():
            for kind in declared:
                if kind in kinds:
                    return condition, kind
        return None

    def _check_condition_tasks(self, declared: Mapping[str, Sequence[str]]) -> dict[str, tuple[str, ...] | None]:
        """Check each condition's task list; return it by condition, None where the condition runs on every task."""
        out: dict[str, tuple[str, ...] | None] = dict.fromkeys(self.conditions)
        for name, listed in declared.items():
            if name not in self.conditions:
                raise Section({}, self.file, "[conditions]").fail(name, f"declares {TASKS_KEY} but is no condition")
            where = Section({}, self.file, f"[conditions.{name}]")
            listed = tuple(listed)
            if not listed or not all(isinstance(t, str) and t for t in listed):
                raise where.fail(TASKS_KEY, "must be a non-empty array of task names")
            repeated = sorted({t for t in listed if listed.count(t) > 1})
            if repeated:
                raise where.fail(TASKS_KEY, f"repeats {', '.join(map(repr, repeated))}")
            out[name] = listed
        return out

    def _check_exclusive(self, declared: Mapping[str, bool]) -> dict[str, bool]:
        """Check each condition's exclusive setting; return it by condition, False where none is declared."""
        out = dict.fromkeys(self.conditions, False)
        for name, value in declared.items():
            if name not in self.conditions:
                raise Section({}, self.file, "[conditions]").fail(name, f"declares {EXCLUSIVE_KEY} but is no condition")
            if not isinstance(value, bool):
                raise Section({}, self.file, f"[conditions.{name}]").fail(EXCLUSIVE_KEY, "must be true or false")
            out[name] = value
        return out

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
        listed = self.condition_tasks[condition]
        if listed is not None and task not in listed:
            raise PlanError(f"task {task!r} is not one of the tasks of condition {condition!r}: {', '.join(listed)}")
        values = {"task": task, "model": model, "seed": seed, **self.conditions[condition]}
        return RunKey(self.order, tuple(values[name] for name in self.order))

    def expand(self, tasks: Iterable[str], models: Iterable[str], seeds: Iterable[int]) -> list[RunKey]:
        """Return the key of every condition x task x model x seed, refusing an empty list or a repeated value.

        A condition is expanded over its own tasks only (the plan's tasks, in the plan's order, when it names none).
        A task a condition names that is not a task of the plan is refused, naming the file and the key.
        """
        tasks, models, seeds = _distinct("task", tasks), _distinct("model", models), _distinct("seed", seeds)
        keys = [
            self.key(condition, task, model, seed)
            for condition in self.conditions
            for task, model, seed in itertools.product(self.tasks_of(condition, tasks), models, seeds)
        ]
        if len({k.text for k in keys}) != len(keys):  # unreachable while conditions differ; kept as the last guard
            raise PlanError("the plan holds a repeated run key")
        return keys

    def tasks_of(self, condition: str, plan_tasks: Sequence[str]) -> list[str]:
        """Return the tasks a condition runs on within a plan: its own list, or all of the plan's tasks."""
        listed = self.condition_tasks[condition]
        if listed is None:
            return list(plan_tasks)
        unknown = [t for t in listed if t not in plan_tasks]
        if unknown:
            raise Section({}, self.file, f"[conditions.{condition}]").fail(
                TASKS_KEY, f"names unknown task {unknown[0]!r}; the plan's tasks are: {', '.join(plan_tasks)}"
            )
        return [t for t in plan_tasks if t in listed]

    def check_key(self, key: RunKey) -> str:
        """Return the condition of a key; refuse a key no condition runs, by its levels or by its task."""
        condition = self.condition_of(key)
        listed = self.condition_tasks[condition]
        if listed is not None and key.factors["task"] not in listed:
            raise PlanError(f"run key {key.text!r} is on a task that condition {condition!r} does not run")
        return condition

    def treatment_targets_of(self, key: RunKey) -> dict[str, str]:
        """Return the target the condition of a run key declares for any of its treatment kinds."""
        return self.treatment_targets[self.condition_of(key)]

    def treatment_kinds_of(self, key: RunKey) -> tuple[str, ...]:
        """Return the infrastructure kinds declared as treatment by the condition of a run key.

        The campaign passes these to `build_record` and `record_attempt`, so the record and the store agree on which
        infrastructure failures are scored.
        """
        return self.treatment_kinds[self.condition_of(key)]

    def exclusive_of(self, key: RunKey) -> bool:
        """Return whether the condition of a run key holds the platform exclusively."""
        return self.exclusive[self.condition_of(key)]

    def condition_of(self, key: RunKey) -> str:
        """Return the name of the condition a key belongs to; refuse a key whose levels fit none."""
        return self.condition_named(key.factors, key.text)

    def condition_named(self, factors: Mapping[str, str | int], label: str = "") -> str:
        """Return the condition whose levels the factor values carry; `label` names the run in the refusal."""
        try:
            return self._by_levels[tuple(str(factors[f]) for f in self.declared)]
        except KeyError:
            raise PlanError(f"run key {label!r} fits no condition of the schema") from None

    # --- identity of the schema itself --------------------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """Return the schema as plain data, the form that is fingerprinted and pinned. The exclusive conditions are in
        it only when there are any, so a schema without them has the form, and the digest, it had before."""
        exclusive = sorted(name for name, value in self.exclusive.items() if value)
        return {
            **({EXCLUSIVE_KEY: exclusive} if exclusive else {}),
            "factors": {name: list(levels) for name, levels in self.declared.items()},
            "order": list(self.order),
            "conditions": {name: dict(c) for name, c in self.conditions.items()},
            "treatment": {name: self._treatment_json(name) for name in self.treatment_kinds},
            "condition_tasks": {
                name: None if listed is None else dict.fromkeys(listed, True)
                for name, listed in self.condition_tasks.items()
            },
        }

    def _treatment_json(self, condition: str) -> list[Any]:
        """A condition's declared treatment as plain data: a kind's name, or `{kind, target}` where a target is
        declared, so a schema that declares no target has the form, and the digest, it had before targets existed."""
        targets = self.treatment_targets[condition]
        return [
            {"kind": kind, "target": targets[kind]} if kind in targets else kind
            for kind in self.treatment_kinds[condition]
        ]

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


def check_condition_key(where: Section, factor: str, declared: Mapping[str, Any]) -> None:
    """Refuse a key of a condition that is not a factor it may assign: a built-in one, or one the study never declared.

    It runs before the key's value is typed, so a list on a key that is no factor (`seeds = [1]`) is refused for the
    key, and the message never suggests that a string would be accepted.
    """
    if factor in BUILTIN_FACTORS:
        raise where.fail(factor, "is built in; a condition assigns declared factors only")
    if factor not in declared:
        raise where.fail(factor, f"is not a declared factor; declared: {', '.join(declared) or 'none'}")


def _table(parent: Section, key: str, where: str) -> Section:
    """Return a required sub-table of `parent` named `where` in messages, as TOML writes the table."""
    return Section(parent.section(key).data, parent.file, where)


def _treatment_value(section: Section) -> tuple[Any, ...]:
    """Return a condition's treatment entries as written: names and `{kind, target}` tables; absent is none."""
    value = section.data.get(TREATMENT_KEY, [])
    if not isinstance(value, list):
        raise section.fail(
            TREATMENT_KEY, "must be an array of infrastructure kinds, each a name or a table {kind, target}"
        )
    return tuple(dict(v) if isinstance(v, Mapping) else v for v in value)


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
    conditions, treatment, tasks, exclusive = {}, {}, {}, {}
    settings = (TREATMENT_KEY, TASKS_KEY, EXCLUSIVE_KEY)
    all_conditions = _table(top, "conditions", "[conditions]")
    for name in all_conditions.data:
        section = _table(all_conditions, name, f"[conditions.{name}]")
        # As written: the schema checks each key as a factor before it checks the value's type, after the factors
        conditions[name] = {f: v for f, v in section.data.items() if f not in settings}
        treatment[name] = _treatment_value(section)
        if TASKS_KEY in section.data:
            tasks[name] = section.strings(TASKS_KEY)
        if EXCLUSIVE_KEY in section.data:
            exclusive[name] = section.data[EXCLUSIVE_KEY]
    return FactorSchema(declared, order, conditions, treatment=treatment, tasks=tasks, exclusive=exclusive, file=path)
