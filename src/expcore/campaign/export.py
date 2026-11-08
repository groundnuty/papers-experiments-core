# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/export.py, blob da7dd6d933dc5c2fe6aa371a849e20e7b684f791
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Export of the infrastructure-failure account for the analysis (#14, #16).

The store keeps every attempt; the analysis sees only scored runs. A run whose three infra attempts
are used up ('infra_exhausted') has no analysed attempt and no scored row, so without this export it
would vanish. `export_infra` lists them, the discarded attempts' usage, and (so that nothing else
vanishes either) the runs that are neither done nor exhausted. The analysis requires this file.

Rule (parameters.md, "Infrastructure failures"): a run's analysed attempt is the first one that
ended without an infrastructure failure. The scheduler stops at it (done), so for a done run it is
the single 'done' attempt; every earlier 'infra_fail' attempt is discarded, and so are all the
attempts of an exhausted run. 'interrupted' attempts (a killed process; billed, never an observed
infra fault) are reported in their own columns, not folded into the discarded ones. A 'harness_error'
attempt (a bug of ours; the run goes back to pending when the campaign next opens) is never analysed:
a run that has not finished since is listed as unfinished, and one that finished has its 'done'
attempt. Its billed usage is lost work, so it is in `discarded_usage`, in its own `harness_error_*`
columns (the lead, 2 Nov), beside the infra-discarded and the interrupted ones.

JSON format `dataset-153`: see `FORMAT` and the field lists below. Empty lists are valid
and must be written out: the analysis rejects a file with a missing key.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
from pathlib import Path
from typing import Iterable

from dataset_76.records import Instance, Layout, Option, RunId

from .store import Store

FORMAT = "dataset-153"
EXHAUSTED_FIELDS = ("block", "run_key", "key", "option", "layout", "model", "seed", "instance_id",
                    "region_id", "attempts", "reasons", "exclude_from_analysis", "no_outcome_at_snapshot")
UNFINISHED_FIELDS = ("block", "run_key", "key", "status", "option", "layout", "model", "seed",
                     "instance_id", "region_id", "exclude_from_analysis", "in_cohort")
USAGE_FIELDS = ("block", "option", "layout", "model", "attempts", "input_tokens", "output_tokens",
                "wall_s", "interrupted_attempts", "interrupted_input_tokens", "interrupted_output_tokens",
                "interrupted_wall_s", "harness_error_attempts", "harness_error_input_tokens", "harness_error_output_tokens",
                "harness_error_wall_s")
ANALYSED_FIELDS = ("block", "run_key", "key", "option", "layout", "model", "attempt", "run_dir")


def _key(r) -> str:
    return RunId(r["instance_id"], Option(r["option"]), Layout(r["layout"]), r["model"], r["seed"]).key()


def host_pressure_cause(detail: str | None) -> str | None:
    """The host's own kill of a process of the run, from an attempt's detail; a journal that could not be read is not one. Science, #34, 2 Nov: these fall on the largest process, and options
    may differ in size, so a difference by option must show."""
    d = detail or ""
    if "agents' uid" in d and "earlyoom killed" in d:
        return "earlyoom_unattributed"
    if "earlyoom killed" in d or ": earlyoom (" in d:
        return "earlyoom"
    return "host_oom" if "host_oom" in d else None


def host_pressure_kills(store: Store) -> list[dict]:
    """Per block x option x layout x model: the attempts the host's memory pressure ended, by cause, beside all finished attempts of the cell. Retried as infrastructure."""
    cells: dict = {}
    for a in store.q("SELECT r.block,r.option,r.layout,r.model,a.state,a.infra_detail FROM attempts a JOIN runs r USING(run_key) WHERE a.state IN ('done','infra_fail','harness_error')"):
        c = cells.setdefault((a["block"], a["option"], a["layout"], a["model"]), {"attempts": 0, "earlyoom": 0, "earlyoom_unattributed": 0, "host_oom": 0})
        c["attempts"] += 1
        cause = host_pressure_cause(a["infra_detail"]) if a["state"] == "infra_fail" else None
        if cause:
            c[cause] += 1
    return [{"block": k[0], "option": k[1], "layout": k[2], "model": k[3], **v} for k, v in sorted(cells.items())]


def _when(ts: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t


def _in_cohort(r, att, cut: str | None) -> bool | None:
    """A run with no outcome: was it in the cohort? True if its first started attempt, or its deferral by M's or D's cap (amendment 24), came before the cut
    the campaign stamped at open, or the campaign has no cut ("none"); False if neither did; None if no cut was stamped, so it cannot be told."""
    if cut is None:
        return None
    if cut == "none":
        return True
    began = [a["started_utc"] for a in att if a["state"] != "unstarted" and a["started_utc"]]
    if "deferred_utc" in r.keys() and r["deferred_utc"]:
        began.append(r["deferred_utc"])
    return bool(began) and min(_when(t) for t in began) < _when(cut)


def container_deaths(store: Store) -> list[dict]:
    """Per block x option x layout x model: the attempts in which a gate or sidecar died, by cause (amendment 24, science, #2), beside all finished attempts of the cell. `host_pressure`,
    `component_defect` and `undetermined` are infrastructure and were retried; `own_limit_agent` (the sidecar at its own memory limit) is the agent's outcome, counted in its done attempt.
    `undetermined` is listed so that it is looked at: retried because nothing could show it was not the agent's."""
    from dataset_76.sandbox.infradeath import CAUSES
    cells: dict = {}
    for a in store.q("SELECT r.block,r.option,r.layout,r.model,a.state,a.container_death_cause AS cause FROM attempts a JOIN runs r USING(run_key) WHERE a.state IN ('done','infra_fail','harness_error')"):
        c = cells.setdefault((a["block"], a["option"], a["layout"], a["model"]), {"attempts": 0, **dict.fromkeys(CAUSES, 0)})
        c["attempts"] += 1
        if a["cause"] in CAUSES:
            c[a["cause"]] += 1
    return [{"block": k[0], "option": k[1], "layout": k[2], "model": k[3], **v} for k, v in sorted(cells.items())]


def export_infra(store: Store, instances: Iterable[Instance]) -> dict:
    region = {i.instance_id: i.region_id for i in instances}
    stamped = store.q("SELECT value FROM meta WHERE key='cohort_cut_utc'")
    cut = stamped[0]["value"] if stamped else None
    # the runs a paper snapshot's cut turned into exhausted ones, for having no outcome at t (amendment 29(c)): told apart from infrastructure-exhausted ones per cell
    noted = store.q("SELECT value FROM meta WHERE key='snapshot_no_outcome_runs'")
    converted = set(json.loads(noted[0]["value"])) if noted else set()

    def reg(r) -> str:
        if r["instance_id"] not in region:
            raise ValueError(f"run {r['run_key']}: instance {r['instance_id']} is not in the instance set")
        return region[r["instance_id"]]

    runs = store.q("SELECT * FROM runs ORDER BY seq")
    base = lambda r: {"block": r["block"], "run_key": r["run_key"], "key": _key(r), "option": r["option"],
                      "layout": r["layout"], "model": r["model"], "seed": r["seed"],
                      "instance_id": r["instance_id"], "region_id": reg(r),
                      "exclude_from_analysis": bool(r["exclude_from_analysis"])}
    exhausted, unfinished, analysed = [], [], []
    for r in runs:
        att = store.q("SELECT * FROM attempts WHERE run_key=? ORDER BY id", (r["run_key"],))
        infra = [a for a in att if a["state"] == "infra_fail"]
        if r["status"] == "infra_exhausted":
            exhausted.append({**base(r), "attempts": len(infra),
                              "reasons": [f"{a['infra_kind']}: {a['infra_detail']}" for a in infra],
                              "no_outcome_at_snapshot": r["run_key"] in converted})
        elif r["status"] in ("pending", "harness_error"):
            unfinished.append({**{k: v for k, v in base(r).items()}, "status": r["status"], "in_cohort": _in_cohort(r, att, cut)})
        elif r["status"] == "done":
            done = [a for a in att if a["state"] == "done"]
            if len(done) != 1:
                raise ValueError(f"run {r['run_key']} is done but has {len(done)} done attempts")
            analysed.append({"block": r["block"], "run_key": r["run_key"], "key": _key(r),
                             "option": r["option"], "layout": r["layout"], "model": r["model"],
                             "attempt": done[0]["attempt"], "run_dir": done[0]["run_dir"]})
        else:
            raise ValueError(f"run {r['run_key']}: unknown status {r['status']!r}")
    agg: dict = collections.defaultdict(lambda: dict.fromkeys(USAGE_FIELDS[4:], 0))
    for a in store.q("SELECT r.block,r.option,r.layout,r.model,a.state,a.input_tokens,a.output_tokens,a.wall_s"
                     " FROM attempts a JOIN runs r USING(run_key) WHERE a.state IN ('infra_fail','interrupted','harness_error')"):
        c = agg[(a["block"], a["option"], a["layout"], a["model"])]
        pre = {"infra_fail": "", "interrupted": "interrupted_", "harness_error": "harness_error_"}[a["state"]]
        c[pre + "attempts"] += 1
        c[pre + "input_tokens"] += a["input_tokens"] or 0
        c[pre + "output_tokens"] += a["output_tokens"] or 0
        c[pre + "wall_s"] += a["wall_s"] or 0.0
    usage = [dict(zip(USAGE_FIELDS[:4], k), **v) for k, v in sorted(agg.items())]
    # science, 2 Nov: a retry of a kill the harness lost track of can discard an agent's own outcome, and kills may correlate with option through D's double charge
    retries = store.q("SELECT r.block,r.option,r.layout,r.model,a.infra_kind AS kind,COUNT(*) AS attempts FROM attempts a JOIN runs r USING(run_key)"
                      " WHERE a.state='infra_fail' GROUP BY 1,2,3,4,5 ORDER BY 1,2,3,4,5")
    return {"format": FORMAT, "exhausted_runs": exhausted, "unfinished_runs": unfinished,
            "discarded_usage": usage, "analysed_attempts": analysed, "retries_by_kind": [dict(r) for r in retries], "host_pressure_kills": host_pressure_kills(store),
            "container_deaths": container_deaths(store)}


def write_infra_export(store: Store, instances: Iterable[Instance], path: Path) -> dict:
    d = export_infra(store, instances)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    os.replace(tmp, path)
    return d
