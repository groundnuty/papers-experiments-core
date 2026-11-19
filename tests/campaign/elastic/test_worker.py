"""C44: the node worker: one lease at a time in lease-id order, heartbeats while it runs, a completion or a return at
its end, and a replayed lease, another federation's or a tombstone never run."""

import hashlib
import json
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path

import pytest
from channel_support import FakeHandle, FakeRunner, FakeSsh, channel_of, outcome_of, site_of, transport
from elastic_support import SITE, assert_invariants, join, make, plan
from fake_ssh_bin import install_fake_ssh
from hpc_fleet_support import fleet_modules
from leased_support import LABEL, InProcess, leased_example, ran_for_real
from merge_by_run_support import make_running
from study_support import Campaign, clean_environment

from expcore.campaign import out_snapshot
from expcore.campaign.elastic import channel as ch
from expcore.campaign.elastic.control import Settle, SiteSettings
from expcore.campaign.elastic.dispatch import TICK_S, Dispatcher
from expcore.campaign.elastic.records import Lease
from expcore.campaign.elastic.worker import Occupied, ProcessRunner, Worker, WorkerError, main
from expcore.campaign.run_lease import LEASE_JOURNAL, read_lease

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


def a_lease(lease_id: int, run_key: str = "task=task-0|model=m1|condition=api-none") -> Lease:
    """A ledger lease of fed-1 for the tests that write the inbox by hand."""
    return Lease(
        lease_id=lease_id,
        run_key=run_key,
        model="m1",
        condition="api-none",
        federation=LABEL,
        site=SITE,
        fingerprint="a" * 64,
        attempt_base=0,
        grant_micro=2_000_000,
        issued_at=time.time(),
        renewed_at=time.time(),
        counter=None,
        hard_term_at=time.time() + 3600,
        state="issued",
        slot=True,
        charge_micro=2_000_000,
        verdict=None,
        completion_state=None,
        proven=False,
    )


def deliver(channel: Path, lease: Lease) -> None:
    ch.make_dirs(channel)
    assert ch.create_json(channel / ch.INBOX / ch.lease_name(lease.lease_id), ch.envelope(lease))


def worker_on(tmp_path: Path, runner=None, **kwargs) -> tuple[Worker, Path]:
    channel = tmp_path / "channels" / LABEL
    study = tmp_path / "study"
    study.mkdir(exist_ok=True)
    (study / "study.toml").write_text("# the study\n", encoding="utf-8")
    kwargs.setdefault("outcome", outcome_of())
    worker = Worker(LABEL, channel, study, tmp_path / "work", runner or FakeRunner(), beat_s=30.0, **kwargs)
    worker.open()
    return worker, channel


def outbox(channel: Path, name: str) -> dict | None:
    try:
        return json.loads((channel / ch.OUTBOX / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def test_c44_the_worker_runs_its_leases_one_at_a_time_in_lease_order_and_beats_while_each_runs(tmp_path):
    """C44: two leases in the inbox run one after the other, the lower id first, never at once; the heartbeat's
    counter starts at 1 when a lease is taken and advances every beat while its run lives; each ends in a completion
    naming the lease, its run, the store, the archive's hash, the state, the charged attempts and the spend."""
    beats: list[tuple[int, int]] = []
    runner = FakeRunner(make=lambda study, lease_file: FakeHandle(beats=3))
    worker, channel = worker_on(tmp_path, runner)
    real_beat = worker._beat
    worker._beat = lambda lease_id, counter: (beats.append((lease_id, counter)), real_beat(lease_id, counter))
    deliver(channel, a_lease(5))
    deliver(channel, a_lease(3))
    assert worker.step() == "ran"
    assert worker.step() == "ran"
    assert worker.step() == "idle"
    assert runner.started == [3, 5]
    assert not runner.overlapped
    assert beats == [(3, 1), (3, 2), (3, 3), (3, 4), (5, 1), (5, 2), (5, 3), (5, 4)]
    done = outbox(channel, ch.completion_name(5))
    archive = channel / ch.ARCHIVES / ch.archive_name(5)
    assert done["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert (done["lease_id"], done["run_key"], done["federation"]) == (5, a_lease(5).run_key, LABEL)
    assert (done["state"], done["attempts"], done["attempts_started"], done["charged"]) == ("done", 1, 1, 0)
    assert (done["spend_micro_usd"], done["spend_usd"]) == (500_000, 0.5)
    assert done["store"].endswith("lease-000000000005/study/out")
    with tarfile.open(archive) as tar:
        assert "out/state.sqlite" in tar.getnames()
    assert worker.last() == 5
    assert not (channel / ch.WORKER / "running.json").exists()


def test_c44_a_lease_id_not_above_the_last_one_taken_is_refused_and_nothing_is_written_for_it(tmp_path):
    """C44: after lease 7, a lease 6 that lands late is a replay: it is refused, never run, and nothing goes to the
    outbox for it; a new worker on the channel (a restart) neither runs 6 nor runs 7 again."""
    runner = FakeRunner()
    worker, channel = worker_on(tmp_path, runner)
    deliver(channel, a_lease(7))
    assert worker.step() == "ran"
    deliver(channel, a_lease(6))
    assert worker.step() == "idle"
    assert runner.started == [7]
    assert outbox(channel, ch.completion_name(6)) is None
    assert outbox(channel, ch.return_name(6)) is None
    assert any("lease 6" in refusal and "not above 7" in refusal for refusal in worker.refusals)
    again, _ = worker_on(tmp_path, runner)
    assert again.step() == "idle"
    assert runner.started == [7]


def test_c44_a_lease_file_for_another_federation_or_a_tombstone_is_never_run(tmp_path):
    """C44: a lease whose file names another federation is refused and not taken; a tombstone holds a cancelled
    lease's name and is passed over; the next lease of this federation still runs."""
    runner = FakeRunner()
    worker, channel = worker_on(tmp_path, runner)
    stray = ch.envelope(a_lease(2)) | {"federation": "fed-2"}
    assert ch.create_json(channel / ch.INBOX / ch.lease_name(2), stray)
    assert ch.create_json(channel / ch.INBOX / ch.lease_name(3), ch.tombstone(LABEL, SITE, 3))
    assert worker.step() == "idle"
    assert runner.started == []
    assert worker.last() == 0
    deliver(channel, a_lease(4))
    assert worker.step() == "ran"
    assert runner.started == [4]


def test_c44_a_second_worker_does_not_start_on_a_channel_where_a_lease_runs(tmp_path):
    """C44: `running.json` marks a lease in progress: no other worker starts on the channel, and one that finds it
    created between its look and its take runs nothing."""
    worker, channel = worker_on(tmp_path)
    ch.write_json(channel / ch.WORKER / "running.json", {"lease_id": 1})
    with pytest.raises(Occupied):
        worker.open()
    deliver(channel, a_lease(1))
    assert worker.step() == "busy"


def test_c44_a_lease_that_began_no_attempt_is_returned_with_what_its_store_spent(tmp_path):
    """C44: a lease held before any attempt (a grant below the reservation, a closed gate) is returned with the
    store's spend, and one the kernel refused before making a store is returned with nothing spent."""
    worker, channel = worker_on(tmp_path, outcome=outcome_of(attempts=0, charged=0, state=None, spend_micro_usd=20_000))
    deliver(channel, a_lease(1))
    assert worker.step() == "returned"
    returned = outbox(channel, ch.return_name(1))
    assert (returned["lease_id"], returned["spend_micro_usd"], returned["run_key"]) == (1, 20_000, a_lease(1).run_key)
    assert outbox(channel, ch.completion_name(1)) is None
    worker.outcome = lambda study, lease: None
    deliver(channel, a_lease(2))
    assert worker.step() == "returned"
    assert outbox(channel, ch.return_name(2))["spend_micro_usd"] == 0


def test_c44_a_run_that_ends_with_an_attempt_still_running_stops_the_worker_unreported(tmp_path):
    """C44: an attempt left `running` is no end the ledger can count: the worker writes no completion or return,
    stops, and leaves `running.json`, so no other lease runs on the federation and the ledger expires the lease."""
    worker, channel = worker_on(tmp_path, outcome=outcome_of(state="running"))
    deliver(channel, a_lease(1))
    with pytest.raises(WorkerError):
        worker.step()
    assert outbox(channel, ch.completion_name(1)) is None
    assert outbox(channel, ch.return_name(1)) is None
    with pytest.raises(Occupied):
        worker.open()


def test_c44_a_drain_returns_the_lease_not_taken_and_the_worker_exits(tmp_path):
    """C44: with a drain asked, a lease not yet taken is returned with nothing spent and never run, the worker writes
    `exited.json` and its command exits 0."""
    runner = FakeRunner()
    worker, channel = worker_on(tmp_path, runner)
    deliver(channel, a_lease(1))
    ch.create_json(channel / ch.INBOX / ch.DRAIN, {"format": ch.DRAIN_FORMAT})
    assert worker.step() == "returned"
    assert worker.serve(sleep=lambda s: None) == 0
    assert runner.started == []
    assert outbox(channel, ch.return_name(1))["reason"] == "draining"
    assert outbox(channel, ch.EXITED)["last_lease_id"] == 1


def test_c44_the_worker_command_refuses_a_channel_where_a_lease_runs(tmp_path, capsys):
    """C44: `python -m expcore.campaign.elastic.worker` exits 1 on an occupied channel, saying why."""
    channel = tmp_path / "channels" / LABEL
    ch.make_dirs(channel)
    ch.write_json(channel / ch.WORKER / "running.json", {"lease_id": 1})
    study, work = str(tmp_path / "study"), str(tmp_path / "work")
    argv = ["--federation", LABEL, "--channel", str(channel), "--study", study, "--work", work]
    assert main(argv) == 1
    assert "takes no other lease" in capsys.readouterr().err


def test_c44_the_lease_file_the_channel_writes_is_the_one_the_kernel_reads(tmp_path):
    """C44: the inbox's kernel lease, written out by the worker, is read by `run_lease.read_lease` with the ledger's
    facts: the run, the id, its base, its grant in micro-dollars, the campaign's hash and the hard term."""
    ledger, clock = make(tmp_path, max_usd=10.0, grants_usd={"m1": 1.25})
    join(ledger, clock, LABEL)
    lease = ledger.issue(LABEL)
    path = tmp_path / "lease.json"
    ch.write_json(path, ch.envelope(lease)["lease"])
    read = read_lease(path)
    assert (read.run_key, read.lease_id, read.attempt_base) == (lease.run_key, 1, 0)
    assert (read.grant_micro_usd, read.campaign_sha256) == (1_250_000, ledger.config.fingerprint)
    assert read.expiry.timestamp() == int(lease.hard_term_at)


def test_c44_a_real_lease_goes_from_the_ledger_through_the_channel_and_the_kernel_to_a_counted_run(
    tmp_path, clean_environment
):
    """C44: end to end on the example study: the dispatcher leases a run to a federation over the fake ssh, the
    worker runs the kernel on it in a copy of the study, its completion and archive come back on the next tick, and
    the ledger counts the run done with the store's charged attempt and measured spend."""
    study, _, ledger, site, dispatch, lease, clock_now = leased_example(tmp_path)
    runner = InProcess(clean_environment)
    worker = Worker(LABEL, channel_of(site, LABEL), study, tmp_path / "work", runner)
    worker.open()
    assert worker.step() == "ran", runner.codes
    assert runner.codes[0][0] == 0, runner.codes
    clock_now[0] += TICK_S
    dispatch.tick()
    assert ledger.lease(lease.lease_id).state == "counted"
    assert ledger.run(lease.run_key).state == "done"
    completion = json.loads((channel_of(site, LABEL) / ch.OUTBOX / ch.completion_name(lease.lease_id)).read_text())
    # A done attempt counts against no attempt limit (`attempts.classify`): one attempt, none charged.
    assert (completion["state"], completion["attempts"], completion["attempts_started"], completion["charged"]) == (
        "done",
        1,
        1,
        0,
    )
    assert completion["spend_known"]
    assert 0 < completion["spend_micro_usd"] <= 5_000_000
    assert ledger.lease(lease.lease_id).charge_micro == completion["spend_micro_usd"]
    assert ledger.lease(lease.lease_id).charge_micro > 0
    assert_invariants(ledger)


def test_c48_a_reply_under_no_declared_fingerprint_sets_its_run_aside_and_pauses_its_model_through_the_worker(
    tmp_path, clean_environment
):
    """C48: end to end on the example, whose stub replies carry no fingerprint, with "none" not declared: the kernel
    judges the run by its lease's set and sets it aside, its store holds the model after that one attempt (the run
    keeps its other attempts), the completion names the reply with none, and the dispatcher's next tick pauses the
    model in the ledger while the run goes back to pending."""
    study, _, ledger, site, dispatch, lease, clock_now = leased_example(tmp_path, none=False)
    assert lease.declared is not None
    assert not lease.declared.none
    runner = InProcess(clean_environment)
    worker = Worker(LABEL, channel_of(site, LABEL), study, tmp_path / "work", runner)
    worker.open()
    assert worker.step() == "ran", runner.codes
    completion = json.loads((channel_of(site, LABEL) / ch.OUTBOX / ch.completion_name(lease.lease_id)).read_text())
    assert (completion["state"], completion["attempts"], completion["charged"]) == ("infra_fail", 1, 1)
    assert completion["undeclared_fingerprints"] == [None]
    clock_now[0] += TICK_S
    dispatch.tick()
    assert ledger.lease(lease.lease_id).state == "counted"
    assert ledger.run(lease.run_key).state == "pending"
    assert ledger.paused() == {lease.model: (None,)}
    assert_invariants(ledger)


def test_c44_a_second_run_of_the_same_worker_never_takes_the_ledgers_settled_lease_again(tmp_path):
    """C44: through the dispatcher, a federation's worker runs its lease, the ledger leases it the next run, and
    the worker takes that one only: the first is never run twice."""
    ledger, clock = make(tmp_path, runs=plan(tasks=1))
    join(ledger, clock, LABEL)
    ssh, site = FakeSsh(), site_of(tmp_path)
    dispatch = Dispatcher(ledger, [site], transport(ssh), tmp_path / "out" / "leases")
    runner = FakeRunner()
    study = tmp_path / "study"
    study.mkdir()
    worker = Worker(LABEL, channel_of(site, LABEL), study, tmp_path / "work", runner, outcome=outcome_of())
    for _ in range(6):
        clock.advance(TICK_S)
        dispatch.tick()
        ch.make_dirs(channel_of(site, LABEL))
        while worker.step() == "ran":
            pass
    assert runner.started == [1, 2]
    assert [run.state for run in ledger.runs()] == ["done", "done"]
    assert_invariants(ledger)


def test_c44_a_heartbeat_that_cannot_be_written_never_abandons_the_run_it_beats_for(tmp_path, monkeypatch):
    """C44: a beat whose write fails is a beat missed: the run goes on and its completion is still written."""
    runner = FakeRunner(make=lambda study, lease_file: FakeHandle(beats=2))
    worker, channel = worker_on(tmp_path, runner)
    deliver(channel, a_lease(1))
    real = ch.write_json

    def write_json(path, record):
        if path.name == ch.HEARTBEAT:
            raise OSError(5, "Input/output error")
        return real(path, record)

    monkeypatch.setattr(ch, "write_json", write_json)
    assert worker.step() == "ran"
    assert outbox(channel, ch.completion_name(1))["state"] == "done"


def test_c44_a_state_that_cannot_be_read_never_lets_an_ended_lease_run_again(tmp_path):
    """C44: after lease 7 ended, a state file that is listed and cannot be read is no answer (the poll is retried),
    and a channel whose state is gone still does not run lease 7 again: its end is in the outbox."""
    runner = FakeRunner()
    worker, channel = worker_on(tmp_path, runner)
    deliver(channel, a_lease(7))
    assert worker.step() == "ran"
    state = channel / ch.WORKER / "state.json"
    state.write_text('{"format": "expcore-elastic-wor', encoding="utf-8")
    with pytest.raises(OSError, match="could not be read"):
        worker.step()
    state.unlink()
    assert worker.step() == "idle"
    assert runner.started == [7]


def test_c44_relative_directories_are_taken_as_absolute_and_a_work_directory_inside_the_study_is_refused(
    tmp_path, monkeypatch
):
    """C44: the run's working directory is its lease directory, so the worker holds its directories as absolute
    paths; and a work directory inside the study would copy itself, so the worker refuses it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "study").mkdir()
    worker = Worker(LABEL, Path("channels") / LABEL, Path("study"), Path("work"), FakeRunner(), outcome=outcome_of())
    assert (worker.channel, worker.study, worker.work) == (
        tmp_path / "channels" / LABEL,
        tmp_path / "study",
        tmp_path / "work",
    )
    inside = Worker(
        LABEL, tmp_path / "channels" / LABEL, tmp_path / "study", tmp_path / "study" / "scratch", FakeRunner()
    )
    with pytest.raises(WorkerError):
        inside.open()


def test_c44_a_take_whose_state_landed_before_its_fsync_failed_still_runs_the_lease(tmp_path, monkeypatch):
    """C44: the state's rename landed and the fsync of its directory failed: the state shows the lease as taken, so
    the worker runs it (a later poll would pass it over as taken), and its completion is written."""
    runner = FakeRunner()
    worker, channel = worker_on(tmp_path, runner)
    deliver(channel, a_lease(1))
    real, failed = ch.fsync_dir, []

    def fsync_dir(directory):
        if directory == channel / ch.WORKER and (directory / "state.json").exists() and not failed:
            failed.append(directory)
            raise OSError(5, "Input/output error")
        return real(directory)

    monkeypatch.setattr(ch, "fsync_dir", fsync_dir)
    assert worker.step() == "ran"
    assert failed
    assert runner.started == [1]
    assert outbox(channel, ch.completion_name(1))["state"] == "done"


def test_a_lease_run_has_its_spend_journal_where_the_worker_puts_it(tmp_path):
    """The real runner starts `<prefix> <study copy> --lease <file> --spend-journal <journal>`, the journal the
    worker names (its lease's place on the shared file system)."""
    place = tmp_path / "lease-000000000001"
    (place / "study").mkdir(parents=True)
    journal = tmp_path / "shared" / "spend.jsonl"
    keep = "import json, sys; json.dump(sys.argv[1:], open('argv.json', 'w'))"
    runner = ProcessRunner([sys.executable, "-c", keep])
    handle = runner.start(place / "study", place / "lease.json", place / "run.log", journal)
    assert handle.wait(60) == 0
    argv = json.loads((place / "argv.json").read_text(encoding="utf-8"))
    assert argv == [str(place / "study"), "--lease", str(place / "lease.json"), "--spend-journal", str(journal)]


def test_c46_a_worker_running_a_lease_leaves_a_verifying_snapshot_and_its_spend_journal_in_the_leases_place(
    tmp_path, clean_environment
):
    """C46: while a lease runs the worker snapshots its `out/` (`out_snapshot`), and the run journals its spend, in
    `<channel>/places/lease-<id>/` on the shared file system, which the dispatcher's later archive fetch leaves."""
    leased, kept = ran_for_real(tmp_path, clean_environment)
    (newest, *_) = out_snapshot.snapshots(kept / "snapshots")
    manifest = out_snapshot.restore(newest, tmp_path / "restored" / "out")  # every file read against its manifest
    assert manifest["facts"]["lease"] == {"lease_id": leased.lease.lease_id, "run_key": leased.lease.run_key}
    lines = [json.loads(line) for line in (kept / LEASE_JOURNAL).read_text(encoding="utf-8").splitlines()]
    assert lines[0]["kind"] == "header"
    assert any(line["kind"] == "attempt" for line in lines)
    assert not (tmp_path / "work" / f"lease-{leased.lease.lease_id:012d}" / LEASE_JOURNAL).exists()  # not the node's
    leased.now[0] += TICK_S
    leased.dispatch.tick()  # the archive is fetched, the lease counted: the worker's evidence stays
    assert leased.ledger.lease(leased.lease.lease_id).state == "counted"
    assert out_snapshot.snapshots(kept / "snapshots")
    assert (kept / LEASE_JOURNAL).is_file()


def test_c46_locate_gives_a_lost_leases_newest_snapshot_with_its_journal_and_the_read_is_fenced_to_that_lease(
    tmp_path, clean_environment
):
    """C46: a federation lost mid-run leaves its place: the newest snapshot (the store as of its last attempt's
    start) and the journal. `locate` restores that snapshot beside the journal, `dead_store_reader` settles the lease
    from them, and the same place under another lease's id gives nothing."""
    leased, kept = ran_for_real(tmp_path, clean_environment)
    lease_id = leased.lease.lease_id
    ended = out_snapshot.snapshots(kept / "snapshots")[0]
    manifest = out_snapshot.restore(ended, tmp_path / "mid" / "study" / "out")
    make_running(Campaign(tmp_path / "mid" / "study"))  # the snapshot the loss left: its last attempt still running
    out_snapshot.take(
        tmp_path / "mid" / "study" / "out", kept / "snapshots", scratch=tmp_path / "scratch",
        facts=manifest["facts"], live=False, now="2026-11-18T12:00:00Z",
    )  # fmt: skip
    with fleet_modules():
        import fleetexecute
        import fleetidentity

        config = fleetexecute.SiteConfig(
            SiteSettings(SITE, 7, 1e12, 47.0), tmp_path / "fleet", channel=Path(leased.site.root)
        )
        read = fleetexecute.dead_store_reader(fleetidentity.locator(tmp_path / "evidence"), leased.loaded)
        found = read(config, Settle(SITE, LABEL, LABEL, lease_id, str(tmp_path / "root"), "lost"))
        assert found is not None
        spend_micro, source, charged = found
        assert source == "root"
        assert spend_micro > 0
        assert charged == 0  # a done attempt is no attempt of the limit
        shutil.copytree(kept, kept.with_name(ch.place_name(lease_id + 1)))
        other = Settle(SITE, LABEL, LABEL, lease_id + 1, str(tmp_path / "root"), "lost")
        assert read(config, other) is None


@pytest.mark.parametrize("trouble", ["fail-listing", "fail-snapshot", "hang-snapshot", "fail-journal"])
def test_c47_a_lost_lease_at_an_ssh_site_settles_at_its_journals_spend_and_a_failed_fetch_leaves_it_charged(
    tmp_path, clean_environment, monkeypatch, trouble
):
    """C47: the place of a lease lost at an ssh site is on its login node: `locate` streams the newest snapshot and
    the journal into a private directory here and settles at the journal's spend (not the grant). A call that fails or
    hangs reads nothing this tick (the lease stays charged its grant, never taken for a place with nothing in it) and
    the next tick settles it; the private directory is gone either way."""
    leased, kept = ran_for_real(tmp_path, clean_environment)
    lease_id = leased.lease.lease_id
    manifest = out_snapshot.restore(out_snapshot.snapshots(kept / "snapshots")[0], tmp_path / "mid" / "study" / "out")
    make_running(Campaign(tmp_path / "mid" / "study"))
    out_snapshot.take(
        tmp_path / "mid" / "study" / "out", kept / "snapshots", scratch=tmp_path / "scratch",
        facts=manifest["facts"], live=False, now="2026-11-18T12:00:00Z",
    )  # fmt: skip
    fake = install_fake_ssh(tmp_path / "bin", monkeypatch, "zz-login-7f3a.invalid")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    with fleet_modules():
        import fleetexecute
        import fleetidentity
        import fleetssh

        config = fleetexecute.SiteConfig(
            SiteSettings(SITE, 7, 1e12, 47.0), tmp_path / "fleet", "ssh", sys.executable, tmp_path / "deploy",
            Path(leased.site.root), "zz-login-7f3a.invalid", str(tmp_path / "site.env"),
        )  # fmt: skip
        ssh = fleetssh.SshSiteTransport(dict(os.environ), fetch_timeout_s=3.0)
        read = fleetexecute.dead_store_reader(
            fleetidentity.locator(tmp_path / "evidence", fleetexecute.SiteTransport(dict(os.environ), ssh=ssh)),
            leased.loaded,
        )
        lost = Settle(SITE, LABEL, LABEL, lease_id, str(tmp_path / "root"), "lost")
        match, action = {
            "fail-listing": ("ls -1", "fail"),
            "fail-snapshot": ("tar -C", "fail"),
            "hang-snapshot": ("tar -C", "hang_child"),
            "fail-journal": ("spend.jsonl", "fail"),
        }[trouble]
        fake.rule(match, action)
        assert read(config, lost) is None  # charged its grant this tick
        if action == "hang_child":
            assert fake.child_gone()
        assert not list((tmp_path / "tmp").iterdir())  # nothing of the fetch is left here
        fake.clear()
        found = read(config, lost)
        assert found is not None
        spend_micro, source, charged = found
        assert (source, charged) == ("root", 0)
        assert 0 < spend_micro < 5_000_000  # the journal's spend, not the grant of the model (5 USD)
