"""O43: what is kept of a cell's stdout and stderr is capped, the cut is recorded, and the agent is told of it.

A 1 MiB read printed about 1.4 MB, all of it read into the harness's memory and kept. The sandbox now keeps the first
`OUTPUT_CAP_BYTES` of each stream of a cell (a whole number of characters), reads and drops the rest, and records on the
cell how much was written and how much kept (`output_cut`), so the agent's view, which says so, and the record agree on
what was truncated. The runner is `hostops.run_bounded` (test_hostops_output.py); the host here is the in-memory one,
which cuts a result as the runner does.
"""

import asyncio
from pathlib import Path

import pytest
from sandbox_support import make_spec, start_options

from expcore.loop.toolbox import ToolBox
from expcore.models.adapters import ToolCall
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


def test_o43_the_cap_is_a_fixed_bound_of_the_core_that_a_sandbox_takes_unless_told_another(world):
    default = DockerSandbox(world.host, world.s, make_spec(world, "isolated"), **start_options(world))
    assert cells.OUTPUT_CAP_BYTES == 1 << 20
    assert default._cells is None  # not started: nothing is run, the bound is what a started one gives its runner
    assert default._output_cap == cells.OUTPUT_CAP_BYTES


def test_o43_a_cell_that_prints_more_than_the_cap_has_the_first_bytes_kept_and_the_cut_recorded(world, sbx):
    world.host.exec_handler = prints("a" * 1000, "b" * 30)
    result = sbx.exec("print('a' * 1000)", 5)
    assert result.stdout == "a" * 100
    assert result.stderr == "b" * 30
    assert result.output_cut == {"stdout": {"produced": 1000, "kept": 100}}  # only the stream that was cut
    assert sbx._cells.cells[-1]["output_cut"] == result.output_cut  # the cell's record says the same
    assert sbx.metadata()["cell_output_cuts"] == [{"cell": 1, **result.output_cut}]  # and so does the run's


def test_o43_both_streams_cut_are_both_recorded(world, sbx):
    world.host.exec_handler = prints("é" * 80, "z" * 500, 1)
    result = sbx.exec("1/0", 5)
    assert result.stdout == "é" * 50  # 100 bytes: whole characters
    assert result.output_cut == {"stdout": {"produced": 160, "kept": 100}, "stderr": {"produced": 500, "kept": 100}}


def test_o43_a_cell_within_the_cap_records_no_cut(world, sbx):
    world.host.exec_handler = prints("x" * 100, "y" * 100)  # exactly the cap: nothing was dropped
    result = sbx.exec("print(1)", 5)
    assert (result.stdout, result.stderr, result.output_cut) == ("x" * 100, "y" * 100, None)
    assert "output_cut" not in sbx._cells.cells[-1]
    assert "cell_output_cuts" not in sbx.metadata()


def test_o43_a_harness_step_that_was_cut_says_so_in_its_record(world, sbx):
    world.host.exec_handler = prints("s" * 500)
    sbx.exec("print(1)", 5, record=False)
    (step,) = sbx.metadata()["harness_steps"]
    assert step["output_cut"] == {"stdout": {"produced": 500, "kept": 100}}
    assert "cell_output_cuts" not in sbx.metadata()  # it is no cell of the agent's


def test_o43_the_cap_is_asked_of_the_host_for_every_cell_and_every_harness_step(world, sbx):
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


def test_o43_the_model_is_told_what_the_sandbox_cut_and_the_record_flags_the_source_limit():
    """O43 with L07: the notice after the kept output says how much was written and kept, and the call's
    `source_truncated` is True, apart from the harness's own cap."""
    cut = {"stdout": {"produced": 1_400_000, "kept": 1_048_576}}
    got = execute(scripted(stdout="data", output_cut=cut))
    assert got.text == "data\n[output cut by the sandbox: the first 1048576 of the 1400000 bytes of stdout were kept]"
    assert got.source_truncated is True
    assert not got.is_error


def test_o43_a_failed_cell_with_cut_streams_says_so_after_the_error_text():
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


def test_o43_a_cell_that_was_not_cut_says_nothing_of_the_source_limit():
    got = execute(scripted(stdout="all of it"))
    assert (got.text, got.source_truncated) == ("all of it", None)


# What the model receives when the agent's own cap (`output_cap_chars`) meets the sandbox's cut. The order is fixed by
# the data flow: the sandbox cuts each stream at its cap and the code tool ends the text with its notice, then the
# loop's cap cuts the whole text from its end, its own notice counted inside it (L06). The numbers are fixed by the
# cell below: 3 MiB of one character on stdout, of which the sandbox keeps 1 MiB.
KEPT, PRODUCED = 1 << 20, 3 << 20
SANDBOX_NOTE = "[output cut by the sandbox: the first 1048576 of the 3145728 bytes of stdout were kept]"
KEPT_TEXT = "a" * KEPT + "\n" + SANDBOX_NOTE  # what the code tool returns: 1048664 characters


@pytest.fixture
def big_cell(world):
    """A sandbox with the default cap, whose one cell prints 3 MiB."""
    sandbox = DockerSandbox.start(world.host, world.s, make_spec(world, "isolated"), **start_options(world))
    world.host.exec_handler = prints("a" * PRODUCED)
    yield sandbox
    sandbox.stop()


def model_sees(sandbox: DockerSandbox, cap_chars: int):
    box = ToolBox([CodeExecutionProvider(sandbox)], cap_chars=cap_chars)
    return asyncio.run(box.run(ToolCall("call-1", "python", {"code": "print('a' * 3 * 1024 * 1024)"}), timeout_s=5.0))


def test_o43_an_agent_cap_below_the_sandbox_cap_cuts_the_sandbox_note_and_the_model_sees_the_loops_notice(big_cell):
    """O43: the sandbox cut (1 MiB of 3) applies first and the agent's cap (10,000) second, from the end of the text,
    so the sandbox's note, which ends the text, goes with the cut tail and the model sees the loop's notice alone; the
    call's record keeps both flags and the whole text, note included, in `raw_text`."""
    run = model_sees(big_cell, 10_000)
    assert run.result.text == (
        "a" * 9923 + "\n[output truncated: 1038741 of 1048664 characters dropped by the harness cap]"
    )
    assert len(run.result.text) == 10_000  # the loop's notice counts inside the cap
    assert "sandbox" not in run.result.text
    assert (run.truncated, run.source_truncated, run.status) == (True, True, "ok")
    assert (run.raw_text, run.raw_chars) == (KEPT_TEXT, 1_048_664)
    assert big_cell._cells.cells[-1]["output_cut"] == {"stdout": {"produced": PRODUCED, "kept": KEPT}}


def test_o43_the_text_the_model_sees_is_the_same_on_every_run(big_cell):
    first = model_sees(big_cell, 10_000)
    second = model_sees(big_cell, 10_000)
    assert first.result.text == second.result.text
    assert (first.raw_text, first.truncated, first.source_truncated) == (
        second.raw_text,
        second.truncated,
        second.source_truncated,
    )


def test_o43_an_agent_cap_above_the_sandbox_cap_leaves_the_sandbox_note_as_the_last_line(big_cell):
    """O43: with the cap above the kept text and its note (here 2 MiB), the loop cuts nothing: the model sees the
    sandbox's note, whole, after the 1 MiB that was kept, and no notice of the loop's."""
    run = model_sees(big_cell, 2 << 20)
    assert run.result.text == KEPT_TEXT
    assert run.result.text.endswith("\n" + SANDBOX_NOTE)
    assert "harness cap" not in run.result.text
    assert (run.truncated, run.source_truncated) == (False, True)  # only the sandbox's own cut, as L07 says


def test_o43_an_agent_cap_that_the_kept_text_fits_but_its_note_does_not_shows_the_loops_notice_alone(big_cell):
    run = model_sees(big_cell, KEPT)  # exactly what the sandbox kept: the note cannot follow it
    assert run.result.text == "a" * 1_048_503 + (
        "\n[output truncated: 161 of 1048664 characters dropped by the harness cap]"
    )
    assert len(run.result.text) == KEPT
    assert (run.truncated, run.source_truncated) == (True, True)


def test_o43_a_cap_a_few_characters_short_of_the_kept_text_and_its_note_leaves_a_piece_of_the_note(big_cell):
    """O43: the note is part of the text the cap counts, and the loop's notice takes room from the cap too, so a cap
    1 to 14 characters below the whole text (1,048,650 to 1,048,663) cuts the note in the middle. It is the same
    piece on every run, and a study keeps its cap away."""
    run = model_sees(big_cell, 1_048_663)
    assert run.result.text == (
        "a" * KEPT + "\n[output cut by\n[output truncated: 73 of 1048664 characters dropped by the harness cap]"
    )
    assert len(run.result.text) == 1_048_663
    shortest = model_sees(big_cell, 1_048_650)  # the first cap that leaves a piece of it: one character
    assert shortest.result.text == (
        "a" * KEPT + "\n[\n[output truncated: 86 of 1048664 characters dropped by the harness cap]"
    )
    nothing = model_sees(big_cell, 1_048_649)  # one less leaves no piece: the loop's notice follows the newline
    assert nothing.result.text == (
        "a" * KEPT + "\n\n[output truncated: 87 of 1048664 characters dropped by the harness cap]"
    )


def test_o43_a_cap_too_short_for_the_loops_notice_cuts_the_text_plain_and_77_characters_hold_the_notice_alone(big_cell):
    """O43 with L06: the notice that counts inside the cap needs 77 characters here; below that the model reads the
    first characters of the output and no notice, at that it reads the notice and no output."""
    assert model_sees(big_cell, 76).result.text == "a" * 76
    assert model_sees(big_cell, 77).result.text == (
        "\n[output truncated: 1048664 of 1048664 characters dropped by the harness cap]"
    )


def test_o43_a_cap_of_2_mib_and_1_kib_cuts_nothing_of_the_longest_text_the_default_cap_lets_the_tool_return():
    """O43: the bound the guide states: both streams kept at 1 MiB, a failure's exit code line and both clauses of the
    notice, with the cell's byte counts as long as they can be, are shorter than 2 MiB and 1 KiB."""
    huge = 10**19  # a count of 20 digits
    cut = {name: {"produced": huge, "kept": KEPT} for name in ("stdout", "stderr")}
    got = execute(scripted(stdout="a" * KEPT, stderr="b" * KEPT, exit_code=255, output_cut=cut))
    assert len(got.text) < (2 << 20) + 1024
    box = ToolBox(
        [scripted(stdout="a" * KEPT, stderr="b" * KEPT, exit_code=255, output_cut=cut)], cap_chars=(2 << 20) + 1024
    )
    run = asyncio.run(box.run(ToolCall("call-1", "python", {"code": "x"}), timeout_s=5.0))
    assert (run.truncated, run.result.text) == (False, f"exit_status: {got.text}")


def test_o43_the_study_guide_and_the_interface_notes_quote_the_strings_the_model_reads():
    """O43: the exact notices a study freezes are written in the docs as the code and the tests have them."""
    root = Path(__file__).resolve().parents[3]
    guide = (root / "docs" / "study-guide.md").read_text(encoding="utf-8")
    interfaces = (root / "docs" / "interfaces.md").read_text(encoding="utf-8")
    template = "[output cut by the sandbox: the first K of the P bytes of stdout were kept]"
    harness = "[output truncated: 1038741 of 1048664 characters dropped by the harness cap]"
    harness_template = "[output truncated: D of T characters dropped by the harness cap]"
    for quoted in (SANDBOX_NOTE, template, harness, harness_template):
        assert quoted in guide, quoted
    for quoted in (template, harness_template):
        assert quoted in interfaces, quoted
