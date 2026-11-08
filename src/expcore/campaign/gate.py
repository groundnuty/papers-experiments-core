# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/gate.py, blob 83b918421c9430284da616edd24d91ea23475549
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""The operator's model_66 gating (2 Nov 01:15Z, via the science agent; amendment 18(e) as changed by 29(d)). SCHEDULING ONLY: model_66 keeps its full registered set.

In the main block (not the pilot and calibration barriers) model_66 runs dispatch only after calibration's model_66 runs and the first
model_67 main runs are priced: until then every model_66 seed waits. The projected Claude total is then fixed and shown in the status. At most `gate_max_usd`:
model_66 proceeds on its own. Above it: a LATER seed's model_66 runs hold until the release flag file exists; the seed in progress is never held by it. model_67 and
the open models are never held by it. An unknown projection (a price not set, a model with no priced runs) holds the later seeds, like an over-limit one; the
release flag lifts it.

Projection (stated assumption; amendment 29(d)): the past test spend + a fixed allowance for the Claude coders (`gate_coder_allowance_usd`; dispatch does
not depend on coding's progress, so their actual spend is read by the spend alarm and the report, never here) + spent so far on every Claude model + for each
Claude model its mean cost per run (model_66: its calibration runs; model_67: its first `gate_model_67_runs` main runs completed) x its runs still to come in THE SEED
IN PROGRESS AND THE NEXT SEED, rather than every pending run, plus its runs in flight (pending, already started: their cost is not recorded yet).

The seed in progress, for a model: the lowest seed with a main-block run that is pending and has not started (no attempt other than a refusal to start a sandbox).
A run stays 'pending' until it ends, so a seed whose runs are all in flight is no longer the one in progress. The next seed is the next higher seed with such
a run. The decision is made once for each seed in progress, at the rates so far, and a release counts only if written after it (a release given for one seed's
decision does not carry to the next).

The gate keeps the highest seed it has AUTHORISED (meta `model_66_gate_authorised`, only ever rising). The first seed in progress is authorised at the first decision, and
a `proceed` or a release authorises the seed in progress. While the state is hold_over or hold_unknown with no release, the seeds ABOVE that seed are held, whichever seed is
in progress now: when a seed's last run starts, the next seed becomes the one in progress, and with no release written after its own decision it stays held (amendment 29(d):
a later seed's model_66 runs hold when the projection reaches the limit; science, 3 Nov 04:03Z: the overrun in the seed in progress is accepted). Dispatch also keeps the seeds
above the authorised one back while the gate is NOT holding, until the next decision is made (`blocked_by_gate`): a decision read a few seconds ago must not let the next
seed start before its own. Only the scheduler (`latch=True`) writes the record.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from . import attempt_bounds, release
from .config import CampaignConfig
from .store import Store

BILLED = "('done','infra_fail','harness_error','interrupted')"
META = "model_66_gate"
META_UNKNOWN = "model_66_gate_unknown_since"     # a projection that cannot be made is not latched, so a release needs another moment to be newer than
META_UNKNOWN_SEED = "model_66_gate_unknown_seed"  # the seed in progress it was stamped for: a new seed in progress is a new moment
META_AUTH = "model_66_gate_authorised"            # the highest seed the gate has let go on; it only rises, and survives a restart


def _authorised(store: Store, d: dict[str, Any] | None) -> int | None:
    """The highest seed authorised so far, or None before the first decision. A record that cannot be read authorises nothing (0: every seed waits). A store with a
    latched decision but no record (made before it was kept) takes the seed of that decision, never the seed in progress now, which would reopen the hold."""
    raw = store.get_meta(META_AUTH)
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            return 0
    if d is not None and isinstance(d.get("seed"), int):
        return d["seed"]
    return None


def _mean_usd(store: Store, cfg: CampaignConfig, model: str, keys: list[str]) -> float | None:
    """The mean cost of a price-sample run: the tokens of its attempts with COMPLETE usage only. The partial tokens an incomplete attempt recorded are no part of what a run costs
    (the run's own measured usage is its complete attempts'); they are counted once, in the total spent, with the bound on the request that was cut (`_project`)."""
    from .progress import _usd
    if not keys:
        return None
    ph = ",".join("?" * len(keys))
    r = store.q(f"SELECT SUM(input_tokens) i, SUM(output_tokens) o, SUM(cache_creation_tokens) cw, SUM(cache_read_tokens) cr "
                f"FROM attempts WHERE state IN {BILLED} AND COALESCE(usage_complete,1)=1 AND run_key IN ({ph})", keys)[0]
    u = _usd(cfg, model, r["i"] or 0, r["o"] or 0, r["cw"] or 0, r["cr"] or 0)
    return None if u is None else u / len(keys)


def _with_tokens(store: Store, keys: list[str]) -> list[str]:
    """The runs that are price samples: each has MEASURED usage, a complete attempt (usage_complete 1) that recorded input tokens, and its finishing attempt (`done`) is complete too.
    A run finished with unknown usage (a request abandoned before its first reply, or after some) would otherwise count as a cheap sample and could release model_66 for good; and the
    tokens an incomplete attempt did record are partial, so they are no measured ones (a run with only incomplete attempts is no sample).
    Any OTHER billed attempt of the run may have incomplete usage, whatever it recorded (science, #34, 4 Nov 00:32Z): an `interrupted` attempt, killed by a restart before its outcome
    was recorded, beside the `done` attempt that holds the run's measured usage, no longer unprices the run. Such an attempt is no sample: it is charged by the rule the spend alarm
    uses (progress.bounded_usage: what it recorded plus the request that was cut, or with nothing recorded the ceiling of one attempt, the run token budget at the dearest rate, or
    the budget a record `attempt_bounds` gives for it; never 0), a bound that moves the projected total by exactly its dollars and never the means (`_mean_usd` counts only the
    tokens of attempts with complete usage). The rule is general, not for the one attempt: it holds for every incomplete attempt of a run with a measured one, model_67's too. A price still unset
    makes the projection unknown (`_project`), as before."""
    if not keys:
        return []
    ph = ",".join("?" * len(keys))
    ok = {r["run_key"] for r in store.q(f"SELECT run_key FROM attempts WHERE state IN {BILLED} AND run_key IN ({ph}) GROUP BY run_key "
                                         "HAVING COALESCE(SUM(CASE WHEN COALESCE(usage_complete,1)=1 THEN input_tokens END),0) > 0 "
                                         "AND MIN(CASE WHEN state='done' THEN COALESCE(usage_complete,1) ELSE 1 END) = 1", keys)}
    return [k for k in keys if k in ok]


STARTED = ("(EXISTS (SELECT 1 FROM attempts a WHERE a.run_key=r.run_key AND a.state != 'unstarted') OR r.deferred_utc IS NOT NULL)")     # a refusal to start a sandbox is no start;
# a run deferred by its host's M or D cap counts as started, as at the seed boundary (amendments 24 and 30(f)): the next seed's decision does not wait for it


def _pending_runs(store: Store, cfg: CampaignConfig, keys: set[str] | None) -> list[Any]:
    return [r for r in store.q(f"SELECT r.run_key, r.model, r.seed, r.block, {STARTED} started FROM runs r WHERE r.status='pending'")
            if r["model"] in cfg.api_models and (keys is None or r["run_key"] in keys)]


def _windows(cfg: CampaignConfig, pend: list[Any]) -> dict[str, list[int]]:
    """Per Claude model, the seed in progress and the next: the two lowest seeds with a main-block run pending and not started."""
    seeds: dict[str, set[int]] = {}
    for r in pend:
        if not r["started"] and r["block"] not in cfg.barrier_blocks:
            seeds.setdefault(r["model"], set()).add(r["seed"])
    return {m: sorted(v)[:2] for m, v in seeds.items()}


def held_by_gate(g: dict[str, Any], seed: int) -> bool:
    """Does the gate hold a model_66 main-block run of this seed? Before pricing every seed waits (`hold_after_seed` 0); on an over-limit or unknown projection
    the seeds after the highest authorised one (the first seed in progress, then each that a `proceed` or a release authorised). A gate dict that says held and names no
    seed holds all of them (the conservative reading)."""
    if not g.get("held"):
        return False
    if "hold_after_seed" not in g:
        return True
    after = g["hold_after_seed"]
    return after is not None and seed > after


def blocked_by_gate(g: dict[str, Any], seed: int) -> bool:
    """Does dispatch keep a model_66 main-block run of this seed back now? What the gate holds, and also, while it is not holding, the seeds above the authorised one: the
    decision on hand was made for an earlier seed in progress, and the next seed starts only on its own (the gate is read every few seconds, and the last run of a
    seed can start in between)."""
    if held_by_gate(g, seed):
        return True
    auth = g.get("authorised_seed")
    return auth is not None and seed > auth


def evaluate(store: Store, cfg: CampaignConfig, keys: set[str] | None = None, latch: bool = True) -> dict[str, Any]:
    """The gate's state: off | hold_unpriced | proceed | hold_over | hold_unknown, plus `held` (does model_66 wait right now).
    `latch=False` (the status process) only reads a decision the scheduler already made; it never saves one, so a status run with other
    settings cannot authorize dispatch."""
    from .progress import _usd
    sm, hm = cfg.gate_model, cfg.gate_model_67_model
    out: dict[str, Any] = {"model": sm, "max_usd": cfg.gate_max_usd, "release_file": cfg.gate_release_file,
                           "released": False, "released_by": None,
                           "projected_usd": None, "state": "off", "held": False}
    if not sm or sm not in cfg.api_models:
        return out
    ab = attempt_bounds.read(store, BILLED)        # the budgets an attempt ran under, from the store: read at every evaluation, never kept (attempt_bounds.py)
    if ab.present:
        out["attempt_bounds"] = {"in_force": {str(i): e for i, e in sorted(ab.entries.items())}, "ignored": ab.ignored}
    inrun = (lambda ks: [k for k in ks if keys is None or k in keys])
    barriers = list(cfg.barrier_blocks)
    bph = ",".join("?" * len(barriers)) or "''"
    cal = _with_tokens(store, inrun([r["run_key"] for r in store.q(
        "SELECT run_key FROM runs WHERE model=? AND status='done' AND block='calibration'", [sm])]))   # not the pilot
    hk = _with_tokens(store, inrun([r["run_key"] for r in store.q(
        f"SELECT r.run_key, MAX(a.ended_utc) e FROM runs r JOIN attempts a USING(run_key) WHERE r.model=? AND r.status='done' "
        f"AND r.block NOT IN ({bph}) AND a.state='done' GROUP BY r.run_key ORDER BY e, r.seq", [hm, *barriers])]))[:cfg.gate_model_67_runs]
    out["priced_runs"] = {sm: len(cal), hm: len(hk), "need": {sm: cfg.gate_calibration_runs, hm: cfg.gate_model_67_runs}}
    if len(cal) < cfg.gate_calibration_runs or len(hk) < cfg.gate_model_67_runs:
        out["state"], out["held"], out["hold_after_seed"] = "hold_unpriced", True, 0           # every model_66 seed waits for the pricing
        return out
    stage = (_windows(cfg, _pending_runs(store, cfg, keys)).get(sm) or [None])[0]       # model_66's seed in progress; None: no model_66 run left to start
    out["seed_in_progress"] = stage
    latched = store.get_meta(META)
    d = json.loads(latched) if latched else None
    stored_auth = store.get_meta(META_AUTH)
    auth = _authorised(store, d)                  # the highest seed let go on, before this call's decision
    decided_for = d is not None and (d.get("seed") == stage or stage is None)           # decided for this seed in progress (or nothing is left to start)
    if decided_for:                   # decided once for this seed in progress: the decision does not drift with later runs
        out.update(state=d["state"], projected_usd=d["projected_usd"], decided_utc=d.get("decided_utc"), bounded=d.get("bounded", {}))
    elif not latch:                   # the status process: no decision has been saved for this seed, and it does not make one
        if d is not None:             # an earlier seed's decision stands on display, until the scheduler makes the next
            out.update(state=d["state"], projected_usd=d["projected_usd"], decided_utc=d.get("decided_utc"), bounded=d.get("bounded", {}),
                       note=f"decided for seed {d.get('seed')}; the scheduler has not yet decided for seed {stage}")
            stage = d.get("seed")
        else:
            out["state"], out["held"], out["hold_after_seed"] = "hold_unpriced", True, 0
            out["note"] = "priced runs are in, the scheduler has not yet decided"
            return out
    else:
        why: list[str] = list(ab.ignored)             # an entry of the record that was not used, and why
        out["state"], out["projected_usd"] = _project(store, cfg, keys, {sm: cal, hm: hk}, why, out, ab.budgets())
        if why:
            out["why"] = why
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if out["state"] != "hold_unknown":
            out["decided_utc"] = now
            store.put_meta(META, json.dumps({"state": out["state"], "projected_usd": out["projected_usd"], "decided_utc": now,
                                             "bounded": out.get("bounded", {}), "seed": stage, "max_usd": cfg.gate_max_usd}), now)
            bounded = out.get("bounded") or {}
            store.event(now, "model_66_gate", sm, f"seed {stage} in progress: projected Claude total {out['projected_usd']:.2f} USD vs {cfg.gate_max_usd:.2f}: {out['state']}"
                        + (f" ({sum(b['attempts'] for b in bounded.values())} attempt(s) bounded at an upper bound, ${sum(b['usd'] for b in bounded.values()):.2f})"
                           if bounded else ""))
        else:
            seen = store.get_meta(META_UNKNOWN_SEED)
            if store.get_meta(META_UNKNOWN) is None or (seen is not None and seen != str(stage)):      # a new seed in progress is a new moment
                store.put_meta_many({META_UNKNOWN: now, META_UNKNOWN_SEED: str(stage)}, now)
    # The release counts only if written after this outcome (the lead, 2 Nov): written earlier, nobody had seen the figure, and a file left over or
    # put there ahead of time would pass the $5k hold without it ever holding. One given while the total was unknown does not carry over to a known one.
    if cfg.gate_release_file:
        since = out.get("decided_utc") or store.get_meta(META_UNKNOWN)
        out["released_by"] = release.written_after(cfg.gate_release_file, release.epoch_of(since)) if since else None
        out["released"] = bool(out["released_by"])
    out["held"] = out["state"] in ("hold_over", "hold_unknown") and not out["released"]
    # Which seed this decision lets go on. The first seed in progress is authorised at the first decision (whatever it says: it goes on, 29(d)); a proceed or a release
    # authorises the seed in progress. A hold on a LATER seed in progress authorises nothing: the seeds above the highest authorised one stay held, whichever seed is in
    # progress, until a release written after that seed's own decision (or a proceed). Only the scheduler writes it, and it only rises.
    why_auth = None
    if latch and stage is not None:
        if auth is None:
            why_auth = "the first decision"
        elif auth < stage and out["state"] == "proceed":
            why_auth = "proceed"
        elif auth < stage and out["released"]:
            why_auth = f"release ({out['released_by']})"
    if why_auth:
        auth = stage
    if latch and auth is not None and (why_auth or stored_auth is None):       # a store from before the record: keep what its decision implied
        now_s = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.put_meta_many({META_AUTH: str(auth)}, now_s, [("model_66_gate_authorised", sm, f"model_66 main runs of seeds up to {auth} are authorised ({why_auth or 'kept from the earlier decision'}); "
                                                                                          f"seed {stage} is in progress, state {out['state']}")] if why_auth else [])
    out["authorised_seed"] = auth
    out["hold_after_seed"] = auth if out["held"] else None        # the seeds above the highest authorised one wait (None: nothing is held)
    return out


def _project(store: Store, cfg: CampaignConfig, keys: set[str] | None, basis: dict[str, list[str]], why: list[str] | None = None,
             out: dict[str, Any] | None = None, attempt_budgets: dict[int, int] | None = None) -> tuple[str, float | None]:
    from .progress import _usd, bounded_for_status, bounded_usage, usage_rows
    why = [] if why is None else why
    coders = float(cfg.gate_coder_allowance_usd)      # a fixed allowance, whatever the coders have done (CampaignConfig.gate_coder_allowance_usd)
    if out is not None:
        out["coder_allowance_usd"] = coders
    total = float(cfg.past_spend_usd) + coders  # test spend before the campaign (a stated figure, see CampaignConfig.past_spend_source), and the coders'
    # What each attempt's recorded usage is worth is the spend alarm's rule (progress.usage_rows and bounded_usage, shared): an attempt with incomplete usage is
    # charged what it recorded plus the request that was cut, at an upper bound; a count that cannot be read, the largest complete attempt of the model; one with
    # no usage recorded, the ceiling of one attempt (the run token budget at the dearest rate), never zero and never in a mean. Unknown only where a price
    # cannot be computed.
    rows = usage_rows(store, BILLED)
    bounds = bounded_usage(store, cfg, BILLED, {m: r for m, r in rows.items() if m in cfg.api_models}, attempt_budgets)       # None: the record is read here, now
    if out is not None:
        out["bounded"] = bounded_for_status(bounds)
    if any(b["unknown"] for b in bounds.values()):
        why += [w for b in bounds.values() for w in b["unknown"]]
        return "hold_unknown", None
    for m, r in rows.items():
        if m not in cfg.api_models:
            continue
        u = _usd(cfg, m, r["i"], r["o"], r["cw"], r["cr"])
        if u is None:
            return "hold_unknown", None
        total += u + (float(bounds[m]["usd"]) if m in bounds else 0.0)      # a bound moves the total by exactly its dollars, and not the means below
    # Runs still to come (amendment 29(d)): a model's seed in progress and the next, its runs already started (in flight: pending until they end, their cost not
    # recorded yet), and its barrier-block runs; not the seeds after those.
    pend = _pending_runs(store, cfg, keys)
    window = _windows(cfg, pend)
    pending: dict[str, int] = {}
    for r in pend:
        if r["started"] or r["block"] in cfg.barrier_blocks or r["seed"] in window.get(r["model"], ()):
            pending[r["model"]] = pending.get(r["model"], 0) + 1
    if out is not None:
        out["window_seeds"] = {m: list(v) for m, v in window.items()}
    for m, n in pending.items():
        if m not in basis:            # a Claude model the gate has no priced runs for cannot be projected
            return "hold_unknown", None
        mean = _mean_usd(store, cfg, m, basis[m])
        if mean is None:
            return "hold_unknown", None
        total += mean * n
    return ("proceed" if total <= cfg.gate_max_usd else "hold_over"), total
