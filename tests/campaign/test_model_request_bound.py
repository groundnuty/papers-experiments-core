"""B21 in a campaign: an attempt whose model request was given up at the study's bound is charged and run again.

The backend is the core's OpenAI-compatible adapter against a fake client, built by the campaign's backend factory, and
the runs go through the real scheduler, executor, recording and loop. The endpoint of the first attempt dispatched takes
a request and never answers; every other answers.
"""

import asyncio

from loop_support import FakeProvider
from openai.types.chat import ChatCompletion
from record_support import CODE, make_config
from scheduler_support import FakeExecutor, attempts_of, build, settings_for

from expcore.campaign.executor import record_run
from expcore.models.adapters import OpenAICompatAdapter

BOUND_S = 0.05  # real seconds: the hung request is given up at once
SMALL = {"tasks": ("task-a",), "seeds": (1,)}  # three runs, one per condition, dispatched one at a time


def final_reply() -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "cmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "m1",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }
    )


class Client:
    """A fake SDK client: answers every request, or takes each and never answers it."""

    def __init__(self, *, hangs: bool):
        self.hangs, self.requests = hangs, 0
        self.chat = type("Chat", (), {"completions": type("Completions", (), {"create": self.create})()})()

    async def create(self, **request):
        self.requests += 1
        if self.hangs:
            await asyncio.Event().wait()
        return final_reply()

    async def close(self):
        return None


class AdapterFactory:
    """Builds the core's adapter for admission and every run; the backend of the first run hangs."""

    def __init__(self):
        self.clients: list[Client] = []

    def __call__(self, spec, *, seed, limits, base_url, time_left):
        client = Client(hangs=len(self.clients) == 1)  # 0 is admission's probe, 1 the first attempt
        self.clients.append(client)
        return OpenAICompatAdapter(spec, seed, limits, base_url=base_url, client=client, time_left=time_left)


def bounded_executor() -> FakeExecutor:
    """The study's executor: every run through the core's recording, under a declared bound on one request."""
    config = make_config(model_request_bound_s=BOUND_S)

    async def run(request):
        tools = [FakeProvider({"look": lambda arguments: "ok"})]
        record = await record_run(
            request, tools, "be brief", "do the task", config, code=dict(CODE), core_version="0.1.0"
        )
        executor.records.append(record)
        return record

    executor = FakeExecutor(config=config, default=run)
    return executor


def test_b21_an_attempt_given_up_at_the_request_bound_is_charged_and_the_run_goes_again(tmp_path):
    """B21 and C03: the first attempt ends as `endpoint` infrastructure once its request passes the bound, is charged,
    and the run goes again from the same row; its second attempt is answered and every run is done. The hung request
    was sent once."""
    factory = AdapterFactory()
    executor = bounded_executor()
    settings = settings_for(max_attempts=3, unit_fail_threshold=10)
    with build(tmp_path, executor=executor, factory=factory, settings=settings, plan=SMALL) as scheduler:
        summary = scheduler.run()
        key = executor.requests[0].run_key  # the run whose first attempt got the backend that hangs
        assert attempts_of(scheduler.store, key) == [("infra_fail", 1, 1), ("done", 0, 2)]
        row = scheduler.store.q("SELECT infra_kind FROM attempts WHERE run_key=? AND attempt=1", (key,))[0]
        assert row["infra_kind"] == "endpoint"
        assert summary.statuses == {"done": 3}
    first = next(r for r in executor.records if r.run_key == key and r.attempt == 1)
    assert (first.outcome["name"], first.outcome["infra_kind"]) == ("infrastructure", "endpoint")
    assert first.route["abandoned_request"]["reason"] == "request_bound"
    assert first.agent_config["model_request_bound_s"] == BOUND_S
    assert factory.clients[1].requests == 1
