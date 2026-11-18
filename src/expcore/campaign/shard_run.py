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

A run on a lease (`campaign.run_lease`, `expcore-shard/5`) pins the same facts, with the lease as its spec and one more,
its attempt base (`META_ATTEMPT_BASE`), and is never resumed: a store holds one lease, once. Any later start of it is
refused (`LeaseReplayed` for the same lease id, `ShardMismatch` for another lease or another form), whether or not the
facts agree, so a replayed or a stale lease file cannot run a store a second time (`_pin_lease`).

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
from expcore.campaign.run_lease import META_ATTEMPT_BASE, RULE_LEASE, LeaseError, RunLease
from expcore.campaign.selection import RULE_SELECTION, SelectionSpec, Spec, describe, resume_hint
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
    micro_usd,
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


class LeaseReplayed(ShardMismatch):
    """A start gives the lease id the store already ran: a lease is used once, in a store of its own."""


@dataclass(frozen=True)
class ShardStart:
    """A shard's start: its spec, the environment identity it reads, checked, and the shares of the caps and budget."""

    spec: Spec | RunLease  # a shard's, a selection's (C38) or a lease's (C41)
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


def refuse_lease_misfit(lease: RunLease, campaign_sha256: str, settings: "SchedulerConfig") -> None:
    """The kernel's own hold on a lease (C41), for a start that did not come through `study.lease_start`: the scheduler
    refuses, before it opens the store, a lease issued for another campaign, a run with no attempt left, and settings
    whose budget is not held to the grant (no budget for a grant above 0, a ceiling above the grant, a release; any
    budget for a grant of 0). `lease_start` makes the settings that pass."""
    lease.check_campaign(campaign_sha256)
    lease.check_attempts_left(settings.max_attempts)
    budget = settings.budget
    if lease.grant_micro_usd == 0:
        held = budget is None
    else:
        held = (
            budget is not None
            and micro_usd(budget.max_usd) <= lease.grant_micro_usd
            and budget.release_file is None
            and budget.release_step_usd == 0
        )
    if not held:
        raise LeaseError(
            f"{lease.name} grants {lease.grant_usd:.6f} USD, and the scheduler's budget is not held to it (a budget "
            "of at most the grant and no release for a grant above 0, none for a grant of 0): the grant is the "
            "run's whole budget"
        )


def environment_block(spec: Spec | RunLease, environment: Mapping[str, Any]) -> str:
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
    if isinstance(spec, RunLease):
        what, every = f"lease {spec.lease_id}", "lease"
    elif isinstance(spec, SelectionSpec):
        what, every = spec.name, "selection"
    else:
        what, every = f"shard {spec.index} of {spec.count}", "shard"
    lines = [
        f"run: {what}: environment sha256 {fingerprint_sha256(environment)}, "
        f"python {environment['python']}, {len(versions)} distributions "
        f"(every {every} of the campaign must print this same digest)"
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
    recorded_rule = json.loads(held[META_SHARD]).get("rule") if META_SHARD in held else None
    if recorded_rule == RULE_LEASE or (shard is not None and isinstance(shard.spec, RunLease)):
        _pin_lease(store, shard, held, started, now)  # before any comparison: no start of a lease store may resume it
        return
    if shard is None:
        if META_SHARD in held:
            recorded = json.loads(held[META_SHARD])
            if recorded.get("rule") == RULE_SELECTION:
                raise ShardMismatch(
                    f"this store belongs to {describe(recorded)} of a campaign run in selections, and this start "
                    f"gives no selection: resume it with {resume_hint(recorded)}"
                )
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
    if RULE_SELECTION in (recorded_spec.get("rule"), shard.spec.rule):
        reason = _SELECTION_REASONS[first].format(what=describe(recorded_spec))
    else:
        reason = _REASONS[first].format(**recorded_spec)
    raise ShardMismatch(f"{reason}:\n  {lines}")


def _pin_lease(store: Store, shard: ShardStart | None, held: Mapping[str, str], started: bool, now: str) -> None:
    """The pin of a run on a lease (C41): a first start pins the lease and its attempt base; every later start of the
    store is refused, and a lease start of a store that another form made is too. Inside the caller's transaction, so a
    refusal leaves the store as it was."""
    if META_SHARD in held:
        recorded = json.loads(held[META_SHARD])
        if recorded.get("rule") != RULE_LEASE:
            assert shard is not None  # a store of another form, started with a lease (the caller sent a lease here)
            raise ShardMismatch(
                f"this store belongs to {describe(recorded)}, and this start gives {shard.spec.name}: a lease runs in "
                "a store of its own, never one that a shard or a selection started"
            )
        owner = f"lease {recorded.get('lease_id')}"
        given = None if shard is None else shard.spec
        if not isinstance(given, RunLease):
            what = "no lease" if given is None else describe(given.to_json())
            raise ShardMismatch(
                f"this store belongs to {owner} of a run taken on a lease, and this start gives {what}: a lease store "
                "is not resumed, the run goes on a new lease, in a store of its own"
            )
        if given.lease_id == recorded.get("lease_id"):
            raise LeaseReplayed(
                f"{owner} was already started in this store (a replay): a lease is used once, and the ledger gives "
                "a run a new lease id for each lease"
            )
        raise ShardMismatch(
            f"this store belongs to {owner}, and this start gives lease {given.lease_id}: one store per lease, so a "
            "run on a new lease goes in a store of its own"
        )
    assert shard is not None
    assert isinstance(shard.spec, RunLease)
    if started:
        raise ShardMismatch(
            "this store holds an unsharded campaign, and this start gives a lease: a lease runs in a store of its own "
            "(start a new store for it)"
        )
    for key, text in shard.pinned().items():
        store.db.execute("INSERT INTO meta(key,value,set_utc) VALUES(?,?,?)", (key, text, now))
    store.db.execute("INSERT INTO meta(key,value,set_utc) VALUES(?,?,?)", (META_STARTED, now, now))
    store.db.execute(
        "INSERT INTO meta(key,value,set_utc) VALUES(?,?,?)", (META_ATTEMPT_BASE, str(shard.spec.attempt_base), now)
    )


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


_SELECTION_REASONS = {
    META_SHARD: (
        "this store started as {what}, and this start gives another selection, label, mode or rule: a selection "
        "resumes only as itself (C38)"
    ),
    META_ENVIRONMENT: (
        "the environment identity of {what} changed since its first start: it would no longer have run against what "
        "its first runs did"
    ),
    META_SHARES: (
        "the shares of the caps and the budget of {what} changed since its first start, or the earlier selections it "
        "ran after (--after) or what they spent: they are fixed when it starts, so that the selections' shares never "
        "sum past the totals"
    ),
}


def _spec_differences(recorded: Mapping[str, Any], now: Mapping[str, Any]) -> list[str]:
    names = ("rule", "index", "count", "label")
    if RULE_SELECTION in (recorded.get("rule"), now.get("rule")):
        names = tuple(sorted(set(recorded) | set(now)))
    return [
        f"{name}: recorded {recorded.get(name)!r}, now {now.get(name)!r}"
        for name in names
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
