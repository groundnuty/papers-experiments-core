"""O89, O73: the name the agent's container was given (`cell_hostname`) is added when the metadata is read, through the
run's one writer of its infrastructure facts: to the copy `metadata()` returns, as the feature's own key. Every read
carries it, the run's own record never holds it, and no other writer may take the key. The Apptainer sandbox's, with
the scan of the agent node's names, are in `apptainer/test_cell_facts_one_writer.py`."""

import dataclasses

import pytest
from sandbox_support import make_spec, start_options

from expcore_onedata.sandbox import cellhost, infrafacts
from expcore_onedata.sandbox.sandbox import DockerSandbox


def _started(world):
    return DockerSandbox.start(
        world.host, world.s, dataclasses.replace(make_spec(world, "via-gate")), **start_options(world)
    )


def _stopped(world, box):
    world.host.unkillable.clear()
    box.stop()


def test_o89_every_read_carries_the_name_beside_the_other_facts_and_the_run_stores_none(world):
    """O89: two reads of the metadata both carry the name, with the node's readings and a fact the platform recorded
    before; the run's own table never holds it."""
    box = _started(world)
    try:
        box.record_infrastructure_fact("federation_window", {"verdict": "invented"}, owner=infrafacts.WINDOW)
        for _ in range(2):
            facts = box.metadata()["infrastructure_facts"]
            assert facts["cell_hostname"] == cellhost.CELL_HOSTNAME
            assert set(facts) == {"node", "cell_hostname", "federation_window"}
        assert set(box._meta["infrastructure_facts"]) == {"node", "federation_window"}
    finally:
        _stopped(world, box)


def test_o89_the_name_is_its_features_own_key_and_no_other_writer_takes_it(world):
    """O89: once the metadata has been read, `cell_hostname` is the feature's own: another owner's write is a conflict
    and writes nothing."""
    box = _started(world)
    try:
        box.metadata()
        before = dict(box._meta.get("infrastructure_facts", {}))
        with pytest.raises(infrafacts.FactsConflict, match="cell_hostname"):
            box.record_infrastructure_fact("cell_hostname", "another's", owner="another")
        assert box._meta.get("infrastructure_facts", {}) == before
    finally:
        _stopped(world, box)
