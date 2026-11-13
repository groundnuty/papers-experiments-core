"""Helpers for the tests of a cell's hard deadline (O30): a fake clock and process tracker for the escalation alone, and
the cell processes, the run to the cap and the kills sent, for the deadline through a sandbox on the fake host.

A real process in uninterruptible sleep cannot be made in a test: the escalation is driven by a fake process tracker,
and through the sandbox by the fake host's processes that ignore every signal (`FakeHost.unkillable`)."""

import threading
import time

from expcore_onedata.sandbox import deadline
from expcore_onedata.sandbox.hostops import CmdResult

CELL_S = 1.5  # a cell's limit: more than the second a cell needs once prepared, and short for a test


class Clock:
    """A clock that moves only when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 100.0
        self.slept = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


class Processes:
    """A fake process tracker: the cell's processes and the kills sent to them. A kill ends a process unless it is
    wedged (in D state, a read waiting in the kernel), which ignores SIGTERM and SIGKILL alike until released. With a
    `clock` and `slow_s`, each listing takes `slow_s` of it, as a slow docker daemon's inspect does, and one given less
    time than that times out unknown; the time each listing and kill was given is kept."""

    def __init__(
        self, pids: dict[int, str], *, wedged: tuple[int, ...] = (), clock: Clock | None = None, slow_s: float = 0.0
    ) -> None:
        self.procs = {pid: {"pid": pid, "comm": comm, "state": "S", "start": str(pid)} for pid, comm in pids.items()}
        self.wedged = set(wedged)
        for pid in self.wedged:
            self.procs[pid]["state"] = "D"
        self.kills: list[list[int]] = []
        self.unknown = False
        self.clock, self.slow_s = clock, slow_s
        self.given: list[tuple[str, float]] = []

    def processes(self, timeout_s: float) -> list[dict] | None:
        self.given.append(("processes", timeout_s))
        if self.clock is not None and self.slow_s:
            self.clock.now += min(self.slow_s, timeout_s)
            if self.slow_s > timeout_s:
                return None
        return None if self.unknown else [dict(p) for p in self.procs.values()]

    def kill(self, procs: list[dict], timeout_s: float) -> None:
        self.given.append(("kill", timeout_s))
        self.kills.append(sorted(p["pid"] for p in procs))
        for p in procs:
            if p["pid"] not in self.wedged:
                self.procs.pop(p["pid"], None)

    def release(self) -> None:
        """The wedged read returns (an aborted connection ends it with an errno): the pending SIGKILL takes."""
        for pid in self.wedged:
            self.procs.pop(pid, None)
        self.wedged.clear()


def escalation(clock: Clock, *, abort=None, grace_s: float = 3.0) -> tuple[deadline.Escalation, list]:
    """The escalation under test, and the list of what it was asked to do, in order, with the time each was given."""
    calls: list[tuple[str, float]] = []

    def aborting(timeout_s: float) -> dict:
        calls.append(("abort", timeout_s))
        return abort(timeout_s) if abort else {"ok": None, "why": "no FUSE mount"}

    return deadline.Escalation(grace_s=grace_s, abort=aborting, sleep=clock.sleep, clock=clock), calls


def steps(record: dict) -> list[str]:
    return [s["step"] for s in record["steps"]]


def cell_processes(world, sandbox, *, wedged: bool = False) -> dict[str, int]:
    """The cell's processes in the agent's cgroup beside its init and the init's `sleep` (the one docker-init started
    with the container): the cell's python, its child, and a grandchild in a session of its own (a detached worker);
    the python wedged in D when asked."""
    agent = world.host.containers[sandbox.names.agent]
    pids = {"sleep": agent.init_child, "python": 7002, "child": 7003, "grandchild": 7004}
    world.host.add_stat(pids["python"], "python", 6999, state="D" if wedged else "S")  # the exec's, not the init's
    world.host.add_stat(pids["child"], "python", pids["python"])
    world.host.add_stat(pids["grandchild"], "worker", pids["child"])
    world.host.add_cgroup(agent, [agent.pid, *pids.values()])
    if wedged:
        world.host.unkillable.add(pids["python"])
    return pids


def run_to_the_cap(sandbox, world, code: str = "open('/data/x').read()"):
    """Run a cell that is still running at its cap: the fake docker returns once the harness's timer has fired (the
    real one returns when the cell's processes end or its container goes), and the deadline then runs to its end. Any
    other exec meanwhile goes to the handler the test set; the cell's result, and every exec other than the cell, by
    the container it ran in and its command."""
    cells, firing, others = sandbox._cells, threading.Event(), []
    kill, handler = cells._kill_at_the_cap, world.host.exec_handler

    def the_harness_kills(*args):  # the fake docker holds its lock while a cell runs: the kill proceeds once it returns
        firing.set()
        return kill(*args)

    def runs_until_its_cap(container, argv, stdin):
        if stdin != code:
            others.append((container.name, argv))
            return handler(container, argv, stdin)
        assert firing.wait(10), "the harness's timer did not fire"
        return CmdResult(137, "", "")

    cells._kill_at_the_cap, world.host.exec_handler = the_harness_kills, runs_until_its_cap
    try:
        return sandbox.exec(code, CELL_S), others
    finally:
        cells._kill_at_the_cap, world.host.exec_handler = kill, handler


def killed_pids(world) -> set[int]:
    return {int(p) for argv in world.host.sudo_commands if argv[:3] == ["kill", "-s", "KILL"] for p in argv[3:]}


def stopped_step(record: dict, within_s: float = 10.0) -> dict:
    """The `stopped` step of a deadline that ordered the sandbox's stop: the stop runs in the timer's thread once the
    outcome is recorded, so it may still be under way when the cell returns."""
    deadline_at = time.monotonic() + within_s
    while time.monotonic() < deadline_at:
        found = [s for s in record["steps"] if s["step"] == "stopped"]
        if found:
            return found[-1]
        time.sleep(0.01)
    raise AssertionError("the sandbox's stop did not finish")
