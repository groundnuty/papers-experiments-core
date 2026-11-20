# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/gate.py, blob 83b918421c9430284da616edd24d91ea23475549
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""The budget gate: budgets are enforced, not only estimated (C12), and a hold is lifted by a release file (C13).

Before a run starts, the gate asks whether the campaign can afford one more of its model. The answer is the dollars
spent so far (attempts counted by their counts, an interrupted one by its call log, plus one request's bound for a
request in flight, or else by the bound its run was reserved, which its row records, C05, C27), plus what the runs in flight can cost, plus what this run can cost, against a ceiling. **What a
run can cost is always its bound**, the most one attempt's caps let it cost, the request it can make past its token
cap included (`attempt_bounds.bound_usd`, C25), by the reply maximum only for a model the campaign holds to it (C27),
never the mean of the runs that finished: a reservation for a
pending or running attempt is the worst case, because an attempt that overruns its mean is still inside the ceiling
only if the bound was set aside for it. A run starts only when the remaining budget covers the bound. The historical mean is an estimate for
the status and the reports (`BudgetState.estimate_usd`) and decides nothing.

A send of a run in flight that its token cap cannot account for asks for a top-up first (`refusal`, C26; the ledger
is `expcore.campaign.topup`): the same figure, with the top-up in place of a new run's bound. What was spent includes
every top-up, of finished attempts and of running ones, from the moment it was reserved, and what admission's probes
were charged (`expcore.campaign.probe_spend`), which each send of a probe asks for the same way before it goes out,
and what a kill took out of a store copied back, counted from the spend journal (`expcore.campaign.lost_spend`, O62):
each lost attempt charged as its row would be, each lost top-up and probe charge at its dollars. A start of the
campaign takes a release before its probes ask (`take_releases`), so a released campaign is not held for want of the
room the release gave.

A store that holds one run, a lease's (C41), is built with `single_run`: the question "can another run start" has no
other run to ask about. While its run is in flight, that run is reserved once and no ceiling is asked about a next run
(`_no_run_left_to_start`); once the run is done nothing is left to ask about either. Before the run starts, and again
for a run set aside and pending, the run is weighed in full, so a grant below one reservation still starts nothing.
Top-ups and probes (`refusal`) are weighed as always, against what is reserved and charged (C64).

Cost that cannot be known holds like cost that is over: a model whose price is unknown (no price, not free) is held,
and so is every model once an attempt of an unpriced model has been charged, because the total spent is then unknown.
These holds end only when the table is fixed; a release file does not price a model.

Two kinds of ceiling, both releasable. `max_usd` is the campaign's total and `per_model_usd` caps single models. The
first moment a ceiling is found over is its **stop**, recorded in the store with its time and a serial
(`release.next_serial`) in one transaction, and the hold names both. A release file (`release.take_listed`) lists the
stop it ends, as `<scope> <serial>` with `ALL` for the total or a model key: a line counts only when it names the
serial of the stop in force, and is used up when it ends it; a bare scope, the form before serials, or another stop's
serial ends nothing and is said once with the line that would end the stop (C13). A stop recorded before serials is
given one at the next assessment, in a transaction with its event. A release adds `release_step_usd` to the ceiling it
ends, so it lets the campaign go on by a stated amount and no more; a campaign that wants no release leaves the step
at 0. The stops and the amounts released are kept in the store (meta), so a restart changes nothing. A stop's time
(`since_utc`, `since_epoch` at full precision) is for display: no release is judged by it.
"""

import dataclasses
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from expcore.campaign import lost_spend, probe_spend, release, reply_cap
from expcore.campaign.attempt_bounds import (
    AttemptCost,
    attempt_costs,
    bound_usd,
    mean_run_usd,
    request_bound_usd,
    spent_usd,
)
from expcore.campaign.store import Store
from expcore.config.models import Limits, ModelSpec

HOLDS_KEY = "budget_holds"  # scope -> {"since_utc", "since_epoch", "reason", "serial"}: the stops in force
RELEASED_KEY = "budget_released"  # scope -> dollars released so far
ALL = "ALL"
WHERE = "the budget's release file"  # how a hold names the file: the store, which may be published, has no path


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
        *,
        single_run: bool = False,
    ):
        """Take the store, the plan's models, the ceilings and the caps every attempt runs under; refuse a priced model
        with no context window, whose request past the token cap nothing bounds (C25).

        `reply_capped` are the models this start of the campaign showed held to the reply maximum (C27); the store's
        marks take a model off it for the rest of the campaign (`expcore.campaign.reply_cap`). `single_run` is for a
        store that holds one run, a lease's (C41): it has no second run to ask about, so once its run is in flight, or
        done, no run is held for want of room (C64).
        """
        windowless = [key for key, spec in models.items() if spec.price is not None and spec.context_tokens is None]
        if windowless:
            raise ValueError(
                f"model(s) {', '.join(windowless)} have a price and no context_tokens: the budget gate cannot bound the "
                "request a run makes past its token cap"
            )
        self.store, self.models, self.budget, self.limits = store, models, budget, limits
        self.single_run = single_run
        self.shown_capped = frozenset(reply_capped)
        self._said: set[release.Refused] = set()  # the refused release lines said already, each once per process

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

    def take_releases(self, now: str) -> None:
        """Use up the release file for the stops in force, as each assessment does first. A start of the campaign
        calls it before admission, whose probes are paid from the budget and must find the room a release gave
        (C13, C26)."""
        self._apply_releases(self._json(HOLDS_KEY), self._json(RELEASED_KEY), now)

    def _numbered(self, holds: dict, now: str) -> dict:
        """Return the stops in force, each with its serial: one recorded before serials is given its serial first, in
        a transaction with its event."""
        if all(release.serial_of(h) is not None for h in holds.values()):
            return holds

        def change(current: str | None) -> tuple[str | None, list[tuple[str, str | None, str]], dict]:
            try:
                stored = json.loads(current) if current else {}
            except ValueError:
                return None, [], holds
            if not isinstance(stored, dict):
                return None, [], holds
            fresh, given = release.numbered(self.store, stored, now)
            said = "the hold, recorded without a serial, is stop"
            events = [
                ("budget_hold_numbered", None if s == ALL else s, f"{s}: {said} {fresh[s]['serial']}") for s in given
            ]
            return (json.dumps(fresh, sort_keys=True) if given else None), events, fresh

        return self.store.update_meta(HOLDS_KEY, change, now)

    def _apply_releases(self, holds: dict, released: dict, now: str) -> tuple[dict, dict]:
        """Use up the release file for the stops in force; return the holds and the released amounts that remain."""
        holds = self._numbered(holds, now)
        path = self.budget.release_file
        if path is None or not holds:
            return holds, released
        serials = {scope: release.serial_of(h) for scope, h in holds.items()}
        stops = {scope: serial for scope, serial in serials.items() if serial is not None}
        taken, refused = release.take_listed(path, stops)
        release.say_refused(self.store, path, refused, self._said, "budget_release_refused", "budget hold", now)
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
                    f"{scope} (stop {stops[scope]}): released by {who}; the ceiling rises by "
                    f"{self.budget.release_step_usd:g} USD to {self.ceiling(scope, released):g} USD",
                )
            )
        self.store.put_meta_many(
            {HOLDS_KEY: json.dumps(holds, sort_keys=True), RELEASED_KEY: json.dumps(released, sort_keys=True)},
            now,
            events,
        )
        return holds, released

    # --- the assessment -----------------------------------------------------------------------------------------------

    def assess(
        self, in_flight: Sequence[str], now: str, exact: str | None = None, *, live: Collection[str] | None = None
    ) -> BudgetState:
        """Decide which models may start a run, given the models of the runs now in flight.

        `in_flight` lists one model key per run being worked on, counted by the dispatcher itself, since an attempt's
        row exists only a moment after the dispatcher chooses the run. `live` are those runs' keys: a running attempt of
        any other run has no worker left to settle it and is charged its bound (C17, C26); None takes every running
        attempt as live. Records the stops that began and ended, and a hold a release can lift names its stop's serial
        and, with a release file, the line that ends it. `exact` is `now` at full precision (fractional seconds), with
        which a stop that begins is dated.
        """
        holds, released = self._apply_releases(self._json(HOLDS_KEY), self._json(RELEASED_KEY), now)
        costs, capped = self._costs(live)
        probes = self._charged_outside()
        spent = _with_probes(spent_usd(costs), probes)
        reserve = {m: self._reserve(m, capped) for m in self.models}
        estimate = {m: self._estimate(m, costs, capped) for m in self.models}
        flying = {m: sum(1 for x in in_flight if x == m) for m in self.models}
        found: dict[str, list[Hold]] = {}
        over: dict[str, str] = {}
        for model, spec in self.models.items():
            for hold in self._holds_of(model, spec, costs, spent, reserve, flying, released, probes):
                found.setdefault(model, []).append(hold)
                if hold.releasable:
                    over.setdefault(hold.scope, hold.reason)
        stops = self._record_stops(holds, over, now, exact or now)
        where = None if self.budget.release_file is None else WHERE
        told = {s: release.told(s, release.serial_of(h), where) for s, h in stops.items()}
        return BudgetState(
            {m: tuple(_told(h, told) for h in v) for m, v in found.items()},
            spent,
            reserve,
            self.ceiling(ALL, released) or 0.0,
            estimate,
            capped,
        )

    def refusal(
        self,
        model: str,
        in_flight: Sequence[str],
        extra_usd: float,
        label: str = "this top-up",
        *,
        live: Collection[str] | None = None,
    ) -> str | None:
        """Tell why `extra_usd` more for a run of `model` already in flight would not fit, or None when it would (C26).

        The figure is the assessment's (what was spent, top-ups included, and the reservations of the runs in flight,
        this one among them), with `extra_usd` in place of a new run's reservation, against both ceilings as released so
        far, `live` as `assess` takes it. Nothing is recorded: a stop begins at the next assessment, which a request this
        refused would not pass either. A total that is unknown refuses too.
        """
        released = self._json(RELEASED_KEY)
        costs, capped = self._costs(live)
        probes = self._charged_outside()
        spent = _with_probes(spent_usd(costs), probes)
        reserve = {m: self._reserve(m, capped) for m in self.models}
        flying = {m: sum(1 for x in in_flight if x == m) for m in self.models}
        spec = self.models[model]
        holds = self._holds_of(model, spec, costs, spent, reserve, flying, released, probes, extra_usd, label)
        return "; ".join(h.reason for h in holds) if holds else None

    def capped(self) -> frozenset[str]:
        """The models whose runs are reserved by the reply maximum now: shown at this start and not taken off since."""
        return self.shown_capped - self._taken_off(reply_cap.unheld(self.store))

    def _taken_off(self, marks: Mapping) -> frozenset[str]:
        """The models the store's marks take off the reply maximum for the rest of the campaign (C27)."""
        return frozenset(m for m in {*self.models, *self.shown_capped} if reply_cap.is_unheld(marks, m))

    def _costs(self, live: Collection[str] | None = None) -> tuple[list[AttemptCost], frozenset[str]]:
        """Every attempt's cost, and the models reserved by the reply maximum now, from one reading of the store's
        marks. An attempt whose usage is unknown is charged the bound its row records it was reserved at dispatch, not
        this gate's view of its model, which before admission holds none to the maximum; by the window once the store
        has taken its model off it (C05, C27). A running attempt of a run not in `live` is charged its bound (C17)."""
        taken_off = self._taken_off(reply_cap.unheld(self.store))
        capped = self.shown_capped - taken_off
        costs = attempt_costs(self.store, self.models, self.limits, reply_capped=capped, taken_off=taken_off, live=live)
        ledger = lost_spend.read(self.store)  # what a kill took out of the store, counted from the journal (O62)
        if ledger is not None:
            costs += lost_spend.costs(ledger, self.models, self.limits, reply_capped=capped, taken_off=taken_off)
        return costs, capped

    def _charged_outside(self) -> Mapping[str, float] | None:
        """What admission's probes were charged, per model; None when that record, or the record of what a kill lost
        (whose costs `_costs` adds), cannot be read: the total spent is then unknown."""
        probes = probe_spend.charged(self.store)
        return None if probes is None or lost_spend.read(self.store) is None else probes

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
        probes: Mapping[str, float] | None,
        topup: float | None = None,
        label: str = "this top-up",
    ) -> list[Hold]:
        """Return the holds on one model: the unknown ones first, then each ceiling its next run would pass, or, with
        `topup`, each ceiling that top-up of a run in flight would pass. `probes` are what admission's probes were
        charged, per model; None when the store's record of them cannot be read."""
        if not spec.priced:
            return [Hold(model, f"model {model} has no price and is not marked free: its cost is unknown", False)]
        if probes is None:
            why = (
                f"the store's {probe_spend.PROBES_KEY}, what admission's probes were charged, or its "
                f"{lost_spend.KEY}, what a kill took out of it, cannot be read"
            )
            return [Hold(ALL, f"the total spent is unknown: {why}", False)]
        if spent is None:
            return [Hold(ALL, "the total spent is unknown: an attempt of a model without a price was charged", False)]
        later = {m: n * reserve[m] for m, n in flying.items() if n and reserve[m] is not None}
        if any(reserve[m] is None for m, n in flying.items() if n):
            return [Hold(ALL, "a run in flight is of a model without a price: the total is unknown", False)]
        if topup is None and self._no_run_left_to_start(model, flying):
            return []
        mine = reserve[model] if topup is None else topup
        what = "this run" if topup is None else label
        out = []
        cap = self.ceiling(model, released)
        if cap is not None:
            used = (spent_usd(costs, model) or 0.0) + probes.get(model, 0.0)
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

    def _no_run_left_to_start(self, model: str, flying: Mapping[str, int]) -> bool:
        """Tell whether a store that holds one run (`single_run`, a lease's) has none left to start: its run is in
        flight, where it is counted once as reserved, or it is done. No ceiling is asked about a run that cannot come
        (C64): that run's reservation was weighed when it was admitted, and what it may still charge, a top-up, is
        weighed against both ceilings by `refusal`, which this does not touch. A run pending and not in flight, a first
        attempt or one after a costly attempt, is weighed in full."""
        return self.single_run and (bool(flying.get(model)) or not self.store.pending())

    def _record_stops(self, holds: dict, over: dict[str, str], now: str, exact: str) -> dict:
        """Keep the stops in force: a ceiling found over for the first time begins one, given its serial in the
        transaction that records it, and one no longer over ends it. Return the stops in force."""
        began = {s: r for s, r in over.items() if s not in holds}
        ended = [s for s in holds if s not in over]
        if not began and not ended:
            return holds

        def record() -> dict:
            kept = {s: h for s, h in holds.items() if s in over}
            since = release.epoch_of(exact)
            events = []
            for s, r in sorted(began.items()):
                serial = release.next_serial(self.store, now)
                kept[s] = {"since_utc": now, "since_epoch": since, "reason": r, "serial": serial}
                events.append(("budget_hold", None if s == ALL else s, f"{s} (stop {serial}): {r}"))
            events += [("budget_hold_over", None if s == ALL else s, f"{s}: no longer over its ceiling") for s in ended]
            self.store.put_meta_many({HOLDS_KEY: json.dumps(kept, sort_keys=True)}, now, events)
            return kept

        return self.store.transact(record)


def _told(hold: Hold, told: Mapping[str, str]) -> Hold:
    """A hold a release can lift, with what its sentence says of its stop: the serial, and the line that ends it."""
    said = told.get(hold.scope, "") if hold.releasable else ""
    return dataclasses.replace(hold, reason=hold.reason + said) if said else hold


def _with_probes(spent: float | None, probes: Mapping[str, float] | None) -> float | None:
    """What was spent on attempts plus what admission's probes were charged; unknown when either is."""
    return None if spent is None or probes is None else spent + sum(probes.values())


def _over(
    what: str, figure: float, spent: float, flying: float, mine: float | None, cap: float, mine_is: str = "this run"
) -> str:
    """Return the sentence that says what is over: the figure, how it is made up, and the ceiling."""
    return (
        f"{what}: {figure:.2f} USD (spent {spent:.2f} + in flight {flying:.2f} + {mine_is} {mine or 0.0:.2f}) "
        f"is over the ceiling of {cap:.2f} USD"
    )
