"""O35: what a run's pre-run check notes, the same try's run admission reads.

A run's pre-run check and its admission can be far apart (a wait for a lease, then for the platform's admission), so a
platform whose admission must know what the check saw notes it in the try's note (`expcore.campaign.prerun`). The
check runs in a thread of its own, in a copy of the worker's context, which refers to the same note.
"""

import contextlib
import itertools

from scheduler_support import FakeExecutor, build, settings_for

from expcore.campaign import prerun
from expcore.campaign.healthgate import Health

TWO_RUNS = {"tasks": ("task-a",), "seeds": (1, 2)}


class NotingOnce:
    """A health check that notes only at its first call (the first try's pre-run check), in the note of that try."""

    def __init__(self):
        self.calls = itertools.count(1)

    def __call__(self):
        if next(self.calls) == 1:
            prerun.note("check", "first")
        return Health(True, "ok")


def reading(seen):
    """A run admission that keeps the note it is entered with (a copy, as it stands at admission)."""

    @contextlib.contextmanager
    def admit(kinds, targets, cancel, exclusive):
        note = prerun.current()
        seen.append(None if note is None else dict(note))
        yield "shared"

    return admit


def test_o35_a_runs_admission_reads_what_its_own_pre_run_check_noted(tmp_path):
    """O35: the first try's admission reads what its pre-run check noted; every later try has a fresh note, so its
    admission never reads another try's."""
    seen = []
    settings = settings_for(workers=1)
    with build(
        tmp_path,
        executor=FakeExecutor(),
        settings=settings,
        plan=TWO_RUNS,
        health=NotingOnce(),
        admission=reading(seen),
    ) as scheduler:
        scheduler.run()
    assert len(seen) > 1
    assert seen[0] == {"check": "first"}
    assert all(note == {} for note in seen[1:])


def test_o35_a_try_of_a_campaign_with_no_health_check_has_a_note_that_says_so(tmp_path):
    """O35: no pre-run check is asked, so nothing will write the try's note, and it says so (`prerun.UNCHECKED`): an
    admission can tell a try of a campaign with no check from a caller that is no campaign, which has no note."""
    seen = []
    with build(tmp_path, executor=FakeExecutor(), plan=TWO_RUNS, admission=reading(seen)) as scheduler:
        scheduler.run()
    assert seen
    assert all(note == {prerun.UNCHECKED: True} for note in seen)


def test_o35_a_note_opened_for_a_campaign_with_no_health_check_carries_the_mark_and_one_with_a_check_does_not():
    """O35: `opened(checked=False)` is the mark; the default note is empty, as before."""
    with prerun.opened(checked=False) as note:
        assert note == {prerun.UNCHECKED: True}
        assert prerun.current() is note
    with prerun.opened() as note:
        assert note == {}
    assert prerun.current() is None


def test_o35_outside_a_try_noting_does_nothing():
    """O35: a check made outside any try (the dispatcher's own, or a caller that is no campaign) has no note."""
    assert prerun.current() is None
    assert prerun.note("check", 1) is False
    with prerun.opened() as note:
        assert prerun.note("check", 1) is True
        assert prerun.current() is note
        assert note == {"check": 1}
    assert prerun.current() is None
