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


def quiet(seconds: float) -> None:
    """The launch's wait on its client, not slept on."""


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules() as found:
        import fleetplan
        import fleetrecord
        import fleetworker
        import hpclaunch

        yield SimpleNamespace(found=found, fleetplan=fleetplan, fleetrecord=fleetrecord, fleetworker=fleetworker,
                              hpclaunch=hpclaunch)  # fmt: skip


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
    assert deploy.fleetworker.launch(arguments(bench, settings, remaining_s=7201.0), runner, lambda: NOW, quiet) == 0
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
    assert deploy.fleetworker.launch(arguments(bench, settings), runner, lambda: NOW, quiet) == 0
    assert deploy.fleetworker.launch(arguments(bench, settings), runner, lambda: NOW, quiet) == 0
    assert len(bench.spawned) == 1
    assert bench.run("down", "--member", "fed-2")[0] == 0
    assert (
        deploy.fleetworker.launch(arguments(bench, settings, "fed-2"), StepRunner(bench, "fed-2"), lambda: NOW, quiet)
        == 2
    )
    assert len(bench.spawned) == 1


def test_the_fleets_shard_settings_take_home_files(deploy, bench):
    """`home_files` is one of the fleet's optional shard settings: read, and kept for the lines it makes."""
    path = bench.settings('home_files = "/operator/keys"')
    assert deploy.fleetplan.read_settings(path)["home_files"] == "/operator/keys"


def test_a_worker_whose_step_ends_at_once_is_no_launch(deploy, bench):
    """`node` refuses a start (an unreadable key tree, a core it cannot run) within seconds of its step starting: a
    client that is gone while the launch watches it is no worker, so the launch fails, records none, and the
    controller's join waits."""
    assert bench.up(2)[0] == 0
    settings = bench.settings()
    runner = StepRunner(bench, "fed-1")
    died = lambda seconds: bench.alive.clear()  # noqa: E731
    assert deploy.fleetworker.launch(arguments(bench, settings), runner, lambda: NOW, died) == 2
    assert member(bench, "fed-1").get("worker") is None


HOP = 'fleet_hop = { login = "h", socket = "/s", key = "/k", known_hosts = "/h", local_port = 30051 }'


def watch_until(bench, tick: int):
    """A launch's wait on its client that ends the client at the given look, and counts the looks."""
    looks = []

    def slept(seconds: float) -> None:
        looks.append(seconds)
        if len(looks) == tick:
            bench.alive.clear()

    return slept, looks


def test_o93_a_worker_whose_hop_never_opens_ends_inside_the_launchs_longer_watch_and_is_no_launch(deploy, bench):
    """O93: `node` waits for the hop's port before it refuses a start, longer than the usual twenty seconds, so with a
    `fleet_hop` setting the launch watches its client for that wait too: a worker that ends forty looks in is no launch,
    records none, and the controller's join waits instead of joining a federation with no worker."""
    assert bench.up(2)[0] == 0
    settings = bench.settings(HOP)
    slept, looks = watch_until(bench, 40)
    runner = StepRunner(bench, "fed-1")
    assert deploy.fleetworker.launch(arguments(bench, settings), runner, lambda: NOW, slept) == 2
    assert len(looks) == 40
    assert member(bench, "fed-1").get("worker") is None


def test_o93_a_launch_without_a_hop_watches_its_client_as_long_as_it_always_did(deploy, bench):
    """O93: with no `fleet_hop` setting the watch is the usual twenty looks: the same worker, ending at the fortieth,
    is past it and is launched."""
    assert bench.up(2)[0] == 0
    slept, looks = watch_until(bench, 40)
    runner = StepRunner(bench, "fed-1")
    assert deploy.fleetworker.launch(arguments(bench, bench.settings()), runner, lambda: NOW, slept) == 0
    assert len(looks) == int(deploy.fleetworker.SETTLE_S)


def test_c57_a_worker_is_launched_while_an_add_holds_the_fleets_lifecycle_lock(deploy, bench):
    """C57: an add holds the fleet's lifecycle lock for its new members' whole ups, the queue wait included (hours on a
    site whose ups queue); the launch of an up member's worker does not take that lock, so the join is not refused
    behind the add while the member idles."""
    assert bench.up(2)[0] == 0
    runner = StepRunner(bench, "fed-1")
    with deploy.fleetrecord.Record(bench.fleet).lifecycle("add"):
        assert deploy.fleetworker.launch(arguments(bench, bench.settings()), runner, lambda: NOW, quiet) == 0
    (launched,) = bench.spawned
    assert member(bench, "fed-1")["worker"]["pid"] == launched.pid


def test_c57_no_worker_is_launched_while_its_federations_up_or_down_runs(deploy, bench):
    """C57: the launch holds its federation's own lifecycle lock (`<root>.lifecycle.lock`, which `hpcfed.py up` and
    `down` hold for their whole run): while a down of that federation runs, the launch is refused with nothing started
    or recorded."""
    assert bench.up(2)[0] == 0
    root = deploy.fleetrecord.root_of(bench.fleet, "fed-1")
    runner = StepRunner(bench, "fed-1")
    with deploy.hpclaunch.StateStore(root).lifecycle("down"):
        assert deploy.fleetworker.launch(arguments(bench, bench.settings()), runner, lambda: NOW, quiet) == 2
    assert bench.spawned == []
    assert member(bench, "fed-1").get("worker") is None


def test_c57_a_down_of_the_federation_is_refused_while_its_worker_launches(deploy, bench):
    """C57: the other way round, `fleet.py down --member` of the federation whose worker is being launched is refused
    by its `hpcfed.py down` (the federation's lifecycle lock) and leaves the federation up; the launch goes on."""
    assert bench.up(2)[0] == 0
    downs = []

    def during(seconds: float) -> None:
        if not downs:
            downs.append(bench.run("down", "--member", "fed-1"))

    runner = StepRunner(bench, "fed-1")
    assert deploy.fleetworker.launch(arguments(bench, bench.settings()), runner, lambda: NOW, during) == 0
    ((code, printed),) = downs
    assert code != 0
    assert "another up or down of this root is running" in printed
    assert deploy.fleetrecord.root_of(bench.fleet, "fed-1").is_dir()
    assert member(bench, "fed-1")["worker"]["pid"] == bench.spawned[0].pid
