"""Fixtures for the loop tests. The helper classes live in tests/support/loop_support.py."""

import asyncio
import dataclasses

import pytest
from loop_support import FakeClock, never

from expcore.config.models import Limits
from expcore.loop.agent_loop import run_agent
from expcore.loop.config import AgentConfig
from expcore.loop.toolbox import ToolBox


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_config():
    """Return a builder of loop configurations; overrides go to the limits or, for the policy fields, to the config."""

    def build(**overrides):
        limit_names = {f.name for f in dataclasses.fields(Limits)}
        limits = {
            "max_tool_rounds": 10,
            "time_cap_s": 300.0,
            "token_budget": 100_000,
            "output_cap_chars": 8000,
            "max_output_tokens": 4096,
        }
        limits.update({k: v for k, v in overrides.items() if k in limit_names})
        rest = {k: v for k, v in overrides.items() if k not in limit_names}
        return AgentConfig(Limits(**limits), **rest)

    return build


@pytest.fixture
def run(make_config, clock):
    """Return a runner: run(backend, providers, **overrides) -> LoopResult, on the fake clock.

    The default sleep never ends by itself, so a bound passes only when a test says so.
    """

    def go(backend, providers, *, sleep=never, system="system", user="task", **overrides):
        config = make_config(**overrides)
        box = ToolBox(list(providers), cap_chars=config.limits.output_cap_chars)
        return asyncio.run(run_agent(backend, box, system, user, config, clock=clock, sleep=sleep))

    return go
