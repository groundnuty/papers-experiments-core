"""The Anthropic adapter against a fake client: B05 (thinking verbatim), B10 (cache split), B11, B12, B14, B15, B09."""

import asyncio
import json

import anthropic
import pytest

from expcore.errors import ContextOverflow, InfraError, RequestAbandoned
from expcore.models.adapters import AnthropicAdapter, ToolResult

TOOLS = [{"name": "run", "description": "run code", "input_schema": {"type": "object", "properties": {}}}]
THINKING = {"type": "thinking", "thinking": "weighing the options", "signature": "c2lnbmF0dXJl=="}
REDACTED = {"type": "redacted_thinking", "data": "ZW5jcnlwdGVk"}


async def _no_sleep(seconds):
    return None


def adapter(spec, limits, client, **kwargs):
    kwargs.setdefault("sleep", _no_sleep)
    return AnthropicAdapter(spec, 7, limits, client=client, **kwargs)


def step(backend):
    return asyncio.run(backend.step())


def test_a_turn_is_sent_with_the_system_block_tools_and_limit_and_parsed(make_spec, limits, message, fake_client):
    client = fake_client(message(usage={"input_tokens": 7, "output_tokens": 3}), api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("be brief", "hello", TOOLS)
    turn = step(backend)
    request = client.requests[0]
    assert request["model"] == "test-claude"
    assert request["max_tokens"] == 4096
    assert request["system"] == [{"type": "text", "text": "be brief"}]
    assert request["messages"] == [{"role": "user", "content": "hello"}]
    assert request["tools"] == TOOLS
    assert not {"cache_control", "extra_body", "temperature", "seed"} & request.keys()
    assert (turn.text, turn.input_tokens, turn.output_tokens, turn.stop_reason) == ("hello", 7, 3, "end_turn")
    assert turn.served_model == "test-claude"


def test_a_conversation_without_a_system_prompt_sends_none(make_spec, limits, message, fake_client):
    client = fake_client(message(), api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "hello", [])
    step(backend)
    assert "system" not in client.requests[0]
    assert "tools" not in client.requests[0]
    assert [m["role"] for m in backend.transcript()] == ["user", "assistant"]


def test_b05_thinking_blocks_go_back_verbatim_signatures_included(make_spec, limits, message, fake_client):
    """B05: the next request's assistant content equals the previous response's blocks."""
    blocks = [
        THINKING,
        REDACTED,
        {"type": "text", "text": "calling"},
        {"type": "tool_use", "id": "tu_1", "name": "run", "input": {"code": "1+1"}},
    ]
    client = fake_client(message(blocks, stop="tool_use"), message(), api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "go", TOOLS)
    turn = step(backend)
    (call,) = turn.tool_calls
    backend.add_tool_results([ToolResult(call, "2")])
    step(backend)
    resent = client.requests[1]["messages"]
    assert resent[1] == {"role": "assistant", "content": blocks}
    assert resent[1]["content"][0]["signature"] == "c2lnbmF0dXJl=="
    assert resent[2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "2"}]}
    assert turn.reasoning == "weighing the options"


def test_a_tool_error_result_is_marked_as_an_error(make_spec, limits, message, fake_client):
    blocks = [{"type": "tool_use", "id": "tu_1", "name": "run", "input": {}}]
    client = fake_client(message(blocks, stop="tool_use"), message(), api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "go", TOOLS)
    (call,) = step(backend).tool_calls
    backend.add_tool_results([ToolResult(call, "boom", is_error=True)])
    step(backend)
    assert client.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_b10_input_tokens_are_uncached_plus_cache_write_plus_cache_read(make_spec, limits, message, fake_client):
    """B10: the prompt size is the sum of the three parts, so cached tokens count toward the token budget."""
    usage = {
        "input_tokens": 7,
        "output_tokens": 3,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 100,
    }
    client = fake_client(message(usage=usage), api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "go", [])
    turn = step(backend)
    assert turn.input_tokens == 127
    assert (turn.uncached_input_tokens, turn.cache_creation_input_tokens, turn.cache_read_input_tokens) == (7, 20, 100)
    assert backend.info()["usage_seen"] == {"responses": 1, "input": 127, "output": 3, "missing": 0}


def test_b10_cache_parts_the_reply_does_not_report_stay_none_and_do_not_change_the_total(
    make_spec, limits, message, fake_client
):
    """B10: Cache parts the reply does not report stay none and do not change the total."""
    client = fake_client(message(usage={"input_tokens": 7, "output_tokens": 3}), api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "go", [])
    turn = step(backend)
    assert turn.input_tokens == 7
    assert (turn.cache_creation_input_tokens, turn.cache_read_input_tokens) == (None, None)


def test_b10_cache_and_reasoning_fields_beyond_the_dedicated_ones_are_kept_under_their_own_names(
    make_spec, limits, message, fake_client
):
    """B10: the cache split and the thinking count a reply reports ride on the turn, recorded only. The counts the turn
    already holds (the input, the output and the two cache counts) are not repeated, and text is left out."""
    usage = {
        "input_tokens": 7,
        "output_tokens": 3,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 100,
        "cache_creation": {"ephemeral_5m_input_tokens": 12, "ephemeral_1h_input_tokens": 8},
        "reasoning_tokens": 41,
        "service_tier": "standard",
    }
    client = fake_client(message(usage=usage), api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "go", [])
    turn = step(backend)
    assert turn.usage_extra == {
        "cache_creation.ephemeral_1h_input_tokens": 8,
        "cache_creation.ephemeral_5m_input_tokens": 12,
        "reasoning_tokens": 41,
    }
    assert turn.usage_extra_cut == 0
    assert turn.input_tokens == 127  # the budget's count is unchanged by the extras
    assert (turn.uncached_input_tokens, turn.cache_creation_input_tokens, turn.cache_read_input_tokens) == (7, 20, 100)


def test_b10_a_reply_with_only_the_usual_counts_has_no_extra_fields(make_spec, limits, message, fake_client):
    """B10: a field the reply does not carry is absent from the mapping, never 0."""
    client = fake_client(message(usage={"input_tokens": 7, "output_tokens": 3}), api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "go", [])
    turn = step(backend)
    assert (turn.usage_extra, turn.usage_extra_cut) == ({}, 0)


def test_b10_a_reply_without_usage_is_infrastructure_and_counted_missing(make_spec, limits, message, fake_client):
    """B10: unreported usage is None, never 0."""
    reply = message()
    reply.usage = None
    client = fake_client(reply, api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "go", [])
    with pytest.raises(InfraError, match="no usage") as caught:
        step(backend)
    assert caught.value.kind == "endpoint"
    assert backend.info()["usage_seen"] == {"responses": 1, "input": None, "output": None, "missing": 1}


def test_b10_a_reply_without_content_is_infrastructure_and_its_usage_still_counts(
    make_spec, limits, message, fake_client
):
    """B10: A reply without content is infrastructure and its usage still counts."""
    reply = message(usage={"input_tokens": 500, "output_tokens": 1})
    reply.content = None
    client = fake_client(reply, api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "go", [])
    with pytest.raises(InfraError, match="no content blocks"):
        step(backend)
    assert backend.info()["usage_seen"]["input"] == 500


def test_b11_a_model_with_a_lower_maximum_sends_its_own_value(make_spec, limits, message, fake_client):
    """B11: A model with a lower maximum sends its own value."""
    client = fake_client(message(), api="anthropic")
    backend = adapter(make_spec(backend="anthropic", max_output_tokens=2048), limits, client)
    backend.begin("", "go", [])
    step(backend)
    assert client.requests[0]["max_tokens"] == 2048
    assert backend.output_limit == 2048


def test_b12_a_reply_cut_at_max_tokens_is_recorded_as_cut(make_spec, limits, message, fake_client):
    """B12: the provider's stop reason max_tokens is kept and marks the turn as cut at the output limit."""
    client = fake_client(message(stop="max_tokens"), message(), api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "go", [])
    cut = step(backend)
    assert (cut.stop_reason, cut.hit_output_limit) == ("max_tokens", True)
    assert step(backend).hit_output_limit is False


def test_b14_vendor_extras_such_as_thinking_reach_the_request(make_spec, limits, message, fake_client):
    """B14: extra_body passes through, and the entry's sampling keys are sent as they are."""
    extras = {"thinking": {"type": "enabled", "budget_tokens": 2048}, "output_config": {"effort": "high"}}
    spec = make_spec(backend="anthropic", extra_body=extras, sampling={"temperature": 1.0})
    client = fake_client(message(), api="anthropic")
    backend = adapter(spec, limits, client)
    backend.begin("", "go", [])
    step(backend)
    assert client.requests[0]["extra_body"] == extras
    assert client.requests[0]["temperature"] == 1.0


def test_prompt_cache_markers_are_sent_only_when_the_entry_sets_a_ttl(make_spec, limits, message, fake_client):
    cached = fake_client(message(), api="anthropic")
    backend = adapter(make_spec(backend="anthropic", prompt_cache_ttl="1h"), limits, cached)
    backend.begin("system", "go", [])
    step(backend)
    marker = {"type": "ephemeral", "ttl": "1h"}
    assert cached.requests[0]["cache_control"] == marker
    assert cached.requests[0]["system"][0]["cache_control"] == marker
    assert backend.info()["prompt_cache"] == marker
    assert backend.transcript()[0] == {"role": "system", "content": "system"}  # history never holds a marker
    plain = adapter(make_spec(backend="anthropic"), limits, fake_client(message(), api="anthropic"))
    assert plain.info()["prompt_cache"] is None


def test_b15_the_recorded_settings_come_from_the_request_and_the_url_loses_its_credentials(
    make_spec, limits, message, fake_client
):
    """B15: sampling in the record is what the request carried; credentials in a base URL are not recorded."""
    spec = make_spec(backend="anthropic", sampling={"temperature": 0.5})
    client = fake_client(message(), api="anthropic")
    backend = adapter(spec, limits, client, base_url="https://user:pw@gateway.example.invalid/anthropic")
    backend.begin("system text", "user text", TOOLS)
    step(backend)
    info = backend.info()
    assert info["settings_sent"]["temperature"] == client.requests[0]["temperature"] == 0.5
    assert info["settings_sent"]["max_tokens"] == client.requests[0]["max_tokens"]
    assert not {"messages", "tools", "system"} & info["settings_sent"].keys()
    assert info["base_url"] == "https://gateway.example.invalid/anthropic"
    assert "pw" not in json.dumps(info)
    assert (info["backend"], info["seed_honoured"], info["model"]) == ("anthropic", False, "test-claude")
    assert info["sdk_versions"]["anthropic"]


def test_b01_a_529_overload_is_retried_and_recorded_on_the_turn(make_spec, limits, message, fake_client, make_error):
    """B01: A 529 overload is retried and recorded on the turn."""
    client = fake_client(make_error(529, sdk=anthropic), make_error(429, sdk=anthropic), message(), api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "go", [])
    turn = step(backend)
    assert [r["status"] for r in turn.retries] == [529, 429]
    assert backend.info()["rate_limited_429"] == 1


def test_b02_retries_that_run_out_end_as_infrastructure_endpoint(make_spec, limits, fake_client, make_error):
    """B02: Retries that run out end as infrastructure endpoint."""
    from expcore.config.models import ClientSettings

    client = fake_client(*[make_error(500, sdk=anthropic) for _ in range(4)], api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client, client_cfg=ClientSettings(max_retries=1))
    backend.begin("", "go", [])
    with pytest.raises(InfraError) as caught:
        step(backend)
    assert caught.value.kind == "endpoint"
    assert len(client.requests) == 2


def test_b03_a_read_timeout_is_abandoned_after_one_request(make_spec, limits, fake_client, make_timeout):
    """B03: A read timeout is abandoned after one request."""
    client = fake_client(make_timeout(read=True, sdk=anthropic), api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "go", [])
    with pytest.raises(RequestAbandoned):
        step(backend)
    assert len(client.requests) == 1
    entry = backend.info()["abandoned_request"]
    assert (entry["reason"], entry["usage"]) == ("read_timeout", "unknown")
    assert entry["prompt_bound_by"] == "none"  # the first request, and the entry names no window (B19)


@pytest.mark.parametrize("status", [401, 403])
def test_b09_a_rejected_key_gives_key_rejected_after_one_request(make_spec, limits, fake_client, make_error, status):
    """B09: A rejected key gives key rejected after one request."""
    client = fake_client(make_error(status, message="invalid x-api-key sk-ant-0123", sdk=anthropic), api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "go", [])
    with pytest.raises(InfraError) as caught:
        step(backend)
    assert caught.value.kind == "key_rejected"
    assert len(client.requests) == 1
    assert "sk-ant" not in f"{caught.value} {json.dumps(backend.info(), default=str)}"


def test_b13_a_prompt_too_long_is_a_context_overflow_and_not_retried(make_spec, limits, fake_client, make_error):
    """B13: A prompt too long is a context overflow and not retried."""
    client = fake_client(
        make_error(400, message="prompt is too long: 250000 tokens > 200000", sdk=anthropic), api="anthropic"
    )
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "go", [])
    with pytest.raises(ContextOverflow):
        step(backend)
    assert len(client.requests) == 1


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings")  # the test plants a value the SDK type forbids
def test_malformed_tool_input_is_not_run_as_an_empty_object_and_not_resent(make_spec, limits, message, fake_client):
    """A tool_use input that is not an object keeps its raw text and is resent inside a valid object."""
    blocks = [{"type": "tool_use", "id": "tu_1", "name": "run", "input": {"a": 1}}]
    reply = message(blocks, stop="tool_use")
    reply.content[0].input = "not-an-object"
    client = fake_client(reply, message(), api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "go", TOOLS)
    (call,) = step(backend).tool_calls
    assert call.arguments == {}
    assert call.parse_error == "arguments are not a JSON object"
    assert call.raw_arguments == '"not-an-object"'
    step(backend)
    assert client.requests[1]["messages"][1]["content"][0]["input"] == {"_raw": '"not-an-object"'}


def test_thinking_is_counted_per_reply(make_spec, limits, message, fake_client):
    client = fake_client(message([THINKING, {"type": "text", "text": "a"}]), message(), api="anthropic")
    backend = adapter(make_spec(backend="anthropic"), limits, client)
    backend.begin("", "go", [])
    step(backend)
    step(backend)
    thinking = backend.info()["thinking"]
    assert (thinking["calls"], thinking["calls_with_thinking"], thinking["thinking_tokens"]) == (2, 1, None)


def test_probe_sends_one_token_without_the_vendor_extras_and_counts_no_usage(make_spec, limits, message, fake_client):
    """B07 support: extended thinking needs a larger budget than one token, so the probe leaves extras out."""
    spec = make_spec(backend="anthropic", extra_body={"thinking": {"type": "enabled", "budget_tokens": 2048}})
    client = fake_client(message(), api="anthropic")
    backend = adapter(spec, limits, client)
    asyncio.run(backend.probe())
    (request,) = client.requests
    assert request["max_tokens"] == 1
    assert "extra_body" not in request
    assert "tools" not in request
    assert backend.info()["usage_seen"]["responses"] == 0


def test_b01_the_real_client_is_built_without_sdk_retries_and_with_a_read_timeout_past_the_time_cap(make_spec, limits):
    """B01: The real client is built without sdk retries and with a read timeout past the time cap."""
    backend = AnthropicAdapter(make_spec(backend="anthropic"), 1, limits, api_key="test-key")
    client = backend._client
    assert client.max_retries == 0
    assert client.timeout.read >= limits.time_cap_s + 60
    assert client.timeout.connect == 10.0
    assert str(client.base_url).startswith("https://api.example.invalid")
    asyncio.run(backend.aclose())
