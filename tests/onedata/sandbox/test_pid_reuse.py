"""O72: a pid that ends and is given to another process between the listing of the agent's processes and the signal sent
to them is never signalled, on all of the Docker sandbox's kills of the agent's processes: the sweep after a cell, the
quiesce before the collection, the kill at the cell's cap and the teardown's readers.

The harness lists a container's processes from its cgroup and signals them by pid through the host's privilege, so on
the agent's node a pid reused in that window could be the gate or the dispatcher. The list carries each process's start
tick and the digest of its command line, and the privileged half (`killhelper.py`) signals a pid only while both still
match (a pidfd where the kernel has one). A process it did not signal is never read as gone or as quiet.

The fake host plays that helper against its fake /proc (`FakeHost._signal_helper`), and a test changes what a pid is
just before the first signal (`before_first_signal`), which is where the race is."""

import pytest
from deadline_support import (
    Clock,
    Processes,
    cell_processes,
    escalation,
    killed_pids,
    run_to_the_cap,
    steps,
)
from sandbox_support import before_first_signal, make_spec, start_options

from expcore_onedata.sandbox import deadline
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox


@pytest.fixture
def sbx(world):
    sandbox = DockerSandbox.start(world.host, world.s, make_spec(world, "isolated"), **start_options(world))
    yield sandbox
    world.host.unkillable.clear()
    sandbox.stop(force=True)


def give_the_pid_away(world, sandbox, pid: int, keep: dict[str, int]) -> None:
    """The listed process ended and another process, outside the container, was given its pid: the pid is no longer in
    the container's cgroup, and /proc shows another process with a start tick of its own."""
    agent = world.host.containers[sandbox.names.agent]
    world.host.add_stat(pid, "gate", 1, start=999_999)
    world.host.add_cgroup(agent, [agent.pid, *(p for p in keep.values() if p != pid)])


def changes_its_command_line(world, pid: int) -> None:
    """The listed process execs: the same process, the same start tick, another command line."""
    world.host.files[f"/proc/{pid}/cmdline"] = "an-exec-ed-program\0--arg\0"


def plain_kills(world) -> list[list[int]]:
    """The pids of each `kill` the privileged half ended up running, in order."""
    return [[int(p) for p in argv[3:]] for argv in world.host.sudo_commands if argv[:1] == ["kill"]]


# --------------------------------------------------------------------------------------- the sweep after every cell
def test_o72_the_sweep_never_signals_a_pid_another_process_took_since_the_listing(world, sbx):
    """O72: the sweep listed the cell's worker, which ended before the kill and whose pid another process took. That
    pid is never signalled; what the cell really left is killed, and the run is quiet."""
    pids = cell_processes(world, sbx)
    before_first_signal(world.host, lambda: give_the_pid_away(world, sbx, pids["grandchild"], pids))
    result = sbx.exec("start_a_worker()", 30)
    assert result.kill is None
    assert pids["grandchild"] not in killed_pids(world)
    assert {pids["python"], pids["child"]} <= killed_pids(world)
    assert sbx._cells.ended is None


def test_o72_a_leftover_that_changed_its_command_line_since_the_listing_is_listed_again_and_killed(world, sbx):
    """O72: a process that execs between the listing and the kill is still the cell's leftover. The stale identity is
    never signalled; the next listing carries the new one, and the sweep kills it then, so it neither survives nor
    ends the run."""
    pids = cell_processes(world, sbx)
    before_first_signal(world.host, lambda: changes_its_command_line(world, pids["child"]))
    result = sbx.exec("start_a_worker()", 30)
    assert result.kill is None
    first, *later = plain_kills(world)
    assert pids["child"] not in first
    assert pids["child"] in [pid for kill in later for pid in kill]
    assert sbx._cells.ended is None
    assert sbx._cells.cells[-1]["left_after_exec"] == 3  # the three processes the cell left, each counted once


def test_o72_a_leftover_whose_identity_cannot_be_read_is_never_signalled_and_never_read_as_quiet(world, sbx):
    """O72: a process whose command line the listing could not read has no identity to check, so nothing signals it,
    and it is alive: the run ends as `survived_kill`, never as a cell that left nothing."""
    pids = cell_processes(world, sbx)
    world.host.denied.add(f"/proc/{pids['grandchild']}/cmdline")
    sbx.exec("start_a_worker()", 30)
    assert pids["grandchild"] not in killed_pids(world)
    assert sbx._cells.ended["outcome"] == deadline.SURVIVED


def test_o72_a_signal_the_helper_could_not_give_is_never_read_as_quiet(world, sbx):
    """O72: the privileged command failing as a whole (sudo refusing it) signals nothing: a leftover is alive and the
    run ends, as the same failure of the plain `kill` did before."""
    cell_processes(world, sbx)
    world.host.run_handlers["kill"] = lambda argv: CmdResult(1, "", "sudo: a password is required")
    sbx.exec("start_a_worker()", 30)
    assert sbx._cells.ended["outcome"] == deadline.SURVIVED


# ---------------------------------------------------------------------------------- the quiesce before the collection
def test_o72_the_quiesce_before_the_collection_never_signals_a_pid_another_process_took(world, sbx):
    """O72: the collection's quiesce lists the agent's processes and kills them: a pid given away in between is spared,
    and the state counts the processes the listing found."""
    pids = cell_processes(world, sbx)
    before_first_signal(world.host, lambda: give_the_pid_away(world, sbx, pids["child"], pids))
    state = sbx._cells.quiesce("end of run")
    assert pids["child"] not in killed_pids(world)
    assert {pids["python"], pids["grandchild"]} <= killed_pids(world)
    assert state["killed"] == 3
    assert state["left_alive"] == 0


# ------------------------------------------------------------------------------------------ the kill at the cell's cap
def test_o72_the_kill_at_the_cap_never_signals_a_pid_another_process_took_since_the_listing(world, mounted):
    """O72: the cap's kill is decided from a listing made a moment before. A pid given to another process in between is
    spared, and the cell's own processes are killed within the deadline."""
    pids = cell_processes(world, mounted)
    before_first_signal(world.host, lambda: give_the_pid_away(world, mounted, pids["grandchild"], pids))
    result, _ = run_to_the_cap(mounted, world)
    assert pids["grandchild"] not in killed_pids(world)
    assert {pids["python"], pids["child"]} <= killed_pids(world)
    assert result.kill["reason"] == "time_limit"
    assert mounted.metadata()["cell_deadlines"][-1]["outcome"] == deadline.KILLED


def test_o72_the_kill_at_the_cap_lists_a_process_that_changed_its_command_line_again_and_kills_it(world, mounted):
    """O72: declined once because it execed, a cell process is signalled within the grace period by its new identity,
    and the deadline's record counts only what a signal reached."""
    pids = cell_processes(world, mounted)
    before_first_signal(world.host, lambda: changes_its_command_line(world, pids["child"]))
    result, _ = run_to_the_cap(mounted, world)
    first, *later = plain_kills(world)
    assert pids["child"] not in first
    assert pids["child"] in [pid for kill in later for pid in kill]
    assert result.kill["reason"] == "time_limit"
    assert mounted.metadata()["cell_deadlines"][-1]["outcome"] == deadline.KILLED


def test_o72_a_cell_process_the_cap_cannot_identify_survives_its_kill_and_the_deadline_escalates(world, mounted):
    """O72: unknown is never gone at the cap either: a process whose identity cannot be read is not signalled, is alive
    when the grace period ends, and the outcome is an escalated one, never `killed`."""
    pids = cell_processes(world, mounted)
    world.host.denied.add(f"/proc/{pids['grandchild']}/cmdline")
    result, _ = run_to_the_cap(mounted, world)
    assert pids["grandchild"] not in killed_pids(world)
    assert result.kill["deadline"]["outcome"] in deadline.ESCALATED
    assert result.kill["reason"] == "kill_survived"


# -------------------------------------------------------------------------------- the deadline's own use of the answer
class Declining(Processes):
    """A tracker whose kill reaches only some processes, as the privileged half declines one whose identity changed:
    `decline` is how many times each pid in it is declined before it is signalled, and a pid in `always` never is."""

    def __init__(self, *args, decline: dict[int, int] | None = None, always: tuple[int, ...] = (), **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.decline, self.always = dict(decline or {}), set(always)

    def kill(self, procs: list[dict], timeout_s: float):
        self.given.append(("kill", timeout_s))
        self.kills.append(sorted(p["pid"] for p in procs))
        reached = []
        for p in procs:
            if p["pid"] in self.always or self.decline.get(p["pid"], 0) > 0:
                self.decline[p["pid"]] = self.decline.get(p["pid"], 0) - 1
                continue
            reached.append(deadline.key_of(p))
            if p["pid"] not in self.wedged:
                self.procs.pop(p["pid"], None)
        return reached


def test_o72_a_process_the_kill_did_not_reach_is_not_recorded_as_signalled_and_is_tried_again():
    """O72: the tracker answers with the processes its signal reached; one it declined is neither counted nor marked
    done, so the next listing sends it again."""
    clock, procs = Clock(), Declining({11: "python", 12: "worker"}, decline={11: 1, 12: 1})
    esc, _ = escalation(clock)
    record = deadline.enforce(procs, esc)
    assert record["outcome"] == deadline.KILLED
    assert procs.kills[0] == [11, 12]
    assert procs.kills[1:]
    assert {11, 12} <= {pid for kill in procs.kills[1:] for pid in kill}
    assert record["steps"][0]["step"] == "kill"
    assert record["steps"][0]["signalled"] == 0
    assert record["steps"][0]["unreached"] == 2


def test_o72_a_process_no_signal_ever_reaches_is_a_survivor_and_never_a_clean_kill():
    """O72: left alive when the grace ends, so the deadline escalates as it does for a process in D, whatever the
    reason the signal did not reach it."""
    clock, procs = Clock(), Declining({11: "python", 12: "worker"}, always=(12,))
    esc, calls = escalation(clock)
    record = deadline.enforce(procs, esc)
    assert record["outcome"] != deadline.KILLED
    assert "survived" in steps(record)
    assert [name for name, _ in calls] == ["abort"]
    assert 11 not in procs.procs
    assert 12 in procs.procs


def test_o72_a_tracker_that_answers_nothing_has_reached_every_process_it_was_given():
    """O72: the Apptainer backend's tracker signals through pidfds of its own and answers None, which is all of them."""
    clock, procs = Clock(), Processes({11: "python", 12: "worker"})
    esc, _ = escalation(clock)
    record = deadline.enforce(procs, esc)
    assert record["steps"][0] == {"step": "kill", "t_s": 0.0, "signalled": 2}
