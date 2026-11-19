"""L18: the code tool under the study's limit on one cell (`[agent] code_cell_bound_s`).

With the limit shorter than the agent's time left, the loop hands the code tool the limit as its `timeout_s` and says
so through the call context. The sandbox kills a cell that runs past it, as it kills a cell at any limit (O34), and
the call returns the code tool's timeout with the output the cell kept and a text that names the limit. That result is
the agent's own: the run goes on, and the agent's next turn runs. A cell that reaches the time left first is the
loop's cut, as it always was (`tests/loop/test_code_cell_bound.py`), and a cell that outlives its kill is still
`cell_unkillable`, infrastructure, which ends the run.

Both backends reach the code tool through `Sandbox.exec(code, timeout_s)` and report a cell killed at its limit the same
way (`timed_out`, a `time_limit` kill of the agent's category): the Docker sandbox over its fake host is driven here
through the loop, the Apptainer one next to its own cell tests (`apptainer/test_cells.py`).
"""

import asyncio
import subprocess
import sys
import threading
import time

from deadline_support import CELL_S, cell_processes
from loop_support import CellBoundContext, FakeClock, ScriptedBackend, call, never, turn

from expcore.config.models import Limits
from expcore.errors import InfraError
from expcore.loop.agent_loop import run_agent
from expcore.loop.config import AgentConfig
from expcore.loop.toolbox import ToolBox
from expcore_onedata.sandbox import kinds
from expcore_onedata.sandbox.api import ExecResult
from expcore_onedata.sandbox.codetool import CodeExecutionProvider
from expcore_onedata.sandbox.hostops import CmdResult

CAP_S = 300.0
LIMITS = Limits(
    max_tool_rounds=5, time_cap_s=CAP_S, token_budget=100_000, output_cap_chars=8000, max_output_tokens=4096
)
TIME_LIMIT = {"reason": "time_limit", "category": "agent", "basis": "the cell ran to its limit"}
SURVIVED = {"reason": "kill_survived", "category": "infrastructure", "basis": "x", "deadline": {"outcome": "x"}}
SLEEP_30 = "import time\nprint('started', flush=True)\ntime.sleep(30)"


class ScriptedSandbox:
    """A sandbox that records the limit each cell was handed. A cell whose code says `sleep N` runs N s: it is killed
    at its limit, keeping `partial`, when that is shorter, and returns `ok` otherwise."""

    def __init__(self) -> None:
        self.limits: list[float] = []

    def exec(self, code: str, timeout_s: float) -> ExecResult:
        self.limits.append(timeout_s)
        seconds = float(code.split("sleep", 1)[1]) if "sleep" in code else 0.0
        if seconds > timeout_s:
            return ExecResult("partial", "", 137, timeout_s, timed_out=True, kill=TIME_LIMIT)
        return ExecResult("ok", "", 0, seconds)


class QueueSandbox:
    """A sandbox that answers each cell with the next step it was given: a result, or an error it raises."""

    def __init__(self, *steps) -> None:
        self.steps, self.limits = list(steps), []

    def exec(self, code: str, timeout_s: float) -> ExecResult:
        self.limits.append(timeout_s)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class ProcessSandbox:
    """A sandbox that runs each cell as a real process of its own and kills it at its limit, as the real ones do."""

    def __init__(self) -> None:
        self.limits: list[float] = []

    def exec(self, code: str, timeout_s: float) -> ExecResult:
        self.limits.append(timeout_s)
        started = time.monotonic()
        try:
            done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as expired:
            kept = expired.stdout if isinstance(expired.stdout, bytes) else (expired.stdout or "").encode()
            return ExecResult(kept.decode(), "", 137, time.monotonic() - started, timed_out=True, kill=TIME_LIMIT)
        return ExecResult(done.stdout.decode(), done.stderr.decode(), done.returncode, time.monotonic() - started)


class FakeContext:
    """A call context that names no limit and measures no wait: what a provider has outside the loop."""

    def infrastructure_wait(self, kind):
        raise AssertionError("no wait is opened")


def run_cells(sandbox, codes, *, clock=None, **config):
    """The loop over the code tool, one call per code in `codes`, then a final answer; returns the loop's result."""
    box = ToolBox([CodeExecutionProvider(sandbox)], cap_chars=LIMITS.output_cap_chars)
    backend = ScriptedBackend(
        [turn(call(f"call-{i}", "python", {"code": code})) for i, code in enumerate(codes)] + ["done"]
    )
    options = {"clock": clock, "sleep": never} if clock is not None else {}
    return asyncio.run(run_agent(backend, box, "system", "task", AgentConfig(LIMITS, **config), **options))


def test_l18_the_code_tool_is_a_code_cell_provider_and_no_tool_server():
    """L18: the provider says its tool runs code cells, which is how the loop knows which calls the limit is for, and
    the tool box lists it by name. It is no tool server, so the tool server's bound never reaches it (L17)."""
    provider = CodeExecutionProvider(ScriptedSandbox())
    assert provider.code_cell is True
    box = ToolBox([provider], cap_chars=100)
    assert box.code_cell_names == ["python"]
    assert box.tool_server_names == []


def test_l18_a_cell_that_outlasts_the_limit_is_killed_at_it_told_to_the_agent_and_the_next_turn_runs():
    """L18: with a 2 s limit and plenty of time left, a cell that sleeps 30 s is handed 2 s and killed there. The agent
    gets the code tool's timeout result, with the output the cell kept, naming the limit and saying it was killed, and
    its next turn runs: the run is not ended, not infrastructure, not a time cap."""
    sandbox = ScriptedSandbox()
    result = run_cells(sandbox, ["sleep 30", "print(1)"], clock=FakeClock(), code_cell_bound_s=2)
    assert sandbox.limits == [2.0, 2.0]
    first, second = result.tool_calls
    assert (first.status, first.error_class) == ("error", "timeout")
    assert first.text == "timeout: partial\nthe code ran past its 2 s limit and was killed"
    assert (second.status, second.text) == ("ok", "ok")
    assert (result.outcome.name, result.outcome.infra_kind) == ("finished", None)


def test_l18_a_real_process_that_sleeps_past_the_limit_is_killed_at_it_and_the_run_goes_on():
    """L18: the same through a real process on the real clock: a cell that would sleep 30 s is gone after about the 2 s
    it was handed, keeping what it printed, and the agent's next cell runs and finishes the run."""
    sandbox = ProcessSandbox()
    started = time.monotonic()
    result = run_cells(sandbox, [SLEEP_30, "print('next cell')"], code_cell_bound_s=2)
    assert time.monotonic() - started < 20  # killed at ~2 s, not at 30
    assert sandbox.limits == [2.0, 2.0]
    first, second = result.tool_calls
    assert (first.status, first.error_class) == ("error", "timeout")
    assert first.text == "timeout: started\nthe code ran past its 2 s limit and was killed"
    assert (second.status, second.text.strip()) == ("ok", "next cell")
    assert result.outcome.name == "finished"


def test_l18_a_cell_that_reaches_the_time_left_first_has_the_text_it_always_had():
    """L18: a limit past the time left is none: the sandbox is handed the time left and the text of a cell that ran out
    of it is the one it has without the key, byte for byte."""
    declared, plain = ScriptedSandbox(), ScriptedSandbox()
    with_limit = run_cells(declared, ["sleep 900"], clock=FakeClock(), code_cell_bound_s=900)
    without = run_cells(plain, ["sleep 900"], clock=FakeClock())
    assert declared.limits == plain.limits == [CAP_S]
    assert with_limit.tool_calls[0].text == without.tool_calls[0].text
    assert (
        with_limit.tool_calls[0].raw_text
        == "timeout: partial\nthe code did not finish within the time limit and was stopped"
    )
    assert with_limit.tool_calls[0].raw_text == without.tool_calls[0].raw_text


def test_l18_a_cell_that_outlives_its_kill_at_the_limit_still_ends_the_run_as_infrastructure():
    """L18: the limit changes nothing of O34. A cell a process of which outlived its kill returns its timeout as before,
    without telling the agent it was killed since it was not, and the next call is refused as `cell_unkillable`: the
    run's ending, infrastructure, never the agent's."""

    survivor = QueueSandbox(
        ExecResult("partial", "", 137, 2.0, timed_out=True, kill=SURVIVED),  # the first cell: its kill ended no process
        InfraError(kinds.CELL_UNKILLABLE, "the run was ended after cell 1's deadline"),  # the next call is refused
    )
    result = run_cells(survivor, ["sleep 30", "print(1)"], clock=FakeClock(), code_cell_bound_s=2)
    assert (
        result.tool_calls[0].text == "timeout: partial\nthe code did not finish within the time limit and was stopped"
    )
    assert (result.outcome.name, result.outcome.infra_kind) == ("infrastructure", "cell_unkillable")


def test_l18_through_the_docker_sandbox_a_cell_killed_at_the_limit_is_told_and_the_next_cell_runs(world, mounted):
    """L18, O34: the Docker sandbox over its fake host, through the code tool and the loop, on the real clock. The cell
    that runs to its limit is killed by the harness's timer at the 1.5 s the loop handed it, with 300 s of the agent's
    time left; its deadline's record says `killed`, the agent is told, and its next cell runs on the same sandbox."""
    hang = "open('/data/x').read()"
    cell_processes(world, mounted)
    cells, firing = mounted._cells, threading.Event()
    kill, handler = cells._kill_at_the_cap, world.host.exec_handler

    def the_harness_kills(*args):  # the fake docker holds its lock while a cell runs: the kill proceeds once it returns
        firing.set()
        return kill(*args)

    def runs_until_its_cap(container, argv, stdin):
        if stdin != hang:
            return handler(container, argv, stdin)
        assert firing.wait(10), "the harness's timer did not fire"
        return CmdResult(137, "", "")

    cells._kill_at_the_cap, world.host.exec_handler = the_harness_kills, runs_until_its_cap
    try:
        result = run_cells(mounted, [hang, "print(1)"], code_cell_bound_s=CELL_S)
    finally:
        cells._kill_at_the_cap, world.host.exec_handler = kill, handler
    first, second = result.tool_calls
    assert (first.status, first.error_class) == ("error", "timeout")
    assert first.text.endswith(f"the code ran past its {CELL_S:g} s limit and was killed")
    assert second.status == "ok"
    assert (result.outcome.name, result.outcome.infra_kind) == ("finished", None)
    assert [d["outcome"] for d in mounted.metadata()["cell_deadlines"]] == ["killed"]
    assert mounted.stopped is False


def test_l18_outside_the_loop_a_call_without_the_context_is_told_what_it_always_was():
    """L18: the code tool reads the limit from the context with a default: a call made with none, a probe or a test,
    and a context that names a limit, are told apart by it alone."""
    sandbox = ScriptedSandbox()
    provider = CodeExecutionProvider(sandbox)
    plain = asyncio.run(provider.execute("python", {"code": "sleep 30"}, timeout_s=2, ctx=FakeContext()))
    bounded = asyncio.run(provider.execute("python", {"code": "sleep 30"}, timeout_s=2, ctx=CellBoundContext(2)))
    assert plain.text == "partial\nthe code did not finish within the time limit and was stopped"
    assert bounded.text == "partial\nthe code ran past its 2 s limit and was killed"
    assert (plain.error_class, bounded.error_class) == ("timeout", "timeout")
