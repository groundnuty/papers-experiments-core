"""F01 and F02: an `openai_compat` model behind a fleet's front can send an opaque session header and read the
deployment that served each reply. Both are optional per-model settings; the real SDK runs against an
`httpx.MockTransport`, so what the front would see on the wire and what the adapter reads back is the SDK's own. No
network."""

import asyncio
import re

import httpx
import pytest
from openai import AsyncOpenAI, BadRequestError

from expcore.models.adapters import OpenAICompatAdapter, ToolResult
from expcore.models.fleetheaders import clean_deployment_id, session_id

TOOLS = [{"name": "run", "description": "run code", "input_schema": {"type": "object", "properties": {}}}]
SESSION = "X-Session-Id"
DEPLOYMENT = "hpcburst-deployment-id"
HEX32 = re.compile(r"[0-9a-f]{32}")


class Front:
    """A fake front: records the headers of every request and answers the replies it was given, in order."""

    def __init__(self, *answers):
        self.headers: list[httpx.Headers] = []
        self.answers = list(answers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        status, reply_headers = self.answers.pop(0)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "nope"}}, headers=reply_headers)
        body = {
            "id": "cmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "system_fingerprint": "fp-1",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls" if len(self.headers) == 1 else "stop",
                    "message": {
                        "role": "assistant",
                        "content": None if len(self.headers) == 1 else "done",
                        **(
                            {
                                "tool_calls": [
                                    {"id": "call-0", "type": "function", "function": {"name": "run", "arguments": "{}"}}
                                ]
                            }
                            if len(self.headers) == 1
                            else {}
                        ),
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        return httpx.Response(200, json=body, headers=reply_headers)


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


def two_turns(model):
    """A tool round and a final reply: two requests of one attempt."""
    model.begin("", "go", TOOLS)
    first = asyncio.run(model.step())
    model.add_tool_results([ToolResult(first.tool_calls[0], "ok")])
    return first, asyncio.run(model.step())


def test_f01_every_request_of_an_attempt_carries_the_same_opaque_session_id_and_it_is_not_a_setting(make_spec, limits):
    """F01: a model with `session_header` sends it on every request of the attempt, one id, 32 hex characters; the id is
    not in the settings the record keeps (they must not differ from attempt to attempt)."""
    front = Front((200, {}), (200, {}))
    model = backend(make_spec(session_header=SESSION), limits, front)
    identity = session_id("a" * 64, "task=t|model=m|condition=secret-name|seed=1", 1)
    model.bind_session(identity)
    two_turns(model)
    assert [h.get(SESSION) for h in front.headers] == [identity, identity]
    assert HEX32.fullmatch(identity)
    assert "secret-name" not in identity
    assert not any(SESSION.lower() in str(v).lower() for v in model.info()["settings_sent"])


def test_f01_attempts_of_a_run_get_different_ids_a_lease_tells_two_attempts_of_one_number_apart():
    """F01: the id is a function of the campaign, the run, the attempt and the lease, and nothing else."""
    one = session_id("f" * 64, "run-key", 1)
    assert one == session_id("f" * 64, "run-key", 1)
    assert len({one, session_id("f" * 64, "run-key", 2), session_id("e" * 64, "run-key", 1)}) == 3
    assert len({one, session_id("f" * 64, "other-run", 1), session_id("f" * 64, "run-key", 1, lease_id=3)}) == 3
    assert session_id("f" * 64, "run-key", 1, lease_id=3) != session_id("f" * 64, "run-key", 1, lease_id=4)
    assert HEX32.fullmatch(session_id(None, "run-key", 1))  # a store that records no fingerprint is allowed
    # the encoding is unambiguous: moving a character between the run key and the attempt changes the id
    assert session_id("f" * 64, "run-key1", 2) != session_id("f" * 64, "run-key", 12)


def test_f01_a_model_with_no_session_header_sends_none_and_calls_the_client_as_before(
    make_spec, limits, completion, fake_client
):
    """F01: unset, the request carries no extra header and the client is called exactly as it always was."""
    client = fake_client(completion())
    plain = OpenAICompatAdapter(make_spec(), 7, limits, client=client, sleep=_no_sleep)
    plain.begin("", "go", [])
    asyncio.run(plain.step())
    assert "extra_headers" not in client.requests[0]
    front = Front((200, {}))
    wired = backend(make_spec(), limits, front)
    wired.begin("", "go", [])
    asyncio.run(wired.step())
    assert SESSION.lower() not in front.headers[0]


def test_f01_a_backend_nobody_bound_still_sends_one_stable_id_of_its_own(make_spec, limits):
    """F01: an admission probe builds the same adapter with no attempt. A header the model sets is sent with an id of
    the instance's own (one backend is one attempt), stable across its requests, never an error."""
    front = Front((200, {}), (200, {}))
    model = backend(make_spec(session_header=SESSION), limits, front)
    two_turns(model)
    ids = [h.get(SESSION) for h in front.headers]
    assert ids[0] == ids[1]
    assert HEX32.fullmatch(ids[0])
    other = Front((200, {}))
    elsewhere = backend(make_spec(session_header=SESSION), limits, other)
    elsewhere.begin("", "go", [])
    asyncio.run(elsewhere.step())
    assert other.headers[0].get(SESSION) != ids[0]


def test_f01_a_retried_request_carries_the_same_session_id(make_spec, limits):
    """F01: the retry layer resends the request: the resend is the same attempt's, so it carries the same id."""
    front = Front((503, {}), (200, {}))
    model = backend(make_spec(session_header=SESSION), limits, front)
    model.bind_session("0123456789abcdef0123456789abcdef")
    model.begin("", "go", [])
    turn = asyncio.run(model.step())
    assert [h.get(SESSION) for h in front.headers] == ["0123456789abcdef0123456789abcdef"] * 2
    assert len(turn.retries) == 1


def test_f02_the_deployment_header_of_each_reply_is_recorded_on_its_turn(make_spec, limits):
    """F02: with `deployment_header` set the adapter reads that response header (any case) on every reply: each turn
    names the deployment that served it, and what else the turn holds is what it always held."""
    front = Front((200, {"HPCBURST-Deployment-Id": "dep-1"}), (200, {"hpcburst-deployment-id": "dep-2"}))
    model = backend(make_spec(deployment_header=DEPLOYMENT), limits, front)
    first, second = two_turns(model)
    assert (first.deployment_id, second.deployment_id) == ("dep-1", "dep-2")
    assert (first.served_model, first.system_fingerprint, first.input_tokens, first.output_tokens) == (
        "test-model",
        "fp-1",
        10,
        5,
    )
    assert first.tool_calls[0].name == "run"
    assert second.text == "done"


def test_f02_a_reply_without_the_header_has_no_deployment_and_is_no_error(make_spec, limits):
    """F02: the header absent from a reply is None; so is one that is empty, too long, or not printable."""
    long = "x" * 129
    front = Front((200, {}), (200, {DEPLOYMENT: "  "}), (200, {DEPLOYMENT: long}))
    model = backend(make_spec(deployment_header=DEPLOYMENT), limits, front)
    model.begin("", "go", [])
    first = asyncio.run(model.step())
    second = asyncio.run(model.step())
    third = asyncio.run(model.step())
    assert (first.deployment_id, second.deployment_id, third.deployment_id) == (None, None, None)
    assert clean_deployment_id(" dep-9 ") == "dep-9"
    assert clean_deployment_id("a\x00b") is None
    assert clean_deployment_id(None) is None


def test_f02_a_model_with_no_deployment_header_records_no_deployment(make_spec, limits):
    """F02: unset, the reply's headers are not read: a front that sends one changes nothing."""
    front = Front((200, {DEPLOYMENT: "dep-1"}))
    model = backend(make_spec(), limits, front)
    model.begin("", "go", [])
    assert asyncio.run(model.step()).deployment_id is None


def test_f02_the_deployment_comes_from_the_reply_that_was_taken_not_from_a_failed_try(make_spec, limits):
    """F02: a reply the retry layer set aside names no deployment of the turn: the turn's is the one that answered."""
    front = Front((503, {DEPLOYMENT: "dep-bad"}), (200, {DEPLOYMENT: "dep-good"}))
    model = backend(make_spec(deployment_header=DEPLOYMENT), limits, front)
    model.begin("", "go", [])
    turn = asyncio.run(model.step())
    assert turn.deployment_id == "dep-good"
    assert len(turn.retries) == 1


def test_f02_a_failing_request_fails_the_way_it_did_without_the_setting(make_spec, limits):
    """F02: the raw-response path raises the SDK's own error: a 400 is the same refusal with the setting as without."""
    errors = []
    for extra in ({}, {"deployment_header": DEPLOYMENT, "session_header": SESSION}):
        model = backend(make_spec(**extra), limits, Front((400, {})))
        model.begin("", "go", [])
        with pytest.raises(BadRequestError) as caught:
            asyncio.run(model.step())
        errors.append((type(caught.value), str(caught.value)))
    assert errors[0] == errors[1]
