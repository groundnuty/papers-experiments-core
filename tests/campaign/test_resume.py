"""Resume: a campaign never continues into another experiment (C06) and keeps its order (C08)."""

import json
import sqlite3

import pytest
from campaign_support import NOW, finished, make_fingerprint, make_manifest, make_schema, sha, treatment_schema

from expcore.campaign.attempts import PinMismatch, ResumeRefused, SchemaMismatch
from expcore.campaign.factors import FactorSchema
from expcore.campaign.recovery import Recovery
from expcore.campaign.resume import FINGERPRINT_KEY, FingerprintMismatch, open_campaign
from expcore.campaign.store import Store

LIMIT = 3


def start(tmp_path, *, order_seed=7, allow_growth=False, schema=None, plan=None, **inputs):
    """Open the campaign store of a small plan; extra keywords replace one fingerprint section."""
    schema = schema or make_schema()
    manifest = make_manifest(schema, order_seed, **(plan or {}))
    tasks = tuple(dict.fromkeys(row.key.factors["task"] for row in manifest.rows))
    models = tuple(dict.fromkeys(row.key.factors["model"] for row in manifest.rows))
    fingerprint = make_fingerprint(schema, task_ids=tasks, model_keys=models, **inputs)
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
