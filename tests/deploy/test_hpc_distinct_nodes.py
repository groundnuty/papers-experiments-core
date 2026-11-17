"""Two federations of one account never share a node (deploy/hpc-federation/hpcexclude.py, DESIGN.md section 1, "Two
federations of one account"; README.md, "Two federations of one account"; issue 398).

A federation's services run with no network namespace and Couchbase's guard watches the fixed port 8091, so two
federations' services on one node would fail the second one's `up`. `up` reads the account's live federation jobs
before it submits and excludes their nodes, and reads them again once its job runs and before any step: a service on
another federation's node (two ups that raced) is refused and the job released. Both reads are read-only, an unreadable
one refuses, and nothing a message says names a host.

Slurm is the fake of hpc_fed_support, whose `squeue --me` lists this fake's own jobs, its peers' and `OtherJob`s."""

import os
import stat

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, OtherJob, agent_site, hpc_on_path
from hpc_fleet_support import SEPARATE_LAYOUT, fleet_modules, job_ids, make_bench, make_ca
from tls_support import HAS_OPENSSL

# no `-t`: squeue's own default lists every job that holds a node; `--all`: in every partition, hidden ones included
ME = ["squeue", "--me", "-h", "--all", "-o", "%i %j %T %N"]
OTHER = "hpcop-another-study-1a2b3c4d"  # another federation's held job: hpcop-<project>-<hex>
POOL = [f"node{i:02d}" for i in range(1, 41)]  # a cluster big enough for several federations' seven nodes each


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
    """A driver over the fake Slurm, the core's phase recorded instead of run; `said` is what `up` told the operator."""

    def __init__(self, hpc, tmp_path, site, layout=LAYOUT, fake=None):
        self.hpc = hpc
        self.fake = fake or FakeSlurm()
        self.root = tmp_path / "fed" / "root"
        self.site = site
        self.said: list = []
        self.driver = hpc.cli.make_driver(layout, str(self.root), runner=self.fake)
        self.driver.say = self.said.append
        self.driver.sleep = lambda s: None
        self.driver.poll_s = 0.0
        self.driver.slurm.sleep = lambda s: None
        self.driver.slurm.poll_s = 0.0
        self.driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        self.driver.relay_available = lambda: False

    def up(self):
        return self.driver.up(self.hpc.settings.site_from_environment(self.site))

    def state(self):
        return self.driver.store.read()

    def calls(self, tool):
        return [c for c in self.fake.calls if c[0] == tool]


def names_no_node(text) -> bool:
    return "node0" not in str(text) and "node1" not in str(text)


# -- the prefix --------------------------------------------------------------------------------------------------


def test_o57_the_name_up_gives_its_jobs_is_the_prefix_the_live_read_matches(hpc, tmp_path, site):
    """O57: the one prefix names every held job `up` submits (the services', the agent's own under "separate") and is
    the one the live read matches, so a federation's job is never read as no federation's; the bundle's batch scripts
    carry it too."""
    for index, layout in enumerate((LAYOUT, SEPARATE_LAYOUT)):
        (tmp_path / str(index) / "fed").mkdir(parents=True)
        rig = Rig(hpc, tmp_path / str(index), site, layout)
        state = rig.driver.initial_state(hpc.settings.site_from_environment(site))
        assert state["allocation"]["name"].startswith(hpc.exclude.JOB_PREFIX)
        assert state["sandbox"]["name"].startswith(hpc.exclude.JOB_PREFIX)
    for script in ("zone", "provider"):
        text = (hpc.driver.HERE / "bundle" / f"{script}.sbatch").read_text(encoding="utf-8")
        assert f"--job-name={hpc.exclude.JOB_PREFIX}{script}" in text


# -- the read before the submit ----------------------------------------------------------------------------------


def test_o57_up_submits_with_the_nodes_of_the_accounts_other_live_federations_excluded(hpc, tmp_path, site):
    """O57: before it submits, `up` reads the account's live jobs (`squeue --me`, read-only) and excludes every node of
    every job that carries the driver's prefix, a pending job's none, a completing job's still, each component of a
    heterogeneous job's; a job of no federation's is none of its business. The held job lands on none of them, the
    count (never a name) is in the state and in what `up` said."""
    other = [
        OtherJob("5001", OTHER, "RUNNING", ("node01", "node02", "node03")),
        OtherJob("5002+0", "hpcop-het-5e6f7a8b", "RUNNING", ("node04",)),
        OtherJob("5002+1", "hpcop-het-5e6f7a8b", "RUNNING", ("node05",)),
        OtherJob("5003", "hpcop-waiting-9a0b1c2d", "PENDING"),
        OtherJob("5004", "hpcop-sep-3c4d5e6f-sandbox", "COMPLETING", ("node06",)),
        OtherJob("5005", "my analysis", "RUNNING", ("node07",)),
    ]
    rig = Rig(hpc, tmp_path, site, fake=FakeSlurm(others=other, nodes=POOL))
    rig.up()
    excluded = {f"node0{i}" for i in range(1, 7)}
    assert rig.fake.asked["--exclude"] == ",".join(sorted(excluded))
    nodes = rig.state()["allocation"]["nodes"]
    assert len(nodes) == 6
    assert not set(nodes) & excluded
    assert rig.state()["sandbox"]["node"] not in excluded  # the agent's node keeps off them too
    assert rig.state()["allocation"]["excluded_nodes"] == 6
    assert any("6 node(s)" in line and "excluded" in line for line in rig.said)
    assert all(names_no_node(line) for line in rig.said if "excluded" in line)
    assert rig.calls("squeue")[0] == ME  # read-only, and before anything is submitted
    order = [" ".join(c[:3]) if c[0] == "scontrol" else c[0] for c in rig.fake.calls]
    assert order.index("squeue") < order.index("sbatch") < order.index("scontrol show job")


def test_o57_every_component_and_job_of_the_up_excludes_the_same_nodes(hpc, tmp_path, site):
    """O57: the services' job, the agent's component of a heterogeneous job (named on each, so nothing depends on
    Slurm passing it on) and the agent's own job under "separate" all keep off the other federations' nodes: an agent's
    sandbox never runs on a node that hosts an Onedata service, another federation's included."""
    other = [OtherJob("5001", OTHER, "RUNNING", ("node01", "node02"))]
    for index, layout in enumerate((LAYOUT, SEPARATE_LAYOUT)):
        (tmp_path / str(index) / "fed").mkdir(parents=True)
        rig = Rig(hpc, tmp_path / str(index), site, layout, FakeSlurm(others=other, nodes=POOL))
        rig.up()
        asked = [word for argv in rig.calls("sbatch") for word in argv]
        assert asked.count("--exclude=node01,node02") == 2, layout.stem  # a component, or a job, each
        assert asked.count("--nodes=1") >= 1
        agent = rig.state()["sandbox"]["node"]
        assert agent not in ("node01", "node02"), layout.stem
        assert not set(rig.state()["allocation"]["nodes"]) & {"node01", "node02"}, layout.stem


def test_o57_up_with_no_other_federation_live_asks_for_no_exclusion(hpc, tmp_path, site):
    """O57: with no other federation's job live, the held job's argv holds no `--exclude`, as it did before the read
    existed, and the state records 0."""
    rig = Rig(hpc, tmp_path, site, fake=FakeSlurm(others=[OtherJob("5005", "my analysis", "RUNNING", ("node01",))]))
    rig.up()
    assert not any(word.startswith("--exclude") for argv in rig.calls("sbatch") for word in argv)
    assert rig.state()["allocation"]["excluded_nodes"] == 0
    assert any("nothing is excluded" in line for line in rig.said)


def test_o57_a_completing_job_with_no_node_left_holds_none_and_does_not_refuse_up(hpc, tmp_path, site):
    """O57: squeue lists a completing job's nodes not yet returned to service, so right after a `down` its list may be
    empty: that job holds no node, and an `up` straight after it (a fleet's `add`) goes on, excluding nothing."""
    rig = Rig(hpc, tmp_path, site, fake=FakeSlurm(others=[OtherJob("5001", OTHER, "COMPLETING")]))
    rig.up()
    assert not any(word.startswith("--exclude") for argv in rig.calls("sbatch") for word in argv)
    assert rig.state()["allocation"]["excluded_nodes"] == 0


@pytest.mark.parametrize(
    "rows",
    [
        ["5001 hpcop-x RUNNING"],  # a running job with no node list
        ["5001 hpcop-x"],  # no state
        ["5001 hpcop-x RUNNING node01 another"],  # more than four words
        ["5001 hpcop-x RUNNING node[01"],  # a node list that is not one
        ["5001 hpcop-x COMPLETING node[01"],  # a completing job's list that is there and cannot be read
        ["x hpcop-x RUNNING node01"],  # an id that is none
    ],
)
def test_o57_a_federation_job_listed_in_a_way_not_understood_refuses_before_any_submit(hpc, tmp_path, site, rows):
    """O57: a line of a job that carries the prefix and that the read cannot take apart refuses `up`, whatever it is:
    never a guess, never skipped."""
    fake = FakeSlurm(on_me=lambda read, listed: hpc.slurm.Completed(0, "".join(f"{row}\n" for row in rows), ""))
    rig = Rig(hpc, tmp_path, site, fake=fake)
    with pytest.raises(hpc.slurm.SlurmError, match="refusing to submit"):
        rig.up()
    assert not rig.calls("sbatch")
    assert not (rig.root / "hpc-federation.json").exists()


def test_o57_up_refuses_when_the_accounts_live_jobs_cannot_be_read_and_submits_nothing(hpc, tmp_path, site):
    """O57: fail closed: an unreadable `squeue --me` (a status other than 0, an error of the call itself) refuses `up`
    before anything is made or submitted, and the message says neither what squeue said nor a node."""
    stderr = "squeue: error: Unable to contact slurm controller on node03.cluster.example.org (connect failure)\n"
    refusals = {
        "status": FakeSlurm(on_me=lambda read, listed: hpc.slurm.Completed(1, "", stderr)),
        "timeout": FakeSlurm(on_me=lambda read, listed: (_ for _ in ()).throw(hpc.slurm.SlurmError(stderr))),
    }
    for kind, fake in refusals.items():
        (tmp_path / kind / "fed").mkdir(parents=True)
        rig = Rig(hpc, tmp_path / kind, site, fake=fake)
        with pytest.raises(hpc.slurm.SlurmError, match="refusing to submit") as refused:
            rig.up()
        assert names_no_node(refused.value), kind
        assert "controller" not in str(refused.value), kind
        assert not rig.calls("sbatch"), kind
        assert not (rig.root / "hpc-federation.json").exists(), kind
        assert rig.calls("squeue") == [ME], kind


# -- the read once the job runs ----------------------------------------------------------------------------------


def test_o57_a_service_placed_on_another_federations_node_is_refused_before_any_step_and_the_job_released(
    hpc, tmp_path, site
):
    """O57: a race: the other federation's job was still pending at the first read, so nothing was excluded, and it
    runs on the zone's node and on one provider's when this one's job runs. The second read, after the allocation and
    before the first step, refuses: it names the roles (the zone, providers[1]; never a host), cancels the held job
    by its id, and no step ran. `down` still finds the job gone and the root removed."""
    pending = OtherJob("5001", OTHER, "PENDING")
    running = OtherJob("5001", OTHER, "RUNNING", ("node01", "node03"))
    fake = FakeSlurm(others_by_read=[[pending], [running]])
    rig = Rig(hpc, tmp_path, site, fake=fake)
    with pytest.raises(hpc.slurm.SlurmError) as refused:
        rig.up()
    text = str(refused.value)
    assert "zone" in text
    assert "providers[1]" in text
    assert "providers[0]" not in text
    assert names_no_node(text)
    assert "5001" not in text
    assert OTHER not in text
    assert fake.cancelled == ["4242"]
    assert fake.sruns == []  # not a control step, not a service
    assert fake.spawned == []
    assert fake.me_reads == 2
    assert rig.state()["services"]["zone"]["node"] is None  # nothing was recorded as placed
    rig.driver.down()
    assert not rig.root.exists()


def test_o57_the_refusal_releases_both_held_jobs_when_the_agents_node_is_a_job_of_its_own(hpc, tmp_path, site):
    """O57: under "separate" the refusal cancels both held jobs, each by its own id, so neither keeps billing."""
    pending = OtherJob("5001", OTHER, "PENDING")
    running = OtherJob("5001", OTHER, "RUNNING", ("node02",))
    fake = FakeSlurm(others_by_read=[[pending], [running]])
    rig = Rig(hpc, tmp_path, site, SEPARATE_LAYOUT, fake)
    with pytest.raises(hpc.slurm.SlurmError, match=r"providers\[0\]"):
        rig.up()
    assert sorted(fake.cancelled) == ["4242", "4343"]
    assert fake.sruns == []


def test_o57_an_agent_placed_on_another_federations_node_is_refused_too(hpc, tmp_path, site):
    """O57: the agent's node is checked as the services' are: only it is on a node another federation holds (the
    seventh, the first six being this one's services), and the refusal names the agent and no service."""
    pending = OtherJob("5001", OTHER, "PENDING")
    running = OtherJob("5001", OTHER, "RUNNING", ("node07",))
    fake = FakeSlurm(others_by_read=[[pending], [running]])
    rig = Rig(hpc, tmp_path, site, fake=fake)
    with pytest.raises(hpc.slurm.SlurmError) as refused:
        rig.up()
    assert "agent" in str(refused.value)
    assert "zone" not in str(refused.value)
    assert "providers[" not in str(refused.value)
    assert fake.cancelled == ["4242"]
    assert fake.sruns == []


def test_o57_another_federations_agent_node_is_one_of_its_nodes_too(hpc, tmp_path, site):
    """O57: the other federation's job is read whole, its agent's node (a heterogeneous job's component 1, or the job
    of its own, named with `-sandbox`) included: the read is by the prefix, as the lead's design has it."""
    pending = OtherJob("5001", f"{OTHER}-sandbox", "PENDING")
    running = OtherJob("5001", f"{OTHER}-sandbox", "RUNNING", ("node01",))
    rig = Rig(hpc, tmp_path, site, fake=FakeSlurm(others_by_read=[[pending], [running]]))
    with pytest.raises(hpc.slurm.SlurmError, match="zone"):
        rig.up()


def test_o57_this_federations_own_jobs_are_never_another_federations(hpc, tmp_path, site):
    """O57: the second read lists this federation's own jobs too (a heterogeneous job as `<leader>+0` and `+1`, the
    agent's own job as a second id under "separate"): none refuses it, and a job of no federation's never does."""
    for index, layout in enumerate((LAYOUT, SEPARATE_LAYOUT)):
        (tmp_path / str(index) / "fed").mkdir(parents=True)
        fake = FakeSlurm(others=[OtherJob("5005", "my analysis", "RUNNING", ("node01", "node02"))])
        rig = Rig(hpc, tmp_path / str(index), site, layout, fake)
        rig.up()
        assert fake.me_reads == 2
        assert rig.state()["phase"] == "linked"
        assert rig.state()["allocation"]["excluded_nodes"] == 0


def test_o57_a_second_read_that_fails_refuses_and_releases_the_job_too(hpc, tmp_path, site):
    """O57: fail closed after the allocation as before it: with the jobs unreadable once this one runs, nothing proves
    its nodes are its own, so it is refused, released and no step runs; the message names no host."""
    stderr = "squeue: error: slurm_load_jobs error: Socket timed out on node03\n"
    fake = FakeSlurm(on_me=lambda read, listed: hpc.slurm.Completed(1, "", stderr) if read == 2 else None)
    rig = Rig(hpc, tmp_path, site, fake=fake)
    with pytest.raises(hpc.slurm.SlurmError) as refused:
        rig.up()
    assert names_no_node(refused.value)
    assert "Socket" not in str(refused.value)
    assert fake.cancelled == ["4242"]
    assert fake.sruns == []


# -- a job the exclusion keeps waiting -------------------------------------------------------------------------


def test_o57_a_job_the_exclusion_keeps_pending_is_cancelled_at_the_queue_bound_and_the_message_says_why(
    hpc, tmp_path, site
):
    """O57: Slurm keeps a job pending when the exclusion leaves too few nodes; `up` waits no longer than its queue
    bound for it (the one it always had), cancels it by its id and says that the nodes of other federations were
    excluded, how many, and never which."""
    other = [OtherJob("5001", OTHER, "RUNNING", ("node01", "node02"))]
    fake = FakeSlurm(state="PENDING", others=other)
    rig = Rig(hpc, tmp_path, site, fake=fake)
    clock = iter(range(0, 10**7, 100))
    rig.driver.slurm.clock = lambda: next(clock)
    with pytest.raises(hpc.slurm.SlurmError, match="did not start within 1800 s") as refused:
        rig.up()
    text = str(refused.value)
    assert "excluded 2 node(s) of other live federations" in text
    assert names_no_node(text)
    assert fake.cancelled == ["4242"]


def test_o57_a_job_that_waits_with_nothing_excluded_is_not_blamed_on_the_exclusion(hpc, tmp_path, site):
    """O57: without an exclusion, the queue bound's message is what it was."""
    fake = FakeSlurm(state="PENDING")
    rig = Rig(hpc, tmp_path, site, fake=fake)
    clock = iter(range(0, 10**7, 100))
    rig.driver.slurm.clock = lambda: next(clock)
    with pytest.raises(hpc.slurm.SlurmError) as refused:
        rig.up()
    assert str(refused.value).endswith("it was cancelled")


def test_o57_a_submit_the_scheduler_refuses_says_what_was_excluded_and_names_no_node(hpc, tmp_path, site):
    """O57: a scheduler that refuses the request because the exclusion leaves no node that fits fails `sbatch`: the
    failure says the request excluded 2 node(s) of other federations, and a node Slurm's own words repeat is not
    repeated."""
    other = [OtherJob("5001", OTHER, "RUNNING", ("node01", "node02"))]
    rig = Rig(hpc, tmp_path, site, fake=FakeSlurm(others=other))
    real = rig.fake.run

    def refusing(argv, timeout):
        if argv[0] == "sbatch":
            return hpc.slurm.Completed(1, "", "sbatch: error: node01,node02 are excluded: no such configuration\n")
        return real(argv, timeout)

    rig.fake.run = refusing
    with pytest.raises(hpc.slurm.SlurmError, match="sbatch failed with status 1") as refused:
        rig.up()
    text = str(refused.value)
    assert "excluded 2 node(s) of other live federations" in text
    assert names_no_node(text)


# -- a fleet ----------------------------------------------------------------------------------------------------

needs_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="the zone's CA is made with openssl")


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules() as found:
        yield found


@pytest.fixture(scope="module")
def ca(tmp_path_factory):
    return make_ca(tmp_path_factory.mktemp("ca"))


@needs_openssl
def test_o57_a_fleets_members_keep_their_services_off_the_nodes_of_the_members_already_up(
    deploy, ca, tmp_path, monkeypatch
):
    """O57: a fleet's members are federations of one account on one cluster: each member's `up` reads the others' live
    jobs (the cluster here is one pool the members' fake Slurms share) and excludes their nodes, so fed-2's nodes,
    its agent's included, are none of fed-1's, nor fed-3's any of the first two's; the count is in each member's state
    and in its log."""
    import json

    bench = make_bench(deploy, tmp_path, ca, monkeypatch)
    bench.pool = POOL
    code, printed = bench.up(3)
    assert code == 0, printed
    taken: set = set()
    for index, label in enumerate(("fed-1", "fed-2", "fed-3")):
        services, agent = job_ids(label)
        fake = bench.fake(label)
        asked = set(filter(None, fake.submitted[services][0].get("--exclude", "").split(",")))
        assert asked == taken, label
        nodes = set(fake.placed(services, 0))
        assert len(nodes) == 6, label
        assert not nodes & taken, label
        assert not set(fake.placed(agent, 0)) & taken, label  # an agent never runs beside another federation's node
        recorded = json.loads((bench.root(label) / "hpc-federation.json").read_text(encoding="utf-8"))
        assert recorded["allocation"]["excluded_nodes"] == len(taken)
        log = (bench.fleet / "logs" / f"{label}.up.log").read_text(encoding="utf-8")
        said = [line for line in log.splitlines() if "node(s)" in line and "excluded" in line]
        assert bool(said) == bool(index), label
        assert all(names_no_node(line) for line in said), label
        taken |= nodes | set(fake.placed(agent, 0))
    assert names_no_node(printed)


@needs_openssl
def test_o57_two_members_that_raced_and_share_a_node_leave_the_later_one_refused_and_the_rest_up(
    deploy, ca, tmp_path, monkeypatch
):
    """O57: a fleet that raced: fed-2's job ran on a node fed-1's already held and nothing was excluded (fed-1's job
    was pending at fed-2's first read). fed-2's up is refused and its job cancelled, as a failed member of a fleet is
    reported, and fed-1 stays up."""
    bench = make_bench(deploy, tmp_path, ca, monkeypatch)
    bench.pool = POOL
    first = bench.fake("fed-1")
    second = bench.fake("fed-2")
    first_seen = second.me_rows  # what fed-2 reads: fed-1's job pending on the first read, running on the second
    reads = {"n": 0}

    def rows():
        reads["n"] += 1
        return [] if reads["n"] == 1 else first_seen()

    second.me_rows = rows
    code, printed = bench.up(2)
    assert code != 0
    assert sorted(second.cancelled) == sorted(job_ids("fed-2"))  # its services' job and its agent's own
    assert first.cancelled == []
    log = (bench.fleet / "logs" / "fed-2.up.log").read_text(encoding="utf-8")
    assert "zone" in log  # the member's own log has the refusal; the fleet's line is the log's last
    assert names_no_node(log)
    assert names_no_node(printed)


# -- what the guard reads, and how (a third Codex pass, 17 Nov 2026) --------------------------------------------


# a job of another federation that holds its nodes in a state other than running: suspended, a stop sent to it, a signal
# pending to it, staging out, configuring. squeue lists each by default; a `-t` of base state names would not list the
# flagged ones (STOPPED, SIGNALING), which hold their node and memory as a running job does
HOLDING = ["SUSPENDED", "STOPPED", "SIGNALING", "STAGE_OUT", "CONFIGURING"]


@pytest.mark.parametrize("state", HOLDING)
def test_o57_a_federation_job_that_holds_its_nodes_in_any_state_is_read_and_its_nodes_excluded(
    hpc, tmp_path, site, state
):
    """O57 (a third Codex pass, 17 Nov 2026, and the review of its fix): the guard's read names no state, so squeue's
    own default applies, which lists a suspended job, and a running one with a stop or a signal pending, as it lists a
    plain running one (an explicit list of state names, tried first, listed fewer: a job that was stopped or about to
    be signalled would have gone unseen). Each such job's nodes are excluded from a second `up`."""
    other = [OtherJob("5001", OTHER, state, ("node01", "node02"))]
    rig = Rig(hpc, tmp_path, site, fake=FakeSlurm(others=other, nodes=POOL))
    rig.up()
    assert rig.fake.asked["--exclude"] == "node01,node02"
    assert not set(rig.state()["allocation"]["nodes"]) & {"node01", "node02"}
    assert rig.calls("squeue")[0] == ME
    assert "-t" not in rig.calls("squeue")[0]


def test_o57_a_job_that_has_ended_holds_no_node_and_is_not_excluded(hpc, tmp_path, site):
    """O57: a finished job is not listed by squeue's default, so the nodes it ran on are free for a second `up`."""
    other = [OtherJob("5001", OTHER, "CANCELLED", ("node01", "node02"))]
    rig = Rig(hpc, tmp_path, site, fake=FakeSlurm(others=other, nodes=POOL))
    rig.up()
    assert not any(word.startswith("--exclude") for argv in rig.calls("sbatch") for word in argv)


@pytest.mark.parametrize("state", ["SUSPENDED", "STOPPED"])
def test_o57_a_service_placed_on_the_node_of_a_job_in_such_a_state_is_refused_after_the_start(
    hpc, tmp_path, site, state
):
    """O57: the second read, once the job runs and before any step, sees such a job on a service's node as it sees a
    running one: the service is refused, naming its role, and the held job is released."""
    other = OtherJob("5001", OTHER, state, ("node01",))
    fake = FakeSlurm(others_by_read=[[], [other]])  # not there at the first read: it raced the second `up`
    rig = Rig(hpc, tmp_path, site, fake=fake)
    with pytest.raises(RuntimeError, match=r"zone"):
        rig.up()
    assert names_no_node(" ".join(rig.said))


def scripts_on_the_path(tmp_path, monkeypatch) -> None:
    """An invented `squeue` and `scontrol` on the PATH, as the real Runner will find them: squeue lists the federation's
    job only when no SQUEUE_* variable and no output-format variable reaches it and the site's SLURM_CONF does (the
    scrub removes the former and keeps the latter); scontrol expands a node list as a word per name."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    squeue = (
        '#!/bin/sh\nenv | grep -q "^SQUEUE_" && exit 0\n'
        'env | grep -q "^SLURM_TIME_FORMAT=" && exit 0\nenv | grep -q "^SLURM_BITSTR_LEN=" && exit 0\n'
        '[ "$SLURM_CONF" = "/invented/slurm.conf" ] || exit 0\n'
        'case " $* " in *" --all "*) ;; *) exit 0 ;; esac\necho "5001 hpcop-x RUNNING node01"\n'
    )
    scontrol = "#!/bin/sh\necho node01\n"
    for name, text in (("squeue", squeue), ("scontrol", scontrol)):
        (bin_dir / name).write_text(text, encoding="utf-8")
        (bin_dir / name).chmod(stat.S_IRWXU)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("SLURM_CONF", "/invented/slurm.conf")


@pytest.mark.parametrize(
    "variable",
    ["SQUEUE_NAMES", "SQUEUE_STATES", "SQUEUE_ACCOUNT", "SQUEUE_FORMAT", "SLURM_TIME_FORMAT", "SLURM_BITSTR_LEN"],
)
def test_o57_the_guards_squeue_does_not_inherit_the_login_shells_own_defaults(hpc, tmp_path, monkeypatch, variable):
    """O57 (Codex, 17 Nov 2026): squeue takes defaults from SQUEUE_* variables (`SQUEUE_NAMES=unrelated` hides every
    federation job) and its output format from a few SLURM_* ones, and the guard's read inherited the shell's, so it
    could fail open without a word. The commands the driver runs on the login node get an environment without them, and
    keep the rest of it (the site's own SLURM_CONF among it): the real runner, an invented squeue."""
    import hpcexclude

    scripts_on_the_path(tmp_path, monkeypatch)
    monkeypatch.setenv(variable, "unrelated")
    jobs = hpcexclude.live_jobs(hpc.slurm.Slurm(hpc.slurm.Runner()))
    assert [(job.job, job.name, job.nodes) for job in jobs] == [("5001", "hpcop-x", ("node01",))]


def test_o57_the_runner_passes_the_rest_of_the_environment_on_unchanged(hpc, monkeypatch):
    """O57: only the variables that narrow what squeue lists, or change the form it prints in, are left out of a
    command's environment; a variable of the site (SLURM_CONF, SBATCH_*, PATH) reaches it, and so does SQUEUE_ALL, which
    only widens what is listed (jobs of hidden partitions) and is what a site that hides its partition relies on for
    every squeue read of the driver, not the guard's alone."""
    for name, value in (("SQUEUE_NAMES", "x"), ("SLURM_TIME_FORMAT", "relative"), ("SBATCH_ACCOUNT", "acct"),
                        ("SLURM_CONF", "/invented/slurm.conf"), ("SQUEUE_ALL", "1")):  # fmt: skip
        monkeypatch.setenv(name, value)
    seen = dict(
        line.split("=", 1) for line in hpc.slurm.Runner().run(["/usr/bin/env"], 10).stdout.splitlines() if "=" in line
    )
    assert "SQUEUE_NAMES" not in seen
    assert "SLURM_TIME_FORMAT" not in seen
    assert seen["SBATCH_ACCOUNT"] == "acct"
    assert seen["SLURM_CONF"] == "/invented/slurm.conf"
    assert seen["SQUEUE_ALL"] == "1"
    assert seen["PATH"] == os.environ["PATH"]


def test_o57_a_pending_federation_job_whose_node_list_cannot_be_expanded_refuses_naming_the_job(hpc, tmp_path, site):
    """O57 (Codex, 17 Nov 2026): a pending job whose node list is given and cannot be expanded counted as holding no
    node, so its nodes were not excluded, against the guard's rule that a read that cannot be taken apart refuses. It
    refuses before anything is submitted, naming the job (its own name) and no node; a pending job with no list is
    still no node."""
    fake = FakeSlurm(on_me=lambda read, listed: hpc.slurm.Completed(0, "5001 hpcop-x PENDING node[01\n", ""))
    rig = Rig(hpc, tmp_path, site, fake=fake)
    with pytest.raises(hpc.slurm.SlurmError, match=r"hpcop-x.*could not be expanded.*refusing to submit") as refused:
        rig.up()
    assert names_no_node(refused.value)
    assert not rig.calls("sbatch")
    assert not (rig.root / "hpc-federation.json").exists()


@pytest.mark.parametrize("listed", ["", "(null)"], ids=["no list", "null list"])
def test_o57_a_pending_federation_job_with_no_node_list_holds_none_and_does_not_refuse(hpc, tmp_path, site, listed):
    """O57: a pending job that has no list holds nothing: `up` goes on, excluding nothing."""
    row = f"5001 hpcop-x PENDING {listed}".rstrip()
    fake = FakeSlurm(on_me=lambda read, rows: hpc.slurm.Completed(0, row + "\n", ""))
    rig = Rig(hpc, tmp_path, site, fake=fake)
    rig.up()
    assert rig.state()["allocation"]["excluded_nodes"] == 0
