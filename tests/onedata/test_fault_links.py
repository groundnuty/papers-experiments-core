"""O35: a restored provider is not restored while a link of it transfers nothing. Once it serves, the recovery asks the
driver's link check for every pair the provider is in, and a pair that does not pass fails the restore (`links_stuck`):
the injection stays open and the federation held, as for a provider that never served, until a later clear finds every
link transferring."""

import uuid

import pytest
from fault_support import OUTAGE_TARGET, SERVICES, FakeClock, FakeServices, injector_for, write_faults

from expcore.errors import InfraError
from expcore_onedata.compose import ComposeDriver
from expcore_onedata.config import load_platform
from expcore_onedata.fault_recovery import Recovery, digest
from expcore_onedata.faults import load_faults
from expcore_onedata.linkcheck import REMEDY

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
STUCK = "no transfer from providers[1] to providers[0] within 30 s; remedy: restart the rtransfer link on providers[0]"


class LinkedServices(FakeServices):
    """Fake services whose driver also answers the link check of a restored service: `stuck` is its failure, or None."""

    def __init__(self, **knobs):
        super().__init__(**knobs)
        self.stuck: str | None = STUCK
        self.asked: list[str] = []

    def restored_links(self, service: str) -> str | None:
        self.asked.append(service)
        return self.stuck


def step(injector, name):
    return [s for s in injector.snapshot()["steps"] if s["step"] == name][-1]


def test_o35_a_restore_whose_links_do_not_transfer_fails_and_a_later_clear_verifies_it(tmp_path):
    """O35: A restore whose links do not transfer fails and a later clear verifies it."""
    services = LinkedServices()
    injector = injector_for(write_faults(tmp_path), services, clock=FakeClock())
    injector.start(RUN)
    assert injector.clear() is False  # it serves, and its link is stuck: not restored
    assert (step(injector, "cleared")["ok"], step(injector, "cleared")["how"]) == (False, "links_stuck")
    assert services.asked == [SERVICES["beta"]]
    assert injector.snapshot()["injections"][0]["target"] == OUTAGE_TARGET
    services.stuck = None  # the link restarted
    assert injector.clear() is True
    assert injector.snapshot()["ok"] is True
    assert step(injector, "cleared")["how"] == "found_restored"


def test_o35_a_link_check_that_raises_fails_the_restore_closed(tmp_path):
    """O35: A link check that raises fails the restore closed."""
    services = LinkedServices()

    def broken(service):
        raise RuntimeError("the check could not run")

    services.restored_links = broken
    injector = injector_for(write_faults(tmp_path), services, clock=FakeClock())
    injector.start(RUN)
    assert injector.clear() is False
    assert step(injector, "cleared")["how"] == "links_stuck"


def test_o35_the_compose_driver_asks_its_probes_link_check_and_none_without_one(tmp_path):
    """O35: The compose driver asks its probes link check and none without one."""

    class Probe:
        def __call__(self, service):
            return True

        def links(self, service):
            return f"stuck for {service}"

    with_links = ComposeDriver(tmp_path / "compose.yaml", probe=Probe())
    assert with_links.restored_links("svc") == "stuck for svc"
    without = ComposeDriver(tmp_path / "compose.yaml", probe=lambda service: True)
    assert without.restored_links("svc") is None


def test_o35_a_links_stuck_recovery_names_the_reader_of_the_failed_pair_not_the_local_provider(tmp_path):
    """O35: a run left beta's outage open; recovery restores it, and the link check of every pair finds stuck the link
    through which gamma reads alpha's files, a pair neither side of which is the restored provider and whose source is
    the platform's local provider. The failure says to restart the reading provider's link, names the reader by
    position, and never tells a study to restart the platform's local provider."""
    platform = write_faults(tmp_path)
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    services = LinkedServices()
    remedy = REMEDY.format(reader="providers[2]")
    services.stuck = (
        f"a provider link is stuck or untested: no transfer from providers[0] to providers[2] within 30 s; {remedy}"
    )
    clock = FakeClock()
    recovery = Recovery(settings, services, clock=clock, sleep=clock.sleep)
    service = SERVICES["beta"]
    recovery.journal.append(
        {
            "event": "injecting",
            "id": uuid.uuid4().hex,
            "target": OUTAGE_TARGET,
            "service": service,
            "identity_sha256": digest(services.identity(service)),
            "holder": {"pid": 999999, "boot_id": "x"},
        }
    )
    services.stop(service)  # the run died with the provider down
    with pytest.raises(InfraError) as caught:
        recovery.recover()
    assert (caught.value.target, services.asked) == (OUTAGE_TARGET, [service])
    head, _, why = caught.value.detail.partition("was not cleared (")
    assert OUTAGE_TARGET in head  # the injection the message is about: the restored provider
    assert "restart the rtransfer link on the reading provider each failed pair names" in why
    assert "providers[2]" in why  # the reader of the failed pair, from the link check's own detail
    assert "on the local provider" not in why
    assert OUTAGE_TARGET not in why  # the restored provider is not the one to restart
    (still_open,) = recovery.journal.open_entries()
    assert still_open["clear_failed"] == "links_stuck"
