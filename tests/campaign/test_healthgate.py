"""Health that cannot be read is unhealthy; an outage holds all dispatch for one attempt (C11)."""

import json
import threading

import pytest
from campaign_support import LATER, NOW, planned_store

from expcore.campaign.healthgate import (
    FOUND,
    GO,
    HOLD,
    META,
    Health,
    HealthGate,
    alarms,
    fact,
    parse,
    read_health,
)


class Check:
    """A health check that answers from a list, then keeps the last answer; it counts its calls."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        answer = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def gate_for(tmp_path, check, **options):
    store, _, _ = planned_store(tmp_path)
    clock = iter(f"2026-01-01T00:{minute:02d}:00Z" for minute in range(1, 60))
    gate = HealthGate(store, check, lambda: next(clock), **options)
    gate.load()
    return gate, store


@pytest.mark.parametrize(
    "answer",
    [OSError("cannot reach"), None, "healthy", True, (1, "x"), ("yes", "x"), (True,), [True, "a", "b"]],
)
def test_c11_a_check_that_cannot_be_read_is_unhealthy(answer):
    """C11: a check that raises, or answers something that is not (ok, detail), is a failed check and never a pass."""
    health = read_health(Check(answer))
    assert health.ok is False
    assert health.detail


def test_c11_a_check_that_answers_properly_is_taken_at_its_word():
    """C11: a Health, or a pair with a real boolean, is read as given."""
    assert read_health(Check(Health(True, "fine"))) == Health(True, "fine")
    assert read_health(Check((False, "down"))) == Health(False, "down")


def test_c11_a_failed_check_holds_all_dispatch_for_one_attempt_per_outage(tmp_path):
    """C11: the first failed check closes the gate and charges its run; every other run is held, none is charged."""
    check = Check(Health(False, "platform unreachable"))
    gate, store = gate_for(tmp_path, check)
    first = gate.before_run("run-a")
    assert (first.verdict, first.detail) == (FOUND, "platform unreachable")
    assert gate.closed
    calls = check.calls
    assert gate.before_run("run-b").verdict == HOLD  # held without even a check
    assert check.calls == calls
    [outage] = parse(store.get_meta(META))
    assert (outage["end_utc"], outage["first_detail"]) == (None, "platform unreachable")
    assert [e["kind"] for e in store.q("SELECT kind FROM events")] == ["health_gate_closed"]


@pytest.mark.parametrize("mine", [Health(False, "mine failed too"), Health(True, "mine passed")])
def test_c11_a_run_whose_check_returns_after_another_run_closed_the_gate_is_held_not_charged(tmp_path, mine):
    """C11: a check, failing or passing, that returns after another run closed the gate starts and charges nothing."""
    holder = {}

    def racing():
        holder["gate"]._close("run-other", "the other run's check failed first")
        return mine

    gate, _ = gate_for(tmp_path, racing)
    holder["gate"] = gate
    assert gate.before_run("run-a").verdict == HOLD
    assert len(parse(gate.store.get_meta(META))) == 1  # one outage, found by the other run


def test_c11_unreadable_state_blocks_dispatch_until_a_check_passes(tmp_path):
    """C11: the dispatcher's own check keeps the gate closed while it fails, and the first pass opens it."""
    check = Check(Health(False, "down"), Health(False, "still down"), Health(False, "still down"), Health(True, "up"))
    gate, store = gate_for(tmp_path, check)
    assert gate.before_run("run-a").verdict == FOUND
    assert gate.poll() is False
    assert gate.closed
    assert gate.poll() is False
    assert gate.poll() is True
    assert not gate.closed
    assert gate.before_run("run-b").verdict == GO
    [outage] = parse(store.get_meta(META))
    assert (outage["how"], outage["checks"], outage["last_detail"]) == ("check_passed", 3, "up")
    assert "health_gate_opened" in [e["kind"] for e in store.q("SELECT kind FROM events")]


def test_c11_a_closed_gate_survives_a_restart_and_is_checked_at_once(tmp_path):
    """C11: a gate closed when the last process ended is closed again; the restart is counted and charges nothing."""
    store, _, _ = planned_store(tmp_path)
    clock = lambda: NOW  # noqa: E731
    first = HealthGate(store, Check(Health(False, "down")), clock)
    first.load()
    assert first.before_run("run-a").verdict == FOUND
    second = HealthGate(store, Check(Health(True, "up")), clock)
    second.load()
    assert second.closed
    assert second.check_now
    assert parse(store.get_meta(META))[0]["restarts"] == 1
    assert "health_gate_restored" in [e["kind"] for e in store.q("SELECT kind FROM events")]
    assert second.poll() is True


def test_c11_an_unreadable_gate_state_at_restart_opens_closed(tmp_path):
    """C11: a record that cannot be read reads as closed, until a check passes."""
    store, _, _ = planned_store(tmp_path)
    store.put_meta(META, "{this is not an outage list", NOW)
    gate = HealthGate(store, Check(Health(True, "up")), lambda: LATER)
    gate.load()
    assert gate.closed
    assert gate.before_run("run-a").verdict == HOLD
    assert "health_gate_record_unreadable" in [e["kind"] for e in store.q("SELECT kind FROM events")]
    assert gate.poll() is True
    assert not gate.closed
    assert parse(store.get_meta(META))[0]["first_detail"] == "the stored outage record was unreadable"


def test_c11_a_check_that_does_not_answer_in_time_reads_as_down_and_its_late_answer_is_dropped(tmp_path):
    """C11: a hung check is down; no second check starts while it is out; its late pass never opens the gate."""
    release = threading.Event()

    def hung():
        release.wait(10)
        return Health(True, "late")

    gate, _ = gate_for(tmp_path, Check(Health(False, "down")), timeout_s=0.1)
    gate.before_run("run-a")
    gate.check = hung
    assert gate.poll() is False  # timed out
    assert gate.closed
    assert "did not answer within" in parse(gate.store.get_meta(META))[0]["last_detail"]
    assert gate.poll() is False  # the first is still out
    assert "still not answered" in parse(gate.store.get_meta(META))[0]["last_detail"]
    release.set()


def test_c11_the_gates_own_thread_opens_it_on_the_first_passing_check(tmp_path):
    """C11: while the gate is closed the dispatcher checks every period itself and tells the caller when it opens."""
    gate, _ = gate_for(tmp_path, Check(Health(False, "down"), Health(True, "up")), recheck_s=0.01)
    gate.before_run("run-a")
    opened, stop = threading.Event(), threading.Event()
    gate.start(stop, opened.set)
    try:
        assert opened.wait(10)
    finally:
        stop.set()
        gate.join()
    assert not gate.closed


def test_c11_a_failed_post_run_check_is_the_outages_only_when_the_gate_is_closed_as_it_returns(tmp_path):
    """C11: ordered by the instant the check returns: closed then, it is the outage's and is counted in its record."""
    gate, store = gate_for(tmp_path, Check(Health(True, "up")))
    health, outage = gate.after_run()
    assert health.ok
    assert outage is None
    gate.check = Check(Health(False, "down"))
    health, outage = gate.after_run()
    assert not health.ok
    assert outage is None  # the gate was still open: this check does not close it
    assert gate.before_run("run-a").verdict == FOUND
    _, outage = gate.after_run()
    assert outage is not None
    gate.note_discard(outage, 100, 20)
    gate.note_discard(outage, None, None)
    [record] = parse(store.get_meta(META))
    assert (record["discarded"], record["discarded_usage_unknown"]) == (2, 1)
    assert (record["discarded_input_tokens"], record["discarded_output_tokens"]) == (100, 20)


def test_c11_the_status_names_a_closed_gate_and_an_unreadable_record():
    """C11: the alarm says the gate is closed, and an unreadable record is unknown, never 'no outage'."""
    unreadable = fact("{broken")
    assert unreadable == {"readable": False, "outages": None, "open": None}
    assert "unreadable" in alarms(unreadable, 30)[0]
    closed = fact(json.dumps({"outages": [{"start_utc": NOW, "end_utc": None, "checks": 2, "last_detail": "down"}]}))
    assert "HEALTH GATE CLOSED since" in alarms(closed, 30)[0]
    assert alarms(fact(None), 30) == []
    assert alarms(fact(json.dumps({"outages": [{"start_utc": NOW, "end_utc": LATER}]})), 30) == []


def test_c11_the_limits_are_checked(tmp_path):
    """C11: a period or timeout that cannot work is refused."""
    store, _, _ = planned_store(tmp_path)
    with pytest.raises(ValueError, match="positive"):
        HealthGate(store, Check(Health(True, "up")), lambda: NOW, recheck_s=0)
