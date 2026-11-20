"""The Docker backend's helpers of the read-log channel: the run's directories and pinned copies, the bounded whole
read of the log, and the scan of the log for the token that never follows a link."""

import os
import stat
from pathlib import Path

import pytest

from expcore_onedata.readlog import files, paths
from expcore_onedata.sandbox import readlog_channel as RL
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.containers import READLOG_LOG_NAME, READLOG_PROGRAM_NAME, ReadLogDirs

TOKEN = b"tok-test-0123456789"


@pytest.fixture
def dirs(tmp_path):
    log = tmp_path / "log"
    log.mkdir()
    return ReadLogDirs(program=tmp_path / "program", sockets=tmp_path / "sock", log=log, client=tmp_path / "client")


def write(dirs, data: bytes) -> Path:
    path = RL.log_path(dirs)
    path.write_bytes(data)
    return path


def test_the_copy_names_are_the_ones_the_apptainer_backend_shares():
    assert RL.COPY_NAME == "read-log.jsonl"
    assert (RL.DIRECTORY, RL.SOCKET_DIRECTORY, RL.CLIENT_DIRECTORY) == ("readlog", "readlog-sock", "readlog-client")


def test_prepare_makes_the_directories_with_their_modes_and_the_two_pinned_read_only_copies(tmp_path):
    pin = files.sha256_of(files.collector_file())
    made = RL.prepare(tmp_path, agent_uid=os.getuid() + 1, pin=pin)
    assert made.collector_sha256 == pin
    assert made.client_sha256 == files.sha256_of(files.client_file())
    assert (made.uid, made.gid) == (os.getuid(), os.getgid())
    mode = {name: stat.S_IMODE(place.stat().st_mode) for name, place in vars(made.dirs).items()}
    assert mode == {"program": 0o755, "sockets": 0o755, "log": 0o700, "client": 0o755}
    assert stat.S_IMODE((tmp_path / RL.DIRECTORY).stat().st_mode) == 0o700
    for copy in (made.dirs.program / READLOG_PROGRAM_NAME, made.dirs.client / paths.CLIENT_NAME):
        assert stat.S_IMODE(copy.stat().st_mode) == 0o444


def test_prepare_refuses_a_copy_that_is_not_the_pinned_file(tmp_path):
    with pytest.raises(SandboxError, match="not the pinned file"):
        RL.prepare(tmp_path, agent_uid=os.getuid() + 1, pin="0" * 64)


def test_the_pin_matches_this_cores_collector_and_nothing_else(monkeypatch):
    assert RL.pin_matches(files.sha256_of(files.collector_file()))
    assert not RL.pin_matches("0" * 64)
    monkeypatch.setattr(files, "collector_file", lambda: Path("/nonexistent/collector.py"))
    assert not RL.pin_matches("0" * 64)  # a collector that cannot be read is no match, not a crash


def test_the_canary_is_said_only_by_a_cell_that_exited_zero_and_printed_it():
    assert RL.canary_sent("canary sent\n", 0)
    assert not RL.canary_sent("canary sent\n", 1)
    assert not RL.canary_sent("", 0)
    assert not RL.canary_sent("canary sent and more\n", 0)


def test_the_canary_imports_the_client_by_its_fixed_path_and_sends_through_it():
    compile(RL.CANARY_SOURCE, "<canary>", "exec")
    assert paths.CLIENT in RL.CANARY_SOURCE
    assert "log_canary()" in RL.CANARY_SOURCE


def test_the_log_is_read_whole(dirs):
    write(dirs, b"abc\n")
    assert RL.read_log_bytes(dirs) == b"abc\n"


def test_a_missing_log_a_link_and_a_directory_are_not_read(dirs, tmp_path):
    with pytest.raises(FileNotFoundError):
        RL.read_log_bytes(dirs)
    target = tmp_path / "target"
    target.write_bytes(b"x")
    RL.log_path(dirs).symlink_to(target)
    with pytest.raises(OSError):  # noqa: PT011  # which errno a link gives is the platform's (ELOOP): any refusal will do
        RL.read_log_bytes(dirs)
    RL.log_path(dirs).unlink()
    RL.log_path(dirs).mkdir()
    with pytest.raises(OSError):  # noqa: PT011  # a directory is refused, by `fdopen` or by the kind check
        RL.read_log_bytes(dirs)


def test_a_log_larger_than_a_collector_writes_is_refused_never_returned_as_a_prefix(dirs, monkeypatch):
    monkeypatch.setattr(RL, "MAX_READ_BYTES", 8)
    write(dirs, b"12345678")
    assert RL.read_log_bytes(dirs) == b"12345678"  # exactly the most: whole
    write(dirs, b"123456789")
    with pytest.raises(OSError, match="larger"):
        RL.read_log_bytes(dirs)


def test_the_scan_finds_the_token_wherever_it_lies_and_never_reads_a_link(dirs, tmp_path):
    assert RL.holds_token(dirs, TOKEN) is False  # no log: nothing held
    write(dirs, b"")
    assert RL.holds_token(dirs, TOKEN) is False
    assert RL.holds_token(dirs, b"") is False  # no token: nothing to find
    write(dirs, b"x" * 100 + TOKEN + b"y")
    assert RL.holds_token(dirs, TOKEN) is True
    write(dirs, b"x" * 100)
    assert RL.holds_token(dirs, TOKEN) is False


def test_the_scan_finds_a_token_that_straddles_two_chunks_of_the_read(dirs):
    chunk = 1 << 20
    for cut in range(1, len(TOKEN)):
        write(dirs, b"z" * (chunk - cut) + TOKEN + b"z" * 10)
        assert RL.holds_token(dirs, TOKEN) is True, cut
    write(dirs, b"z" * (3 * chunk))
    assert RL.holds_token(dirs, TOKEN) is False


def test_a_log_that_cannot_be_scanned_is_none_for_the_caller_to_treat_as_holding_it(dirs, tmp_path):
    target = tmp_path / "target"
    target.write_bytes(TOKEN)
    RL.log_path(dirs).symlink_to(target)
    assert RL.holds_token(dirs, TOKEN) is None  # a link is never followed
    RL.log_path(dirs).unlink()
    RL.log_path(dirs).mkdir()
    assert RL.holds_token(dirs, TOKEN) is None
    assert READLOG_LOG_NAME in os.listdir(dirs.log)
