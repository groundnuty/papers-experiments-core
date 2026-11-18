"""The HPC driver's wait for its held jobs to start (deploy/hpc-federation/hpcdriver.py, `bring_up`; README.md, "Two
federations of one account" and "Site configuration").

`up` submits every held job (the services', and under sandbox_placement "separate" the agent's own) and then waits for
each to run, at a bound the site may set (`HPC_QUEUE_WAIT_S`, seconds; 1800 when unset). A wait that fails releases
every held job of this up that has not ended, by its own id, and never one that is not its own.

Slurm is the fake of hpc_fed_support."""

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, hpc_on_path
from hpc_fleet_support import SEPARATE_LAYOUT

KEY = "HPC_QUEUE_WAIT_S"
SERVICES, AGENT = "4242", "4343"  # the fake's two job ids under "separate"


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
    """A driver over the fake Slurm whose clock moves 100 s a reading: a job that never starts reaches its bound."""

    def __init__(self, hpc, tmp_path, site, layout=LAYOUT, fake=None, environ=None):
        self.hpc, self.site, self.fake = hpc, site, fake or FakeSlurm()
        (tmp_path / "fed").mkdir(parents=True, exist_ok=True)
        self.driver = hpc.cli.make_driver(layout, str(tmp_path / "fed" / "root"), runner=self.fake, environ=environ)
        self.driver.say = lambda text: None
        self.driver.sleep = lambda s: None
        self.driver.poll_s = 0.0
        self.driver.slurm.sleep = lambda s: None
        self.driver.slurm.poll_s = 0.0
        ticks = iter(range(0, 10**7, 100))
        self.driver.slurm.clock = lambda: next(ticks)
        self.driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        self.driver.relay_available = lambda: False

    def up(self):
        return self.driver.up(self.hpc.settings.site_from_environment(self.site))


def separate(state, agent_state="RUNNING"):
    """A fake whose services job is `state` and whose agent's own job is `agent_state`."""
    return FakeSlurm(state=state, job_states={AGENT: agent_state})


# -- the site's key -------------------------------------------------------------------------------------------------


def test_o57_the_queue_bound_is_the_drivers_own_when_the_site_sets_none(hpc, tmp_path):
    """O57: with HPC_QUEUE_WAIT_S unset (or empty) the held job's start bound is the 1800 s it always was, and one
    driver's setting never changes the next one's default."""
    for index, environ in enumerate(({}, {KEY: ""})):
        rig = Rig(hpc, tmp_path / str(index), {}, environ=environ)
        assert rig.driver.timeouts["queue"] == 1800.0
    set_driver = Rig(hpc, tmp_path / "set", {}, environ={KEY: "21600"}).driver
    assert set_driver.timeouts["queue"] == 21600.0
    assert hpc.driver.TIMEOUTS["queue"] == 1800.0
    assert Rig(hpc, tmp_path / "after", {}, environ={}).driver.timeouts["queue"] == 1800.0


def test_o57_the_site_key_is_the_bound_the_wait_gets_and_the_one_the_failure_says(hpc, tmp_path, site):
    """O57: HPC_QUEUE_WAIT_S=21600 is the bound `wait_running` is given, and a job that never starts is cancelled at
    that bound and not at 1800 s, saying so."""
    late = Rig(hpc, tmp_path / "late", site, environ={KEY: "21600"}, fake=FakeSlurm(state="PENDING"))
    seen = []
    wait = late.driver.slurm.wait_running
    late.driver.slurm.wait_running = lambda job, timeout_s, **kw: seen.append(timeout_s) or wait(job, timeout_s, **kw)
    with pytest.raises(hpc.slurm.SlurmError, match="did not start within 21600 s"):
        late.up()
    assert seen == [21600.0]


@pytest.mark.parametrize(
    "value",
    [
        "soon",
        "0",
        "-900",
        "599",
        "86401",
        "1800.5",
        "1800.0",
        "1e3",
        "1_800",
        "+900",
        " 900",
        "900\n",
        "nan",
        "inf",
        "/some/where/else",
    ],
)
def test_o57_a_queue_bound_that_is_no_whole_number_from_600_to_86400_is_refused_naming_the_key(hpc, tmp_path, value):
    """O57: a value that is not a whole number of seconds from 600 to 86,400 is refused by `make_driver` (so before
    anything is made or submitted), the message names HPC_QUEUE_WAIT_S and is the same whatever the value was, so it
    repeats none."""
    with pytest.raises(hpc.settings.SettingsError) as refused:
        Rig(hpc, tmp_path, {}, environ={KEY: value})
    assert str(refused.value) == f"{KEY}: must be a whole number of seconds from 600 to 86400"


@pytest.mark.parametrize("value", ["600", "1800", "21600", "86400"])
def test_o57_the_ends_of_the_queue_bound_range_are_accepted(hpc, tmp_path, value):
    """O57: 600 and 86,400 are in the range, and the bound the driver holds is that many seconds."""
    assert Rig(hpc, tmp_path, {}, environ={KEY: value}).driver.timeouts["queue"] == float(value)


def test_o57_a_stale_queue_bound_never_stops_a_command_that_does_not_wait(hpc, tmp_path):
    """O57: `down` and the service verbs work from the state file and never wait for a queue, so a bad value in the
    environment cannot keep them from releasing a held job."""
    driver = hpc.cli.make_driver(LAYOUT, str(tmp_path / "root"), runner=FakeSlurm(), environ={KEY: "soon"}, agent=False)
    assert driver.timeouts["queue"] == 1800.0


# -- the held jobs of one up ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("agent_state", ["PENDING", "SUSPENDED"])
def test_o57_the_services_job_timing_out_releases_the_agents_job_too(hpc, tmp_path, site, agent_state):
    """O57: under "separate" the services job that never starts is cancelled by the wait, and the agent's own held job,
    submitted with it and in any state short of ended, is cancelled by its id too: none of this up's jobs is left to
    start and bill with no federation. The failure says what was released."""
    fake = separate("PENDING", agent_state)
    rig = Rig(hpc, tmp_path, site, SEPARATE_LAYOUT, fake)
    with pytest.raises(hpc.slurm.SlurmError, match="did not start within 1800 s") as refused:
        rig.up()
    assert sorted(fake.cancelled) == [SERVICES, AGENT]
    assert f"the held job(s) {AGENT} were cancelled" in str(refused.value)


def test_o57_the_agents_job_timing_out_releases_the_running_services_job_too(hpc, tmp_path, site):
    """O57: the second wait failing (the agent's own job never starts) cancels the services job that is running, by its
    id, once each."""
    fake = separate("RUNNING", "PENDING")
    rig = Rig(hpc, tmp_path, site, SEPARATE_LAYOUT, fake)
    with pytest.raises(hpc.slurm.SlurmError, match="did not start within 1800 s") as refused:
        rig.up()
    assert fake.cancelled == [AGENT, SERVICES]
    assert f"the held job(s) {SERVICES} were cancelled" in str(refused.value)


def test_o57_a_held_job_that_ended_before_it_ran_releases_the_other_one(hpc, tmp_path, site):
    """O57: the other way a wait fails (the job ends before it ran) releases the same way."""
    fake = separate("FAILED", "PENDING")
    rig = Rig(hpc, tmp_path, site, SEPARATE_LAYOUT, fake)
    with pytest.raises(hpc.slurm.SlurmError, match="FAILED before it ran"):
        rig.up()
    assert AGENT in fake.cancelled


def test_o57_a_release_that_fails_is_said_and_left_to_down(hpc, tmp_path, site):
    """O57: a cancel that Slurm refuses is not hidden: the failure names the job and says to run down."""
    fake = separate("PENDING", "PENDING")
    real = fake.run

    def refusing(argv, timeout):
        if argv[:2] == ["scancel", AGENT]:
            return hpc.slurm.Completed(1, "", "scancel: error: Socket timed out\n")
        return real(argv, timeout)

    fake.run = refusing
    rig = Rig(hpc, tmp_path, site, SEPARATE_LAYOUT, fake)
    with pytest.raises(hpc.slurm.SlurmError) as refused:
        rig.up()
    assert f"cancelling job(s) {AGENT} failed: run down to release them" in str(refused.value)


def test_o57_a_held_job_that_cannot_be_read_is_never_guessed_at_and_is_left_to_down(hpc, tmp_path, site):
    """O57: a job whose record cannot be read at the release is not cancelled on a guess (nothing proves it is still
    this up's); the failure names it and says to run down, which checks it the same way."""
    fake = separate("PENDING", "RUNNING")
    real = fake.run

    def unreadable(argv, timeout):
        if argv[:5] == ["scontrol", "show", "job", "-o", AGENT] and fake.cancelled:
            return hpc.slurm.Completed(1, "", "slurm_load_jobs error: Socket timed out\n")
        return real(argv, timeout)

    fake.run = unreadable
    rig = Rig(hpc, tmp_path, site, SEPARATE_LAYOUT, fake)
    with pytest.raises(hpc.slurm.SlurmError) as refused:
        rig.up()
    assert fake.cancelled == [SERVICES]
    assert f"job(s) {AGENT} could not be read: run down to release them" in str(refused.value)


def test_o57_a_job_id_that_now_carries_another_name_is_never_cancelled(hpc, tmp_path, site):
    """O57: Slurm reuses ids. When the services job times out and the agent's id now belongs to a job of another name,
    that job is not this up's: it is not cancelled, and the failure says so."""
    fake = separate("PENDING", "RUNNING")
    real = fake.run

    def reused(argv, timeout):
        if argv[:2] == ["scancel", SERVICES]:  # the agent's id is another job's from here on
            fake.component_recorded[(AGENT, 0)] = {"JobName": "somebody-elses-analysis"}
        return real(argv, timeout)

    fake.run = reused
    rig = Rig(hpc, tmp_path, site, SEPARATE_LAYOUT, fake)
    with pytest.raises(hpc.slurm.SlurmError) as refused:
        rig.up()
    assert fake.cancelled == [SERVICES]
    assert f"job(s) {AGENT} carry another job's name and were not cancelled" in str(refused.value)
