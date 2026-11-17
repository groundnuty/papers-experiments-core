"""O91: the read-log channel on the Apptainer backend, on the fake node (the collector emulated by what it does).

A study's canonical scripts log every read they make, from inside a cell, to a collector the harness runs outside it:
a helper of the driver's uid, started before any cell, whose socket directory and the client module are bound
READ-ONLY at the channel's fixed targets in every cell. At the collection the collector is stopped (so it drains and
writes its end line), its log read once, copied and scanned for the token, and exposed as `read_log()`. A collector that
was killed, or a log that cannot be read, is an UNKNOWN log, never a run that read nothing. The real collector process
is `test_readlog_real.py`'s.
"""

import dataclasses
import json
import os
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest
from apptainer_support import TOKEN, make_settings, make_spec, start
from fake_node import FakeNode

from expcore.errors import InfraError
from expcore.replay.side_file_placement import stage_harness_side_file
from expcore.replay.side_files import SideFile
from expcore_onedata.readlog import facts, files, paths
from expcore_onedata.readlog.instrument import ReadLog
from expcore_onedata.sandbox.api import ReadOnlyInput, SandboxError, StartRefused
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.hostops import CmdResult

LOG = "readlog.jsonl"  # the collector's own log, in `readlog/`
COPY = "read-log.jsonl"  # the collection's evidence copy of it


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, readlog_collector_sha256=files.sha256_of(files.collector_file()))


def instrument(box) -> ReadLog:
    return ReadLog(box, box.readlog_nonce, box.readlog_expected_peer_uid)


def cells(node: FakeNode):
    """Every cell exec the node saw (a cell runs under `timeout`; the harness's one-shot reads of the image do not)."""
    return [done for done in node.execs if done.command[0] == "timeout"]


@pytest.mark.parametrize("level", ["via-gate", "isolated"])
def test_o91_every_cell_binds_the_socket_directory_and_the_client_read_only(node, settings, level):
    """O91: the collector's socket directory and the client module are bound read-only at the fixed targets in every
    cell, the start's canaries and probes included, whatever the data path."""
    box = start(node, settings, make_spec(level))
    try:
        box.exec("print('x')", 30)
        seen = cells(node)
        assert len(seen) >= 3  # the egress canary, the read-log canary, the read-only probe, the agent's cell
        for done in seen:
            assert done.binds[paths.SOCKET_DIR] == (str(box.dir / "readlog-sock"), False)
            assert done.binds[paths.CLIENT_DIR] == (str(box.dir / "readlog-client"), False)
            assert f"{box.dir / 'readlog-sock'}:{paths.SOCKET_DIR}:ro" in done.argv
            assert f"{box.dir / 'readlog-client'}:{paths.CLIENT_DIR}:ro" in done.argv
    finally:
        box.stop()


def test_o91_no_bind_reaches_the_log_the_collector_or_its_directory(node, settings):
    """O91: nothing a cell binds is, holds or lies under the collector's directory (its copy, its log, its streams),
    and the client's directory holds the client module alone, the very file of the package."""
    box = start(node, settings)
    try:
        box.exec("print('x')", 30)
        own = box.dir / "readlog"
        assert own.is_dir()
        for done in cells(node):
            for source, _ in done.binds.values():
                source = Path(source)
                assert source != own
                assert own not in source.parents
                assert source not in own.parents, "a bind of an ancestor of the collector's directory"
        assert sorted(p.name for p in (box.dir / "readlog-client").iterdir()) == [paths.CLIENT_NAME]
        assert (box.dir / "readlog-client" / paths.CLIENT_NAME).read_bytes() == files.client_file().read_bytes()
    finally:
        box.stop()


def test_o91_the_directories_are_owner_only_and_the_copies_are_not_writable(node, settings):
    """O91: the collector's, the socket's and the client's directories are 0700, and the run's copies of the collector
    and the client are read-only files: what ran is what was hashed."""
    box = start(node, settings)
    try:
        for name in ("readlog", "readlog-sock", "readlog-client"):
            assert (box.dir / name).stat().st_mode & 0o777 == 0o700, name
        for copy in (box.dir / "readlog" / "readlog-collector.py", box.dir / "readlog-client" / paths.CLIENT_NAME):
            assert copy.stat().st_mode & 0o777 == 0o444, copy.name
        assert (box.dir / "readlog" / "readlog-collector.py").read_bytes() == files.collector_file().read_bytes()
    finally:
        box.stop()


def test_o91_the_collector_starts_before_any_cell_and_is_told_the_nonce_and_the_drivers_uid(node, settings):
    """O91: the helper `readlog` is spawned before the first exec, as `python3 -I`, with the socket directory, the log
    in its own directory, the run's nonce and the uid of the cell (the driver's own: every cell runs as it)."""
    box = start(node, settings)
    try:
        collector = node.readlog_collector()
        assert collector.execs_before == 0
        words = collector.proc.cmdline
        assert words[1:3] == ("-I", "-B")
        assert Path(words[3]) == box.dir / "readlog" / "readlog-collector.py"
        assert words[4:] == (
            str(box.dir / "readlog-sock"), str(box.dir / "readlog" / LOG), box.readlog_nonce, str(os.getuid())
        )  # fmt: skip
        assert box.readlog_expected_peer_uid == os.getuid()
        assert collector.proc.environ == (f"{AV.RUN_TAG}={box.sid}",)  # a reaper finds it by the run's tag
        assert box._helpers["readlog"].process.pid == collector.proc.pid
    finally:
        box.stop()


def test_o91_the_canary_is_the_first_record_of_the_log(node, settings, tmp_path):
    """O91: a canary sent through the client from inside a cell, before anything else runs, is the log's first record:
    with it, the collection's log reads intact, with the canary seen."""
    box = start(node, settings)
    try:
        box.collect(tmp_path / "out")
        lines = [json.loads(line) for line in box.read_log().splitlines()]
        assert [line["event"] for line in lines[:2]] == ["start", "record"]
        assert lines[1]["rec"] == {"v": 1, "op": "canary", "item": paths.CANARY_ITEM}
        snapshot = instrument(box).finish()
        assert snapshot["log"] == "intact"
        assert snapshot["canary"] is True
        assert snapshot["counts"] == {"reads": 0, "canaries": 1}
    finally:
        box.stop()


def test_o91_a_canary_the_client_could_not_send_fails_the_start_and_leaves_nothing(node, settings):
    """O91: a cell that cannot send the canary (no socket, a client that raised) fails the start, the collector is
    stopped and the run's directory removed."""
    default = node.cell_handler

    def handler(cell):
        if "log_canary" in cell.program:
            return CmdResult(1, "", "ReadLogError: no socket\n")
        return default(cell)

    node.cell_handler = handler
    with pytest.raises(SandboxError, match="read-log canary"):
        start(node, settings)
    assert not node.readlog_collector().proc.alive
    assert [p.name for p in settings.work_root.iterdir() if p.name.startswith("run-key")] == []


def test_o91_the_log_is_read_once_after_the_collector_stopped_and_copied_into_the_evidence(node, settings, tmp_path):
    """O91: `read_log()` is None while the collector runs; the collection stops it on SIGTERM (it writes its end line),
    reads its log once and copies it; a later change of the file is not what the instrument reads."""
    box = start(node, settings)
    try:
        assert box.read_log() is None
        node.readlog_collector().record({"v": 1, "op": "read", "item": "file-1", "bytes": 9})
        done = box.collect(tmp_path / "out")
        data = box.read_log()
        assert done["read_log"] == COPY
        assert (tmp_path / "out" / COPY).read_bytes() == data
        lines = [json.loads(line) for line in data.splitlines()]
        assert lines[-1]["event"] == "end"
        assert lines[-1]["records"] == 2
        stop = box.metadata()["readlog_stop"]
        assert stop["term"] is True
        assert stop["ended_on_term"] is True
        (box.dir / "readlog" / LOG).write_bytes(b"changed")
        assert box.read_log() == data
        snapshot = instrument(box).finish()
        assert snapshot["log"] == "intact"
        assert snapshot["counts"]["reads"] == 1
        assert snapshot["side_sha256"] is not None
    finally:
        box.stop()


@pytest.mark.parametrize("killed", [False, True], ids=["intact", "killed"])
def test_o91_the_evidence_copy_is_what_the_executor_stages_and_its_hash_is_the_snapshots(
    node, settings, tmp_path, killed
):
    """O91: the executor hands `stage_harness_side_file` the copy `collect` wrote (`result["read_log"]`): the bytes
    the instrument hashed are the staged file's, whether the log is intact or unknown (a killed collector's log is
    staged too, so what was read before the kill can be looked at)."""
    box = start(node, settings)
    try:
        node.readlog_collector().record({"v": 1, "op": "read", "item": "file-1", "bytes": 9})
        if killed:
            proc = node.readlog_collector().proc
            assert node.signal(proc.pid, proc.start, signal.SIGKILL) is True
        done = box.collect(tmp_path / "out")
        snapshot = instrument(box).finish()
        assert snapshot["log"] == ("unknown" if killed else "intact")
        request = SimpleNamespace(
            run_dir=tmp_path / "runs" / "aaaa" / "try1", identity=SimpleNamespace(run_key="run-1", attempt=1)
        )
        declared = [SideFile(paths.SIDE_FILE_NAME, paths.SIDE_HASH_FIELD)]
        staged = stage_harness_side_file(request, declared, paths.SIDE_FILE_NAME, tmp_path / "out" / done["read_log"])
        assert staged is not None
        assert staged.sha256 == snapshot["side_sha256"]
    finally:
        box.stop()


def test_o91_a_collector_killed_during_the_run_gives_an_unknown_log_and_is_a_death(node, settings, tmp_path):
    """O91: a killed collector writes no end line: its death is infrastructure, and the log reads as UNKNOWN reads
    (`not_closed`), with no counts, never as a run that read nothing."""
    box = start(node, settings)
    try:
        proc = node.readlog_collector().proc
        assert node.signal(proc.pid, proc.start, signal.SIGKILL) is True
        done = box.collect(tmp_path / "out")
        assert [d["role"] for d in done["deaths"]] == ["readlog"]
        assert done["deaths"][0]["category"] == "infrastructure"
        snapshot = instrument(box).finish()
        assert snapshot["log"] == "unknown"
        assert "not_closed" in snapshot["unknown"]
        assert snapshot["counts"] is None
    finally:
        box.stop()


def test_o91_a_collector_that_ended_before_the_harness_stopped_it_gives_an_unknown_log(node, settings, tmp_path):
    """O91: a collector told to end by something else before the collection writes a clean end line, so its log would
    read intact with the reads it took and none after (the client then raises); the death is reported and the log is
    WITHHELD (`read_log()` None, no copy, `log_missing`): a lost collector is an unknown log, never a clean one."""
    box = start(node, settings)
    try:
        node.readlog_collector().record({"v": 1, "op": "read", "item": "file-1", "bytes": 9})
        proc = node.readlog_collector().proc
        assert node.signal(proc.pid, proc.start, signal.SIGTERM) is True
        done = box.collect(tmp_path / "out")
        assert [d["role"] for d in done["deaths"]] == ["readlog"]
        assert done["read_log"] is None
        assert not (tmp_path / "out" / COPY).exists()
        assert box.read_log() is None
        assert box.metadata()["readlog_stop"]["ran_until_stop"] is False
        snapshot = instrument(box).finish()
        assert snapshot["log"] == "unknown"
        assert "log_missing" in snapshot["unknown"]
        assert snapshot["counts"] is None
        assert snapshot["side_sha256"] is None
    finally:
        box.stop()


def test_o91_a_stop_that_needed_the_kill_gives_an_unknown_log_even_with_an_end_line(node, settings, tmp_path):
    """O91: a collector that wrote its end line on SIGTERM and did not end on it (the harness had to kill it) is not
    shown to have ended itself: its log is withheld, as a stop that was not clean never gives an intact log."""
    node.readlog_lingers = True
    box = start(node, settings)
    try:
        done = box.collect(tmp_path / "out")
        stop = box.metadata()["readlog_stop"]
        assert stop["ran_until_stop"] is True
        assert stop["ended_on_term"] is False
        assert done["read_log"] is None
        assert box.read_log() is None
        snapshot = instrument(box).finish()
        assert snapshot["log"] == "unknown"
        assert "log_missing" in snapshot["unknown"]
    finally:
        box.stop()


def test_o91_a_stop_whose_state_could_not_be_told_gives_an_unknown_log(node, settings, tmp_path, monkeypatch):
    """O91: when the node cannot say whether the collector is gone after its stop, the stop failed (`readlog_stop` holds
    the error) and its log, which may read intact, is withheld."""
    box = start(node, settings)
    try:
        pid = node.readlog_collector().proc.pid
        real = node.alive
        with monkeypatch.context() as patch:
            patch.setattr(node, "alive", lambda p, s: None if p == pid else real(p, s))
            done = box.collect(tmp_path / "out")
        assert "error" in box.metadata()["readlog_stop"]
        assert done["read_log"] is None
        assert box.read_log() is None
        assert instrument(box).finish()["log"] == "unknown"
    finally:
        box.stop()


def test_o91_a_log_that_cannot_be_read_is_unknown_never_a_run_that_read_nothing(node, settings, tmp_path):
    """O91: a log that is gone when the collection reads it is None, and the instrument says `log_missing`."""
    box = start(node, settings)
    try:
        (box.dir / "readlog" / LOG).unlink()
        done = box.collect(tmp_path / "out")
        assert done["read_log"] is None
        assert box.read_log() is None
        snapshot = instrument(box).finish()
        assert snapshot["log"] == "unknown"
        assert "log_missing" in snapshot["unknown"]
        assert snapshot["counts"] is None
    finally:
        box.stop()


def test_o91_a_log_that_holds_the_token_is_removed_and_fails_the_collection(node, settings, tmp_path):
    """O91, O05: a secret is never evidence: the read-log's copy holding the data-path token is removed, the collection
    fails and the bytes are not handed on as the log."""
    box = start(node, settings)
    try:
        with open(box.dir / "readlog" / LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "leak", "value": TOKEN}) + "\n")
        with pytest.raises(InfraError) as failed:
            box.collect(tmp_path / "out")
        assert failed.value.kind == "output_collection"
        assert not (tmp_path / "out" / COPY).exists()
        assert box.read_log() is None
    finally:
        box.stop()


def test_o91_the_collector_is_part_of_the_run_s_infrastructure_check(node, settings):
    """O91: `infra_check` names the `readlog` helper beside the egress collector and the gate, running throughout."""
    box = start(node, settings)
    try:
        check = box.infra_check()
        assert set(check["containers"]) == {"collector", "readlog", "gate"}
        assert check["containers"]["readlog"]["end"]["running"] is True
        assert check["deaths"] == []
    finally:
        box.stop()


def test_o91_the_teardown_stops_the_collector_and_removes_its_directories(node, settings):
    """O91: after the stop the collector is shown gone, the run's directory (the socket, the log, the copies) is removed
    and nothing of the helper is left to the reaper."""
    box = start(node, settings)
    proc = node.readlog_collector().proc
    run = box.dir
    assert proc.alive
    assert run.is_dir()
    box.stop()
    assert not proc.alive
    assert not run.exists()
    assert box.teardown_left()["helpers"] == 0


def test_o91_the_run_records_the_collector_the_client_and_where_it_ran(node, settings):
    """O91: the collector's and the client's hashes, the placement and the sender uid are recorded once, through the
    infrastructure facts' one writer, as the readlog's own."""
    box = start(node, settings)
    try:
        recorded = box.metadata()["infrastructure_facts"][facts.KEY]
        assert recorded == facts.fact(
            collector_sha256=files.sha256_of(files.collector_file()),
            client_sha256=files.sha256_of(files.client_file()),
            placement="apptainer_helper",
            expected_peer_uid=os.getuid(),
        )
    finally:
        box.stop()


def test_o91_a_platform_that_pins_another_collector_than_the_core_s_refuses_the_start(node, tmp_path):
    """O91: the collector is pinned by its SHA-256, as the egress collector is: another file is another experiment, and
    the start is refused before anything exists."""
    wrong = make_settings(tmp_path, readlog_collector_sha256="0" * 64)
    with pytest.raises(StartRefused, match="read-log collector"):
        start(node, wrong)
    assert not node.readlogs
    assert not node.spawned
    assert not any(p.name.startswith("run-key") for p in wrong.work_root.iterdir())


def test_o91_a_platform_that_declares_no_collector_has_no_channel_and_no_log(node, tmp_path):
    """O91: with no `readlog_collector_sha256` there is no helper, no directory and no bind (a platform written before
    keeps its record), and `read_log()` is None, so a study that asks reads UNKNOWN reads, never none."""
    plain = make_settings(tmp_path)
    box = start(node, plain)
    try:
        box.exec("print('x')", 30)
        assert not node.readlogs
        assert not any((box.dir / name).exists() for name in ("readlog", "readlog-sock", "readlog-client"))
        for done in cells(node):
            assert paths.SOCKET_DIR not in done.binds
            assert paths.CLIENT_DIR not in done.binds
        assert set(box.infra_check()["containers"]) == {"collector", "gate"}
        done = box.collect(tmp_path / "out")
        assert "read_log" not in done
        assert box.read_log() is None
        assert "readlog_stop" not in box.metadata()
        assert facts.KEY not in box.metadata().get("infrastructure_facts", {})
        snapshot = instrument(box).finish()
        assert snapshot["log"] == "unknown"
    finally:
        box.stop()


TARGETS = [paths.SOCKET_DIR, paths.SOCKET, paths.CLIENT_DIR, paths.CLIENT, f"{paths.CLIENT_DIR}/x"]


@pytest.mark.parametrize("target", TARGETS)
def test_o91_a_study_input_at_either_target_is_refused_on_this_backend(node, settings, tmp_path, target):
    """O91: both read-log targets are the harness's own: a read-only input of the study may not be, hold or lie under
    either, so it can neither shadow the socket nor the client."""
    source = tmp_path / "supplied"
    source.mkdir()
    (source / "a.py").write_text("x = 1\n", encoding="utf-8")
    spec = dataclasses.replace(make_spec("isolated"), inputs=(ReadOnlyInput(source, target),))
    with pytest.raises(StartRefused, match="another mount of the agent"):
        start(node, settings, spec)
    assert not node.spawned
