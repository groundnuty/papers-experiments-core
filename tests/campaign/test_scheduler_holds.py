"""A start the platform holds back costs no attempt, before the attempt or at its start, and the campaign holds and
asks again instead of spinning (C33)."""

import itertools
import json
import time

import pytest
from campaign_support import make_schema
from scheduler_support import FakeExecutor, attempts_of, build, key_of, settings_for

from expcore import errors
from expcore.campaign.factors import FactorSchema
from expcore.campaign.healthgate import META as HEALTH_META
from expcore.campaign.healthgate import Health, alarms, fact, parse, read_health

SMALL = {"tasks": ("task-a",), "seeds": (1,)}
SCHEMA = make_schema()
KEY = key_of(SCHEMA, "api-none")
ONE = FactorSchema(SCHEMA.declared, SCHEMA.order, {"api-none": SCHEMA.conditions["api-none"]})  # a plan of one run
ONE_KEY = key_of(ONE, "api-none")


@pytest.fixture
def held_kind(monkeypatch):
    """A platform's hold (`charged=False`), registered for this test alone."""
    monkeypatch.setattr(errors, "_platform_kinds", dict(errors._platform_kinds))
    monkeypatch.setattr(errors, "_platform_untreatable", dict(errors._platform_untreatable))
    monkeypatch.setattr(errors, "_platform_uncharged", dict(errors._platform_uncharged))
    errors.register_infra_kind("start_held_here", "a start the platform held back", treatable=False, charged=False)
    return "start_held_here"


class Answers:
    """A health check that answers from a list, then passes; it notes when each call came."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self):
        self.calls.append(time.monotonic())
        return self.answers.pop(0) if self.answers else Health(True, "fine")


def test_c33_a_start_held_back_at_the_attempt_costs_its_run_no_attempt_however_often(tmp_path, held_kind):
    """C33: the executor's start is held back four times, more than `max_attempts`, by a platform's hold; each
    attempt is set aside uncharged under the same attempt number, and the fifth runs: no run is lost to the limit."""
    executor = FakeExecutor({KEY: [("raise_infra", held_kind)] * 4})
    with build(tmp_path, executor=executor, plan=SMALL, settings=settings_for(max_attempts=2)) as scheduler:
        summary = scheduler.run()
        assert attempts_of(scheduler.store, KEY) == [("infra_fail", 0, 1)] * 4 + [("done", 0, 1)]
        assert scheduler.store.charged_attempts(KEY) == 0
        assert summary.statuses == {"done": 3}


def test_c33_a_charged_failure_at_the_start_still_counts_against_the_limit(tmp_path, held_kind):
    """C33: only a hold is free: a start that fails for another infrastructure reason is charged, as before, so the
    same run is exhausted at `max_attempts`."""
    executor = FakeExecutor({KEY: [("raise_infra", "sandbox")] * 4})
    with build(tmp_path, executor=executor, plan=SMALL, settings=settings_for(max_attempts=2)) as scheduler:
        summary = scheduler.run()
        assert attempts_of(scheduler.store, KEY) == [("infra_fail", 1, 1), ("infra_fail", 1, 2)]
        assert summary.statuses == {"done": 2, "infra_exhausted": 1}


def test_c33_a_pre_run_check_that_charges_no_one_closes_the_gate_and_holds_every_run_the_finder_included(tmp_path):
    """C33: the platform's check fails for a failure an earlier attempt left (`charge=False`): the gate closes, so no
    run starts, but the run that found it begins no attempt either; the dispatcher's own check opens the gate once it
    passes, and every run then starts at its first attempt, none charged."""
    check = Answers(Health(False, "a baseline an earlier attempt left not whole", charge=False))
    settings = settings_for(recheck_s=0.05)
    with build(tmp_path, executor=FakeExecutor(), plan=SMALL, health=check, settings=settings) as scheduler:
        summary = scheduler.run()
        store = scheduler.store
        assert summary.statuses == {"done": 3}
        assert [a for k in store.q("SELECT run_key FROM runs") for a in attempts_of(store, k["run_key"])] == [
            ("done", 0, 1)
        ] * 3
        outage = parse(store.get_meta(HEALTH_META))[0]
        assert (outage["charged"], outage["how"]) == (False, "check_passed")
        closed = store.q("SELECT detail FROM events WHERE kind='health_gate_closed'")[0]["detail"]
        assert "no run is charged, this one included" in closed


def test_c33_a_post_run_check_that_charges_no_one_sets_its_attempt_aside_uncharged(tmp_path, held_kind):
    """C33: the run's post-run check fails for a failure another attempt left (`charge=False`): its attempt, a held
    start or a finished run alike, is set aside as one an outage overlapped and never charged, and the run goes again;
    a post-run failure that charges, the attempt's own, is charged as before."""
    left = Health(False, "a baseline another attempt left not whole", charge=False)
    own = Health(False, "this attempt's own reset failed")
    check = Answers(Health(True, "fine"), left, Health(True, "fine"), left, Health(True, "fine"), own)
    executor = FakeExecutor({ONE_KEY: [("raise_infra", held_kind), "finish", "finish"]})
    settings = settings_for(max_attempts=3)
    with build(tmp_path, schema=ONE, executor=executor, plan=SMALL, health=check, settings=settings) as scheduler:
        scheduler.run()
        assert attempts_of(scheduler.store, ONE_KEY)[:3] == [
            ("outage_discarded", 0, 1),
            ("outage_discarded", 0, 1),
            ("infra_fail", 1, 1),
        ]
        texts = [r["detail"] for r in scheduler.store.q("SELECT detail FROM events WHERE kind='post_run_check_failed'")]
        assert sum("uncharged" in text for text in texts) == 2


def test_c33_the_reading_keeps_whether_a_failure_charges_and_a_flag_that_is_not_a_boolean_is_a_failed_check():
    """C33: `charge` is kept through the reading, a plain failure charges as before, and a reading whose flag is not a
    boolean is no reading at all: a failed check, which charges as any does."""
    assert read_health(lambda: Health(False, "left by an earlier attempt", charge=False)).charge is False
    assert read_health(lambda: Health(False, "down")).charge is True
    odd = read_health(lambda: Health(False, "left", charge="no"))
    assert (odd.ok, odd.charge) == (False, True)


def test_c33_the_status_of_a_gate_closed_by_a_failure_that_charges_no_one_says_so():
    """C33: the alarm of such a closed gate says no attempt was charged, not that the run that found it was."""
    closed = {"start_utc": "2026-01-01T00:00:00Z", "end_utc": None, "checks": 0, "charged": False}
    text = alarms(fact(json.dumps({"outages": [closed]})), 30.0)[0]
    assert "no attempt was charged" in text
    assert "to the run that found it" not in text


def test_c33_a_held_platform_holds_its_runs_and_asks_again_no_sooner_than_recheck_s(tmp_path):
    """C33: while the platform's check is deferred (a live attempt holds what runs need), no attempt begins and the
    run is asked again only after `recheck_s`, never in a loop; once the check passes it runs at its first attempt."""
    held = Health(False, "a live attempt holds the baseline", deferred=True, charge=False)
    check = Answers(held, held, held)
    settings = settings_for(recheck_s=0.05)
    with build(tmp_path, schema=ONE, executor=FakeExecutor(), plan=SMALL, health=check, settings=settings) as scheduler:
        summary = scheduler.run()
        assert attempts_of(scheduler.store, ONE_KEY) == [("done", 0, 1)]
        assert summary.attempts == 1
    pre_run = check.calls[:4]  # three deferred, then the one that passed; the post-run check follows
    waits = [later - earlier for earlier, later in itertools.pairwise(pre_run)]
    assert len(waits) == 3
    assert min(waits) >= 0.05  # each deferred check was followed by a wait of recheck_s, not by another check at once
