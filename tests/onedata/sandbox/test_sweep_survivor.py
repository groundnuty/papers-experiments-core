"""O72: the record of a run the end-of-cell sweep ended keeps the cell's own result and names the sweep.

A Docker cell's exec returned normally, with its exit status, its output and its completion record; the sweep after it
then could not end a worker the cell left, so the run ends as `survived_kill` (infrastructure kind `cell_unkillable`,
set aside). Two things must not be confused in the record the loop writes: the cell's result is what the cell did, whole
and unchanged, and the run's ending is the sweep's failure, said in the outcome's own words. The record is built through
the real agent loop over the real code tool and sandbox on the fake host, as a study's is."""

import pytest
from deadline_support import cell_processes
from record_support import record_run
from sandbox_support import make_spec, start_options

from expcore_onedata.sandbox import teardown
from expcore_onedata.sandbox.codetool import CodeExecutionProvider
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox

SCRIPT = [[("python", {"code": "start_a_worker()"})], [("python", {"code": "print('next')"})], "done"]


@pytest.fixture
def sbx(world, monkeypatch):
    wait_gone = teardown._wait_gone  # the sweep's wait for a survivor (10 s), shortened: this one never goes
    monkeypatch.setattr(teardown, "_wait_gone", lambda host, procs, seconds, poll: wait_gone(host, procs, 0.3, 0.05))
    sandbox = DockerSandbox.start(world.host, world.s, make_spec(world, "isolated"), **start_options(world))
    yield sandbox
    world.host.unkillable.clear()
    sandbox.stop(force=True)


def recorded(world, sbx, exit_code: int):
    """The loop's record of an agent that runs a cell which leaves a worker no kill ends, and then asks for another."""
    pids = cell_processes(world, sbx)
    world.host.unkillable.add(pids["grandchild"])
    world.host.exec_handler = lambda container, argv, stdin: CmdResult(exit_code, "worker started\n", "")
    return record_run(SCRIPT, {}, providers=[CodeExecutionProvider(sbx)])


@pytest.mark.parametrize(
    ("exit_code", "status", "error_class"), [(0, "ok", None), (3, "error", "exit_status")], ids=["exit 0", "exit 3"]
)
def test_o72_the_record_keeps_the_cell_s_own_result_and_names_the_sweep_as_the_run_s_ending(
    world, sbx, exit_code, status, error_class
):
    """O72: the cell's call has its real status, its output and the exec's check, whatever came after; the outcome is
    infrastructure of kind `cell_unkillable` whose detail says it was the end-of-cell sweep after that cell, never a
    deadline the cell had not reached."""
    record = recorded(world, sbx, exit_code)
    cell, refused = record.tool_calls
    assert (cell["status"], cell["error_class"]) == (status, error_class)
    assert "worker started" in cell["text"]
    assert cell["exec_check"] == {"source_whole": True, "exit_status": exit_code, "nonce_matched": True}
    assert cell["detail"] == ""
    assert refused["status"] == "failed"
    assert "exec_check" not in refused
    outcome = record.outcome
    assert (outcome["name"], outcome["infra_kind"]) == ("infrastructure", "cell_unkillable")
    assert "end-of-cell sweep" in outcome["detail"]
    assert "cell 1" in outcome["detail"]
    assert "deadline" not in outcome["detail"]
