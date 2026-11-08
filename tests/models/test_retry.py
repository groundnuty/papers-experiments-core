"""Retry and classification: B01 (backoff, Retry-After), B02, B03, B09 (key rejected), B13 (context overflow)."""

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from expcore.config.models import ClientSettings
from expcore.errors import ContextOverflow, InfraError, RequestAbandoned
from expcore.models.retry import (
    CONTEXT_OVERFLOW_PATTERNS,
    Retrier,
    RetryPolicy,
    classify,
    context_overflow_match,
    retry_after_seconds,
)

POLICY = RetryPolicy(max_retries=5, base_delay_s=1.0, max_delay_s=10.0, jitter=0.25)


class Script:
    """A send function that raises the given errors in order, then answers; counts its requests."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = 0

    async def __call__(self):
        self.requests += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "answer"
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def run(retrier, send):
    return asyncio.run(retrier.call(send))


def retrier(sleeps, jitter, **kwargs):
    return Retrier(kwargs.pop("policy", POLICY), sleep=sleeps, rng=jitter(kwargs.pop("share", 0.0)), **kwargs)


def test_b01_three_429s_then_an_answer_gives_one_reply_with_three_recorded_retries(make_error, sleeps, jitter, sdk):
    """B01: 429 three times then an answer gives one reply, three recorded retries, and waits >= Retry-After."""
    errors = [make_error(429, headers={"retry-after": "60"}, message="slow down", sdk=sdk) for _ in range(3)]
    send = Script(*errors)
    reply, events = run(retrier(sleeps, jitter, share=0.5), send)
    assert reply == "answer"
    assert send.requests == 4
    assert [e["retry"] for e in events] == [1, 2, 3]
    assert all(e["status"] == 429 and e["retry_after_s"] == 60.0 and e["kind"] == "rate_limit" for e in events)
    assert len(sleeps.waits) == 3
    assert all(wait >= 60.0 for wait in sleeps.waits)
    assert sleeps.waits == [e["wait_s"] for e in events]


def test_b01_a_400_is_not_retried_and_is_raised_unchanged(make_error, sleeps, jitter):
    """B01: a plain 400 is raised after exactly one request."""
    error = make_error(400, message="bad field")
    send = Script(error)
    with pytest.raises(type(error)) as caught:
        run(retrier(sleeps, jitter), send)
    assert caught.value is error
    assert send.requests == 1
    assert sleeps.waits == []


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 529])
def test_b01_every_transient_status_is_retried(make_error, sleeps, jitter, status):
    """B01: 429, 408, 409 and every 5xx are transient."""
    send = Script(make_error(status))
    reply, events = run(retrier(sleeps, jitter), send)
    assert (reply, send.requests, len(events)) == ("answer", 2, 1)


@pytest.mark.parametrize("status", [400, 404, 410, 422])
def test_b01_other_statuses_are_not_retried(make_error, sleeps, jitter, status):
    """B01: Other statuses are not retried."""
    send = Script(make_error(status))
    with pytest.raises(Exception, match="the provider said no"):
        run(retrier(sleeps, jitter), send)
    assert send.requests == 1


def test_b01_timeouts_before_a_reply_and_connection_errors_are_retried(
    make_timeout, make_connection_error, sleeps, jitter, sdk
):
    """B01: Timeouts before a reply and connection errors are retried."""
    send = Script(make_timeout(read=False, sdk=sdk), make_connection_error(sdk=sdk))
    reply, events = run(retrier(sleeps, jitter), send)
    assert reply == "answer"
    assert [e["kind"] for e in events] == ["timeout", "connection"]


def test_b01_the_backoff_doubles_up_to_its_cap(make_error, sleeps, jitter):
    """B01: The backoff doubles up to its cap."""
    send = Script(*[make_error(503) for _ in range(5)])
    run(retrier(sleeps, jitter, share=0.0), send)
    assert sleeps.waits == [1.0, 2.0, 4.0, 8.0, 10.0]


def test_b01_jitter_only_lengthens_a_delay(jitter):
    """B01: whatever the draw, the wait is at least the backoff and at least the server's Retry-After."""
    for retry in range(6):
        for retry_after in (None, 0.5, 3.0, 60.0):
            for share in (0.0, 0.3, 1.0):
                wait = POLICY.delay(retry, retry_after, jitter(share))
                backoff = min(1.0 * 2**retry, 10.0)
                assert wait >= backoff
                assert wait >= (retry_after or 0.0)
                assert wait <= max(backoff, retry_after or 0.0) * 1.25 + 1e-9


def test_b01_retry_after_is_not_capped_by_the_maximum_delay(jitter):
    """B01: a 60 s header with a 10 s cap waits at least 60 s; the earlier design could sleep 45 s."""
    assert POLICY.delay(0, 60.0, jitter(0.0)) == 60.0
    assert POLICY.delay(0, 60.0, jitter(1.0)) == pytest.approx(75.0)


def test_b01_the_policy_comes_from_the_client_settings():
    """B01: The policy comes from the client settings."""
    policy = RetryPolicy.from_settings(
        ClientSettings(max_retries=3, retry_base_s=2.0, retry_max_s=30.0, retry_jitter=0.1)
    )
    assert policy == RetryPolicy(3, 2.0, 30.0, 0.1)


def test_b01_retry_after_is_read_in_seconds_milliseconds_and_as_a_date(make_error):
    """B01: Retry after is read in seconds milliseconds and as a date."""
    assert retry_after_seconds(make_error(429, headers={"Retry-After": "12"})) == 12.0
    assert retry_after_seconds(make_error(429, headers={"retry-after-ms": "1500"})) == 1.5
    now = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
    dated = format_datetime(now + timedelta(seconds=45), usegmt=True)
    assert retry_after_seconds(make_error(429, headers={"retry-after": dated}), now=lambda: now) == 45.0


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after-ms": "1500", "retry-after": "9"}, 9.0),
        ({"retry-after-ms": "30000", "retry-after": "9"}, 30.0),
        ({"Retry-After-Ms": "9000", "Retry-After": "9"}, 9.0),
        ({"retry-after-ms": "soon", "retry-after": "9"}, 9.0),
        ({"retry-after-ms": "-500", "retry-after": "9"}, 9.0),
        ({"retry-after-ms": "inf", "retry-after": "9"}, 9.0),
        ({"retry-after-ms": "1500", "retry-after": "soon"}, 1.5),
        ({"retry-after-ms": "1500", "retry-after": "-5"}, 1.5),
        ({"retry-after-ms": "1500", "retry-after": ""}, 1.5),
        ({"retry-after-ms": "soon", "retry-after": "soon"}, None),
    ],
)
def test_b01_retry_after_ms_never_shortens_retry_after_and_a_malformed_header_leaves_the_other(
    make_error, headers, expected
):
    """B01: the wait is the longer of the two valid headers; a malformed one falls back to the valid one."""
    assert retry_after_seconds(make_error(429, headers=headers)) == expected


def test_b01_the_longer_header_is_the_floor_of_the_wait(make_error, sleeps, jitter):
    """B01: A millisecond header shorter than Retry-After leaves the sleep at least Retry-After."""
    headers = {"retry-after-ms": "1500", "retry-after": "60"}
    send = Script(make_error(429, headers=headers))
    run(retrier(sleeps, jitter, share=0.0), send)
    assert sleeps.waits == [60.0]


@pytest.mark.parametrize("value", ["soon", "-5", "nan", "inf", ""])
def test_b01_an_unreadable_retry_after_is_ignored_not_trusted(make_error, value):
    """B01: An unreadable retry after is ignored not trusted."""
    assert retry_after_seconds(make_error(429, headers={"retry-after": value})) is None


def test_b01_no_header_and_no_response_give_none(make_error):
    """B01: No header and no response give none."""
    assert retry_after_seconds(make_error(429)) is None
    assert retry_after_seconds(ValueError("no response")) is None


def test_b02_retries_that_run_out_are_infrastructure_of_kind_endpoint(make_error, sleeps, jitter):
    """B02: max_retries + 1 transient errors end as InfraError endpoint, after exactly that many requests."""
    send = Script(*[make_error(503, message="upstream down") for _ in range(10)])
    with pytest.raises(InfraError) as caught:
        run(retrier(sleeps, jitter), send)
    assert caught.value.kind == "endpoint"
    assert send.requests == POLICY.max_retries + 1
    assert len(sleeps.waits) == POLICY.max_retries
    assert "InternalServerError HTTP 503" in caught.value.detail
    assert f"after {POLICY.max_retries} retries" in caught.value.detail


def test_b02_zero_retries_means_one_request(make_error, sleeps, jitter):
    """B02: Zero retries means one request."""
    send = Script(make_error(500), make_error(500))
    with pytest.raises(InfraError, match="after 0 retries"):
        run(retrier(sleeps, jitter, policy=RetryPolicy(max_retries=0)), send)
    assert send.requests == 1


def test_b03_a_read_timeout_is_abandoned_after_exactly_one_request(make_timeout, sleeps, jitter, sdk):
    """B03: a request that went out and is slow is never resent; usage is marked unknown."""
    send = Script(make_timeout(read=True, sdk=sdk))
    pump = retrier(sleeps, jitter)
    with pytest.raises(RequestAbandoned):
        run(pump, send)
    assert send.requests == 1
    assert sleeps.waits == []
    assert pump.abandoned == {"reason": "read_timeout", "usage": "unknown"}
    assert pump.stats.read_timeouts == 1


def test_b03_a_connect_timeout_is_retried_and_then_infrastructure(make_timeout, sleeps, jitter, sdk):
    """B03: connect, pool and write timeouts are failures to reach the endpoint."""
    send = Script(*[make_timeout(read=False, sdk=sdk) for _ in range(10)])
    with pytest.raises(InfraError) as caught:
        run(retrier(sleeps, jitter), send)
    assert caught.value.kind == "endpoint"
    assert send.requests == POLICY.max_retries + 1


def test_b03_a_retry_that_would_outlast_the_run_is_infrastructure_and_nothing_sleeps(make_error, sleeps, jitter):
    """B03: the retry's wait is checked against the run's remaining time."""
    send = Script(make_error(429, headers={"retry-after": "30"}))
    with pytest.raises(InfraError, match=r"1 s left in the run, no time to retry") as caught:
        run(retrier(sleeps, jitter, time_left=lambda: 1.0), send)
    assert caught.value.kind == "endpoint"
    assert send.requests == 1
    assert sleeps.waits == []


def test_b03_the_runs_deadline_is_checked_before_the_retry_limit(make_error, sleeps, jitter):
    """B03: with both applying, the detail names the run's time, not the retries."""
    send = Script(make_error(503))
    with pytest.raises(InfraError, match="no time to retry"):
        run(retrier(sleeps, jitter, policy=RetryPolicy(max_retries=0), time_left=lambda: 0.1), send)


def test_b03_a_wait_that_fits_the_run_is_taken(make_error, sleeps, jitter):
    """B03: A wait that fits the run is taken."""
    send = Script(make_error(503))
    reply, _ = run(retrier(sleeps, jitter, time_left=lambda: 100.0), send)
    assert reply == "answer"
    assert sleeps.waits == [1.0]


def test_b03_a_cancelled_request_is_recorded_as_abandoned_at_the_deadline(sleeps, jitter):
    """B03: A cancelled request is recorded as abandoned at the deadline."""

    async def send():
        raise asyncio.CancelledError

    pump = retrier(sleeps, jitter)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pump.call(send))
    assert pump.abandoned == {"reason": "deadline", "usage": "unknown"}


@pytest.mark.parametrize("status", [401, 403])
def test_b09_a_rejected_key_is_not_retried_and_the_sdk_text_is_not_kept(make_error, sleeps, jitter, status, sdk):
    """B09: 401 and 403 end as key_rejected after one request; the SDK message, which may echo a key, is dropped."""
    leak = "Incorrect API key provided: sk-live-0123456789"
    send = Script(make_error(status, message=leak, body={"error": {"message": leak}}, sdk=sdk))
    with pytest.raises(InfraError) as caught:
        run(retrier(sleeps, jitter), send)
    error = caught.value
    assert error.kind == "key_rejected"
    assert send.requests == 1
    assert sleeps.waits == []
    assert error.detail.startswith(f"HTTP {status} ")
    assert "sk-live" not in f"{error} {error.detail} {error!r}"
    assert error.__cause__ is None


def test_b13_every_tested_provider_wording_classifies_as_context_overflow(make_error):
    """B13: each row of the pattern table matches its own example, as a 400 and as a 413."""
    assert len(CONTEXT_OVERFLOW_PATTERNS) >= 4
    for row_id, example, _pattern in CONTEXT_OVERFLOW_PATTERNS:
        for status in (400, 413):
            info = classify(make_error(status, message=example))
            assert info is not None
            assert (info.kind, info.transient, info.overflow) == ("context_overflow", False, row_id)


def test_b13_the_error_code_and_a_413_alone_are_enough(make_error):
    """B13: The error code and a 413 alone are enough."""
    by_code = classify(make_error(400, message="rejected", body={"error": {"code": "context_length_exceeded"}}))
    assert by_code is not None
    assert (by_code.kind, by_code.overflow) == ("context_overflow", "code")
    bare_413 = classify(make_error(413, message="Payload Too Large"))
    assert bare_413 is not None
    assert (bare_413.kind, bare_413.overflow) == ("context_overflow", "http_413")


def test_b13_the_same_words_with_another_status_are_not_an_overflow(make_error):
    """B13: The same words with another status are not an overflow."""
    example = CONTEXT_OVERFLOW_PATTERNS[1][1]
    assert context_overflow_match(500, None, example) is None
    server = classify(make_error(500, message=example))
    assert server is not None
    assert (server.kind, server.transient) == ("server", True)
    plain = classify(make_error(400, message="messages.0.content: field required"))
    assert plain is not None
    assert (plain.kind, plain.transient) == ("bad_request", False)


def test_b13_an_overflow_is_raised_once_without_retry_and_without_provider_text(make_error, sleeps, jitter):
    """B13: not retried; the error keeps the status and pattern, not the provider's wording."""
    message = CONTEXT_OVERFLOW_PATTERNS[0][1]
    send = Script(make_error(400, message=message))
    with pytest.raises(ContextOverflow) as caught:
        run(retrier(sleeps, jitter), send)
    assert (caught.value.status, caught.value.pattern) == (400, "prompt_too_long")
    assert "1000001" not in str(caught.value)
    assert send.requests == 1
    assert sleeps.waits == []


def test_a_404_and_other_statuses_classify_as_not_transient(make_error):
    for status, kind in ((404, "not_found"), (422, "bad_request"), (410, "bad_request")):
        info = classify(make_error(status))
        assert info is not None
        assert (info.kind, info.transient) == (kind, False)


def test_an_error_that_is_not_the_providers_is_not_classified_and_passes_through(sleeps, jitter):
    """A bug in the harness is not an API error: it is raised unchanged after one request (L08)."""
    assert classify(ValueError("harness bug")) is None
    assert classify(httpx.ReadTimeout("raw transport error")) is None
    send = Script(ValueError("harness bug"))
    with pytest.raises(ValueError, match="harness bug"):
        run(retrier(sleeps, jitter), send)
    assert send.requests == 1


def test_a_test_double_with_the_shape_of_an_sdk_error_is_classified_by_shape():
    class RateLimitError(Exception):
        status_code = 429
        response = None

    info = classify(RateLimitError("fake"))
    assert info is not None
    assert (info.kind, info.transient) == ("rate_limit", True)


def test_retries_are_counted_across_calls_and_recorded_with_their_errors(make_error, sleeps, jitter):
    pump = retrier(sleeps, jitter)
    run(pump, Script(make_error(429, message="first")))
    run(pump, Script(make_error(500, message="second")))
    assert pump.stats.requests == 4
    assert pump.stats.retries == 2
    assert pump.stats.rate_limited == 1
    assert [e["message"] for e in pump.events] == ["first", "second"]
    assert [e["retry"] for e in pump.events] == [1, 1]
