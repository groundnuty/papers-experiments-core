"""O91: on the Apptainer backend, the one exec that runs the agent's code carries the read-log binds; the others do not.

The rule this file pins: the ONLY `apptainer exec` builder that runs the AGENT's code (or a study's canonical scripts,
which run in the agent's code tool) is the cell exec, `ApptainerSandbox.cell_argv`, and with the channel on it always
carries the two read-only read-log binds. The other two builders run the harness's own fixed code and need none:

* the startup probes (`ApptainerSandbox._probe_argv`): a certifi probe and a `cat` of a trust path;
* the collection's copy execs (the `argv_of` of `workdisk.ImageCopyHost`, built in `Collection._image_outputs`): the
  harness's own copy helper.

Four tests do the work. The structural ones read the sources with `ast` (as `test_infrafacts.py`'s scanner does) and
fail until a new exec builder, a second way to spell an exec, or a new feeder of a probe's command is classified here:
a builder that runs a caller's command with no read-log bind would let a canonical script read without a record, and
one that carries the binds into a harness-only exec would hand the collector's socket to code that is not the agent's.
The scanner is tested on synthetic sources first, so a guard that cannot fail is caught. The behavioural ones read the
execs the fake node saw, on a run that has every kind of exec (via-gate, an image work disk, a read-only input, the
channel on): the probes and the copy execs are exact, fixed commands with no read-log bind and no path of the caller's,
and every cell of that run carries both binds.

Not repeated here: `test_readlog.py::test_o91_every_cell_binds_the_socket_directory_and_the_client_read_only` asserts
both binds, read-only at the fixed targets, in every cell the fake node saw on `via-gate` and `isolated` with a
directory work disk. The cell test below adds the configuration that one does not cover (the image work disk with a
read-only input) and the builder itself, `cell_argv`, called directly.
"""

from __future__ import annotations

import ast
import dataclasses
import errno
import json
from pathlib import Path

import pytest
from apptainer_support import make_settings, make_spec, start
from fake_node import CERTIFI, Exec, FakeNode, parse_exec

from expcore_onedata.readlog import files, paths
from expcore_onedata.sandbox.api import ReadOnlyInput
from expcore_onedata.sandbox.apptainer import workdisk
from expcore_onedata.sandbox.containers import CERTIFI_PROBE, trust_targets
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.inputs import CHANGES, PROBE_MARK

SRC = Path(__file__).resolve().parents[4] / "src" / "expcore_onedata"
APPTAINER = "sandbox/apptainer/"  # the backend's package, as the scanner names it
READLOG_TARGETS = (paths.SOCKET_DIR, paths.CLIENT_DIR)

# What the sources hold today, classified. A change to any of these sets is the guard doing its job: classify the new
# place by the rule in the module's docstring (does it run the agent's code?), and say which way in the PR.
CELL = f"{APPTAINER}sandbox.py::ApptainerSandbox.cell_argv"  # runs the agent's code: carries the read-log binds
PROBE = f"{APPTAINER}sandbox.py::ApptainerSandbox._probe_argv"  # harness code only: no read-log bind
COPY = f"{APPTAINER}collect.py::Collection._image_outputs.<lambda>"  # the copy helper: no read-log bind
PRIMITIVE = f"{APPTAINER}argv.py::exec_argv"  # the one place that spells `exec`; the three above call it
SPAWNS = {  # every place that hands an argv to the node's `apptainer(...)`, and what it hands
    f"{APPTAINER}node.py::LocalNode.node_facts": "version, not an exec: `[*apptainer, '--version']`",
    f"{APPTAINER}sandbox.py::ApptainerSandbox._trust_bundles": "the two probes",
    f"{APPTAINER}workdisk.py::ImageCopyHost.copy_helper": "the copy exec",
    f"{APPTAINER}cells.py::AppCellRunner.run": "the cell exec",
}
FEEDS = {  # what each caller passes a builder: a literal command of the harness's own, never a variable of a caller's
    f"{APPTAINER}sandbox.py::ApptainerSandbox._trust_bundles <- _probe_argv": {
        "[self.app.python, '-c', CERTIFI_PROBE]",
        "['cat', path]",  # `path` is one of the platform's declared trust paths or the image's certifi bundle
    },
    f"{APPTAINER}workdisk.py::ImageCopyHost.copy_helper <- _argv_of": {
        "[self._python, '-I', '-B', '-c', wrapper_source(), operation]",
    },
    f"{APPTAINER}cells.py::AppCellRunner.run <- _argv_of": {
        "AV.cell_command(limit_s, completion.argv(self._python))",  # the completion bootstrap; the code goes on stdin
    },
}


# ------------------------------------------------------------------------------------------------ the scanner
class _Places(ast.NodeVisitor):
    """Where, by enclosing class, function and lambda, a source names `exec_argv`, spells the word `exec`, calls a
    node's `apptainer(...)`, or feeds a builder (`_probe_argv`, `_argv_of`)."""

    def __init__(self, module: str) -> None:
        self.module, self.stack = module, []
        self.found: dict[str, set[str]] = {"exec_argv": set(), "exec_word": set(), "apptainer": set(), "feeds": set()}

    def place(self) -> str:
        return f"{self.module}::{'.'.join(self.stack) or '<module>'}"

    def _scoped(self, node: ast.AST, name: str) -> None:
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._scoped(node, "<lambda>")

    def visit_Assign(self, node: ast.Assign) -> None:
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            return  # the module's export list names its functions as strings
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "exec_argv":  # called, aliased, passed as a value
            self.found["exec_argv"].add(self.place())

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "exec_argv":
            self.found["exec_argv"].add(self.place())
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if any(alias.name == "exec_argv" for alias in node.names):
            self.found["exec_argv"].add(self.place())

    def visit_Constant(self, node: ast.Constant) -> None:
        if node.value == "exec":  # a hand-rolled `[*apptainer, "exec", ...]`
            self.found["exec_word"].add(self.place())
        elif node.value == "exec_argv":  # `getattr(argv, "exec_argv")`
            self.found["exec_argv"].add(self.place())

    def visit_Call(self, node: ast.Call) -> None:
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
        if name == "apptainer" and isinstance(node.func, ast.Attribute):
            self.found["apptainer"].add(self.place())
        if name in ("_probe_argv", "_argv_of"):
            arguments = ", ".join(ast.unparse(a) for a in [*node.args, *(k.value for k in node.keywords)])
            self.found["feeds"].add(f"{self.place()} <- {name}: {arguments}")
        self.generic_visit(node)


def scan(source: str, module: str) -> dict[str, set[str]]:
    visitor = _Places(module)
    visitor.visit(ast.parse(source))
    return visitor.found


def scan_tree() -> dict[str, set[str]]:
    """Every source of `expcore_onedata`: the places of `exec_argv` and of `apptainer(...)` anywhere, the word `exec`
    in the Apptainer backend's package alone (Docker's own `exec` is another backend's)."""
    total: dict[str, set[str]] = {"exec_argv": set(), "exec_word": set(), "apptainer": set(), "feeds": set()}
    for path in sorted(SRC.rglob("*.py")):
        module = path.relative_to(SRC).as_posix()
        found = scan(path.read_text(encoding="utf-8"), module)
        if not module.startswith(APPTAINER):
            found["exec_word"] = set()
        for key, places in found.items():
            total[key] |= places
    return total


def test_o91_the_scanner_reports_every_way_to_name_an_exec_builder():
    """O91: the structural guard can fail. A synthetic source for each way past it: an aliased import, a reference
    passed as a value, a name looked up as a string, a hand-rolled exec, and a node's `apptainer(...)` call; and
    nothing for a docstring, an export list or the word in a longer string."""
    cases = {
        "exec_argv": [
            "from expcore_onedata.sandbox.apptainer.argv import exec_argv as build\n",
            "def f(AV):\n    return run(argv_of=AV.exec_argv)\n",
            "def f(AV):\n    return AV.exec_argv(a, b, c, d, e)\n",
            "def f(AV):\n    return getattr(AV, 'exec_argv')\n",
            "class K:\n    def m(self):\n        build = lambda c: exec_argv(c)\n",
        ],
        "exec_word": ["def f(app):\n    return [*app.apptainer, 'exec', image, 'sh']\n"],
        "apptainer": ["class K:\n    def m(self):\n        return self.node.apptainer(argv, 1)\n"],
    }
    for kind, sources in cases.items():
        for source in sources:
            assert scan(source, "m.py")[kind], (kind, source)
    assert scan("class K:\n    def m(self):\n        build = lambda c: exec_argv(c)\n", "m.py")["exec_argv"] == {
        "m.py::K.m.<lambda>"
    }
    quiet = (
        '"""exec_argv builds an exec."""\n__all__ = ["exec_argv"]\n'
        "def exec_argv():\n    '''a docstring: exec'''\n    return 'exec the thing'\n"
    )
    assert not any(scan(quiet, "m.py").values())
    fed = scan("class K:\n    def m(self, x):\n        self._probe_argv(['cat', x])\n", "m.py")["feeds"]
    assert fed == {"m.py::K.m <- _probe_argv: ['cat', x]"}


# ------------------------------------------------------------------------------------------------ the structure
def test_o91_the_exec_builders_are_exactly_the_cell_the_probe_and_the_copy():
    """O91: every place that names `exec_argv` is `cell_argv` (the agent's code, with the read-log binds), the probe
    builder or the collection's copy builder (the harness's own code, without): a new builder fails here until it is
    classified, so none runs a caller's command unclassified."""
    assert scan_tree()["exec_argv"] == {CELL, PROBE, COPY}


def test_o91_no_other_place_spells_an_apptainer_exec_or_hands_the_node_an_argv():
    """O91: the word `exec` is spelled in the Apptainer package only by the primitive the three builders call, and the
    node's `apptainer(...)` is called from exactly the node's version read, the probes' caller, the copy host and the
    cell runner: a builder that skips `exec_argv` is not a way round the classification."""
    found = scan_tree()
    assert found["exec_word"] == {PRIMITIVE}
    assert found["apptainer"] == set(SPAWNS)


def test_o91_the_harness_builders_are_fed_literal_harness_commands_and_the_cell_runner_the_bootstrap():
    """O91: the probe builder takes its command from its caller, and the copy builder and the cell runner theirs: the
    only feeders are the trust-bundle reads (a certifi probe and a `cat` of a trust path), the copy host (the copy
    helper with an operation) and the cell runner (the completion bootstrap, the agent's code going on stdin)."""
    fed: dict[str, set[str]] = {}
    for line in scan_tree()["feeds"]:
        head, _, arguments = line.partition(": ")
        fed.setdefault(head, set()).add(arguments)
    assert fed == FEEDS


# ------------------------------------------------------------------------------------------------ the behaviour
MARK_OUTPUT = "zqmark-output.txt"  # a distinctive name the agent makes and the collection copies out
MARK_INPUT = "/zqmark-input"  # a distinctive target of a read-only input
MARK_CODE = "zqmark-code"  # in the agent's source


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, work_disk="image", readlog_collector_sha256=files.sha256_of(files.collector_file()))


def _answer(node: FakeNode):
    """The cells of this run as the node answers them: the harness's own programs as `default_cell` does, the input's
    read-only probe passing (every change refused with EROFS), and the agent's source writing its named output."""

    def handle(cell) -> CmdResult:
        if cell.program.startswith(PROBE_MARK):
            refused = dict.fromkeys(CHANGES, errno.EROFS)
            return CmdResult(0, json.dumps({"read": "done", **refused, "work": "done"}) + "\n")
        if MARK_CODE in cell.program:
            path = cell.work_dir() / MARK_OUTPUT
            path.write_text("42\n")
            return CmdResult(0, "")
        return node.default_cell(cell)

    return handle


@pytest.fixture
def box(node, settings, tmp_path):
    """A run with every kind of exec: via-gate (the trust-bundle probes), an image work disk (the copy helper), a
    read-only input, the channel on, an agent cell with its own source, and a collection of a named output."""
    source = tmp_path / "supplied"
    source.mkdir()
    (source / "a.py").write_text("x = 1\n", encoding="utf-8")
    spec = dataclasses.replace(make_spec("via-gate"), inputs=(ReadOnlyInput(source, MARK_INPUT),))
    node.cell_handler = _answer(node)
    box = start(node, settings, spec)
    try:
        box.exec(f"# {MARK_CODE}\nopen({MARK_OUTPUT!r}, 'w')", 30)
        box.collect(tmp_path / "out", [MARK_OUTPUT])
        yield box
    finally:
        box.stop()


def kind(done: Exec) -> str:
    """The kind of exec, as the harness builds it: a cell runs under `timeout`, the copy helper is the wrapper source
    run by python, and the rest are the start's one-shot reads of the image."""
    if done.command[:3] == ["timeout", "-s", "KILL"]:
        return "cell"
    if done.command[1:5] == ["-I", "-B", "-c", workdisk.wrapper_source()]:
        return "copy"
    return "probe"


def test_o91_every_exec_of_a_run_is_a_cell_a_probe_or_a_copy_and_the_run_has_all_three(node, box):
    """O91: nothing the fake node saw is another kind of exec, and each kind occurred, so the two tests below judge
    real execs and never an empty list."""
    assert {kind(done) for done in node.execs} == {"cell", "probe", "copy"}
    agent = [done for done in node.execs if kind(done) == "cell" and MARK_CODE in (done.stdin or "")]
    assert agent, "the agent's source runs in a cell"
    assert agent[0].binds[MARK_INPUT][1] is False  # and that cell has the input, so the probes' lack of it is a fact
    for done in node.execs:
        if kind(done) != "cell":
            assert MARK_CODE not in (done.stdin or ""), "the agent's source reached an exec that is not a cell"


def test_o91_the_probes_are_the_certifi_probe_and_a_cat_of_each_trust_path_with_the_work_bind_alone(
    node, box, settings
):
    """O91: the startup probes run exactly the harness's own commands (the image's python on the certifi probe, then
    `cat` of each trust target), with `/work` the only bind and an empty environment: no read-log bind, no input
    target, nothing of the agent's. Their commands are the whole of what they run, never a caller's."""
    probes = [done for done in node.execs if kind(done) == "probe"]
    wanted = [[settings.apptainer.python, "-c", CERTIFI_PROBE]]
    wanted += [["cat", target] for target in trust_targets(settings.trust_paths, CERTIFI)]
    assert [done.command for done in probes] == wanted
    for done in probes:
        assert set(done.binds) == {"/work"}, "a probe binds /work alone"
        assert done.env == {}
        assert not set(READLOG_TARGETS) & set(done.binds)
        assert not any(arg == "/work" or arg.startswith("/work/") for arg in done.command)
        assert MARK_INPUT not in " ".join(done.argv)
        assert MARK_OUTPUT not in " ".join(done.argv)
        assert MARK_CODE not in " ".join(done.argv)


def test_o91_the_copy_execs_are_the_copy_helper_with_the_work_and_staging_binds_alone(node, box):
    """O91: the collection's copy execs run exactly the copy helper (the image's python on the wrapper source, `list`
    or `copy`), with `/work` and the staging directory the only binds and the copy's own tag the only variable: no
    read-log bind, no input target; the output's name travels on stdin, never in the argv."""
    copies = [done for done in node.execs if kind(done) == "copy"]
    assert copies
    for done in copies:
        assert done.command[0] == box.app.python
        assert done.command[1:5] == ["-I", "-B", "-c", workdisk.wrapper_source()]
        assert done.command[5] in ("list", "copy")
        assert len(done.command) == 6
        assert set(done.binds) == {"/work", workdisk.EXPORT}
        assert done.env == workdisk.copy_env(box.sid)
        assert not set(READLOG_TARGETS) & set(done.binds)
        assert MARK_INPUT not in " ".join(done.argv)
        assert MARK_OUTPUT not in " ".join(done.argv)
    assert any(MARK_OUTPUT in (done.stdin or "") for done in copies), "the name was asked for, on stdin"


def test_o91_every_cell_of_the_run_and_the_cell_builder_carry_both_read_log_binds_read_only(node, box):
    """O91: with the channel on, every cell exec of this run (the canaries, the probes of the cell, the agent's) and
    `cell_argv` itself carry the socket directory and the client module read-only at their fixed targets, and only
    those two of the run's read-log directories. `test_readlog.py::test_o91_every_cell_binds_the_socket_directory_and_
    the_client_read_only` asserts the same for a directory work disk on via-gate and isolated; this is the image disk
    with a read-only input, and the builder called directly with a command of its own."""
    wanted = {
        paths.SOCKET_DIR: (str(box.dir / "readlog-sock"), False),
        paths.CLIENT_DIR: (str(box.dir / "readlog-client"), False),
    }
    cells = [done for done in node.execs if kind(done) == "cell"]
    assert len(cells) >= 3  # the egress canary, the read-log canary, the read-only probe, the agent's cell
    built = parse_exec(box.cell_argv(["true"]), None)
    for done in [*cells, built]:
        for target, bind in wanted.items():
            assert done.binds[target] == bind
            assert f"{bind[0]}:{target}:ro" in done.argv
        assert not any(Path(src) == box.dir / "readlog" for src, _ in done.binds.values())
