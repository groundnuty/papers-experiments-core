"""O56: the collection on the Apptainer backend: outputs, the gate's log and count, the egress log the instrument reads,
and the evidence scanned for the token; the run's start marked at the gate."""

import json
import stat
from pathlib import Path

import pytest
from apptainer_support import TOKEN, make_settings, make_spec, start
from fake_node import FakeNode
from locked_work import lock, locked_afterwards, needs_unprivileged, not_the_owner

from expcore.egress import EgressLog
from expcore.errors import InfraError
from expcore_onedata.sandbox import copyout
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


@pytest.fixture
def unlocked_afterwards(tmp_path):
    yield from locked_afterwards(lambda: tmp_path)


def _locked_work(box, *, root: bool = False) -> None:
    """What an agent can do in /work (a directory of the node's here): leave a file and a directory (with a file in it)
    with every bit cleared, and, with `root`, clear the bits of /work itself."""
    work = box.dir / "work"
    (work / "out" / "locked").mkdir(parents=True)
    (work / "out" / "ok.txt").write_text("ok\n")
    (work / "out" / "secret.txt").write_text("secret\n")
    (work / "out" / "locked" / "inner.txt").write_text("inner\n")
    lock(work / "out" / "secret.txt")
    lock(work / "out" / "locked")
    if root:
        lock(work)


@needs_unprivileged
@pytest.mark.parametrize("root", [False, True])
def test_o71_a_mode_000_file_and_directory_the_agent_locked_are_collected_never_set_aside(
    node, settings, tmp_path, unlocked_afterwards, root
):
    """O71: with `work_disk = "directory"` the copy helper runs as the agent's own uid too: an entry the agent locked
    (`chmod 000` of a file, of a directory, of /work itself) is collected with what it holds, never an infrastructure
    failure set aside instead of scored."""
    box = start(node, settings)
    try:
        _locked_work(box, root=root)
        done = box.collect(tmp_path / "out", ["out"])  # no InfraError
        assert done["outputs"] == {"out": True}
        assert "refused_output" not in done
        got = tmp_path / "out" / "work" / "out"
        assert (got / "secret.txt").read_text() == "secret\n"
        assert (got / "locked" / "inner.txt").read_text() == "inner\n"
        assert (got / "ok.txt").read_text() == "ok\n"
    finally:
        box.stop()


@needs_unprivileged
def test_o71_an_entry_the_helper_cannot_read_is_the_agents_refusal_never_a_set_aside_in_directory_mode(
    node, settings, tmp_path, monkeypatch, unlocked_afterwards
):
    """O71, O19: where the owner's bits cannot be given back, the entry is named for the scorers (`refused_output`),
    none of the outputs is copied, and the collection ends normally."""
    box = start(node, settings)
    try:
        _locked_work(box)
        source = copyout._helper_source()
        monkeypatch.setattr(copyout, "_helper_source", lambda: not_the_owner(source))
        done = box.collect(tmp_path / "out", ["out"])  # no InfraError
        assert done["outputs"] == {"out": False}
        assert done["refused_output"]["by_what"] == {copyout.UNREADABLE: 2}
        assert not (tmp_path / "out" / "work").exists()
    finally:
        box.stop()


@needs_unprivileged
def test_o71_a_gate_log_the_helper_cannot_read_is_infrastructure_and_is_never_restored(
    node, settings, tmp_path, unlocked_afterwards
):
    """O71: the gate's own log is the harness's, not the agent's: where it cannot be read the collection is an
    infrastructure failure, set aside as before, and its mode is not given back (the helper's restore is only for
    the agent's /work)."""
    box = start(node, settings)
    try:
        log = box.dir / "gate" / "requests.jsonl"
        lock(log)
        with pytest.raises(InfraError) as failed:
            box.collect(tmp_path / "out")
        assert failed.value.kind == "output_collection"
        assert stat.S_IMODE(log.lstat().st_mode) == 0
    finally:
        log.chmod(0o600)  # the teardown reads the gate's log
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


def test_o05_an_egress_log_that_holds_the_token_is_never_copied_or_handed_on_even_when_the_scan_never_runs(
    node, settings, tmp_path, monkeypatch
):
    """O05: the egress log is scanned as it is read, before its copy is written and before its bytes are handed on, so
    a collection that fails between the two (here, before the scan) leaves no unscanned copy on disk and no bytes."""
    box = start(node, settings)
    try:
        with open(box.dir / "egress" / "egress.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "leak", "value": TOKEN}) + "\n")

        def interrupted(paths):
            raise RuntimeError("interrupted before the scan")

        monkeypatch.setattr(box.collection, "_scan", interrupted)
        with pytest.raises(RuntimeError, match="before the scan"):
            box.collect(tmp_path / "out")
        assert not (tmp_path / "out" / "egress.jsonl").exists()
        assert box.egress_log() is None
        monkeypatch.undo()  # the scan, run again, still fails the collection for the log that holds the token
        with pytest.raises(InfraError) as failed:
            box.collect(tmp_path / "again")
        assert failed.value.kind == "output_collection"
        assert not (tmp_path / "again" / "egress.jsonl").exists()
        assert box.egress_log() is None
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


def test_a_kept_run_never_keeps_a_file_that_holds_the_token(node, settings, tmp_path):
    """O56, O05: a run kept as evidence after a failed collection has every file holding the token removed first."""
    box = start(node, settings)
    with open(box.dir / "gate" / "requests.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "leak", "value": TOKEN}) + "\n")
    with pytest.raises(InfraError):
        box.collect(tmp_path / "out")
    box.stop()
    kept = settings.work_root / "_kept_evidence" / box.sid
    assert kept.is_dir()
    assert not (kept / "gate" / "requests.jsonl").exists()
    for path in kept.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path
    assert box.metadata()["evidence_kept"]["files_holding_the_token_removed"] == 1
