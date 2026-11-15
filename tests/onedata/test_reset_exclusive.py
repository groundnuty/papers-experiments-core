"""O11: a shortcut reset holds the reset lock exclusively, and nothing that can place bytes holds it beside.

Every shared admission (a campaign run's, a tool's outside a campaign) holds `federation.reset` shared beside its share
of `federation.lock`. A shortcut reset gives up its own run's share of `federation.reset`, never of `federation.lock`,
and waits, within a bound, for `federation.reset` exclusively; past the bound it is told the shortcut is unavailable,
with the holders, and takes its share back. Every test runs against the real locks on the fixture's journal directory;
the waits are real seconds, kept short.
"""

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

import pytest
from fault_support import OUTAGE_TARGET, write_faults
from reset_lock_support import hold_and_fork, run_and_hang
from shared_support import hold_and_die

from expcore_onedata.admission import (
    FEDERATION_LOCK,
    AdmissionRefused,
    FederationBusy,
    FederationLock,
    current_admission,
    require_admission,
)
from expcore_onedata.config import load_platform
from expcore_onedata.fault_journal import _HELD, boot_id
from expcore_onedata.faults import load_faults
from expcore_onedata.rangemap import PlacementSettings
from expcore_onedata.resetgrant import (
    EXCLUSIVE,
    FEDERATION_EXCLUSIVE,
    HOLDER_GONE,
    NO_ADMISSION,
    OFF,
    UNAVAILABLE,
    exclusive,
)
from expcore_onedata.resetlock import FEDERATION_RESET, RESET_HOLDS
from expcore_onedata.shared_holds import SharedHolds
from expcore_onedata.testing import write_platform
from expcore_onedata.tool_admission import shared_admission

WAIT_S = 10.0  # the bound of a wait that must be granted
SHORT_S = 0.4  # the bound of a wait that must run out
PROBE_S = 0.2
REASON = "smoke: warm-up of the first space"
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


def test_o11_a_tools_hold_past_the_bound_makes_the_shortcut_unavailable_and_names_the_holders(tmp_path):
    """O11: a tool outside a campaign holds the reset lock shared past the bound: the shortcut is unavailable, never a
    failure; the grant says how long it waited and names the live holder by its process (pid, boot id, start time),
    never by its reason (O12), and the run has its share back for its agent."""
    config, _settings, lock = federation(tmp_path)
    release = threading.Event()
    thread, _ = tool_in_thread(config, release, reason="a reason that names the first space")
    try:
        with lock.held(exclusive=False, timeout_s=PROBE_S) as admission, exclusive(config, shortcut(SHORT_S)) as grant:
            assert (grant.mode, grant.why, grant.shortcut, grant.held) == (None, UNAVAILABLE, False, False)
            facts = grant.facts()
            assert facts["wait_s"] >= SHORT_S
            (holder,) = facts["holders"]
            assert holder["status"] == "live"
            assert (holder["pid"], holder["boot_id"]) == (os.getpid(), boot_id())
            assert isinstance(holder["start_time"], str)
            assert holder["start_time"]
            assert holder["in_campaign"] is False
            assert "first space" not in json.dumps(facts)
            assert admission.reset.shared  # the run's agent may start: its share is back
            require_admission(config)
    finally:
        release.set()
        thread.join(WAIT_S)


def test_o11_a_dead_holder_beside_a_live_one_is_shown_as_holder_gone(tmp_path):
    """O11: the record of the holds names the process each was taken by; one whose process is gone is shown as
    `holder_gone`, beside the live holder that kept the shortcut out."""
    config, _settings, lock = federation(tmp_path, admission_timeout_s=10)
    platform = Path(config.file)
    child = multiprocessing.get_context("spawn").Process(target=hold_and_die, args=(str(platform), "a killed tool"))
    child.start()
    child.join(120)
    assert child.exitcode == 0
    release = threading.Event()
    thread, _ = tool_in_thread(config, release)
    try:
        with lock.held(exclusive=False, timeout_s=PROBE_S), exclusive(config, shortcut(SHORT_S)) as grant:
            statuses = sorted(holder["status"] for holder in grant.facts()["holders"])
            assert grant.why == UNAVAILABLE
            assert statuses == ["holder_gone", "live"]
            gone = next(h for h in grant.facts()["holders"] if h["status"] == "holder_gone")
            assert gone["pid"] == child.pid
    finally:
        release.set()
        thread.join(WAIT_S)


def test_o11_a_grant_that_finds_a_dead_holder_gives_no_shortcut_and_ends_its_record(tmp_path):
    """O11: a holder that died may have left a read in flight, and a tool server it started may still run: the
    kernel let its lock go, so the exclusive hold is granted, but the shortcut is refused (`holder_gone`, full reads),
    the dead holder's line is ended as `holder_gone`, and the run's share is taken back at once."""
    config, settings, lock = federation(tmp_path)
    child = multiprocessing.get_context("spawn").Process(target=hold_and_die, args=(str(config.file), "a killed tool"))
    child.start()
    child.join(120)
    with lock.held(exclusive=False, timeout_s=PROBE_S) as admission, exclusive(config, shortcut()) as grant:
        assert (grant.why, grant.shortcut) == (HOLDER_GONE, False)
        (holder,) = grant.facts()["holders"]
        assert (holder["pid"], holder["status"]) == (child.pid, "holder_gone")
        assert admission.reset.shared
    dead = [hold for hold in reset_holds(settings) if hold["owner"]["process"].split(":")[1] == str(child.pid)]
    assert [hold["how"] for hold in dead] == ["holder_gone"]


# --- deadlocks, outages and the main share --------------------------------------------------------------------------


def test_o11_two_resetters_do_not_deadlock_and_never_hold_the_lock_together(tmp_path):
    """O11: two runs reset at once: each gives up its own share before it waits, so neither waits on the other's
    share; one is granted, the other is granted once the first has ended its run (or is told the shortcut is
    unavailable), and their exclusive holds never overlap."""
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


def test_o11_the_shortcut_is_off_by_default_and_unavailable_with_no_admission(tmp_path):
    """O11: with `reset_shortcut` off nothing is taken; with it on and no admission held there is no exclusivity to
    show, so the shortcut is unavailable."""
    config, settings, _lock = federation(tmp_path)
    with exclusive(config, PlacementSettings(holder="alpha")) as grant:
        assert (grant.why, grant.mode) == (OFF, None)
    with exclusive(config, shortcut()) as grant:
        assert (grant.why, grant.shortcut) == (NO_ADMISSION, False)
    assert reset_holds(settings) == []


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
    could hand a lock to a process that outlives its holder."""
    offending = [
        f"{path.relative_to(SRC)}:{number}"
        for path in SRC.rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "pass_fds" in line or "close_fds=False" in line.replace(" ", "")
    ]
    assert offending == []


def test_o11_a_shared_admission_is_refused_when_its_reset_share_cannot_be_recorded(tmp_path, monkeypatch):
    """O11: the record of the reset lock's holds is what tells a reset that a holder died; a share that cannot be
    recorded is refused, and leaves neither lock held."""
    _config, settings, lock = federation(tmp_path)

    def broken(self, reason):
        raise OSError("disk full")

    monkeypatch.setattr(SharedHolds, "begin", broken)
    with pytest.raises(Exception, match="could not be recorded"):
        lock.acquire(exclusive=False, timeout_s=PROBE_S)
    assert free(settings, FEDERATION_LOCK)
    assert free(settings, FEDERATION_RESET)
    assert current_admission() is None
