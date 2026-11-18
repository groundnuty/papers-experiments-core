"""A fleet's ramp-up is parallel and cheap (deploy/hpc-federation/fleet.py, hpcdriver.py, hpcexclude.py, hpcslurm.py;
DESIGN.md section 1, "A fleet"; README.md, "A fleet of federations"), regression O68.

Three changes, each over the fake Slurm of hpc_fed_support (which models `--dependency=after:<job>`):

- under `sandbox_placement = "separate"` the agent's own held job is submitted to START after the services' job, so a
  pending services job never leaves the agent's node billing alone;
- the fleet's ceiling is a site key, `HPC_FLEET_CEILING` (1 to 512, 64 when unset);
- the ups of a fleet that run at once take one lock around their first read of the account's live jobs with their
  submits and around their second read, and one that is placed on another's node is released and tried again, up to
  three times in all; an up run by hand takes no lock and is tried once."""

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from hpc_fed_support import SITE, FakeSlurm, OtherJob, agent_site, hpc_on_path
from hpc_fleet_support import SEPARATE_LAYOUT, fleet_modules, make_bench, make_ca
from tls_support import HAS_OPENSSL

SERVICES, AGENT = "4242", "4343"  # the fake's two job ids under "separate"
RIVAL = "hpcop-another-study-1a2b3c4d"  # another federation's held job: hpcop-<project>-<hex>
POOL = [f"node{i:02d}" for i in range(1, 41)]  # a cluster big enough for several federations' seven nodes each
needs_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="the zone's CA is made with openssl")


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules() as found:
        yield found


@pytest.fixture(scope="module")
def ca(tmp_path_factory):
    return make_ca(tmp_path_factory.mktemp("ca"))


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


@pytest.fixture
def keep_umask():
    """Drivers run in threads here share the process's umask, which each sets while it writes a secret: left as the
    test found it, so that no later test's files are made with another mode."""
    previous = os.umask(0o022)
    os.umask(previous)
    yield
    os.umask(previous)


class Rig:
    """One driver over the fake Slurm, the core's phase recorded instead of run. Its clock moves 100 s a reading, so a
    job that never starts reaches its bound; `lock` makes it an up of a fleet (`hpcdriver.Driver.placement_lock`)."""

    def __init__(self, hpc, tmp_path, site, fake=None, lock=False, environ=None):
        self.hpc, self.site, self.fake = hpc, site, fake or FakeSlurm()
        self.lock = tmp_path / ".placement.lock"
        self.said: list = []
        self.driver = hpc.cli.make_driver(SEPARATE_LAYOUT, str(tmp_path / "fed" / "root"), runner=self.fake,
                                          environ=environ)  # fmt: skip
        self.driver.say = self.said.append
        self.driver.sleep = lambda s: None
        self.driver.poll_s = 0.0
        self.driver.slurm.sleep = lambda s: None
        self.driver.slurm.poll_s = 0.0
        ticks = iter(range(0, 10**7, 100))
        self.driver.slurm.clock = lambda: next(ticks)
        self.driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        self.driver.relay_available = lambda: False
        if lock:
            self.driver.placement_lock = self.lock

    def up(self):
        return self.driver.up(self.hpc.settings.site_from_environment(self.site))

    def sbatches(self) -> list:
        return [call for call in self.fake.calls if call[0] == "sbatch"]

    def reads(self) -> int:
        return len([call for call in self.fake.calls if call[:2] == ["squeue", "--me"]])

    def state(self) -> dict:
        return self.driver.store.read()


def lock_is_held(path: Path) -> bool:
    """Whether another descriptor can take the lock now: a lock held by anyone else refuses it."""
    import fcntl

    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)
    return False


# ------------------------------------------------------------------------------------ (1) no idle agent node
def test_o68_the_agents_job_is_submitted_to_start_after_the_services_job(hpc, tmp_path, site):
    """O68: [fails before] under "separate" the agent's own held job carries `--dependency=after:<services job>`
    ("begin after the job starts", never `afterok`), the services' job none, and the dependency is what Slurm
    recorded."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    services, agent = rig.sbatches()
    assert not any(word.startswith("--dependency") for word in services)
    assert f"--dependency=after:{SERVICES}" in agent
    assert not any("afterok" in word for word in agent)
    assert rig.fake.submitted[AGENT][0]["--dependency"] == f"after:{SERVICES}"
    assert "--dependency" not in rig.fake.submitted[SERVICES][0]


def test_o68_a_services_job_that_never_starts_leaves_no_agent_job_billing(hpc, tmp_path, site):
    """O68: [fails before] a services job that pends past the queue bound is cancelled, and at that moment the agent's
    job was held with it and billed nothing; both are then cancelled, and the failure says what was released."""
    fake = FakeSlurm(state="PENDING")
    rig = Rig(hpc, tmp_path, site, fake)
    with pytest.raises(hpc.slurm.SlurmError, match="did not start within 1800 s") as refused:
        rig.up()
    assert fake.state_of(AGENT) != "PENDING" or AGENT in fake.cancelled
    assert dict(fake.cancel_views)[SERVICES] == []  # nothing billed when the services' job was cancelled
    assert sorted(fake.cancelled) == [SERVICES, AGENT]
    assert f"the held job(s) {AGENT} were cancelled" in str(refused.value)


def test_o68_the_agents_job_waits_for_the_services_job_and_starts_when_it_does(hpc, tmp_path, site):
    """O68: a services job pending at the read-back (the agent's job shows `after:<job>(unfulfilled)`) and starting
    later brings the agent's job up with it: the agent's job is pending while the services' pends, running once it
    starts, and the up completes."""
    fake = FakeSlurm(state="PENDING")
    rig = Rig(hpc, tmp_path, site, fake)
    seen = []

    def start(seconds):  # each poll of the services' pending job
        seen.append((fake.state_of(SERVICES), fake.state_of(AGENT)))
        fake.state = "RUNNING"

    rig.driver.slurm.sleep = start
    rig.up()
    assert seen == [("PENDING", "PENDING")]
    assert rig.state()["phase"] == "linked"
    assert fake.billing() == [SERVICES, AGENT]


def test_o68_a_dependency_slurm_did_not_record_cancels_the_agents_job_and_fails_the_up(hpc, tmp_path, site):
    """O68: the read-back of the dependency: a site that drops it (the agent's job recorded with none while the
    services' job still pends, so it would start with no services) has that job cancelled by its own id and the up
    fails; the services' job is kept for down, as for any job recorded otherwise than asked."""
    fake = FakeSlurm(state="PENDING", component_recorded={(AGENT, 0): {"Dependency": "(null)"}})
    rig = Rig(hpc, tmp_path, site, fake)
    with pytest.raises(hpc.slurm.SlurmError, match=r"recorded otherwise than asked \(its dependency\)"):
        rig.up()
    assert fake.cancelled == [AGENT]


def test_o68_a_dependency_on_another_job_is_no_dependency_on_the_services_job(hpc, tmp_path, site):
    """O68: a dependency that names another job than the services' one is recorded otherwise than asked."""
    fake = FakeSlurm(state="PENDING", component_recorded={(AGENT, 0): {"Dependency": "after:9999(unfulfilled)"}})
    rig = Rig(hpc, tmp_path, site, fake)
    with pytest.raises(hpc.slurm.SlurmError, match=r"\(its dependency\)"):
        rig.up()
    assert fake.cancelled == [AGENT]


def test_o68_a_dependency_that_cannot_be_checked_cancels_the_agents_job_too(hpc, tmp_path, site):
    """O68: the read-back with no dependency recorded asks whether the services' job still waits; when that read fails
    nothing shows the dependency holds, so the agent's job is cancelled by its id and never left free to start."""
    fake = FakeSlurm(state="PENDING", component_recorded={(AGENT, 0): {"Dependency": "(null)"}})
    real = fake.run

    def unreadable(argv, timeout):
        if argv[:5] == ["scontrol", "show", "job", "-o", SERVICES] and AGENT in fake.submitted:
            return hpc.slurm.Completed(1, "", "slurm_load_jobs error: Socket timed out\n")
        return real(argv, timeout)

    fake.run = unreadable
    rig = Rig(hpc, tmp_path, site, fake)
    with pytest.raises(hpc.slurm.SlurmError, match="dependency could not be checked"):
        rig.up()
    assert fake.cancelled == [AGENT]


def test_o68_the_het_placement_and_a_job_with_no_agent_carry_no_dependency(hpc):
    """O68: only the agent's own job under "separate" has one: the argv of every other Allocation is what it was."""
    plain = hpc.slurm.Allocation("hpcop-p-abcd", "a", "p", 6, 5, 11264, "02:00:00", 7200)
    assert not any(word.startswith("--dependency") for word in plain.argv())
    with pytest.raises(hpc.slurm.SlurmError, match="not one job id"):
        hpc.slurm.Allocation("hpcop-p-abcd", "a", "p", 1, 5, 11264, "02:00:00", 7200, after="4242; rm").argv()


# ----------------------------------------------------------------------------- (2) the ceiling, a site key
def stand_in(label, command, layout, root, log):
    """A federation's up that does nothing and fails at once: a fleet made of these shows what the fleet accepted."""
    return 1, "error: up: a stand-in"


def record(bench) -> dict:
    return json.loads((bench.fleet / "fleet.json").read_text(encoding="utf-8"))


@needs_openssl
def test_o68_the_ceiling_is_64_when_the_site_sets_none(bench):
    """O68: HPC_FLEET_CEILING unset or empty keeps the 64 nothing could pass: 65 federations are refused before anything
    is made, and 64 are accepted."""
    fleet = bench.deploy.fleet
    assert fleet.ceiling({}) == fleet.ceiling({"HPC_FLEET_CEILING": ""}) == 64
    code, printed = bench.up(65, environ={**bench.environ, "HPC_FLEET_MAX": "65"})
    assert code == 2
    assert "HPC_FLEET_MAX must be a whole number from 1 to 64" in printed
    assert not bench.fleet.exists()
    code, printed = bench.up(64, environ={**bench.environ, "HPC_FLEET_MAX": "64"}, federation=stand_in)
    assert "must be a whole number" not in printed
    assert len(record(bench)["members"]) == 64


@needs_openssl
def test_o68_a_ceiling_of_100_accepts_a_66_federation_fleet(bench):
    """O68: with HPC_FLEET_CEILING=100 and HPC_FLEET_MAX=66, `up 66` is accepted and makes 66 members (their ups run
    through a stand-in); HPC_FLEET_PARALLEL may be as high as the ceiling too."""
    environ = {**bench.environ, "HPC_FLEET_MAX": "66", "HPC_FLEET_CEILING": "100", "HPC_FLEET_PARALLEL": "100"}
    code, printed = bench.up(66, environ=environ, federation=stand_in)
    assert "must be a whole number" not in printed
    assert code == 1  # the stand-in's ups failed: the fleet was accepted, and reports them
    assert [m["label"] for m in record(bench)["members"]] == [f"fed-{n}" for n in range(1, 67)]
    assert len(record(bench)["shards"]) == 66
    assert bench.fakes == {}


@needs_openssl
def test_o68_the_maximum_still_applies_under_the_ceiling(bench):
    """O68: HPC_FLEET_MAX is the operator's maximum whatever the ceiling: past it, refused before anything is made."""
    environ = {**bench.environ, "HPC_FLEET_MAX": "5", "HPC_FLEET_CEILING": "100"}
    code, printed = bench.up(6, environ=environ)
    assert code == 2
    assert "past the maximum of 5 (HPC_FLEET_MAX)" in printed
    assert not bench.fleet.exists()


@needs_openssl
@pytest.mark.parametrize(
    "value", ["0", "513", "-1", "sixty", "64.0", "1e2", " 64", "٦٤", pytest.param("9" * 4301, id="4301-nines")]
)
def test_o68_a_ceiling_out_of_range_is_refused_naming_the_key(bench, value):
    """O68: a ceiling that is not a whole number from 1 to 512 refuses `up` before anything is made."""
    code, printed = bench.up(2, environ={**bench.environ, "HPC_FLEET_CEILING": value})
    assert code == 2
    assert "HPC_FLEET_CEILING must be a whole number from 1 to 512" in printed
    assert not bench.fleet.exists()
    assert bench.fakes == {}


@needs_openssl
@pytest.mark.parametrize(("ceiling", "ok"), [("1", False), ("5", True), ("512", True)])
def test_o68_the_ceiling_bounds_the_maximum_and_the_parallel_setting(bench, ceiling, ok):
    """O68: a maximum or a parallel setting past the site's ceiling is refused, naming the setting; the ends of the
    range are accepted."""
    fleet = bench.deploy.fleet
    environ = {"HPC_FLEET_CEILING": ceiling, "HPC_FLEET_MAX": "5", "HPC_FLEET_PARALLEL": "2"}
    if ok:
        assert (fleet.setting(environ, "HPC_FLEET_MAX"), fleet.setting(environ, "HPC_FLEET_PARALLEL")) == (5, 2)
    else:
        with pytest.raises(fleet.FleetError, match="HPC_FLEET_MAX must be a whole number from 1 to 1"):
            fleet.setting(environ, "HPC_FLEET_MAX")


# ------------------------------------------------------------------- (3) parallel ups with no loser left
@pytest.fixture
def bench(deploy, ca, tmp_path, monkeypatch):
    return make_bench(deploy, tmp_path, ca, monkeypatch)


def test_o68_the_fleet_gives_each_up_the_lock_it_shares_and_a_down_none(deploy, tmp_path):
    """O68: `hpcfed_process` runs an up with `HPC_FLEET_PLACEMENT_LOCK` naming `<fleet>/.placement.lock` and a down with
    none (an operator's own value never reaches either): the child is how an up knows it is a fleet's."""
    seen = []

    def run(argv, env, cwd, **kwargs):
        seen.append((argv[-1], dict(env), cwd))
        return SimpleNamespace(returncode=0)

    fleet = tmp_path / "fleet"
    (fleet / "fed-1" / "root").mkdir(parents=True)
    operate = deploy.fleet.hpcfed_process({"HPC_FLEET_PLACEMENT_LOCK": "/somewhere/else", "A": "1"}, run=run)
    for command in ("up", "down"):
        operate("fed-1", command, tmp_path / "layout.toml", fleet / "fed-1" / "root", tmp_path / f"{command}.log")
    (up, down) = seen
    assert up[1]["HPC_FLEET_PLACEMENT_LOCK"] == str(fleet / ".placement.lock")
    assert up[1]["A"] == "1"
    assert "HPC_FLEET_PLACEMENT_LOCK" not in down[1]


def test_o68_an_up_run_by_hand_takes_no_lock_and_is_tried_once(hpc, tmp_path, site):
    """O68: with no fleet around it (no lock in its environment) an up reads the account's live jobs twice, takes no
    lock and, placed on another federation's node, is refused once and never submitted again, exactly as before."""
    fake = FakeSlurm(others_by_read=[[], [OtherJob("9001", RIVAL, "RUNNING", ("node01", "node02"))]])
    rig = Rig(hpc, tmp_path, site, fake)
    assert rig.driver.placement_lock is None
    with pytest.raises(hpc.exclude.SharedNode, match=r"zone, providers\[0\] would run on a node another live"):
        rig.up()
    assert len(rig.sbatches()) == 2  # the services' job and the agent's: once
    assert rig.reads() == 2
    assert sorted(fake.cancelled) == [SERVICES, AGENT]
    assert not rig.lock.exists()


def test_o68_make_driver_reads_the_lock_from_the_environment_of_an_up_alone(hpc, tmp_path):
    """O68: the lock reaches the driver of an up (and describe) from `HPC_FLEET_PLACEMENT_LOCK`, never the driver of a
    down or a verb, and a value that is no absolute path refuses."""
    lock = str(tmp_path / ".placement.lock")
    root = str(tmp_path / "root")
    asked = hpc.cli.make_driver(SEPARATE_LAYOUT, root, runner=FakeSlurm(), environ={"HPC_FLEET_PLACEMENT_LOCK": lock})
    assert asked.placement_lock == Path(lock)
    down = hpc.cli.make_driver(SEPARATE_LAYOUT, root, runner=FakeSlurm(), environ={"HPC_FLEET_PLACEMENT_LOCK": lock},
                               agent=False)  # fmt: skip
    assert down.placement_lock is None
    with pytest.raises(ValueError, match="absolute path"):
        hpc.cli.make_driver(SEPARATE_LAYOUT, root, runner=FakeSlurm(), environ={"HPC_FLEET_PLACEMENT_LOCK": "lock"})


def test_o68_the_lock_is_held_around_the_two_reads_and_the_submits_and_nothing_else(hpc, tmp_path, site, monkeypatch):
    """O68: the whole of an up under a fleet takes the lock twice: around its first read of the account's live jobs with
    both submits, and around its second read; every read and every submit is inside one, and no step (the services, the
    core, the link check) is, so the rest of the up runs in parallel with the others'."""
    rig = Rig(hpc, tmp_path, site, lock=True)
    entered, seen = [], []
    real_lock = hpc.exclude.placement_lock

    def counted(path):
        entered.append(path)
        return real_lock(path)

    monkeypatch.setattr(hpc.exclude, "placement_lock", counted)
    real_run, real_start, real_pipe = rig.fake.run, rig.fake.start, rig.fake.pipe

    def run(argv, timeout):
        seen.append(("squeue --me" if argv[:2] == ["squeue", "--me"] else argv[0], lock_is_held(rig.lock)))
        return real_run(argv, timeout)

    def start(argv, data=None):
        seen.append(("step", lock_is_held(rig.lock)))
        return real_start(argv, data)

    def pipe(first, second):
        seen.append(("step", lock_is_held(rig.lock)))
        return real_pipe(first, second)

    rig.fake.run, rig.fake.start, rig.fake.pipe = run, start, pipe
    rig.up()
    assert entered == [rig.lock, rig.lock]
    kinds = [kind for kind, _ in seen]
    assert kinds.count("sbatch") == 2
    assert kinds.count("squeue --me") == 2
    assert "step" in kinds  # the services, the facts, the export: they ran
    assert all(held for kind, held in seen if kind in ("sbatch", "squeue --me"))
    assert not any(held for kind, held in seen if kind == "step")
    assert not lock_is_held(rig.lock)  # let go when the up ends


def rival_on_my_nodes(fake, attempts_on_rival):
    """The cluster's scheduler places the jobs of the first `attempts_on_rival` attempts on the rival's nodes whatever
    they exclude (as a scheduler that places before another job shows would), and the rival's job holds them."""
    nodes = tuple(f"node{i:02d}" for i in range(1, 8))
    fake.others = [OtherJob("9001", RIVAL, "RUNNING", nodes)]
    for attempt in range(attempts_on_rival):
        services, agent = (SERVICES, AGENT) if attempt == 0 else (str(4343 + 10 * (2 * attempt - 1)),
                                                                  str(4343 + 10 * (2 * attempt)))  # fmt: skip
        fake.node_lists[(services, 0)] = list(nodes[:6])
        fake.node_lists[(agent, 0)] = [nodes[6]]


def test_o68_an_up_placed_on_another_federations_node_is_released_and_tried_again(hpc, tmp_path, site):
    """O68: [fails before] under a fleet, an up whose jobs ran on the node of a federation that was pending at its
    first read is released (its jobs cancelled, by their ids) and tried again: the new submits exclude the nodes held by
    then, and the up ends on others. Two attempts of two jobs, the first two cancelled, a state that names the new
    ones."""
    fake = FakeSlurm(others_by_read=[[], [OtherJob("9001", RIVAL, "RUNNING", tuple(POOL[:7]))]])
    fake.nodes = list(POOL)
    rig = Rig(hpc, tmp_path, site, fake, lock=True)
    rig.up()
    first, second = rig.sbatches()[:2], rig.sbatches()[2:]
    assert not any(word.startswith("--exclude") for word in first[0])
    assert f"--exclude={','.join(POOL[:7])}" in second[0]  # the services' retried job keeps off the rival's nodes
    assert f"--exclude={','.join(POOL[:7])}" in second[1]  # and so does the agent's
    assert sorted(fake.cancelled) == sorted([SERVICES, AGENT])  # only the first attempt's jobs
    state = rig.state()
    assert [state["allocation"]["job"], state["sandbox"]["job"]] == ["4353", "4363"]
    assert state["allocation"]["excluded_nodes"] == 7
    assert not set(state["allocation"]["nodes"]) & set(POOL[:7])
    assert state["sandbox"]["node"] not in POOL[:7]
    assert state["phase"] == "linked"
    said = " ".join(rig.said)
    assert "trying again (2 of 3)" in said
    assert "node0" not in said


def test_o68_a_second_loss_is_tried_again_and_the_third_attempt_wins(hpc, tmp_path, site):
    """O68: two losses in a row leave the third attempt, the last, to run: it ends the up on others."""
    fake = FakeSlurm()
    fake.nodes = list(POOL)
    rival_on_my_nodes(fake, 2)
    rig = Rig(hpc, tmp_path, site, fake, lock=True)
    rig.up()
    assert len(rig.sbatches()) == 6
    assert len(fake.cancelled) == 4
    assert "trying again (3 of 3)" in " ".join(rig.said)
    assert rig.state()["phase"] == "linked"


def test_o68_the_retry_has_a_bound_and_ends_in_the_refusal_it_always_had(hpc, tmp_path, site):
    """O68: three attempts in all (`PLACEMENT_ATTEMPTS`): a third loss fails the up with the refusal's own words, every
    job of every attempt cancelled by its id and none left held, and no fourth submit."""
    assert hpc.driver.PLACEMENT_ATTEMPTS == 3
    fake = FakeSlurm()
    fake.nodes = list(POOL)
    rival_on_my_nodes(fake, 3)
    rig = Rig(hpc, tmp_path, site, fake, lock=True)
    with pytest.raises(hpc.exclude.SharedNode) as refused:
        rig.up()
    assert str(refused.value).startswith("zone, providers[0], providers[1], providers[2], providers[3], providers[4], ")
    assert "would run on a node another live federation of this account holds, and two federations never share one" in (
        str(refused.value)
    )
    assert "no service started, and the held job(s) 4373, 4383 were cancelled" in str(refused.value)
    assert len(rig.sbatches()) == 6
    assert len(fake.cancelled) == 6
    assert set(fake.cancelled) == set(fake.submitted)
    assert fake.billing() == []


def test_o68_a_retry_that_dies_after_its_sbatch_leaves_down_to_find_the_job_by_name(hpc, tmp_path, site):
    """O68: the state of a later attempt names none of the jobs the attempt before cancelled: an up that dies after a
    retry's sbatch and before it records the new id leaves `down` a state that names no job, so `down` finds that job by
    its name, as for a first attempt, and cancels it, and does not look up a cancelled id and stop there."""
    fake = FakeSlurm(others_by_read=[[], [OtherJob("9001", RIVAL, "RUNNING", tuple(POOL[:7]))]])
    fake.nodes = list(POOL)
    rig = Rig(hpc, tmp_path, site, fake, lock=True)
    real, submitted = rig.driver.slurm.submit, []

    def dying(asked):
        submitted.append(real(asked))
        if len(submitted) == 3:  # the retry's services job: accepted by Slurm, never recorded
            raise hpc.slurm.SlurmError("the process died")
        return submitted[-1]

    rig.driver.slurm.submit = dying
    with pytest.raises(hpc.slurm.SlurmError, match="the process died"):
        rig.up()
    assert submitted == [SERVICES, AGENT, "4353"]
    state = rig.state()
    assert [state["allocation"]["job"], state["sandbox"]["job"]] == [None, None]
    assert state["allocation"]["readback"] is None
    assert fake.billing() == ["4353"]  # the replacement runs, and no state file names it
    rig.driver.down()
    assert fake.billing() == []
    assert "4353" in fake.cancelled
    assert not (tmp_path / "fed" / "root").exists()


def test_o68_a_release_whose_cancel_failed_is_not_tried_again(hpc, tmp_path, site):
    """O68: a loser whose cancel failed still holds a job `down` must find by the state's ids, which a resubmit would
    overwrite: it is refused as before, with `run down` in the message, and never submitted over."""
    fake = FakeSlurm(others_by_read=[[], [OtherJob("9001", RIVAL, "RUNNING", ("node01", "node02"))]])
    rig = Rig(hpc, tmp_path, site, fake, lock=True)

    def refusing(job):
        raise hpc.slurm.SlurmError("scancel failed")

    rig.driver.slurm.cancel = refusing
    with pytest.raises(hpc.exclude.SharedNode, match="failed: run down to release them") as refused:
        rig.up()
    assert refused.value.released is False
    assert len(rig.sbatches()) == 2


def test_o68_a_second_read_that_fails_is_no_reason_to_try_again(hpc, tmp_path, site):
    """O68: only a placement on another's node is retried: a second read that cannot be taken refuses the up, its jobs
    released, as it always did, and nothing is submitted again."""
    fake = FakeSlurm()
    real = fake.run

    def unreadable(argv, timeout):
        if argv[:2] == ["squeue", "--me"] and fake.me_reads >= 1:
            return hpc.slurm.Completed(1, "", "squeue: error: Socket timed out\n")
        return real(argv, timeout)

    fake.run = unreadable
    rig = Rig(hpc, tmp_path, site, fake, lock=True)
    with pytest.raises(hpc.slurm.SlurmError, match="could not be read") as refused:
        rig.up()
    assert not isinstance(refused.value, hpc.exclude.SharedNode)
    assert len(rig.sbatches()) == 2


@needs_openssl
def test_o68_two_parallel_ups_whose_pending_jobs_land_on_one_node_both_end_on_distinct_nodes(bench, keep_umask):
    """O68: [fails before] two members brought up at once whose jobs both pended at the other's first read, then ran
    together on the same nodes: whichever takes the lock second reads the first's jobs and passes, the first is
    released and tried again, and both federations end linked on nodes of their own. Nothing of the other's, and no job
    of a third, is cancelled."""
    bench.pool = POOL
    first, second = bench.fake("fed-1"), bench.fake("fed-2")  # made now: they share the cluster
    first.state = second.state = "PENDING"
    start = threading.Barrier(2, timeout=60, action=lambda: [setattr(f, "state", "RUNNING") for f in (first, second)])
    waited = set()

    def sleeping(label):
        def wait(seconds):  # each up polling its pending job: once both are, their jobs start together
            if label not in waited:
                waited.add(label)
                start.wait()

        return wait

    bench.sleeping = sleeping
    code, printed = bench.up(2, environ={**bench.environ, "HPC_FLEET_PARALLEL": "2"})
    assert code == 0, printed
    assert not start.broken
    states = {
        label: json.loads((bench.root(label) / "hpc-federation.json").read_text("utf-8")) for label in bench.fakes
    }
    assert {s["phase"] for s in states.values()} == {"linked"}
    nodes = {label: set(state["allocation"]["nodes"]) | {state["sandbox"]["node"]} for label, state in states.items()}
    assert len(nodes["fed-1"]) == len(nodes["fed-2"]) == 7
    assert not nodes["fed-1"] & nodes["fed-2"]
    submitted = sorted(len(fake.submitted) for fake in bench.fakes.values())
    assert submitted == [2, 4]  # exactly one of them lost and was tried again
    loser, winner = sorted(bench.fakes, key=lambda label: -len(bench.fake(label).submitted))
    assert sorted(bench.fake(loser).cancelled) == sorted(list(bench.fake(loser).submitted)[:2])
    assert bench.fake(winner).cancelled == []  # nothing of the winner's was touched
    again = bench.fake(loser).submitted[list(bench.fake(loser).submitted)[2]][0]
    assert set(again["--exclude"].split(",")) == nodes[winner]
    assert [m["status"] for m in record(bench)["members"]] == ["up", "up"]


@needs_openssl
def test_o68_a_member_placed_on_another_members_nodes_three_times_fails_as_before(bench):
    """O68: every attempt of fed-2 is placed on the nodes fed-1 holds (a scheduler that ignores the exclusion): its
    third refusal is a failed member, reported with the command that releases it, fed-1 stays up and none of its jobs
    is cancelled, and every job fed-2 submitted is cancelled by its id."""
    bench.pool = POOL
    first, second = bench.fake("fed-1"), bench.fake("fed-2")
    for services, agent in (("4200", "4250"), ("4260", "4270"), ("4280", "4290")):  # fed-2's three attempts' ids
        second.node_lists[(services, 0)] = POOL[:6]
        second.node_lists[(agent, 0)] = [POOL[6]]
    code, printed = bench.up(2)
    assert code == 1, printed
    assert [m["status"] for m in record(bench)["members"]] == ["up", "failed"]
    assert first.cancelled == []
    assert len(second.submitted) == 6
    assert sorted(second.cancelled) == sorted(second.submitted)
    log = (bench.fleet / "logs" / "fed-2.up.log").read_text(encoding="utf-8")
    assert log.count("trying again") == 2
    assert "two federations never share one" in log
    assert "fed-2: up FAILED" in printed
    assert "node0" not in log
