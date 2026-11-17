"""O11: a reset holds the reset lock exclusively, and nothing that can place bytes holds it beside.

Every shared admission (a campaign run's, a tool's outside a campaign) holds `federation.reset` shared beside its share
of `federation.lock`. A reset's section gives up its own run's share of `federation.reset`, never of `federation.lock`,
and waits, within `[faults] admission_timeout_s`, for `federation.reset` exclusively; past the bound it is refused
(`ResetLockBusy`, uncharged), naming the holders, and takes its share back (O66: it never resets under them). Every test
runs against the real locks on the fixture's journal directory; the waits are real seconds, kept short.
"""

import ast
import contextlib
import fcntl
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fault_support import OUTAGE_TARGET, FakeServices, write_faults
from reset_lock_support import hold_and_fork, run_and_hang
from shared_support import hold_and_die

import expcore_onedata.admission as admission_module
from expcore.campaign.healthgate import Health
from expcore.errors import InfraError
from expcore_onedata import resetgrant, resetshortcut
from expcore_onedata.admission import (
    FEDERATION_LOCK,
    AdmissionRefused,
    FederationBusy,
    FederationLock,
    current_admission,
    require_admission,
    retain_to_act,
)
from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.fault_journal import _HELD, boot_id
from expcore_onedata.faults import load_faults
from expcore_onedata.occupancy import Listing
from expcore_onedata.rangemap import PlacementSettings
from expcore_onedata.resetgrant import (
    EXCLUSIVE,
    FEDERATION_EXCLUSIVE,
    HOLDER_GONE,
    NO_ADMISSION,
    OFF,
    SHARES_RETAINED,
    ResetLockBusy,
    exclusive,
    federation_for,
)
from expcore_onedata.resetlock import FEDERATION_RESET, OUTSIDE_CAMPAIGN, RESET_HOLDS
from expcore_onedata.shared_holds import SharedHolds
from expcore_onedata.testing import write_platform
from expcore_onedata.tool_admission import shared_admission

WAIT_S = 10.0  # the bound of a wait that must be granted
SHORT_S = 0.4  # the bound of a wait that must run out
PROBE_S = 0.2
REASON = "smoke_warmup"
SRC = Path(__file__).resolve().parents[2] / "src"


def federation(tmp_path, **faults):
    platform = write_faults(tmp_path, **faults)
    config = load_platform(platform, require_secrets=False)
    settings = load_faults(platform, config)
    return config, settings, FederationLock(settings)


def shortcut(wait_s=WAIT_S, **changes):
    return PlacementSettings(holder="alpha", reset_shortcut=True, reset_exclusive_wait_s=wait_s, **changes)


def free(settings, name):
    """Whether the lock file `name` can be taken exclusively at once, on a descriptor of the test's own."""
    descriptor = os.open(Path(settings.journal) / name, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(descriptor)  # closing drops the lock if it was taken
    return True


def in_thread(work, name=None):
    landed: list = []

    def run():
        try:
            landed.append(work())
        except BaseException as error:
            landed.append(error)

    thread = threading.Thread(target=run, name=name)
    thread.start()
    return thread, landed


def tool_in_thread(config, release, reason=REASON):
    """A tool outside a campaign holding the shared admission in a thread until `release` is set."""
    holding = threading.Event()

    def tool():
        with shared_admission(config, reason=reason):
            holding.set()
            release.wait(WAIT_S)

    thread, landed = in_thread(tool)
    assert holding.wait(WAIT_S)
    return thread, landed


def reset_holds(settings):
    return SharedHolds(settings.journal, name=RESET_HOLDS).holds()


# --- every shared admission holds the reset lock shared -------------------------------------------------------------


def test_o11_a_campaign_runs_shared_admission_holds_the_reset_lock_shared_and_lets_it_go(tmp_path):
    """O11: a run's shared admission holds `federation.reset` shared for as long as it holds `federation.lock`, and
    records the hold; both go when it ends."""
    _, settings, lock = federation(tmp_path)
    with lock.held(exclusive=False, timeout_s=PROBE_S) as admission:
        assert admission.reset is not None
        assert admission.reset.shared
        assert not free(settings, FEDERATION_RESET)
        (hold,) = reset_holds(settings)
        assert hold["until_utc"] is None
    assert free(settings, FEDERATION_RESET)
    (hold,) = reset_holds(settings)
    assert hold["how"] == "released"


def test_o11_an_outage_runs_exclusive_admission_takes_no_reset_lock(tmp_path):
    """O11: an exclusive admission of `federation.lock` already excludes every shared one, every resetter included,
    so it takes `federation.reset` in no mode and records no hold of it."""
    _, settings, lock = federation(tmp_path)
    with lock.held(exclusive=True, timeout_s=PROBE_S, target=OUTAGE_TARGET) as admission:
        assert admission.reset is None
        assert free(settings, FEDERATION_RESET)
    assert reset_holds(settings) == []


# --- a shortcut reset waits for every reader, and only them ----------------------------------------------------------


def test_o11_a_concurrent_shared_run_holds_the_shortcut_off_until_it_releases(tmp_path):
    """O11: another run in its agent phase holds `federation.reset` shared; the shortcut reset's exclusive wait is
    granted only once that run has let go."""
    config, _, lock = federation(tmp_path)
    other = lock.acquire(exclusive=False, timeout_s=PROBE_S)  # another campaign run, its agent running
    with lock.held(exclusive=False, timeout_s=PROBE_S):
        letting_go = threading.Timer(0.5, other.close)
        began = time.monotonic()
        letting_go.start()
        with exclusive(config, shortcut()) as grant:
            assert (grant.mode, grant.why) == (EXCLUSIVE, None)
            assert grant.shortcut
            assert grant.held
            assert not other.held, "the exclusive hold was granted while another run still held its share"
            assert time.monotonic() - began >= 0.45
        letting_go.join(WAIT_S)


def test_o11_the_shortcut_proceeds_when_a_tools_hold_ends_within_the_bound(tmp_path):
    """O11: a tool outside a campaign that lets go within the bound lets the shortcut reset in; the wait is recorded."""
    config, _, lock = federation(tmp_path)
    release = threading.Event()
    thread, landed = tool_in_thread(config, release)
    threading.Timer(0.5, release.set).start()
    with lock.held(exclusive=False, timeout_s=PROBE_S), exclusive(config, shortcut()) as grant:
        assert grant.shortcut
        assert landed == [None], "the tool still held the federation when the exclusive hold was granted"
        assert grant.facts()["wait_s"] >= 0.45
    thread.join(WAIT_S)


def test_o11_a_tools_hold_past_the_bound_refuses_the_section_and_names_the_holders(tmp_path):
    """O11: a tool outside a campaign holds the reset lock shared past `admission_timeout_s`: the section is refused
    (`ResetLockBusy`, uncharged), never entered under it (O66; it used to give a grant with no shortcut, and the reset
    then ran beside the tool); the refusal says how long it waited and names the live holder by its process (pid, boot
    id, start time), never by its reason (O12), and the run has its share back for its agent."""
    config, _settings, lock = federation(tmp_path, admission_timeout_s=SHORT_S)
    release = threading.Event()
    thread, _ = tool_in_thread(config, release, reason="warmup_first_run")
    try:
        with lock.held(exclusive=False, timeout_s=PROBE_S) as admission:
            with pytest.raises(ResetLockBusy) as refused, exclusive(config, shortcut()):
                pass
            assert refused.value.wait_s >= SHORT_S
            (holder,) = refused.value.holders
            assert holder["status"] == "live"
            assert (holder["pid"], holder["boot_id"]) == (os.getpid(), boot_id())
            assert isinstance(holder["start_time"], str)
            assert holder["start_time"]
            assert holder["in_campaign"] is False
            assert f"pid {os.getpid()} (live)" in refused.value.detail
            assert "warmup_first_run" not in str(refused.value)
            assert admission.reset.shared  # the run's agent may start: its share is back
            require_admission(config)
    finally:
        release.set()
        thread.join(WAIT_S)


def test_o11_a_dead_holder_beside_a_live_one_is_shown_as_holder_gone(tmp_path):
    """O11: the record of the holds names the process each was taken by; one whose process is gone is shown as
    `holder_gone`, beside the live holder whose hold refused the section."""
    config, _settings, lock = federation(tmp_path, admission_timeout_s=SHORT_S)
    platform = Path(config.file)
    child = multiprocessing.get_context("spawn").Process(target=hold_and_die, args=(str(platform), "killed_tool"))
    child.start()
    child.join(120)
    assert child.exitcode == 0
    release = threading.Event()
    thread, _ = tool_in_thread(config, release)
    try:
        with lock.held(exclusive=False, timeout_s=PROBE_S):
            with pytest.raises(ResetLockBusy) as refused, exclusive(config, shortcut()):
                pass
            statuses = sorted(holder["status"] for holder in refused.value.holders)
            assert statuses == ["holder_gone", "live"]
            gone = next(h for h in refused.value.holders if h["status"] == "holder_gone")
            assert gone["pid"] == child.pid
            assert f"pid {child.pid} (holder_gone)" in refused.value.detail
    finally:
        release.set()
        thread.join(WAIT_S)


def test_o11_a_grant_that_finds_a_dead_holder_gives_no_shortcut_and_ends_its_record(tmp_path):
    """O11: a holder that died may have left a read in flight, and a tool server it started may still run: the
    kernel let its lock go, so the exclusive hold is granted, but the shortcut is refused (`holder_gone`, full reads)
    and the dead holder's line is ended as `holder_gone`. The hold is kept for the whole section (O66: the run's share
    used to be taken back at once, and the full reset then ran beside every holder); the share is back after it."""
    config, settings, lock = federation(tmp_path)
    child = multiprocessing.get_context("spawn").Process(target=hold_and_die, args=(str(config.file), "killed_tool"))
    child.start()
    child.join(120)
    with lock.held(exclusive=False, timeout_s=PROBE_S) as admission:
        with exclusive(config, shortcut()) as grant:
            assert (grant.why, grant.shortcut, grant.held) == (HOLDER_GONE, False, True)
            (holder,) = grant.facts()["holders"]
            assert (holder["pid"], holder["status"]) == (child.pid, "holder_gone")
            assert not admission.reset.shared
            assert not free(settings, FEDERATION_RESET)
        assert admission.reset.shared
    dead = [hold for hold in reset_holds(settings) if hold["owner"]["process"].split(":")[1] == str(child.pid)]
    assert [hold["how"] for hold in dead] == ["holder_gone"]


# --- deadlocks, outages and the main share --------------------------------------------------------------------------


def test_o11_two_resetters_do_not_deadlock_and_never_hold_the_lock_together(tmp_path):
    """O11: two runs reset at once: each gives up its own share before it waits, so neither waits on the other's
    share; one is granted, the other is granted once the first has ended its section, and their exclusive holds never
    overlap."""
    config, _, lock = federation(tmp_path, admission_timeout_s=10)
    barrier = threading.Barrier(2)
    spans: dict[str, tuple[float, float]] = {}

    def resetter(name):
        def run():
            with lock.held(exclusive=False, timeout_s=WAIT_S):
                barrier.wait(WAIT_S)
                with exclusive(config, shortcut(5.0)) as grant:
                    began = time.monotonic()
                    time.sleep(0.3)
                    spans[name] = (began, time.monotonic())
                    return grant.why

        return run

    threads = [in_thread(resetter(name), name=name) for name in ("first", "second")]
    for thread, _ in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread, _ in threads), "a resetter hung"
    assert [landed for _, landed in threads] == [[None], [None]]
    (first_in, first_out), (second_in, second_out) = spans["first"], spans["second"]
    assert first_out <= second_in or second_out <= first_in, "two exclusive holds of the reset lock overlapped"


def test_o11_an_outage_run_cannot_get_in_during_a_shortcut_reset(tmp_path):
    """O11: the resetting run never gives up its share of `federation.lock`, so an outage run's exclusive admission is
    refused while the reset holds the reset lock exclusively."""
    config, _, lock = federation(tmp_path)
    with lock.held(exclusive=False, timeout_s=PROBE_S), exclusive(config, shortcut()) as grant:
        assert grant.shortcut
        with pytest.raises(FederationBusy):
            lock.acquire(exclusive=True, timeout_s=PROBE_S, target=OUTAGE_TARGET)
    outage = lock.acquire(exclusive=True, timeout_s=PROBE_S, target=OUTAGE_TARGET)
    outage.close()


def test_o11_the_runs_share_of_the_federation_lock_is_held_throughout_the_shortcut(tmp_path):
    """O11: through the share given up, the wait, the exclusive hold, its end and the share taken back, a watcher that
    keeps trying `federation.lock` exclusively never gets it: the run's main share never lapses."""
    config, settings, lock = federation(tmp_path, admission_timeout_s=10)
    stop, got = threading.Event(), []

    def watch():
        descriptor = os.open(Path(settings.journal) / FEDERATION_LOCK, os.O_RDWR | os.O_CLOEXEC)
        try:
            while not stop.is_set():
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    time.sleep(0.002)  # a poll, never a busy loop
                    continue
                got.append(time.monotonic())
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    release = threading.Event()
    with lock.held(exclusive=False, timeout_s=PROBE_S) as admission:
        main = admission.lock
        watcher, _ = in_thread(watch)
        tool, _ = tool_in_thread(config, release)  # makes the reset wait, its own share given up
        threading.Timer(0.3, release.set).start()
        with exclusive(config, shortcut()) as grant:
            assert grant.shortcut
            time.sleep(0.1)
        time.sleep(0.1)
        assert admission.lock is main
        assert main.held
        stop.set()
        watcher.join(WAIT_S)
        tool.join(WAIT_S)
    assert got == [], "the run's share of federation.lock lapsed during its shortcut reset"


def test_o11_a_worker_that_holds_a_share_keeps_the_shortcut_off_and_the_lock_is_held_all_the_same(tmp_path):
    """O11: a worker of the run that holds a share of its admission (a sandbox's start thread, a task reusing the
    admission through a tool's hold) may be reading, which the section cannot rule out: no shortcut
    (`shares_retained`). The run's share of the reset lock is still given up and the lock held exclusively, so no other
    holder acts during the reset (O66: nothing used to be given up, and the reset ran beside every holder); nothing of
    the run may act meanwhile, and its share is back after the section."""
    config, settings, lock = federation(tmp_path)
    with lock.held(exclusive=False, timeout_s=PROBE_S) as admission:
        give_up = admission.retain()
        with exclusive(config, shortcut()) as grant:
            assert (grant.why, grant.shortcut, grant.mode, grant.held) == (SHARES_RETAINED, False, EXCLUSIVE, True)
            assert not admission.reset.shared
            assert not admission.may_act
            assert not free(settings, FEDERATION_RESET)
        assert admission.reset.shared
        give_up()


def test_o11_a_section_entered_after_the_owner_left_a_worker_holding_the_last_share_is_refused(tmp_path):
    """O11: the owner has left and a worker it handed a share still holds the admission: a section entered from a
    context that inherited it is refused, giving nothing up, since the worker's release would let the hold go during
    the reset (O66: it used to give no shortcut, and the reset ran beside every holder)."""
    config, settings, lock = federation(tmp_path)
    with lock.held(exclusive=False, timeout_s=PROBE_S) as admission:
        give_up = admission.retain()
    assert admission.held  # the worker's share keeps it
    assert admission.seal() is False
    token = admission_module._ADMISSION.set(admission)  # a context that inherited it, as a task inherits its parent's
    try:
        with pytest.raises(AdmissionRefused, match="owner"), exclusive(config, shortcut()):
            pass
        assert admission.reset.shared
        assert not free(settings, FEDERATION_RESET)
    finally:
        admission_module._ADMISSION.reset(token)
        give_up()
    assert free(settings, FEDERATION_RESET)


def test_o11_no_worker_takes_a_share_while_a_section_is_held(tmp_path):
    """O11: once the section is entered, the admission is sealed: no worker of the run takes a share, and nothing of the
    run may act; both are open again once it ends."""
    config, _, lock = federation(tmp_path)
    with lock.held(exclusive=False, timeout_s=PROBE_S) as admission:
        with exclusive(config, shortcut()) as grant:
            assert grant.shortcut
            assert admission.try_retain() is None
            assert not admission.may_act
        give_up = admission.try_retain()
        assert give_up is not None
        give_up()
        assert admission.may_act


def test_o11_no_section_joins_a_grant_its_last_user_is_ending(tmp_path, monkeypatch):
    """O11: once the last section using a grant has left, the grant is ending: a nested section that would join it in
    that window is not given it, so no reset begins under a hold about to be let go."""
    config, _, lock = federation(tmp_path)
    joined: list[bool] = []
    end = resetgrant.ResetGrant.end

    def ending(self):
        joined.append(self.join())
        end(self)

    monkeypatch.setattr(resetgrant.ResetGrant, "end", ending)
    with lock.held(exclusive=False, timeout_s=PROBE_S), exclusive(config, shortcut()) as grant:
        assert grant.held
    assert joined == [False]


def test_o11_a_dead_holder_keeps_refusing_the_shortcut_for_its_quiet_window(tmp_path, monkeypatch):
    """O11: what a dead holder left behind (a tool server in a session of its own, a read the provider still serves)
    may outlive the section that found it: the next sections refuse the shortcut too, naming it, until
    `DEAD_HOLDER_QUIET_S` has passed since it was found."""
    config, _, lock = federation(tmp_path)
    child = multiprocessing.get_context("spawn").Process(target=hold_and_die, args=(str(config.file), "killed_tool"))
    child.start()
    child.join(120)
    with lock.held(exclusive=False, timeout_s=PROBE_S):
        for _ in range(2):
            with exclusive(config, shortcut()) as grant:
                assert grant.why == HOLDER_GONE
                assert [holder["pid"] for holder in grant.facts()["holders"]] == [child.pid]
        monkeypatch.setattr(resetgrant, "DEAD_HOLDER_QUIET_S", 0.0)
        with exclusive(config, shortcut()) as grant:
            assert grant.shortcut


def test_o11_a_live_process_whose_end_was_not_recorded_refuses_no_shortcut(tmp_path):
    """O11: a line left open by a live process that let the lock go (its end could not be written) is ended as
    `lock_free`, not `holder_gone`, and refuses no shortcut now or later."""
    config, settings, lock = federation(tmp_path)
    SharedHolds(settings.journal, name=RESET_HOLDS).begin(OUTSIDE_CAMPAIGN)  # this live process holds no lock
    with lock.held(exclusive=False, timeout_s=PROBE_S):
        for _ in range(2):
            with exclusive(config, shortcut()) as grant:
                assert grant.shortcut
    first = reset_holds(settings)[0]
    assert first["how"] == "lock_free"


def test_o11_a_section_of_one_federation_stands_for_none_of_another(tmp_path):
    """O11: a nested section for another federation's configuration is refused, not given the outer grant, and a
    reading made in one federation's section gives the full reads to a reset of another (`other_federation`)."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    config_a, _, lock_a = federation(tmp_path / "a")
    config_b, _, _ = federation(tmp_path / "b")
    with lock_a.held(exclusive=False, timeout_s=PROBE_S), exclusive(config_a, shortcut()) as grant:
        assert grant.shortcut
        with pytest.raises(AdmissionRefused, match="another federation"), exclusive(config_b, shortcut()):
            pass
        reading = SimpleNamespace(grant=grant, read_epoch=1.0, scan=SimpleNamespace(listing=Listing()), held={})
        assert resetshortcut.initial(shortcut(), reading, federation_for(config_b))["why"] == "other_federation"
        assert resetshortcut.initial(shortcut(), reading, federation_for(config_a))["mode"] == "shortcut"


def test_o11_a_health_check_in_the_resetting_runs_own_context_is_deferred(tmp_path):
    """O11: a health check that inherits the resetting run's admission (a task of the run) reads a study's file, so it
    is deferred, uncharged, while the run's own section holds the federation."""
    config, settings, lock = federation(tmp_path)
    with lock.held(exclusive=False, timeout_s=PROBE_S), exclusive(config, shortcut()) as grant:
        assert grant.shortcut
        health = FaultGate(settings, FakeServices())()
        assert (health.ok, health.deferred, health.charge) == (False, True, False), health.detail


def test_o11_a_health_check_in_the_runs_own_context_holds_a_share_so_no_section_begins_while_it_reads(tmp_path):
    """O11: a health check that inherits the run's admission (a thread or task of the run) holds a share of it for as
    long as it reads, so the run's own shortcut section, which seals the admission only while no worker holds a share,
    cannot begin while the check reads a study's file; the share is given back once the check has answered."""
    _, settings, lock = federation(tmp_path)
    sealed: list[bool] = []
    with lock.held(exclusive=False, timeout_s=PROBE_S) as admission:

        def reading():
            sealed.append(admission.seal())
            return Health(True, "read")

        health = FaultGate(settings, FakeServices(), reading)()
        assert health.ok, health.detail
        assert sealed == [False]
        assert admission.seal() is True  # the check's share is given back
        admission.unseal()


def test_o11_a_start_under_the_runs_admission_is_refused_while_a_section_holds_it_or_its_share_is_given_up(tmp_path):
    """O11: a start that acts on the federation (a sandbox's) takes a share of the run's admission it must have: it is
    refused while the run's section seals the admission, and while the run's share of the reset lock is given up after
    the section, and a refused start keeps no share."""
    config, _, lock = federation(tmp_path)
    with lock.held(exclusive=False, timeout_s=PROBE_S) as admission:
        with exclusive(config, shortcut()) as grant:
            assert grant.shortcut
            with pytest.raises(AdmissionRefused, match="reset"):
                retain_to_act()
        admission.reset.give_up()  # a share that was not taken back after a section; the admission is unsealed
        with pytest.raises(AdmissionRefused, match="reset"):
            retain_to_act()
        assert admission.seal() is True  # the refused start gave its share back
        admission.unseal()
    assert retain_to_act()() is None  # no admission in the context: nothing to hold


def test_o11_an_interrupt_while_the_section_reads_its_holders_lets_the_exclusive_hold_go_and_takes_the_share_back(
    tmp_path, monkeypatch
):
    """O11: an interrupt after the exclusive hold is taken and before the grant exists (while the record of the holders
    is read and settled) lets the exclusive hold go and takes the run's share back: no reader is kept out and the run
    may act."""
    config, settings, lock = federation(tmp_path)

    def interrupted(holds):
        raise KeyboardInterrupt

    monkeypatch.setattr(resetgrant, "_settled", interrupted)
    with lock.held(exclusive=False, timeout_s=PROBE_S) as admission:
        with pytest.raises(KeyboardInterrupt), exclusive(config, shortcut()):
            pass
        assert admission.reset.shared
        assert admission.may_act
        descriptor = os.open(Path(settings.journal) / FEDERATION_RESET, os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)  # BlockingIOError while it is held exclusively
        finally:
            os.close(descriptor)


def test_o11_a_health_check_during_a_shortcut_reset_is_deferred_uncharged_and_passes_after_it(tmp_path):
    """O11: the health check's shared hold takes its share of the reset lock like every shared admission (its first
    call reads a file of a study's space): during a shortcut reset it is deferred, charging no run, and the exclusive
    hold is never broken; once the reset has ended it passes."""
    config, settings, lock = federation(tmp_path, health_wait_s=0.3)
    with lock.held(exclusive=False, timeout_s=PROBE_S):
        with exclusive(config, shortcut()) as grant:
            thread, landed = in_thread(lambda: FaultGate(settings, FakeServices())())  # no admission in its context
            thread.join(WAIT_S)
            (health,) = landed
            assert (health.ok, health.deferred, health.charge) == (False, True, False), health.detail
            assert grant.held
        thread, landed = in_thread(lambda: FaultGate(settings, FakeServices())())
        thread.join(WAIT_S)
        (health,) = landed
        assert health.ok, health.detail


def test_o11_no_tool_and_no_agent_start_while_the_runs_reset_share_is_given_up(tmp_path):
    """O11: while its share of the reset lock is given up, the run's infrastructure may not start
    (`require_admission`, which the tool server and the sandbox call) and no tool may reuse its admission."""
    config, _, lock = federation(tmp_path)
    with lock.held(exclusive=False, timeout_s=PROBE_S), exclusive(config, shortcut()) as grant:
        assert grant.shortcut
        with pytest.raises(AdmissionRefused, match="reset"):
            require_admission(config)
        with pytest.raises(AdmissionRefused, match="reset"), shared_admission(config, reason=REASON):
            pass
    with lock.held(exclusive=False, timeout_s=PROBE_S):
        require_admission(config)


def test_o11_a_run_holding_the_federation_exclusively_has_the_shortcut_without_a_wait(tmp_path):
    """O11: a run that holds `federation.lock` exclusively (a matched control declared `exclusive = true`) shares the
    federation with no one, so its reset has the shortcut at once."""
    config, _settings, lock = federation(tmp_path)
    with lock.held(exclusive=True, timeout_s=PROBE_S, purpose="exclusive_run"), exclusive(config, shortcut()) as grant:
        assert (grant.mode, grant.why, grant.held) == (FEDERATION_EXCLUSIVE, None, True)


def test_o11_with_the_shortcut_off_the_lock_is_held_and_with_no_admission_the_section_is_refused(tmp_path):
    """O11: with `reset_shortcut` off (the default) the section still holds the lock exclusively and gives no shortcut
    (`off`); with no admission held, on a platform with `[faults]`, it is refused whatever the setting, taking nothing
    (O66: with the shortcut off nothing used to be taken, and with no admission the reset ran unheld)."""
    config, settings, lock = federation(tmp_path)
    for placement in (PlacementSettings(holder="alpha"), shortcut()):
        with pytest.raises(AdmissionRefused, match="shared_admission"), exclusive(config, placement):
            pass
    assert reset_holds(settings) == []
    with lock.held(exclusive=False, timeout_s=PROBE_S) as admission:
        with exclusive(config, PlacementSettings(holder="alpha")) as grant:
            assert (grant.why, grant.mode, grant.held, grant.shortcut) == (OFF, EXCLUSIVE, True, False)
            assert not free(settings, FEDERATION_RESET)
        assert admission.reset.shared


def test_o11_a_platform_without_faults_gives_no_shortcut(tmp_path):
    """O11: without `[faults]` there is no lock to hold, so the shortcut is unavailable."""
    config = load_platform(write_platform(tmp_path), require_secrets=False)
    with exclusive(config, shortcut()) as grant:
        assert grant.why == NO_ADMISSION


def test_o11_the_share_is_taken_back_when_the_reset_body_raises(tmp_path):
    """O11: a reset that fails inside its exclusive section lets the exclusive hold go and takes the share back."""
    config, settings, lock = federation(tmp_path)
    with lock.held(exclusive=False, timeout_s=PROBE_S) as admission:
        with pytest.raises(RuntimeError, match="boom"), exclusive(config, shortcut()):
            raise RuntimeError("boom")
        assert admission.reset.shared
        assert not free(settings, FEDERATION_RESET)
    assert free(settings, FEDERATION_RESET)


def test_o11_a_share_that_cannot_be_taken_back_refuses_the_runs_agent(tmp_path):
    """O11: once the exclusive hold ends another reset may take the lock first; past the admission's bound the share is
    not back, which raises an infrastructure failure and leaves the run unable to start its agent."""
    config, _, lock = federation(tmp_path, admission_timeout_s=0.3)
    other = lock.acquire(exclusive=False, timeout_s=PROBE_S)
    other.reset.give_up()  # a second resetter, waiting for the lock
    with lock.held(exclusive=False, timeout_s=PROBE_S) as admission:
        share = admission.reset
        ended = share.end_exclusive

        def and_taken_by_the_other():
            ended()
            assert other.reset.try_exclusive()  # the second reset gets in before the share is taken back

        share.end_exclusive = and_taken_by_the_other
        with pytest.raises(FederationBusy, match="taken back"), exclusive(config, shortcut()) as grant:
            assert grant.shortcut
        assert not share.shared
        with pytest.raises(AdmissionRefused, match="reset"):
            require_admission(config)
    other.reset.end_exclusive()
    other.close()


def test_o11_a_reset_that_fails_keeps_its_failure_when_its_share_cannot_be_taken_back(tmp_path):
    """O11: a reset that failed inside its section raises its own failure, which says why the reset failed, with the
    share's failure noted on it; the share stays given up, so the run's agent is refused all the same."""
    config, _, lock = federation(tmp_path, admission_timeout_s=0.3)
    other = lock.acquire(exclusive=False, timeout_s=PROBE_S)
    other.reset.give_up()

    def failing_reset():
        with exclusive(config, shortcut()):
            raise RuntimeError("the reset failed at its quiet stage")

    with lock.held(exclusive=False, timeout_s=PROBE_S) as admission:
        share = admission.reset
        ended = share.end_exclusive

        def and_taken_by_the_other():
            ended()
            assert other.reset.try_exclusive()

        share.end_exclusive = and_taken_by_the_other
        with pytest.raises(RuntimeError, match="quiet stage") as caught:
            failing_reset()
        assert any("not taken back" in note for note in caught.value.__notes__)
        with pytest.raises(AdmissionRefused, match="reset"):
            require_admission(config)
    other.reset.end_exclusive()
    other.close()


# --- no lock outlives its holder ------------------------------------------------------------------------------------


@pytest.mark.parametrize("point", ["agent", "reset"])
def test_o11_a_campaign_process_killed_mid_run_leaves_both_locks_free(tmp_path, point):
    """O11: a campaign run killed with SIGKILL, in its agent phase or inside its shortcut reset, with a child started
    the way the tool server is (its own session, still running after the kill), leaves both `federation.lock` and
    `federation.reset` free: no descriptor of either reached the child."""
    _, settings, _ = federation(tmp_path)
    out = tmp_path / "pids.json"
    platform = str(tmp_path / "platform.toml")
    process = multiprocessing.get_context("spawn").Process(target=run_and_hang, args=(platform, str(out), point))
    process.start()
    pids = {}
    try:
        deadline = time.monotonic() + 120
        while not out.exists() and time.monotonic() < deadline and process.is_alive():
            time.sleep(0.05)
        pids = json.loads(out.read_text(encoding="utf-8"))
        assert not free(settings, FEDERATION_LOCK)
        assert not free(settings, FEDERATION_RESET)
        os.kill(pids["parent"], signal.SIGKILL)
        process.join(WAIT_S)
        os.kill(pids["child"], 0)  # the tool server's stand-in outlived its parent
        assert free(settings, FEDERATION_LOCK)
        assert free(settings, FEDERATION_RESET)
    finally:
        if process.is_alive():
            process.kill()
            process.join(WAIT_S)
        if "child" in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pids["child"], signal.SIGKILL)


def test_o11_a_forked_child_keeps_no_copy_of_the_reset_share(tmp_path):
    """O11: the fork hook closes every lock descriptor a forked child inherits, the reset lock's among them: with the
    child still alive, its parent's share gone lets a reset take the lock exclusively."""
    platform = write_faults(tmp_path)
    out = tmp_path / "fork.json"
    child = multiprocessing.get_context("spawn").Process(target=hold_and_fork, args=(str(platform), str(out)))
    child.start()
    child.join(120)  # the grandchild holds the child's sentinel pipe, so this ends when the grandchild has ended too
    result = json.loads(out.read_text(encoding="utf-8"))
    try:
        assert result["grandchild_alive"] is True  # it was alive, with whatever it inherited, when the lock was tried
        assert result["free"] is True
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(result["grandchild"], signal.SIGKILL)


def test_o11_no_lock_descriptor_is_inheritable_and_no_exec_child_receives_one(tmp_path):
    """O11: every lock descriptor of an admission and of a shortcut's exclusive hold is opened non-inheritable, so even
    a child started with `close_fds=False` holds none of the lock files."""
    config, settings, lock = federation(tmp_path)
    with lock.held(exclusive=False, timeout_s=PROBE_S), exclusive(config, shortcut()) as grant:
        assert grant.shortcut
        assert _HELD, "the test holds no lock"
        assert all(os.get_inheritable(descriptor) is False for descriptor in _HELD)
        listing = (
            "import os\n"
            "for n in os.listdir('/proc/self/fd'):\n"
            "    try:\n"
            "        print(os.readlink(f'/proc/self/fd/{n}'))\n"
            "    except OSError:\n"
            "        pass  # the listing's own descriptor, closed by now\n"
        )
        seen = subprocess.run(
            [sys.executable, "-c", listing], close_fds=False, capture_output=True, text=True, check=True, timeout=60
        ).stdout
    journal = str(Path(settings.journal).resolve())
    assert journal not in seen


def test_o11_no_subprocess_call_in_the_package_passes_descriptors_on():
    """O11: no `subprocess` call of the package passes descriptors to a child (`pass_fds`, `close_fds=False`), which
    could hand a lock to a process that outlives its holder. Every call of every module is read, by its keywords."""
    offending = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            for keyword in node.keywords if isinstance(node, ast.Call) else ():
                kept = keyword.arg == "close_fds" and not (
                    isinstance(keyword.value, ast.Constant) and keyword.value.value is True
                )
                if keyword.arg == "pass_fds" or kept:
                    offending.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offending == []


def test_o11_a_shared_admission_is_refused_when_its_reset_share_cannot_be_recorded(tmp_path, monkeypatch):
    """O11: the record of the reset lock's holds is what tells a reset that a holder died; a share that cannot be
    recorded is refused, and leaves neither lock held."""
    _config, settings, lock = federation(tmp_path)

    def broken(self, reason):
        raise OSError("disk full")

    monkeypatch.setattr(SharedHolds, "begin", broken)
    with pytest.raises(InfraError, match="could not be recorded"):
        lock.acquire(exclusive=False, timeout_s=PROBE_S)
    assert free(settings, FEDERATION_LOCK)
    assert free(settings, FEDERATION_RESET)
    assert current_admission() is None


def test_o11_a_share_taken_back_that_cannot_be_recorded_is_let_go_and_refuses_the_agent(tmp_path, monkeypatch):
    """O11: a share of the reset lock is never held without its line in the record: one taken back after a shortcut
    reset whose line cannot be written is let go again, the failure raised, and the run's agent refused, its share of
    `federation.lock` still held."""
    config, settings, lock = federation(tmp_path)

    def broken(self, reason):
        raise OSError("disk full")

    def section_that_breaks_the_record():
        with exclusive(config, shortcut()) as grant:
            assert grant.shortcut
            monkeypatch.setattr(SharedHolds, "begin", broken)

    with lock.held(exclusive=False, timeout_s=PROBE_S) as admission:
        with pytest.raises(InfraError, match="could not be recorded"):
            section_that_breaks_the_record()
        assert not admission.reset.shared
        assert free(settings, FEDERATION_RESET)
        assert not free(settings, FEDERATION_LOCK)
        with pytest.raises(AdmissionRefused, match="reset"):
            require_admission(config)
