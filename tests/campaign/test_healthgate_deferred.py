"""A deferred health check: the platform is held on purpose, by a run, not down, so a run waits and is charged nothing.

A check answers `Health(False, detail, deferred=True)` when it could not look at the platform for a reason that is no
fault: an outage run of the study holds the federation. A deferral is never a pass and never an outage: the pre-run
check holds its run without closing the gate or charging an attempt, and a deferred post-run check leaves the finished
outcome as it stands (C11).
"""

import contextlib
import contextvars

from campaign_support import planned_store
from scheduler_support import FakeExecutor, attempts_of, build, event_kinds, settings_for

from expcore.campaign.healthgate import DEFER, GO, META, Health, HealthGate, parse, read_health
from expcore.errors import is_treatable

SMALL = {"tasks": ("task-a",), "seeds": (1,)}
DEFERRED = Health(False, "an outage run holds the platform", deferred=True)
HOLDING = contextvars.ContextVar("holding", default=False)


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


def test_c11_a_deferred_post_run_check_never_finalizes_the_outcome_it_sets_the_attempt_aside(tmp_path):
    """C11: a deferred post-run check verified nothing, so the finished result is never scored on it: the attempt is
    set aside as the untreatable kind post_check_unverified and the run is tried again."""
    settings = settings_for(recheck_s=0.01, health_timeout_s=5, workers=1)
    health = Answers(Health(True, "fine"), DEFERRED)  # the first run's post-run check is deferred, every other passes
    with build(tmp_path, executor=FakeExecutor(), settings=settings, health=health, plan=SMALL) as scheduler:
        summary = scheduler.run()
        assert summary.statuses == {"done": 3}
        rows = scheduler.store.q("SELECT run_key, state, infra_kind FROM attempts ORDER BY id")
        first = rows[0]
        assert (first["state"], first["infra_kind"]) == ("infra_fail", "post_check_unverified")
        assert [r["state"] for r in rows if r["run_key"] == first["run_key"]] == ["infra_fail", "done"]
        assert event_kinds(scheduler.store).count("post_run_check_deferred") == 1
    assert not is_treatable("post_check_unverified")


class Held:
    """A run admission that notes when it is entered and left, and marks the context it holds."""

    def __init__(self):
        self.events, self.seen = [], []

    @contextlib.contextmanager
    def __call__(self, treatment_kinds, treatment_targets, cancel):
        self.events.append("enter")
        token = HOLDING.set(True)
        try:
            yield
        finally:
            HOLDING.reset(token)
            self.events.append("leave")


def test_c11_the_runs_admission_covers_its_attempt_and_its_post_run_check(tmp_path):
    """C11: the platform's run admission is entered before the attempt and left after its post-run check, which runs
    in the attempt's context, so a platform that holds a resource for the run can check it still held."""
    held = Held()
    calls = []

    def health():
        calls.append(HOLDING.get())
        return Health(True, "fine")

    executor = FakeExecutor()
    settings = settings_for(recheck_s=0.01, health_timeout_s=5, workers=1)
    with build(tmp_path, executor=executor, settings=settings, health=health, plan=SMALL, admission=held) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
    assert held.events == ["enter", "leave"] * 3
    assert calls == [False, True] * 3  # each pre-run check outside the admission, each post-run check inside it
