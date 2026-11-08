# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/scripted/mplus.py, blob 2f5fe937abe6f53f8cce023ad17e8c960e7053a0
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""M+: data pre-provisioned at the agent-site provider, measured by script only (#12).

Space: `study_space_prefix_2_<stamp>`, supported by the data provider fr and the agent site de, holding the same bytes as the
main space (uploaded to fr, hashed from fr against #3's manifests). The main space stays exclusive to fr.

Forbidden check (the issue's): M+ measured on data already cached from an earlier M run. So, before every provisioning,
de's replica is evicted and de is confirmed EMPTY through the block map; after it, the required extent is confirmed
PLACED through the block map; right before the mount the sandbox's `replicate` hook confirms it once more; every run has
a fresh oneclient process and mount. A transfer's reported status never counts as placement (dataset_77 0003, 0006).

Provisioning scopes:
  required   S1: one replication job per required daily file. S2: Onedata's jobs replicate whole files only, so the
             required byte ranges (traced from the pipeline's reader, `s2ranges`) are read through de's REST
             endpoint, which fetches them from fr on the fly.
  layout     one replication job for the s1z directory, or for the S2 file.

M cold has two variants, kept apart in the records: `cold-fr` (mount on the data provider, the campaign's M; de is never
involved) and `cold-de` (mount on de with its replica evicted, so reads replicate on the fly from fr).
"""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

import numpy as np

from ..instances import load as L
from ..records import Layout, Option
from . import federation as F
from . import insandbox as I
from . import provision as V
from .runner import Plan, run_one, split_days

provider_e, provider_f = "provider_e", "provider_f"
state_a, state_b = Layout.state_a, Layout.state_b


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclasses.dataclass(frozen=True)
class MplusSpace:
    name: str
    space_id: str
    root_id: str
    s1_dir_id: str
    s2_dir_id: str
    s2_file_id: str
    s2_size: int

    def target(self, provider: str) -> F.DataTarget:
        od = F.od_module()
        return F.DataTarget(f"mplus-{provider}", self.name, self.space_id, provider, od.PROVIDERS[provider]["host"])

    @classmethod
    def load(cls, path: Path | str) -> "MplusSpace":
        return cls(**json.loads(Path(path).read_text()))


class Campaign:
    """Operations on one M+ space. `o` is an authenticated `od.OD` (host side only, never reaches a sandbox)."""

    def __init__(self, o, sp: MplusSpace, log_path: Path):
        self.o, self.sp, self.log_path = o, sp, Path(log_path)
        od = F.od_module()
        self.fr_id, self.de_id = od.PROVIDERS[provider_e]["id"], od.PROVIDERS[provider_f]["id"]
        V.check_space(o, provider_e, sp.space_id, sp.root_id,
                      {V.S1_DIR: sp.s1_dir_id, V.S2_DIR: sp.s2_dir_id, f"{V.S2_DIR}/{V.S2_NAME}": sp.s2_file_id})
        self.s1_entries = V.children(o, provider_e, sp.s1_dir_id)   # name -> entry (file id, size), the whole directory

    def log(self, event: str, **kw) -> dict:
        rec = {"utc": now(), "event": event, **kw}
        with open(self.log_path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        print(json.dumps({k: v for k, v in rec.items() if k != "short_file_ids"}, default=str)[:600], flush=True)
        return rec

    # ----------------------------------------------------------------- what a scope consists of
    def s1_required(self, rec: dict) -> dict[str, list[tuple[int, int]]]:
        """file id -> whole-file range, for the instance's 1,544 daily files."""
        out = {}
        for d in L.required_day_indices(rec):
            e = self.s1_entries[I.s1_name(int(d))]
            out[e["fileId"]] = [(0, int(e["size"]))]
        return out

    def wanted(self, layout: Layout, rec: dict | None, s2_ranges: list[tuple[int, int]] | None) -> dict[str, list[tuple[int, int]]]:
        """The placement a scope promises. rec None = the whole layout."""
        if layout == state_a:
            if rec is None:
                return {e["fileId"]: [(0, int(e["size"]))] for e in self.s1_entries.values()}
            return self.s1_required(rec)
        if rec is None:
            return {self.sp.s2_file_id: [(0, self.sp.s2_size)]}
        if not s2_ranges:
            raise ValueError("the required extent of S2 needs its traced byte ranges")
        return {self.sp.s2_file_id: list(s2_ranges)}

    # ----------------------------------------------------------------- eviction
    def evict_de(self, layout: Layout, *, timeout_s: float = 3600) -> dict:
        """Evict de's replica of the whole layout, one job; returns once the block map shows de empty."""
        target = self.sp.s1_dir_id if layout == state_a else self.sp.s2_file_id
        files = [e["fileId"] for e in self.s1_entries.values()] if layout == state_a else [self.sp.s2_file_id]
        t0 = time.time()
        tid = V.schedule_eviction(self.o, provider_f, target, self.de_id, holder_id=self.fr_id)
        report = V.wait_transfers(self.o, provider_f, [tid], timeout_s=timeout_s)
        self.require_ended(report, "eviction")
        empty = None
        for _ in range(60):
            empty = V.holds_nothing(self.o, provider_f, self.de_id, files)
            if empty["empty"]:
                break
            time.sleep(5)
        return self.log("evict", layout=layout.value, seconds=round(time.time() - t0, 1), transfer=report, provider_f=empty,
                        confirmed_empty=bool(empty and empty["empty"]))

    def require_ended(self, report: dict, what: str) -> None:
        """A transfer still running can change de's replica after any later check, so nothing may follow it."""
        if report["timed_out"]:
            self.log("transfer_not_ended", what=what, transfers=report["timed_out"])
            raise RuntimeError(f"{what}: transfers {report['timed_out']} had not ended at the timeout; stopping. They may "
                               "still change de's replica: check them before any further step (no cancel without the operator)")

    def require_empty(self, layout: Layout) -> dict:
        ev = self.evict_de(layout)
        if not ev["confirmed_empty"]:
            raise RuntimeError(f"de still holds {ev['provider_f']['held_bytes']} B of {layout.value} after eviction: "
                               "M+ cannot start from a known-empty site (dataset_77 0006); stopping")
        return ev

    # ----------------------------------------------------------------- provisioning
    def provision_jobs(self, file_ids: list[str], *, timeout_s: float) -> dict:
        t0 = time.time()
        tids = [V.schedule_replication(self.o, provider_f, fid, self.de_id) for fid in file_ids]
        t_posted = time.time()
        report = V.wait_transfers(self.o, provider_f, tids, timeout_s=timeout_s)
        self.require_ended(report, "replication")
        return {"jobs": len(tids), "post_seconds": round(t_posted - t0, 3), "transfer": report,
                "reported_seconds": round(time.time() - t0, 3)}

    def provision_ranges(self, file_id: str, ranges: list[tuple[int, int]]) -> dict:
        """Read the ranges through de's REST endpoint: de fetches what it lacks from fr, on the fly."""
        t0 = time.time()
        n = 0
        for off, ln in ranges:
            r = self.o.op(provider_f, "GET", f"/data/{file_id}/content", timeout=900, expect=(200, 206),
                          headers={"Range": f"bytes={off}-{off + ln - 1}"})
            n += len(r.content)
        return {"ranges": len(ranges), "bytes_read": n, "reported_seconds": round(time.time() - t0, 3)}

    def confirm_placed(self, wanted: dict[str, list[tuple[int, int]]], *, settle_s: float = 300) -> dict:
        """Poll the block map until the scope is placed or `settle_s` passes. The time it took is the placement lag."""
        t0 = time.time()
        while True:
            rep = V.placement_report(self.o, provider_f, self.de_id, wanted)
            if rep["fully_placed"] or time.time() - t0 > settle_s:
                rep["lag_seconds"] = round(time.time() - t0, 3)
                return rep
            time.sleep(5)

    def provision(self, layout: Layout, rec: dict | None, s2_ranges) -> dict:
        """From a confirmed-empty de to a confirmed placement. `seconds` includes the block-map wait, not the eviction."""
        wanted = self.wanted(layout, rec, s2_ranges)
        scope = "required" if rec is not None else "layout"
        t0 = time.time()
        if layout == state_a and rec is not None:
            step = self.provision_jobs(list(wanted), timeout_s=4 * 3600)
        elif layout == state_a:
            step = self.provision_jobs([self.sp.s1_dir_id], timeout_s=4 * 3600)
        elif rec is None:
            step = self.provision_jobs([self.sp.s2_file_id], timeout_s=4 * 3600)
        else:
            step = self.provision_ranges(self.sp.s2_file_id, list(s2_ranges))
        placed = self.confirm_placed(wanted)
        seconds = round(time.time() - t0, 3)
        out = self.log("provision", scope=scope, layout=layout.value, instance=(rec or {}).get("instance", {}).get("instance_id"),
                       seconds=seconds, step=step, placement=placed, placed_bytes=placed["held_bytes"],
                       confirmed_placed=placed["fully_placed"])
        if not out["confirmed_placed"]:
            raise RuntimeError(f"{scope} {layout.value}: {placed['bytes_short']} B not placed on de after the transfer "
                               f"reported {step.get('transfer', {}).get('states')} (dataset_77 0001, 0003); stopping")
        return out

    def placement_hook(self, wanted: dict[str, list[tuple[int, int]]], state: dict):
        """`DataConfig.replicate`: the sandbox calls it right before the mount; no placement, no run. `state` tells the
        caller whether it ran and what it found, since the runner turns the exception into a failed record."""
        def check() -> None:
            rep = V.placement_report(self.o, provider_f, self.de_id, wanted)
            state.update(checked=True, fully_placed=rep["fully_placed"], bytes_short=rep["bytes_short"])
            self.log("placement_before_mount", files=rep["files"], held_bytes=rep["held_bytes"],
                     fully_placed=rep["fully_placed"], bytes_short=rep["bytes_short"])
            if not rep["fully_placed"]:
                raise RuntimeError(f"refusing to run M+: {rep['bytes_short']} B of the scope are not on de")
        return check


def run_mplus(camp: Campaign, scope: str, plan: Plan, rec, truth, cfg, out: Path, expected_sst: str, wanted) -> dict:
    """One M+ run on pre-placed data. What de held before and after is recorded, so that on-the-fly fetches during the
    run (bytes the scope did not cover) show as growth instead of passing as pre-placed."""
    target = camp.sp.target(provider_f)
    file_ids = [camp.sp.s2_file_id] if plan.layout == state_b else list(wanted)
    before = V.holds_nothing(camp.o, provider_f, camp.de_id, file_ids)["held_bytes"]
    state: dict = {"checked": False}
    r = run_one(plan, rec, truth, lambda: F.data_config(target, Option.condition_d, replicate=camp.placement_hook(wanted, state)),
                cfg, out, expected_sst=expected_sst, extra={"variant": "mplus", "scope": scope, "de_held_before": before})
    if not (state["checked"] and state.get("fully_placed")):
        camp.log("mplus_stopped", run_key=r["run_key"], placement_check=state, errors=r["errors"][:2])
        raise RuntimeError(f"{r['run_key']}: the placement check before the mount did not pass ({state}); stopping")
    after = V.holds_nothing(camp.o, provider_f, camp.de_id, file_ids)["held_bytes"]
    camp.log("mplus_run", scope=scope, run_key=r["run_key"], ok=r["ok"], wall_s=r.get("wall_s"),
             de_held_before=before, de_held_after=after, on_the_fly_growth=after - before)
    return r


def run_cold(camp: Campaign, variant: str, plan: Plan, rec, truth, cfg, out: Path, expected_sst: str) -> dict:
    """One M cold run. `cold-de`: mount on de from a confirmed-empty replica. `cold-fr`: mount on fr, the data provider."""
    if variant == "cold-de":
        camp.require_empty(plan.layout)
        target = camp.sp.target(provider_f)
    elif variant == "cold-fr":
        target = camp.sp.target(provider_e)
    else:
        raise ValueError(f"unknown variant {variant!r}")
    return run_one(plan, rec, truth, lambda: F.data_config(target, Option.M), cfg, out, expected_sst=expected_sst,
                   extra={"variant": variant})


def whole_cells(runs: list[dict]) -> list[dict]:
    """The records of whole cells: one carrying `extra.probe_files` is the live probe's prefix of a cell (`baseline --probe-files`), no run of the study, and is never a sample here."""
    return [r for r in runs if (r.get("extra") or {}).get("probe_files") is None]


def break_even_inputs(prov_log: list[dict], runs: list[dict], *, wan_runs: list[dict] | None = None,
                      fr_runs: list[dict] | None = None) -> dict:
    """Samples per (scope, layout) for the bootstrap: P from the provisioning log, W and C from the run records.

    `wan_runs`: the WAN anchor's records; its M runs give C of the scripted WAN bound. `fr_runs`: M runs on the data
    provider from another batch (the pilot pass), counted as cold-fr. Only clean runs count, so a failed run can never
    shorten a measured time; the excluded counts are returned."""
    out: dict[str, dict] = {}
    for layout in ("state_a", "state_b"):
        wall: dict[str, list[float]] = {"mplus-required": [], "mplus-layout": [], "cold-fr": [], "cold-de": [],
                                        "cold-wan": []}
        first: dict[str, list[float]] = {k: [] for k in wall}
        excluded = 0
        tagged = [(r, r["extra"].get("variant")) for r in whole_cells(runs)]
        tagged += [(r, "cold-wan") for r in whole_cells(wan_runs or []) if r["option"] == "M"]
        tagged += [(r, "cold-fr") for r in whole_cells(fr_runs or []) if r["option"] == "M"]
        for r, key in tagged:
            if r["layout"] != layout:
                continue
            if key == "mplus":
                key = f"mplus-{r['extra'].get('scope')}"
            if key not in wall:
                continue
            if not r["ok"]:
                excluded += 1
                continue
            wall[key].append(r["wall_s"])
            first[key].append(r["script"]["first_compute_s"])
        events = [e for e in prov_log if e.get("event") == "provision" and e["layout"] == layout]
        good = [e for e in events if e.get("confirmed_placed") is True]
        prov = {s: [e["seconds"] for e in good if e["scope"] == s] for s in ("required", "layout")}
        pbytes = {s: [e["placed_bytes"] for e in good if e["scope"] == s] for s in ("required", "layout")}
        out[layout] = {"provision_s": prov, "placed_bytes": pbytes, "wall_s": wall, "first_compute_s": first,
                       "failed_runs_excluded": excluded, "unconfirmed_provisioning_excluded": len(events) - len(good)}
    return out


def s2_required_ranges(s2_path: Path, rec: dict) -> list[tuple[int, int]]:
    from .s2ranges import required_ranges
    base, tgt = split_days(rec)
    return required_ranges(s2_path, rec, np.concatenate([base, tgt]))


def setup_space(o, od_safe, out: Path, manifests: Path, s1_dir: Path, s2_file: Path, *, size_bytes: int = 20 * 10 ** 9) -> MplusSpace:
    """Create the M+ space on fr + de, upload to fr, hash everything from fr against #3's manifests. The time this takes and the blocks that stalled are in the
    record as the setup's (science, #34, 2 Nov 11:37Z). They are not the break-even's: that reads only the `provision` events, the replication to de."""
    t_setup = time.time()
    name = "study_space_prefix_2_" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    sid = V.create_space(o, od_safe, name, [provider_e, provider_f], size_bytes)
    (out / "space_created.json").write_text(json.dumps({"name": name, "space_id": sid, "providers": [provider_e, provider_f], "utc": now()}))
    root = V.space_root(o, provider_e, sid)
    up = V.upload_dataset(o, provider_e, root, s1_dir, s2_file, stall_retry=True)
    state_a = V.children(o, provider_e, up["s1_dir_id"])
    state_b = V.children(o, provider_e, up["s2_dir_id"])
    m1, m2 = V.read_manifest(manifests / "s1_sha256.txt"), V.read_manifest(manifests / "s2_sha256.txt")
    if sorted(state_a) != sorted(m1) or sorted(state_b) != sorted(m2):
        raise RuntimeError("the uploaded names differ from the manifests")
    check1 = V.verify_files(o, provider_e, state_a, m1)
    check2 = V.verify_files(o, provider_e, state_b, m2, workers=1)
    rec = {"space": name, "space_id": sid, "upload": up, "verify_s1": check1, "verify_s2": check2, "utc": now(),
           "setup_seconds": round(time.time() - t_setup, 1), "stalled_blocks": up["stalled_blocks"], "stall_seconds": up["stall_seconds"]}
    (out / "space_verify.json").write_text(json.dumps(rec, indent=1))
    with open(out / "mplus_log.jsonl", "a") as f:
        f.write(json.dumps({"utc": rec["utc"], "event": "setup", "space": name, "setup_seconds": rec["setup_seconds"], "stalled_blocks": rec["stalled_blocks"],
                            "stall_seconds": rec["stall_seconds"], "verified": bool(check1["ok"] and check2["ok"])}) + "\n")
    if not (check1["ok"] and check2["ok"]):
        raise RuntimeError(f"verification failed: {check1['mismatches'][:5]} {check2['mismatches'][:5]}")
    sp = MplusSpace(name, sid, root, up["s1_dir_id"], up["s2_dir_id"], state_b["dataset-2.nc"]["fileId"], int(state_b["dataset-2.nc"]["size"]))
    (out / "space.json").write_text(json.dumps(dataclasses.asdict(sp), indent=1))
    return sp


def transfer_health(camp: Campaign) -> dict:
    """fr -> de -> evicted, on one small file, confirmed by the block map: the path works before anything is measured."""
    name = sorted(camp.s1_entries)[0]
    fid, size = camp.s1_entries[name]["fileId"], int(camp.s1_entries[name]["size"])
    t0 = time.time()
    tid = V.schedule_replication(camp.o, provider_f, fid, camp.de_id)
    rep = V.wait_transfers(camp.o, provider_f, [tid], timeout_s=600)
    camp.require_ended(rep, "health replication")
    placed = camp.confirm_placed({fid: [(0, size)]}, settle_s=120)
    tid2 = V.schedule_eviction(camp.o, provider_f, fid, camp.de_id, holder_id=camp.fr_id)
    ev = V.wait_transfers(camp.o, provider_f, [tid2], timeout_s=600)
    camp.require_ended(ev, "health eviction")
    empty = None
    for _ in range(24):
        empty = V.holds_nothing(camp.o, provider_f, camp.de_id, [fid])
        if empty["empty"]:
            break
        time.sleep(5)
    rec = camp.log("transfer_health", file=name, size=size, replication=rep, placed=placed["fully_placed"], eviction=ev,
                   evicted=bool(empty and empty["empty"]), seconds=round(time.time() - t0, 1))
    if not rec["placed"] or not rec["evicted"]:
        raise RuntimeError(f"fr -> de transfer path is not healthy: {rec}; stopping (no restart without the operator)")
    return rec


def measure(camp: Campaign, recs: dict, truth, refs: dict, cfg, out: Path, s2_path: Path, *, instances,
            layouts=(state_a, state_b), layout_instances=None, done=frozenset()) -> None:
    """M+ on de and M cold on de interleaved in one window (science's condition on #12: it runs during the campaign, so a
    contended M+ is never compared with a quiet M cold). Per layout and instance: one of each, in an order that alternates,
    each from a confirmed-empty replica; then the whole layout, provisioned once and read by `layout_instances`.

    `done`: (instance, layout, step) of clean runs already made (`done_steps`; step is cold-de, mplus-required or mplus-layout). They are not
    repeated, so a probe's runs count as the campaign's M+ runs (science, #34, 01:03Z). `layout_instances=()` makes no layout scope at all;
    None reads it with `instances`."""
    for layout in layouts:
        for j, iid in enumerate(instances):
            rec = recs[iid]

            def cold() -> None:
                run_cold(camp, "cold-de", Plan(iid, Option.M, layout, tag=f"cold-de-p{j}"), rec, truth, cfg, out, refs[iid])

            def placed() -> None:
                ranges = s2_required_ranges(s2_path, rec) if layout == state_b else None
                camp.require_empty(layout)
                camp.provision(layout, rec, ranges)
                run_mplus(camp, "required", Plan(iid, Option.condition_d, layout, tag=f"mplus-required-p{j}"), rec, truth, cfg, out,
                          refs[iid], camp.wanted(layout, rec, ranges))

            for step, name in (((cold, "cold-de"), (placed, "mplus-required")) if j % 2 == 0 else ((placed, "mplus-required"), (cold, "cold-de"))):
                if (iid, layout.value, name) not in done:
                    step()
        wide = list(instances if layout_instances is None else layout_instances)
        left = [(j, iid) for j, iid in enumerate(wide) if (iid, layout.value, "mplus-layout") not in done]
        if not left:
            continue
        camp.require_empty(layout)
        camp.provision(layout, None, None)
        for j, iid in left:
            run_mplus(camp, "layout", Plan(iid, Option.condition_d, layout, tag=f"mplus-layout-p{j}"), recs[iid], truth, cfg, out, refs[iid],
                      camp.wanted(layout, None, None))


# ---- the quiet window's size (science, #34, 01:03Z) ----------------------------------------------------------------------------------------------
WINDOW_LIMIT_S = 4 * 3600      # all 30 runs if the projected window, the drain excluded, is at most this; else the first REDUCED_N instances per layout
REDUCED_N = 3
LAYOUT_CODE = {state_a: 1, state_b: 2}
ASSUMPTIONS = ("the layout scope's provisioning takes the required scope's measured time scaled by bytes (the layout's bytes over the bytes the probe placed)",
               "S2's required scope is read by ranges and its layout scope is one replication job, so the byte scaling is the roughest figure for S2",
               "a layout-scope M+ run takes as long as a required-scope M+ run",
               "every eviction takes the probe's mean eviction of that layout",
               "the drain (runs in flight finishing before the window opens) is not in it")


def seeded_order(instances, seed: int, layout: Layout) -> list[str]:
    """The instances in an order fixed by the seed and the layout alone (the listed order does not matter: the names are sorted first)."""
    ids = sorted(set(instances))
    rng = np.random.default_rng([int(seed), 0x4D50, LAYOUT_CODE[layout]])
    return [ids[int(k)] for k in rng.permutation(len(ids))]


def select_instances(instances, seed: int, layout: Layout, n: int | None) -> list[str]:
    """The first `n` of the seeded order (all of them for None): a probe is the first, a reduced window the first 3."""
    order = seeded_order(instances, seed, layout)
    return order if n is None else order[:n]


def done_steps(runs: list[dict]) -> set[tuple[str, str, str]]:
    """(instance, layout, step) of every clean M cold on de and M+ run: what a later window must not repeat."""
    out = set()
    for r in runs:
        if not r.get("ok"):
            continue
        variant = (r.get("extra") or {}).get("variant")
        if variant == "cold-de":
            out.add((r["instance_id"], r["layout"], "cold-de"))
        elif variant == "mplus":
            out.add((r["instance_id"], r["layout"], f"mplus-{r['extra'].get('scope')}"))
    return out


def step_times(log: list[dict], runs: list[dict]) -> dict[str, dict]:
    """Per layout, the means the window's projection uses, from the probe: an eviction, the required scope's provisioning (and the bytes it placed),
    M cold on de's wall and M+ required's wall. A step with no clean sample is None, and the projection refuses it."""
    mean = lambda xs: sum(xs) / len(xs) if xs else None      # noqa: E731
    out = {}
    for layout in ("state_a", "state_b"):
        ev = [e for e in log if e.get("event") == "evict" and e.get("layout") == layout and e.get("confirmed_empty") is True]
        pr = [e for e in log if e.get("event") == "provision" and e.get("layout") == layout and e.get("scope") == "required"
              and e.get("confirmed_placed") is True]

        def walls(variant, scope=None):
            return [r["wall_s"] for r in runs if r.get("ok") and r.get("layout") == layout and (r.get("extra") or {}).get("variant") == variant
                    and (scope is None or r["extra"].get("scope") == scope)]
        out[layout] = {"evict_s": mean([e["seconds"] for e in ev]), "provision_required_s": mean([e["seconds"] for e in pr]),
                       "placed_bytes_required": mean([e["placed_bytes"] for e in pr]),
                       "cold_de_wall_s": mean(walls("cold-de")), "mplus_required_wall_s": mean(walls("mplus", "required")),
                       "samples": {"evict": len(ev), "provision": len(pr), "cold_de": len(walls("cold-de")), "mplus_required": len(walls("mplus", "required"))}}
    return out


def project_window(steps: dict, layout_bytes: dict, chosen: dict[str, list[str]], done=frozenset()) -> dict:
    """Seconds the window takes for `chosen` (layout -> instances), the drain excluded, minus the steps in `done`. Per layout, per instance: M cold
    (an eviction and its wall), M+ required (an eviction, the provisioning, its wall); then once the layout scope (an eviction, the provisioning scaled
    by bytes, and one M+ wall per instance left). A step the probe did not measure is refused: no number from nothing."""
    per, total = {}, 0.0
    for layout, ids in chosen.items():
        st = steps[layout]
        missing = [k for k in ("evict_s", "provision_required_s", "placed_bytes_required", "cold_de_wall_s", "mplus_required_wall_s") if not st.get(k)]
        if missing:
            raise ValueError(f"{layout}: the probe measured no {missing}: the window cannot be projected")
        e, p, c, m = st["evict_s"], st["provision_required_s"], st["cold_de_wall_s"], st["mplus_required_wall_s"]
        s = 0.0
        for iid in ids:
            if (iid, layout, "cold-de") not in done:
                s += e + c
            if (iid, layout, "mplus-required") not in done:
                s += e + p + m
        left = [iid for iid in ids if (iid, layout, "mplus-layout") not in done]
        if left:
            s += e + p * layout_bytes[layout] / st["placed_bytes_required"] + len(left) * m
        per[layout] = {"instances": list(ids), "seconds": s}
        total += s
    return {"layouts": per, "seconds": total}


def plan_window(instances, seed: int, steps: dict, layout_bytes: dict, done=frozenset(), *, limit_s: float = WINDOW_LIMIT_S,
                reduced_n: int = REDUCED_N) -> dict:
    """Science's size rule, on duration alone: every instance of both layouts (30 runs) when the projected window is at most `limit_s`, else the first
    `reduced_n` of each layout's seeded order (18 runs for 3 of 5). The probe's runs are in `done` and count; the projection is of what is left."""
    order = {la.value: select_instances(instances, seed, la, None) for la in (state_a, state_b)}
    everything = project_window(steps, layout_bytes, order, done)
    keep = everything["seconds"] <= limit_s
    chosen = order if keep else {la: ids[:reduced_n] for la, ids in order.items()}
    now = everything if keep else project_window(steps, layout_bytes, chosen, done)
    return {"seed": seed, "limit_s": limit_s, "all_instances": keep, "instances": chosen, "seeded_order": order,
            "projected_all_s": everything["seconds"], "projected_chosen_s": now["seconds"], "per_layout": now["layouts"],
            "rule": (f"all {len(order['state_a'])} instances per layout" if keep else f"the first {reduced_n} of {len(order['state_a'])} instances per layout, in the seeded order")
                    + f"; projected {everything['seconds'] / 3600:.2f} h for all against a limit of {limit_s / 3600:g} h, the drain excluded",
            "assumptions": list(ASSUMPTIONS)}


ROWS = {"cold-de": "primary: M+ on de vs M cold on de, interleaved in one window (replica evicted, reads replicate on the fly from fr)",
        "cold-fr": "secondary, different conditions: M+ on de during the campaign vs M cold on fr from the quiet baselines, the path the agent runs use",
        "cold-wan": "scripted bound: the same P and W, C from M's runs in the WAN anchor (cloud), no extra runs"}


WAN_NOTE = ("off-site rates varied more than 20x between measurements (science, #12, 21:02Z): the WAN figures are the link at the UTC "
            "time shown, not a property of it. Day-0 measured the cloud provider at 1.6-12.6 Mbit/s (about 02:07Z); the rate check "
            "and the S2 runs on 1 Nov measured 163-371 Mbit/s (20:00-20:40Z).")


def wan_figures(wan_runs: list[dict]) -> dict:
    """The WAN anchor's M runs behind the scripted bound, per layout, each with its UTC time (S1 and S2 both count)."""
    out: dict[str, list[dict]] = {"state_a": [], "state_b": []}
    for r in whole_cells(wan_runs):
        if r["option"] == "M" and r["layout"] in out:
            out[r["layout"]].append({"run_key": r["run_key"], "utc": r.get("exec_started_utc"), "ok": bool(r["ok"]),
                                     "wall_s": r.get("wall_s")})
    for rows in out.values():
        rows.sort(key=lambda x: x["utc"] or "")
    return out


def report(results_dir: Path, seed: int = 20271201, *, wan_dir: Path | None = None, fr_dir: Path | None = None) -> dict:
    """The break-even per layout, scope and row (science, #12: primary, secondary, scripted WAN bound), in seconds,
    with a bootstrap interval; written next to the runs. A row without samples says what is missing."""
    from .breakeven import bootstrap_interval, json_safe
    from .summarize import load_runs
    log = [json.loads(x) for x in (results_dir / "mplus_log.jsonl").read_text().splitlines()]
    x = break_even_inputs(log, load_runs(results_dir), wan_runs=load_runs(wan_dir) if wan_dir else None,
                          fr_runs=load_runs(fr_dir) if fr_dir else None)
    out: dict = {"inputs": x, "rows": ROWS, "break_even": {}, "seed": seed,
                 "wan_runs": wan_figures(load_runs(wan_dir)) if wan_dir else None, "wan_note": WAN_NOTE,
                 "labels": "client caches fresh per run; provider and storage page caches not controlled"}
    for layout, d in x.items():
        for scope in ("required", "layout"):
            for variant in ROWS:
                p, c, w = d["provision_s"][scope], d["wall_s"][variant], d["wall_s"][f"mplus-{scope}"]
                key = f"{layout}/{scope}/{variant}"
                out["break_even"][key] = (bootstrap_interval(p, c, w, seed=seed) if p and c and w
                                          else {"missing": {"provision": len(p), "cold": len(c), "mplus": len(w)}})
    (results_dir / "breakeven.json").write_text(json.dumps(json_safe(out), indent=1))
    return out
