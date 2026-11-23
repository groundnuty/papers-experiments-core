"""O98: before it journals or stops anything the injector checks that a mirror is asked before the owner it is about to
take down (`FaultInjector._require_failover_order`, `expcore_onedata.failover`), and changes nothing for a target that
is no owner.

The platform has two providers, `alpha` (providers[0], local and the owner of the one space) and `beta` (providers[1],
its mirror); their ids are the test's own. Every test runs against `FakeServices`; nothing contacts a deployment."""

import json

import pytest
from fault_support import LOCAL_TARGET, OUTAGE_TARGET, SERVICES, FakeServices, faults_table, injector_for

from expcore.errors import InfraError
from expcore_onedata.fault_journal import JOURNAL
from expcore_onedata.faults import INJECTOR_FAILURE
from expcore_onedata.testing import write_platform
from expcore_onedata.testing.world import with_spaces

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
