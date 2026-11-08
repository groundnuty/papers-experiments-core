"""The code tool: runs the agent's code in the sandbox, never inside a wait, and tells kills by cause (O17)."""

import asyncio
import threading

import pytest

from expcore.errors import InfraError
from expcore.plugin import NO_WAITS, ToolProvider
from expcore_onedata.sandbox.api import ExecResult
from expcore_onedata.sandbox.codetool import CodeExecutionProvider


class Waits:
    """A call context that fails the test if a wait is opened: executing code is the agent's own time."""

    def infrastructure_wait(self, kind):
        raise AssertionError(f"executing the agent's code opened a wait of kind {kind!r}")


class ScriptedSandbox:
    """A sandbox whose `exec` returns what the test scripted, and records where it ran."""

    def __init__(self, result: ExecResult):
        self.result, self.calls, self.thread = result, [], None

    def exec(self, code: str, timeout_s: float) -> ExecResult:
        self.calls.append((code, timeout_s))
        self.thread = threading.current_thread()
        return self.result


def ok(stdout="", stderr="", exit_code=0, **kw) -> ExecResult:
    return ExecResult(stdout, stderr, exit_code, 0.1, **kw)


def run(provider, arguments, *, timeout_s=30.0, name="python", ctx=None):
    return asyncio.run(provider.execute(name, arguments, timeout_s=timeout_s, ctx=ctx or Waits()))


def test_it_is_a_tool_provider_with_one_tool_the_study_names():
    provider = CodeExecutionProvider(ScriptedSandbox(ok()), name="run_code", description="Run code.")
    assert isinstance(provider, ToolProvider)
    (spec,) = provider.tools()
    assert (spec.name, spec.description, spec.terminal) == ("run_code", "Run code.", False)
    assert spec.input_schema["required"] == ["code"]


def test_the_codes_output_is_what_the_model_sees_and_no_wait_is_opened():
    """L04: running the agent's code and its reads of data are the agent's time; the context fails on any wait."""
    sandbox = ScriptedSandbox(ok("42\n"))
    out = run(CodeExecutionProvider(sandbox), {"code": "print(42)"})
    assert (out.text, out.is_error) == ("42\n", False)
    assert sandbox.calls == [("print(42)", 30.0)]


def test_the_code_does_not_run_on_the_event_loop():
    """L15: blocking work runs in a thread."""
    sandbox = ScriptedSandbox(ok())
    run(CodeExecutionProvider(sandbox), {"code": "x = 1"})
    assert sandbox.thread is not threading.main_thread()


def test_the_time_left_bounds_the_call_and_is_never_below_the_least_the_sandbox_takes():
    sandbox = ScriptedSandbox(ok())
    run(CodeExecutionProvider(sandbox), {"code": "x = 1"}, timeout_s=0.2)
    assert sandbox.calls[0][1] == 1.0


def test_an_exception_in_the_agents_code_is_a_result_the_model_sees_not_an_error_of_ours():
    out = run(
        CodeExecutionProvider(ScriptedSandbox(ok("partial\n", "Traceback...\nValueError: x\n", 1))), {"code": "1/0"}
    )
    assert out.is_error
    assert out.error_class == "exit_status"
    assert out.text.startswith("partial")
    assert "exit code 1" in out.text
    assert "ValueError" in out.text


def test_stderr_of_a_successful_run_is_shown_after_its_output():
    out = run(CodeExecutionProvider(ScriptedSandbox(ok("a\n", "warn\n"))), {"code": "x"})
    assert (out.text, out.is_error) == ("a\nwarn\n", False)


def test_o17_the_agents_own_memory_limit_is_its_outcome_and_is_told_as_such():
    """O17: The agents own memory limit is its outcome and is told as such."""
    kill = {"reason": "memory_limit", "category": "agent", "basis": "the container limit"}
    out = run(CodeExecutionProvider(ScriptedSandbox(ok("", "", 137, kill=kill))), {"code": "x = [0] * 10**12"})
    assert out.is_error
    assert out.error_class == "killed"
    assert "memory limit" in out.text


def test_o17_the_agents_own_time_limit_is_a_timeout_result():
    """O17: The agents own time limit is a timeout result."""
    kill = {"reason": "time_limit", "category": "agent", "basis": "the cell ran to its limit"}
    out = run(
        CodeExecutionProvider(ScriptedSandbox(ok("", "", 137, timed_out=True, kill=kill))), {"code": "while 1: pass"}
    )
    assert out.error_class == "timeout"


@pytest.mark.parametrize(("category", "kind"), [("infrastructure", "sandbox"), ("unknown", "unattributed_kill")])
def test_o17_a_kill_the_host_or_nothing_explains_ends_the_attempt_as_infrastructure(category, kind):
    """O17: the exit code is 137 in every case; the counters decide, and infrastructure is raised, not shown."""
    kill = {"reason": "host_oom", "category": category, "basis": "the counters"}
    provider = CodeExecutionProvider(ScriptedSandbox(ok("", "", 137, kill=kill)))
    with pytest.raises(InfraError) as caught:
        run(provider, {"code": "x"})
    assert caught.value.kind == kind


@pytest.mark.parametrize("arguments", [{}, {"code": ""}, {"code": "  \n"}, {"code": 5}, {"other": "x"}])
def test_bad_arguments_are_an_error_result_for_the_model_and_the_code_does_not_run(arguments):
    sandbox = ScriptedSandbox(ok())
    out = run(CodeExecutionProvider(sandbox), arguments)
    assert out.is_error
    assert out.error_class == "bad_arguments"
    assert sandbox.calls == []


def test_a_call_for_another_tool_is_an_error_result():
    out = run(CodeExecutionProvider(ScriptedSandbox(ok())), {"code": "x"}, name="other")
    assert (out.is_error, out.error_class) == (True, "unknown_tool")


def test_the_waits_it_never_opens_are_still_kinds_of_the_cores_vocabulary():
    """The only waits of the platform's sandbox are the core's `sandbox_lifecycle`; the no-wait context accepts it."""
    NO_WAITS.infrastructure_wait("sandbox_lifecycle")
    with pytest.raises(ValueError, match="unknown infrastructure wait kind"):
        NO_WAITS.infrastructure_wait("code_execution")
