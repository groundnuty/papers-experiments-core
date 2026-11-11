"""The spend guard's seam (C26): what the retrier and the recording tell an attempt's guard, and what a refusal does."""

import asyncio

import pytest
from loop_support import ScriptedBackend

from expcore.errors import BudgetHold, InfraError
from expcore.models import spend
from expcore.models.adapters import AnthropicAdapter, ModelTurn, OpenAICompatAdapter
from expcore.models.retry import Retrier, RetryPolicy
from expcore.records.recording import RecordingBackend

POLICY = RetryPolicy(max_retries=3, base_delay_s=1.0, max_delay_s=10.0, jitter=0.0)


class Guard:
    """A guard that logs what it is told and refuses the n-th clearance it is asked for (resends and requests alike),
    and, with `refuse_resend`, every resend it is asked to clear again after the wait."""

    def __init__(self, refuse_at: int | None = None, refuse_resend: bool = False):
        self.log: list[tuple] = []
        self.refuse_at, self.asked, self.refuse_resend = refuse_at, 0, refuse_resend

    def _clear(self, what: str) -> None:
        self.asked += 1
        if self.asked == self.refuse_at:
            raise BudgetHold(f"{what} does not fit")

    async def before_request(self) -> None:
        self.log.append(("request",))
        self._clear("the request")

    async def after_reply(self, input_tokens, output_tokens) -> None:
        self.log.append(("reply", input_tokens, output_tokens))

    async def after_failed_send(self, kind, *, resend) -> None:
        self.log.append(("failed", kind, resend))
        if resend:
            self._clear("the resend")

    async def before_resend(self, kind) -> None:
        self.log.append(("resend", kind))
        if self.refuse_resend:
            raise BudgetHold("the resend no longer fits")


class Sends:
    """A send function that raises the given errors in order, then answers; counts its sends."""

    def __init__(self, *outcomes):
        self.outcomes, self.sends = list(outcomes), 0

    async def __call__(self):
        self.sends += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "answer"
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def call_under(guard, retrier, send):
    async def go():
        with spend.watching(guard):
            return await retrier.call(send)

    return asyncio.run(go())


def test_c26_a_send_that_failed_after_it_may_have_been_billed_is_told_to_the_guard_before_it_is_sent_again(
    make_error, make_connection_error, sleeps
):
    """C26: a 5xx and a connection error may come after the request was billed, so each is told to the guard, which
    clears the resend; a 429 and a 408 are refused before any work and are not; the reply after them counts as usual.
    Every resend, whatever failure it follows, is cleared again right before it goes out (C27)."""
    send = Sends(make_error(503), make_connection_error(), make_error(429), make_error(408))
    guard = Guard()
    policy = RetryPolicy(max_retries=4, base_delay_s=1.0, max_delay_s=10.0, jitter=0.0)
    reply, events = call_under(guard, Retrier(policy, sleep=sleeps, rng=_Fixed()), send)
    assert (reply, send.sends, len(events)) == ("answer", 5, 4)
    assert guard.log == [
        ("failed", "server", True),
        ("resend", "server"),
        ("failed", "connection", True),
        ("resend", "connection"),
        ("resend", "rate_limit"),
        ("resend", "timeout"),
    ]


def test_c26_the_last_failed_send_of_a_request_is_told_to_the_guard_too_with_nothing_sent_after_it(make_error, sleeps):
    """C26: when the retries run out the last send may have been billed as well: the guard is told, with no resend."""
    send = Sends(*[make_error(500) for _ in range(4)])
    guard = Guard()
    with pytest.raises(InfraError, match="after 3 retries") as caught:
        call_under(guard, Retrier(POLICY, sleep=sleeps, rng=_Fixed()), send)
    assert caught.value.kind == "endpoint"
    assert guard.log == [("failed", "server", True), ("resend", "server")] * 3 + [("failed", "server", False)]


def test_c26_a_resend_the_guard_refuses_is_never_sent_and_no_retry_is_recorded_for_it(make_error, sleeps):
    """C26: a refused top-up ends the request as a budget hold before the wait: nothing more reaches the endpoint."""
    send = Sends(make_error(502), make_error(502))
    guard = Guard(refuse_at=2)
    retrier = Retrier(POLICY, sleep=sleeps, rng=_Fixed())
    with pytest.raises(BudgetHold, match="the resend does not fit") as caught:
        call_under(guard, retrier, send)
    assert caught.value.kind == "budget_hold"
    assert send.sends == 2
    assert [event["retry"] for event in retrier.events] == [1]  # the first resend went out; the second never did
    assert len(sleeps.waits) == 1


def test_c27_a_resend_is_cleared_again_after_the_wait_and_one_that_no_longer_fits_is_never_sent(make_error):
    """C27 and C26: what the guard cleared before the wait may not hold after it, so it is asked again right before the
    resend goes out; a refusal then ends the request as a budget hold, with nothing more sent to the endpoint."""
    send = Sends(make_error(502))
    guard = Guard(refuse_resend=True)

    async def wait(seconds: float) -> None:
        guard.log.append(("wait", seconds))

    retrier = Retrier(POLICY, sleep=wait, rng=_Fixed())
    with pytest.raises(BudgetHold, match="no longer fits") as caught:
        call_under(guard, retrier, send)
    assert caught.value.kind == "budget_hold"
    assert send.sends == 1
    assert guard.log == [("failed", "server", True), ("wait", 1.0), ("resend", "server")]


def test_c26_without_a_guard_the_retrier_behaves_as_before(make_error, sleeps):
    """C26: no campaign budget, no guard: a 503 is retried as it always was, and nothing is asked."""
    send = Sends(make_error(503))
    reply, events = asyncio.run(Retrier(POLICY, sleep=sleeps, rng=_Fixed()).call(send))
    assert (reply, send.sends, len(events)) == ("answer", 2, 1)


def test_c26_the_recording_clears_each_request_with_the_guard_and_tells_it_what_each_reply_reported():
    """C26: the guard sees every request before it is built and every reply's counts, missing ones as None."""
    script = [ModelTurn("first", [], None, 12, "stop", 0.0), ModelTurn("second", [], 30, None, "stop", 0.0)]
    backend = RecordingBackend(ScriptedBackend(script))
    guard = Guard()

    async def go():
        backend.begin("", "go", [])
        with spend.watching(guard):
            await backend.step()
            await backend.step()

    asyncio.run(go())
    assert guard.log == [("request",), ("reply", None, 12), ("request",), ("reply", 30, None)]


def test_c26_a_request_the_guard_refuses_is_recorded_as_that_requests_failure_a_budget_hold():
    """C26: the refusal is the request's own failure, so a record keeps it and a replay serves it again."""
    inner = ScriptedBackend([ModelTurn("first", [], None, 12, "stop", 0.0), "never sent"])
    backend = RecordingBackend(inner)
    guard = Guard(refuse_at=2)

    async def go():
        backend.begin("", "go", [])
        with spend.watching(guard):
            await backend.step()
            await backend.step()

    with pytest.raises(BudgetHold):
        asyncio.run(go())
    assert len(backend.captured) == 1
    assert (backend.failure["kind"], backend.failure["infra_kind"]) == ("infra", "budget_hold")
    assert inner._i == 1  # the second reply was never asked of the backend


class _Fixed:
    def uniform(self, a, b):
        return a


async def _no_sleep(seconds):
    return None


@pytest.mark.parametrize("sdk", ["openai", "anthropic"])
def test_c27_a_billed_reply_the_adapter_cannot_read_still_tells_the_guard_what_it_reported(
    sdk, make_spec, limits, completion, message, fake_client
):
    """C27 and C26: a 200 that carries usage but no choices, or content that is not a list of blocks, was billed: the
    adapter counts it and then refuses it, before the recording keeps a turn. The guard is told its counts all the
    same, so a reply past the reply maximum still takes the model off it."""
    if sdk == "openai":
        client = fake_client(completion(choices=False, usage=(900, 5_000)))
        backend = OpenAICompatAdapter(make_spec(), 7, limits, client=client, sleep=_no_sleep)
        counted = 900
    else:
        reply = message(usage={"input_tokens": 500, "output_tokens": 5_000})
        reply.content = None
        client = fake_client(reply, api="anthropic")
        backend = AnthropicAdapter(make_spec(backend="anthropic"), 7, limits, client=client, sleep=_no_sleep)
        counted = 500
    recording = RecordingBackend(backend)
    recording.begin("", "go", [])
    guard = Guard()

    async def go():
        with spend.watching(guard):
            await recording.step()

    with pytest.raises(InfraError, match="usage already counted"):
        asyncio.run(go())
    assert guard.log == [("request",), ("reply", counted, 5_000)]
    assert recording.failure["kind"] == "infra"
