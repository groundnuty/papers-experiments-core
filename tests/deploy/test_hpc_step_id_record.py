"""A service's step id is recorded when `up` launches it, even when its log says it is ready before squeue lists the
step (O86, seen live): `wait_log` recorded the id only on a poll where the log was NOT yet ready, so a step that
a busy login node listed late kept `id: null` for its whole launch, and the window reading of that service, once
stopped, found no id and no squeue entry and read "unknown" instead of the stopped step's state. `wait_log` now waits,
within `timeouts["listed"]`, for the listing of a step whose log is ready, records the id as the loop always did, and
returns the log either way: a service that says it is ready is never failed for a listing that did not come. Over the
fake Slurm of hpc_fed_support: every node name and step id here is invented."""

import re

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, hpc_on_path

READY = "hpcop: OK zone"  # what a zone's log says once it is ready
NOT_RECORDED = "its step id is not recorded"


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture
def site(tmp_path):
    images, logs = tmp_path / "images", tmp_path / "logs"
    images.mkdir()
    logs.mkdir()
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    (tmp_path / "fed").mkdir()
    return {**SITE, **agent_site(tmp_path / "agent"), "SH": str(images), "L": str(logs)}


class Rig:
    """A driver over the fake Slurm, the core's phase recorded instead of run; what it says and sleeps is kept."""

    def __init__(self, hpc, tmp_path, site):
        self.hpc, self.site, self.fake = hpc, site, FakeSlurm()
        self.driver = hpc.cli.make_driver(LAYOUT, str(tmp_path / "fed" / "root"), runner=self.fake)
        self.said, self.slept = [], []
        self.driver.say = self.said.append
        self.driver.sleep = self.slept.append
        self.driver.poll_s = 0.0
        self.driver.slurm.sleep = lambda s: None
        self.driver.slurm.poll_s = 0.0
        self.driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        self.driver.relay_available = lambda: False

    def up(self):
        return self.driver.up(self.hpc.settings.site_from_environment(self.site))

    def services(self):
        return self.driver.store.read()["services"]

    def step(self, service):
        return self.services()[service]["step"]

    def log(self, service):
        return self.services()[service]["log"]


def ticking(rig, poll_s=5.0):
    """The driver on a clock of its own that moves only when it sleeps, `poll_s` apart, so a bound is counted, not
    waited for; the pauses it took stay in `rig.slept`."""
    now = [0.0]
    rig.driver.poll_s = poll_s

    def sleep(seconds):
        rig.slept.append(seconds)
        now[0] += seconds

    rig.driver.clock, rig.driver.sleep = (lambda: now[0]), sleep


def squeue_lists_late(rig, polls):
    """squeue lists no service step of a launch (`-run-N`) for its first `polls` asks, by name, and then as the fake
    does. Returns what was asked: name -> how many times."""
    real, asked = rig.driver.launcher.listed, {}

    def listed(name):
        asked[name] = asked.get(name, 0) + 1
        return None if "-run-" in name and asked[name] <= polls else real(name)

    rig.driver.launcher.listed = listed
    return asked


def launched(rig):
    """A federation `up` has finished, its zone's step id cleared, so that one wait of it is read in isolation."""
    rig.up()
    rig.slept.clear()  # the stagger between launches is not the wait under test
    rig.driver.set(lambda s: s["services"]["zone"]["step"].update(id=None))
    assert rig.step("zone")["id"] is None
    return rig


@pytest.mark.parametrize("polls", [0, 3])
def test_o86_up_records_every_services_step_id_though_its_log_is_ready_before_squeue_lists_the_step(
    hpc, tmp_path, site, polls
):
    """O86: with the step listed at once (0) or only on the fourth ask (3), every service the federation launched has
    its step's id in the state, the id squeue lists it under, each asked for once the log was ready, and a provider's
    second wait (its verdict) does not wait for it again."""
    rig = Rig(hpc, tmp_path, site)
    asked = squeue_lists_late(rig, polls)
    rig.up()
    services = rig.services()
    assert len(services) > 2
    for service, record in services.items():
        if record["step"] is None:
            continue
        name, recorded = record["step"]["name"], record["step"]["id"]
        assert re.fullmatch(r"[0-9]+(\+[0-9]+)?\.[0-9]+", recorded or ""), f"{service}: the id is {recorded!r}"
        assert recorded == rig.fake.steps[name]
        assert asked[name] == polls + 1  # polls without it, one with it, and no wait again for a recorded id


def test_o86_wait_log_waits_for_squeue_to_list_a_step_whose_log_is_ready_and_records_the_id(hpc, tmp_path, site):
    """O86: the log is ready on the first poll and squeue lists nothing for the first three asks: the id is recorded
    once it is listed, the log is returned, and the pauses were three polls."""
    rig = launched(Rig(hpc, tmp_path, site))
    ticking(rig)
    asked = squeue_lists_late(rig, 3)
    text = rig.driver.wait_log("zone", READY)
    name = rig.step("zone")["name"]
    assert READY in text
    assert rig.step("zone")["id"] == rig.fake.steps[name]
    assert (asked[name], rig.slept) == (4, [5.0, 5.0, 5.0])
    assert NOT_RECORDED not in " ".join(rig.said)


def test_o86_a_step_squeue_never_lists_leaves_the_id_unrecorded_and_fails_nothing(hpc, tmp_path, site):
    """O86: squeue never lists the step within the bound: the log, which is ready, is returned with no exception, the id
    stays None, and that is said, naming the service and the bound and no node."""
    rig = launched(Rig(hpc, tmp_path, site))
    ticking(rig)
    rig.driver.timeouts["listed"] = 20.0
    asked = squeue_lists_late(rig, 10**6)
    text = rig.driver.wait_log("zone", READY)
    name = rig.step("zone")["name"]
    assert READY in text
    assert rig.step("zone")["id"] is None
    assert asked[name] == 5  # at 0, 5, 10, 15 and 20 s: the last is at the bound
    assert rig.slept == [5.0] * 4
    (note,) = [line for line in rig.said if NOT_RECORDED in line]
    assert note.startswith("zone: ")
    assert "within 20 s" in note
    assert not re.search(r"node[0-9]{2}", note)


def test_o86_a_squeue_read_that_fails_after_the_log_is_ready_is_waited_through_and_never_fails_the_start(
    hpc, tmp_path, site
):
    """O86: squeue timing out or failing (`SlurmError`) while the id is awaited is a poll without an answer: the id is
    recorded once a read answers, and a squeue that never does leaves the id None and names only the error's type."""
    SlurmError = hpc.slurm.SlurmError
    rig = launched(Rig(hpc, tmp_path, site))
    ticking(rig)
    real, reads = rig.driver.launcher.listed, []

    def flaky(name):
        reads.append(name)
        if len(reads) <= 2:
            raise SlurmError("squeue did not answer in 30 s: secret-node-text")
        return real(name)

    rig.driver.launcher.listed = flaky
    assert READY in rig.driver.wait_log("zone", READY)
    assert rig.step("zone")["id"] == rig.fake.steps[rig.step("zone")["name"]]
    assert len(reads) == 3

    rig.driver.set(lambda s: s["services"]["zone"]["step"].update(id=None))
    rig.driver.timeouts["listed"] = 10.0
    rig.driver.launcher.listed = lambda name: (_ for _ in ()).throw(SlurmError("secret-node-text"))
    assert READY in rig.driver.wait_log("zone", READY)
    assert rig.step("zone")["id"] is None
    (note,) = [line for line in rig.said if NOT_RECORDED in line]
    assert "SlurmError" in note
    assert "secret-node-text" not in note


def test_o86_an_id_an_earlier_wait_of_the_launch_recorded_is_not_waited_for_again(hpc, tmp_path, site):
    """O86: a provider's second wait_log (its verdict, after its token) finds the id recorded by the first and asks
    squeue nothing before it returns the log."""
    rig = launched(Rig(hpc, tmp_path, site))
    rig.driver.set(lambda s: s["services"]["zone"]["step"].update(id="4242.9"))

    def never(name):
        raise AssertionError("squeue was asked though the id was recorded")

    rig.driver.launcher.listed = never
    assert READY in rig.driver.wait_log("zone", READY)
    assert rig.step("zone")["id"] == "4242.9"


def test_o86_a_step_listed_before_its_log_is_ready_is_recorded_as_it_always_was(hpc, tmp_path, site):
    """O86: unchanged: while the log is not ready every poll asks squeue, the first listing records the id, and once
    the log is ready it is returned at once, with no further ask."""
    rig = launched(Rig(hpc, tmp_path, site))
    log = rig.log("zone")
    open(log, "w", encoding="utf-8").close()  # the service's log, empty again
    asked, ticks = [], []

    def sleep(seconds):
        ticks.append(seconds)
        if len(ticks) == 2:
            with open(log, "w", encoding="utf-8") as handle:
                handle.write(READY + "\n")

    rig.driver.sleep = sleep
    rig.driver.launcher.listed = lambda name: asked.append(name) or "4242.9"
    assert READY in rig.driver.wait_log("zone", READY)
    assert rig.step("zone")["id"] == "4242.9"
    assert len(asked) == 2  # the turns with an empty log; the ready one asks nothing
    assert len(ticks) == 2


def test_o86_a_failure_line_and_a_step_gone_before_the_log_is_ready_fail_as_they_always_did(hpc, tmp_path, site):
    """O86: unchanged: a failure line in the log fails the start (the service `failed`, squeue not asked); a step that
    was listed and is then gone for three polls is `StepGone`, its id kept; neither waits for a listing."""
    rig = launched(Rig(hpc, tmp_path, site))
    log = rig.log("zone")
    asked = []
    rig.driver.launcher.listed = lambda name: asked.append(name) or "4242.9"
    with open(log, "w", encoding="utf-8") as handle:
        handle.write("hpcop: ERROR: nothing came up\n")
    with pytest.raises(RuntimeError, match="zone failed to start"):
        rig.driver.wait_log("zone", READY)
    assert asked == []
    assert rig.services()["zone"]["state"] == "failed"

    open(log, "w", encoding="utf-8").close()
    answers = iter(["4242.9"])
    rig.driver.launcher.listed = lambda name: next(answers, None)
    with pytest.raises(hpc.driver.StepGone, match="zone's step ended before its log said"):
        rig.driver.wait_log("zone", READY)
    assert rig.step("zone")["id"] == "4242.9"
    assert rig.services()["zone"]["state"] == "failed"
