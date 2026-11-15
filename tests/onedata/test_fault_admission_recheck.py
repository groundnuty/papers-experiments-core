"""O35: a run's admission re-reads the fault gate's state, and refuses, uncharged, a run its check cannot vouch for.

A run's pre-run check can pass before a heal (or before a pair fails), and the run then waits for its lease and for the
federation's admission. A heal that runs, or a link check that finds a pair failing, in that window would otherwise let
the run in onto a stuck link once the heal let the federation go. So the check notes the federation's link epoch in the
try's note (`expcore.campaign.prerun`), and the run admission, holding the lock it took, compares it with the epoch
now: a heal begun or a pair found failing since refuses the run as the hold `federation_busy`, which uses no attempt,
and the campaign asks again, with a fresh check.
"""

import contextlib
import contextvars
import threading

import pytest
from campaign_support import make_schema
from gate_support import CRASHED, EVERY, READER, Federation, gate_of
from heal_support import heal_injector, stale
from scheduler_support import FakeExecutor, attempts_of, build, key_of, settings_for

from expcore.campaign import prerun
from expcore.campaign.factors import FactorSchema
from expcore.campaign.health_required import required_reason
from expcore.config.loading import ConfigError
from expcore.errors import uncharged_kinds
from expcore_onedata.admission import (
    EXCLUSIVE_RUN,
    AdmissionRefused,
    FederationBusy,
    FederationLock,
    require_admission,
    run_admission,
)
from expcore_onedata.config import load_platform
from expcore_onedata.fault_epoch import GATE_NOTE, LinkEpoch, noted_of
from expcore_onedata.fault_gate_heal import GateHeals
from expcore_onedata.fault_settings import load_faults
from expcore_onedata.sandbox.kinds import FEDERATION_BUSY
from expcore_onedata.testing import write_platform

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
SMALL = {"tasks": ("task-a",), "seeds": (1,)}
SCHEMA = make_schema()
ONE = FactorSchema(SCHEMA.declared, SCHEMA.order, {"api-none": SCHEMA.conditions["api-none"]})  # a plan of one run
ONE_KEY = key_of(ONE, "api-none")
HEAL_WAITS = (False, True, False)  # (ok, deferred, charge): the answer of the check that begins a heal
LONG_DEFERRAL_S = 10_000.0  # above the longest wait a declared heal can defer a check (`max_deferral_s`, #293)


def admission_in(directory):
    """The campaign's run admission for the platform in `directory`."""
    return run_admission(load_platform(directory / "platform.toml", require_secrets=False))


def admitted(admit) -> str:
    """Enter and leave a baseline run's admission; what it held."""
    with admit((), {}, threading.Event(), False) as mode:
        return mode


def elsewhere(call):
    """`call` in a context of its own: another worker's check, never the try under test's."""
    return contextvars.Context().run(call)


def heal_meanwhile(gate, federation):
    """Another run's check finds the pair stuck and begins the heal; once it has ended, past its settle."""
    federation.stuck = {(READER, CRASHED)}
    began = elsewhere(gate)
    assert (began.ok, began.deferred, began.charge) == HEAL_WAITS, began.detail
    assert gate.wait_for_heal(10)
    federation.clock.sleep(600)  # past the drain record each restart's settle wrote on the gate's clock


def test_o35_a_heal_between_a_runs_check_and_its_admission_refuses_it_until_a_fresh_check(tmp_path):
    """O35: the run's check passed; then another check heals a stuck pair, and the heal mends it. The run is refused at
    admission, uncharged, and admitted once a check made after the heal has passed."""
    gate, federation, _, _ = gate_of(tmp_path, heal="all_links")
    federation.stuck = set()
    admit = admission_in(tmp_path)
    with prerun.opened():
        assert gate().ok
        heal_meanwhile(gate, federation)
        assert gate.last_heal["outcome"] == "healed"
        with pytest.raises(FederationBusy, match="a heal began") as refused:
            admitted(admit)
        assert gate().ok  # the run's next check, after the heal
        assert admitted(admit) == "shared"
    assert refused.value.kind == FEDERATION_BUSY
    assert FEDERATION_BUSY in uncharged_kinds()


def test_o35_a_heal_that_leaves_the_pair_stuck_with_no_container_restart_refuses_the_waiting_run(tmp_path):
    """O35: the heal ends `links_stuck` and restarted no container; the run whose check passed before it is refused at
    admission, and its next check finds the pair still stuck, so it is never admitted onto it."""
    federation = Federation()
    federation.link_fixes = dict.fromkeys(EVERY, ())
    gate, federation, _, _ = gate_of(tmp_path, federation, heal="all_links", heal_container_fallback=False)
    federation.stuck = set()
    admit = admission_in(tmp_path)
    with prerun.opened():
        assert gate().ok
        heal_meanwhile(gate, federation)
        assert gate.last_heal["outcome"] == "links_stuck"
        assert [verb for verb, _, _ in federation.actions if verb in ("stop", "start")] == []
        with pytest.raises(FederationBusy, match="a heal began"):
            admitted(admit)
        after = gate()
    assert (after.ok, after.deferred) == (False, False), after.detail


def test_o35_a_pair_found_failing_between_a_runs_check_and_its_admission_refuses_it_uncharged(tmp_path):
    """O35: no heal runs (`heal = "off"`), but another check finds a pair failing after the run's check passed: the run
    is refused at admission as the uncharged hold."""
    gate, federation, _, _ = gate_of(tmp_path, heal="off")
    federation.stuck = set()
    admit = admission_in(tmp_path)
    with prerun.opened():
        assert gate().ok
        federation.stuck = {(READER, CRASHED)}
        assert elsewhere(gate).ok is False
        with pytest.raises(FederationBusy, match="found a pair failing") as refused:
            admitted(admit)
    assert refused.value.kind == FEDERATION_BUSY


def test_o35_a_run_whose_federation_saw_no_heal_and_no_failure_since_its_check_is_admitted(tmp_path):
    """O35: other checks pass meanwhile and nothing is healed: the run is admitted as it would be without the
    re-check."""
    gate, federation, _, _ = gate_of(tmp_path, heal="all_links")
    federation.stuck = set()
    admit = admission_in(tmp_path)
    with prerun.opened():
        assert gate().ok
        assert elsewhere(gate).ok
        assert admitted(admit) == "shared"


def test_o35_a_try_whose_check_noted_no_link_epoch_is_refused_once_then_admitted_after_its_check(tmp_path):
    """O35: the try's check never reached the fault gate, so when it was made is unknown: the run is refused once,
    uncharged, never admitted blind, and admitted after a check of the gate."""
    gate, federation, _, _ = gate_of(tmp_path, heal="all_links")
    federation.stuck = set()
    admit = admission_in(tmp_path)
    with prerun.opened():
        with pytest.raises(FederationBusy, match="unknown"):
            admitted(admit)
        assert gate().ok
        assert admitted(admit) == "shared"


def test_o35_a_heal_on_record_since_the_gates_link_check_last_passed_refuses_the_admission(tmp_path):
    """O35: the heal record holds a heal begun since the gate's link check last passed (one the epoch did not count):
    the gate has not seen every pair pass since, so the run is refused, uncharged."""
    gate, federation, _, settings = gate_of(tmp_path, heal="all_links")
    federation.stuck = set()
    admit = admission_in(tmp_path)
    with prerun.opened():
        assert gate().ok
        GateHeals(settings.journal).append({"event": "healing", "id": "another-process", "mode": "all_links"})
        with pytest.raises(FederationBusy, match="since the fault gate's link check last passed"):
            admitted(admit)


def test_o35_a_failure_another_process_found_makes_the_next_check_check_the_links_afresh(tmp_path):
    """O35: this process's last link check passed and is still recent; another process's gate then finds a pair
    failing. The next check here checks the links afresh instead of answering the older pass, so it never vouches for
    a run on that pair."""
    gate, federation, check, settings = gate_of(tmp_path, heal="off")
    check.cache = True
    federation.stuck = set()
    assert gate().ok
    federation.stuck = {(READER, CRASHED)}
    assert gate().ok  # the recent pass answers: nothing here knows the pair is failing
    LinkEpoch(settings.journal).raise_for("links_failed")  # another process's gate finds it
    after = gate()
    assert (after.ok, after.deferred) == (False, False), after.detail


def test_o35_a_restores_heal_raises_the_link_epoch_so_a_run_checked_before_it_is_refused(tmp_path):
    """O35: an outage run's restore heals the pairs its provider's outage left stuck; a run whose check passed before
    that heal is refused at its admission, as for a heal of the gate's."""
    injector, _ = heal_injector(tmp_path, stale(), heal="all_links")
    platform = tmp_path / "platform.toml"
    journal = load_faults(platform, load_platform(platform, require_secrets=False)).journal
    before = noted_of(LinkEpoch(journal).ensure())
    injector.start(RUN)
    assert injector.clear() is True
    assert LinkEpoch(journal).read()["event"] == "heal"
    with prerun.opened():
        prerun.note(GATE_NOTE, before)  # what the run's check noted before the outage run took the federation
        with pytest.raises(FederationBusy, match="a heal began"):
            admitted(admission_in(tmp_path))


def test_o35_a_campaign_run_refused_for_a_heal_uses_no_attempt_and_runs_after_a_fresh_check(tmp_path):
    """O35: in a campaign whose `max_attempts` is 1, a heal runs between the run's pre-run check and its admission. The
    run is left unstarted with the reason in its event, uses no attempt, and runs once a fresh check has passed."""
    federation_dir = tmp_path / "federation"
    gate, federation, _, _ = gate_of(federation_dir, heal="all_links")
    federation.stuck = set()
    admit = admission_in(federation_dir)
    asked = []

    @contextlib.contextmanager
    def heal_first(kinds, targets, cancel, exclusive):
        asked.append(kinds)
        if len(asked) == 1:  # between this run's check and its admission, another check heals a stuck pair
            heal_meanwhile(gate, federation)
        with admit(kinds, targets, cancel, exclusive) as mode:
            yield mode

    settings = settings_for(max_attempts=1, max_defer_s=LONG_DEFERRAL_S)
    with build(
        tmp_path / "campaign",
        schema=ONE,
        executor=FakeExecutor(),
        plan=SMALL,
        settings=settings,
        health=gate,
        admission=heal_first,
    ) as scheduler:
        summary = scheduler.run()
        rows = attempts_of(scheduler.store, ONE_KEY)
        charged = scheduler.store.charged_attempts(ONE_KEY)
        unstarted = [r["detail"] for r in scheduler.store.q("SELECT detail FROM events WHERE kind='run_unstarted'")]
    assert summary.statuses == {"done": 1}
    assert [state for state, _, _ in rows] == ["done"]
    assert charged == 0
    assert len(unstarted) == 1
    assert "no attempt used" in unstarted[0]
    assert "a heal began" in unstarted[0]


def test_o35_a_failing_pair_whose_epoch_cannot_be_raised_still_refuses_the_waiting_run(tmp_path, monkeypatch):
    """O35: another check finds a pair failing, but the link epoch's record cannot be written: the record is removed
    instead of left at its old count, so the waiting run is refused, uncharged, and a later check makes the record
    anew, under which a run checked afresh is admitted."""
    gate, federation, _, settings = gate_of(tmp_path, heal="off")
    federation.stuck = set()
    admit = admission_in(tmp_path)

    def unwritable(self, record):
        raise OSError("the journal's disk is full")

    with prerun.opened():
        assert gate().ok
        federation.stuck = {(READER, CRASHED)}
        with monkeypatch.context() as patched:
            patched.setattr(LinkEpoch, "_write", unwritable)
            failing = elsewhere(gate)
        assert failing.ok is False
        assert "could not be raised" in failing.detail
        assert LinkEpoch(settings.journal).read() is None
        with pytest.raises(FederationBusy, match="missing"):
            admitted(admit)
    federation.stuck = set()
    with prerun.opened():
        assert gate().ok
        assert admitted(admit) == "shared"


def test_o35_an_admission_waits_for_a_link_check_under_way_and_is_refused_when_it_finds_a_pair_failing(tmp_path):
    """O35: another check is reading the pairs when the run's admission re-reads the epoch; the admission waits for
    that check to end, so a pair it finds failing refuses the run instead of slipping in between the finding and the
    raise of the epoch."""
    gate, federation, check, _ = gate_of(tmp_path, heal="off")
    federation.stuck = set()
    admit = admission_in(tmp_path)
    entered, release = threading.Event(), threading.Event()
    checked, refused = [], []

    def held_open():
        entered.set()
        release.wait(10)

    with prerun.opened():
        assert gate().ok
        try_context = contextvars.copy_context()  # the try's own: it refers to the note the check just wrote
    federation.stuck = {(READER, CRASHED)}
    check.on_check = held_open
    checker = threading.Thread(target=lambda: checked.append(elsewhere(gate)))
    checker.start()
    try:
        assert entered.wait(10)

        def admission():
            try:
                admitted(admit)
            except FederationBusy as error:
                refused.append(error)

        admitting = threading.Thread(target=try_context.run, args=(admission,))
        admitting.start()
        admitting.join(0.3)
        assert admitting.is_alive()  # it waits for the check under way
    finally:
        release.set()
        checker.join(10)
    admitting.join(10)
    assert checked[0].ok is False
    assert len(refused) == 1
    assert "found a pair failing" in str(refused[0])


def test_o35_a_link_check_under_way_past_the_admissions_bound_refuses_the_run_uncharged(tmp_path):
    """O35: a check under way outlasts the admission's bound (`admission_timeout_s`): the run is refused, uncharged,
    instead of waiting for ever or being admitted on a count the check may still raise."""
    gate, federation, check, _ = gate_of(tmp_path, heal="off", admission_timeout_s=0.2)
    federation.stuck = set()
    admit = admission_in(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def held_open():
        entered.set()
        release.wait(10)

    with prerun.opened():
        assert gate().ok
        check.on_check = held_open
        checker = threading.Thread(target=lambda: elsewhere(gate))
        checker.start()
        try:
            assert entered.wait(10)
            with pytest.raises(FederationBusy, match="under way") as refused:
                admitted(admit)
        finally:
            release.set()
            checker.join(10)
    assert refused.value.kind == FEDERATION_BUSY


def test_o35_a_raise_the_directory_refuses_spoils_the_record_in_place_so_the_waiting_run_is_refused(
    tmp_path, monkeypatch
):
    """O35: the journal's directory refuses both the new record and the removal of the old one, while the record file
    itself still takes writes: the record is emptied in place, which no reader takes for a count, so the waiting run is
    refused; once the directory takes writes again, the next check makes the record anew and a run checked afresh is
    admitted."""
    gate, federation, _, settings = gate_of(tmp_path, heal="off")
    federation.stuck = set()
    admit = admission_in(tmp_path)

    def refused_by_the_directory(self, *args):
        raise PermissionError("the directory takes no new entry")

    with prerun.opened():
        assert gate().ok
        federation.stuck = {(READER, CRASHED)}
        with monkeypatch.context() as patched:
            patched.setattr(LinkEpoch, "_write", refused_by_the_directory)
            patched.setattr(LinkEpoch, "_remove", refused_by_the_directory)
            assert elsewhere(gate).ok is False
        assert (settings.journal / "link-epoch.json").read_bytes() == b""
        with pytest.raises(FederationBusy, match="missing"):
            admitted(admit)
    federation.stuck = set()
    with prerun.opened():
        assert gate().ok
        assert admitted(admit) == "shared"


def test_o35_a_raise_that_nothing_can_record_keeps_every_admission_out_until_one_is_recorded(tmp_path, monkeypatch):
    """O35: neither a new record, nor the removal of the old one, nor an overwrite of it can be made: the gate keeps
    `link-epoch.lock` held, so every admission's wait runs out and the run is refused, uncharged, instead of being
    admitted on the old count; the gate's next check that records the raise lets it go, and a run checked afresh is
    admitted."""
    gate, federation, _, _ = gate_of(tmp_path, heal="off", admission_timeout_s=0.2)
    federation.stuck = set()
    admit = admission_in(tmp_path)

    def read_only(self, *args):
        raise PermissionError("the journal is read-only")

    with prerun.opened():
        assert gate().ok
        federation.stuck = {(READER, CRASHED)}
        with monkeypatch.context() as patched:
            for step in ("_write", "_remove", "_truncate"):
                patched.setattr(LinkEpoch, step, read_only)
            assert elsewhere(gate).ok is False
        with pytest.raises(FederationBusy, match="under way"):
            admitted(admit)
    federation.stuck = set()
    with prerun.opened():
        assert gate().ok  # it records the raise it owed, and lets the lock go
        assert admitted(admit) == "shared"


NEEDS_THE_GATE = (
    "an outage-capable federation needs the gate's health check, or runs would be admitted without a link verdict"
)


@pytest.mark.parametrize("heal", ["off", "all_links"])
def test_o35_a_faults_campaign_with_no_health_check_is_refused_at_load_naming_why_and_the_fix(tmp_path, heal):
    """O35: with `[faults]`, a try with no health check has no note and would be admitted blind, so the campaign is
    refused when the scheduler is built, before its lock or store: the message says why and to pass the platform's
    health check, whatever the heal mode."""
    federation_dir = tmp_path / "federation"
    gate_of(federation_dir, heal=heal)
    admit = admission_in(federation_dir)
    assert NEEDS_THE_GATE in required_reason(admit)
    with pytest.raises(ConfigError) as refused:
        build(tmp_path / "campaign", schema=ONE, executor=FakeExecutor(), plan=SMALL, admission=admit)
    message = str(refused.value)
    assert NEEDS_THE_GATE in message
    assert "PlatformHealth" in message
    assert "FaultGate" in message
    assert not (tmp_path / "campaign" / "campaign.lock").exists()


def test_o35_a_faults_campaign_with_the_platforms_health_check_starts_and_runs(tmp_path):
    """O35: given the gate as its health check, the same campaign starts and runs its run, admitted on the check's
    note."""
    federation_dir = tmp_path / "federation"
    gate, federation, _, _ = gate_of(federation_dir, heal="all_links")
    federation.stuck = set()
    with build(
        tmp_path / "campaign",
        schema=ONE,
        executor=FakeExecutor(),
        plan=SMALL,
        settings=settings_for(max_defer_s=LONG_DEFERRAL_S),
        health=gate,
        admission=admission_in(federation_dir),
    ) as scheduler:
        summary = scheduler.run()
    assert summary.statuses == {"done": 1}


def test_o35_a_platform_without_faults_declares_nothing_and_a_campaign_with_no_health_check_starts_as_before(tmp_path):
    """O35: without `[faults]` there is no run admission, so nothing needs a health check: today's behaviour stands."""
    (tmp_path / "plain").mkdir()
    config = load_platform(write_platform(tmp_path / "plain"), require_secrets=False)
    admit = run_admission(config)
    assert admit is None
    assert required_reason(admit) is None
    with build(
        tmp_path / "campaign", schema=ONE, executor=FakeExecutor(), plan=SMALL, settings=settings_for(), admission=admit
    ) as scheduler:
        summary = scheduler.run()
    assert summary.statuses == {"done": 1}


def test_o35_an_admission_wrapped_without_the_declaration_stops_a_campaign_with_no_health_check_loudly(tmp_path):
    """O35: a study wraps the platform's admission in a function that leaves the declaration out, so the scheduler's
    refusal at load never sees it. The try of a campaign with no health check carries a note that says so, and the
    platform's admission, reached through the wrapper, stops the campaign instead of admitting the run blind: no
    attempt is made, charged or scored."""
    federation_dir = tmp_path / "federation"
    gate_of(federation_dir, heal="off")
    admit = admission_in(federation_dir)

    @contextlib.contextmanager
    def wrapper(kinds, targets, cancel, exclusive):  # no `functools.wraps`: the attribute is not carried over
        with admit(kinds, targets, cancel, exclusive) as mode:
            yield mode

    with build(
        tmp_path / "campaign",
        schema=ONE,
        executor=FakeExecutor(),
        plan=SMALL,
        settings=settings_for(),
        admission=wrapper,
    ) as scheduler:
        with pytest.raises(RuntimeError, match="stopped by an unexpected error") as stopped:
            scheduler.run()
        rows = attempts_of(scheduler.store, ONE_KEY)
    assert isinstance(stopped.value.__cause__, AdmissionRefused)
    assert NEEDS_THE_GATE in str(stopped.value.__cause__)
    assert "keep" in str(stopped.value.__cause__)  # the fix: the wrapper must keep the declaration
    assert rows == []


def test_o35_the_platforms_admission_is_called_with_keywords_as_it_was_before_it_declared_anything(tmp_path):
    """O35: a caller that names the admission's arguments is admitted as before the declaration, and a caller that is
    no campaign (no try note) is admitted without a health check."""
    federation_dir = tmp_path / "federation"
    gate_of(federation_dir, heal="off")
    admit = admission_in(federation_dir)
    with admit(treatment_kinds=(), treatment_targets={}, cancel=threading.Event(), exclusive=False) as mode:
        assert mode == "shared"


def held_by_the_executor(federation_dir):
    """The federation held the way an executor does that takes it itself, not through a run admission of the
    campaign's: the platform's settings, and a shared hold of the lock as the running task's admission."""
    platform = federation_dir / "platform.toml"
    config = load_platform(platform, require_secrets=False)
    lock = FederationLock(load_faults(platform, config))
    return config, lock.held(exclusive=False, timeout_s=5, cancel=threading.Event(), purpose=EXCLUSIVE_RUN)


def test_o35_a_runs_infrastructure_in_a_try_of_a_campaign_with_no_health_check_is_refused_under_any_admission(tmp_path):
    """O35: a campaign that gave no run admission (the executor takes the federation itself) and has no health check
    passes neither the refusal at load nor the run admission's. The sandbox and the tool server's `require_admission`
    reads the try's note, which says there is no check, and refuses, however the federation was taken; a try with a
    check, and a tool outside any campaign (no note), pass."""
    federation_dir = tmp_path / "federation"
    gate_of(federation_dir, heal="off")
    config, held = held_by_the_executor(federation_dir)
    with held, prerun.opened(checked=False), pytest.raises(AdmissionRefused, match="needs the gate's health check"):
        require_admission(config)
    config, held = held_by_the_executor(federation_dir)
    with held, prerun.opened():
        require_admission(config)
    config, held = held_by_the_executor(federation_dir)
    with held:
        require_admission(config)


def test_o35_a_campaign_with_no_admission_and_no_health_check_never_scores_a_run_that_starts_infrastructure(tmp_path):
    """O35: the study gave `admission=None` and no health check, and its executor takes the federation itself and
    starts its infrastructure. The platform refuses that start in the run's try, so nothing is scored."""
    federation_dir = tmp_path / "federation"
    gate_of(federation_dir, heal="off")
    config, held = held_by_the_executor(federation_dir)
    executor = FakeExecutor()

    async def start_infrastructure(request):
        with held:
            require_admission(config)
        return await executor._run(request, "finish")

    executor.default = start_infrastructure
    with build(
        tmp_path / "campaign", schema=ONE, executor=executor, plan=SMALL, settings=settings_for(max_attempts=1)
    ) as scheduler:
        try:
            summary = scheduler.run()
        except RuntimeError:  # a campaign stopped by the platform's refusal is as good as one that scores nothing
            summary = None
        scored = scheduler.store.q("SELECT state FROM attempts WHERE state='done'")
    assert scored == []
    assert summary is None or "done" not in summary.statuses
