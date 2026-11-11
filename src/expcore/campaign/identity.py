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

A declared set of accepted fingerprints (B18). A load-balanced endpoint can serve one model from several deployments,
each with its own fingerprint, within one run. A model whose entry declares `accepted_fingerprints` is checked against
that set instead of a fingerprint baseline: a reply served under a fingerprint of the set is the model, one under any
other is a change as above, and a reply that carries several fingerprints is the model only when the set lists each
of them. The served model id is checked against its baseline as before. Whether the deployments are the same model
(the same weights, the same precision) is the study's decision; the set records it, and the campaign fingerprint
freezes it (`expcore.study.pipeline.model_digest`).

A declared fingerprint is confirmed by its first witness: the admission probe, or the first run with a reply that
showed it under the model's own id (its API id or an alias). That is a record of observation, not a gate: a run is
not set aside for showing a declared fingerprint first, since its retry would accept exactly what was refused. A
fingerprint no reply has shown stays unconfirmed, and the export and the report say so. Admission is a gate: a probe
served by another model id, or under a fingerprint outside the set, does not admit the model, since the declaration
does not describe that endpoint. The set and its confirmations are kept in the store's meta (`ACCEPTED_META`), and
each attempt of such a model records its deployment mix, the turns each fingerprint served (`deployment_mix`). A model
that declares no set is checked as before, and nothing of this is written for it.
"""

import json
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from expcore.campaign.deployments import ACCEPTED_META
from expcore.campaign.store import Store
from expcore.config.models import ModelSpec
from expcore.models.served import MIXED_FINGERPRINTS, Baseline, same_model

__all__ = ["ACCEPTED_META", "META", "ServedIdentities", "parts"]

META = "served_baselines"
ADMISSION_PROBE = "admission_probe"
RUN = "run"


def parts(fingerprint: str) -> list[str]:
    """The fingerprints a reply carried: one, or several joined into a mixed value."""
    return fingerprint.split(MIXED_FINGERPRINTS)


class ServedIdentities:
    """One baseline of served identity per model, kept in the store's meta, and the declared sets of accepted
    fingerprints with their confirmations (B18). `declared` holds the planned models' entries; those without a set
    are ignored."""

    def __init__(self, store: Store, stamp: Callable[[], str], declared: Mapping[str, ModelSpec] | None = None):
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
        self._declared = {m: spec for m, spec in (declared or {}).items() if spec.accepted_fingerprints}
        self._accepted: dict[str, dict[str, Any]] = {}
        if self._declared:
            self._open_declared(stamp())

    def baseline(self, model: str) -> dict[str, str | None] | None:
        """Return the baseline of a model as a dict, or None before its first reply."""
        with self._lock:
            found = self._baselines.get(model)
            return None if found is None else found.to_dict()

    def check(
        self, model: str, transcript: Sequence[Mapping[str, Any]], now: str, run_key: str | None = None
    ) -> dict[str, Any] | None:
        """Compare the replies of a run with the model's baseline, in order, and learn from the first.

        Return None when nothing changed, else the first change: the first and the changed served model or fingerprint,
        or, for a model that declares a set, the fingerprint outside it with the set. A baseline set by this check is
        saved, and so is each declared fingerprint the run confirmed (`run_key` names the run that did).
        """
        with self._lock:
            spec = self._declared.get(model)
            known = self._baselines.get(model)
            before = None if known is None else known.to_dict()
            baseline = known if known is not None else Baseline()
            change = None
            for turn in transcript:
                served, fingerprint = turn.get("served_model"), turn.get("system_fingerprint")
                if spec is None:
                    found = baseline.check(served, fingerprint)
                else:  # the set replaces the fingerprint baseline; the served model keeps its own
                    found = baseline.check(served, None) or _outside(spec, fingerprint)
                if found is not None and change is None:
                    change = found
            after = baseline.to_dict()
            if after != before and (baseline.served_model or baseline.system_fingerprint):
                self._baselines[model] = baseline  # set by this run, or learned a fingerprint it lacked
                self._save(now)
            if spec is not None:
                shown = [turn.get("system_fingerprint") for turn in transcript if _own(spec, turn.get("served_model"))]
                self._confirm(model, [p for f in shown if f for p in parts(f)], RUN, run_key, now)
            return change

    def admit(self, model: str, backend: Any, now: str) -> str | None:
        """Hold the admission probe of a model that declares a set to it; None when the model may be dispatched.

        The probe's reply is read from the backend's `info()` (`served_model_ids`, `system_fingerprints`). A reply
        served by another model id, or under a fingerprint outside the set, refuses the model. One served by the model
        confirms the fingerprints it showed. A model that declares no set is not looked at.
        """
        spec = self._declared.get(model)
        if spec is None:
            return None
        info = backend.info()
        ids = [str(i) for i in info.get("served_model_ids") or () if i]
        fingerprints = [str(f) for f in info.get("system_fingerprints") or () if f]
        other = [i for i in ids if not _own(spec, i)]
        if other:
            return (
                f"the admission probe was served by model id {', '.join(other)}, not {spec.api_id}; a fingerprint it "
                "shows says nothing of the deployments of this model"
            )
        outside = [f for f in fingerprints if _outside(spec, f)]
        if outside:
            return (
                f"the admission probe was served under system fingerprint {outside[0]!r}, which accepted_fingerprints "
                f"({', '.join(spec.accepted_fingerprints)}) does not list"
            )
        with self._lock:
            self._confirm(model, [p for f in fingerprints for p in parts(f)] if ids else [], ADMISSION_PROBE, None, now)
            unconfirmed = [f for f in spec.accepted_fingerprints if f not in self._accepted[model]["confirmed"]]
        if unconfirmed:
            self.store.event(
                now,
                "accepted_fingerprints_unconfirmed",
                model,
                f"{model}: {', '.join(unconfirmed)} declared in accepted_fingerprints and not observed yet; each is "
                "confirmed by the first reply that shows it under the model's id",
            )
        return None

    def deployment_mix(self, model: str, transcript: Sequence[Mapping[str, Any]]) -> str | None:
        """The turns each fingerprint served in a run of a model that declares a set, as JSON pairs in fingerprint
        order, a turn whose reply carried none counted under `null` last; None for a model that declares no set."""
        if model not in self._declared:
            return None
        served = Counter(turn.get("system_fingerprint") or None for turn in transcript)
        pairs = sorted(served.items(), key=lambda item: (item[0] is None, item[0] or ""))
        return json.dumps([[fingerprint, turns] for fingerprint, turns in pairs])

    def reset(self, model: str, now: str) -> None:
        """Forget a model's baseline: the endpoint moved on purpose, and the next reply sets the new one."""
        with self._lock:
            if self._baselines.pop(model, None) is not None:
                self._save(now)
                self.store.event(now, "served_baseline_reset", model, f"{model}: the pinned identity was reset")

    def _save(self, now: str) -> None:
        text = json.dumps({m: b.to_dict() for m, b in sorted(self._baselines.items())}, sort_keys=True)
        self.store.put_meta(META, text, now)

    def _open_declared(self, now: str) -> None:
        """Record each declared set, keeping the confirmations an earlier start made of fingerprints it still lists."""
        raw = self.store.get_meta(ACCEPTED_META)
        try:
            data = json.loads(raw) if raw else {}
            kept = {m: dict(entry["confirmed"]) for m, entry in data.items()}
        except (ValueError, AttributeError, TypeError, KeyError):
            self.store.event(
                now,
                "accepted_fingerprints_unreadable",
                None,
                f"{ACCEPTED_META} cannot be read; no fingerprint counts as observed until a reply shows it again",
            )
            data, kept = {}, {}
        for model, spec in self._declared.items():
            confirmed = {f: w for f, w in kept.get(model, {}).items() if f in spec.accepted_fingerprints}
            self._accepted[model] = {"accepted": list(spec.accepted_fingerprints), "confirmed": confirmed}
        self.store.put_meta(ACCEPTED_META, json.dumps({**data, **self._accepted}, sort_keys=True), now)

    def _confirm(self, model: str, shown: list[str], by: str, run_key: str | None, now: str) -> None:
        """Confirm each declared fingerprint `shown` that no witness confirmed before; call with the lock held."""
        entry = self._accepted[model]
        new = [f for f in dict.fromkeys(shown) if f in entry["accepted"] and f not in entry["confirmed"]]
        if not new:
            return
        for fingerprint in new:
            entry["confirmed"][fingerprint] = {"by": by, "run": run_key, "at": now}
        raw = self.store.get_meta(ACCEPTED_META)
        data = {**(json.loads(raw) if raw else {}), model: entry}
        witness = "the admission probe" if by == ADMISSION_PROBE else f"run {run_key}"
        events = [("accepted_fingerprint_confirmed", model, f"{model}: {f} observed by {witness}") for f in new]
        self.store.put_meta_many({ACCEPTED_META: json.dumps(data, sort_keys=True)}, now, events)


def _own(spec: ModelSpec, served: str | None) -> bool:
    """Tell whether a served model id is the entry's own: its API id or an alias, or a dated snapshot of either."""
    return bool(served) and any(same_model(name, str(served)) for name in (spec.api_id, *spec.aliases))


def _outside(spec: ModelSpec, fingerprint: str | None) -> dict[str, Any] | None:
    """The change a fingerprint is under the entry's declared set: None when the reply carried none, or when the set
    lists every fingerprint it carried."""
    if not fingerprint or all(p in spec.accepted_fingerprints for p in parts(fingerprint)):
        return None
    return {"system_fingerprint": fingerprint, "accepted_fingerprints": list(spec.accepted_fingerprints)}
