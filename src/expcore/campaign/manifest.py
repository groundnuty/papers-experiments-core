# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/manifest.py, blob aac5bf109fa04b7a080706e27db2833d118bd18f
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: www-2027-experiments @ 46a5828d, scripts/run_pilot.py:1782-1855
#   merged: www-2027-experiments @ 46a5828d, scripts/run_pilot.py:1413-1645
#   changes: see git history of this file from its import commit
"""The campaign manifest: the plan in a seeded run order, and the fingerprint of the experiment it belongs to.

Order (C08). Each run's place is the SHA-256 of the order seed and its run key, and the plan is sorted by it. A run's
place depends on nothing else, so runs added to the plan never move the runs already in it, and a resume rebuilds the
same order exactly. The order seed and the rule are pinned in the store; a resume under another seed is refused.

Fingerprint (C06). A plain, JSON-ready dictionary of everything that makes two starts one experiment: the factor
schema, the tasks, the models and the seeds (the built-in factors of every run), the caps, the prompt and tool hashes
of each condition, the scorer digest, the hash of every configuration file, the platform's own settings when it
gives any (its TLS mode, say) and, for a study, the environment: the distributions the study's code imports with
their versions and sources (`expcore.provenance.environment`). Only hashes, names, versions and numbers go in, never
a secret or a path of the machine. A resume must present the same fingerprint, and a manifest that is not the plan the fingerprint declares is
refused. The plan may grow, but only by whole tasks, models, seeds or conditions, and only when the caller allows it;
changing or removing anything already recorded is refused.
"""

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from expcore.campaign.factors import FactorSchema, PlanError, RunKey
from expcore.campaign.shard import ModelShards, ShardError, ShardSpec, in_shard, layout_of, pair_of, partition

ORDER_RULE = "sha256(order_seed|run_key)"
_HEX64 = re.compile(r"[0-9a-f]{64}")
# Sections of the fingerprint that may gain whole entries when the plan grows (C08), and the conditions of the schema.
GROWABLE_SECTIONS = ("tasks", "models", "seeds", "prompts", "tools")
_CONDITION_SECTIONS = (("schema", "conditions"), ("schema", "treatment"), ("schema", "condition_tasks"))
_SHOWN_LIMIT = 48


def order_key(order_seed: int, key: RunKey) -> str:
    """Return a run's place in the order: the SHA-256 of the seed and the run key, which depend on nothing else."""
    return hashlib.sha256(f"{order_seed}|{key.text}".encode()).hexdigest()


@dataclass(frozen=True)
class ManifestRow:
    """One planned run and its place in the order."""

    seq: int
    key: RunKey


@dataclass(frozen=True)
class Manifest:
    """The plan in run order, with the seed that made the order."""

    order_seed: int
    rows: tuple[ManifestRow, ...]

    @cached_property
    def plan_sha256(self) -> str:
        """Return a hash of the set of planned runs, whatever their order."""
        return _sha256_lines(sorted(row.key.text for row in self.rows))

    @cached_property
    def order_sha256(self) -> str:
        """Return a hash of the run keys in run order."""
        return _sha256_lines([row.key.text for row in self.rows])


def _sha256_lines(lines: Iterable[str]) -> str:
    """Return the SHA-256 of the lines joined by newlines."""
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def shard_manifest(manifest: Manifest, spec: ShardSpec, layout: ModelShards | None = None) -> Manifest:
    """Return the runs of shard `spec`, each keeping its place in the whole plan and the plan's order (C28, C08).

    The partition is assigned from the whole plan, pair by pair, under the spec's rule (by model with the campaign's
    `[shards]` `layout`, C31), and then each run is kept or dropped: no run is renumbered, so a merge can take the union
    of the shards' runs in the one order the full plan has.
    """
    assignment = partition({pair_of(row.key.factors) for row in manifest.rows}, spec, layout)
    return Manifest(
        manifest.order_seed, tuple(row for row in manifest.rows if in_shard(row.key.factors, spec, assignment))
    )


def build_manifest(keys: Iterable[RunKey], order_seed: int) -> Manifest:
    """Order the planned runs by their seeded place; refuse a run key that appears twice (R11)."""
    if isinstance(order_seed, bool) or not isinstance(order_seed, int):
        raise PlanError("the order seed must be an integer")
    planned = list(keys)
    texts = [key.text for key in planned]
    repeated = [text for text in dict.fromkeys(texts) if texts.count(text) > 1]
    if repeated:
        raise PlanError(f"run key {repeated[0]!r} appears more than once in the plan")
    ordered = sorted(planned, key=lambda key: order_key(order_seed, key))
    return Manifest(order_seed, tuple(ManifestRow(seq, key) for seq, key in enumerate(ordered)))


def relative_order_problem(recorded: Sequence[str], now: Sequence[str]) -> str | None:
    """Describe how the runs two orders share come out in another relative order; None when they agree.

    A plan that grew keeps the order of the runs it shares with the plan before it. If it does not, the remaining
    runs would silently follow a different sequence than the finished ones did.
    """
    shared = set(recorded) & set(now)
    if [key for key in recorded if key in shared] != [key for key in now if key in shared]:
        return f"the {len(shared)} runs this plan shares with the recorded one come out in another relative order"
    return None


# --- the fingerprint -----------------------------------------------------------------------------------------------


def _digest_map(label: str, value: Mapping[str, str], keys: Iterable[str] | None = None) -> dict[str, str]:
    """Return a mapping of names to 64-character hex digests; with `keys`, require exactly those names."""
    if not isinstance(value, Mapping) or not value:
        raise PlanError(f"fingerprint {label} must be a non-empty mapping of names to digests")
    for name, digest in value.items():
        if not isinstance(name, str) or not name or not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise PlanError(f"fingerprint {label}[{name!r}] must be a 64-character lower-case hex digest")
    if keys is not None and set(value) != set(keys):
        missing, extra = sorted(set(keys) - set(value)), sorted(set(value) - set(keys))
        raise PlanError(f"fingerprint {label} must cover exactly the conditions; missing {missing}, unexpected {extra}")
    return dict(value)


def _plain(label: str, value: Any) -> Any:
    """Return a value made only of strings, finite numbers, booleans, None, lists and string-keyed mappings."""
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise PlanError(f"fingerprint {label} has a key that is not a string")
        return {k: _plain(f"{label}.{k}", v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(label, v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise PlanError(f"fingerprint {label} holds a number that is not finite")
    if value is not None and not isinstance(value, str | int | float | bool):
        raise PlanError(f"fingerprint {label} holds a {type(value).__name__}, which is not plain data")
    return value


def build_fingerprint(
    schema: FactorSchema,
    *,
    tasks: Mapping[str, str],
    models: Mapping[str, Any],
    seeds: Iterable[int],
    caps: Mapping[str, Any],
    prompts: Mapping[str, str],
    tools: Mapping[str, str],
    scorer_digest: str,
    configs: Mapping[str, str],
    platform: Mapping[str, Any] | None = None,
    backend_factory: Mapping[str, str] | None = None,
    environment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the fingerprint of an experiment.

    - `tasks`: task id to a digest of the task's content.
    - `models`: model key to a digest of its resolved settings, or to a mapping that holds that digest and the
      model's own caps; the entry is compared whole, so a model that is added later brings its caps with it.
    - `seeds`: the integer seeds of the plan. With `tasks` and `models` they are the built-in factors of every run, so
      a resume cannot change which runs are planned without the fingerprint changing.
    - `caps`: the campaign-wide caps, attempt limit and budgets.
    - `prompts` and `tools`: for each condition, the hash of its prompt template and of its frozen tool manifest.
    - `scorer_digest`: the digest of the scorers a resume must keep.
    - `configs`: the hash of every configuration file, by file name.
    - `platform`: the settings the platform says make two starts one experiment, as plain data without a secret (the
      Onedata layer's is its TLS mode, CA bundle hash and data paths, `PlatformConfig.fingerprint`). It is a section only when
      given and not empty, so a study without a platform keeps the fingerprint it had; no change to it is growth.
    - `backend_factory`: the name and version of the factory a study's plugin builds its backends with. It is a section
      only when given, so a study on the core's factory keeps the fingerprint it had; no change to it is growth.
    - `environment`: the environment the campaign runs in (`expcore.provenance.environment.environment`). A resume under
      another one is refused like any other change: a distribution added, removed, at another version or from another
      source.
    """
    if not isinstance(scorer_digest, str) or not _HEX64.fullmatch(scorer_digest):
        raise PlanError("fingerprint scorer_digest must be a 64-character lower-case hex digest")
    if not isinstance(models, Mapping) or not models or not all(isinstance(k, str) and k for k in models):
        raise PlanError("fingerprint models must be a non-empty mapping of model keys to their settings")
    seed_list = list(seeds)
    if not seed_list or not all(isinstance(x, int) and not isinstance(x, bool) for x in seed_list):
        raise PlanError("fingerprint seeds must be a non-empty list of integers")
    if len(set(seed_list)) != len(seed_list):
        raise PlanError("fingerprint seeds must not repeat")
    fingerprint = {
        "schema": schema.to_json(),
        "tasks": _digest_map("tasks", tasks),
        "models": _plain("models", models),
        "seeds": {str(seed): True for seed in seed_list},
        "caps": _plain("caps", caps),
        "prompts": _digest_map("prompts", prompts, schema.conditions),
        "tools": _digest_map("tools", tools, schema.conditions),
        "scorer_digest": scorer_digest,
        "configs": _digest_map("configs", configs),
    }
    if platform:
        fingerprint["platform"] = _plain("platform", platform)
    if backend_factory:
        fingerprint["backend_factory"] = _plain("backend_factory", backend_factory)
    if environment is not None:
        fingerprint["environment"] = _plain("environment", environment)
    return json.loads(json.dumps(fingerprint, sort_keys=True, allow_nan=False))


def plan_problem(
    schema: FactorSchema, manifest: Manifest, fingerprint: Mapping[str, Any], shard: ShardSpec | None = None
) -> str | None:
    """Describe how a manifest is not the plan its fingerprint declares; None when it is exactly that plan.

    The plan is every condition x task x model x seed of the fingerprint. A manifest that lists other runs would be a
    different experiment whatever the fingerprint says, so a start whose two disagree is refused. A shard's plan is the
    part of that one its pairs are assigned to, with the assignment made from the whole declared plan (C28) under the
    shard's rule, by model with the `[shards]` layout the fingerprint declares (C31).
    """
    keys = schema.expand(fingerprint["tasks"], fingerprint["models"], [int(seed) for seed in fingerprint["seeds"]])
    if shard is not None:
        try:
            assignment = partition({pair_of(key.factors) for key in keys}, shard, layout_of(fingerprint))
        except ShardError as error:
            return str(error)
        keys = [key for key in keys if in_shard(key.factors, shard, assignment)]
    declared = {key.text for key in keys}
    planned = {row.key.text for row in manifest.rows}
    if planned == declared:
        return None
    return (
        f"the manifest lists {len(planned - declared)} run(s) its fingerprint does not declare and omits "
        f"{len(declared - planned)} that it does; the tasks, models, seeds and conditions must agree"
    )


def fingerprint_sha256(fingerprint: Mapping[str, Any]) -> str:
    """Return the SHA-256 of a fingerprint's canonical JSON."""
    text = json.dumps(fingerprint, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _flatten(obj: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Map the path of every leaf of nested mappings to its value; an empty mapping and a list are leaves."""
    if isinstance(obj, dict) and obj:
        out: dict[tuple[str, ...], Any] = {}
        for key, value in obj.items():
            out.update(_flatten(value, (*prefix, str(key))))
        return out
    return {prefix: obj}


def _growth_item(path: tuple[str, ...], recorded_conditions: frozenset[str]) -> tuple[str, ...] | None:
    """Name the whole entry a path belongs to when entries of its section may be added, else None.

    A condition, its treatment declaration and its task list are one entry when the condition is new. A task added to
    the list of a condition that already exists is an entry of its own.
    """
    if len(path) >= 2 and path[0] in GROWABLE_SECTIONS:
        return path[:2]
    if len(path) >= 3 and path[:2] in _CONDITION_SECTIONS:
        if path[1] == "condition_tasks" and len(path) >= 4 and path[2] in recorded_conditions:
            return path[:4]
        return ("schema", "conditions", path[2])
    return None


def _shown(value: Any) -> str:
    """Render a leaf for a message, cut short."""
    text = repr(value)
    return text if len(text) <= _SHOWN_LIMIT else text[: _SHOWN_LIMIT - 3] + "..."


@dataclass(frozen=True)
class FingerprintComparison:
    """How a start's fingerprint differs from the recorded one: what would be refused, and what only grew the plan."""

    refused: tuple[str, ...]
    grown: tuple[str, ...]

    @property
    def same(self) -> bool:
        """Tell whether the two fingerprints are equal."""
        return not self.refused and not self.grown


def compare_fingerprints(recorded: Mapping[str, Any], now: Mapping[str, Any]) -> FingerprintComparison:
    """Compare a fingerprint with the recorded one, leaf by leaf.

    A leaf that changed, a leaf that vanished, and a leaf that appeared inside an entry already recorded are refusals.
    A whole new entry of `tasks`, `models`, `prompts`, `tools` or a new condition of the schema is growth.
    """
    before, after = _flatten(recorded), _flatten(now)
    conditions = frozenset(path[2] for path in before if path[:2] == ("schema", "conditions") and len(path) >= 3)
    known = {item for path in before if (item := _growth_item(path, conditions)) is not None}
    refused: list[str] = []
    grown: set[tuple[str, ...]] = set()
    for path in sorted(set(before) | set(after)):
        name = ".".join(path)
        if path in before and path in after:
            if before[path] != after[path]:
                refused.append(f"{name}: recorded {_shown(before[path])}, now {_shown(after[path])}")
        elif path in before:
            refused.append(f"{name}: recorded {_shown(before[path])}, now absent")
        else:
            item = _growth_item(path, conditions)
            if item is not None and item not in known:
                grown.add(item)
            else:
                refused.append(f"{name}: absent when the campaign started, now {_shown(after[path])}")
    return FingerprintComparison(tuple(refused), tuple(".".join(item) for item in sorted(grown)))
