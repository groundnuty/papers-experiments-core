"""O89: every writer of a run's `infrastructure_facts` runs, in the order a run meets them, and not one key is lost.

On each backend a sandbox is made, the platform records the federation's window before the start, another feature
adds a fact of its own, the sandbox starts (the node's readings, and on Apptainer the work disk's proof), the run reads
its metadata, collects, has its window closed, and stops. At each step every key written so far has to be there with
the value its writer gave it: a key that an overwrite replaced is still a key, so the values are asserted, not only the
names. The three drops of 16 Nov 2026 (the work disk's proof dropped `provider_storage` and later `node` and the window,
and the Docker start dropped the window) are each a step of this sequence.
"""

import json

import pytest
from apptainer_support import make_settings as apptainer_settings
from apptainer_support import make_spec as apptainer_spec
from apptainer_support import options
from fake_host import READINGS as HOST_READINGS
from fake_node import READINGS as NODE_READINGS
from fake_node import FakeNode
from sandbox_support import make_spec, start_options

from expcore_onedata.sandbox import cellhost, fedwindow, infrafacts
from expcore_onedata.sandbox.api import DataPathMode, SandboxSpec
from expcore_onedata.sandbox.apptainer import facts as apptainer_facts
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox
from expcore_onedata.sandbox.infrafacts import FactsConflict
from expcore_onedata.sandbox.sandbox import DockerSandbox
from expcore_onedata.tokens import Secret

LITERAL = "infrastructure_facts"
NAME = {cellhost.FACT: cellhost.CELL_HOSTNAME}  # every read of a Docker run's metadata adds the cells' name (O73)
OTHER = "another_feature"
OTHER_KEY = "recorded_before_the_start_by_another_feature"
CLOSED = {**fedwindow.unread("the window closed after the run"), "verdict": "stable"}
OTHER_NODE = {"os": {"id": "other", "version_id": "7"}, "glibc": "glibc 2.17", "kernel": "3.10.0-other"}
GATE_LINES = [
    json.dumps({"method": "GET", "status": 200, "body_bytes": 10, "body": "other", "node_name_hits": 2}),
    json.dumps({"method": "GET", "status": 200, "body_bytes": 10, "body": "other", "node_name_hits": 1}),
    json.dumps(
        {
            "method": "GET",
            "status": 200,
            "body_bytes": 10,
            "body": "other",
            "upstream_conn": "new",
            "duration_ms": 40,
            "upstream_wait_ms": 40,
            "upstream_wait_conn": "new",
            "upstream_wait_end": "first_byte",
        }
    ),
]


def table(box):
    return box.metadata()[LITERAL]


# ------------------------------------------------------------------------------------------------------ Docker


def test_o89_a_docker_run_keeps_every_fact_every_writer_made_from_its_preparation_to_its_stop(world, tmp_path):
    """O89: the window is recorded before the start (O86), the start records the host's readings (O87, which once
    replaced the table and dropped the window), another feature's fact is there beside them, and the collection's file,
    the closed window and the stop each leave all three."""
    box = DockerSandbox(world.host, world.s, make_spec(world, "via-gate"), **start_options(world))
    box.record_infrastructure_fact(fedwindow.KEY, fedwindow.pending(), owner=infrafacts.WINDOW)
    box.record_infrastructure_fact(OTHER_KEY, {"kept": True}, owner=OTHER)
    assert table(box) == {fedwindow.KEY: fedwindow.pending(), OTHER_KEY: {"kept": True}, **NAME}  # before the start

    box.begin()
    started = {fedwindow.KEY: fedwindow.pending(), OTHER_KEY: {"kept": True}, "node": HOST_READINGS, **NAME}
    assert table(box) == started

    (box.dir / "gatelog" / "requests.jsonl").write_text("\n".join(GATE_LINES) + "\n", encoding="utf-8")
    box.collect(tmp_path / "evidence")
    written = json.loads((tmp_path / "evidence" / "metadata.json").read_text(encoding="utf-8"))
    assert written[LITERAL] == started  # the file `collect` writes holds what the run held then
    assert table(box) == started

    box.record_infrastructure_fact(fedwindow.KEY, CLOSED, owner=infrafacts.WINDOW)  # `launch`'s closing read
    ended = {**started, fedwindow.KEY: CLOSED}
    assert table(box) == ended
    box.stop()
    assert table(box) == ended
    assert box.metadata()["infrastructure_facts"] == ended


def test_o89_a_docker_run_without_a_window_or_another_fact_holds_the_node_alone(world):
    """O89: a record that no feature added to keeps what it had (O87's test of the host's readings says the same)."""
    box = DockerSandbox.start(world.host, world.s, make_spec(world, "via-gate"), **start_options(world))
    try:
        assert table(box) == {"node": HOST_READINGS, **NAME}
    finally:
        box.stop()


def test_o89_a_docker_writer_overwriting_another_s_key_is_refused_and_its_own_is_replaced(world):
    box = DockerSandbox(world.host, world.s, make_spec(world, "via-gate"), **start_options(world))
    box.record_infrastructure_fact(fedwindow.KEY, fedwindow.pending(), owner=infrafacts.WINDOW)
    with pytest.raises(FactsConflict, match=fedwindow.KEY):
        box.record_infrastructure_fact(fedwindow.KEY, {"verdict": "stable"}, owner=OTHER)
    box.begin()
    try:
        with pytest.raises(FactsConflict, match="node"):
            box.record_infrastructure_fact("node", {"kernel": "mine"}, owner=OTHER)
        box.record_infrastructure_fact(fedwindow.KEY, CLOSED, owner=infrafacts.WINDOW)  # its own, again
        assert table(box) == {fedwindow.KEY: CLOSED, "node": HOST_READINGS, **NAME}
    finally:
        box.stop()


def test_o89_a_record_cannot_replace_the_facts_of_a_docker_run(world):
    box = DockerSandbox(world.host, world.s, make_spec(world, "via-gate"), **start_options(world))
    box.record_infrastructure_fact(fedwindow.KEY, fedwindow.pending(), owner=infrafacts.WINDOW)
    with pytest.raises(FactsConflict, match="whole"):
        box.record(**{LITERAL: {}})
    assert table(box) == {fedwindow.KEY: fedwindow.pending(), **NAME}
    box.record(lease={"slot": 0})  # any other field of the metadata is recorded as ever
    assert box.metadata()["lease"] == {"slot": 0}


# ------------------------------------------------------------------------------------------------------ Apptainer


def rest_spec() -> SandboxSpec:
    """A REST run that has a list of node names, so the gate's two scans are in its facts (O73)."""
    base = apptainer_spec("via-gate")
    return SandboxSpec(
        run_key=base.run_key, mode=DataPathMode.REST, token=Secret("tok-0123456789"), zone=base.zone,
        provider=base.provider, node_names=("cn-r3c07-n12.cluster.example.org", "198.51.100.21"),
    )  # fmt: skip


def departures(work_disk: str):
    """What `facts.record` makes for a work disk's state, as a run of this sandbox (no mount helper) records it."""
    return apptainer_facts.record(work_disk, mount_refused=True, provider_storage="shared_filesystem")


@pytest.mark.parametrize(("work_disk", "proofs"), [("directory", (None,)), ("image", ("full_fill", "invariants"))])
def test_o89_an_apptainer_run_keeps_every_fact_every_writer_made_from_its_preparation_to_its_stop(
    tmp_path, work_disk, proofs
):
    """O89: the sandbox's own record (the departures, `provider_storage`), the window recorded before the start, another
    feature's fact, the node's readings and the tool server's host (O87), the work disk's proof (O71: a full fill on the
    node's first start, the invariants on the next), the readings `metadata()` makes (limits, memory, and the gate's two
    scans) and the stop: every key written so far is there after each step, with its value. The proof replaced
    `provider_storage`, `node` and the window before the helper."""
    node = FakeNode()
    node.tool_server_host = OTHER_NODE
    settings = apptainer_settings(tmp_path, work_disk=work_disk, provider_storage="shared_filesystem")
    for proof in proofs:
        box = ApptainerSandbox(node, settings, rest_spec(), **options())
        state = "image_unverified" if work_disk == "image" else "directory"
        assert table(box)["facts"] == departures(state)["facts"]  # the sandbox's own record, before anything is added
        box.record_infrastructure_fact(fedwindow.KEY, fedwindow.pending(), owner=infrafacts.WINDOW)
        box.record_infrastructure_fact(OTHER_KEY, {"kept": True}, owner=OTHER)
        box.begin()
        try:
            proved = "image" if work_disk == "image" else "directory"
            if proof is not None:
                assert box.metadata()["work_disk"]["proof"]["kind"] == proof
            box.exec("print(1)", 30)  # the watch has a memory reading from the first cell on
            with box.tool_server_process():
                pass
            mid = table(box)
            assert {k: mid[k] for k in departures(proved)} == departures(proved)  # incl. provider_storage
            assert mid["node"] == NODE_READINGS
            assert mid["tool_server_host"] == OTHER_NODE
            assert mid[fedwindow.KEY] == fedwindow.pending()
            assert mid[OTHER_KEY] == {"kept": True}
            assert mid["memory"]["reading"] == "within"
            assert mid["limits"]["nofile"]["reading"] == "set"
            assert mid[cellhost.FACT] == cellhost.CELL_HOSTNAME  # the facts the cell host name adds at the read (O73)
            assert mid["own_node_scan"]["version"] == 1
            assert mid["rest_node_name_hits"]["unknown"] == [gatelog_not_collected()]
            assert mid["rest_request_durations"]["unknown"] == [gatelog_not_collected()]

            with open(box.dir / "gate" / "requests.jsonl", "a", encoding="utf-8") as log:
                log.write("\n".join(GATE_LINES) + "\n")
            box.collect(tmp_path / f"evidence-{proof}")
            box.record_infrastructure_fact(fedwindow.KEY, CLOSED, owner=infrafacts.WINDOW)
            end = table(box)
            assert end["rest_node_name_hits"] == {"version": 1, "rest_answers": 3, "unknown": []}
            assert end["rest_request_durations"]["requests"] == 1
            assert end[fedwindow.KEY] == CLOSED
            assert {k: v for k, v in end.items() if k not in ("rest_node_name_hits", "rest_request_durations")} == {
                **{k: v for k, v in mid.items() if k not in ("rest_node_name_hits", "rest_request_durations")},
                fedwindow.KEY: CLOSED,
            }
        finally:
            box.stop()
        assert table(box) == {**end, **{k: table(box)[k] for k in ("memory", "limits")}}  # the same table at the end
        for key in (*departures(proved), "node", "tool_server_host", fedwindow.KEY, OTHER_KEY, "provider_storage"):
            assert key in table(box), key


def gatelog_not_collected() -> str:
    from expcore_onedata.sandbox import gatelog

    return gatelog.GATE_NOT_COLLECTED


def test_o89_an_apptainer_writer_overwriting_another_s_key_is_refused_and_its_own_is_replaced(tmp_path):
    settings = apptainer_settings(tmp_path, work_disk="image", provider_storage="tmpfs")
    box = ApptainerSandbox(FakeNode(), settings, rest_spec(), **options())
    with pytest.raises(FactsConflict, match="provider_storage"):
        box.record_infrastructure_fact("provider_storage", "shared_filesystem", owner=OTHER)
    with pytest.raises(FactsConflict, match="limits"):
        box.record_infrastructure_fact("limits", {}, owner=OTHER)  # read when the metadata is, never stored
    box.begin()
    try:
        with pytest.raises(FactsConflict, match="node"):
            box.record_infrastructure_fact("node", {"kernel": "mine"}, owner=infrafacts.WINDOW)
        assert table(box)["provider_storage"] == "tmpfs"
        assert table(box)["node"] == NODE_READINGS
        box.record_infrastructure_fact(OTHER_KEY, 1, owner=OTHER)
        box.record_infrastructure_fact(OTHER_KEY, 2, owner=OTHER)  # its own, again
        assert table(box)[OTHER_KEY] == 2
    finally:
        box.stop()


def test_o89_a_record_cannot_replace_the_facts_of_an_apptainer_run(tmp_path):
    box = ApptainerSandbox(FakeNode(), apptainer_settings(tmp_path), rest_spec(), **options())
    before = table(box)
    with pytest.raises(FactsConflict, match="whole"):
        box.record(**{LITERAL: {}})
    assert table(box) == before
    box.record(lease={"slot": 0})
    assert box.metadata()["lease"] == {"slot": 0}
