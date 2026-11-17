"""O73: every Apptainer cell has the declared host name, the same string as Docker's. A cell of this backend has no
network namespace and, without `--uts`, no UTS namespace either: `hostname` and `uname -n` would show
the node's own name, a private detail of the site. `--uts --hostname <name>` is in every exec's argv. The Docker
sandbox's are in `../test_cell_hostname.py`."""

import dataclasses
from pathlib import Path

import pytest
from apptainer_support import make_settings, make_spec, start
from fake_node import FakeNode, parse_exec

from expcore_onedata.sandbox import cellhost, infrafacts
from expcore_onedata.sandbox.api import StartRefused
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.hostops import CmdResult


@pytest.fixture
def node():
    return FakeNode()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


def test_o73_the_exec_argv_gives_the_cell_a_uts_namespace_and_the_declared_name():
    """O73: `--uts --hostname <name>` stands among apptainer's own options, before the binds and the image."""
    argv = AV.exec_argv(["apptainer"], Path("/i.sif"), [AV.Bind(Path("/r/work"), "/work", True)], {"A": "b"}, ["true"])
    at = argv.index("--uts")
    assert argv[at + 1 : at + 3] == ["--hostname", cellhost.CELL_HOSTNAME]
    assert at < argv.index("-B")
    assert at < argv.index("--env")
    assert at < argv.index("/i.sif")


def test_o73_the_exec_argv_names_the_hostname_once():
    """O73: one namespace, one name: the flags are not repeated, and a name is no word of the command."""
    argv = AV.exec_argv(["apptainer"], Path("/i.sif"), [], {}, ["true"])
    assert argv.count("--uts") == argv.count("--hostname") == 1


def test_o73_the_fake_node_refuses_an_exec_without_the_flags():
    """O73: the harness of these tests fails an exec path of the backend that lacks `--uts --hostname`."""
    argv = AV.exec_argv(["apptainer"], Path("/i.sif"), [], {}, ["true"])
    parse_exec(argv, None)
    at = argv.index("--uts")
    with pytest.raises(AssertionError, match="UTS flags"):
        parse_exec(argv[:at] + argv[at + 3 :], None)


def test_o73_every_exec_a_run_makes_carries_the_declared_name(node, settings):
    """O73: the probes of the image, the canary, the cells of the agent and the harness's own: each exec the node saw
    was given the name, so no exec path shows the node's."""
    box = start(node, settings)
    try:
        box.exec("print('x')", 30)
        box.exec("print('y')", 30)
        assert len(node.execs) >= 4
        assert {done.hostname for done in node.execs} == {cellhost.CELL_HOSTNAME}
    finally:
        box.stop()


@pytest.mark.parametrize("level", ["via-gate", "isolated"])
def test_o73_a_cell_reads_the_declared_name_as_its_host_name(node, settings, level):
    """O73: what `hostname` and `uname -n` print in the fake node's cell is the name set in its UTS namespace, with and
    without a gate; an exec without the flags would not parse."""
    box = start(node, settings, make_spec(level))
    try:
        node.cell_handler = lambda cell: CmdResult(0, cell.hostname() + "\n")  # after the start's canary
        done = box.exec("import socket; print(socket.gethostname())", 30)
        assert done.stdout.strip() == cellhost.CELL_HOSTNAME
    finally:
        box.stop()


def test_o73_no_exec_binds_a_file_of_the_sandbox_s_own_onto_etc_hostname(node, settings):
    """O73: `/etc/hostname` is left to Apptainer, which writes its own from `--uts --hostname` and never binds the
    node's: a bind of ours would be a second mount on the same path, and fatal where the image had none to bind over."""
    box = start(node, settings)
    try:
        box.exec("print('x')", 30)
        assert node.execs
        assert all("/etc/hostname" not in done.binds for done in node.execs)
    finally:
        box.stop()


def test_o73_the_run_s_record_carries_the_name_its_execs_were_given(node, settings):
    """O73: `infrastructure_facts.cell_hostname` is the name of the flags the execs carried, beside the departures'
    keys and the node's readings, never a reading from inside the cell."""
    box = start(node, settings)
    try:
        box.exec("print('x')", 30)
        box.record_infrastructure_fact("federation_window", {"verdict": "invented"}, owner=infrafacts.WINDOW)
        facts = box.metadata()["infrastructure_facts"]
        assert facts["cell_hostname"] == cellhost.CELL_HOSTNAME
        assert {done.hostname for done in node.execs} == {facts["cell_hostname"]}
        assert {"backend", "facts", "dns", "node", "federation_window"} <= set(facts)
    finally:
        box.stop()


def test_o73_the_record_keeps_the_name_when_the_facts_are_replaced_by_a_later_record(node, settings):
    """O73: the name is added when the metadata is read, so a record of the facts made again later (the work disk's
    proof makes one) cannot drop it."""
    box = start(node, settings)
    try:
        box._meta["infrastructure_facts"] = {"backend": "apptainer", "facts": [], "dns": "none"}
        assert box.metadata()["infrastructure_facts"]["cell_hostname"] == cellhost.CELL_HOSTNAME
    finally:
        box.stop()


@pytest.mark.parametrize("node_name", ["sandbox", "Sandbox", "sandbox.cluster.example.invalid"])
def test_o73_a_node_named_as_the_cell_is_refused_before_any_exec(node, settings, node_name):
    """O73: the declared name must never be a node name: a start with a node of that name, or an FQDN whose first label
    is it, is refused before anything ran, and the refusal names no node."""
    spec = dataclasses.replace(make_spec("via-gate"), node_names=("198.51.100.7", node_name))
    with pytest.raises(StartRefused, match="host name") as refused:
        start(node, settings, spec)
    assert node_name not in str(refused.value)
    assert "198.51.100.7" not in str(refused.value)
    assert node.execs == []


def test_o73_a_node_that_only_contains_the_name_starts(node, settings):
    """O73: a node name that merely holds the declared name is no clash."""
    spec = dataclasses.replace(make_spec("via-gate"), node_names=("sandbox-7.example.invalid", "xsandbox"))
    start(node, settings, spec).stop()
