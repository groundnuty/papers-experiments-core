"""The export: exhausted runs reach the analysis (C09), every infrastructure kind keeps its name (C02), C01."""

import json
import os

import pytest
from campaign_support import LATER, NOW, defect, finished, infra, planned_store, run_one

from expcore import errors
from expcore.campaign.export import (
    FORMAT,
    REQUIRED_KEYS,
    RUN_LISTS,
    ExportError,
    export_run_state,
    read_export,
    write_export,
)
from expcore.campaign.markers import AttemptResult
from expcore.campaign.recovery import recover
from expcore.errors import infra_kinds, register_infra_kind
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


def test_c02_every_infrastructure_kind_keeps_its_own_name_in_the_export(tmp_path):
    """C02: every harness kind appears under its own name, the core's and a platform's alike."""
    register_infra_kind("tool_outage", "a platform's own kind")
    store, _, manifest = planned_store(tmp_path, tasks=("task-a", "task-b", "task-c"))
    kinds = infra_kinds()
    assert len(kinds) == 6
    for index, kind in enumerate(kinds):
        for _ in range(index + 1):  # a different count for each kind, so a merge could not hide
            run_one(store, keys_of(manifest)[index], infra(kind), max_attempts=10)
    data = export_run_state(store)
    assert data["kinds"] == list(kinds)
    per_kind = {}
    for row in data["retries_by_kind"]:
        per_kind[row["kind"]] = per_kind.get(row["kind"], 0) + row["attempts"]
    assert per_kind == {kind: index + 1 for index, kind in enumerate(kinds)}
    assert {"knowledge", "unattributed_kill", "tool_outage"} <= set(per_kind)  # none folded into "sandbox"


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
    assert set(data["analysed_attempts"][0]) == identity | {"attempt", "run_dir"}
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

    monkeypatch.setattr(json, "dump", broken)
    with pytest.raises(OSError, match="disk full"):
        write_export(store, target)
    assert target.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []
