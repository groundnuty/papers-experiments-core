"""The export of a merged batch: shard exports joined into the one of the whole plan (#154, C29)."""

import copy
import json

import pytest

from expcore.campaign.deployments import deployment_shares
from expcore.campaign.export import FORMAT, ExportError
from expcore.campaign.export_join import join_exports
from expcore.campaign.merge import MergeRefused, read_merge
from expcore.campaign.shard import EXPORT_KEY, MERGE_SCHEMA, RULE
from expcore.errors import CORE_KINDS


def entry(key: str, condition: str = "base", **more) -> dict:
    return {"run_key": key, "digest": key * 2, "condition": condition, "factors": {"task": key}, **more}


def export(analysed=(), exhausted=(), unfinished=(), excluded=(), **tables) -> dict:
    """A shard's export in the partial-analysis form, with empty accounting tables unless given."""
    planned = len(analysed) + len(exhausted) + len(unfinished) + len(excluded)
    data = {
        "format": FORMAT,
        "factors": ["task", "model", "seed"],
        "planned_runs": planned,
        "exhausted_runs": list(exhausted),
        "unfinished_runs": list(unfinished),
        "analysed_attempts": list(analysed),
        "discarded_usage": [],
        "retries_by_kind": [],
        "infrastructure_by_condition": [],
        "budget_holds": [],
        "kinds": list(CORE_KINDS),
        "partial": bool(excluded),
        "running_attempts": 0,
        "excluded_attempts": list(excluded),
    }
    data.update(tables)
    return data


SHARDS = [
    {
        "index": 1,
        "label": "shard-1",
        "started_utc": "2026-03-01T10:00:00Z",
        "shares": {},
        "runs": 0,
        "snapshot_sha256": "a" * 64,
    },
    {
        "index": 2,
        "label": "shard-2",
        "started_utc": "2026-03-02T10:00:00Z",
        "shares": {},
        "runs": 0,
        "snapshot_sha256": "b" * 64,
    },
]
SEQ = {"a": 0, "b": 1, "c": 2, "d": 3, "e": 4, "f": 5}


def plan(*keys: str) -> dict[str, int]:
    """The place in the whole plan of each of `keys`: a plan of exactly those runs."""
    return {key: SEQ[key] for key in keys}


def test_c29_run_lists_join_in_plan_order_each_entry_tagged_with_its_shard():
    """C29: the run lists are concatenated in the order of the whole plan, whatever the shard, every entry says which
    shard ran it, and the planned runs add up."""
    one = export(analysed=[entry("c"), entry("a")], exhausted=[entry("e")])
    two = export(analysed=[entry("d")], unfinished=[entry("b", status="pending")])
    joined = join_exports({2: two, 1: one}, plan("a", "b", "c", "d", "e"), SHARDS)
    assert [(e["run_key"], e["shard"]) for e in joined["analysed_attempts"]] == [("a", 1), ("c", 1), ("d", 2)]
    assert [(e["run_key"], e["shard"]) for e in joined["exhausted_runs"]] == [("e", 1)]
    assert [(e["run_key"], e["shard"]) for e in joined["unfinished_runs"]] == [("b", 2)]
    assert joined["planned_runs"] == 5
    assert joined["shards"] == SHARDS


def test_c29_left_out_runs_are_tagged_and_ordered_like_the_others():
    """C29: an entry of `excluded_attempts` carries its shard too, and `partial` holds when any shard's does."""
    one = export(analysed=[entry("a")], excluded=[entry("e", attempt=1, reason="uncovered")])
    two = export(excluded=[entry("b", attempt=1, reason="uncovered")])
    joined = join_exports({1: one, 2: two}, plan("a", "b", "e"), SHARDS)
    assert [(e["run_key"], e["shard"]) for e in joined["excluded_attempts"]] == [("b", 2), ("e", 1)]
    assert joined["partial"] is True
    assert joined["running_attempts"] == 0


def test_c29_a_shards_own_shard_key_does_not_reach_the_merged_export():
    """C29: a shard's export may carry its own spec under `shard`; the merged export has `shards` instead."""
    own = {  # the object the run side puts last in a shard's export
        "spec": {"rule": RULE, "index": 1, "count": 2, "label": "shard-1"},
        "started_utc": "2026-03-01T10:00:00Z",
        "environment_identity": {},
        "shares": {"unit_caps": {"alpha": 1}, "budget": {"max_usd": 2.5, "per_model_usd": {}}},
    }
    one = {**export(analysed=[entry("a")]), EXPORT_KEY: own}
    two = export(analysed=[entry("b")])
    joined = join_exports({1: one, 2: two}, plan("a", "b"), SHARDS)
    assert EXPORT_KEY not in joined
    assert "shards" in joined


def usage(condition: str, n: int, wall: float) -> dict:
    return {
        "condition": condition,
        "model": "m",
        "state": "interrupted",
        "attempts": n,
        "usage_incomplete_attempts": 0,
        "input_tokens": 10 * n,
        "output_tokens": n,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "wall_s": wall,
    }


def retry(n: int) -> dict:
    return {"condition": "base", "model": "m", "kind": "sandbox", "state": "infra_fail", "charged": True, "attempts": n}


def infra(kind: str, aside: int, treatment: int, declared: bool = True) -> dict:
    return {
        "condition": "base",
        "kind": kind,
        "declared_treatment": declared,
        "set_aside": aside,
        "treatment": treatment,
        "excluded": 0,
    }


def holds(base: int) -> list[dict]:
    return [{"condition": "base", "model": "m", "attempts": base}, {"condition": "zed", "model": "m", "attempts": 0}]


def test_c29_accounting_figures_that_are_sums_are_summed_row_by_row():
    """C29: rows with one key add up, rows of other keys stay, in the order the unsharded export sorts them."""
    one = export(
        analysed=[entry("a")],
        discarded_usage=[usage("base", 2, 1.5), usage("zed", 1, 0.5)],
        retries_by_kind=[retry(2)],
        infrastructure_by_condition=[infra("endpoint", 1, 2)],
        budget_holds=holds(1),
    )
    two = export(
        analysed=[entry("b")],
        discarded_usage=[usage("base", 3, 2.0), usage("mid", 1, 1.0)],
        retries_by_kind=[retry(5)],
        infrastructure_by_condition=[infra("endpoint", 4, 1)],
        budget_holds=holds(2),
    )
    joined = join_exports({1: one, 2: two}, plan("a", "b"), SHARDS)
    assert [(r["condition"], r["attempts"], r["input_tokens"], r["wall_s"]) for r in joined["discarded_usage"]] == [
        ("base", 5, 50, 3.5),
        ("mid", 1, 10, 1.0),
        ("zed", 1, 10, 0.5),
    ]
    assert [r["attempts"] for r in joined["retries_by_kind"]] == [7]
    (row,) = joined["infrastructure_by_condition"]
    assert (row["set_aside"], row["treatment"], row["declared_treatment"]) == (5, 3, True)
    assert [(r["condition"], r["attempts"]) for r in joined["budget_holds"]] == [("base", 3), ("zed", 0)]


def test_c29_the_kinds_are_the_cores_then_every_other_kind_sorted():
    """C29: the vocabulary of kinds is what the unsharded export gives: the core's, then the others, sorted."""
    one = export(analysed=[entry("a")], kinds=[*CORE_KINDS, "zeta"])
    two = export(analysed=[entry("b")], kinds=[*CORE_KINDS, "alpha"])
    assert join_exports({1: one, 2: two}, plan("a", "b"), SHARDS)["kinds"] == [*CORE_KINDS, "alpha", "zeta"]


def test_c29_exports_that_are_not_of_one_campaign_are_refused():
    """C29: other factors, other keys, a run the plan lacks, or a flag that disagrees, are refused."""
    one, two = export(analysed=[entry("a")]), export(analysed=[entry("b")])
    other = copy.deepcopy(two)
    other["factors"] = ["task"]
    with pytest.raises(ExportError, match="disagree on 'factors'"):
        join_exports({1: one, 2: other}, plan("a", "b"), SHARDS)
    short = copy.deepcopy(two)
    del short["budget_holds"]
    with pytest.raises(ExportError, match="budget_holds"):
        join_exports({1: one, 2: short}, plan("a", "b"), SHARDS)
    with pytest.raises(ExportError, match="run b of the shards' exports is not in the plan"):
        join_exports({1: one, 2: two}, {"a": 0}, SHARDS)
    with pytest.raises(ExportError, match="disagree on 'declared_treatment'"):
        join_exports(
            {
                1: export(infrastructure_by_condition=[infra("endpoint", 0, 0, declared=True)]),
                2: export(infrastructure_by_condition=[infra("endpoint", 0, 0, declared=False)]),
            },
            plan(),
            SHARDS,
        )


def test_c29_a_join_that_does_not_cover_the_plan_is_refused():
    """C29: the joined run lists hold every run of the whole plan exactly once, and the planned runs add up to it, so
    a merged batch with a shard left out is refused here too, whatever its merge.json says."""
    one, two = export(analysed=[entry("a")]), export(analysed=[entry("b")])
    with pytest.raises(ExportError, match=r"run c of the plan is in no shard's export \(1 of the plan's 3 runs"):
        join_exports({1: one, 2: two}, plan("a", "b", "c"), SHARDS)
    with pytest.raises(ExportError, match="run b of the plan is in no shard's export"):
        join_exports({1: one}, plan("a", "b"), SHARDS[:1])
    with pytest.raises(ExportError, match="run a is listed more than once"):
        join_exports({1: one, 2: export(exhausted=[entry("a")])}, plan("a"), SHARDS)
    with pytest.raises(ExportError, match="the shards' exports plan 3 runs, but the plan has 2"):
        join_exports({1: {**one, "planned_runs": 2}, 2: two}, plan("a", "b"), SHARDS)


def test_c29_a_merge_file_that_is_not_the_shards_one_to_n_is_refused():
    """C29: merge.json is read back strictly: its schema, its keys, and shards 1..N of N at least 2, each with a
    snapshot hash; a batch of one shard is no merge."""
    good = {
        "schema": MERGE_SCHEMA,
        "fingerprint": "f" * 64,
        "rule": "r",
        "count": 2,
        "environment_identity": {},
        "shards": [dict(shard) for shard in SHARDS],
    }
    assert read_merge(json.dumps(good).encode())["count"] == 2
    for broken in (
        {**good, "schema": "expcore-merge/0"},
        {**good, "extra": 1},
        {**good, "shards": good["shards"][:1]},
        {**good, "count": 1, "shards": good["shards"][:1]},
        {**good, "count": True, "shards": good["shards"][:1]},
        {**good, "shards": [good["shards"][1], good["shards"][0]]},
        {**good, "shards": [good["shards"][0], {**good["shards"][1], "snapshot_sha256": "short"}]},
        {**good, "shards": [good["shards"][0], {k: v for k, v in good["shards"][1].items() if k != "label"}]},
    ):
        with pytest.raises(MergeRefused):
            read_merge(json.dumps(broken).encode())
    with pytest.raises(MergeRefused, match="not JSON"):
        read_merge(b"{")


# --- the deployment shares of models that declare accepted fingerprints (B18) -----------------------------------------

PROBE = {"by": "admission_probe", "run": None, "at": "2026-03-01T10:00:01Z"}
SEEN_IN_TWO = {"by": "run", "run": "d", "at": "2026-03-02T10:05:00Z"}


def served(*turns) -> dict:
    """An analysed attempt's row as `deployment_shares` reads it: the turns each fingerprint served, or no mix."""
    return {"deployments": json.dumps([list(pair) for pair in turns]) if turns else None}


def declared(confirmed: dict) -> dict:
    """The store's record of the declared sets, with model-a's confirmations as given."""
    return {
        "model-a": {"accepted": ["fp-east", "fp-west"], "confirmed": confirmed},
        "model-b": {"accepted": ["fp-solo"], "confirmed": {}},
    }


ONE = [
    ("model-a", served(("fp-east", 3))),
    ("model-a", served(("fp-east", 2), ("fp-west", 1))),
    ("model-a", served((None, 1), ("fp-east", 1))),
    ("model-b", served()),
]
TWO = [
    ("model-a", served(("fp-west", 4))),
    ("model-a", served(("fp-other", 2))),
    ("model-a", served()),
    ("model-b", served(("fp-solo", 5))),
]


def with_shares(key: str, shares: list) -> dict:
    return export(analysed=[entry(key)], deployment_shares=shares)


def test_b18_the_deployment_shares_of_shards_that_differ_join_into_one_drivers():
    """B18, C29: each model's and fingerprint's runs and turns are summed over the shards, every share is computed
    again from the sums, a fingerprint seen by one shard only is kept, and a fingerprint is confirmed when any shard
    confirmed it, by the witness of the first shard that did: what one driver of every run gives."""
    one = deployment_shares(declared({"fp-east": PROBE}), ONE)
    two = deployment_shares(declared({"fp-east": SEEN_IN_TWO, "fp-west": SEEN_IN_TWO}), TWO)
    assert one != two
    joined = join_exports({1: with_shares("a", one), 2: with_shares("b", two)}, plan("a", "b"), SHARDS)
    whole = deployment_shares(declared({"fp-east": PROBE, "fp-west": SEEN_IN_TWO}), ONE + TWO)
    assert joined["deployment_shares"] == whole
    assert json.dumps(joined["deployment_shares"]) == json.dumps(whole)  # the export's bytes: the keys' order too


def test_b18_shards_whose_deployment_shares_agree_are_summed_not_taken_from_one():
    """B18, C29: two shards that happen to report the same shares still served twice the runs and turns."""
    shares = deployment_shares(declared({"fp-east": PROBE}), ONE)
    joined = join_exports({1: with_shares("a", shares), 2: with_shares("b", shares)}, plan("a", "b"), SHARDS)
    assert joined["deployment_shares"] == deployment_shares(declared({"fp-east": PROBE}), ONE + ONE)


def test_b18_shards_that_declare_other_accepted_fingerprints_are_refused():
    """B18, C29: one campaign declares one set per model, so shards that report another are not joined."""
    one = deployment_shares(declared({}), ONE)
    other = deployment_shares({**declared({}), "model-b": {"accepted": ["fp-else"], "confirmed": {}}}, TWO)
    with pytest.raises(ExportError, match="model-b"):
        join_exports({1: with_shares("a", one), 2: with_shares("b", other)}, plan("a", "b"), SHARDS)
