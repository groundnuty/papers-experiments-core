# Provenance: new
"""Sharding by model and then by run (C32, `expcore-shard/5`): any number of shards per model, up to its runs.

A campaign that declares `[shards] per_model` with `by = "run"` maps shard i to one model exactly as `expcore-shard/3`
does (`ModelShards.at`), and deals that model's runs, not its pairs, among the model's own F shards:

- **An arm** is one condition of the plan, known by its declared factor values (`arm_of`): the factors of a run other
  than task, model and seed. Two conditions never assign the same levels (C18), so an arm is a condition, whatever it is
  named, and a factor contrast's level is a set of arms.
- **The deal.** The model's arms are ranked by the SHA-256 of their factor values; each arm's runs by the SHA-256 of
  their pair, `json [task, model, seed]`, as `assign` ranks pairs. The runs are dealt in that one order, arm after arm,
  round the model's shards from the starting shard that the model's own hash picks (as in `assign`): each arm starts
  where the one before it stopped.
- **What it gives.** Each arm's runs are consecutive in the deal, so every shard of the model gets the floor or the
  ceiling of each arm's runs over F, and the floor or the ceiling of all the model's runs. An arm is in min(its runs, F)
  of the shards: a shard lacks an arm, or holds one arm only, only when F is above that arm's runs. With two arms of n
  runs each, a shard's two counts are equal when it holds an even number of runs and differ by one when odd, so no
  shard (no federation) coincides with an arm while the counts allow both. F may be any number from 1 to the model's
  runs; a shard with no run (F above them) is refused at its start.
- **Pairs.** The two runs of a pair are in one shard only when the earlier arms' runs sum to a multiple of F; otherwise
  they are on two shards, and the shard is a factor crossed with the arms, balanced as above, never a block within a
  pair. Each run's shard is recorded where every shard's is: its store's spec, and in a merged export each entry's
  `"shard"` with merge.json's list of labels (`campaign.export_join`), so an analysis can look for a shard's effect.
- **One assignment everywhere.** The deal depends on the set of the plan's runs and the layout only: never on the
  order of the plan, a condition's name or the order of the declared factors. So every shard, a merge and the plan a
  fingerprint declares compute the same one, and the assignment depends on the whole plan, which cannot grow on resume.

`owners` gives the shard of each run of the whole plan under any rule, by run key: under `expcore-shard/2` and `/3` from
the pair's shard (`shard.partition`), under `/5` from this deal. A run outside the plan has none.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from expcore.campaign.shard import (
    BY_RUN,
    RULE_BY_RUN,
    ModelShards,
    ShardError,
    ShardSpec,
    assign_by_model,
    check_layout,
    pair_of,
    partition,
)

BUILT_IN = ("task", "model", "seed")
"""The factors every run has; the others are its arm's."""


def _digest(*parts: Any) -> str:
    """The SHA-256 of the parts' canonical JSON, as `shard.assign` hashes a pair and a model."""
    text = json.dumps(list(parts), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def arm_of(factors: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    """A run's arm: its declared factor values, by name, sorted by name."""
    return tuple(sorted((name, value) for name, value in factors.items() if name not in BUILT_IN))


def assign_by_run(runs: Mapping[str, Mapping[str, Any]], layout: ModelShards) -> dict[str, int]:
    """The 1-based shard of every run of the whole plan under `expcore-shard/5`, by run key; see the module docstring.

    `runs` maps each run key of the whole plan to its factors. A model of the plan without a count is refused: its runs
    would be in no shard.
    """
    by_model: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for key, factors in runs.items():
        arms = by_model.setdefault(factors["model"], {})
        arms.setdefault(_digest("arm", [list(item) for item in arm_of(factors)]), []).append(
            (_digest(*pair_of(factors)), key)
        )
    declared = [model for model, _ in layout.per_model]
    unknown = sorted(set(by_model) - set(declared))
    if unknown:
        raise ShardError(f"model {unknown[0]!r} has runs in the plan and no count in [shards] per_model")
    assignment: dict[str, int] = {}
    first = 0
    for model, count in layout.per_model:
        arms = by_model.get(model, {})
        dealt = [key for arm in sorted(arms) for _, key in sorted(arms[arm])]
        start = int(_digest("model", model), 16) % count
        for place, key in enumerate(dealt):
            assignment[key] = first + (start + place) % count + 1
        first += count
    return assignment


def shards_of_layout(runs: Mapping[str, Mapping[str, Any]], layout: ModelShards) -> dict[str, int]:
    """The 1-based shard of every run of the whole plan by run key, under the layout's rule (`/3` or `/5`)."""
    if layout.by == BY_RUN:
        return assign_by_run(runs, layout)
    by_pair = assign_by_model({pair_of(factors) for factors in runs.values()}, layout)
    return {key: by_pair[pair_of(factors)] for key, factors in runs.items()}


@dataclass(frozen=True)
class RunOwners:
    """The shard that owns each run of the whole plan under one rule: by its pair, or by its run key under `/5`."""

    by_pair: Mapping[tuple[str, str, int], int] | None
    by_key: Mapping[str, int] | None

    def of(self, key: str, factors: Mapping[str, Any]) -> int | None:
        """The index of the shard that owns the run; None for a run outside the plan (under `/2` and `/3`, one whose
        task, model and seed are no pair of the plan)."""
        if self.by_key is not None:
            return self.by_key.get(key)
        assert self.by_pair is not None  # `owners` sets one of the two
        return self.by_pair.get(pair_of(factors))


def owners(runs: Mapping[str, Mapping[str, Any]], spec: ShardSpec, layout: ModelShards | None) -> RunOwners:
    """Who owns each run of the whole plan (`runs`, by run key) under `spec`'s rule, with the campaign's layout.

    A spec under `expcore-shard/5` needs a layout by run with N its sum; the other rules are `shard.partition`'s, which
    refuses a spec of `/2` or `/3` beside a layout by run.
    """
    if spec.rule == RULE_BY_RUN:
        return RunOwners(None, assign_by_run(runs, check_layout(spec, layout)))
    return RunOwners(partition({pair_of(factors) for factors in runs.values()}, spec, layout), None)


__all__ = ["BUILT_IN", "RunOwners", "arm_of", "assign_by_run", "owners", "shards_of_layout"]
