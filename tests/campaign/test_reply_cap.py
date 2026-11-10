"""The bound by the reply maximum applies only where the campaign showed the endpoint holds replies to it (C27)."""

import asyncio
import threading

import openai
import pytest
from campaign_support import NOW, planned_store
from openai.types.chat import ChatCompletion
from scheduler_support import Factory, FakeExecutor, build, event_kinds, settings_for

from expcore.campaign.attempt_bounds import bound_usd, request_bound_usd
from expcore.campaign.executor import default_backend_factory
from expcore.campaign.gate import Budget, BudgetGate
from expcore.campaign.reply_cap import UNHELD_KEY, mark_unheld, probed, unheld
from expcore.campaign.topup import AttemptSpend, Ledger
from expcore.config.models import Limits, ModelSpec, ModelTable
from expcore.config.prices import Price
from expcore.errors import BudgetHold

MODEL = "teasel"
LIMITS = Limits(
    max_tool_rounds=10, time_cap_s=300.0, token_budget=100_000, output_cap_chars=200, max_output_tokens=4096
)  # the support executor's caps
SPEC = ModelSpec(
    key=MODEL,
    backend="openai_compat",
    api_id="vendor/teasel-1",
    status="frozen",
    base_url="https://llm.example.invalid/v1",
    key_env="TEASEL_TEST_KEY",
    price=Price(1.0, 10.0),
    context_tokens=32_000,
)
# Held to its 4,096-token replies: at most 40,960 output tokens at 10 USD and the rest of the cap as input at 1, 0.469;
# past the cap a 32,000-token prompt and one reply, 0.07296. Not held: the cap and the window at 10 USD, 1.0 + 0.32.
HELD, UNHELD = 0.46864 + 0.07296, 1.32


def test_c27_the_bound_is_by_the_reply_maximum_only_when_the_caller_says_the_replies_are_held_to_it():
    """C27: the backend's name decides nothing; the bound by the window is the default."""
    assert bound_usd(SPEC, LIMITS, reply_capped=True) == pytest.approx(HELD)
    assert bound_usd(SPEC, LIMITS) == pytest.approx(UNHELD)
    assert request_bound_usd(SPEC, LIMITS, reply_capped=True) == pytest.approx(0.07296)
    assert request_bound_usd(SPEC, LIMITS) == pytest.approx(0.32)


def test_c27_only_a_backend_of_the_cores_own_factory_that_sends_the_maximum_is_probed_and_only_with_a_budget():
    """C27: a plugin's factory may wrap the core's adapter or replace it, and the stub ignores the maximum."""
    assert probed(SPEC, default_backend_factory, with_budget=True)
    assert not probed(SPEC, default_backend_factory, with_budget=False)
    assert not probed(SPEC, Factory(), with_budget=True)
    stub = ModelSpec(key=MODEL, backend="stub", api_id=MODEL, status="frozen", price=SPEC.price, context_tokens=32_000)
    assert not probed(stub, default_backend_factory, with_budget=True)


def gate_for(store, max_usd: float = 10.0) -> BudgetGate:
    return BudgetGate(store, {MODEL: SPEC}, Budget(max_usd=max_usd), LIMITS, reply_capped={MODEL})


def test_c27_a_model_shown_at_admission_is_reserved_by_the_maximum_until_the_store_takes_it_off(tmp_path):
    """C27: the mark is in the store, so it holds for the rest of the campaign, a resume included."""
    store, _, _ = planned_store(tmp_path, models=(MODEL,))
    assert gate_for(store).assess([], NOW).reserve_usd[MODEL] == pytest.approx(HELD)
    mark_unheld(store, MODEL, NOW, "reply_over_maximum", "a reply of 5000 output tokens")
    mark_unheld(store, MODEL, NOW, "reply_over_maximum", "a second one")
    assert unheld(store)[MODEL]["reason"] == "a reply of 5000 output tokens"  # the first reason is kept
    assert event_kinds(store).count("reply_over_maximum") == 2
    assert gate_for(store).assess([], NOW).reserve_usd[MODEL] == pytest.approx(UNHELD)
    store.put_meta(UNHELD_KEY, "{not json", NOW)
    assert gate_for(store).assess([], NOW).reserve_usd[MODEL] == pytest.approx(UNHELD)  # unreadable: every model


def test_c27_a_reply_past_the_maximum_takes_the_model_off_it_and_its_runs_in_flight_are_checked_before_each_request(
    tmp_path,
):
    """C27: the reply is an event and the model's runs are reserved by the window from then on, the runs in flight
    included; each later request of a run started under the smaller bound goes out only while the campaign is within
    its ceiling, and is a budget hold once it is not."""
    store, _, manifest = planned_store(tmp_path, models=(MODEL,))
    key = manifest.rows[0].key.text
    begun = store.begin_attempt(key, NOW, 4321, str(tmp_path / "attempt"))
    room = HELD + 0.1  # room for the run as it was reserved, not for the bound by the window
    gate = gate_for(store, max_usd=room)
    ledger = Ledger(gate, store, lambda: NOW, (threading.Lock(),), lambda: [MODEL])
    guard = AttemptSpend(ledger, begun.id, key, MODEL)

    async def go():
        await guard.before_request()
        await guard.after_reply(1_000, 4_096)  # within the maximum
        await guard.before_request()
        await guard.after_reply(1_000, 4_097)  # past it
        await guard.before_request()

    with pytest.raises(BudgetHold, match="over the ceiling"):
        asyncio.run(go())
    [over] = store.q("SELECT model, detail FROM events WHERE kind='reply_over_maximum'")
    assert over["model"] == MODEL
    assert "4097 output tokens, over the reply maximum of 4096" in over["detail"]
    assert gate.assess([MODEL], NOW).reserve_usd[MODEL] == pytest.approx(UNHELD)
    assert "topup_refused" in event_kinds(store)


def test_c27_a_reply_past_the_maximum_of_a_model_not_held_to_it_is_nothing_new(tmp_path):
    """C27: a model reserved by the window already covers such a reply: no event, no check."""
    store, _, manifest = planned_store(tmp_path, models=(MODEL,))
    key = manifest.rows[0].key.text
    begun = store.begin_attempt(key, NOW, 4321, str(tmp_path / "attempt"))
    gate = BudgetGate(store, {MODEL: SPEC}, Budget(max_usd=10.0), LIMITS)
    guard = AttemptSpend(Ledger(gate, store, lambda: NOW, (threading.Lock(),), lambda: [MODEL]), begun.id, key, MODEL)
    asyncio.run(guard.after_reply(1_000, 20_000))
    asyncio.run(guard.before_request())
    assert "reply_over_maximum" not in event_kinds(store)


# --- admission through the scheduler, with the core's own factory -----------------------------------------------------


class Endpoint:
    """Stands in for the OpenAI client the core's adapter builds: answers the one-token completion and the probe of
    the reply maximum, with a reply of `long_reply` output tokens and `finish` as its stop reason."""

    def __init__(self, *, long_reply: int, finish: str):
        self.long_reply, self.finish, self.requests = long_reply, finish, []
        self.chat = type("Chat", (), {"completions": type("Completions", (), {"create": self.create})()})()

    async def create(self, **request):
        self.requests.append(request)
        tokens, finish = (1, "length") if request["max_tokens"] == 1 else (self.long_reply, self.finish)
        return ChatCompletion.model_validate(
            {
                "id": "cmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": "vendor/teasel-1",
                "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": "1"}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": tokens, "total_tokens": 20 + tokens},
            }
        )

    async def close(self):
        return None


BUILT: list[Endpoint] = []  # every fake client the core's adapters were given, in order
BUDGET = Budget(max_usd=100.0)


@pytest.fixture
def endpoint(monkeypatch):
    """Make the core's adapter talk to a fake endpoint, whose behaviour each test sets."""
    monkeypatch.setenv("TEASEL_TEST_KEY", "teasel-test-key-0123456789")
    behaviour = {"long_reply": 16, "finish": "length"}

    def client(**options):
        made = Endpoint(**behaviour)
        BUILT.append(made)
        return made

    monkeypatch.setattr(openai, "AsyncOpenAI", client)
    BUILT.clear()
    return behaviour


def opened(tmp_path, factory, budget: Budget | None = BUDGET):
    plan = {"models": (MODEL,), "tasks": ("task-a",), "seeds": (1,)}
    settings = settings_for(budget=budget) if budget is not None else settings_for()
    table = ModelTable({MODEL: SPEC})
    return build(
        tmp_path / "campaign", executor=FakeExecutor(), factory=factory, settings=settings, plan=plan, models=table
    )


def test_c27_an_endpoint_that_cut_the_probe_at_its_max_tokens_is_reserved_by_the_reply_maximum(tmp_path, endpoint):
    """C27: the core's own factory, and an endpoint that cut a request for a long reply at the small max_tokens it was
    sent: the model's runs are reserved the smaller bound."""
    with opened(tmp_path, default_backend_factory) as scheduler:
        assert scheduler.not_admitted == {}
        assert scheduler.budget.assess([], NOW).reserve_usd[MODEL] == pytest.approx(HELD)
        probe = BUILT[0].requests[1]
        assert probe["max_tokens"] == 16
        assert "Count from 1 to" in probe["messages"][0]["content"]


@pytest.mark.parametrize(
    ("long_reply", "finish", "why"),
    [
        (400, "length", "reported 400 output tokens for a max_tokens of 16"),
        (12, "stop", "stopped by itself"),
    ],
)
def test_c27_an_endpoint_the_probe_did_not_show_is_reserved_by_the_window_and_the_store_says_why(
    tmp_path, endpoint, long_reply, finish, why
):
    """C27: a reply past the small max_tokens shows the endpoint does not apply it; one that stopped by itself shows
    nothing either way. The model is admitted and reserved the bound by the window, with an event."""
    endpoint.update(long_reply=long_reply, finish=finish)
    with opened(tmp_path, default_backend_factory) as scheduler:
        assert scheduler.not_admitted == {}
        assert scheduler.budget.assess([], NOW).reserve_usd[MODEL] == pytest.approx(UNHELD)
        [event] = scheduler.store.q("SELECT detail FROM events WHERE kind='reply_cap_not_shown'")
        assert why in event["detail"]


def test_c27_a_plugin_factory_that_builds_an_openai_compatible_model_is_reserved_by_the_window_and_never_probed(
    tmp_path, endpoint
):
    """C27: the name of the backend says what the entry asks for, not what the plugin's factory built."""
    with opened(tmp_path, Factory()) as scheduler:
        assert scheduler.budget.assess([], NOW).reserve_usd[MODEL] == pytest.approx(UNHELD)
        assert "reply_cap_not_shown" not in event_kinds(scheduler.store)
    assert BUILT == []


def test_c27_without_a_budget_no_probe_of_the_reply_maximum_is_sent(tmp_path, endpoint):
    """C27: the probe serves the budget alone, so without one only the one-token completion goes out."""
    with opened(tmp_path, default_backend_factory, budget=None) as scheduler:
        assert scheduler.budget is None
    assert [request["max_tokens"] for request in BUILT[0].requests] == [1]
