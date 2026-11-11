"""Merging the shards of one campaign (#154, C29): the six refusals, the merged layout, and check, analyze, report and
regenerate on the merged batch, which give the scores of one driver of the whole campaign.

The shards are the example study run for real, each over its own part of the plan (`shard_support.run_shard`), so the
records, stores and dispatch sequences are a shard's own. Each refusal plants one fault in a copy of a finished shard
and expects the merge to name the shard and the reason and to leave nothing behind.
"""

import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from shard_support import IDENTITY, keys_of_shard, merge_into, run_shard, shard_meta, write_meta
from study_support import Campaign, clean_environment, cli, copy_campaign, finish, make_copy

from expcore.campaign.merge import SHARDS_DIR
from expcore.campaign.shard import (
    MERGE_FILE,
    MERGE_SCHEMA,
    META_ENVIRONMENT,
    META_SHARD,
    RULE,
    ShardSpec,
)
from expcore.study.pipeline import regenerate

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

SNAPSHOT = "campaign-snapshot.sqlite"
RECORD = re.compile(r"[0-9a-f]{64}\.\d+\.json")


@pytest.fixture(scope="module")
def base(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("merge")


@pytest.fixture(scope="module")
def whole(base, clean_environment) -> Campaign:
    """One driver of the whole campaign: the reference the merged batch must agree with."""
    campaign = Campaign(make_copy(base, "whole"))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture(scope="module")
def shard1(base, clean_environment) -> Campaign:
    return run_shard(base, "shard-one", clean_environment, 1)


@pytest.fixture(scope="module")
def shard2(base, clean_environment) -> Campaign:
    return run_shard(base, "shard-two", clean_environment, 2)


@pytest.fixture(scope="module")
def foreign(shard2) -> str:
    """A run of shard 2, which shard 1 may be made to hold by mistake."""
    return keys_of_shard(shard2.root, 2)[0]


@pytest.fixture(scope="module")
def shard1_with_foreign(base, clean_environment, foreign) -> Campaign:
    """Shard 1 whose plan also holds a run that belongs to shard 2."""
    return run_shard(base, "shard-one-plus", clean_environment, 1, add=[foreign])


@pytest.fixture(scope="module")
def shard2_without_foreign(base, clean_environment, foreign) -> Campaign:
    """Shard 2 whose plan leaves out one of its own runs."""
    return run_shard(base, "shard-two-minus", clean_environment, 2, drop=[foreign])


@pytest.fixture(scope="module")
def merged(base, clean_environment, shard1, shard2) -> Campaign:
    """The two shards merged into a copy of the study, checked, analysed and reported."""
    campaign = Campaign(make_copy(base, "merged"))
    campaign.codes["merge"], errors = merge_into(clean_environment, campaign, shard1, shard2)
    assert campaign.codes["merge"] == 0, errors
    finish(clean_environment, campaign)
    assert campaign.codes == {"merge": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


# --- helpers ---------------------------------------------------------------------------------------------------------


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_out(shard: Campaign, dest: Path) -> Path:
    """A copy of a shard's out/, to plant a fault in."""
    return Path(shutil.copytree(shard.root / "out", dest / "out"))


def refused(env, study: Campaign, out: Path, *outs: Path, fragments: tuple[str, ...]) -> str:
    """Merge `outs` into `out` for `study`, expect a refusal that holds every fragment and leaves `out` unwritten."""
    code, errors = cli(env, "merge", str(out), *map(str, outs), "--study", str(study.root))
    assert code == 1, errors
    assert errors.startswith("merge refused: "), errors
    for fragment in fragments:
        assert fragment in errors, errors
    assert not out.exists(), "a refused merge wrote the batch"
    assert not list(out.parent.glob(f".{out.name}.merge-*")), "a refused merge left its staging directory"
    return errors


def store_of(out: Path) -> Path:
    return out / "state.sqlite"


def edit_json_meta(out: Path, key: str, change) -> None:
    connection = sqlite3.connect(store_of(out))
    try:
        value = json.loads(connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0])
    finally:
        connection.close()
    write_meta(store_of(out), {key: json.dumps(change(value), sort_keys=True, separators=(",", ":"))})


def read(campaign: Campaign, name: str):
    return json.loads((campaign.root / "out" / name).read_text(encoding="utf-8"))


LISTS = ("exhausted_runs", "unfinished_runs", "analysed_attempts", "excluded_attempts")


# --- the merged layout -----------------------------------------------------------------------------------------------


def test_c29_a_good_merge_writes_the_batch_the_snapshots_and_the_merge_file(merged, shard1, shard2):
    """C29: shards that are one campaign merge into OUT/runs, one snapshot per shard and merge.json, which names no
    host or path and holds each snapshot's SHA-256."""
    out = merged.root / "out"
    document = json.loads((out / MERGE_FILE).read_text(encoding="utf-8"))
    assert document["schema"] == MERGE_SCHEMA
    assert (document["rule"], document["count"]) == (RULE, 2)
    assert document["environment_identity"] == IDENTITY
    assert [(s["index"], s["label"]) for s in document["shards"]] == [(1, "shard-1"), (2, "shard-2")]
    for shard, campaign in zip(document["shards"], (shard1, shard2), strict=True):
        snapshot = out / SHARDS_DIR / str(shard["index"]) / SNAPSHOT
        assert shard["snapshot_sha256"] == sha(snapshot)
        assert shard["runs"] == len(keys_of_shard(campaign.root, shard["index"]))
        assert shard["started_utc"].startswith(f"2026-03-0{shard['index']}")
        assert shard["shares"] == json.loads(shard_meta(shard["index"], 2, "x")["shard_shares"])
    expected = {p.name for c in (shard1, shard2) for p in (c.root / "out" / "runs").iterdir() if p.is_dir()}
    assert {p.name for p in (out / "runs").iterdir() if p.is_dir()} == expected
    assert not (out / "runs" / SNAPSHOT).exists()  # the merged batch holds no snapshot of its own
    text = (out / MERGE_FILE).read_text(encoding="utf-8")
    assert str(merged.root) not in text
    assert str(shard1.root) not in text


def test_c29_a_merge_only_reads_the_shards(base, clean_environment, shard1, shard2):
    """C29: the merge never changes a shard's out/: its records and its store stay as they were."""
    before = {p: sha(p) for c in (shard1, shard2) for p in (c.root / "out" / "runs").rglob("*") if p.is_file()}
    campaign = Campaign(make_copy(base, "reads-only"))
    code, errors = merge_into(clean_environment, campaign, shard1, shard2)
    assert code == 0, errors
    after = {p: sha(p) for c in (shard1, shard2) for p in (c.root / "out" / "runs").rglob("*") if p.is_file()}
    assert after == before


def test_c29_check_binds_merge_json_and_every_shard_snapshot_to_the_check(merged):
    """C29: `check` on the merged batch records the SHA-256 of merge.json, and merge.json holds each snapshot's."""
    out = merged.root / "out"
    check = json.loads((out / "runs" / "replay-check.json").read_text(encoding="utf-8"))
    assert check["merge"]["file"] == MERGE_FILE
    assert check["merge"]["sha256"] == sha(out / MERGE_FILE)
    assert check["n_ok"] == check["n_records"] > 0
    assert "snapshot" not in check  # no snapshot of a store: the shards' were taken at the merge


# --- the export, analysis and report of a merged batch ----------------------------------------------------------------


def test_c29_the_merged_export_tags_every_run_with_its_shard_and_lists_the_shards(merged, shard1, shard2):
    """C29: the batch's export is the shards' joined: every entry of every run list (excluded ones included) says
    which shard ran it, `shards` is merge.json's list, and each shard's `runs` counts the entries tagged with it."""
    export = read(merged, "export.json")
    merge = json.loads((merged.root / "out" / MERGE_FILE).read_text(encoding="utf-8"))
    assert export["shards"] == merge["shards"]
    indices = [shard["index"] for shard in export["shards"]]
    assert indices == sorted(set(indices)) == [1, 2]
    for name in LISTS:
        assert all("shard" in entry for entry in export.get(name, [])), name
    for shard in export["shards"]:
        tagged = [e for name in LISTS for e in export.get(name, []) if e["shard"] == shard["index"]]
        assert len(tagged) == shard["runs"]
    owner = {
        key: index for index, campaign in ((1, shard1), (2, shard2)) for key in keys_of_shard(campaign.root, index)
    }
    assert all(e["shard"] == owner[e["run_key"]] for e in export["analysed_attempts"])
    assert export["planned_runs"] == sum(shard["runs"] for shard in export["shards"])


def test_c29_the_merged_batch_gives_the_scores_of_one_unsharded_driver(whole, merged):
    """C29: scores, analysis and report of the merged batch equal those of one driver of the same campaign; only
    the fields of a shard's own sequence (dispatch positions, start times, lease waits) and the shard tags differ."""
    assert (merged.root / "out" / "scores.json").read_bytes() == (whole.root / "out" / "scores.json").read_bytes()
    assert (merged.root / "out" / "analysis.json").read_bytes() == (whole.root / "out" / "analysis.json").read_bytes()
    one, many = read(whole, "export.json"), read(merged, "export.json")
    own = {"shard", "dispatch_seq", "started_at", "lease_wait_s"}
    for key in one:
        if key == "analysed_attempts":
            continue
        assert many[key] == one[key], key
    plain = lambda entries: [{k: v for k, v in e.items() if k not in own} for e in entries]  # noqa: E731
    assert plain(many["analysed_attempts"]) == plain(one["analysed_attempts"])  # the same rows, in the same order
    assert set(many) - set(one) == {"shards"}
    assert _report_differs_only_in_positions(merged.report, whole.report)


def _report_differs_only_in_positions(merged: str, whole: str) -> bool:
    """The reports are the same line by line, but for the last cell of the conditions table: the median dispatch
    position, which each shard counts on its own."""
    lines_a, lines_b = merged.splitlines(), whole.splitlines()
    if len(lines_a) != len(lines_b):
        return False
    for a, b in zip(lines_a, lines_b, strict=True):
        if a != b and not (a.startswith("| ") and a.rsplit(" | ", 1)[0] == b.rsplit(" | ", 1)[0]):
            return False
    return True


def test_c29_regenerate_derives_the_outputs_of_a_merged_batch_anew(merged):
    """C29: the four outputs of a merged batch regenerate from its records and snapshots byte for byte."""
    assert regenerate(merged.root) == []


def test_c29_the_export_of_an_unmerged_batch_has_neither_key(whole):
    """C29: an unsharded campaign's export and its run entries carry no shard, and its check no merge."""
    export = read(whole, "export.json")
    assert "shards" not in export
    assert "shard" not in export
    assert all("shard" not in e for name in LISTS for e in export.get(name, []))
    assert "merge" not in read(whole, "runs/replay-check.json")


def test_c29_a_partial_analysis_of_a_merged_batch_tags_the_runs_it_leaves_out(base, clean_environment, merged):
    """C29: a run whose record is gone is left out under --partial, and its entry in `excluded_attempts` says which
    shard ran it, as every other entry does."""
    copy = copy_campaign(merged, base, "partial")
    victim = next(p for p in sorted(copy.runs.rglob("*.json")) if RECORD.fullmatch(p.name))
    victim.unlink()
    assert cli(clean_environment, "check", str(copy.runs), "--partial")[0] == 0
    code, errors = cli(clean_environment, "analyze", str(copy.root), "--partial")
    assert code == 0, errors
    export = read(copy, "export.json")
    (left,) = export["excluded_attempts"]
    assert left["shard"] in (1, 2)
    assert export["partial"] is True
    assert sum(s["runs"] for s in export["shards"]) == export["planned_runs"]


# --- the checks a merged batch keeps making ---------------------------------------------------------------------------


def test_c29_a_snapshot_changed_after_the_merge_fails_the_check(base, clean_environment, merged):
    """C29: `check` compares each shard's snapshot with the SHA-256 merge.json names, so a snapshot edited after the
    merge is refused, naming the shard."""
    copy = copy_campaign(merged, base, "snapshot-edited")
    snapshot = copy.root / "out" / SHARDS_DIR / "2" / SNAPSHOT
    connection = sqlite3.connect(snapshot)
    connection.execute("UPDATE attempts SET wall_s = wall_s + 1")
    connection.commit()
    connection.close()
    code, errors = cli(clean_environment, "check", str(copy.runs))
    assert code == 1
    assert "shard 2 (shard-2)" in errors
    assert "changed since the merge" in errors


def test_c29_a_merge_file_changed_after_the_check_is_refused_by_the_analysis(base, clean_environment, merged):
    """C29: the analysis reads merge.json once and compares it with the hash the check recorded, so a file edited
    after the check is refused."""
    copy = copy_campaign(merged, base, "merge-edited")
    path = copy.root / "out" / MERGE_FILE
    document = json.loads(path.read_text(encoding="utf-8"))
    document["shards"][0]["label"] = "another-label"
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    code, errors = cli(clean_environment, "analyze", str(copy.root))
    assert code == 1
    assert "changed after the replay check read it" in errors


def test_c29_a_snapshot_changed_after_the_check_is_refused_by_the_analysis(base, clean_environment, merged):
    """C29: nor does the analysis read a shard's snapshot that is not the bytes merge.json names."""
    copy = copy_campaign(merged, base, "snapshot-edited-late")
    snapshot = copy.root / "out" / SHARDS_DIR / "1" / SNAPSHOT
    snapshot.write_bytes(snapshot.read_bytes() + b"\0" * 4096)
    code, errors = cli(clean_environment, "analyze", str(copy.root))
    assert code == 1
    assert f"{SHARDS_DIR}/1/{SNAPSHOT}" in errors
    assert "changed after the replay check read it" in errors


# --- what a merge refuses -------------------------------------------------------------------------------------------


def test_c29_a_merge_needs_every_shard_exactly_once(base, clean_environment, whole, shard1, shard2):
    """C29: shards 1..N each exactly once: a missing index and a duplicated one are refused, naming the shard."""
    out = base / "refused-missing" / "out"
    refused(clean_environment, whole, out, shard1.root / "out", fragments=("shard 2 of 2 is missing",))
    twin = copy_out(shard1, base / "twin-of-one")
    refused(
        clean_environment,
        whole,
        base / "refused-dup" / "out",
        shard1.root / "out",
        twin,
        shard2.root / "out",
        fragments=("shard 1 (shard-1) and shard 1 (shard-1) are both shard 1 of 2",),
    )


def test_c29_shards_of_another_count_are_refused(base, clean_environment, whole, shard1, shard2):
    """C29: a shard of a campaign split into another number of shards is refused, naming both."""
    other = copy_out(shard2, base / "count-three")
    write_meta(store_of(other), shard_meta(2, 3, "shard-2"))
    refused(
        clean_environment,
        whole,
        base / "refused-count" / "out",
        shard1.root / "out",
        other,
        fragments=("shard 2 (shard-2): its campaign was split into 3 shards", "shard 1 (shard-1)'s into 2"),
    )


def test_c29_a_shard_under_another_partition_rule_is_refused(base, clean_environment, whole, shard1, shard2):
    """C29: a store made under another partition rule is refused, naming the rules."""
    other = copy_out(shard2, base / "rule-other")
    edit_json_meta(other, META_SHARD, lambda spec: {**spec, "rule": "expcore-shard/1: an older rule"})
    refused(
        clean_environment,
        whole,
        base / "refused-rule" / "out",
        shard1.root / "out",
        other,
        fragments=("shard spec in its store is unusable", "expcore-shard/1: an older rule", RULE),
    )


def test_c29_two_shards_with_one_label_are_refused(base, clean_environment, whole, shard1, shard2):
    """C29: labels are distinct, so a merge never takes two shards for one host."""
    other = copy_out(shard2, base / "label-same")
    write_meta(store_of(other), {META_SHARD: json.dumps(ShardSpec(2, 2, "shard-1").to_json())})
    refused(
        clean_environment,
        whole,
        base / "refused-label" / "out",
        shard1.root / "out",
        other,
        fragments=("shard 2 (shard-1) and shard 1 (shard-1) have the same label",),
    )


def test_c29_shards_of_different_campaigns_are_refused_naming_what_differs(
    base, clean_environment, whole, shard1, shard2
):
    """C29: every shard has the campaign fingerprint of the first; one that differs is refused with the paths."""
    other = copy_out(shard2, base / "fingerprint-other")
    edit_json_meta(other, "fingerprint", lambda fingerprint: {**fingerprint, "seeds": {"1": True, "2": True}})
    errors = refused(
        clean_environment,
        whole,
        base / "refused-fingerprint" / "out",
        shard1.root / "out",
        other,
        fragments=("shard 2 (shard-2): ran another campaign than shard 1 (shard-1)", "seeds.2"),
    )
    assert "fingerprint differs" in errors


def test_c29_shards_that_overlap_are_refused_naming_the_run(
    base, clean_environment, whole, foreign, shard1_with_foreign, shard2
):
    """C29: a run in two shards is refused, naming the run and both shards."""
    errors = refused(
        clean_environment,
        whole,
        base / "refused-overlap" / "out",
        shard1_with_foreign.root / "out",
        shard2.root / "out",
        fragments=(f"run {foreign} is in both shard 1 (shard-1) and shard 2 (shard-2)", "the shards overlap"),
    )
    assert "belongs to" not in errors  # the overlap is the first thing said, not the shard it is wrong in


def test_c29_a_run_in_the_wrong_shard_is_refused_naming_its_own(
    base, clean_environment, whole, foreign, shard1_with_foreign, shard2_without_foreign
):
    """C29: every run belongs to its shard under the assignment of the whole plan: shard 1 holding a run of shard 2
    is refused, naming the run and the shard it belongs to."""
    refused(
        clean_environment,
        whole,
        base / "refused-wrong" / "out",
        shard1_with_foreign.root / "out",
        shard2_without_foreign.root / "out",
        fragments=(f"shard 1 (shard-1): run {foreign} belongs to shard 2 (shard-2), not to this shard",),
    )


def test_c29_a_run_missing_from_every_shard_is_refused_naming_the_shard_it_belongs_to(
    base, clean_environment, whole, foreign, shard1, shard2_without_foreign
):
    """C29: the shards' runs together are exactly the plan: one that no shard holds is refused."""
    refused(
        clean_environment,
        whole,
        base / "refused-missing-run" / "out",
        shard1.root / "out",
        shard2_without_foreign.root / "out",
        fragments=(f"run {foreign}, which belongs to shard 2 (shard-2), is in no shard", "1 of the plan's 22 runs"),
    )


def test_c29_a_shard_whose_replay_check_fails_is_refused_naming_the_record(
    base, clean_environment, whole, shard1, shard2
):
    """C29: each shard passes the replay check `check` makes: a record changed since it was written is refused,
    naming the shard, the file and the stage."""
    other = copy_out(shard2, base / "replay-fails")
    victim = next(p for p in sorted((other / "runs").rglob("*.json")) if RECORD.fullmatch(p.name))
    victim.write_bytes(victim.read_bytes() + b" ")
    refused(
        clean_environment,
        whole,
        base / "refused-replay" / "out",
        shard1.root / "out",
        other,
        fragments=(
            "shard 2 (shard-2): 1 of ",
            "records fail the replay check",
            f"{victim.relative_to(other / 'runs').as_posix()} [roundtrip]",
        ),
    )


def test_c29_shards_run_against_different_environments_are_refused(base, clean_environment, whole, shard1, shard2):
    """C29: every shard records the same environment identity; a shard that ran against other data is refused,
    naming what differs."""
    other = copy_out(shard2, base / "identity-other")
    changed = {**IDENTITY, "platform": hashlib.sha256(b"another platform configuration").hexdigest()}
    write_meta(store_of(other), {META_ENVIRONMENT: json.dumps(changed, sort_keys=True)})
    refused(
        clean_environment,
        whole,
        base / "refused-identity" / "out",
        shard1.root / "out",
        other,
        fragments=("shard 2 (shard-2): ran against another environment than shard 1 (shard-1)", "differs in platform"),
    )


def test_c29_a_store_that_ran_unsharded_or_has_attempts_running_is_refused(
    base, clean_environment, whole, shard1, shard2
):
    """C29: an unsharded store is no shard, and a shard with attempts running has not finished."""
    refused(
        clean_environment,
        whole,
        base / "refused-unsharded" / "out",
        shard1.root / "out",
        whole.root / "out",
        fragments=("not a shard", "ran unsharded"),
    )
    busy = copy_out(shard2, base / "busy")
    connection = sqlite3.connect(store_of(busy))
    connection.execute("UPDATE attempts SET state = 'running' WHERE id = (SELECT MIN(id) FROM attempts)")
    connection.commit()
    connection.close()
    refused(
        clean_environment,
        whole,
        base / "refused-busy" / "out",
        shard1.root / "out",
        busy,
        fragments=("still running", "a shard is merged when it has finished"),
    )


def test_c29_a_merge_writes_only_a_new_or_empty_directory(base, clean_environment, whole, shard1, shard2):
    """C29: OUT must not exist or must be empty, so a merge never mixes into a batch that is already there."""
    taken = base / "taken" / "out"
    (taken / "runs").mkdir(parents=True)
    code, errors = cli(clean_environment, "merge", str(taken), str(shard1.root / "out"), str(shard2.root / "out"),
                       "--study", str(whole.root))  # fmt: skip
    assert code == 1
    assert "must not exist or must be an empty directory" in errors
    assert (taken / "runs").is_dir()
    assert not (taken / MERGE_FILE).exists()
    empty = base / "empty" / "out"
    empty.mkdir(parents=True)
    code, errors = cli(clean_environment, "merge", str(empty), str(shard1.root / "out"), str(shard2.root / "out"),
                       "--study", str(whole.root))  # fmt: skip
    assert code == 0, errors  # an empty directory is taken
    assert (empty / MERGE_FILE).is_file()
