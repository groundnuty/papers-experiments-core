"""O70: the pure parts of the Apptainer backend's cpus, pids and nofile limits: what `/proc` says of a process's tasks
and cores, the cell's command prefix, the canary's answer, and the new kill cause."""

import pytest

from expcore_onedata.sandbox import killcause as KC
from expcore_onedata.sandbox.apptainer import limits as L
from expcore_onedata.sandbox.apptainer import procs as P


def test_o70_threads_are_the_status_line_alone_and_a_missing_or_foreign_line_is_unknown():
    """O70: Docker's pids.max counts tasks, so the watch counts `Threads:` of the status file; a line that is missing
    or not a whole number is unknown, never 0."""
    assert P.parse_threads("Name:\tx\nThreads:\t17\nSigQ:\t0/1\n") == 17
    assert P.parse_threads("Name:\tx\n") is None
    assert P.parse_threads("Threads:\tmany\n") is None
    assert P.parse_threads("Threads:\t\n") is None
    assert P.parse_threads("Threads:\t-3\n") is None


@pytest.mark.parametrize(
    ("text", "cores"),
    [("0", {0}), ("0-3", {0, 1, 2, 3}), ("0-1,4,6-7", {0, 1, 4, 6, 7}), ("3,1", {1, 3}), ("2-2", {2})],
)
def test_o70_a_cpu_list_is_read_as_a_set_of_cores(text, cores):
    """O70: `Cpus_allowed_list` and the canary's own answer are lists of cores and ranges."""
    assert P.parse_cpu_list(text) == frozenset(cores)


@pytest.mark.parametrize("text", ["", " ", "a", "1-", "-1", "3-1", "1,,2", "1 2", "0-99999999", "1-2-3"])
def test_o70_a_cpu_list_that_is_not_one_is_unknown_never_the_empty_set(text):
    """O70: an affinity that cannot be read is unknown; the empty set would pass for a subset of any pin."""
    assert P.parse_cpu_list(text) is None


def test_o70_the_allowed_cores_are_the_status_line_alone():
    """O70: `Cpus_allowed_list` of a task's status file; the mask line beside it is not read."""
    text = "Name:\tx\nCpus_allowed:\tff\nCpus_allowed_list:\t0-3\nMems_allowed_list:\t0\n"
    assert P.parse_cpus_allowed(text) == frozenset({0, 1, 2, 3})
    assert P.parse_cpus_allowed("Cpus_allowed:\tff\n") is None
    assert P.parse_cpus_allowed("Cpus_allowed_list:\tx\n") is None


def test_o70_a_cell_is_pinned_to_the_first_ceil_of_cpus_cores_of_the_step_s_set():
    """O70: a fractional cpus is a whole core here; the set is the first k of the step's sorted cores."""
    assert L.pin_for([4, 2, 3, 9], 2.0) == [2, 3]
    assert L.pin_for([4, 2, 3, 9], 1.5) == [2, 3]
    assert L.pin_for([4, 2, 3, 9], 0.1) == [2]
    assert L.pin_for([4, 2, 3, 9], 2.0000001) == [2, 3, 4]


def test_o70_a_pin_wider_than_the_step_s_set_is_the_whole_set():
    """O70: cpus above the cores the step holds pins to all of them (Docker's --cpus past the host's cores is the
    host's cores); the record's effective count then says how many."""
    assert L.pin_for([0, 1], 8.0) == [0, 1]


def test_o70_a_step_set_that_cannot_be_read_gives_no_pin():
    """O70: no step set, no pin: the cell is not given a guessed one."""
    assert L.pin_for(None, 2.0) is None
    assert L.pin_for([], 2.0) is None


def test_o70_the_prefix_sets_the_file_limit_and_the_pin_before_apptainer_starts():
    """O70: `prlimit` sets a hard and soft RLIMIT_NOFILE before the exec (the cell, with no CAP_SYS_RESOURCE, cannot
    raise it) and `taskset` the affinity; with no pin there is no `taskset`."""
    assert L.prefix(4096, [2, 3]) == ["prlimit", "--nofile=4096:4096", "--", "taskset", "-c", "2,3"]
    assert L.prefix(64, None) == ["prlimit", "--nofile=64:64", "--"]


def test_o70_the_canary_s_answer_is_read_back_whole_or_not_at_all():
    """O70: the canary prints its own limit and affinity on a line after the connect's; a line that is not whole
    leaves the limit unknown, and one whose cores are not a list leaves only them so."""
    probe = L.parse_probe("canary open\nlimits 64 64 2,3\n")
    assert (probe.nofile_soft, probe.nofile_hard, probe.cpus) == (64, 64, frozenset({2, 3}))
    assert L.parse_probe("canary open\n") is None
    assert L.parse_probe("canary open\nlimits 64\n") is None
    assert L.parse_probe("limits a b 2\n") is None
    cut = L.parse_probe("limits 64 64 x\n")
    assert (cut.nofile_hard, cut.cpus) == (64, None)


def test_o70_the_canary_program_prints_the_limits_a_cell_has_whatever_the_connect_did():
    """O70: the program the cell runs reads `RLIMIT_NOFILE` and the affinity inside the cell (it cannot be told from
    outside what the starter did to them)."""
    source = L.CANARY.format(port=1234, hold=0.0)
    assert "getrlimit(resource.RLIMIT_NOFILE)" in source
    assert "sched_getaffinity(0)" in source
    compile(source, "<canary>", "exec")


def test_o70_a_pids_limit_is_the_agent_s_own_outcome_ordered_beside_the_disk_limit():
    """O70: a cell that forks past `pids` is killed by the watch: the agent's own, after the memory and disk limits
    and before the harness's timer."""
    assert KC.PIDS_LIMIT == "pids_limit"
    assert KC.CATEGORY["pids_limit"] == "agent"
    assert "pids_limit" in KC.REASONS
    order = [r for r in KC.PRECEDENCE if r in ("memory_limit", "disk_limit", "pids_limit", "time_limit")]
    assert order == ["memory_limit", "disk_limit", "pids_limit", "time_limit"]
    assert KC.run_cause(["time_limit", "pids_limit"]) == "pids_limit"
    assert KC.run_cause(["pids_limit", "disk_limit"]) == "disk_limit"
