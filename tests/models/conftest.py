"""Fixtures for the model-layer tests: SDK-shaped errors and replies, fake clients, a recording sleep. No network."""

import copy
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest
from anthropic.types import Message
from openai.types.chat import ChatCompletion

from expcore.config.models import Limits, ModelSpec

URL = "https://llm.example.invalid/v1/chat/completions"


class Sleeps:
    """Stands in for asyncio.sleep: records each wait and returns at once."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


class FixedJitter:
    """Stands in for random.Random: every draw is the same share of the allowed range."""

    def __init__(self, share: float = 0.0) -> None:
        self.share = share

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.share


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def jitter():
    return FixedJitter


def _status_class(sdk, status: int):
    named = {
        400: "BadRequestError",
        401: "AuthenticationError",
        403: "PermissionDeniedError",
        404: "NotFoundError",
        409: "ConflictError",
        422: "UnprocessableEntityError",
        429: "RateLimitError",
    }
    if status in named:
        return getattr(sdk, named[status])
    return sdk.InternalServerError if status >= 500 else sdk.APIStatusError


@pytest.fixture
def make_error():
    """Return a builder of SDK errors: make_error(status, headers=..., message=..., body=..., sdk=openai)."""

    def build(status, *, headers=None, message="the provider said no", body=None, sdk=openai):
        request = httpx.Request("POST", URL)
        response = httpx.Response(status, headers=headers or {}, request=request)
        return _status_class(sdk, status)(message, response=response, body=body)

    return build


@pytest.fixture
def make_timeout():
    """Return a builder of SDK timeouts: read=True is a timeout after the request went out, else a connect timeout."""

    def build(*, read, sdk=openai):
        error = sdk.APITimeoutError(request=httpx.Request("POST", URL))
        error.__cause__ = httpx.ReadTimeout("slow") if read else httpx.ConnectTimeout("no connection")
        return error

    return build


@pytest.fixture
def make_connection_error():
    def build(*, sdk=openai):
        return sdk.APIConnectionError(request=httpx.Request("POST", URL))

    return build


@pytest.fixture(params=[openai, anthropic], ids=["openai", "anthropic"])
def sdk(request):
    """Run a test against both SDKs' error classes."""
    return request.param


@pytest.fixture
def limits():
    return Limits(
        max_tool_rounds=10, time_cap_s=300.0, token_budget=100_000, output_cap_chars=8000, max_output_tokens=4096
    )


@pytest.fixture
def make_spec():
    """Return a builder of model entries; the defaults describe a frozen OpenAI-compatible model on a .invalid host."""

    def build(**overrides):
        base = {
            "key": "test-model",
            "backend": "openai_compat",
            "api_id": "test-model",
            "status": "frozen",
            "base_url": "https://llm.example.invalid/v1",
        }
        if overrides.get("backend") == "anthropic":
            base.update(api_id="test-claude", key_env="TEST_CLAUDE_KEY", base_url="https://api.example.invalid")
        return ModelSpec(**{**base, **overrides})

    return build


@pytest.fixture
def completion():
    """Return a builder of OpenAI chat completions (the SDK's own response type)."""

    def build(
        content="hello",
        *,
        finish="stop",
        usage=(10, 5),
        tool_calls=None,
        model="test-model",
        fingerprint=None,
        choices=True,
        **message_extra,
    ):
        message = {"role": "assistant", "content": content, **message_extra}
        if tool_calls:
            message["tool_calls"] = [
                {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": arguments}}
                for i, (name, arguments) in enumerate(tool_calls)
            ]
        body = {
            "id": "cmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "system_fingerprint": fingerprint,
            "choices": [{"index": 0, "finish_reason": finish, "message": message}] if choices else [],
        }
        if usage is not None:
            prompt, out = usage
            body["usage"] = {
                "prompt_tokens": prompt or 0,
                "completion_tokens": out or 0,
                "total_tokens": (prompt or 0) + (out or 0),
            }
        reply = ChatCompletion.model_validate(body)
        if usage is not None and None in usage:
            # the SDK type wants integers; a gateway that leaves a count out is modelled by dropping it afterwards
            reply.usage = SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1])
        return reply

    return build


@pytest.fixture
def message():
    """Return a builder of Anthropic messages (the SDK's own response type)."""

    def build(content=None, *, stop="end_turn", usage=None, model="test-claude"):
        blocks = [{"type": "text", "text": "hello"}] if content is None else content
        counts = {"input_tokens": 7, "output_tokens": 3} if usage is None else usage
        return Message.model_validate(
            {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "model": model,
                "stop_reason": stop,
                "stop_sequence": None,
                "content": blocks,
                "usage": counts,
            }
        )

    return build


class FakeClient:
    """Stands in for an SDK client: records a deep copy of every request body, answers or raises in order."""

    def __init__(self, *outcomes, api):
        self.requests: list[dict] = []
        self.outcomes = list(outcomes)
        self.closed = False
        endpoint = SimpleNamespace(create=self._create)
        if api == "openai":
            self.chat = SimpleNamespace(completions=endpoint)
        else:
            self.messages = endpoint

    async def _create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_client():
    """Return a builder: fake_client(reply, error, ..., api="openai" or "anthropic")."""

    def build(*outcomes, api="openai"):
        return FakeClient(*outcomes, api=api)

    return build
