"""O91: the read-log channel on the Apptainer backend with the REAL collector process and the real client.

The fake node runs the collector program for real (`FakeNode.real_readlog`: a helper of this uid, in a session of its
own, bound to a Unix datagram socket in the run's directory). A cell is not run, so the client a script would import is
loaded from the file the cell's bind holds, its fixed socket path (which exists only inside a cell) replaced by the
bind's source: what is proved is the collector's command line, the peer uid it sees, the drain at the stop, a killed
collector and the client's refusal to carry on unlogged. What only a real Apptainer cell can show (the read-only bind
taking a `sendto`, an unprivileged user namespace over it) is the live slot's.
"""

import json
import os
import shutil
import signal
import stat
import time

import pytest
from apptainer_support import (
    make_settings,
    make_spec,
    settings_with_socket_bytes,
    settings_with_work_root_bytes,
    short_dir,
)
from apptainer_support import start as start_with
from fake_node import FakeNode

from expcore_onedata.readlog import files, paths
from expcore_onedata.readlog.instrument import ReadLog
from expcore_onedata.sandbox.apptainer.sockets import (
    READLOG_SOCK_DIR,
    RUN_ID_MAX_BYTES,
    work_root_max_bytes,
    work_root_problem,
)
from expcore_onedata.sandbox.hostops import CmdResult

SHA = "ab" * 32
UNIX_SOCKET_PATH_BYTES = 107  # AF_UNIX's sun_path holds 108 bytes, the last one a NUL


@pytest.fixture
def node() -> FakeNode:
    real = FakeNode()
    real.real_readlog = True
    return real


@pytest.fixture
def settings(short_tmp):
    """A short work root: the collector's socket path must fit a Unix socket's address, which a pytest path may not (on
    a batch node pytest's own paths are well over it, and the start refuses a path that does not fit)."""
    return make_settings(short_tmp, readlog_collector_sha256=files.sha256_of(files.collector_file()))


def start(node, settings, spec=None):
    """A start on the real clock: the helpers' waits (`helpers.stop` gives a collector a few seconds to drain and write
    its end line) are in real time here, since the collector is a real process; the default's clock moves only when the
    sandbox sleeps, so a stop would give up at once and kill the collector before its end line."""
    return start_with(node, settings, spec, sleep=time.sleep, clock=time.monotonic)


def instrument(box) -> ReadLog:
    return ReadLog(box, box.readlog_nonce, box.readlog_expected_peer_uid)


def sends(*items, **fields):
    """A cell's handler: a canonical script that logs each item through the client before it returns."""

    def handler(cell):
        client = cell.readlog_client()
        for item in items:
            client.log_read(item, **fields)
        return CmdResult(0, "")

    return handler


def entries(box):
    return [json.loads(line) for line in box.read_log().splitlines()]


def test_o91_a_client_in_a_cell_sends_and_the_record_appears_with_the_expected_peer_uid(node, settings, tmp_path):
    """O91: a read logged through the real client by a script in a cell is a record of the log, stamped with the
    sender's uid as the kernel attests it: the driver's own, which the collector was told to expect."""
    box = start(node, settings)
    try:
        assert len(str(box.dir / "readlog-sock" / paths.SOCKET_NAME)) <= UNIX_SOCKET_PATH_BYTES
        node.cell_handler = sends("file-7", range=[0, 10], bytes=10, sha256=SHA)
        box.exec("print('x')", 30)
        box.collect(tmp_path / "out")
        records = [line for line in entries(box) if line["event"] == "record"]
        assert [r["rec"]["op"] for r in records] == ["canary", "read"]
        expected = {"v": 1, "op": "read", "item": "file-7", "range": [0, 10], "bytes": 10, "sha256": SHA}
        assert records[1]["rec"] == expected
        assert {r["peer"]["uid"] for r in records} == {box.readlog_expected_peer_uid} == {os.getuid()}
        assert all(r["peer"]["pid"] > 0 and r["wall"] > 0 and r["mono"] > 0 for r in records)
        snapshot = instrument(box).finish()
        assert snapshot["log"] == "intact"
        assert snapshot["canary"] is True
        assert snapshot["counts"] == {"reads": 1, "canaries": 1}
    finally:
        box.stop()


def test_o91_the_canary_through_the_real_client_is_the_first_record_and_the_collector_ran_as_asked(
    node, settings, tmp_path
):
    """O91: the start's canary goes through the real client to the real collector: the log's start line carries the
    run's nonce and the expected uid the backend gave on the command line, the canary is the first record."""
    box = start(node, settings)
    try:
        box.collect(tmp_path / "out")
        lines = entries(box)
        assert lines[0]["event"] == "start"
        assert lines[0]["nonce"] == box.readlog_nonce
        assert lines[0]["peer_uid"] == os.getuid()
        assert lines[0]["format"] == paths.FORMAT
        assert (lines[1]["event"], lines[1]["rec"]["op"]) == ("record", "canary")
        assert (lines[-1]["event"], lines[-1]["records"]) == ("end", 1)
        assert instrument(box).finish()["log"] == "intact"
    finally:
        box.stop()


def test_o91_every_record_queued_before_the_stop_is_in_the_log_and_the_counts_match(node, settings, tmp_path):
    """O91: the stop is SIGTERM, so what the collector holds queued is drained before its end line: all of fifty reads
    are in the log, and the end line's counts are the lines before it."""
    box = start(node, settings)
    try:
        node.cell_handler = sends(*(f"file-{n}" for n in range(50)))
        box.exec("print('x')", 30)
        box.collect(tmp_path / "out")
        snapshot = instrument(box).finish()
        assert snapshot["log"] == "intact", snapshot
        assert snapshot["counts"]["reads"] == 50
        assert entries(box)[-1]["records"] == 51
    finally:
        box.stop()


def test_o91_the_socket_directory_holds_the_socket_alone_and_the_log_is_owner_only_outside_it(node, settings):
    """O91: what the cell's bind shows is one socket the harness's own uid alone may use; the log, the collector's
    copy and its streams are in a directory of their own, owner-only, that no cell binds."""
    box = start(node, settings)
    try:
        assert os.listdir(box.dir / "readlog-sock") == [paths.SOCKET_NAME]
        info = os.lstat(box.dir / "readlog-sock" / paths.SOCKET_NAME)
        assert stat.S_ISSOCK(info.st_mode)
        assert info.st_mode & 0o077 == 0
        assert info.st_uid == os.getuid()
        log = box.dir / "readlog" / "readlog.jsonl"
        assert stat.S_IMODE(log.stat().st_mode) == 0o600
        assert sorted(p.name for p in (box.dir / "readlog").iterdir()) == [
            "readlog-collector.py", "readlog.jsonl", "readlog.stderr", "readlog.stdout"
        ]  # fmt: skip
    finally:
        box.stop()


def test_o91_a_collector_killed_mid_run_gives_an_unknown_log_and_the_client_fails_closed(node, settings, tmp_path):
    """O91: with the collector SIGKILLed the client RAISES, so the script returns no data ("unread"); the log has no
    end line, the run reads as UNKNOWN reads (`not_closed`), and the death is infrastructure."""
    box = start(node, settings)
    try:
        node.cell_handler = sends("file-1")
        box.exec("print('x')", 30)  # one read logged before the kill
        ((pid, start_time),) = node.real_helpers
        assert node.signal(pid, start_time, signal.SIGKILL) is True
        until = time.monotonic() + 5
        while node.alive(pid, start_time) and time.monotonic() < until:
            time.sleep(0.01)
        outcome = {}

        def refused(cell):
            client = cell.readlog_client()
            try:
                client.log_read("file-2")
            except client.ReadLogError as error:
                outcome["error"] = error
            return CmdResult(0, "")

        node.cell_handler = refused
        box.exec("print('x')", 30)
        assert "error" in outcome, "the client returned as if the read were logged"
        done = box.collect(tmp_path / "out")
        assert [d["role"] for d in done["deaths"]] == ["readlog"]
        snapshot = instrument(box).finish()
        assert snapshot["log"] == "unknown"
        assert "not_closed" in snapshot["unknown"]
        assert snapshot["counts"] is None
    finally:
        box.stop()


def test_o91_a_real_collector_terminated_before_the_collection_gives_an_unknown_log(node, settings, tmp_path):
    """O91: SIGTERM to the real collector before the collection makes it drain, write a clean end line and exit, so the
    bytes read intact with the canary alone; the death is reported and the log is withheld, never an intact log of a
    run whose collector was lost."""
    box = start(node, settings)
    try:
        ((pid, start_time),) = node.real_helpers
        assert node.signal(pid, start_time, signal.SIGTERM) is True
        until = time.monotonic() + 10
        while node.alive(pid, start_time) and time.monotonic() < until:
            time.sleep(0.01)
        assert (box.dir / "readlog" / "readlog.jsonl").read_bytes().endswith(b"}\n")  # it wrote its end line
        done = box.collect(tmp_path / "out")
        assert [d["role"] for d in done["deaths"]] == ["readlog"]
        assert done["read_log"] is None
        assert box.read_log() is None
        snapshot = instrument(box).finish()
        assert snapshot["log"] == "unknown"
        assert "log_missing" in snapshot["unknown"]
    finally:
        box.stop()


def test_o91_a_socket_path_of_exactly_the_limit_binds_with_the_real_collector(node, short_tmp):
    """O91: the start's refusal of a long socket path (`test_readlog.py`) is placed where the kernel's own limit is: the
    real collector binds a path of exactly 107 bytes, and the start, canary included, completes."""
    long = settings_with_socket_bytes(
        short_tmp, UNIX_SOCKET_PATH_BYTES, readlog_collector_sha256=files.sha256_of(files.collector_file())
    )
    box = start(node, long)
    try:
        assert len(os.fsencode(box.dir / "readlog-sock" / paths.SOCKET_NAME)) == UNIX_SOCKET_PATH_BYTES
        assert stat.S_ISSOCK(os.lstat(box.dir / "readlog-sock" / paths.SOCKET_NAME).st_mode)
    finally:
        box.stop()


def test_o91_the_longest_work_root_the_load_allows_binds_with_the_real_collector_at_the_longest_run_id(node):
    """O91: the load's bound (`work_root_problem`) is the work root that, with a run id of its longest (a 200-character
    lease key cut to 48, "-" and 8 hex digits: 57 bytes), leaves the read-log socket path at exactly 107 bytes, and the
    real collector binds it: the bound refuses what cannot bind and nothing that can."""
    longest = work_root_max_bytes(READLOG_SOCK_DIR)
    assert work_root_problem(longest) is None
    assert work_root_problem(longest + 1) is not None
    base = short_dir(limit=longest - len("/root") - 2)  # leaves one byte at least for the name that makes up the rest
    try:
        pin = files.sha256_of(files.collector_file())
        box = start(node, settings_with_work_root_bytes(base, longest, readlog_collector_sha256=pin),
                    make_spec(lease_key="k" * 200))  # fmt: skip
        try:
            assert len(box.sid) == RUN_ID_MAX_BYTES
            socket = box.dir / READLOG_SOCK_DIR / paths.SOCKET_NAME
            assert len(os.fsencode(socket)) == UNIX_SOCKET_PATH_BYTES
            assert stat.S_ISSOCK(os.lstat(socket).st_mode)
        finally:
            box.stop()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_o91_the_teardown_stops_the_real_collector_and_removes_the_socket_and_the_log(node, settings):
    """O91: after the stop the collector process is gone, the socket and the log with the run's directory, whether or
    not the collection ran."""
    box = start(node, settings)
    ((pid, start_time),) = node.real_helpers
    run = box.dir
    assert node.alive(pid, start_time) is True
    box.stop()
    assert node.alive(pid, start_time) is False
    assert not run.exists()
