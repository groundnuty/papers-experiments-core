"""The bound by the reply maximum applies only where the campaign showed the endpoint holds replies to it (C27)."""

import asyncio
import threading

import httpx
import openai
import pytest
from campaign_support import NOW, planned_store
from loop_support import FakeProvider
from openai.types.chat import ChatCompletion
from record_support import CODE
from scheduler_support import Factory, FakeExecutor, build, event_kinds, settings_for

from expcore.campaign import probe_spend
from expcore.campaign.attempt import AttemptRunner
from expcore.campaign.attempt_bounds import bound_usd, request_bound_usd
from expcore.campaign.executor import default_backend_factory, record_run
from expcore.campaign.gate import Budget, BudgetGate
from expcore.campaign.recovery import recover
from expcore.campaign.reply_cap import UNHELD_KEY, mark_unheld, probed, unheld
from expcore.campaign.topup import AttemptSpend, Ledger
from expcore.config.models import Limits, ModelSpec, ModelTable
from expcore.config.prices import Price
from expcore.errors import BudgetHold
from expcore.models import spend
from expcore.models.retry import Retrier, RetryPolicy
from expcore.replay.replay import replay_record

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
    guard = AttemptSpend(ledger, begun.id, key, MODEL, reserved_capped=True)

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
    ledger = Ledger(gate, store, lambda: NOW, (threading.Lock(),), lambda: [MODEL])
    guard = AttemptSpend(ledger, begun.id, key, MODEL, reserved_capped=False)
    asyncio.run(guard.after_reply(1_000, 20_000))
    asyncio.run(guard.before_request())
    assert "reply_over_maximum" not in event_kinds(store)


# --- admission through the scheduler, with the core's own factory -----------------------------------------------------


class Endpoint:
    """Stands in for the OpenAI client the core's adapter builds: answers the one-token completion and the probe of
    the reply maximum, with a reply of `long_reply` output tokens and `finish` as its stop reason."""

    def __init__(self, *, long_reply: int, finish: str, run_unreadable: int | None = None, one_token_reply: int = 1):
        self.long_reply, self.finish, self.requests = long_reply, finish, []
        self.one_token_reply = one_token_reply  # what the one-token completion's reply reports as its output
        self.run_unreadable = run_unreadable  # a run's request gets a billed reply of this many tokens and no choices
        self.chat = type("Chat", (), {"completions": type("Completions", (), {"create": self.create})()})()

    async def create(self, **request):
        self.requests.append(request)
        tokens, finish = (
            (self.one_token_reply, "length") if request["max_tokens"] == 1 else (self.long_reply, self.finish)
        )
        choices = [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": "1"}}]
        if self.run_unreadable is not None and request["max_tokens"] > 16:  # neither probe: a run's request
            tokens, choices = self.run_unreadable, []
        return ChatCompletion.model_validate(
            {
                "id": "cmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": "vendor/teasel-1",
                "choices": choices,
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


async def through_the_adapter(request):
    """The study's executor: the real loop and recording over the attempt's own backend, the core's adapter."""
    return await record_run(
        request,
        [FakeProvider({"look": lambda arguments: "ok"})],
        "be brief",
        "do the task",
        FakeExecutor().config,
        code=dict(CODE),
        core_version="0.1.0",
    )


def test_c27_a_run_admitted_by_the_maximum_keeps_that_decision_when_the_model_is_taken_off_before_its_guard_is_built(
    tmp_path, endpoint, monkeypatch
):
    """C27: the run was admitted with the smaller reservation; another attempt takes the model off the maximum after
    that and before the run's spend guard exists. The guard carries the decision made when the run was admitted, never
    the model's status as it reads it later, so the run's first request is checked against the ceiling, does not fit
    the bound by the window, and is a budget hold, sent to no endpoint."""
    original = AttemptRunner._begin

    def begin_after_a_mark(runner, row, lease=None, **recorded):
        mark_unheld(runner.p.store, MODEL, NOW, "reply_over_maximum", "another attempt's reply of 5000 output tokens")
        return original(runner, row, lease, **recorded)

    monkeypatch.setattr(AttemptRunner, "_begin", begin_after_a_mark)
    plan = {"models": (MODEL,), "tasks": ("task-a",), "seeds": (1,)}
    room = Budget(max_usd=HELD + 0.1)  # room for a run reserved by the maximum, not for one reserved by the window
    with build(
        tmp_path / "campaign",
        executor=FakeExecutor(default=through_the_adapter),
        factory=default_backend_factory,
        settings=settings_for(budget=room),
        plan=plan,
        models=ModelTable({MODEL: SPEC}),
    ) as scheduler:
        summary = scheduler.run()
        first = scheduler.store.q("SELECT state, infra_kind FROM attempts ORDER BY id")[0]
        assert (first["state"], first["infra_kind"]) == ("infra_fail", "budget_hold")
        assert BUILT[1].requests == []  # the attempt's client: nothing reached the endpoint
        assert "topup_refused" in event_kinds(scheduler.store)
        assert summary.holds[MODEL].kind == "budget"


# --- a resend is cleared again right before it goes out -------------------------------------------------------------

HELD_TOPUP, UNHELD_TOPUP = 0.07296, 0.32  # one request's bound, by the maximum and by the window


class FailsOnce:
    """A send that fails once with a 503, after which the request may have been billed, then answers."""

    def __init__(self):
        self.sends = 0

    async def __call__(self):
        self.sends += 1
        if self.sends == 1:
            request = httpx.Request("POST", "https://llm.example.invalid/v1/chat/completions")
            raise openai.InternalServerError("busy", response=httpx.Response(503, request=request), body=None)
        return "answer"


@pytest.mark.parametrize("room", ["enough", "short"])
def test_c27_a_resend_whose_model_was_taken_off_the_maximum_during_its_wait_reserves_the_rest_of_its_bound_or_holds(
    tmp_path, room
):
    """C27 and C26: the resend's top-up was reserved before the wait, by the maximum; during the wait another attempt
    takes the model off it. Right before the resend goes out it is cleared again: the rest of its bound by the window
    is reserved, or, with no room for it, the resend is not sent and the attempt is a budget hold, its failed send
    charged all the same."""
    store, _, manifest = planned_store(tmp_path, models=(MODEL,))
    key = manifest.rows[0].key.text
    begun = store.begin_attempt(key, NOW, 4321, str(tmp_path / "attempt"))
    max_usd = 10.0 if room == "enough" else HELD + HELD_TOPUP + 0.01  # short: no room for the bound by the window
    ledger = Ledger(gate_for(store, max_usd=max_usd), store, lambda: NOW, (threading.Lock(),), lambda: [MODEL])
    guard = AttemptSpend(ledger, begun.id, key, MODEL, reserved_capped=True)

    async def wait_while_another_attempt_marks_the_model(seconds: float) -> None:
        mark_unheld(store, MODEL, NOW, "reply_over_maximum", "another attempt's reply of 5000 output tokens")

    policy = RetryPolicy(max_retries=3, base_delay_s=1.0, max_delay_s=10.0, jitter=0.0)
    retrier = Retrier(policy, sleep=wait_while_another_attempt_marks_the_model)
    send = FailsOnce()

    async def go():
        with spend.watching(guard):
            return await retrier.call(send)

    if room == "enough":
        reply, _ = asyncio.run(go())
        assert (reply, send.sends) == ("answer", 2)
        charged = store.q("SELECT topups, topup_usd FROM attempts WHERE id=?", (begun.id,))[0]
        assert charged["topup_usd"] == pytest.approx(UNHELD_TOPUP)  # by the maximum before the wait, the rest after
        return
    with pytest.raises(BudgetHold, match="over the ceiling"):
        asyncio.run(go())
    assert send.sends == 1
    charged = store.q("SELECT topups, topup_usd FROM attempts WHERE id=?", (begun.id,))[0]
    assert (charged["topups"], charged["topup_usd"]) == (1, pytest.approx(HELD_TOPUP))
    assert "topup_refused" in event_kinds(store)


# --- an attempt whose usage is unknown is charged the bound its run was reserved, across a restart ------------------

CEILING = Budget(max_usd=1.20)  # the bound by the maximum and one probe fit under it; the bound by the window does not


def killed_before_marker(point, run_key):
    """The process dies once the attempt's 'running' row is committed, before anything else of it is written."""
    if point == "before_marker":
        raise OSError("the process is killed here")


def started(tmp_path, factory, executor, budget: Budget, **options):
    plan = {"models": (MODEL,), "tasks": ("task-a",), "seeds": (1,)}
    return build(
        tmp_path / "campaign",
        executor=executor,
        factory=factory,
        settings=settings_for(budget=budget),
        plan=plan,
        models=ModelTable({MODEL: SPEC}),
        **options,
    )


def test_c27_a_restart_charges_an_interrupted_attempt_the_bound_by_the_maximum_it_was_reserved_and_resumes(
    tmp_path, endpoint
):
    """C27 and C05: a run reserved by the reply maximum is killed before its usage was recorded. At the restart the
    early gate, which pays for admission's probes before any model is shown held to the maximum, charges the
    interrupted attempt the bound its row records, 0.54 USD, not the bound by the window, 1.32 USD. Under a ceiling of
    1.20 USD, which the real total fits, the probes go out, the model is admitted, and the run is run again."""
    executor = FakeExecutor(default=through_the_adapter)
    killing = started(tmp_path, default_backend_factory, executor, CEILING, crash_hook=killed_before_marker)
    with killing as killed, pytest.raises(RuntimeError, match="unexpected error"):
        killed.run()
    with started(tmp_path, default_backend_factory, executor, CEILING) as restart:
        assert restart.not_admitted == {}
        assert restart.run().statuses == {"done": 3}  # one run of each of the support schema's three conditions
        attempts = restart.store.q("SELECT state, reserved_capped FROM attempts ORDER BY id")
        assert [(entry["state"], entry["reserved_capped"]) for entry in attempts] == [("interrupted", 1)] + [
            ("done", 1)
        ] * 3


def test_c27_an_interrupted_attempt_reserved_by_the_window_stays_charged_so_after_a_restart_that_shows_the_model_held(
    tmp_path, endpoint
):
    """C27 and C05: a run of a model a plugin's factory builds, never probed and so reserved by the window, is killed
    before its usage was recorded. The restart builds the model with the core's factory, whose probe shows it held to
    the reply maximum: new runs are reserved the smaller bound, and the interrupted attempt is still charged the bound
    by the window it was reserved."""
    roomy = Budget(max_usd=100.0)
    killing = started(tmp_path, Factory(), FakeExecutor(), roomy, crash_hook=killed_before_marker)
    with killing as killed, pytest.raises(RuntimeError, match="unexpected error"):
        killed.run()
    with started(tmp_path, default_backend_factory, FakeExecutor(), roomy) as restart:
        assessed = restart.budget.assess([], NOW)
        assert assessed.reserve_usd[MODEL] == pytest.approx(HELD)  # this start shows the model held
        probes = sum(probe_spend.charged(restart.store).values())
        assert assessed.spent_usd == pytest.approx(UNHELD + probes)
        [attempt] = restart.store.q("SELECT state, reserved_capped FROM attempts")
        assert (attempt["state"], attempt["reserved_capped"]) == ("interrupted", 0)


def interrupted_store(tmp_path, **recorded):
    """A store with one attempt of the model killed before its usage was recorded, settled by recovery."""
    store, _, manifest = planned_store(tmp_path, models=(MODEL,))
    store.begin_attempt(manifest.rows[0].key.text, NOW, 4321, "", **recorded)
    recover(store, NOW, 3)
    return store


def test_c27_an_attempt_from_before_its_reservation_was_recorded_follows_the_gates_view_of_the_model(tmp_path):
    """C27 and C05: a row written before the column existed keeps the earlier rule: the bound by the maximum only for a
    model the gate holds to it, so the early gate, which holds none, charges the bound by the window and can only
    refuse more."""
    store = interrupted_store(tmp_path)
    early = BudgetGate(store, {MODEL: SPEC}, Budget(max_usd=10.0), LIMITS)
    assert early.assess([], NOW).spent_usd == pytest.approx(UNHELD)
    assert gate_for(store).assess([], NOW).spent_usd == pytest.approx(HELD)


def test_c27_an_attempt_recorded_by_the_maximum_is_charged_by_the_window_once_the_store_takes_its_model_off(tmp_path):
    """C27 and C05: the reply past the maximum may have been one of the interrupted attempt's own, so its record does
    not outlast the mark, at any gate."""
    store = interrupted_store(tmp_path, reserved_capped=True)
    early = BudgetGate(store, {MODEL: SPEC}, Budget(max_usd=10.0), LIMITS)
    assert early.assess([], NOW).spent_usd == pytest.approx(HELD)
    mark_unheld(store, MODEL, NOW, "reply_over_maximum", "a reply of 5000 output tokens")
    assert early.assess([], NOW).spent_usd == pytest.approx(UNHELD)
    assert gate_for(store).assess([], NOW).spent_usd == pytest.approx(UNHELD)


def test_c27_a_billed_reply_past_the_maximum_that_cannot_be_read_marks_the_model_and_leaves_the_usage_incomplete(
    tmp_path, endpoint
):
    """C27 and C26: a run's request is answered 200 with 5000 output tokens and no choices. The adapter counts the
    reply and refuses it before the record keeps a turn, so the record's totals miss it: the attempt's usage is
    incomplete and it is charged its bound, never the 0 its empty totals would give, and the reply past the reply
    maximum still takes the model off it, so that bound is the window's. The record replays as it was written."""
    endpoint.update(run_unreadable=5_000)
    records = []

    async def keeping(request):
        record = await through_the_adapter(request)
        records.append(record)
        return record

    executor = FakeExecutor(default=keeping)
    with started(tmp_path, default_backend_factory, executor, Budget(max_usd=100.0)) as scheduler:
        scheduler.run(max_dispatch=1)
        [attempt] = scheduler.store.q("SELECT state, infra_kind, usage_complete FROM attempts")
        assert (attempt["state"], attempt["infra_kind"], attempt["usage_complete"]) == ("infra_fail", "endpoint", 0)
        assert "reply_over_maximum" in event_kinds(scheduler.store)
        probes = sum(probe_spend.charged(scheduler.store).values())
        assert scheduler.budget.assess([], NOW).spent_usd == pytest.approx(UNHELD + probes)
    [record] = records
    assert record.totals["usage_incomplete"] is True
    assert replay_record(record).to_json() == record.to_json()


def test_c27_a_reply_past_the_maximum_of_a_run_reserved_by_the_window_still_takes_a_held_model_off_it(tmp_path):
    """C27: the run was reserved by the window, but its model is held to the reply maximum in the campaign. Its reply
    past the maximum is evidence against every other run of the model, so it takes the model off the maximum too."""
    store, _, manifest = planned_store(tmp_path, models=(MODEL,))
    key = manifest.rows[0].key.text
    begun = store.begin_attempt(key, NOW, 4321, str(tmp_path / "attempt"))
    gate = gate_for(store)
    ledger = Ledger(gate, store, lambda: NOW, (threading.Lock(),), lambda: [MODEL])
    guard = AttemptSpend(ledger, begun.id, key, MODEL, reserved_capped=False)
    asyncio.run(guard.after_reply(1_000, 4_096))  # within the maximum: nothing
    assert "reply_over_maximum" not in event_kinds(store)
    asyncio.run(guard.after_reply(1_000, 5_000))
    assert "reply_over_maximum" in event_kinds(store)
    assert MODEL in unheld(store)
    assert gate.assess([], NOW).reserve_usd[MODEL] == pytest.approx(UNHELD)


def test_c27_a_one_token_completion_answered_past_its_max_tokens_takes_the_model_off_the_maximum_at_once(
    tmp_path, endpoint
):
    """C27: admission sends a max_tokens of 1, and the endpoint answered 50 output tokens: it does not apply the
    max_tokens it is sent. The model is taken off the reply maximum then and there, before any later probe is sent
    that the budget could refuse, and the probe of the reply maximum is not sent at all; the model is admitted and
    reserved by its window."""
    endpoint.update(one_token_reply=50)
    with opened(tmp_path, default_backend_factory) as scheduler:
        assert scheduler.not_admitted == {}
        assert scheduler.budget.assess([], NOW).reserve_usd[MODEL] == pytest.approx(UNHELD)
        [event] = scheduler.store.q("SELECT detail FROM events WHERE kind='reply_cap_not_shown'")
        assert "50 output tokens for a max_tokens of 1" in event["detail"]
        assert MODEL in unheld(scheduler.store)
    assert [request["max_tokens"] for request in BUILT[0].requests] == [1]
