"""O90: the structured reading a post-run check leaves is kept with the attempt, in its marker and in the events.

The record is the executor's, written before the post-run check, so it carries a reading only when the executor took
one. The scheduler keeps what the post-run check noted for its own try (`prerun.HEALTH_READING`): in the attempt's
marker as the optional `post_run_health`, and as one event, in the transaction of the result. Nothing is kept for a
check that noted none, and a reading from before the run is never taken for the post-run check's.
"""

import json

import pytest
from campaign_support import LATER, make_schema
from scheduler_support import FakeExecutor, build, key_of, settings_for

from expcore.campaign import prerun
from expcore.campaign.factors import FactorSchema
from expcore.campaign.healthgate import Health
from expcore.campaign.markers import MARKER_NAME, AttemptResult, MarkerError, read_marker, write_marker
from expcore.loop.outcomes import Outcome


def nested(depth):
    """A JSON-looking value nested `depth` lists deep, a few KiB of text, that `json.dumps` cannot dump."""
    value: list = []
    for _ in range(depth):
        value = [value]
    return {"x": value}


READING = {
    "spaces": [{"position": "spaces[0]", "read": True, "offline": ["providers[1]"]}],
    "targets": ["providers[1]"],
}
SCHEMA = make_schema()
ONE = FactorSchema(SCHEMA.declared, SCHEMA.order, {"api-none": SCHEMA.conditions["api-none"]})  # a plan of one run
ONE_KEY = key_of(ONE, "api-none")
SMALL = {"tasks": ("task-a",), "seeds": (1,)}


class Noting:
    """A health check that notes a reading in its try, as the platform's does, `notes` of them for its calls in order
    (None: it notes nothing that call)."""

    def __init__(self, *notes):
        self.notes, self.calls = list(notes), 0

    def __call__(self):
        self.calls += 1
        noted = self.notes.pop(0) if self.notes else None
        if noted is not None:
            prerun.note(prerun.HEALTH_READING, noted)
        return Health(True, "fine")


def run_one(tmp_path, check):
    """One run of one condition with `check` as its health check; return the store, the executor and the attempts'
    markers read back by their run directory and the store's id of each attempt."""
    executor = FakeExecutor()
    with build(tmp_path, schema=ONE, executor=executor, plan=SMALL, health=check, settings=settings_for()) as scheduler:
        scheduler.run()
        rows = scheduler.store.q("SELECT id, run_key FROM attempts ORDER BY id")
        events = [r["detail"] for r in scheduler.store.q("SELECT detail FROM events WHERE kind='post_run_health'")]
        refused = [
            r["detail"] for r in scheduler.store.q("SELECT detail FROM events WHERE kind='post_run_health_refused'")
        ]
        markers = [read_marker(executor.requests[0].run_dir, row["run_key"], row["id"]) for row in rows[:1]]
    return markers, events, refused


def test_o90_a_reading_the_post_run_check_noted_is_kept_in_the_marker_and_as_an_event(tmp_path):
    """O90: the pre-run check noted nothing; the post-run check noted a reading: the attempt's marker holds it as
    `post_run_health`, and the store holds one event whose text is the run and the reading as JSON."""
    markers, events, refused = run_one(tmp_path, Noting(None, READING))
    assert markers[0].post_run_health == READING
    assert [json.loads(text.split(": ", 1)[1]) for text in events] == [READING]
    assert refused == []


def test_o90_a_reading_from_the_pre_run_check_is_never_the_post_run_checks(tmp_path):
    """O90: the pre-run check noted a reading and the post-run check none (a wrapper that never asked the platform):
    what the pre-run check left is not kept as the post-run state."""
    markers, events, _ = run_one(tmp_path, Noting(READING, None))
    assert markers[0].post_run_health is None
    assert events == []


@pytest.mark.parametrize(
    "unusable",
    ["a text", ["a", "list"], {"a": {1, 2}}, {"big": "x" * 70_000}, nested(5_000)],
    ids=["text", "list", "not-json", "too-big", "too-deep"],
)
def test_o90_a_reading_that_is_no_small_json_object_is_refused_with_an_event_and_never_kept(tmp_path, unusable):
    """O90: a reading is a JSON object of a bounded size; anything else is not kept, and the store says so."""
    markers, events, refused = run_one(tmp_path, Noting(None, unusable))
    assert markers[0].post_run_health is None
    assert events == []
    assert len(refused) == 1


def test_o90_a_marker_carries_the_reading_only_when_there_is_one(tmp_path):
    """O90: written only when present, so every other marker is as it was, and read back as it was written."""
    plain = AttemptResult(Outcome("finished"), LATER, 1.0)
    write_marker(tmp_path / "a", "k", 1, plain)
    assert "post_run_health" not in json.loads((tmp_path / "a" / MARKER_NAME).read_text())["result"]
    assert read_marker(tmp_path / "a", "k", 1).post_run_health is None
    held = AttemptResult(Outcome("finished"), LATER, 1.0, post_run_health=READING)
    write_marker(tmp_path / "b", "k", 1, held)
    assert json.loads((tmp_path / "b" / MARKER_NAME).read_text())["result"]["post_run_health"] == READING
    assert read_marker(tmp_path / "b", "k", 1) == held


def test_o90_a_marker_whose_reading_is_no_object_cannot_be_trusted(tmp_path):
    """O90: a marker holding a `post_run_health` that is not an object is refused, as any malformed marker is."""
    write_marker(tmp_path, "k", 1, AttemptResult(Outcome("finished"), LATER, 1.0, post_run_health=READING))
    path = tmp_path / MARKER_NAME
    body = json.loads(path.read_text())
    body["result"]["post_run_health"] = "providers[1]"
    path.write_text(json.dumps(body))
    with pytest.raises(MarkerError):
        read_marker(tmp_path, "k", 1)


def test_o90_the_helper_keeps_a_small_json_object_and_nothing_else():
    """O90: `prerun.reading_of` answers the reading when it is a JSON object within `READING_MAX_BYTES`, else None."""
    assert prerun.reading_of(READING) == READING
    for value in (None, "x", [1], {"a": {1}}, {"big": "x" * (prerun.READING_MAX_BYTES + 1)}, nested(5_000)):
        assert prerun.reading_of(value) is None
