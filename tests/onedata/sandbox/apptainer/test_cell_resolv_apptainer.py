"""O56: an Apptainer cell reads exactly the shared neutral /etc/resolv.conf, and the run records the hash a cell read.

The shared constant and the Docker side are in `../test_cell_resolv.py`. Here the fake node parses each `apptainer exec`
back into its binds, and a cell that reads the path gets the text of the file bound there, or the node's own file (with
its search domain and a name server) where nothing is bound or `node_resolv_shows` stands for a bind that did not take.
"""

import hashlib
from pathlib import Path

import pytest
from apptainer_support import make_settings, make_spec, start
from fake_node import NODE_RESOLV, FakeNode

from expcore_onedata.sandbox import cellresolv
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.apptainer import runs
from expcore_onedata.sandbox.hostops import CmdResult

READ = "print(open('/etc/resolv.conf').read(), end='')"
SHA = hashlib.sha256(cellresolv.RESOLV_TEXT.encode()).hexdigest()
LEVELS = ["via-gate", "isolated"]


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


def nothing_left(node, settings) -> bool:
    """No process of the run but the driver's, no directory of the work root but the records', and no record."""
    return (
        not [p for p in node.procs.values() if p.alive and p.role != "driver"]
        and not [p for p in settings.work_root.iterdir() if p.name != runs.RUNS]
        and not list((settings.work_root / runs.RUNS).iterdir())
    )


def reading_cells(node: FakeNode) -> None:
    """A cell that runs `READ` gets what the exec's binds hold at the path; any other program is the fake's own."""
    default = node.cell_handler
    node.cell_handler = lambda cell: (
        CmdResult(0, node.read_in(cell, "/etc/resolv.conf") or "") if cell.program == READ else default(cell)
    )


@pytest.mark.parametrize("level", LEVELS)
def test_o56_a_cell_of_the_apptainer_backend_reads_exactly_the_shared_text(settings, level):
    """O56: the file bound read-only onto /etc/resolv.conf is the shared text: not the node's own, with its search
    domain and name server."""
    node = FakeNode()
    reading_cells(node)
    box = start(node, settings, make_spec(level))
    try:
        read = box.exec(READ, 30).stdout
        source, writable = node.execs[-1].binds["/etc/resolv.conf"]
        assert not writable
        assert Path(source).read_text(encoding="utf-8") == cellresolv.RESOLV_TEXT
        assert read == cellresolv.RESOLV_TEXT
        assert read != NODE_RESOLV
    finally:
        box.stop()


@pytest.mark.parametrize("level", LEVELS)
def test_o56_an_apptainer_run_records_the_hash_a_cell_read(settings, level):
    """O56: the same key and value as a Docker run's, read from inside a cell at the start, beside the departures and
    `dns: none`; no text of the file is in the record."""
    box = start(FakeNode(), settings, make_spec(level))
    try:
        facts = box.metadata()["infrastructure_facts"]
        assert facts[cellresolv.FACT] == SHA
        assert facts["dns"] == "none"
        assert cellresolv.RESOLV_TEXT not in str(box.metadata())
    finally:
        box.stop()


def test_o56_a_node_whose_own_file_shows_in_a_cell_refuses_the_start_and_leaves_nothing_behind(settings):
    """O56: fail closed. A bind that did not take, so a cell reads the node's own file, ends the start; the message
    holds none of the file."""
    node = FakeNode()
    node.node_resolv_shows = True
    with pytest.raises(SandboxError) as raised:
        start(node, settings, make_spec("via-gate"))
    assert str(raised.value) == cellresolv.REFUSAL
    assert "node-search" not in str(raised.value)
    assert nothing_left(node, settings)


def test_o56_a_probe_that_cannot_run_also_refuses_the_start(settings):
    """O56: no answer is taken for the file: a cell that fails reads nothing."""
    node = FakeNode()
    default = node.cell_handler
    node.cell_handler = lambda cell: (
        CmdResult(1, "", "no python") if cell.program == cellresolv.PROBE else default(cell)
    )
    with pytest.raises(SandboxError, match="could not read"):
        start(node, settings, make_spec("via-gate"))
    assert nothing_left(node, settings)
