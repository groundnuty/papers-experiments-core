"""O98: before it journals or stops anything the injector checks that a failover holder is asked before the owner it is
about to take down (`FaultInjector._require_failover_order`, `expcore_onedata.failover`), and changes nothing for a
target that is no owner, or for a platform that declares no failover holders.

The platform has two providers, `alpha` (providers[0], local and the owner of the one space) and `beta` (providers[1],
the holder its `failover` declares); their ids are the test's own. Every test runs against `FakeServices`; nothing
contacts a deployment. The declaration is in the platform file, which the injector's configuration was loaded from:
nothing of the federation's root is read."""

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

OWNER_FIRST = ("id-0100", "id-0200")  # alpha, beta: the owner sorts below its failover holder
MIRROR_FIRST = ("id-0300", "id-0200")  # the holder sorts below the owner


def platform_of(tmp_path, ids, *, declare=True):
    """A platform with `[faults]`, the two providers' ids from `ids`, and one space that `alpha` holds, `beta` and
    `alpha` supporting it, declaring `beta` its failover holder (`declare`; false: it declares none)."""

    def edit(data):
        data["faults"] = faults_table()
        data["providers"]["alpha"]["id"], data["providers"]["beta"]["id"] = ids
        with_spaces(data, 1)
        data["spaces"][0]["holder"] = "alpha"
        if declare:
            data["spaces"][0]["failover"] = ["beta"]

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
    """O98: with `beta` the holder (declaring `gamma`, with `alpha` local), the stop of `alpha`, which holds nothing, is
    unchanged even though `alpha` sorts first, and the stop of `beta` is the one that is refused when it sorts below
    `gamma`."""
    holds_beta = ({"holder": "beta", "failover": ["gamma"], "local": "alpha"},)
    first = three_provider_platform(tmp_path, ("id-a-0100", "id-b-0200", "id-g-0300"), holds_beta)
    services = FakeServices()
    injector_for(first, services, target=LOCAL_TARGET).start(RUN)
    assert services.running(SERVICES["alpha"]) is False
    second = tmp_path / "second"
    second.mkdir()
    refused = FakeServices()
    with pytest.raises(InfraError, match="owner providers\\[1\\]; mirrors providers\\[2\\]"):
        injector_for(
            three_provider_platform(second, ("id-a-0300", "id-b-0100", "id-g-0200"), holds_beta),
            refused,
            target=OUTAGE_TARGET,
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


def test_o98_an_edit_of_the_platform_file_after_the_injector_was_built_does_not_change_what_it_checks(tmp_path):
    """O98: the injector holds the ids, holders and declarations it was built with; the file is read nowhere again. An
    owner stop that was refused stays refused after the file is edited to declare no failover holder (a fresh read would
    find nothing in scope and let it go ahead), and after it is removed."""
    platform = platform_of(tmp_path, OWNER_FIRST)
    services = FakeServices()
    injector = injector_for(platform, services, target=LOCAL_TARGET)
    platform.write_text(platform.read_text(encoding="utf-8").replace('failover = ["beta"]\n', ""), "utf-8")
    with pytest.raises(InfraError) as refused:
        injector.start(RUN)
    assert FAILOVER_SENTENCE in refused.value.detail
    assert stops_and_starts(services) == []
    platform.unlink()
    with pytest.raises(InfraError) as again:
        injector.start(RUN)
    assert FAILOVER_SENTENCE in again.value.detail


# -- the declared holders, and not the supporters, are what is ordered (the review of #548) ----------------------------

LOCAL_FIRST = ("id-0200", "id-0300", "id-0100")  # alpha (the owner), beta (the declared holder), gamma (the local one)
MIRROR_BELOW = ("id-0200", "id-0100", "id-0300")  # the declared holder sorts below the owner


def three_provider_platform(tmp_path, ids, entries=({"holder": "alpha", "failover": ["beta"]},)):
    """A platform whose spaces `entries` update: each is supported by `alpha`, `beta` and `gamma`, the agent's local
    provider, which holds none of its blocks, and by default held by `alpha` with `beta` declared its failover holder;
    `ids` are alpha's, beta's and gamma's."""

    def edit(data):
        data["faults"] = faults_table()
        data["providers"]["gamma"] = {"url": "https://gamma.test", "id": ids[2]}
        data["providers"]["alpha"]["id"], data["providers"]["beta"]["id"] = ids[:2]
        with_spaces(data, len(entries))
        for entry, own in zip(data["spaces"], entries, strict=True):
            entry.update({"providers": ["alpha", "beta", "gamma"], "local": "gamma", **own})

    return write_platform(tmp_path, edit)


def test_o98_a_local_provider_that_sorts_first_does_not_let_an_owner_stop_pass_and_nothing_is_touched(tmp_path):
    """O98, the review of #548: the local provider supports the space and holds none of its blocks, and sorts below the
    owner, which sorts below the declared holder: the start fails as an injector failure before anything is stopped,
    locked or journalled, naming labels only, and not the local provider. Counting every supporter let it go ahead."""
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


def test_o98_a_declared_holder_that_sorts_below_the_owner_goes_ahead_whatever_the_local_provider_does(tmp_path):
    """O98: the declared holder below the owner in the platform's ids: the stop is admitted as it always was."""
    services = FakeServices()
    injector = injector_for(three_provider_platform(tmp_path, MIRROR_BELOW), services, target=LOCAL_TARGET)
    injector.start(RUN)
    assert services.running(SERVICES["alpha"]) is False
    assert journal_events(tmp_path) == ["injecting", "stopping", "injected"]
    injector.finish()


def test_o98_a_platform_that_declares_no_failover_holder_is_not_checked_and_the_owner_stop_goes_ahead(tmp_path):
    """O98: the ruling: a space that declares none is out of scope, so an owner stop on a platform that declares none
    anywhere is not checked, whatever the ids (the owner below its supporter, or the local provider first): the
    behaviour before this check existed."""
    (tmp_path / "two").mkdir()
    (tmp_path / "three").mkdir()
    plains = {
        tmp_path / "two": platform_of(tmp_path / "two", OWNER_FIRST, declare=False),
        tmp_path / "three": three_provider_platform(tmp_path / "three", LOCAL_FIRST, ({"holder": "alpha"},)),
    }
    for folder, plain in plains.items():
        services = FakeServices()
        injector = injector_for(plain, services, target=LOCAL_TARGET)
        injector.start(RUN)
        assert services.running(SERVICES["alpha"]) is False
        assert journal_events(folder) == ["injecting", "stopping", "injected"]
        injector.finish()


UNDECLARED_BESIDE_DECLARED = (
    {"holder": "alpha", "failover": ["beta"]},
    {"holder": "alpha"},  # the owner holds it and others support it, and it declares no failover holder
)


def test_o98_where_a_space_declares_holders_a_supported_space_of_the_owner_that_declares_none_refuses_the_stop(
    tmp_path,
):
    """O98: spaces[0] declares its holder, which sorts below the owner, so it passes alone. spaces[1], which the owner
    holds as well and the others support, declares none: the start fails as an injector failure naming the space by
    position, before anything is stopped, locked or journalled."""
    services = FakeServices()
    platform = three_provider_platform(tmp_path, MIRROR_BELOW, UNDECLARED_BESIDE_DECLARED)
    injector = injector_for(platform, services, target=LOCAL_TARGET)
    with pytest.raises(InfraError) as refused:
        injector.start(RUN)
    assert (refused.value.kind, refused.value.target) == (INJECTOR_FAILURE, LOCAL_TARGET)
    detail = refused.value.detail
    assert "spaces[1]: declares no failover holder" in detail
    assert detail.endswith("nothing is taken down")
    for hidden in ("alpha", "beta", "gamma", "id-0", "spaces[0]"):
        assert hidden not in detail
    assert stops_and_starts(services) == []
    assert journal_events(tmp_path) == []


def test_o98_a_space_another_provider_holds_needs_no_declaration_for_the_stop_of_this_owner(tmp_path):
    """O98: spaces[1] is held by beta, not by the owner alpha the stop takes down, so it need not declare a holder."""
    other_holds = ({"holder": "alpha", "failover": ["beta"]}, {"holder": "beta"})
    services = FakeServices()
    injector = injector_for(three_provider_platform(tmp_path, MIRROR_BELOW, other_holds), services, target=LOCAL_TARGET)
    injector.start(RUN)
    assert services.running(SERVICES["alpha"]) is False
    assert journal_events(tmp_path) == ["injecting", "stopping", "injected"]
    injector.finish()


def test_o98_a_stop_of_a_provider_that_owns_nothing_is_unchanged_beside_an_undeclared_space(tmp_path):
    """O98: the stop of beta, which holds no space, is no owner stop: another space of the platform that declares none
    changes nothing for it."""
    services = FakeServices()
    platform = three_provider_platform(tmp_path, LOCAL_FIRST, UNDECLARED_BESIDE_DECLARED)
    injector = injector_for(platform, services, target=OUTAGE_TARGET)
    injector.start(RUN)
    assert services.running(SERVICES["beta"]) is False
    injector.finish()


RECORD_SAYING_IT_HOLDS = failover.record_text(
    [failover.SpaceOrder("spaces[0]", "providers[0]", ("providers[1]",), ("providers[1]", "providers[0]"))]
)


@pytest.mark.parametrize("record", ["garbage", RECORD_SAYING_IT_HOLDS], ids=["garbage", "a record that says it holds"])
def test_o98_the_federations_record_is_not_read_the_platform_file_decides(tmp_path, record):
    """O98: the record `up` leaves in an HPC federation's root is evidence and no authority any more: in the root of
    the platform file a damaged one, or one that claims the order holds, neither refuses a stop the platform file lets
    through nor lets one through that the platform file refuses."""
    for name in ("passing", "refusing"):
        (tmp_path / name).mkdir()
        (tmp_path / name / failover.FILE).write_text(record, encoding="utf-8")
    services = FakeServices()
    injector = injector_for(three_provider_platform(tmp_path / "passing", MIRROR_BELOW), services, target=LOCAL_TARGET)
    injector.start(RUN)
    assert services.running(SERVICES["alpha"]) is False
    injector.finish()
    refusing = three_provider_platform(tmp_path / "refusing", LOCAL_FIRST)
    with pytest.raises(InfraError) as refused:
        injector_for(refusing, FakeServices(), target=LOCAL_TARGET).start(RUN)
    assert FAILOVER_SENTENCE in refused.value.detail
