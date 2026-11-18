"""The HPC driver's wait for its held jobs to start (deploy/hpc-federation/hpcdriver.py, `bring_up`; README.md, "Two
federations of one account" and "Site configuration").

`up` submits every held job (the services', and under sandbox_placement "separate" the agent's own) and then waits for
each to run, at a bound the site may set (`HPC_QUEUE_WAIT_S`, seconds; 1800 when unset). A wait that fails releases
every held job of this up that has not ended, by its own id, and never one that is not its own.

A failure anywhere between the first sbatch and the end of the placement (a second submit, the running jobs' read-back,
a job placed on a node another holds, the live read of other federations, the placement itself) releases them the same
way. `HPC_EXCLUSIVE_NODES=1` puts `--exclusive` on every held job, so that no other job shares its nodes, and the
read-back refuses a job Slurm did not record as exclusive.

Slurm is the fake of hpc_fed_support."""

import dataclasses
import re

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, hpc_on_path
from hpc_fleet_support import SEPARATE_LAYOUT

KEY = "HPC_QUEUE_WAIT_S"
EXCLUSIVE = "HPC_EXCLUSIVE_NODES"
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


# -- a failure before the federation is up releases every held job --------------------------------------------------


@pytest.mark.parametrize(
    ("placed", "words"),
    [
        ({(AGENT, 0): ["node01"]}, f"job {AGENT} was placed on node01, held twice, not on the 1 asked for"),
        ({(SERVICES, 0): [f"node0{i}" for i in range(1, 6)]}, f"job {SERVICES} was placed on 5 node(s), not on the 6"),
    ],
    ids=["the agents job on a node the services job holds", "the services job on fewer nodes than asked"],
)
def test_o57_a_job_placed_wrongly_releases_every_held_job_of_the_up(hpc, tmp_path, site, placed, words):
    """O57: Slurm packed a held job onto a node already held (live, 18 Nov: five ups at once). The job placed wrongly is
    no longer the only one cancelled: the other held job of this up, running, is cancelled by its id too, or it would
    bill six nodes with no federation. The failure says what was released, and no step ran."""
    fake = FakeSlurm(node_lists=placed)
    rig = Rig(hpc, tmp_path, site, SEPARATE_LAYOUT, fake)
    with pytest.raises(hpc.slurm.SlurmError, match=re.escape(words)) as refused:
        rig.up()
    assert sorted(fake.cancelled) == [SERVICES, AGENT]
    assert str(refused.value).endswith(f"the held job(s) {SERVICES}, {AGENT} were cancelled")
    assert fake.sruns == []


def test_o57_a_read_back_of_the_running_jobs_that_fails_releases_every_held_job(hpc, tmp_path, site):
    """O57: the read-back taken once the jobs run (the scheduler may allocate more than a pending request showed)
    cancels the job it found otherwise than asked, and the other held job of this up with it."""
    fake = FakeSlurm(running_recorded={"NumNodes": "5"})
    rig = Rig(hpc, tmp_path, site, SEPARATE_LAYOUT, fake)
    with pytest.raises(hpc.slurm.SlurmError, match="NumNodes") as refused:
        rig.up()
    assert fake.cancelled == [SERVICES, AGENT]
    assert str(refused.value).endswith(f"the held job(s) {AGENT} were cancelled")


@pytest.mark.parametrize("method", ["keep_off_other_federations", "place"])
def test_o57_a_failure_of_the_live_read_or_the_placement_releases_every_held_job(hpc, tmp_path, site, method):
    """O57: whatever fails once the jobs run and before a step does (the read of other federations' jobs, the recording
    of the placement), every held job of this up is cancelled by its id, and the failure says so."""
    fake = FakeSlurm()
    rig = Rig(hpc, tmp_path, site, SEPARATE_LAYOUT, fake)

    def failing(*args, **kwargs):
        raise hpc.slurm.SlurmError("boom")

    setattr(rig.driver, method, failing)
    with pytest.raises(hpc.slurm.SlurmError) as refused:
        rig.up()
    assert str(refused.value) == f"boom; the held job(s) {SERVICES}, {AGENT} were cancelled"
    assert sorted(fake.cancelled) == [SERVICES, AGENT]


def test_o57_a_failure_that_is_no_slurm_error_releases_every_held_job_and_is_raised_as_it_was(hpc, tmp_path, site):
    """O57: a state that cannot be written (here, a placement that fails on a missing key) is no Slurm error and still
    leaves no held job to run: they are cancelled, the operator is told, and the error is the one raised."""
    fake = FakeSlurm()
    rig = Rig(hpc, tmp_path, site, SEPARATE_LAYOUT, fake)
    said = []
    rig.driver.say = said.append

    def failing(*args, **kwargs):
        raise KeyError("services")

    rig.driver.place = failing
    with pytest.raises(KeyError):
        rig.up()
    assert sorted(fake.cancelled) == [SERVICES, AGENT]
    assert f"the held job(s) {SERVICES}, {AGENT} were cancelled" in said


def test_o57_a_second_submit_that_fails_releases_the_first_job(hpc, tmp_path, site):
    """O57: the agent's own job refused by sbatch leaves the services' job, already submitted: it is cancelled by its id
    and the failure says so."""
    fake = FakeSlurm()
    real, asked = fake.run, []

    def refusing(argv, timeout):
        if argv[0] == "sbatch":
            asked.append(argv)
            if len(asked) == 2:
                return hpc.slurm.Completed(1, "", "sbatch: error: Batch job submission failed\n")
        return real(argv, timeout)

    fake.run = refusing
    rig = Rig(hpc, tmp_path, site, SEPARATE_LAYOUT, fake)
    with pytest.raises(hpc.slurm.SlurmError, match="sbatch failed with status 1") as refused:
        rig.up()
    assert fake.cancelled == [SERVICES]
    assert str(refused.value).endswith(f"the held job(s) {SERVICES} were cancelled")


def test_o57_a_first_submit_that_fails_has_nothing_to_release_and_says_what_it_did(hpc, tmp_path, site):
    """O57: with no held job yet, the failure is sbatch's own words and nothing is cancelled."""
    fake = FakeSlurm()
    real = fake.run
    fake.run = lambda argv, timeout: (
        hpc.slurm.Completed(1, "", "sbatch: error: invalid account\n") if argv[0] == "sbatch" else real(argv, timeout)
    )
    rig = Rig(hpc, tmp_path, site, SEPARATE_LAYOUT, fake)
    with pytest.raises(hpc.slurm.SlurmError, match="sbatch failed with status 1") as refused:
        rig.up()
    assert "were cancelled" not in str(refused.value)
    assert fake.cancelled == []


# -- exclusive nodes, a site key ------------------------------------------------------------------------------------

ARGV = [
    "sbatch", "--parsable", "--job-name=hpcop-p-abcd", "--account=acct", "--partition=part", "--no-requeue",
    "--time=02:00:00", "--nodes=6", "--ntasks-per-node=1", "--cpus-per-task=5", "--mem=11264M", "--exclude=node01",
    "--chdir=/", "--output=/dev/null", "--export=NONE", "--wrap=exec sleep 7200",
    ":", "--account=acct", "--partition=part", "--time=02:00:00", "--nodes=1", "--ntasks-per-node=1",
    "--cpus-per-task=9", "--mem=18432M", "--exclude=node01",
]  # fmt: skip


def test_o57_an_allocation_not_asked_exclusive_has_the_argv_it_always_had(hpc):
    """O57: the argv of a held job with no `exclusive` is word for word what it was before the option existed, so a site
    that sets no key is unchanged; asked exclusive, `--exclusive` follows `--mem` in every component, once."""
    shape = hpc.slurm.Shape(1, 9, 18432)
    asked = hpc.slurm.Allocation(
        "hpcop-p-abcd", "acct", "part", 6, 5, 11264, "02:00:00", 7200, het=(shape,), exclude=("node01",)
    )
    assert asked.argv() == ARGV
    wanted = [word for item in ARGV for word in ([item, "--exclusive"] if item.startswith("--mem=") else [item])]
    assert dataclasses.replace(asked, exclusive=True).argv() == wanted
    assert wanted.count("--exclusive") == 2


def test_o57_the_key_is_off_unless_the_site_sets_it_to_one_and_is_no_part_of_the_state(hpc, site):
    """O57: HPC_EXCLUSIVE_NODES unset, empty or 0 is off, 1 is on; the state's record of the site is what it was in
    either case (only `up` submits, and `down` needs none of it)."""
    off = hpc.settings.site_from_environment(site)
    for value in ("", "0"):
        assert hpc.settings.site_from_environment({**site, EXCLUSIVE: value}) == off
    on = hpc.settings.site_from_environment({**site, EXCLUSIVE: "1"})
    assert (off.exclusive_nodes, on.exclusive_nodes) == (False, True)
    assert off.to_json() == on.to_json()
    assert "exclusive_nodes" not in on.to_json()


@pytest.mark.parametrize(
    "value", ["2", "-1", "01", "00", "true", "yes", "on", "1.0", " 1", "1\n", "0 ", "10", "/some/where/else"]
)
def test_o57_a_value_that_is_neither_zero_nor_one_is_refused_naming_the_key(hpc, site, value):
    """O57: any other value is refused before anything is made or submitted, naming HPC_EXCLUSIVE_NODES and repeating
    none of it."""
    with pytest.raises(hpc.settings.SettingsError) as refused:
        hpc.settings.site_from_environment({**site, EXCLUSIVE: value})
    assert str(refused.value) == "HPC_EXCLUSIVE_NODES: must be 0 or 1"


@pytest.mark.parametrize("layout", [LAYOUT, SEPARATE_LAYOUT], ids=["het", "separate"])
def test_o57_with_the_key_every_held_job_and_component_is_submitted_exclusive(hpc, tmp_path, site, layout):
    """O57: HPC_EXCLUSIVE_NODES=1 puts `--exclusive` on the services' job, on the agent's component of a het job and on
    the agent's own job under "separate"; `up` says so, and the read-back accepts what Slurm recorded."""
    fake = FakeSlurm()
    rig = Rig(hpc, tmp_path, {**site, EXCLUSIVE: "1"}, layout, fake)
    said = []
    rig.driver.say = said.append
    rig.up()
    components = [options for held in fake.submitted.values() for options in held]
    assert len(components) == 2
    assert all("--exclusive" in options for options in components)
    assert len(fake.submitted) == (1 if layout == LAYOUT else 2)
    assert any(line.startswith(EXCLUSIVE) for line in said)


@pytest.mark.parametrize("layout", [LAYOUT, SEPARATE_LAYOUT], ids=["het", "separate"])
def test_o57_with_the_key_unset_or_zero_no_job_is_exclusive_and_the_argv_is_the_same(hpc, tmp_path, site, layout):
    """O57: with the key unset and with 0, the sbatch argv of every held job is the same and names no `--exclusive`."""
    argv = {}
    for index, environ in enumerate(({}, {EXCLUSIVE: "0"})):
        fake = FakeSlurm()
        rig = Rig(hpc, tmp_path / str(index), {**site, **environ}, layout, fake)
        rig.up()
        argv[index] = [  # the job's name holds a random part of up's own
            [word.partition("=")[0] if word.startswith("--job-name=") else word for word in call]
            for call in fake.calls
            if call[0] == "sbatch"
        ]
    assert argv[0] == argv[1] != []
    assert not any("--exclusive" in call for call in argv[0])


@pytest.mark.parametrize("recorded", ["OK", "YES", "USER", "MCS"])
@pytest.mark.parametrize(
    ("layout", "job", "component", "cancelled"),
    [
        (LAYOUT, SERVICES, 0, [SERVICES]),
        (LAYOUT, SERVICES, 1, [SERVICES]),
        (SEPARATE_LAYOUT, AGENT, 0, [AGENT, SERVICES]),
    ],
    ids=["the services' job", "the het component", "the agent's own job"],
)
def test_o57_a_job_slurm_did_not_record_as_exclusive_is_refused_and_cancelled(
    hpc, tmp_path, site, layout, job, component, cancelled, recorded
):
    """O57: with the key set, a held job whose OverSubscribe Slurm recorded as anything but NO shares its nodes: it is
    refused naming the field, no step runs, and every held job of this up is cancelled."""
    fake = FakeSlurm(component_recorded={(job, component): {"OverSubscribe": recorded}})
    rig = Rig(hpc, tmp_path, {**site, EXCLUSIVE: "1"}, layout, fake)
    with pytest.raises(hpc.slurm.SlurmError, match="OverSubscribe") as refused:
        rig.up()
    assert fake.cancelled == cancelled
    assert "recorded otherwise than asked" in str(refused.value)
    assert fake.sruns == []


def test_o57_a_job_that_shares_its_nodes_is_not_refused_when_the_key_is_off(hpc, tmp_path, site):
    """O57: OverSubscribe is read only for a job asked exclusive; a site that sets no key is never refused for it."""
    fake = FakeSlurm(component_recorded={(SERVICES, 0): {"OverSubscribe": "OK"}})
    Rig(hpc, tmp_path, site, LAYOUT, fake).up()
    assert fake.cancelled == []


def test_o57_an_exclusive_job_that_reports_every_core_of_its_nodes_is_accepted(hpc, tmp_path, site):
    """O57: Slurm gives an exclusive job every core of each node, and reports them: more cores than asked is accepted
    for a job asked exclusive; a job not asked exclusive is still refused for it."""
    whole = FakeSlurm(component_recorded={(SERVICES, 0): {"NumCPUs": "384"}})  # six nodes of 64 cores
    Rig(hpc, tmp_path / "exclusive", {**site, EXCLUSIVE: "1"}, LAYOUT, whole).up()
    assert whole.cancelled == []
    shared = FakeSlurm(component_recorded={(SERVICES, 0): {"NumCPUs": "384"}})
    with pytest.raises(hpc.slurm.SlurmError, match="NumCPUs"):
        Rig(hpc, tmp_path / "shared", site, LAYOUT, shared).up()
    assert shared.cancelled == [SERVICES]
