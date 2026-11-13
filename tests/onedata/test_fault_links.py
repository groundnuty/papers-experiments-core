"""O35: a restored provider is not restored while a link of it transfers nothing. Once it serves, the recovery asks the
driver's link check for every pair the provider is in, and a pair that does not pass fails the restore (`links_stuck`):
the injection stays open and the federation held, as for a provider that never served, until a later clear finds every
link transferring."""

from fault_support import OUTAGE_TARGET, SERVICES, FakeClock, FakeServices, injector_for, write_faults

from expcore_onedata.compose import ComposeDriver

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
    services = LinkedServices()

    def broken(service):
        raise RuntimeError("the check could not run")

    services.restored_links = broken
    injector = injector_for(write_faults(tmp_path), services, clock=FakeClock())
    injector.start(RUN)
    assert injector.clear() is False
    assert step(injector, "cleared")["how"] == "links_stuck"


def test_o35_the_compose_driver_asks_its_probes_link_check_and_none_without_one(tmp_path):
    class Probe:
        def __call__(self, service):
            return True

        def links(self, service):
            return f"stuck for {service}"

    with_links = ComposeDriver(tmp_path / "compose.yaml", probe=Probe())
    assert with_links.restored_links("svc") == "stuck for svc"
    without = ComposeDriver(tmp_path / "compose.yaml", probe=lambda service: True)
    assert without.restored_links("svc") is None
