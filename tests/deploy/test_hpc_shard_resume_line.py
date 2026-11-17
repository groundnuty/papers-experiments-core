"""`shards.py resume-line` and what follows it (live slot 5's resume gap, W1): a shard killed outright on its
federation's agent's node is resumed in a held job of the operator's on another node, by a line resume-line prints only
once the job is the operator's and free, no step of the shard lives anywhere, and the newest snapshot verifies and pins
the study's campaign; it records the job and the node in the federation's state, and the federation's `down` sweeps
that node too, in that job, which it never cancels. The federation is brought up by the driver over the fake Slurm of
hpc_fed_support; the study is a stamped copy of the example; the snapshot is the core's own, of a store made here."""

import importlib.util
import json
import sqlite3
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from campaign_support import make_manifest, make_schema
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, flagged, hpc_on_path
from shard_support import make_study

import expcore
from expcore.campaign import out_snapshot
from expcore.campaign.manifest import fingerprint_sha256
from expcore.campaign.store import Store

HPC = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation"
SCRIPT = HPC / "shards.py"
CONTROL = 'control = { cpus = 1, memory = "1g" }'
AGENT = 'sandbox = { cpus = 8, memory = "17g" }'
SPEC = {"index": 1, "count": 2, "label": "fed-a"}
JOB, NODE = "5151", "node08"  # the operator's held job for the resume, on a node outside the federation


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcdata

        modules.data = hpcdata
        yield modules


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # its dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def shards():
    try:
        yield _load("hpc_shards_resume_line", SCRIPT)
    finally:
        sys.modules.pop("hpc_shards_resume_line", None)


@pytest.fixture(scope="module")
def resume():
    try:
        yield _load("hpc_shardresume", HPC / "shardresume.py")
    finally:
        sys.modules.pop("hpc_shardresume", None)


class World:
    """A federation up over the fake Slurm (the agent's node a held job of its own), the operator's job on another
    node, a shard manifest of two shards, and shard 1's snapshot from its killed first life."""

    def __init__(self, hpc, shards, resume, tmp_path):
        self.hpc, self.shards, self.resume, self.tmp = hpc, shards, resume, tmp_path
        self.root, self.fake, self.driver = self._up()
        self.fake.submitted[JOB] = [{"--job-name": "expcore-resume", "--nodes": "1", "--cpus-per-task": "9",
                                     "--mem": "18G"}]  # fmt: skip
        self.fake.node_lists[(JOB, 0)] = [NODE]
        self.manifest = self._manifest()
        self.plan = shards.load_plan(self.manifest)

    def _up(self):
        images, logs, layouts = self.tmp / "images", self.tmp / "logs", self.tmp / "layout"
        for folder in (images, logs, layouts, self.tmp / "fed"):
            folder.mkdir()
        for name in ("onezone.tar", "oneprovider.tar"):
            (images / name).write_bytes(b"tar")
            (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
        text = LAYOUT.read_text(encoding="utf-8").replace('sandbox_placement = "het"', 'sandbox_placement = "separate"')
        template = LAYOUT.parent / "pilot-shape.platform.toml.template"
        (layouts / LAYOUT.name).write_text(text.replace(CONTROL, f"{CONTROL}\n{AGENT}"), encoding="utf-8")
        (layouts / template.name).write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        fake, root = FakeSlurm(), self.tmp / "fed" / "root"
        driver = self.hpc.cli.make_driver(layouts / LAYOUT.name, str(root), runner=fake)
        driver.sleep = driver.slurm.sleep = lambda s: None
        driver.poll_s = driver.slurm.poll_s = 0.0

        def core(layout, owner, values, environ=None):
            (root / "platform.toml").write_text("", encoding="utf-8")  # the file the real phase renders (plan reads it)
            return "core: the link check passed"

        driver.core = core
        driver.relay_available = lambda: False
        site = {**SITE, **agent_site(self.tmp / "agent"), "SH": str(images), "L": str(logs)}
        driver.up(self.hpc.settings.site_from_environment(site))
        # the layout's one ingested space holds nothing, as its manifest (the empty one) declares: verified
        driver.verify = lambda layout, owner, manifests: self.hpc.data.verify(manifests, lambda space: ({}, set()))
        driver.verify_data()
        return root, fake, driver

    def _manifest(self):
        study = make_study(self.tmp, "study")
        values = {"study": study, "python": sys.executable, "core_src": Path(expcore.__file__).parents[1],
                  "results": self.tmp / "results", "tmpfs": self.tmp / "tmpfs"}  # fmt: skip
        lines = [f'{key} = "{value}"' for key, value in values.items()]
        lines += ['step_path = "/usr/bin:/bin"', "cpus = 8", 'memory = "17g"']
        lines += ["[[shards]]", 'label = "fed-a"', f'root = "{self.root}"']
        lines += ["[[shards]]", 'label = "fed-b"', f'root = "{self.tmp / "other" / "root"}"']
        path = self.tmp / "shards.toml"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def fingerprint(self):
        """What the study pins beside this federation's platform file, as resume-line computes it."""

        def refuse(why):
            raise AssertionError(why)

        scratch = self.tmp / "fp-scratch"
        scratch.mkdir(mode=0o700)
        return self.resume.study_fingerprint(self.shards, self.plan, self.plan.shards[0], scratch, refuse)

    def snapshot(self, fingerprint, *, label="fed-a", index=1):
        """A snapshot of the shard's out/, its store pinning `fingerprint`, as the step's snapshot thread takes one."""
        out = self.tmp / f"out-{label}"
        (out / "runs").mkdir(parents=True)
        (out / "campaign.lock").write_text("")
        schema = make_schema()
        store = Store(out / "state.sqlite", schema)
        store.sync_manifest(make_manifest(schema).rows)
        store.close()
        connection = sqlite3.connect(out / "state.sqlite")
        with connection:
            connection.execute("INSERT OR REPLACE INTO meta(key, value, set_utc) VALUES ('fingerprint', ?, 'now')",
                               (json.dumps(fingerprint),))  # fmt: skip
        connection.close()
        facts = {"shard": {"index": index, "count": 2, "label": label}, "base": None}
        snapdir = self.plan.results / label / "snapshots"
        return out_snapshot.take(out, snapdir, scratch=self.tmp / f"scratch-{label}", facts=facts, live=False,
                                 now="20261116T222322Z")  # fmt: skip

    def line(self, **given):
        job, node = given.get("job", JOB), given.get("node", NODE)
        return self.resume.resume_line(self.shards, self.manifest, "fed-a", job, node, script=SCRIPT,
                                       runner=self.fake, user="someone")  # fmt: skip

    def state(self):
        return json.loads((self.root / "hpc-federation.json").read_text(encoding="utf-8"))


@pytest.fixture
def world(hpc, shards, resume, tmp_path):
    return World(hpc, shards, resume, tmp_path)


def test_resume_line_prints_the_shards_resume_in_the_operators_job_and_records_its_node(world):
    """O63: The line: the shard's step in job J on node N (no het group), in `/`, named as every step of the shard,
    every Python isolated and writing no bytecode, with --resume; and J and N recorded in the federation's state."""
    world.snapshot(world.fingerprint())
    line = flagged(world.line())
    fed = world.shards.federation(world.plan.shards[0])
    assert line[:3] == ["srun", f"--jobid={JOB}", "--exact"]
    assert line[line.index("-w") + 1] == NODE
    assert "--chdir=/" in line
    assert line[line.index("-J") + 1] == f"{fed['name']}-shard-1"
    assert line[-1] == "--resume"
    assert line[line.index("--job") + 1] == JOB
    entry = {"job": JOB, "node": NODE, "label": "fed-a", "name": "expcore-resume"}
    assert world.state()["shard_nodes"] == [entry]
    world.line()
    assert world.state()["shard_nodes"] == [entry]  # once


def test_resume_line_passes_the_proof_that_the_marked_step_is_gone(world):
    """O63: A running mark the killed step left, naming a step no longer listed: the line carries its nonce (`--over`),
    which the step needs to take it over on another node."""
    world.snapshot(world.fingerprint())
    mark = world.plan.results / "fed-a" / "running.json"
    mark.write_text(json.dumps({"format": "expcore-hpc-shard-running/1", "shard": SPEC, "nonce": "a" * 32,
                                "node": "node07", "job": "4343", "step": "x-shard-1"}))  # fmt: skip
    line = world.line()
    assert line[-3:] == ["--resume", "--over", "a" * 32]


def refused(world, match, **given):
    before = world.state()
    with pytest.raises(world.shards.ShardsError, match=match):
        world.line(**given)
    assert world.state() == before  # nothing recorded
    return before


def test_resume_line_refuses_while_a_step_of_the_shard_is_listed_anywhere(world):
    """O63: A step carrying the shard's step name under any job of this user's (here the agent's own) lives: refused."""
    world.snapshot(world.fingerprint())
    fed = world.shards.federation(world.plan.shards[0])
    world.fake.steps[f"{fed['name']}-shard-1"] = "4343.9"
    refused(world, "a step of it runs")


def test_resume_line_refuses_while_the_step_its_mark_names_is_listed(world):
    """O63: The running mark names a step still listed (under its own name, here): refused."""
    world.snapshot(world.fingerprint())
    mark = world.plan.results / "fed-a" / "running.json"
    mark.write_text(json.dumps({"format": "expcore-hpc-shard-running/1", "shard": SPEC, "nonce": "a" * 32,
                                "node": "node07", "job": "4343", "step": "hand-made"}))  # fmt: skip
    world.fake.steps["hand-made"] = "4343.4"
    refused(world, "still listed")


def test_resume_line_refuses_a_mark_that_names_no_step(world):
    """O63: A mark it cannot show gone (no job or step in it): refused, never taken for gone."""
    world.snapshot(world.fingerprint())
    mark = world.plan.results / "fed-a" / "running.json"
    mark.write_text(json.dumps({"format": "expcore-hpc-shard-running/1", "shard": SPEC, "nonce": "a" * 32}))
    refused(world, "names no step")


def declare_pass_env(world, names):
    """The manifest, with `pass_env` declared (a top-level key: before the `[[shards]]`)."""
    text = world.manifest.read_text(encoding="utf-8")
    world.manifest.write_text(text.replace('memory = "17g"\n', f'memory = "17g"\npass_env = {json.dumps(names)}\n'),
                              encoding="utf-8")  # fmt: skip


def test_resume_line_pins_the_declared_variables_as_every_other_line_does(world, monkeypatch):
    """O63: a manifest declaring `pass_env` has its resume line carry each name with the operator's value (an `env -i`
    word) and `--pass-env NAME`, as `lines` prints them, so a resumed step is passed the same variables."""
    declare_pass_env(world, ["FAULT_ACK_MODE"])
    monkeypatch.setenv("FAULT_ACK_MODE", "go-ahead")
    world.snapshot(world.fingerprint())
    line = flagged(world.line())
    assert "FAULT_ACK_MODE=go-ahead" in line
    assert line.count("--pass-env") == 1
    assert line[line.index("--pass-env") + 1] == "FAULT_ACK_MODE"
    assert "--resume" in line


def test_resume_line_refuses_an_unset_declared_variable_before_it_records_anything(world, monkeypatch):
    """O63: the operator's value is read before the job and node are recorded in the federation's state: a declared
    variable that is unset refuses the line, naming it, and the state is as it was."""
    declare_pass_env(world, ["FAULT_ACK_MODE"])
    monkeypatch.delenv("FAULT_ACK_MODE", raising=False)
    world.snapshot(world.fingerprint())
    refused(world, "FAULT_ACK_MODE is not set")


FIRST, LATER = "go-ahead", "changed-mode"  # the value the shard's first step passed, and one the operator holds later


def lay_origin(world, values):
    """The shard's first step's record of the values it passed, as the step's own writer makes it."""
    world.shards.record_pass_env_origin(world.plan.results, SPEC, values)


def refusal_naming_only(world, name):
    """`resume-line` refuses, with the federation's state untouched, naming `name` and showing neither value."""
    before = world.state()
    with pytest.raises(world.shards.ShardsError) as caught:
        world.line()
    said = str(caught.value)
    assert name in said
    assert FIRST not in said
    assert LATER not in said
    assert world.state() == before  # nothing recorded
    return said


def test_resume_line_refuses_a_value_that_changed_since_the_first_step_naming_only_the_variable(world, monkeypatch):
    """O63: the operator now holds another value of a declared variable than the shard's first step passed: the
    resume's line is refused, before the job is looked at and before its node is recorded, naming the variable and
    showing neither value."""
    declare_pass_env(world, ["FAULT_ACK_MODE"])
    monkeypatch.setenv("FAULT_ACK_MODE", LATER)
    world.snapshot(world.fingerprint())
    lay_origin(world, {"FAULT_ACK_MODE": FIRST})
    said = refusal_naming_only(world, "FAULT_ACK_MODE")
    assert "no line is printed" in said


def test_resume_line_refuses_a_changed_value_before_it_asks_slurm_anything(world, monkeypatch):
    """O63: the check is the first of the line's: a job that is not the operator's (here, one Slurm does not know)
    does not hide it, so the operator learns the value first."""
    declare_pass_env(world, ["FAULT_ACK_MODE"])
    monkeypatch.setenv("FAULT_ACK_MODE", LATER)
    world.snapshot(world.fingerprint())
    lay_origin(world, {"FAULT_ACK_MODE": FIRST})
    before = world.state()
    with pytest.raises(world.shards.ShardsError, match="FAULT_ACK_MODE") as caught:
        world.line(job="999999")
    assert "not known to Slurm" not in str(caught.value)
    assert world.state() == before


def test_resume_line_with_the_values_of_the_first_step_prints_its_line(world, monkeypatch):
    """O63: the same value again: the line is printed and the job and node recorded, as ever, and the origin is the
    first step's, untouched."""
    declare_pass_env(world, ["FAULT_ACK_MODE"])
    monkeypatch.setenv("FAULT_ACK_MODE", FIRST)
    world.snapshot(world.fingerprint())
    lay_origin(world, {"FAULT_ACK_MODE": FIRST})
    origin = world.plan.results / "fed-a" / "pass-env.json"
    before = origin.read_bytes()
    line = flagged(world.line())
    assert f"FAULT_ACK_MODE={FIRST}" in line
    assert world.state()["shard_nodes"] == [{"job": JOB, "node": NODE, "label": "fed-a", "name": "expcore-resume"}]
    assert origin.read_bytes() == before


def test_resume_line_refuses_a_manifest_that_dropped_a_variable_the_first_step_passed(world, monkeypatch):
    """O63: the manifest declares nothing now, the first step passed a variable: the resumed shard would lose it,
    so the line is refused, naming the variable."""
    monkeypatch.setenv("FAULT_ACK_MODE", FIRST)
    world.snapshot(world.fingerprint())
    lay_origin(world, {"FAULT_ACK_MODE": FIRST})
    refusal_naming_only(world, "FAULT_ACK_MODE")


def test_resume_line_refuses_an_origin_it_cannot_read_as_the_shards(world, monkeypatch):
    """O63: an origin that is not a record of this shard refuses the line, as no record at all would not."""
    declare_pass_env(world, ["FAULT_ACK_MODE"])
    monkeypatch.setenv("FAULT_ACK_MODE", FIRST)
    world.snapshot(world.fingerprint())
    origin = world.plan.results / "fed-a" / "pass-env.json"
    origin.write_text("{")
    before = world.state()
    with pytest.raises(world.shards.ShardsError, match=r"pass-env\.json"):
        world.line()
    assert world.state() == before


GUARD, POST = "raise SystemExit(0)  # guard\n", "raise SystemExit(0)  # post-run\n"


def declare_scripts(world, *, write=True):
    """The manifest, with the study's guard and post-run script declared (and, `write`, in the study)."""
    if write:
        guards = world.plan.study / "guards"
        guards.mkdir()
        (guards / "pre_step.py").write_text(GUARD)
        (guards / "post_run.py").write_text(POST)
    text = world.manifest.read_text(encoding="utf-8")
    world.manifest.write_text(
        text.replace(
            'memory = "17g"\n', 'memory = "17g"\npre_step = "guards/pre_step.py"\npost_run = "guards/post_run.py"\n'
        ),
        encoding="utf-8",
    )


def test_resume_line_pins_the_studys_guard_and_post_run_script_as_lines_does(world):
    """O63: A manifest declaring `pre_step` and `post_run` gives a resume line carrying each script's path and its
    SHA-256 as the study holds it, as `lines` does: the resumed step runs the study's guard first and its post-run
    script last. The line was refused as unpinned (`step_argv`), so such a shard could not be resumed at all."""
    declare_scripts(world)
    world.snapshot(world.fingerprint())
    line = flagged(world.line())
    options = ("--pre-step", "--pre-step-sha256", "--post-run", "--post-run-sha256")
    wanted = {"--pre-step": "guards/pre_step.py", "--pre-step-sha256": sha256(GUARD.encode()).hexdigest(),
              "--post-run": "guards/post_run.py", "--post-run-sha256": sha256(POST.encode()).hexdigest()}  # fmt: skip
    assert {option: line[line.index(option) + 1] for option in options} == wanted
    assert "--resume" in line


def test_resume_line_refuses_a_declared_script_the_study_does_not_hold_before_it_records_anything(world):
    """O63: The scripts are read before the job and node are recorded in the federation's state: a declared guard that
    is not in the study refuses the line, naming it, and the state is as it was."""
    declare_scripts(world, write=False)
    world.snapshot(world.fingerprint())
    refused(world, "guards/pre_step.py")


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"job": "4242"}, "the services' job"),
        ({"node": "node07"}, "does not hold node node07"),
        ({"job": "6161"}, "not known to Slurm"),
    ],
    ids=["the services' job", "another node", "no such job"],
)
def test_resume_line_refuses_a_job_that_is_not_the_operators_free_one_on_the_node(world, change, match):
    """O63: the services' job of a federation, a job on another node than the one named, and an id Slurm does not know
    are refused, and nothing is recorded."""
    world.snapshot(world.fingerprint())
    world.fake.job_states["6161"] = "GONE"  # Slurm knows no such id
    refused(world, match, **change)


def test_resume_line_refuses_a_job_that_is_not_this_users_or_not_running(world):
    """O63: a job of another user's (its UserId), and one that is not RUNNING, are refused."""
    world.snapshot(world.fingerprint())
    world.fake.component_recorded[(JOB, 0)] = {"UserId": "another(1)"}
    refused(world, "not this user's")
    world.fake.component_recorded[(JOB, 0)] = {"JobState": "PENDING"}
    refused(world, "not RUNNING")


def test_resume_line_refuses_a_job_whose_steps_hold_its_cores(world):
    """O63: The job's running steps hold 4 of its 9 cores: the manifest's 8 are not free."""
    world.snapshot(world.fingerprint())
    rows = [f"{JOB}|RUNNING|cpu=9,mem=18G,node=1", f"{JOB}.batch|RUNNING|cpu=9,mem=18G,node=1",
            f"{JOB}.3|RUNNING|cpu=4,mem=2G,node=1", f"{JOB}.2|COMPLETED|cpu=9,mem=18G,node=1"]  # fmt: skip
    world.fake.accounting[JOB] = "\n".join(rows) + "\n"
    refused(world, "has not the manifest's 8 cores")
    world.fake.accounting[JOB] = f"{JOB}.3|RUNNING|cpu=1,mem=nothing\n"
    refused(world, "could not be read")


def test_resume_line_refuses_a_shard_with_no_snapshot(world):
    """O63: a shard with no snapshot newer than its archive has nothing a resume elsewhere continues: refused."""
    refused(world, "no snapshot of it verifies")


def test_resume_line_refuses_a_snapshot_of_another_campaign(world):
    """O63: The snapshot pins a fingerprint the study does not, with this federation's platform file: refused before a
    node is taken."""
    pinned = world.fingerprint()
    world.snapshot({**pinned, "seeds": ["another"]})
    refused(world, "pins another campaign than the study")


def test_resume_line_refuses_when_another_shard_pins_another_campaign(world):
    """O63: the other shard's newest snapshot pins another fingerprint: the shards would never merge, so refused before
    a node is taken."""
    pinned = world.fingerprint()
    world.snapshot(pinned)
    world.snapshot({**pinned, "seeds": ["another"]}, label="fed-b", index=2)
    refused(world, "another campaign than shard fed-b")
    assert fingerprint_sha256(pinned)  # the comparison is by the fingerprint's own hash


def test_resume_line_refuses_while_the_federation_is_not_linked(world):
    """O63: a federation whose state is not linked (going down, say) is refused before anything is read of Slurm."""
    world.snapshot(world.fingerprint())
    path = world.root / "hpc-federation.json"
    state = world.state()
    path.write_text(json.dumps({**state, "phase": "stopping"}), encoding="utf-8")
    with pytest.raises(world.shards.ShardsError, match="has not finished"):
        world.line()


def test_up_refuses_an_agents_node_without_the_cores_python_on_the_nodes(hpc, shards, resume, tmp_path, monkeypatch):
    """O63 (W2): down's sweep of a node a shard step ran on reaps the sandbox's killed runs with the core's reaper, so
    up asks for the core's Python on the nodes (HPC_CORE_PYTHON) with an agent's node, before it submits anything."""
    import hpc_fed_support

    real = hpc_fed_support.agent_site

    def without(directory):
        return {k: v for k, v in real(directory).items() if k != "HPC_CORE_PYTHON"}

    monkeypatch.setattr(sys.modules[__name__], "agent_site", without)
    with pytest.raises(RuntimeError, match="needs HPC_CORE_PYTHON"):
        World(hpc, shards, resume, tmp_path)


def test_the_command_line_refuses_a_manifest_it_cannot_read(shards, tmp_path, capsys):
    """O63: `shards.py resume-line` with a manifest that cannot be read exits 2, naming it, and prints no line."""
    assert shards.main(["resume-line", "--manifest", str(tmp_path / "none.toml"), "--label", "fed-a", "--job", JOB,
                        "--node", NODE]) == 2  # fmt: skip
    assert "could not be read" in capsys.readouterr().err


# -- down sweeps the node too (W1) -----------------------------------------------------------------------------------


def test_w1_down_sweeps_the_node_a_shard_was_resumed_on_in_its_job_and_never_cancels_that_job(world):
    """O63 (W1): down sweeps the agent's node and the node resume-line recorded, in the job it recorded, each by the
    core's Python; it cancels the federation's jobs and never the operator's, and removes the root."""
    world.snapshot(world.fingerprint())
    world.line()
    printed = world.driver.down()
    swept = [s for s in world.fake.sruns if "sweep" in s.command]
    assert [(s.job, s.node) for s in swept] == [("4343", "node07"), (JOB, NODE)]
    assert all(s.command[:3] == ["/shared/venv/bin/python", "-I", "-B"] for s in swept)
    assert JOB not in world.fake.cancelled
    assert printed.startswith("down: the held job")
    assert not world.root.exists()


def test_w1_down_cancels_the_shards_step_still_running_in_the_resume_job_before_it_sweeps(world):
    """O63 (W1): a shard step of this federation still running in the operator's job (its name) would outlive its
    federation: down cancels it by its own id before it sweeps that node, and touches no other step of that job, nor
    the job itself."""
    world.snapshot(world.fingerprint())
    world.line()
    fed = world.shards.federation(world.plan.shards[0])
    world.fake.steps[f"{fed['name']}-shard-1"] = f"{JOB}.2"
    world.fake.steps["the-operators-own"] = f"{JOB}.3"
    world.driver.down()
    assert f"{JOB}.2" in world.fake.cancelled
    assert f"{JOB}.3" not in world.fake.cancelled
    assert JOB not in world.fake.cancelled
    (resumed,) = [s for s in world.fake.sruns if "sweep" in s.command and s.job == JOB]
    assert "--live-fails" in resumed.command
    assert not world.root.exists()


def test_w1_a_resume_job_that_ended_is_said_and_never_stepped_into(world, capsys):
    """O63 (W1): a recorded resume job that ended is said and no step goes into it (its id may be another job's by now);
    down still finishes."""
    world.snapshot(world.fingerprint())
    world.line()
    world.fake.job_states[JOB] = "GONE"
    world.driver.down()
    assert [s.job for s in world.fake.sruns if "sweep" in s.command] == ["4343"]
    assert f"resumed in job {JOB}, which has ended" in capsys.readouterr().err


def test_w1_a_sweep_of_the_resume_node_that_fails_keeps_the_root(world):
    """O63 (W1): a sweep of the resume node that fails keeps the root and its state for a second down."""
    from hpc_fed_support import FakeProcess
    from hpcslurm import Completed

    world.snapshot(world.fingerprint())
    world.line()
    world.fake.on_control = lambda srun: FakeProcess(Completed(1, "", "")) if srun.job == JOB else None
    with pytest.raises(RuntimeError, match="the node shard fed-a was resumed on"):
        world.driver.down()
    assert world.root.exists()


def test_the_launcher_steps_into_no_job_the_state_does_not_record_a_resume_in(world):
    """O63 (W1): the launcher runs a step in a job of the operator's only where the state records a resume of this
    federation's shard."""
    with pytest.raises(world.hpc.state.StateError, match="no shard of this federation was resumed in"):
        world.driver.launcher.control(NODE, ["true"], name="x-sweep", timeout_s=5, job=JOB)


@pytest.mark.parametrize(
    "entry",
    [{"job": "51 51", "node": NODE, "label": "a", "name": "b"}, {"job": JOB, "node": "-w x", "label": "a", "name": "b"},
     {"job": JOB, "node": NODE, "label": "a"}],
    ids=["a job that is no id", "a node that is no name", "a missing name"],
)  # fmt: skip
def test_a_state_whose_shard_nodes_are_not_slurms_is_refused(world, entry):
    """O63 (W1): down steps into each recorded job on its node, so a `shard_nodes` entry must be a job id, a node name,
    a label and a job name."""
    path = world.root / "hpc-federation.json"
    path.write_text(json.dumps({**world.state(), "shard_nodes": [entry]}), encoding="utf-8")
    with pytest.raises(world.hpc.state.StateError, match="shard_nodes"):
        world.hpc.state.load(world.root)
