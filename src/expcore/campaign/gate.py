# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/gate.py, blob 83b918421c9430284da616edd24d91ea23475549
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""The budget gate: budgets are enforced, not only estimated (C12), and a hold is lifted by a release file (C13).

Before a run starts, the gate asks whether the campaign can afford one more of its model. The answer is the dollars
spent so far (attempts counted by their counts, an interrupted one by its bound, C05), plus what the runs in flight can
cost, plus what this run can cost, against a ceiling. **What a run can cost is always its bound**, the most one attempt's
caps let it cost, the request it can make past its token cap included (`attempt_bounds.bound_usd`, C25), by the reply
maximum only for a model the campaign holds to it (C27), never the mean of the runs that finished: a reservation for a
pending or running attempt is the worst case, because an attempt that overruns its mean is still inside the ceiling
only if the bound was set aside for it. A run starts only when the remaining budget covers the bound. The historical mean is an estimate for
the status and the reports (`BudgetState.estimate_usd`) and decides nothing.

A send of a run in flight that its token cap cannot account for asks for a top-up first (`refusal`, C26; the ledger
is `expcore.campaign.topup`): the same figure, with the top-up in place of a new run's bound. What was spent includes
every top-up, of finished attempts and of running ones, from the moment it was reserved.

Cost that cannot be known holds like cost that is over: a model whose price is unknown (no price, not free) is held,
and so is every model once an attempt of an unpriced model has been charged, because the total spent is then unknown.
These holds end only when the table is fixed; a release file does not price a model.

Two kinds of ceiling, both releasable. `max_usd` is the campaign's total and `per_model_usd` caps single models. The
first moment a ceiling is found over is its **stop**, recorded in the store with its time. A release file
(`release.take_listed`) lists the scope it ends, `ALL` for the total or a model key, counts only if written after that
stop, and is used up when it ends it. A release adds `release_step_usd` to the ceiling it ends, so it lets the campaign
go on by a stated amount and no more; a campaign that wants no release leaves the step at 0. The stops and the amounts
released are kept in the store (meta), so a restart changes nothing. A stop is dated at full precision (`since_epoch`),
not to the whole second the store's timestamps carry, because a release must be strictly newer than it.
"""

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from expcore.campaign import release, reply_cap
from expcore.campaign.attempt_bounds import attempt_costs, bound_usd, mean_run_usd, request_bound_usd, spent_usd
from expcore.campaign.store import Store
from expcore.config.models import Limits, ModelSpec

HOLDS_KEY = "budget_holds"  # scope -> {"since_utc", "reason"}: the stops in force
RELEASED_KEY = "budget_released"  # scope -> dollars released so far
ALL = "ALL"


@dataclass(frozen=True)
class Budget:
    """The campaign's ceilings in US dollars, and how a hold is lifted.

    - `max_usd`: the total the campaign may spend, bounds and runs in flight counted.
    - `per_model_usd`: further ceilings for single models.
    - `release_step_usd`, `release_file`: a release listed in the file adds the step to the ceiling it ends.
    """

    max_usd: float
    per_model_usd: Mapping[str, float] = field(default_factory=dict)
    release_step_usd: float = 0.0
    release_file: Path | None = None

    def __post_init__(self) -> None:
        """Refuse ceilings that cannot work."""
        amounts = [("max_usd", self.max_usd), ("release_step_usd", self.release_step_usd)]
        amounts += [(f"per_model_usd[{k}]", v) for k, v in self.per_model_usd.items()]
        for name, value in amounts:
            if isinstance(value, bool) or not isinstance(value, int | float) or not value >= 0 or value == float("inf"):
                raise ValueError(f"{name} must be a finite number of dollars, not below zero, got {value!r}")
        if self.max_usd == 0:
            raise ValueError("max_usd of 0 holds every run: leave the budget out to run without one")
        if ALL in self.per_model_usd:
            raise ValueError(f"{ALL} is the scope of the total: a model cannot be called that")
        if self.release_step_usd > 0 and self.release_file is None:
            raise ValueError("a release step needs a release_file")
        if self.release_file is not None and self.release_step_usd <= 0:
            raise ValueError(
                "a release_file needs a release_step_usd above 0: a release that adds nothing ends nothing"
            )


@dataclass(frozen=True)
class Hold:
    """Why a model may not start a run: the scope of the ceiling, the reason, and whether a release can lift it."""

    scope: str
    reason: str
    releasable: bool


@dataclass(frozen=True)
class BudgetState:
    """One assessment: what is held, and the figures behind it, for the status and the tests."""

    holds: Mapping[str, tuple[Hold, ...]]
    spent_usd: float | None
    reserve_usd: Mapping[str, float | None]  # per model, what one pending or running run is reserved: its bound
    ceiling_usd: float
    estimate_usd: Mapping[str, float | None] = field(default_factory=dict)  # per model, the mean run else the bound
    capped: frozenset[str] = frozenset()  # the models whose runs this assessment reserved by the reply maximum (C27)

    def reason(self, model: str) -> str | None:
        """Return why the model may not start a run now, or None when it may."""
        found = self.holds.get(model, ())
        return "; ".join(h.reason for h in found) if found else None


class BudgetGate:
    """Assess the budget before each dispatch; read the store afresh every time, keep nothing between assessments."""

    def __init__(
        self,
        store: Store,
        models: Mapping[str, ModelSpec],
        budget: Budget,
        limits: Limits,
        reply_capped: Collection[str] = (),
    ):
        """Take the store, the plan's models, the ceilings and the caps every attempt runs under; refuse a priced model
        with no context window, whose request past the token cap nothing bounds (C25).

        `reply_capped` are the models this start of the campaign showed held to the reply maximum (C27); the store's
        marks take a model off it for the rest of the campaign (`expcore.campaign.reply_cap`).
        """
        windowless = [key for key, spec in models.items() if spec.price is not None and spec.context_tokens is None]
        if windowless:
            raise ValueError(
                f"model(s) {', '.join(windowless)} have a price and no context_tokens: the budget gate cannot bound the "
                "request a run makes past its token cap"
            )
        self.store, self.models, self.budget, self.limits = store, models, budget, limits
        self.shown_capped = frozenset(reply_capped)

    # --- the ceilings and what has been released -------------------------------------------------------------------

    def _json(self, key: str) -> dict:
        """Read a stored mapping; one that cannot be read is empty, which holds more, never less (a release is lost)."""
        raw = self.store.get_meta(key)
        try:
            value = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def ceiling(self, scope: str, released: Mapping[str, float]) -> float | None:
        """Return the ceiling of a scope in dollars, releases included; None if the scope has no ceiling."""
        base = self.budget.max_usd if scope == ALL else self.budget.per_model_usd.get(scope)
        return None if base is None else base + float(released.get(scope, 0.0))

    def _apply_releases(self, holds: dict, released: dict, now: str) -> tuple[dict, dict]:
        """Use up the release file for the stops in force; return the holds and the released amounts that remain."""
        path = self.budget.release_file
        if path is None or not holds:
            return holds, released
        stops = {scope: _stop_epoch(h) for scope, h in holds.items()}
        taken = release.take_listed(path, stops)
        if not taken:
            return holds, released
        holds, released = dict(holds), dict(released)
        events = []
        for scope, who in sorted(taken.items()):
            released[scope] = float(released.get(scope, 0.0)) + self.budget.release_step_usd
            holds.pop(scope, None)
            events.append(
                (
                    "budget_released",
                    None if scope == ALL else scope,
                    f"{scope}: released by {who}; the ceiling rises by {self.budget.release_step_usd:g} USD "
                    f"to {self.ceiling(scope, released):g} USD",
                )
            )
        self.store.put_meta_many(
            {HOLDS_KEY: json.dumps(holds, sort_keys=True), RELEASED_KEY: json.dumps(released, sort_keys=True)},
            now,
            events,
        )
        return holds, released

    # --- the assessment -----------------------------------------------------------------------------------------------

    def assess(self, in_flight: Sequence[str], now: str, exact: str | None = None) -> BudgetState:
        """Decide which models may start a run, given the models of the runs now in flight.

        `in_flight` lists one model key per run being worked on, counted by the dispatcher itself, since an attempt's
        row exists only a moment after the dispatcher chooses the run. Records the stops that began and ended. `exact` is
        `now` at full precision (fractional seconds), with which a stop that begins is dated.
        """
        holds, released = self._apply_releases(self._json(HOLDS_KEY), self._json(RELEASED_KEY), now)
        capped = self.capped()
        costs = attempt_costs(self.store, self.models, self.limits, reply_capped=capped)
        spent = spent_usd(costs)
        reserve = {m: self._reserve(m, capped) for m in self.models}
        estimate = {m: self._estimate(m, costs, capped) for m in self.models}
        flying = {m: sum(1 for x in in_flight if x == m) for m in self.models}
        found: dict[str, list[Hold]] = {}
        over: dict[str, str] = {}
        for model, spec in self.models.items():
            for hold in self._holds_of(model, spec, costs, spent, reserve, flying, released):
                found.setdefault(model, []).append(hold)
                if hold.releasable:
                    over.setdefault(hold.scope, hold.reason)
        self._record_stops(holds, over, now, exact or now)
        return BudgetState(
            {m: tuple(v) for m, v in found.items()},
            spent,
            reserve,
            self.ceiling(ALL, released) or 0.0,
            estimate,
            capped,
        )

    def refusal(self, model: str, in_flight: Sequence[str], extra_usd: float, label: str = "this top-up") -> str | None:
        """Tell why `extra_usd` more for a run of `model` already in flight would not fit, or None when it would (C26).

        The figure is the assessment's (what was spent, top-ups included, and the reservations of the runs in flight,
        this one among them), with `extra_usd` in place of a new run's reservation, against both ceilings as released so
        far. Nothing is recorded: a stop begins at the next assessment, which a request this refused would not pass
        either. A total that is unknown refuses too.
        """
        released = self._json(RELEASED_KEY)
        capped = self.capped()
        costs = attempt_costs(self.store, self.models, self.limits, reply_capped=capped)
        spent = spent_usd(costs)
        reserve = {m: self._reserve(m, capped) for m in self.models}
        flying = {m: sum(1 for x in in_flight if x == m) for m in self.models}
        holds = self._holds_of(model, self.models[model], costs, spent, reserve, flying, released, extra_usd, label)
        return "; ".join(h.reason for h in holds) if holds else None

    def capped(self) -> frozenset[str]:
        """The models whose runs are reserved by the reply maximum now: shown at this start and not taken off since."""
        marks = reply_cap.unheld(self.store)
        return frozenset(m for m in self.shown_capped if not reply_cap.is_unheld(marks, m))

    def request_bound(self, model: str) -> float | None:
        """What one request of the model can cost now: the bound of a top-up (C26); None when unpriced."""
        return request_bound_usd(self.models[model], self.limits, reply_capped=model in self.capped())

    def _reserve(self, model: str, capped: Collection[str]) -> float | None:
        """Return what one pending or running run of the model is reserved: its bound, always; None if unpriced."""
        spec = self.models[model]
        return bound_usd(spec, self.limits, reply_capped=model in capped) if spec.priced else None

    def _estimate(self, model: str, costs: list, capped: Collection[str]) -> float | None:
        """Return what one more run of the model is expected to cost, for reports: its mean, else its bound."""
        spec = self.models[model]
        if not spec.priced:
            return None
        mean = mean_run_usd(self.store, costs, model)
        return mean if mean is not None else bound_usd(spec, self.limits, reply_capped=model in capped)

    def _holds_of(
        self,
        model: str,
        spec: ModelSpec,
        costs: list,
        spent: float | None,
        reserve: Mapping[str, float | None],
        flying: Mapping[str, int],
        released: Mapping[str, float],
        topup: float | None = None,
        label: str = "this top-up",
    ) -> list[Hold]:
        """Return the holds on one model: the unknown ones first, then each ceiling its next run would pass, or, with
        `topup`, each ceiling that top-up of a run in flight would pass."""
        if not spec.priced:
            return [Hold(model, f"model {model} has no price and is not marked free: its cost is unknown", False)]
        if spent is None:
            return [Hold(ALL, "the total spent is unknown: an attempt of a model without a price was charged", False)]
        later = {m: n * reserve[m] for m, n in flying.items() if n and reserve[m] is not None}
        if any(reserve[m] is None for m, n in flying.items() if n):
            return [Hold(ALL, "a run in flight is of a model without a price: the total is unknown", False)]
        mine = reserve[model] if topup is None else topup
        what = "this run" if topup is None else label
        out = []
        cap = self.ceiling(model, released)
        if cap is not None:
            used = spent_usd(costs, model) or 0.0
            figure = used + later.get(model, 0.0) + (mine or 0.0)
            if figure > cap:
                text = _over(f"model {model}", figure, used, later.get(model, 0.0), mine, cap, what)
                out.append(Hold(model, text, True))
        total_cap = self.ceiling(ALL, released) or 0.0
        figure = spent + sum(later.values()) + (mine or 0.0)
        if figure > total_cap:
            out.append(
                Hold(ALL, _over("the campaign", figure, spent, sum(later.values()), mine, total_cap, what), True)
            )
        return out

    def _record_stops(self, holds: dict, over: dict[str, str], now: str, exact: str) -> None:
        """Keep the stops in force: a ceiling found over for the first time begins one, one no longer over ends it."""
        began = {s: r for s, r in over.items() if s not in holds}
        ended = [s for s in holds if s not in over]
        if not began and not ended:
            return
        kept = {s: h for s, h in holds.items() if s in over}
        since = release.epoch_of(exact)
        kept.update({s: {"since_utc": now, "since_epoch": since, "reason": r} for s, r in began.items()})
        events = [("budget_hold", None if s == ALL else s, f"{s}: {r}") for s, r in sorted(began.items())]
        events += [("budget_hold_over", None if s == ALL else s, f"{s}: no longer over its ceiling") for s in ended]
        self.store.put_meta_many({HOLDS_KEY: json.dumps(kept, sort_keys=True)}, now, events)


def _stop_epoch(hold: Mapping) -> float:
    """Return when a stop began, at full precision where it was recorded so, else from its whole-second time."""
    since = hold.get("since_epoch")
    if isinstance(since, int | float) and not isinstance(since, bool):
        return float(since)
    return release.epoch_of(hold.get("since_utc"))


def _over(
    what: str, figure: float, spent: float, flying: float, mine: float | None, cap: float, mine_is: str = "this run"
) -> str:
    """Return the sentence that says what is over: the figure, how it is made up, and the ceiling."""
    return (
        f"{what}: {figure:.2f} USD (spent {spent:.2f} + in flight {flying:.2f} + {mine_is} {mine or 0.0:.2f}) "
        f"is over the ceiling of {cap:.2f} USD"
    )
