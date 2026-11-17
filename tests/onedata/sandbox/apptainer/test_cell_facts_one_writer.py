"""O89, O73: the two facts the cell host name feature adds when the metadata is read (`cell_hostname`, and
`own_node_scan` for a run with names to scan) go through the run's one writer of its infrastructure facts. They are
declared for the facts made at the read (`infrafacts.OWNED`) and added to the copy `metadata()` returns: every read
carries them, the run's own record holds neither, and no other writer may take either key, before the first read or
after it, for a run that scans names and for one that scans none. The Docker sandbox's are in
`../test_cell_facts_one_writer.py`."""

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
def test_o89_no_other_writer_takes_either_fact_before_the_first_read_or_after_it(box, key):
    """O89: the two keys are declared for the facts made at the read, so another owner's write is refused from the
    start (no read has claimed the key yet) and after a read, and writes nothing: a later `metadata()` never meets a
    key another writer stored."""
    before = dict(box._meta["infrastructure_facts"])
    with pytest.raises(infrafacts.FactsConflict, match=key):
        box.record_infrastructure_fact(key, "another's", owner="another")
    assert box._meta["infrastructure_facts"] == before
    assert box.metadata()["infrastructure_facts"][key]
    with pytest.raises(infrafacts.FactsConflict, match=key):
        box.record_infrastructure_fact(key, "another's", owner="another")
    assert box._meta["infrastructure_facts"] == before


def test_o89_a_run_that_scans_no_names_refuses_another_writer_the_scan_key_too(tmp_path):
    """O89: with no names to scan the run adds no `own_node_scan`, and the key is still declared: a stray write by
    another owner is refused, so it cannot be stored and then met by a read."""
    started = start(FakeNode(), make_settings(tmp_path))
    try:
        assert "own_node_scan" not in started.metadata()["infrastructure_facts"]
        with pytest.raises(infrafacts.FactsConflict, match="own_node_scan"):
            started.record_infrastructure_fact("own_node_scan", {"version": 1}, owner="another")
        assert "own_node_scan" not in started._meta["infrastructure_facts"]
    finally:
        started.stop()
