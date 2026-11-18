# Provenance: new
"""Which run an idle federation leases next (#462 step 1, C42): outcome-blind, from lease states, caps, the budget
and expected times only.

1. **Set.** The current set is the earliest declared set with a pending run. Progress counts leases, never results,
   and a run that goes back to pending (returned, expired, lost) makes its set current again.
2. **Caps and budget.** A model of that set is eligible when its leases holding a cap slot (in flight, or expired with
   their holder not yet proven ended) are below its cap (as set, `caps`), and its grant fits what is left of `max_usd`
   and of its own ceiling (as raised, `ceilings`) after every lease's charge (spent, settled, or its grant while it is
   held).
3. **Remaining work.** The eligible model with the most remaining expected time wins: its pending runs' estimates in
   the set, plus what is left of its leases in flight. A tie goes to the model whose first pending run comes first.
   With no model eligible the federation waits: it is never sent ahead to a later set.
4. **The run.** That model's first pending run of the set in the seeded order, whatever its condition.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from expcore.campaign.elastic.ceilings import pinned
from expcore.campaign.elastic.config import LedgerConfig, usd_of
from expcore.campaign.elastic.records import Ceilings, Wait


@dataclass(frozen=True)
class Pending:
    """A pending run of the current set."""

    run_key: str
    seq: int
    model: str
    condition: str


@dataclass(frozen=True)
class InFlight:
    """A lease in flight: its cell and when it was issued."""

    model: str
    condition: str
    issued_at: float


def choose(
    set_index: int,
    pending: Iterable[Pending],
    in_flight: Iterable[InFlight],
    slots: Mapping[str, int],
    committed: tuple[int, Mapping[str, int]],
    config: LedgerConfig,
    estimate_s: Callable[[str, str], float],
    now: float,
    ceilings: Ceilings | None = None,
    caps: Mapping[str, int] | None = None,
) -> Pending | Wait:
    """The run to lease from the current set's pending runs, or why the federation waits. `ceilings` and `caps` are the
    ones in force (raised, or changed); by default the configuration's own."""
    ceilings = pinned(config) if ceilings is None else ceilings
    caps = config.caps if caps is None else caps
    by_model: dict[str, list[Pending]] = defaultdict(list)
    for run in sorted(pending, key=lambda r: r.seq):
        by_model[run.model].append(run)
    if not by_model:
        return Wait("no run is pending")
    remaining = {m: sum(estimate_s(m, r.condition) for r in runs) for m, runs in by_model.items()}
    for lease in in_flight:
        if lease.model in remaining:
            remaining[lease.model] += max(0.0, estimate_s(lease.model, lease.condition) - (now - lease.issued_at))
    order = sorted(by_model, key=lambda m: (-remaining[m], by_model[m][0].seq))
    held = []
    for model in order:
        reason = _held(model, slots, committed, config, ceilings, caps)
        if reason is None:
            return by_model[model][0]
        held.append(reason)
    return Wait(f"set {set_index} waits: " + "; ".join(held))


def _held(
    model: str,
    slots: Mapping[str, int],
    committed: tuple[int, Mapping[str, int]],
    config: LedgerConfig,
    ceilings: Ceilings,
    caps: Mapping[str, int],
) -> str | None:
    """Why the model cannot take a lease now, or None."""
    cap = caps.get(model)
    if cap is not None and slots.get(model, 0) >= cap:
        return f"model {model} is at its cap of {cap}"
    if ceilings.max_micro is None:
        return None
    grant = config.grant_micro(model)
    total, per_model = committed
    left = ceilings.max_micro - total
    if grant > left:
        return f"model {model}'s grant {usd_of(grant):g} USD is over the budget left, {usd_of(left):g} USD of max_usd"
    ceiling = ceilings.per_model_micro.get(model)
    if ceiling is not None:
        left = ceiling - per_model.get(model, 0)
        if grant > left:
            return f"model {model}'s grant {usd_of(grant):g} USD is over what is left of its ceiling, {usd_of(left):g}"
    return None
