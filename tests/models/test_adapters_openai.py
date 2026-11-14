"""The OpenAI-compatible adapter against a fake client: B01-B05, B09-B15 as they show on the wire and in the record."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from expcore.errors import ContextOverflow, InfraError, RequestAbandoned
from expcore.models.adapters import OpenAICompatAdapter, ToolResult
from expcore.models.served import judge, requested_from_sent

TOOLS = [{"name": "run", "description": "run code", "input_schema": {"type": "object", "properties": {}}}]


def adapter(spec, limits, client, **kwargs):
    kwargs.setdefault("sleep", _no_sleep)
    return OpenAICompatAdapter(spec, 7, limits, client=client, **kwargs)


async def _no_sleep(seconds):
    return None


def step(backend):
    return asyncio.run(backend.step())


def test_a_turn_is_sent_with_the_model_seed_limit_and_tools_and_parsed(make_spec, limits, completion, fake_client):
    client = fake_client(completion("answer", usage=(120, 30), model="test-model", fingerprint="fp-1"))
    backend = adapter(make_spec(), limits, client)
    backend.begin("be brief", "hello", TOOLS)
    turn = step(backend)
    request = client.requests[0]
    assert request["model"] == "test-model"
    assert request["seed"] == 7
    assert request["max_tokens"] == 4096
    assert request["tool_choice"] == "auto"
    assert "extra_body" not in request  # an entry with no extras sends none
    assert "temperature" not in request  # and no sampling it did not set
    assert request["tools"][0]["function"] == {
        "name": "run",
        "description": "run code",
        "parameters": TOOLS[0]["input_schema"],
    }
    assert request["messages"] == [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hello"}]
    assert (turn.text, turn.input_tokens, turn.output_tokens, turn.stop_reason) == ("answer", 120, 30, "stop")
    assert (turn.served_model, turn.system_fingerprint, turn.retries) == ("test-model", "fp-1", ())
    assert turn.uncached_input_tokens is None
    assert turn.cache_read_input_tokens is None


def test_a_request_without_tools_carries_neither_tools_nor_tool_choice(make_spec, limits, completion, fake_client):
    client = fake_client(completion())
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "hello", [])
    step(backend)
    assert "tools" not in client.requests[0]
    assert "tool_choice" not in client.requests[0]
    assert client.requests[0]["messages"] == [{"role": "user", "content": "hello"}]


def test_tool_calls_are_parsed_and_their_results_go_back_as_tool_messages(make_spec, limits, completion, fake_client):
    client = fake_client(completion(None, finish="tool_calls", tool_calls=[("run", '{"code": "1+1"}')]), completion())
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", TOOLS)
    turn = step(backend)
    (call,) = turn.tool_calls
    assert (call.name, call.arguments, call.parse_error) == ("run", {"code": "1+1"}, None)
    backend.add_tool_results([ToolResult(call, "2")])
    step(backend)
    sent = client.requests[1]["messages"]
    assert sent[-1] == {"role": "tool", "tool_call_id": call.id, "content": "2"}
    assert sent[-2]["tool_calls"][0]["function"]["arguments"] == '{"code": "1+1"}'


def test_malformed_arguments_are_never_resent_the_history_holds_the_raw_text(
    make_spec, limits, completion, fake_client
):
    """Servers reject invalid JSON in history with HTTP 400, so the raw text rides inside a valid object."""
    client = fake_client(completion(None, finish="tool_calls", tool_calls=[("run", '{"a":')]), completion())
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", TOOLS)
    (call,) = step(backend).tool_calls
    assert call.arguments == {}
    assert call.raw_arguments == '{"a":'
    assert call.parse_error
    assert "not valid JSON" in call.parse_error
    step(backend)
    resent = client.requests[1]["messages"][-1]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(resent) == {"_raw": '{"a":'}


def test_b04_with_echo_on_the_next_request_carries_the_reasoning_field(make_spec, limits, completion, fake_client):
    """B04: the previous turn's reasoning goes back under the field the deployment expects."""
    client = fake_client(completion("a", reasoning_content="private thoughts"), completion("b"))
    backend = adapter(make_spec(echo_reasoning_field="reasoning_content"), limits, client)
    backend.begin("", "go", [])
    turn = step(backend)
    step(backend)
    assistant = client.requests[1]["messages"][-1]
    assert assistant["reasoning_content"] == "private thoughts"
    assert "_reasoning" not in assistant
    assert turn.reasoning == "private thoughts"


def test_b04_with_echo_off_the_wire_has_neither_the_field_nor_the_internal_copy_but_the_transcript_does(
    make_spec, limits, completion, fake_client
):
    """B04: echo is off by default; the reasoning is still kept in the transcript."""
    client = fake_client(completion("a", reasoning_content="private thoughts"), completion("b"))
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])
    step(backend)
    step(backend)
    assistant = client.requests[1]["messages"][-1]
    assert "reasoning_content" not in assistant
    assert "_reasoning" not in assistant
    assert "private thoughts" not in json.dumps(client.requests[1])
    assert backend.transcript()[-2]["_reasoning"] == "private thoughts"


def test_b04_the_reasoning_under_the_other_common_field_name_is_read_too(make_spec, limits, completion, fake_client):
    """B04: The reasoning under the other common field name is read too."""
    client = fake_client(completion("a", reasoning="thoughts"))
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])
    assert step(backend).reasoning == "thoughts"
    assert backend.info()["thinking"]["calls_with_thinking"] == 1


def test_b10_a_200_with_usage_and_no_choices_is_infrastructure_and_the_usage_still_counts(
    make_spec, limits, completion, fake_client
):
    """B10: tokens are counted on arrival, before parsing can fail."""
    client = fake_client(completion(choices=False, usage=(900, 0)))
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])
    with pytest.raises(InfraError, match="no choices") as caught:
        step(backend)
    assert caught.value.kind == "endpoint"
    assert backend.info()["usage_seen"] == {"responses": 1, "input": 900, "output": 0, "missing": 0}


def test_b10_a_200_without_usage_is_infrastructure_and_counts_as_missing_not_zero(
    make_spec, limits, completion, fake_client
):
    """B10: unreported usage is None, never 0, and a reply carrying none cannot be held to the token budget."""
    client = fake_client(completion(usage=None))
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])
    with pytest.raises(InfraError, match="no usage") as caught:
        step(backend)
    assert caught.value.kind == "endpoint"
    assert backend.info()["usage_seen"] == {"responses": 1, "input": None, "output": None, "missing": 1}


def test_b10_a_count_the_reply_leaves_out_is_none_on_the_turn(make_spec, limits, completion, fake_client):
    """B10: A count the reply leaves out is none on the turn."""
    client = fake_client(completion(usage=(50, None)))
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])
    turn = step(backend)
    assert (turn.input_tokens, turn.output_tokens) == (50, None)
    assert backend.info()["usage_seen"]["output"] is None


def test_b10_usage_adds_up_over_replies(make_spec, limits, completion, fake_client):
    """B10: Usage adds up over replies."""
    client = fake_client(completion(usage=(10, 2)), completion(usage=(30, 4)))
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])
    step(backend)
    step(backend)
    assert backend.info()["usage_seen"] == {"responses": 2, "input": 40, "output": 6, "missing": 0}


def test_b10_an_endpoints_own_numeric_usage_fields_ride_on_the_turn_under_their_own_names(
    make_spec, limits, completion, fake_client
):
    """B10: a credit count and the reasoning and cache counts an endpoint reports are kept per turn, recorded only:
    the two counts the budget uses are unchanged and the counts the turn already holds are not repeated."""
    extra = {
        "used_credits": 3.5,
        "completion_tokens_details": {"reasoning_tokens": 12},
        "prompt_tokens_details": {"cached_tokens": 40},
    }
    client = fake_client(completion("a", usage=(120, 30), usage_extra=extra))
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])
    turn = step(backend)
    assert turn.usage_extra == {
        "completion_tokens_details.reasoning_tokens": 12,
        "prompt_tokens_details.cached_tokens": 40,
        "used_credits": 3.5,
    }
    assert turn.usage_extra_cut == 0
    assert (turn.input_tokens, turn.output_tokens) == (120, 30)
    assert backend.info()["usage_seen"] == {"responses": 1, "input": 120, "output": 30, "missing": 0}


def test_b10_a_field_the_reply_does_not_carry_is_absent_never_zero(make_spec, limits, completion, fake_client):
    """B10: a reply with no extra field has an empty mapping, and a later reply without the field does not get a 0."""
    client = fake_client(completion(usage=(10, 2), usage_extra={"used_credits": 1.5}), completion(usage=(10, 2)))
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])
    first, second = step(backend), step(backend)
    assert first.usage_extra == {"used_credits": 1.5}
    assert second.usage_extra == {}
    assert second.usage_extra_cut == 0


def test_b10_a_usage_that_is_a_plain_object_is_read_by_its_attributes(make_spec, limits, completion, fake_client):
    """B10: a client that hands back a plain object instead of the SDK's model gives its numeric attributes too, and
    one that lacks a count invents none."""
    reply = completion()
    reply.usage = SimpleNamespace(prompt_tokens=50, completion_tokens=None, used_credits=2.5, tier="gold")
    backend = adapter(make_spec(), limits, fake_client(reply))
    backend.begin("", "go", [])
    turn = step(backend)
    assert (turn.input_tokens, turn.output_tokens) == (50, None)
    assert turn.usage_extra == {"used_credits": 2.5}


@pytest.mark.parametrize(
    "value",
    ["gold", True, None, [1, 2], float("nan"), float("inf"), {"label": "x"}],
    ids=["text", "bool", "null", "list", "nan", "inf", "object_of_text"],
)
def test_b10_a_field_that_is_not_a_number_is_left_out(make_spec, limits, completion, fake_client, value):
    """B10: only numbers are kept: text, a flag, null, a list, a non-finite float and an object with no number in it
    are left out, and a number beside them is kept."""
    client = fake_client(completion(usage=(10, 2), usage_extra={"odd_field": value, "used_credits": 2}))
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])
    turn = step(backend)
    assert turn.usage_extra == {"used_credits": 2}
    assert turn.usage_extra_cut == 0


def test_b10_more_fields_than_the_cap_keep_the_first_by_name_and_count_the_rest_as_cut(
    make_spec, limits, completion, fake_client
):
    """B10: a reply that reports more numeric fields than the cap cannot grow the record: the first by name are kept
    and the number left out is recorded."""
    from expcore.models import usage as rules

    many = {f"field_{i:03d}": i for i in range(rules.USAGE_EXTRA_CAP + 5)}
    client = fake_client(completion(usage=(10, 2), usage_extra=dict(reversed(many.items()))))
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])
    turn = step(backend)
    assert list(turn.usage_extra) == sorted(many)[: rules.USAGE_EXTRA_CAP]
    assert turn.usage_extra_cut == 5


def test_b11_a_model_with_a_lower_maximum_sends_its_own_value(make_spec, limits, completion, fake_client):
    """B11: max_tokens is the study's value or the model's own lower maximum."""
    client = fake_client(completion())
    backend = adapter(make_spec(max_output_tokens=1024), limits, client)
    backend.begin("", "go", [])
    step(backend)
    assert client.requests[0]["max_tokens"] == 1024
    assert backend.output_limit == 1024


def test_b11_the_study_value_wins_when_it_is_the_lower(make_spec, limits, completion, fake_client):
    """B11: The study value wins when it is the lower."""
    client = fake_client(completion())
    backend = adapter(make_spec(max_output_tokens=100_000), limits, client)
    backend.begin("", "go", [])
    step(backend)
    assert client.requests[0]["max_tokens"] == 4096


def test_b12_a_reply_cut_at_the_output_limit_is_recorded_as_such(make_spec, limits, completion, fake_client):
    """B12: finish_reason length is recorded and marks the turn as cut."""
    client = fake_client(completion("cut off mid-", finish="length"), completion())
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])
    cut = step(backend)
    assert (cut.stop_reason, cut.hit_output_limit) == ("length", True)
    assert step(backend).hit_output_limit is False


def test_b14_vendor_extras_reach_the_request_body_and_sampling_keys_beyond_temperature_ride_along(
    make_spec, limits, completion, fake_client
):
    """B14: extra_body passes through (a provider pin without fallback), merged with the non-direct sampling keys."""
    pin = {"provider": {"order": ["alpha"], "allow_fallbacks": False}}
    spec = make_spec(sampling={"temperature": 0.3, "top_k": 20}, extra_body={**pin, "chat_template_kwargs": {"x": 1}})
    client = fake_client(completion())
    backend = adapter(spec, limits, client)
    backend.begin("", "go", [])
    step(backend)
    request = client.requests[0]
    assert request["temperature"] == 0.3
    assert request["extra_body"] == {"top_k": 20, **pin, "chat_template_kwargs": {"x": 1}}
    assert "top_k" not in request


def test_b15_the_recorded_settings_come_from_the_request_that_was_sent(make_spec, limits, completion, fake_client):
    """B15: the recorded temperature equals the request's; the conversation is not part of the settings."""
    spec = make_spec(sampling={"temperature": 0.3, "top_p": 0.9}, extra_body={"provider": {"order": ["alpha"]}})
    client = fake_client(completion())
    backend = adapter(spec, limits, client)
    backend.begin("system text", "user text", TOOLS)
    step(backend)
    recorded = backend.info()["settings_sent"]
    sent = client.requests[0]
    assert recorded["temperature"] == sent["temperature"] == 0.3
    assert recorded["top_p"] == sent["top_p"]
    assert recorded["extra_body"] == sent["extra_body"]
    assert recorded["max_tokens"] == sent["max_tokens"]
    assert recorded["seed"] == 7
    assert not {"messages", "tools", "system"} & recorded.keys()
    assert "user text" not in json.dumps(backend.info())


def test_b15_a_base_url_with_credentials_is_recorded_without_them_and_the_override_is_the_url_in_the_record(
    make_spec, limits, fake_client
):
    """B15 and C14: the endpoint a campaign admitted (an override) is the one recorded, with no credentials in it."""
    backend = adapter(make_spec(), limits, fake_client(), base_url="https://user:pw@other.example.invalid:8443/v1?k=1")
    info = backend.info()
    assert info["base_url"] == "https://other.example.invalid:8443/v1"
    assert "pw" not in json.dumps(info)
    assert adapter(make_spec(), limits, fake_client()).info()["base_url"] == "https://llm.example.invalid/v1"


def test_the_record_names_the_route_and_the_sdk(make_spec, limits, fake_client):
    info = adapter(make_spec(), limits, fake_client()).info()
    assert (info["backend"], info["model"], info["seed"], info["seed_honoured"]) == (
        "openai_compat",
        "test-model",
        7,
        True,
    )
    assert info["sdk_versions"]["openai"]


def test_b01_transient_errors_are_retried_and_each_retry_is_recorded_on_the_turn(
    make_spec, limits, completion, fake_client, make_error
):
    """B01: two 429s then an answer give one turn with two recorded retries; the SDK's own retries are off."""
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    errors = [make_error(429, headers={"retry-after": "30"}), make_error(429, headers={"retry-after": "30"})]
    client = fake_client(*errors, completion("finally"))
    backend = adapter(make_spec(), limits, client, sleep=sleep)
    backend.begin("", "go", [])
    turn = step(backend)
    assert turn.text == "finally"
    assert [r["status"] for r in turn.retries] == [429, 429]
    assert len(client.requests) == 3
    assert all(wait >= 30 for wait in waits)
    info = backend.info()
    assert (info["retries"], info["rate_limited_429"]) == (2, 2)
    assert [e["retry"] for e in info["retry_events"]] == [1, 2]


def test_c31_the_backend_counts_every_request_it_started_resends_included(
    make_spec, limits, completion, fake_client, make_error
):
    """C31: `requests_sent` is what the scheduler reads to tell an attempt that sent nothing: none before a step, and a
    resend after a failure counts as another request."""

    async def sleep(seconds):
        pass

    client = fake_client(make_error(429, headers={"retry-after": "1"}), completion("finally"))
    backend = adapter(make_spec(), limits, client, sleep=sleep)
    backend.begin("", "go", [])
    assert backend.requests_sent == 0
    step(backend)
    assert backend.requests_sent == 2


def test_b02_retries_that_run_out_end_as_infrastructure_endpoint(make_spec, limits, fake_client, make_error):
    """B02: N+1 transient errors raise InfraError endpoint, which the loop records unscored."""
    from expcore.config.models import ClientSettings

    client = fake_client(*[make_error(503) for _ in range(5)])
    backend = adapter(make_spec(), limits, client, client_cfg=ClientSettings(max_retries=2))
    backend.begin("", "go", [])
    with pytest.raises(InfraError) as caught:
        step(backend)
    assert caught.value.kind == "endpoint"
    assert len(client.requests) == 3


def test_b03_a_read_timeout_is_abandoned_after_one_request_with_usage_unknown(
    make_spec, limits, fake_client, make_timeout
):
    """B03: the request that went out is never resent; the record says the abandoned request's usage is unknown."""
    client = fake_client(make_timeout(read=True))
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])
    with pytest.raises(RequestAbandoned):
        step(backend)
    assert len(client.requests) == 1
    info = backend.info()
    entry = info["abandoned_request"]
    assert (entry["reason"], entry["usage"]) == ("read_timeout", "unknown")
    assert entry["prompt_bound_by"] == "none"  # the first request, and the entry names no window (B19)
    assert info["read_timeouts"] == 1


def test_b03_the_run_deadline_cancelling_a_request_is_recorded_as_abandoned(make_spec, limits, fake_client):
    """B03: The run deadline cancelling a request is recorded as abandoned."""

    async def hang(**kw):
        await asyncio.sleep(60)

    client = fake_client()
    client.chat.completions.create = hang
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])

    async def run():
        await asyncio.wait_for(backend.step(), timeout=0.05)

    with pytest.raises(TimeoutError):
        asyncio.run(run())
    entry = backend.info()["abandoned_request"]
    assert (entry["reason"], entry["usage"]) == ("deadline", "unknown")
    assert entry["prompt_bound_by"] == "none"  # the first request, and the entry names no window (B19)


@pytest.mark.parametrize("status", [401, 403])
def test_b09_a_rejected_key_gives_key_rejected_after_one_request_and_no_sdk_text_in_the_record(
    make_spec, limits, fake_client, make_error, status
):
    """B09: the SDK's text can echo the key, so the record holds the status and class only."""
    client = fake_client(make_error(status, message="Incorrect API key provided: sk-live-0123456789"))
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])
    with pytest.raises(InfraError) as caught:
        step(backend)
    assert caught.value.kind == "key_rejected"
    assert len(client.requests) == 1
    assert "sk-live" not in f"{caught.value} {json.dumps(backend.info(), default=str)}"


def test_b13_a_context_overflow_is_raised_once_and_not_retried(make_spec, limits, fake_client, make_error):
    """B13: a 400 naming the context is the agent's outcome."""
    client = fake_client(make_error(400, message="This model's maximum context length is 8192 tokens."))
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])
    with pytest.raises(ContextOverflow):
        step(backend)
    assert len(client.requests) == 1


def test_b06_what_the_replies_reported_is_in_the_record(make_spec, limits, completion, fake_client):
    """B06: What the replies reported is in the record."""
    client = fake_client(completion(model="test-model-20261001", fingerprint="fp-1", usage=(1, 1)))
    backend = adapter(make_spec(), limits, client)
    backend.begin("", "go", [])
    step(backend)
    info = backend.info()
    assert info["served_model_ids"] == ["test-model-20261001"]
    assert info["system_fingerprints"] == ["fp-1"]
    assert info["served_settings"]["temperature"] == "not reported"


def test_b06_the_adapter_tells_the_check_what_it_sent_so_an_echo_of_it_is_judged(
    make_spec, limits, completion, fake_client
):
    """B06: seed and max_tokens echoed back are no silent default; a changed vendor extra is a mismatch."""
    spec = make_spec(sampling={"temperature": 0.2}, extra_body={"preserve_thinking": True})
    reply = completion(usage=(1, 1))
    reply.seed, reply.max_tokens, reply.temperature = 7, 4096, 0.2
    reply.preserve_thinking, reply.request_id = False, "req-1"
    backend = adapter(spec, limits, fake_client(reply))
    backend.begin("", "go", [])
    step(backend)
    info = backend.info()
    assert info["served_settings"]["preserve_thinking"] is False
    assert info["other_reply_fields"] == {"request_id": {"first": "req-1", "replies": 1}}
    requested = requested_from_sent(info["settings_sent"])
    result = judge(info["served_model_ids"], info["served_settings"], "test-model", requested)
    assert result["state"] == "mismatch"
    assert result["reasons"] == ["the server reports preserve_thinking=False, requested True"]


def test_probe_sends_one_token_with_the_vendor_extras_and_counts_no_usage(make_spec, limits, completion, fake_client):
    """B07 support: the one-token completion uses the model id and the provider pin and leaves usage alone."""
    spec = make_spec(extra_body={"provider": {"order": ["alpha"], "allow_fallbacks": False}})
    client = fake_client(completion(usage=(5, 1)))
    backend = adapter(spec, limits, client)
    asyncio.run(backend.probe())
    (request,) = client.requests
    assert request["max_tokens"] == 1
    assert request["model"] == "test-model"
    assert request["extra_body"]["provider"]["allow_fallbacks"] is False
    assert "tools" not in request
    assert backend.info()["usage_seen"]["responses"] == 0


def test_probe_with_a_reply_that_has_no_choices_fails(make_spec, limits, completion, fake_client):
    backend = adapter(make_spec(), limits, fake_client(completion(choices=False)))
    with pytest.raises(InfraError, match="no choices"):
        asyncio.run(backend.probe())


def test_aclose_closes_the_client(make_spec, limits, fake_client):
    client = fake_client()
    asyncio.run(adapter(make_spec(), limits, client).aclose())
    assert client.closed


def test_b01_the_real_client_is_built_without_sdk_retries_and_with_a_read_timeout_past_the_time_cap(make_spec, limits):
    """B01 and B03: the SDK's own retries are off, and the read timeout never fires before the run's time is gone."""
    backend = OpenAICompatAdapter(make_spec(), 1, limits, api_key="test-key")
    client = backend._client
    assert client.max_retries == 0
    assert client.timeout.read >= limits.time_cap_s + 60
    assert client.timeout.connect == 10.0
    assert str(client.base_url).startswith("https://llm.example.invalid/v1")
    asyncio.run(backend.aclose())
