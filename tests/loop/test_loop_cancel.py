"""An outside cancellation of the loop leaves no child task of the loop pending."""

import asyncio

import pytest
from loop_support import FakeProvider, never

from expcore.loop.agent_loop import run_agent
from expcore.loop.toolbox import ToolBox
from expcore.models.adapters import StubAdapter


class Hangs(StubAdapter):
    """A backend whose request never returns; it needs the loop to clean up after it is cancelled."""

    def __init__(self, started, cleaned):
        super().__init__([])
        self.started, self.cleaned = started, cleaned

    async def step(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)  # cleanup that needs the loop: the call has to be awaited, not only cancelled
            self.cleaned.append("model")
            raise


@pytest.mark.parametrize("where", ["model request", "tool call"])
def test_an_outside_cancellation_cancels_and_awaits_the_children_before_it_propagates(make_config, clock, where):
    """Cancelling the whole run while a call is in flight cancels the call and the bound's timer and waits for both."""
    cleaned: list[str] = []
    config = make_config()

    async def scenario():
        started = asyncio.Event()

        async def hangs(args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                cleaned.append("tool")
                raise

        if where == "tool call":
            backend, providers = StubAdapter([[("hang", {})]]), [FakeProvider({"hang": hangs})]
        else:
            backend, providers = Hangs(started, cleaned), [FakeProvider({"t": lambda args: ""})]
        box = ToolBox(providers, cap_chars=config.limits.output_cap_chars)
        task = asyncio.ensure_future(run_agent(backend, box, "s", "u", config, clock=clock, sleep=never))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    pending = asyncio.run(scenario())
    assert pending == []
    assert cleaned == ["tool" if where == "tool call" else "model"]
