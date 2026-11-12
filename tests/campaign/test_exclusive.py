"""O11: a condition may run exclusive with no fault, and every attempt says which admission it actually held.

An outage run always sees a federation no other run shares, so its controls should be able to run under the same
isolation: a condition declares `exclusive = true`, and the platform's run admission takes the federation exclusively
for it. Each attempt's admission mode (`shared`, `exclusive`, or `none` for a study without admission) is stored as it
is admitted and goes into the export, so an analysis can check what isolation each compared attempt had.
"""

import contextlib
import json

import pytest
from campaign_support import make_schema
from scheduler_support import FakeExecutor, build, settings_for

from expcore.campaign.export import export_run_state, export_text
from expcore.campaign.factors import FactorSchema, load_factor_schema
from expcore.config.loading import ConfigError

SMALL = {"tasks": ("task-a",), "seeds": (1,)}
STUDY = """
[factors.data_path]
levels = ["mount", "api"]

[identity]
order = ["task", "data_path", "model", "seed"]

[conditions.outage]
data_path = "api"

[conditions.control]
data_path = "mount"
exclusive = {value}
"""


def exclusive_schema(condition="api-none"):
    base = make_schema()
    return FactorSchema(base.declared, base.order, base.conditions, exclusive={condition: True})


def test_o11_a_condition_declares_exclusive_true_and_the_schema_keeps_it(tmp_path):
    """O11: `exclusive = true` is a condition's setting, not a factor level; every other condition is not exclusive."""
    path = tmp_path / "study.toml"
    path.write_text(STUDY.format(value="true"), encoding="utf-8")
    schema = load_factor_schema(path)
    assert schema.exclusive == {"outage": False, "control": True}
    assert schema.conditions["control"] == {"data_path": "mount"}


@pytest.mark.parametrize("value", ['"yes"', "1", "[true]"])
def test_o11_an_exclusive_that_is_not_true_or_false_is_refused_naming_the_file_and_the_key(tmp_path, value):
    """O11: the setting is validated at load, and the refusal names the file and the key."""
    path = tmp_path / "study.toml"
    path.write_text(STUDY.format(value=value), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"study\.toml.*\[conditions\.control\].*exclusive.*true or false"):
        load_factor_schema(path)


def test_o11_exclusive_is_part_of_the_schema_and_leaves_the_digest_of_a_schema_without_it_unchanged():
    """O11: declaring a condition exclusive changes the schema's digest, so a resume cannot drop it unnoticed; a schema
    that declares none has the digest it had before the setting existed."""
    base = make_schema()
    unchanged = FactorSchema(base.declared, base.order, base.conditions, exclusive={"api-none": False})
    assert unchanged.digest == base.digest
    assert exclusive_schema().digest != base.digest
    with pytest.raises(ConfigError, match="exclusive"):
        FactorSchema(base.declared, base.order, base.conditions, exclusive={"no-such-condition": True})


def test_o11_a_campaign_with_an_exclusive_condition_and_no_run_admission_is_refused_naming_the_file_and_key(tmp_path):
    """O11: without a run admission there is nothing to hold exclusively, so the campaign refuses to open."""
    refused = pytest.raises(ConfigError, match=r"\[conditions\.api-none\].*exclusive.*run admission")
    with refused, build(tmp_path, schema=exclusive_schema(), plan=SMALL):
        pass


class Holds:
    """A run admission that holds what the test says it holds, whatever the condition asked for, and notes the asks."""

    def __init__(self, held):
        self.held, self.asked = held, []

    @contextlib.contextmanager
    def __call__(self, treatment_kinds, treatment_targets, cancel, exclusive):
        self.asked.append(exclusive)
        yield self.held


@pytest.mark.parametrize("held", ["shared", "exclusive"])
def test_o11_each_attempt_stores_and_exports_the_admission_it_actually_held(tmp_path, held):
    """O11: the mode is what the platform's admission yielded as it admitted the attempt, never what the condition
    declares; it is in the store's row and in every export entry that names an attempt."""
    admission = Holds(held)
    with build(
        tmp_path, schema=exclusive_schema(), executor=FakeExecutor(), plan=SMALL, admission=admission
    ) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        stored = {row["admission"] for row in scheduler.store.q("SELECT admission FROM attempts")}
        exported = export_run_state(scheduler.store)
        partial = export_run_state(scheduler.store, excluded={exported["analysed_attempts"][0]["run_key"]: "left out"})
    assert sorted(admission.asked) == [False, False, True]  # the condition's ask: one exclusive condition of three
    assert stored == {held}
    assert {entry["admission"] for entry in exported["analysed_attempts"]} == {held}
    assert [entry["admission"] for entry in partial["excluded_attempts"]] == [held]
    assert json.loads(export_text(exported)) == exported


def test_o11_a_study_without_run_admission_stores_and_exports_none(tmp_path):
    """O11: a study whose platform has no run admission holds nothing, and says so: `none`, never a guess."""
    with build(tmp_path, executor=FakeExecutor(), plan=SMALL) as scheduler:
        scheduler.run()
        stored = {row["admission"] for row in scheduler.store.q("SELECT admission FROM attempts")}
        exported = export_run_state(scheduler.store)
    assert stored == {"none"}
    assert {entry["admission"] for entry in exported["analysed_attempts"]} == {"none"}


def test_o11_an_exhausted_run_exports_the_admission_of_each_attempt_it_set_aside(tmp_path):
    """O11: a run that used all its attempts on infrastructure failures lists, beside each reason, the admission its
    attempt held."""
    base = make_schema()
    doomed = base.key("mount-read", "task-a", "m1", 1).text
    executor = FakeExecutor({doomed: [("raise_infra", "sandbox")] * 2})
    settings = settings_for(max_attempts=2)
    with build(tmp_path, executor=executor, settings=settings, plan=SMALL, admission=Holds("shared")) as scheduler:
        scheduler.run()
        exported = export_run_state(scheduler.store)
    [exhausted] = exported["exhausted_runs"]
    assert exhausted["admissions"] == ["shared", "shared"]
    assert len(exhausted["admissions"]) == len(exhausted["reasons"])
