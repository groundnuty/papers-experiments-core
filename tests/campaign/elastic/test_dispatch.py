"""C43: the dispatcher's loop over a temp-dir site behind a fake ssh: one call per site per tick, the ledger decides."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from channel_support import FakeSsh, beat, channel_of, complete, inbox_record, site_of, transport
from elastic_support import SITE, assert_invariants, join, ledger_path, make, plan

import expcore
from expcore.campaign.elastic import Ledger
from expcore.campaign.elastic import channel as ch
from expcore.campaign.elastic.dispatch import FETCH_TRIES, TICK_S, Dispatcher
from expcore.campaign.elastic.transport import Site, SshTransport

ONE_RUN = {"runs": plan(tasks=1, conditions=("api-none",))}


def dispatcher(tmp_path, ledger, ssh, *sites):
    return Dispatcher(ledger, list(sites) or [site_of(tmp_path)], transport(ssh), tmp_path / "out" / "leases")


def ticks(dispatch, clock, count, seconds=TICK_S):
    for _ in range(count):
        clock.advance(seconds)
        dispatch.tick()


def sent_lease(tmp_path, **changes):
    """A ledger with fed-1 joined at site-a and its first lease sent through the channel."""
    ledger, clock = make(tmp_path, **changes)
    join(ledger, clock, "fed-1")
    ssh = FakeSsh()
    site = site_of(tmp_path)
    dispatch = dispatcher(tmp_path, ledger, ssh, site)
    ticks(dispatch, clock, 2)
    (lease,) = ledger.leases()
    assert lease.state == "sent"
    return ledger, clock, ssh, site, dispatch, lease


def test_c43_a_lease_goes_out_after_its_site_answered_and_is_sent_only_once_the_listing_shows_it(tmp_path):
    """C43: the first tick reads the site and issues nothing; the next issues the lease, writes its file in the
    federation's inbox in the same call and marks it sent from that call's listing; one call per site per tick."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-1")
    ssh = FakeSsh()
    site = site_of(tmp_path)
    dispatch = dispatcher(tmp_path, ledger, ssh, site)
    first = dispatch.tick()[SITE]
    assert first.read
    assert not first.issued
    assert ledger.leases() == []
    second = dispatch.tick()[SITE]
    (lease,) = ledger.leases()
    assert second.issued == second.sent == [lease.lease_id]
    assert lease.state == "sent"
    record = inbox_record(site, "fed-1", lease.lease_id)
    assert record == ch.envelope(lease)
    assert record["lease"]["campaign_sha256"] == ledger.config.fingerprint
    assert len(ssh.calls) == 2
    dispatch.tick()
    assert [lease.lease_id for lease in ledger.leases()] == [lease.lease_id]  # it holds its slot: no second lease
    assert len(ssh.calls) == 3
    assert_invariants(ledger)


def test_c43_one_call_per_site_carries_every_federation_of_the_site(tmp_path):
    """C43: three federations at one site and one at another: each tick makes one call per site."""
    ledger, clock = make(tmp_path, runs=plan(tasks=4))
    for label in ("fed-1", "fed-2", "fed-3"):
        join(ledger, clock, label)
    join(ledger, clock, "fed-4", site="site-b")
    ssh = FakeSsh()
    sites = [site_of(tmp_path), site_of(tmp_path, "site-b")]
    dispatch = dispatcher(tmp_path, ledger, ssh, *sites)
    ticks(dispatch, clock, 2)
    assert sorted(site for site, _ in ssh.calls) == ["site-a", "site-a", "site-b", "site-b"]
    assert sorted(lease.federation for lease in ledger.leases() if lease.state == "sent") == [
        "fed-1",
        "fed-2",
        "fed-3",
        "fed-4",
    ]
    assert_invariants(ledger)


def test_c43_a_crash_between_issued_and_sent_is_settled_on_restart_by_a_tombstone_and_the_run_goes_out_again(tmp_path):
    """C43: a lease written `issued` whose file never went out (the dispatcher died before its call) is settled on
    restart: a tombstone takes its name, so no late delivery can land, the lease is cancelled with nothing charged,
    and its run is leased again under a new id."""
    ledger, clock = make(tmp_path, **ONE_RUN)
    join(ledger, clock, "fed-1")
    orphan = ledger.issue("fed-1")
    ledger.close()  # the crash: issued, never sent
    ledger = Ledger.open(ledger_path(tmp_path), clock)
    ssh = FakeSsh()
    site = site_of(tmp_path)
    dispatch = dispatcher(tmp_path, ledger, ssh, site)
    done = dispatch.tick()[SITE]
    assert done.settled == {orphan.lease_id: "tombstone"}
    assert done.cancelled == [orphan.lease_id]
    assert ledger.lease(orphan.lease_id).state == "cancelled"
    assert ledger.lease(orphan.lease_id).charge_micro == 0
    assert ch.is_tombstone(inbox_record(site, "fed-1", orphan.lease_id))
    assert not ch.create_json(channel_of(site, "fed-1") / ch.INBOX / ch.lease_name(orphan.lease_id), {"late": True})
    clock.advance(TICK_S)
    dispatch.tick()
    again = [lease for lease in ledger.leases() if lease.state == "sent"]
    assert [(lease.run_key, lease.lease_id > orphan.lease_id) for lease in again] == [(orphan.run_key, True)]
    assert_invariants(ledger)


def test_c43_a_lease_whose_file_landed_before_a_lost_reply_is_marked_sent_on_restart_never_cancelled(tmp_path):
    """C43: the call wrote the lease file and its reply was lost (the connection dropped), so the lease stayed
    `issued`; after a restart the settle finds the file under its name and marks it sent: its worker may run it."""
    ledger, clock = make(tmp_path, **ONE_RUN)
    join(ledger, clock, "fed-1")
    ssh = FakeSsh()
    site = site_of(tmp_path)
    dispatch = dispatcher(tmp_path, ledger, ssh, site)
    dispatch.tick()
    ssh.lost_reply.add(SITE)
    clock.advance(TICK_S)
    dispatch.tick()
    (lease,) = ledger.leases()
    assert lease.state == "issued"
    assert inbox_record(site, "fed-1", lease.lease_id) == ch.envelope(lease)
    ledger.close()
    ledger = Ledger.open(ledger_path(tmp_path), clock)
    ssh.lost_reply.clear()
    done = dispatcher(tmp_path, ledger, ssh, site).tick()[SITE]
    assert done.settled == {lease.lease_id: "lease"}
    assert done.sent == [lease.lease_id]
    assert not done.cancelled
    assert ledger.lease(lease.lease_id).state == "sent"
    assert inbox_record(site, "fed-1", lease.lease_id) == ch.envelope(lease)  # never replaced by a tombstone
    assert_invariants(ledger)


def test_c43_a_heartbeat_that_advances_keeps_its_lease_and_one_that_stands_still_expires_it(tmp_path):
    """C43: the worker's counter, read each tick, renews the lease while it advances; read unchanged for the ledger's
    stale_s (5 minutes) of reads, the lease expires and its run goes back to pending."""
    ledger, clock, _ssh, site, dispatch, lease = sent_lease(tmp_path, **ONE_RUN)
    for counter in range(1, 21):
        beat(site, "fed-1", lease.lease_id, counter)
        ticks(dispatch, clock, 1)
    assert ledger.lease(lease.lease_id).state == "sent"
    ticks(dispatch, clock, 9)
    assert ledger.lease(lease.lease_id).state == "sent"  # 270 s unchanged
    ticks(dispatch, clock, 1)
    assert ledger.lease(lease.lease_id).state == "expired"
    assert ledger.run(lease.run_key).state == "pending"
    assert_invariants(ledger)


def test_c43_a_partition_expires_a_sites_leases_after_site_out_and_the_late_completion_is_superseded(tmp_path):
    """C43: a site unreachable for N ticks is unread, not stale: its lease holds until the ledger's site_out_s (15
    minutes), then expires, and its run goes to another site's federation. When the site returns, the completion the
    first federation left is read and fenced: the later lease's completion is the run's, the earlier one is
    superseded, and the run is counted once."""
    ledger, clock = make(tmp_path, **ONE_RUN, caps={"m1": 2})
    join(ledger, clock, "fed-a")
    ssh = FakeSsh()
    site_a, site_b = site_of(tmp_path), site_of(tmp_path, "site-b")
    dispatch = dispatcher(tmp_path, ledger, ssh, site_a, site_b)
    ticks(dispatch, clock, 2)
    (first,) = ledger.leases()
    beat(site_a, "fed-a", first.lease_id, 1)
    ticks(dispatch, clock, 1)
    join(ledger, clock, "fed-b", site="site-b")
    ssh.down.add("site-a")
    ticks(dispatch, clock, 29)  # 870 s unread
    assert ledger.lease(first.lease_id).state == "sent"
    ticks(dispatch, clock, 1)
    assert ledger.lease(first.lease_id).state == "expired"
    ticks(dispatch, clock, 1)
    second = next(lease for lease in ledger.leases() if lease.federation == "fed-b")
    assert (second.run_key, second.state) == (first.run_key, "sent")
    complete(site_a, "fed-a", first)
    complete(site_b, "fed-b", second, spend_micro_usd=500_000)
    ssh.down.clear()
    ticks(dispatch, clock, 2)
    assert ledger.lease(first.lease_id).state == "superseded"
    assert ledger.lease(second.lease_id).state == "counted"
    assert ledger.run(first.run_key).state == "done"
    assert [event["kind"] for event in ledger.events()].count("counted") == 1
    assert (tmp_path / "out" / "leases" / ch.archive_name(second.lease_id)).is_file()
    assert_invariants(ledger)


def test_c43_a_completion_read_on_every_tick_counts_once_and_its_pulled_archive_settles_the_run(tmp_path):
    """C43: the completion file stays in the outbox and is read every tick; the ledger reads it once, its archive is
    pulled and hashed, and the run is done once, charged the micro-dollars the file names."""
    ledger, clock, ssh, site, dispatch, lease = sent_lease(tmp_path, **ONE_RUN)
    complete(site, "fed-1", lease, spend_micro_usd=250_001)
    ticks(dispatch, clock, 3)
    assert ledger.lease(lease.lease_id).state == "counted"
    assert ledger.run(lease.run_key).state == "done"
    assert ledger.lease(lease.lease_id).charge_micro == 250_001  # the file's micro-dollars, exactly
    assert [event["kind"] for event in ledger.events()].count("report") == 1
    pulls = [remote for _, remote in ssh.calls if remote[0] == "cat"]
    assert len(pulls) == 1
    assert_invariants(ledger)


def test_c43_a_file_in_one_federations_outbox_naming_anothers_lease_is_never_read(tmp_path):
    """C43: the ledger fences by site; the dispatcher fences by federation too. A completion and a heartbeat of
    fed-1's lease found in fed-2's outbox are dropped, and fed-1's lease is as it was."""
    ledger, clock = make(tmp_path, runs=plan(tasks=1), caps={"m1": 2})
    join(ledger, clock, "fed-1")
    join(ledger, clock, "fed-2")
    ssh = FakeSsh()
    site = site_of(tmp_path)
    dispatch = dispatcher(tmp_path, ledger, ssh, site)
    ticks(dispatch, clock, 2)
    lease = next(lease for lease in ledger.leases() if lease.federation == "fed-1")
    stray = channel_of(site, "fed-2") / ch.OUTBOX
    ch.write_json(
        stray / ch.completion_name(lease.lease_id),
        {
            "format": ch.COMPLETION_FORMAT,
            "federation": "fed-2",
            "lease_id": lease.lease_id,
            "run_key": lease.run_key,
            "archive_sha256": "0" * 64,
            "state": "done",
            "charged": 1,
            "spend_micro_usd": 100_000,
        },
    )
    beat(site, "fed-2", lease.lease_id, 9)
    clock.advance(TICK_S)
    done = dispatch.tick()[SITE]
    assert done.read
    assert len(done.dropped) == 2
    assert ledger.lease(lease.lease_id).state == "sent"
    assert ledger.lease(lease.lease_id).counter is None
    assert_invariants(ledger)


def test_c43_a_reply_cut_short_is_no_reading_and_the_site_is_only_swept(tmp_path):
    """C43: a reply that is not one whole JSON document reads nothing (no heartbeat renews), and the sweep runs as for
    an unreachable site; the next whole reply reads the site."""
    ledger, clock, ssh, site, dispatch, lease = sent_lease(tmp_path, **ONE_RUN)
    beat(site, "fed-1", lease.lease_id, 4)
    ssh.cut.add(SITE)
    clock.advance(TICK_S)
    assert not dispatch.tick()[SITE].read
    assert ledger.lease(lease.lease_id).counter is None
    ssh.cut.clear()
    clock.advance(TICK_S)
    assert dispatch.tick()[SITE].read
    assert ledger.lease(lease.lease_id).counter == 4


def test_c43_an_outbox_that_cannot_be_listed_leaves_the_site_unread(tmp_path, monkeypatch):
    """C43: a listing that fails is no reading of the site, never an empty one that would let a running lease's
    heartbeat go stale."""
    ledger, clock, _ssh, site, dispatch, lease = sent_lease(tmp_path, **ONE_RUN)
    beat(site, "fed-1", lease.lease_id, 2)
    outbox, real = str(channel_of(site, "fed-1") / ch.OUTBOX), os.scandir

    def scandir(path=None):
        if path is not None and os.fspath(path) == outbox:
            raise OSError(116, "Stale file handle")
        return real() if path is None else real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    clock.advance(TICK_S)
    assert not dispatch.tick()[SITE].read
    assert ledger.lease(lease.lease_id).counter is None


def test_c43_an_archive_that_never_matches_its_completion_makes_the_lease_lost_after_its_tries(tmp_path):
    """C43: the archive pulled hashes to another value than the completion names: it is pulled again on each tick,
    and after FETCH_TRIES the lease is lost and its run pending again."""
    ledger, clock, _ssh, site, dispatch, lease = sent_lease(tmp_path, **ONE_RUN)
    complete(site, "fed-1", lease, sha256="f" * 64)
    ticks(dispatch, clock, FETCH_TRIES - 1)
    assert ledger.lease(lease.lease_id).state == "reported"
    ticks(dispatch, clock, 1)
    assert ledger.lease(lease.lease_id).state == "lost"
    assert ledger.run(lease.run_key).state == "pending"
    assert not (tmp_path / "out" / "leases" / ch.archive_name(lease.lease_id)).exists()
    assert_invariants(ledger)


def test_c43_an_unreachable_pull_is_tried_again_and_never_counts_against_the_archive(tmp_path):
    """C43: a pull whose ssh fails is no try: after the site answers again, the archive verifies."""
    ledger, clock, ssh, site, dispatch, lease = sent_lease(tmp_path, **ONE_RUN)
    complete(site, "fed-1", lease)
    ssh.pull_down.add(SITE)
    ticks(dispatch, clock, FETCH_TRIES + 1)
    assert ledger.lease(lease.lease_id).state == "reported"
    ssh.pull_down.clear()
    ticks(dispatch, clock, 1)
    assert ledger.lease(lease.lease_id).state == "counted"


def test_c43_a_drain_asked_by_the_watchdog_drains_the_federation_in_the_ledger(tmp_path):
    """C43: a drain marker in a federation's inbox stops the ledger leasing to it."""
    ledger, clock, _ssh, site, dispatch, lease = sent_lease(tmp_path)
    ch.create_json(channel_of(site, "fed-1") / ch.INBOX / ch.DRAIN, {"format": ch.DRAIN_FORMAT})
    complete(site, "fed-1", lease)
    ticks(dispatch, clock, 3)
    assert [fed.drained_at is not None for fed in ledger.federations()] == [True]
    assert [lease.lease_id for lease in ledger.leases()] == [lease.lease_id]


def test_c43_the_exchange_runs_as_its_own_process_behind_an_ssh_command(tmp_path):
    """C43 (guard): the verb runs as `python -m expcore.campaign.elastic.exchange` behind a real process standing in
    for ssh (it drops the host and runs the command through a shell), with the request on its standard input."""
    ssh_script = tmp_path / "fake-ssh"
    ssh_script.write_text('#!/bin/sh\nshift\nexec /bin/sh -c "$1"\n', encoding="utf-8")
    ssh_script.chmod(0o700)
    source = str(Path(expcore.__file__).resolve().parents[1])
    site = Site(
        name=SITE,
        root=str(tmp_path / "channels"),
        ssh=(str(ssh_script), "login"),
        python=(sys.executable, "-B"),
    )
    ledger, clock = make(tmp_path, **ONE_RUN)
    join(ledger, clock, "fed-1")
    env = {**os.environ, "PYTHONPATH": source}

    def run(argv, **kwargs):
        return subprocess.run(argv, env=env, **kwargs)

    dispatch = Dispatcher(ledger, [site], SshTransport(run=run), tmp_path / "out" / "leases")
    ticks(dispatch, clock, 2)
    (lease,) = ledger.leases()
    assert lease.state == "sent"
    contact = json.loads((tmp_path / "channels" / ch.CONTACT).read_text(encoding="utf-8"))
    assert contact["format"] == ch.CONTACT_FORMAT
    assert contact["seq"] == 2


@pytest.mark.parametrize("bad", ["../escape", "Fed-1", ""])
def test_c43_the_exchange_refuses_a_federation_label_that_is_not_an_operator_label(tmp_path, bad):
    """C43: a label is a directory name under the channel root, so only an operator label is taken, and a request
    holding another is refused whole, with nothing written."""
    from expcore.campaign.elastic.exchange import RequestError, handle

    request = {
        "format": "expcore-elastic-exchange/1",
        "site": SITE,
        "root": str(tmp_path / "channels"),
        "contact": {},
        "federations": [{"label": "fed-1"}, {"label": bad}],
    }
    with pytest.raises(RequestError):
        handle(request)
    assert not (tmp_path / "channels").exists()


def test_c43_a_reported_lease_of_a_federation_proven_ended_is_still_pulled_and_settled(tmp_path):
    """C43: the archive pull failed, then the federation's job was proven ended: its reported lease is still read and
    pulled once the site answers, so a counting completion settles its run."""
    ledger, clock, ssh, site, dispatch, lease = sent_lease(tmp_path, **ONE_RUN)
    complete(site, "fed-1", lease)
    ssh.pull_down.add(SITE)
    ticks(dispatch, clock, 1)
    assert ledger.lease(lease.lease_id).state == "reported"
    ledger.prove_ended("fed-1", "terminal")
    ssh.pull_down.clear()
    ticks(dispatch, clock, 1)
    assert ledger.lease(lease.lease_id).state == "counted"
    assert ledger.run(lease.run_key).state == "done"


def test_c43_an_archive_pulled_before_a_restart_settles_its_lease_without_another_pull(tmp_path):
    """C43: the dispatcher pulled and checked an archive and stopped before it settled the lease; on the next tick the
    archive it holds settles the lease, though the site's copy is gone."""
    ledger, clock, ssh, site, dispatch, lease = sent_lease(tmp_path, **ONE_RUN)
    complete(site, "fed-1", lease)
    remote = channel_of(site, "fed-1") / ch.ARCHIVES / ch.archive_name(lease.lease_id)
    local = tmp_path / "out" / "leases" / ch.archive_name(lease.lease_id)
    local.parent.mkdir(parents=True)
    local.write_bytes(remote.read_bytes())
    remote.unlink()
    ticks(dispatch, clock, 1)
    assert ledger.lease(lease.lease_id).state == "counted"
    assert not [remote for _, remote in ssh.calls if remote[0] == "cat"]


class Partial:
    """A transport whose replies lack one key of fed-1's answer (or of its outbox): a verb of another form."""

    def __init__(self, inner, key, in_outbox=False):
        self.inner, self.key, self.in_outbox = inner, key, in_outbox

    def exchange(self, site, request):
        reply = self.inner.exchange(site, request)
        answer = reply["federations"]["fed-1"]
        del (answer["outbox"] if self.in_outbox else answer)[self.key]
        return reply

    def fetch(self, site, remote, local):
        return self.inner.fetch(site, remote, local)


@pytest.mark.parametrize(("key", "in_outbox"), [("drain", False), ("heartbeat", True)])
def test_c43_a_reply_missing_part_of_an_answer_is_no_reading_and_never_a_crash(tmp_path, key, in_outbox):
    """C43: an answer without its drain flag, or an outbox without its heartbeat entry, is not the verb's whole form:
    the site is unread (no heartbeat is taken as absent), and the tick does not fail."""
    ledger, clock, ssh, site, dispatch, lease = sent_lease(tmp_path, **ONE_RUN)
    beat(site, "fed-1", lease.lease_id, 3)
    dispatch.transport = Partial(transport(ssh), key, in_outbox)
    clock.advance(TICK_S)
    assert not dispatch.tick()[SITE].read
    assert ledger.lease(lease.lease_id).counter is None


def test_c43_a_cached_archive_that_cannot_be_read_falls_back_to_the_sites_copy(tmp_path, monkeypatch):
    """C43: a local archive left by an earlier pull that this host cannot read is passed over, never a crash of the
    tick: the site's copy is pulled and settles the lease."""
    from expcore.campaign.elastic import dispatch as module

    ledger, clock, _ssh, site, dispatch, lease = sent_lease(tmp_path, **ONE_RUN)
    complete(site, "fed-1", lease)
    local = tmp_path / "out" / "leases" / ch.archive_name(lease.lease_id)
    local.parent.mkdir(parents=True)
    local.write_bytes(b"left by an earlier pull")
    real = module._sha256

    def sha256(path):
        if path == local:
            raise PermissionError(13, "Permission denied")
        return real(path)

    monkeypatch.setattr(module, "_sha256", sha256)
    ticks(dispatch, clock, 1)
    assert ledger.lease(lease.lease_id).state == "counted"
