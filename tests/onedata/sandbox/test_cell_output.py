"""O40: what is kept of a cell's stdout and stderr is capped, the cut is recorded, and the agent is told of it.

A 1 MiB read printed about 1.4 MB, all of it read into the harness's memory and kept. The sandbox now keeps the first
`OUTPUT_CAP_BYTES` of each stream of a cell (a whole number of characters), reads and drops the rest, and records on the
cell how much was written and how much kept (`output_cut`), so the agent's view, which says so, and the record agree on
what was truncated. The runner is `hostops.run_bounded` (test_hostops_output.py); the host here is the in-memory one,
which cuts a result as the runner does.
"""

import asyncio

import pytest
from sandbox_support import make_spec, start_options

from expcore.plugin import NO_WAITS
from expcore_onedata.sandbox import cells
from expcore_onedata.sandbox.api import ExecResult
from expcore_onedata.sandbox.codetool import CodeExecutionProvider
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox


@pytest.fixture
def sbx(world):
    sandbox = DockerSandbox.start(
        world.host, world.s, make_spec(world, "isolated"), output_cap_bytes=100, **start_options(world)
    )
    yield sandbox
    sandbox.stop()


def prints(stdout: str, stderr: str = "", code: int = 0):
    return lambda container, argv, stdin: CmdResult(code, stdout, stderr)


def test_o40_the_cap_is_a_fixed_bound_of_the_core_that_a_sandbox_takes_unless_told_another(world):
    default = DockerSandbox(world.host, world.s, make_spec(world, "isolated"), **start_options(world))
    assert cells.OUTPUT_CAP_BYTES == 1 << 20
    assert default._cells is None  # not started: nothing is run, the bound is what a started one gives its runner
    assert default._output_cap == cells.OUTPUT_CAP_BYTES


def test_o40_a_cell_that_prints_more_than_the_cap_has_the_first_bytes_kept_and_the_cut_recorded(world, sbx):
    world.host.exec_handler = prints("a" * 1000, "b" * 30)
    result = sbx.exec("print('a' * 1000)", 5)
    assert result.stdout == "a" * 100
    assert result.stderr == "b" * 30
    assert result.output_cut == {"stdout": {"produced": 1000, "kept": 100}}  # only the stream that was cut
    assert sbx._cells.cells[-1]["output_cut"] == result.output_cut  # the cell's record says the same
    assert sbx.metadata()["cell_output_cuts"] == [{"cell": 1, **result.output_cut}]  # and so does the run's


def test_o40_both_streams_cut_are_both_recorded(world, sbx):
    world.host.exec_handler = prints("é" * 80, "z" * 500, 1)
    result = sbx.exec("1/0", 5)
    assert result.stdout == "é" * 50  # 100 bytes: whole characters
    assert result.output_cut == {"stdout": {"produced": 160, "kept": 100}, "stderr": {"produced": 500, "kept": 100}}


def test_o40_a_cell_within_the_cap_records_no_cut(world, sbx):
    world.host.exec_handler = prints("x" * 100, "y" * 100)  # exactly the cap: nothing was dropped
    result = sbx.exec("print(1)", 5)
    assert (result.stdout, result.stderr, result.output_cut) == ("x" * 100, "y" * 100, None)
    assert "output_cut" not in sbx._cells.cells[-1]
    assert "cell_output_cuts" not in sbx.metadata()


def test_o40_a_harness_step_that_was_cut_says_so_in_its_record(world, sbx):
    world.host.exec_handler = prints("s" * 500)
    sbx.exec("print(1)", 5, record=False)
    (step,) = sbx.metadata()["harness_steps"]
    assert step["output_cut"] == {"stdout": {"produced": 500, "kept": 100}}
    assert "cell_output_cuts" not in sbx.metadata()  # it is no cell of the agent's


def test_o40_the_cap_is_asked_of_the_host_for_every_cell_and_every_harness_step(world, sbx):
    asked = []
    real = world.host.docker

    def docker(*args, **kwargs):
        if args[0] == "exec" and "timeout" in args:
            asked.append(kwargs.get("output_cap"))
        return real(*args, **kwargs)

    world.host.docker = docker
    sbx.exec("print(1)", 5, record=False)  # a harness step runs before the agent's first cell, never after it
    sbx.exec("print(2)", 5)
    assert asked == [100, 100]


def scripted(**kw) -> CodeExecutionProvider:
    class Scripted:
        def exec(self, code, timeout_s):
            return ExecResult(kw.pop("stdout", ""), kw.pop("stderr", ""), kw.pop("exit_code", 0), 0.1, **kw)

    return CodeExecutionProvider(Scripted())


def execute(provider):
    return asyncio.run(provider.execute("python", {"code": "x"}, timeout_s=5.0, ctx=NO_WAITS))


def test_o40_the_model_is_told_what_the_sandbox_cut_and_the_record_flags_the_source_limit():
    """O40 with L07: the notice after the kept output says how much was written and kept, and the call's
    `source_truncated` is True, apart from the harness's own cap."""
    cut = {"stdout": {"produced": 1_400_000, "kept": 1_048_576}}
    got = execute(scripted(stdout="data", output_cut=cut))
    assert got.text == "data\n[output cut by the sandbox: the first 1048576 of the 1400000 bytes of stdout were kept]"
    assert got.source_truncated is True
    assert not got.is_error


def test_o40_a_failed_cell_with_cut_streams_says_so_after_the_error_text():
    cut = {"stdout": {"produced": 500, "kept": 100}, "stderr": {"produced": 900, "kept": 100}}
    got = execute(scripted(stdout="out", stderr="Traceback", exit_code=1, output_cut=cut))
    assert got.is_error
    assert got.error_class == "exit_status"
    assert got.text == (
        "out\nexit code 1\nTraceback\n"
        "[output cut by the sandbox: the first 100 of the 500 bytes of stdout were kept; "
        "the first 100 of the 900 bytes of stderr were kept]"
    )
    assert got.source_truncated is True


def test_o40_a_cell_that_was_not_cut_says_nothing_of_the_source_limit():
    got = execute(scripted(stdout="all of it"))
    assert (got.text, got.source_truncated) == ("all of it", None)
