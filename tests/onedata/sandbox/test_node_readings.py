"""O87: a Docker run records the OS release, glibc version and kernel release of its host, the machine that runs the
agent's container, as `metadata()["infrastructure_facts"]["node"]`: the keys, the shape and the `unknown` of an
Apptainer run's node (`tests/onedata/sandbox/apptainer/test_node_readings.py`), so a guard that compares the records
of two shards, one on each backend, reads one place. Recorded only, never refused."""

import pytest
from apptainer_support import make_settings as apptainer_settings
from apptainer_support import make_spec as apptainer_spec
from apptainer_support import start as apptainer_start
from fake_host import READINGS, FakeHost
from fake_node import FakeNode
from sandbox_support import make_spec, start_options

from expcore_onedata.sandbox import hostfacts
from expcore_onedata.sandbox.sandbox import DockerSandbox


def docker_run(world, **options):
    """A started Docker sandbox of the mount data path on the world's host."""
    return DockerSandbox.start(world.host, world.s, make_spec(world, "mounted"), **start_options(world), **options)


def stopped(world, box):
    world.host.unkillable.clear()
    box.stop()


def test_o87_a_docker_run_records_its_hosts_readings_in_infrastructure_facts(world):
    """O87: the host's `ID` and `VERSION_ID`, glibc and kernel, where an Apptainer run keeps its node's; the block holds
    nothing else, and the metadata's `host` (kernel, docker server) is as it was."""
    box = docker_run(world)
    try:
        meta = box.metadata()
        assert meta["infrastructure_facts"] == {"node": READINGS}
        assert meta["host"] == {"kernel": "test-kernel", "docker_server": "0.0"}
    finally:
        stopped(world, box)


def test_o87_a_host_whose_readings_cannot_be_made_is_unknown_and_the_run_starts_all_the_same(world):
    """O87: recorded only, never refused: a host whose reading raises is `unknown` with the exception's type."""
    world.host.readings_error = RuntimeError("a message that is never recorded")
    box = docker_run(world)
    try:
        found = box.metadata()["infrastructure_facts"]["node"]
        assert found == hostfacts.unreadable("RuntimeError")
        assert "never recorded" not in str(found)
    finally:
        stopped(world, box)


@pytest.mark.parametrize("answer", [None, "text", ["os"], {"os": 1}])
def test_o87_a_host_whose_readings_are_no_mapping_of_the_shape_is_unknown_never_refused(world, answer):
    world.host.readings = answer
    box = docker_run(world)
    try:
        assert box.metadata()["infrastructure_facts"]["node"]["kernel"] == hostfacts.UNKNOWN
    finally:
        stopped(world, box)


class OlderHost:
    """A host made before the readings: it answers everything a FakeHost does, and has no `host_readings`."""

    def __init__(self, host):
        self._host = host

    def __getattr__(self, name):
        if name == "host_readings":
            raise AttributeError(name)
        return getattr(self._host, name)


def test_o87_a_host_without_the_readings_method_is_unknown_and_the_start_goes_on(world):
    """O87: the lookup is guarded as the call is."""
    box = DockerSandbox.start(OlderHost(world.host), world.s, make_spec(world, "mounted"), **start_options(world))
    try:
        found = box.metadata()["infrastructure_facts"]["node"]
        assert set(found["unknown"].values()) == {"AttributeError"}
    finally:
        stopped(world, box)


def test_o87_the_docker_host_reads_this_machine_with_the_same_reader_as_the_apptainer_node():
    """O87: `DockerHost` is the driver's own machine, as its `host_facts` kernel is: the shared reader answers."""
    from expcore_onedata.sandbox.hostops import DockerHost

    found = DockerHost().host_readings()
    assert set(found) - {"unknown"} == {"os", "glibc", "kernel"}
    assert set(found["os"]) == {"id", "version_id"}


@pytest.mark.parametrize("readable", [True, False], ids=["read", "unknown"])
def test_o87_both_backends_carry_identical_keys_for_the_same_machine(world, tmp_path, readable):
    """O87: one machine read through each backend's own node or host object gives the same `node` record, key for key,
    whether it could be read or not, so a comparison across shards needs no case for the backend."""
    fake_node, fake_host = FakeNode(), world.host
    if not readable:
        fake_node.readings_error = fake_host.readings_error = OSError("never recorded")
    (tmp_path / "apptainer").mkdir()
    node_box = apptainer_start(fake_node, apptainer_settings(tmp_path / "apptainer"), apptainer_spec("via-gate"))
    host_box = docker_run(world)
    try:
        theirs = node_box.metadata()["infrastructure_facts"]["node"]
        ours = host_box.metadata()["infrastructure_facts"]["node"]
        assert set(ours) == set(theirs)
        assert ours["os"].keys() == theirs["os"].keys()
        assert ours == theirs  # FakeNode's and FakeHost's invented readings are one machine
    finally:
        node_box.stop()
        stopped(world, host_box)


def test_o87_the_fake_host_and_the_fake_node_invent_one_machine():
    """The two fakes' readings agree in shape, so the comparison above compares the backends and not the fakes."""
    from fake_node import READINGS as NODE

    assert NODE == READINGS
    assert FakeHost().host_readings() == FakeNode().node_readings()
