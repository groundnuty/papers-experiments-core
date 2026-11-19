"""B20: an `openai_compat` model's `extra_headers` ride on every request it makes, admission probes included, beside
the session header. The real SDK runs against an `httpx.MockTransport`, so what the front would see on the wire is the
SDK's own. No network."""

import asyncio

import httpx
from openai import AsyncOpenAI

from expcore.models.adapters import OpenAICompatAdapter

ROUTE = {"x-hpcburst-workload": "batch-a", "X-Other": "a b"}
SESSION = "X-Session-Id"


class Front:
    """A fake front: records the headers of every request and answers each with a plain reply."""

    def __init__(self):
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        body = {
            "id": "cmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        return httpx.Response(200, json=body)


async def _no_sleep(seconds):
    return None


def backend(spec, limits, front):
    client = AsyncOpenAI(
        base_url="https://llm.example.invalid/v1",
        api_key="k",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(front)),
    )
    return OpenAICompatAdapter(spec, 7, limits, client=client, sleep=_no_sleep)


def test_x02_every_request_of_the_model_carries_the_headers_the_probes_too(make_spec, limits):
    """B20: a step, the admission probe and the probe of the reply maximum each carry the model's headers (the front
    routes by them, so a probe without them would be served, and judged, by another deployment)."""
    front = Front()
    model = backend(make_spec(extra_headers=ROUTE), limits, front)
    asyncio.run(model.probe())
    asyncio.run(model.probe_reply_cap())
    model.begin("", "go", [])
    asyncio.run(model.step())
    assert len(front.headers) == 3
    for sent in front.headers:
        assert (sent.get("x-hpcburst-workload"), sent.get("x-other")) == ("batch-a", "a b")


def test_x02_the_headers_are_merged_with_the_session_header_which_stays_the_attempts_id(make_spec, limits):
    """B20: both ride on the same request, and the session id is still the one the attempt bound."""
    front = Front()
    model = backend(make_spec(extra_headers=ROUTE, session_header=SESSION), limits, front)
    model.bind_session("0123456789abcdef0123456789abcdef")
    model.begin("", "go", [])
    asyncio.run(model.step())
    asyncio.run(model.probe())
    for sent in front.headers:
        assert sent.get("x-hpcburst-workload") == "batch-a"
        assert sent.get(SESSION) == "0123456789abcdef0123456789abcdef"


def test_x02_a_model_with_no_extra_headers_calls_the_client_and_sends_the_wire_as_before(
    make_spec, limits, completion, fake_client
):
    """B20: unset, the client is called exactly as it always was (no `extra_headers` argument) and the request on the
    wire carries none of the headers a model that sets them does."""
    client = fake_client(completion())
    plain = OpenAICompatAdapter(make_spec(), 7, limits, client=client, sleep=_no_sleep)
    plain.begin("", "go", [])
    asyncio.run(plain.step())
    assert "extra_headers" not in client.requests[0]
    front = Front()
    wired = backend(make_spec(), limits, front)
    asyncio.run(wired.probe())
    routed = Front()
    asyncio.run(backend(make_spec(extra_headers=ROUTE), limits, routed).probe())
    assert set(routed.headers[0].keys()) - set(front.headers[0].keys()) == {"x-hpcburst-workload", "x-other"}
    assert not {"x-hpcburst-workload", "x-other"} & set(front.headers[0].keys())


def test_x02_the_record_names_the_headers_sent_and_never_their_values(make_spec, limits):
    """B20: `backend_info` says which header names the model sent, sorted; a model that sets none says nothing, so its
    record is what it was; the values stay in models.toml."""
    model = backend(make_spec(extra_headers=ROUTE), limits, Front())
    info = model.info()
    assert info["extra_header_names"] == ["X-Other", "x-hpcburst-workload"]
    assert "batch-a" not in str(info)
    assert "a b" not in str(info)
    assert "extra_header_names" not in backend(make_spec(), limits, Front()).info()
