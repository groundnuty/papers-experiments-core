"""O91: the read-log collector on real `AF_UNIX` datagram sockets: a collector process, a client sending, a SIGTERM
drain, a killed collector, a full log, a malformed datagram, a bad peer uid, and every refusal of a start.

The reader (`expcore_onedata.readlog.reader`) judges every log here, as it will judge a run's, so what the collector
writes and what the reader accepts cannot drift apart. These tests need Unix sockets: a sandbox that forbids them
skips them, and CI and the HPC allocation run them.
"""

import json
import os
import shutil
import signal
import socket
import stat
import subprocess

import pytest
from readlog_logs import NONCE, SHA
from readlog_rig import Rig, load_client, load_collector, needs_unix, raw_sender, short_dir

from expcore_onedata.readlog import paths, reader
from expcore_onedata.readlog.reader import read_log

pytestmark = needs_unix

OTHER_UID = os.getuid() + 1


@pytest.fixture
def rig():
    made = Rig(short_dir())
    yield made
    made.close()


@pytest.fixture
def client(rig):
    return load_client(rig.sock)


def judged(rig, uid=None, **options):
    return read_log(rig.data(), NONCE, expected_peer_uid=os.getuid() if uid is None else uid, **options)


def lines_of(rig):
    return [json.loads(line) for line in rig.data().decode().splitlines()]


# --- the lifecycle ----------------------------------------------------------------------------------------------------


def test_o91_a_client_sending_to_a_collector_that_is_stopped_with_sigterm_leaves_an_intact_log(rig, client):
    """O91: the canary, then reads, each stamped; SIGTERM drains and writes the end line; the reader says intact."""
    rig.start()
    client.log_canary()
    client.log_read("file-1", range=[0, 4096], bytes=4096, sha256=SHA)
    client.log_read("file-2")
    rig.wait_for_lines(4)
    assert rig.stop() == 0
    reading = judged(rig)
    assert reading.intact, reading.problems
    assert [r["item"] for r in reading.reads] == ["file-1", "file-2"]
    assert reading.reads[0] == {
        "v": 1,
        "op": "read",
        "item": "file-1",
        "range": [0, 4096],
        "bytes": 4096,
        "sha256": SHA,
    }
    lines = lines_of(rig)
    assert [line["seq"] for line in lines] == list(range(len(lines)))
    assert lines[0]["event"] == "start"
    assert lines[0]["nonce"] == NONCE
    assert lines[0]["format"] == paths.FORMAT
    assert lines[0]["peer_uid"] == os.getuid()
    assert lines[-1] == {
        **lines[-1],
        "event": "end",
        "records": 3,
        "malformed": 0,
        "unwritten": 0,
        "cap_reached": False,
    }
    for line in lines[1:-1]:
        assert line["peer"]["uid"] == os.getuid()
        assert line["wall"] > 0
        assert line["mono"] > 0


def test_o91_a_sigterm_drain_keeps_every_datagram_queued_before_the_stop(rig, client):
    """O91: datagrams queued while the collector cannot run are all written once it is told to stop: the stopped
    collector is sent TERM and then continued, and the drain reads what was queued before it ends."""
    rig.start()
    client.log_canary()
    rig.wait_for_lines(2)
    assert rig.proc is not None
    rig.proc.send_signal(signal.SIGSTOP)
    for n in range(8):
        client.log_read(f"queued-{n}")
    rig.proc.send_signal(signal.SIGTERM)
    rig.proc.send_signal(signal.SIGCONT)
    assert rig.proc.wait(timeout=30) == 0
    reading = judged(rig)
    assert reading.intact, reading.problems
    assert [r["item"] for r in reading.reads] == [f"queued-{n}" for n in range(8)]


def test_o91_after_the_stop_the_socket_is_gone_and_a_late_send_raises(rig, client):
    """O91: nothing can be logged to a stopped collector: the call raises, so the script returns no data."""
    rig.start()
    client.log_canary()
    assert rig.stop() == 0
    assert not os.path.exists(rig.sock)
    with pytest.raises(client.ReadLogError):
        client.log_read("late")


def test_o91_a_killed_collector_gives_an_unknown_log_and_the_client_then_fails_closed(rig, client):
    """O91: SIGKILL leaves no end line: the log is unknown (not_closed), the records read before are kept as a lower
    bound, and a send after the kill raises: the script returns no data."""
    rig.start()
    client.log_canary()
    client.log_read("before-the-kill")
    rig.wait_for_lines(3)
    rig.kill()
    reading = judged(rig)
    assert not reading.intact
    assert reader.NOT_CLOSED in reading.problems
    assert [r["item"] for r in reading.reads] == ["before-the-kill"]
    with pytest.raises(client.ReadLogError):
        client.log_read("after-the-kill")


def test_o91_the_log_is_created_exclusively_with_mode_0600_and_a_log_that_exists_is_refused(rig):
    """O91: the log is created exclusively with mode 0600 and a log that exists is refused."""
    rig.start()
    assert stat.S_IMODE(rig.log.stat().st_mode) == 0o600
    rig.stop()
    done = subprocess.run(rig.command(os.getuid()), capture_output=True, timeout=30, check=False)
    assert done.returncode == 2
    assert b"already exists" in done.stderr
    assert lines_of(rig)[0]["event"] == "start"  # the earlier run's log is untouched


def test_o91_the_start_line_names_the_nonce_the_format_and_the_peer_uid_it_was_told(rig):
    """O91: the start line names the nonce the format and the peer uid it was told."""
    rig.start()
    rig.stop()
    start = lines_of(rig)[0]
    assert (start["event"], start["format"], start["nonce"], start["peer_uid"]) == (
        "start",
        "expcore-readlog/1",
        NONCE,
        os.getuid(),
    )
    assert start["uid"] == os.getuid()
    assert start["max_bytes"] == paths.MAX_LOG_BYTES


# --- what is kept, and what is not ------------------------------------------------------------------------------------


MARKER = "SECRET-MARKER-5f3a"
MALFORMED = {
    "not_json": b"not json at all " + MARKER.encode(),
    "not_ascii": b'{"v":1,"op":"read","item":"\xc3\xa9' + MARKER.encode() + b'"}',
    "deep": b"[" * 4000,
    "extra_field": b'{"v":1,"op":"read","item":"a","path":"/data/' + MARKER.encode() + b'"}',
    "duplicate_key": b'{"v":1,"op":"read","item":"a","item":"b' + MARKER.encode() + b'"}',
    "constant": b'{"v":1,"op":"read","item":"a","bytes":NaN}',
    "float": b'{"v":1.0,"op":"read","item":"a"}',
    "bad_op": b'{"v":1,"op":"write","item":"a"}',
    "empty": b"",
    "too_long": b'{"v":1,"op":"read","item":"' + b"x" * 4100 + b'"}',
}


@pytest.mark.parametrize("kind", sorted(MALFORMED))
def test_o91_a_datagram_that_is_no_client_line_is_a_malformed_line_by_hash_only_and_the_log_is_unknown(
    rig, client, kind
):
    """O91: no byte of a datagram that is no record is written (its length and SHA-256 are), the end line counts it,
    and the whole log is unknown: its reads are unknown, never fewer."""
    rig.start()
    client.log_canary()
    client.log_read("fine")
    sender = raw_sender(rig.sock)
    sender.send(MALFORMED[kind])
    sender.close()
    rig.wait_for_lines(4)
    assert rig.stop() == 0
    assert MARKER.encode() not in rig.data()
    lines = lines_of(rig)
    [bad] = [line for line in lines if line["event"] == "malformed"]
    assert bad["bytes"] == min(len(MALFORMED[kind]), paths.MAX_LINE_BYTES + 1)
    assert bad["why"]
    assert len(bad["sha256"]) == 64
    assert lines[-1]["malformed"] == 1
    reading = judged(rig)
    assert reading.problems == (reader.MALFORMED,)
    assert [r["item"] for r in reading.reads] == ["fine"]


def test_o91_a_datagram_longer_than_the_buffer_is_cut_and_malformed_whatever_it_holds(rig):
    """O91: the receive buffer is one byte past the cap, so a datagram of any greater length is seen as too long (the
    kernel's MSG_TRUNC), never as a valid prefix."""
    rig.start()
    valid = b'{"v":1,"op":"canary","item":"canary"}'
    sender = raw_sender(rig.sock)
    sender.send(valid + b" " * 5000)  # a valid line followed by padding, far past the cap
    sender.close()
    rig.wait_for_lines(2)
    rig.stop()
    [bad] = [line for line in lines_of(rig) if line["event"] == "malformed"]
    assert bad["why"] == "too_long"


def test_o91_a_line_of_exactly_the_cap_is_accepted_and_one_byte_more_is_refused_by_the_client(rig, client):
    """O91: 4,096 bytes is the most a line may hold: the client refuses 4,097, the collector takes 4,096."""
    rig.start()
    client.log_canary()
    overhead = len(client.encode("read", "x")) - 1  # the line's bytes around an empty item
    fill = paths.MAX_LINE_BYTES - overhead
    item = "é" * (fill // 6) + "a" * (fill % 6)  # each e-acute is 6 bytes, `é`, in ASCII JSON
    assert len(client.encode("read", item)) == paths.MAX_LINE_BYTES
    client.log_read(item)
    with pytest.raises(client.ReadLogError, match="longer than"):
        client.log_read(item + "a")
    rig.wait_for_lines(3)
    rig.stop()
    reading = judged(rig)
    assert reading.intact, reading.problems
    assert reading.reads[0]["item"] == item


def test_o91_a_full_log_stops_at_its_cap_counts_what_it_could_not_write_and_is_unknown(rig, client):
    """O91: past the byte cap a datagram is only counted: the log stays under the cap with room for its end line, the
    end line says cap_reached and counts the unwritten, and the reader calls the log unknown. The client's sends still
    succeed (queued is all a send knows): the loss is the run's, never a quiet one."""
    cap = 16 * 1024
    rig.start(max_bytes=cap)
    client.log_canary()
    for n in range(150):
        client.log_read(f"file-{n:04d}", range=[0, 4096], bytes=4096, sha256=SHA)
    rig.wait_for_lines(5)
    assert rig.stop() == 0
    assert len(rig.data()) <= cap
    end = lines_of(rig)[-1]
    assert end["event"] == "end"
    assert end["cap_reached"] is True
    assert end["unwritten"] > 0
    assert end["records"] + end["unwritten"] == 151
    reading = judged(rig)
    assert not reading.intact
    assert {reader.CAP_REACHED, reader.UNWRITTEN} <= set(reading.problems)


# --- the peer rule ----------------------------------------------------------------------------------------------------


def test_o91_a_record_from_another_uid_than_the_expected_one_makes_the_log_unknown(rig, client):
    """O91: the collector is told the uid to expect (here not this process's, as on Docker, where it differs from the
    collector's own): every record is stamped with the sender's real uid, and the reader refuses a foreign one."""
    rig.start(OTHER_UID)
    client.log_canary()
    client.log_read("file-1")
    rig.wait_for_lines(3)
    rig.stop()
    assert lines_of(rig)[0]["peer_uid"] == OTHER_UID
    reading = judged(rig, OTHER_UID)
    assert reader.FOREIGN in reading.problems
    assert not reading.intact
    assert judged(rig, os.getuid()).problems == (reader.WRONG_PEER_RULE,)  # told another uid than this one: no match


def test_o91_the_socket_and_its_directory_follow_the_peer_rule(rig):
    """O91: for a sender of the collector's own uid the directory is 0700 and the socket owner-only; for another uid
    (a Docker agent) the directory is 0755 and the socket 0666, so it can connect, and the log stays 0600."""
    rig.start()
    assert stat.S_IMODE(rig.dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(os.stat(rig.sock).st_mode) & 0o077 == 0
    rig.stop()
    other = Rig(short_dir())
    try:
        other.start(OTHER_UID)
        assert stat.S_IMODE(other.dir.stat().st_mode) == 0o755
        assert stat.S_IMODE(os.stat(other.sock).st_mode) == 0o666
        assert stat.S_IMODE(other.log.stat().st_mode) == 0o600
        other.stop()
    finally:
        other.close()


# --- a start the collector refuses --------------------------------------------------------------------------------


def refused(rig, argv):
    done = subprocess.run(argv, capture_output=True, timeout=30, check=False)
    assert done.returncode == 2, done.stderr
    assert not rig.log.exists()
    return done.stderr.decode()


def collector_argv(rig, *tail):
    return [*rig.command(os.getuid())[:3], str(rig.dir), str(rig.log), *tail]


@pytest.mark.parametrize(
    ("problem", "expect"),
    [
        ("short_nonce", "nonce"),
        ("upper_nonce", "nonce"),
        ("bad_uid", "uid"),
        ("negative_uid", "uid"),
        ("bad_max", "max"),
        ("tiny_max", "max"),
        ("huge_max", "max"),
        ("no_dir", "no socket directory"),
        ("open_dir", "0700"),
        ("closed_dir_for_other", "0755"),
        ("not_empty", "empty"),
        ("too_few_arguments", "usage"),
        ("too_many_arguments", "usage"),
    ],
)
def test_o91_the_collector_refuses_a_bad_start_before_it_creates_a_log(rig, problem, expect):
    """O91: arguments, the socket directory's owner, mode and emptiness are checked first: a refused start exits 2 with
    its reason on stderr and leaves no log behind."""
    rig.make_dir(0o700)
    uid = str(os.getuid())
    base = collector_argv(rig, NONCE, uid)
    argv = {
        "short_nonce": collector_argv(rig, "abc", uid),
        "upper_nonce": collector_argv(rig, "AB" * 16, uid),
        "bad_uid": collector_argv(rig, NONCE, "me"),
        "negative_uid": collector_argv(rig, NONCE, "-1"),
        "bad_max": [*base, "lots"],
        "tiny_max": [*base, "10"],
        "huge_max": [*base, str(paths.MAX_LOG_BYTES + 1)],
        "no_dir": [*base[:3], str(rig.dir / "gone"), *base[4:]],
        "open_dir": base,
        "closed_dir_for_other": collector_argv(rig, NONCE, str(OTHER_UID)),
        "not_empty": base,
        "too_few_arguments": base[:5],
        "too_many_arguments": [*base, "20000", "x"],
    }[problem]
    if problem == "open_dir":
        rig.dir.chmod(0o755)
    if problem == "not_empty":
        (rig.dir / "stray").write_text("x")
    assert expect in refused(rig, argv)


# --- in-process: the order of a stop -----------------------------------------------------------------------------


def test_o91_once_the_collector_stops_reading_a_send_raises_while_what_was_queued_is_still_drained():
    """O91: a send that lands between the last read and the close would succeed with nothing recorded. The collector
    shuts its receiving side before it drains: from then on a send fails (EPIPE), and the datagrams queued before are
    still read, so a call that succeeded was recorded, and one that was not raises."""
    collector = load_collector()
    root = short_dir()
    try:
        where = str(root / "sock")
        receiving = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        receiving.bind(where)
        client = load_client(where)
        for n in range(3):
            client.log_read(f"queued-{n}")
        collector.stop_receiving(receiving)
        with pytest.raises(client.ReadLogError):
            client.log_read("too-late")
        receiving.setblocking(False)
        got = []
        while True:
            try:
                got.append(receiving.recv(paths.MAX_LINE_BYTES + 1))
            except BlockingIOError:
                break
        assert [json.loads(g)["item"] for g in got] == ["queued-0", "queued-1", "queued-2"]
        receiving.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)
