# Provenance: new
"""One campaign run as N shards on different hosts (#154): the names, the partition and the shares every part uses.

A shard is one driver on one host, with its own store, `out/` and platform, running a fixed part of the campaign's
plan. Shard i of N is given on the command line (`run --shard i/N --shard-label L`), never in `campaign.toml`, so
every shard has the campaign fingerprint of the whole plan. Without `--shard` a campaign is unsharded, and nothing
in this module is consulted but the `[shards]` layout a campaign may declare: without that table too, its store,
records and exports are byte for byte what they were before shards existed.

**The partition is by pair, balanced per model (C28).** `assign(pairs, count)` takes every (task, model, seed) of
the whole plan, ranks each model's pairs by the SHA-256 of their canonical text, and deals them round the shards from a
starting shard that the model's own hash picks: so every shard gets the floor or the ceiling of n/N of each model's
pairs, and the shards that get the ceiling differ from model to model, so no shard collects every remainder. Every pair
a contrast forms shares task, model and seed (`stats.pairing`), so a pair never crosses shards. The assignment depends
only on the plan and N, so every host computes the same one; it depends on the whole plan, so a sharded campaign's plan
cannot grow on resume. Inside a shard the runs keep their plan order (C08) and their place in the whole plan, so a
merge can take the union.

**A campaign may shard by model first (C31, `RULE_BY_MODEL`, `expcore-shard/3`).** When one federation serves one
concurrent run, N is the number of concurrent runs, far above a model's concurrent sessions, and the rule above would
deal every model's pairs to shards that get none of its sessions. `[shards] per_model = { model-a = 20, model-b = 6 }`
in `campaign.toml` (`ModelShards`, in the campaign fingerprint every shard shares) fixes N as the sum and maps shard i
to one model: the models in declared order, each taking as many shards as its count (`ModelShards.at`). A shard serves
that model only, and the model's pairs are dealt among its own shards exactly as `assign` deals them (ranked by
SHA-256, from a start the model's hash picks), so a pair stays whole and the model's shards differ by at most one
pair. Each such shard runs one run at a time, so a model's concurrency is its shard count. `partition` picks the
assignment by the spec's rule; a store made under `expcore-shard/2` still resumes and merges under it.

**Shares never sum past the totals (C30).** `[scheduler.unit_caps]`, `[budget] max_usd` and `per_model_usd` are
campaign totals. Shard i of N gets `share(total, spec)`: an equal part, the remainder going to the first shards, so
the N shares sum to the total exactly. Dollars are shared in whole micro-dollars, rounded down, so they sum to at
most the total. A total smaller than N is refused, since some shard would get nothing. The one exception is a
`per_model_usd` ceiling of zero, which holds a model entirely: it is zero for every shard. Under `expcore-shard/3` the
budget is shared by `campaign.shard_budget` instead, and each shard's cap is one run of its model's unit.

**A shard records what it ran against.** A study may declare its environment's identity: a mapping of names to
SHA-256 digests (the platform's configuration, the layout of its federation, each space's verified ingest
manifest). It is read once when the shard starts and kept with the shard spec. A merge refuses shards whose
identities differ (C29), so a shard that ran against other data cannot join the batch silently. Names and digests
only: a package is public and double-blind, so no host name, path or label that identifies a machine is kept, and the
shard's label is chosen by the operator, from `[a-z0-9-]` unless `--shard-label-wide` allows `.` and `_` too.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Any

RULE = "expcore-shard/2: per model, pairs ranked by sha256(json [task, model, seed]), dealt round the shards"
"""The partition rule's name, kept with every shard spec; a store made under another rule is refused."""
RULE_BY_MODEL = (
    "expcore-shard/3: shards by model in the declared order of [shards] per_model; per model, pairs ranked by "
    "sha256(json [task, model, seed]), dealt round its own shards"
)
"""The rule of a campaign that declares `[shards] per_model` (C31): every shard serves one model."""
RULES = (RULE, RULE_BY_MODEL)
"""The rules this core partitions by and reads back."""
SHARDS_CAP = "shards"
"""The key of the campaign fingerprint's caps that holds `[shards] per_model` (`ModelShards.to_json`), when declared."""

META_SHARD = "shard_spec"
"""Store meta key: the shard spec as JSON (`ShardSpec.to_json`), pinned at the first start."""
META_STARTED = "shard_started_utc"
"""Store meta key: when the shard first started, ISO 8601 UTC, pinned at the first start."""
META_ENVIRONMENT = "environment_identity"
"""Store meta key: the environment identity as sorted JSON, pinned at the first start."""
META_SHARES = "shard_shares"
"""Store meta key: the shard's shares, `{"unit_caps": {...}, "budget": {"max_usd": x, "per_model_usd": {...}}}`, the
budget being null when the campaign has none."""
EXPORT_KEY = "shard"
"""Top-level export key, present only in a sharded campaign's export: the spec, start, identity and shares."""
MERGE_FILE = "merge.json"
"""The file a merge writes at the top of the merged batch, naming its shards (`expcore-merge/1`)."""
MERGE_SCHEMA = "expcore-merge/1"

_LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_LABEL_WIDE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_MICRO = Decimal(1_000_000)


class ShardError(ValueError):
    """A shard spec, a share or an environment identity is unusable; the message names what and why."""


@dataclass(frozen=True)
class ShardSpec:
    """Shard `index` of `count` (1-based, count at least 2), with the operator's `label` and the partition rule.

    A label is 1 to 64 of `[a-z0-9-]`, starting with a letter or digit: no dot, so a fully qualified host name or an
    IPv4 address cannot be used by accident. `wide_label` (not kept, not compared) lets it use `.` and `_` as well; it
    is the one way to name a shard in that wider form, and `from_json` takes it, so a store that pinned such a label
    still reads back.
    """

    index: int
    count: int
    label: str
    rule: str = RULE
    wide_label: InitVar[bool] = False

    def __post_init__(self, wide_label: bool) -> None:
        """Refuse a spec no shard can have."""
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 2:
            raise ShardError(f"a sharded campaign has at least 2 shards, not {self.count!r}; run unsharded instead")
        if isinstance(self.index, bool) or not isinstance(self.index, int) or not 1 <= self.index <= self.count:
            raise ShardError(f"shard index {self.index!r} is not between 1 and {self.count}")
        if not isinstance(self.label, str) or not (_LABEL_WIDE if wide_label else _LABEL).fullmatch(self.label):
            raise ShardError(
                "a shard label is 1 to 64 lower-case letters, digits or '-', starting with a letter or digit, with "
                "no '.', so that a host name or an address is not used by accident; choose one that names no "
                "machine, since packages are public (--shard-label-wide also allows '.' and '_')"
            )
        if self.rule not in RULES:
            raise ShardError(
                f"the partition rule {self.rule!r} is not one of this core's ({RULE!r}, or {RULE_BY_MODEL!r})"
            )

    @classmethod
    def parse(cls, text: str, label: str | None = None, *, wide_label: bool = False) -> ShardSpec:
        """Read `i/N` from the command line; the label defaults to `shard-i`, and is wide only if `wide_label`."""
        match = re.fullmatch(r"(\d+)/(\d+)", text.strip())
        if match is None:
            raise ShardError(f"--shard takes i/N, such as 2/4, not {text!r}")
        index, count = int(match.group(1)), int(match.group(2))
        return cls(index, count, label if label is not None else f"shard-{index}", wide_label=wide_label)

    def under(self, rule: str) -> ShardSpec:
        """The same shard under `rule`; its label was checked when this spec was made."""
        return ShardSpec(self.index, self.count, self.label, rule, wide_label=True)

    def to_json(self) -> dict[str, Any]:
        """The spec as the store, the export and a merge keep it."""
        return {"rule": self.rule, "index": self.index, "count": self.count, "label": self.label}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ShardSpec:
        """Read a spec back, refusing missing or extra keys; a label of the wide form is read too."""
        if not isinstance(data, Mapping) or set(data) != {"rule", "index", "count", "label"}:
            raise ShardError("a shard spec has exactly the keys rule, index, count and label")
        return cls(data["index"], data["count"], data["label"], data["rule"], wide_label=True)


def is_default_label(label: str) -> bool:
    """Whether a label is one the default rule allows (no '.' or '_'), so needs no `--shard-label-wide`."""
    return _LABEL.fullmatch(label) is not None


def pair_of(factors: Mapping[str, Any]) -> tuple[str, str, int]:
    """The pair a run belongs to: its task, model and seed, which every contrast's pairs share."""
    return (factors["task"], factors["model"], factors["seed"])


def _digest(*parts: Any) -> str:
    text = json.dumps(list(parts), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def assign(pairs: Iterable[tuple[str, str, int]], count: int) -> dict[tuple[str, str, int], int]:
    """The 1-based shard of every pair of the whole plan, out of `count`, balanced per model (C28)."""
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ShardError(f"a shard count is a positive whole number, not {count!r}")
    by_model: dict[str, list[tuple[str, tuple[str, str, int]]]] = {}
    for pair in set(pairs):
        by_model.setdefault(pair[1], []).append((_digest(*pair), pair))
    assignment: dict[tuple[str, str, int], int] = {}
    for model, ranked in by_model.items():
        start = int(_digest("model", model), 16) % count
        for rank, (_, pair) in enumerate(sorted(ranked)):
            assignment[pair] = (start + rank) % count + 1
    return assignment


def in_shard(factors: Mapping[str, Any], spec: ShardSpec, assignment: Mapping[tuple[str, str, int], int]) -> bool:
    """Whether a run, by its factor values, belongs to `spec`'s shard under the whole plan's `assignment`."""
    pair = pair_of(factors)
    if pair not in assignment:
        raise ShardError("a run's task, model and seed are not in the plan the shards were assigned from")
    return assignment[pair] == spec.index


@dataclass(frozen=True)
class ModelShards:
    """`[shards] per_model`: the plan's models in declared order, each with how many shards serve it (C31).

    The order is kept as a sequence, never as a mapping whose keys a canonical text would sort, so every host maps
    shard i to the same model. N is the sum, at least 2; each count is at least 1.
    """

    per_model: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        """Refuse a layout no campaign can shard by."""
        names = [entry[0] if isinstance(entry, tuple) and len(entry) == 2 else None for entry in self.per_model]
        if not names or any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
            raise ShardError("[shards] per_model names each model of the plan once, with its count")
        for model, count in self.per_model:
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ShardError(f"[shards] per_model gives model {model!r} {count!r} shards, not a whole number >= 1")
        if self.count < 2:
            raise ShardError(f"[shards] per_model sums to {self.count}: a sharded campaign has at least 2 shards")

    @property
    def count(self) -> int:
        """N: how many shards the campaign has."""
        return sum(count for _, count in self.per_model)

    def at(self, index: int) -> tuple[str, int, int]:
        """Shard `index`'s model, its place k among that model's shards (1-based) and that model's shard count."""
        first = 1
        for model, count in self.per_model:
            if first <= index < first + count:
                return model, index - first + 1, count
            first += count
        raise ShardError(f"shard {index!r} is not between 1 and {self.count}, the shards [shards] per_model declares")

    def to_json(self) -> dict[str, Any]:
        """The layout as the campaign fingerprint keeps it, in `caps[SHARDS_CAP]`."""
        return {"per_model": [[model, count] for model, count in self.per_model]}

    @classmethod
    def from_json(cls, data: Any) -> ModelShards:
        """Read a layout back from a fingerprint, refusing any other form."""
        entries = data.get("per_model") if isinstance(data, Mapping) and set(data) == {"per_model"} else None
        if not isinstance(entries, list) or not all(isinstance(e, list) and len(e) == 2 for e in entries):
            raise ShardError("a fingerprint's [shards] layout is {'per_model': [[model, count], ...]}")
        return cls(tuple((model, count) for model, count in entries))


def layout_of(fingerprint: Mapping[str, Any]) -> ModelShards | None:
    """The `[shards] per_model` layout a campaign fingerprint declares in its caps; None when it declares none."""
    caps = fingerprint.get("caps")
    if not isinstance(caps, Mapping) or SHARDS_CAP not in caps:
        return None
    return ModelShards.from_json(caps[SHARDS_CAP])


def assign_by_model(pairs: Iterable[tuple[str, str, int]], layout: ModelShards) -> dict[tuple[str, str, int], int]:
    """The 1-based shard of every pair of the whole plan under `expcore-shard/3` (C31).

    Each model's pairs are dealt among that model's own shards as `assign` deals pairs among all shards (ranked by
    SHA-256, from a start the model's hash picks), offset by the model's first shard. A model of the plan without a
    count is refused: its pairs would be in no shard.
    """
    by_model: dict[str, set[tuple[str, str, int]]] = {}
    for pair in pairs:
        by_model.setdefault(pair[1], set()).add(pair)
    declared = [model for model, _ in layout.per_model]
    unknown = sorted(set(by_model) - set(declared))
    if unknown:
        raise ShardError(f"model {unknown[0]!r} has pairs in the plan and no count in [shards] per_model")
    assignment: dict[tuple[str, str, int], int] = {}
    first = 0
    for model, count in layout.per_model:
        assignment.update({pair: first + k for pair, k in assign(by_model.get(model, ()), count).items()})
        first += count
    return assignment


def partition(
    pairs: Iterable[tuple[str, str, int]], spec: ShardSpec, layout: ModelShards | None
) -> dict[tuple[str, str, int], int]:
    """The whole plan's assignment of pairs to shards under `spec`'s rule: `assign`, or by model with `layout`.

    A spec under `expcore-shard/3` needs the layout, and a count that is its sum; one under `expcore-shard/2` is
    refused for a campaign that declares a layout, which shards by model.
    """
    if spec.rule == RULE:
        if layout is not None:
            raise ShardError(
                "the campaign declares [shards] per_model, so its shards partition by expcore-shard/3, and this "
                "shard was made under expcore-shard/2"
            )
        return assign(pairs, spec.count)
    if layout is None:
        raise ShardError(
            f"shard {spec.index} of {spec.count} partitions by expcore-shard/3, and the campaign declares no "
            "[shards] per_model"
        )
    if layout.count != spec.count:
        raise ShardError(count_mismatch(spec, layout))
    return assign_by_model(pairs, layout)


def count_mismatch(spec: ShardSpec, layout: ModelShards) -> str:
    """Why `--shard i/N` does not fit a campaign whose `[shards] per_model` sums to another N."""
    counts = ", ".join(f"{model} = {count}" for model, count in layout.per_model)
    return (
        f"--shard {spec.index}/{spec.count} does not fit campaign.toml's [shards] per_model ({counts}), which declares "
        f"{layout.count} shards: give --shard i/{layout.count}"
    )


def by_model(spec: ShardSpec, layout: ModelShards) -> ShardSpec:
    """The command line's spec as a shard of a campaign that shards by model (`expcore-shard/3`); a count that is not
    the layout's sum is refused."""
    if spec.count != layout.count:
        raise ShardError(count_mismatch(spec, layout))
    return spec.under(RULE_BY_MODEL)


def part(total: int, index: int, count: int, *, what: str, allow_zero: bool = False) -> int:
    """Part `index` of `count` of a whole-number total; the `count` parts sum to `total` exactly (C30).

    A total below `count` is refused, since some part would be nothing, unless `allow_zero`: then the parts past the
    total are 0, which only a check that refuses them may ask for.
    """
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ShardError(f"{what} is a whole number, not {total!r}")
    if total < count and not allow_zero:
        raise ShardError(f"{what} is {total}, less than the {count} shards, so some shard would get none")
    return total // count + (1 if index <= total % count else 0)


def share(total: int, spec: ShardSpec, *, what: str) -> int:
    """Shard `spec.index`'s part of a whole-number total: the shares of all N shards sum to `total` exactly (C30)."""
    return part(total, spec.index, spec.count, what=what)


def micro_usd(total: float) -> int:
    """A dollar amount in whole micro-dollars, rounded down."""
    return int((Decimal(str(total)) * _MICRO).to_integral_value(rounding=ROUND_FLOOR))


def usd_of(micro: int) -> float:
    """Whole micro-dollars as dollars."""
    return float(Decimal(micro) / _MICRO)


def part_usd(total: float, index: int, count: int, *, what: str, allow_zero: bool = False) -> float:
    """Part `index` of `count` of a dollar total, in whole micro-dollars rounded down: the parts sum to at most it.

    A total of zero is refused like any total below the count, unless `allow_zero`: then it is zero for every part.
    """
    micro = micro_usd(total)
    if allow_zero and micro == 0:
        return 0.0
    return usd_of(part(micro, index, count, what=what))


def share_usd(total: float, spec: ShardSpec, *, what: str, allow_zero: bool = False) -> float:
    """Shard `spec.index`'s part of a dollar total, in whole micro-dollars rounded down: shares sum to at most it.

    A total of zero is refused like any total below the shard count, unless `allow_zero`: then it is zero for every
    shard. Only a ceiling that holds something entirely may be zero (`per_model_usd`); `Budget` refuses a total of 0.
    """
    return part_usd(total, spec.index, spec.count, what=what, allow_zero=allow_zero)


def caps_shares(unit_caps: Mapping[str, int], spec: ShardSpec) -> dict[str, int]:
    """Shard `spec`'s part of every `[scheduler.unit_caps]` total, by unit (C30)."""
    return {unit: share(cap, spec, what=f"[scheduler.unit_caps] {unit}") for unit, cap in sorted(unit_caps.items())}


def budget_shares(max_usd: float, per_model_usd: Mapping[str, float], spec: ShardSpec) -> dict[str, Any]:
    """Shard `spec`'s part of `[budget] max_usd` and of each `per_model_usd` ceiling, in micro-dollars rounded down.

    A model's ceiling of zero holds that model entirely, so every shard gets zero of it.
    """
    return {
        "max_usd": share_usd(max_usd, spec, what="[budget] max_usd"),
        "per_model_usd": {
            model: share_usd(ceiling, spec, what=f"[budget.per_model_usd] {model}", allow_zero=True)
            for model, ceiling in sorted(per_model_usd.items())
        },
    }


def check_identity(identity: Mapping[str, str]) -> dict[str, str]:
    """Return an environment identity sorted by name, refusing anything but names mapped to SHA-256 hex digests."""
    if not isinstance(identity, Mapping):
        raise ShardError("an environment identity is a mapping of names to SHA-256 hex digests")
    for name in identity:  # every name is checked before they are sorted: keys of two types cannot be
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ShardError(
                f"an environment identity name is 1 to 64 lower-case letters, digits, '.', '_' or '-': {name!r}"
            )
    checked: dict[str, str] = {}
    for name in sorted(identity):
        digest = identity[name]
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise ShardError(f"environment identity {name!r} is not a SHA-256 hex digest")
        checked[name] = digest
    return checked


__all__ = [
    "EXPORT_KEY",
    "MERGE_FILE",
    "MERGE_SCHEMA",
    "META_ENVIRONMENT",
    "META_SHARD",
    "META_SHARES",
    "META_STARTED",
    "RULE",
    "RULES",
    "RULE_BY_MODEL",
    "SHARDS_CAP",
    "ModelShards",
    "ShardError",
    "ShardSpec",
    "assign",
    "assign_by_model",
    "budget_shares",
    "by_model",
    "caps_shares",
    "check_identity",
    "count_mismatch",
    "in_shard",
    "is_default_label",
    "layout_of",
    "micro_usd",
    "pair_of",
    "part",
    "part_usd",
    "partition",
    "share",
    "share_usd",
    "usd_of",
]
