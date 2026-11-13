"""The run times a shard's export gets before it is joined into a merged batch's export (#154, C29).

`timed_export` is given a shard's export, the wall time of each of its done attempts (from the shard's snapshot) and
the verified records (the analysis reads them anyway); it returns a copy whose analysed entries also carry `wall_s` and
`turn_latency_s`. The export it is given, and the exports of an unmerged batch and of a shard, are never touched.
"""

import copy
from types import SimpleNamespace

import pytest

from expcore.campaign.export import ExportError
from expcore.study.merged_times import timed_export

EXPORT = {
    "format": "campaign-export-1",
    "planned_runs": 3,
    "exhausted_runs": [{"run_key": "c", "attempts": 3, "reasons": ["endpoint: down"]}],
    "analysed_attempts": [
        {"run_key": "a", "digest": "da", "run_dir": "da/try1", "attempt": 1, "lease_outcome": "granted"},
        {"run_key": "b", "digest": "db", "run_dir": "db/try2", "attempt": 2, "lease_outcome": "none"},
    ],
    "retries_by_kind": [],
}
WALLS = {("a", 1): 12.5, ("b", 2): 3.0, ("b", 1): 99.0}  # an earlier attempt of b is in the snapshot too


def record(*latencies: float) -> SimpleNamespace:
    return SimpleNamespace(transcript=[{"index": i, "latency_s": value} for i, value in enumerate(latencies)])


def lookup(records: dict[tuple, SimpleNamespace]):
    """A `record_for` that answers by the arguments the analysis passes: digest, try directory, run key, attempt."""
    return lambda digest, try_name, run_key, attempt: records.get((digest, try_name, run_key, attempt))


RECORDS = {("da", "try1", "a", 1): record(0.5, 1.25, 2.0), ("db", "try2", "b", 2): record()}


def test_c29_each_analysed_entry_gets_its_attempts_wall_time_and_the_latency_of_its_turns_in_turn_order():
    """C29: the wall time of the analysed attempt itself (not an earlier one of the run), and one latency per turn of
    its record, in turn order; a record without a turn gives an empty list, which is recorded and holds no value."""
    timed = timed_export(EXPORT, WALLS, lookup(RECORDS))
    first, second = timed["analysed_attempts"]
    assert (first["wall_s"], first["turn_latency_s"]) == (12.5, [0.5, 1.25, 2.0])
    assert (second["wall_s"], second["turn_latency_s"]) == (3.0, [])
    assert list(first) == ["run_key", "digest", "run_dir", "attempt", "lease_outcome", "wall_s", "turn_latency_s"]


def test_c29_a_timed_export_changes_nothing_else_and_leaves_the_export_it_was_given_as_it_was():
    """C29: every other key and list is the export's own, in its own order, and the argument is not mutated."""
    before = copy.deepcopy(EXPORT)
    timed = timed_export(EXPORT, WALLS, lookup(RECORDS))
    assert before == EXPORT
    assert list(timed) == list(EXPORT)
    assert {k: v for k, v in timed.items() if k != "analysed_attempts"} == {
        k: v for k, v in EXPORT.items() if k != "analysed_attempts"
    }
    assert [
        {k: v for k, v in e.items() if k not in ("wall_s", "turn_latency_s")} for e in timed["analysed_attempts"]
    ] == (EXPORT["analysed_attempts"])


def test_c29_an_attempt_without_a_wall_time_row_keeps_none_as_unknown_never_zero():
    """C29: a store from before the column holds NULL; the entry says so with None, read as unknown."""
    timed = timed_export(EXPORT, {**WALLS, ("a", 1): None}, lookup(RECORDS))
    assert timed["analysed_attempts"][0]["wall_s"] is None


def test_c29_an_entry_with_no_record_or_no_attempt_row_is_refused_naming_the_run():
    """C29: a time is never invented: an analysed entry whose record or attempt the shard does not hold is refused."""
    with pytest.raises(ExportError, match="run b: no verified record"):
        timed_export(EXPORT, WALLS, lookup({("da", "try1", "a", 1): record(1.0)}))
    with pytest.raises(ExportError, match=r"run a: the shard's snapshot has no done attempt 1"):
        timed_export(EXPORT, {("b", 2): 3.0}, lookup(RECORDS))
