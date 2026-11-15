"""C11: a wait or a failure that only an operator can end reads as one in the status and the run log.

A platform leads a detail with its reason when only a person can end what it says (`OPERATOR_ACTION`): the fault gate
after a failed heal (O35) says `a failed heal: operator action needed (stuck pairs: ...)`. The core keeps the first
200 characters of a detail and the status shows 160, so the reason leads, and the status and the hold the run says are
worded for it: a deferral past `max_defer_s` is a hold, and no outage run, nor any repair that will end by itself,
holds the platform. A detail that does not lead with one reads exactly as it did.
"""

import datetime as dt
import json

from campaign_support import planned_store
from scheduler_support import FakeExecutor, build, settings_for

from expcore.campaign.deferral import DEFERRAL_META, OPERATOR_ACTION, deferral_alarms, operator_reason
from expcore.campaign.healthgate import FOUND, META, Health, HealthGate, alarms, fact, parse

SMALL = {"tasks": ("task-a",), "seeds": (1,)}
REASON = f"a failed heal: {OPERATOR_ACTION} (stuck pairs: providers[2] reading from providers[0])"
LEADING = f"{REASON}; no run is admitted until every provider link transfers: stuck: providers[0]->providers[2]"
NOW = "2026-01-01T00:00:00Z"
LATER = "2026-01-01T00:05:00Z"


def test_c11_a_detail_that_leads_with_an_operator_reason_is_told_by_its_first_clause():
    """C11: the reason is the clause a detail leads with, up to the first semicolon; a detail that mentions it later,
    names none or is not text is none, so a platform's other words never read as a hold for an operator."""
    assert operator_reason(LEADING) == REASON
    assert operator_reason(REASON) == REASON  # a detail that is the reason alone, or one cut at 200 characters
    assert operator_reason(f"an outage run holds the federation; {REASON}") is None
    assert operator_reason("an outage run of providers[1] holds the federation: the check waits for it") is None
    assert operator_reason(None) is None
    assert operator_reason(7) is None


def test_c11_the_status_says_a_deferral_that_needs_an_operator_is_no_run_holding_the_platform():
    """C11: the deferral's alarm leads with the reason and does not say a run holds the platform on purpose, and a
    deferral without one is worded exactly as before."""
    record = {"since_utc": NOW, "last_utc": LATER, "checks": 3, "last_detail": LEADING[:200]}
    (alarm,) = deferral_alarms(record)
    assert REASON in alarm
    assert "a run holds" not in alarm
    assert "past max_defer_s the gate fails and blocks dispatch" in alarm
    plain = {**record, "last_detail": "an outage run of providers[1] holds the federation: the check waits for it"}
    assert deferral_alarms(plain) == [
        f"HEALTH CHECK DEFERRED since {NOW}: runs wait for the platform, which a run holds on purpose "
        f"(3 deferred check(s), last {LATER}: an outage run of providers[1] holds the federation: the check waits "
        "for it); past max_defer_s the gate fails and blocks dispatch"
    ]


def test_c11_the_status_says_a_closed_gate_that_needs_an_operator_does_not_resume_by_itself():
    """C11: the closed gate's alarm leads with the reason, and says a check passes only once an operator has mended
    what the failed heal left, where a plain closure says the dispatcher resumes by itself on the first pass."""
    outage = {"start_utc": NOW, "end_utc": None, "checks": 4, "last_check_utc": LATER, "last_detail": LEADING[:200]}
    (alarm,) = alarms(fact(json.dumps({"outages": [outage]})), 30)
    assert REASON in alarm
    assert "resumes by itself" not in alarm
    assert "operator" in alarm
    (plain,) = alarms(fact(json.dumps({"outages": [{**outage, "last_detail": "platform unreachable"}]})), 30)
    assert "resumes by itself on the first pass" in plain
    assert "operator" not in plain


def test_c11_a_deferral_past_the_bound_that_needs_an_operator_is_a_hold_the_run_says_so(tmp_path):
    """C11: a check deferred past `max_defer_s` with the reason leading closes the gate blocking, charging no run, and
    the hold the run says, the outage the status reads and the log's event all carry the reason; the line still says
    the hold may not end by itself."""
    moments = iter(dt.datetime(2026, 1, 1, tzinfo=dt.UTC) + dt.timedelta(seconds=40 * n) for n in range(10_000))
    settings = settings_for(recheck_s=0.01, health_timeout_s=5, max_defer_s=100, workers=1)
    lines = []
    operator_wait = Health(False, LEADING, deferred=True, charge=False)
    with build(
        tmp_path,
        executor=FakeExecutor(),
        settings=settings,
        health=lambda: operator_wait,
        plan=SMALL,
        clock=lambda: next(moments),
        on_hold=lines.append,
    ) as scheduler:
        scheduler.run()
        [outage] = parse(scheduler.store.get_meta(META))
        assert (outage["blocking"], outage["charged"]) == (True, False)
        assert outage["first_detail"].startswith(f"{REASON}; ")
        assert REASON in "".join(
            alarms(fact(scheduler.store.get_meta(META), scheduler.store.get_meta(DEFERRAL_META)), 30)
        )
        events = scheduler.store.q("SELECT kind, detail FROM events WHERE kind IN ('hold', 'health_gate_closed')")
        assert all(REASON in row["detail"] for row in events), [row["detail"] for row in events]
    said = [line for line in lines if line.startswith("hold on all (health_gate)")]
    assert said == [
        "hold on all (health_gate): the health gate is closed: the platform's check stayed deferred past max_defer_s: "
        f"{REASON}; it may not end by itself: dispatch resumes only when a health check passes (checked every 0.01 "
        "s), so someone should look at what holds the platform"
    ]


def test_c11_a_gate_closed_by_a_failure_that_needs_an_operator_names_it_in_the_hold(tmp_path):
    """C11: a failure with the reason leading closes the gate as any failure does, and the hold the run says carries
    it, once, while the dispatcher re-checks; a plain failure's line is what it was."""
    answers = [Health(False, LEADING)] * 3

    def check():
        return answers.pop(0) if answers else Health(True, "fine")

    lines = []
    settings = settings_for(recheck_s=0.01, health_timeout_s=5)
    with build(tmp_path, settings=settings, health=check, plan=SMALL, on_hold=lines.append) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
    said = [line for line in lines if line.startswith("hold on all (health_gate)")]
    assert said == [
        f"hold on all (health_gate): the health gate is closed: no run starts until a check passes: {REASON}; dispatch "
        "resumes when a health check passes (checked every 0.01 s)"
    ]


def test_c11_an_outage_keeps_the_reason_it_was_last_told_after_a_check_passes(tmp_path):
    """C11: a gate closed by an ordinary failure whose recheck then says an operator is needed (a heal the dispatcher's
    own check began failed), and that a repair then opens, has the reason in its record: the record's detail fields
    hold the first failure's words and the pass's, so the reason is kept apart, and an outage no such answer touched
    has no such field."""
    store, _, _ = planned_store(tmp_path)
    answers = [Health(False, "the platform is unreachable"), Health(False, LEADING), Health(True, "fine")]
    gate = HealthGate(store, lambda: answers.pop(0), lambda: NOW, recheck_s=0.01, timeout_s=5)
    gate.load()
    assert gate.before_run("run-a").verdict == FOUND
    assert gate.poll() is False
    assert gate.poll() is True
    (outage,) = parse(store.get_meta(META))
    assert outage["operator_reason"] == REASON
    assert operator_reason(outage["first_detail"]) is None
    assert operator_reason(outage["last_detail"]) is None
    plain = HealthGate(store, lambda: Health(False, "the platform is unreachable"), lambda: LATER, timeout_s=5)
    plain.load()
    assert plain.before_run("run-b").verdict == FOUND
    assert "operator_reason" not in parse(store.get_meta(META))[-1]
