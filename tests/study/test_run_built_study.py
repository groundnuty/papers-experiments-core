"""`run_built_study` (C35): a study built in code, a smoke cut or a selection, runs the way one read from disk does.

`run_study` loads a study directory and runs it; a study that builds its own `StudyConfig` has no directory to name,
and calling the pipeline's private campaign function directly skipped the study's kinds scope, so an `InfraError` of a
kind the study declares was refused in the executor's thread. `run_built_study` is the one public path: `run_study`
loads and calls it.
"""

import dataclasses
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from study_support import HEAD, Campaign, clean_environment, finish, make_copy

from expcore import errors
from expcore.campaign.resume import read_fingerprint
from expcore.config.loading import ConfigError
from expcore.provenance.codestamp import write_stamp
from expcore.records.record import load_record
from expcore.study.config import STUDY_FILE, check_study, load_study
from expcore.study.pipeline import run_built_study, run_study

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
TREATMENT = 'infrastructure_is_treatment = ["knowledge"]'
CODE_END = '"plugin.py", "prompt.txt"]\n'  # the end of [study]
CONDITION = "        condition = study.schema.condition_named(factors, request.run_key)\n"
RAISE_IN_A_THREAD = """\
        if condition == "relay-down":  # the kind is constructed, and so looked up, in a thread the executor starts
            await asyncio.to_thread(raise_relay_flap)
"""
THE_FLAP = """

def raise_relay_flap() -> None:
    raise InfraError("relay_flap", "the relay restarted under the run")
"""


@pytest.fixture
def registry(monkeypatch):
    """Start with only the core's infrastructure kinds and leave no platform or study kind behind."""
    monkeypatch.setattr(errors, "_platform_kinds", {})
    monkeypatch.setattr(errors, "_platform_untreatable", {})
    monkeypatch.setattr(errors, "_platform_uncharged", {})
    monkeypatch.setattr(errors, "_platform_targeted", {})
    monkeypatch.setattr(errors, "_study_kinds", {})


def edit(study: Path, name: str, old: str, new: str) -> None:
    path = study / name
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def flapping_copy(base: Path) -> Path:
    """A stamped copy of the example whose relay-down runs fail in a worker thread, with a kind of the study's own."""
    copy = base / "flapping"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    edit(copy, "study.toml", TREATMENT, 'infrastructure_is_treatment = ["relay_flap"]')
    kinds = '\n[study.kinds]\nrelay_flap = { description = "the relay restarted", treatable = true }\n'
    edit(copy, "study.toml", CODE_END, CODE_END + kinds)
    edit(copy, "plugin.py", CONDITION, CONDITION + RAISE_IN_A_THREAD)
    plugin = copy / "plugin.py"
    plugin.write_text(plugin.read_text(encoding="utf-8") + THE_FLAP, encoding="utf-8")
    write_stamp(copy, HEAD, load_study(copy).scope)
    return copy


def attempts(study: Path) -> list[tuple[str, str]]:
    """The (state, infra kind) of every attempt the study's store holds."""
    connection = sqlite3.connect(study / "out" / "state.sqlite")
    try:
        return connection.execute("SELECT state, infra_kind FROM attempts").fetchall()
    finally:
        connection.close()


def test_c35_an_executor_thread_sees_the_kinds_of_a_study_built_in_code(tmp_path, registry):
    """C35: a cut of the study (one model) run through `run_built_study` holds the study's kinds for the whole run, so
    an `InfraError` of the study's own kind, constructed in a thread the executor starts, is recorded as the attempt's
    infrastructure kind and never refused as an unknown one (a harness defect that stops dispatch)."""
    root = flapping_copy(tmp_path)
    loaded = load_study(root)
    smoke = dataclasses.replace(loaded, campaign=dataclasses.replace(loaded.campaign, models=("alpha-stub",)))
    summary = run_built_study(smoke)
    assert "harness_errors" not in {hold.kind for hold in summary.holds.values()}, summary.holds
    assert summary.harness_log is None
    kinds = {kind for _, kind in attempts(root)}
    assert "relay_flap" in kinds
    assert kinds <= {"relay_flap", None}, kinds
    assert errors.infra_kinds() == errors.CORE_KINDS  # and the run leaves none of them behind


def test_c35_the_runs_of_a_cut_are_the_cut_s_and_no_more(tmp_path, registry):
    """C35: the plan a built config runs is its own: the cut's models and no other."""
    root = flapping_copy(tmp_path)
    loaded = load_study(root)
    smoke = dataclasses.replace(loaded, campaign=dataclasses.replace(loaded.campaign, models=("alpha-stub",)))
    run_built_study(smoke)
    connection = sqlite3.connect(root / "out" / "state.sqlite")
    try:
        models = {json.loads(factors)["model"] for (factors,) in connection.execute("SELECT factors FROM runs")}
    finally:
        connection.close()
    assert models == {"alpha-stub"}


def test_c35_run_and_run_built_study_give_the_same_campaign_and_the_same_scores(tmp_path, clean_environment):
    """C35: `run_study` loads and calls `run_built_study`, so the same study through either gives the same summary, the
    same campaign fingerprint and byte-identical scores and analysis; and a campaign one of them started the other
    resumes, finding nothing left to run."""
    by_path, by_config = make_copy(tmp_path, "by-path"), make_copy(tmp_path, "by-config")
    first, second = run_study(by_path), run_built_study(load_study(by_config))
    assert (first.statuses, first.attempts, first.dispatched) == (second.statuses, second.attempts, second.dispatched)
    assert first.statuses.get("done", 0) > 0
    assert read_fingerprint(by_path / "out" / "state.sqlite") == read_fingerprint(by_config / "out" / "state.sqlite")
    finished = []
    for root in (by_path, by_config):
        campaign = Campaign(root)
        finish(clean_environment, campaign)
        assert campaign.codes == {"check": 0, "analyze": 0, "report": 0}
        finished.append(campaign)
    for name in ("scores.json", "analysis.json"):
        assert (by_path / "out" / name).read_bytes() == (by_config / "out" / name).read_bytes(), name
    assert run_built_study(load_study(by_path)).dispatched == 0  # resumed, not refused
    assert run_study(by_config).dispatched == 0


def test_c35_a_smoke_runs_from_a_copy_of_the_study_under_the_code_identity_of_the_real_directory(tmp_path, registry):
    """C35: the documented way to run a cut: the study directory is copied, the cut is made from the copy and run with
    `code_root` the real directory, so the smoke's store and records are in the copy and its records carry the real
    directory's identity; the real directory gets no `out/`, so the whole campaign can still start there."""
    real = make_copy(tmp_path, "real")
    smoke_dir = tmp_path / "smoke"
    shutil.copytree(real, smoke_dir, ignore=shutil.ignore_patterns("out", "__pycache__"))
    write_stamp(real, "b" * 40, load_study(real).scope)  # the real directory's identity is not the copy's
    loaded = load_study(smoke_dir)
    smoke = dataclasses.replace(loaded, campaign=dataclasses.replace(loaded.campaign, models=("alpha-stub",)))
    summary = run_built_study(smoke, code_root=real)
    assert summary.statuses == {"done": len(smoke.manifest.rows)}
    assert not (real / "out").exists()
    records = [
        p for p in (smoke_dir / "out" / "runs").rglob("*.json") if re.fullmatch(r"[0-9a-f]{64}\.\d+\.json", p.name)
    ]
    assert len(records) == len(smoke.manifest.rows)
    assert {load_record(p).code["commit"] for p in records} == {"b" * 40}


def test_c35_a_valid_built_config_passes_the_check_a_loaded_one_has_had(tmp_path):
    """C35: `check_study` accepts what `load_study` returned, so the entry refuses nothing the loader let through."""
    check_study(load_study(make_copy(tmp_path, "valid")))


def built(tmp_path: Path, **changes):
    """A loaded copy of the example with parts of it replaced: what a study that builds its config might hand over."""
    loaded = load_study(make_copy(tmp_path, "built"))
    return dataclasses.replace(loaded, **changes), loaded


def test_c35_a_cut_that_names_a_model_the_table_lacks_is_refused_before_anything_is_written(tmp_path):
    """C35: the loader refuses a campaign whose models are not all in models.toml, naming both files; so does the
    entry, before the store, the leases or the code identity are touched."""
    loaded = load_study(make_copy(tmp_path, "unknown-model"))
    cut = dataclasses.replace(
        loaded, campaign=dataclasses.replace(loaded.campaign, models=("alpha-stub", "gamma-stub"))
    )
    with pytest.raises(ConfigError, match=r"key 'models' names 'gamma-stub'.*models.toml does not define"):
        run_built_study(cut)
    assert not (loaded.root / "out").exists()


def test_c35_a_cut_of_tasks_that_leaves_a_condition_naming_a_task_it_dropped_is_refused(tmp_path):
    """C35: the extended condition runs on the lake, river and hill; a cut that drops the hill leaves it naming a task
    the plan lacks, which the plan's expansion refuses, naming the condition and the task, before any run starts."""
    loaded = load_study(make_copy(tmp_path, "cut-tasks"))
    tasks = {name: task for name, task in loaded.tasks.items() if name != "hill"}
    with pytest.raises(ConfigError, match=r"\[conditions\.extended\]: key 'tasks' names unknown task 'hill'"):
        run_built_study(dataclasses.replace(loaded, tasks=tasks))
    assert not (loaded.root / "out").exists()


def test_c35_a_task_without_the_attribute_a_contrast_clusters_by_is_refused(tmp_path):
    """C35: a contrast clusters by an attribute every task has; a task built without it would silently leave the
    contrast, and the loader refuses that, so the entry does too."""
    loaded = load_study(make_copy(tmp_path, "no-attribute"))
    bare = dataclasses.replace(loaded.tasks["lake"], attributes={})
    with pytest.raises(ConfigError, match=r"clusters by 'region', which task 'lake' of tiny-study does not have"):
        run_built_study(dataclasses.replace(loaded, tasks={**loaded.tasks, "lake": bare}))
    assert not (loaded.root / "out").exists()


def test_c35_a_scheduler_kind_the_vocabulary_lacks_is_refused_naming_campaign_toml_and_the_key(tmp_path):
    """C35: the kinds the scheduler charges its own failures to must be known kinds."""
    loaded = load_study(make_copy(tmp_path, "scheduler-kind"))
    scheduler = dataclasses.replace(loaded.campaign.scheduler, health_kind="wifi")
    cut = dataclasses.replace(loaded, campaign=dataclasses.replace(loaded.campaign, scheduler=scheduler))
    with pytest.raises(
        ConfigError, match=r"\[scheduler\]: key 'health_kind' names a kind that is not an infrastructure"
    ):
        run_built_study(cut)
    assert not (loaded.root / "out").exists()


@pytest.mark.parametrize("kind", ["endpoint", "Bad-Kind"])
def test_c35_a_kind_the_study_may_not_declare_is_refused_naming_the_file_and_the_kind(tmp_path, registry, kind):
    """C35: a core kind, or a name no kind may have, declared in the built config's `kinds` is refused as it is in
    `[study.kinds]`, naming the key, instead of surfacing as a bare ValueError from the scope."""
    loaded = load_study(make_copy(tmp_path, "bad-kind"))
    declared = {kind: errors.StudyKind("the relay restarted", True)}
    with pytest.raises(ConfigError, match=rf"{STUDY_FILE}: \[study\.kinds\]: key '{kind}' cannot be declared"):
        run_built_study(dataclasses.replace(loaded, kinds=declared))
    assert not (loaded.root / "out").exists()
    assert errors.infra_kinds() == errors.CORE_KINDS


def test_c35_a_schema_whose_treatment_names_a_kind_not_in_scope_is_refused(tmp_path, registry):
    """C35: the schema is checked again against the vocabulary the study's own kinds make: a condition's treatment
    that names a kind neither the core, a platform nor the config's `kinds` holds is refused, as `load_study` refuses
    it, naming the condition."""
    loaded = load_study(make_copy(tmp_path, "unknown-treatment"))
    loaded.schema.treatment_kinds["relay-down"] = ("relay_flap",)
    with pytest.raises(
        ConfigError, match=r"\[conditions\.relay-down\]: key 'infrastructure_is_treatment' names unknown"
    ):
        run_built_study(loaded)
    assert not (loaded.root / "out").exists()


def test_c35_a_schema_that_lost_a_treatment_target_is_refused(tmp_path, registry):
    """C35: a kind that is a fault on one target is declared with its target (C34). A schema edited after it was built,
    its target dropped, would score any target's failure as the manipulation; the entry builds the schema again from
    its parts and the constructor refuses the plain name, naming the condition."""
    errors.register_infra_kind("provider_down", "a provider that is down", requires_target=True)
    loaded = load_study(make_copy(tmp_path, "lost-target"))
    schema = loaded.schema
    schema.treatment_kinds["relay-down"] = ("provider_down",)  # a target-less declaration of a targeted kind
    schema.treatment_targets["relay-down"] = {}
    with pytest.raises(ConfigError, match=r"\[conditions\.relay-down\]: key 'infrastructure_is_treatment'.*no target"):
        run_built_study(loaded)
    assert not (loaded.root / "out").exists()


def test_c35_a_config_that_is_not_a_study_config_is_refused_naming_what_arrived(tmp_path):
    """C35: the entry takes a `StudyConfig`; a path, which `run_study` takes, is named in the refusal."""
    root = make_copy(tmp_path, "a-path")
    with pytest.raises(
        TypeError, match=r"run_built_study takes a StudyConfig, not PosixPath; run_study takes the path"
    ):
        run_built_study(root)  # type: ignore[arg-type]
