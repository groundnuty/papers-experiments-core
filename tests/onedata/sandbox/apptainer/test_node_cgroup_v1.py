"""O56: the node reads the step's memory cgroup on a cgroup v1 host as it does on v2, from a fake `/proc` and
`/sys/fs/cgroup` built from one real reading of a Slurm step on a v1 node (`cgroup_v1_support`)."""

import pytest
from cgroup_v1_support import JOB, NO_LIMIT, OWN, READING, STEP, level, v1_host

from expcore_onedata.sandbox.apptainer import node as N
from expcore_onedata.sandbox.apptainer import procs as P
from expcore_onedata.sandbox.apptainer.cells import _oom_rose
from expcore_onedata.sandbox.apptainer.node import LocalNode

STAT = "4242 (python3 (cell)) S 4200 4242 4242 0 -1 4194560 " + " ".join(["0"] * 12) + " 987654 0 0"
STATUS = "Name:\tpython3\nRssAnon:\t  2048 kB\nRssShmem:\t 0 kB\nThreads:\t1\n"


def test_o56_the_limit_comes_from_the_nearest_cgroup_that_has_one(tmp_path, monkeypatch):
    """O56: the step process's own cgroup has no limit (v1's near-2^63), so the walk goes up one level to the step's and
    reads the limit and the peak there."""
    v1_host(tmp_path, monkeypatch)
    got = LocalNode().cgroup_memory()
    assert got["depth"] == 1
    assert got["max_bytes"] == int(level(STEP)["files"]["memory.limit_in_bytes"]) == 542236672
    assert got["peak_bytes"] == int(level(STEP)["files"]["memory.max_usage_in_bytes"])
    assert got["events"] == {"max": 0, "oom_kill": 0}


def test_o56_a_kill_the_kernel_counted_in_the_step_process_s_own_cgroup_is_the_oom_count(tmp_path, monkeypatch):
    """O56: the kernel counts an OOM kill in the cgroup the victim sits in, not in the one whose limit it hit: in the
    real reading the own cgroup shows `oom_kill 1` after the kill while the limited step above it shows 0. The count is
    the own cgroup's; `max` is the limit-holder's `failcnt`."""
    v1_host(tmp_path, monkeypatch, after={OWN: True, STEP: True})
    got = LocalNode().cgroup_memory()
    assert got["events"] == {"max": 74, "oom_kill": 1}
    assert got["peak_bytes"] == got["max_bytes"]  # the step used all it was allowed


def test_o56_a_level_with_no_limit_is_walked_past_and_a_walk_with_none_is_unknown(tmp_path, monkeypatch):
    """O56: the step's level unlimited too: the job's limit is the one, two levels up; none anywhere is None."""
    v1_host(tmp_path / "a", monkeypatch, files={STEP: {"memory.limit_in_bytes": NO_LIMIT}})
    got = LocalNode().cgroup_memory()
    assert (got["depth"], got["max_bytes"]) == (2, int(level(JOB)["files"]["memory.limit_in_bytes"]))
    unlimited = {"memory.limit_in_bytes": NO_LIMIT}
    v1_host(tmp_path / "b", monkeypatch, files={STEP: unlimited, JOB: unlimited})
    assert LocalNode().cgroup_memory() is None


def test_o56_a_kernel_with_no_oom_kill_line_gives_no_oom_count_and_no_attribution(tmp_path, monkeypatch):
    """O56: a v1 kernel without the `oom_kill` line of `memory.oom_control`: the count is left out (never 0, never a
    guess), and the cell's attribution reads that as no rise."""
    v1_host(tmp_path, monkeypatch, files={OWN: {"memory.oom_control": "oom_kill_disable 0\nunder_oom 0\n"}})
    got = LocalNode().cgroup_memory()
    assert got["events"] == {"max": 0}
    assert not _oom_rose(got, {"events": {"max": 3}})
    assert not _oom_rose(got, {"events": {"max": 3, "oom_kill": 1}})  # a kill from before the cell is no rise


@pytest.mark.parametrize(
    ("level_path", "name", "figure"),
    [
        (STEP, "memory.max_usage_in_bytes", "peak_bytes"),
        (STEP, "memory.failcnt", "max"),
        (OWN, "memory.oom_control", "oom_kill"),
    ],
)
def test_o56_a_file_that_is_not_there_leaves_only_its_figure_unknown(tmp_path, monkeypatch, level_path, name, figure):
    """O56: the peak, the `max` count and the OOM count are each unknown alone when their file cannot be read."""
    v1_host(tmp_path, monkeypatch, files={level_path: {name: None}})
    got = LocalNode().cgroup_memory()
    assert got["max_bytes"] == 542236672
    assert (got["peak_bytes"] is None) == (figure == "peak_bytes")
    assert {"max", "oom_kill"} - set(got["events"]) == ({figure} - {"peak_bytes"})


def test_o56_no_readable_limit_and_no_proc_cgroup_are_unknown(tmp_path, monkeypatch):
    """O56: nothing readable is None, never a crash."""
    v1_host(tmp_path / "a", monkeypatch, files={lv: {"memory.limit_in_bytes": None} for lv in (OWN, STEP, JOB)})
    assert LocalNode().cgroup_memory() is None
    nothing = tmp_path / "b"
    nothing.mkdir()
    monkeypatch.setattr(N, "PROC", nothing)
    assert LocalNode().cgroup_memory() is None
    assert LocalNode().own_cgroup() is None


def test_o56_the_cgroup_s_anonymous_memory_on_v1_is_total_rss_and_falls_back_to_rss(tmp_path, monkeypatch):
    """O56: v1's `total_rss` counts the cgroup and everything below it, as v2's `anon` does; `rss` (the cgroup alone)
    only when there is no `total_rss`; unknown for a cgroup or a file that is not there."""
    v1_host(tmp_path / "a", monkeypatch)
    node = LocalNode()
    assert node.cgroup_anon(OWN) == 450560
    assert node.cgroup_anon(STEP) == 888832  # more than the step's own `rss` of 434176: what is below it counts
    assert node.cgroup_anon("/slurm/uid_UID/job_JOB/step_other") is None
    stat = level(OWN)["files"]["memory.stat"]
    without_total = "".join(line + "\n" for line in stat.splitlines() if not line.startswith("total_rss"))
    v1_host(tmp_path / "b", monkeypatch, files={OWN: {"memory.stat": without_total}})
    assert LocalNode().cgroup_anon(OWN) == 450560  # the rss line: this cgroup's own, the same here
    v1_host(tmp_path / "c", monkeypatch, files={OWN: {"memory.stat": None}})
    assert LocalNode().cgroup_anon(OWN) is None


def test_o56_the_own_cgroup_on_v1_is_the_memory_controller_s_path(tmp_path, monkeypatch):
    """O56: the path the hidden-memory check holds and passes back to `cgroup_anon` is the `memory` line of the node's
    `/proc/self/cgroup`, not the devices, freezer or cpuset line's."""
    v1_host(tmp_path, monkeypatch)
    assert LocalNode().own_cgroup() == OWN


def test_o56_a_listed_process_on_v1_has_the_same_kind_of_path_as_the_own_cgroup(tmp_path, monkeypatch):
    """O56: the check keeps the processes whose path is in the own cgroup's (`in_cgroup`), so a listed process's path
    is the memory line's too: a process of the step is in it, one of another job is not."""
    v1_host(tmp_path, monkeypatch)
    for pid, text in (
        ("4242", READING["proc_self_cgroup"]),
        ("4243", READING["proc_self_cgroup"].replace("JOB", "ANOTHER")),
    ):
        (N.PROC / pid).mkdir()
        (N.PROC / pid / "stat").write_text(STAT)
        (N.PROC / pid / "status").write_text(STATUS)
        (N.PROC / pid / "cgroup").write_text(text)
    found = {proc.pid: proc for proc in LocalNode().processes()}
    assert found[4242].cgroup == OWN
    assert P.in_cgroup(found[4242].cgroup, OWN)
    assert not P.in_cgroup(found[4243].cgroup, OWN)


def test_o56_a_v2_host_reads_as_before(tmp_path, monkeypatch):
    """O56: a `0::` line and the unified hierarchy's files: the limit from `memory.max` (`max` walks on), the
    `memory.events` as they are, `memory.peak`, the path itself, and `anon`."""
    proc, sys_cgroup = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "cgroup").write_text("0::/job/step\n")
    (sys_cgroup / "job" / "step").mkdir(parents=True)
    (sys_cgroup / "job" / "step" / "memory.max").write_text("max\n")
    (sys_cgroup / "job" / "step" / "memory.stat").write_text("anon 536870912\nfile 0\n")
    (sys_cgroup / "job" / "memory.max").write_text("1073741824\n")
    (sys_cgroup / "job" / "memory.events").write_text("low 0\nhigh 0\nmax 2\noom 1\noom_kill 1\n")
    (sys_cgroup / "job" / "memory.peak").write_text("5000\n")
    monkeypatch.setattr(N, "PROC", proc)
    monkeypatch.setattr(N, "CGROUP_ROOT", sys_cgroup)
    node = LocalNode()
    assert node.own_cgroup() == "/job/step"
    assert node.cgroup_memory() == {"depth": 1, "max_bytes": 1073741824, "peak_bytes": 5000,
                                    "events": {"low": 0, "high": 0, "max": 2, "oom": 1, "oom_kill": 1}}  # fmt: skip
    assert node.cgroup_anon("/job/step") == 536870912


def test_o56_a_hybrid_host_reads_v2_when_its_memory_files_are_there_and_v1_when_not(tmp_path, monkeypatch):
    """O56: a `0::` line beside the `memory` line: the v2 path when its `memory.stat` exists under the root, else the
    memory controller's v1 path."""
    hybrid = "0::/unified/path\n" + READING["proc_self_cgroup"]
    sys_cgroup = v1_host(tmp_path / "a", monkeypatch, proc_cgroup=hybrid)
    assert LocalNode().own_cgroup() == OWN  # the controller is on v1: no v2 memory files
    (sys_cgroup / "unified" / "path").mkdir(parents=True)
    (sys_cgroup / "unified" / "path" / "memory.stat").write_text("anon 1\n")
    assert LocalNode().own_cgroup() == "/unified/path"
