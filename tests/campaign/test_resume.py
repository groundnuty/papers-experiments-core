"""Resume: a campaign never continues into another experiment (C06) and keeps its order (C08)."""

import json
import sqlite3
from dataclasses import replace

import pytest
from campaign_support import (
    NOW,
    chain_schema,
    finished,
    make_fingerprint,
    make_manifest,
    make_schema,
    sha,
    treatment_schema,
)

from expcore.campaign.attempts import PinMismatch, ResumeRefused, SchemaMismatch
from expcore.campaign.factors import FactorSchema, PlanError
from expcore.campaign.recovery import Recovery
from expcore.campaign.resume import FINGERPRINT_KEY, FingerprintMismatch, RunSetMismatch, open_campaign
from expcore.campaign.store import Store

LIMIT = 3


def start(tmp_path, *, order_seed=7, allow_growth=False, schema=None, plan=None, **inputs):
    """Open the campaign store of a small plan; extra keywords replace one fingerprint section."""
    schema = schema or make_schema()
    manifest = make_manifest(schema, order_seed, **(plan or {}))
    tasks = tuple(dict.fromkeys(row.key.factors["task"] for row in manifest.rows))
    models = tuple(dict.fromkeys(row.key.factors["model"] for row in manifest.rows))
    seeds = tuple(dict.fromkeys(row.key.factors["seed"] for row in manifest.rows))
    fingerprint = make_fingerprint(schema, task_ids=tasks, model_keys=models, seed_ids=seeds, **inputs)
    return open_campaign(
        tmp_path / "campaign.db", schema, manifest, fingerprint, now=NOW, max_attempts=LIMIT, allow_growth=allow_growth
    )


def state_of(store):
    """Everything a refused start must leave alone."""
    return (
        [tuple(r) for r in store.q("SELECT run_key, seq, status FROM runs ORDER BY run_key")],
        [tuple(r) for r in store.q("SELECT key, value FROM meta ORDER BY key")],
        [tuple(r) for r in store.q("SELECT kind, detail FROM events ORDER BY id")],
    )


def test_a_first_start_adds_the_plan_pins_the_order_and_records_the_fingerprint(tmp_path):
    store, recovery = start(tmp_path)
    assert recovery == Recovery(0, 0, 0)
    assert len(store.pending()) == len(make_manifest(make_schema()).rows)
    assert store.get_meta("order_seed") == "7"
    assert store.get_meta("order_rule") == "sha256(order_seed|run_key)"
    assert json.loads(store.get_meta(FINGERPRINT_KEY)) == make_fingerprint(make_schema())


def test_a_resume_with_the_same_experiment_changes_nothing(tmp_path):
    store, _ = start(tmp_path)
    before = state_of(store)
    store.close()
    again, recovery = start(tmp_path)
    assert recovery == Recovery(0, 0, 0)
    assert state_of(again) == before


def test_c08_resuming_with_another_order_seed_is_refused(tmp_path):
    """C08: resuming with another order seed is refused, and the store is left as it was."""
    store, _ = start(tmp_path)
    before = state_of(store)
    store.close()
    with pytest.raises(PinMismatch, match="order_seed is pinned to 7 in this campaign, but is now 8"):
        start(tmp_path, order_seed=8)
    again, _ = start(tmp_path)
    assert state_of(again) == before


CHANGES = {
    "task content": ("tasks", {"task-a": sha("edited"), "task-b": sha("task task-b")}),
    "model settings": ("models", {"m1": sha("edited")}),
    "caps": ("caps", {"attempt_limit": 4, "time_cap_s": 600}),
    "prompt hash": ("prompts", {c: sha("edited") for c in make_schema().conditions}),
    "tool hash": ("tools", {c: sha("edited") for c in make_schema().conditions}),
    "scorer digest": ("scorer_digest", sha("another scorer")),
    "configuration hash": (
        "configs",
        {"study.toml": sha("edited"), "models.toml": sha("models"), "campaign.toml": sha("campaign")},
    ),
}


@pytest.mark.parametrize("label", CHANGES)
def test_c06_changing_any_fingerprinted_input_refuses_the_resume(tmp_path, label):
    """C06: changing any fingerprinted input refuses the resume, naming it, and writes nothing."""
    store, _ = start(tmp_path)
    before = state_of(store)
    store.close()
    section, value = CHANGES[label]
    with pytest.raises(FingerprintMismatch) as caught:
        start(
            tmp_path, **{section: value}, allow_growth=True
        )  # growth allowed: still not a change to what was recorded
    assert any(line.startswith(f"{section}") for line in caught.value.differences)
    assert section in str(caught.value)
    again, _ = start(tmp_path)
    assert state_of(again) == before


def test_c06_a_resume_under_another_platform_tls_mode_is_refused_growth_allowed_or_not(tmp_path):
    """C06: a campaign started with one TLS mode of its platform does not resume under another, nor under another CA
    bundle, nor without the platform's settings; the store is left as it was, and the same mode resumes."""
    verify = {"tls": {"mode": "verify"}}
    store, _ = start(tmp_path, platform=verify)
    before = state_of(store)
    store.close()
    for other in ({"tls": {"mode": "insecure"}}, {"tls": {"mode": "ca_file", "ca_sha256": sha("bundle")}}, None):
        with pytest.raises(FingerprintMismatch) as caught:
            start(tmp_path, platform=other, allow_growth=True)
        assert caught.value.differences
        assert all(line.startswith("platform.tls") for line in caught.value.differences)
    again, _ = start(tmp_path, platform=verify)
    assert state_of(again) == before


ENVIRONMENT = {
    "python": "3.12",
    "distributions": {"papers-experiments-core": "0.1.0", "numlib": "1.4.2", "toy-platform": "2.0.0"},
    "sources": {"papers-experiments-core": {"kind": "vcs", "vcs": "git", "commit": "c" * 40}},
}


@pytest.mark.parametrize(
    ("change", "path"),
    [
        (lambda env: env["distributions"].update({"numlib": "1.5.0"}), "environment.distributions.numlib"),
        (lambda env: env["distributions"].pop("toy-platform"), "environment.distributions.toy-platform"),
        (lambda env: env["distributions"].update({"new-lib": "1.0"}), "environment.distributions.new-lib"),
        (lambda env: env["sources"]["papers-experiments-core"].update({"commit": "d" * 40}), "environment.sources"),
        (lambda env: env.update({"python": "3.13"}), "environment.python"),
    ],
)
def test_c06_a_resume_under_another_environment_is_refused(tmp_path, change, path):
    """C06: the environment is part of the experiment: a distribution at another version, removed, added or from
    another source (the core moved to another commit under the same version), or another Python, refuses the resume,
    even with growth allowed."""
    store, _ = start(tmp_path, environment=ENVIRONMENT)
    store.close()
    changed = json.loads(json.dumps(ENVIRONMENT))
    change(changed)
    with pytest.raises(FingerprintMismatch) as caught:
        start(tmp_path, environment=changed, allow_growth=True)
    assert any(line.startswith(path) for line in caught.value.differences)
    again, _ = start(tmp_path, environment=ENVIRONMENT)
    again.close()


def test_c06_a_condition_that_changed_is_a_different_experiment(tmp_path):
    """C06: the conditions are part of the experiment."""
    schema = make_schema()
    start(tmp_path)[0].close()
    swapped = FactorSchema(
        schema.declared,
        schema.order,
        {
            **schema.conditions,
            "mount-read": {"data_path": "mount", "tools": "write"},
            "download-write": {"data_path": "download", "tools": "read"},
        },
    )
    with pytest.raises(ResumeRefused, match=r"schema\.conditions"):
        start(tmp_path, schema=swapped)


def test_c06_the_factors_of_the_store_cannot_change_under_a_resume(tmp_path):
    """C06: a store made for other factors is another experiment."""
    start(tmp_path)[0].close()
    other = FactorSchema({"depth": ["1"]}, ["task", "depth", "model", "seed"], {"only": {"depth": "1"}})
    with pytest.raises(SchemaMismatch):
        start(tmp_path, schema=other)


def test_c08_a_plan_that_grew_is_refused_unless_growth_is_allowed(tmp_path):
    """C08: whole new tasks are growth, which a resume must ask for."""
    start(tmp_path)[0].close()
    with pytest.raises(FingerprintMismatch, match="allow_growth") as caught:
        start(tmp_path, plan={"tasks": ("task-a", "task-b", "task-c")})
    assert caught.value.differences == ("tasks.task-c: not in the recorded experiment",)


def test_c08_a_plan_that_grew_keeps_the_order_and_status_of_the_runs_it_had(tmp_path):
    """C08: growth adds the new runs, keeps statuses and the relative order of the old, and is logged."""
    store, _ = start(tmp_path)
    old = [r["run_key"] for r in store.q("SELECT run_key FROM runs ORDER BY seq")]
    done = old[0]
    begun = store.begin_attempt(done, NOW, 1, "")
    store.finish_attempt(begun.id, finished(), LIMIT)
    store.close()
    grown, _ = start(tmp_path, plan={"tasks": ("task-a", "task-b", "task-c")}, allow_growth=True)
    now_order = [r["run_key"] for r in grown.q("SELECT run_key FROM runs ORDER BY seq")]
    assert [k for k in now_order if k in set(old)] == old
    assert len(now_order) == len(old) * 3 // 2
    assert grown.q("SELECT status FROM runs WHERE run_key=?", (done,))[0]["status"] == "done"
    assert grown.q("SELECT detail FROM events WHERE kind='plan_grown'")[0]["detail"] == "added: tasks.task-c"
    grown.close()
    start(tmp_path, plan={"tasks": ("task-a", "task-b", "task-c")})[0].close()  # the grown plan is now the recorded one
    with pytest.raises(FingerprintMismatch, match="now absent"):
        start(tmp_path)  # and going back to the smaller one is a removal


def test_c06_growth_never_excuses_a_removal(tmp_path):
    """C06: a resume may add to the plan and never take a recorded task away."""
    start(tmp_path, plan={"tasks": ("task-a", "task-b", "task-c")})[0].close()
    with pytest.raises(FingerprintMismatch, match=r"tasks\.task-c: recorded"):
        start(tmp_path, allow_growth=True)


def test_c04_opening_a_campaign_after_a_kill_settles_the_running_attempt(tmp_path):
    """C04: the open that follows a kill interrupts the running attempt and the run is back in the queue."""
    store, _ = start(tmp_path)
    key = store.pending()[0]["run_key"]
    store.begin_attempt(key, NOW, 1, str(tmp_path / "run"))
    store.close()  # as if killed here
    store, recovery = start(tmp_path)
    assert recovery == Recovery(committed=0, interrupted=1, requeued=0)
    assert key in [r["run_key"] for r in store.pending()]
    assert store.charged_attempts(key) == 0


def test_a_refused_start_closes_the_store_it_opened(tmp_path, monkeypatch):
    start(tmp_path)[0].close()
    opened = []
    original = Store.__init__

    def remember(self, *args, **kwargs):
        original(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(Store, "__init__", remember)
    with pytest.raises(PinMismatch):
        start(tmp_path, order_seed=99)
    (store,) = opened
    with pytest.raises(sqlite3.ProgrammingError):
        store.q("SELECT 1")  # closed


def test_c06_a_treatment_declaration_added_changed_or_dropped_between_starts_is_refused(tmp_path):
    """C06: adds that the infrastructure kinds a condition treats as its treatment are part of the experiment."""
    start(tmp_path, schema=treatment_schema(("endpoint",)))[0].close()
    for variant in (make_schema(), treatment_schema(("endpoint", "sandbox")), treatment_schema(("sandbox",))):
        with pytest.raises(FingerprintMismatch, match=r"schema\.treatment\.api-none") as caught:
            start(tmp_path, schema=variant, allow_growth=True)  # growth never excuses a changed declaration
        assert caught.value.differences[0].startswith("schema.treatment.api-none: recorded ['endpoint']")
    start(tmp_path, schema=treatment_schema(("endpoint",)))[0].close()  # the recorded declaration still resumes


def test_c08_a_new_condition_that_declares_a_treatment_is_one_growth_entry(tmp_path):
    """C08: adds that a new condition brings its treatment declaration with it as part of the same growth."""
    base = make_schema()
    start(tmp_path, schema=base)[0].close()
    grown = FactorSchema(
        base.declared,
        base.order,
        {**base.conditions, "mount-none": {"data_path": "mount", "tools": "none"}},
        treatment={"mount-none": ["endpoint"]},
    )
    with pytest.raises(FingerprintMismatch) as caught:
        start(tmp_path, schema=grown)
    assert "schema.conditions.mount-none: not in the recorded experiment" in caught.value.differences
    assert not [line for line in caught.value.differences if "treatment" in line]  # one entry, not two
    store, _ = start(tmp_path, schema=grown, allow_growth=True)
    assert json.loads(store.get_meta(FINGERPRINT_KEY))["schema"]["treatment"]["mount-none"] == ["endpoint"]


def test_c06_a_resume_with_other_seeds_is_refused_even_when_growth_is_allowed(tmp_path):
    """C06: adds that the seeds are fingerprinted, so seeds [1] then [2] is a changed run set and is refused."""
    start(tmp_path, plan={"seeds": (1,)})[0].close()
    with pytest.raises(FingerprintMismatch) as caught:
        start(tmp_path, plan={"seeds": (2,)}, allow_growth=True)
    assert "seeds.1: recorded True, now absent" in caught.value.differences
    start(tmp_path, plan={"seeds": (1,)})[0].close()  # the recorded seeds still resume


def test_c08_new_seeds_are_growth_that_a_resume_must_ask_for(tmp_path):
    """C08: adds that a seed added to the plan is growth, refused without leave and added with it."""
    start(tmp_path, plan={"seeds": (1,)})[0].close()
    with pytest.raises(FingerprintMismatch) as caught:
        start(tmp_path, plan={"seeds": (1, 2)})
    assert caught.value.differences == ("seeds.2: not in the recorded experiment",)
    store, _ = start(tmp_path, plan={"seeds": (1, 2)}, allow_growth=True)
    assert len(store.run_keys()) == len(make_manifest(make_schema(), 7).rows)


def test_c06_a_manifest_that_is_not_the_plan_of_its_fingerprint_is_refused_before_the_store_is_opened(tmp_path):
    """C06: adds that a start whose manifest and fingerprint disagree about the runs is refused and creates nothing."""
    schema = make_schema()
    manifest = make_manifest(schema, 7, seeds=(1,))
    fingerprint = make_fingerprint(schema, seed_ids=(1, 2))
    with pytest.raises(RunSetMismatch, match="omits 6 that it does"):
        open_campaign(tmp_path / "campaign.db", schema, manifest, fingerprint, now=NOW, max_attempts=LIMIT)
    assert not (tmp_path / "campaign.db").exists()


def test_c06_runs_the_store_holds_are_never_dropped_and_runs_it_lacks_need_leave(tmp_path):
    """C06: adds the run-set check against the store: a removed run is always refused, an added one needs growth."""
    store, _ = start(tmp_path)
    keys = sorted(store.run_keys())
    store.db.execute("INSERT INTO runs(run_key,digest,seq,factors) VALUES('task=old','d0',99,'{}')")
    store.close()
    with pytest.raises(RunSetMismatch, match=r"omits 1 run\(s\) the store holds, the first 'task=old'"):
        start(tmp_path, allow_growth=True)  # leave to grow never excuses a removal
    store = Store(tmp_path / "campaign.db", make_schema())
    store.db.execute("DELETE FROM runs WHERE run_key='task=old'")
    store.db.execute("DELETE FROM runs WHERE run_key=?", (keys[0],))
    store.close()
    with pytest.raises(RunSetMismatch, match=r"adds 1 run\(s\) the store does not hold.*pass allow_growth"):
        start(tmp_path)
    again, _ = start(tmp_path, allow_growth=True)
    assert again.run_keys() == set(keys)


def test_c06_a_refused_growth_leaves_the_stored_fingerprint_so_the_original_resumes(tmp_path):
    """C06: adds that the fingerprint update, the checks and the insertion of runs stand or fall in one transaction."""
    store, _ = start(tmp_path)
    before = state_of(store)
    recorded = store.get_meta(FINGERPRINT_KEY)
    store.close()
    schema = make_schema()
    grown_plan = {"tasks": ("task-a", "task-b", "task-c")}
    manifest = make_manifest(schema, 7, **grown_plan)
    bad = replace(manifest, rows=(*manifest.rows, manifest.rows[0]))  # a duplicate key, past the plan builder
    fingerprint = make_fingerprint(schema, task_ids=grown_plan["tasks"], model_keys=("m1",))
    with pytest.raises(ValueError, match="appears more than once"):
        open_campaign(
            tmp_path / "campaign.db", schema, bad, fingerprint, now=NOW, max_attempts=LIMIT, allow_growth=True
        )
    check = Store(tmp_path / "campaign.db", schema)
    assert check.get_meta(FINGERPRINT_KEY) == recorded
    assert state_of(check) == before  # no run, no event, no pin changed
    check.close()
    again, _ = start(tmp_path)  # the original start still resumes
    assert state_of(again) == before


# --- a condition that runs on a subset of the tasks (resume) ---------------------------------------------------------

PLAN = {"tasks": ("task-a", "task-b", "task-c", "task-d")}


def test_r11_the_store_refuses_a_run_on_a_task_its_condition_does_not_run(tmp_path):
    """R11: adds that a run outside a condition's task list cannot enter the store, even from a hand-made manifest."""
    chain = chain_schema(("task-a", "task-b"))
    store = Store(tmp_path / "campaign.db", chain)
    outside = make_manifest(make_schema(), **PLAN)  # the full cross, which holds download-write on task-c
    with pytest.raises(PlanError, match="is on a task that condition 'download-write' does not run"):
        store.sync_manifest(outside.rows)
    assert store.run_keys() == set()
    inside = make_manifest(chain, **PLAN)
    assert store.sync_manifest(inside.rows) == len(inside.rows)


def test_r11_a_chain_design_plans_and_stores_the_subset_condition_on_its_tasks_only(tmp_path):
    """R11: adds a chain design end to end: the subset condition has runs on its tasks and the others on all."""
    chain = chain_schema(("task-a", "task-b"))
    store, _ = start(tmp_path, schema=chain, plan=PLAN)
    held = [json.loads(r["factors"]) for r in store.q("SELECT factors FROM runs")]
    under = {
        c: sorted({f["task"] for f in held if (f["data_path"], f["tools"]) == lv}) for c, lv in CHAIN_LEVELS.items()
    }
    assert under == {
        "mount-read": ["task-a", "task-b", "task-c", "task-d"],
        "api-none": ["task-a", "task-b", "task-c", "task-d"],
        "download-write": ["task-a", "task-b"],
    }
    assert len(held) == 10 * 2  # ten runs per seed (4 + 4 + 2 tasks, one model), two seeds
    assert len(store.pending(data_path="download")) == 2 * 2


CHAIN_LEVELS = {"mount-read": ("mount", "read"), "api-none": ("api", "none"), "download-write": ("download", "write")}


def test_c06_a_resume_that_changes_a_conditions_task_list_is_refused_and_one_that_adds_tasks_is_growth(tmp_path):
    """C06: adds the task lists to resume: a changed list is refused, an added task needs leave and adds its runs."""
    start(tmp_path, schema=chain_schema(("task-a", "task-b")), plan=PLAN)[0].close()
    for changed in (("task-a",), ("task-a", "task-c")):
        with pytest.raises(FingerprintMismatch, match=r"schema\.condition_tasks\.download-write"):
            start(tmp_path, schema=chain_schema(changed), plan=PLAN, allow_growth=True)
    with pytest.raises(FingerprintMismatch) as caught:
        start(tmp_path, schema=chain_schema(("task-a", "task-b", "task-c")), plan=PLAN)
    assert caught.value.differences == ("schema.condition_tasks.download-write.task-c: not in the recorded experiment",)
    store, _ = start(tmp_path, schema=chain_schema(("task-a", "task-b", "task-c")), plan=PLAN, allow_growth=True)
    assert len(store.run_keys()) == 11 * 2
    assert store.q("SELECT detail FROM events WHERE kind='plan_grown'")[0]["detail"] == (
        "added: schema.condition_tasks.download-write.task-c"
    )


def test_c06_a_manifest_that_runs_the_subset_condition_on_every_task_is_refused_before_the_store_opens(tmp_path):
    """C06: adds that the run-set check is against the declared plan: the full cross is not the chain design."""
    chain = chain_schema(("task-a", "task-b"))
    fingerprint = make_fingerprint(chain, task_ids=PLAN["tasks"])
    with pytest.raises(RunSetMismatch, match=r"lists 4 run\(s\) its fingerprint does not declare"):
        open_campaign(
            tmp_path / "campaign.db",
            chain,
            make_manifest(make_schema(), 7, **PLAN),
            fingerprint,
            now=NOW,
            max_attempts=LIMIT,
        )
    assert not (tmp_path / "campaign.db").exists()
