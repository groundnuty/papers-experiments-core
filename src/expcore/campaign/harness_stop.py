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

A stop's time is kept at full precision (`epoch`, float seconds) and to the second (`utc`), for display: no release is
judged by it (C13). A stop derived at the start, whose error time is known only to the second, is dated at the end of
that second, the latest it can have been.

A stop is recorded **in the transaction that records the error that causes it** (`HarnessStops.note` joins the store's
write transaction when called from `Store.finish_attempt`'s `also` step), so a kill between the two cannot lose it. And
because a result can also reach the store from its outcome marker at the next start, without the process that made it,
`HarnessStops.reconcile` derives the stops from the stored attempts at every start and fires any the record lacks. A
stop is never read from the record alone.

Each stop is given a **serial** in the transaction that records it (`release.next_serial`, `<count>.<random>`), shown
in its event, in the hold (`hold_reasons`) and in the status. The release file lists lines `<name> <serial>`: a model's
name with its stop's serial ends that model's stop, `ALL` with the all-stop's serial ends the all-stop, and a line is
used once (`release.take_listed`). A bare name, the form before serials, or another stop's serial ends nothing, and is
said once with the line that would end the stop in force (`release.say_refused`); a name with no stop in force is left
alone. A stop recorded before serials is given one at the start (`reconcile`), in a transaction with its event, before
anything can end it. A record that cannot be read stops everything: the gate fails closed.
"""

import datetime as dt
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.campaign import release
from expcore.campaign.store import Store, column_for
from expcore.loop.outcomes import Outcome

log = logging.getLogger(__name__)

KEY = "harness_error_stops"
ALL = "ALL"
UNKNOWN_KIND = "unknown"
WHERE = "the harness stop's release file"  # how a hold names the file: the store, which may be published, has no path
WHAT = "harness-error stop"


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


def _end_of_second(utc: str) -> float:
    """Return the end of the whole second a stored time names: the latest the moment can have been (or now, if unread)."""
    try:
        return dt.datetime.fromisoformat(str(utc).replace("Z", "+00:00")).timestamp() + 1.0
    except ValueError:
        return time.time()


def stored_errors(store: Store) -> list[dict[str, Any]]:
    """Return every harness-error attempt: when it ended, its kind, its model and its run."""
    rows = store.q(
        f"SELECT a.id, r.{column_for('model')} AS model, r.run_key, a.detail, a.ended_utc "
        "FROM attempts a JOIN runs r USING(run_key) WHERE a.state='harness_error' ORDER BY a.id"
    )
    return [
        {
            "id": r["id"],
            "epoch": _epoch(r["ended_utc"]),
            "utc": r["ended_utc"],
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
        self._said: set[release.Refused] = set()  # the refused release lines said already, each once per process

    def load(self) -> dict[str, Any]:
        """Return the stops in force now."""
        return parse(self.store.get_meta(KEY))

    def stopped(self) -> tuple[frozenset[str], bool]:
        """Return the models stopped and whether all dispatch is: an unreadable record stops all."""
        state = self.load()
        return frozenset(state["models"]), state["all"] is not None or bool(state.get("unreadable"))

    def hold_reasons(self) -> tuple[dict[str, str], str | None]:
        """Return the sentence of each model's stop, and of the all-stop (None when it is not in force), each naming
        its serial and, with a release file, the line that ends it. An unreadable record stops all."""
        state = self.load()
        said = "dispatch of every model is stopped by harness errors"
        if state.get("unreadable"):
            return {}, said
        where = None if self.cfg.release_file is None else WHERE
        models = {
            m: f"dispatch of {m} is stopped by harness errors{release.told(m, release.serial_of(s), where)}"
            for m, s in state["models"].items()
        }
        every = state["all"]
        return models, None if every is None else f"{said}{release.told(ALL, release.serial_of(every), where)}"

    def _fire(
        self, state: dict[str, Any], errors: list[dict], model: str, kind: str, now: str, epoch: float
    ) -> tuple[list[str], list[tuple[str, str | None, str]]]:
        """Change `state` by the stops that `errors` bring to reach their limit once one more of `kind` is on `model`.

        Return the reasons of the stops that fired and their events. A stop that has fired stays: it fires once, with
        a serial taken in the caller's transaction. `now` is its time as the store writes it and `epoch` the same
        moment at full precision.
        """
        inwin = counted(errors, state["cleared"], _epoch(now), self.cfg.window_s)
        ofkind = [e for e in inwin if e["kind"] == kind]
        window_h = f"{self.cfg.window_s / 3600:g} h"
        reasons: list[str] = []
        events: list[tuple[str, str | None, str]] = []
        n_model = sum(1 for e in ofkind if e["model"] == model)
        if n_model >= self.cfg.stop_after and model not in state["models"]:
            why = f"{n_model} harness errors of kind {kind} on {model} within {window_h}"
            serial = release.next_serial(self.store, now)
            state["models"][model] = {"kind": kind, "utc": now, "epoch": epoch, "why": why, "serial": serial}
            reasons.append(why)
            events.append(("harness_error_stop", model, f"dispatch of {model} stops (stop {serial}): {why}"))
        spread = sorted({e["model"] for e in ofkind})
        if len(spread) >= self.cfg.models_stop and state["all"] is None:
            why = f"kind {kind} on {len(spread)} models ({', '.join(spread)}) within {window_h}"
            serial = release.next_serial(self.store, now)
            state["all"] = {"kinds": [kind], "utc": now, "epoch": epoch, "why": why, "serial": serial}
            reasons.append(why)
            events.append(("harness_error_stop_all", None, f"dispatch of every model stops (stop {serial}): {why}"))
        return reasons, events

    @staticmethod
    def _unnumbered(state: dict[str, Any]) -> bool:
        """Whether a stop in force has no serial, as one recorded before serials (an unreadable record has none)."""
        stops = [*state["models"].values(), *([] if state["all"] is None else [state["all"]])]
        return not state.get("unreadable") and any(release.serial_of(s) is None for s in stops)

    def _number(self, state: dict[str, Any], now: str) -> list[tuple[str, str | None, str]]:
        """Give each stop in force that has no serial its serial, in the caller's transaction; return their events."""
        events: list[tuple[str, str | None, str]] = []
        for model, stop in sorted(state["models"].items()):
            if release.serial_of(stop) is None:
                serial = release.next_serial(self.store, now)
                state["models"][model] = {**(stop if isinstance(stop, dict) else {}), "serial": serial}
                said = f"the stop on {model}, recorded without a serial, is stop {serial}"
                events.append(("harness_error_stop_numbered", model, said))
        if state["all"] is not None and release.serial_of(state["all"]) is None:
            serial = release.next_serial(self.store, now)
            state["all"] = {**state["all"], "serial": serial}
            said = f"the all-stop, recorded without a serial, is stop {serial}"
            events.append(("harness_error_stop_numbered", None, said))
        return events

    def note(self, model: str, kind: str, now: str, exact: str | None = None) -> list[str]:
        """Count the harness errors after one more on `model`, fire the stops that reach their limit, and return the
        reasons of those that fired now. The error must be in the store already; a stop that has fired stays.

        `exact` is the moment at full precision (fractional seconds); a stop is dated with it, else with `now`."""
        errors = stored_errors(self.store)

        def change(current: str | None) -> tuple[str | None, list[tuple[str, str | None, str]], list[str]]:
            state = parse(current)
            if state.get("unreadable"):
                return None, [], []  # the record stays unreadable and everything stays stopped, until it is fixed
            reasons, events = self._fire(state, errors, model, kind, now, _epoch(exact or now))
            return (json.dumps(state, sort_keys=True) if reasons else None), events, reasons

        return self.store.update_meta(KEY, change, now)

    def reconcile(self, now: str) -> list[str]:
        """Derive the stops from the stored harness-error attempts and fire those the record lacks; return their reasons.

        The errors are replayed in the order they ended (the time each ended, then the attempt's number as a tiebreak,
        never the order the attempts began), each judged as `note` would have judged it when it was stored (the errors
        that ended before it, inside the window, after any release), so the stops, and the moments they began, are
        what an uninterrupted run would have recorded. A stop already in the record, or released since, is left as it
        is; one recorded before serials is given its serial. Call it at every start, before dispatch, because a result
        committed from its marker at this start never went through `note`.
        """
        errors = sorted(stored_errors(self.store), key=lambda e: (e["epoch"], e["id"]))
        if not errors and not self._unnumbered(self.load()):
            return []

        def change(current: str | None) -> tuple[str | None, list[tuple[str, str | None, str]], list[str]]:
            state = parse(current)
            if state.get("unreadable"):
                return None, [], []
            events = self._number(state, now)
            reasons: list[str] = []
            for index, error in enumerate(errors):
                at = error["utc"] if isinstance(error["utc"], str) and error["utc"] else now
                fired, noted = self._fire(
                    state, errors[: index + 1], error["model"], error["kind"], at, _end_of_second(at)
                )
                reasons += fired
                events += [
                    (kind, model, f"{text} (derived at the start from the stored attempts)")
                    for kind, model, text in noted
                ]
            return (json.dumps(state, sort_keys=True) if events else None), events, reasons

        return self.store.update_meta(KEY, change, now)

    def _numbered(self, now: str) -> dict[str, Any]:
        """Return the stops in force, each with its serial: one recorded without is given its serial first."""
        state = self.load()
        if not self._unnumbered(state):
            return state

        def change(current: str | None) -> tuple[str | None, list[tuple[str, str | None, str]], dict[str, Any]]:
            fresh = parse(current)
            if fresh.get("unreadable"):
                return None, [], fresh
            events = self._number(fresh, now)
            return (json.dumps(fresh, sort_keys=True) if events else None), events, fresh

        return self.store.update_meta(KEY, change, now)

    def take_releases(self, now: str) -> dict[str, str]:
        """Use up the release file for the stops in force; return {name: who and when} for those it ended.

        A line ends a stop only when it names that stop's serial, and ends that stop only; the errors up to now stop
        counting for it. A line used up for a stop that ended or changed between the file's reading and this
        transaction ends nothing. With no file configured nothing is released.
        """
        path = self.cfg.release_file
        if path is None:
            return {}
        state = self._numbered(now)
        if state.get("unreadable"):
            return {}  # a record nobody can read is not ended by a name in a file: fix it, restart
        stops = {m: release.serial_of(s) for m, s in state["models"].items()}
        if state["all"] is not None:
            stops[ALL] = release.serial_of(state["all"])
        serials = {name: serial for name, serial in stops.items() if serial is not None}
        taken, refused = release.take_listed(path, serials)
        release.say_refused(self.store, path, refused, self._said, "harness_error_release_refused", WHAT, now)
        if not taken:
            return {}

        def change(current: str | None) -> tuple[str | None, list[tuple[str, str | None, str]], dict[str, str]]:
            now_state = parse(current)
            if now_state.get("unreadable"):
                return None, [], {}
            events, ended = [], {}
            for name, who in sorted(taken.items()):
                stop, serial = now_state["all"] if name == ALL else now_state["models"].get(name), serials[name]
                if release.serial_of(stop) != serial:
                    line = release.line_for(name, serial)
                    log.warning("%s: the line '%s' ends nothing: its stop ended or changed as it was read", path, line)
                    continue
                ended[name] = who
                if name == ALL:
                    for kind in now_state["all"].get("kinds", []):
                        now_state["cleared"][f"{kind}|*"] = _epoch(now)
                    now_state["all"] = None
                    events.append(("harness_error_released_all", None, f"the all-stop (stop {serial}) ended by {who}"))
                else:
                    kind = now_state["models"].pop(name).get("kind", UNKNOWN_KIND)
                    now_state["cleared"][f"{kind}|{name}"] = _epoch(now)
                    said = f"the stop on {name} (stop {serial}) ended by {who}"
                    events.append(("harness_error_released", name, said))
            return (json.dumps(now_state, sort_keys=True) if ended else None), events, ended

        return self.store.update_meta(KEY, change, now)

    def status(self) -> dict[str, Any]:
        """Return the stops in force, their serials and the errors that count now, for the status view."""
        state = self.load()
        inwin = counted(stored_errors(self.store), state.get("cleared", {}), time.time(), self.cfg.window_s)
        per: dict[str, dict[str, int]] = {}
        for e in inwin:
            per.setdefault(e["kind"], {})[e["model"]] = per.setdefault(e["kind"], {}).get(e["model"], 0) + 1
        serials = {m: release.serial_of(s) for m, s in sorted(state["models"].items())}
        if state["all"] is not None:
            serials[ALL] = release.serial_of(state["all"])
        return {
            "unreadable": bool(state.get("unreadable")),
            "stopped_models": {m: s.get("why") for m, s in sorted(state["models"].items())},
            "all_stop": None if state["all"] is None else state["all"].get("why"),
            "serials": serials,
            "in_window": per,
        }
