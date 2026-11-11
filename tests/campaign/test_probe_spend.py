"""Admission probes are paid from the campaign's budget: each send is reserved before it goes out and charged (C26)."""

import asyncio
import threading

import httpx
import openai
import pytest
from campaign_support import planned_store
from openai.types.chat import ChatCompletion
from scheduler_support import build, event_kinds, settings_for

from expcore.campaign.executor import default_backend_factory
from expcore.campaign.gate import Budget, BudgetGate
from expcore.campaign.probe_spend import MeteredProbes, charged
from expcore.campaign.topup import Ledger
from expcore.config.models import Limits, ModelSpec, ModelTable
from expcore.config.prices import Price
from expcore.errors import BudgetHold
from expcore.models.adapters import ProbeReply
from expcore.models.retry import Retrier, RetryPolicy

MODEL = "sorrel"
LIMITS = Limits(
    max_tool_rounds=10, time_cap_s=300.0, token_budget=100_000, output_cap_chars=200, max_output_tokens=4096
)  # the support executor's caps
SPEC = ModelSpec(
    key=MODEL,
    backend="openai_compat",
    api_id="vendor/sorrel-1",
    status="frozen",
    base_url="https://llm.example.invalid/v1",
    key_env="SORREL_TEST_KEY",
    price=Price(1.0, 10.0),
    context_tokens=32_000,
)
PROBE_BOUND = 0.32  # one request by the window, which is what a probe could cost: 32,000 tokens at 10 USD per million
ONE_TOKEN = (20 * 1.0 + 1 * 10.0) / 1e6  # what the one-token completion's reply reported, at the dearest rates
REPLY_CAP = (20 * 1.0 + 16 * 10.0) / 1e6  # what the probe of the reply maximum's reply reported
URL = "https://llm.example.invalid/v1/chat/completions"


def server_error() -> openai.InternalServerError:
    return openai.InternalServerError(
        "busy", response=httpx.Response(503, request=httpx.Request("POST", URL)), body=None
    )


class Endpoint:
    """Stands in for the OpenAI client of the core's adapter: answers the one-token completion and the probe of the
    reply maximum, which it cuts at its max_tokens, each reply reporting a 20-token prompt."""

    def __init__(self):
        self.requests = []
        self.chat = type("Chat", (), {"completions": type("Completions", (), {"create": self.create})()})()

    async def create(self, **request):
        self.requests.append(request)
        tokens = request["max_tokens"]
        return ChatCompletion.model_validate(
            {
                "id": "cmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": "vendor/sorrel-1",
                "choices": [{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": "1"}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": tokens, "total_tokens": 20 + tokens},
            }
        )

    async def close(self):
        return None


BUILT: list[Endpoint] = []  # every fake client the core's adapters were given, in order


@pytest.fixture(autouse=True)
def endpoint(monkeypatch):
    monkeypatch.setenv("SORREL_TEST_KEY", "sorrel-test-key-0123456789")

    def client(**options):
        made = Endpoint()
        BUILT.append(made)
        return made

    monkeypatch.setattr(openai, "AsyncOpenAI", client)
    BUILT.clear()


def opened(tmp_path, budget: Budget):
    plan = {"models": (MODEL,), "tasks": ("task-a",), "seeds": (1,)}
    return build(
        tmp_path / "campaign",
        factory=default_backend_factory,
        settings=settings_for(budget=budget),
        plan=plan,
        models=ModelTable({MODEL: SPEC}),
    )


def sent() -> int:
    return sum(len(client.requests) for client in BUILT)


def test_c26_a_campaign_whose_budget_cannot_cover_a_probe_sends_none_and_does_not_admit_the_model_on_any_start(
    tmp_path,
):
    """C26: a probe is reserved its bound before it is sent, like a top-up; with no room for it, nothing reaches the
    endpoint, the model is not admitted and the hold says the budget is spent, at this start and the next."""
    for _ in range(2):  # a restart sends nothing either
        with opened(tmp_path, Budget(max_usd=0.1)) as scheduler:
            reason = scheduler.not_admitted[MODEL]
            assert reason.startswith("the campaign's budget is spent:")
            assert "this probe 0.32" in reason
            summary = scheduler.run()
            assert summary.holds[MODEL].kind == "not_admitted"
            assert summary.attempts == 0
            assert {"probe_refused", "model_not_admitted"} <= set(event_kinds(scheduler.store))
            assert charged(scheduler.store) == {}
    assert sent() == 0


def test_c26_each_probe_send_is_charged_in_the_store_and_settled_to_what_its_reply_reported(tmp_path):
    """C26: the bound is charged before the send and kept in the store, so the gate counts it from then on and a
    restart keeps it; a reply that reports both counts settles it to what they cost at the dearest rates."""
    with opened(tmp_path, Budget(max_usd=100.0)) as scheduler:
        assert scheduler.not_admitted == {}
        assert sent() == 2  # the one-token completion and the probe of the reply maximum
        assert charged(scheduler.store) == {MODEL: pytest.approx(ONE_TOKEN + REPLY_CAP)}
        assert scheduler.budget.assess([], "2026-01-01T00:00:00Z").spent_usd == pytest.approx(ONE_TOKEN + REPLY_CAP)
        kinds = [k for k in event_kinds(scheduler.store) if k.startswith("probe_")]
        assert kinds == ["probe_charged", "probe_settled"] * 2
    with opened(tmp_path, Budget(max_usd=100.0)) as scheduler:  # a second start pays for its own probes
        assert charged(scheduler.store) == {MODEL: pytest.approx(2 * (ONE_TOKEN + REPLY_CAP))}


def test_c26_a_release_written_for_a_stopped_campaign_is_taken_before_its_probes_ask_for_room(tmp_path):
    """C26 and C13: the campaign stopped at its ceiling and was released; the release is taken at the start, before
    admission, so the probes find the room it gave instead of holding the model until a second restart."""
    release = tmp_path / "release.txt"
    budget = Budget(max_usd=0.1, release_step_usd=1.0, release_file=release)
    with opened(tmp_path, budget) as scheduler:
        scheduler.run()  # the stop on the whole campaign begins
        assert MODEL in scheduler.not_admitted
    release.write_text("ALL\n", encoding="utf-8")
    with opened(tmp_path, budget) as scheduler:
        assert not release.exists()  # used up at the start
        assert scheduler.not_admitted == {}
        assert sent() == 2


class Retrying:
    """A backend whose probe goes through the core's retrier: its first send fails with a 503, which may have been
    billed, and the second is answered, reporting a 20-token prompt and one output token."""

    def __init__(self):
        self.sends = 0
        policy = RetryPolicy(max_retries=3, base_delay_s=1.0, max_delay_s=10.0, jitter=0.0)
        self.retrier = Retrier(policy, sleep=self._no_wait)

    @staticmethod
    async def _no_wait(seconds: float) -> None:
        return None

    async def _send(self):
        self.sends += 1
        if self.sends == 1:
            raise server_error()
        return "answered"

    async def probe(self) -> ProbeReply:
        await self.retrier.call(self._send)
        return ProbeReply(1, 20, 1)


def ledger_for(tmp_path, max_usd: float) -> Ledger:
    store, _, _ = planned_store(tmp_path, models=(MODEL,))
    gate = BudgetGate(store, {MODEL: SPEC}, Budget(max_usd=max_usd), LIMITS)
    return Ledger(gate, store, lambda: "2026-01-01T00:00:00Z", (threading.Lock(),), lambda: [])


@pytest.mark.parametrize("room", ["enough", "short"])
def test_c26_a_probe_resent_after_a_failure_that_may_have_been_billed_reserves_the_resend_too(tmp_path, room):
    """C26: the failed send keeps its bound, since what it cost is unknown; the resend is reserved before it goes out
    and settled by its reply, or, with no room for it, is not sent and the probe fails as a budget hold."""
    ledger = ledger_for(tmp_path, 10.0 if room == "enough" else PROBE_BOUND + 0.01)
    backend = Retrying()
    metered = MeteredProbes(backend, ledger, MODEL)
    if room == "enough":
        assert asyncio.run(metered.probe()) == ProbeReply(1, 20, 1)
        assert backend.sends == 2
        assert charged(ledger.store) == {MODEL: pytest.approx(PROBE_BOUND + ONE_TOKEN)}
        return
    with pytest.raises(BudgetHold, match="over the ceiling"):
        asyncio.run(metered.probe())
    assert backend.sends == 1
    assert metered.refusal is not None
    assert charged(ledger.store) == {MODEL: pytest.approx(PROBE_BOUND)}
