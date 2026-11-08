# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/healthgate.py, blob d11e173f034051b52302d1d23168ac34c6f19f2a
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""The Onedata health gate's outage log (the queue worker's report D.1(b); science's ruling, #34, 3 Nov 03:27Z).

A failed pre-run health check closes the gate: no run starts on any host until the dispatcher's own check passes, which it runs itself every `onedata_pause_s`.
The gate is global because the check is: `Campaign._work_inner` runs it on the dispatcher host for every run, whichever host will execute it (remote.py has none).
One infrastructure attempt is charged per outage, to the run that found it.

Each outage is one record in the store's meta `health_outages`, written when the gate closes, at every check and when it opens, so a restart in the outage restores it
(`restarts` counts those) and the status and the stability report list it from the same record: when it began, when it ended, and how.

    {"start_utc", "end_utc": null while open, "how": null | "check_passed", "checks": checks made by the dispatcher itself, the passing one included,
     "restarts": campaign restarts survived while open, "last_check_utc", "first_detail", "last_detail"}

`first_detail` and `last_detail` are the health check's own words for the status; the stability report never prints them.

Post-run checks (science, #34, 3 Nov 04:24Z). A run whose post-run check fails while the gate is closed, or as it closes, is retried without the attempt counting toward its 3: the
attempt is kept with its tokens as a discarded attempt (state infra_fail, kind `onedata_outage`, left out of `store.infra_attempts`), and the outage's record counts it
(`discarded`, `discarded_input_tokens`, `discarded_output_tokens`, `discarded_usage_unknown`). The definition is by ORDER, with no clock window: the failed check is the outage's
exactly when the gate is CLOSED AT THE INSTANT THE CHECK RETURNS. That covers a check begun after the gate closed, and one in flight as it closed (begun before, returned
after: it lasts at most the check's own timeouts). Nothing is reclassified afterwards: a failed post-run check that returned while the gate was still open (a run that finished
before the outage, or whose check failed alone, or the first sign of an outage) is counted as today and does not close the gate; the next pre-run check that fails does, and is the
outage's one attempt. So an outage first shown by a post-run check can charge two attempts, and a canary that finds one charges none (30(e)).
"""
from __future__ import annotations

import json
from typing import Any

META = "health_outages"
HOW_PASSED = "check_passed"            # the dispatcher's own health check passed: dispatch resumed by itself
HOWS = (HOW_PASSED,)
KEEP = 200                             # the newest this many outages are kept


def parse(raw: str | None) -> list[dict[str, Any]] | None:
    """The recorded outages, oldest first; [] when none was ever recorded; None when the value cannot be read as a record (unknown, never 'none')."""
    if not raw:
        return []
    try:
        d = json.loads(raw)
        outs = d["outages"]
        if not isinstance(outs, list) or not all(isinstance(o, dict) and isinstance(o.get("start_utc"), str) for o in outs):
            raise ValueError("not a list of outages")
    except (ValueError, KeyError, TypeError):
        return None
    return outs


def dump(outages: list[dict[str, Any]]) -> str:
    return json.dumps({"outages": outages[-KEEP:]}, sort_keys=True)


def fact(raw: str | None) -> dict[str, Any]:
    """The status fact: {"readable", "outages", "open"}; an unreadable record is readable False with no outages listed, never an empty list."""
    outs = parse(raw)
    if outs is None:
        return {"readable": False, "outages": None, "open": None}
    return {"readable": True, "outages": outs, "open": next((o for o in reversed(outs) if o.get("end_utc") is None), None)}


def alarms(f: dict[str, Any], every_s: float) -> list[str]:
    """The status alarm for the gate being closed now (or its record unreadable)."""
    if not f["readable"]:
        return ["ONEDATA HEALTH GATE: the outage record in the store is unreadable, so whether the gate is closed is unknown (a restart treats it as closed until a check passes)"]
    o = f["open"]
    if o is None:
        return []
    last = o.get("last_detail") or o.get("first_detail") or "no detail"
    return [f"ONEDATA HEALTH GATE CLOSED since {o['start_utc']}: no run starts on any host; the dispatcher re-checks every {every_s:g} s and resumes by itself on the first pass "
            f"({o.get('checks', 0)} check(s) so far, last {o.get('last_check_utc') or 'not yet'}: {str(last)[:160]}); one infra attempt was charged, to the run that found it"]
