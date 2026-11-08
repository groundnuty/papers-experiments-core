# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/harness_stop.py, blob e36b086b771945e2048ed94663467b51c27a89ab
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Harness errors stop dispatch, narrowly then widely (C10).

A harness error is a crash in our own code on valid input. It is neither infrastructure nor an outcome: the attempt is
kept as a `harness_error` attempt, charged to nobody, and the run goes back to pending from the same row and seed when
the campaign next opens, after the fix (`expcore.campaign.recovery`). What the errors stop while the campaign runs:

- a **model**: `stop_after` errors of one kind on that model within `window_s`;
- **all dispatch**: one kind on `models_stop` different models within the window, the same bug seen across the plan.

The kind is the exception's class name. The errors are not counted anywhere else: they are the `harness_error`
attempts in the store, with their own end times, so the window is read from store timestamps and a restart changes
nothing. What is kept is a stop once it has fired (a window moving on never ends one), and the releases (`cleared`):
a release ends a stop, and the errors up to then stop counting for that kind on that model, or on all models.

The release file lists names, one per line: a model's name ends that model's stop, the word `ALL` ends the all-stop.
Each counts only if written after its stop and is used once (`release.take_listed`); a name with no stop in force is
left alone. A record that cannot be read stops everything: the gate fails closed.
"""

import datetime as dt
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.campaign import release
from expcore.campaign.store import Store, column_for
from expcore.loop.outcomes import Outcome

KEY = "harness_error_stops"
ALL = "ALL"
UNKNOWN_KIND = "unknown"


@dataclass(frozen=True)
class HarnessStopConfig:
    """When harness errors stop dispatch, and where a release is written.

    `stop_after` errors of one kind on one model, or one kind on `models_stop` models, both within `window_s` seconds.
    """

    stop_after: int = 3
    models_stop: int = 3
    window_s: float = 6 * 3600.0
    release_file: Path | None = None

    def __post_init__(self) -> None:
        """Refuse limits that cannot work."""
        if self.stop_after < 1 or self.models_stop < 1:
            raise ValueError("stop_after and models_stop must be at least 1")
        if self.window_s <= 0:
            raise ValueError("window_s must be positive")


def kind_of(outcome: Outcome) -> str:
    """Return the kind of a harness defect: the class name of its exception."""
    return outcome.defect.exception if outcome.defect is not None else UNKNOWN_KIND


def kind_of_detail(detail: str | None) -> str:
    """Return the kind from an attempt's stored detail, which the store writes as `<exception> <stack hash>: <text>`."""
    head = (detail or "").split(":", 1)[0].split()
    return head[0] if head else UNKNOWN_KIND


def _epoch(utc: str | None) -> float:
    """Return a stored time as epoch seconds; one that cannot be read counts as now: toward the stop, never away."""
    try:
        return dt.datetime.fromisoformat(str(utc).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def stored_errors(store: Store) -> list[dict[str, Any]]:
    """Return every harness-error attempt: when it ended, its kind, its model and its run."""
    rows = store.q(
        f"SELECT r.{column_for('model')} AS model, r.run_key, a.detail, a.ended_utc "
        "FROM attempts a JOIN runs r USING(run_key) WHERE a.state='harness_error' ORDER BY a.id"
    )
    return [
        {
            "epoch": _epoch(r["ended_utc"]),
            "kind": kind_of_detail(r["detail"]),
            "model": r["model"],
            "run_key": r["run_key"],
        }
        for r in rows
    ]


def counted(errors: list[dict], cleared: Mapping[str, float], upto: float, window_s: float) -> list[dict]:
    """Return the errors that count at time `upto`: inside the window, and after any release that cleared their kind on
    their model or on all models."""
    return [
        e
        for e in errors
        if e["epoch"] > upto - window_s
        and e["epoch"]
        > max(
            cleared.get(f"{e['kind']}|{e['model']}", 0.0), cleared.get(f"{e['kind']}|*", 0.0), cleared.get("*|*", 0.0)
        )
    ]


def empty() -> dict[str, Any]:
    """Return the state with no stop in force."""
    return {"models": {}, "all": None, "cleared": {}}


def parse(raw: str | None) -> dict[str, Any]:
    """Return the stops in force. A record that cannot be read stops everything (fail closed): {"unreadable": True}."""
    if raw is None:
        return empty()
    try:
        data = json.loads(raw)
        shaped = (
            isinstance(data, dict)
            and isinstance(data.get("models"), dict)
            and isinstance(data.get("cleared"), dict)
            and (data.get("all") is None or isinstance(data["all"], dict))
        )
        if not shaped:
            raise ValueError("not a record of stops")
        return data
    except ValueError:
        return {"unreadable": True, **empty()}


class HarnessStops:
    """The harness-error stops of one campaign, kept in the store's meta so that a restart keeps them."""

    def __init__(self, store: Store, cfg: HarnessStopConfig):
        self.store, self.cfg = store, cfg

    def load(self) -> dict[str, Any]:
        """Return the stops in force now."""
        return parse(self.store.get_meta(KEY))

    def stopped(self) -> tuple[frozenset[str], bool]:
        """Return the models stopped and whether all dispatch is: an unreadable record stops all."""
        state = self.load()
        return frozenset(state["models"]), state["all"] is not None or bool(state.get("unreadable"))

    def note(self, model: str, kind: str, now: str) -> list[str]:
        """Count the harness errors after one more on `model`, fire the stops that reach their limit, and return the
        reasons of those that fired now. The error must be in the store already; a stop that has fired stays."""
        errors = stored_errors(self.store)

        def change(current: str | None) -> tuple[str | None, list[tuple[str, str | None, str]], list[str]]:
            state = parse(current)
            if state.get("unreadable"):
                return None, [], []  # the record stays unreadable and everything stays stopped, until it is fixed
            inwin = counted(errors, state["cleared"], _epoch(now), self.cfg.window_s)
            ofkind = [e for e in inwin if e["kind"] == kind]
            window_h = f"{self.cfg.window_s / 3600:g} h"
            reasons: list[str] = []
            events: list[tuple[str, str | None, str]] = []
            n_model = sum(1 for e in ofkind if e["model"] == model)
            if n_model >= self.cfg.stop_after and model not in state["models"]:
                why = f"{n_model} harness errors of kind {kind} on {model} within {window_h}"
                state["models"][model] = {"kind": kind, "utc": now, "epoch": _epoch(now), "why": why}
                reasons.append(why)
                events.append(("harness_error_stop", model, f"dispatch of {model} stops: {why}"))
            spread = sorted({e["model"] for e in ofkind})
            if len(spread) >= self.cfg.models_stop and state["all"] is None:
                why = f"kind {kind} on {len(spread)} models ({', '.join(spread)}) within {window_h}"
                state["all"] = {"kinds": [kind], "utc": now, "epoch": _epoch(now), "why": why}
                reasons.append(why)
                events.append(("harness_error_stop_all", None, f"dispatch of every model stops: {why}"))
            return (json.dumps(state, sort_keys=True) if reasons else None), events, reasons

        return self.store.update_meta(KEY, change, now)

    def take_releases(self, now: str) -> dict[str, str]:
        """Use up the release file for the stops in force; return {name: who and when} for those it ended.

        A release counts only if written after its stop, and ends that stop only; the errors up to now stop counting for
        it. With no file configured nothing is released.
        """
        path = self.cfg.release_file
        if path is None:
            return {}
        state = self.load()
        if state.get("unreadable"):
            return {}  # a record nobody can read is not ended by a name in a file: fix it, restart
        stops = {m: float(s["epoch"]) for m, s in state["models"].items()}
        if state["all"] is not None:
            stops[ALL] = float(state["all"]["epoch"])
        taken = release.take_listed(path, stops)
        if not taken:
            return {}

        def change(current: str | None) -> tuple[str | None, list[tuple[str, str | None, str]], dict[str, str]]:
            now_state = parse(current)
            if now_state.get("unreadable"):
                return None, [], {}
            events = []
            for name, who in sorted(taken.items()):
                if name == ALL:
                    kinds = now_state["all"]["kinds"] if now_state["all"] else []
                    for kind in kinds:
                        now_state["cleared"][f"{kind}|*"] = _epoch(now)
                    now_state["all"] = None
                    events.append(("harness_error_released_all", None, f"the all-stop ended by {who}"))
                else:
                    kind = now_state["models"].pop(name, {}).get("kind", UNKNOWN_KIND)
                    now_state["cleared"][f"{kind}|{name}"] = _epoch(now)
                    events.append(("harness_error_released", name, f"the stop on {name} ended by {who}"))
            return json.dumps(now_state, sort_keys=True), events, taken

        return self.store.update_meta(KEY, change, now)

    def status(self) -> dict[str, Any]:
        """Return the stops in force and the errors that count now, for the status view."""
        state = self.load()
        inwin = counted(stored_errors(self.store), state.get("cleared", {}), time.time(), self.cfg.window_s)
        per: dict[str, dict[str, int]] = {}
        for e in inwin:
            per.setdefault(e["kind"], {})[e["model"]] = per.setdefault(e["kind"], {}).get(e["model"], 0) + 1
        return {
            "unreadable": bool(state.get("unreadable")),
            "stopped_models": {m: s.get("why") for m, s in sorted(state["models"].items())},
            "all_stop": None if state["all"] is None else state["all"].get("why"),
            "in_window": per,
        }
