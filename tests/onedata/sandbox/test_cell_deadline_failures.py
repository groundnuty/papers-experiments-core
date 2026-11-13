"""O34: a cell's deadline whose escalation fails. The cell's own wait for the outcome is bounded, like the settle's,
so an escalation that stalls never holds the cell, its `exec` or a worker thread for ever; an outcome not decided in
that time is `survived_kill`, which is `cell_unkillable`. A listing or kill of the escalation that raises ends in the
sandbox's quiesce: every process shown gone is the clean kill it would have been, the agent's `time_limit`; anything
else is `cell_unkillable`, ends the run and orders the sandbox's stop. Nothing but a clean kill is a time limit."""

import threading
import time

import pytest
from deadline_support import (
    CELL_S,
    Clock,
    Processes,
    cell_processes,
    escalation,
    killed_pids,
    run_to_the_cap,
    steps,
    stopped_step,
)
from deadline_support import timers_by_the_clock as by_the_clock

from expcore.errors import InfraError
from expcore_onedata.sandbox import cells as cells_module
from expcore_onedata.sandbox import deadline, kinds, teardown
from expcore_onedata.sandbox.cells import SETTLE_SLACK_S, CellTimer
from expcore_onedata.sandbox.hostops import CmdResult

CODE = "open('/data/x').read()"


# ------------------------------------------------------------------------------- the escalation's own failure, alone
class KillRaises(Processes):
    """A fake tracker whose kill raises, as a `sudo kill` whose HostOps call fails does."""

    def kill(self, procs: list[dict], timeout_s: float) -> None:
        raise OSError("the host's kill could not run")


@pytest.mark.parametrize(
    ("left", "outcome", "after"),
    [
        pytest.param([], deadline.KILLED, ["ended"], id="the fallback shows every process gone"),
        pytest.param([{"pid": 12, "state": "D"}], deadline.SURVIVED, ["survived", "stop"], id="a process is left"),
        pytest.param(None, deadline.SURVIVED, ["survived", "stop"], id="the fallback could not list them"),
    ],
)
def test_o34_a_kill_that_raises_is_decided_by_what_the_fallback_leaves(left, outcome, after):
    """O34: a kill of the escalation that raises is recorded as `raised`, with the step and the error's class, and the
    caller's fallback kills what is left: every process gone is `killed`; a process left, in whatever state, or none
    that could be listed, is `survived_kill` with the stop ordered. Unknown is never gone."""
    clock = Clock()
    record = deadline.enforce(KillRaises({12: "reader"}), escalation(clock)[0], fallback=lambda: left)
    assert record["outcome"] == outcome
    assert steps(record) == ["raised", *after]
    assert record["steps"][0] == {"step": "raised", "t_s": 0.0, "during": "kill", "error": "the kill raised OSError"}


@pytest.mark.parametrize("fallback", [None, "raises"], ids=["no fallback", "the fallback raises"])
def test_o34_a_kill_that_raises_with_no_fallback_that_answers_is_survived_kill(fallback):
    """O34: with no fallback, or one that raises itself, nothing shows the cell gone: `survived_kill`, stop ordered."""

    def raising() -> list[dict]:
        raise OSError("the quiesce could not run")

    given = raising if fallback == "raises" else None
    record = deadline.enforce(KillRaises({12: "reader"}), escalation(Clock())[0], fallback=given)
    assert record["outcome"] == deadline.SURVIVED
    assert record["outcome"] in deadline.STOPS
    assert steps(record)[-1] == "stop"


# --------------------------------------------------------------------------------- the cell's own wait, bounded
def test_o34_a_cell_timers_wait_for_an_outcome_that_never_comes_is_bounded():
    """O34: the timer fired and its kill never decides (a docker daemon that answers nothing): the cell's `end` waits
    the seconds it is given and answers None, never waiting for ever."""
    release, started = threading.Event(), threading.Event()

    def stalls():
        started.set()
        release.wait(30)

    timer = CellTimer(0.0, stalls)
    timer.start()
    try:
        assert started.wait(10), "the timer did not fire"
        began = time.monotonic()
        assert timer.end(0.2) is None
        assert time.monotonic() - began < 5
    finally:
        release.set()


def hang_the_kill(sandbox, host) -> threading.Event:
    """The cell runs until its cap, and the deadline's escalation never returns until the event is set."""
    firing, release = threading.Event(), threading.Event()

    def hangs(*cell):
        firing.set()
        release.wait(60)

    def runs_until_its_cap(container, argv, stdin):
        if stdin == CODE:
            assert firing.wait(30), "the cell's deadline never fired"
            return CmdResult(137, "", "")
        return CmdResult(0)

    sandbox._cells._kill_at_the_cap, host.exec_handler = hangs, runs_until_its_cap
    return release


def test_o34_a_cell_whose_escalation_never_decides_returns_within_its_bound_as_cell_unkillable(
    world, mounted, monkeypatch
):
    """O34: the cell's escalation never decides. Its `exec` waits for the outcome the margin and `SETTLE_SLACK_S` by
    the clock (60 s with the default grace, counted by a fake clock), once, and returns: the cell is `kill_survived`,
    its attempt `cell_unkillable`, the run is ended and the next cell refused. Waited for without a bound, `exec` and
    the worker thread that runs it never returned, and `asyncio.run` could not shut down."""
    clock = Clock()
    decided = by_the_clock(monkeypatch, clock)
    release = hang_the_kill(mounted, world.host)
    try:
        result = mounted.exec(CODE, CELL_S)
    finally:
        release.set()
    bound = deadline.margin_s(world.s.kill_grace_s) + SETTLE_SLACK_S
    assert decided[-1].bounds == [bound] == [60.0]  # one wait, bounded, though `end` is reached twice
    assert clock.now == 100.0 + bound
    assert (result.kill["reason"], result.kill["category"]) == ("kill_survived", "infrastructure")
    assert result.timed_out is True
    assert kinds.deadline_error(result.kill).kind == kinds.CELL_UNKILLABLE
    record = result.kill["deadline"]
    assert (record["outcome"], record["why"]) == (deadline.SURVIVED, "not decided within its margin")
    assert mounted._cells.ended["outcome"] == deadline.SURVIVED
    assert mounted.metadata()["cell_deadlines"][-1]["cell"] == 1
    with pytest.raises(InfraError) as refused:
        mounted.exec("print(1)", 30)
    assert refused.value.kind == kinds.CELL_UNKILLABLE


# ------------------------------------------------------------------ an escalation that raises, through the sandbox
def raising_kill(*args, **kwargs):
    raise OSError("the host's kill could not run")


def quick_quiesce(monkeypatch) -> None:
    """The quiesce's wait for a killed process to go (10 s), shortened: a wedged one never goes."""
    wait_gone = teardown._wait_gone
    monkeypatch.setattr(teardown, "_wait_gone", lambda host, procs, seconds, poll: wait_gone(host, procs, 0.3, 0.05))


def host_kill_fails_twice(world) -> None:
    """`HostOps.run` raises for the next two SIGKILLs, the deadline's and the quiesce's, then kills as before."""
    raises = iter([True, True])

    def kill(argv):
        if "KILL" in argv and next(raises, False):
            raise OSError("the host's kill could not run")
        return world.host._run_default(argv)

    world.host.run_handlers["kill"] = kill


@pytest.mark.parametrize(
    "how",
    ["the host's kill raises for the quiesce too", "the process stays wedged", "the quiesce cannot list them"],
)
def test_o34_a_kill_that_raises_and_a_quiesce_that_fails_end_the_run_as_cell_unkillable(
    world, mounted, monkeypatch, how
):
    """O34: the deadline's kill raises (its `HostOps.run` fails) and the quiesce it falls back on cannot show the cell
    gone: it cannot kill, a process is left in D, or it cannot list them. The cell is `kill_survived`, its attempt
    `cell_unkillable` with the step and the error recorded, the run is ended, a later cell refused and the sandbox's
    stop ordered. Before, the handler ignored the quiesce, published nothing and the cell was the agent's
    `time_limit`, with later cells still allowed."""
    cell_processes(world, mounted, wedged=how == "the process stays wedged")
    quick_quiesce(monkeypatch)
    if how == "the host's kill raises for the quiesce too":
        host_kill_fails_twice(world)
    else:
        monkeypatch.setattr(cells_module._CellProcesses, "kill", raising_kill)
    if how == "the quiesce cannot list them":
        listing = cells_module.CellRunner.agent_processes
        monkeypatch.setattr(  # the deadline's own listing, which is given a time, still answers
            cells_module.CellRunner, "agent_processes", lambda self, t=None: None if t is None else listing(self, t)
        )
    result, _ = run_to_the_cap(mounted, world, CODE)
    assert (result.kill["reason"], result.kill["category"]) == ("kill_survived", "infrastructure")
    assert kinds.deadline_error(result.kill).kind == kinds.CELL_UNKILLABLE
    record = result.kill["deadline"]
    assert record["outcome"] == deadline.SURVIVED
    raised = record["steps"][0]
    assert (raised["step"], raised["during"], raised["error"]) == ("raised", "kill", "the kill raised OSError")
    assert steps(record)[1:3] == ["survived", "stop"]
    assert stopped_step(record)["ok"] is True  # the stop the record ordered ran, once the outcome was recorded
    assert mounted._cells.ended is record
    assert mounted.stopped is True
    with pytest.raises(InfraError) as refused:
        mounted.exec("print(1)", 30)
    assert refused.value.kind == kinds.CELL_UNKILLABLE


def test_o34_a_kill_that_raises_and_a_quiesce_that_kills_every_process_is_the_agents_time_limit(
    world, mounted, monkeypatch
):
    """O34: the deadline's kill raises, and the quiesce it falls back on kills every process of the cell, all shown
    gone: the cell ended at its cap as a clean kill would have ended it, so it is the agent's own `time_limit`, with
    the error in its deadline's record; the run is not ended and goes on. The harness's failed command freed the cell
    all the same: nothing outlived its kill, and an untreatable kind would misreport the agent's own timeout."""
    pids = cell_processes(world, mounted)
    monkeypatch.setattr(cells_module._CellProcesses, "kill", raising_kill)
    result, _ = run_to_the_cap(mounted, world, CODE)
    assert (result.kill["reason"], result.kill["category"]) == ("time_limit", "agent")
    assert kinds.kill_error(result.kill) is None
    record = mounted.metadata()["cell_deadlines"][-1]
    assert (record["cell"], record["outcome"]) == (1, deadline.KILLED)
    assert steps(record) == ["raised", "ended"]
    assert record["steps"][0]["error"] == "the kill raised OSError"
    assert {pids["python"], pids["child"], pids["grandchild"]} <= killed_pids(world)  # by the quiesce
    assert mounted._cells.ended is None
    assert mounted.stopped is False
    assert mounted.exec("print(1)", 30).exit_code == 0
