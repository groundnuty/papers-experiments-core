"""The export: exhausted runs reach the analysis (C09), every infrastructure kind keeps its name (C02), C01."""

import json
import os
import stat
import threading

import pytest
from campaign_support import (
    LATER,
    NOW,
    defect,
    finished,
    infra,
    keys_under,
    make_manifest,
    open_store,
    planned_store,
    run_one,
    treatment_schema,
)

from expcore import errors
from expcore.campaign.export import (
    FORMAT,
    REQUIRED_KEYS,
    RUN_LISTS,
    ExportError,
    export_run_state,
    read_export,
    write_export,
    write_export_data,
)
from expcore.campaign.markers import AttemptResult
from expcore.campaign.recovery import recover
from expcore.errors import CORE_KINDS, infra_kinds, register_infra_kind
from expcore.loop.outcomes import Outcome


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Every test starts with only the core's infrastructure kinds and leaves no platform kind behind."""
    monkeypatch.setattr(errors, "_platform_kinds", {})


def keys_of(manifest):
    return [row.key.text for row in manifest.rows]


def test_c09_an_exhausted_run_appears_in_the_export_with_its_reasons(tmp_path):
    """C09: a run that used all its attempts is exported with reasons, so no denominator loses it."""
    store, schema, manifest = planned_store(tmp_path)
    lost, kept, untouched = keys_of(manifest)[:3]
    run_one(store, lost, infra("endpoint", "HTTP 503"), max_attempts=2)
    run_one(store, lost, infra("sandbox", "container died"), max_attempts=2)
    run_one(store, kept, finished())
    data = export_run_state(store)
    (row,) = data["exhausted_runs"]
    assert row["run_key"] == lost
    assert row["attempts"] == 2
    assert row["reasons"] == ["endpoint: HTTP 503", "sandbox: container died"]
    assert row["condition"] in schema.conditions
    assert row["factors"]["task"]
    assert row["factors"]["seed"] in (1, 2)
    assert [r["run_key"] for r in data["analysed_attempts"]] == [kept]
    assert untouched in [r["run_key"] for r in data["unfinished_runs"]]
    assert data["planned_runs"] == len(manifest.rows)
    total = sum(len(data[name]) for name in RUN_LISTS)
    assert total == len(manifest.rows)  # every run is somewhere


def test_c09_the_analysis_refuses_an_export_without_the_key(tmp_path):
    """C09: an export that lacks a key, even an empty list, is refused by the reader."""
    store, _, _ = planned_store(tmp_path)
    path = tmp_path / "export.json"
    write_export(store, path)
    assert read_export(path)["exhausted_runs"] == []  # empty lists are written out and valid
    for key in REQUIRED_KEYS:
        data = json.loads(path.read_text(encoding="utf-8"))
        del data[key]
        broken = tmp_path / f"without-{key}.json"
        broken.write_text(json.dumps(data), encoding="utf-8")
        message = f"{FORMAT} export" if key == "format" else f"missing key.*{key}"
        with pytest.raises(ExportError, match=message):
            read_export(broken)


def test_c09_the_reader_refuses_an_export_that_loses_a_run(tmp_path):
    """C09: the lists of runs must add up to the plan."""
    store, _, manifest = planned_store(tmp_path)
    path = tmp_path / "export.json"
    write_export(store, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["unfinished_runs"].pop()
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(
        ExportError, match=f"lists {len(manifest.rows) - 1} runs but the plan held {len(manifest.rows)}"
    ):
        read_export(path)


@pytest.mark.parametrize("content", ["{not json", "[]", '{"format": "something-else"}'])
def test_the_reader_refuses_a_file_that_is_not_an_export(tmp_path, content):
    path = tmp_path / "export.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ExportError, match=r"cannot be read as an export|not a campaign-export-1 export"):
        read_export(path)
    with pytest.raises(ExportError, match="cannot be read as an export"):
        read_export(tmp_path / "absent.json")


def test_c01_an_infrastructure_failure_is_absent_from_the_analysed_attempts(tmp_path):
    """C01: an API error never counts as a scored outcome; only agent outcomes reach the analysed attempts."""
    store, _, manifest = planned_store(tmp_path)
    failed, scored, capped = keys_of(manifest)[:3]
    run_one(store, failed, infra("endpoint"))
    run_one(store, scored, finished())
    run_one(store, capped, AttemptResult(Outcome("turn_cap", "20 rounds"), LATER, 40.0))  # the agent's own outcome
    data = export_run_state(store)
    assert sorted(r["run_key"] for r in data["analysed_attempts"]) == sorted([scored, capped])
    assert failed in [r["run_key"] for r in data["unfinished_runs"]]
    assert failed not in [r["run_key"] for r in data["analysed_attempts"]]


def test_an_analysed_attempt_names_its_directory_within_the_batch_and_not_the_path_it_ran_under(tmp_path):
    """The export holds no path of the machine the campaign ran on, so a copy of the campaign exports the same bytes."""
    store, _, manifest = planned_store(tmp_path)
    key = keys_of(manifest)[0]
    ran_under = tmp_path / "somewhere" / "out" / "runs" / ("d" * 64) / "try1"
    run_one(store, key, finished(), run_dir=str(ran_under))
    (entry,) = export_run_state(store)["analysed_attempts"]
    assert entry["run_dir"] == f"{'d' * 64}/try1"
    assert str(tmp_path) not in json.dumps(export_run_state(store))


def test_c02_every_infrastructure_kind_keeps_its_own_name_in_the_export(tmp_path):
    """C02: every harness kind appears under its own name, the core's and a platform's alike."""
    register_infra_kind("tool_outage", "a platform's own kind")
    store, _, manifest = planned_store(tmp_path, tasks=("task-a", "task-b", "task-c"))
    kinds = infra_kinds()
    assert len(kinds) == len(CORE_KINDS) + 1
    for index, kind in enumerate(kinds):
        for _ in range(index + 1):  # a different count for each kind, so a merge could not hide
            run_one(store, keys_of(manifest)[index], infra(kind), max_attempts=10)
    data = export_run_state(store)
    assert data["kinds"] == list(kinds)
    per_kind = {}
    for row in data["retries_by_kind"]:
        per_kind[row["kind"]] = per_kind.get(row["kind"], 0) + row["attempts"]
    assert per_kind == {kind: index + 1 for index, kind in enumerate(kinds)}
    assert {"knowledge", "unattributed_kill", "ceiling", "tool_outage"} <= set(per_kind)  # none folded into "sandbox"


def test_retries_are_counted_per_condition_model_kind_and_whether_they_were_charged(tmp_path):
    store, schema, manifest = planned_store(tmp_path, models=("m1", "m2"))
    chosen = {}
    for row in manifest.rows:
        chosen.setdefault((schema.condition_of(row.key), row.key.factors["model"]), row.key.text)
    (first_cell, first), (second_cell, second) = list(chosen.items())[:2]
    run_one(store, first, infra("endpoint"), max_attempts=9)
    run_one(store, first, infra("endpoint"), max_attempts=9)
    run_one(store, first, infra("key_rejected"), max_attempts=9)
    run_one(store, second, AttemptResult(Outcome("finished"), LATER, 1.0, outage_kind="endpoint"))
    rows = {
        (r["condition"], r["model"], r["kind"], r["state"], r["charged"]): r["attempts"]
        for r in export_run_state(store)["retries_by_kind"]
    }
    assert rows == {
        (*first_cell, "endpoint", "infra_fail", True): 2,
        (*first_cell, "key_rejected", "infra_fail", False): 1,
        (*second_cell, "endpoint", "outage_discarded", False): 1,
    }


def test_discarded_usage_keeps_infrastructure_interrupted_outage_and_harness_attempts_apart(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    a, b, c, d = keys_of(manifest)[:4]
    run_one(store, a, infra(input_tokens=100, output_tokens=10), max_attempts=9)
    run_one(store, a, infra(input_tokens=50, output_tokens=5), max_attempts=9)
    run_one(store, b, AttemptResult(Outcome("finished"), LATER, 4.0, outage_kind="sandbox", input_tokens=7))
    run_one(store, c, defect(input_tokens=3))
    store.begin_attempt(d, NOW, 1, "")
    recover(store, LATER, 3)
    usage = {}
    for row in export_run_state(store)["discarded_usage"]:
        total = usage.setdefault(
            row["state"], dict.fromkeys(("attempts", "input_tokens", "output_tokens", "usage_incomplete_attempts"), 0)
        )
        for name in total:
            total[name] += row[name]
    assert set(usage) == {"infra_fail", "outage_discarded", "harness_error", "interrupted"}
    assert usage["infra_fail"] == {
        "attempts": 2,
        "input_tokens": 150,
        "output_tokens": 15,
        "usage_incomplete_attempts": 0,
    }
    assert usage["outage_discarded"]["input_tokens"] == 7
    assert usage["harness_error"]["input_tokens"] == 3
    assert (usage["interrupted"]["attempts"], usage["interrupted"]["usage_incomplete_attempts"]) == (1, 1)


def test_a_done_run_with_no_single_done_attempt_is_an_error_not_a_guess(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    key = keys_of(manifest)[0]
    run_one(store, key, finished())
    store.db.execute("UPDATE attempts SET state='infra_fail' WHERE run_key=?", (key,))
    with pytest.raises(ExportError, match="is done but has 0 done attempts"):
        export_run_state(store)


def test_a_status_the_export_does_not_know_is_an_error(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    store.db.execute("UPDATE runs SET status='lost' WHERE run_key=?", (keys_of(manifest)[0],))
    with pytest.raises(ExportError, match="unknown status 'lost'"):
        export_run_state(store)


def test_the_export_uses_generic_field_names_only(tmp_path):
    store, _, manifest = planned_store(tmp_path)
    keys = keys_of(manifest)
    run_one(store, keys[0], finished())
    run_one(store, keys[1], infra(), max_attempts=1)
    run_one(store, keys[2], infra())
    run_one(store, keys[3], defect())
    data = export_run_state(store)
    identity = {"run_key", "digest", "condition", "factors"}
    assert set(data) == set(REQUIRED_KEYS)
    assert data["format"] == FORMAT
    assert data["factors"] == ["task", "data_path", "tools", "model", "seed"]
    assert set(data["exhausted_runs"][0]) == identity | {"attempts", "reasons"}
    assert set(data["unfinished_runs"][0]) == identity | {"status"}
    assert set(data["analysed_attempts"][0]) == identity | {
        "attempt",
        "run_dir",
        "treatment",
        "infra_kind",
        "dispatch_seq",
        "started_at",
        "lease_wait_s",
        "lease_outcome",
    }
    assert set(data["discarded_usage"][0]) == {
        "condition",
        "model",
        "state",
        "attempts",
        "usage_incomplete_attempts",
        "input_tokens",
        "output_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
        "wall_s",
    }
    assert set(data["retries_by_kind"][0]) == {"condition", "model", "kind", "state", "charged", "attempts"}
    assert set(data["infrastructure_by_condition"][0]) == {
        "condition",
        "kind",
        "declared_treatment",
        "set_aside",
        "treatment",
    }


def test_the_export_is_written_to_a_temporary_file_and_moved_into_place(tmp_path, monkeypatch):
    store, _, _ = planned_store(tmp_path)
    target = tmp_path / "out" / "export.json"
    calls = []
    real_replace = os.replace

    def spy(source, destination):
        calls.append((source, destination, os.path.exists(source), os.path.exists(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", spy)
    written = write_export(store, target)
    ((source, destination, source_existed, target_existed),) = calls
    assert (os.path.dirname(source), destination, source_existed, target_existed) == (
        str(target.parent),
        target,
        True,
        False,
    )
    assert os.path.basename(source) != target.name
    assert [p.name for p in target.parent.iterdir()] == ["export.json"]
    assert json.loads(target.read_text(encoding="utf-8")) == written == read_export(target)


def test_a_failed_write_leaves_the_earlier_export_whole_and_no_temporary_file(tmp_path, monkeypatch):
    store, _, manifest = planned_store(tmp_path)
    target = tmp_path / "export.json"
    write_export(store, target)
    before = target.read_text(encoding="utf-8")
    run_one(store, keys_of(manifest)[0], finished())

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(json, "dumps", broken)  # the export's text is made inside the write, after the scratch file
    with pytest.raises(OSError, match="disk full"):
        write_export(store, target)
    assert target.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


# --- infrastructure that is a condition's treatment ----------------------------------------------------------------


def test_c09_a_treatment_attempt_reaches_the_analysis_marked_with_its_kind(tmp_path):
    """C09: adds that a run ended by its condition's treatment fault is an analysed attempt, not an exhausted run."""
    schema = treatment_schema(("endpoint",))
    store = open_store(tmp_path, schema)
    manifest = make_manifest(schema)
    store.sync_manifest(manifest.rows)
    down, healthy = keys_under(schema, manifest, "api-none")[0], keys_under(schema, manifest, "mount-read")[0]
    run_one(store, down, infra("endpoint", "HTTP 503"))
    run_one(store, healthy, finished())
    data = export_run_state(store)
    rows = {r["run_key"]: r for r in data["analysed_attempts"]}
    assert (rows[down]["treatment"], rows[down]["infra_kind"]) == (True, "endpoint")
    assert (rows[healthy]["treatment"], rows[healthy]["infra_kind"]) == (False, None)
    assert data["exhausted_runs"] == []
    assert down not in [r["run_key"] for r in data["unfinished_runs"]]
    assert sum(len(data[name]) for name in RUN_LISTS) == data["planned_runs"] == len(manifest.rows)


def test_c02_the_export_counts_set_aside_and_treatment_attempts_per_condition_and_kind(tmp_path):
    """C02: adds per-condition counts of set-aside and treatment attempts for each kind, beside the retry totals."""
    schema = treatment_schema(("endpoint", "knowledge"))
    store = open_store(tmp_path, schema)
    manifest = make_manifest(schema)
    store.sync_manifest(manifest.rows)
    declared, other = keys_under(schema, manifest, "api-none"), keys_under(schema, manifest, "mount-read")
    run_one(store, declared[0], infra("endpoint"))  # treatment
    run_one(store, declared[1], infra("endpoint"))  # treatment
    run_one(store, declared[2], infra("sandbox"), max_attempts=9)  # set aside: not declared
    run_one(store, declared[3], AttemptResult(Outcome("finished"), LATER, 1.0, outage_kind="endpoint"))  # set aside
    run_one(store, other[0], infra("endpoint"), max_attempts=9)  # set aside: this condition declares nothing
    run_one(store, other[0], infra("endpoint"), max_attempts=9)
    data = export_run_state(store)
    by_cell = {(r["condition"], r["kind"]): r for r in data["infrastructure_by_condition"]}
    assert {k: (v["declared_treatment"], v["set_aside"], v["treatment"]) for k, v in by_cell.items()} == {
        ("api-none", "endpoint"): (True, 1, 2),
        ("api-none", "knowledge"): (True, 0, 0),  # declared, and nothing of it happened
        ("api-none", "sandbox"): (False, 1, 0),
        ("mount-read", "endpoint"): (False, 2, 0),
    }
    assert list(data["infrastructure_by_condition"][0]) == [
        "condition",
        "kind",
        "declared_treatment",
        "set_aside",
        "treatment",
    ]
    retries = {}
    for row in data["retries_by_kind"]:
        retries[row["kind"]] = retries.get(row["kind"], 0) + row["attempts"]
    assert retries == {"endpoint": 3, "sandbox": 1}  # the treatment attempts are not retries


def test_c09_the_reader_refuses_an_export_without_the_per_condition_counts(tmp_path):
    """C09: adds that the per-condition counts are a required key, so an export cannot omit them silently."""
    store, _, _ = planned_store(tmp_path)
    path = tmp_path / "export.json"
    write_export(store, path)
    assert read_export(path)["infrastructure_by_condition"] == []
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["infrastructure_by_condition"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ExportError, match=r"missing key.*infrastructure_by_condition"):
        read_export(path)


def test_c02_a_treatment_attempt_is_counted_under_its_own_kind_when_an_outage_is_flagged(tmp_path):
    """C02: adds that the export counts a treatment attempt under its outcome's kind, not under the outage's."""
    schema = treatment_schema(("endpoint",))
    store = open_store(tmp_path, schema)
    manifest = make_manifest(schema)
    store.sync_manifest(manifest.rows)
    declared = keys_under(schema, manifest, "api-none")
    run_one(store, declared[0], infra("endpoint", outage_kind="sandbox"))
    run_one(store, declared[1], AttemptResult(Outcome("finished"), LATER, 1.0, outage_kind="sandbox"))
    data = export_run_state(store)
    cells = {(r["condition"], r["kind"]): (r["set_aside"], r["treatment"]) for r in data["infrastructure_by_condition"]}
    assert cells == {("api-none", "endpoint"): (0, 1), ("api-none", "sandbox"): (1, 0)}
    analysed = {r["run_key"]: r for r in data["analysed_attempts"]}
    assert (analysed[declared[0]]["treatment"], analysed[declared[0]]["infra_kind"]) == (True, "endpoint")
    retries = [(r["kind"], r["state"], r["attempts"]) for r in data["retries_by_kind"]]
    assert retries == [("sandbox", "outage_discarded", 1)]


def test_c09_every_write_of_the_export_uses_a_temporary_file_of_its_own(tmp_path, monkeypatch):
    """C09: adds that two writes never share a temporary file, so a second writer cannot move or damage the first's."""
    store, _, _ = planned_store(tmp_path)
    target = tmp_path / "out" / "export.json"
    sources = []
    real_replace = os.replace

    def spy(source, destination):
        sources.append(os.fspath(source))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", spy)
    write_export(store, target)
    write_export(store, target)
    assert len(set(sources)) == 2
    assert all(
        os.path.dirname(s) == str(target.parent) and os.path.basename(s).startswith(".export.json.") for s in sources
    )
    assert [p.name for p in target.parent.iterdir()] == ["export.json"]


def test_c09_two_writers_of_one_destination_always_leave_a_complete_export_for_a_reader(tmp_path):
    """C09: adds that concurrent writers never expose a half-written or missing file to a reader of the destination."""
    quiet, _, _ = planned_store(tmp_path / "quiet")
    busy, _, manifest = planned_store(tmp_path / "busy")
    for key in keys_of(manifest)[:6]:
        run_one(busy, key, finished())
    target = tmp_path / "export.json"
    write_export(quiet, target)
    failures, stop = [], threading.Event()

    def writer(store):
        try:
            for _ in range(25):
                write_export(store, target)
        except Exception as error:  # the test reports whatever a writer raised
            failures.append(error)

    def reader():
        while not stop.is_set():
            try:
                read_export(target)
            except Exception as error:  # a torn or missing file is the failure being tested for
                failures.append(error)
                return

    threads = [threading.Thread(target=writer, args=(s,)) for s in (quiet, busy)]
    watcher = threading.Thread(target=reader)
    watcher.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    watcher.join()
    assert failures == []
    assert read_export(target)["planned_runs"] == len(manifest.rows)
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


@pytest.mark.parametrize("umask", [0o000, 0o022, 0o077])
def test_c09_the_export_file_is_owner_only_whatever_the_umask(tmp_path, umask):
    """C09: adds that an export, which can carry study data, is written 0600 and no umask widens it."""
    store, _, _ = planned_store(tmp_path)
    target = tmp_path / "export.json"
    previous = os.umask(umask)
    try:
        write_export(store, target)
        write_export(store, target)  # and a rewrite over an existing file keeps the mode
    finally:
        os.umask(previous)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_c02_an_attempt_with_no_kind_is_counted_last_and_does_not_break_the_export(tmp_path):
    """C02: adds that a missing kind, which no backfill could name, sorts after the named kinds instead of raising."""
    store, schema, manifest = planned_store(tmp_path)
    one, two, three = keys_under(schema, manifest, "mount-read")[:3]
    for key, kind in ((one, "sandbox"), (two, "endpoint"), (three, "sandbox")):
        run_one(store, key, AttemptResult(Outcome("finished"), LATER, 1.0, outage_kind=kind))
    store.db.execute("UPDATE attempts SET outage_kind=NULL WHERE run_key=?", (two,))
    data = export_run_state(store)
    assert [(r["kind"], r["attempts"]) for r in data["retries_by_kind"]] == [("sandbox", 2), (None, 1)]
    assert [(r["condition"], r["kind"]) for r in data["infrastructure_by_condition"]] == [
        ("mount-read", "sandbox"),
        ("mount-read", None),
    ]


# --- a partial analysis: what is left out is named, and none of it is counted as scored ---------------------


def partial_store(tmp_path):
    """Two treatment attempts and a scored one under api-none, a set-aside then a scored one under mount-read."""
    schema = treatment_schema(("endpoint",))
    store = open_store(tmp_path, schema)
    manifest = make_manifest(schema)
    store.sync_manifest(manifest.rows)
    declared, other = keys_under(schema, manifest, "api-none"), keys_under(schema, manifest, "mount-read")
    run_one(store, declared[0], infra("endpoint"))  # treatment
    run_one(store, declared[1], infra("endpoint"))  # treatment
    run_one(store, declared[2], finished())
    run_one(store, other[0], infra("endpoint"), max_attempts=9)  # set aside, then run again to done
    run_one(store, other[0], finished())
    return store, declared, other


def test_without_excluded_the_export_has_none_of_the_partial_keys(tmp_path):
    store, _, _ = partial_store(tmp_path)
    data = export_run_state(store)
    assert not {"partial", "running_attempts", "excluded_attempts"} & set(data)
    assert all("excluded" not in row for row in data["infrastructure_by_condition"])


def test_an_export_asked_for_a_partial_analysis_with_nothing_left_out_says_it_is_not_partial(tmp_path):
    store, _, _ = partial_store(tmp_path)
    data = export_run_state(store, excluded={})
    assert (data["partial"], data["running_attempts"], data["excluded_attempts"]) == (False, 0, [])
    assert all(row["excluded"] == 0 for row in data["infrastructure_by_condition"])
    assert len(data["analysed_attempts"]) == 4


def test_a_run_left_out_is_named_with_its_attempt_and_reason_and_is_not_among_the_analysed(tmp_path):
    store, declared, _ = partial_store(tmp_path)
    data = export_run_state(store, excluded={declared[0]: "no replayed record covers its final attempt"})
    assert data["partial"] is True
    assert [(e["run_key"], e["attempt"], e["reason"]) for e in data["excluded_attempts"]] == [
        (declared[0], 1, "no replayed record covers its final attempt")
    ]
    assert data["excluded_attempts"][0]["condition"] == "api-none"
    assert declared[0] not in [e["run_key"] for e in data["analysed_attempts"]]
    assert len(data["analysed_attempts"]) == 3  # four finished runs, one left out
    assert data["planned_runs"] == sum(len(data[n]) for n in RUN_LISTS) + 1


def test_the_attempts_of_a_run_left_out_are_counted_apart_and_not_as_scored_or_set_aside(tmp_path):
    store, declared, other = partial_store(tmp_path)
    full = {(r["condition"], r["kind"]): r for r in export_run_state(store)["infrastructure_by_condition"]}
    assert full[("api-none", "endpoint")]["treatment"] == 2
    assert full[("mount-read", "endpoint")]["set_aside"] == 1
    data = export_run_state(store, excluded={declared[0]: "r", other[0]: "r"})
    cells = {(r["condition"], r["kind"]): r for r in data["infrastructure_by_condition"]}
    assert (cells[("api-none", "endpoint")]["treatment"], cells[("api-none", "endpoint")]["excluded"]) == (1, 1)
    assert cells[("mount-read", "endpoint")]["set_aside"] == 0  # its set-aside attempt belongs to a run left out
    assert cells[("mount-read", "endpoint")]["excluded"] == 1


def test_what_was_spent_and_set_aside_stays_over_every_attempt_whoever_is_left_out(tmp_path):
    store, declared, _ = partial_store(tmp_path)
    everything = export_run_state(store)
    data = export_run_state(store, excluded={declared[0]: "r"})
    assert data["discarded_usage"] == everything["discarded_usage"]
    assert data["retries_by_kind"] == everything["retries_by_kind"]


def test_attempts_still_running_make_the_export_partial_even_with_nothing_left_out(tmp_path):
    store, _, _ = partial_store(tmp_path)
    store.begin_attempt(keys_under(store.schema, make_manifest(store.schema), "download-write")[0], NOW, 1, "")
    data = export_run_state(store, excluded={})
    assert (data["partial"], data["running_attempts"], data["excluded_attempts"]) == (True, 1, [])


def test_a_run_that_is_not_finished_cannot_be_left_out(tmp_path):
    store, _, _ = partial_store(tmp_path)
    pending = keys_under(store.schema, make_manifest(store.schema), "download-write")[0]
    with pytest.raises(ExportError, match="cannot leave out run"):
        export_run_state(store, excluded={pending: "r"})


def test_a_written_partial_export_reads_back_and_its_runs_still_add_up_to_the_plan(tmp_path):
    store, declared, _ = partial_store(tmp_path)
    path = tmp_path / "out" / "export.json"
    write_export_data(export_run_state(store, excluded={declared[0]: "r"}), path)
    back = read_export(path)
    assert back["partial"] is True
    assert len(back["excluded_attempts"]) == 1
