"""O58: how the fault relay's client waits for its answer (`expcore_onedata.fault_relay.RelayClient`).

The relay answers into `answers/<id>.json` on the federation's shared root, from the login node, and the client waits
on a compute node. A node's NFS client keeps a negative entry for a name it looked up while the file was missing, so
a client that polls the answer's path sees nothing until the directory's attributes time out (about 30 s live, on every
verb), however soon the relay answered. The client lists `answers/` on every poll and reads the file only once the
listing shows its name. These tests model that cache (`relay_support.NegativeLookupCache`) over the real file system,
run the relay in this process (a client's pause serves it once), and end with a real relay process and a real client.

Tests marked (guard) keep a guarantee the wait must not lose, and pass on the path-polling client as well; the
others fail on it.
"""

import errno
import json
import os
import re
import subprocess
import sys
import time
import uuid

import pytest
from fault_support import FakeClock
from relay_support import SERVICE, NegativeLookupCache, Verbs, relay_of, root_of, serving

from expcore_onedata.compose import CommandResult
from expcore_onedata.fault_hpc import Request
from expcore_onedata.fault_relay import (
    FORMAT,
    POLL_S,
    RelayClient,
    RelayDown,
    beat,
    clean_environment,
    relay_dir,
)


def test_o58_an_answer_is_read_on_the_first_look_after_the_relay_wrote_it_behind_a_negative_lookup_cache(
    tmp_path, monkeypatch
):
    """O58: the request is seen and run at once and its answer is written before the client's second look; the node
    had looked up the answer's name while it was missing. The client reads the answer at that look, one pause after
    the request, not `acdirmin` later (the live median was 28.7 s on a verb the relay ran in under a second)."""
    root, verbs = root_of(tmp_path), Verbs()
    relay, clock = relay_of(root, verbs), FakeClock()
    NegativeLookupCache(monkeypatch, relay_dir(root) / "answers", clock)
    with relay.holding():
        client = RelayClient(root, wait_s=60, clock=clock, sleep=serving(relay, clock))
        began = clock()
        done = client(Request("status", SERVICE), timeout_s=10)
    assert done == verbs.answer
    assert clock() - began <= POLL_S, "the answer waited for the node's cached lookup to time out"


def test_o58_the_client_lists_the_answers_directory_on_every_look_and_looks_up_no_name_it_did_not_show(
    tmp_path, monkeypatch
):
    """O58: one listing of `answers/` per look (a fresh one: a listing kept from an earlier look would show nothing
    new), the first showing nothing and the second the answer, and no lookup by path (stat, open) of the answer's
    name before a listing has shown it, the lookup that leaves a negative entry on the node."""
    root, verbs = root_of(tmp_path), Verbs()
    relay, clock = relay_of(root, verbs), FakeClock()
    cache = NegativeLookupCache(monkeypatch, relay_dir(root) / "answers", clock)
    with relay.holding():
        client = RelayClient(root, wait_s=60, clock=clock, sleep=serving(relay, clock))
        client(Request("status", SERVICE), timeout_s=10)
    assert cache.premature == []
    first, second = cache.listings
    assert first == []
    assert len(second) == 1
    assert second[0].endswith(".json")


def test_o58_guard_only_a_file_named_exactly_by_the_request_id_is_read_as_its_answer(tmp_path):
    """O58 (guard): the answers directory also holds a draft of an answer being written, files that merely contain the
    id, and another request's answer. Each of them reads as a good answer for this request (its id, the relay's nonce,
    a return code of 7), so a client that matched by prefix, substring or content would return it; the client returns
    the relay's own answer and leaves every one of them where it is."""
    root, verbs = root_of(tmp_path), Verbs()
    relay, clock = relay_of(root, verbs), FakeClock()
    answers, paused = relay_dir(root) / "answers", []

    def pause(seconds):
        clock.sleep(seconds)
        paused.append(seconds)
        if len(paused) == 1:
            (request,) = (relay_dir(root) / "requests").glob("*.json")
            key = request.stem
            decoy = {
                "format": FORMAT,
                "id": key,
                "relay": relay.nonce,
                "returncode": 7,
                "stdout": "decoy",
                "stderr": "",
            }
            for name in (f"{key}.json.part", f".{key}.json.{'0' * 32}", f"x{key}.json", f"{uuid.uuid4().hex}.json"):
                (answers / name).write_text(json.dumps(decoy), encoding="utf-8")
            return
        relay.serve_once()

    with relay.holding():
        done = RelayClient(root, wait_s=60, clock=clock, sleep=pause)(Request("status", SERVICE), timeout_s=10)
        assert done == verbs.answer
        assert len(list(answers.iterdir())) == 4, "the client took a file that was not its answer"


def test_o58_guard_an_answer_under_the_requests_name_that_is_for_another_request_is_no_answer(tmp_path):
    """O58 (guard): the file the client waits for exists, but it answers another request (its id is not the request's):
    the client reads it as a command with no answer (`TimeoutExpired`, the outcome unknown), never as this request's
    end, and does not leave it behind."""
    root, verbs = root_of(tmp_path), Verbs()
    relay, clock = relay_of(root, verbs), FakeClock()
    directory = relay_dir(root)

    def pause(seconds):
        clock.sleep(seconds)
        (request,) = (directory / "requests").glob("*.json")
        os.rename(request, directory / "taken" / request.name)
        other = {
            "format": FORMAT,
            "id": uuid.uuid4().hex,
            "relay": relay.nonce,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
        }
        (directory / "answers" / request.name).write_text(json.dumps(other), encoding="utf-8")

    with relay.holding():
        with pytest.raises(subprocess.TimeoutExpired):
            RelayClient(root, wait_s=60, clock=clock, sleep=pause)(Request("start", SERVICE), timeout_s=10)
        assert list((directory / "answers").iterdir()) == []


@pytest.mark.parametrize("taken", [False, True])
def test_o58_a_directory_that_cannot_be_listed_is_no_answer_yet_and_the_end_is_as_before(tmp_path, monkeypatch, taken):
    """O58: a listing that fails (a stale handle, an I/O error) shows nothing, as a missing file read as
    nothing. Until the deadline the client goes on; then a request the relay never took is withdrawn and reads as not
    sent (`RelayDown`), and one it took reads as unknown (`TimeoutExpired`), never as a bare error from the listing."""
    root, verbs = root_of(tmp_path), Verbs()
    relay, clock = relay_of(root, verbs), FakeClock()
    directory, real = relay_dir(root), os.scandir
    listed = []

    def scandir(path=None):
        if path is not None and os.fspath(path) == str(directory / "answers"):
            listed.append(path)
            raise OSError(errno.ESTALE, os.strerror(errno.ESTALE))
        return real() if path is None else real(path)

    def pause(seconds):
        clock.sleep(seconds)
        if taken:
            for request in (directory / "requests").glob("*.json"):
                os.rename(request, directory / "taken" / request.name)

    with relay.holding():
        monkeypatch.setattr(os, "scandir", scandir)
        with pytest.raises(subprocess.TimeoutExpired if taken else RelayDown):
            RelayClient(root, wait_s=1, clock=clock, sleep=pause)(Request("stop", SERVICE), timeout_s=3)
        assert len(listed) > 1
        assert list((directory / "requests").iterdir()) == []


def test_o58_a_listing_that_fails_once_is_looked_at_again_and_the_answer_is_still_read(tmp_path, monkeypatch):
    """O58: one failed listing, then a good one: the answer written meanwhile is read at the good one."""
    root, verbs = root_of(tmp_path), Verbs()
    relay, clock = relay_of(root, verbs), FakeClock()
    answers, real, failed = relay_dir(root) / "answers", os.scandir, []

    def scandir(path=None):
        if path is not None and os.fspath(path) == str(answers) and not failed:
            failed.append(path)
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real() if path is None else real(path)

    with relay.holding():
        monkeypatch.setattr(os, "scandir", scandir)
        client = RelayClient(root, wait_s=60, clock=clock, sleep=serving(relay, clock))
        assert client(Request("status", SERVICE), timeout_s=10) == verbs.answer
    assert failed


def test_o58_a_real_relay_process_answers_a_real_client_through_the_listing(tmp_path):
    """O58 (guard): a relay started as `up` starts it (its own process, `-m expcore_onedata.fault_relay serve`) runs a
    verb through a real prefix (a script that prints a status) and answers a `RelayClient` in this process by its
    files. On a local file system, where no negative entry exists, this proves the whole round trip over the listing
    (the request's handoff, the run, the answer's integrity checks, the cleanup of both files) and passes on the
    path-polling client too; the cache itself is modelled by the tests above."""
    root, log = root_of(tmp_path), tmp_path / "relay.log"
    verb = tmp_path / "verb.py"
    verb.write_text(
        "import json, sys\nprint(json.dumps({'running': True, 'argv': sys.argv[1:]}))\nsys.exit(3)\n", encoding="utf-8"
    )
    prefix = [sys.executable, "-I", str(verb)]
    with log.open("wb") as sink:
        relay = subprocess.Popen(
            [sys.executable, "-B", "-m", "expcore_onedata.fault_relay", "serve", "--root", str(root), "--", *prefix],
            env=clean_environment(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
        )
    try:
        deadline = time.monotonic() + 30
        while beat(root) is None:
            assert relay.poll() is None, f"the relay ended at its start: {log.read_text()}"
            assert time.monotonic() < deadline, f"the relay never beat: {log.read_text()}"
            time.sleep(0.1)
        done = RelayClient(root, wait_s=20)(Request("status", SERVICE), timeout_s=20)
        assert isinstance(done, CommandResult)
        assert done.returncode == 3
        said = json.loads(done.stdout)
        assert said["running"] is True
        assert said["argv"][0] == "--step-tag"  # the relay's tag for the verb's steps, then the verb
        assert re.fullmatch(r"[0-9a-f]{12}", said["argv"][1])
        assert said["argv"][2:] == ["status", SERVICE]
        for name in ("requests", "taken", "answers"):
            assert list((relay_dir(root) / name).iterdir()) == []
    finally:
        relay.terminate()
        relay.wait(timeout=30)
