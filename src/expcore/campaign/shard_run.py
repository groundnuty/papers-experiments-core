# Provenance: new
"""What one shard of a campaign pins in its store at its first start, and what a resume of it must keep (#154).

`campaign.shard` names the partition, the shares and the identity check; this module is the run side that uses them.
A start with a `ShardStart` pins four facts in the store's meta table inside the start's own transaction, as the order
seed is pinned: the spec (`META_SHARD`), when the shard first started (`META_STARTED`), the environment identity it
ran against (`META_ENVIRONMENT`) and its shares of the caps and the budget (`META_SHARES`), the last three read from
the plugin and the settings once, when the shard starts. A later start of the same store must give the same spec, the
same identity and the same shares; it never moves the first start's time.

A resume refuses (`ShardMismatch`), naming the difference and leaving the store as it was: another spec, label or rule;
a sharded store resumed without a shard, and an unsharded one resumed with a shard; a changed environment identity;
changed shares. A start without a shard reads one meta row and writes nothing, so an unsharded campaign's store is what
it was before shards existed.

The totals stay in the campaign fingerprint, which every shard shares; the shares are applied to the settings after it
is computed (`share_settings`, or `share_settings_by_model` for a campaign that shards by model, C32), so they never
reach it. Before any share is taken, a shard holding pairs of a model whose concurrency share is 0 is refused, naming
the model (`refuse_unrunnable`).

The fingerprint also holds the environment of the shard's host: its Python and the installed distributions it
imports through, and the shards of one campaign must have one fingerprint. A host whose environment differs would be
refused only when its shard is merged, after it ran, so `environment_block` says it as the shard starts: the
environment's digest, which every host of the campaign must print alike, and its distributions.
"""

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from expcore.campaign.attempts import ResumeRefused
from expcore.campaign.manifest import fingerprint_sha256
from expcore.campaign.shard import (
    META_ENVIRONMENT,
    META_SHARD,
    META_SHARES,
    META_STARTED,
    RULE_BY_MODEL,
    ModelShards,
    ShardError,
    ShardSpec,
    budget_shares,
    caps_shares,
    is_default_label,
    part,
)
from expcore.campaign.store import Store

if TYPE_CHECKING:
    from expcore.campaign.settings import SchedulerConfig

PINNED = (META_SHARD, META_ENVIRONMENT, META_SHARES)  # compared at every start; META_STARTED is only ever written once
GROWTH_HINT = (
    " (a sharded campaign's plan cannot grow: the shards' assignment of pairs depends on the whole plan; "
    "start new stores for a new plan)"
)
_DIGEST_SHOWN = 12
LIST_LIMIT = 60
"""Up to this many distributions the start lists each with its version; more, and each with a short digest."""


class ShardMismatch(ResumeRefused):
    """A start's shard, environment identity or shares differ from what the store recorded at its first start."""


@dataclass(frozen=True)
class ShardStart:
    """A shard's start: its spec, the environment identity it reads, checked, and the shares of the caps and budget."""

    spec: ShardSpec
    environment: Mapping[str, str]
    shares: Mapping[str, Any]  # {"unit_caps": {unit: n}, "budget": {"max_usd": x, "per_model_usd": {...}} or None}

    def pinned(self) -> dict[str, str]:
        """The three facts a start compares, by meta key, as canonical JSON text."""
        return {
            META_SHARD: canonical(self.spec.to_json()),
            META_ENVIRONMENT: canonical(dict(self.environment)),
            META_SHARES: canonical(self.shares),
        }


def canonical(value: Any) -> str:
    """Sorted, compact JSON: the one text a pinned fact is kept and compared as."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def share_settings(settings: "SchedulerConfig", spec: ShardSpec) -> tuple["SchedulerConfig", dict[str, Any]]:
    """Shard `spec`'s settings: each unit's cap and the budget's ceilings replaced by its shares of those totals (C30).

    Returns them with the shares as the store and the export keep them. A total below the shard count raises
    `ShardError`, naming the key it came from. The release step and file of a budget are the host's own and stay.
    """
    caps = caps_shares(settings.unit_caps, spec)
    budget = settings.budget
    if budget is None:
        return dataclasses.replace(settings, unit_caps=caps), {"unit_caps": caps, "budget": None}
    parts = budget_shares(budget.max_usd, budget.per_model_usd, spec)
    shared = dataclasses.replace(budget, max_usd=parts["max_usd"], per_model_usd=parts["per_model_usd"])
    return dataclasses.replace(settings, unit_caps=caps, budget=shared), {"unit_caps": caps, "budget": parts}


def share_settings_by_model(
    settings: "SchedulerConfig", spec: ShardSpec, layout: ModelShards, unit: str, budget: Mapping[str, Any] | None
) -> tuple["SchedulerConfig", dict[str, Any]]:
    """Shard `spec`'s settings under `expcore-shard/3` (C32), with the shares as the store and the export keep them.

    The shard runs one run at a time: its cap is 1 on `unit`, its model's, and it runs nothing of any other unit.
    `budget` is its part of the budget (`shard_budget.shard_budgets`), None without one. A shard whose part of
    `max_usd` is 0 runs a model that costs nothing, which no gate can hold for cost, so it runs without one: a `Budget`
    of 0 dollars holds every run.
    """
    if spec.rule != RULE_BY_MODEL or layout.count != spec.count:
        raise ShardError(f"shard {spec.index} of {spec.count} is not a shard by model of this layout ({spec.rule!r})")
    caps = {unit: 1}
    if settings.budget is None or budget is None:
        return dataclasses.replace(settings, unit_caps=caps, budget=None), {"unit_caps": caps, "budget": None}
    parts = {"max_usd": budget["max_usd"], "per_model_usd": dict(budget["per_model_usd"])}
    shared = None
    if parts["max_usd"] > 0:
        shared = dataclasses.replace(settings.budget, max_usd=parts["max_usd"], per_model_usd=parts["per_model_usd"])
    return dataclasses.replace(settings, unit_caps=caps, budget=shared), {"unit_caps": caps, "budget": parts}


def refuse_unrunnable(
    spec: ShardSpec,
    pairs: Mapping[str, int],
    unit_of: Mapping[str, str],
    unit_caps: Mapping[str, int],
    layout: ModelShards | None = None,
) -> None:
    """Refuse a shard that holds pairs of a model whose concurrency share is 0 (C32): it could never run them.

    `pairs` counts the shard's pairs by model, `unit_of` gives each model's unit and `unit_caps` the campaign's totals.
    Under `expcore-shard/2` a model's share is the shard's part of its unit's cap, which is 0 for the shards past the
    cap when the cap is below N (an uncapped unit has no share to lack); under `expcore-shard/3`, with the campaign's
    `layout`, it is 1 for the shard's own model and 0 for every other. Raises ShardError naming the model.
    """
    for model, held in sorted(pairs.items()):
        if spec.rule == RULE_BY_MODEL:
            serves = layout.at(spec.index)[0] if layout is not None else None
            if model == serves:
                continue
            why = f"shard {spec.index} serves model {serves} only (expcore-shard/3)"
        else:
            unit = unit_of[model]
            cap = unit_caps.get(unit)
            if cap is None or part(cap, spec.index, spec.count, what=unit, allow_zero=True) > 0:
                continue
            why = f"[scheduler.unit_caps] {unit} = {cap} over {spec.count} shards"
        raise ShardError(
            f"shard {spec.index} of {spec.count} holds {held} pair(s) of model {model}, and its share of that model's "
            f"concurrency is 0 ({why}), so it could never run them; shard by model with [shards] per_model "
            "(expcore-shard/3), or use fewer shards"
        )


def environment_block(spec: ShardSpec, environment: Mapping[str, Any]) -> str:
    """The environment part of the campaign fingerprint, as one block of text a shard prints as it starts.

    The first line gives the whole environment's SHA-256 (the digest `fingerprint_sha256` makes of the section, which
    holds the sources of the distributions installed from a URL), the Python and the number of distributions; each
    later line is one distribution. Up to `LIST_LIMIT` of them are listed with their versions. A longer list shows
    each with the first 12 characters of the SHA-256 of its version and its source instead, so that two hosts' blocks
    compare line by line, and a distribution that differs only in where it came from still shows. Nothing of a source
    is printed (no URL, commit or path of this machine), and no path or host name enters the block.
    """
    versions = environment["distributions"]
    sources = environment.get("sources", {})
    lines = [
        f"run: shard {spec.index} of {spec.count}: environment sha256 {fingerprint_sha256(environment)}, "
        f"python {environment['python']}, {len(versions)} distributions "
        "(every shard of the campaign must print this same digest)"
    ]
    for name in sorted(versions):
        if len(versions) <= LIST_LIMIT:
            lines.append(f"  {name} {versions[name]}")
        else:
            part = hashlib.sha256(canonical([versions[name], sources.get(name)]).encode("utf-8")).hexdigest()
            lines.append(f"  {name} {part[:_DIGEST_SHOWN]}")
    return "\n".join(lines)


def pin_shard(store: Store, shard: ShardStart | None, started: bool, now: str) -> None:
    """Pin a first start's shard facts, or compare a later start's with them; inside the caller's transaction.

    `started` says the store has been started before (it records a fingerprint). Without a shard this reads one row
    and writes nothing. Raises ShardMismatch for each refusal above.
    """
    rows = store.db.execute("SELECT key, value FROM meta WHERE key IN (?,?,?,?)", (*PINNED, META_STARTED)).fetchall()
    held = {row["key"]: row["value"] for row in rows}
    if shard is None:
        if META_SHARD in held:
            recorded = json.loads(held[META_SHARD])
            raise ShardMismatch(
                f"this store belongs to shard {recorded['index']} of {recorded['count']} ({recorded['label']!r}) of a "
                f"sharded campaign, and this start gives no shard: resume it with --shard "
                f"{recorded['index']}/{recorded['count']} --shard-label {recorded['label']}{_wide_flag(recorded)}"
            )
        return
    if META_SHARD not in held:
        if started:
            raise ShardMismatch(
                "this store holds an unsharded campaign, and this start gives a shard: a campaign is sharded from "
                "its first start (start a new store for the shard)"
            )
        for key, text in shard.pinned().items():
            store.db.execute("INSERT INTO meta(key,value,set_utc) VALUES(?,?,?)", (key, text, now))
        store.db.execute("INSERT INTO meta(key,value,set_utc) VALUES(?,?,?)", (META_STARTED, now, now))
        return
    recorded_spec = json.loads(held[META_SHARD])
    differences = {
        META_SHARD: _spec_differences(recorded_spec, shard.spec.to_json()),
        META_ENVIRONMENT: _identity_differences(json.loads(held.get(META_ENVIRONMENT, "{}")), shard.environment),
        META_SHARES: _share_differences(json.loads(held.get(META_SHARES, "null")), shard.shares),
    }
    refused = {key: lines for key, lines in differences.items() if lines}
    if not refused:
        return
    first = next(iter(refused))
    lines = "\n  ".join(line for found in refused.values() for line in found)
    raise ShardMismatch(f"{_REASONS[first].format(**recorded_spec)}:\n  {lines}")


def _wide_flag(recorded: Mapping[str, Any]) -> str:
    """` --shard-label-wide` when the recorded label has a '.' or '_' that only that flag lets a label have."""
    return "" if is_default_label(str(recorded["label"])) else " --shard-label-wide"


_REASONS = {
    META_SHARD: (
        "this store started as shard {index} of {count} ({label!r}), and this start gives another spec, label or "
        "rule: a shard resumes only as itself"
    ),
    META_ENVIRONMENT: (
        "this shard's environment identity changed since its first start (shard {index} of {count}): it would no "
        "longer have run against what its first runs did"
    ),
    META_SHARES: (
        "this shard's shares of the caps and the budget changed since its first start (shard {index} of {count}): "
        "they are fixed when it starts, so that the shards' shares never sum past the totals"
    ),
}


def _spec_differences(recorded: Mapping[str, Any], now: Mapping[str, Any]) -> list[str]:
    return [
        f"{name}: recorded {recorded.get(name)!r}, now {now.get(name)!r}"
        for name in ("rule", "index", "count", "label")
        if recorded.get(name) != now.get(name)
    ]


def _identity_differences(recorded: Mapping[str, str], now: Mapping[str, str]) -> list[str]:
    def shown(digest: str | None) -> str:
        return "absent" if digest is None else digest[:_DIGEST_SHOWN]

    return [
        f"{name}: recorded {shown(recorded.get(name))}, now {shown(now.get(name))}"
        for name in sorted(set(recorded) | set(now))
        if recorded.get(name) != now.get(name)
    ]


def _share_differences(recorded: Any, now: Mapping[str, Any]) -> list[str]:
    if recorded == json.loads(canonical(now)):
        return []
    return [f"recorded {canonical(recorded)}, now {canonical(now)}"]
