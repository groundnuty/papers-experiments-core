"""The agent's node of an HPC federation (deploy/hpc-federation, DESIGN.md sections 1, 2, 4 and 5): sized on its own,
from the layout's [hpc] table or the agent's limits in its platform template, and held either as the last component
of a heterogeneous job ([hpc] sandbox_placement = "het", the default) or as a held job of its own ("separate"). Both
components, or both jobs, are read back, every step on the agent's node runs in its own component or job, the state
file records where the agent's node is (`sandbox.node`, M5's shard driver reads it), and down cancels both after a
whole or a partial up, never a job that is not ours. Slurm is the fake of hpc_fed_support."""

import itertools
import json
import os
from pathlib import Path

import pytest
from hpc_fed_support import (
    LAYOUT,
    PILOT_TEMPLATE,
    SITE,
    FakeProcess,
    FakeSlurm,
    agent_site,
    hpc_on_path,
    services_only,
    without_sandbox,
)

PROVIDERS = ["aspen", "larch", "rowan", "alder", "hazel"]
CONTROL = 'control = { cpus = 1, memory = "1g" }'
PLACEMENT = 'sandbox_placement = "het"'
AGENT = 'sandbox = { cpus = 8, memory = "17g" }'
GIB = 1 << 30


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


def agent_layout(
    folder: Path, placement: str = "het", template_extra: str = "", hpc_extra: str = AGENT, rename=("", "")
) -> Path:
    """The pilot-shape layout with the agent's node and its placement, beside its platform template; `rename` gives a
    provider another name in both."""
    folder.mkdir()
    text = LAYOUT.read_text(encoding="utf-8")
    assert CONTROL in text
    assert PLACEMENT in text  # the pilot's layout records the coordinator's choice
    text = text.replace(PLACEMENT, f'sandbox_placement = "{placement}"')
    text = text.replace(CONTROL, f"{CONTROL}\n{hpc_extra}") if hpc_extra else text
    template = LAYOUT.parent / "pilot-shape.platform.toml.template"
    plate = template.read_text(encoding="utf-8")
    plate = without_sandbox(plate) + template_extra if template_extra else plate  # the extra's [sandbox] in its place
    if rename[0]:
        text, plate = text.replace(*rename), plate.replace(*rename)
    (folder / LAYOUT.name).write_text(text, encoding="utf-8")
    (folder / template.name).write_text(plate, encoding="utf-8")
    return folder / LAYOUT.name


def apptainer_limits(cpus: int = 8, memory: int = 8 * GIB, work: int = 4 * GIB, tmp: int = GIB) -> str:
    """A platform template's tail declaring the Apptainer sandbox and the agent's limits (M5's [sandbox])."""
    return (
        '\n[sandbox]\nbackend = "apptainer"\nwork_root = "/tmp/agent"\n[sandbox.limits]\n'
        f"cpus = {cpus}\nmemory_bytes = {memory}\nwork_disk_bytes = {work}\ntmp_bytes = {tmp}\n"
        "pids = 512\nnofile = 1024\nshm_bytes = 1073741824\n"
    )


class Rig:
    """A driver over the fake Slurm, the core's phase recorded instead of run."""

    def __init__(self, hpc, tmp_path, site, placement="het", fake=None, rename=("", "")):
        self.hpc = hpc
        self.fake = fake or FakeSlurm()
        self.root = tmp_path / "fed" / "root"
        self.site = site
        self.layout = agent_layout(tmp_path / "layout", placement, rename=rename)
        self.driver = hpc.cli.make_driver(self.layout, str(self.root), runner=self.fake)
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

    def sbatches(self):
        return [c for c in self.fake.calls if c[0] == "sbatch"]


# -- the [hpc] table --------------------------------------------------------------------------------------------


def test_the_agents_node_has_its_own_shape_and_the_service_nodes_keep_theirs(hpc):
    """O57: the agent's node is sized on its own (its step plus the control reserve), held as a heterogeneous job's
    component by default; the service nodes keep the larger service step plus the reserve."""
    settings = hpc.settings.load_settings({"sandbox": {"cpus": 8, "memory": "17g"}})
    assert settings.sandbox_placement == "het"
    assert settings.node() == hpc.settings.Size(5, 11264)
    assert settings.sandbox_node() == hpc.settings.Size(9, 18432)
    assert hpc.settings.load_settings(None).sandbox_node() is None


def test_the_agents_node_bills_at_its_own_shape(hpc):
    """O57: with a memory weight of 0.5, the pilot shape's six service nodes bill 6 x 5.5 and the agent's 9-core 18 GB
    node 9: 42 per hour, against 7 x 9 = 63 for one uniform 18 GB shape."""
    settings = hpc.settings.load_settings({"sandbox": {"cpus": 8, "memory": "17g"}})
    assert settings.billing_per_hour(7, 0.5) == 6 * 5.5 + 9 == 42


def test_a_placement_other_than_het_or_separate_is_refused(hpc):
    """O57: sandbox_placement is "het" or "separate"; anything else is refused, naming the key."""
    with pytest.raises(hpc.settings.SettingsError, match=r"hpc\.sandbox_placement"):
        hpc.settings.load_settings({"sandbox_placement": "uniform"})


def test_the_agents_node_is_derived_from_the_agents_limits_in_the_template(hpc, tmp_path):
    """O57: a platform template declaring the Apptainer sandbox gives the agent's limits: its node's step is the
    agent's cores plus 2 and its memory, work and tmp bytes plus 4 GiB (8 cores and 8 + 4 + 1 GiB: 10 cores, 17 GiB)."""
    template = tmp_path / "platform.toml.template"
    head = 'zone = { url = "@zone_url@" }\n[tokens]\nmargin_s = @hpc_token_margin_s@\n'
    template.write_text(head + apptainer_limits(), encoding="utf-8")
    agent = hpc.settings.agent_limits(template)
    assert agent == hpc.settings.AgentLimits(8.0, 8 * GIB, 4 * GIB, GIB)
    assert hpc.settings.load_settings({}, agent).sandbox == hpc.settings.Size(10, 17 * 1024)


def test_a_declared_agents_node_below_the_agents_limits_is_refused(hpc):
    """O57: an [hpc] sandbox smaller than the agent's limits need is refused, naming the key and the least size."""
    agent = hpc.settings.AgentLimits(8.0, 8 * GIB, 4 * GIB, GIB)
    with pytest.raises(hpc.settings.SettingsError, match=r"hpc\.sandbox: is below .* 10 cores"):
        hpc.settings.load_settings({"sandbox": {"cpus": 8, "memory": "17g"}}, agent)
    assert hpc.settings.load_settings({"sandbox": {"cpus": 12, "memory": "20g"}}, agent).sandbox.cpus == 12


def test_a_template_without_the_apptainer_sandbox_gives_no_agents_limits(hpc, tmp_path):
    """O57: a template with no [sandbox], or one whose sandbox is Docker's, declares no agent's node on HPC."""
    bare = tmp_path / "bare.toml.template"
    bare.write_text(without_sandbox(PILOT_TEMPLATE.read_text(encoding="utf-8")), encoding="utf-8")
    assert hpc.settings.agent_limits(bare) is None
    docker = tmp_path / "docker.toml.template"
    docker.write_text(apptainer_limits().replace('"apptainer"', '"docker"'), encoding="utf-8")
    assert hpc.settings.agent_limits(docker) is None


def test_the_pilot_layout_holds_the_agents_node_its_template_sizes_as_a_het_component(hpc):
    """O57, O63: the pilot's layout records sandbox_placement = "het" in its [hpc] table, and its template declares the
    Apptainer sandbox (6 cores and 8 + 4 + 1 GiB), so it holds the agent's node at the least step for those limits: 8
    cores and 17 GiB, the 18 GB node the billing is reckoned with."""
    import fedspec

    spec = fedspec.load_spec(LAYOUT)
    assert spec.hpc["sandbox_placement"] == "het"
    agent = hpc.settings.agent_limits(spec.platform_template)
    assert agent == hpc.settings.AgentLimits(6.0, 8 * GIB, 4 * GIB, GIB)
    settings = hpc.settings.load_settings(spec.hpc, agent)
    assert (settings.sandbox_placement, settings.sandbox) == ("het", hpc.settings.Size(8, 17 * 1024))
    assert settings.sandbox_node() == hpc.settings.Size(9, 18 * 1024)


def test_the_layouts_template_gives_the_driver_the_agents_node(hpc, tmp_path):
    """O57: make_driver reads the agent's limits from the layout's own template, so a layout that declares the
    Apptainer sandbox gets its node without an [hpc] sandbox."""
    (tmp_path / "fed").mkdir()
    layout = agent_layout(tmp_path / "layout", template_extra=apptainer_limits(), hpc_extra="")
    driver = hpc.cli.make_driver(layout, str(tmp_path / "fed" / "root"), runner=FakeSlurm())
    assert driver.settings.sandbox == hpc.settings.Size(10, 17 * 1024)


@pytest.mark.parametrize(
    "tail", [apptainer_limits(memory=64 * GIB), "\nnot = toml = at all\n"], ids=["limits outgrew it", "not toml"]
)
def test_down_never_reads_the_agents_limits_so_an_edited_template_cannot_keep_a_job_billing(hpc, tmp_path, site, tail):
    """O57: the agent's limits are read for up and describe alone: once a federation is up, a template whose limits
    outgrew [hpc] sandbox (or that stopped parsing) makes describe refuse, while down still cancels the held job and
    removes the root."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    template = rig.layout.parent / "pilot-shape.platform.toml.template"
    template.write_text(without_sandbox(template.read_text(encoding="utf-8")) + tail, encoding="utf-8")
    words = ["--layout", str(rig.layout), "--root", str(rig.root)]
    assert hpc.cli.main([*words, "describe"], {}, runner=rig.fake) == 2
    assert hpc.cli.main([*words, "down"], {}, runner=rig.fake) == 0
    assert rig.fake.cancelled == ["4242"]
    assert not rig.root.exists()


def test_a_provider_named_like_the_agents_role_is_refused_before_any_submit(hpc, tmp_path, site):
    """O57: the agent's node is the role "sandbox"; a provider of that name would lose its node to it, so a layout
    with both is refused before anything is submitted."""
    with pytest.raises(hpc.settings.SettingsError, match="sandbox"):
        hpc.settings.load_settings({"sandbox": {"cpus": 8, "memory": "17g"}}).roles(("aspen", "sandbox"))
    assert [r.name for r in hpc.settings.load_settings({}).roles(("aspen", "sandbox"))] == ["zone", "aspen", "sandbox"]
    rig = Rig(hpc, tmp_path, site, rename=("hazel", "sandbox"))
    with pytest.raises(hpc.settings.SettingsError, match="sandbox"):
        rig.up()
    assert rig.sbatches() == []


# -- Slurm: a heterogeneous job --------------------------------------------------------------------------------


def het_allocation(hpc):
    shape = hpc.slurm.Shape(1, 9, 18432)
    return hpc.slurm.Allocation("hpcop-p-abcd", "acct", "part", 6, 5, 11264, "02:00:00", 7200, het=(shape,))


def test_a_heterogeneous_held_job_names_every_option_of_its_second_component(hpc):
    """O57: the agent's component repeats the account, partition and time and names its nodes, tasks, cores and memory
    (Slurm propagates no partition, nodes or cores to it); --wrap is given once, in component 0 (sbatch builds the
    script once, for the first component)."""
    argv = het_allocation(hpc).argv()
    assert argv.count(":") == 1
    first, second = argv[: argv.index(":")], argv[argv.index(":") + 1 :]
    assert "--wrap=exec sleep 7200" in first
    assert "--no-requeue" in first
    assert "--nodes=6" in first
    assert second == [
        "--account=acct", "--partition=part", "--time=02:00:00", "--nodes=1", "--ntasks-per-node=1",
        "--cpus-per-task=9", "--mem=18432M",
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("component", "recorded", "words"),
    [
        (0, {"MinMemoryNode": "12G"}, "component 0's MinMemoryNode"),
        (1, {"MinMemoryNode": "11264M"}, "component 1's MinMemoryNode"),
        (1, {"NumCPUs": "5"}, "component 1's NumCPUs"),
        (1, {"Requeue": "1"}, "component 1's Requeue"),
        (1, {"TimeLimit": "04:00:00"}, "component 1's TimeLimit"),
        (1, {"Partition": "other"}, "component 1's Partition"),
        (1, {"HetJobOffset": "2"}, "component 1's HetJobOffset"),
    ],
    ids=["services memory", "agent memory", "agent cores", "agent requeue", "agent time", "agent partition", "offset"],
)
def test_each_component_is_read_back_and_a_mismatch_in_either_cancels_only_that_job(hpc, component, recorded, words):
    """O57: every component of the heterogeneous job is read back; a field of either recorded otherwise than asked
    (--no-requeue and --time included, on the whole job) cancels that one job by its leader's id, naming the field."""
    fake = FakeSlurm(component_recorded={("4242", component): recorded})
    slurm = hpc.slurm.Slurm(fake, sleep=lambda s: None)
    asked = het_allocation(hpc)
    job = slurm.submit(asked)
    with pytest.raises(hpc.slurm.SlurmError, match=words):
        slurm.checked(job, asked)
    assert fake.cancelled == ["4242"]


@pytest.mark.parametrize("het", [False, True], ids=["one component", "heterogeneous"])
def test_a_submitted_id_that_now_names_another_job_is_never_cancelled(hpc, het):
    """O57: Slurm reuses ids: a submitted id whose job (component 0) now carries another name is not the job just
    submitted, so neither the read-back nor the wait for it to run cancels it; up fails, naming it."""
    fake = FakeSlurm(recorded={"JobName": "someone-elses-job"})
    slurm = hpc.slurm.Slurm(fake, sleep=lambda s: None)
    plain = hpc.slurm.Allocation("hpcop-p-abcd", "a", "p", 6, 5, 11264, "02:00:00", 7200)
    asked = het_allocation(hpc) if het else plain
    job = slurm.submit(asked)
    with pytest.raises(hpc.slurm.SlurmError, match="another job's name"):
        slurm.checked(job, asked)
    with pytest.raises(hpc.slurm.SlurmError, match="another job's name"):
        slurm.wait_running(job, 60.0, name=asked.name)
    assert fake.cancelled == []


def test_a_heterogeneous_job_recorded_without_its_second_component_is_cancelled(hpc):
    """O57: a heterogeneous job Slurm reports with one component is not the job asked for: cancelled, by its id."""
    fake = FakeSlurm()
    real = fake.run

    def one_record(argv, timeout):
        done = real(argv, timeout)
        if argv[:3] != ["scontrol", "show", "job"]:
            return done
        return done._replace(stdout=done.stdout.splitlines()[0] + "\n")

    fake.run = one_record
    slurm = hpc.slurm.Slurm(fake, sleep=lambda s: None)
    asked = het_allocation(hpc)
    with pytest.raises(hpc.slurm.SlurmError, match="its components"):
        slurm.checked(slurm.submit(asked), asked)
    assert fake.cancelled == ["4242"]


def test_a_heterogeneous_job_recorded_as_asked_gives_each_components_own_id_and_nodes(hpc):
    """O57: a heterogeneous job recorded as asked passes; each component's record carries its own job id and nodes."""
    fake = FakeSlurm()
    slurm = hpc.slurm.Slurm(fake, sleep=lambda s: None)
    asked = het_allocation(hpc)
    records = slurm.checked(slurm.submit(asked), asked)
    assert [r["JobId"] for r in records] == ["4242", "4243"]
    assert slurm.hostnames(records[1]["NodeList"]) == ["node07"]
    assert slurm.readback("4242", asked)["MinMemoryNode"] == "11264M"
    assert fake.cancelled == []


def test_a_heterogeneous_job_is_listed_by_its_components_and_found_by_its_name(hpc):
    """O57: squeue lists a heterogeneous job as <leader>+<k>: it is still there while any component is listed, and
    found by its name as one job, its leader's id."""
    fake = FakeSlurm()
    slurm = hpc.slurm.Slurm(fake, sleep=lambda s: None)
    job = slurm.submit(het_allocation(hpc))
    assert not slurm.gone(job)
    assert slurm.jobs_named("me", "hpcop-p-abcd") == ["4242"]
    slurm.cancel(job)
    assert slurm.gone(job)


def test_a_step_in_the_agents_component_names_its_het_group_and_is_listed_by_the_components_id(hpc):
    """O57: a step on the agent's node runs with --het-group=1 and its status is asked of the component's own job id;
    a step of component 0 names --het-group=0 (the site refuses a het job's step that names none), and a step of a
    job of one component names no het group."""
    step = hpc.slurm.Step("4242", "node07", "hpcop-x-sandbox-facts", 1, 1024, (), ("true",), het_group=1,
                          component="4243")  # fmt: skip
    argv = step.argv()
    assert argv[1:3] == ["--jobid=4242", "--het-group=1"]
    services = hpc.slurm.Step("4242", "node01", "x", 1, 1024, (), ("true",), het_group=0).argv()
    assert services[1:3] == ["--jobid=4242", "--het-group=0"]
    assert "--het-group" not in " ".join(hpc.slurm.Step("4242", "node01", "x", 1, 1024, (), ("true",)).argv())
    fake = FakeSlurm()
    slurm = hpc.slurm.Slurm(fake, poll_s=0.0, sleep=lambda s: None)
    asked = []
    slurm.steps = lambda job: asked.append(job) or {}
    slurm._finish(FakeProcess(hpc.slurm.Completed(None, "", ""), hangs=True), step, 0.0)
    assert set(asked) == {"4243"}
    assert next(c for c in fake.calls if c[0] == "sacct")[:3] == ["sacct", "-j", "4243"]


def test_a_step_of_a_component_is_cancelled_by_its_own_id_and_nothing_else(hpc):
    """O57: a step listed under a component (<leader>+<k>.<n> or <component id>.<n>) is cancelled by that id; a
    component or a job is never taken for a step."""
    fake = FakeSlurm()
    slurm = hpc.slurm.Slurm(fake)
    slurm.cancel_step("4242+1.3")
    slurm.cancel_step("4243.3")
    for bad in ("4242+1", "4242", "4242+1.3,4242.0"):
        with pytest.raises(hpc.slurm.SlurmError):
            slurm.cancel_step(bad)
    assert fake.cancelled == ["4242+1.3", "4243.3"]


# -- up and down, "het" -----------------------------------------------------------------------------------------


def test_het_up_holds_the_services_and_the_agents_node_as_two_components_of_one_job(hpc, tmp_path, site):
    """O57: one sbatch of two components: the six service nodes at their shape, then the agent's node at its own; the
    state records each component, and `sandbox` (M5's shard driver reads its node) names the node, the job and the
    het group a step there takes."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    assert len(rig.sbatches()) == 1
    services, agent = rig.fake.submitted["4242"]
    assert (services["--nodes"], services["--cpus-per-task"], services["--mem"]) == ("6", "5", "11264M")
    assert (agent["--nodes"], agent["--cpus-per-task"], agent["--mem"]) == ("1", "9", "18432M")
    state = rig.state()
    assert state["allocation"]["nodes"] == [f"node{i:02d}" for i in range(1, 7)]
    assert state["allocation"]["readback"]["NumCPUs"] == "30"
    sandbox = state["sandbox"]
    assert (sandbox["placement"], sandbox["node"], sandbox["job"], sandbox["het_group"]) == ("het", "node07", "4242", 1)
    assert sandbox["component_job"] == "4243"
    assert sandbox["readback"]["MinMemoryNode"] == "18432M"
    assert (sandbox["cpus_per_node"], sandbox["memory_mib_per_node"]) == (9, 18432)
    assert state["services"]["sandbox"]["node"] == "node07"
    assert state["services"]["sandbox"]["address"] == "10.0.0.7"


def test_every_step_on_the_agents_node_runs_in_its_component_and_no_service_runs_there(hpc, tmp_path, site):
    """O57: the agent's node facts step runs with --het-group=1; every other step names --het-group=0, the services'
    component (the site refuses a het job's step that names none); no service step runs on the agent's node, and down
    runs one step there, its sweep (O63), in the same component, and every other step of its own in component 0."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    on_agent = [s for s in rig.fake.sruns if s.node == "node07"]
    assert [s.name.rsplit("-", 2)[-2:] for s in on_agent] == [["sandbox", "facts"]]
    assert all(s.het_group == 1 and s.job == "4242" for s in on_agent)
    assert all(s.het_group == 0 for s in rig.fake.sruns if s.node != "node07")
    assert not [s for s in rig.fake.sruns if s.refused]
    assert all(s.node != "node07" for s in rig.fake.spawned)
    rig.fake.sruns.clear()
    rig.driver.down()
    (swept,) = [s for s in rig.fake.sruns if s.node == "node07"]
    assert swept in rig.fake.sweeps
    assert (swept.het_group, swept.job) == (1, "4242")
    assert all(s.het_group == 0 for s in rig.fake.sruns if s.node != "node07")


def test_the_fake_refuses_a_het_jobs_step_that_names_no_component_as_the_site_does(hpc, tmp_path, site):
    """O57: on the site's Slurm a step of a heterogeneous job with no --het-group fails ("No directives to start
    application on any available hetjob components"), component 0 included; the fake answers so, and up fails at its
    first step instead of placing anything, its message carrying srun's error."""
    rig = Rig(hpc, tmp_path, site)
    where = rig.driver.launcher._where

    def no_group(node, name, job=None):  # the driver as the slot ran it: no group on a service node
        job, group, component, at = where(node, name, job)
        return job, (None if group == 0 else group), component, at

    rig.driver.launcher._where = no_group
    with pytest.raises(RuntimeError, match="node facts of zone") as raised:
        rig.up()
    assert "status 1; its stderr: srun: error: No directives to start application" in str(raised.value)
    (facts,) = rig.fake.sruns
    assert facts.refused
    assert rig.fake.spawned == []


@pytest.mark.parametrize("services_alone", [False, True])
def test_a_job_of_one_component_takes_no_het_group(hpc, tmp_path, site, services_alone):
    """O57: under "separate", and without an agent's node, each held job has one component: no step names a group."""
    rig = Rig(hpc, tmp_path, site, "separate")
    if services_alone:
        rig.driver = hpc.cli.make_driver(services_only(tmp_path / "plain"), str(rig.root), runner=rig.fake)
        rig.driver.sleep, rig.driver.slurm.sleep = (lambda s: None), (lambda s: None)
        rig.driver.poll_s = rig.driver.slurm.poll_s = 0.0
        rig.driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        rig.driver.relay_available = lambda: False
    rig.up()
    facts = [s for s in rig.fake.sruns if s.name.endswith("-facts")]
    assert len(facts) == (6 if services_alone else 7)  # one on each node, the agent's included under "separate"
    assert all(s.het_group is None and not s.refused for s in rig.fake.sruns)


def test_het_up_whose_agents_component_is_recorded_otherwise_cancels_the_job_and_down_removes_the_root(
    hpc, tmp_path, site
):
    """O57: a partial up: the agent's component recorded with too little memory fails the read-back, which cancels
    the job (every component with its leader) before any step; down finds it gone and removes the root."""
    fake = FakeSlurm(component_recorded={("4242", 1): {"MinMemoryNode": "11264M"}})
    rig = Rig(hpc, tmp_path, site, fake=fake)
    with pytest.raises(hpc.slurm.SlurmError, match="component 1's MinMemoryNode"):
        rig.up()
    assert fake.cancelled == ["4242"]
    assert fake.sruns == []
    rig.driver.down()
    assert fake.cancelled == ["4242"]
    assert not rig.root.exists()


@pytest.mark.parametrize(("placement", "listed_as"), [("het", "4243"), ("separate", "4343")])
def test_a_step_on_the_agents_node_is_found_by_name_under_the_id_squeue_lists_it_under(
    hpc, tmp_path, site, placement, listed_as
):
    """O57: squeue -s lists a step of a heterogeneous job's agent's component under the component's own id alone,
    never the leader's (seen live), and one of the agent's own job under that job's: the launcher's lookup by name
    asks each, and a service step is still found under the held job's id."""
    rig = Rig(hpc, tmp_path, site, placement)
    rig.up()
    prefix = rig.state()["allocation"]["name"]
    rig.fake.steps[f"{prefix}-shard-1"] = f"{listed_as}.17"
    rig.fake.calls.clear()
    assert rig.driver.launcher.listed(f"{prefix}-shard-1") == f"{listed_as}.17"
    assert rig.driver.launcher.listed(f"{prefix}-zone-run-1") == rig.fake.steps[f"{prefix}-zone-run-1"]
    assert rig.driver.launcher.listed("hpcop-not-a-step") is None
    asked = {c[c.index("-j") + 1] for c in rig.fake.calls if c[:2] == ["squeue", "-s"]}
    assert asked == {"4242", listed_as}


def test_down_waits_for_a_step_on_the_agents_component_and_cancels_it_by_its_own_id(hpc, tmp_path, site):
    """O57: down's wait for every step to leave asks the agent's component's own id too, where alone its steps are
    listed: one still running there at the stop's bound (a shard step, say) is cancelled by its own id before the
    held job is, and the root goes."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    prefix = rig.state()["allocation"]["name"]
    rig.fake.steps[f"{prefix}-shard-1"] = "4243.17"
    ticks = itertools.count(0, 60)
    rig.driver.clock = lambda: next(ticks)
    rig.driver.down()
    assert rig.fake.cancelled.index("4243.17") < rig.fake.cancelled.index("4242")
    assert rig.fake.steps == {}
    assert not rig.root.exists()


def test_het_down_cancels_the_leader_once_and_waits_until_no_component_is_listed(hpc, tmp_path, site):
    """O57: down cancels the heterogeneous job by its leader's id alone (every component goes with it) and waits until
    squeue lists no component of it, then removes the root."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    rig.fake.calls.clear()
    assert "4242" in rig.driver.down()
    assert rig.fake.cancelled == ["4242"]
    assert any(c[:3] == ["squeue", "-h", "-j"] for c in rig.fake.calls)
    assert not rig.root.exists()


def test_het_down_finds_a_job_whose_id_never_reached_the_state_by_its_components(hpc, tmp_path, site):
    """O57: when sbatch's id never reached the state, down finds the heterogeneous job by its exact name (listed once
    per component, <leader>+<k>) and cancels its leader once."""
    rig = Rig(hpc, tmp_path, site)
    real = rig.fake.run

    def garbled(argv, timeout):
        done = real(argv, timeout)
        return done._replace(stdout="Submitted.\n") if argv[0] == "sbatch" else done

    rig.fake.run = garbled
    with pytest.raises(hpc.slurm.SlurmError, match="no job id"):
        rig.up()
    rig.driver.down()
    assert rig.fake.cancelled == ["4242"]
    assert not rig.root.exists()


def test_an_agents_node_that_is_also_a_service_node_fails_up_before_any_step(hpc, tmp_path, site):
    """O57: one service per node, and never the agent beside one: a component placed on a service's node fails up,
    the job cancelled by its id before any step."""
    fake = FakeSlurm(node_lists={("4242", 1): ["node03"]})
    rig = Rig(hpc, tmp_path, site, fake=fake)
    with pytest.raises(hpc.slurm.SlurmError, match="node03"):
        rig.up()
    assert fake.cancelled == ["4242"]
    assert fake.sruns == []


# -- up and down, "separate" ------------------------------------------------------------------------------------


def test_separate_up_holds_the_agents_node_in_a_second_job_read_back_like_the_first(hpc, tmp_path, site):
    """O57: "separate" submits a second held job for the agent's node alone, under a name of its own holding up's
    random part, read back like the first; the state records its id, and its steps take its own job id."""
    rig = Rig(hpc, tmp_path, site, placement="separate")
    rig.up()
    services, agent = rig.sbatches()
    assert ":" not in services
    assert ":" not in agent
    first, second = rig.fake.submitted["4242"][0], rig.fake.submitted["4343"][0]
    assert first["--nodes"] == "6"
    assert second["--job-name"] == first["--job-name"] + "-sandbox"
    assert (second["--nodes"], second["--cpus-per-task"], second["--mem"]) == ("1", "9", "18432M")
    assert "--no-requeue" in second
    assert second["--time"] == "02:00:00"
    sandbox = rig.state()["sandbox"]
    assert (sandbox["placement"], sandbox["job"], sandbox["het_group"], sandbox["node"]) == (
        "separate", "4343", None, "node07"
    )  # fmt: skip
    assert sandbox["readback"]["NumNodes"] == "1"
    facts = next(s for s in rig.fake.sruns if s.node == "node07")
    assert (facts.job, facts.het_group) == ("4343", None)


def test_separate_agents_job_recorded_otherwise_is_cancelled_with_the_first_and_down_removes_the_root(
    hpc, tmp_path, site
):
    """O57: the agent's job recorded otherwise than asked is cancelled by its own id, and so is the services' job, by
    its own (up leaves no held job pending); the state still records it, and down, which finds both gone, removes the
    root."""
    fake = FakeSlurm(component_recorded={("4343", 0): {"MinMemoryNode": "1G"}})
    rig = Rig(hpc, tmp_path, site, placement="separate", fake=fake)
    with pytest.raises(hpc.slurm.SlurmError, match="MinMemoryNode"):
        rig.up()
    assert fake.cancelled == ["4343", "4242"]
    assert rig.state()["allocation"]["job"] == "4242"
    rig.driver.down()
    assert fake.cancelled == ["4343", "4242"]
    assert not rig.root.exists()


def test_separate_down_cancels_both_jobs_by_their_own_ids_and_ends_both_jobs_clients(hpc, tmp_path, site):
    """O57: down cancels the services' job and the agent's job, each by its own id and once, waits for both to leave,
    and looks for srun clients of both on this host."""
    rig = Rig(hpc, tmp_path, site, placement="separate")
    rig.up()
    asked = []
    rig.fake.clients_of = lambda job: asked.append(job) or []
    rig.driver.down()
    assert sorted(rig.fake.cancelled) == ["4242", "4343"]
    assert sorted(asked) == ["4242", "4343"]
    assert not rig.root.exists()


def test_separate_down_never_touches_an_agents_job_id_that_now_names_another_job(hpc, tmp_path, site):
    """O57: Slurm reuses ids: a recorded agent's job id whose job now carries another name is not ours and is not
    cancelled; the services' job is."""
    rig = Rig(hpc, tmp_path, site, placement="separate")
    rig.up()
    rig.fake.component_recorded[("4343", 0)] = {"JobName": "someone-elses-job"}
    rig.driver.down()
    assert rig.fake.cancelled == ["4242"]


def test_separate_down_finds_the_agents_job_by_its_own_name_when_its_id_never_reached_the_state(hpc, tmp_path, site):
    """O57: the agent's job's id lost after its sbatch: down finds it by its own exact name and cancels it, and the
    services' job by its recorded id."""
    rig = Rig(hpc, tmp_path, site, placement="separate")
    real = rig.fake.run

    def garbled(argv, timeout):
        done = real(argv, timeout)
        return done._replace(stdout="Submitted.\n") if argv[0] == "sbatch" and done.stdout == "4343\n" else done

    rig.fake.run = garbled
    with pytest.raises(hpc.slurm.SlurmError, match="no job id"):
        rig.up()
    assert rig.state()["sandbox"]["job"] is None
    rig.driver.down()
    assert sorted(rig.fake.cancelled) == ["4242", "4343"]
    assert not rig.root.exists()


def test_separate_agents_job_that_never_starts_is_cancelled_and_so_is_the_first(hpc, tmp_path, site):
    """O57: the agent's job still pending at the queue's bound is cancelled by its id and up fails, and the services'
    job, running, is cancelled by its own: no held job of this up is left to bill. down finds both gone."""
    fake = FakeSlurm(job_states={"4343": "PENDING"})
    rig = Rig(hpc, tmp_path, site, placement="separate", fake=fake)
    clock = iter(range(0, 10**7, 100))
    rig.driver.slurm.clock = lambda: next(clock)
    with pytest.raises(hpc.slurm.SlurmError, match="did not start"):
        rig.up()
    assert fake.cancelled == ["4343", "4242"]
    rig.driver.down()
    assert fake.cancelled == ["4343", "4242"]


def test_down_cancels_the_agents_job_even_when_the_services_cancel_fails(hpc, tmp_path, site):
    """O57: a failed scancel of one job never leaves the other billing: both are cancelled before down reports the
    failure, and the root and its state are kept for a second down."""
    rig = Rig(hpc, tmp_path, site, placement="separate")
    rig.up()
    real = rig.fake.run

    def failing(argv, timeout):
        if argv == ["scancel", "4242"]:
            return hpc.slurm.Completed(1, "", "scancel: error: Socket timed out\n")
        return real(argv, timeout)

    rig.fake.run = failing
    with pytest.raises(RuntimeError, match="4242"):
        rig.driver.down()
    assert rig.fake.cancelled == ["4343"]
    assert rig.root.exists()


# -- describe and the state file --------------------------------------------------------------------------------


def test_describe_names_both_shapes_where_the_agents_node_is_held_and_the_billing(hpc, tmp_path, site):
    """O57: describe prints the service nodes' shape, the agent's node's own shape and placement, and the billing per
    hour with the agent's node at its own shape (42 with a weight of 0.5)."""
    rig = Rig(hpc, tmp_path, site)
    text = rig.driver.describe(0.5)
    assert "6 node(s) of 5 cores and 11264 MiB" in text
    assert "the agent's node: 1 node of 9 cores and 18432 MiB, component 1 of the held job" in text
    assert "billing: 42 units per hour" in text
    other = tmp_path / "other"
    (other / "fed").mkdir(parents=True)
    assert "a held job of its own" in Rig(hpc, other, site, placement="separate").driver.describe(None)


def test_a_state_whose_agents_job_is_not_a_job_id_is_refused(hpc, tmp_path, site):
    """O57: down cancels what the state's `sandbox` names, so its job ids must be Slurm job ids."""
    rig = Rig(hpc, tmp_path, site, placement="separate")
    rig.up()
    path = rig.root / "hpc-federation.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["sandbox"]["job"] = "4343 -u me"
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(hpc.state.StateError, match=r"sandbox\.job"):
        hpc.state.load(rig.root)


# -- down's sweep of the agent's node (O63) ------------------------------------------------------------------------


def step_directory(node_tmp: Path, name: str, root: Path, *, start: str = "0", failed: bool = False) -> Path:
    """A shard step's directory on the agent's node's tmpfs, as shards.py step makes it: its owner record (by default
    of a step killed while its dispatcher ran: this process's pid with another start time) and, with `failed`, the
    record of a dispatcher that ended and its archive-failed mark."""
    path = node_tmp / f"expcore-shard-fed-a.{name}"
    (path / "study" / "out").mkdir(parents=True)
    shard = {"index": 1, "count": 2, "label": "fed-a"}
    record = {"shard": shard, "pid": os.getpid(), "start": start, "root": str(root),
              "phase": "archiving" if failed else "running"}  # fmt: skip
    (path / "step.json").write_text(json.dumps(record), encoding="utf-8")
    if failed:
        (path / "ARCHIVE-FAILED.json").write_text(json.dumps({"shard": shard, "exit_status": 0}), encoding="utf-8")
    return path


@pytest.mark.parametrize(("placement", "job", "group"), [("het", "4242", 1), ("separate", "4343", None)])
def test_o63_down_sweeps_the_agents_node_removing_gone_steps_and_naming_a_kept_out(
    hpc, tmp_path, site, placement, job, group
):
    """O63: down runs shards.py's sweep as a control step on the agent's node (its component, or its own job), with
    the nodes' Python, the node's tmpfs and this root: a step directory of this federation whose step is gone is
    removed, one whose out/ could not be archived is kept and named in down's report, and the root goes."""
    node_tmp = tmp_path / "node-tmp"
    node_tmp.mkdir()
    fake = FakeSlurm(sweep_here=True, node_tmp=str(node_tmp))
    rig = Rig(hpc, tmp_path, {**site, "HPC_NODE_TMP": str(node_tmp)}, placement, fake)
    rig.up()
    gone = step_directory(node_tmp, "gone", rig.root)
    kept = step_directory(node_tmp, "kept", rig.root, failed=True)
    printed = rig.driver.down()
    (swept,) = rig.fake.sweeps
    assert (swept.node, swept.job, swept.het_group) == ("node07", job, group)
    assert swept.command[:3] == ["/shared/venv/bin/python", "-I", "-B"]  # the core's Python: its reaper (W2)
    assert swept.command[swept.command.index("--tmpfs") + 1] == str(node_tmp)
    assert swept.command[swept.command.index("--root") + 1] == str(rig.root)
    assert not gone.exists()
    assert kept.is_dir()
    assert f"{kept.name} (its out/ was not archived" in printed
    assert not rig.root.exists()


def test_o63_a_sweep_that_fails_leaves_the_root_but_never_the_job_billing(hpc, tmp_path, site):
    """O63: the agent's node could not be swept: the held job is cancelled all the same, and down reports the node
    unverified, keeping the root for a second down."""

    def failing(srun):
        if "sweep" in srun.command:
            from hpcslurm import Completed

            return FakeProcess(Completed(1, "", "Traceback ...\n"))
        return None

    fake = FakeSlurm(on_control=failing)
    rig = Rig(hpc, tmp_path, site, fake=fake)
    rig.up()
    with pytest.raises(RuntimeError, match="the agent's node could not be verified clean"):
        rig.driver.down()
    assert fake.cancelled == ["4242"]
    assert rig.root.exists()
