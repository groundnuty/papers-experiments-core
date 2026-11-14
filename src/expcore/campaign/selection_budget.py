# Provenance: new
"""A selection's share of the campaign's caps and budget (C35): never past the totals, across all its selections.

A selection knows only itself when it starts, never how many batches its campaign will have, so every share is a part
of the **whole plan's** figure, rounded down; the merge refuses selections that are not disjoint and covering, so the
shares of the selections a merge accepts sum to at most each total. Two modes, always declared:

- **Concurrent (the default).** The selections may run at the same time.
  - `max_usd` is split in proportion to the reserved bound of the planned runs: a selection's planned runs of each
    priced model times one run's reservation (`shard_budget.reservation_usd`, the bound the budget gate reserves),
    over the same sum for the whole plan, in whole micro-dollars rounded down (`proportional_budget`).
  - Each `per_model_usd` ceiling is split by the model's planned runs, the selection's over the plan's; a ceiling of 0
    holds the model entirely and is 0 for every selection.
  - Each `[scheduler.unit_caps]` cap is split by the planned runs on the unit, rounded down (`proportional_caps`); a
    unit the selection runs nothing on gets 0, and is left out of its settings (a unit cap is at least 1, and no run of
    the selection can start there).
- **Sequential (`--sequential`).** The selections run one after another, which the merge checks from their stores'
  times. Each gets the full caps. The first gets the whole budget; each later one gets the total minus what the earlier
  ones actually spent (`remaining_budget`), read from their stores (`campaign.selection_after`), and a ceiling of 0
  stays 0. A release raises only the ceiling of the batch that took it: the next one starts from the totals.

**Refused at start, in every mode, naming the model or the unit:** a share of `max_usd`, or of a model's ceiling above
0, below one run's reservation of a priced model the selection runs (`refuse_short`); a concurrent share of 0 of a
unit's cap the selection runs on; and a model with no price that is not free, whose runs no share can reserve.
"""

from collections.abc import Mapping
from fractions import Fraction
from typing import Any

from expcore.campaign.shard import ShardError, micro_usd, usd_of

_MICRO = 1_000_000


def _floor_part(total_micro: int, part: Fraction) -> int:
    """`total_micro` times `part` (between 0 and 1), rounded down to a whole micro-dollar."""
    return int(total_micro * part.numerator // part.denominator)


def proportional_budget(
    max_usd: float,
    per_model_usd: Mapping[str, float],
    reserve: Mapping[str, float],
    selected: Mapping[str, int],
    planned: Mapping[str, int],
) -> dict[str, Any]:
    """A concurrent selection's part of the budget: `{"max_usd": x, "per_model_usd": {...}}`.

    `reserve` is one run's reservation per model of the plan (0 for a free model), `selected` and `planned` the runs of
    each model in the selection and in the whole plan. `max_usd` goes in proportion to the reserved bound of the
    planned runs, each ceiling by the model's runs; both in whole micro-dollars rounded down, so the parts of disjoint
    selections that cover the plan sum to at most each total.
    """
    whole = sum(Fraction(reserve[m]) * n for m, n in planned.items() if reserve.get(m))
    mine = sum(Fraction(reserve[m]) * n for m, n in selected.items() if reserve.get(m))
    share = _floor_part(micro_usd(max_usd), mine / whole) if whole else 0
    ceilings = {}
    for model, ceiling in sorted(per_model_usd.items()):
        runs, of = selected.get(model, 0), planned.get(model, 0)
        ceilings[model] = usd_of(_floor_part(micro_usd(ceiling), Fraction(runs, of))) if of else 0.0
    return {"max_usd": usd_of(share), "per_model_usd": ceilings}


def remaining_budget(
    max_usd: float, per_model_usd: Mapping[str, float], spent_usd: float, spent_per_model: Mapping[str, float]
) -> dict[str, Any]:
    """A sequential selection's budget: each total minus what the earlier selections spent of it, in whole
    micro-dollars rounded down and never below 0. The first selection, with nothing spent before it, gets the totals."""
    left = max(micro_usd(max_usd) - _ceil_micro(spent_usd), 0)
    ceilings = {}
    for model, ceiling in sorted(per_model_usd.items()):
        ceilings[model] = usd_of(max(micro_usd(ceiling) - _ceil_micro(spent_per_model.get(model, 0.0)), 0))
    return {"max_usd": usd_of(left), "per_model_usd": ceilings}


def _ceil_micro(usd: float) -> int:
    """Dollars spent in whole micro-dollars, rounded up: what is left is never more than the total minus the spend."""
    exact = Fraction(usd) * _MICRO
    return -(-exact.numerator // exact.denominator)


def refuse_short(
    budget: Mapping[str, Any],
    ceilings: Mapping[str, float],
    reserve: Mapping[str, float],
    selected: Mapping[str, int],
    how: str,
) -> None:
    """Refuse a selection whose part of `max_usd`, or of a model's ceiling, cannot pay one run of a priced model it
    runs, naming the model; `ceilings` are the campaign's `per_model_usd` (one of 0 holds its model by design, and is
    not refused), and `how` says how the part was made, for the message."""
    for model in sorted(m for m, n in selected.items() if n and reserve.get(m)):
        reserved = reserve[model]
        parts = [("[budget] max_usd", budget["max_usd"])]
        if ceilings.get(model, 0) > 0:
            parts.append((f"[budget.per_model_usd] {model}", budget["per_model_usd"][model]))
        for what, part in parts:
            if Fraction(part) < Fraction(reserved):
                raise ShardError(
                    f"this selection runs model {model}, and its part of {what} ({how}) is {part:.6f} USD, less than "
                    f"one run's reservation of {reserved:.6f} USD, so no run of {model} could start; run the batches "
                    "one after another with --sequential (each later one gets what the earlier ones left), or raise "
                    f"{what}"
                )


def proportional_caps(
    unit_caps: Mapping[str, int], selected: Mapping[str, int], planned: Mapping[str, int], unit_of: Mapping[str, str]
) -> dict[str, int]:
    """A concurrent selection's part of every `[scheduler.unit_caps]` cap: the cap times the selection's planned runs on
    the unit over the plan's, rounded down, so the parts of disjoint selections sum to at most the cap.

    Raises ShardError naming the unit and its models when the part of a unit the selection runs on is 0."""
    on_unit: dict[str, list[int]] = {}
    for model, runs in planned.items():
        cell = on_unit.setdefault(unit_of[model], [0, 0])
        cell[0] += selected.get(model, 0)
        cell[1] += runs
    shares = {}
    for unit, cap in sorted(unit_caps.items()):
        mine, of = on_unit.get(unit, (0, 0))
        shares[unit] = cap * mine // of if of else 0
        if mine and shares[unit] == 0:
            models = ", ".join(sorted(m for m in selected if unit_of[m] == unit and selected[m]))
            raise ShardError(
                f"[scheduler.unit_caps] {unit} = {cap}: this selection plans {mine} of the plan's {of} runs on it "
                f"(model {models}), so its part of the cap is 0 and none of them could start; run the batches one "
                "after another with --sequential (each gets the full caps), or raise the cap"
            )
    return shares


__all__ = ["proportional_budget", "proportional_caps", "refuse_short", "remaining_budget"]
