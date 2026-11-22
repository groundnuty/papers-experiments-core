"""B21: the loop under the study's bound on one model request: when it arms the bound, and how a request given up at it
ends the attempt.

The loop hands the bound to the backend before each step (`bound_requests`) while it is strictly shorter than both the
agent's time left and the ceiling left at the reading before the step, and None otherwise, so the step is then watched
and cut as without the key. The adapters' side, the send given up at the bound and never resent, is in
`tests/models/test_adapters_request_bound.py`; the record and the replay are in `tests/records`.
"""

import asyncio

from loop_support import FakeProvider, slow

from expcore.errors import ModelRequestTimedOut
from expcore.loop.config import AgentConfig
from expcore.models.adapters import StubAdapter


class TakesTheBound(StubAdapter):
    """A scripted backend that keeps every bound the loop hands it. Request number `stall_at`, when given, runs
    `seconds` on the fake clock and then never returns."""

    def __init__(self, script, clock=None, *, stall_at=None, seconds=0.0):
        super().__init__(script)
        self.handed: list[float | None] = []
        self.clock, self.stall_at, self.seconds = clock, stall_at, seconds

    def bound_requests(self, seconds):
        self.handed.append(seconds)

    async def step(self):
        if self._i == self.stall_at:
            self.clock.advance(self.seconds)
            await asyncio.Event().wait()
        return await super().step()


def test_b21_the_bound_is_handed_while_it_is_the_shorter_and_lifted_once_the_time_left_is(run, clock):
    """B21: with 300 s left the step gets the bound of 100 s; after a call of 250 s only 50 s are left, so the next step
    gets None and is the loop's to cut, as without the key."""
    backend = TakesTheBound([[("slow", {})], "done"])
    result = run(backend, [FakeProvider({"slow": slow(clock, 250)})], time_cap_s=300, model_request_bound_s=100)
    assert backend.handed == [100.0, None]
    assert result.outcome.name == "finished"


def test_b21_a_bound_at_the_time_left_is_not_armed_and_a_hang_is_the_agents_time_cap(run, clock, sleeper):
    """B21 and L16: a bound equal to the agent's time left would expire at the moment of the loop's cut, so it is not
    armed. A request that then hangs for the whole cap is cut by the loop: the agent's time cap, its usage unknown, as
    without the key, and never `endpoint` infrastructure."""
    backend = TakesTheBound([], clock, stall_at=0, seconds=300)
    result = run(
        backend, [FakeProvider({"t": lambda a: "x"})], sleep=sleeper, time_cap_s=300, model_request_bound_s=300
    )
    assert backend.handed == [None]
    assert (result.outcome.name, result.outcome.infra_kind) == ("time_cap", None)
    assert result.usage_incomplete is True


def test_b21_a_bound_at_or_past_the_ceiling_left_is_not_armed(run, clock):
    """B21: a call that waited 250 s on infrastructure leaves the agent's 300 s untouched but only 50 s of the ceiling
    of 300 s; a bound of 100 s is then not the first deadline, and the step gets None (the ceiling's cut decides)."""
    backend = TakesTheBound([[("store", {})], "done"])
    provider = FakeProvider({"store": slow(clock, 250, wait=250)})
    result = run(backend, [provider], time_cap_s=300, ceiling_s=300, model_request_bound_s=100)
    assert backend.handed == [100.0, None]
    assert result.outcome.name == "finished"


def test_b21_an_undeclared_bound_is_never_handed_and_leaves_the_configuration_as_it_was(run, make_config):
    """B21: without the key the backend is never told anything, and the configuration the record keeps and hashes is
    the one it was before the key existed: no key, never a null, and the same hash."""
    backend = TakesTheBound(["done"])
    result = run(backend, [FakeProvider({"t": lambda a: "x"})])
    assert backend.handed == []
    assert "model_request_bound_s" not in result.config
    plain = make_config()
    assert "model_request_bound_s" not in plain.as_dict()
    assert plain.config_sha256() == AgentConfig(plain.limits, model_request_bound_s=None).config_sha256()
    assert make_config(model_request_bound_s=600).config_sha256() != plain.config_sha256()


def test_b21_a_request_given_up_at_the_bound_ends_the_attempt_as_endpoint_infrastructure(run):
    """B21: the backend's `ModelRequestTimedOut` is an infrastructure failure of kind `endpoint`, which the campaign
    charges and runs again (C02), and not the agent's time cap."""
    backend = TakesTheBound([ModelRequestTimedOut(100.0)])
    result = run(backend, [FakeProvider({"t": lambda a: "x"})], time_cap_s=300, model_request_bound_s=100)
    assert backend.handed == [100.0]
    assert (result.outcome.name, result.outcome.infra_kind) == ("infrastructure", "endpoint")
    assert result.outcome.detail == "the model endpoint did not answer a request within 100 s; not resent"
