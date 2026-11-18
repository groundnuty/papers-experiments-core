"""Under "separate" the agent's own held job never lands on a node of its own federation's services job
(deploy/hpc-federation/hpcdriver.py `hold_and_place`; DESIGN.md section 1; live, 18 Nov: four ups failed).

Submitted beside the services' job, the agent's job could be packed by Slurm onto one of the services' nodes, which
the exclusion (other federations' nodes, read before the submit) cannot name: the up's own nodes are not known yet. So
under "separate" without `HPC_EXCLUSIVE_NODES` the agent's job is submitted once the services' job runs and its nodes
are read, excluding them and the other federations' nodes. Exclusive jobs keep today's simultaneous submission (whole
nodes cannot be shared), and a heterogeneous job (`het`) is one job and unchanged.

Slurm is the fake of hpc_fed_support with `shared_nodes`: its jobs share nodes by cores, as the live cluster's did."""

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, OtherJob, agent_site, hpc_on_path
from hpc_fleet_support import SEPARATE_LAYOUT

SERVICES, AGENT = "4242", "4343"  # the fake's two job ids under "separate"
OTHER = "hpcop-another-study-1a2b3c4d"  # another federation's held job
POOL = [f"node{i:02d}" for i in range(1, 21)]


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
    """A driver over the fake Slurm whose clock moves 100 s a reading (a job that never starts reaches its bound);
    `said` is what `up` told the operator; `sbatch` is each sbatch's argv with the services job's state at that time."""

    def __init__(self, hpc, tmp_path, site, layout=SEPARATE_LAYOUT, fake=None):
        self.hpc, self.site, self.fake = hpc, site, fake or FakeSlurm()
        self.said, self.sbatch = [], []
        (tmp_path / "fed").mkdir(parents=True, exist_ok=True)
        self.driver = hpc.cli.make_driver(layout, str(tmp_path / "fed" / "root"), runner=self.fake)
        self.driver.say = self.said.append
        self.driver.sleep = lambda s: None
        self.driver.poll_s = 0.0
        self.driver.slurm.sleep = lambda s: None
        self.driver.slurm.poll_s = 0.0
        ticks = iter(range(0, 10**7, 100))
        self.driver.slurm.clock = lambda: next(ticks)
        self.driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        self.driver.relay_available = lambda: False
        real = self.fake.run

        def spy(argv, timeout):
            if argv[0] == "sbatch":
                self.sbatch.append((list(argv), self.fake.state))
            return real(argv, timeout)

        self.fake.run = spy

    def up(self):
        return self.driver.up(self.hpc.settings.site_from_environment(self.site))

    def state(self):
        return self.driver.store.read()

    def starts_when_waited_for(self):
        """The services job is queued until the driver first waits for it, then runs."""
        self.fake.state = "PENDING"
        self.driver.slurm.sleep = lambda s: setattr(self.fake, "state", "RUNNING")


def excluded(argv):
    return [word.partition("=")[2] for word in argv if word.startswith("--exclude=")]


def test_o57_under_separate_the_agents_job_is_submitted_once_the_services_job_runs_off_its_nodes(hpc, tmp_path, site):
    """O57: with the services' job queued, the agent's job is not submitted; once it runs and its nodes are read, the
    agent's sbatch carries `--exclude` naming every one of them, and the services' sbatch named none (no other
    federation held a node). A fake that packs jobs onto shared nodes would have put the agent on the first of them."""
    rig = Rig(hpc, tmp_path, site, fake=FakeSlurm(nodes=POOL, shared_nodes=True))
    rig.starts_when_waited_for()
    rig.up()
    (services, queued), (agent, running) = rig.sbatch
    assert (queued, running) == ("PENDING", "RUNNING")  # the second sbatch came after the first job ran
    assert excluded(services) == []
    assert excluded(agent) == [",".join(f"node0{i}" for i in range(1, 7))]
    state = rig.state()
    assert state["allocation"]["nodes"] == [f"node0{i}" for i in range(1, 7)]
    assert state["sandbox"]["node"] == "node07"
    assert state["phase"] in ("placed", "linked")
    assert not any("node0" in line for line in rig.said)  # a count is said, never a node


def test_o57_the_agents_exclusion_is_the_services_nodes_plus_the_other_federations(hpc, tmp_path, site):
    """O57: the agent's job keeps off the services' nodes and the other federations' nodes both, and lands on neither
    group."""
    others = [OtherJob("5001", OTHER, "RUNNING", ("node01", "node02"))]
    rig = Rig(hpc, tmp_path, site, fake=FakeSlurm(nodes=POOL, shared_nodes=True, others=others))
    rig.up()
    (services, _), (agent, _) = rig.sbatch
    assert excluded(services) == ["node01,node02"]
    assert excluded(agent) == [",".join(f"node0{i}" for i in range(1, 9))]  # 2 of the other's, 6 of the services'
    assert rig.state()["sandbox"]["node"] == "node09"


def test_o57_an_agent_job_that_never_starts_within_the_bound_releases_both_jobs(hpc, tmp_path, site):
    """O57: the agent's job queued for lack of nodes (what is left after the exclusion) fails at the queue bound, which
    says how many nodes it excluded (a count, never a node); its own job is cancelled by the wait, and the running
    services' job by the release, each by its id, with no step run."""
    fake = FakeSlurm(nodes=POOL, shared_nodes=True, job_states={AGENT: "PENDING"})
    rig = Rig(hpc, tmp_path, site, fake=fake)
    with pytest.raises(hpc.slurm.SlurmError, match="did not start within 1800 s") as refused:
        rig.up()
    assert fake.cancelled == [AGENT, SERVICES]
    assert "excluded 6 node(s) of this federation's own services job" in str(refused.value)
    assert "node0" not in str(refused.value)
    assert fake.sruns == []


def test_o57_the_agents_wait_rides_out_a_slow_controller_like_the_services(hpc, tmp_path, site):
    """O57: reads of the agent's job that fail while it waits are missed polls (said once), not the job's end."""
    fake = FakeSlurm(nodes=POOL, shared_nodes=True)
    real, reads = fake.run, []

    def slow(argv, timeout):
        if argv[:3] == ["scontrol", "show", "job"] and argv[4] == AGENT:
            reads.append(argv)
            if len(reads) in (2, 3):  # the read-back after the submit is the first: the wait's two reads fail
                return hpc.slurm.Completed(1, "", "slurm_load_jobs error: Socket timed out on send/recv operation\n")
        return real(argv, timeout)

    fake.run = slow
    rig = Rig(hpc, tmp_path, site, fake=fake)
    rig.up()
    assert len(reads) > 3
    assert sum("a missed poll of held job" in line and AGENT in line for line in rig.said) == 1
    assert fake.cancelled == []


def test_o57_with_exclusive_nodes_both_jobs_are_submitted_at_once_as_they_were(hpc, tmp_path, site):
    """O57: with HPC_EXCLUSIVE_NODES=1 the jobs hold whole nodes and cannot share one, so the agent's job is submitted
    with the services' as before: while it is queued, naming no services' node (none is known) and `--exclusive`."""
    rig = Rig(hpc, tmp_path, {**site, "HPC_EXCLUSIVE_NODES": "1"}, fake=FakeSlurm(nodes=POOL))
    rig.starts_when_waited_for()
    rig.up()
    (services, queued), (agent, also) = rig.sbatch
    assert (queued, also) == ("PENDING", "PENDING")
    assert excluded(services) == excluded(agent) == []
    assert "--exclusive" in services
    assert "--exclusive" in agent
    assert rig.state()["sandbox"]["node"] != rig.state()["allocation"]["nodes"][0]


def test_o57_a_heterogeneous_job_is_one_job_and_unchanged(hpc, tmp_path, site):
    """O57: under "het" the agent's node is component 1 of the one held job: one sbatch, no services' node in it."""
    others = [OtherJob("5001", OTHER, "RUNNING", ("node01", "node02"))]
    rig = Rig(hpc, tmp_path, site, LAYOUT, FakeSlurm(nodes=POOL, shared_nodes=True, others=others))
    rig.up()
    assert len(rig.sbatch) == 1
    assert excluded(rig.sbatch[0][0]) == ["node01,node02", "node01,node02"]  # each component, the other's nodes only
    assert rig.state()["sandbox"]["node"] not in rig.state()["allocation"]["nodes"]


def test_o57_a_services_job_whose_id_was_reused_during_the_agents_wait_is_never_cancelled(hpc, tmp_path, site):
    """O57: the services' job is read when it runs, and the agent's job queues after that. Every held job is read again
    once both run, so a services' job id that now carries another name (it ended, Slurm reused its id) is found there:
    it is named and never cancelled, and the agent's own job is."""
    fake = FakeSlurm(nodes=POOL, shared_nodes=True)
    rig = Rig(hpc, tmp_path, site, fake=fake)
    real = fake.run

    def reused(argv, timeout):
        if argv[0] == "sbatch" and len(rig.sbatch) == 1:  # the agent's job is being submitted: its wait begins
            fake.component_recorded[(SERVICES, 0)] = {"JobName": "somebody-elses-analysis"}
        return real(argv, timeout)

    fake.run = reused
    with pytest.raises(hpc.slurm.SlurmError, match="carries another job's name") as refused:
        rig.up()
    assert fake.cancelled == [AGENT]
    assert f"job(s) {SERVICES} carry another job's name and were not cancelled" in str(refused.value)
