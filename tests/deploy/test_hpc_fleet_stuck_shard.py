"""O68: a stuck shard moves to a spare federation through the fleet alone (deploy/hpc-federation/fleet.py `step-stop` and
`step --resume`; fleetstuck.py).

`step-stop --shard L` ends the step the fleet launched for L, by the ids the fleet recorded for it and nothing else: its
srun client by pid, once the pid is shown to be that process still (its start tick), and its Slurm step by its exact
`<job>.<n>` id. `step --resume` of a step killed outright (its running mark, `running.json`, left) carries the mark's
nonce as `--over` exactly as `shards.py resume-line` does, and only after `shardresume.check_no_live_step` showed the
step that holds the mark gone: a resume whose holder still runs is refused and nothing is launched.

Every federation runs over a fake cluster of its own (`hpc_fleet_support.Bench`); no process is signalled and nothing
reaches a cluster. The one real comparison, a recorded client against the process that holds its pid, is made on a real
process (the last test)."""

import json
import re
from types import SimpleNamespace

import pytest
from hpc_fleet_support import fleet_modules, job_ids, make_bench, make_ca
from tls_support import HAS_OPENSSL

pytestmark = pytest.mark.skipif(not HAS_OPENSSL, reason="the zone's CA is made with openssl")

NONCE = re.compile(r"[0-9a-f]{32}")


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules() as found:
        yield found


@pytest.fixture(scope="module")
def ca(tmp_path_factory):
    return make_ca(tmp_path_factory.mktemp("ca"))


@pytest.fixture
def bench(deploy, ca, tmp_path, monkeypatch):
    return make_bench(deploy, tmp_path, ca, monkeypatch)


def record(bench) -> dict:
    return json.loads((bench.fleet / "fleet.json").read_text(encoding="utf-8"))


def shard_record(bench, label: str) -> dict:
    return next(s for s in record(bench)["shards"] if s["label"] == label)


def plan(bench) -> tuple[int, str]:
    return bench.run("plan", "--manifest", str(bench.settings()))


def started(bench) -> None:
    """Two federations up, both shards planned and launched: shard-1's client is pid 30000 on fed-1, shard-2's 30001."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    code, printed = bench.run("step")
    assert code == 0, printed


def step_of(bench, deploy, label: str) -> tuple[str, str]:
    """The name its step carries and the job id `squeue -s` lists it under, as the manifest's line gives them."""
    shards = deploy.fleet.fleetplan.shards
    loaded = deploy.fleet.fleetplan.load(bench.fleet / "shards.toml")
    shard = next(s for s in loaded.shards if s.label == label)
    fed = shards.federation(shard)
    return shards.step_name(fed, shard), fed["listed"]


def list_step(bench, deploy, label: str, cluster: str, number: int = 7) -> str:
    """The shard's step listed by `squeue -s` on `cluster`'s fake Slurm, as the id it is listed under."""
    name, job = step_of(bench, deploy, label)
    bench.fake(cluster).steps[name] = f"{job}.{number}"
    return f"{job}.{number}"


def leave_mark(bench, deploy, label: str) -> dict:
    """A step of the shard killed outright: its running mark in the results directory, made by the step's own
    `claim_running`, and left there."""
    name, job = step_of(bench, deploy, label)
    spec = {"index": int(label.split("-")[1]), "count": 2, "label": label}
    args = SimpleNamespace(results=bench.base / "results", label=label, resume=False, over=None, job=job,
                           step_name=name)  # fmt: skip
    deploy.shards.claim_running(args, spec)
    return json.loads((bench.base / "results" / label / "running.json").read_text(encoding="utf-8"))


def move_to_a_spare(bench) -> None:
    """What frees shard-1 of a federation whose step is gone: fed-1 down, a fresh fed-3 up, the plan giving it the shard."""
    assert bench.run("down", "--member", "fed-1")[0] == 0
    assert bench.run("add", "1")[0] == 0
    code, printed = plan(bench)
    assert code == 0, printed
    assert shard_record(bench, "shard-1")["member"] == "fed-3"


def over_of(argv: list) -> str | None:
    return argv[argv.index("--over") + 1] if "--over" in argv else None


# ------------------------------------------------------------------------------------------------ step --resume
@pytest.mark.parametrize("flags", [("--resume",), ()], ids=["with --resume", "plain step of a moved shard"])
def test_o68_a_step_killed_outright_resumes_on_the_spare_carrying_over_the_marks_nonce(bench, deploy, flags):
    """O68: shard-1's step was killed outright on fed-1 (its running mark left, its client and step gone); fed-1 is
    taken down, the shard moves to the fresh fed-3, and `fleet.py step --resume` launches it there with `--resume` and
    `--over` set to the mark's nonce, as `shards.py resume-line` builds the line."""
    started(bench)
    mark = leave_mark(bench, deploy, "shard-1")
    assert NONCE.fullmatch(mark["nonce"])
    bench.alive.discard(bench.spawned[0].pid)  # its client died with its step: nothing of it is listed anywhere
    move_to_a_spare(bench)
    launched = len(bench.spawned)
    code, printed = bench.run("step", "--shard", "shard-1", *flags)
    assert code == 0, printed
    assert len(bench.spawned) == launched + 1
    moved = bench.spawned[-1]
    assert moved.label == "fed-3"
    assert "--resume" in moved.argv
    assert over_of(moved.argv) == mark["nonce"]


def test_o68_a_resume_whose_marks_step_is_still_listed_on_the_cluster_is_refused_and_launches_nothing(bench, deploy):
    """O68: the mark names a step that `squeue -s` still lists (here on the spare's cluster): `--over` is never
    carried without the proof that the holder is gone, so the shard is refused and no step is launched."""
    started(bench)
    mark = leave_mark(bench, deploy, "shard-1")
    bench.alive.discard(bench.spawned[0].pid)
    move_to_a_spare(bench)
    name, _ = step_of(bench, deploy, "shard-1")
    held = mark["step"]
    assert held != name  # the mark names fed-1's step; the new step is fed-3's
    bench.fake("fed-3").steps[held] = f"{job_ids('fed-3')[1]}.5"
    launched = len(bench.spawned)
    code, printed = bench.run("step", "--shard", "shard-1", "--resume")
    assert code == 1
    assert "shard-1: not launched: " in printed
    assert f"the step its running mark names ({held}" in printed
    assert len(bench.spawned) == launched
    assert all(over_of(s.argv) is None for s in bench.spawned)


def test_o68_a_resume_on_the_same_federation_whose_step_is_still_listed_is_refused(bench, deploy):
    """O68: shard-1's client is dead but its step is still listed on fed-1 (the stuck step the operator has not
    cancelled): `step --resume` refuses it, launches nothing, and carries no `--over`."""
    started(bench)
    leave_mark(bench, deploy, "shard-1")
    bench.alive.discard(bench.spawned[0].pid)
    list_step(bench, deploy, "shard-1", "fed-1")
    launched = len(bench.spawned)
    code, printed = bench.run("step", "--shard", "shard-1", "--resume")
    assert code == 1
    assert "a step of it runs" in printed
    assert len(bench.spawned) == launched
    assert all(over_of(s.argv) is None for s in bench.spawned)


def test_o68_a_resume_with_no_running_mark_carries_no_over(bench, deploy):
    """O68: a step that ended by itself removed its mark: `--resume` is as it always was, with no `--over`."""
    started(bench)
    bench.alive.discard(bench.spawned[0].pid)
    code, printed = bench.run("step", "--shard", "shard-1", "--resume")
    assert code == 0, printed
    assert (bench.spawned[-1].argv[-1], over_of(bench.spawned[-1].argv)) == ("--resume", None)


@pytest.mark.parametrize("damage", ["garbage", "another shard"])
def test_o68_a_running_mark_that_is_not_this_shards_is_never_taken_over(bench, deploy, damage):
    """O68: a mark that cannot be read, or is another shard's, refuses the resume (`check_no_live_step`'s own rule):
    nothing is launched and no `--over` is carried."""
    started(bench)
    mark = leave_mark(bench, deploy, "shard-1")
    path = bench.base / "results" / "shard-1" / "running.json"
    if damage == "garbage":
        path.write_text("{ not json", encoding="utf-8")
    else:
        path.write_text(json.dumps({**mark, "shard": {"index": 2, "count": 2, "label": "shard-2"}}), encoding="utf-8")
    bench.alive.discard(bench.spawned[0].pid)
    launched = len(bench.spawned)
    code, printed = bench.run("step", "--shard", "shard-1", "--resume")
    assert code == 1
    assert "cannot be read as this shard's" in printed
    assert len(bench.spawned) == launched


# ------------------------------------------------------------------------------------------------ step-stop
def test_o68_step_stop_ends_only_the_recorded_client_and_cancels_only_the_recorded_step(bench, deploy):
    """O68: shard-1's recorded client (pid 30000) is ended and its step, found under the recorded job by the name
    the fleet gave it, is cancelled by its exact `<job>.<n>` id; another step of the same job, shard-2's client and
    step, and every other job are left as they are. The stop is recorded in the fleet's record."""
    started(bench)
    mine = list_step(bench, deploy, "shard-1", "fed-1", 7)
    job = mine.split(".")[0]
    bench.fake("fed-1").steps["someone-elses-step"] = f"{job}.8"  # the same job, another name
    theirs = list_step(bench, deploy, "shard-2", "fed-2", 7)
    code, printed = bench.run("step-stop", "--shard", "shard-1")
    assert code == 0, printed
    assert bench.fake("fed-1").cancelled == [mine]
    assert bench.fake("fed-1").steps == {"someone-elses-step": f"{job}.8"}
    assert bench.fake("fed-2").cancelled == [] and list(bench.fake("fed-2").steps.values()) == [theirs]
    assert [c.pid for c in bench.ended] == [bench.spawned[0].pid]
    assert bench.spawned[1].pid in bench.alive
    stopped = shard_record(bench, "shard-1")["launches"][-1]["stopped"]
    assert (stopped["step"], stopped["cancelled"]) == (mine, True)
    assert "stopped" not in shard_record(bench, "shard-2")["launches"][-1]


def test_o68_step_stop_signals_nothing_when_the_recorded_pid_is_another_process_now(bench, deploy):
    """O68: the pid the fleet recorded now belongs to a process of another start tick (the client ended and the pid was
    reused): nothing is signalled and nothing is cancelled, the stop is refused whole and says so, and the record
    holds no stop."""
    started(bench)
    mine = list_step(bench, deploy, "shard-1", "fed-1")
    bench.starts[bench.spawned[0].pid] = 99
    code, printed = bench.run("step-stop", "--shard", "shard-1")
    assert code != 0
    assert "another process" in printed
    assert bench.ended == []
    assert bench.fake("fed-1").cancelled == []
    assert mine in bench.fake("fed-1").steps.values()
    assert "stopped" not in shard_record(bench, "shard-1")["launches"][-1]


@pytest.mark.parametrize("change", ["another job", "state of another federation"])
def test_o68_step_stop_touches_nothing_of_a_job_that_is_not_the_federations(bench, deploy, change):
    """O68: Slurm reused the agent's job id for another job, or the root's state is another federation's: the fleet
    never steps into it, so no client is signalled and no step is cancelled."""
    started(bench)
    mine = list_step(bench, deploy, "shard-1", "fed-1")
    if change == "another job":
        bench.fake("fed-1").submitted[job_ids("fed-1")[1]][0]["--job-name"] = "someone-elses-job"
    else:
        path = bench.root("fed-1") / "hpc-federation.json"
        replaced = json.loads(path.read_text(encoding="utf-8"))
        replaced["allocation"]["name"] = "hpcop-another-federation-0000"
        path.write_text(json.dumps(replaced), encoding="utf-8")
    code, printed = bench.run("step-stop", "--shard", "shard-1")
    assert code != 0
    assert bench.ended == [] and bench.fake("fed-1").cancelled == []
    assert mine in bench.fake("fed-1").steps.values()


def test_o68_step_stop_that_cannot_cancel_the_step_still_ends_the_client_and_says_so(bench, deploy):
    """O68: Slurm refuses the step's cancel: the client is ended all the same, the stop is recorded as not cancelled,
    and the command exits 1 naming the step that may still run."""
    started(bench)
    mine = list_step(bench, deploy, "shard-1", "fed-1")
    bench.fake("fed-1").step_cancel_rc = 225
    code, printed = bench.run("step-stop", "--shard", "shard-1")
    assert code == 1
    assert mine in printed and "not cancelled" in printed
    assert [c.pid for c in bench.ended] == [bench.spawned[0].pid]
    stopped = shard_record(bench, "shard-1")["launches"][-1]["stopped"]
    assert (stopped["step"], stopped["cancelled"]) == (mine, False)


@pytest.mark.parametrize("shard", ["shard-9", "shard-1"])
def test_o68_step_stop_refuses_a_shard_the_fleet_has_no_launch_of(bench, shard):
    """O68: a shard that is not the fleet's, or one planned and never launched, has no recorded client and no recorded
    step: refused, nothing signalled or cancelled."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    code, printed = bench.run("step-stop", "--shard", shard)
    assert code == 2
    assert "error: step-stop:" in printed
    assert bench.ended == [] and all(f.cancelled == [] for f in bench.fakes.values())


def test_o68_step_stop_needs_the_shard_it_stops(bench):
    """O68: `step-stop` with no `--shard` is refused before anything is read: it stops one shard's step, never all."""
    started(bench)
    code, printed = bench.run("step-stop")
    assert code == 2
    assert "step-stop needs --shard" in printed
    assert bench.ended == []


def test_o68_status_says_a_step_was_stopped(bench, deploy):
    """O68: `status` of a shard whose latest launch was stopped says so, beside the client's end."""
    started(bench)
    list_step(bench, deploy, "shard-1", "fed-1")
    assert bench.run("step-stop", "--shard", "shard-1")[0] == 0
    code, printed = bench.run("status")
    assert code == 0
    assert "shard-1: on fed-1, its step ended (stopped by step-stop)" in printed


def test_o68_the_stuck_shard_procedure_runs_through_the_fleet_alone(bench, deploy):
    """O68: the README's procedure end to end: shard-1's step is stuck (client alive, step listed, mark left); step-stop
    ends both, fed-1 goes down, a spare comes up, the plan gives it the shard, and `step --resume` launches it there
    with the mark's nonce as `--over`; shard-2 runs on, untouched."""
    started(bench)
    mark = leave_mark(bench, deploy, "shard-1")
    mine = list_step(bench, deploy, "shard-1", "fed-1")
    assert bench.run("step", "--shard", "shard-1", "--resume")[0] == 1  # stuck: its client still runs
    assert bench.run("step-stop", "--shard", "shard-1")[0] == 0
    assert bench.fake("fed-1").cancelled == [mine]
    move_to_a_spare(bench)
    code, printed = bench.run("step", "--shard", "shard-1", "--resume")
    assert code == 0, printed
    moved = bench.spawned[-1]
    assert (moved.label, over_of(moved.argv)) == ("fed-3", mark["nonce"])
    assert bench.spawned[1].pid in bench.alive  # shard-2's client was never signalled
    assert [c.pid for c in bench.ended] == [bench.spawned[0].pid]


# ------------------------------------------------------------------------------------------------ the real comparison
def test_o68_a_recorded_client_is_alive_gone_or_another_process_by_its_start_tick(deploy, tmp_path):
    """O68: the login node's Runner on a real process: the recorded pid and start tick are alive; the same pid with
    another start tick (a reused pid) is another process, which is never signalled; once ended it is gone."""
    import hpcslurm

    runner = hpcslurm.Runner()
    client = runner.spawn(["sleep", "30"], str(tmp_path / "client.log"))
    try:
        assert runner.client_state(client) == hpcslurm.ALIVE
        assert runner.client_state(hpcslurm.Client(client.pid, client.start + 1)) == hpcslurm.ANOTHER
        assert runner.client_state(hpcslurm.Client(client.pid, -1)) == hpcslurm.ANOTHER  # its tick was never read
        assert runner.client_alive(client)
    finally:
        assert runner.end_client(client, grace_s=5.0)
    assert runner.client_state(client) == hpcslurm.GONE
