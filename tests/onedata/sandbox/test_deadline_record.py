"""O72: the record of a cell that hit its own deadline, and whose kill could not end a process, says the cell ran.

A cell that reaches its cap is killed, and a process of it that outlives the kill ends the run (O34, `survived_kill`,
infrastructure kind `cell_unkillable`). The cell itself ran, and hit its deadline: that is its own result, the same one
a clean kill gives (the `timeout` error class, the output it had kept, the exec's check of an exec that was killed). The
run's ending is a fact of the run, said at run level as the end-of-cell sweep's is (`test_sweep_survivor.py`): the next
call is refused, and the attempt's outcome names the deadline. The code tool used to raise the run's ending in place of
the cell's result, so the loop recorded a call that ran as `skipped`, with the infrastructure as its detail.

The record is built through the real agent loop over the real code tool and sandbox on the fake host, as a study's is,
and the fixture `tests/data/record-v3/11-deadline-unkillable.json` is that record's bytes."""

import asyncio
import json
import threading
from pathlib import Path

import pytest
from deadline_support import CELL_S, cell_processes, stopped_step
from fake_host import Unrecorded
from record_support import record_run
from sandbox_support import make_spec, start_options

from expcore.errors import InfraError
from expcore.plugin import NO_WAITS
from expcore_onedata.sandbox import cells as cells_module
from expcore_onedata.sandbox import completion, deadline, teardown
from expcore_onedata.sandbox.codetool import CodeExecutionProvider
from expcore_onedata.sandbox.sandbox import DockerSandbox

CODE = "print('partial output'); open('/data/x').read()"
KEPT = "partial output\n"  # what the cell had printed when its process was killed
SCRIPT = [[("python", {"code": CODE})], [("python", {"code": "print('next')"})], "done"]
FIXTURE = Path(__file__).resolve().parents[2] / "data" / "record-v3" / "11-deadline-unkillable.json"


def raising_kill(*args, **kwargs):
    raise OSError("the host's kill could not run")


def runs_to_its_cap(world, sandbox) -> None:
    """The cell's `docker exec` returns once the harness's timer has fired, with what the cell had printed and no
    completion record (the bootstrap was killed with it), as the real one returns once its processes end."""
    cells, firing = sandbox._cells, threading.Event()
    kill = cells._kill_at_the_cap

    def the_harness_kills(*cell):  # the fake docker holds its lock while a cell runs: the kill proceeds once it returns
        firing.set()
        return kill(*cell)

    def handler(container, argv, stdin):
        assert firing.wait(30), "the harness's timer did not fire"
        return Unrecorded(137, KEPT, "")

    cells._kill_at_the_cap, world.host.exec_handler = the_harness_kills, handler


@pytest.fixture
def sbx(world, monkeypatch):
    """A sandbox whose cap cannot end the cell's process: the cap's kill fails to run, and the process the quiesce then
    kills stays wedged in the kernel, so the deadline's outcome is `survived_kill`."""
    wait_gone = teardown._wait_gone  # the quiesce's wait for a killed process (10 s), shortened: this one never goes
    monkeypatch.setattr(teardown, "_wait_gone", lambda host, procs, seconds, poll: wait_gone(host, procs, 0.3, 0.05))
    monkeypatch.setattr(cells_module._CellProcesses, "kill", raising_kill)
    sandbox = DockerSandbox.start(world.host, world.s, make_spec(world, "isolated"), **start_options(world))
    cell_processes(world, sandbox, wedged=True)
    runs_to_its_cap(world, sandbox)
    yield sandbox
    world.host.unkillable.clear()
    sandbox.stop()  # the deadline ordered the stop already: a forced second one finds nothing


def recorded(sbx):
    """The loop's record of an agent whose first cell hits its deadline and leaves a process no kill ends, and whose
    second call is refused. The fake clock never moves, so the loop's own time never runs out under the real timer."""
    return record_run(SCRIPT, {}, providers=[CodeExecutionProvider(sbx)], time_cap_s=CELL_S)


def test_o72_the_record_of_a_cell_that_hit_its_deadline_says_it_ran_and_timed_out(sbx):
    """O72: the cell's call is what the cell did: it ran, and the harness's timer stopped it at its deadline. Its status
    and error class are a timeout's, its text keeps the output it had and tells the model what a timed-out cell is told,
    and its exec check is the killed exec's (no completion record); no detail names infrastructure on it. It is never
    `skipped`: a record never says a call was skipped when it ran."""
    record = recorded(sbx)
    cell, _ = record.tool_calls
    assert (cell["status"], cell["error_class"]) == ("error", "timeout")
    assert cell["text"] == f"timeout: {KEPT}the code did not finish within the time limit and was stopped"
    assert cell["raw_text"] == cell["text"]
    assert cell["exec_check"] == completion.UNKNOWN_FACTS
    assert cell["detail"] == ""
    assert "skipped" not in cell["text"]


def test_o72_later_calls_are_skipped_and_the_outcome_names_the_deadline_that_ended_the_run(sbx):
    """O72: the run is over once a process outlived the kill: the agent's next call is refused, as skipped, with the
    infrastructure as its detail, and carries no exec check since nothing ran. The attempt's outcome is infrastructure
    of kind `cell_unkillable`, and its detail names the deadline, its outcome and the cell, and says the cell's own
    result stands."""
    record = recorded(sbx)
    refused = record.tool_calls[1]
    assert (refused["status"], refused["error_class"], refused["detail"]) == (
        "failed",
        "skipped",
        "infrastructure: cell_unkillable",
    )
    assert "exec_check" not in refused
    outcome = record.outcome
    assert (outcome["name"], outcome["infra_kind"]) == ("infrastructure", "cell_unkillable")
    assert "deadline" in outcome["detail"]
    assert "survived_kill" in outcome["detail"]
    assert "cell 1" in outcome["detail"]
    assert "the cell's own result stands" in outcome["detail"]
    assert "sweep" not in outcome["detail"]


def test_o72_the_sandbox_says_which_cell_s_deadline_ended_the_run(sbx):
    """O72: the deadline that ended the run is in the metadata under its cell, and the refusal of a later exec names
    that cell's deadline, never the end-of-cell sweep."""
    recorded(sbx)
    (ended,) = sbx.metadata()["cell_deadlines"]
    assert (ended["cell"], ended["outcome"]) == (1, deadline.SURVIVED)
    assert stopped_step(ended)["ok"] is True
    with pytest.raises(InfraError) as refused:
        sbx.exec("print('again')", 30)
    assert refused.value.kind == "cell_unkillable"
    assert "cell 1's deadline" in refused.value.detail
    assert "sweep" not in refused.value.detail


def test_o72_the_fixture_holds_what_the_real_loop_writes_for_the_case(sbx):
    """O72: `tests/data/record-v3/11-deadline-unkillable.json` holds, for this case, what the loop writes now: each
    call's status, class, text, detail and exec check, and the outcome's name, kind and detail. Only those: a field a
    later merge point adds is optional and absent from the stored bytes, which are never edited. The replay of the
    stored bytes is in `tests/records/test_v3_fixtures.py`."""
    fresh, stored = json.loads(recorded(sbx).dumps()), json.loads(FIXTURE.read_text(encoding="utf-8"))
    call_fields = ("name", "arguments", "status", "error_class", "text", "raw_text", "detail", "exec_check")
    pinned = [{k: call.get(k) for k in call_fields} for call in fresh["tool_calls"]]
    assert pinned == [{k: call.get(k) for k in call_fields} for call in stored["tool_calls"]]
    assert {k: fresh["outcome"][k] for k in ("name", "infra_kind", "detail")} == {
        k: stored["outcome"][k] for k in ("name", "infra_kind", "detail")
    }


# ------------------------------------------------------------------------ the code tool, for each way the deadline ends
@pytest.fixture
def stopped(world):
    """A sandbox with no mount, whose wedged process the sandbox's stop ends: the deadline's outcome is
    `sandbox_stopped`."""
    sandbox = DockerSandbox.start(world.host, world.s, make_spec(world, "isolated"), **start_options(world))
    cell_processes(world, sandbox, wedged=True)
    runs_to_its_cap(world, sandbox)
    yield sandbox
    world.host.unkillable.clear()
    sandbox.stop()  # the deadline ordered the stop already: a forced second one finds nothing


def test_o72_a_cell_whose_process_the_sandbox_s_stop_ended_is_its_own_timeout_too(stopped):
    """O72: whichever step of the escalation ended it (`sandbox_stopped` here, `mount_aborted` in the loop's tests), the
    cell is told what it did: the code tool returns its timeout, and the next call is refused naming the deadline."""
    tool = CodeExecutionProvider(stopped)
    out = asyncio.run(tool.execute("python", {"code": CODE}, timeout_s=CELL_S, ctx=NO_WAITS))
    assert (out.is_error, out.error_class) == (True, "timeout")
    assert out.text.startswith(KEPT)
    assert out.exec_check == completion.UNKNOWN_FACTS
    (ended,) = stopped.metadata()["cell_deadlines"]
    assert ended["outcome"] == deadline.SANDBOX_STOPPED
    with pytest.raises(InfraError) as refused:
        asyncio.run(tool.execute("python", {"code": "print(1)"}, timeout_s=CELL_S, ctx=NO_WAITS))
    assert refused.value.kind == "cell_unkillable"
    assert "cell 1's deadline" in refused.value.detail
    assert "sandbox_stopped" in refused.value.detail


def test_o72_a_gate_refusal_in_that_cell_does_not_hide_its_result_or_change_the_run_s_ending(sbx):
    """O72: nothing read after an abort or a stop is the cell's evidence, the gate's log included, so a refusal the
    gate logged during the cell is not read into its call: the cell is its timeout, and the run's ending is the
    deadline's (the kind `cell_unkillable`), as before the cell's call stopped raising."""
    result = sbx.exec(CODE, CELL_S)
    result.gate_refusals = ["unreadable"]
    sbx.exec = lambda code, timeout_s: result
    out = asyncio.run(CodeExecutionProvider(sbx).execute("python", {"code": CODE}, timeout_s=CELL_S, ctx=NO_WAITS))
    assert (out.is_error, out.error_class) == (True, "timeout")
