# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/harness_stop.py, blob e36b086b771945e2048ed94663467b51c27a89ab
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""The harness-error stops (science, #34, 3 Nov 01:08Z). A harness error is a bug of ours, not an outcome; its run is pending again after the fix. What it stops:

- a MODEL: `harness_error_stop_after` (3) errors of one KIND on that model within `harness_error_window_s` (6 h);
- ALL dispatch: one kind on `harness_error_models_stop` (3) different models, or `harness_error_option_stop` (5) errors of one kind on one option across models, in the window.

The kind is the exception's type name; a remote run reports every error as RemoteHarnessError with the real type first in its detail, so its kind is that inner type
(store.harness_error_kind): two different inner types do not pool. The errors are not counted anywhere: they are the `harness_error` attempts in the store, with their
own end times, so the 6 h window is read from store timestamps and a restart changes nothing. What is kept is a stop once it has fired (a window moving on never ends
one) and the releases (`cleared`): a release ends the stops at their limit and the errors up to then stop counting for that kind on that model (or on all models).

The release file lists names, one per line: a model's name ends that model's stop, the word ALL ends the all-stop. Each counts only if written after its stop and is used
once (release.take_listed); a name with no stop in force is left alone."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import time
from typing import Any

from .store import Store, harness_error_kind

KEY = "harness_error_stops"
ALL = "ALL"
LEGACY = ("harness_error_stopped_epoch", "harness_error_counts")      # the one-count design this replaces: a stop in force under it is carried over once


def _epoch(utc: str | None) -> float:
    try:
        return dt.datetime.fromisoformat(str(utc).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()            # an end time that cannot be read counts as now: toward the stop, never away from it


def stored_errors(store: Store) -> list[dict[str, Any]]:
    """Every harness-error attempt: when it ended, its kind, model, option and run."""
    return [{"epoch": _epoch(r["e"]), "kind": harness_error_kind(r["d"]), "model": r["model"], "option": r["option"], "run_key": r["run_key"]}
            for r in store.q("SELECT r.model, r.option, r.run_key, a.infra_detail d, a.ended_utc e FROM attempts a JOIN runs r USING(run_key) "
                             "WHERE a.state='harness_error' ORDER BY a.id")]


def counted(errors: list[dict[str, Any]], cleared: dict[str, float], upto: float, window_s: float) -> list[dict[str, Any]]:
    """The errors that count at time `upto`: inside the window, and after any release that cleared their kind on their model or on all models."""
    return [e for e in errors if e["epoch"] > upto - window_s
            and e["epoch"] > max(cleared.get(f"{e['kind']}|{e['model']}", 0.0), cleared.get(f"{e['kind']}|*", 0.0), cleared.get("*|*", 0.0))]


def fired(inwin: list[dict[str, Any]], kind: str, model: str, option: str, cfg: Any) -> tuple[str | None, str | None]:
    """(a model-stop reason, an all-stop reason) for the error just added, from the counted errors of its kind."""
    ek = [e for e in inwin if e["kind"] == kind]
    n_model = sum(1 for e in ek if e["model"] == model)
    models = sorted({e["model"] for e in ek})
    n_opt = sum(1 for e in ek if e["option"] == option)
    mstop = (f"{n_model} harness errors of kind {kind} on {model} within {cfg.harness_error_window_s / 3600:g} h" if n_model >= cfg.harness_error_stop_after else None)
    if len(models) >= cfg.harness_error_models_stop:
        return mstop, f"kind {kind} on {len(models)} models ({', '.join(models)}) within {cfg.harness_error_window_s / 3600:g} h"
    if n_opt >= cfg.harness_error_option_stop:
        return mstop, f"{n_opt} harness errors of kind {kind} on option {option} across models within {cfg.harness_error_window_s / 3600:g} h"
    return mstop, None


def empty() -> dict[str, Any]:
    return {"models": {}, "all": None, "cleared": {}}


def load(store: Store) -> dict[str, Any]:
    """The stops in force. A record that cannot be read stops everything (fail closed): {"unreadable": True}. No record is a campaign with no stop, except that one
    left by the earlier one-count design (a stop epoch and a count at its limit) is carried over as an all-stop, once, so a restart with this code does not lift it."""
    raw = store.get_meta(KEY)
    if raw is None:
        st = empty()
        legacy_epoch = store.get_meta(LEGACY[0])
        if legacy_epoch is not None:
            try:
                kinds = sorted(k for k, per in json.loads(store.get_meta(LEGACY[1]) or "{}").items() if sum(per.values()) >= 3)
                st["all"] = {"kinds": kinds or ["*"], "epoch": float(legacy_epoch), "utc": None, "why": "carried over from the earlier harness-error stop"}
            except (ValueError, TypeError, AttributeError):
                st["all"] = {"kinds": ["*"], "epoch": time.time(), "utc": None, "why": "carried over from the earlier harness-error stop"}
        return st
    try:
        d = json.loads(raw)
        if not (isinstance(d, dict) and isinstance(d.get("models"), dict) and isinstance(d.get("cleared"), dict) and (d.get("all") is None or isinstance(d["all"], dict))):
            raise ValueError
        return d
    except ValueError:
        return {"unreadable": True, **empty()}


def status(store: Store, cfg: Any, now: dt.datetime) -> dict[str, Any]:
    """For the status: what counts in the window now, what is stopped, and every error ever, per kind and option (the report, not the stop's count)."""
    st, errs = load(store), stored_errors(store)
    inwin = counted(errs, st.get("cleared", {}), now.timestamp(), cfg.harness_error_window_s)
    per: dict[str, dict[str, int]] = {}
    for e in inwin:
        per.setdefault(e["kind"], {})[e["model"]] = per.setdefault(e["kind"], {}).get(e["model"], 0) + 1
    all_time_kind: dict[str, int] = {}
    all_time_opt: dict[str, dict[str, int]] = {}
    for e in errs:
        all_time_kind[e["kind"]] = all_time_kind.get(e["kind"], 0) + 1
        o = all_time_opt.setdefault(e["option"], {})
        o[e["kind"]] = o.get(e["kind"], 0) + 1
    return {"stop_after": cfg.harness_error_stop_after, "window_s": cfg.harness_error_window_s, "models_stop_after": cfg.harness_error_models_stop,
            "option_stop_after": cfg.harness_error_option_stop, "in_window": per, "unreadable": bool(st.get("unreadable")),
            "stopped_models": {m: {"kind": s.get("kind"), "utc": s.get("utc"), "why": s.get("why")} for m, s in sorted(st["models"].items())},
            "all_stop": None if st["all"] is None else {k: st["all"].get(k) for k in ("kinds", "utc", "why")},
            "all_time_by_kind": dict(sorted(all_time_kind.items())), "all_time_by_option": {o: dict(sorted(v.items())) for o, v in sorted(all_time_opt.items())}}


def alarms(h: dict[str, Any], cfg: Any) -> list[str]:
    out = []
    f = cfg.harness_error_release_file
    if h["unreadable"]:
        out.append(f"the harness-error record is unreadable: dispatch of ALL options is STOPPED (fail closed); resume by writing the word {ALL} in {f} after the campaign is restarted with the fix")
    for m, s in h["stopped_models"].items():
        out.append(f"HARNESS ERRORS: {s['why']}: dispatch of {m} is STOPPED, the other models go on (runs in flight finish; a bug of ours, the runs are pending again when the campaign "
                   f"restarts with the fix); then resume {m} by writing its name on a line of {f} now: it ends this stop only, and one older than the stop does not count")
    if h["all_stop"]:
        out.append(f"HARNESS ERRORS: {h['all_stop']['why']}: dispatch of ALL options and models is STOPPED (runs in flight finish; the runs are pending again when the campaign restarts "
                   f"with the fix); then resume by writing the word {ALL} on a line of {f} now: it ends this stop only, and one older than the stop does not count")
    if h["in_window"] and not (h["stopped_models"] or h["all_stop"] or h["unreadable"]):
        said = "; ".join(f"{k}: " + ", ".join(f"{m} {n}" for m, n in sorted(per.items())) for k, per in sorted(h["in_window"].items()))
        out.append(f"harness errors in the last {h['window_s'] / 3600:g} h, by kind and model ({said}): each run is pending again when the campaign restarts with the fix; a model stops at "
                   f"{h['stop_after']} of one kind, all dispatch at one kind on {h['models_stop_after']} models or {h['option_stop_after']} on one option")
    return out
