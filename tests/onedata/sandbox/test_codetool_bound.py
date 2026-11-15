"""L17: the study's per-call bound is for tool servers and never reaches the code tool. A study that declares
`[agent] tool_server_call_bound_s` to classify a hung tool server must not cap the agent's code cells at it, nor change
what the model is told when a cell runs long: the sandbox is handed the agent's time left, as it is without the key."""

import asyncio

from loop_support import FakeClock, ScriptedBackend, call, never, turn

from expcore.config.models import Limits
from expcore.loop.agent_loop import run_agent
from expcore.loop.config import AgentConfig
from expcore.loop.toolbox import ToolBox
from expcore_onedata.sandbox.api import ExecResult
from expcore_onedata.sandbox.codetool import CodeExecutionProvider

CAP_S = 300.0
BOUND_S = 60.0
LIMITS = Limits(
    max_tool_rounds=5, time_cap_s=CAP_S, token_budget=100_000, output_cap_chars=8000, max_output_tokens=4096
)
TIME_LIMIT = {"reason": "time_limit", "category": "agent", "basis": "the cell ran to its limit"}


class ScriptedSandbox:
    """A sandbox whose `exec` records the limit it was handed and answers as a cell that ran out of that limit."""

    def __init__(self) -> None:
        self.limits: list[float] = []

    def exec(self, code: str, timeout_s: float) -> ExecResult:
        self.limits.append(timeout_s)
        return ExecResult("partial output", "", 137, timeout_s, timed_out=True, kill=TIME_LIMIT)


def run_one_cell(**config):
    """The loop over the code tool on a fake clock, with one call that runs a cell; returns the sandbox and result."""
    sandbox = ScriptedSandbox()
    agent = AgentConfig(LIMITS, **config)
    box = ToolBox([CodeExecutionProvider(sandbox)], cap_chars=LIMITS.output_cap_chars)
    backend = ScriptedBackend([turn(call("call-1", "python", {"code": "while True: pass"})), "done"])
    result = asyncio.run(run_agent(backend, box, "system", "task", agent, clock=FakeClock(), sleep=never))
    return sandbox, result


def test_l17_the_code_tool_is_no_tool_server():
    """L17: the code tool does not say it serves a tool server, so the loop never gives it the bound."""
    provider = CodeExecutionProvider(ScriptedSandbox())
    assert getattr(provider, "tool_server", False) is False
    assert ToolBox([provider], cap_chars=100).tool_server_names == []


def test_l17_a_code_cell_is_handed_the_time_left_not_the_declared_bound():
    """L17: with a bound of 60 s declared, the cell still runs to the agent's 300 s: the sandbox is told 300, which is
    what it is told without the key."""
    without, _ = run_one_cell()
    declared, _ = run_one_cell(tool_server_call_bound_s=BOUND_S)
    assert without.limits == [CAP_S]
    assert declared.limits == [CAP_S]


def test_l17_what_the_model_is_told_of_a_cell_that_ran_out_of_its_time_does_not_depend_on_the_key():
    """L17: "the code did not finish within the time limit" is the same text, the same error class and the same record
    of the call with the bound declared and without it, and the run ends the same way."""
    _, without = run_one_cell()
    _, declared = run_one_cell(tool_server_call_bound_s=BOUND_S)
    assert "the code did not finish within the time limit and was stopped" in declared.tool_calls[0].text
    assert declared.tool_calls[0].text == without.tool_calls[0].text
    assert declared.tool_calls[0].error_class == without.tool_calls[0].error_class == "timeout"
    assert declared.tool_calls[0].status == without.tool_calls[0].status == "error"
    assert declared.outcome == without.outcome
    assert declared.config["tool_server_tools"] == []  # the record says the bound reached no tool
