"""O89, O73: the two facts the cell host name feature adds when the metadata is read (`cell_hostname`, and
`own_node_scan` for a run with names to scan) go through the run's one writer of its infrastructure facts. They are
added to the copy `metadata()` returns, as the feature's own keys: every read carries them, the run's own record holds
neither, and no other writer may take either key. The Docker sandbox's are in `../test_cell_facts_one_writer.py`."""

import pytest
from apptainer_support import make_settings, make_spec, start
from fake_node import FakeNode

from expcore_onedata.sandbox import cellhost, infrafacts
from expcore_onedata.sandbox.api import DataPathMode, SandboxSpec
from expcore_onedata.tokens import Secret

SERVICES = ("cn-r3c07-n12.cluster.example.org", "198.51.100.21")


def _spec() -> SandboxSpec:
    base = make_spec("via-gate")
    return SandboxSpec(
        run_key=base.run_key, mode=DataPathMode.REST, token=Secret("tok-0123456789"), zone=base.zone,
        provider=base.provider, node_names=SERVICES,
    )  # fmt: skip


@pytest.fixture
def box(tmp_path):
    started = start(FakeNode(), make_settings(tmp_path), _spec())
    try:
        yield started
    finally:
        started.stop()


def test_o89_every_read_of_the_metadata_carries_both_facts_and_the_run_stores_neither(box):
    """O89: two reads of the metadata both carry the name and the scan, beside the sandbox's own keys and the node's,
    and the run's own table holds neither, so a later write to it cannot have dropped them."""
    box.record_infrastructure_fact("federation_window", {"verdict": "invented"}, owner=infrafacts.WINDOW)
    for _ in range(2):
        facts = box.metadata()["infrastructure_facts"]
        assert facts["cell_hostname"] == cellhost.CELL_HOSTNAME
        assert facts["own_node_scan"]["version"] == 1
        assert {"backend", "facts", "dns", "node", "federation_window", "limits"} <= set(facts)
    stored = box._meta["infrastructure_facts"]
    assert "cell_hostname" not in stored
    assert "own_node_scan" not in stored
    assert {"backend", "facts", "dns", "node", "federation_window"} <= set(stored)


@pytest.mark.parametrize("key", ["cell_hostname", "own_node_scan"])
def test_o89_the_two_facts_are_their_features_own_keys_and_no_other_writer_takes_one(box, key):
    """O89: once the metadata has been read, the key is the feature's own: another owner's write to it is a conflict
    (a bug of that writer), and writes nothing."""
    box.metadata()
    before = dict(box._meta["infrastructure_facts"])
    with pytest.raises(infrafacts.FactsConflict, match=key):
        box.record_infrastructure_fact(key, "another's", owner="another")
    assert box._meta["infrastructure_facts"] == before
