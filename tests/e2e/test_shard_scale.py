"""The rehearsal of #154 at a main study's scale: 4 models, 150 pairs of task and seed per model, 2 arms, sharded by
model (`expcore-shard/3`) into N = 27 and N = 21, every shard a driver process of its own with its own study copy,
store and `out/`, as hosts would run them.

**Campaign A, N = 27, clean: the determinism claim.** The same plan, fakes and seeds are run once by one unsharded
driver of the same campaign.toml and once as 27 shards merged. `scores.json` and `analysis.json` are the same bytes;
the export and the report are the same but for the fields documented to belong to a shard or to a driver's own order
(`SHARD_ONLY`, `PER_DRIVER` below, and the report's `## Shards` section and median dispatch position).

**Campaign B, N = 21, with the faults a fleet of hosts meets:** a host killed mid-run and the shard resumed; a host
whose runs cost more, so that its share of the budget runs out; a host whose infrastructure fails attempts; and, at
the merge, a shard's output given twice, a shard missing and a shard of campaign A. The merge accepts what the hosts
did and refuses the planted inconsistencies; check, analyze, report and package work on the merged batch, and every
run it analysed scores as one driver's run of it does.

The host is shared, so at most three processes run at once (`shard_scale_support.PARALLEL`); the rehearsal takes some
minutes, most of it in the start of 50 driver processes.
"""

import json
import shutil
import sqlite3
import sys
import tarfile
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import pytest
from package_support import stand_in_vendor
from shard_rehearsal_support import apart_from_where_and_when, without_dispatch_positions
from shard_scale_support import (
    LAYOUT_21,
    LAYOUT_27,
    MODELS,
    TASKS,
    UNIT_CAP,
    Done,
    Host,
    finish_batch,
    in_pool,
    make_scale_study,
    measured,
    reserved_total_usd,
    run_process,
)
from shard_support import MERGED_ONLY, keys_of_shard, without_shard_section
from study_support import clean_environment

from expcore.campaign.resume import FINGERPRINT_KEY
from expcore.campaign.shard import (
    MERGE_FILE,
    META_ENVIRONMENT,
    META_SHARD,
    META_SHARES,
    META_STARTED,
    RULE_BY_MODEL,
    ModelShards,
    micro_usd,
)
from expcore.campaign.shard_budget import reservation_usd
from expcore.package.build import build_package
from expcore.provenance.hashfile import render_hash_file
from expcore.study.config import load_study

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

A_COUNT, B_COUNT = sum(LAYOUT_27.values()), sum(LAYOUT_21.values())
PLANNED = len(MODELS) * TASKS * 2 * 2  # models x tasks x arms x seeds
KILLED, HEAVY, FAILING = 3, 9, 14  # B's faulty hosts: shard 3 serves alpha-stub, 9 beta-stub, 14 gamma-stub
KILL_AT = 10  # the attempt of the killed host's process that dies, after its 'running' row and before its marker
B_BUDGET = Fraction(1, 5)  # B's max_usd as a part of what its whole plan reserves: ample for a run of normal cost
SHARD_ONLY = ("shard", *MERGED_ONLY, "wall_s", "turn_latency_s")  # what a merged export adds (C29)
PER_DRIVER = ("dispatch_seq", "started_at", "lease_wait_s")  # where and when a driver dispatched an attempt
PINNED = (META_SHARD, META_STARTED, META_ENVIRONMENT, META_SHARES)
DUPLICATED, MISSING = 5, 12


@dataclass
class Rehearsal:
    """Both campaigns, run, merged and finished; what every process did, and the measurements of the merged ones."""

    base: Path
    a_ref: Host
    a_shards: dict[int, Host]
    a_merged: Path
    b_shards: dict[int, Host]
    b_merged: Path
    infra_keys: list[str]
    runs: dict[str, Done] = field(default_factory=dict)
    finished: dict[str, dict[str, Done]] = field(default_factory=dict)
    killed: dict[str, dict[str, str]] = field(default_factory=dict)


def meta(root: Path) -> dict[str, str]:
    connection = sqlite3.connect(root / "out" / "state.sqlite")
    try:
        return dict(connection.execute("SELECT key, value FROM meta").fetchall())
    finally:
        connection.close()


def rows(root: Path, query: str, *args: object) -> list[tuple]:
    connection = sqlite3.connect(root / "out" / "state.sqlite")
    try:
        return connection.execute(query, args).fetchall()
    finally:
        connection.close()


def read(root: Path, name: str) -> dict:
    return json.loads((root / "out" / name).read_text(encoding="utf-8"))


SHARD_TABLE = "| Shard | Started (UTC) | Condition |"  # the header of the report's first table of `## Shards`


def shard_rows(root: Path) -> list[list[str]]:
    """The cells of every row of the report's table of runs per shard and condition."""
    report = (root / "out" / "report.md").read_text(encoding="utf-8")
    section = report.split("\n## Shards\n", 1)[1].split("\n## ", 1)[0]
    lines = section.split(SHARD_TABLE, 1)[1].split("\n\n", 1)[0].splitlines()[2:]  # past the header and its rule
    return [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]


def merge_and_finish(env: dict[str, str], target: Path, hosts: dict[int, Host]) -> dict[str, Done]:
    """Merge the shards' outputs into `target`'s out/, then check, analyze and report it; each step measured."""
    merged = measured(env, "merge", str(target / "out"), *(str(host.out) for host in hosts.values()))
    return {"merge": merged, **(finish_batch(env, target) if merged.code == 0 else {})}


@pytest.fixture(scope="module")
def rehearsal(tmp_path_factory, clean_environment) -> Rehearsal:
    env = clean_environment
    base = tmp_path_factory.mktemp("scale")
    a_ref = Host(make_scale_study(base, "a-unsharded", per_model=LAYOUT_27, workers=4))
    a_shards = {
        i: Host(make_scale_study(base, f"a-host-{i}", per_model=LAYOUT_27, workers=4), (i, A_COUNT))
        for i in range(1, A_COUNT + 1)
    }
    probe = make_scale_study(base, "b-probe", per_model=LAYOUT_21)
    b_max = float(round(reserved_total_usd(probe) * B_BUDGET, 6))
    b_roots = {
        i: make_scale_study(base, f"b-host-{i}", per_model=LAYOUT_21, max_usd=b_max) for i in range(1, B_COUNT + 1)
    }
    infra_keys = keys_of_shard(b_roots[FAILING], FAILING, B_COUNT)[::4]
    faults = {HEAVY: {"SCALE_HEAVY": "1"}, FAILING: {"SCALE_INFRA_RUNS": json.dumps(infra_keys)}}
    b_shards = {i: Host(root, (i, B_COUNT), faults.get(i, {})) for i, root in b_roots.items()}
    found = Rehearsal(
        base,
        a_ref,
        a_shards,
        make_scale_study(base, "a-merged", per_model=LAYOUT_27, workers=4),
        b_shards,
        make_scale_study(base, "b-merged", per_model=LAYOUT_21, max_usd=b_max),
        infra_keys,
    )

    def killed_and_resumed() -> Done:
        host = b_shards[KILLED]
        found.runs["b crash"] = host.crash(env, "before_marker", KILL_AT)
        found.killed["before"] = meta(host.root)
        resumed = host.run(env)
        found.killed["after"] = meta(host.root)
        return resumed

    jobs = {"a unsharded": lambda: a_ref.run(env), f"b {KILLED}": killed_and_resumed}  # the longest go first
    jobs |= {f"a {i}": (lambda host=host: host.run(env)) for i, host in a_shards.items()}
    jobs |= {f"b {i}": (lambda host=host: host.run(env)) for i, host in b_shards.items() if i != KILLED}
    began = time.monotonic()
    found.runs |= in_pool(jobs)
    ran = time.monotonic()
    found.finished = in_pool(
        {
            "a unsharded": lambda: finish_batch(env, a_ref.root),
            "a merged": lambda: merge_and_finish(env, found.a_merged, a_shards),
            "b merged": lambda: merge_and_finish(env, found.b_merged, b_shards),
        }
    )
    steps = ", ".join(f"{name} {step} {done.measured}" for (name, step), done in _steps(found) if done.code == 0)
    print(f"drivers {ran - began:.0f} s, merged and finished {time.monotonic() - ran:.0f} s; KiB, s: {steps}")
    return found


# --- every process ended as planned ---------------------------------------------------------------------------------


def test_every_driver_ends_as_its_host_planned_and_every_merged_step_succeeds(rehearsal):
    """Every driver of A ends with all its runs done (0); in B the killed host dies (9) and its resume ends (0), the
    host whose runs cost more ends with runs held by its budget (3), and every other ends (0). Both merges, and check,
    analyze and report of both merged batches and of the unsharded one, succeed."""
    codes = {name: done.code for name, done in rehearsal.runs.items()}
    expected = {"a unsharded": 0, "b crash": 9, **{f"a {i}": 0 for i in rehearsal.a_shards}}
    expected |= {f"b {i}": 3 if i == HEAVY else 0 for i in rehearsal.b_shards}
    failing = {name: rehearsal.runs[name].errors[-800:] for name in codes if codes[name] != expected.get(name)}
    assert codes == expected, failing
    finished = {(name, step): done.code for name, steps in rehearsal.finished.items() for step, done in steps.items()}
    every = {"check": 0, "analyze": 0, "report": 0}
    assert finished == {
        **{("a unsharded", step): 0 for step in every},
        **{(name, step): 0 for name in ("a merged", "b merged") for step in ("merge", *every)},
    }, {key: done.errors[-800:] for key, done in _steps(rehearsal) if done.code}


def _steps(rehearsal: Rehearsal):
    for name, steps in rehearsal.finished.items():
        for step, done in steps.items():
            yield (name, step), done


# --- A: the partition, the shares and the fingerprint at N = 27 -----------------------------------------------------


def test_c32_each_of_27_shards_ran_its_models_pairs_only_and_together_the_plan(rehearsal):
    """C32: every shard's store holds exactly the runs the deal by model gives it, all done, of its model only; the
    stores are disjoint and their union is the plan of the unsharded driver."""
    layout = ModelShards(tuple(LAYOUT_27.items()))
    held = {}
    for index, host in rehearsal.a_shards.items():
        found = dict(rows(host.root, "SELECT run_key, status FROM runs"))
        assert set(found) == set(keys_of_shard(host.root, index, A_COUNT)), index
        assert set(found.values()) == {"done"}, index
        assert {json.loads(f)["model"] for (f,) in rows(host.root, "SELECT factors FROM runs")} == {layout.at(index)[0]}
        held[index] = set(found)
    whole = {key for (key,) in rows(rehearsal.a_ref.root, "SELECT run_key FROM runs")}
    assert len(whole) == PLANNED
    assert sum(len(keys) for keys in held.values()) == PLANNED
    assert set().union(*held.values()) == whole


def test_c32_a_models_20_sessions_are_its_shards_one_each_and_never_more(rehearsal):
    """C32: each shard runs one run at a time on its model's unit and nothing of another, so a model's concurrency is
    its shard count, within its cap of 20; its shards differ by at most one pair."""
    layout = ModelShards(tuple(LAYOUT_27.items()))
    sessions = dict.fromkeys(MODELS, 0)
    pairs: dict[str, list[int]] = {model: [] for model in MODELS}
    for index, host in rehearsal.a_shards.items():
        model = layout.at(index)[0]
        assert json.loads(meta(host.root)[META_SHARES])["unit_caps"] == {model: 1}
        sessions[model] += 1
        pairs[model].append(len(rows(host.root, "SELECT run_key FROM runs")) // 2)  # a pair is one run per arm
    assert sessions == LAYOUT_27
    assert all(count <= UNIT_CAP for count in sessions.values())
    assert all(max(sizes) - min(sizes) <= 1 for sizes in pairs.values()), pairs


def test_c32_the_27_budget_shares_sum_within_max_usd_and_none_is_below_one_run(rehearsal):
    """C32: the shares of `max_usd` the shards pinned sum to at most it, in micro-dollars, and each pays at least one
    run's reservation of its model; each shard's split follows its reserved bound, so no shard of A was held."""
    study = load_study(rehearsal.a_ref.root)
    layout, limits = study.campaign.shards, study.campaign.agent.limits
    shares = {
        i: json.loads(meta(host.root)[META_SHARES])["budget"]["max_usd"] for i, host in rehearsal.a_shards.items()
    }
    assert sum(micro_usd(share) for share in shares.values()) <= micro_usd(study.campaign.scheduler.budget.max_usd)
    for index, share in shares.items():
        assert share >= reservation_usd(study.models.resolve(layout.at(index)[0]), limits), index
    assert all(entry["attempts"] == 0 for entry in read(rehearsal.a_merged, "export.json")["budget_holds"])


def test_c28_every_shard_of_27_and_the_unsharded_driver_have_one_fingerprint_and_one_rule(rehearsal):
    """C28: the fingerprint is the whole campaign's on every host, the unsharded driver's included; every shard pins
    the rule by model and its own index of 27 under its default label."""
    whole = meta(rehearsal.a_ref.root)[FINGERPRINT_KEY]
    assert {i: meta(host.root)[FINGERPRINT_KEY] for i, host in rehearsal.a_shards.items()} == dict.fromkeys(
        rehearsal.a_shards, whole
    )
    specs = {i: json.loads(meta(host.root)[META_SHARD]) for i, host in rehearsal.a_shards.items()}
    assert specs == {i: {"rule": RULE_BY_MODEL, "index": i, "count": A_COUNT, "label": f"shard-{i}"} for i in specs}


# --- A: the merged batch is one driver's ----------------------------------------------------------------------------


def test_c29_the_merged_scores_and_analysis_of_27_shards_are_one_drivers_byte_for_byte(rehearsal):
    """C29: scores.json and analysis.json of the 27 shards merged are the bytes the unsharded driver wrote: every
    contrast, its Holm decision, each condition's counts and the pairs used and missing."""
    one, many = rehearsal.a_ref.root / "out", rehearsal.a_merged / "out"
    assert len(json.loads((one / "scores.json").read_text(encoding="utf-8"))) == PLANNED
    assert (many / "scores.json").read_bytes() == (one / "scores.json").read_bytes()
    assert (many / "analysis.json").read_bytes() == (one / "analysis.json").read_bytes()


def test_c29_the_merged_export_differs_from_one_drivers_only_in_the_documented_shard_fields(rehearsal):
    """C29: the merged export is the unsharded one but for what a merge adds (`SHARD_ONLY`: each run's shard tag, the
    shards' own retries, merge.json's list, each analysed attempt's wall time and turn latencies) and what is a
    driver's own order (`PER_DRIVER`); its run set is the same runs, at the same attempts, ending the same way."""
    one, many = read(rehearsal.a_ref.root, "export.json"), read(rehearsal.a_merged, "export.json")
    assert list(many) == [*one, *MERGED_ONLY]
    assert apart_from_where_and_when(many) == apart_from_where_and_when(one)
    runs = lambda export: [(e["run_key"], e["attempt"], e["condition"]) for e in export["analysed_attempts"]]  # noqa: E731
    assert runs(many) == runs(one)
    assert len(runs(one)) == PLANNED


def test_c29_the_merged_report_differs_from_one_drivers_only_in_its_shards_and_its_dispatch_positions(rehearsal):
    """C29: every number of the report is one driver's, but the `## Shards` section only a merge has and each
    condition's median dispatch position, which pools every shard's own positions."""
    one = (rehearsal.a_ref.root / "out" / "report.md").read_text(encoding="utf-8")
    many = (rehearsal.a_merged / "out" / "report.md").read_text(encoding="utf-8")
    assert without_dispatch_positions(without_shard_section(many)) == without_dispatch_positions(one)


def test_c29_the_merged_batch_names_27_shards_by_label_with_every_run_once(rehearsal):
    """C29: merge.json and the export list the 27 shards in order under their labels, each with the runs it planned,
    which sum to the plan; every run entry names the shard the deal gave it."""
    document = read(rehearsal.a_merged, MERGE_FILE)
    assert [(s["index"], s["label"]) for s in document["shards"]] == [(i, f"shard-{i}") for i in rehearsal.a_shards]
    assert sum(s["runs"] for s in document["shards"]) == PLANNED
    export = read(rehearsal.a_merged, "export.json")
    assert export["shards"] == document["shards"]
    owner = {key: i for i, host in rehearsal.a_shards.items() for key in keys_of_shard(host.root, i, A_COUNT)}
    assert {e["run_key"]: e["shard"] for e in export["analysed_attempts"]} == owner


def test_the_merge_of_27_batches_holds_no_more_memory_than_one_drivers_check_and_analysis(rehearsal):
    """The merge reads one shard's store at a time and replays its records as `check` does, so its peak memory is the
    check's, not 27 stores at once; the merged analysis holds every snapshot, as one driver's holds its one store, and
    peaks within a quarter more than one driver's."""
    merged, alone = rehearsal.finished["a merged"], rehearsal.finished["a unsharded"]
    peak = {name: done.measured[0] for name, done in merged.items()}
    assert peak["merge"] <= 1.25 * alone["check"].measured[0], peak
    assert peak["analyze"] <= 1.25 * alone["analyze"].measured[0], peak


# --- B: the faults of a fleet ----------------------------------------------------------------------------------------


def test_c28_a_shard_killed_mid_run_resumes_as_itself_and_keeps_every_pinned_fact(rehearsal):
    """C28: the host killed between an attempt's 'running' row and its marker comes back with the same command; its
    spec, first start, environment identity and shares are what they were before the kill."""
    before, after = rehearsal.killed["before"], rehearsal.killed["after"]
    assert {key: after[key] for key in PINNED} == {key: before[key] for key in PINNED}
    assert json.loads(after[META_SHARD])["index"] == KILLED


def test_the_killed_attempt_is_interrupted_and_its_run_ends_in_a_later_attempt_of_the_same_shard(rehearsal):
    """The attempt the kill caught is interrupted, its run is run again to the end on the same host, and the merged
    export keeps the interrupted attempt in `discarded_usage` and the run under shard 3."""
    root = rehearsal.b_shards[KILLED].root
    interrupted = rows(root, "SELECT run_key, attempt FROM attempts WHERE state = 'interrupted'")
    assert len(interrupted) == 1
    key = interrupted[0][0]
    assert rows(root, "SELECT status FROM runs WHERE run_key = ?", key) == [("done",)]
    assert {status for (status,) in rows(root, "SELECT status FROM runs")} == {"done"}
    export = read(rehearsal.b_merged, "export.json")
    entry = next(e for e in export["analysed_attempts"] if e["run_key"] == key)
    assert entry["shard"] == KILLED
    assert entry["attempt"] > interrupted[0][1]
    lost = [row for row in export["discarded_usage"] if row["state"] == "interrupted"]
    assert [(row["model"], row["attempts"]) for row in lost] == [("alpha-stub", 1)]


def test_c32_a_shard_whose_budget_share_runs_out_holds_its_remaining_runs_and_no_sibling_does(rehearsal):
    """C32: the host whose runs cost more meets its own share of `max_usd` and ends with runs held, as the gate
    reserves a run's bound before it starts; every other shard, on the same split, runs all its runs."""
    statuses = {
        i: dict(rows(host.root, "SELECT status, COUNT(*) FROM runs GROUP BY status"))
        for i, host in rehearsal.b_shards.items()
    }
    heavy = statuses.pop(HEAVY)
    assert heavy.get("done", 0) > 0
    assert heavy.get("pending", 0) > 0
    assert all(set(found) == {"done"} for found in statuses.values()), statuses
    assert "hold on beta-stub (budget)" in rehearsal.runs[f"b {HEAVY}"].errors


def test_c29_the_merged_batch_counts_the_held_runs_as_unfinished_under_the_shard_that_holds_them(rehearsal):
    """C29: the merge takes a shard with runs that never started; the merged export lists exactly them as unfinished,
    tagged with their shard, the analysis counts them so, and the report's shard table shows them for that shard only,
    as an unsharded campaign whose budget held them would."""
    held = {key for (key,) in rows(rehearsal.b_shards[HEAVY].root, "SELECT run_key FROM runs WHERE status='pending'")}
    export = read(rehearsal.b_merged, "export.json")
    assert {(e["run_key"], e["shard"]) for e in export["unfinished_runs"]} == {(key, HEAVY) for key in held}
    conditions = read(rehearsal.b_merged, "analysis.json")["conditions"]
    assert sum(c["unfinished"] for c in conditions) == len(held)
    assert sum(c["planned"] for c in conditions) == PLANNED
    unfinished = {}
    for cells in shard_rows(rehearsal.b_merged):  # Shard, Started, Condition, Planned, Analysed, Exhausted, Unfinished
        unfinished[cells[0]] = unfinished.get(cells[0], 0) + int(cells[6])
    assert len(unfinished) == B_COUNT
    assert {label for label, n in unfinished.items() if n} == {f"shard-{HEAVY}"}
    assert unfinished[f"shard-{HEAVY}"] == len(held)


def test_a_shard_whose_infrastructure_fails_attempts_sets_them_aside_and_runs_them_again(rehearsal):
    """The host whose tool server fails the first attempt of a quarter of its runs sets each aside under its kind and
    runs it again to the end; the merged export and analysis count exactly those set-asides, under that shard."""
    root = rehearsal.b_shards[FAILING].root
    failed = rows(root, "SELECT run_key, infra_kind FROM attempts WHERE state = 'infra_fail'")
    assert sorted(failed) == sorted((key, "knowledge") for key in rehearsal.infra_keys)
    assert {status for (status,) in rows(root, "SELECT status FROM runs")} == {"done"}
    export = read(rehearsal.b_merged, "export.json")
    own = export["shard_retries_by_kind"]
    assert {row["shard"] for row in own} == {FAILING}
    assert sum(row["attempts"] for row in own) == len(rehearsal.infra_keys)
    assert sum(row["set_aside"] for row in export["infrastructure_by_condition"]) == len(rehearsal.infra_keys)
    conditions = read(rehearsal.b_merged, "analysis.json")["conditions"]
    assert sum(c["set_aside"] for c in conditions) == len(rehearsal.infra_keys)


def test_every_run_the_faulted_campaign_analysed_scores_as_one_drivers_run_of_it(rehearsal):
    """The kill, the costlier runs and the failed attempts change how the runs got done, never what they score: each
    run B's merged batch scored has the score the unsharded driver of A gave it, and only the held runs are missing."""
    one, many = read(rehearsal.a_ref.root, "scores.json"), read(rehearsal.b_merged, "scores.json")
    held = {key for (key,) in rows(rehearsal.b_shards[HEAVY].root, "SELECT run_key FROM runs WHERE status='pending'")}
    assert set(many) == set(one) - held
    assert {key: many[key] for key in many} == {key: one[key] for key in many}


# --- B: the merge refuses what is not one campaign ------------------------------------------------------------------


def refused(rehearsal: Rehearsal, env: dict[str, str], name: str, outs: list[Path], fragments: tuple[str, ...]) -> None:
    """Merge `outs` into a new study copy of B and expect a refusal holding every fragment, with nothing written."""
    target = make_scale_study(rehearsal.base / "refused", name, per_model=LAYOUT_21)
    command = [sys.executable, "-m", "expcore", "merge", str(target / "out"), *(str(out) for out in outs)]
    done = run_process(command, env, timeout=600)
    assert done.code == 1, done.errors[-800:]
    for fragment in fragments:
        assert fragment in done.errors, f"{fragment!r} is not in: {done.errors[-800:]}"
    assert not (target / "out").exists()


def test_c29_a_shards_output_given_twice_is_refused_naming_the_shard(rehearsal, clean_environment):
    """C29: the out/ of shard 5, copied as a second host's, is the same shard twice."""
    copy = rehearsal.base / "refused" / "copy-of-5"
    shutil.copytree(rehearsal.b_shards[DUPLICATED].out, copy / "out")
    outs = [host.out for host in rehearsal.b_shards.values()] + [copy / "out"]
    fragment = f"shard {DUPLICATED} (shard-{DUPLICATED}) and shard {DUPLICATED} (shard-{DUPLICATED}) are both shard"
    refused(rehearsal, clean_environment, "twice", outs, (fragment,))


def test_c29_a_missing_shard_of_21_is_refused_naming_it(rehearsal, clean_environment):
    outs = [host.out for i, host in rehearsal.b_shards.items() if i != MISSING]
    refused(rehearsal, clean_environment, "missing", outs, (f"shard {MISSING} of {B_COUNT} is missing",))


def test_c29_a_shard_of_another_campaign_is_refused_naming_it(rehearsal, clean_environment):
    """C29: shard 1 of A (27 shards) in place of B's shard 1: another split of the plan."""
    outs = [rehearsal.a_shards[1].out] + [host.out for i, host in rehearsal.b_shards.items() if i != 1]
    refused(rehearsal, clean_environment, "foreign", outs, (f"split into {A_COUNT} shards", f"into {B_COUNT}"))


# --- B: the package -------------------------------------------------------------------------------------------------


def test_c29_the_package_of_the_faulted_merged_batch_passes_its_own_check(rehearsal, tmp_path):
    """C29: the replication package of B's merged batch (21 snapshots, an interrupted attempt, held runs, set-aside
    attempts) is built, which it is only when its own check regenerates every output byte for byte."""
    hashes = tmp_path / "private.sha256"
    hashes.write_text(render_hash_file(["zarnothwick"]), encoding="utf-8")  # an invented private name
    archive = build_package(rehearsal.b_merged, tmp_path / "dist", hashes=hashes, vendor=stand_in_vendor)
    with tarfile.open(archive) as members:
        names = set(members.getnames())
    root = "scale-study-package"
    assert {f"{root}/out/{MERGE_FILE}", f"{root}/out/analysis.json"} <= names
    assert {f"{root}/out/shards/{i}/campaign-snapshot.sqlite" for i in rehearsal.b_shards} <= names
