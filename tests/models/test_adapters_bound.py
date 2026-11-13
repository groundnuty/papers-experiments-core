"""B19: what the two SDK adapters record of an abandoned request's prompt bound, against a fake client.

The tracker's arithmetic is in `test_request_bound.py`. Here the adapters are driven the way the loop drives them, and
each entry is checked against the bodies the fake client was actually sent: the bound is the last reported prompt and
completion plus the bytes of what the wire carried after it.
"""

import asyncio
import json

import anthropic
import pytest

from expcore.errors import RequestAbandoned
from expcore.models.adapters import AnthropicAdapter, OpenAICompatAdapter, ToolResult
from expcore.models.request_bound import FRAMING_MARGIN_TOKENS

WINDOW = 200_000
TOOLS = [{"name": "run", "description": "run code", "input_schema": {"type": "object", "properties": {}}}]


async def _no_sleep(seconds):
    return None


def openai_backend(spec, limits, client):
    return OpenAICompatAdapter(spec, 7, limits, client=client, sleep=_no_sleep)


def anthropic_backend(spec, limits, client):
    return AnthropicAdapter(spec, 7, limits, client=client, sleep=_no_sleep)


def step(backend):
    return asyncio.run(backend.step())


def wire_bytes(message) -> int:
    """The bytes of a message as a chat template can write it, counted here from what the client received: ASCII JSON,
    and six bytes for each `<`, `>`, `&` and `'`, which Jinja's `tojson` writes as `\\u003c` and its kin."""
    text = json.dumps(message, ensure_ascii=True)
    return len(text) + 5 * sum(text.count(c) for c in "<>&'")


def bytes_bound(prompt, completion, appended) -> int:
    """The bound the derivation gives: both reported counts, the bytes after them, and a margin per message."""
    return prompt + completion + sum(wire_bytes(m) for m in appended) + FRAMING_MARGIN_TOKENS * (1 + len(appended))


def round_trip(backend):
    """Ask once, answer the tool call the reply made, and leave the next request to send."""
    backend.begin("be brief", "go", TOOLS)
    (call,) = step(backend).tool_calls
    backend.add_tool_results([ToolResult(call, "2")])


def abandon_next(backend):
    """Send the next request into a read timeout, and return the entry the record keeps for it."""
    with pytest.raises(RequestAbandoned):
        step(backend)
    return backend.info()["abandoned_request"]


@pytest.fixture
def first_reply(completion):
    """A reply with a tool call, a prompt of 120 tokens and a completion of 30."""
    return completion(None, finish="tool_calls", tool_calls=[("run", '{"code": "1+1"}')], usage=(120, 30))


def test_b19_a_request_after_a_reply_is_bounded_by_its_reported_counts_and_the_bytes_the_wire_carried_since(
    make_spec, limits, first_reply, fake_client, make_timeout
):
    """B19: an OpenAI-compatible request abandoned after one reply: 120 prompt and 30 completion reported, plus the
    reply as it went back, the tool result and the margins; the reply maximum is the max_tokens the request carried."""
    client = fake_client(first_reply, make_timeout(read=True))
    backend = openai_backend(make_spec(context_tokens=WINDOW), limits, client)
    round_trip(backend)
    entry = abandon_next(backend)
    sent = client.requests[1]
    assert [m["role"] for m in sent["messages"]] == ["system", "user", "assistant", "tool"]
    bound = bytes_bound(120, 30, sent["messages"][2:])
    assert entry == {
        "reason": "read_timeout",
        "usage": "unknown",
        "prompt_bound_tokens": bound,
        "prompt_bound_by": "bytes",
        "reply_max_tokens": 4096,
        "window_tokens": WINDOW,
        "bound_checked": 0,
        "bound_exceeded": 0,
        "bound_unreported": 0,
        "bound_overlapped": 0,
    }
    assert sent["max_tokens"] == entry["reply_max_tokens"]
    assert bound < 2_000  # a few hundred tokens over the 150 reported, not the 200,000 of the window


def test_b19_the_first_request_abandoned_is_bounded_by_the_window_and_without_one_not_at_all(
    make_spec, limits, fake_client, make_timeout
):
    """B19: nothing anchors the first request, and the provider's own text is not in its bytes."""
    for window, prompt, by in ((WINDOW, WINDOW, "window"), (None, None, "none")):
        backend = openai_backend(make_spec(context_tokens=window), limits, fake_client(make_timeout(read=True)))
        backend.begin("", "go", [])
        entry = abandon_next(backend)
        assert (entry["prompt_bound_tokens"], entry["prompt_bound_by"], entry["reply_max_tokens"]) == (prompt, by, 4096)


def test_b19_a_request_the_run_deadline_cancelled_carries_the_same_bound(make_spec, limits, first_reply, fake_client):
    """B19: the loop's cut cancels the request in flight; the entry is made inside the retrier at that moment, from the
    request built last, and the request the cancel interrupted is no longer in flight afterwards."""

    async def hang(**kw):
        await asyncio.sleep(60)

    client = fake_client(first_reply)
    backend = openai_backend(make_spec(context_tokens=WINDOW), limits, client)
    round_trip(backend)
    client.chat.completions.create = hang

    async def run():
        await asyncio.wait_for(backend.step(), timeout=0.05)

    with pytest.raises(TimeoutError):
        asyncio.run(run())
    entry = backend.info()["abandoned_request"]
    assert entry["reason"] == "deadline"
    assert (entry["prompt_bound_by"], entry["prompt_bound_tokens"]) == (
        "bytes",
        bytes_bound(120, 30, backend.transcript()[2:]),
    )


def test_b19_a_resent_request_has_the_bound_of_its_step_and_the_failed_send_before_it_is_no_part_of_it(
    make_spec, limits, first_reply, fake_client, make_error, make_timeout
):
    """B19: the retrier resends the same body, so a resend abandoned has the step's bound; the 503 before it was charged
    as a top-up when it failed (C26) and is outside this bound."""
    client = fake_client(first_reply, make_error(503), make_timeout(read=True))
    backend = openai_backend(make_spec(context_tokens=WINDOW), limits, client)
    round_trip(backend)
    entry = abandon_next(backend)
    assert len(client.requests) == 3
    assert client.requests[1] == client.requests[2]
    assert entry["prompt_bound_tokens"] == bytes_bound(120, 30, client.requests[2]["messages"][2:])
    assert backend.info()["retries"] == 1


def test_b19_malformed_arguments_go_back_wrapped_and_the_wrapper_is_counted(
    make_spec, limits, completion, fake_client, make_timeout
):
    """B19: the reply reported two completion tokens, but the history carries its 300-character arguments again,
    escaped, inside `{"_raw": ...}`: the bound grows by those bytes, as it would for any tokenizer."""
    raw = '{"code": "' + 'a\\"' * 150
    reply = completion(None, finish="tool_calls", tool_calls=[("run", raw)], usage=(120, 2))
    client = fake_client(reply, make_timeout(read=True))
    backend = openai_backend(make_spec(context_tokens=WINDOW), limits, client)
    round_trip(backend)
    entry = abandon_next(backend)
    resent = client.requests[1]["messages"][2]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(resent) == {"_raw": raw}
    assert entry["prompt_bound_tokens"] == bytes_bound(120, 2, client.requests[1]["messages"][2:])
    assert entry["prompt_bound_tokens"] > 120 + 2 + len(resent)


def test_b19_reasoning_is_counted_when_the_entry_echoes_it_and_not_when_it_does_not(
    make_spec, limits, completion, fake_client, make_timeout
):
    """B19: the wire form decides: a reasoning field the history carries is input, and one stripped from it is not."""
    thoughts = "private thoughts " * 40
    bounds = {}
    for echo in ("reasoning_content", None):
        reply = completion(
            None, finish="tool_calls", tool_calls=[("run", "{}")], usage=(120, 30), reasoning_content=thoughts
        )
        client = fake_client(reply, make_timeout(read=True))
        backend = openai_backend(make_spec(context_tokens=WINDOW, echo_reasoning_field=echo), limits, client)
        round_trip(backend)
        entry = abandon_next(backend)
        assert (thoughts in json.dumps(client.requests[1])) is bool(echo)
        bounds[echo] = entry["prompt_bound_tokens"]
    assert bounds["reasoning_content"] - bounds[None] >= len(thoughts) + len("reasoning_content")


def test_b19_a_tool_result_of_wide_characters_is_counted_as_the_escapes_a_template_writes(
    make_spec, limits, first_reply, fake_client, make_timeout
):
    """B19: bytes and not characters: a result of 100 Japanese characters is 100 characters and 300 bytes of UTF-8,
    and a template's `\\uXXXX` escapes of them are 600."""
    bounds = {}
    for name, text in (("ascii", "a" * 100), ("wide", "日" * 100)):
        client = fake_client(first_reply, make_timeout(read=True))
        backend = openai_backend(make_spec(context_tokens=WINDOW), limits, client)
        backend.begin("be brief", "go", TOOLS)
        (call,) = step(backend).tool_calls
        backend.add_tool_results([ToolResult(call, text)])
        bounds[name] = abandon_next(backend)["prompt_bound_tokens"]
    assert bounds["wide"] - bounds["ascii"] == 600 - 100


def test_b19_apostrophes_in_the_arguments_a_model_sent_are_counted_as_the_template_will_write_them(
    make_spec, limits, completion, fake_client, make_timeout
):
    """B19: well-formed arguments go back as the model wrote them, and an OpenAI-compatible server's template writes
    them again with `tojson`, an apostrophe as the six bytes `\\u0027`. 40 apostrophes in a code argument make the
    bound 200 bytes larger than the same argument without them, as the adapter sent them."""
    bounds = {}
    for name, code in (("plain", "print(a); " * 40), ("quoted", "print('a'); " * 20)):  # 400 and 260 characters
        arguments = json.dumps({"code": code}, ensure_ascii=False)
        reply = completion(None, finish="tool_calls", tool_calls=[("run", arguments)], usage=(120, 30))
        client = fake_client(reply, make_timeout(read=True))
        backend = openai_backend(make_spec(context_tokens=WINDOW), limits, client)
        round_trip(backend)
        entry = abandon_next(backend)
        sent = client.requests[1]["messages"][2]["tool_calls"][0]["function"]["arguments"]
        assert sent == arguments  # the adapter sends the model's own text, apostrophes unescaped
        bounds[name] = (entry["prompt_bound_tokens"], len(arguments), code.count("'"))
    (plain, plain_length, _), (quoted, quoted_length, apostrophes) = bounds["plain"], bounds["quoted"]
    assert apostrophes == 40
    assert quoted - plain == (quoted_length - plain_length) + 5 * apostrophes


def test_b19_a_reply_that_left_its_prompt_count_out_gives_the_next_request_the_window(
    make_spec, limits, completion, fake_client, make_timeout
):
    """B19: the second reply reported no prompt count, so what it generated and what its prompt held cannot be added
    up: the third request is bounded by the window, and the entry counts the reply that left a count out."""
    one = completion(None, finish="tool_calls", tool_calls=[("run", "{}")], usage=(100, 5))
    two = completion(None, finish="tool_calls", tool_calls=[("run", "{}")], usage=(None, 5))
    client = fake_client(one, two, make_timeout(read=True))
    backend = openai_backend(make_spec(context_tokens=WINDOW), limits, client)
    backend.begin("", "go", TOOLS)
    for _ in range(2):
        (call,) = step(backend).tool_calls
        backend.add_tool_results([ToolResult(call, "r")])
    entry = abandon_next(backend)
    assert len(client.requests[2]["messages"]) == 5
    assert (entry["prompt_bound_by"], entry["prompt_bound_tokens"]) == ("window", WINDOW)
    assert entry["bound_unreported"] == 1


def test_b19_a_reply_over_its_bound_ends_the_byte_bound_for_the_run_and_the_record_says_so(
    make_spec, limits, completion, fake_client, make_timeout
):
    """B19: the second request's reply reports a prompt a million tokens long, far over the bytes it was sent with:
    the assumption failed for this endpoint, so the third request, abandoned, is bounded by the window, and the entry
    counts the one check and the one failure."""
    one = completion(None, finish="tool_calls", tool_calls=[("run", "{}")], usage=(100, 5))
    two = completion(None, finish="tool_calls", tool_calls=[("run", "{}")], usage=(1_000_000, 5))
    client = fake_client(one, two, make_timeout(read=True))
    backend = openai_backend(make_spec(context_tokens=2_000_000), limits, client)
    backend.begin("", "go", TOOLS)
    for _ in range(2):
        (call,) = step(backend).tool_calls
        backend.add_tool_results([ToolResult(call, "r")])
    entry = abandon_next(backend)
    assert (entry["prompt_bound_by"], entry["prompt_bound_tokens"]) == ("window", 2_000_000)
    assert (entry["bound_checked"], entry["bound_exceeded"]) == (1, 1)


def test_b19_a_request_that_failed_is_not_the_one_in_flight_when_a_probe_is_abandoned_after_it(
    make_spec, limits, first_reply, fake_client, make_error, make_timeout
):
    """B19: after a send that ended in an error, an abandon with nothing in flight (the admission probe, say) is bounded
    by the window, which holds for any request, and not by the stale bound of the one that failed."""
    client = fake_client(first_reply, make_error(400, message="bad request"), make_timeout(read=True))
    backend = openai_backend(make_spec(context_tokens=WINDOW), limits, client)
    round_trip(backend)
    with pytest.raises(Exception, match="bad request"):
        step(backend)
    with pytest.raises(RequestAbandoned):
        asyncio.run(backend.probe())
    entry = backend.info()["abandoned_request"]
    assert (entry["prompt_bound_by"], entry["prompt_bound_tokens"]) == ("window", WINDOW)


def test_b19_two_requests_in_flight_at_once_cost_the_conversation_its_byte_bound(
    make_spec, limits, first_reply, fake_client, make_timeout
):
    """B19: the loop sends one request at a time and the adapters are written for that, but a caller that does not
    leaves the anchor resting on a reply that cannot be matched to its request. The overlap is counted, and the
    abandoned request after it is bounded by the window though every count was truthful."""
    client = fake_client(first_reply, first_reply, make_timeout(read=True))
    answer = client.chat.completions.create

    async def slow(**request):
        await asyncio.sleep(0)  # a real send is not over before the next one starts
        return await answer(**request)

    client.chat.completions.create = slow
    backend = openai_backend(make_spec(context_tokens=WINDOW), limits, client)
    backend.begin("be brief", "go", TOOLS)

    async def both():
        return await asyncio.gather(backend.step(), backend.step())

    asyncio.run(both())
    entry = abandon_next(backend)
    assert (entry["prompt_bound_by"], entry["prompt_bound_tokens"]) == ("window", WINDOW)
    assert entry["bound_overlapped"] == 1


def test_b19_a_new_conversation_on_the_same_backend_is_not_bounded_from_the_old_one(
    make_spec, limits, first_reply, completion, fake_client, make_timeout
):
    """B19: `begin` starts the tracker over, so the first request of a second conversation is bounded by the window."""
    client = fake_client(first_reply, make_timeout(read=True))
    backend = openai_backend(make_spec(context_tokens=WINDOW), limits, client)
    round_trip(backend)
    backend.begin("be brief", "again", TOOLS)
    entry = abandon_next(backend)
    assert (entry["prompt_bound_by"], entry["prompt_bound_tokens"]) == ("window", WINDOW)


def test_b19_a_backend_that_was_not_abandoned_records_no_entry_and_no_bound(
    make_spec, limits, first_reply, fake_client
):
    """B19: the bound is computed for every request and shown only for the one that was given up."""
    backend = openai_backend(make_spec(context_tokens=WINDOW), limits, fake_client(first_reply))
    backend.begin("", "go", TOOLS)
    step(backend)
    assert backend.info()["abandoned_request"] is None


# --- the Messages API ---------------------------------------------------------------------------------------------

THINKING = {"type": "thinking", "thinking": "weighing the options " * 20, "signature": "c2lnbmF0dXJl" * 30}
TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "run", "input": {"code": "1+1"}}


def anthropic_abandoned(make_spec, limits, message, fake_client, make_timeout, blocks, usage):
    reply = message(blocks, stop="tool_use", usage=usage)
    client = fake_client(reply, make_timeout(read=True, sdk=anthropic), api="anthropic")
    backend = anthropic_backend(make_spec(backend="anthropic", context_tokens=WINDOW), limits, client)
    round_trip(backend)
    return abandon_next(backend), client


def test_b19_the_anchor_is_the_whole_prompt_cache_parts_included_and_thinking_blocks_with_signatures_are_counted(
    make_spec, limits, message, fake_client, make_timeout
):
    """B19: the Messages API reports the uncached remainder as `input_tokens`; the prompt the budget counts, and the
    anchor, adds both cache counts. The thinking blocks go back verbatim with their signatures, and are counted."""
    usage = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 300,
        "output_tokens": 5,
    }
    entry, client = anthropic_abandoned(
        make_spec, limits, message, fake_client, make_timeout, [THINKING, TOOL_USE], usage
    )
    sent = client.requests[1]["messages"]
    assert sent[1]["content"][0]["signature"] == THINKING["signature"]
    assert entry["prompt_bound_by"] == "bytes"
    assert entry["prompt_bound_tokens"] == bytes_bound(510, 5, sent[1:])
    assert entry["prompt_bound_tokens"] > 510 + len(THINKING["signature"]) + len(THINKING["thinking"])
    assert (entry["reply_max_tokens"], entry["window_tokens"]) == (4096, WINDOW)


def test_b19_a_system_prompt_and_tool_list_are_in_the_reported_prompt_and_are_not_counted_again(
    make_spec, limits, message, fake_client, make_timeout
):
    """B19: the first reply's prompt already holds the system prompt, the tools and any preamble the provider adds, so
    the bytes counted after it are the history appended, and the system block's 8 bytes are not among them."""
    usage = {"input_tokens": 700, "output_tokens": 5}
    entry, client = anthropic_abandoned(make_spec, limits, message, fake_client, make_timeout, [TOOL_USE], usage)
    assert entry["prompt_bound_tokens"] == bytes_bound(700, 5, client.requests[1]["messages"][1:])
