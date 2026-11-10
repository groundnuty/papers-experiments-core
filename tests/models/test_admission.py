"""The admission check: B07 (a listed model is not a servable model) and the admission half of B11."""

import asyncio
from types import SimpleNamespace

import anthropic
import pytest

from expcore.errors import ContextOverflow, InfraError, RequestAbandoned
from expcore.models.adapters import AnthropicAdapter, OpenAICompatAdapter, ProbeReply, StubAdapter
from expcore.models.admission import admit, reply_cap_unshown
from expcore.records.recording import RecordingBackend


async def _no_sleep(seconds):
    return None


def admitted(backend):
    return asyncio.run(admit(backend))


def test_b07_a_model_that_is_listed_but_answers_400_on_completion_is_rejected(
    make_spec, limits, fake_client, make_error
):
    """B07: the listing says the id exists; the first completion says it is not servable. Only the completion counts."""
    listings = []
    client = fake_client(make_error(400, message="The model `test-model` is not active for this key"))
    client.models = SimpleNamespace(list=lambda **kw: listings.append(kw) or ["test-model"])
    backend = OpenAICompatAdapter(make_spec(), 1, limits, client=client, sleep=_no_sleep)
    reason = admitted(backend)
    assert reason is not None
    assert "refused the one-token completion" in reason
    assert "HTTP 400" in reason
    assert "not active" in reason
    assert len(client.requests) == 1
    assert client.requests[0]["max_tokens"] == 1
    assert listings == []  # the listing is never consulted


def test_b07_a_model_whose_one_token_completion_succeeds_is_admitted(make_spec, limits, fake_client, completion):
    """B07: A model whose one token completion succeeds is admitted."""
    client = fake_client(completion("x", usage=(3, 1)))
    backend = OpenAICompatAdapter(make_spec(), 1, limits, client=client, sleep=_no_sleep)
    assert admitted(backend) is None
    assert len(client.requests) == 1


def test_b07_a_rejected_key_is_a_reason_not_an_exception(make_spec, limits, fake_client, make_error):
    """B07: A rejected key is a reason not an exception."""
    client = fake_client(make_error(401, message="bad key sk-live-9"))
    reason = admitted(OpenAICompatAdapter(make_spec(), 1, limits, client=client, sleep=_no_sleep))
    assert reason == "the one-token completion failed: key_rejected: HTTP 401 AuthenticationError"


def test_b07_an_outage_that_outlasts_the_retries_is_a_reason(make_spec, limits, fake_client, make_error):
    """B07: An outage that outlasts the retries is a reason."""
    from expcore.config.models import ClientSettings

    client = fake_client(*[make_error(503) for _ in range(3)])
    backend = OpenAICompatAdapter(
        make_spec(), 1, limits, client=client, sleep=_no_sleep, client_cfg=ClientSettings(max_retries=2)
    )
    reason = admitted(backend)
    assert reason is not None
    assert reason.startswith("the one-token completion failed: endpoint:")


def test_b07_a_slow_completion_and_a_context_error_are_reasons(
    make_spec, limits, fake_client, make_timeout, make_error
):
    """B07: A slow completion and a context error are reasons."""
    slow = OpenAICompatAdapter(make_spec(), 1, limits, client=fake_client(make_timeout(read=True)), sleep=_no_sleep)
    assert admitted(slow) == "the one-token completion was sent and not answered in time"
    overflow = fake_client(make_error(400, message="This model's maximum context length is 8 tokens."))
    reason = admitted(OpenAICompatAdapter(make_spec(), 1, limits, client=overflow, sleep=_no_sleep))
    assert reason == "the one-token completion overflowed the context (maximum_context_length)"


@pytest.mark.parametrize(
    "error",
    [InfraError("endpoint", "down"), RequestAbandoned("slow"), ContextOverflow(400, "code")],
)
def test_b07_any_backend_may_fail_its_probe_with_the_core_errors(error):
    """B07: Any backend may fail its probe with the core errors."""
    assert admitted(StubAdapter([], probe_error=error)) is not None


def test_b07_an_error_that_is_not_the_providers_is_a_bug_and_is_raised():
    """B07: An error that is not the providers is a bug and is raised."""
    with pytest.raises(ZeroDivisionError):
        admitted(StubAdapter([], probe_error=ZeroDivisionError("harness bug")))


def test_b11_an_output_limit_over_the_endpoints_context_fails_admission_before_any_request(
    make_spec, limits, fake_client
):
    """B11: the limit is checked against the context window the table gives; nothing is sent."""
    client = fake_client()
    spec = make_spec(context_tokens=4000)  # the study's limit is 4096
    reason = admitted(OpenAICompatAdapter(spec, 1, limits, client=client, sleep=_no_sleep))
    assert reason == "the output limit of 4096 tokens does not fit the endpoint's context of 4000 tokens"
    assert client.requests == []


def test_b11_the_models_own_lower_maximum_is_what_is_checked(make_spec, limits, fake_client, completion):
    """B11: The models own lower maximum is what is checked."""
    spec = make_spec(context_tokens=4000, max_output_tokens=2000)
    client = fake_client(completion())
    assert admitted(OpenAICompatAdapter(spec, 1, limits, client=client, sleep=_no_sleep)) is None


def test_b11_a_backend_with_unknown_limits_is_not_refused_for_them():
    """B11: A backend with unknown limits is not refused for them."""
    assert admitted(StubAdapter([])) is None
    assert admitted(StubAdapter([], output_limit=10, context_tokens=10)) is not None


@pytest.mark.parametrize(
    ("usage", "missing"),
    [(None, "no input and no output"), ((None, 1), "no input"), ((3, None), "no output")],
)
def test_b07_an_endpoint_whose_one_token_completion_omits_usage_is_not_admitted(
    make_spec, limits, fake_client, completion, usage, missing
):
    """B07: the token budget is held from the counts each reply reports, so a model whose endpoint leaves them out
    would be the one model the cap does not bind; its one-token completion says so, and it is not admitted."""
    backend = OpenAICompatAdapter(make_spec(), 1, limits, client=fake_client(completion("x", usage=usage)))
    reason = admitted(backend)
    assert reason is not None
    assert reason.startswith(f"the one-token completion reported {missing} token count")
    assert backend.info()["usage_seen"]["responses"] == 0  # read, never tallied: the probe is no part of a run


def test_b07_an_anthropic_completion_counts_its_prompt_with_its_cached_parts_and_must_report_both_counts(
    make_spec, limits, fake_client, message
):
    """B07: the Messages API's input_tokens is only the uncached remainder; the whole prompt is what the probe reads,
    and a reply without its output count is not admitted."""
    spec = make_spec(backend="anthropic")
    usage = {"input_tokens": 2, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0, "output_tokens": 1}
    client = fake_client(message(usage=usage), api="anthropic")
    backend = AnthropicAdapter(spec, 1, limits, client=client, api_key="test-key", sleep=_no_sleep)
    assert asyncio.run(backend.probe()) == ProbeReply(1, 7, 1)
    silent = fake_client(message(usage={"input_tokens": 2, "output_tokens": 1}), api="anthropic")
    silent.outcomes[0].usage.output_tokens = None
    backend = AnthropicAdapter(spec, 1, limits, client=silent, api_key="test-key", sleep=_no_sleep)
    assert admitted(backend).startswith("the one-token completion reported no output token count")


def test_b07_a_backend_whose_probe_returns_nothing_to_read_cannot_show_its_usage_and_is_not_admitted():
    """B07: a backend of a study's own whose probe returns None says nothing of the usage its endpoint reports, so it
    is refused as one that omits it; a backend that wraps a core one and passes its probe's reply on is admitted."""

    class Silent(StubAdapter):
        async def probe(self):
            await super().probe()

    assert admitted(Silent([])) == (
        "the backend's probe returned no reply to read, so it cannot show that the endpoint reports usage"
    )
    assert admitted(RecordingBackend(StubAdapter([]))) is None


def cap_probe(backend):
    return asyncio.run(reply_cap_unshown(backend))


@pytest.mark.parametrize(
    ("reply", "why"),
    [
        ({"usage": (20, 16), "finish": "length"}, None),
        ({"usage": (20, 300), "finish": "length"}, "reported 300 output tokens for a max_tokens of 16"),
        ({"usage": (20, 9), "finish": "stop"}, "stopped by itself (stop) after 9 tokens"),
        ({"usage": (20, None), "finish": "length"}, "reported no output token count"),
    ],
)
def test_c27_the_probe_of_the_reply_maximum_shows_it_held_only_by_a_reply_cut_at_a_small_max_tokens(
    make_spec, limits, fake_client, completion, reply, why
):
    """C27: a request for a long reply under max_tokens 16, the vendor extras with it, must come back stopped at the
    limit with an output count within it; a longer reply, one that stopped by itself, or one without the count do not
    show it."""
    spec = make_spec(extra_body={"provider": {"order": ["alpha"]}})
    client = fake_client(completion("1\n2\n3", usage=reply["usage"], finish=reply["finish"]))
    result = cap_probe(OpenAICompatAdapter(spec, 1, limits, client=client, sleep=_no_sleep))
    assert result is None if why is None else why in result
    (request,) = client.requests
    assert request["max_tokens"] == 16
    assert request["extra_body"]["provider"] == {"order": ["alpha"]}
    assert "Count from 1 to 500" in request["messages"][0]["content"]


def test_c27_the_anthropic_probe_of_the_reply_maximum_reads_its_stop_reason_and_a_refusal_is_a_reason(
    make_spec, limits, fake_client, message, make_error
):
    """C27: the Messages API says `max_tokens` when it cut the reply; an endpoint that refuses the probe shows nothing,
    and says why, while the model is still admitted on its one-token completion."""
    spec = make_spec(backend="anthropic")
    cut = fake_client(message(stop="max_tokens", usage={"input_tokens": 20, "output_tokens": 16}), api="anthropic")
    assert cap_probe(AnthropicAdapter(spec, 1, limits, client=cut, api_key="test-key", sleep=_no_sleep)) is None
    assert cut.requests[0]["max_tokens"] == 16
    assert "extra_body" not in cut.requests[0]
    refused = fake_client(make_error(400, message="max_tokens too small", sdk=anthropic), api="anthropic")
    reason = cap_probe(AnthropicAdapter(spec, 1, limits, client=refused, api_key="test-key", sleep=_no_sleep))
    assert reason.startswith("the endpoint refused the probe of the reply maximum: BadRequestError HTTP 400")
