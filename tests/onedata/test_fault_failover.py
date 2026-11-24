"""O98: before it journals or stops anything the injector checks that a mirror is asked before the owner it is about to
take down (`FaultInjector._require_failover_order`, `expcore_onedata.failover`), and changes nothing for a target that
is no owner.

The platform has two providers, `alpha` (providers[0], local and the owner of the one space) and `beta` (providers[1],
its mirror); their ids are the test's own. Every test runs against `FakeServices`; nothing contacts a deployment."""

import gc
import json
import threading

import pytest
from fault_support import LOCAL_TARGET, OUTAGE_TARGET, SERVICES, FakeServices, faults_table, injector_for

from expcore.errors import InfraError
from expcore_onedata import failover
from expcore_onedata.admission import FederationLock
from expcore_onedata.config import load_platform
from expcore_onedata.fault_journal import JOURNAL
from expcore_onedata.faults import INJECTOR_FAILURE, load_faults
from expcore_onedata.testing import write_platform
from expcore_onedata.testing.world import with_placement, with_spaces

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1, "factors": {}}

OWNER_FIRST = ("id-0100", "id-0200")  # alpha, beta: the owner sorts below its mirror
MIRROR_FIRST = ("id-0300", "id-0200")  # the mirror sorts below the owner


def platform_of(tmp_path, ids, *, owns=True):
    """A platform with `[faults]`, the two providers' ids from `ids`, and one space that `alpha` holds (`owns`), or that
    `beta` holds, the same two supporting it."""

    def edit(data):
        data["faults"] = faults_table()
        data["providers"]["alpha"]["id"], data["providers"]["beta"]["id"] = ids
        with_spaces(data, 1)
        data["spaces"][0]["holder"] = "alpha" if owns else "beta"

    return write_platform(tmp_path, edit)


def journal_events(tmp_path):
    """The events of the injection journal in order, empty when none was written."""
    path = tmp_path / "fault-journal" / JOURNAL
    return (
        [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    )


def stops_and_starts(services):
    return [call for call in services.calls if call[0] in ("stop", "start")]


def test_o98_an_owner_stop_whose_mirror_sorts_after_it_is_refused_with_nothing_touched(tmp_path):
    """O98: the owner's id below its mirror's, so op-worker would ask the stopped owner first: the start fails as an
    injector failure naming the target by label, and nothing was stopped, journalled or locked."""
    services = FakeServices()
    injector = injector_for(platform_of(tmp_path, OWNER_FIRST), services, target=LOCAL_TARGET)
    with pytest.raises(InfraError) as refused:
        injector.start(RUN)
    assert (refused.value.kind, refused.value.target) == (INJECTOR_FAILURE, LOCAL_TARGET)
    detail = refused.value.detail
    assert (
        "the owner's provider id sorts before every mirror's, so op-worker would ask the stopped owner first" in detail
    )
    assert detail.endswith("nothing is taken down")
    assert "spaces[0]" in detail
    assert "owner providers[0]" in detail
    assert "mirrors providers[1]" in detail
    for hidden in ("alpha", "beta", "id-0100", "id-0200", "store-one"):
        assert hidden not in detail
    assert stops_and_starts(services) == []
    assert services.running(SERVICES["alpha"]) is True
    assert journal_events(tmp_path) == []  # not even an `injecting` line: nothing was locked or journalled


def test_o98_the_refusal_lets_the_federation_go_so_another_target_can_start_at_once(tmp_path):
    """O98: the refused start holds no lock and no admission: a start for the mirror, no owner, goes through."""
    platform = platform_of(tmp_path, OWNER_FIRST)
    with pytest.raises(InfraError):
        injector_for(platform, FakeServices(), target=LOCAL_TARGET).start(RUN)
    services = FakeServices()
    other = injector_for(platform, services, target=OUTAGE_TARGET)
    other.start(RUN)
    assert services.running(SERVICES["beta"]) is False
    other.finish()


def test_o98_an_owner_stop_whose_mirror_sorts_before_it_goes_ahead_as_it_always_did(tmp_path):
    """O98: with a mirror asked first the start takes the owner down: the same journal lines and the same steps as a
    stop with no check, and the check adds no step to the snapshot."""
    services = FakeServices()
    injector = injector_for(platform_of(tmp_path, MIRROR_FIRST), services, target=LOCAL_TARGET)
    injector.start(RUN)
    assert services.running(SERVICES["alpha"]) is False
    assert journal_events(tmp_path) == ["injecting", "stopping", "injected"]
    snapshot = injector.finish()
    assert snapshot["ok"] is True
    assert "failover" not in json.dumps(snapshot)


def test_o98_a_target_that_owns_no_space_with_a_mirror_is_unchanged_whatever_the_ids(tmp_path):
    """O98: the mirror taken down is no owner stop: the order is not read, so the worst ids refuse nothing."""
    services = FakeServices()
    injector = injector_for(platform_of(tmp_path, OWNER_FIRST), services, target=OUTAGE_TARGET)
    injector.start(RUN)
    assert services.running(SERVICES["beta"]) is False
    assert journal_events(tmp_path) == ["injecting", "stopping", "injected"]
    injector.finish()


def test_o98_a_platform_that_names_no_holder_has_no_owner_stop_and_is_unchanged(tmp_path):
    """O98: the default one-space platform names no data provider, so nothing is in scope for any target."""

    def edit(data):
        data["faults"] = faults_table()
        data["support"] = {"providers": ["alpha", "beta"], "local": "alpha"}
        data["providers"]["alpha"]["id"], data["providers"]["beta"]["id"] = OWNER_FIRST

    services = FakeServices()
    injector = injector_for(write_platform(tmp_path, edit), services, target=LOCAL_TARGET)
    injector.start(RUN)
    assert services.running(SERVICES["alpha"]) is False
    injector.finish()


def test_o98_which_provider_holds_the_space_decides_which_stop_is_an_owner_stop(tmp_path):
    """O98: with `beta` the holder, the stop of `alpha`, a mirror, is unchanged even though `alpha` sorts first, and the
    stop of `beta` is the one that is refused when it sorts below `alpha`."""
    mirror_sorts_first = platform_of(tmp_path, ("id-a-0100", "id-b-0200"), owns=False)
    services = FakeServices()
    injector_for(mirror_sorts_first, services, target=LOCAL_TARGET).start(RUN)
    assert services.running(SERVICES["alpha"]) is False
    owner_sorts_first = tmp_path / "second"
    owner_sorts_first.mkdir()
    refused = FakeServices()
    with pytest.raises(InfraError, match="owner providers\\[1\\]"):
        injector_for(
            platform_of(owner_sorts_first, ("id-b-0200", "id-a-0100"), owns=False), refused, target=OUTAGE_TARGET
        ).start(RUN)
    assert stops_and_starts(refused) == []


# -- the refusal comes before anything is taken, recovered or journalled -----------------------------------------------

OTHER_RUN = {"run_key": "task=task-b|model=m1|seed=1", "attempt": 1}
FAILOVER_SENTENCE = (
    "the owner's provider id sorts before every mirror's, so op-worker would ask the stopped owner first"
)


def journal_bytes(tmp_path):
    path = tmp_path / "fault-journal" / JOURNAL
    return path.read_bytes() if path.exists() else b""


def test_o98_a_refused_owner_stop_recovers_nothing_an_earlier_run_left_and_writes_no_line(tmp_path):
    """O98: a crashed run left the mirror down and journalled. The owner stop that follows is refused for its order: it
    must not restore that mirror or write a recovery line first, since nothing is locked, recovered, journalled or
    stopped for a stop that is known to be invalid."""
    platform = platform_of(tmp_path, OWNER_FIRST)
    services = FakeServices()
    crashed = injector_for(platform, services, target=OUTAGE_TARGET)
    crashed.start(RUN)
    del crashed  # dropped: its locks go, the open injection stays
    gc.collect()
    left = journal_bytes(tmp_path)
    assert left
    assert services.running(SERVICES["beta"]) is False
    calls = list(services.calls)
    with pytest.raises(InfraError) as refused:
        injector_for(platform, services, target=LOCAL_TARGET).start(OTHER_RUN)
    assert (refused.value.kind, refused.value.target) == (INJECTOR_FAILURE, LOCAL_TARGET)
    assert FAILOVER_SENTENCE in refused.value.detail
    assert services.calls == calls  # no start of the mirror, no observation, no stop
    assert services.running(SERVICES["beta"]) is False
    assert journal_bytes(tmp_path) == left


def test_o98_a_refused_owner_stop_does_not_wait_for_a_federation_another_party_holds(tmp_path):
    """O98: with the federation held exclusively elsewhere, an owner stop that would be refused is `injector_failure`
    at once, not a wait for the admission that runs out as `federation_busy`: the order is read before the lock is."""
    platform = platform_of(tmp_path, OWNER_FIRST)
    config = load_platform(platform, require_secrets=False)
    lock = FederationLock(load_faults(platform, config))
    injector = injector_for(platform, FakeServices(), target=LOCAL_TARGET)
    held, release = threading.Event(), threading.Event()

    def hold_the_federation():  # another run's thread: this thread's own hold would be taken for the injector's run's
        with lock.held(exclusive=True):
            held.set()
            release.wait(30)

    other = threading.Thread(target=hold_the_federation)
    other.start()
    try:
        assert held.wait(30)
        with pytest.raises(InfraError) as refused:
            injector.start(RUN)
    finally:
        release.set()
        other.join(30)
    assert refused.value.kind == INJECTOR_FAILURE
    assert FAILOVER_SENTENCE in refused.value.detail


def test_o98_a_prepared_block_refuses_an_owner_stop_before_it_admits_anything(tmp_path):
    """O98: `prepared`, the two-halved start, refuses on entry, as `start` does: its body never runs, and the journal
    and the services are as they were."""
    services = FakeServices()
    injector = injector_for(platform_of(tmp_path, OWNER_FIRST), services, target=LOCAL_TARGET)
    with pytest.raises(InfraError, match="nothing is taken down"), injector.prepared(RUN):
        raise AssertionError("the body of a refused block ran")
    assert stops_and_starts(services) == []
    assert journal_bytes(tmp_path) == b""


# -- the holder is read once, with the ids ----------------------------------------------------------------------------


def one_space_platform(tmp_path, ids, holder):
    """The one-space form: both providers support the space, and `[placement]` names `holder` its data provider."""

    def edit(data):
        data["faults"] = faults_table()
        data["support"] = {"providers": ["alpha", "beta"], "local": "alpha"}
        data["providers"]["alpha"]["id"], data["providers"]["beta"]["id"] = ids
        with_placement(data, holder=holder)

    return write_platform(tmp_path, edit)


def test_o98_a_mirror_stop_reads_nothing_of_the_platform_file_again(tmp_path):
    """O98: the one-space form's holder came from reopening platform.toml, so a file that became unreadable after the
    injector was built failed even the stop of a mirror. The holder is part of what was loaded with the ids: the mirror
    goes down as it always did."""
    platform = one_space_platform(tmp_path, OWNER_FIRST, "alpha")
    services = FakeServices()
    injector = injector_for(platform, services, target=OUTAGE_TARGET)
    platform.unlink()
    injector.start(RUN)
    assert services.running(SERVICES["beta"]) is False
    assert journal_events(tmp_path) == ["injecting", "stopping", "injected"]


def test_o98_an_edit_of_the_holder_after_the_injector_was_built_does_not_pair_it_with_the_earlier_ids(tmp_path):
    """O98: the injector holds the ids it was built with; a placement holder edited since is not read, so an owner stop
    that was refused by the ids and holder it was built with stays refused (the edit makes beta the holder, which would
    make the stop of alpha, a mirror then, no owner stop at all)."""
    platform = one_space_platform(tmp_path, OWNER_FIRST, "alpha")
    services = FakeServices()
    injector = injector_for(platform, services, target=LOCAL_TARGET)
    platform.write_text(platform.read_text(encoding="utf-8").replace('holder = "alpha"', 'holder = "beta"'), "utf-8")
    with pytest.raises(InfraError) as refused:
        injector.start(RUN)
    assert FAILOVER_SENTENCE in refused.value.detail
    assert stops_and_starts(services) == []


# -- a root's record names the mirrors (the second code review) --------------------------------------------------------

LOCAL_FIRST = ("id-0200", "id-0300", "id-0100")  # alpha (the owner), beta (the declared mirror), gamma (the local one)
MIRROR_BELOW = ("id-0200", "id-0100", "id-0300")  # the declared mirror sorts below the owner


def three_provider_platform(tmp_path, ids):
    """A platform whose one space `alpha` holds is supported by `beta`, the declared mirror, and by `gamma`, the agent's
    local provider, which is no mirror and holds none of its blocks; `ids` are alpha's, beta's and gamma's."""

    def edit(data):
        data["faults"] = faults_table()
        data["providers"]["gamma"] = {"url": "https://gamma.test", "id": ids[2]}
        data["providers"]["alpha"]["id"], data["providers"]["beta"]["id"] = ids[:2]
        with_spaces(data, 1)
        data["spaces"][0].update({"providers": ["alpha", "beta", "gamma"], "local": "gamma", "holder": "alpha"})

    return write_platform(tmp_path, edit)


def write_record(tmp_path, text):
    (tmp_path / failover.FILE).write_text(text, encoding="utf-8")


# the record `up` wrote: the owner providers[0], the one declared mirror providers[1], the mirror first
DECLARED_BETA = failover.record_text(
    [failover.SpaceOrder("spaces[0]", "providers[0]", ("providers[1]",), ("providers[1]", "providers[0]"))]
)


def test_o98_with_no_record_a_local_provider_that_holds_nothing_still_counts_as_a_mirror_as_before(tmp_path):
    """O98: a Compose federation writes no record: every supporter counts, so the local provider sorting first lets the
    owner stop go ahead exactly as it did. The stop of the owner is journalled as always."""
    services = FakeServices()
    injector = injector_for(three_provider_platform(tmp_path, LOCAL_FIRST), services, target=LOCAL_TARGET)
    injector.start(RUN)
    assert services.running(SERVICES["alpha"]) is False
    assert journal_events(tmp_path) == ["injecting", "stopping", "injected"]
    injector.finish()


def test_o98_with_a_record_the_owner_stop_is_checked_over_its_declared_mirrors_and_refused_with_nothing_touched(
    tmp_path,
):
    """O98, the second code review: the same platform and ids, with the record `up` wrote naming beta the one mirror:
    the owner sorts below it, the local provider is ignored, and the start fails as an injector failure before anything
    is stopped, locked or journalled, naming labels only."""
    write_record(tmp_path, DECLARED_BETA)
    services = FakeServices()
    injector = injector_for(three_provider_platform(tmp_path, LOCAL_FIRST), services, target=LOCAL_TARGET)
    with pytest.raises(InfraError) as refused:
        injector.start(RUN)
    assert (refused.value.kind, refused.value.target) == (INJECTOR_FAILURE, LOCAL_TARGET)
    detail = refused.value.detail
    assert FAILOVER_SENTENCE in detail
    assert "owner providers[0]; mirrors providers[1])" in detail  # not providers[2], the local provider
    assert detail.endswith("nothing is taken down")
    for hidden in ("alpha", "beta", "gamma", "id-0"):
        assert hidden not in detail
    assert stops_and_starts(services) == []
    assert journal_events(tmp_path) == []


def test_o98_with_a_record_a_declared_mirror_that_sorts_below_the_owner_goes_ahead(tmp_path):
    """O98: the record's mirror below the owner in the platform's current ids: the stop is admitted as it always was."""
    write_record(tmp_path, DECLARED_BETA)
    services = FakeServices()
    injector = injector_for(three_provider_platform(tmp_path, MIRROR_BELOW), services, target=LOCAL_TARGET)
    injector.start(RUN)
    assert services.running(SERVICES["alpha"]) is False
    assert journal_events(tmp_path) == ["injecting", "stopping", "injected"]
    injector.finish()


@pytest.mark.parametrize(
    "record",
    [
        "not a record",
        DECLARED_BETA.replace("providers[1]", "providers[9]"),  # a provider the platform file does not have
        DECLARED_BETA.replace("spaces[0]", "spaces[4]"),  # a space it does not have
        failover.record_text(  # well formed, and another owner than the platform's holder of the space
            [failover.SpaceOrder("spaces[0]", "providers[1]", ("providers[0]",), ("providers[0]", "providers[1]"))]
        ),
    ],
    ids=["no record", "a provider it lacks", "a space it lacks", "another owner"],
)
def test_o98_a_record_that_is_unreadable_or_does_not_resolve_refuses_the_owner_stop_with_nothing_touched(
    tmp_path, record
):
    """O98: even with the ids that would pass (the mirror below the owner), a record the injector cannot read strictly,
    or whose labels do not resolve in the platform file, is no ground to stop the owner: the start fails closed."""
    write_record(tmp_path, record)
    services = FakeServices()
    injector = injector_for(three_provider_platform(tmp_path, MIRROR_BELOW), services, target=LOCAL_TARGET)
    with pytest.raises(InfraError) as refused:
        injector.start(RUN)
    assert refused.value.kind == INJECTOR_FAILURE
    assert refused.value.detail.startswith("failover order: ")
    assert refused.value.detail.endswith("nothing is taken down")
    assert stops_and_starts(services) == []
    assert journal_events(tmp_path) == []


def test_o98_a_stop_of_a_provider_that_owns_nothing_reads_no_record_even_when_the_record_is_garbage(tmp_path):
    """O98: the mirror taken down is no owner stop, so the record is not read: a damaged one changes nothing for it."""
    write_record(tmp_path, "garbage")
    services = FakeServices()
    injector = injector_for(three_provider_platform(tmp_path, LOCAL_FIRST), services, target=OUTAGE_TARGET)
    injector.start(RUN)
    assert services.running(SERVICES["beta"]) is False
    injector.finish()
