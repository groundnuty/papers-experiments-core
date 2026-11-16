"""O11: a baseline between the fault injector's admission and its stop (`FaultInjector.prepared`).

`start(identity)` takes the federation, recovers what an earlier run left open, waits out the drain that recovery owes
and the federation's drain record, and only then locks the target, journals the injection and stops it. A study whose
probes read a baseline reads it between the two halves: `with injector.prepared(identity) as prepared: <baseline>;
prepared.inject()`, and `start` is that block with nothing in its body. The block never restores: the caller's `finish`
or `clear` does, as after `start`. Every test runs against `FakeServices` on a fake clock; nothing here contacts Docker
or a deployment.
"""

import asyncio
import contextvars
import json
import multiprocessing
import threading

import pytest
from fault_support import (
    LOCAL_TARGET,
    OUTAGE_TARGET,
    SERVICES,
    FakeClock,
    FakeServices,
    inject_and_die,
    injector_for,
    write_faults,
)
from record_support import hand_made

from expcore.errors import InfraError
from expcore.records.instruments import finish_instruments, start_instruments
from expcore_onedata.admission import OUTAGE_RUN, FederationLock, current_admission, retain_admission
from expcore_onedata.config import load_platform
from expcore_onedata.fault_epoch import LINKS_FAILED, LinkEpoch
from expcore_onedata.fault_journal import JOURNAL, InjectionJournal
from expcore_onedata.faults import INJECTOR_FAILURE, load_faults
from expcore_onedata.sandbox.kinds import FEDERATION_BUSY

OUTAGE_SERVICE, LOCAL_SERVICE = SERVICES["beta"], SERVICES["alpha"]
RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 2}
OTHER_RUN = {"run_key": "task=task-b|model=m1|seed=1", "attempt": 1}
DRAIN_S = 320.0
# the fixture's restore and gate (505 s), the slack (300 s) and the drain a clear may wait (320 s): a platform that
# declares the drain needs `[tokens] margin_s` of 1,125 s (`fault_settings.restore_token_margin_s`)
DRAIN_MARGIN_S = 1125


def settings_of(platform):
    return load_faults(platform, load_platform(platform, require_secrets=False))


def journal_lines(platform):
    path = settings_of(platform).journal / JOURNAL
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def open_targets(platform):
    return [entry["target"] for entry in InjectionJournal(settings_of(platform).journal).open_entries()]


def federation_free(platform):
    """Whether another run could be admitted now: a shared try for the federation, let go at once."""
    taken = FederationLock(settings_of(platform)).try_acquire(exclusive=False)
    if taken is None:
        return False
    taken.close()
    return True


def stops(services):
    return [call for call in services.calls if call[0] == "stop"]


def crashed(directory, **options):
    """A process took the target down and died before clearing; return the platform file and the services' state."""
    directory.mkdir(parents=True, exist_ok=True)
    platform, state = write_faults(directory, **options), directory / "services.json"
    FakeServices(path=state)
    child = multiprocessing.get_context("spawn").Process(
        target=inject_and_die, args=(str(platform), str(state), OTHER_RUN["run_key"])
    )
    child.start()
    child.join(60)
    assert child.exitcode == 0
    assert open_targets(platform) == [OUTAGE_TARGET]
    return platform, state


class CountingClock(FakeClock):
    """The fake clock, counting its readings and recording its sleeps; `wall` reads the same time, counted apart."""

    def __init__(self):
        super().__init__()
        self.reads, self.wall_reads, self.sleeps = 0, 0, []

    def __call__(self):
        self.reads += 1
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def wall(self):
        self.wall_reads += 1
        return self.now


# --- start is the block with nothing in its body ---------------------------------------------------------------------


def normalised(lines):
    """Journal lines with each injection's id replaced by its order of first appearance, and without the holder (the
    pid and boot id of the process that journalled it)."""
    ids: dict[str, int] = {}
    return [
        {**{key: value for key, value in line.items() if key != "holder"}, "id": ids.setdefault(line["id"], len(ids))}
        for line in lines
    ]


def one_outage_run(directory, inject):
    """The fullest start there is, on a counting fake clock: an earlier run's injection left open, a recovery that owes
    the drain, a drain record raised after the run's admission that outlasts it, then `inject(injector)`, the agent's
    run and the restore. Return everything the two forms could differ in."""
    platform, state = crashed(
        directory,
        session_drain_s=DRAIN_S,
        margin_s=DRAIN_MARGIN_S,
        poll_s=0.01,
        ready_timeout_s=5,
        admission_timeout_s=600,
    )
    clock, services = CountingClock(), FakeServices(path=state)
    injector = injector_for(platform, services, clock=clock, wall=clock.wall)
    lock = FederationLock(settings_of(platform), clock=clock, sleep=clock.sleep, wall=clock.wall)
    with lock.held(exclusive=True, target=OUTAGE_TARGET, purpose=OUTAGE_RUN):
        lock.hold_for(DRAIN_S + 80)  # still standing once the recovery's own drain is waited out
        inject(injector)
        clock.now += 30  # the agent's run
        snapshot = injector.finish()
    return {
        "journal": normalised(journal_lines(platform)),
        "snapshot": snapshot,
        "calls": services.calls,
        "clock_reads": clock.reads,
        "wall_reads": clock.wall_reads,
        "sleeps": clock.sleeps,
        "epoch_record": LinkEpoch(settings_of(platform).journal).path.exists(),
    }


def test_o11_start_is_prepared_then_inject_with_the_same_journal_steps_calls_and_clock_reads(tmp_path):
    """O11: `start` and a block whose body only calls `inject` journal the same lines, record the same snapshot (every
    step, with its time), make the same driver calls, and read the injector's clock and sleep the same number of times
    with the same sleeps, over a start that recovers an earlier run's injection, waits out the drain that recovery owes
    and then a drain record that outlasts it, before it takes the target down and is restored. What the caller's block
    alone re-reads before its stop adds one reading of the wall clock (the drain record's) and the link epoch's record,
    which it makes when there is none; `start`'s own block reads neither."""

    def by_start(injector):
        injector.start(RUN)

    def by_block(injector):
        with injector.prepared(RUN) as prepared:
            prepared.inject()

    started = one_outage_run(tmp_path / "start", by_start)
    blocked = one_outage_run(tmp_path / "block", by_block)
    apart = ("wall_reads", "epoch_record")
    assert {key: value for key, value in blocked.items() if key not in apart} == {
        key: value for key, value in started.items() if key not in apart
    }
    assert blocked["wall_reads"] == started["wall_reads"] + 1
    assert (blocked["epoch_record"], started["epoch_record"]) == (True, False)
    steps = [step["step"] for step in started["snapshot"]["steps"]]
    assert {"recovered", "recovery_drain", "injected", "cleared", "verified"} <= set(steps)
    injected = next(step for step in started["snapshot"]["steps"] if step["step"] == "injected")
    assert injected["at_s"] >= DRAIN_S + 79  # the drain record, not only the recovery's drain, was waited out
    assert started["snapshot"]["ok"] is True


def legacy_start(injector, identity):
    """`start` as it was before the block existed (at ac11fb5e): its first half and its second, which this change
    moved unchanged into `_prepare` and `_inject`, under the injector's lock and one share of the run's admission, with
    nothing between them and nothing read again."""
    with injector._mutex:
        give_up = retain_admission()
        try:
            injector._prepare(identity)
            injector._inject()
        except BaseException:
            if injector._pending is None:
                injector._release_admission()
            raise
        finally:
            give_up()


def test_o11_start_is_the_start_before_the_block_down_to_every_clock_read_and_the_files_it_makes(tmp_path):
    """O11: against `start` as it was before the block, rebuilt from its two unchanged halves, the new one journals
    the same lines, records the same snapshot, makes the same driver calls, reads the injector's clock and the wall
    clock and sleeps alike, and makes no link-epoch record: its own block reads nothing again."""
    before = one_outage_run(tmp_path / "legacy", lambda injector: legacy_start(injector, RUN))
    now = one_outage_run(tmp_path / "start", lambda injector: injector.start(RUN))
    assert now == before
    assert now["epoch_record"] is False


# --- the pilot's shape: a probe's start inside the block --------------------------------------------------------------


class BaselineProbe:
    """An instrument whose `start` reads its baseline, called by `start_instruments` inside the block, and whose
    `finish` reads the run's end, as the platform's probes do: here, what the federation looks like at each."""

    namespace = "baseline_probe"

    def __init__(self, services, platform, injector):
        self.services, self.platform, self.injector = services, platform, injector
        self.baseline = None

    def start(self, identity):
        held = current_admission()
        self.baseline = {
            "target_running": self.services.running(OUTAGE_SERVICE),
            "stops_sent": len(stops(self.services)),
            "open_injections": open_targets(self.platform),
            "exclusive": held is not None and held.exclusive,
            "drain_left": FederationLock(settings_of(self.platform)).drain_left(),
            "steps": [step["step"] for step in self.injector.snapshot()["steps"]],
        }

    def finish(self):
        return {"baseline": self.baseline, "target_running_at_finish": self.services.running(OUTAGE_SERVICE)}


def test_o11_a_probe_started_inside_the_block_reads_the_federation_recovered_admitted_and_drained_before_the_stop(
    tmp_path,
):
    """O11: the pilot's shape. The campaign admits the outage run exclusively; the executor, in one worker thread
    (`asyncio.to_thread` carries the run's context, so its admission), prepares the injector, starts its probe with
    `start_instruments` inside the block and injects; after the agent's run `finish_instruments` finishes the probe
    before the injector's restore. The probe's baseline sees the earlier run's injection recovered and the drain it
    owed waited out, the federation held exclusively, the drain record passed and the target still up with nothing
    stopped; its finish sees the target down; the restore is verified."""
    platform, state = crashed(
        tmp_path, session_drain_s=DRAIN_S, margin_s=DRAIN_MARGIN_S, poll_s=0.01, ready_timeout_s=5
    )
    clock, services = FakeClock(), FakeServices(path=state)
    injector = injector_for(platform, services, clock=clock)
    probe = BaselineProbe(services, platform, injector)

    def begin():
        with injector.prepared(RUN) as prepared:
            start_instruments([probe], RUN)
            prepared.inject()

    async def outage_run():
        async with FederationLock(settings_of(platform)).admitted(exclusive=True, target=OUTAGE_TARGET):
            await asyncio.to_thread(begin)
            record = hand_made()  # the agent's run
            return await asyncio.to_thread(finish_instruments, record, [injector, probe])

    record = asyncio.run(outage_run())
    assert record.instruments["baseline_probe"] == {
        "baseline": {
            "target_running": True,
            "stops_sent": 0,
            "open_injections": [],
            "exclusive": True,
            "drain_left": 0.0,
            "steps": ["sanctioned", "recovered", "recovery_drain"],
        },
        "target_running_at_finish": False,
    }
    reading = record.instruments["fault_injector"]
    assert (reading["ok"], reading["restore_verified"]) == (True, True)
    assert services.running(OUTAGE_SERVICE) is True
    assert open_targets(platform) == []


# --- the exit ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "raised"),
    [
        ("exception", RuntimeError),
        ("interrupt", KeyboardInterrupt),
        ("no_inject", None),
        ("inject_fails_before_the_stop", InfraError),
    ],
)
def test_o11_a_block_that_takes_nothing_down_gives_the_federation_back_however_it_ends(tmp_path, body, raised):
    """O11: the exit keeps `start`'s rule, the admission let go only when nothing is down. After an `Exception` in the
    caller's body, an interrupt (raised as it was), a body that never injects, and an injection that fails before its
    stop (the target's identity cannot be read), the federation admits the next run at once, nothing is journalled or
    stopped, the target's lock is free and the snapshot claims no injection; the federation was held for the whole
    body."""
    platform, services = write_faults(tmp_path), FakeServices()
    injector = injector_for(platform, services)
    held_in_body: list[bool] = []

    def run():
        with injector.prepared(RUN) as prepared:
            held_in_body.append(not federation_free(platform))
            if body == "exception":
                raise RuntimeError("the baseline failed")
            if body == "interrupt":
                raise KeyboardInterrupt
            if body == "inject_fails_before_the_stop":
                services.knobs["unreadable"] = True
                prepared.inject()

    if raised is None:
        run()
    else:
        with pytest.raises(raised) as caught:
            run()
        if raised is InfraError:
            assert caught.value.kind == INJECTOR_FAILURE
    services.knobs["unreadable"] = False
    assert held_in_body == [True]
    assert federation_free(platform) is True
    assert journal_lines(platform) == []
    assert stops(services) == []
    snapshot = injector.snapshot()
    assert (snapshot["injections"], "restore_verified" in snapshot) == ([], False)
    again = injector_for(platform, services)  # the target's lock is free too
    again.start({**RUN, "attempt": 3})
    assert again.finish()["ok"] is True


@pytest.mark.parametrize("body_raises", [False, True])
def test_o11_a_block_that_took_the_target_down_keeps_the_federation_until_the_restore(tmp_path, body_raises):
    """O11: after a stop that took effect the exit lets nothing go, as `start` does: the target stays down, its
    injection open and the federation held until the caller's `finish` restores it, whether the body then returned or
    raised. The block is no `session`: it never restores."""
    platform, services = write_faults(tmp_path), FakeServices()
    injector = injector_for(platform, services)

    def run():
        with injector.prepared(RUN) as prepared:
            prepared.inject()
            if body_raises:
                raise RuntimeError("the body broke after the injection")

    if body_raises:
        with pytest.raises(RuntimeError, match="after the injection"):
            run()
    else:
        run()
    assert services.running(OUTAGE_SERVICE) is False
    assert open_targets(platform) == [OUTAGE_TARGET]
    assert federation_free(platform) is False
    assert injector.finish()["ok"] is True
    assert services.running(OUTAGE_SERVICE) is True
    assert federation_free(platform) is True


# --- misuse -----------------------------------------------------------------------------------------------------------


def test_o11_a_block_refuses_a_second_block_a_start_and_a_recovery_inside_it_and_stays_whole(tmp_path):
    """O11: while a block is open, `prepared`, `start` (and so `session` and `running`) and `recover` are refused as a
    defect of the caller (`RuntimeError`, not `InfraError`) before they touch anything, and the block they were refused
    in still injects, keeps its run's identity and is restored as usual."""
    platform, services = write_faults(tmp_path), FakeServices()
    injector = injector_for(platform, services)

    def second_block():
        with injector.prepared({**RUN, "attempt": 3}):
            pass

    refused = []
    with injector.prepared(RUN) as prepared:
        for call in (second_block, lambda: injector.start({**RUN, "attempt": 3}), injector.recover):
            with pytest.raises(RuntimeError, match="prepared") as caught:
                call()
            refused.append(type(caught.value))
        assert federation_free(platform) is False  # the refusals let nothing go
        prepared.inject()
    assert refused == [RuntimeError] * 3
    assert stops(services) == [("stop", OUTAGE_SERVICE)]
    snapshot = injector.finish()
    assert (snapshot["ok"], snapshot["attempt"]) == (True, RUN["attempt"])


@pytest.mark.parametrize("first_fails", [False, True])
def test_o11_inject_is_called_once_per_block(tmp_path, first_fails):
    """O11: a second `inject` in the same block is refused (`RuntimeError`), whether or not the first took the target
    down, and journals and stops nothing: one injection per block."""
    platform, services = write_faults(tmp_path), FakeServices(unreadable=first_fails)
    injector = injector_for(platform, services)
    with injector.prepared(RUN) as prepared:
        if first_fails:
            with pytest.raises(InfraError):
                prepared.inject()
            services.knobs["unreadable"] = False
        else:
            prepared.inject()
        with pytest.raises(RuntimeError, match="once") as caught:
            prepared.inject()
        assert type(caught.value) is RuntimeError
    events = [line["event"] for line in journal_lines(platform)]
    assert events == ([] if first_fails else ["injecting", "stopping", "injected"])
    assert len(stops(services)) == (0 if first_fails else 1)
    assert injector.finish()["ok"] is not first_fails


def test_o11_inject_outside_its_block_is_refused_and_takes_nothing_down(tmp_path):
    """O11: `inject` after its block has ended, and the handle of an earlier block inside a later one, are refused
    (`RuntimeError`) with nothing journalled or stopped, and the later block's exit gives the federation back."""
    platform, services = write_faults(tmp_path), FakeServices()
    injector = injector_for(platform, services)
    with injector.prepared(RUN) as earlier:
        pass
    with pytest.raises(RuntimeError, match="inside its") as after:
        earlier.inject()
    with injector.prepared({**RUN, "attempt": 3}) as later:
        with pytest.raises(RuntimeError, match="inside its") as inside:
            earlier.inject()
        assert later is not earlier
    assert (type(after.value), type(inside.value)) == (RuntimeError, RuntimeError)
    assert journal_lines(platform) == []
    assert stops(services) == []
    assert federation_free(platform) is True


# --- two threads: the injector's lock is not held across the body -----------------------------------------------------


def test_o11_a_clear_from_another_thread_during_the_baseline_returns_at_once_and_the_block_then_takes_nothing_down(
    tmp_path,
):
    """O11: the injector's lock is held while the block prepares and while it injects, never across the caller's body.
    A `clear` from another thread while the baseline runs (the caller's way to cancel the block) returns at once,
    True, nothing being down, and the block's later `inject` refuses as `injector_failure` with nothing locked,
    journalled or stopped, so nothing is taken down after a clear has returned; a `start` from that thread is refused
    at once rather than waiting for the baseline. The federation stays held until the block ends, and goes then."""
    platform, services = write_faults(tmp_path), FakeServices()
    injector = injector_for(platform, services)
    in_baseline, baseline_done = threading.Event(), threading.Event()
    outcome: list[BaseException] = []

    def block():
        try:
            with injector.prepared(RUN) as prepared:
                in_baseline.set()
                baseline_done.wait(10)
                prepared.inject()
        except BaseException as error:
            outcome.append(error)

    worker = threading.Thread(target=block)
    worker.start()
    try:
        assert in_baseline.wait(10)
        answers: list[bool] = []
        other = threading.Thread(target=lambda: answers.append(injector.clear()))
        other.start()
        other.join(5)
        assert not other.is_alive()
        assert answers == [True]
        with pytest.raises(RuntimeError, match="prepared") as refused:
            injector.start({**RUN, "attempt": 3})
        assert type(refused.value) is RuntimeError
        assert worker.is_alive()  # the baseline still runs
        assert federation_free(platform) is False
    finally:
        baseline_done.set()
        worker.join(10)
    assert not worker.is_alive()
    (error,) = outcome
    assert isinstance(error, InfraError)
    assert error.kind == INJECTOR_FAILURE
    assert "clear" in error.detail
    assert stops(services) == []
    assert journal_lines(platform) == []
    assert services.running(OUTAGE_SERVICE) is True
    assert federation_free(platform) is True


class SlowStop(FakeServices):
    """A stop command that takes effect only once the test lets it: `inject` holds the injector's lock meanwhile."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stopping, self.release = threading.Event(), threading.Event()

    def stop(self, service):
        self.stopping.set()
        self.release.wait(10)
        super().stop(service)


def injecting_slowly(services, injector):
    """A worker whose block injects; return it once its stop command is under way, `inject` holding the lock."""

    def block():
        with injector.prepared(RUN) as prepared:
            prepared.inject()

    worker = threading.Thread(target=block)
    worker.start()
    assert services.stopping.wait(10)
    return worker


def at_once(call):
    """Run `call` in a thread and return what it raised, or None; fail when it has not ended within 5 s."""
    raised: list[BaseException | None] = []

    def run():
        try:
            call()
            raised.append(None)
        except BaseException as error:
            raised.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "it waited for the block's inject instead of being refused at once"
    return raised[0]


def test_o11_a_start_a_block_and_a_recovery_are_refused_at_once_while_the_blocks_inject_holds_the_lock(tmp_path):
    """O11: a block's `inject` holds the injector's lock through its stop; a `start`, a second block, a block built
    before the first one opened and entered only now, and a recovery, from another thread meanwhile, are refused at
    once (`RuntimeError`), before they wait for that lock, so none of them can run on once the block has ended; the
    block's injection goes on and is restored as usual."""
    platform, services = write_faults(tmp_path), SlowStop()
    injector = injector_for(platform, services)
    built_early = injector.prepared({**RUN, "attempt": 4})  # entering, not building, is what is refused
    worker = injecting_slowly(services, injector)
    try:

        def second_block():
            with injector.prepared({**RUN, "attempt": 3}):
                pass

        def enter_built_early():
            with built_early:
                pass

        calls = (lambda: injector.start({**RUN, "attempt": 3}), second_block, enter_built_early, injector.recover)
        refused = [at_once(call) for call in calls]
    finally:
        services.release.set()
        worker.join(10)
    assert [type(error) for error in refused] == [RuntimeError] * 4
    assert all("prepared" in str(error) for error in refused)
    assert stops(services) == [("stop", OUTAGE_SERVICE)]
    assert injector.finish()["ok"] is True


def test_o11_a_clear_while_the_blocks_inject_runs_waits_for_it_and_restores_what_it_took_down(tmp_path):
    """O11: a `clear` that comes while `inject` holds the lock waits for it, as it waits for a start, and then restores
    the target the injection took down: nothing is down once it has returned True."""
    platform, services = write_faults(tmp_path), SlowStop()
    injector = injector_for(platform, services)
    worker = injecting_slowly(services, injector)
    answers: list[bool] = []
    clearing = threading.Thread(target=lambda: answers.append(injector.clear()))
    try:
        clearing.start()
        clearing.join(0.3)
        assert clearing.is_alive()  # it waits for the injection under way
    finally:
        services.release.set()
        worker.join(10)
        clearing.join(10)
    assert answers == [True]
    assert services.running(OUTAGE_SERVICE) is True
    assert open_targets(platform) == []
    assert federation_free(platform) is True


# --- what inject re-checks after an unbounded baseline ----------------------------------------------------------------


def test_o11_a_drain_record_raised_during_the_baseline_holds_the_injection_back_as_federation_busy(tmp_path):
    """O11: the drain record was read after the journal and before the baseline; one raised during the baseline
    (which no holder the exclusive admission lets in can do) is read again by `inject`, which refuses as the hold
    `federation_busy`, charging no attempt, before it locks, journals or stops anything."""
    platform, services = write_faults(tmp_path), FakeServices()
    injector = injector_for(platform, services)
    with injector.prepared(RUN) as prepared:
        FederationLock(settings_of(platform)).hold_for(30)
        with pytest.raises(InfraError) as caught:
            prepared.inject()
    assert caught.value.kind == FEDERATION_BUSY
    assert stops(services) == []
    assert journal_lines(platform) == []
    assert services.running(OUTAGE_SERVICE) is True


@pytest.mark.parametrize("how", ["raised", "spoiled_with_none_before", "made_anew", "unreadable", "unreadable_before"])
def test_o11_a_link_epoch_not_proven_unmoved_holds_the_injection_back_as_federation_busy(tmp_path, how):
    """O11: only the same link epoch (`fault_epoch`: its `id` and count) at the preparation and at `inject` lets the
    target be stopped. A link check in the run's own context that found a pair failing and raised it; a raise that could
    not be written and spoiled the record, even when there was none before the block (the preparation made one, so the
    spoiling shows); a record made anew; and a record unreadable at `inject` or already at the preparation: each makes
    `inject` refuse as the hold `federation_busy` before it locks, journals or stops anything."""
    platform, services = write_faults(tmp_path), FakeServices()
    injector = injector_for(platform, services)
    epochs = LinkEpoch(settings_of(platform).journal)
    if how != "spoiled_with_none_before":
        epochs.ensure()
    if how == "unreadable_before":
        epochs.path.write_text("not json", encoding="utf-8")
    with injector.prepared(RUN) as prepared:
        if how == "raised":
            epochs.raise_for(LINKS_FAILED)
        elif how == "spoiled_with_none_before":
            assert epochs.path.exists()  # made by the preparation
            epochs.path.unlink()
        elif how == "made_anew":
            epochs.path.unlink()
            epochs.ensure()
        elif how == "unreadable":
            epochs.path.write_text("not json", encoding="utf-8")
        with pytest.raises(InfraError) as caught:
            prepared.inject()
    assert caught.value.kind == FEDERATION_BUSY
    assert "link epoch" in caught.value.detail
    assert stops(services) == []
    assert journal_lines(platform) == []
    assert federation_free(platform) is True


def test_o11_inject_waits_for_a_link_check_under_way_and_refuses_when_it_found_a_pair_failing(tmp_path):
    """O11: a link check under way holds `link-epoch.lock` shared from before it reads the pairs until it has raised
    the epoch; `inject` reads the epoch holding that lock exclusively, as a run's admission does, so it waits for the
    check to end and then sees its raise, instead of reading between the finding and the raise and stopping the
    target."""
    platform, services = write_faults(tmp_path, admission_timeout_s=10), FakeServices()
    injector = injector_for(platform, services)
    epochs = LinkEpoch(settings_of(platform).journal)
    epochs.ensure()
    checking, found = threading.Event(), threading.Event()

    def link_check():
        with epochs.checking():
            checking.set()
            found.wait(10)
            epochs.raise_for(LINKS_FAILED)

    errors: list[BaseException] = []

    def inject(prepared):
        try:
            prepared.inject()
        except BaseException as error:
            errors.append(error)

    with injector.prepared(RUN) as prepared:
        check = threading.Thread(target=link_check)
        check.start()
        try:
            assert checking.wait(10)
            injecting = threading.Thread(target=inject, args=(prepared,))
            injecting.start()
            injecting.join(0.3)
            assert injecting.is_alive()  # waiting for the check (secondary: a slow start would pass it too)
        finally:
            found.set()
            check.join(10)
        injecting.join(10)
    (error,) = errors
    assert isinstance(error, InfraError)
    assert error.kind == FEDERATION_BUSY
    assert stops(services) == []
    assert journal_lines(platform) == []


def test_o11_a_link_check_that_outlasts_the_wait_holds_the_injection_back_as_federation_busy(tmp_path):
    """O11: a link check under way past `admission_timeout_s` leaves the epoch unknown; `inject` refuses as the hold
    `federation_busy`, with nothing locked, journalled or stopped."""
    platform, services = write_faults(tmp_path, admission_timeout_s=0.3), FakeServices()
    injector = injector_for(platform, services)
    epochs = LinkEpoch(settings_of(platform).journal)
    checking, done = threading.Event(), threading.Event()

    def link_check():
        with epochs.checking():
            checking.set()
            done.wait(10)

    check = threading.Thread(target=link_check)
    check.start()
    try:
        assert checking.wait(10)
        with injector.prepared(RUN) as prepared, pytest.raises(InfraError) as caught:
            prepared.inject()
    finally:
        done.set()
        check.join(10)
    assert caught.value.kind == FEDERATION_BUSY
    assert "outlasted" in caught.value.detail
    assert stops(services) == []
    assert journal_lines(platform) == []


def test_o11_a_plain_start_neither_waits_for_a_link_check_under_way_nor_reads_the_epoch(tmp_path):
    """O11: `start`'s own block has no body between its halves and reads nothing again, so a link check under way past
    `admission_timeout_s` holds a plain `start` back no more than before the block existed: it takes the target down at
    once and makes no link-epoch record."""
    platform, services = write_faults(tmp_path, admission_timeout_s=0.3), FakeServices()
    injector = injector_for(platform, services)
    epochs = LinkEpoch(settings_of(platform).journal)
    checking, done = threading.Event(), threading.Event()

    def link_check():
        with epochs.checking():
            checking.set()
            done.wait(10)

    check = threading.Thread(target=link_check)
    check.start()
    try:
        assert checking.wait(10)
        assert at_once(lambda: injector.start(RUN)) is None
    finally:
        done.set()
        check.join(10)
    assert stops(services) == [("stop", OUTAGE_SERVICE)]
    assert epochs.path.exists() is False
    assert injector.finish()["ok"] is True


def test_o11_an_injection_journalled_during_the_baseline_refuses_the_injection(tmp_path):
    """O11: the recovery found no injection open before the baseline; one journalled during it (by a second injector
    under the same run's exclusive admission, which no other run's can be) leaves the federation out of its baseline
    state, so `inject` refuses as `injector_failure`, naming that injection's target by its label, and stops nothing of
    its own; the other injector then restores its own target."""
    platform, services = write_faults(tmp_path), FakeServices()
    injector = injector_for(platform, services)
    other = injector_for(platform, services, target=LOCAL_TARGET)
    with FederationLock(settings_of(platform)).held(exclusive=True, target=OUTAGE_TARGET, purpose=OUTAGE_RUN):
        with injector.prepared(RUN) as prepared:
            other.start(OTHER_RUN)
            with pytest.raises(InfraError) as caught:
                prepared.inject()
        assert caught.value.kind == INJECTOR_FAILURE
        assert LOCAL_TARGET in caught.value.detail
        assert stops(services) == [("stop", LOCAL_SERVICE)]
        assert services.running(OUTAGE_SERVICE) is True
        assert other.finish()["ok"] is True
    assert open_targets(platform) == []


def test_o11_inject_where_the_runs_admission_is_not_held_is_refused(tmp_path):
    """O11: a block prepared under the run's exclusive admission injects only where that admission is held. Called in
    a context that does not carry it (a thread started with an empty context, not through `asyncio.to_thread`),
    `inject` refuses as `injector_failure` with nothing stopped, since what follows a stop there (its restore on a
    failure, the token's window) would not see the run's admission."""
    platform, services = write_faults(tmp_path), FakeServices()
    injector = injector_for(platform, services)
    errors: list[BaseException] = []
    lock = FederationLock(settings_of(platform))
    with lock.held(exclusive=True, target=OUTAGE_TARGET, purpose=OUTAGE_RUN), injector.prepared(RUN) as prepared:

        def elsewhere():
            try:
                prepared.inject()
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=contextvars.Context().run, args=(elsewhere,))
        thread.start()
        thread.join(10)
    (error,) = errors
    assert isinstance(error, InfraError)
    assert error.kind == INJECTOR_FAILURE
    assert stops(services) == []
    assert journal_lines(platform) == []
