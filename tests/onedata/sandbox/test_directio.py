"""O40: the start-time check that a direct mount reads the local provider's storage itself, judged offline.

The check has two inputs: the probe the agent runs on the check file (its size, its storage, how much of it the local
provider holds, and a read of the whole file within a deadline) and the sidecar's oneclient log after that read. A read
that succeeds is not enough: a direct read refused with EACCES is retried through the provider (`fsLogic.cc`, the
`readInternal` fallback, lines 1184-1203), so the log is what tells a direct read from one that fell back.
"""

import json
import subprocess
import sys

import pytest

from expcore_onedata.sandbox import directio as D
from expcore_onedata.sandbox.oneclient_args import DISABLE_LOG_BUFFERING

STORAGES = ("0a1b2c3d4e5f", "6a7b8c9d0e1f")  # invented storage ids, in the file's order
HEADER = """Log file created at: 2026/11/13 10:00:00
Running on machine: invented-host
I1113 10:00:00.100000     1 logging.cc:76] Oneclient version: 25.0.0
I1113 10:00:00.100300     1 logging.cc:84] Forced direct IO: {direct}
I1113 10:00:00.100400     1 logging.cc:85] Forced proxy IO: {proxy}
I1113 10:00:00.101100     1 logging.cc:110] Mountpoint: /mnt/oneclient
"""
FALLBACK = (
    "E1113 10:00:02.000000    12 fsLogic.cc:1184] Reading from 0123abcd failed due to insufficient permissions\n"
    "I1113 10:00:02.000100    12 helpersCache.h:467] Performing automatic storage access type detection for storage "
    "0a1b2c3d4e5f for file 0123abcd with forced proxy io mode: 1\n"
)
PROBE = {"size": 4096, "storage_id": STORAGES[1], "held": "100%", "read": 4096, "seconds": 0.02}


def log(direct="1", proxy="0", tail=""):
    return HEADER.format(direct=direct, proxy=proxy) + tail


def judge(probe=PROBE, text=None, storages=STORAGES):
    return D.judge(probe, log() if text is None else text, storages)


def test_o40_a_whole_read_of_a_held_file_with_forced_direct_and_no_other_line_is_direct():
    """O40: forced direct on, forced proxy off, the file held in full by the local provider and on a mapped storage,
    read whole, and no line of detection or fallback: direct I/O is in effect. The record names the storage by its
    position, never its id."""
    problems, record = judge()
    assert problems == []
    assert record == {
        "forced_direct": "1", "forced_proxy": "0", "size": 4096, "read": 4096, "seconds": 0.02, "held": "100%",
        "storage": 1, "lines": [],
    }  # fmt: skip
    assert STORAGES[1] not in json.dumps(record)


def test_o40_a_read_that_fell_back_to_the_provider_is_refused_though_it_read_every_byte():
    """O40: the false pass the log exists to catch: the read returned the whole file, through the provider."""
    problems, record = judge(text=log(tail=FALLBACK))
    assert record["read"] == record["size"]
    assert any("proxy_fallback" in p for p in problems)
    assert any("automatic_detection" in p for p in problems)
    assert record["lines"] == ["automatic_detection", "proxy_fallback"]


@pytest.mark.parametrize(
    ("line", "name"),
    [
        ("I1113 10:00:02.0 7 helpersCache.h:467] Performing automatic storage access type detection for storage x "
         "for file y with forced proxy io mode: 0", "automatic_detection"),
        ("I1113 10:00:02.0 7 helpersCache.h:498] Storage x not accessible for direct access from this Oneprovider "
         "- switching to proxy mode.", "switched_to_proxy"),
        ("E1113 10:00:02.0 7 helpersCache.h:645] File y is not accessible in direct IO mode on this provider",
         "not_direct_on_provider"),
        ("I1113 10:00:02.0 7 helpersCache.h:668] Direct IO requested to Posix storage x - attempting storage "
         "mountpoint detection in local filesystem", "storage_not_mapped"),
        ("W1113 10:00:02.0 7 storageAccessManager.cc:56] Manually specified mountPoint /m for storage x is not one "
         "of mounts available in the system. Please make sure it is correct...", "mount_point_not_mounted"),
        ("E1113 10:00:02.0 7 fuseFileHandle.cc:70] Could not create storage helper for file y on storage x",
         "no_storage_helper"),
    ],
)  # fmt: skip
def test_o40_each_line_that_shows_direct_io_not_in_effect_refuses_the_start_and_is_named(line, name):
    """O40: every marker line refuses, named in the problem and in the record."""
    problems, record = judge(text=log(tail=line + "\n"))
    assert any(name in p for p in problems)
    assert record["lines"] == [name]


@pytest.mark.parametrize(("direct", "proxy"), [("0", "0"), ("0", "1"), ("1", "1")])
def test_o40_start_lines_that_do_not_say_forced_direct_and_no_forced_proxy_refuse(direct, proxy):
    """O40: the start lines must say forced direct and no forced proxy."""
    problems, _ = judge(text=log(direct, proxy))
    assert any("Forced" in p for p in problems)


def test_o40_a_log_without_its_start_lines_refuses():
    """O40: an unread or empty log shows nothing, which is never taken for direct I/O."""
    problems, record = judge(text="")
    assert any("Forced direct IO" in p for p in problems)
    assert record["forced_direct"] is None


@pytest.mark.parametrize(
    ("probe", "words"),
    [
        ({**PROBE, "read": 1000}, "read 1000 of 4096"),
        ({**PROBE, "read": None, "read_error": "EIO"}, "EIO"),
        ({**PROBE, "read": None, "blocked": True}, "did not return"),
        ({**PROBE, "size": 0, "read": 0}, "empty"),
        ({**PROBE, "held": "40%", "read": None}, "holds 40%"),
        ({**PROBE, "storage_id": "ffffffffffff"}, "not one of [sandbox.mount.storages]"),
        ({"error": "ENOENT"}, "ENOENT"),
        ({}, "no answer"),
    ],
)
def test_o40_a_probe_that_did_not_read_a_held_file_of_a_mapped_storage_whole_refuses(probe, words):
    """O40: the read must be the whole file, of a storage the mount maps, held in full by the local provider (a read
    of a block it lacks would fetch and place it), within the deadline; a file that is empty proves nothing."""
    problems, _ = judge(probe=probe)
    assert any(words in p for p in problems), problems
    assert all("ffffffffffff" not in p for p in problems)


def test_o40_the_probe_reads_nothing_of_a_file_the_local_provider_does_not_hold_in_full(tmp_path):
    """O40: the probe asks the mount how much of the file the local provider holds before it reads, and reads nothing
    when it is not all of it, so the check never places a block. Run here on a plain file whose attributes the stand-in
    answers."""
    found = run_probe(tmp_path, held="40%")
    assert found["held"] == "40%"
    assert found["read"] is None
    assert "read_error" not in found


def test_o40_the_probe_reads_a_held_file_whole_and_reports_its_storage_size_and_seconds(tmp_path):
    """O40: a file held in full is read whole, with its storage, size and seconds."""
    found = run_probe(tmp_path, held="100%")
    assert found == {**found, "size": 5000, "read": 5000, "held": "100%", "storage_id": STORAGES[0]}
    assert found["seconds"] >= 0
    assert "blocked" not in found


def test_o40_the_probe_kills_a_read_that_does_not_return_by_its_deadline(tmp_path):
    """O40: a wrong storage path makes oneclient retry an ENOENT for minutes before the application sees it; the probe
    gives up at its deadline and says so, and the start refuses (above)."""
    import os

    target = tmp_path / "hung.bin"
    os.mkfifo(target)
    found = run_probe(tmp_path, held="100%", path=target, size=10, deadline_s=1)
    assert found["blocked"] is True
    assert found["read"] is None
    assert found["seconds"] >= 1


def run_probe(tmp_path, *, held, path=None, size=5000, deadline_s=30):
    """Run the probe's source here, with `os.getxattr` and `os.stat` answering for the mount's attributes."""
    target = path or tmp_path / "check.bin"
    if path is None:
        target.write_bytes(b"x" * size)
    answers = {"org.onedata.storage_id": f'"{STORAGES[0]}"', "org.onedata.replication_progress": f'"{held}"'}
    stand_in = (
        "import os\n"
        f"_answers = {answers!r}\n"
        "os.getxattr = lambda path, name: _answers[name].encode()\n"
        "_stat = os.stat\n"
        f"os.stat = lambda path, *a, **k: type('S', (), {{'st_size': {size}}})() if path == {str(target)!r} "
        "else _stat(path, *a, **k)\n"
    )
    code = stand_in + D.probe_source(str(target), deadline_s)
    done = subprocess.run([sys.executable, "-"], input=code.encode(), capture_output=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.decode().strip().splitlines()[-1])


def test_o40_the_core_passes_the_log_switch_the_check_relies_on():
    """O40: glog buffers INFO lines for up to 30 s unless told not to (`logging.cc`, `disableLogBuffering`); the
    detection line is INFO, so the check needs every line written as it is logged."""
    assert DISABLE_LOG_BUFFERING == "--disable-log-buffering"
    assert D.LOG_FILE == "oneclient.INFO"
