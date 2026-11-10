"""A run never costs more than its reservation and its top-ups together (C26), over random runs of the real loop.

Each run drives the real loop, the real recording, the real OpenAI-compatible adapter and its retrier against a fake
endpoint that bills every send it is sent: replies of random sizes, some leaving a count out, and failures that may
or may not have been billed (5xx, connection errors) or were refused before any work (429). The attempt's guard is
the real one, over a ledger that keeps what it was charged and refuses some top-ups. What the endpoint billed is the
run's true cost.
"""

import asyncio
import dataclasses
import random

import httpx
import openai
import pytest
from loop_support import FakeClock, FakeProvider, never
from openai.types.chat import ChatCompletion
from record_support import make_config

from expcore.campaign.attempt_bounds import bound_usd, request_bound_usd
from expcore.campaign.topup import AttemptSpend
from expcore.config.models import ClientSettings, Limits, ModelSpec
from expcore.config.prices import Price, PriceTier, TieredPrice
from expcore.loop.agent_loop import run_agent
from expcore.loop.toolbox import ToolBox
from expcore.models import spend
from expcore.models.adapters import OpenAICompatAdapter
from expcore.records.recording import RecordingBackend

URL = "https://llm.example.invalid/v1/chat/completions"


class Tally:
    """A ledger that charges every top-up its bound, refuses some reservations, and keeps the total charged."""

    def __init__(self, usd: float, rng: random.Random, refuse_share: float):
        self.usd, self.rng, self.refuse_share, self.charged = usd, rng, refuse_share, 0.0

    def reserve(self, attempt_id, run_key, model, why) -> str | None:
        if self.rng.random() < self.refuse_share:
            return "no room left"
        self.charged += self.usd
        return None

    def charge(self, attempt_id, run_key, model, why) -> None:
        self.charged += self.usd

    def note(self, model, kind, detail) -> None:
        return None


class Endpoint:
    """A fake chat-completions client that bills each send and answers, fails, or leaves a count out at random.

    `reply_cap` is what the endpoint holds a reply to: the max_tokens it is sent when it enforces it, else nothing but
    its window, prompt and reply together.
    """

    def __init__(self, rng: random.Random, spec: ModelSpec, reply_cap: int | None):
        self.rng, self.spec, self.reply_cap, self.billed = rng, spec, reply_cap, 0.0
        self.chat = type("Chat", (), {"completions": type("Completions", (), {"create": self.create})()})()

    def _draw(self, top: int) -> int:
        return self.rng.choice([0, top, top, self.rng.randint(0, top)])

    def _bill(self) -> tuple[int, int]:
        window = self.spec.context_tokens
        prompt = self._draw(window)
        reply = self._draw(self.reply_cap if self.reply_cap is not None else window - prompt)
        written = self.rng.randint(0, prompt)
        self.billed += self.spec.price.request_usd(prompt, reply, written, self.rng.randint(0, prompt - written))
        return prompt, reply

    async def create(self, **request):
        roll = self.rng.random()
        if roll < 0.04:
            raise openai.RateLimitError("slow down", response=httpx.Response(429, request=_request()), body=None)
        if roll < 0.18:  # a failure after the request may have been billed, or before it was
            if self.rng.random() < 0.7:
                self._bill()
            if self.rng.random() < 0.5:
                raise openai.InternalServerError("busy", response=httpx.Response(503, request=_request()), body=None)
            raise openai.APIConnectionError(request=_request())
        prompt, reply = self._bill()
        counts = [prompt, reply]
        if self.rng.random() < 0.12:  # a reply that leaves one count out, or (rarely) both
            counts[self.rng.randrange(2)] = None
            if self.rng.random() < 0.15:
                counts = [None, None]
        calls = [("look", "{}")] if self.rng.random() < 0.85 else None
        return _completion(counts, calls)


def _request() -> httpx.Request:
    return httpx.Request("POST", URL)


def _completion(counts: list[int | None], calls) -> ChatCompletion:
    message = {"role": "assistant", "content": None if calls else "the answer"}
    if calls:
        message["tool_calls"] = [
            {"id": f"call_{k}", "type": "function", "function": {"name": name, "arguments": arguments}}
            for k, (name, arguments) in enumerate(calls)
        ]
    finish = "tool_calls" if calls else "stop"
    reply = ChatCompletion.model_validate(
        {
            "id": "cmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "vendor/quince-1",
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    )
    reply.usage = type("Usage", (), {"prompt_tokens": counts[0], "completion_tokens": counts[1]})()
    return reply


def random_price(rng: random.Random) -> Price | TieredPrice:
    def flat() -> Price:
        cache = [rng.choice([None, rng.uniform(0, 8)]) for _ in range(2)]
        return Price(rng.uniform(0, 8), rng.uniform(0, 8), *cache)

    if rng.random() < 0.5:
        return flat()
    bounds = sorted(rng.sample(range(1, 60_000), rng.randint(1, 2)))
    return TieredPrice((*(PriceTier(b, flat()) for b in bounds), PriceTier(None, flat())))


def random_case(rng: random.Random) -> tuple[ModelSpec, Limits]:
    limits = Limits(
        max_tool_rounds=rng.randint(0, 12),
        time_cap_s=300.0,
        token_budget=rng.randint(1, 400_000),
        output_cap_chars=200,
        max_output_tokens=rng.randint(1, 16_000),
    )
    spec = ModelSpec(
        key="quince",
        backend="openai_compat",
        api_id="vendor/quince-1",
        status="frozen",
        base_url="https://llm.example.invalid/v1",
        price=random_price(rng),
        context_tokens=rng.randint(20_000, 120_000),
    )
    return spec, limits


async def _no_sleep(seconds: float) -> None:
    return None


def one_run(rng: random.Random, spec: ModelSpec, limits: Limits, reply_cap: int | None) -> tuple[float, float]:
    """Run one attempt; return what the endpoint billed and what the guard's ledger charged in top-ups."""
    endpoint = Endpoint(rng, spec, reply_cap)
    tally = Tally(request_bound_usd(spec, limits), rng, refuse_share=rng.choice([0.0, 0.0, 0.2]))
    adapter = OpenAICompatAdapter(
        spec, 1, limits, client=endpoint, sleep=_no_sleep, client_cfg=ClientSettings(max_retries=rng.randint(0, 3))
    )
    config = make_config(**dataclasses.asdict(limits))
    box = ToolBox([FakeProvider({"look": lambda arguments: "ok"})], cap_chars=limits.output_cap_chars)

    async def go():
        with spend.watching(AttemptSpend(tally, 1, "run-quince", "quince")):
            backend = RecordingBackend(adapter)
            return await run_agent(backend, box, "be brief", "do the task", config, clock=FakeClock(), sleep=never)

    asyncio.run(go())
    return endpoint.billed, tally.charged


def test_c26_no_run_with_retries_and_replies_that_leave_counts_out_costs_more_than_its_reservation_and_top_ups():
    """C26: over random prices, caps, usages, failed sends that may have been billed, retries and replies that leave a
    count out, what the endpoint billed for a run is never more than the run's reservation plus the top-ups its guard
    was charged, the runs ended by a refused top-up included. The endpoint holds each reply to the max_tokens sent."""
    rng = random.Random(20261110)
    for _ in range(150):
        spec, limits = random_case(rng)
        billed, topped = one_run(rng, spec, limits, reply_cap=spec.output_tokens(limits.max_output_tokens))
        assert billed <= (bound_usd(spec, limits) + topped) * (1 + 1e-9) + 1e-12


def test_c26_the_same_runs_without_top_ups_can_cost_more_than_their_reservation():
    """C26: the premise of the test above, shown: with nothing charged for the sends the cap cannot account for, some
    of those runs cost more than the reservation alone."""
    rng = random.Random(20261110)
    over = 0
    for _ in range(150):
        spec, limits = random_case(rng)
        billed, _ = one_run(rng, spec, limits, reply_cap=spec.output_tokens(limits.max_output_tokens))
        over += billed > bound_usd(spec, limits) * (1 + 1e-9) + 1e-12
    assert over > 0


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """The adapters read no key: the fake endpoint stands in for the client."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
