"""An offline rehearsal of the HPC path, end to end: what the live slots will do, on fakes, each stage reading only the
files the stage before it wrote.

1. `hpcfed.py up` over the pilot's layout and template as committed (`sandbox_placement = "het"`, the agent's node
   sized by the Apptainer sandbox's limits in the template, its pins and work root from `site.env`), twice, each over
   a fake cluster of its own (`hpc_fed_support.FakeSlurm`), the core's phase the real one over a bootstrap that answers
   as a live zone would: the state file, `platform.toml` and `link-check.json` in each root, the fault relay started.
2. Two shards of the example study (stub models), one per federation: `shards.py plan` over the manifest that names
   the two roots, then each shard's `step`, run as its node would run the line `plan` printed (behind `env -i` and the
   allowlist alone): the step makes the sandbox's work root and copies the root's `platform.toml` beside the study's
   `study.toml`; its dispatcher `python -I -m expcore run` runs every run in the Apptainer sandbox with `[sandbox]` as
   that file has it, on a fake node (`fake_mount_node`), with a spend journal and snapshots.
3. Shard fed-a killed outright after its first snapshot, with an attempt that began after it ended and one in flight,
   then `step --resume`: the killed step's directory on the node removed, the snapshot restored, what the journal holds
   that it lacks counted once as `lost_in_kill`.
4. `shards.py collect --run` (which runs `expcore merge`), then `check`, `analyze` and `report` on the merge.
5. `hpcfed.py down` on each: the relay stopped, the agent's node swept, the held job cancelled, the root gone.

Nothing here reaches a cluster, a container or a model. The kill is timed by holds the study's plugin reads
(`hpc_rehearsal_plugin`), never by sleeping: the step's own snapshot thread looks for a run's end every 30 s
(`shards.POLL_S`), and the manifest's `snapshot_every_s` is past the rehearsal, so the first snapshot is the one taken
at its first look, and the next could come no sooner than 30 s after it; the kill comes within seconds of it, and the
module checks that no second snapshot was taken. That look is why the module takes about a minute.
"""

import contextlib
import io
import json
import shlex
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from hpc_rehearsal_support import (
    JOBS,
    LABELS,
    Control,
    Federation,
    StepLine,
    attempts_in,
    deploy_modules,
    hpcfed,
    kill_outright,
    link_check_stand_in,
    make_ca,
    make_study,
    wait_for,
    wait_step,
    write_manifest,
)
from study_support import Campaign, clean_environment, finish
from tls_support import HAS_OPENSSL

from expcore.campaign.interrupted_spend import EXPORT_KEY as INTERRUPTED_KEY
from expcore.campaign.lost_ledger import KEY as LOST_KEY
from expcore.campaign.resume import read_fingerprint
from expcore_onedata.config import load_platform

__all__ = ["clean_environment"]  # a fixture the stages below use, imported from the support module

pytestmark = [
    pytest.mark.benchmark,  # a minute of wall time: the step's own snapshot thread looks every 30 s
    pytest.mark.skipif(not HAS_OPENSSL, reason="the zone's CA is made with openssl"),
]

KILLED, WHOLE = LABELS  # fed-a is killed outright and resumed; fed-b runs to its end
SNAPSHOT_AT, KILL_AT = 3, 5  # the attempts of fed-a's first life that are held: the snapshot, then the kill
SERVICES = ["zone", "aspen", "larch", "rowan", "alder", "hazel"]


# ------------------------------------------------------------------------------------------------ stage 1: up
@pytest.fixture(scope="module")
def deploy():
    with deploy_modules() as found:
        yield found


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    """The filesystem the login node and the nodes see (the control directory), and the agents' nodes' tmpfs, a
    directory of this host both nodes stand for here. The mount helper's oneclient is each federation's pinned bundle
    (`agent_site`), as its rendered platform file names it."""
    base = tmp_path_factory.mktemp("shared")
    for name in ("control", "node-tmpfs"):
        (base / name).mkdir()
    return SimpleNamespace(base=base, control=Control(base / "control"), node_tmp=base / "node-tmpfs")


@pytest.fixture(scope="module")
def federations(deploy, shared, tmp_path_factory) -> dict[str, Federation]:
    """Both federations up, each by `hpcfed.py up` over a fake cluster of its own."""
    ca = make_ca(tmp_path_factory.mktemp("ca"))
    feds, pairs = {}, []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(deploy.fedlinks, "check_links", link_check_stand_in(pairs))
        for label in LABELS:
            fed = Federation(label, tmp_path_factory.mktemp(label), shared.node_tmp, ca)
            hpcfed(deploy, fed, "up", fed.site())
            feds[label] = fed
    for fed in feds.values():
        fed.pairs = pairs
    return feds


def printing(function, argv):
    """A command's exit status and what it printed."""
    shown = io.StringIO()
    with contextlib.redirect_stdout(shown), contextlib.redirect_stderr(shown):
        code = function(argv)
    return code, shown.getvalue()


def state_of(fed: Federation) -> dict:
    return json.loads((fed.root / "hpc-federation.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("label", LABELS)
def test_up_brings_the_federation_up_and_holds_the_agents_node_as_component_1(federations, label):
    fed = federations[label]
    assert fed.codes["up"] == 0, fed.printed["up"]
    state = state_of(fed)
    assert state["phase"] == "linked"
    assert state["roles"] == [*SERVICES, "sandbox"]
    assert {name: state["services"][name]["state"] for name in SERVICES} == dict.fromkeys(SERVICES, "running")
    sandbox = state["sandbox"]
    assert (sandbox["placement"], sandbox["het_group"], sandbox["job"]) == ("het", 1, JOBS[label][0])
    assert sandbox["node"] == state["services"]["sandbox"]["node"] == "node07"
    agent = fed.fake.submitted[JOBS[label][0]][1]  # the held job's component 1: sized by the template's limits
    assert (agent["--nodes"], agent["--cpus-per-task"], agent["--mem"]) == ("1", "9", "18432M")


@pytest.mark.parametrize("label", LABELS)
def test_up_renders_platform_toml_with_the_apptainer_sandbox_and_writes_the_ten_pair_link_record(federations, label):
    fed = federations[label]
    config = load_platform(fed.root / "platform.toml")
    assert config.sandbox.backend == "apptainer"
    assert config.sandbox.work_root == fed.work_root
    assert config.sandbox.apptainer.image.sha256 == fed.agent["HPC_AGENT_SIF_SHA256"]
    assert not fed.work_root.exists()  # the shard step makes it, on the agent's node
    assert config.zone.url == "https://zone.expcore-hpc-pilot.test:8443"  # the declared name (O70), never a node's
    record = json.loads((fed.root / "link-check.json").read_text(encoding="utf-8"))
    assert record["ok"] is True
    assert len(record["pairs"]) == 10
    assert [len(p) for p in fed.pairs] == [10, 10]


@pytest.mark.parametrize("label", LABELS)
def test_up_starts_the_login_nodes_fault_relay_last_and_records_it(federations, label):
    fed = federations[label]
    (argv,) = fed.fake.relays
    assert argv[argv.index("-m") + 1 : argv.index("-m") + 5] == [
        "expcore_onedata.fault_relay", "serve", "--root", str(fed.root)
    ]  # fmt: skip
    assert fed.fake.relay_calls[:2] == ["serve", "alive"]
    assert state_of(fed)["relay"]["pid"] == 9999
    assert fed.printed["up"].rstrip().endswith("fault relay: serving")


# ------------------------------------------------------------------------------------------------ stage 2: the plan
@pytest.fixture(scope="module")
def plan(deploy, federations, shared):
    """The study, the manifest over the roots `up` wrote, and each shard's line as `shards.py plan` prints it."""
    base = shared.base
    study = make_study(base / "studies", shared.control.path)
    for name in ("results", "logs"):
        (base / name).mkdir()
    manifest = write_manifest(base / "shards.toml", study, base / "results", shared.node_tmp, federations)
    code, printed = printing(deploy.shards.main, ["plan", "--manifest", str(manifest)])
    assert code == 0, printed
    lines = {}
    for text in printed.splitlines():
        entry = json.loads(text)
        lines[entry["shard"]] = StepLine.parse(entry["shard"], entry["argv"])
    return SimpleNamespace(study=study, manifest=manifest, results=base / "results", logs=base / "logs", lines=lines)


def test_plan_puts_each_shards_step_on_its_federations_agent_node_in_component_1(plan, federations):
    """O57: each line is an exact step of the held job's component 1, on the node the state file records, behind the
    allowlist alone, the root of its own federation in it; the fake cluster places it there."""
    assert list(plan.lines) == list(LABELS)
    for index, label in enumerate(LABELS, 1):
        line, fed = plan.lines[label], federations[label]
        assert line.srun[1:4] == [f"--jobid={JOBS[label][0]}", "--het-group=1", "--exact"]
        assert line.option("-w") == "node07"
        homeless = {k: v for k, v in line.env.items() if k not in ("PATH", "LC_ALL") and not k.startswith("EXPCORE")}
        assert line.env == {
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "EXPCORE_FED_ROOT_PARENT": str(fed.root.parent),
            "EXPCORE_FED_ROOT": str(fed.root),
            **homeless,
        }
        assert homeless["HOME"] == "/dev/null/hpcop-home"  # no home of the account's, nor any (O57)
        assert homeless["PYTHONNOUSERSITE"] == "1"
        assert line.command[2:4] == [str(Path(line.command[2])), "step"]
        assert line.command[line.command.index("--index") + 1] == str(index)
        fed.fake._step([*line.srun, "/usr/bin/env", "-i", *(f"{k}={v}" for k, v in line.env.items()), *line.command])


# ------------------------------------------------------------------------------------------------ stages 2 and 3
@dataclass
class Shards:
    """What the steps did: each life's exit status and output, and what the kill left."""

    codes: dict[str, int] = field(default_factory=dict)
    logs: dict[str, Path] = field(default_factory=dict)
    snapshot_at_kill: list[Path] = field(default_factory=list)
    snapshot_attempts: list[tuple[str, str]] = field(default_factory=list)
    journals_at_kill: list[str] = field(default_factory=list)
    archive_at_kill: bool = False
    left_at_kill: list[str] = field(default_factory=list)  # the killed step's directory on the agent's node
    left_after_resume: list[str] = field(default_factory=list)

    def printed(self, life: str) -> str:
        return self.logs[life].read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def shards(plan, federations, shared) -> Shards:
    """fed-b's step runs to its end while fed-a's first life is held at its third attempt until the step's first
    snapshot holds that attempt in flight, released, held again at its fifth, and killed outright; then fed-a's step is
    run again with `--resume`."""
    found, control = Shards(), shared.control
    snapdir = plan.results / KILLED / "snapshots"
    for n in (SNAPSHOT_AT, KILL_AT):
        control.hold(KILLED, n)
    found.logs = {life: plan.logs / f"{life}.log" for life in (f"{KILLED}-1", f"{KILLED}-2", WHOLE)}
    killed = plan.lines[KILLED].start(found.logs[f"{KILLED}-1"])
    whole = plan.lines[WHOLE].start(found.logs[WHOLE])
    try:
        control.wait_held(KILLED, SNAPSHOT_AT, killed)

        def snapshot_with_the_held_attempt() -> bool:
            taken = sorted(snapdir.glob("snap-*"))
            return bool(taken) and any(state == "running" for _, state in attempts_in(taken[-1]))

        wait_for(snapshot_with_the_held_attempt, killed, 150.0, "a snapshot holding the held attempt")
        control.release(KILLED, SNAPSHOT_AT)
        control.wait_held(KILLED, KILL_AT, killed)
        found.snapshot_at_kill = sorted(snapdir.glob("snap-*"))
        found.journals_at_kill = sorted(p.name for p in (plan.results / KILLED / "journals").iterdir())
        kill_outright(killed)
        found.codes[f"{KILLED}-1"] = killed.returncode
        found.archive_at_kill = (plan.results / f"{KILLED}.out.tar").exists()
        found.snapshot_attempts = attempts_in(found.snapshot_at_kill[-1])
        found.left_at_kill = sorted(p.name for p in shared.node_tmp.glob(f"expcore-shard-{KILLED}.*"))
        control.clear()  # the second life holds nothing
        again = plan.lines[KILLED].start(found.logs[f"{KILLED}-2"], "--resume")
        found.codes[f"{KILLED}-2"] = wait_step(again)
        found.left_after_resume = sorted(p.name for p in shared.node_tmp.glob(f"expcore-shard-{KILLED}.*"))
        found.codes[WHOLE] = wait_step(whole)
    finally:
        for process in (killed, whole):
            if process.poll() is None:
                kill_outright(process)
    return found


def test_every_step_ends_and_fed_a_was_killed_outright_after_exactly_one_snapshot(shards):
    """The kill left the snapshot taken while attempt 3 was in flight and nothing newer (the rehearsal's timing: a
    second snapshot here would make what follows another case, so it fails the module rather than pass another way),
    one journal, and no archive."""
    assert shards.codes[f"{KILLED}-1"] < 0, shards.printed(f"{KILLED}-1")
    assert [p.name for p in shards.snapshot_at_kill] == ["snap-000001"]
    assert len(shards.journals_at_kill) == 1
    assert not shards.archive_at_kill
    states = [state for _, state in shards.snapshot_attempts]
    assert states.count("running") == 1, states
    assert states[-1] == "running"
    assert shards.codes[f"{KILLED}-2"] == 0, shards.printed(f"{KILLED}-2")
    assert shards.codes[WHOLE] == 0, shards.printed(WHOLE)


def test_each_step_made_its_federations_sandbox_work_root_on_the_nodes_tmpfs(shards, federations):
    """O63: no work root existed after up; each shard's step made its own federation's, 0700, before its dispatcher."""
    for fed in federations.values():
        assert stat.S_IMODE(fed.work_root.stat().st_mode) == 0o700, fed.label


def test_the_resume_removed_the_killed_steps_directory_on_the_agents_node(shards):
    """O63: the step killed outright left its directory on the node's tmpfs; the resume removed it, its step shown
    gone by pid and start time, before it made its own, which it removed when it ended."""
    assert len(shards.left_at_kill) == 1
    assert shards.left_after_resume == []
    assert "an earlier step's directory on this node is removed" in shards.printed(f"{KILLED}-2")


def test_the_resume_restores_the_snapshot_and_counts_what_the_journal_holds_beyond_it_once(shards):
    printed = shards.printed(f"{KILLED}-2")
    assert f"shards: shard {KILLED} resumes from snapshot snap-000001" in printed
    assert "counted as lost_in_kill" in printed


# ------------------------------------------------------------------------------------------------ stage 4: the merge
@pytest.fixture(scope="module")
def merged(deploy, plan, shards, clean_environment) -> Campaign:
    """`shards.py collect --run` (which runs the merge it prints), then check, analyze and report on the merge."""
    into = plan.results.parent / "collected"
    argv = ["collect", "--manifest", str(plan.manifest), "--into", str(into), "--run"]
    code, printed = printing(deploy.shards.main, argv)
    if code != 0:  # the merge's own words: `--run` lets them go to the inherited stderr; a refused merge wrote nothing
        line = shlex.split(printed.splitlines()[0])
        again = subprocess.run(line, capture_output=True, text=True, env=clean_environment, check=False)
        pytest.fail(f"collect --run failed ({code}): {printed}\n{again.stdout}{again.stderr}")
    campaign = Campaign(plan.study)
    finish(clean_environment, campaign)
    campaign.codes["collect"] = code
    return campaign


def test_collect_merges_both_shards_and_every_command_on_the_merge_succeeds(merged):
    assert merged.codes == {"collect": 0, "check": 0, "analyze": 0, "report": 0}
    batch = merged.read("merge.json")
    assert [(s["index"], s["label"]) for s in batch["shards"]] == [(1, KILLED), (2, WHOLE)]


def test_the_merged_export_carries_the_kills_lost_spend_with_its_basis_and_the_interrupted_attempt(merged):
    export = merged.read("export.json")
    lost = export[LOST_KEY]
    assert [(entry["kind"], entry["ended"]) for entry in lost] == [("attempt", True), ("attempt", False)]
    assert {entry["shard"] for entry in lost} == {1}
    assert lost[1]["basis"] == "measured"
    assert len({entry["run_key"] for entry in lost}) == 2
    interrupted = export[INTERRUPTED_KEY]
    assert [(entry["shard"], entry["basis"]) for entry in interrupted] == [(1, "measured")]


def test_the_report_shows_the_shards_both_guards_and_the_spend_the_kill_took(merged):
    report = merged.report
    for heading in (
        "## Shards",
        "## Egress (detected, not prevented)",
        "## Mount isolation (detected, not prevented)",
        "## Egress and mount isolation together (detected, not prevented)",
        "### Spend lost in kills",
        "### Interrupted attempts' spend",
    ):
        assert heading in report, heading
    lost = report.split("### Spend lost in kills", 1)[1].split("\n#", 1)[0]
    assert "| Shard |" in lost.replace("|  ", "| ") or "Shard" in lost
    assert "Basis" in lost


def collected_fingerprint(plan, label: str) -> dict:
    return read_fingerprint(plan.results.parent / "collected" / label / "out" / "state.sqlite")


def test_c06_both_shards_ran_one_campaign_with_each_spaces_data_identity_and_the_agents_pins(plan, merged):
    """C06, O63: the two federations' platform files differ (every Onezone id, the work root), yet both shards record
    one campaign fingerprint: each space by its declared identity and its data's content hash (`data_manifest`, which
    up rendered and checked), never its id, and the sandbox's pins; so the merge above is evidence, not a default."""
    one, two = (collected_fingerprint(plan, label) for label in LABELS)
    assert one == two
    platform = one["platform"]
    assert len(platform["spaces"]) == 4
    assert set(platform["sandbox_backend"]) >= {"image_sha256", "egress_shim_sha256", "egress_collector_sha256"}
    assert merged.read("export.json")["campaign_fingerprint"]["fingerprint"] == one


def test_o63_the_campaign_fingerprint_holds_no_site_value_but_the_declared_pins(plan, merged, federations):
    """O63: of every value site.env gives either federation, only the two declared SHA-256s reach the fingerprint, under
    the sandbox's pins; no path (the SIF, the shim, the work root, SH, L, the node's tmpfs), account or partition."""
    text = json.dumps(collected_fingerprint(plan, KILLED), sort_keys=True)
    for fed in federations.values():
        for variable, value in fed.site().items():
            if variable.endswith("_SHA256"):
                assert text.count(value) == 1, variable
            elif variable not in ("HPC_STEP_PATH",):  # "/usr/bin:/bin": no site's own words
                assert value not in text, variable


@pytest.fixture(scope="module")
def other_build(deploy, plan, shards, merged, federations, shared, tmp_path_factory, clean_environment):
    """fed-c: a third federation of the same layout and data whose site declares another build of the agent's image;
    shard 2 of a second manifest run there, beside fed-a's shard 1, and the merge of the two."""
    fed = Federation("fed-c", tmp_path_factory.mktemp("fed-c"), shared.node_tmp, federations[KILLED].ca_pem,
                     sif=b"another build of the agent's image\n")  # fmt: skip
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(deploy.fedlinks, "check_links", link_check_stand_in([]))
        hpcfed(deploy, fed, "up", fed.site())
    base = shared.base / "other"
    study = make_study(base / "studies", shared.control.path)
    (base / "results").mkdir()
    for kind in ("tar", "json"):  # fed-a's shard as it came back, shard 1 of the second manifest too
        shutil.copyfile(plan.results / f"{KILLED}.out.{kind}", base / "results" / f"{KILLED}.out.{kind}")
    feds = {KILLED: federations[KILLED], fed.label: fed}
    manifest = write_manifest(base / "shards.toml", study, base / "results", shared.node_tmp, feds)
    code, printed = printing(deploy.shards.main, ["plan", "--manifest", str(manifest)])
    assert code == 0, printed
    entry = json.loads(printed.splitlines()[1])
    step = StepLine.parse(entry["shard"], entry["argv"]).start(plan.logs / "fed-c.log")
    stepped = wait_step(step)
    collected = printing(deploy.shards.main, ["collect", "--manifest", str(manifest), "--into", str(base / "in")])
    line = shlex.split(collected[1].splitlines()[0])
    merge = subprocess.run(line, capture_output=True, text=True, env=clean_environment, check=False)
    hpcfed(deploy, fed, "down", {})
    return SimpleNamespace(fed=fed, stepped=stepped, collected=collected[0], merge=merge, study=study)


def test_c06_a_shard_on_another_build_of_the_agents_image_is_another_campaign_and_the_merge_refuses(other_build):
    """C06, O63: the same layout and the same data, another SIF declared (and checked) at up: the step runs, its archive
    is collected, and the merge refuses it beside fed-a's shard, by the image's pin alone, writing nothing."""
    assert other_build.stepped == 0
    assert other_build.collected == 0
    assert other_build.merge.returncode == 1, other_build.merge.stdout + other_build.merge.stderr
    said = other_build.merge.stderr
    assert "shard 2 (fed-c): ran another campaign than shard 1 (fed-a)" in said
    assert "platform.sandbox_backend.image_sha256" in said
    assert "platform.spaces" not in said
    assert not (other_build.study / "out").exists()
    assert other_build.fed.codes["down"] == 0, other_build.fed.printed["down"]


# ------------------------------------------------------------------------------------------------ stage 5: down
@pytest.fixture(scope="module")
def downed(deploy, federations, merged, other_build) -> dict[str, Federation]:
    for fed in federations.values():
        fed.fake.calls.clear()
        hpcfed(deploy, fed, "down", {})
    return federations


@pytest.mark.parametrize("label", LABELS)
def test_down_stops_the_relay_first_cancels_the_held_job_and_removes_the_root(downed, label):
    fed = downed[label]
    assert fed.codes["down"] == 0, fed.printed["down"]
    assert fed.fake.calls[0][1:5] == ["-B", "-m", "expcore_onedata.fault_relay", "stop"]
    assert fed.fake.cancelled == [JOBS[label][0]]
    assert fed.fake.steps == {}
    assert not fed.root.exists()


@pytest.mark.parametrize("label", LABELS)
def test_down_swept_the_agents_node_and_left_no_step_directory_there(downed, shared, label):
    """O63: down ran its sweep on the agent's node, in the held job's component 1: nothing of a step was left to
    remove or to keep, and nothing of either federation's steps is on the node's tmpfs."""
    fed = downed[label]
    (swept,) = fed.fake.sweeps
    assert (swept.node, swept.het_group) == ("node07", 1)
    assert "shards: sweep: 0 step directory(ies) removed; kept: none" in fed.printed["down"]
    assert not list(shared.node_tmp.glob("expcore-shard-*"))


def test_the_sandbox_left_no_run_record_on_either_agent_node(downed):
    """The resumed dispatcher's first sandbox reaped what the killed one left; every other run's teardown removed its
    own record."""
    for fed in downed.values():
        assert sorted(p.name for p in fed.work_root.iterdir()) == ["_runs"], fed.label
        assert not list((fed.work_root / "_runs").iterdir()), fed.label
