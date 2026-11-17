"""O82: the attempt runner gives each health check the identity of the attempt it judges, through the scheduler.

The gate-level behaviour is in `test_healthgate_context.py`; these tests run whole campaigns, so a wrong argument at the
runner's two call sites (the pre-run check before the attempt's row exists, the post-run check after its result) shows.
"""

import threading

from campaign_support import make_schema
from scheduler_support import FakeExecutor, build, key_of, settings_for

from expcore.campaign.healthgate import POST_RUN, PRE_RUN, Health

SMALL = {"tasks": ("task-a",), "seeds": (1,)}
SCHEMA = make_schema()


class Owner:
    """An owner-style check: keeps every context it is given, in the order given; `fail_first_post` is a run key whose
    first post-run check fails for a reason another attempt left (`charge=False`), so its attempt is set aside."""

    def __init__(self, fail_first_post=None):
        self.seen = []
        self.fail_first_post = fail_first_post
        self._lock = threading.Lock()

    def __call__(self, context):
        with self._lock:
            self.seen.append(context)
            failing = context is not None and context.phase == POST_RUN and context.run_key == self.fail_first_post
            if failing:
                self.fail_first_post = None
        return Health(False, "left by another attempt", charge=False) if failing else Health(True, "fine")


def rows_of(store, key):
    return [
        (row["attempt"], row["id"])
        for row in store.q("SELECT id, attempt FROM attempts WHERE run_key=? ORDER BY id", (key,))
    ]


def test_o82_the_runner_names_each_attempt_to_the_checks_that_judge_it(tmp_path):
    """O82: every pre-run check is given the attempt number its try will get (before any row exists, so no id) and every
    post-run check the number and the store's id of the attempt it judges, the second attempt of a retried run
    included, and the dispatcher's own check is never asked here, so none is given None."""
    retry = key_of(SCHEMA, "mount-read")
    executor = FakeExecutor({retry: [("infra", "sandbox")]})
    check = Owner()
    with build(tmp_path, executor=executor, plan=SMALL, health=check, settings=settings_for(workers=1)) as scheduler:
        summary = scheduler.run()
        store = scheduler.store
        assert summary.statuses == {"done": 3}
        assert [number for number, _ in rows_of(store, retry)] == [1, 2]
        assert None not in check.seen
        stored = {
            (row["run_key"], row["attempt"], row["id"]) for row in store.q("SELECT run_key, attempt, id FROM attempts")
        }
        post = {(c.run_key, c.attempt, c.attempt_id) for c in check.seen if c.phase == POST_RUN}
        pre = [(c.run_key, c.attempt, c.attempt_id) for c in check.seen if c.phase == PRE_RUN]
        assert post == stored
        assert sorted(pre) == sorted((key, number, None) for key, number, _ in stored)
        mine = [c for c in check.seen if c.run_key == retry]
        assert [(c.phase, c.attempt, c.attempt_id is None) for c in mine] == [
            (PRE_RUN, 1, True),
            (POST_RUN, 1, False),
            (PRE_RUN, 2, True),
            (POST_RUN, 2, False),
        ]
        assert [r.identity.attempt for r in executor.requests if r.run_key == retry] == [1, 2]


def test_o82_an_attempt_run_again_under_its_number_is_told_apart_by_its_id(tmp_path):
    """O82: an attempt set aside uncharged is run again under the number it had, so a number alone names two
    attempts: the second try's pre-run check is given that number with no id, and its post-run check the number with
    the id of its own row, which is not the first attempt's."""
    key = key_of(SCHEMA, "mount-read")
    check = Owner(fail_first_post=key)
    with build(
        tmp_path, executor=FakeExecutor(), plan=SMALL, health=check, settings=settings_for(workers=1)
    ) as scheduler:
        scheduler.run()
        rows = rows_of(scheduler.store, key)
        assert [number for number, _ in rows] == [1, 1]  # the first was never charged
        assert rows[0][1] != rows[1][1]
        mine = [c for c in check.seen if c.run_key == key]
        assert [(c.phase, c.attempt, c.attempt_id) for c in mine] == [
            (PRE_RUN, 1, None),
            (POST_RUN, 1, rows[0][1]),
            (PRE_RUN, 1, None),
            (POST_RUN, 1, rows[1][1]),
        ]
