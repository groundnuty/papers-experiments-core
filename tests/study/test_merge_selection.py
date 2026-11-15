"""A campaign run in batches by factor levels (C38): selections run, refused, merged, and analysed as one driver's.

The selections are the example study run for real with `run --select access=... --shard-label L`
(`selection_support.run_selection`): `access=direct` holds the conditions `baseline` and `extended`, `access=relay`
holds `relay-down`, so the preregistered contrast of `relay-down` with `baseline` pairs runs of two batches. Both
modes are run: concurrent (proportional shares of the caps and the budget) and `--sequential` (the full caps; the
first the whole budget, the second what the first left, `--after`). Each merge refusal plants one fault in a copy of
a finished selection, or runs a selection that cannot join, and expects the merge to name it and write nothing.
"""

import json
import shutil
from pathlib import Path

import pytest
from selection_support import (
    DIRECT,
    RELAY,
    execute,
    make_selection_study,
    run_refused,
    run_selection,
    select_args,
    spent,
)
from shard_support import MERGE_TIMES, MERGED_ONLY, merge_into, meta_of, without_shard_section
from study_support import HEAD, Campaign, clean_environment, cli, copy_campaign, finish

from expcore.campaign.lock import DispatchLock
from expcore.campaign.selection import RULE_SELECTION
from expcore.campaign.shard import MERGE_FILE, META_SHARD, META_SHARES
from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study
from expcore.study.pipeline import regenerate

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


@pytest.fixture(scope="module")
def base(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("selections")


@pytest.fixture(scope="module")
def whole(base, clean_environment) -> Campaign:
    """One driver of the whole campaign: the reference the merged selections must agree with."""
    campaign = Campaign(make_selection_study(base, "whole"))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture(scope="module")
def direct(base, clean_environment) -> Campaign:
    return run_selection(base, "direct", clean_environment, [DIRECT], "direct")


@pytest.fixture(scope="module")
def relay(base, clean_environment) -> Campaign:
    return run_selection(base, "relay", clean_environment, [RELAY], "relay")


@pytest.fixture(scope="module")
def now(base, clean_environment) -> Campaign:
    """The first of two sequential selections."""
    return run_selection(base, "now", clean_environment, [DIRECT], "now", sequential=True)


@pytest.fixture(scope="module")
def later(base, clean_environment, now) -> Campaign:
    """The second sequential selection, run after the first."""
    return run_selection(base, "later", clean_environment, [RELAY], "later", sequential=True, after=[now])


def merged_of(base: Path, env: dict[str, str], name: str, *selections: Campaign) -> Campaign:
    """The selections merged into a new copy of the study, checked, analysed and reported."""
    campaign = Campaign(make_selection_study(base, name))
    campaign.codes["merge"], errors = merge_into(env, campaign, *selections)
    assert campaign.codes["merge"] == 0, errors
    finish(env, campaign)
    assert campaign.codes == {"merge": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture(scope="module")
def merged(base, clean_environment, direct, relay) -> Campaign:
    return merged_of(base, clean_environment, "merged", direct, relay)


@pytest.fixture(scope="module")
def merged_sequential(base, clean_environment, now, later) -> Campaign:
    return merged_of(base, clean_environment, "merged-sequential", now, later)


def read(campaign: Campaign, name: str):
    return json.loads((campaign.root / "out" / name).read_text(encoding="utf-8"))


def shares_of(campaign: Campaign):
    return json.loads(meta_of(campaign)[META_SHARES])


def refused(env, study: Campaign, *outs: Path, fragments: tuple[str, ...]) -> str:
    """Merge `outs` for `study` into a new directory; expect a refusal holding every fragment, and nothing written."""
    out = study.root / "merged-out"
    code, errors = cli(env, "merge", str(out), *map(str, outs), "--study", str(study.root))
    assert code == 1, errors
    for fragment in fragments:
        assert fragment in errors, errors
    assert not out.exists(), "a refused merge wrote the batch"
    return errors


# --- the analysis of merged selections is one driver's ---------------------------------------------------------------

OWN = {"shard", *("selection",), "dispatch_seq", "started_at", "lease_wait_s", *MERGE_TIMES}
"""The fields of an export entry a merge of selections documents as its own (C29, C38), excluded from the comparison."""


def _report_differs_only_in_positions(merged: str, whole: str) -> bool:
    """The reports are the same line by line but for the last cell of the conditions table: the median dispatch
    position, which each selection counts on its own."""
    lines_a, lines_b = merged.splitlines(), whole.splitlines()
    if len(lines_a) != len(lines_b):
        return False
    return all(
        a == b or (a.startswith("| ") and a.rsplit(" | ", 1)[0] == b.rsplit(" | ", 1)[0])
        for a, b in zip(lines_a, lines_b, strict=True)
    )


@pytest.mark.parametrize("which", ["merged", "merged_sequential"])
def test_c38_selections_merged_give_the_analysis_of_one_driver_byte_for_byte(request, whole, which):
    """C38: the direct and relay selections, run separately (concurrently or one after the other) and merged, give
    scores and analysis byte-identical to one driver of the same campaign; the export differs only in the documented
    fields of a merge (shard, selection and each selection's own dispatch figures), and the report in the dispatch
    positions and its `## Shards` section."""
    many = request.getfixturevalue(which)
    assert (many.root / "out" / "scores.json").read_bytes() == (whole.root / "out" / "scores.json").read_bytes()
    assert (many.root / "out" / "analysis.json").read_bytes() == (whole.root / "out" / "analysis.json").read_bytes()
    one, joined = read(whole, "export.json"), read(many, "export.json")
    plain = lambda entries: [{k: v for k, v in e.items() if k not in OWN} for e in entries]  # noqa: E731
    for key in one:
        if key in ("analysed_attempts", "exhausted_runs", "unfinished_runs", "excluded_attempts"):
            assert plain(joined[key]) == plain(one[key]), key
        else:
            assert joined[key] == one[key], key
    assert set(joined) - set(one) == set(MERGED_ONLY)
    assert _report_differs_only_in_positions(without_shard_section(many.report), whole.report)


@pytest.mark.parametrize("which", ["merged", "merged_sequential"])
def test_c38_regenerate_derives_the_outputs_of_merged_selections_anew(request, which):
    """C38: the four outputs of merged selections regenerate from their records and snapshots byte for byte."""
    assert regenerate(request.getfixturevalue(which).root) == []


def test_c38_a_merge_under_other_prices_than_the_campaigns_is_refused(base, clean_environment, direct, relay):
    """C38: the merge counts what the selections spent with the study's prices; a study whose models.toml is not the
    one the campaign ran under is refused, not used to count."""
    root = make_selection_study(base, "repriced")
    models = (root / "models.toml").read_text(encoding="utf-8")
    assert "input_per_mtok = 1.0" in models
    (root / "models.toml").write_text(
        models.replace("input_per_mtok = 1.0", "input_per_mtok = 0.001"), encoding="utf-8"
    )
    write_stamp(root, HEAD, load_study(root).scope)
    refused(clean_environment, Campaign(root), direct.root / "out", relay.root / "out", fragments=("models.toml",))


def test_c38_a_contrast_across_selections_pairs_runs_of_both_batches(merged):
    """C38: the contrast of relay-down (the relay selection) with baseline (the direct selection) is analysed with
    pairs made of one run of each batch."""
    analysis = read(merged, "analysis.json")
    export = read(merged, "export.json")
    labels = {entry["condition"]: entry["selection"] for entry in export["analysed_attempts"]}
    assert labels["baseline"] == "direct"
    assert labels["relay-down"] == "relay"
    crossing = [c for c in analysis["contrasts"] if c["name"] == "relay-vs-baseline"]
    assert len(crossing) == 1, analysis["contrasts"]
    assert set(crossing[0]["arms"]) == {"relay-down", "baseline"}
    assert crossing[0]["n_pairs"] > 0, crossing


def test_c38_merge_json_the_export_and_the_report_name_each_runs_selection(merged, direct, relay):
    """C38: merge.json lists each selection with its levels and mode under rule expcore-shard/4, numbered by its
    levels; every run entry of the export names its selection; the report's table names each with its levels."""
    document = json.loads((merged.root / "out" / MERGE_FILE).read_text(encoding="utf-8"))
    assert (document["rule"], document["count"]) == (RULE_SELECTION, 2)
    entries = {s["label"]: s for s in document["shards"]}
    assert [s["label"] for s in document["shards"]] == ["direct", "relay"]
    assert entries["direct"]["selection"] == {"access": ["direct"]}
    assert entries["relay"]["sequential"] is False
    assert entries["direct"]["shares"] == shares_of(direct)
    assert entries["relay"]["runs"] == 8
    export = read(merged, "export.json")
    for name in ("analysed_attempts", "exhausted_runs", "unfinished_runs"):
        for entry in export[name]:
            want = "relay" if entry["factors"]["access"] == "relay" else "direct"
            assert entry["selection"] == want, entry
    assert "| direct (access=direct) |" in merged.report
    assert "| Selection |" in merged.report
    assert "a pair of runs may cross two" in merged.report


# --- the run side ----------------------------------------------------------------------------------------------------


def test_c38_a_selection_runs_its_runs_only_in_the_whole_plans_places(direct):
    """C38: the direct selection's store holds exactly the plan's direct runs, at their places in the whole plan, and
    pins the selection spec under expcore-shard/4."""
    study = load_study(direct.root)
    expected = {row.key.text: row.seq for row in study.manifest.rows if row.key.factors["access"] == "direct"}
    held = {row["run_key"]: row["seq"] for row in direct.sql("SELECT run_key, seq FROM runs")}
    assert held == expected
    spec = json.loads(meta_of(direct)[META_SHARD])
    assert spec == {"rule": RULE_SELECTION, "label": "direct", "select": {"access": ["direct"]}, "sequential": False}


def test_c38_concurrent_selections_share_the_caps_and_the_budget_within_the_totals(direct, relay):
    """C38: two concurrent selections get parts of each unit's cap (4) and of max_usd (5.0) by their planned runs,
    rounded down, so together they never pass the totals."""
    one, two = shares_of(direct), shares_of(relay)
    assert one["unit_caps"] == {"alpha-stub": 2, "beta-stub": 2}  # 4 x 7 of 11 runs on each unit
    assert two["unit_caps"] == {"alpha-stub": 1, "beta-stub": 1}  # 4 x 4 of 11
    assert one["budget"]["max_usd"] + two["budget"]["max_usd"] <= 5.0
    assert "after" not in one


def test_c38_sequential_selections_have_the_full_caps_the_first_the_whole_budget_the_second_what_it_left(now, later):
    """C38: under --sequential the first selection gets the full caps and the whole $5, and the second the full caps
    and $5 minus what the first actually spent, which it records with the first's start."""
    first, second = shares_of(now), shares_of(later)
    assert first["unit_caps"] == second["unit_caps"] == {"alpha-stub": 4, "beta-stub": 4}
    assert first["budget"]["max_usd"] == 5.0
    assert first["after"] == []
    used = spent(now)
    assert 0 < used < 5.0
    assert second["budget"]["max_usd"] == pytest.approx(5.0 - used, abs=2e-6)
    assert second["budget"]["max_usd"] <= 5.0 - used
    assert [entry["label"] for entry in second["after"]] == ["now"]
    assert second["after"][0]["spent_usd"] == round(used, 6)


@pytest.mark.parametrize(
    ("selects", "fragment"),
    [
        (["region=north"], "factor 'region', which the study does not declare"),
        (["access=sideways"], "level 'sideways'"),
        (["access=relay", "tools=extended"], "selects no run of the plan"),
        (["access=direct,relay"], "selects every run of the plan"),
    ],
)
def test_c38_a_selection_that_cannot_be_run_is_refused_at_start(tmp_path, clean_environment, selects, fragment):
    """C38: an unknown factor or level, a selection that selects nothing, and one that selects everything are
    refused before the store is opened."""
    root = make_selection_study(tmp_path, "study")
    code, errors = run_refused(clean_environment, root, *select_args(selects, "batch"))
    assert code == 1, errors
    assert fragment in errors, errors
    assert not (root / "out" / "state.sqlite").exists()


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (["--select", DIRECT, "--shard-label", "a", "--shard", "1/2"], "cannot be combined with --shard"),
        (["--select", DIRECT], "--select needs --shard-label"),
        (["--select", DIRECT, "--shard-label", "a", "--after", "x"], "--after needs --sequential"),
        (["--sequential"], "need --select"),
    ],
)
def test_c38_options_that_do_not_combine_are_refused_saying_why(tmp_path, clean_environment, args, fragment):
    """C38: --select with --shard (refused in this version), without a label, --after without --sequential and
    --sequential without --select are refused before the study is read, the refusal naming the options."""
    root = make_selection_study(tmp_path, "study")
    code, errors = cli(clean_environment, "run", str(root), *args)
    assert code == 1, errors
    assert fragment in errors, errors
    assert not (root / "out").exists()


def test_c38_a_selection_of_a_campaign_that_shards_by_model_is_refused_in_this_version(tmp_path, clean_environment):
    """C38: [shards] per_model and --select do not combine yet, and the refusal says so."""
    extra = "\n[shards]\nper_model = { alpha-stub = 1, beta-stub = 1 }\n"
    root = make_selection_study(tmp_path, "study", extra=extra)
    code, errors = run_refused(clean_environment, root, *select_args([DIRECT], "batch"))
    assert code == 1, errors
    assert "cannot be combined with [shards] per_model in this version" in errors, errors


def test_c38_a_concurrent_share_below_one_runs_reservation_is_refused_naming_the_model(tmp_path, clean_environment):
    """C38: a budget whose proportional part cannot pay one run of a model the selection runs is refused at start,
    naming the model and pointing to --sequential."""
    root = make_selection_study(tmp_path, "study", budget=0.1)
    code, errors = run_refused(clean_environment, root, *select_args([RELAY], "batch"))
    assert code == 1, errors
    assert "model alpha-stub" in errors, errors
    assert "--sequential" in errors, errors


def test_c38_a_selection_resumes_only_as_itself(base, clean_environment, direct):
    """C38: a selection's store resumes under the same selection (nothing left to run), and is refused under another
    selection, another label, or no selection at all."""
    copy = copy_campaign(direct, base, "direct-resumed")
    code, errors = run_refused(clean_environment, copy.root, *select_args([DIRECT], "direct"))
    assert code == 0, errors
    code, errors = run_refused(clean_environment, copy.root, *select_args([RELAY], "direct"))
    assert code == 1, errors
    assert "a selection resumes only as itself" in errors, errors
    code, errors = run_refused(clean_environment, copy.root, *select_args([DIRECT], "other"))
    assert code == 1, errors
    assert "label" in errors, errors
    code, errors = run_refused(clean_environment, copy.root)
    assert code == 1, errors
    assert "--select access=direct --shard-label direct" in errors, errors


def test_c38_after_must_name_a_stopped_sequential_selection_of_the_campaign_disjoint_from_this_one(
    base, clean_environment, direct, now
):
    """C38: --after refuses a selection that ran concurrently, one that selects runs of this one, and a path with no
    store."""
    root = make_selection_study(base, "after-refused")
    for after, fragment in (
        (direct, "not a selection run with --sequential"),
        (now, "also selects run"),
    ):
        args = select_args([DIRECT] if after is now else [RELAY], "next", sequential=True, after=[after.root / "out"])
        code, errors = run_refused(clean_environment, root, *args)
        assert code == 1, errors
        assert fragment in errors, errors
    code, errors = run_refused(clean_environment, root, *select_args([RELAY], "next", sequential=True, after=[root]))
    assert code == 1, errors
    assert "no campaign store there" in errors, errors
    assert not (root / "out" / "state.sqlite").exists()


def test_c38_after_refuses_an_earlier_selection_still_running(base, clean_environment, now):
    """C38: while a driver holds the earlier selection's store, a later one cannot read what it spent: refused."""
    root = make_selection_study(base, "after-running")
    with DispatchLock(now.root / "out"):
        code, errors = run_refused(
            clean_environment, root, *select_args([RELAY], "next", sequential=True, after=[now.root / "out"])
        )
    assert code == 1, errors
    assert "has stopped" in errors, errors
    assert not (root / "out" / "state.sqlite").exists()


def test_c38_a_later_selection_resumed_after_its_earlier_one_spent_more_is_refused(base, clean_environment, now, later):
    """C38: the second selection's shares hold what the first had spent; if the first's store says otherwise when
    the second resumes, the resume is refused."""
    first = copy_campaign(now, base, "now-spent-more")
    second = copy_campaign(later, base, "later-resumed")
    execute(first.root / "out" / "state.sqlite", "INSERT OR REPLACE INTO meta(key,value,set_utc) VALUES"
            "('probe_spend_usd', '{\"alpha-stub\": 0.5}', '2026-11-01T00:00:00Z')")  # fmt: skip
    args = select_args([RELAY], "later", sequential=True, after=[first.root / "out"])
    code, errors = run_refused(clean_environment, second.root, *args)
    assert code == 1, errors
    assert "changed since its first start" in errors, errors


# --- the merge's refusals --------------------------------------------------------------------------------------------


def test_c38_a_merge_of_one_selection_is_refused(clean_environment, whole, direct):
    """C38: selections merge with selections only, at least two of them."""
    refused(clean_environment, whole, direct.root / "out", fragments=("at least 2",))


def test_c38_selections_that_overlap_are_refused_naming_both(base, clean_environment, whole, direct):
    """C38: two selections that both select a run (access=direct and tools=basic both hold baseline) are refused."""
    basic = run_selection(base, "basic", clean_environment, ["tools=basic"], "basic")
    refused(clean_environment, whole, direct.root / "out", basic.root / "out", fragments=("both select run",))


def test_c38_selections_that_do_not_cover_the_plan_are_refused(base, clean_environment, whole, relay):
    """C38: relay-down and extended leave baseline in no selection: refused, counting the runs left out."""
    extended = run_selection(base, "extended", clean_environment, ["tools=extended"], "extended")
    refused(clean_environment, whole, relay.root / "out", extended.root / "out", fragments=("is in no selection",))


def test_c38_concurrent_and_sequential_selections_are_not_merged_together(clean_environment, whole, direct, later):
    """C38: the caps of a sequential selection are the full caps, so it never merges with one that ran on a part."""
    refused(clean_environment, whole, direct.root / "out", later.root / "out", fragments=("--sequential",))


def test_c38_a_later_selection_started_without_after_is_refused_at_merge(base, clean_environment, whole, now):
    """C38: the core cannot tell a first selection from a later one whose --after was left out; the merge can: two
    selections that each ran after none are not one chain."""
    orphan = run_selection(base, "orphan", clean_environment, [RELAY], "orphan", sequential=True)
    refused(clean_environment, whole, now.root / "out", orphan.root / "out", fragments=("--after",))


def test_c38_sequential_selections_whose_windows_overlap_are_refused(base, clean_environment, whole, now, later):
    """C38: a first selection whose attempts ran on past the second's start is refused at merge, naming both."""
    first = copy_campaign(now, base, "now-overlapping")
    execute(first.root / "out" / "state.sqlite", "UPDATE attempts SET ended_utc='2099-01-01T00:00:00Z'")
    refused(clean_environment, whole, first.root / "out", later.root / "out", fragments=("must not overlap",))


def test_c38_an_earlier_selection_that_spent_more_after_the_later_started_is_refused_at_merge(
    base, clean_environment, whole, now, later
):
    """C38: the second selection recorded what the first had spent; a first whose store now says more is refused."""
    first = copy_campaign(now, base, "now-spent-again")
    execute(first.root / "out" / "state.sqlite", "INSERT OR REPLACE INTO meta(key,value,set_utc) VALUES"
            "('probe_spend_usd', '{\"alpha-stub\": 0.5}', '2026-11-01T00:00:00Z')")  # fmt: skip
    refused(clean_environment, whole, first.root / "out", later.root / "out", fragments=("ran again after",))


def test_c38_selections_whose_spend_passes_the_budget_are_refused_at_merge(
    base, clean_environment, whole, direct, relay
):
    """C38: the merge sums what every selection spent and refuses a total over max_usd."""
    over = copy_campaign(direct, base, "direct-over")
    execute(over.root / "out" / "state.sqlite", "INSERT OR REPLACE INTO meta(key,value,set_utc) VALUES"
            "('probe_spend_usd', '{\"alpha-stub\": 9.0}', '2026-11-01T00:00:00Z')")  # fmt: skip
    refused(clean_environment, whole, over.root / "out", relay.root / "out", fragments=("over [budget] max_usd",))


def test_c38_a_selections_snapshot_under_another_index_is_refused_by_the_check(base, clean_environment, merged):
    """C38: the merge numbers selections by their levels, so swapping the two snapshots of a merged batch (and the
    hashes merge.json names) is refused by the check."""
    copy = copy_campaign(merged, base, "merged-swapped")
    out = copy.root / "out"
    one, two = out / "shards" / "1" / "campaign-snapshot.sqlite", out / "shards" / "2" / "campaign-snapshot.sqlite"
    first, second = one.read_bytes(), two.read_bytes()
    one.write_bytes(second)
    two.write_bytes(first)
    document = json.loads((out / MERGE_FILE).read_text(encoding="utf-8"))
    hashes = [s["snapshot_sha256"] for s in document["shards"]]
    for shard, digest in zip(document["shards"], reversed(hashes), strict=True):
        shard["snapshot_sha256"] = digest
    (out / MERGE_FILE).write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    code, errors = cli(clean_environment, "check", str(copy.runs))
    assert code == 1, errors
    assert "merge numbers" in errors or "differs" in errors, errors
    shutil.rmtree(copy.root)
