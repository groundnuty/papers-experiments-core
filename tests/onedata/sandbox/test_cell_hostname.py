"""O73: every agent cell has one fixed, neutral host name, the same string on both backends, so `hostname` and
`uname -n` show a node's name nowhere. These are the Docker sandbox's and the name's own; the Apptainer
backend's are in `apptainer/test_cell_hostname.py`."""

import dataclasses
from pathlib import Path

import pytest
from sandbox_support import make_spec, start_options

from expcore_onedata.sandbox import cellhost
from expcore_onedata.sandbox import containers as C
from expcore_onedata.sandbox.api import DataPathMode, StartRefused
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox

LEVELS = ["mounted", "via-gate", "isolated"]  # the three data paths
LABELS = ["--label", "org.test.sandbox=1"]


def started(world, level="via-gate", **changes):
    spec = dataclasses.replace(make_spec(world, level), **changes)
    return DockerSandbox.start(world.host, world.s, spec, **start_options(world))


def stopped(world, box):
    world.host.unkillable.clear()
    box.stop()


def test_o73_the_declared_host_name_is_sandbox_and_a_valid_one():
    """O73: the default is `sandbox`: one label that is a legal host name, and no node's."""
    assert cellhost.CELL_HOSTNAME == "sandbox"
    assert cellhost.valid(cellhost.CELL_HOSTNAME)
    assert not any(cellhost.valid(name) for name in ("", "a b", "-x", "x-", "x" * 64, "a.b", "not_ok"))


@pytest.mark.parametrize("mode", [DataPathMode.REST, DataPathMode.NONE])
def test_o73_the_agent_s_docker_arguments_carry_the_declared_host_name(world, mode):
    """O73: `docker run` of the agent has `--hostname <name>`, on a data path with a gate and on one with none."""
    names = C.Names.make(world.s.name_prefix, "k1-0123abcd")
    rest = mode is DataPathMode.REST
    args, _ = C.agent_run_args(
        world.s, names, LABELS, image_id=world.s.agent.id, mode=mode, work=Path("/w/work"), share=None,
        gate_ip="10.1.0.2" if rest else None, gate_hosts=["zone.test"] if rest else [],
        ca=Path("/w/ca.pem") if rest else None, trust_bundles={}, zone_host="zone.test" if rest else None,
    )  # fmt: skip
    at = args.index("--hostname")
    assert args[at + 1] == cellhost.CELL_HOSTNAME
    assert at + 1 < args.index(world.s.agent.id)  # an option of docker's, not a word of the agent's command


@pytest.mark.parametrize("level", LEVELS)
def test_o73_the_agent_s_container_is_created_with_the_declared_host_name(world, level):
    """O73: the fake host's container, as the sandbox created it, was given the name on every data path."""
    box = started(world, level)
    try:
        assert world.host.containers[box.names.agent].options["--hostname"] == cellhost.CELL_HOSTNAME
    finally:
        stopped(world, box)


def test_o73_only_the_agent_s_container_is_renamed(world):
    """O73: the gate and the sidecar are the harness's, not the agent's: they keep the engine's own names."""
    box = started(world, "mounted")
    try:
        others = [c for name, c in world.host.containers.items() if name != box.names.agent]
        assert others
        assert all("--hostname" not in c.options for c in others)
    finally:
        stopped(world, box)


def test_o73_a_cell_on_docker_reads_the_declared_name_as_its_host_name(world):
    """O73: what `hostname` prints in the fake host's cell is the name its container was given, not the engine's short
    container id, which would differ on every run. One data path: the argument is the same for each."""
    box = started(world, "isolated")
    try:
        world.host.exec_handler = lambda container, argv, stdin: CmdResult(0, container.hostname() + "\n")
        done = box.exec("import socket; print(socket.gethostname())", 30)
        assert done.stdout.strip() == cellhost.CELL_HOSTNAME
    finally:
        stopped(world, box)


def test_o73_the_run_s_record_carries_the_name_its_container_was_given_beside_the_node(world):
    """O73: `infrastructure_facts.cell_hostname` is the name the container was created with, and the block keeps
    what was in it: the node's readings, and a fact the platform recorded before or after."""
    box = started(world, "via-gate")
    try:
        box.record_infrastructure_fact("federation_window", {"verdict": "invented"})
        facts = box.metadata()["infrastructure_facts"]
        assert facts["cell_hostname"] == world.host.containers[box.names.agent].options["--hostname"]
        assert set(facts) == {"node", "cell_hostname", "federation_window"}
    finally:
        stopped(world, box)


def test_o73_the_same_string_is_given_to_both_backends():
    """O73: Docker's `--hostname` and Apptainer's `--uts --hostname` carry one declared constant."""
    exec_argv = AV.exec_argv(["apptainer"], Path("/i.sif"), [], {}, ["true"])
    assert exec_argv[exec_argv.index("--hostname") + 1] == cellhost.CELL_HOSTNAME


@pytest.mark.parametrize(
    "node_name",
    ["sandbox", "SANDBOX", "sandbox.cluster.example.invalid", "Sandbox.Cluster.Example.Invalid"],
)
def test_o73_a_node_named_as_the_cell_is_refused_before_anything_is_created(world, node_name):
    """O73: the declared name must never be a node name (the gate's scan counts the node names an answer holds, and a
    cell's own host name would be one): a federation with a node of that name, or whose FQDN starts with it, refuses
    the start, and the refusal names no node."""
    with pytest.raises(StartRefused, match="host name") as refused:
        started(world, "via-gate", node_names=("198.51.100.7", node_name))
    assert node_name not in str(refused.value)
    assert "198.51.100.7" not in str(refused.value)
    assert not world.host.containers
    assert not world.host.networks
    assert not world.host.mounts


@pytest.mark.parametrize("node_name", ["sandbox-7.example.invalid", "cn-sandbox", "198.51.100.7", "xsandbox"])
def test_o73_a_node_that_only_contains_the_name_is_no_clash(world, node_name):
    """O73: only a node name that is the declared name, or whose first label is, clashes with it."""
    box = started(world, "via-gate", node_names=(node_name,))
    stopped(world, box)
