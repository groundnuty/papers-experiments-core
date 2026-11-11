# Provenance: new
"""One campaign run as N shards on different hosts (#154): the names, the partition and the shares every part uses.

A shard is one driver on one host, with its own store, `out/` and platform, running a fixed part of the campaign's
plan. Shard i of N is given on the command line (`run --shard i/N --shard-label L`), never in `campaign.toml`, so
every shard has the campaign fingerprint of the whole plan. Without `--shard` a campaign is unsharded, and nothing
in this module is consulted: its store, records and exports are byte for byte what they were before shards existed.

**The partition is by pair, balanced per model (C28).** `assign(pairs, count)` takes every (task, model, seed) of
the whole plan, ranks each model's pairs by the SHA-256 of their canonical text, and deals them round the shards from a
starting shard that the model's own hash picks: so every shard gets the floor or the ceiling of n/N of each model's
pairs, and the shards that get the ceiling differ from model to model, so no shard collects every remainder. Every pair
a contrast forms shares task, model and seed (`stats.pairing`), so a pair never crosses shards. The assignment depends
only on the plan and N, so every host computes the same one; it depends on the whole plan, so a sharded campaign's plan
cannot grow on resume. Inside a shard the runs keep their plan order (C08) and their place in the whole plan, so a
merge can take the union.

**Shares never sum past the totals (C30).** `[scheduler.unit_caps]`, `[budget] max_usd` and `per_model_usd` are
campaign totals. Shard i of N gets `share(total, spec)`: an equal part, the remainder going to the first shards, so
the N shares sum to the total exactly. Dollars are shared in whole micro-dollars, rounded down, so they sum to at
most the total. A total smaller than N is refused, since some shard would get nothing. The one exception is a
`per_model_usd` ceiling of zero, which holds a model entirely: it is zero for every shard.

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
        if self.rule != RULE:
            raise ShardError(f"the partition rule {self.rule!r} is not this core's ({RULE!r})")

    @classmethod
    def parse(cls, text: str, label: str | None = None, *, wide_label: bool = False) -> ShardSpec:
        """Read `i/N` from the command line; the label defaults to `shard-i`, and is wide only if `wide_label`."""
        match = re.fullmatch(r"(\d+)/(\d+)", text.strip())
        if match is None:
            raise ShardError(f"--shard takes i/N, such as 2/4, not {text!r}")
        index, count = int(match.group(1)), int(match.group(2))
        return cls(index, count, label if label is not None else f"shard-{index}", wide_label=wide_label)

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


def share(total: int, spec: ShardSpec, *, what: str) -> int:
    """Shard `spec.index`'s part of a whole-number total: the shares of all N shards sum to `total` exactly (C30)."""
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ShardError(f"{what} is a whole number, not {total!r}")
    if total < spec.count:
        raise ShardError(f"{what} is {total}, less than the {spec.count} shards, so some shard would get none")
    return total // spec.count + (1 if spec.index <= total % spec.count else 0)


def share_usd(total: float, spec: ShardSpec, *, what: str, allow_zero: bool = False) -> float:
    """Shard `spec.index`'s part of a dollar total, in whole micro-dollars rounded down: shares sum to at most it.

    A total of zero is refused like any total below the shard count, unless `allow_zero`: then it is zero for every
    shard. Only a ceiling that holds something entirely may be zero (`per_model_usd`); `Budget` refuses a total of 0.
    """
    micro = int((Decimal(str(total)) * _MICRO).to_integral_value(rounding=ROUND_FLOOR))
    if allow_zero and micro == 0:
        return 0.0
    return float(Decimal(share(micro, spec, what=what)) / _MICRO)


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
    "ShardError",
    "ShardSpec",
    "assign",
    "budget_shares",
    "caps_shares",
    "check_identity",
    "in_shard",
    "is_default_label",
    "pair_of",
    "share",
    "share_usd",
]
