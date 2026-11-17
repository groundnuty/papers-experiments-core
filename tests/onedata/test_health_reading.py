"""O90: the check's structured reading, and `target_only`, which a study asks whether an outage was the run's own.

The reading is per space (the declared providers Onezone reports offline, by layout label, and whether the space was
read completely) and per run (the targets its own attempt's injections took down, by label). It sits beside the text
detail, which `test_health_text.py` pins; a reading that cannot say never says yes.
"""

import dataclasses
import json
import threading

import pytest
from expcore_onedata.healthreading import (
    READING_NOTE,
    UNREAD,
    HealthReading,
    SpaceReading,
    noted_reading,
    own_targets,
    read_space,
    target_only,
)
from fault_support import FakeServices, injector_for
from health_support import FOURTH, down, four_spaces, up

from expcore.campaign import liveness, prerun
from expcore_onedata.config import load_platform
from expcore_onedata.fault_journal import InjectionJournal
from expcore_onedata.faults import load_faults
from expcore_onedata.federation import ReportedProvider, SpaceView
from expcore_onedata.health import compare
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
    tmp_path, online, name, read, offline
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


def test_o90_a_provider_whose_online_flag_is_not_a_boolean_leaves_the_space_unread(tmp_path):
    """O90: an online flag that is neither true nor false is no reading; `compare()` counts only `False` as offline."""
    config = load_platform(write_platform(tmp_path))
    view = SpaceView("spmock0001", config.space.name, (ReportedProvider(ALPHA_ID, "a", "alpha.test", None),))
    assert read_space(view, config, config.space_named()).read is False


def test_o90_a_host_only_declaration_that_two_reported_providers_fit_is_unread(tmp_path):
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
