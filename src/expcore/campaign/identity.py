# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/campaign/scheduler.py:1004-1023
"""An endpoint whose served identity changed makes the run that saw it infrastructure (C16).

A server says which model it ran and often a system fingerprint (the served-model check, `expcore.models.served`). The
first identity a model is served with is its baseline, kept in the store so a restart keeps it. A run whose replies
carry another served model, a changed fingerprint, or a mixed fingerprint was not served by the endpoint that was
admitted, so its outcome says nothing about the model: the run is infrastructure and is run again, and the endpoint
counts one failure toward its unit's health (C15).

The comparison is over the replies of the whole run, in order, against the baseline. A run that starts on one identity
and ends on another is caught as surely as one that starts on a changed endpoint. The baseline is pinned: an endpoint
that moved on purpose is accepted by `reset`, an operator's decision and not the scheduler's.
"""

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from expcore.campaign.store import Store
from expcore.models.served import Baseline

META = "served_baselines"


class ServedIdentities:
    """One baseline of served identity per model, kept in the store's meta."""

    def __init__(self, store: Store, stamp: Callable[[], str]):
        self.store = store
        self._lock = threading.Lock()
        self._baselines: dict[str, Baseline] = {}
        raw = store.get_meta(META)
        try:
            data = json.loads(raw) if raw else {}
            self._baselines = {m: Baseline.from_dict(d) for m, d in data.items()}
        except (ValueError, AttributeError, TypeError):
            # An unreadable record forgets the baselines: the next run sets them afresh, and that is on the record.
            store.event(stamp(), "served_baselines_unreadable", None, f"{META} cannot be read; baselines start afresh")

    def baseline(self, model: str) -> dict[str, str | None] | None:
        """Return the baseline of a model as a dict, or None before its first reply."""
        with self._lock:
            found = self._baselines.get(model)
            return None if found is None else found.to_dict()

    def check(self, model: str, transcript: Sequence[Mapping[str, Any]], now: str) -> dict[str, str | None] | None:
        """Compare the replies of a run with the model's baseline, in order, and learn from the first.

        Return None when nothing changed, else the first change: the first and the changed served model or fingerprint.
        A baseline set by this check is saved.
        """
        with self._lock:
            known = self._baselines.get(model)
            before = None if known is None else known.to_dict()
            baseline = known if known is not None else Baseline()
            change = None
            for turn in transcript:
                found = baseline.check(turn.get("served_model"), turn.get("system_fingerprint"))
                if found is not None and change is None:
                    change = found
            after = baseline.to_dict()
            if after != before and (baseline.served_model or baseline.system_fingerprint):
                self._baselines[model] = baseline  # set by this run, or learned a fingerprint it lacked
                self._save(now)
            return change

    def reset(self, model: str, now: str) -> None:
        """Forget a model's baseline: the endpoint moved on purpose, and the next reply sets the new one."""
        with self._lock:
            if self._baselines.pop(model, None) is not None:
                self._save(now)
                self.store.event(now, "served_baseline_reset", model, f"{model}: the pinned identity was reset")

    def _save(self, now: str) -> None:
        text = json.dumps({m: b.to_dict() for m, b in sorted(self._baselines.items())}, sort_keys=True)
        self.store.put_meta(META, text, now)
