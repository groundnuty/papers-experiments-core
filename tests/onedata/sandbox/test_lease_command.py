"""The operator's command that revokes a held slot's tokens, so a slot quarantined before revocation existed frees
after the settle and not at its tokens' expiry (C19)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_host import FakeHost

from expcore.campaign.lease import LeasePool
from expcore_onedata import lease as command
from expcore_onedata.testing import CEILING_S, NOW
from expcore_onedata.tokens import TokenIssuer
from expcore_onedata.users import GROUP, JOURNAL, SlotProbe, SlotTokens, TokenJournal

KEY = "slot-a"
SETTLE_S = 330


def no_events(kind, detail):
    """The event sink of a test that is not about events."""


@pytest.fixture
def stuck(config, slots, client, fake, tmp_path):
    """A run's lease on the first slot, its tokens minted and journalled, and the run over: the lease is finished and,
    its tokens alive, quarantined by the pool, which keeps the key's lock."""
    leases, now = tmp_path / "leases", [NOW]
    journal = TokenJournal(leases)
    probe = SlotProbe(journal, clock=lambda: now[0])
    pool = LeasePool(leases, slots.keys_of, probe, clock=lambda: 0.0, quarantine_retry_s=0.0)
    lease = pool.acquire(GROUP, run_key="run-1")
    issuer = TokenIssuer(client, config, clock=lambda: NOW)
    SlotTokens(issuer, config, slots, journal, events=no_events, clock=lambda: NOW).mint(
        lease, ceiling_s=CEILING_S, condition="read"
    )
    return SimpleNamespace(pool=pool, lease=lease, journal=journal, leases=leases, now=now)


def run(config, client, leases, *, slot=KEY, host=None, clock=lambda: NOW):
    argv = ["revoke", "--platform", str(config.file), "--leases", str(leases), "--slot", slot]
    return command.main(argv, client=client, host=host or FakeHost(), clock=clock)


def revocations(leases):
    lines = [json.loads(line) for line in (leases / JOURNAL).read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line.get("plane") == "revoked"]


def test_c19_the_command_refuses_a_slot_a_running_campaign_keeps_in_quarantine(stuck, config, client, fake, capsys):
    """C19: a running campaign holds a quarantined lease by its lock and frees it by letting the lock go, at a moment
    the command cannot know; a next run could then mint tokens the revocation would revoke. So the command, which
    cannot own the slot, refuses it and sends nothing, and the quarantine stands."""
    assert stuck.pool.finish(stuck.lease) is False  # quarantined: its tokens live for another half hour
    assert run(config, client, stuck.leases) == 1
    out = capsys.readouterr().out
    assert "a live process holds the lock of slots[0]" in out
    assert "stop that campaign first" in out
    assert KEY not in out
    assert not [r for r in fake.requests if r.method == "DELETE"]
    assert not revocations(stuck.leases)
    stuck.now[0] = NOW + SETTLE_S + 1
    assert stuck.pool.settle_quarantines() == []  # the expiry rule stands


DEAD_OWNER = "00000000-0000-0000-0000-000000000000:999999:1"  # a process of another boot: shown gone


def died(stuck) -> Path:
    """The holder's process died: the kernel dropped its lock, and its note names it."""
    lock = stuck.leases / f"{KEY}.lock"
    stuck.lease._handle.close()
    lock.write_text(json.dumps({"owner": DEAD_OWNER, "pid": 999999, "run_key": "run-1", "key": KEY}), encoding="utf-8")
    return lock


def test_c19_no_pool_frees_or_leases_the_slot_while_the_command_revokes_it(stuck, config, client, monkeypatch, capsys):
    """C19: the command holds the slot's lease lock from before its first check to after its revoked line. A pool that
    would free the key at once (its tokens' expiry passed while the command ran, its sandbox gone) can neither reap it,
    nor ask its held-back proof again, nor lease it, until the command ends; then it reaps and leases it at its next
    look. So no next run can mint while the command revokes."""
    lock = died(stuck)
    moment = [NOW]
    probe = SlotProbe(stuck.journal, sandbox_gone=lambda key, note: True, clock=lambda: moment[0])
    pool = LeasePool(stuck.leases, lambda group: [KEY], probe, clock=lambda: 0.0, dead_retry_s=0.0)
    assert pool.sweep_dead_holders([GROUP]) == [KEY]  # its tokens live: held back for the dead holder
    during = []
    revoke_own = TokenIssuer.revoke_own

    def revoking(self, principal):
        if not during:  # before the first bump
            moment[0] = NOW + 10**6
            owner = json.loads(lock.read_text(encoding="utf-8"))["owner"]
            during.append((pool.settle_quarantines(), pool.try_acquire(GROUP), pool.sweep_dead_holders([GROUP]), owner))
        return revoke_own(self, principal)

    monkeypatch.setattr(TokenIssuer, "revoke_own", revoking)
    assert run(config, client, stuck.leases) == 0, capsys.readouterr().out
    assert during == [([], None, [], DEAD_OWNER)]  # nothing freed, leased or reaped while the command ran
    lease = pool.try_acquire(GROUP)
    assert lease is not None
    assert lease.key == KEY
    lease.release()


def test_c19_the_command_revokes_a_slot_whose_holder_died(stuck, config, client, slots):
    """C19: a holder that died left its note; the command takes the free lock, revokes, and leaves the note, so the
    next campaign's sweep asks the probe, which frees the key once the settle has passed."""
    lock = died(stuck)
    assert run(config, client, stuck.leases) == 0
    assert revocations(stuck.leases)
    assert json.loads(lock.read_text(encoding="utf-8"))["owner"] == DEAD_OWNER  # the note is left as it was
    for moment, held_back in ((NOW + SETTLE_S, [KEY]), (NOW + SETTLE_S + 1, [])):
        probe = SlotProbe(stuck.journal, sandbox_gone=lambda key, note: True, clock=lambda moment=moment: moment)
        assert LeasePool(stuck.leases, slots.keys_of, probe, clock=lambda: 0.0).sweep_dead_holders([GROUP]) == held_back


def test_c19_the_command_refuses_while_a_run_holds_the_slot(stuck, config, client, fake, capsys):
    """C19: the lease is held and was never finished, so its run is under way: revoking now would end it."""
    assert run(config, client, stuck.leases) == 1
    assert "a live process holds the lock of slots[0]" in capsys.readouterr().out
    assert not [r for r in fake.requests if r.method == "DELETE"]
    assert not revocations(stuck.leases)


def test_c19_the_command_refuses_a_slot_whose_sandbox_is_not_shown_gone(stuck, config, client, fake, capsys):
    died(stuck)
    host = FakeHost()
    host.docker("run", "-d", "--name", "leftover", "--label", f"org.test.lease={KEY}", "agent:1")
    assert run(config, client, stuck.leases, host=host) == 1
    assert "the sandbox of slots[0] is not shown gone" in capsys.readouterr().out
    assert not [r for r in fake.requests if r.method == "DELETE"]


def test_c19_a_revocation_the_command_cannot_confirm_journals_nothing(stuck, config, client, fake, capsys):
    died(stuck)
    fake.revoke_bumps = False
    assert run(config, client, stuck.leases) == 1
    assert "did not rise" in capsys.readouterr().out
    assert not revocations(stuck.leases)


@pytest.mark.parametrize("slot", ["no-such-slot", "slot-b"], ids=["unknown-slot", "no-lock-file"])
def test_c19_the_command_refuses_a_slot_it_cannot_find(stuck, config, client, fake, slot, capsys):
    """C19: an unknown name, or a slot the pool of that directory never leased (no lock file: the wrong directory)."""
    assert run(config, client, stuck.leases, slot=slot) == 1
    assert "refused" in capsys.readouterr().out
    assert not [r for r in fake.requests if r.method == "DELETE"]
    assert not (stuck.leases / "slot-b.lock").exists()
