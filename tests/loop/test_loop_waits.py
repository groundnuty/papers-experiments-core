"""Waits on infrastructure have a kind from a closed vocabulary, and the seconds are kept per kind (L04).

The kinds are the harness's own machinery: retries, queues for a slot or lease, and the start-up and teardown of the
sandbox and of a tool server. Nothing the agent's own call does, however slow, is a kind.
"""

import asyncio

import pytest
from loop_support import FakeProvider

from expcore.loop.timer import AgentTimer, WaitContext
from expcore.models.adapters import StubAdapter
from expcore.plugin import NO_WAITS, WAIT_KINDS, CallContext

KINDS = ("model_retry", "lease_queue", "sandbox_lifecycle", "toolserver_lifecycle")


def make_timer(clock, **kwargs):
    timer = AgentTimer(300, clock=clock, **kwargs)
    timer.start()
    return timer


def test_l04_the_kinds_are_a_closed_vocabulary_of_the_harness_machinery_only():
    """L04: four kinds, none of which can cover tool execution, data access or a federation operation."""
    assert tuple(WAIT_KINDS) == KINDS
    banned = ("tool_exec", "data", "federation", "transfer", "replicat", "read", "write", "call")
    assert not [k for k in WAIT_KINDS if any(word in k for word in banned)]
    assert all(WAIT_KINDS.values())


def test_l04_the_call_context_says_the_agents_own_work_is_never_infrastructure():
    """L04: the contract a provider reads states what is not a wait, and names the kinds."""
    doc = CallContext.__doc__ or ""
    for kind in KINDS:
        assert kind in doc
    assert "never infrastructure" in doc
    for work in ("executing a tool", "reading or", "federation operation"):
        assert work in doc


@pytest.mark.parametrize("kind", [None, "", "tool_execution", "data_access", "federation", "LEASE_QUEUE", "queue", 3])
def test_l04_a_kind_outside_the_vocabulary_raises_at_the_call_naming_it(clock, kind):
    """L04: the call itself raises, before the wait is entered, whichever way the wait is reached."""
    timer = make_timer(clock)
    for reach in (timer, WaitContext(timer), NO_WAITS):
        with pytest.raises(ValueError, match=f"unknown infrastructure wait kind {kind!r}"):
            reach.infrastructure_wait(kind)
    assert timer.readings == [("start", clock.now)]


def test_l04_no_kind_at_all_raises_at_the_call(clock):
    """L04: a wait without a kind is a call that fails."""
    timer = make_timer(clock)
    for reach in (timer, WaitContext(timer), NO_WAITS):
        with pytest.raises(TypeError):
            reach.infrastructure_wait()


def test_l04_a_provider_that_names_a_wrong_kind_is_a_harness_defect_not_a_hidden_cost(run, clock):
    """L04: the wrong kind is the provider's defect, recorded as such; the call's time stays the agent's."""

    async def handler(args, ctx):
        async with ctx.infrastructure_wait("data_access"):
            clock.advance(50)
        return "never"

    result = run(StubAdapter([[("fetch", {})], "x"]), [FakeProvider({"fetch": handler})])
    assert result.outcome.name == "harness_defect"
    assert "ValueError" in result.tool_calls[0].detail
    assert result.infra_wait_by_kind == {}
    assert result.infra_wait_s == 0


def test_l04_each_call_records_the_seconds_it_waited_by_kind_and_the_run_the_totals(run, clock, sleeper):
    """L04: sequential waits of two kinds in one call are kept apart, per call and for the run."""

    async def handler(args, ctx):
        async with ctx.infrastructure_wait("lease_queue"):
            clock.advance(30)
        clock.advance(5)  # the agent's own time
        async with ctx.infrastructure_wait("sandbox_lifecycle"):
            clock.advance(20)
        return "ok"

    result = run(
        StubAdapter([[("fetch", {})], [("fetch", {})], "done"]),
        [FakeProvider({"fetch": handler})],
        sleep=sleeper,
    )
    assert result.outcome.name == "finished"
    assert [c.infra_wait_by_kind for c in result.tool_calls] == [{"lease_queue": 30, "sandbox_lifecycle": 20}] * 2
    assert result.infra_wait_by_kind == {"lease_queue": 60, "sandbox_lifecycle": 40}
    assert (result.infra_wait_s, result.agent_s) == (100, 10)


def test_l04_a_call_with_no_wait_records_no_kind_however_long_it_takes(run, clock, sleeper):
    """L04: tool execution is the agent's time; no kind is recorded for it."""

    async def handler(args):
        clock.advance(120)
        return "slow work"

    result = run(StubAdapter([[("fetch", {})], "done"]), [FakeProvider({"fetch": handler})], sleep=sleeper)
    assert result.tool_calls[0].infra_wait_by_kind == {}
    assert result.infra_wait_by_kind == {}
    assert (result.infra_wait_s, result.agent_s) == (0, 120)


def test_l04_overlapping_waits_pause_the_clock_for_their_union_and_each_kind_counts_its_own_union(run, clock, sleeper):
    """L04: a lease wait of 40 s with a sandbox wait of 20 s inside it pauses 40 s; the kinds say 40 and 20."""

    async def handler(args, ctx):
        async with ctx.infrastructure_wait("lease_queue"):
            clock.advance(10)
            async with ctx.infrastructure_wait("sandbox_lifecycle"):
                clock.advance(20)
            clock.advance(10)
        return "ok"

    result = run(StubAdapter([[("fetch", {})], "done"]), [FakeProvider({"fetch": handler})], sleep=sleeper)
    assert result.infra_wait_s == 40
    assert result.infra_wait_by_kind == {"lease_queue": 40, "sandbox_lifecycle": 20}
    assert result.agent_s == 0


def test_l04_two_waits_of_one_kind_that_overlap_are_counted_once(run, clock, sleeper):
    """L04: a kind's seconds are the union of its own waits, not the sum of their lengths."""

    async def handler(args, ctx):
        first, second = ctx.infrastructure_wait("lease_queue"), ctx.infrastructure_wait("lease_queue")
        async with first:
            clock.advance(10)
            async with second:
                clock.advance(30)
            clock.advance(10)
        return "ok"

    result = run(StubAdapter([[("fetch", {})], "done"]), [FakeProvider({"fetch": handler})], sleep=sleeper)
    assert result.infra_wait_by_kind == {"lease_queue": 50}
    assert result.infra_wait_s == 50


def test_l04_a_model_request_records_the_seconds_it_waited_by_kind_on_its_turn(make_config, clock, sleeper, run):
    """L04: a backend that retries marks the backoff as `model_retry`, and the turn and the run say how long."""
    timer = make_config().timer(clock)

    class Retrying(StubAdapter):
        async def step(self):
            async with timer.infrastructure_wait("model_retry"):
                clock.advance(7)
            return await super().step()

    result = run(
        Retrying([[("fetch", {})], "done"]), [FakeProvider({"fetch": lambda a: "ok"})], timer=timer, sleep=sleeper
    )
    assert [t.infra_wait_by_kind for t in result.turns] == [{"model_retry": 7}, {"model_retry": 7}]
    assert result.infra_wait_by_kind == {"model_retry": 14}
    assert result.agent_s == 0


def test_l04_a_wait_left_open_is_counted_to_the_end_of_its_call_under_its_kind(run, clock, sleeper):
    """L04: the loop closes a wait the call left open, and the seconds are in the call's kind."""

    async def handler(args, ctx):
        await ctx.infrastructure_wait("toolserver_lifecycle").__aenter__()
        clock.advance(9)
        return "ok"

    result = run(StubAdapter([[("fetch", {})], "done"]), [FakeProvider({"fetch": handler})], sleep=sleeper)
    assert result.tool_calls[0].waits_left_open == 1
    assert result.tool_calls[0].infra_wait_by_kind == {"toolserver_lifecycle": 9}
    assert result.infra_wait_by_kind == {"toolserver_lifecycle": 9}


def test_l04_a_cut_ends_the_open_waits_of_each_kind_at_the_cut(run, clock, sleeper):
    """L04: a call cut while it waits keeps the seconds waited up to the cut, by kind."""

    async def handler(args, ctx):
        async with ctx.infrastructure_wait("lease_queue"):
            while True:
                clock.advance(100)
                await asyncio.sleep(0)

    result = run(
        StubAdapter([[("fetch", {})], "x"]),
        [FakeProvider({"fetch": handler})],
        sleep=sleeper,
        time_cap_s=300,
        ceiling_s=500,
    )
    assert result.outcome.name == "infrastructure"
    waited = result.tool_calls[0].infra_wait_by_kind
    assert list(waited) == ["lease_queue"]
    assert waited["lease_queue"] == result.infra_wait_s
    assert result.infra_wait_by_kind == waited
