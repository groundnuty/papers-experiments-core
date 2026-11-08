"""Fixtures for the model-layer tests: SDK-shaped errors, a recording sleep and a fixed jitter. No network."""

import anthropic
import httpx
import openai
import pytest

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
