"""The rehearsal of #154: a campaign run as two shards by two concurrent drivers merges into the result of one driver,
and the merge refuses a batch that is not one campaign run against one environment.

The example study (stub models) is run three ways from copies made alike: by one unsharded driver, and as `--shard 1/2`
and `--shard 2/2`, two driver processes started together, each in a directory of its own with its own `out/`, as two
hosts would. The shards are merged, and check, analyze and report run on the merge. Each failure of a merge is planted
in a copy of the shards (or in a shard run again against another environment or under another campaign), and the merge
must refuse it, naming the shard and the reason in the words of `campaign.merge`.

The module reads the commands' own help for the run side (`run --shard`, `--shard-label`) and for the merge (`python
-m expcore merge`), and skips, naming what is missing, on a checkout that lacks either. This checkout has both, so the
first test below asserts that nothing is missing: the rehearsal cannot pass by finding nothing to run.
"""

import contextlib
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from shard_rehearsal_support import (
    COUNT,
    IDENTITY,
    LABELS,
    OTHER_IDENTITY,
    apart_from_where_and_when,
    make_driver_study,
    record_files,
    sha256_of,
    start_driver,
    wait_for,
    without_dispatch_positions,
)
from shard_support import json_meta, keys_of_shard, merge_into, meta_of, plant_run, without_run
from study_support import Campaign, clean_environment, cli, copy_campaign, finish

from expcore.campaign.resume import FINGERPRINT_KEY
from expcore.campaign.shard import (
    MERGE_FILE,
    MERGE_SCHEMA,
    META_ENVIRONMENT,
    META_SHARD,
    META_SHARES,
    META_STARTED,
    RULE,
    ShardSpec,
    caps_shares,
)
from expcore.cli import build_parser

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

STEPS = ("check", "analyze", "report")
UNIT_CAPS = {"alpha-stub": 2, "beta-stub": 2}  # the caps of the campaign, which each shard takes half of


# --- the skip: exactly what is missing, read from the commands' own help ---------------------------------------------


def help_of(*command: str) -> str:
    """What `python -m expcore <command> --help` prints; empty for a command the core does not have."""
    shown = io.StringIO()
    with contextlib.redirect_stdout(shown), contextlib.redirect_stderr(io.StringIO()), contextlib.suppress(SystemExit):
        build_parser().parse_args([*command, "--help"])
    return shown.getvalue()


def has_flag(help_text: str, flag: str) -> bool:
    """Whether the help names this option, and not an option that merely starts with its name."""
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text) is not None


def missing_features() -> list[str]:
    """The parts of #154's run side and merge this checkout lacks, each named; empty once it has them all."""
    missing = []
    run_help = help_of("run")
    missing += [f"`run {flag}`" for flag in ("--shard", "--shard-label") if not has_flag(run_help, flag)]
    if re.search(r"^usage:.*\bexpcore merge\b", help_of("merge"), re.MULTILINE) is None:
        missing.append("`merge`")
    return missing


@pytest.fixture(scope="module")
def shard_features() -> None:
    missing = missing_features()
    if missing:
        pytest.skip(f"the checkout lacks {', '.join(missing)}: the run side and the merge of #154 are not in it")


def test_the_checkout_has_the_run_side_and_the_merge_so_that_nothing_below_skips():
    assert missing_features() == []


def test_the_skip_reads_the_help_of_commands_the_core_has_and_of_one_it_has_not():
    """The detection is exact: it finds an option only where the help names it, and a command only where it exists."""
    assert has_flag(help_of("run"), "--allow-dirty")
    assert has_flag(help_of("check"), "--partial")
    assert not has_flag(help_of("run"), "--no-such-option")
    assert help_of("no-such-command") == ""
    assert has_flag("usage: run [--shard SHARD] [--shard-label LABEL]", "--shard")
    assert not has_flag("usage: run [--shard-label LABEL]", "--shard")
    assert not has_flag("usage: run [--shards N]", "--shard")
    assert has_flag("usage: run [--shard-label LABEL]", "--shard-label")


# --- the campaigns -------------------------------------------------------------------------------------------------


@dataclass
class Drivers:
    """One unsharded campaign and the two shards of the same one, each run, checked, and the unsharded one reported."""

    unsharded: Campaign
    shards: dict[int, Campaign]
    codes: dict[str, int] = field(default_factory=dict)
    printed: dict[str, str] = field(default_factory=dict)


@pytest.fixture(scope="module")
def drivers(tmp_path_factory, clean_environment, shard_features) -> Drivers:
    """Start both shards and the unsharded driver together, each a process of its own, then check each."""
    base = tmp_path_factory.mktemp("rehearsal")
    unsharded = Campaign(make_driver_study(base / "unsharded", "study"))
    shards = {i: Campaign(make_driver_study(base / f"host-{i}", "study")) for i in LABELS}
    started = {f"run shard {i}": start_driver(c.root, clean_environment, shard=(i, COUNT)) for i, c in shards.items()}
    started["run"] = start_driver(unsharded.root, clean_environment)  # every driver is started before any is waited on
    result = Drivers(unsharded, shards)
    for name, driver in started.items():
        result.codes[name], result.printed[name] = wait_for(driver)
    for i, campaign in shards.items():
        result.codes[f"check shard {i}"], result.printed[f"check shard {i}"] = cli(
            clean_environment, "check", str(campaign.runs)
        )
    finish(clean_environment, unsharded)
    return result


@pytest.fixture(scope="module")
def merged(tmp_path_factory, clean_environment, drivers) -> Campaign:
    """The shards merged into the batch of a study copy, and check, analyze and report run on it."""
    study = Campaign(make_driver_study(tmp_path_factory.mktemp("merged"), "study"))
    code, errors = merge_into(clean_environment, study, *drivers.shards.values())
    assert code == 0, f"the merge of the two shards was refused: {errors}"
    finish(clean_environment, study)
    return study


def merge_file(batch: Campaign) -> dict:
    return json.loads((batch.root / "out" / MERGE_FILE).read_text(encoding="utf-8"))


# --- the happy path ------------------------------------------------------------------------------------------------


def test_every_driver_ends_and_every_command_on_the_shards_and_on_the_merge_succeeds(drivers, merged):
    steps = dict.fromkeys(STEPS, 0)
    ran = ["run shard 1", "run shard 2", "run", "check shard 1", "check shard 2"]
    assert drivers.codes == dict.fromkeys(ran, 0), drivers.printed
    assert drivers.unsharded.codes == steps
    assert merged.codes == steps


def test_c28_each_shard_planned_and_ran_exactly_its_pairs_of_the_plan(drivers):
    """C28: through the command line: the two stores' plans are the pairs `assign` gives each shard, disjoint, and
    together the plan of the unsharded campaign; every planned run of a shard ended."""
    whole = {key for key, *_ in drivers.unsharded.planned()}
    owned = {i: set(keys_of_shard(drivers.unsharded.root, i)) for i in LABELS}
    assert {i: {key for key, *_ in c.planned()} for i, c in drivers.shards.items()} == owned
    assert owned[1]
    assert owned[2]
    assert not owned[1] & owned[2]
    assert owned[1] | owned[2] == whole
    assert len(whole) == 22
    for campaign in drivers.shards.values():
        assert {r["status"] for r in campaign.sql("SELECT status FROM runs")} == {"done"}


def test_c28_every_shard_has_the_campaign_fingerprint_of_the_whole_plan(drivers):
    whole = meta_of(drivers.unsharded)[FINGERPRINT_KEY]
    assert whole
    assert {i: meta_of(c)[FINGERPRINT_KEY] for i, c in drivers.shards.items()} == {1: whole, 2: whole}


def test_c29_each_shard_pins_its_spec_start_environment_and_shares_in_its_store(drivers):
    """C29: the store keeps what the merge compares: the spec with its label, the start, the identity the plugin gave
    at the start, and the shares of the caps; the unsharded store keeps none of them."""
    assert {META_SHARD, META_STARTED, META_ENVIRONMENT, META_SHARES}.isdisjoint(meta_of(drivers.unsharded))
    for i, campaign in drivers.shards.items():
        spec = ShardSpec(i, COUNT, LABELS[i])
        assert json_meta(campaign, META_SHARD) == spec.to_json()
        assert json_meta(campaign, META_ENVIRONMENT) == dict(sorted(IDENTITY.items()))
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", meta_of(campaign)[META_STARTED])
        assert json_meta(campaign, META_SHARES)["unit_caps"] == caps_shares(UNIT_CAPS, spec)


def test_c29_the_merge_holds_the_union_of_the_shards_records_and_each_shards_snapshot(drivers, merged):
    """C29: OUT/runs is the shards' runs together, byte for byte; each shard's snapshot is kept under its index, with
    the hash merge.json names; merge.json says what the batch is."""
    expected = {}
    for campaign in drivers.shards.values():
        for record in record_files(campaign.runs):
            expected[record.relative_to(campaign.runs)] = record.read_bytes()
    assert len(expected) == 22
    assert {r.relative_to(merged.runs): r.read_bytes() for r in record_files(merged.runs)} == expected
    batch = merge_file(merged)
    assert batch["schema"] == MERGE_SCHEMA
    assert (batch["rule"], batch["count"]) == (RULE, COUNT)
    assert batch["environment_identity"] == dict(sorted(IDENTITY.items()))
    assert batch["fingerprint"]
    assert [(e["index"], e["label"]) for e in batch["shards"]] == list(LABELS.items())
    for entry in batch["shards"]:
        i, campaign = entry["index"], drivers.shards[entry["index"]]
        snapshot = merged.root / "out" / "shards" / str(i) / "campaign-snapshot.sqlite"
        assert entry["snapshot_sha256"] == sha256_of(snapshot)
        assert entry["runs"] == len(keys_of_shard(drivers.unsharded.root, i))
        assert entry["started_utc"] == meta_of(campaign)[META_STARTED]
        assert entry["shares"] == json_meta(campaign, META_SHARES)


def test_c29_the_merged_export_names_each_runs_shard_and_an_unmerged_batch_names_none(drivers, merged):
    """C29: through `analyze` on the merge: each run entry holds the shard that ran it, the top-level `shards` is
    merge.json's list, and the unsharded batch's export has neither."""
    owner = {key: i for i in LABELS for key in keys_of_shard(drivers.unsharded.root, i)}
    export = merged.read("export.json")
    assert export["shards"] == merge_file(merged)["shards"]
    assert len(export["analysed_attempts"]) == 22
    assert {e["run_key"]: e["shard"] for e in export["analysed_attempts"]} == owner
    alone = drivers.unsharded.read("export.json")
    assert "shards" not in alone
    assert all("shard" not in entry for entry in alone["analysed_attempts"])


def test_c29_the_merged_scores_and_analysis_equal_those_of_one_driver_byte_for_byte(drivers, merged):
    """C29: every run's scores, and the analysis (contrasts, Holm decisions, conditions' counts, the pairs used and
    missing), derived anew from the merged records, are the bytes the unsharded campaign wrote."""
    one, two = drivers.unsharded.root / "out", merged.root / "out"
    assert len(json.loads((one / "scores.json").read_text(encoding="utf-8"))) == 22
    assert (two / "scores.json").read_bytes() == (one / "scores.json").read_bytes()
    assert (two / "analysis.json").read_bytes() == (one / "analysis.json").read_bytes()
    conditions = {c["condition"]: (c["planned"], c["analysed"]) for c in merged.read("analysis.json")["conditions"]}
    assert conditions == {"baseline": (8, 8), "extended": (6, 6), "relay-down": (8, 8)}


def test_c29_the_merged_export_and_report_equal_those_of_one_driver_apart_from_where_and_when_runs_were_dispatched(
    drivers, merged
):
    """C29: the export (how each run ended, each run's tallies, the accounting) and the report are the unsharded
    campaign's, but for the shard tags and what is per driver: dispatch positions, start times and lease waits."""
    one, two = drivers.unsharded, merged
    assert apart_from_where_and_when(two.read("export.json")) == apart_from_where_and_when(one.read("export.json"))
    assert without_dispatch_positions(two.report) == without_dispatch_positions(one.report)


# --- the refusals --------------------------------------------------------------------------------------------------


def refused(env, tmp_path: Path, *shards: Campaign, fragments: tuple[str, ...]) -> str:
    """Merge `shards` into a study copy, expect a refusal that holds every fragment and leaves nothing written."""
    study = Campaign(make_driver_study(tmp_path / "target", "study"))
    code, errors = merge_into(env, study, *shards)
    assert code == 1, errors
    assert errors.startswith("merge refused: "), errors
    for fragment in fragments:
        assert fragment in errors, f"{fragment!r} is not in: {errors}"
    assert not (study.root / "out").exists(), "a refused merge wrote the batch"
    assert not list(study.root.glob(".out.merge-*")), "a refused merge left its staging directory"
    return errors


def test_c29_the_untouched_shards_merge_again_from_copies(drivers, clean_environment, tmp_path):
    """C29: the control of the planted failures below: copies of the shards, nothing planted, merge."""
    one, two = (copy_campaign(c, tmp_path, f"copy-{i}") for i, c in drivers.shards.items())
    study = Campaign(make_driver_study(tmp_path / "target", "study"))
    code, errors = merge_into(clean_environment, study, one, two)
    assert code == 0, errors


def test_c29_a_run_in_two_shards_is_refused_naming_the_run_and_both_shards(drivers, clean_environment, tmp_path):
    """C29: a run `assign` gives to shard 2, present in shard 1's store too: the run sets are not disjoint."""
    one, two = drivers.shards.values()
    run = keys_of_shard(drivers.unsharded.root, 2)[0]
    both = plant_run(one, two, run, tmp_path, "shard-one-plus")
    errors = refused(
        clean_environment,
        tmp_path,
        both,
        two,
        fragments=(f"run {run} is in both shard 1 (shard-1) and shard 2 (shard-2)", "the shards overlap"),
    )
    assert "belongs to" not in errors  # the overlap is what is said first


def test_c29_a_run_in_the_wrong_shard_is_refused_naming_the_shard_it_belongs_to(drivers, clean_environment, tmp_path):
    """C29: a run of shard 2 moved into shard 1's store: shard 1 holds a run that is not its own."""
    one, two = drivers.shards.values()
    run = keys_of_shard(drivers.unsharded.root, 2)[0]
    wrong = plant_run(one, two, run, tmp_path, "shard-one-plus")
    short = without_run(two, run, tmp_path, "shard-two-minus")
    refused(
        clean_environment,
        tmp_path,
        wrong,
        short,
        fragments=(f"shard 1 (shard-1): run {run} belongs to shard 2 (shard-2), not to this shard",),
    )


def test_c29_a_missing_shard_is_refused_naming_it(drivers, clean_environment, tmp_path):
    """C29: shard 1 of 2 alone is not the campaign: the indices must be exactly 1 to N, and shard 2 is missing."""
    refused(clean_environment, tmp_path, drivers.shards[1], fragments=("shard 2 of 2 is missing",))


def test_c29_a_record_edited_after_its_check_is_refused_naming_the_shard_and_the_record(
    drivers, clean_environment, tmp_path
):
    """C29: shard 2 passed `check` when it finished; a record of it edited since no longer replays, and the merge
    refuses shard 2, naming the record."""
    one, two = (copy_campaign(c, tmp_path, f"copy-{i}") for i, c in drivers.shards.items())
    record = record_files(two.runs)[0]
    record.write_bytes(record.read_bytes() + b" ")
    refused(
        clean_environment,
        tmp_path,
        one,
        two,
        fragments=(
            "shard 2 (shard-2): 1 of ",
            "records fail the replay check",
            f"{record.relative_to(two.runs).as_posix()} [roundtrip]",
        ),
    )


@dataclass
class Rerun:
    """Shard 2 run again, after the plant that makes it another campaign or another environment, and checked."""

    campaign: Campaign
    printed: str


@pytest.fixture(scope="module")
def reruns(tmp_path_factory, clean_environment, drivers) -> dict[str, Rerun]:
    """Shard 2 again, twice at once: against another environment identity, and under another campaign fingerprint."""
    base = tmp_path_factory.mktemp("reruns")
    plans = {
        "environment": (make_driver_study(base / "environment", "study"), OTHER_IDENTITY),
        "fingerprint": (make_driver_study(base / "fingerprint", "study", time_cap_s=31), IDENTITY),
    }
    started = {
        name: start_driver(study, clean_environment, shard=(2, COUNT), identity=identity)
        for name, (study, identity) in plans.items()
    }
    found = {}
    for name, driver in started.items():
        code, printed = wait_for(driver)
        study = Campaign(plans[name][0])
        assert code == 0, printed
        assert cli(clean_environment, "check", str(study.runs))[0] == 0
        found[name] = Rerun(study, printed)
    return found


def test_c29_a_shard_that_recorded_another_environment_identity_is_refused_naming_what_differs(
    drivers, reruns, clean_environment, tmp_path
):
    """C29: shard 2 run against an environment whose one ingest manifest differs: every record and every check passes,
    and the merge still refuses, naming the shard and the identity that differs."""
    assert json_meta(reruns["environment"].campaign, META_ENVIRONMENT) == dict(sorted(OTHER_IDENTITY.items()))
    refused(
        clean_environment,
        tmp_path,
        drivers.shards[1],
        reruns["environment"].campaign,
        fragments=(
            "shard 2 (shard-2): ran against another environment than shard 1 (shard-1)",
            "its environment identity differs in ingest-space-1",
        ),
    )


def test_c29_a_shard_of_another_campaign_is_refused_naming_what_differs(drivers, reruns, clean_environment, tmp_path):
    """C29: shard 2 run under another time cap has another campaign fingerprint, and the merge refuses it."""
    assert meta_of(reruns["fingerprint"].campaign)[FINGERPRINT_KEY] != meta_of(drivers.shards[1])[FINGERPRINT_KEY]
    refused(
        clean_environment,
        tmp_path,
        drivers.shards[1],
        reruns["fingerprint"].campaign,
        fragments=(
            "shard 2 (shard-2): ran another campaign than shard 1 (shard-1); its fingerprint differs:",
            "caps.agent.limits.time_cap_s: recorded 30.0, now 31.0",
        ),
    )
