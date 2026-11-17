"""O70: the Apptainer sandbox keeps `cpus`, `pids`, `nofile` and the gate's memory limit on a fake node, proves what it
can at the start, and records each reading in the run's `infrastructure_facts.limits` (and its metadata.json)."""

import json
import signal
import time
from dataclasses import replace

import pytest
from apptainer_support import LIMITS, make_settings, make_spec, options, start
from fake_host import bootstrap_of
from fake_node import FakeNode, Unrecorded

from expcore_onedata.sandbox import infradeath as ID
from expcore_onedata.sandbox import kinds
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.apptainer import facts
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox
from expcore_onedata.sandbox.hostops import CmdResult


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


def _box(node, tmp_path, level="via-gate", **limits):
    return start(node, make_settings(tmp_path, limits=replace(LIMITS, **limits)), make_spec(level))


def _until(check, seconds=10.0) -> bool:
    until = time.monotonic() + seconds
    while not check() and time.monotonic() < until:
        time.sleep(0.01)
    return check()


def _limits(box):
    return box.metadata()["infrastructure_facts"]["limits"]


def test_o70_every_cell_command_starts_under_prlimit_and_taskset(node, tmp_path):
    """O70: the file limit and the pin are set on the `apptainer` command before the cell starts; the harness's own
    one-shot reads (the image's certifi bundle, a trust file) are no cell and carry neither."""
    box = _box(node, tmp_path)
    try:
        box.exec("print(1)", 30)
        cells = [e for e in node.execs if bootstrap_of(e.command) is not None]
        reads = [e for e in node.execs if bootstrap_of(e.command) is None]
        assert len(cells) >= 3
        assert reads
        assert all((e.nofile, e.cpus) == (LIMITS.nofile, [0]) for e in cells)
        assert all((e.nofile, e.cpus) == (None, None) for e in reads)
    finally:
        box.stop()


def test_o70_the_pin_is_the_first_ceil_of_cpus_cores_of_the_driver_s_own_affinity(node, tmp_path):
    """O70: 2.5 cpus on a step holding cores 6, 4, 5 and 7 pins to the first three of them, sorted."""
    node.cores = [6, 4, 5, 7]
    box = _box(node, tmp_path, cpus=2.5)
    try:
        assert node.execs[-1].cpus == [4, 5, 6]
        cpus = _limits(box)["cpus"]
        assert (cpus["declared"], cpus["step_set"], cpus["effective_set"], cpus["effective_count"]) == (
            2.5, [4, 5, 6, 7], [4, 5, 6], 3
        )  # fmt: skip
    finally:
        box.stop()


def test_o70_cpus_above_the_cores_the_step_holds_pin_to_all_of_them(node, tmp_path):
    """O70: Docker's --cpus past the host's cores is the host's cores; the record's effective count says how many."""
    node.cores = [0, 1]
    box = _box(node, tmp_path, cpus=8.0)
    try:
        cpus = _limits(box)["cpus"]
        assert (cpus["reading"], cpus["effective_set"], cpus["effective_count"]) == ("pinned", [0, 1], 2)
    finally:
        box.stop()


def test_o70_a_normal_run_s_record_shows_every_limit_kept(node, tmp_path):
    """O70: the whole record of a run that kept its limits: pinned to one core, within 64 tasks, 64 files as the cell
    itself read, and a gate within its memory; also in the collected metadata.json."""
    box = _box(node, tmp_path)
    try:
        box.exec("print('hello')", 30)
        found = _limits(box)
        assert found["version"] == 1
        assert found["cpus"] == {"declared": 1.0, "reading": "pinned", "unknown": [], "effective_set": [0],
                                 "effective_count": 1, "step_set": [0, 1, 2, 3], "widened": False,
                                 "widened_processes": 0}  # fmt: skip
        assert (found["pids"]["reading"], found["pids"]["unknown"], found["pids"]["kills"]) == ("within", [], 0)
        assert found["pids"]["declared"] == 64
        assert 2 <= found["pids"]["max_seen"] <= 64
        assert found["nofile"] == {"declared": 64, "reading": "set", "unknown": [], "hard_limit": 64}
        assert found["gate_memory"] == {"declared": 1 << 30, "reading": "within", "unknown": [],
                                        "peak_bytes": 1 << 20}  # fmt: skip
        box.collect(tmp_path / "out", [])
        saved = json.loads((tmp_path / "out" / "metadata.json").read_text(encoding="utf-8"))
        assert saved["infrastructure_facts"]["limits"]["gate_memory"]["reading"] == "within"
        assert saved["infrastructure_facts"]["limits"]["nofile"]["reading"] == "set"
    finally:
        box.stop()


def test_o70_a_start_whose_cell_has_another_hard_limit_than_declared_fails(node, tmp_path):
    """O70: the canary reads RLIMIT_NOFILE inside a cell; a starter that set another hard limit fails the start, and
    nothing is left running."""
    node.starter_nofile = 1024
    with pytest.raises(SandboxError, match="open-file limit"):
        _box(node, tmp_path)
    assert not [p for p in node.procs.values() if p.alive and p.role != "driver"]


def test_o70_a_prefix_tool_that_fails_is_named_at_the_start_not_the_connect(node, tmp_path):
    """O70: `prlimit` that may not raise a hard limit (or a node without it) fails the start naming the tool, not as an
    egress canary that could not connect."""
    node.cell_handler = lambda cell: Unrecorded(1, "", "prlimit: failed to set the NOFILE resource limit\n")
    with pytest.raises(SandboxError, match="prlimit"):
        _box(node, tmp_path)


def test_o70_a_canary_whose_limits_line_never_came_leaves_nofile_and_cpus_unknown(node, tmp_path):
    """O70: no reading from inside the cell, no `set` and no `pinned`: unknown with `probe_unreadable`, values null."""
    node.canary_limits_missing = True
    box = _box(node, tmp_path)
    try:
        found = _limits(box)
        assert found["nofile"] == {"declared": 64, "reading": "unknown", "unknown": ["probe_unreadable"],
                                   "hard_limit": None}  # fmt: skip
        cpus = found["cpus"]
        assert (cpus["reading"], cpus["unknown"], cpus["effective_set"]) == ("unknown", ["probe_unreadable"], None)
    finally:
        box.stop()


def test_o70_a_starter_that_left_the_cell_on_every_core_reads_widened(node, tmp_path):
    """O70: the canary's cores wider than the pin: widened, with the set the cell really had."""
    node.starter_cpus = frozenset({0, 1, 2, 3})
    box = _box(node, tmp_path)
    try:
        cpus = _limits(box)["cpus"]
        assert (cpus["reading"], cpus["widened"], cpus["effective_count"]) == ("widened", True, 4)
    finally:
        box.stop()


def test_o70_a_step_whose_cores_cannot_be_read_gets_no_pin_and_an_unknown_reading(node, tmp_path):
    """O70: no step set, no `taskset`: the cell is given no guessed pin, and the reading says so."""
    node.step_set_unreadable = True
    box = _box(node, tmp_path)
    try:
        assert node.execs[-1].cpus is None
        cpus = _limits(box)["cpus"]
        assert (cpus["reading"], cpus["step_set"], cpus["unknown"]) == ("unknown", None, ["step_set_unreadable"])
    finally:
        box.stop()


def test_o70_a_cell_that_forks_past_pids_is_killed_by_the_watch_and_the_next_cell_runs(node, tmp_path):
    """O70: the watch counts the tasks of the run's processes: past `pids` the cell is killed as `pids_limit`, the
    agent's own outcome with `scope: cell` (where Docker refuses the fork with EAGAIN), and the run goes on."""
    box = _box(node, tmp_path)
    try:

        def forking(cell):
            cell.procs[1].threads = 100
            _until(lambda: not cell.procs[1].alive)
            return CmdResult(137, "", "")

        node.cell_handler = forking
        result = box.exec("[os.fork() for _ in range(100)]", 30)
        assert (result.kill["reason"], result.kill["category"], result.kill["scope"]) == ("pids_limit", "agent", "cell")
        assert not result.timed_out
        assert result.kill["watch_kills"][0]["what"] == "pids"
        assert kinds.kill_error(result.kill) is None
        node.cell_handler = lambda cell: CmdResult(0, "again\n", "")
        assert box.exec("print('again')", 30).stdout == "again\n"
        pids = _limits(box)["pids"]
        assert (pids["reading"], pids["kills"], pids["max_seen"]) == ("over", 1, 101)
    finally:
        box.stop()


def test_o70_a_process_that_widens_its_own_affinity_is_recorded_and_the_cell_goes_on(node, tmp_path):
    """O70: affinity is a set the agent's own code can widen: the watch records the process and kills nothing."""
    box = _box(node, tmp_path)
    try:

        def widening(cell):
            cell.procs[1].cores = frozenset({0, 1, 2})
            time.sleep(0.3)
            return CmdResult(0, "done\n", "")

        node.cell_handler = widening
        result = box.exec("os.sched_setaffinity(0, {0, 1, 2})", 30)
        assert (result.exit_code, result.kill) == (0, None)
        cpus = _limits(box)["cpus"]
        assert (cpus["reading"], cpus["widened"], cpus["widened_processes"]) == ("widened", True, 1)
    finally:
        box.stop()


def test_o70_a_hidden_child_makes_pids_and_cpus_unknown_never_within_or_pinned(node, tmp_path):
    """O70: /proc hides a non-dumpable process's tasks and cores: with the memory reading holding `hidden_process`, the
    run was not shown within its pids or pinned to its cores."""
    box = _box(node, tmp_path)
    try:

        def hiding(cell):
            node._new_proc(cell.pidns, "python3", hidden=True, anon=1 << 20, parent=cell.procs[1].pid)
            time.sleep(0.4)
            return CmdResult(0, "", "")

        node.cell_handler = hiding
        box.exec("hide()", 30)
        found = _limits(box)
        assert box.metadata()["infrastructure_facts"]["memory"]["reading"] == "unknown"
        for name in ("pids", "cpus"):
            assert (found[name]["reading"], found[name]["unknown"]) == ("unknown", ["hidden_process"]), name
    finally:
        box.stop()


def test_o70_a_gate_past_its_memory_limit_is_killed_and_its_death_is_a_component_defect(node, tmp_path):
    """O70: the watch reads the gate's resident memory and SIGKILLs it over `gate_memory_bytes`; the collection records
    the gate's death with cause `component_defect` and a basis naming the watch, never `undetermined`, and the
    attempt is infrastructure of kind `sandbox`."""
    box = _box(node, tmp_path)
    try:
        gate = node.gates[-1].proc
        gate.rss = 2 << 30
        assert _until(lambda: not gate.alive)
        assert _limits(box)["gate_memory"] == {"declared": 1 << 30, "reading": "over", "unknown": [],
                                               "peak_bytes": 2 << 30}  # fmt: skip
        (death,) = box.collect(tmp_path / "out", [])["deaths"]
        assert (death["role"], death["cause"], death["category"]) == ("gate", ID.COMPONENT_DEFECT, "infrastructure")
        assert "watch" in death["basis"]
        assert "memory" in death["basis"]
        assert kinds.death_error([death]).kind == "sandbox"
    finally:
        box.stop()


def test_o70_a_gate_that_exited_by_itself_before_the_watch_s_signal_stays_undetermined(node, tmp_path):
    """O70: a signal sent to a gate that had already exited (a zombie, not yet reaped) succeeds without ending it: the
    death is the watch's only when the exit status says SIGKILL ended it, never from the signal's success alone."""
    box = _box(node, tmp_path)
    try:
        gate = node.gates[-1].proc
        real = node.signal

        def exited_first(pid, start, sig):
            if pid == gate.pid and sig == signal.SIGKILL:
                gate.alive, gate.exit_status = False, 0
                return True
            return real(pid, start, sig)

        node.signal = exited_first
        gate.rss = 2 << 30
        assert _until(lambda: not gate.alive)
        time.sleep(0.2)
        (death,) = box.collect(tmp_path / "out", [])["deaths"]
        assert (death["cause"], death["basis"]) == (
            ID.UNDETERMINED,
            "the gate's process ended during the run (exit status 0)",
        )
    finally:
        box.stop()


def test_o70_a_gate_that_died_on_its_own_stays_undetermined(node, tmp_path):
    """O70: only a death the watch's own signal caused is the watch's: a gate killed from outside is as it was."""
    box = _box(node, tmp_path)
    try:
        gate = node.gates[-1].proc
        node.signal(gate.pid, gate.start, signal.SIGKILL)
        time.sleep(0.2)
        (death,) = box.collect(tmp_path / "out", [])["deaths"]
        assert death["cause"] == ID.UNDETERMINED
        assert _limits(box)["gate_memory"]["reading"] == "within"
    finally:
        box.stop()


def test_o70_a_run_with_no_gate_has_nothing_to_bound_there(node, tmp_path):
    """O70: no REST data path, no gate: `not_applicable`, and no death to explain."""
    box = _box(node, tmp_path, "isolated")
    try:
        found = _limits(box)["gate_memory"]
        assert (found["reading"], found["unknown"], found["peak_bytes"]) == ("not_applicable", [], None)
        assert box.collect(tmp_path / "out", [])["deaths"] == []
    finally:
        box.stop()


def test_o70_the_record_has_every_key_before_the_run_has_started(node, tmp_path):
    """O70: the shape is fixed whatever the stage: a sandbox that was never started still gives every key, each
    reading unknown with a reason (never within, pinned or set)."""
    box = ApptainerSandbox(node, make_settings(tmp_path), make_spec(), **options())
    found = box.metadata()["infrastructure_facts"]["limits"]
    assert set(found) == {"version", "cpus", "pids", "nofile", "gate_memory"}
    assert {name: found[name]["reading"] for name in ("cpus", "pids", "nofile", "gate_memory")} == dict.fromkeys(
        ("cpus", "pids", "nofile", "gate_memory"), "unknown"
    )
    assert all(found[name]["unknown"] for name in ("cpus", "pids", "nofile", "gate_memory"))


def test_o70_the_facts_name_what_is_still_not_kept_as_a_cgroup_would(node, tmp_path):
    """O70: `cpu_pids_nofile` is split into what remains a departure from Docker, each with its reason: cpus is a set
    and not a quota, pids is a poll that kills where Docker refuses the fork, the gate's memory a poll; nofile is
    Docker's own rlimit and is no departure."""
    assert "cpu_pids_nofile" not in facts.FACTS
    assert {"cpu_affinity", "pids_watch", "gate_memory_watch"} <= set(facts.FACTS)
    assert not any("nofile" in key for key in facts.FACTS)
    assert "quota" in facts.FACTS["cpu_affinity"]
    assert "widen" in facts.FACTS["cpu_affinity"]
    assert "EAGAIN" in facts.FACTS["pids_watch"]
    assert "hidden" in facts.FACTS["pids_watch"]
    assert "poll" in facts.FACTS["gate_memory_watch"]
    assert set(facts.record(mount_refused=True)["facts"]) == set(facts.FACTS)
