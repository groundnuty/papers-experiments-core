"""A deferred health check: the platform is held on purpose, by a run, not down, so a run waits and is charged nothing.

A check answers `Health(False, detail, deferred=True)` when it could not look at the platform for a reason that is no
fault: an outage run of the study holds the federation. A deferral is never a pass and never an outage: the pre-run
check holds its run without closing the gate or charging an attempt, and a deferred post-run check leaves the finished
outcome as it stands (C11).
"""

from campaign_support import planned_store
from scheduler_support import FakeExecutor, attempts_of, build, event_kinds, settings_for

from expcore.campaign.healthgate import DEFER, GO, META, Health, HealthGate, parse, read_health

SMALL = {"tasks": ("task-a",), "seeds": (1,)}
DEFERRED = Health(False, "an outage run holds the platform", deferred=True)


class Answers:
    """A health check that answers from a list, then passes; it counts its calls."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def __call__(self):
        self.calls += 1
        return self.answers.pop(0) if self.answers else Health(True, "fine")


class DeferredAfterRuns:
    """A health check that passes before a run and is deferred after it: an outage run took the platform meanwhile."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return DEFERRED if self.calls % 2 == 0 else Health(True, "fine")


def gate_for(tmp_path, check):
    store, _, _ = planned_store(tmp_path)
    clock = iter(f"2026-01-01T00:{minute:02d}:00Z" for minute in range(1, 60))
    gate = HealthGate(store, check, lambda: next(clock))
    gate.load()
    return gate, store


def test_c11_a_deferred_answer_is_read_as_deferred_and_never_as_a_pass():
    """C11: a deferral is kept through the reading, and one that claims to be healthy too is still not a pass."""
    assert read_health(lambda: DEFERRED) == DEFERRED
    assert read_health(lambda: DEFERRED).deferred is True
    claimed = read_health(lambda: Health(True, "fine and held", deferred=True))
    assert (claimed.ok, claimed.deferred) == (False, True)
    assert read_health(lambda: (True, "fine")).deferred is False  # a plain pair is never a deferral


def test_c11_a_deferred_pre_run_check_holds_its_run_without_closing_the_gate_or_charging_it(tmp_path):
    """C11: a deferred pre-run check is no outage: the run is held, the gate stays open and records nothing, and the
    next check that passes lets the run go."""
    gate, store = gate_for(tmp_path, Answers(DEFERRED))
    held = gate.before_run("run-a")
    assert (held.verdict, held.detail) == (DEFER, DEFERRED.detail)
    assert gate.closed is False
    assert parse(store.get_meta(META)) == []
    assert gate.before_run("run-a").verdict == GO


def test_c11_the_gates_own_check_keeps_a_closed_gate_closed_while_it_is_deferred(tmp_path):
    """C11: a deferral never opens a closed gate: only a check that passes does."""
    gate, _ = gate_for(tmp_path, Answers(Health(False, "down"), DEFERRED))
    assert gate.before_run("run-a").verdict != GO  # the failed check closes the gate
    assert gate.poll() is False
    assert gate.closed is True
    assert gate.poll() is True  # the next check passes


def test_c11_a_run_whose_pre_run_check_is_deferred_waits_and_is_charged_nothing(tmp_path):
    """C11: through the scheduler, a deferred pre-run check uses no attempt and closes no gate; the run is tried again
    after the gate's re-check period and runs once the check passes."""
    health = Answers(DEFERRED, DEFERRED)
    settings = settings_for(recheck_s=0.01, health_timeout_s=5)
    with build(tmp_path, executor=FakeExecutor(), settings=settings, health=health, plan=SMALL) as scheduler:
        summary = scheduler.run()
        assert summary.statuses == {"done": 3}
        rows = scheduler.store.q("SELECT run_key FROM runs")
        assert all(attempts_of(scheduler.store, r["run_key"]) == [("done", 0, 1)] for r in rows)
        kinds = event_kinds(scheduler.store)
        assert kinds.count("run_deferred") == 2
        assert "health_gate_closed" not in kinds
        assert parse(scheduler.store.get_meta(META)) == []


def test_c11_a_deferred_post_run_check_leaves_the_finished_outcome_standing(tmp_path):
    """C11: a post-run check deferred because an outage run took the platform after the run ended says nothing about
    the run, so its outcome stands and no attempt is run again."""
    settings = settings_for(recheck_s=0.01, health_timeout_s=5)
    health = DeferredAfterRuns()
    with build(tmp_path, executor=FakeExecutor(), settings=settings, health=health, plan=SMALL) as scheduler:
        summary = scheduler.run()
        assert summary.statuses == {"done": 3}
        rows = scheduler.store.q("SELECT run_key FROM runs")
        assert all(attempts_of(scheduler.store, r["run_key"]) == [("done", 0, 1)] for r in rows)
        assert event_kinds(scheduler.store).count("post_run_check_deferred") == 3
