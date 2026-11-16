"""O56: the collection on the Apptainer backend: outputs, the gate's log and count, the egress log the instrument reads,
and the evidence scanned for the token; the run's start marked at the gate."""

import json
from pathlib import Path

import pytest
from apptainer_support import TOKEN, make_settings, make_spec, start
from fake_node import FakeNode

from expcore.egress import EgressLog
from expcore.errors import InfraError
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.hostops import CmdResult


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


def test_every_cell_binds_the_egress_socket_the_shim_and_the_preload_that_names_it(node, settings):
    """O56, #292: the collector's socket directory, the shim and `/etc/ld.so.preload` naming it are bound read-only
    into every cell, a harness step's included."""
    box = start(node, settings)
    try:
        box.exec("print('x')", 30)
        for done in node.execs:
            if done.command[0] != "timeout":
                continue
            assert done.binds[AV.EGRESS_DIR] == (str(box.dir / "egress-sock"), False)
            assert done.binds[AV.SHIM_TARGET][1] is False
            preload = done.binds["/etc/ld.so.preload"][0]
            assert Path(preload).read_text(encoding="utf-8").splitlines()[-1] == AV.SHIM_TARGET
    finally:
        box.stop()


def test_the_egress_log_opens_with_the_canary_and_reads_intact_after_the_collection(node, settings, tmp_path):
    """O56, #292: the collector starts first, the canary's connect is its first record, and once the collection has
    stopped it the instrument reads the log intact, the agent's own connects counted by class."""
    box = start(node, settings)
    try:
        node.cell_handler = lambda cell: (node.collector().connect("192.0.2.7", 443), CmdResult(0, ""))[1]
        box.mark_run_start()
        box.exec("import urllib.request; urllib.request.urlopen('https://example.invalid')", 30)
        assert box.egress_log() is None  # the collector runs: nothing read yet
        box.collect(tmp_path / "out")
        snapshot = EgressLog(box, box.egress_declaration(), box.egress_nonce).finish()
        assert snapshot["log"] == "intact"
        assert snapshot["canary"] is True
        assert snapshot["counts"] == {"gate": 0, "loopback": 0, "outside": 1}
        assert (tmp_path / "out" / "egress.jsonl").read_bytes() == box.egress_log()
    finally:
        box.stop()


def test_a_collector_that_ended_during_the_run_is_a_death_and_its_log_reads_unknown(node, settings, tmp_path):
    """O56: a collector that is gone before the collection is infrastructure, and its log has no end line."""
    box = start(node, settings)
    try:
        collector = node.collector().proc
        collector.alive, collector.exit_status = False, 137
        done = box.collect(tmp_path / "out")
        assert [d["role"] for d in done["deaths"]] == ["collector"]
        assert done["deaths"][0]["category"] == "infrastructure"
        snapshot = EgressLog(box, box.egress_declaration(), box.egress_nonce).finish()
        assert snapshot["log"] == "unknown"
    finally:
        box.stop()


def test_the_run_s_start_is_marked_at_the_gate_and_read_back(node, settings):
    """O56, O24: SIGUSR1 to the gate, through its pidfd, writes the mark, which the sandbox reads back."""
    box = start(node, settings)
    try:
        assert box.mark_run_start() == {"gate": True, "signalled": True, "confirmed": True}
        with pytest.raises(Exception, match="already marked"):
            box.mark_run_start()
    finally:
        box.stop()


def test_the_collection_copies_the_outputs_and_the_gate_log_and_counts_the_bytes_once(node, settings, tmp_path):
    """O56, O22: named outputs of /work are copied, the gate is stopped and its log copied, and the count is taken."""
    box = start(node, settings)
    try:
        (box.dir / "work" / "answer.txt").write_text("42\n")
        box.mark_run_start()
        done = box.collect(tmp_path / "out", ["answer.txt"])
        assert done["outputs"] == {"answer.txt": True}
        assert (tmp_path / "out" / "work" / "answer.txt").read_text() == "42\n"
        assert done["gate_log"] == "gate-requests.jsonl"
        assert box.metadata()["gate_stop"]["signalled"] is True
        assert "served_bytes" in json.loads((tmp_path / "out" / "metadata.json").read_text())
    finally:
        box.stop()


def test_a_file_of_evidence_that_holds_the_token_is_removed_and_fails_the_collection(node, settings, tmp_path):
    """O56, O05: a secret is never evidence: the gate's log holding the token is removed, and the collection fails."""
    box = start(node, settings)
    try:
        with open(box.dir / "gate" / "requests.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "leak", "value": TOKEN}) + "\n")
        with pytest.raises(InfraError) as failed:
            box.collect(tmp_path / "out")
        assert failed.value.kind == "output_collection"
        assert not (tmp_path / "out" / "gate-requests.jsonl").exists()
    finally:
        box.stop()


def test_a_run_with_no_data_path_has_no_gate_and_still_logs_its_egress(node, settings, tmp_path):
    """O56: no data path, no gate, no token; the collector and the canary all the same."""
    box = start(node, settings, make_spec("isolated"))
    try:
        assert not node.gates
        assert box.egress_declaration().gate == ()
        box.collect(tmp_path / "out")
        snapshot = EgressLog(box, box.egress_declaration(), box.egress_nonce).finish()
        assert snapshot["log"] == "intact"
        assert snapshot["canary"] is True
    finally:
        box.stop()
