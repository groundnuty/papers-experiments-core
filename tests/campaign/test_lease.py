"""Leases on shared mutable resources fail safe (C19): a fake probe and a fake clock, a real kill for the file lock."""

import asyncio
import errno
import json
import logging
import os
import signal
import subprocess
import sys
import textwrap
import threading

import pytest
from lease_support import Clock, Probe

from expcore.campaign import lease as lease_module
from expcore.campaign.lease import (
    DEAD_LOG,
    WAIT_LOG,
    AllQuarantined,
    LeaseCancelled,
    LeasePool,
    LeaseTimeout,
    NoResources,
    process_gone,
    process_owner,
)
from expcore.loop.timer import AgentTimer


def pool(tmp_path, keys=("k1", "k2"), probe=None, clock=None, owner="me", **options):
    clock = clock or Clock()
    return LeasePool(
        tmp_path / "leases", lambda group: list(keys), probe or Probe(), owner=owner, clock=clock, sleep=clock.sleep,
        **options,
    )  # fmt: skip


HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from expcore.campaign.lease import LeasePool

    class Probe:
        def holder_gone(self, owner): return False
        def resource_gone(self, key, note): return False

    pool = LeasePool(Path(sys.argv[1]), lambda group: ["k1"], Probe(), owner="holder-1")
    lease = pool.acquire("g", run_key="run-a")
    print("held", flush=True)
    sys.stdin.read()
    """
)


def kill_a_holder(directory):
    """Start a process that leases key k1, then kill it without a release; return nothing once it is dead."""
    child = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(directory)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline().strip() == "held"
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        child.kill()
        child.wait()


def test_c19_a_key_is_held_by_one_lease_at_a_time(tmp_path):
    """C19: two leases never hold one key; a group of two keys serves two runs and a third finds none free."""
    leases = pool(tmp_path)
    first, second = leases.try_acquire("g", "run-a"), leases.try_acquire("g", "run-b")
    assert {first.key, second.key} == {"k1", "k2"}
    assert leases.try_acquire("g", "run-c") is None
    first.release()
    third = leases.try_acquire("g", "run-c")
    assert third.key == first.key
    assert third.end is None
    assert first.end is not None
    assert (third.record()["key"], third.record()["group"]) == (first.key, "g")


def test_c19_a_dead_holders_key_is_not_regranted_until_its_resource_is_shown_gone(tmp_path):
    """C19: a killed holder's lock is freed at once, but its sandbox may live: no new holder until it is shown gone."""
    kill_a_holder(tmp_path / "leases")
    clock = Clock()
    probe = Probe(gone_owners={"holder-1"})
    leases = pool(tmp_path, keys=("k1",), probe=probe, clock=clock)
    with pytest.raises(AllQuarantined):  # the only key is held back for its dead holder: refused, not left to wait
        leases.try_acquire("g")
    assert leases.sweep_dead_holders(["g"]) == ["k1"]  # still held back, and said so
    assert (probe.asked[0][0], probe.asked[0][1]["run_key"]) == ("k1", "run-a")
    asked = len(probe.asked)
    clock.now += 10  # inside the retry period: not asked again
    assert leases.sweep_dead_holders(["g"]) == ["k1"]
    assert len(probe.asked) == asked
    probe.gone_keys.add("k1")
    clock.now += 60  # past it: asked again, and shown gone
    lease = leases.try_acquire("g", "run-b")
    assert lease is not None
    assert lease.key == "k1"
    note = json.loads((tmp_path / "leases" / "k1.lock").read_text())
    assert note["run_key"] == "run-b"
    log = [json.loads(x) for x in (tmp_path / "leases" / DEAD_LOG).read_text().splitlines()]
    assert [e["gone"] for e in log] == [False, True]


@pytest.mark.parametrize("how", ["unknown", "raises"])
def test_c19_a_holder_whose_liveness_cannot_be_determined_is_regranted_only_after_the_resource_is_shown_gone(
    tmp_path, how
):
    """C19: an indeterminate holder probe is no proof of a live holder: the key is quarantined until resource_gone."""
    kill_a_holder(tmp_path / "leases")
    clock = Clock()
    probe = Probe(unknown_owners={"holder-1"}) if how == "unknown" else Probe(failing_owners={"holder-1"})
    leases = pool(tmp_path, keys=("k1",), probe=probe, clock=clock)
    with pytest.raises(AllQuarantined):
        leases.try_acquire("g", "run-b")
    assert [asked[0] for asked in probe.asked] == ["k1"]  # the resource was looked at, and not shown gone
    clock.now += 100
    with pytest.raises(AllQuarantined):
        leases.try_acquire("g", "run-b")
    probe.gone_keys.add("k1")
    clock.now += 100
    assert leases.try_acquire("g", "run-b").key == "k1"


@pytest.mark.parametrize("answer", ["false", "raises"])
def test_c19_a_dead_holders_key_stays_quarantined_while_the_resource_probe_is_indeterminate(tmp_path, answer):
    """C19: the holder is shown gone; resource_gone saying no, or failing, keeps the key out. Only True frees it."""
    kill_a_holder(tmp_path / "leases")
    clock = Clock()
    probe = Probe(gone_owners={"holder-1"}, failing_keys={"k1"} if answer == "raises" else ())
    leases = pool(tmp_path, keys=("k1",), probe=probe, clock=clock)
    with pytest.raises(AllQuarantined):
        leases.try_acquire("g")
    assert leases.unusable() == {"k1"}
    probe.failing_keys.clear()
    probe.gone_keys.add("k1")
    clock.now += 100
    assert leases.try_acquire("g", "run-b").key == "k1"


def test_c19_a_killed_holder_whose_owner_is_alive_by_the_probe_is_not_a_dead_holder(tmp_path):
    """C19: a note whose owner the probe still finds alive names no dead holder: the key is granted without a reap."""
    kill_a_holder(tmp_path / "leases")
    probe = Probe(gone_owners=())
    lease = pool(tmp_path, keys=("k1",), probe=probe).try_acquire("g")
    assert lease is not None
    assert probe.asked == []


def test_c19_an_unreadable_note_is_a_dead_holder_to_be_shown_gone_not_a_free_key(tmp_path):
    """C19: a holder killed mid-write leaves a note nobody can read; it is treated as a dead holder, never as free."""
    directory = tmp_path / "leases"
    directory.mkdir()
    (directory / "k1.lock").write_text('{"owner": "holder-1", "run_ke', encoding="utf-8")
    probe = Probe()
    leases = pool(tmp_path, keys=("k1",), probe=probe)
    with pytest.raises(AllQuarantined):
        leases.try_acquire("g")
    assert probe.asked == [("k1", {})]
    probe.gone_keys.add("k1")
    leases._clock.now += 100
    assert leases.try_acquire("g") is not None


def test_c19_an_empty_note_in_a_lock_file_that_was_there_is_indeterminate_and_a_new_file_is_a_key_never_used(tmp_path):
    """C19: a holder can die between taking the key and writing its note: that is not "never used", it needs proof."""
    directory = tmp_path / "leases"
    directory.mkdir()
    (directory / "k1.lock").write_text("", encoding="utf-8")  # the lock file of a holder that died before its note
    probe = Probe()
    clock = Clock()
    leases = pool(tmp_path, keys=("k1", "k2"), probe=probe, clock=clock)
    first = leases.try_acquire("g", "run-a")
    assert first.key == "k2"  # k2 had no file: this acquisition created it, so it was never used and needs no proof
    assert probe.asked == [("k1", {})]  # k1's empty note was looked into, and the resource was not shown gone
    assert leases.unusable() == {"k1"}
    assert leases.sweep_dead_holders(["g"]) == ["k1"]
    probe.gone_keys.add("k1")
    clock.now += 100
    assert leases.try_acquire("g", "run-b").key == "k1"


class FullDisk:
    """A lock file whose writes fail with ENOSPC: every other call goes to the real file."""

    def __init__(self, real):
        self._real = real

    def write(self, text):
        raise OSError(errno.ENOSPC, "No space left on device")

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_c19_a_note_is_written_with_flush_and_fsync_when_a_key_is_taken_and_when_it_is_released(tmp_path, monkeypatch):
    """C19: the note is recovery state, so it reaches the disk: an fsync of the lock file follows each write."""
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(lease_module.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    leases = pool(tmp_path, keys=("k1",))
    leases.try_acquire("g", "run-a").release()  # the first use creates the file, which is synced too
    synced.clear()
    lease = leases.try_acquire("g", "run-a")
    assert len(synced) == 1
    assert json.loads((tmp_path / "leases" / "k1.lock").read_text())["run_key"] == "run-a"
    lease.release()
    assert len(synced) == 2
    assert "released" in json.loads((tmp_path / "leases" / "k1.lock").read_text())


def test_c19_a_note_that_cannot_be_written_fails_the_acquisition_and_releases_the_key(tmp_path, monkeypatch):
    """C19: ENOSPC on the write of the note: the key is released, nothing is granted, the error is raised, and the
    empty note it leaves is indeterminate for the next holder, never a key never used."""
    leases = pool(tmp_path, keys=("k1",))
    leases.dir.mkdir(parents=True)
    (leases.dir / "k1.lock").write_text(json.dumps({"released": 1.0, "key": "k1"}), encoding="utf-8")
    real_open = open
    monkeypatch.setattr(lease_module, "open", lambda *a, **k: FullDisk(real_open(*a, **k)), raising=False)
    with pytest.raises(OSError, match="No space left") as raised:
        leases.try_acquire("g", "run-a")
    assert raised.value.errno == errno.ENOSPC
    monkeypatch.undo()
    handle = leases._flock("k1")
    assert handle is not None  # the lock was dropped with the failed acquisition
    handle.close()
    assert (leases.dir / "k1.lock").read_text() == ""
    probe = Probe()
    again = pool(tmp_path, keys=("k1",), probe=probe)
    with pytest.raises(AllQuarantined):  # the file is empty but was there: its holder is unknown, so a proof is needed
        again.try_acquire("g", "run-b")
    assert probe.asked == [("k1", {})]
    probe.gone_keys.add("k1")
    again._clock.now += 100
    assert again.try_acquire("g", "run-b") is not None


def test_c19_a_note_that_cannot_be_synced_fails_the_acquisition_too(tmp_path, monkeypatch):
    """C19: a write that reached the page cache but failed at fsync is a note that was not written."""

    def failing(fd):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(lease_module.os, "fsync", failing)
    leases = pool(tmp_path, keys=("k1", "k2"))
    with pytest.raises(OSError, match="Input/output"):
        leases.acquire("g", run_key="run-a")
    monkeypatch.undo()
    assert leases._flock("k1") is not None  # the key it had taken is free to lock, not held by a lease nobody has
    assert leases.unusable() == set()


def test_c19_a_release_whose_note_cannot_be_written_still_frees_the_key_and_says_so(tmp_path, monkeypatch, caplog):
    """C19: the lock is dropped even when the note of the release is not written; the failure is logged."""
    leases = pool(tmp_path, keys=("k1",))
    lease = leases.try_acquire("g", "run-a")
    real_open_handle = lease._handle
    monkeypatch.setattr(lease, "_handle", FullDisk(real_open_handle))
    with caplog.at_level(logging.ERROR, logger="expcore.campaign.lease"):
        lease.release()
    assert not lease.held
    assert "could not note the release" in caplog.text
    monkeypatch.undo()
    assert leases._flock("k1") is not None


def test_c19_runs_that_first_use_the_same_keys_at_once_never_mistake_a_file_being_created_for_a_dead_holder(tmp_path):
    """C19: a lock file appears complete, so the run that opens it a moment after its creator never reads it empty."""
    keys = tuple(f"k{i}" for i in range(8))
    for _round in range(5):
        directory = tmp_path / f"round{_round}"
        probe = Probe()  # no resource is ever shown gone: a key misread as a dead holder's would stay out
        leases = LeasePool(directory, lambda group: list(keys), probe, owner="me")
        barrier = threading.Barrier(8)
        got = []

        def take(leases=leases, barrier=barrier, got=got):
            barrier.wait()
            got.append(leases.try_acquire("g", "run"))

        threads = [threading.Thread(target=take) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert all(lease is not None for lease in got)
        assert sorted(lease.key for lease in got) == sorted(keys)
        assert probe.asked == []
        assert leases.unusable() == set()
        assert not [p for p in directory.iterdir() if p.name.endswith(".new")]  # no scratch file is left


def test_c19_an_empty_or_released_note_needs_no_proof(tmp_path):
    """C19: a key never used, and one its last holder released, go to the next run without asking the probe."""
    probe = Probe()
    leases = pool(tmp_path, keys=("k1",), probe=probe)
    first = leases.try_acquire("g")
    first.release()
    assert leases.try_acquire("g") is not None
    assert probe.asked == []


def test_c19_an_unproven_teardown_quarantines_the_key_until_it_is_proven(tmp_path):
    """C19: a quarantined lease stays held and ends its use; its key goes to nobody until the teardown is proven."""
    clock = Clock()
    leases = pool(tmp_path, keys=("k1", "k2"), clock=clock, quarantine_retry_s=60)
    proven = {"yes": False}
    lease = leases.try_acquire("g", "run-a")
    leases.quarantine(lease, lambda: proven["yes"])
    assert lease.quarantined
    assert lease.end is not None
    assert lease.held
    lease.release()  # a release does not undo a quarantine
    assert lease.held
    assert leases.held_back() == 1
    other = leases.try_acquire("g", "run-b")
    assert other.key != lease.key  # the quarantined key is not offered
    assert leases.settle_quarantines() == []  # not due yet
    clock.now += 61
    assert leases.settle_quarantines() == []  # due, not proven: it stays
    proven["yes"] = True
    clock.now += 61
    assert leases.settle_quarantines() == [lease.key]
    assert not lease.held
    assert leases.held_back() == 0
    other.release()
    assert leases.try_acquire("g", "run-c") is not None


def test_c19_a_proof_that_raises_is_not_a_proof(tmp_path):
    """C19: a failing check of the teardown leaves the key quarantined."""
    clock = Clock()
    leases = pool(tmp_path, keys=("k1",), clock=clock, quarantine_retry_s=1)

    def broken():
        raise OSError("cannot look")

    leases.quarantine(leases.try_acquire("g"), broken)
    clock.now += 5
    assert leases.settle_quarantines() == []
    assert leases.held_back() == 1


def test_c19_with_every_key_quarantined_the_run_is_refused_before_it_waits(tmp_path):
    """C19: no usable key means AllQuarantined at once, not a wait for a proof that may never come."""
    clock = Clock()
    leases = pool(tmp_path, keys=("k1", "k2"), clock=clock)
    for _key in ("k1", "k2"):
        leases.quarantine(leases.try_acquire("g"), lambda: False)
    with pytest.raises(AllQuarantined):
        leases.try_acquire("g", "run-c")
    with pytest.raises(AllQuarantined):
        leases.acquire("g", run_key="run-c")
    assert clock.sleeps == 0


def test_c19_a_key_held_back_for_a_dead_holder_counts_as_quarantined(tmp_path):
    """C19: when the only key waits for its dead holder's proof, the run is refused rather than left to wait."""
    kill_a_holder(tmp_path / "leases")
    leases = pool(tmp_path, keys=("k1",), probe=Probe(gone_owners={"holder-1"}))
    assert leases.sweep_dead_holders(["g"]) == ["k1"]
    with pytest.raises(AllQuarantined):
        leases.try_acquire("g")


def test_c19_a_held_back_key_does_not_refuse_a_run_while_another_key_is_only_busy(tmp_path):
    """C19: refusal needs every key unusable; with one key merely busy the run waits for it as for any contention."""
    kill_a_holder(tmp_path / "leases")  # leaves k1 held back for its dead holder
    leases = pool(tmp_path, keys=("k1", "k2"), probe=Probe(gone_owners={"holder-1"}))
    busy = leases.try_acquire("g", "run-a")  # k1 is held back, so this is k2
    assert busy.key == "k2"
    assert leases.try_acquire("g", "run-b") is None
    busy.release()
    assert leases.try_acquire("g", "run-b").key == "k2"


def test_c19_a_group_without_keys_is_refused(tmp_path):
    """C19: a platform that declares no key for a group leaves the run unstarted."""
    with pytest.raises(NoResources):
        pool(tmp_path, keys=()).try_acquire("g")


def test_c19_a_key_that_is_not_a_file_name_is_refused(tmp_path):
    """C19: a key names a lock file, so it cannot climb out of the lease directory."""
    with pytest.raises(ValueError, match="lease key"):
        pool(tmp_path, keys=("../escape",)).try_acquire("g")


def test_c19_a_wait_is_logged_and_a_timeout_is_a_refusal(tmp_path):
    """C19: with no free key the caller waits and the wait is logged with its run; past the timeout it is refused."""
    clock = Clock()
    leases = pool(tmp_path, keys=("k1",), clock=clock, poll_s=2.0)
    holder = leases.try_acquire("g", "run-a")
    clock.on_sleep = lambda: holder.release() if clock.sleeps == 3 else None
    got = leases.acquire("g", run_key="run-b", timeout_s=60)
    assert got.run_key == "run-b"
    assert got.wait_s == pytest.approx(6.0)
    line = json.loads((tmp_path / "leases" / WAIT_LOG).read_text().splitlines()[0])
    assert (line["run_key"], line["group"], line["wait_s"]) == ("run-b", "g", 6.0)
    clock.on_sleep = None
    with pytest.raises(LeaseTimeout):
        leases.acquire("g", run_key="run-c", timeout_s=5)


def wait_lines(tmp_path):
    return [json.loads(x) for x in (tmp_path / "leases" / WAIT_LOG).read_text().splitlines()]


def test_c19_the_wait_is_recorded_on_the_lease_and_in_the_log_when_a_key_is_granted(tmp_path):
    """C19: the measured wait is in Lease.record() and in waits.jsonl with the outcome and the key it ended with."""
    clock = Clock()
    leases = pool(tmp_path, keys=("k1",), clock=clock, poll_s=2.0)
    holder = leases.try_acquire("g", "run-a")
    assert holder.record()["wait_s"] == 0.0
    clock.on_sleep = lambda: holder.release() if clock.sleeps == 3 else None
    got = leases.acquire("g", run_key="run-b")
    assert got.record()["wait_s"] == pytest.approx(6.0)
    [line] = wait_lines(tmp_path)
    assert (line["outcome"], line["key"], line["wait_s"], line["run_key"]) == ("granted", "k1", 6.0, "run-b")


def test_c19_the_wait_before_a_timeout_is_recorded_on_the_refusal_and_in_the_log(tmp_path):
    """C19: a run that waited and was then refused did not lose its queue time: the refusal and waits.jsonl carry it."""
    clock = Clock()
    leases = pool(tmp_path, keys=("k1",), clock=clock, poll_s=2.0)
    holder = leases.try_acquire("g", "run-a")
    with pytest.raises(LeaseTimeout) as refused:
        leases.acquire("g", run_key="run-b", timeout_s=10)
    assert refused.value.wait_s == pytest.approx(10.0)
    [line] = wait_lines(tmp_path)
    assert (line["outcome"], line["key"], line["wait_s"], line["run_key"]) == ("timeout", None, 10.0, "run-b")
    assert holder.held


def test_c19_the_wait_before_an_all_quarantined_refusal_is_recorded_too(tmp_path):
    """C19: keys that were merely busy while the run waited, then all quarantined, leave the wait on the refusal."""
    clock = Clock()
    leases = pool(tmp_path, keys=("k1", "k2"), clock=clock, poll_s=2.0)
    first, second = leases.try_acquire("g", "run-a"), leases.try_acquire("g", "run-b")

    def both_unproven():
        if clock.sleeps == 2:
            leases.quarantine(first, lambda: False)
            leases.quarantine(second, lambda: False)

    clock.on_sleep = both_unproven
    with pytest.raises(AllQuarantined) as refused:
        leases.acquire("g", run_key="run-c")
    assert refused.value.wait_s == pytest.approx(4.0)
    [line] = wait_lines(tmp_path)
    assert (line["outcome"], line["wait_s"], line["run_key"]) == ("all_quarantined", 4.0, "run-c")
    with pytest.raises(AllQuarantined) as at_once:  # refused before any wait: a line of 0 s, not a gap in the log
        leases.acquire("g", run_key="run-d")
    assert at_once.value.wait_s == 0.0
    assert wait_lines(tmp_path)[-1]["wait_s"] == 0.0


def test_c19_a_wait_for_a_key_ends_when_the_callers_campaign_is_ending_and_the_wait_is_recorded(tmp_path):
    """C19: the cancel event ends a wait that may last minutes, with a refusal that carries the seconds waited."""
    clock = Clock()
    leases = pool(tmp_path, keys=("k1",), clock=clock, poll_s=2.0)
    holder = leases.try_acquire("g", "run-a")
    ending = threading.Event()
    clock.on_sleep = lambda: ending.set() if clock.sleeps == 2 else None
    with pytest.raises(LeaseCancelled) as refused:
        leases.acquire("g", run_key="run-b", cancel=ending)
    assert refused.value.wait_s == pytest.approx(4.0)
    assert wait_lines(tmp_path)[-1]["outcome"] == "cancelled"
    assert holder.held


def test_c19_a_group_without_a_key_is_logged_as_a_refusal_without_a_wait(tmp_path):
    """C19: even the refusal that cannot wait leaves its line, so the log lists every run that was not started."""
    leases = pool(tmp_path, keys=())
    with pytest.raises(NoResources) as refused:
        leases.acquire("g", run_key="run-a")
    assert refused.value.wait_s == 0.0
    assert wait_lines(tmp_path)[0]["outcome"] == "no_resources"


def test_c19_waiting_for_a_lease_is_a_lease_queue_wait_on_the_timer_and_not_the_agents_time(tmp_path):
    """C19: a wait for a lease taken under a started timer is marked lease_queue, so the agent's clock does not run."""
    clock = Clock()
    leases = pool(tmp_path, keys=("k1",), clock=clock, poll_s=2.0)
    holder = leases.try_acquire("g", "run-a")
    clock.on_sleep = lambda: holder.release() if clock.sleeps == 2 else None
    timer = AgentTimer(600, clock=clock)
    timer.start()

    async def go():
        lease = await leases.acquire_under(timer, "g", run_key="run-b")
        return lease, timer.check("after")

    lease, tick = asyncio.run(go())
    assert lease.key == "k1"
    assert timer.infra_wait_by_kind == {"lease_queue": pytest.approx(4.0)}
    assert tick.agent_s == pytest.approx(0.0)
    assert tick.elapsed_s == pytest.approx(4.0)
    assert [label for label, _ in timer.readings if label.startswith("wait_")] == [
        "wait_start:lease_queue",
        "wait_end:lease_queue",
    ]


def test_c19_the_owner_of_this_process_is_not_gone_and_a_reused_pid_is(tmp_path):
    """C19: the owner names boot, pid and start time, so a reused pid is no holder; what cannot be read is not gone."""
    owner = process_owner()
    assert process_gone(owner) is False
    boot, pid, started = owner.split(":")
    assert process_gone(f"{boot}:{pid}:{int(started) + 1}") is True
    assert process_gone("another-boot:1:1") is True
    assert process_gone("not an owner") is None  # what cannot be read is not shown gone, and not shown alive
    assert process_gone("pid:not-a-number") is None
    assert process_gone("a:b:c:d") is None


def test_c19_a_missing_proc_entry_is_a_process_that_is_gone_and_its_teardown_still_needs_proof(tmp_path):
    """C19: no /proc/<pid> on this boot (FileNotFoundError) means the process is gone, and that alone frees nothing."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    boot = process_owner().split(":")[0]
    assert process_gone(f"{boot}:{dead.pid}:1") is True
    assert process_gone(f"pid:{dead.pid}") is True
    directory = tmp_path / "leases"
    directory.mkdir()
    note = {"owner": f"{boot}:{dead.pid}:1", "pid": dead.pid, "run_key": "run-a", "key": "k1"}
    (directory / "k1.lock").write_text(json.dumps(note), encoding="utf-8")

    class ByProcess(Probe):
        def holder_gone(self, owner):
            return process_gone(owner)

    probe = ByProcess()
    leases = pool(tmp_path, keys=("k1",), probe=probe)
    with pytest.raises(AllQuarantined):  # the holder is gone; its sandbox is not shown gone, so the key stays out
        leases.try_acquire("g")
    assert probe.asked == [("k1", note)]
    probe.gone_keys.add("k1")
    leases._clock.now += 100
    assert leases.try_acquire("g", "run-b") is not None


def test_c19_a_lease_is_freed_after_its_run_only_when_the_probe_shows_the_resource_gone(tmp_path):
    """C19: an unproven teardown quarantines the key at the end of the run; a proof frees it later."""
    clock = Clock()
    probe = Probe()
    leases = pool(tmp_path, keys=("k1",), probe=probe, clock=clock, quarantine_retry_s=60)
    lease = leases.try_acquire("g", "run-a")
    assert leases.finish(lease) is False  # the probe cannot show it gone
    assert lease.quarantined
    assert leases.held_back() == 1
    assert probe.asked[-1][0] == "k1"
    assert probe.asked[-1][1]["run_key"] == "run-a"
    with pytest.raises(AllQuarantined):
        leases.try_acquire("g", "run-b")
    probe.gone_keys.add("k1")
    clock.now += 61
    assert leases.settle_quarantines() == ["k1"]
    assert leases.try_acquire("g", "run-b") is not None


def test_c19_a_lease_whose_resource_is_shown_gone_is_released_at_once(tmp_path):
    """C19: the probe shows the teardown done, so the key is free for the next run."""
    leases = pool(tmp_path, keys=("k1",), probe=Probe(gone_keys={"k1"}))
    lease = leases.try_acquire("g", "run-a")
    assert leases.finish(lease) is True
    assert not lease.held
    assert leases.try_acquire("g", "run-b") is not None


def test_c19_a_probe_that_raises_while_a_run_ends_leaves_the_key_quarantined(tmp_path):
    """C19: a failed look is not a proof."""

    class Broken(Probe):
        def resource_gone(self, key, note):
            raise OSError("cannot look")

    leases = pool(tmp_path, keys=("k1",), probe=Broken())
    assert leases.finish(leases.try_acquire("g", "run-a")) is False
    assert leases.held_back() == 1
