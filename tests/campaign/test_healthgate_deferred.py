"""A deferred health check: the platform is held on purpose, by a run, not down, so a run waits and is charged nothing.

A check answers `Health(False, detail, deferred=True)` when it could not look at the platform for a reason that is no
fault: an outage run of the study holds the federation. A deferral is never a pass and never an outage: the pre-run
check holds its run without closing the gate or charging an attempt, and a deferred post-run check leaves the finished
outcome as it stands (C11).
"""

import collections
import contextlib
import contextvars
import datetime as dt
import threading

from campaign_support import planned_store, treatment_schema
from scheduler_support import FakeExecutor, attempts_of, build, event_kinds, key_of, settings_for

from expcore.campaign.deferral import DEFERRAL_META
from expcore.campaign.healthgate import DEFER, FOUND, GO, META, Health, HealthGate, alarms, fact, parse, read_health
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
    def __call__(self, treatment_kinds, treatment_targets, cancel, exclusive):
        self.events.append("enter")
        token = HOLDING.set(True)
        try:
            yield "shared"
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


WEDGED = Health(
    False, "an outage run of providers[1] since 2026-01-01T00:00:00+00:00 holds the federation", deferred=True
)


def clock_from(minutes):
    """A gate clock that reads the given minutes past midnight, one per reading, then stays on the last."""
    stamps = [f"2026-01-01T{m // 60:02d}:{m % 60:02d}:00Z" for m in minutes]
    return lambda: stamps.pop(0) if len(stamps) > 1 else stamps[0]


def test_c11_a_deferral_is_kept_in_the_store_and_a_restart_keeps_its_age(tmp_path):
    """C11: the start of the current deferral is in the store's meta and in the status; a gate opened again on the
    same store counts its age from that start, and once the age passes max_defer_s the gate fails, naming the holder's
    purpose and the target, as an outage that blocks dispatch until someone looks."""
    store, _, _ = planned_store(tmp_path)
    first = HealthGate(store, Answers(WEDGED), clock_from([1]), max_defer_s=3000)
    first.load()
    assert first.before_run("run-a").verdict == DEFER
    status = fact(store.get_meta(META), store.get_meta(DEFERRAL_META))
    assert status["deferral"]["since_utc"] == "2026-01-01T00:01:00Z"
    assert any("DEFERRED since 2026-01-01T00:01:00Z" in alarm for alarm in alarms(status, 30))
    again = HealthGate(store, Answers(WEDGED, WEDGED), clock_from([40, 40, 52]), max_defer_s=3000)  # a restart
    again.load()
    assert again.before_run("run-a").verdict == DEFER  # 39 minutes deferred: under the bound
    assert again.closed is False
    found = again.before_run("run-a")  # 51 minutes: past it
    assert found.verdict == FOUND
    assert "max_defer_s" in found.detail
    assert "an outage run of providers[1]" in found.detail
    assert (again.closed, again.blocking) == (True, True)


def test_c11_a_passing_check_ends_the_deferral(tmp_path):
    """C11: a check that passes ends the current deferral, so the next one counts from its own start."""
    store, _, _ = planned_store(tmp_path)
    gate = HealthGate(store, Answers(WEDGED, Health(True, "fine"), WEDGED), clock_from([1, 2, 59]), max_defer_s=3000)
    gate.load()
    assert gate.before_run("run-a").verdict == DEFER
    assert gate.before_run("run-a").verdict == GO
    assert store.get_meta(DEFERRAL_META) in (None, "null")
    assert gate.before_run("run-a").verdict == DEFER  # 59 minutes after the first, but a new deferral


def test_c11_a_campaign_whose_check_stays_deferred_past_the_bound_stops_dispatch_as_blocked(tmp_path):
    """C11: a wedged holder keeps every check deferred; past max_defer_s the gate fails, one attempt is charged to the
    run that found it, and the hold is not temporary, so a campaign that exits when blocked stops."""
    moments = iter(dt.datetime(2026, 1, 1, tzinfo=dt.UTC) + dt.timedelta(seconds=40 * n) for n in range(10_000))
    settings = settings_for(recheck_s=0.01, health_timeout_s=5, max_defer_s=100, workers=1)
    with build(
        tmp_path,
        executor=FakeExecutor(),
        settings=settings,
        health=lambda: WEDGED,
        plan=SMALL,
        clock=lambda: next(moments),
    ) as scheduler:
        summary = scheduler.run()
        kinds = event_kinds(scheduler.store)
        assert kinds.count("health_gate_closed") == 1
        assert summary.statuses.get("done", 0) == 0
        charged = scheduler.store.q("SELECT COUNT(*) FROM attempts WHERE charged=1")[0][0]
        assert charged == 1
        [outage] = parse(scheduler.store.get_meta(META))
        assert outage["blocking"] is True
        assert "providers[1]" in outage["first_detail"]


def test_c11_a_blocking_gate_is_said_once_as_a_hold_that_may_not_end_by_itself(tmp_path):
    """C11: the line `run` says for a gate closed past max_defer_s tells it from a gate that reopens by itself: what
    would end it, and that someone should look, never only the recheck interval."""
    moments = iter(dt.datetime(2026, 1, 1, tzinfo=dt.UTC) + dt.timedelta(seconds=40 * n) for n in range(10_000))
    settings = settings_for(recheck_s=0.01, health_timeout_s=5, max_defer_s=100, workers=1)
    lines = []
    with build(
        tmp_path,
        executor=FakeExecutor(),
        settings=settings,
        health=lambda: WEDGED,
        plan=SMALL,
        clock=lambda: next(moments),
        on_hold=lines.append,
    ) as scheduler:
        scheduler.run()
    said = [line for line in lines if line.startswith("hold on all (health_gate)")]
    assert said == [
        "hold on all (health_gate): the health gate is closed: the platform's check stayed deferred past max_defer_s; "
        "it may not end by itself: dispatch resumes only when a health check passes (checked every 0.01 s), so "
        "someone should look at what holds the platform"
    ]


def test_c11_a_closed_gate_whose_own_checks_stay_deferred_past_the_bound_becomes_blocking(tmp_path):
    """C11: a gate closed by an ordinary failure is re-checked by the dispatcher alone; when those checks stay
    deferred past max_defer_s, because a holder wedged the platform meanwhile, the outage becomes blocking too, so the
    campaign is not left waiting for ever on a temporary hold."""
    store, _, _ = planned_store(tmp_path)
    check = Answers(Health(False, "down"), WEDGED, WEDGED)
    gate = HealthGate(store, check, clock_from([1, 2, 3, 10, 100]), max_defer_s=3000)
    gate.load()
    assert gate.before_run("run-a").verdict == FOUND  # an ordinary failure: a temporary hold
    assert (gate.closed, gate.blocking) == (True, False)
    assert gate.poll() is False  # deferred, under the bound
    assert gate.blocking is False
    assert gate.poll() is False  # deferred past it
    assert gate.blocking is True
    [outage] = parse(store.get_meta(META))
    assert outage["blocking"] is True
    assert "providers[1]" in outage["last_detail"]


def test_c11_a_treatment_failure_whose_post_run_check_is_deferred_is_never_scored(tmp_path):
    """C11: a treatment-kind failure is a scored final attempt only once a post-run check has verified the platform; a
    deferred one verified nothing, so the attempt is set aside as post_check_unverified, never done, and run again."""
    schema = treatment_schema(("endpoint",), "api-none")
    treated = key_of(schema, "api-none")
    executor = FakeExecutor()
    after_treated = threading.Event()

    async def fail_as_treatment(request):
        after_treated.set()  # the post-run check that follows this attempt is deferred
        return await executor._run(request, ("infra_after_turn", "endpoint"))

    executor.behaviours[treated] = collections.deque([fail_as_treatment])

    def health():
        if after_treated.is_set():
            after_treated.clear()
            return DEFERRED
        return Health(True, "fine")

    settings = settings_for(recheck_s=0.01, health_timeout_s=5, workers=1, identity_kind="knowledge")
    with build(tmp_path, schema=schema, executor=executor, settings=settings, health=health, plan=SMALL) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        rows = scheduler.store.q(
            "SELECT state, treatment, infra_kind FROM attempts WHERE run_key=? ORDER BY id", (treated,)
        )
    first = rows[0]
    assert (first["state"], first["treatment"], first["infra_kind"]) == ("infra_fail", 0, "post_check_unverified")
    assert [r["state"] for r in rows] == ["infra_fail", "done"]


def test_c11_a_passing_post_run_check_ends_the_deferral_so_a_later_one_counts_from_its_own_start(tmp_path):
    """C11: a deferral, then a post-run check that passes, then a deferral again: the second counts its age from its
    own start, so it is not past the bound at once."""
    store, _, _ = planned_store(tmp_path)
    answers = Answers(WEDGED, Health(True, "fine"), WEDGED)
    gate = HealthGate(store, answers, clock_from([0, 1, 30, 59]), max_defer_s=3000)  # the first: load
    gate.load()
    assert gate.before_run("run-a").verdict == DEFER  # since 00:01
    health, outage = gate.after_run()  # 00:30: the platform passes
    assert (health.ok, outage) == (True, None)
    assert store.get_meta(DEFERRAL_META) in (None, "null")
    assert gate.before_run("run-a").verdict == DEFER  # 00:59: a new deferral, not 58 minutes old
