"""The executor's side: what a request carries, a record turned into the store's result, a raised error classified."""

import asyncio
import dataclasses

import pytest
from loop_support import FakeProvider
from record_support import CODE, hand_made, make_config
from scheduler_support import ReportingBackend

from expcore.campaign.attempts import classify
from expcore.campaign.executor import Endpoint, RunRequest, failure_result, record_run, result_from_record
from expcore.errors import InfraError
from expcore.records.build import RunIdentity

ENDED = "2026-01-01T00:10:00Z"


def test_a_record_becomes_the_stores_result_with_the_tokens_its_turns_reported():
    """The result carries the outcome, the summed tokens, whether usage is complete and the treatment kinds."""
    record = hand_made()
    result = result_from_record(record, ended_utc=ENDED)
    assert result.outcome.name == record.outcome["name"]
    assert (result.input_tokens, result.output_tokens) == (10, 2)
    assert result.cache_creation_tokens is None
    assert result.usage_complete is True
    assert result.treatment_kinds == ()
    assert result.wall_s == float(record.totals.get("elapsed_s") or 0.0)
    assert result_from_record(record, ended_utc=ENDED, wall_s=7.5).wall_s == 7.5


def test_c05_a_count_no_turn_reported_is_unknown_and_short_totals_are_marked_incomplete():
    """C05: a missing count stays None, never 0, and a record the loop knew to be short is incomplete."""
    record = hand_made()
    turns = [{**t, "usage": {**t["usage"], "input_tokens": None, "output_tokens": None}} for t in record.transcript]
    short = dataclasses.replace(record, transcript=turns, totals={**record.totals, "usage_incomplete": True})
    result = result_from_record(short, ended_utc=ENDED)
    assert (result.input_tokens, result.output_tokens) == (None, None)
    assert result.usage_complete is False


def test_an_infra_error_raised_outside_the_loop_is_infrastructure_of_its_kind_with_no_usage():
    """An InfraError the executor raises is recorded as infrastructure of that kind, uncounted tokens unknown."""
    result = failure_result(InfraError("sandbox", "did not start"), ended_utc=ENDED, wall_s=2.0)
    assert result.outcome.name == "infrastructure"
    assert result.outcome.infra_kind == "sandbox"
    assert (result.input_tokens, result.usage_complete) == (None, False)
    assert classify(result).state == "infra_fail"


def test_c10_any_other_exception_is_a_harness_defect_named_by_its_class_and_never_by_its_text():
    """C10: a crash in our code is a harness defect with a stable hash; the exception text is not kept."""
    secret = "sk-this-must-not-reach-the-store"
    try:
        raise ValueError(secret)
    except ValueError as error:
        result = failure_result(error, ended_utc=ENDED, wall_s=1.0, treatment_kinds=("endpoint",))
    assert result.outcome.name == "harness_defect"
    assert result.outcome.defect.exception == "ValueError"
    assert secret not in result.outcome.detail
    assert result.treatment_kinds == ("endpoint",)
    assert classify(result).state == "harness_error"


def test_record_run_gives_the_record_the_requests_treatment_kinds_backend_and_timer():
    """record_run passes the request's treatment kinds, so the record and the store agree about what is scored."""
    config = make_config()
    backend = ReportingBackend("m1", 1, "https://llm.example.invalid/v1")
    identity = RunIdentity("run-a", {"task": "t", "model": "m1", "seed": 1}, 1)
    request = RunRequest(
        identity, None, Endpoint("m1", "u", "https://llm.example.invalid/v1"), backend, config.timer(), lambda: 0.0,
        ("endpoint",),
    )  # fmt: skip
    record = asyncio.run(
        record_run(request, [FakeProvider({"look": lambda a: "ok"})], "sys", "user", config, code=dict(CODE))
    )
    assert record.treatment_kinds == ["endpoint"]
    assert record.route["base_url"] == "https://llm.example.invalid/v1"
    assert request.run_key == "run-a"
    assert request.timer.started  # the loop started the request's own timer


def test_a_timer_handed_to_the_loop_must_not_have_started_so_the_scheduler_never_starts_it():
    """The loop starts the attempt's timer; a started one is refused, so a lease wait before the loop is not on it."""
    config = make_config()
    timer = config.timer()
    timer.start()
    backend = ReportingBackend("m1", 1, None)
    identity = RunIdentity("run-a", {"task": "t", "model": "m1", "seed": 1}, 1)
    request = RunRequest(identity, None, Endpoint("m1", "u"), backend, timer, lambda: 0.0, ())
    with pytest.raises(ValueError, match="already started"):
        asyncio.run(record_run(request, [FakeProvider({})], "sys", "user", config, code=dict(CODE)))
