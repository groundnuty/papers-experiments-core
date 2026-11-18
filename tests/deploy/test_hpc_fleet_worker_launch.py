"""#462 step 6: `fleetworker.py launch` starts a federation's worker as one detached srun step of the job its up held,
checked on the federation's own fake cluster (`hpc_fleet_support.Bench`); and the fleet's shard settings take
`home_files`. Nothing reaches a cluster."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from hpc_fleet_support import StepRunner, fleet_modules, job_ids, make_bench, make_ca
from tls_support import HAS_OPENSSL

pytestmark = pytest.mark.skipif(not HAS_OPENSSL, reason="the zone's CA is made with openssl")

NOW = 1_800_000_000.0


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules() as found:
        import fleetplan
        import fleetworker

        yield SimpleNamespace(found=found, fleetplan=fleetplan, fleetworker=fleetworker)


@pytest.fixture(scope="module")
def ca(tmp_path_factory):
    return make_ca(tmp_path_factory.mktemp("ca"))


@pytest.fixture
def bench(deploy, ca, tmp_path, monkeypatch):
    return make_bench(deploy.found, tmp_path, ca, monkeypatch)


def arguments(bench, settings: Path, label: str = "fed-1", remaining_s: float = 7200.0) -> SimpleNamespace:
    return SimpleNamespace(fleet=str(bench.fleet), member=label, settings=str(settings),
                           federation=f"site-a-{label}", channel=str(bench.base / "channels" / label),
                           job_end=NOW + remaining_s)  # fmt: skip


def member(bench, label: str) -> dict:
    found = json.loads((bench.fleet / "fleet.json").read_text(encoding="utf-8"))
    return {m["label"]: m for m in found["members"]}[label]


def test_the_workers_line_is_one_exact_step_of_the_agents_job_bounded_by_the_jobs_remaining_time(deploy, bench):
    """The step goes to the agent's own job and node (`--exact`, never `--overlap`, `--chdir=/`), runs the fleetworker
    `node` verb under `-I -B` behind `env -i`, carries the operator's key tree by its path alone, and its `--time` is
    the job's remaining minutes, rounded up; it is recorded as the member's worker."""
    assert bench.up(2)[0] == 0
    settings = bench.settings('home_files = "/operator/keys"')
    runner = StepRunner(bench, "fed-1")
    assert deploy.fleetworker.launch(arguments(bench, settings, remaining_s=7201.0), runner, lambda: NOW) == 0
    (launched,) = bench.spawned
    argv = launched.argv
    assert f"--jobid={job_ids('fed-1')[1]}" in argv
    assert "--exact" in argv
    assert "--overlap" not in argv
    assert "--chdir=/" in argv
    assert "--time=121" in argv
    assert argv[argv.index("/usr/bin/env") + 1] == "-i"
    node = argv[argv.index("-B") + 1 :]
    assert Path(node[0]).name == "fleetworker.py"
    assert node[1] == "node"
    assert node[node.index("--home-files") + 1] == "/operator/keys"
    assert node[node.index("--federation") + 1] == "site-a-fed-1"
    assert member(bench, "fed-1")["worker"]["pid"] == launched.pid


def test_a_worker_that_runs_is_not_launched_again_and_a_member_that_is_not_up_gets_none(deploy, bench):
    """One worker per federation: a second launch finds the recorded client alive and starts nothing; a member the
    fleet took down is refused, with nothing started."""
    assert bench.up(2)[0] == 0
    settings = bench.settings()
    runner = StepRunner(bench, "fed-1")
    assert deploy.fleetworker.launch(arguments(bench, settings), runner, lambda: NOW) == 0
    assert deploy.fleetworker.launch(arguments(bench, settings), runner, lambda: NOW) == 0
    assert len(bench.spawned) == 1
    assert bench.run("down", "--member", "fed-2")[0] == 0
    assert deploy.fleetworker.launch(arguments(bench, settings, "fed-2"), StepRunner(bench, "fed-2"), lambda: NOW) == 2
    assert len(bench.spawned) == 1


def test_the_fleets_shard_settings_take_home_files(deploy, bench):
    """`home_files` is one of the fleet's optional shard settings: read, and kept for the lines it makes."""
    path = bench.settings('home_files = "/operator/keys"')
    assert deploy.fleetplan.read_settings(path)["home_files"] == "/operator/keys"
