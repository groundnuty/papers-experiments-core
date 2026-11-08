# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/campaign/scheduler.py:2379-2494
"""Endpoint health per concurrency unit (C15).

A unit is any key a platform names for the thing runs share and that can fail together: an endpoint, a provider. Runs
of every model that use the unit count toward its health.

After `fail_threshold` consecutive failures the unit is paused for `pause_s` seconds: no new run starts on it, the
others go on. When the pause ends, dispatch may probe the unit with a run. **The pause is cleared only by a success**:
a unit that is down and fails again is paused again on that one failure, and its outage window stays open until a run
succeeds. A success resets the count of failures. The state is kept in the store, so a restart does not forget a pause
or end an outage without a success.

**A record that cannot be read fails safe**: whether any unit is down is then unknown, so every unit is paused for
`pause_s` and stays down until a success on it clears it, and the event is logged. The record keeps this as the entry
`"*"`, the state of every unit it does not list, so a restart in the middle of it does not forget it. One run may probe
a unit when its pause ends, as after any pause, so a unit that is up recovers by itself.

A failure here is the endpoint's: an infrastructure failure of kind `endpoint`, a refused key, or a changed served
identity. **A treatment failure is neither a failure nor a success**: under a condition that declares an infrastructure
kind as its treatment the outage is the manipulation, so it must not pause the unit or clear its pause. The caller
leaves such attempts out; this class does not know about conditions.
"""

import json
import threading
from collections.abc import Callable
from typing import Any

from expcore.campaign.store import Store

META = "unit_health"
EVERY_OTHER = "*"  # the state of every unit the record does not list: set when the record could not be read


class UnitHealth:
    """Failures, pauses and outage windows per unit, kept in the store's meta."""

    def __init__(
        self,
        store: Store,
        *,
        fail_threshold: int,
        pause_s: float,
        now: Callable[[], float],
        stamp: Callable[[], str],
    ):
        if fail_threshold < 1 or pause_s <= 0:
            raise ValueError("fail_threshold must be at least 1 and pause_s positive")
        self.store, self.fail_threshold, self.pause_s = store, fail_threshold, pause_s
        self._now, self._stamp = now, stamp  # epoch seconds, and the same moment as a store timestamp
        self._lock = threading.Lock()  # update and persist of the units is one atomic step
        self._units: dict[str, dict[str, Any]] = {}
        self.load()

    # --- state ----------------------------------------------------------------------------------------------------

    def load(self) -> None:
        """Restore the units after a restart. A record that cannot be read pauses every unit until a success on it."""
        raw = self.store.get_meta(META)
        try:
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
                raise ValueError("not a record of units")
        except ValueError:
            data = {EVERY_OTHER: {"fails": 0, "down": True, "pause_until": self._now() + self.pause_s}}
            self._units = data
            self._save()
            self.store.event(
                self._stamp(),
                "unit_record_unreadable",
                None,
                f"{META} cannot be read: every unit is paused for {self.pause_s:g} s and stays down until a run "
                "on it succeeds",
            )
        self._units = data

    def _fresh(self) -> dict[str, Any]:
        """Return the state of a unit the record does not list: healthy, or down when the record was unreadable."""
        return dict(self._units.get(EVERY_OTHER, {"fails": 0, "down": False, "pause_until": 0.0}))

    def _state(self, unit: str) -> dict[str, Any]:
        """Return the unit's state without recording it."""
        return self._units[unit] if unit in self._units else self._fresh()

    def _unit(self, unit: str) -> dict[str, Any]:
        return self._units.setdefault(unit, self._fresh())

    def _save(self) -> None:
        self.store.put_meta(META, json.dumps(self._units, sort_keys=True), self._stamp())

    # --- questions and answers --------------------------------------------------------------------------------------

    def ok(self, unit: str) -> bool:
        """Tell whether dispatch may start a run on the unit: not paused (after a pause, a run may probe it)."""
        with self._lock:
            return self._now() >= float(self._state(unit).get("pause_until", 0.0))

    def down(self, unit: str) -> bool:
        """Tell whether the unit's outage window is open: paused, or failed since and not yet cleared by a success."""
        with self._lock:
            return bool(self._state(unit).get("down", False))

    def paused_until(self, unit: str) -> float:
        """Return the epoch time the unit's pause ends; 0 when it is not paused."""
        with self._lock:
            return float(self._state(unit).get("pause_until", 0.0))

    def failure(self, unit: str, *, immediate: bool = False, detail: str = "") -> bool:
        """Count a failure; pause the unit at the threshold, or at once when it is down already or `immediate`.

        Return True when this failure paused the unit.
        """
        with self._lock:
            state = self._unit(unit)
            state["fails"] = int(state["fails"]) + 1
            pause = immediate or state["down"] or state["fails"] >= self.fail_threshold
            if pause:
                first = not state["down"]
                state["pause_until"] = self._now() + self.pause_s
                state["down"], state["fails"] = True, 0
            self._save()
            if pause:
                self.store.event(
                    self._stamp(),
                    "unit_paused" if first else "unit_paused_again",
                    None,
                    f"{unit}: no new run starts on it for {self.pause_s:g} s; only a success clears it: {detail[:120]}",
                )
            return pause

    def success(self, unit: str) -> bool:
        """Count a success: the failures reset, the pause and the outage window end. True if it cleared a pause."""
        with self._lock:
            state = self._state(unit)
            if not (state["fails"] or state["down"] or state["pause_until"]):
                return False
            cleared = bool(state["down"])
            self._units[unit] = {"fails": 0, "down": False, "pause_until": 0.0}
            self._save()
            if cleared:
                self.store.event(self._stamp(), "unit_recovered", None, f"{unit}: a run succeeded; the outage is over")
            return cleared

    def status(self) -> dict[str, dict[str, Any]]:
        """Return a copy of every unit's state for the status view."""
        with self._lock:
            return {u: dict(s) for u, s in sorted(self._units.items())}
