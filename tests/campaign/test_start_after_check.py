"""A start that failed right after a post-run check found the platform's links failing costs its run no attempt.

A platform's post-run check that fails on a provider link notes `prerun.NEXT_START_AT_RISK`; the run's next attempt,
should it fail as infrastructure before the agent's first turn with a kind of the study's or the platform's own start,
is set aside as one an outage overlapped, uncharged (`expcore.campaign.start_after_check`). Seen live in a running
campaign: such a start was charged, and the run came within one failure of being exhausted for a link it never broke.
"""

import pytest
from campaign_support import LATER, make_schema
from scheduler_support import FakeExecutor, attempts_of, build, key_of, settings_for

from expcore.campaign import prerun
from expcore.campaign.attempts import classify
from expcore.campaign.factors import FactorSchema
from expcore.campaign.healthgate import Health
from expcore.campaign.markers import AttemptResult, read_marker, write_marker
from expcore.campaign.start_after_check import EVENT, RiskedStarts, spared
from expcore.errors import register_infra_kind
from expcore.loop.outcomes import Outcome

START = "start_unverified"  # a study's own start, as a kind it declares: not one of the core's
register_infra_kind(START, "the start the run induces on the platform did not verify")

SCHEMA = make_schema()
ONE = FactorSchema(SCHEMA.declared, SCHEMA.order, {"api-none": SCHEMA.conditions["api-none"]})  # a plan of one run
KEY = key_of(ONE, "api-none")
SMALL = {"tasks": ("task-a",), "seeds": (1,)}


class Scripted:
    """A health check answering call by call, pre-run and post-run alike: `pass`, `link` (a failure that notes the next
    start at risk, as the Onedata check does for a provider link), `space` (a failure that notes nothing) or `deferred`
    (a deferral that notes it); a pass once the script is used up."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)

    def __call__(self) -> Health:
        answer = self.answers.pop(0) if self.answers else "pass"
        if answer == "pass":
            return Health(True, "fine")
        if answer in ("link", "deferred"):
            prerun.note(prerun.NEXT_START_AT_RISK, True)
        if answer == "deferred":
            return Health(False, "the federation is held on purpose", deferred=True, charge=False)
        return Health(False, f"a {answer} failed")


def campaign(tmp_path, check: Scripted, *behaviours):
    """One run, its attempts behaving as given in order, under `check`; return its attempts and the rule's events."""
    executor = FakeExecutor({KEY: list(behaviours)})
    settings = settings_for(max_attempts=3)
    with build(tmp_path, schema=ONE, executor=executor, plan=SMALL, health=check, settings=settings) as scheduler:
        scheduler.run()
        rows = attempts_of(scheduler.store, KEY)
        events = [r["detail"] for r in scheduler.store.q("SELECT detail FROM events WHERE kind=?", (EVENT,))]
        kinds = [r["outage_kind"] for r in scheduler.store.q("SELECT outage_kind FROM attempts ORDER BY id")]
    return rows, events, kinds


def test_a_start_failure_right_after_a_link_failure_is_set_aside_uncharged(tmp_path):
    """The post-run check of the first attempt fails on a link; the second attempt's start fails before the agent's
    first turn: it is set aside as an outage overlapped it, uncharged, with one event, and the run goes on."""
    rows, events, kinds = campaign(tmp_path, Scripted("pass", "link"), "finish", ("raise_infra", START), "finish")
    assert rows == [("infra_fail", 1, 1), ("outage_discarded", 0, 2), ("done", 0, 2)]
    assert kinds[1] == "sandbox"  # the campaign's health kind, as for a post-run failure another attempt left
    assert len(events) == 1
    assert START in events[0]


def test_a_start_failure_after_a_post_run_failure_that_is_no_link_is_still_charged(tmp_path):
    """A post-run check that failed on something else noted nothing: the next start's failure is charged as before."""
    rows, events, _ = campaign(tmp_path, Scripted("pass", "space"), "finish", ("raise_infra", START), "finish")
    assert rows == [("infra_fail", 1, 1), ("infra_fail", 1, 2), ("done", 0, 3)]
    assert events == []


def test_a_start_failure_with_no_failed_post_run_check_before_it_is_still_charged(tmp_path):
    """No post-run check failed before it: a start's own failure costs the run an attempt, as it always did."""
    rows, events, _ = campaign(tmp_path, Scripted(), ("raise_infra", START), "finish")
    assert rows == [("infra_fail", 1, 1), ("done", 0, 2)]
    assert events == []


def test_the_rule_never_spares_two_starts_in_a_row(tmp_path):
    """The spared start's own post-run check fails on the link too: it marks nothing, so the start after it is charged
    and a run on a link that keeps failing still reaches its attempt limit."""
    check = Scripted("pass", "link", "pass", "link")
    rows, events, _ = campaign(tmp_path, check, "finish", ("raise_infra", START), ("raise_infra", START), "finish")
    assert rows == [("infra_fail", 1, 1), ("outage_discarded", 0, 2), ("infra_fail", 1, 2), ("done", 0, 3)]
    assert len(events) == 1


@pytest.mark.parametrize(
    "behaviour",
    [("raise_infra", "sandbox"), ("raise_infra", "knowledge"), ("infra_after_turn", START)],
    ids=["the core's sandbox", "the core's tool server", "after the first turn"],
)
def test_a_core_kind_or_a_failure_after_the_first_turn_is_charged_after_a_link_failure(tmp_path, behaviour):
    """Only a study's or a platform's start failing before the agent's first turn is covered: the core's own kinds, and
    a failure after a completed turn, are charged after a link failure as before."""
    rows, events, _ = campaign(tmp_path, Scripted("pass", "link"), "finish", behaviour, "finish")
    assert rows == [("infra_fail", 1, 1), ("infra_fail", 1, 2), ("done", 0, 3)]
    assert events == []


def test_a_deferred_post_run_check_marks_nothing(tmp_path):
    """A deferred post-run check verified nothing and found nothing failing: the next start's failure is charged."""
    rows, events, _ = campaign(tmp_path, Scripted("pass", "deferred"), "finish", ("raise_infra", START), "finish")
    assert rows == [("infra_fail", 1, 1), ("infra_fail", 1, 2), ("done", 0, 3)]
    assert events == []


def test_the_mark_is_used_up_by_the_runs_next_attempt_whatever_it_ends_in():
    """The run's next attempt takes the mark once, so only the start directly after the link failure is covered."""
    risked = RiskedStarts()
    risked.mark(KEY)
    assert risked.take(KEY) is True
    assert risked.take(KEY) is False
    assert risked.take("another run") is False


def start_failure(kind: str = START, turns: int = 0) -> AttemptResult:
    return AttemptResult(
        Outcome("infrastructure", "the start did not verify", infra_kind=kind), LATER, 2.0, model_turns=turns
    )


@pytest.mark.parametrize(
    ("result", "covered"),
    [
        (start_failure(), True),
        (start_failure(turns=1), False),
        (start_failure("sandbox"), False),
        (start_failure("endpoint"), False),
        (start_failure("key_rejected"), False),
        (AttemptResult(Outcome("finished"), LATER, 2.0), False),
    ],
    ids=["a study's start", "after a turn", "the health kind", "the identity kind", "uncharged already", "scored"],
)
def test_only_a_charged_start_failure_before_the_first_turn_is_spared(result, covered):
    """`spared` sets aside only a charged infrastructure failure before the first turn, of a kind that is not the
    core's, the health kind or the identity kind; everything else is left as it is."""
    found = spared(result, "sandbox", "endpoint")
    assert (found is not None) is covered
    if covered:
        assert found.outage_kind == "sandbox"
        assert classify(found).state == "outage_discarded"


def test_a_spared_start_committed_from_its_marker_is_classified_the_same(tmp_path):
    """The decision rides on the result's `outage_kind`, in its marker: a result committed from the marker after a
    restart is set aside uncharged as well, with no new field in the marker."""
    found = spared(start_failure(), "sandbox", "endpoint")
    write_marker(tmp_path, KEY, 7, found)
    back = read_marker(tmp_path, KEY, 7)
    assert back == found
    assert classify(back) == ("outage_discarded", False, False)
