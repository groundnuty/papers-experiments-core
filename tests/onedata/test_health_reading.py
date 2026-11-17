"""O90: the check's structured reading, and `target_only`, which a study asks whether an outage was the run's own.

The reading is per space (the declared providers Onezone reports offline, by layout label, and whether the space was
read completely) and per run (the targets its own attempt's injections took down, by label). It sits beside the text
detail, which `test_health_text.py` pins; a reading that cannot say never says yes.
"""

import dataclasses
import gc
import json
import threading

import pytest
from campaign_support import planned_store
from fault_support import FakeServices, injector_for
from health_support import FOURTH, down, four_spaces, up

from expcore.campaign import liveness, prerun
from expcore.campaign.healthgate import GO, Health, HealthGate
from expcore.records.instruments import finish_instruments
from expcore_onedata.admission import run_admission
from expcore_onedata.config import load_platform
from expcore_onedata.fault_journal import InjectionJournal
from expcore_onedata.fault_owner import NO_ATTEMPT, in_progress_of
from expcore_onedata.faults import load_faults
from expcore_onedata.federation import ReportedProvider, SpaceView
from expcore_onedata.health import compare
from expcore_onedata.healthreading import (
    FACT,
    NAMESPACE,
    READING_NOTE,
    UNREAD,
    HealthReading,
    PostRunHealth,
    SpaceReading,
    checked,
    noted_reading,
    own_injections,
    own_targets,
    read_space,
    reading_in,
    reading_in_instruments,
    target_only,
)
from expcore_onedata.sandbox import infrafacts
from expcore_onedata.testing import ALPHA_ID, BETA_ID, write_platform

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
TARGET, OTHER = "providers[1]", "providers[2]"
ATTEMPT = {"process": "host-a:100:1", "campaign": "camp-a", "attempt_id": 8}
ANOTHER = {**ATTEMPT, "attempt_id": 7}


def space(index, *offline, read=True):
    return SpaceReading(f"spaces[{index}]", read, frozenset(offline))


def reading_of(spaces, targets=frozenset()):
    return HealthReading(tuple(spaces), None if targets is None else frozenset(targets))


def four(*offline_by_space, targets=(TARGET,)):
    """Four read spaces, each with the offline set given (a tuple of labels) or none."""
    padded = [*offline_by_space, *[()] * (4 - len(offline_by_space))]
    return reading_of([space(i, *labels) for i, labels in enumerate(padded)], targets)


# --- target_only: the pure judgement ------------------------------------------------------------------------------


def test_o90_the_target_down_in_one_of_four_spaces_reads_target_only():
    """O90: every space read, and the only provider offline anywhere is the run's own target."""
    assert target_only(four((), (TARGET,))) is True


def test_o90_the_target_down_in_every_space_it_supports_reads_target_only():
    """O90: the same target offline in two spaces, which Onezone's per-provider flag makes the usual case."""
    assert target_only(four((), (TARGET,), (), (TARGET,))) is True


@pytest.mark.parametrize("index", [0, 1, 2, 3])
def test_o90_a_second_provider_down_in_any_space_is_not_target_only(index):
    """O90: another provider's offline flag in any one space makes the run not target-only, wherever it shows."""
    offline = [(), (TARGET,), (), (TARGET,)]
    offline[index] = (*offline[index], OTHER)
    assert target_only(four(*offline)) is False


@pytest.mark.parametrize("index", [0, 1, 2, 3])
def test_o90_one_unread_space_is_unknown_never_true(index):
    """O90: a space that was not read cannot be called clean, even when every other space shows only the target."""
    spaces = [space(i, TARGET) if i != index else space(i, read=False) for i in range(4)]
    assert target_only(reading_of(spaces)) is None


def test_o90_an_unread_space_is_unknown_even_beside_another_providers_outage():
    """O90: the rule is the brief's: any unread space makes the answer unknown."""
    spaces = [space(0, OTHER), space(1, read=False), space(2), space(3)]
    assert target_only(reading_of(spaces)) is None


def test_o90_no_outage_and_all_online_is_true_with_no_targets():
    """O90: nothing offline and nothing injected is vacuously the run's own."""
    assert target_only(four(targets=())) is True


def test_o90_a_restored_run_with_everything_online_is_true_with_its_targets_noted():
    """O90: the run's target is back online: nothing is offline, so the offline set is inside the targets."""
    assert target_only(four()) is True


def test_o90_an_offline_provider_the_run_did_not_take_down_is_not_target_only():
    """O90: a run with no injection of its own, beside a provider that is offline, is a wider outage."""
    assert target_only(four((), (TARGET,), targets=())) is False


def test_o90_several_targets_cover_several_offline_providers_and_no_more():
    """O90: a run that took two providers down is target-only while the offline set stays inside both."""
    both = (TARGET, OTHER)
    assert target_only(four((), both, (OTHER,), (), targets=both)) is True
    assert target_only(four((), both, (OTHER, "providers[3]"), (), targets=both)) is False


def test_o90_targets_that_could_not_be_read_make_an_outage_unknown_and_a_clean_federation_true():
    """O90: with the run's own targets unknown, an offline provider cannot be called the run's; with nothing offline
    there is nothing to attribute."""
    assert target_only(four((), (TARGET,), targets=None)) is None
    assert target_only(four(targets=None)) is True


@pytest.mark.parametrize("missing", [None, UNREAD, HealthReading()])
def test_o90_no_reading_is_unknown(missing):
    """O90: an absent reading, and the one a call that never reached the spaces leaves, say nothing."""
    assert target_only(missing) is None


def test_o90_an_unread_space_cannot_claim_providers_offline():
    """O90: a space not read has no offline set to claim; building one that does is refused."""
    with pytest.raises(ValueError, match="unread"):
        SpaceReading("spaces[0]", False, frozenset({TARGET}))


def test_o90_a_reading_round_trips_through_its_json_and_refuses_any_malformed_one():
    """O90: the note and the record hold JSON, and what is read back is the reading or None, never a guess."""
    reading = four((), (TARGET,), targets=(TARGET, OTHER))
    assert HealthReading.from_json(json.loads(json.dumps(reading.to_json()))) == reading
    assert HealthReading.from_json(UNREAD.to_json()) == UNREAD
    unknown = four(targets=None)
    assert HealthReading.from_json(unknown.to_json()) == unknown
    good = reading.to_json()
    for bad in (
        None,
        [],
        {},
        {**good, "spaces": "x"},
        {**good, "targets": "providers[1]"},
        {**good, "targets": ["a name"]},
        {**good, "held": "providers[1]"},
        {**good, "held": ["providers[9]"]},
        {key: value for key, value in good.items() if key != "held"},
        {**good, "spaces": [{"position": "spaces[0]", "read": "yes", "offline": []}]},
        {**good, "spaces": [{"position": "spaces[0]", "read": False, "offline": [TARGET]}]},
        {**good, "spaces": [{"position": "a space", "read": True, "offline": []}]},
        {**good, "spaces": [{"position": "spaces[0]", "read": True, "offline": ["a provider"]}]},
    ):
        assert HealthReading.from_json(bad) is None


# --- the space's reading agrees with the check's own verdict -------------------------------------------------------


def declared_view(config, *, online=(True,), names=None):
    """A reported view of the study space with one reported provider per flag in `online`, the first with the declared
    id, the rest invented."""
    first = ReportedProvider(ALPHA_ID, "a", "alpha.test", online[0])
    rest = [ReportedProvider(f"pr-other-{i}", "x", f"x{i}.test", flag) for i, flag in enumerate(online[1:])]
    return SpaceView("spmock0001", names or config.space.name, (first, *rest))


@pytest.mark.parametrize(
    ("online", "name", "read", "offline"),
    [
        ((True,), None, True, ()),
        ((False,), None, True, ("providers[0]",)),
        ((True,), "renamed", False, ()),
        ((True, True), None, False, ()),  # a reported provider no declaration fits
        ((False, True), None, False, ()),
    ],
    ids=["online", "offline", "renamed", "stranger", "stranger-and-offline"],
)
def test_o90_the_space_reading_is_complete_exactly_when_the_check_found_no_structural_difference(
    tmp_path, fake, online, name, read, offline
):
    """O90: `read` is true only when the check's own comparison got past the name and the membership, so a space with
    a difference is never read as clean; its offline set is then the check's `offline` branch, label for label."""
    config = load_platform(write_platform(tmp_path))
    view = declared_view(config, online=online, names=name)
    entry = config.space_named()
    reading = read_space(view, config, entry)
    verdict = compare(view, config, entry)
    assert (reading.position, reading.read, tuple(sorted(reading.offline))) == ("spaces[0]", read, offline)
    if read and not offline:
        assert verdict.ok is True
    if read and offline:
        assert verdict.detail.startswith("declared provider(s) reported offline")
    if not read:
        assert verdict.ok is False
        assert not verdict.detail.startswith("declared provider(s) reported offline")


def test_o90_a_provider_whose_online_flag_is_not_a_boolean_leaves_the_space_unread(tmp_path, fake):
    """O90: an online flag that is neither true nor false is no reading; `compare()` counts only `False` as offline."""
    config = load_platform(write_platform(tmp_path))
    view = SpaceView("spmock0001", config.space.name, (ReportedProvider(ALPHA_ID, "a", "alpha.test", None),))
    assert read_space(view, config, config.space_named()).read is False


def test_o90_a_host_only_declaration_that_two_reported_providers_fit_is_unread(tmp_path, fake):
    """O90: an ambiguous match is not a read space."""
    config = load_platform(write_platform(tmp_path))
    host_only = dataclasses.replace(config.providers["alpha"], id=None)
    config = dataclasses.replace(config, providers={**config.providers, "alpha": host_only})
    twice = (
        ReportedProvider("pr-one", "one", "alpha.test", True),
        ReportedProvider("pr-two", "two", "alpha.test", True),
    )
    assert read_space(SpaceView("spmock0001", config.space.name, twice), config, config.space_named()).read is False


# --- the run's own targets, read from the journal ----------------------------------------------------------------


def journal_of(tmp_path):
    directory = tmp_path / "journal"
    directory.mkdir()
    return InjectionJournal(directory)


def injection(journal, key, target, owner, *, asked=True, closed=True, **line):
    """One injection as the injector journals it: `injecting`, then `stopping`, `injected` and `cleared`."""
    journal.append({"event": "injecting", "id": key, "target": target, "service": "svc", "owner": owner, **line})
    if asked:
        journal.append({"event": "stopping", "id": key})
        journal.append({"event": "injected", "id": key})
    if closed:
        journal.append({"event": "cleared", "id": key})


def test_o90_the_targets_are_the_ones_the_judged_attempt_took_down_cleared_or_open(tmp_path):
    """O90: a restored injection is gone from the open entries, but it is still the run's own target."""
    journal = journal_of(tmp_path)
    injection(journal, "a", TARGET, ATTEMPT)
    injection(journal, "b", OTHER, ATTEMPT, closed=False)
    assert own_targets(journal, ATTEMPT) == {TARGET, OTHER}


def test_o90_another_attempts_injection_is_never_the_runs_own(tmp_path):
    """O90: the earlier attempt of the same run, another run's, a process alone and a line with no owner are not this
    attempt's: counting one would let a wider outage pass as the run's own."""
    journal = journal_of(tmp_path)
    injection(journal, "a", OTHER, ANOTHER)
    injection(journal, "b", OTHER, {"process": ATTEMPT["process"]})
    injection(journal, "c", OTHER, {**ATTEMPT, "campaign": "camp-b"})
    injection(journal, "d", OTHER, {**ATTEMPT, "process": "host-b:5:1"})
    journal.append({"event": "injecting", "id": "e", "target": OTHER, "service": "svc"})
    journal.append({"event": "stopping", "id": "e"})
    injection(journal, "f", TARGET, ATTEMPT)
    assert own_targets(journal, ATTEMPT) == {TARGET}


def test_o90_a_heals_restart_and_an_injection_never_sent_are_not_the_runs_targets(tmp_path):
    """O90: a heal's container restart is a repair's, not the condition's injection; a line whose stop was never asked
    for took nothing down."""
    journal = journal_of(tmp_path)
    injection(journal, "a", OTHER, ATTEMPT, heal=True)
    injection(journal, "b", OTHER, ATTEMPT, asked=False)
    assert own_targets(journal, ATTEMPT) == frozenset()


def test_o90_a_stop_of_unknown_outcome_counts_as_the_runs_target(tmp_path):
    """O90: the stop was asked for and may have taken the target down: it is the run's own."""
    journal = journal_of(tmp_path)
    journal.append({"event": "injecting", "id": "a", "target": TARGET, "service": "svc", "owner": ATTEMPT})
    journal.append({"event": "stop_unknown", "id": "a"})
    assert own_targets(journal, ATTEMPT) == {TARGET}


def test_o90_no_attempt_has_no_targets_and_reads_no_journal(tmp_path):
    """O90: a check that judges no attempt (a pre-run check, the dispatcher's) has no injection of its own, so even an
    unreadable journal does not matter to it; a platform with no `[faults]` has no journal and no injection."""
    journal = journal_of(tmp_path)
    (journal.directory / "injections.jsonl").write_text("not json\n", encoding="utf-8")
    assert own_targets(journal, None) == frozenset()
    assert own_targets(None, ATTEMPT) == frozenset()


def test_o90_an_unreadable_journal_leaves_the_targets_unknown(tmp_path):
    """O90: a journal that cannot be read is no empty one: the targets are None, never an empty set."""
    journal = journal_of(tmp_path)
    (journal.directory / "injections.jsonl").write_text('{"event": "injecting"\n', encoding="utf-8")
    assert own_targets(journal, ATTEMPT) is None
    (journal.directory / "injections.jsonl").write_text('{"event": "mystery", "id": "a"}\n', encoding="utf-8")
    assert own_targets(journal, ATTEMPT) is None
    (journal.directory / "injections.jsonl").write_text("[1]\n", encoding="utf-8")
    assert own_targets(journal, ATTEMPT) is None


def test_o90_a_journal_that_does_not_exist_yet_holds_no_injection(tmp_path):
    """O90: no journal file is no injection."""
    assert own_targets(InjectionJournal(tmp_path / "none-yet"), ATTEMPT) == frozenset()


# --- the reading a four-space platform makes ------------------------------------------------------------------------


def shape(reading):
    return [(item.position, item.read, tuple(sorted(item.offline))) for item in reading.spaces]


def test_o90_a_four_space_platform_with_the_target_down_reads_target_only(tmp_path, fake):
    """O90: the run's own injection of providers[1] has been restored, and Onezone still reports it offline in the
    two spaces it supports; the text names the first of them, the reading holds both and calls the run target-only."""
    world = four_spaces(tmp_path, fake, faults={})
    injector = injector_for(world.platform, FakeServices())
    with liveness.running("camp-a", 5):
        injector.start(RUN)
        assert injector.finish()["ok"] is True
        down(fake, "beta")
        verdict = world.health.space()
        reading = world.health.reading
    assert verdict.detail == "spaces[1]: declared provider(s) reported offline: providers[1]"
    assert shape(reading) == [
        ("spaces[0]", True, ()),
        ("spaces[1]", True, (TARGET,)),
        ("spaces[2]", True, ()),
        ("spaces[3]", True, (TARGET,)),
    ]
    assert reading.targets == {TARGET}
    assert target_only(reading) is True


def test_o90_a_second_provider_down_beside_the_target_is_not_target_only(tmp_path, fake):
    """O90: providers[2] is offline too, in the spaces it supports: the run is not target-only, though the text names
    only the first failing space."""
    world = four_spaces(tmp_path, fake, faults={})
    injector = injector_for(world.platform, FakeServices())
    with liveness.running("camp-a", 5):
        injector.start(RUN)
        assert injector.finish()["ok"] is True
        down(fake, "beta", "gamma")
        verdict = world.health.space()
        reading = world.health.reading
    assert verdict.detail == "spaces[0]: declared provider(s) reported offline: providers[2]"
    assert shape(reading)[0] == ("spaces[0]", True, (OTHER,))
    assert target_only(reading) is False


def test_o90_a_run_whose_target_is_back_and_everything_online_reads_true(tmp_path, fake):
    """O90: the restore is seen by Onezone: nothing is offline, the targets are noted, and the text is the pass."""
    world = four_spaces(tmp_path, fake, faults={})
    injector = injector_for(world.platform, FakeServices())
    with liveness.running("camp-a", 5):
        injector.start(RUN)
        assert injector.finish()["ok"] is True
        verdict = world.health.space()
        reading = world.health.reading
    assert verdict.ok is True
    assert reading.targets == {TARGET}
    assert reading.held == frozenset()  # restored: nothing is held down any more
    assert target_only(reading) is True


def test_o90_no_outage_and_all_online_reads_true_with_no_targets(tmp_path, fake):
    """O90: a run with no injection, on a platform with `[faults]`, inside an attempt: no targets, nothing offline."""
    world = four_spaces(tmp_path, fake, faults={})
    with liveness.running("camp-a", 5):
        assert world.health.space().ok is True
        reading = world.health.reading
    assert reading.targets == frozenset()
    assert target_only(reading) is True


def test_o90_a_pre_run_check_has_no_targets_so_any_offline_provider_is_not_the_runs(tmp_path, fake):
    """O90: outside an attempt a check has no run of its own: whatever is offline is not target-only."""
    world = four_spaces(tmp_path, fake, faults={})
    down(fake, "beta")
    world.health.space()
    assert world.health.reading.targets == frozenset()
    assert target_only(world.health.reading) is False


def test_o90_a_platform_without_faults_has_known_empty_targets(tmp_path, fake):
    """O90: with no `[faults]` there is no injector and no journal: the targets are known to be empty."""
    world = four_spaces(tmp_path, fake)
    with liveness.running("camp-a", 5):
        world.health.space()
    assert world.health.reading.targets == frozenset()


def test_o90_an_unreadable_scope_for_one_space_leaves_it_unread_and_the_answer_unknown(tmp_path, fake):
    """O90: the answer does not describe the third space; the reading holds the others and calls it unread, and the
    text of an earlier failure is untouched (`test_health_text.py`)."""
    world = four_spaces(tmp_path, fake)
    down(fake, "beta")
    del fake.space_supports["sp-third-0003"]
    world.health.space()
    assert [item.read for item in world.health.reading.spaces] == [True, True, False, True]
    assert target_only(world.health.reading) is None


def test_o90_a_space_the_data_path_token_does_not_reach_is_unread(tmp_path, fake):
    """O90: a space only the tool server's token reaches is not in the scope this check reads, so its reading is
    unread and the platform's answer is unknown whatever else is true; the text counts only the spaces it checked."""
    world = four_spaces(tmp_path, fake, edit=lambda data: data["spaces"][2].update(tokens=["tool_server"]))
    verdict = world.health.space()
    assert verdict.detail == "ok: each of the 3 spaces is supported by its declared set"
    assert [item.read for item in world.health.reading.spaces] == [True, True, False, True]
    assert target_only(world.health.reading) is None


@pytest.mark.parametrize(
    "remove",
    [("sp-second-0002", BETA_ID), (FOURTH[2], BETA_ID)],
    ids=["second-space-lacks-its-provider", "fourth-space-lacks-its-provider"],
)
def test_o90_a_space_whose_support_set_differs_is_unread_not_clean(tmp_path, fake, remove):
    """O90: a provider the federation no longer reports for a space is not an offline provider: the comparison
    returned before computing one, so the space is unread and the answer unknown, never true."""
    world = four_spaces(tmp_path, fake)
    del fake.space_supports[remove[0]][remove[1]]
    assert world.health.space().ok is False
    assert target_only(world.health.reading) is None


def test_o90_a_stranger_in_a_space_is_unread_not_clean(tmp_path, fake):
    """O90: a provider no declaration fits, supporting a space, is a difference the offline set cannot describe."""
    world = four_spaces(tmp_path, fake)
    fake.add_provider("pr-stranger", "stranger.test", supports=False)
    fake.space_supports["sp-second-0002"]["pr-stranger"] = {"readonly": False}
    assert world.health.space().ok is False
    assert target_only(world.health.reading) is None


def test_o90_an_unreadable_journal_makes_an_outage_unknown_and_a_clean_run_true(tmp_path, fake):
    """O90: the platform's own journal cannot be read: with the target offline the answer is unknown, with nothing
    offline it is true, since there is nothing to attribute."""
    world = four_spaces(tmp_path, fake, faults={})
    journal_dir = load_faults(world.platform, world.config).journal
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / "injections.jsonl").write_text("garbage\n", encoding="utf-8")
    with liveness.running("camp-a", 5):
        world.health.space()
        assert world.health.reading.targets is None
        assert target_only(world.health.reading) is True
        down(fake, "beta")
        world.health.space()
        assert target_only(world.health.reading) is None


def test_o90_another_attempts_injection_in_the_platforms_journal_is_not_this_attempts(tmp_path, fake):
    """O90: through the platform, with the journal the injector writes: attempt 7 took providers[2] down, attempt 8 is
    judged, and providers[2] is offline: the run is not target-only."""
    world = four_spaces(tmp_path, fake, faults={})
    journal = InjectionJournal(load_faults(world.platform, world.config).journal)
    injection(journal, "a", OTHER, ANOTHER)
    down(fake, "gamma")
    with liveness.running("camp-a", 8):
        world.health.space()
        reading = world.health.reading
    assert reading.targets == frozenset()
    assert target_only(reading) is False


# --- a reading is per call ------------------------------------------------------------------------------------------


def test_o90_there_is_no_reading_before_a_check_and_a_call_that_never_reached_the_spaces_leaves_none(tmp_path, fake):
    """O90: a call answered before the space was read (here, deferred for an outage run that holds the federation)
    leaves the unread reading, never the earlier call's: a post-run check that read nothing says nothing."""
    world = four_spaces(tmp_path, fake, faults={"health_wait_s": 0.2})
    assert world.health.reading is None
    assert world.health.space().ok is True
    assert target_only(world.health.reading) is True
    injector = injector_for(world.platform, FakeServices())
    injector.start(RUN)
    try:
        answer = world.health()
    finally:
        assert injector.finish()["ok"] is True
    assert (answer.ok, answer.deferred) == (False, True)
    assert world.health.reading == UNREAD
    assert target_only(world.health.reading) is None


def test_o90_each_thread_keeps_its_own_calls_reading(tmp_path, fake):
    """O90: the check is shared by every worker and each check runs in a thread of its own; one thread's reading is
    never another's."""
    world = four_spaces(tmp_path, fake)
    down(fake, "beta")
    seen = {}

    def read_in_a_thread():
        seen["before"] = world.health.reading
        world.health.space()
        seen["after"] = world.health.reading

    thread = threading.Thread(target=read_in_a_thread)
    thread.start()
    thread.join()
    assert seen["before"] is None
    assert target_only(seen["after"]) is False
    assert world.health.reading is None


def test_o90_the_try_note_holds_the_latest_calls_reading_as_json(tmp_path, fake):
    """O90: inside a try the reading is also the try's note, which a study's wrapper check reads after the check ran, in
    the thread the gate gave it; the post-run check's reading replaces the pre-run check's."""
    world = four_spaces(tmp_path, fake)
    with prerun.opened() as note:
        world.health.space()
        assert json.loads(json.dumps(note[READING_NOTE])) == world.health.reading.to_json()
        assert noted_reading() == world.health.reading
        down(fake, "beta")
        world.health.space()
        assert noted_reading() == world.health.reading
        assert target_only(noted_reading()) is False
        up(fake, "beta")
        world.health.space()
        assert noted_reading().offline == frozenset()  # the earlier call's offline set is gone with it
        assert target_only(noted_reading()) is True
    assert noted_reading() is None  # outside a try there is no note and nothing fails


def test_o90_the_reading_makes_no_request_beyond_the_checks_own(tmp_path, fake):
    """O90: the reading is made from the answer the check already read: one scope request per call."""
    world = four_spaces(tmp_path, fake)
    asked = len(fake.requests)
    world.health.space()
    scope = [r for r in fake.requests[asked:] if r.path.endswith("/tokens/infer_access_token_scope")]
    assert len(scope) == 1


def test_o90_the_json_names_spaces_and_providers_by_label_only(tmp_path, fake):
    """O90: the JSON names spaces and providers by layout label only (O12), never a name, an id or a host."""
    world = four_spaces(tmp_path, fake)
    down(fake, "beta")
    world.health.space()
    text = json.dumps(world.health.reading.to_json())
    for configured in ("beta", "alpha", "gamma", BETA_ID, ALPHA_ID, "ridge", "study-", "lakeside", ".test"):
        assert configured not in text


# --- through the gate: the run's own post-run check, under its admission ------------------------------------------


def outage_run(tmp_path, world, *, restore_fails):
    """What the scheduler does around an outage run of providers[1]: a try with its note, the attempt, the run's
    pre-run check through the gate, the run's exclusive admission, the injector inside it, and the post-run check
    (`HealthGate.after_run`) in a thread of its own with the context copied, as the gate runs it. Return the post-run
    answer, the try's reading and the journal entries still open."""
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    store, _, _ = planned_store(campaign)
    clock = iter(f"2026-01-01T00:{minute:02d}:00Z" for minute in range(1, 60))
    gate = HealthGate(store, world.health, lambda: next(clock))
    gate.load()
    services = FakeServices()
    injector = injector_for(world.platform, services)
    admit = run_admission(world.config)
    outage = ("provider_outage",), {"provider_outage": TARGET}
    with prerun.opened(), liveness.running("camp-a", 5):
        assert gate.before_run("run-a", attempt=1).verdict == GO
        with admit(*outage, threading.Event(), False):
            injector.start(RUN)
            if restore_fails:
                services.knobs["start_fails"] = True
                assert injector.clear() is False
            else:
                assert injector.finish()["ok"] is True
            down(world.fake, "beta")
            answer, _ = gate.after_run("run-a", attempt=1, attempt_id=5)
            reading = noted_reading()
            open_entries = InjectionJournal(load_faults(world.platform, world.config).journal).open_entries()
            services.knobs["start_fails"] = False
            assert injector.finish()["ok"] is True  # whatever the check said, the target is brought back
    return answer, reading, open_entries


def test_o90_the_gates_post_run_check_of_a_restored_outage_run_reads_its_own_target(tmp_path, fake):
    """O90: a run's post-run check, run by the gate in a thread of its own under the run's exclusive admission, after
    the restore: no entry is open, so the check reaches the spaces, the text is the unchanged first failing space, the
    try's note holds the reading, and its targets are the run's own journalled injection: target-only."""
    world = four_spaces(tmp_path, fake, faults={})
    answer, reading, open_entries = outage_run(tmp_path, world, restore_fails=False)
    assert open_entries == []
    assert answer.detail == "spaces[1]: declared provider(s) reported offline: providers[1]"
    assert reading.targets == {TARGET}
    assert target_only(reading) is True


def test_o90_the_gates_post_run_check_of_a_run_whose_restore_failed_reads_nothing(tmp_path, fake):
    """O90: the restore failed, so its entry is open and the fault gate answers before any space is read: the text is
    the failed restore's, the reading is unread, and the run is never called target-only."""
    world = four_spaces(tmp_path, fake, faults={})
    answer, reading, open_entries = outage_run(tmp_path, world, restore_fails=True)
    assert [entry["target"] for entry in open_entries] == [TARGET]
    assert answer.detail.startswith("the restore of providers[1]")
    assert reading == UNREAD
    assert target_only(reading) is None


# --- the pilot's order: the run's check inside its own open injection, the restore after ---------------------------


class Record:
    """The part of a record `finish_instruments` writes: its instruments, by namespace."""

    def __init__(self):
        self.instruments = {}

    def with_instrument(self, namespace, data):
        self.instruments = {**self.instruments, namespace: data}
        return self


def inside_injection(
    tmp_path, world, *, extra=None, report_offline=True, down_names=(), abandon=False, instrument=False, returned=False
):
    """The pilot's executor reads health inside the injection and restores after: the run's pre-run check, its
    exclusive admission, the injector started (its entry open), then the post-run check through the gate; `extra` is
    given the journal and the attempt's own token while the injection is open (to put another's lines there), and
    `down_names` go down with the target; `abandon` drops the injector (its lock goes) before the check, as an executor
    that returned without restoring would. Return the answer, the try's reading and the entries open at the check."""
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    store, _, _ = planned_store(campaign)
    clock = iter(f"2026-01-01T00:{minute:02d}:00Z" for minute in range(1, 60))
    gate = HealthGate(store, world.health, lambda: next(clock))
    gate.load()
    injectors = [injector_for(world.platform, FakeServices())]
    admit = run_admission(world.config)
    journal = InjectionJournal(load_faults(world.platform, world.config).journal)
    with prerun.opened(), liveness.running("camp-a", 5):
        assert gate.before_run("run-a", attempt=1).verdict == GO
        with admit(("provider_outage",), {"provider_outage": TARGET}, threading.Event(), False):
            injectors[0].start(RUN)
            if abandon:
                injectors.clear()
                gc.collect()
            if report_offline:
                down(world.fake, "beta", *down_names)
            if extra is not None:
                extra(journal, liveness.current())
            if (
                returned
            ):  # the executor has returned, as the scheduler's post-run check finds it: nothing is in progress
                prerun.note(prerun.EXECUTOR_RETURNED, True)
            if instrument:  # the record's way: the instrument finishes inside the injection, before the restore
                record = finish_instruments(Record(), [PostRunHealth(world.health)])
                answer, reading = record.instruments[NAMESPACE], reading_in_instruments(record.instruments)
            else:
                answer, _ = gate.after_run("run-a", attempt=1, attempt_id=5)
                reading = noted_reading()
            opened = journal.open_entries()
            for entry in opened:  # whoever's: the test's journal is left settled
                if entry["id"] != next(item["id"] for item in opened if item["target"] == TARGET):
                    journal.append({"event": "cleared", "id": entry["id"]})
            if injectors:
                assert injectors[0].finish()["ok"] is True  # the restore, after the reading
    return answer, reading, opened


def test_o90_inside_its_own_injection_the_runs_check_compares_every_space_and_reads_target_only(tmp_path, fake):
    """O90: the run's own injection is open when its post-run check runs under its own admission, as in the pilot's
    order. The fault gate does not answer "unsettled" for it: every space is compared as usual, the text is the first
    failing space's, and the reading marks the target as held by the run's own injection: target-only."""
    world = four_spaces(tmp_path, fake, faults={})
    answer, reading, opened = inside_injection(tmp_path, world)
    assert [entry["target"] for entry in opened] == [TARGET]
    assert answer.detail == "spaces[1]: declared provider(s) reported offline: providers[1]"
    assert reading.targets == {TARGET}
    assert reading.held == {TARGET}
    assert target_only(reading) is True


def test_o90_inside_its_own_injection_a_target_still_reported_online_is_a_pass_and_held(tmp_path, fake):
    """O90: Onezone has not yet seen the target go down: every space compares clean, the check passes, and the reading
    still says the target is held by the run's own injection."""
    world = four_spaces(tmp_path, fake, faults={})
    answer, reading, _ = inside_injection(tmp_path, world, report_offline=False)
    assert answer.ok is True
    assert reading.held == {TARGET}
    assert target_only(reading) is True


def test_o90_inside_its_own_injection_a_second_down_provider_is_not_target_only(tmp_path, fake):
    """O90: the own injection explains the target and nothing else: providers[2] offline too is a wider outage."""
    world = four_spaces(tmp_path, fake, faults={})
    answer, reading, _ = inside_injection(tmp_path, world, down_names=("gamma",))
    assert answer.detail == "spaces[0]: declared provider(s) reported offline: providers[2]"
    assert reading.held == {TARGET}
    assert target_only(reading) is False


def test_o90_another_attempts_open_injection_is_unsettled_and_never_target_only(tmp_path, fake):
    """O90: an open injection another attempt left is still an unsettled injection under this run's admission, never
    explained by the run's own: the fault gate answers it, uncharged, before any space is read."""

    def another(journal, _):
        injection(journal, "other", OTHER, ANOTHER, closed=False)

    world = four_spaces(tmp_path, fake, faults={})
    answer, reading, _ = inside_injection(tmp_path, world, extra=another)
    assert answer.detail.startswith("an injection of providers[2] is unsettled")
    assert answer.charge is False
    assert reading == UNREAD
    assert target_only(reading) is None


def owner_without_attempt(journal, mine):
    """What a harness that runs its injection outside any attempt leaves: the same entry, owned by the process alone."""
    lines = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
    for line in lines:
        if line.get("event") == "injecting" and line.get("target") == TARGET:
            line["owner"] = {"process": mine["process"]}
    journal.path.write_text("".join(json.dumps(line, sort_keys=True) + "\n" for line in lines), encoding="utf-8")


def test_o90_the_same_injection_owned_by_no_attempt_stays_unsettled_where_the_owner_matched_one_is_held(tmp_path, fake):
    """O90: the acceptance case. The injection a real attempt's injector journals carries the attempt as its owner
    (`test_o90_inside_its_own_injection_the_runs_check_compares_every_space_and_reads_target_only`: held, target-only).
    The same entry from an injection run outside the attempt's context names only a process: it explains nothing, the
    fault gate answers it as unsettled, charged as before with the clause that says so, and nothing is read. A study's
    injection runs inside the attempt's context, as the core's injector does, so that its entry names its attempt."""
    world = four_spaces(tmp_path, fake, faults={})
    answer, reading, opened = inside_injection(tmp_path, world, extra=owner_without_attempt)
    assert [entry["target"] for entry in opened] == [TARGET]
    assert answer.detail.startswith("an injection of providers[1] is unsettled")
    assert NO_ATTEMPT in answer.detail
    assert answer.charge is True
    assert reading == UNREAD
    assert target_only(reading) is None


def test_o90_an_open_line_with_no_owner_explains_nothing(tmp_path, fake):
    """O90: an ownerless journal line is no one's: it is not the run's own, so the check answers it as unsettled, and
    charged as before (an owner is never guessed)."""

    def ownerless(journal, _):
        journal.append({"event": "injecting", "id": "none", "target": OTHER, "service": "svc"})
        journal.append({"event": "stopping", "id": "none"})
        journal.append({"event": "injected", "id": "none"})

    world = four_spaces(tmp_path, fake, faults={})
    answer, reading, _ = inside_injection(tmp_path, world, extra=ownerless)
    assert answer.detail.startswith("an injection of providers[2] is unsettled")
    assert answer.charge is True
    assert reading == UNREAD


def never_seen_down(journal, mine):
    """An entry of the run's own whose stop was asked for but the target was never seen down."""
    journal.append({"event": "injecting", "id": "x", "target": OTHER, "service": "svc", "owner": mine})
    journal.append({"event": "stopping", "id": "x"})


def a_heals_restart(journal, mine):
    """A container restart a heal made, which names this attempt as its owner."""
    injection(journal, "h", OTHER, mine, closed=False, heal=True)


@pytest.mark.parametrize("extra", [never_seen_down, a_heals_restart], ids=["never-seen-down", "a-heals-restart"])
def test_o90_a_second_own_entry_that_is_not_in_progress_is_still_unsettled(tmp_path, fake, extra):
    """O90: of the run's own entries only an injection it holds down is explained: one never seen down, and one with
    a heal's mark, are answered as unsettled beside it."""
    world = four_spaces(tmp_path, fake, faults={})
    answer, reading, _ = inside_injection(tmp_path, world, extra=extra)
    assert answer.detail.startswith("an injection of providers[2] is unsettled")
    assert reading == UNREAD


def test_o90_an_own_injection_whose_injector_is_gone_is_still_unsettled(tmp_path, fake):
    """O90: an executor that returned without restoring, its injector dropped, leaves its entry open with no injector
    holding the target's lock: that is no injection in progress, so the scheduler's post-run check answers it as
    unsettled, charged to the run, whoever journalled it. The own-entry rule explains only a live injector's."""
    world = four_spaces(tmp_path, fake, faults={})
    answer, reading, opened = inside_injection(tmp_path, world, abandon=True)
    assert [entry["target"] for entry in opened] == [TARGET]
    assert answer.detail.startswith("an injection of providers[1] is unsettled")
    assert answer.charge is True
    assert reading == UNREAD


def a_dropped_injectors_entry(journal, mine):
    """An entry of the run's own, seen down, that no injector holds the lock of."""
    injection(journal, "g", OTHER, mine, closed=False)


def test_o90_after_the_executor_has_returned_the_runs_own_open_entry_is_unsettled_though_its_injector_lives(
    tmp_path, fake
):
    """O90: the scheduler's post-run check runs after the executor returned, when no injection of the run can be in
    progress: an own entry still open, seen down, with its injector alive and holding its lock (a pending restore
    whose `clear_failed` line was never written, a reused injector), is a failed restore, never explained. The same
    state inside the executor, before it has returned, is the held target (`test_o90_inside_its_own_injection_...`)."""
    world = four_spaces(tmp_path, fake, faults={})
    answer, reading, opened = inside_injection(tmp_path, world, returned=True)
    assert [entry["target"] for entry in opened] == [TARGET]
    assert answer.detail.startswith("an injection of providers[1] is unsettled")
    assert answer.charge is True
    assert reading == UNREAD


def test_o90_an_own_entry_no_live_injector_holds_is_unsettled_beside_the_one_that_is_held(tmp_path, fake):
    """O90: the live injector's own entry is explained, and another entry that names this attempt but whose target no
    injector holds is not."""
    world = four_spaces(tmp_path, fake, faults={})
    answer, reading, _ = inside_injection(tmp_path, world, extra=a_dropped_injectors_entry)
    assert answer.detail.startswith("an injection of providers[2] is unsettled")
    assert reading == UNREAD


def test_o90_the_runs_own_failed_restore_is_still_unsettled_under_its_admission(tmp_path, fake):
    """O90: an own entry whose restore failed (`clear_failed`) is no injection in progress: its failed restore is
    answered, charged to the run."""

    def failed_restore(journal, _):
        key = next(entry["id"] for entry in journal.open_entries() if entry["target"] == TARGET)
        journal.append({"event": "clear_failed", "id": key, "how": "not_serving"})

    world = four_spaces(tmp_path, fake, faults={})
    answer, reading, _ = inside_injection(tmp_path, world, extra=failed_restore)
    assert answer.detail.startswith("the restore of providers[1] (not_serving) failed")
    assert answer.charge is True
    assert reading == UNREAD


@pytest.mark.parametrize(
    ("entry", "explained"),
    [
        ({"injected": True, "owner": ATTEMPT}, True),
        ({"injected": False, "owner": ATTEMPT}, False),
        ({"injected": True, "owner": ANOTHER}, False),
        ({"injected": True, "owner": {"process": ATTEMPT["process"]}}, False),
        ({"injected": True}, False),
        ({"injected": True, "owner": ATTEMPT, "clear_failed": "not_serving"}, False),
        ({"injected": True, "owner": ATTEMPT, "heal": True}, False),
        ({"owner": ATTEMPT}, False),
    ],
    ids=["own", "not-seen-down", "another", "process-alone", "ownerless", "failed-restore", "heal", "no-injected-flag"],
)
def test_o90_only_an_own_injection_seen_down_with_no_failed_restore_is_in_progress(entry, explained):
    """O90: the one predicate the fault gate explains an open entry by."""
    assert in_progress_of(entry, ATTEMPT) is explained


def test_o90_no_attempt_explains_no_entry():
    """O90: a check that judges no attempt explains none, whoever's the entry is."""
    entry = {"injected": True, "owner": ATTEMPT}
    assert in_progress_of(entry, None) is False
    assert in_progress_of(entry, {"process": ATTEMPT["process"]}) is False


def test_o90_the_held_targets_are_the_own_ones_still_open(tmp_path):
    """O90: of the run's own targets, those with no `cleared` line are held by its injection."""
    journal = journal_of(tmp_path)
    injection(journal, "a", TARGET, ATTEMPT, closed=False)
    injection(journal, "b", OTHER, ATTEMPT)
    injection(journal, "c", "providers[3]", ANOTHER, closed=False)
    found = own_injections(journal, ATTEMPT)
    assert (found.targets, found.held) == ({TARGET, OTHER}, {TARGET})
    assert own_targets(journal, ATTEMPT) == found.targets


def test_o90_a_held_target_that_is_no_target_of_the_run_is_refused():
    """O90: a reading cannot hold down a provider the run did not take down."""
    with pytest.raises(ValueError, match="held"):
        HealthReading(targets=frozenset({TARGET}), held=frozenset({OTHER}))
    with pytest.raises(ValueError, match="held"):
        HealthReading(targets=None, held=frozenset({TARGET}))


# --- the reading among a run's facts -------------------------------------------------------------------------------


def test_o90_the_reading_is_a_fact_of_the_run_with_its_own_owner_and_target_only_reads_it_back_alone():
    """O90: the reading goes into a run's facts through the one writer, under the owner `health` and the key
    `post_run_health`; read back from the facts as a record carries them (JSON), `target_only` answers as it did, and
    the key is nobody else's to write."""
    assert infrafacts.OWNED[infrafacts.HEALTH] == {FACT}
    meta: dict = {}
    facts = infrafacts.InfrastructureFacts(meta, threading.RLock())
    reading = four((), (TARGET,), (), (TARGET,))
    facts.write(infrafacts.HEALTH, {FACT: reading.to_json()})
    carried = json.loads(json.dumps(meta))["infrastructure_facts"]  # what a record written to out/runs holds
    assert reading_in(carried) == reading
    assert target_only(reading_in(carried)) is True
    with pytest.raises(infrafacts.FactsConflict):
        facts.write("another", {FACT: {}})


@pytest.mark.parametrize("carried", [None, {}, {FACT: None}, {FACT: {"spaces": []}}, {FACT: "providers[1]"}])
def test_o90_a_run_whose_facts_hold_no_whole_reading_is_unknown(carried):
    """O90: absent or malformed, the reading is None and `target_only` of it is None, never True."""
    assert reading_in(carried) is None
    assert target_only(reading_in(carried)) is None


# --- the reading in the record: an instrument, the full chain, and a reading that is this call's -------------------


def test_o90_checked_returns_the_reading_of_its_own_call_and_never_the_pre_run_checks(tmp_path, fake):
    """O90: `checked` takes the reading of its one call from the check's own, never from the try's note: a wrapper that
    never asked the platform leaves no reading, a reading the pre-run check noted is not taken for the call's, the full
    chain's call returns its own, and outside a try it does as well."""
    world = four_spaces(tmp_path, fake)
    with prerun.opened() as note:
        note[READING_NOTE] = four((), (TARGET,)).to_json()  # what the pre-run check noted
        verdict, reading = checked(lambda: Health(True, "a wrapper that did not ask the platform"))
        assert (verdict.ok, reading) == (True, None)
        verdict, reading = checked(world.health)
        assert verdict.ok is True
        assert reading == world.health.reading
        assert reading.spaces
    outside = checked(world.health)[1]  # outside a try there is no note; the call's own reading is there
    assert outside is not None
    assert outside.spaces


def test_o90_overlapping_calls_in_one_try_each_get_their_own_reading(tmp_path, fake):
    """O90: an executor may make two `checked` calls at once (`asyncio.to_thread` for each) in one try, which share one
    note. A's call has returned its verdict and is held before `checked` takes its reading; B's call, begun after
    the target went down, publishes its own. A's reading is the one A's call made (nothing offline), never B's."""
    world = four_spaces(tmp_path, fake)
    a_called, b_done, got = threading.Event(), threading.Event(), {}

    def slow_a():
        verdict = world.health()
        a_called.set()
        assert b_done.wait(30)
        return verdict

    with prerun.opened():
        a = threading.Thread(target=lambda: got.update(a=checked(slow_a, source=world.health)), name="a")
        a.start()
        assert a_called.wait(30)
        down(fake, "beta")
        got["b"] = checked(world.health)
        b_done.set()
        a.join(30)
    assert target_only(got["a"][1]) is True
    assert got["a"][1].offline == frozenset()
    assert got["b"][1].offline == {TARGET}


def test_o90_a_check_that_never_asked_the_platform_leaves_no_reading_even_in_a_thread_that_read_before(tmp_path, fake):
    """O90: the calling thread's earlier reading is cleared before the call, so a wrapper that answers without asking
    the platform (a guard that refuses early) has no reading, not the previous call's."""
    world = four_spaces(tmp_path, fake)
    with prerun.opened():
        assert checked(world.health)[1] is not None
        verdict, reading = checked(lambda: Health(False, "refused by a guard"), source=world.health)
    assert (verdict.ok, reading) == (False, None)


def test_o90_the_instruments_snapshot_carries_the_reading_inside_the_injection_and_target_only_reads_the_record(
    tmp_path, fake
):
    """O90: the record's way, which the merged batch carries: an instrument listed after the injector finishes before
    its restore, inside the run's own open injection, makes the one full-chain call, and its snapshot in
    `record.instruments` holds the verdict and the reading. `target_only` of the reading read from the record alone,
    after a JSON round trip, is True with the target held."""
    world = four_spaces(tmp_path, fake, faults={})
    snapshot, reading, opened = inside_injection(tmp_path, world, instrument=True)
    assert [entry["target"] for entry in opened] == [TARGET]
    assert (snapshot["ok"], snapshot["deferred"]) == (False, False)  # the target is reported offline
    assert reading.held == {TARGET}
    carried = json.loads(json.dumps({NAMESPACE: snapshot}))
    assert target_only(reading_in_instruments(carried)) is True


def test_o90_the_instruments_full_chain_call_answers_another_attempts_open_injection_as_unknown(tmp_path, fake):
    """O90: through the full chain the fault gate answers another attempt's open injection first: the snapshot says the
    check failed, the reading is unread, and the record's `target_only` is None, where the space check alone
    (`space()`, which skips the gate) would have read the spaces and could have said True or False."""

    def another(journal, _):
        injection(journal, "other", OTHER, ANOTHER, closed=False)

    world = four_spaces(tmp_path, fake, faults={})
    snapshot, reading, _ = inside_injection(tmp_path, world, extra=another, instrument=True)
    assert snapshot["ok"] is False
    assert reading == UNREAD
    assert target_only(reading) is None


def test_o90_an_instrument_whose_check_raises_leaves_no_reading_and_the_record_says_unknown():
    """O90: `finish_instruments` records a failed finish as `{"ok": False, "error": ...}`, never dropped or guessed: the
    record has no reading, and `target_only` of what is read from it is None."""

    def broken():
        raise OSError("the check could not run")

    record = finish_instruments(Record(), [PostRunHealth(broken)])
    assert record.instruments[NAMESPACE] == {"ok": False, "error": "OSError"}
    assert reading_in_instruments(record.instruments) is None
    assert target_only(reading_in_instruments(record.instruments)) is None


@pytest.mark.parametrize(
    "carried", [None, {}, {NAMESPACE: None}, {NAMESPACE: {"ok": True}}, {NAMESPACE: {"reading": 1}}]
)
def test_o90_a_record_with_no_whole_reading_reads_as_unknown(carried):
    """O90: absent or malformed in the record, the reading is None, never True."""
    assert reading_in_instruments(carried) is None
