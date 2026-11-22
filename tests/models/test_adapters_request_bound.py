"""B21: the two SDK adapters under the study's bound on one model request, against a fake client.

The loop hands the bound to the backend before each step (`bound_requests`); these tests hand it directly, as the loop
would, and use a bound of a few hundredths of a second, so a request that never answers is given up at once in real
time. The arming rule (only while the bound is the shorter of it, the agent's time left and the ceiling left) is the
loop's, tested in `tests/loop/test_model_request_bound.py`.
"""

import asyncio

import pytest

from expcore.errors import InfraError, ModelRequestTimedOut
from expcore.models.adapters import REQUEST_BOUND, AnthropicAdapter, OpenAICompatAdapter, ToolResult

BOUND_S = 0.05
TOOLS = [{"name": "run", "description": "run code", "input_schema": {"type": "object", "properties": {}}}]


async def _no_sleep(seconds):
    return None


class Hang:
    """Stands in for the SDK's `create`: takes each request and never answers it, counting the requests."""

    def __init__(self) -> None:
        self.requests = 0

    async def __call__(self, **kw):
        self.requests += 1
        await asyncio.Event().wait()


def openai_backend(spec, limits, client):
    return OpenAICompatAdapter(spec, 7, limits, client=client, sleep=_no_sleep)


def step(backend):
    return asyncio.run(backend.step())


@pytest.fixture
def first_reply(completion):
    """A reply with a tool call, a prompt of 120 tokens and a completion of 30."""
    return completion(None, finish="tool_calls", tool_calls=[("run", '{"code": "1+1"}')], usage=(120, 30))


def test_b21_a_request_accepted_and_never_answered_ends_at_the_bound_as_endpoint_infrastructure(
    make_spec, limits, first_reply, fake_client
):
    """B21: after one reply, the next request hangs; at the bound it is given up as `endpoint` infrastructure, and the
    record's abandoned request says why, with the bound on its prompt read while it was the one in flight (B19)."""
    client = fake_client(first_reply)
    backend = openai_backend(make_spec(context_tokens=200_000), limits, client)
    backend.bound_requests(BOUND_S)
    backend.begin("be brief", "go", TOOLS)
    (call,) = step(backend).tool_calls
    backend.add_tool_results([ToolResult(call, "2")])
    client.chat.completions.create = Hang()
    with pytest.raises(ModelRequestTimedOut) as raised:
        step(backend)
    assert isinstance(raised.value, InfraError)
    assert (raised.value.kind, raised.value.bound_s, raised.value.billed) == ("endpoint", BOUND_S, None)
    assert raised.value.detail == "the model endpoint did not answer a request within 0.05 s; not resent"
    entry = backend.info()["abandoned_request"]
    assert (entry["reason"], entry["usage"], entry["prompt_bound_by"]) == (REQUEST_BOUND, "unknown", "bytes")


def test_b21_the_anthropic_adapter_gives_a_request_up_at_the_bound_alike(make_spec, limits, fake_client):
    """B21: the bound is in the send both SDK adapters share, so a Messages API request is given up the same way."""
    client = fake_client(api="anthropic")
    client.messages.create = Hang()
    backend = AnthropicAdapter(make_spec(backend="anthropic"), 7, limits, client=client, sleep=_no_sleep)
    backend.bound_requests(BOUND_S)
    backend.begin("be brief", "go", TOOLS)
    with pytest.raises(ModelRequestTimedOut):
        step(backend)
    assert backend.info()["abandoned_request"]["reason"] == REQUEST_BOUND


def test_b21_a_request_given_up_at_the_bound_is_never_resent_though_retries_remain(
    make_spec, limits, fake_client, make_error
):
    """B21 and B03: a 503 is resent, as before, and the resend is bounded too; once it hangs past the bound it is given
    up and not sent again, with seven of the eight retries unused."""
    hang = Hang()
    client = fake_client(make_error(503))
    backend = openai_backend(make_spec(), limits, client)
    sends = []

    async def first_fails_then_hangs(**kw):
        sends.append(kw)
        return await (client._create(**kw) if len(sends) == 1 else hang(**kw))

    client.chat.completions.create = first_fails_then_hangs
    backend.bound_requests(BOUND_S)
    backend.begin("", "go", [])
    with pytest.raises(ModelRequestTimedOut):
        step(backend)
    assert hang.requests == 1
    assert backend.requests_sent == 2
    info = backend.info()
    assert (info["retries"], info["read_timeouts"]) == (1, 0)
    assert info["abandoned_request"]["reason"] == REQUEST_BOUND


def test_b21_a_reply_under_the_bound_is_what_it_is_without_one(make_spec, limits, first_reply, fake_client):
    """B21: a request answered before its bound gives the same turn, the same request and the same record as with no
    bound at all, and abandons nothing."""
    seen = []
    for bound in (None, 30.0):
        client = fake_client(first_reply)
        backend = openai_backend(make_spec(), limits, client)
        backend.bound_requests(bound)
        backend.begin("be brief", "go", TOOLS)
        turn = step(backend)
        turn.latency_s = 0.0  # wall time, which differs between any two runs
        seen.append((turn, client.requests, backend.info()))
    assert seen[0] == seen[1]
    assert seen[1][2]["abandoned_request"] is None


def test_b21_a_timeout_error_the_send_raised_itself_is_not_taken_for_the_bound(make_spec, limits, fake_client):
    """B21: only the bound's own expiry is a request given up at the bound; a `TimeoutError` from inside the send, long
    before the bound, goes on as it came and abandons nothing."""
    backend = openai_backend(make_spec(), limits, fake_client(TimeoutError("from the transport")))
    backend.bound_requests(30.0)
    backend.begin("", "go", [])
    with pytest.raises(TimeoutError, match="from the transport") as raised:
        step(backend)
    assert not isinstance(raised.value, ModelRequestTimedOut)
    assert backend.info()["abandoned_request"] is None


def test_b21_a_bound_lifted_with_none_leaves_the_request_to_the_loops_own_cut(make_spec, limits, fake_client):
    """B21: the loop lifts the bound (None) for a step where it is not the shorter; the request is then bounded only by
    the caller, whose cancel abandons it as the run's deadline, as without the key."""
    client = fake_client()
    client.chat.completions.create = Hang()
    backend = openai_backend(make_spec(), limits, client)
    backend.bound_requests(BOUND_S)
    backend.bound_requests(None)
    backend.begin("", "go", [])

    async def cut_from_outside():
        await asyncio.wait_for(backend.step(), timeout=4 * BOUND_S)

    with pytest.raises(TimeoutError) as raised:
        asyncio.run(cut_from_outside())
    assert not isinstance(raised.value, ModelRequestTimedOut)
    assert backend.info()["abandoned_request"]["reason"] == "deadline"
