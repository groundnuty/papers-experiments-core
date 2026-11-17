"""O66: every reset, full or shortcut, holds the federation's reset lock exclusively for its whole run, or refuses.

Every shared admission (another campaign run's, a tool's outside a campaign, a health check's) holds `federation.reset`
shared for as long as it holds the federation. A reset evicts and reads block maps, so it must not run beside any of
them: `SpaceReset.run` (and `SpacesReset`) gives up its own run's share and waits for the lock exclusively, bounded by
`[faults] admission_timeout_s`, and at the bound refuses as `FederationBusy`, uncharged, with nothing sent. A shortcut
reset that cannot take the shortcut still holds the lock; only the reads change. The federation is the fake one; the
locks are real, on the fixture's journal directory, and their waits are real seconds.
"""

import asyncio
import contextlib
import contextvars
import dataclasses
import fcntl
import multiprocessing
import os
import re
import threading
import time
from pathlib import Path

import pytest
from fault_support import faults_table
from qos_world import FILES, federation, two_providers
from reset_lock_support import hold_until_told
from shared_support import hold_and_die

from expcore.errors import uncharged_kinds
from expcore_onedata.admission import (
    SANDBOX_STEP,
    TOOL_SERVER_STEP,
    AdmissionRefused,
    FederationBusy,
    FederationLock,
    require_admission,
    retain_to_act,
    run_admission,
    token_step,
)
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.faults import load_faults
from expcore_onedata.provision import Provisioner, resolve_provider_ids
from expcore_onedata.rangemap import load_placement
from expcore_onedata.reset import STAGES, ResetFailed, SpaceReset
from expcore_onedata.resetlock import FEDERATION_RESET
from expcore_onedata.resetspaces import ResetPlan, SpacesReset
from expcore_onedata.shortcode import checked_code
from expcore_onedata.testing import DATA, write_platform
from expcore_onedata.tool_admission import shared_admission

BOUND_S = 3.0  # the fixture's `[faults] admission_timeout_s`: the bound of a reset's wait for the lock
SHORT_S = 0.5  # a bound that must run out
LONG_S = 30.0  # a bound that must not run out before the test lets the holder go
ALLOWED = {"beta": len(DATA) * len(FILES)}  # beta holds every file, and may
SPAWN = multiprocessing.get_context("spawn")


@pytest.fixture
def fake(monkeypatch, ticker):
    """The QoS world's federation, recording this host's monotonic instant of every request it is sent (`sent`)."""
    world = federation(monkeypatch, ticker)
    sent: list[float] = []
    answer = world.respond

    def recording(request):
        sent.append(time.monotonic())
        return answer(request)

    world.respond = recording
    world.sent = sent
    return world


@pytest.fixture
def platform_file(tmp_path, request):
    """Two supporting providers and `[faults]` whose `admission_timeout_s` is the test's parameter (`BOUND_S`)."""
    bound = getattr(request, "param", BOUND_S)

    def edit(data):
        two_providers(data)
        data["faults"] = faults_table(admission_timeout_s=bound)

    return write_platform(tmp_path, edit)


@pytest.fixture
def lock(platform_file, config):
    return FederationLock(load_faults(platform_file, config))


@pytest.fixture
def journal(platform_file, config):
    return Path(load_faults(platform_file, config).journal)


def provisioner(client, config, platform_file, ticker, tmp_path, *, shortcut=False, wait_s=60.0):
    """A provisioner on the test's clock that finds QoS requirements by the listing, the shortcut on or off."""
    settings = dataclasses.replace(
        load_placement(platform_file, config).settings,
        qos_scan="listing",
        reset_shortcut=shortcut,
        reset_exclusive_wait_s=wait_s,
    )
    return Provisioner(
        client,
        config,
        settings,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    )


def shared_free(journal: Path) -> bool:
    """Whether a reader could take `federation.reset` shared now, on a descriptor of the test's own."""
    descriptor = os.open(journal / FEDERATION_RESET, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(descriptor)
    return True


def tool_in_thread(config, release, reason="smoke_warmup"):
    """A tool outside a campaign holding the shared admission in a thread until `release` is set."""
    holding = threading.Event()

    def tool():
        with shared_admission(config, reason=reason):
            holding.set()
            release.wait(LONG_S)

    thread = threading.Thread(target=tool)
    thread.start()
    assert holding.wait(LONG_S)
    return thread


@contextlib.contextmanager
def holder_process(tmp_path, platform_file):
    """Another campaign run in a process of its own, holding its shared admission until `let_go` is called: yields its
    pid, `let_go` and the reader of the monotonic instant it began letting go at."""
    held, release, releasing = (str(tmp_path / name) for name in ("held", "release", "releasing"))
    child = SPAWN.Process(target=hold_until_told, args=(str(platform_file), held, release, releasing))
    child.start()
    try:
        deadline = time.monotonic() + 120
        while not Path(held).exists():
            assert child.is_alive(), "the holder ended before it held the federation"
            assert time.monotonic() < deadline, "the holder never held the federation"
            time.sleep(0.02)

        def let_go():
            Path(release).write_text("", encoding="utf-8")

        def released_at():
            child.join(60)
            assert child.exitcode == 0
            return float(Path(releasing).read_text(encoding="utf-8"))

        yield int(Path(held).read_text(encoding="utf-8")), let_go, released_at
    finally:
        Path(release).write_text("", encoding="utf-8")
        child.join(60)
        if child.is_alive():
            child.kill()
            child.join(10)


# --- a reset waits for every other holder, then proceeds ---------------------------------------------------------


@pytest.mark.parametrize("platform_file", [LONG_S], indirect=True)
def test_o66_no_reset_starts_while_another_process_holds_a_shared_admission(
    client, config, platform_file, ticker, tmp_path, fake, lock
):
    """O66: another campaign run, in a process of its own, holds its shared admission (and so `federation.reset`
    shared) through the real lock: a full reset of this run, with the shortcut off and no section, sends nothing while
    it holds, waits, and starts only after the other process began letting go; it then proceeds and passes."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    with holder_process(tmp_path, platform_file) as (_, let_go, released_at):
        seen: list[int] = []

        def later():
            time.sleep(0.5)
            seen.append(len(fake.sent))
            let_go()

        helper = threading.Thread(target=later)
        with lock.held(exclusive=False, timeout_s=1.0) as admission:
            before = len(fake.sent)
            helper.start()
            record = SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)
            assert admission.reset.shared  # the run's share is back for its agent
        helper.join(LONG_S)
        assert seen == [before], "the reset sent a request while another process held the federation"
        first = fake.sent[before]
        assert first > released_at(), "the reset began before the other process let its share go"
    assert record["ok"] is True
    assert (record["reset_lock"]["mode"], record["reset_lock"]["why"]) == ("exclusive", "off")
    assert record["reset_lock"]["wait_s"] >= 0.45


def test_o66_a_second_runs_reset_waits_for_the_first_runs_release_then_proceeds(
    client, config, platform_file, ticker, tmp_path, fake, lock
):
    """O66: another run of this process, its agent running, holds its share; this run's reset waits for its release,
    longer than `reset_exclusive_wait_s` (no longer a bound), and then proceeds, nothing sent before."""
    p = provisioner(client, config, platform_file, ticker, tmp_path, wait_s=0.0)
    other = lock.acquire(exclusive=False, timeout_s=1.0)  # another campaign run, its agent running
    seen: list[tuple[int, float]] = []

    def release():
        seen.append((len(fake.sent), time.monotonic()))
        other.close()

    letting_go = threading.Timer(0.5, release)
    with lock.held(exclusive=False, timeout_s=1.0):
        before = len(fake.sent)
        letting_go.start()
        record = SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)
    letting_go.join(LONG_S)
    ((count, released),) = seen
    assert count == before, "the reset sent a request while the other run held its share"
    assert fake.sent[before] > released
    assert record["ok"] is True
    assert record["reset_lock"]["wait_s"] >= 0.45


# --- at its bound a reset refuses, uncharged, naming who held -------------------------------------------------------


@pytest.mark.parametrize("platform_file", [SHORT_S], indirect=True)
def test_o66_a_reset_at_its_bound_refuses_uncharged_names_the_holder_and_sends_nothing(
    client, config, platform_file, ticker, tmp_path, fake, lock
):
    """O66: a tool outside a campaign holds the federation past `admission_timeout_s`: the reset is refused as
    `FederationBusy` (`federation_busy`, which costs the run no attempt), never run under the tool; the refusal names
    the holder by its process (pid), never by its reason (O12); nothing was sent; and the run has its share back."""
    p = provisioner(client, config, platform_file, ticker, tmp_path, shortcut=True)
    release = threading.Event()
    thread = tool_in_thread(config, release, reason="warmup_first_run")
    try:
        with lock.held(exclusive=False, timeout_s=1.0) as admission:
            before = len(fake.sent)
            began = time.monotonic()
            with pytest.raises(FederationBusy) as refused:
                SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)
            assert time.monotonic() - began >= SHORT_S
            assert len(fake.sent) == before, "a refused reset sent a request"
            assert admission.reset.shared
    finally:
        release.set()
        thread.join(LONG_S)
    error = refused.value
    assert error.kind in uncharged_kinds()
    assert "reset lock" in error.detail
    assert f"pid {os.getpid()}" in error.detail
    assert "warmup_first_run" not in str(error)
    (holder,) = error.holders
    assert (holder["pid"], holder["status"]) == (os.getpid(), "live")
    assert error.wait_s >= SHORT_S


@pytest.mark.parametrize("platform_file", [SHORT_S], indirect=True)
def test_o66_a_reset_refuses_at_its_bound_while_another_process_holds_and_names_its_pid(
    client, config, platform_file, ticker, tmp_path, fake, lock
):
    """O66: another run's process holds its shared admission past the bound: the reset is refused, naming that
    process's pid, and sends nothing."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    with holder_process(tmp_path, platform_file) as (pid, let_go, _):
        with lock.held(exclusive=False, timeout_s=1.0):
            before = len(fake.sent)
            with pytest.raises(FederationBusy) as refused:
                SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)
            assert len(fake.sent) == before
        let_go()
    assert f"pid {pid}" in refused.value.detail
    assert [holder["pid"] for holder in refused.value.holders] == [pid]


@pytest.mark.parametrize("platform_file", [LONG_S], indirect=True)
def test_o66_the_runs_cancel_stops_a_resets_wait(client, config, platform_file, ticker, tmp_path, fake, lock):
    """O66: the bound is the admission's (an hour by default), so a reset's wait stops when the run's admission is
    cancelled (the campaign ending): refused as `FederationBusy`, uncharged, long before the bound, nothing sent."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    release, cancel = threading.Event(), threading.Event()
    thread = tool_in_thread(config, release)
    try:
        with lock.held(exclusive=False, timeout_s=1.0, cancel=cancel) as admission:
            threading.Timer(0.3, cancel.set).start()
            before, began = len(fake.sent), time.monotonic()
            with pytest.raises(FederationBusy, match="cancelled"):
                SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)
            assert time.monotonic() - began < LONG_S / 3
            assert len(fake.sent) == before
            assert admission.reset.shared
    finally:
        release.set()
        thread.join(LONG_S)


# --- no reset blocks on its own run's hold ---------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["full", "shortcut", "section", "driver", "twice"])
def test_o66_a_lone_run_resets_at_once_under_its_own_shared_admission(
    client, config, platform_file, ticker, tmp_path, fake, lock, how
):
    """O66: the run's own share is given up before the reset waits, so a run alone on the federation is never kept
    out by its own admission: a full reset, a shortcut reset with no section, one inside the run's own section (the
    section's hold is used, not taken twice), a reset through `SpacesReset.run` and two resets in a row each hold the
    lock at once, and the run has its share back after each."""
    p = provisioner(client, config, platform_file, ticker, tmp_path, shortcut=how != "full")
    reset = SpaceReset(p, now=ticker.clock)
    with lock.held(exclusive=False, timeout_s=1.0) as admission:
        began = time.monotonic()
        if how == "section":
            with reset.exclusive() as grant:
                holdings = reset.plan_scan()
                records = [reset.run(holdings.plan(), allowed=ALLOWED, planned=holdings, qos_rights=True)]
                assert grant.held
        elif how == "driver":
            records = SpacesReset([reset]).run([ResetPlan(allowed=ALLOWED, qos_rights=True)])["spaces"]
        else:
            runs = 2 if how == "twice" else 1
            records = [reset.run(allowed=ALLOWED, qos_rights=True) for _ in range(runs)]
        assert time.monotonic() - began < BOUND_S, "the run's own share kept its reset out"
        assert admission.reset.shared  # the run's own share, held again
    for record in records:
        assert record["reset_lock"]["mode"] == "exclusive"
        assert record["reset_lock"]["wait_s"] < BOUND_S
    if how == "section":
        assert records[0]["occupancy_reads"]["mode"] == "shortcut"


# --- the hold covers the whole reset, whatever the shortcut's verdict -------------------------------------------------


def hold_seen_at(monkeypatch, journal: Path) -> list[bool]:
    """Probe at the reset's first and last stages whether a reader could take `federation.reset` shared then."""
    seen: list[bool] = []
    qos, history = SpaceReset._qos, SpaceReset._history

    def probed_qos(self, *args, **kwargs):
        seen.append(shared_free(journal))
        return qos(self, *args, **kwargs)

    def probed_history(self, *args, **kwargs):
        seen.append(shared_free(journal))
        return history(self, *args, **kwargs)

    monkeypatch.setattr(SpaceReset, "_qos", probed_qos)
    monkeypatch.setattr(SpaceReset, "_history", probed_history)
    return seen


def test_o66_a_full_reset_holds_the_lock_from_its_first_stage_to_its_last(
    client, config, platform_file, ticker, tmp_path, fake, lock, journal, monkeypatch
):
    """O66: with the shortcut off (the default) the reset still holds `federation.reset` exclusively, so no reader can
    take it shared at its first stage or at its last; the lock is free again once the run's admission ends."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    seen = hold_seen_at(monkeypatch, journal)
    with lock.held(exclusive=False, timeout_s=1.0):
        SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)
    assert seen == [False, False]
    assert shared_free(journal)


def test_o66_a_dead_holder_refuses_the_shortcut_but_the_reset_keeps_the_hold(
    client, config, platform_file, ticker, tmp_path, fake, lock, journal, monkeypatch
):
    """O66: a holder that died leaves its line open: the shortcut is refused (`holder_gone`, full reads), and the reset
    keeps the exclusive hold for its whole run instead of letting it go at once."""
    child = SPAWN.Process(target=hold_and_die, args=(str(platform_file), "killed_tool"))
    child.start()
    child.join(120)
    assert child.exitcode == 0
    p = provisioner(client, config, platform_file, ticker, tmp_path, shortcut=True)
    reset = SpaceReset(p, now=ticker.clock)
    seen = hold_seen_at(monkeypatch, journal)
    with lock.held(exclusive=False, timeout_s=1.0), reset.exclusive() as grant:
        assert (grant.why, grant.shortcut, grant.held) == ("holder_gone", False, True)
        holdings = reset.plan_scan()
        record = reset.run(holdings.plan(), allowed=ALLOWED, planned=holdings, qos_rights=True)
    assert seen == [False, False]
    assert (record["occupancy_reads"]["mode"], record["occupancy_reads"]["why"]) == ("full", "holder_gone")


def test_o66_a_worker_holding_a_share_refuses_the_shortcut_but_the_reset_takes_the_hold(
    client, config, platform_file, ticker, tmp_path, fake, lock, journal, monkeypatch
):
    """O66: a worker of the run that holds a share of its admission may be reading, so the shortcut is refused
    (`shares_retained`), but the run's share of the reset lock is given up and the lock held exclusively all the same,
    so no other holder acts during the reset."""
    p = provisioner(client, config, platform_file, ticker, tmp_path, shortcut=True)
    seen = hold_seen_at(monkeypatch, journal)
    with lock.held(exclusive=False, timeout_s=1.0) as admission:
        give_up = admission.retain()
        try:
            record = SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)
        finally:
            give_up()
        assert admission.reset.shared
    assert seen == [False, False]
    assert (record["reset_lock"]["mode"], record["reset_lock"]["why"]) == ("exclusive", "shares_retained")


@pytest.mark.parametrize(("method", "stage"), [("_qos_settle", "qos_settle"), ("_history", "history")])
def test_o66_a_hold_that_lapses_during_a_stage_fails_that_stage_and_begins_no_later_one(
    client, config, platform_file, ticker, tmp_path, fake, lock, monkeypatch, method, stage
):
    """O66: a hold whose lock is let go under the reset (here, the share's descriptor closed during a stage) means
    another holder may have acted: the stage it lapsed in fails, naming the lapse, the last stage included (it used to
    end the reset `ok`), no later stage begins, and the caller must not start the next run."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    original = getattr(SpaceReset, method)

    def and_let_go(self, *args, **kwargs):
        facts = original(self, *args, **kwargs)
        admission.reset.close(record=True)  # the kernel's view: the exclusive descriptor is gone
        return facts

    monkeypatch.setattr(SpaceReset, method, and_let_go)
    with lock.held(exclusive=False, timeout_s=1.0) as admission, pytest.raises(ResetFailed, match="lapsed") as failed:
        SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)
    assert failed.value.stage == stage
    assert failed.value.record["stages"][stage]["ok"] is False
    assert failed.value.record["ok"] is False
    later = list(STAGES)[list(STAGES).index(stage) + 1 :]
    assert not any(name in failed.value.record["stages"] for name in later)


def in_context_thread(work):
    """Run `work` in a thread that inherits this context (its admission and its section), as asyncio.to_thread does;
    return the thread and where its result or error lands."""
    landed: list = []
    context = contextvars.copy_context()

    def run():
        try:
            landed.append(context.run(work))
        except BaseException as error:
            landed.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, landed


def paused_at_qos_settle(monkeypatch, journal: Path):
    """Pause the reset after its qos_settle stage until `go` is set (`paused` is set then), and record at its history
    stage whether a reader could take `federation.reset` shared."""
    paused, go, seen = threading.Event(), threading.Event(), []
    settle, history = SpaceReset._qos_settle, SpaceReset._history

    def pausing(self, *args, **kwargs):
        facts = settle(self, *args, **kwargs)
        paused.set()
        assert go.wait(LONG_S)
        return facts

    def probed(self, *args, **kwargs):
        seen.append(shared_free(journal))
        return history(self, *args, **kwargs)

    monkeypatch.setattr(SpaceReset, "_qos_settle", pausing)
    monkeypatch.setattr(SpaceReset, "_history", probed)
    return paused, go, seen


def test_o66_a_run_whose_owner_lets_its_admission_go_keeps_the_hold_until_its_reset_in_a_thread_ends(
    client, config, platform_file, ticker, tmp_path, fake, lock, journal, monkeypatch
):
    """O66: an attempt cancelled while its reset runs in a thread lets its admission go, but the reset's hold keeps a
    share of it: neither the reset lock nor `federation.lock` is let go until the reset ends, so no other holder gets
    in during it, and the reset passes; both are free once it has ended."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    paused, go, seen = paused_at_qos_settle(monkeypatch, journal)
    try:
        with lock.held(exclusive=False, timeout_s=1.0):
            thread, landed = in_context_thread(
                lambda: SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)
            )
            assert paused.wait(LONG_S)
        assert not shared_free(journal), "the owner's leaving let the reset lock go under its reset"
        with pytest.raises(FederationBusy):
            lock.acquire(exclusive=False, timeout_s=0.2)  # another run is kept out
    finally:
        go.set()
        thread.join(LONG_S)
    (record,) = landed
    assert not isinstance(record, BaseException), record
    assert record["ok"] is True
    assert seen == [False]
    assert shared_free(journal)
    lock.acquire(exclusive=True, timeout_s=1.0).close()  # federation.lock is free too


def test_o66_an_outer_section_that_ends_while_its_nested_reset_runs_leaves_the_hold_to_it(
    client, config, platform_file, ticker, tmp_path, fake, lock, journal, monkeypatch
):
    """O66: a reset in a thread that inherited the caller's section uses its hold; when the caller's section ends first
    (its task cancelled), the hold is not let go until the reset has ended, and the run then has its share back."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    reset = SpaceReset(p, now=ticker.clock)
    paused, go, seen = paused_at_qos_settle(monkeypatch, journal)
    with lock.held(exclusive=False, timeout_s=1.0) as admission:
        try:
            with reset.exclusive() as grant:
                thread, landed = in_context_thread(lambda: reset.run(allowed=ALLOWED, qos_rights=True))
                assert paused.wait(LONG_S)
            assert grant.held, "the caller's section let the hold go under its nested reset"
            assert not shared_free(journal)
        finally:
            go.set()
            thread.join(LONG_S)
        (record,) = landed
        assert not isinstance(record, BaseException), record
        assert record["ok"] is True
        assert seen == [False]
        assert not grant.held
        assert admission.reset.shared


# --- the campaign's own path ------------------------------------------------------------------------------------------


def test_o66_a_reset_in_the_campaigns_attempt_through_to_thread_holds_the_lock(
    client, config, platform_file, ticker, tmp_path, fake, journal, monkeypatch
):
    """O66: the campaign enters the run admission (`run_admission`) in its dispatch thread and runs the executor with
    `asyncio.run`, so a study's reset made from the executor through `asyncio.to_thread` sees the run's admission:
    it holds the reset lock exclusively from its first stage to its last, and passes."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    seen = hold_seen_at(monkeypatch, journal)
    admit = run_admission(config)
    assert admit is not None

    async def executor():
        return await asyncio.to_thread(SpaceReset(p, now=ticker.clock).run, allowed=ALLOWED, qos_rights=True)

    with admit((), {}, threading.Event(), False):
        record = asyncio.run(executor())
    assert record["ok"] is True
    assert record["reset_lock"]["mode"] == "exclusive"
    assert seen == [False, False]


def test_o66_a_reset_in_a_thread_pool_that_did_not_inherit_the_run_is_refused_and_sends_nothing(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O66: `loop.run_in_executor` does not carry the run's context, so a reset made through it holds no admission:
    on a platform with `[faults]` it is refused, nothing sent, rather than waiting on its own run's share or running
    beside another holder."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    admit = run_admission(config)
    assert admit is not None
    reset = SpaceReset(p, now=ticker.clock)

    async def executor():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: reset.run(allowed=ALLOWED, qos_rights=True))

    with admit((), {}, threading.Event(), False):
        before = len(fake.sent)
        with pytest.raises(AdmissionRefused, match="run_in_executor"):
            asyncio.run(executor())
        assert len(fake.sent) == before


# --- nothing of the agent's runs while its run resets --------------------------------------------------------------


def agent_entries(config):
    """Try every way the run's agent can come to act on the federation, as the run's own code would: the first call of
    a sandbox launch and of a tool-server launch (`require_admission`; the agent's cells run in the sandbox, its tool
    calls go to the tool server, and every read it sees goes through one of them), a sandbox start's share
    (`retain_to_act`) and a tool reusing the run's admission (`shared_admission`). Return which were let through,
    giving back at once whatever one took."""
    entries = {
        "launch": lambda stack: require_admission(config),
        "start": lambda stack: stack.callback(retain_to_act()),
        "tool": lambda stack: stack.enter_context(shared_admission(config, reason="smoke_warmup")),
    }
    allowed = []
    for name, entry in entries.items():
        with contextlib.ExitStack() as stack:
            try:
                entry(stack)
            except AdmissionRefused:
                continue
            allowed.append(name)
    return allowed


def test_o66_no_agent_entry_is_let_through_until_the_runs_reset_has_completed(
    client, config, platform_file, ticker, tmp_path, fake, monkeypatch
):
    """O66: in a campaign run (`run_admission`), its reset first, then its agent: at the reset's first stage and at its
    last, every way the run's agent can come to act (a sandbox or tool-server launch, a sandbox start's share, a tool
    reusing the admission) is refused, so a share the run keeps during its reset can only be a harness step's; once
    the reset has completed, each is let through."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    seen: dict[str, list[str]] = {}
    qos, history = SpaceReset._qos, SpaceReset._history

    def probed(stage, method):
        def run(self, *args, **kwargs):
            seen[stage] = agent_entries(config)
            return method(self, *args, **kwargs)

        return run

    monkeypatch.setattr(SpaceReset, "_qos", probed("qos", qos))
    monkeypatch.setattr(SpaceReset, "_history", probed("history", history))
    admit = run_admission(config)
    assert admit is not None
    with admit((), {}, threading.Event(), False):
        record = SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)
        with contextlib.ExitStack() as after:
            require_admission(config)
            after.callback(retain_to_act())
            after.enter_context(shared_admission(config, reason="smoke_warmup"))
    assert record["ok"] is True
    assert seen == {"qos": [], "history": []}


@pytest.mark.parametrize("step", [SANDBOX_STEP, TOOL_SERVER_STEP])
def test_o66_a_reset_after_the_runs_sandbox_or_tool_server_started_is_refused_and_sends_nothing(
    client, config, platform_file, ticker, tmp_path, fake, lock, step
):
    """O66: the broken order. The run's sandbox (or tool server) is up, counted as a step that uses the run's tokens
    from its launch to the end of its stop (`token_step`, as `SandboxPlatform.launch` and the tool-server launch hold
    it), so the agent may be running cells, calling tools or reading: a reset then could run beside the agent, so it
    is refused before anything is sent, naming the step, the run's share left held."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    with lock.held(exclusive=False, timeout_s=1.0) as admission, token_step(step):
        before = len(fake.sent)
        with pytest.raises(AdmissionRefused, match=f"run's {step}") as refused:
            SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)
        assert len(fake.sent) == before
        assert admission.reset.shared
        assert "before the run's sandbox and tool server start" in str(refused.value)
    with lock.held(exclusive=False, timeout_s=1.0):  # the same reset once the step has ended passes
        assert SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)["ok"] is True


def test_o66_an_agent_step_that_begins_during_the_reset_fails_that_stage(
    client, config, platform_file, ticker, tmp_path, fake, lock, monkeypatch
):
    """O66: a step that uses the run's tokens begun while the reset runs (a launch that passed its admission check just
    before the reset began) may let the agent act during it: the stage it began in fails, naming it, and no later stage
    begins."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    settle = SpaceReset._qos_settle

    def and_a_launch(self, *args, **kwargs):
        facts = settle(self, *args, **kwargs)
        admission.begin_step(SANDBOX_STEP)  # counted, as a launch already past its check counts it
        return facts

    monkeypatch.setattr(SpaceReset, "_qos_settle", and_a_launch)
    with lock.held(exclusive=False, timeout_s=1.0) as admission, pytest.raises(ResetFailed, match="sandbox") as failed:
        SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)
    assert failed.value.stage == "qos_settle"
    assert "transfers" not in failed.value.record["stages"]


# --- no reset without the federation's admission --------------------------------------------------------------------


def test_o66_a_reset_with_no_admission_on_a_platform_with_faults_is_refused_and_sends_nothing(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O66: on a platform with `[faults]` a reset outside every admission could run beside any holder, and an outage
    run's agent holds no share of the reset lock to wait for: it is refused before anything is sent (a thread that did
    not inherit the run's context is one such reset), and the refusal names the exact remedy, the hold to wrap the
    reset in with its argument's form, whose example code is one `shared_admission` accepts."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    before = len(fake.sent)
    with pytest.raises(AdmissionRefused) as refused:
        SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)
    assert len(fake.sent) == before
    message = str(refused.value)
    assert "wrap the reset in shared_admission(config, reason=<code>)" in message
    assert "expcore_onedata.tool_admission" in message
    example = re.search(r"a short code such as (\w+)", message)
    assert example is not None, message
    assert checked_code(example.group(1), config, what="reason", says="what the hold is for") == example.group(1)
    assert "run_in_executor" in message  # inside a campaign: reset in the run's own context
    with shared_admission(config, reason=example.group(1)):
        assert SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)["ok"] is True


def test_o66_a_reset_under_a_tools_shared_admission_holds_the_lock(
    client, config, platform_file, ticker, tmp_path, fake, journal, monkeypatch
):
    """O66: a reset outside a campaign takes the tools' shared admission (`shared_admission`) and resets under it,
    holding the reset lock exclusively."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    seen = hold_seen_at(monkeypatch, journal)
    with shared_admission(config, reason="smoke_reset"):
        record = SpaceReset(p, now=ticker.clock).run(allowed=ALLOWED, qos_rights=True)
    assert record["ok"] is True
    assert seen == [False, False]


def test_o66_spaces_of_two_federations_are_refused_in_one_reset(client, config, platform_file, ticker, tmp_path, fake):
    """O66: one reset holds one federation's reset lock, so a reset of several spaces whose platforms name two
    federations is refused before anything is sent."""
    other_dir = tmp_path / "other"
    other_dir.mkdir()

    def edit(data):
        two_providers(data)
        data["faults"] = faults_table(admission_timeout_s=BOUND_S, journal="another-journal")

    other_file = write_platform(other_dir, edit)
    other_config = load_platform(other_file)
    other = provisioner(
        OnezoneClient(other_config, http=fake.client_http()), other_config, other_file, ticker, other_dir
    )
    ours = provisioner(client, config, platform_file, ticker, tmp_path)
    before = len(fake.sent)
    with pytest.raises(ValueError, match="one federation"):
        SpacesReset([SpaceReset(ours, now=ticker.clock), SpaceReset(other, now=ticker.clock)])
    assert len(fake.sent) == before
