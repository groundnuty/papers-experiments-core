"""The admission check: B07 (a listed model is not a servable model) and the admission half of B11."""

import asyncio
from types import SimpleNamespace

import pytest

from expcore.errors import ContextOverflow, InfraError, RequestAbandoned
from expcore.models.adapters import OpenAICompatAdapter, StubAdapter
from expcore.models.admission import admit


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
    client = fake_client(completion("x", usage=(3, 1)))
    backend = OpenAICompatAdapter(make_spec(), 1, limits, client=client, sleep=_no_sleep)
    assert admitted(backend) is None
    assert len(client.requests) == 1


def test_b07_a_rejected_key_is_a_reason_not_an_exception(make_spec, limits, fake_client, make_error):
    client = fake_client(make_error(401, message="bad key sk-live-9"))
    reason = admitted(OpenAICompatAdapter(make_spec(), 1, limits, client=client, sleep=_no_sleep))
    assert reason == "the one-token completion failed: key_rejected: HTTP 401 AuthenticationError"


def test_b07_an_outage_that_outlasts_the_retries_is_a_reason(make_spec, limits, fake_client, make_error):
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
    assert admitted(StubAdapter([], probe_error=error)) is not None


def test_b07_an_error_that_is_not_the_providers_is_a_bug_and_is_raised():
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
    spec = make_spec(context_tokens=4000, max_output_tokens=2000)
    client = fake_client(completion())
    assert admitted(OpenAICompatAdapter(spec, 1, limits, client=client, sleep=_no_sleep)) is None


def test_b11_a_backend_with_unknown_limits_is_not_refused_for_them():
    assert admitted(StubAdapter([])) is None
    assert admitted(StubAdapter([], output_limit=10, context_tokens=10)) is not None
