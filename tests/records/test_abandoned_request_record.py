"""B19: the bound on an abandoned request's prompt travels through the loop into the record, survives a write, a load
and a replay, and prices from the record alone. The adapter is the core's own, against a fake client."""

import json

import httpx
import openai
import pytest
from openai.types.chat import ChatCompletion
from record_support import record_run

from expcore.campaign.abandoned_bound import abandoned_request_bound, held_to_reply_maximum
from expcore.config.models import Limits, ModelSpec
from expcore.config.prices import Price
from expcore.models.adapters import OpenAICompatAdapter
from expcore.records.record import load_record
from expcore.replay.replay import replay_record

WINDOW = 1_000_000
SPEC = ModelSpec(
    key="teasel",
    backend="openai_compat",
    api_id="vendor/teasel-1",
    status="frozen",
    base_url="https://llm.example.invalid/v1",
    price=Price(3.0, 15.0),
    context_tokens=WINDOW,
)
LIMITS = Limits(
    max_tool_rounds=10, time_cap_s=300.0, token_budget=2_000_000, output_cap_chars=200, max_output_tokens=4096
)


def tool_call_reply(prompt: int, completion: int) -> ChatCompletion:
    call = {"id": "call_0", "type": "function", "function": {"name": "look", "arguments": "{}"}}
    return ChatCompletion.model_validate(
        {
            "id": "cmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "vendor/teasel-1",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                }
            ],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
        }
    )


def read_timeout() -> openai.APITimeoutError:
    error = openai.APITimeoutError(request=httpx.Request("POST", "https://llm.example.invalid/v1"))
    error.__cause__ = httpx.ReadTimeout("slow")
    return error


class Client:
    """Answers the first request with a tool call that reported 120,000 prompt tokens, and times out on the second."""

    def __init__(self):
        self.requests: list[dict] = []
        outcomes = [tool_call_reply(120_000, 40), read_timeout()]
        self.chat = type("Chat", (), {"completions": type("Completions", (), {"create": self.create})()})()
        self._outcomes = outcomes

    async def create(self, **request):
        self.requests.append(json.loads(json.dumps(request)))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def close(self):
        return None


async def no_sleep(seconds):
    return None


@pytest.fixture
def abandoned_record():
    client = Client()
    backend = OpenAICompatAdapter(SPEC, 7, LIMITS, client=client, sleep=no_sleep)
    record = record_run(
        [], {"look": lambda arguments: "a few lines of output"}, backend=backend, token_budget=LIMITS.token_budget
    )
    return record, client


def test_b19_the_record_keeps_the_bound_of_the_request_the_run_gave_up_beside_its_reason(abandoned_record):
    """B19: the run ended at its time cap on a request that went out and was never answered; the record's entry says why
    it was given up, that its usage is unknown, and what bounds its prompt: the 120,040 tokens the first reply reported
    and the few hundred bytes that went back, not the million-token window."""
    record, client = abandoned_record
    assert record.outcome["name"] == "time_cap"
    assert record.totals["usage_incomplete"] is True
    entry = record.route["abandoned_request"]
    assert (entry["reason"], entry["usage"]) == ("read_timeout", "unknown")
    assert entry["prompt_bound_by"] == "bytes"
    assert [m["role"] for m in client.requests[1]["messages"]] == ["system", "user", "assistant", "tool"]
    assert 120_040 < entry["prompt_bound_tokens"] < 120_040 + 1_000
    assert (entry["reply_max_tokens"], entry["window_tokens"]) == (4096, WINDOW)
    assert (entry["bound_checked"], entry["bound_exceeded"], entry["bound_unreported"]) == (0, 0, 0)


def test_b19_the_entry_is_written_loaded_and_replayed_as_it_was(abandoned_record, tmp_path):
    """B19: the entry is route data the schema does not enumerate, so what keeps it is the route's own round trip: the
    stored record holds it, a load returns it and a replay serves it back, byte for byte."""
    record, _ = abandoned_record
    stored = load_record(record.write(tmp_path))
    assert stored.route["abandoned_request"] == record.route["abandoned_request"]
    replayed = replay_record(stored)
    assert replayed.route["abandoned_request"] == record.route["abandoned_request"]
    assert replayed.dumps() == stored.dumps()


def test_b19_a_scorer_prices_the_request_from_the_record_alone(abandoned_record):
    """B19: with the record's entry and its totals, and the model's price, the cost bound of the abandoned request is a
    fraction of the loose bound's three dollars; the totals say whether the attempt was held to its
    reply maximum, and this one, run with no budget, was not."""
    record, _ = abandoned_record
    held = held_to_reply_maximum(record.totals)
    assert held is False
    entry = record.route["abandoned_request"]
    tight = abandoned_request_bound(SPEC, LIMITS, entry, reply_capped=True)
    loose = abandoned_request_bound(SPEC, LIMITS, {"reason": "read_timeout", "usage": "unknown"}, reply_capped=True)
    assert tight.prompt_by == "bytes"
    assert loose.prompt_by == "window"
    assert tight.request_bound_usd == pytest.approx(entry["prompt_bound_tokens"] * 3.0 / 1e6 + 4096 * 15.0 / 1e6)
    assert loose.request_bound_usd == pytest.approx(WINDOW * 3.0 / 1e6 + 4096 * 15.0 / 1e6)
    assert tight.request_bound_usd < loose.request_bound_usd / 5
    unheld = abandoned_request_bound(SPEC, LIMITS, entry, reply_capped=held)
    assert (unheld.request_bound_tokens, unheld.reply_by) == (WINDOW, "window")
