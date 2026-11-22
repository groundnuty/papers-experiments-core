"""B21: what a record keeps of the study's bound on one model request, and that a replay ends the run alike.

The bound is a value of the agent's configuration, `model_request_bound_s`, optional in version 3: absent when the study
declared none (never a null), with no list beside it, and unknown to a record of version 2. A request given up at it is
an ordinary failed request of kind `infra` (`endpoint`); the route's abandoned request says why. The adapter here is the
core's own, against a fake client that answers once and then never again, driven through the real loop and recording.
"""

import asyncio
import json

import pytest
from openai.types.chat import ChatCompletion
from record_support import record_run

from expcore.config.models import Limits, ModelSpec
from expcore.models.adapters import OpenAICompatAdapter, StubAdapter
from expcore.records.record import RunRecord, decode
from expcore.records.schema import RecordError, validate
from expcore.replay.replay import replay_record

V2, V3 = "expcore-record/2", "expcore-record/3"
BOUND_S = 0.05  # real seconds: the fake client never answers, and the record's clock is the fake one
SPEC = ModelSpec(
    key="teasel",
    backend="openai_compat",
    api_id="vendor/teasel-1",
    status="frozen",
    base_url="https://llm.example.invalid/v1",
    context_tokens=1_000_000,
)
LIMITS = Limits(
    max_tool_rounds=10, time_cap_s=300.0, token_budget=100_000, output_cap_chars=200, max_output_tokens=4096
)


def tool_call_reply() -> ChatCompletion:
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
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        }
    )


class AnswersOnceThenHangs:
    """Answers the first request with a tool call, takes the second and never answers it."""

    def __init__(self):
        self.requests: list[dict] = []
        self.chat = type("Chat", (), {"completions": type("Completions", (), {"create": self.create})()})()

    async def create(self, **request):
        self.requests.append(json.loads(json.dumps(request)))
        if len(self.requests) == 1:
            return tool_call_reply()
        await asyncio.Event().wait()

    async def close(self):
        return None


async def no_sleep(seconds):
    return None


def given_up_at_the_bound(**config):
    """The record of a run whose second model request was given up at the bound, and the fake client it ran against."""
    client = AnswersOnceThenHangs()
    backend = OpenAICompatAdapter(SPEC, 7, LIMITS, client=client, sleep=no_sleep)
    record = record_run([], {"look": lambda arguments: "a few lines"}, backend=backend, **config)
    return record, client


def test_b21_a_request_given_up_at_the_bound_is_endpoint_infrastructure_in_the_record():
    """B21: the run ends as `endpoint` infrastructure after one turn and one tool call; the failed request is kind
    `infra` with the bound in its detail, the abandoned request says `request_bound`, the usage is incomplete, and the
    hung request was sent once."""
    record, client = given_up_at_the_bound(model_request_bound_s=BOUND_S)
    assert (record.outcome["name"], record.outcome["infra_kind"]) == ("infrastructure", "endpoint")
    assert len(record.transcript) == 1
    assert [c["status"] for c in record.tool_calls] == ["ok"]
    failed = record.failed_request
    assert (failed["kind"], failed["infra_kind"]) == ("infra", "endpoint")
    assert failed["detail"] == "the model endpoint did not answer a request within 0.05 s; not resent"
    assert record.route["abandoned_request"]["reason"] == "request_bound"
    assert record.totals["usage_incomplete"] is True
    assert len(client.requests) == 2
    assert record.agent_config["model_request_bound_s"] == BOUND_S


def test_b21_the_record_validates_writes_back_and_replays_to_its_own_bytes():
    """B21 and R03: the replay raises the recorded failure where the run gave the request up, reads the clock as the run
    did (the bound's expiry takes no reading), and builds the same record."""
    record, _ = given_up_at_the_bound(model_request_bound_s=BOUND_S)
    text = record.dumps()
    assert validate(decode(text)) == V3
    assert RunRecord.from_json(decode(text)).dumps() == text
    assert replay_record(record).dumps() == text


def test_b21_an_undeclared_bound_leaves_agent_config_as_it_was_and_a_declared_one_is_a_value_of_it():
    """B21: absent when the study declared none, never a null and with no list beside it; present as the number when it
    declared one, covered by the configuration's digest."""
    plain = record_run(["hello"], {})
    assert "model_request_bound_s" not in plain.agent_config
    declared = record_run(["hello"], {}, model_request_bound_s=600.0)
    assert declared.agent_config["model_request_bound_s"] == 600.0
    assert list(declared.agent_config)[-2:] == ["model_request_bound_s", "terminal_tools"]
    assert declared.agent_config_sha256 != plain.agent_config_sha256
    assert replay_record(declared).dumps() == declared.dumps()


@pytest.mark.parametrize("bad", [None, 0, -1.0, "600", True])
def test_b21_a_bound_that_is_no_positive_number_is_refused(bad):
    """B21: a null, nothing, a negative, a string or a flag is refused, naming the key."""
    data = decode(record_run(["hello"], {}, model_request_bound_s=600.0).dumps())
    data["agent_config"]["model_request_bound_s"] = bad
    with pytest.raises(RecordError, match=r"agent_config\.model_request_bound_s"):
        validate(data)


def test_b21_a_record_of_version_two_knows_no_such_key():
    """B21: the key is one of version 3's optional additions, so a version 2 record that holds it is refused."""
    data = decode(record_run(["hello"], {}, record_options={"schema_version": V2}).dumps())
    data["agent_config"]["model_request_bound_s"] = 600.0
    with pytest.raises(RecordError, match="model_request_bound_s"):
        validate(data)


class OwnBackend(StubAdapter):
    """Stands in for a backend of a study's own that cannot take the bound: it has no `bound_requests`."""

    bound_requests = None


def test_b21_the_recording_is_judged_by_the_backend_it_wraps_and_a_backend_without_the_method_is_refused():
    """B21: the recording wrapper exposes the wrapped backend's `bound_requests`, so a declared bound reaches a core
    adapter through it, and a backend that has none is refused by name before anything is sent, not by the wrapper's."""
    backend = OwnBackend(["done"])
    with pytest.raises(ValueError, match="the model backend OwnBackend has no bound_requests"):
        record_run([], {}, backend=backend, model_request_bound_s=600.0)
    assert backend.requests_sent == 0
