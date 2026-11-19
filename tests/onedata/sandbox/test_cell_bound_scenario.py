"""L18: the scenario of the live proof of the per-cell kill, checked offline so a live run can only differ in the
sandbox (tests/support/cell_bound_live.py). It runs over a simulated sandbox, and over the Docker sandbox on its fake
host (the Apptainer one on its fake node is in apptainer/test_cells.py). The independent look at the host's processes is
scripted here: the live runs read it from `docker top` and `ps`."""

import threading

import pytest
from cell_bound_live import NEXT_LINE, SLEEP_S, limit_text, problems, run_scenario
from deadline_support import CELL_S, cell_processes
from loop_support import FakeClock

from expcore_onedata.sandbox.api import ExecResult
from expcore_onedata.sandbox.hostops import CmdResult

TIME_LIMIT = {"reason": "time_limit", "category": "agent", "basis": "the cell ran to its limit"}
KILLED = {"cell": 1, "outcome": "killed", "steps": [{"step": "kill"}, {"step": "ended"}]}


class Look:
    """The host's process list as the scenario reads it: the cell's two marked processes first, none after."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> list[str]:
        self.calls += 1
        return ["101 python -c sleeper marker", "102 python -c sleeper marker"] if self.calls == 1 else []


class SimulatedSandbox:
    """A sandbox that kills a cell at its limit, as the real ones do, on a fake clock the kill moves."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock, self.limits = clock, []

    def exec(self, code: str, timeout_s: float) -> ExecResult:
        self.limits.append(timeout_s)
        if "cell started" in code and timeout_s < SLEEP_S:
            self.clock.advance(timeout_s)
            return ExecResult("cell started\n", "", 137, timeout_s, timed_out=True, kill=TIME_LIMIT)
        return ExecResult(f"{NEXT_LINE}\n", "", 0, 0.1)

    def metadata(self) -> dict:
        return {"cell_deadlines": [KILLED]}


def test_l18_the_scenario_shows_the_kill_over_a_simulated_sandbox():
    """L18: with a 60 s bound under a 600 s cap, the first cell is handed 60 s and killed there, the agent is told the
    limit, the next cell runs and the run finishes; the facts have no problem, and the record replays."""
    clock = FakeClock()
    sandbox = SimulatedSandbox(clock)
    facts = run_scenario(sandbox, Look(), clock=clock, look_for_s=5)
    assert sandbox.limits == [60.0, 60.0]
    assert facts["call_1"]["elapsed_s"] == 60.0
    assert facts["call_1"]["text"].endswith(limit_text(60.0))
    assert problems(facts) == []


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (lambda f: f["before"].update(marked=1), "marked process"),
        (lambda f: f["after"].update(marked=1), "still there"),
        (lambda f: f["call_1"].update(elapsed_s=179.0), "the call took"),
        (lambda f: f["deadline"].update(outcome="survived_kill"), "deadline record"),
        (lambda f: f["call_1"].update(text="the code did not finish within the time limit and was stopped"), "told"),
        (lambda f: f["call_2"].update(status="error"), "next cell"),
        (lambda f: f["outcome"].update(name="infrastructure"), "did not finish"),
        (lambda f: f["record"].update(replay_identical=False), "record is not"),
    ],
    ids=["no-process", "survivor", "late", "no-kill", "no-limit-text", "no-next-cell", "no-finish", "bad-record"],
)
def test_l18_each_point_of_the_proof_is_checked(change, fragment):
    """L18: a run that shows the kill has no problem; each point is the one that fails when it is not shown."""
    clock = FakeClock()
    facts = run_scenario(SimulatedSandbox(clock), Look(), clock=clock, look_for_s=5)
    assert problems(facts) == []
    change(facts)
    assert any(fragment in found for found in problems(facts))


def test_l18_the_scenario_runs_over_the_docker_sandbox_on_its_fake_host(world, mounted):
    """L18, O34: the scenario's first cell, its child and its detached grandchild run to the cap on the fake host; the
    harness's timer kills them at the 1.5 s bound, and the facts of the real Docker sandbox have no problem."""
    marker = "cellbound-offline"
    cell_processes(world, mounted)
    cells, firing = mounted._cells, threading.Event()
    kill, handler = cells._kill_at_the_cap, world.host.exec_handler

    def the_harness_kills(*args):  # the fake docker holds its lock while a cell runs: the kill proceeds once it returns
        firing.set()
        return kill(*args)

    def runs_until_its_cap(container, argv, stdin):
        if stdin is not None and NEXT_LINE in stdin and marker not in stdin:  # the second cell prints its line
            return CmdResult(0, f"{NEXT_LINE}\n", "")
        if stdin is None or marker not in stdin:
            return handler(container, argv, stdin)
        assert firing.wait(10), "the harness's timer did not fire"
        return CmdResult(137, "cell started\n", "")

    cells._kill_at_the_cap, world.host.exec_handler = the_harness_kills, runs_until_its_cap
    try:
        facts = run_scenario(mounted, Look(), marker, bound_s=CELL_S, look_for_s=5)
    finally:
        cells._kill_at_the_cap, world.host.exec_handler = kill, handler
    assert problems(facts) == []
    assert facts["deadline"]["outcome"] == "killed"
