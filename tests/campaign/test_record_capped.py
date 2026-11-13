"""C27: the record of an attempt says what the store says about whether it was held to the reservation R.

The attempt's row keeps the bound the budget gate reserved the run by (`attempts.reserved_capped`), and the store the
models it has taken off the reply maximum (`reply_cap_unheld`). The record carries both in its totals, written by
`record_run` from the request, so a scorer that reads a record never opens the store.
"""

import openai
import pytest
from campaign_support import NOW
from loop_support import FakeProvider
from openai.types.chat import ChatCompletion
from record_support import CODE
from scheduler_support import FakeExecutor, build, models_for, settings_for

from expcore.campaign.executor import default_backend_factory, record_run
from expcore.campaign.gate import Budget
from expcore.campaign.reply_cap import is_unheld, mark_unheld, unheld
from expcore.config.models import ModelSpec, ModelTable
from expcore.config.prices import Price

MODEL = "teasel"
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
PLAN = {"models": (MODEL,), "tasks": ("task-a",), "seeds": (1,)}  # one run of each of the three conditions
ROOMY = Budget(max_usd=100.0)


class Endpoint:
    """Stands in for the OpenAI client the core's adapter builds: a reply is one short text that stops at the limit, one
    output token for the one-token completion of admission and 16 for any other request, which is what shows the
    endpoint holds replies to the `max_tokens` it is sent."""

    def __init__(self):
        self.chat = type("Chat", (), {"completions": type("Completions", (), {"create": self.create})()})()

    async def create(self, **request):
        tokens = 1 if request["max_tokens"] == 1 else 16
        choices = [{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": "1"}}]
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


@pytest.fixture
def endpoint(monkeypatch):
    """Make the core's adapter talk to the fake endpoint."""
    monkeypatch.setenv("TEASEL_TEST_KEY", "teasel-test-key-0123456789")
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **options: Endpoint())


def started(tmp_path, executor, **options):
    """A scheduler over the model of this file, with the core's own factory, which probes the model at its start."""
    return build(
        tmp_path / "campaign",
        executor=executor,
        factory=default_backend_factory,
        plan=PLAN,
        models=ModelTable({MODEL: SPEC}),
        **options,
    )


def through_the_adapter(records, before=lambda request: None):
    """The study's executor: the real loop and recording over the attempt's own backend, after `before(request)`. The
    records it made are kept in `records`, in the order the attempts ended."""

    async def execute(request):
        before(request)
        record = await record_run(
            request,
            [FakeProvider({"look": lambda arguments: "ok"})],
            "be brief",
            "do the task",
            FakeExecutor().config,
            code=dict(CODE),
            core_version="0.1.0",
        )
        records.append(record)
        return record

    return execute


def row_of(store, key):
    """The attempt of a run, as the store keeps it: the bound its row records, as True, False or None."""
    [row] = store.q("SELECT reserved_capped FROM attempts WHERE run_key=? AND state='done'", (key,))
    return None if row["reserved_capped"] is None else bool(row["reserved_capped"])


@pytest.mark.parametrize("marked", [False, True], ids=["not-marked", "marked"])
def test_c27_a_run_reserved_by_the_window_has_that_bound_and_the_stores_mark_in_its_record(tmp_path, marked):
    """C27: a model the campaign never showed held to the maximum is reserved by the window, `reserved_capped` False,
    whether or not the store has marked it. The record says the same, and its `reply_cap_unheld` is the mark."""
    executor = FakeExecutor()
    with build(
        tmp_path / "campaign",
        executor=executor,
        plan=PLAN,
        settings=settings_for(budget=ROOMY),
        models=models_for(MODEL),
    ) as scheduler:
        if marked:
            mark_unheld(scheduler.store, MODEL, NOW, "reply_over_maximum", "a reply of 5000 output tokens")
        assert scheduler.run().statuses == {"done": 3}
        assert len(executor.records) == 3
        for record in executor.records:
            assert row_of(scheduler.store, record.run_key) is False
            assert (record.totals["reserved_capped"], record.totals["reply_cap_unheld"]) == (False, marked)
            assert is_unheld(unheld(scheduler.store), MODEL) is marked


def test_c27_a_run_reserved_by_the_maximum_has_that_bound_and_a_mark_made_during_the_run_in_its_record(
    tmp_path, endpoint
):
    """C27: the first run is admitted with the smaller reservation, `reserved_capped` True in its row and its record,
    and finishes before any mark: `reply_cap_unheld` False. While the second is in flight another attempt's reply takes
    the model off the maximum: it keeps the decision of its admission, True, and its record, read when the loop has
    ended, says the model is off: `reply_cap_unheld` True. The third is admitted after the mark and reserved by the
    window. In each the record and the row say one thing."""
    records, seen = [], []

    def mark_after_the_first(request):
        if seen:
            mark_unheld(scheduler.store, MODEL, NOW, "reply_over_maximum", "another attempt's reply of 5000 tokens")
        seen.append(request.run_key)

    executor = FakeExecutor(default=through_the_adapter(records, mark_after_the_first))
    with started(tmp_path, executor, settings=settings_for(budget=ROOMY, workers=1)) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        assert [record.run_key for record in records] == seen
        assert [(r.totals["reserved_capped"], r.totals["reply_cap_unheld"]) for r in records] == [
            (True, False),
            (True, True),
            (False, True),
        ]
        for record in records:
            assert row_of(scheduler.store, record.run_key) is record.totals["reserved_capped"]
        assert is_unheld(unheld(scheduler.store), MODEL)


def test_c27_without_a_budget_nothing_decides_the_bound_and_the_record_holds_neither_field(tmp_path):
    """C27 and R10: no gate ran, so the row's `reserved_capped` is null and there is no maximum to hold; the record
    leaves both fields out, and does not write a null."""
    executor = FakeExecutor()
    plan = {"models": ("m1",), "tasks": ("task-a",), "seeds": (1,)}
    with build(tmp_path / "campaign", executor=executor, plan=plan, settings=settings_for()) as scheduler:
        assert scheduler.run().statuses == {"done": 3}
        rows = scheduler.store.q("SELECT reserved_capped FROM attempts")
        assert [row["reserved_capped"] for row in rows] == [None] * 3
        assert len(executor.records) == 3
        for record in executor.records:
            assert "reserved_capped" not in record.totals
            assert "reply_cap_unheld" not in record.totals
            assert '"reserved_capped"' not in record.dumps()
