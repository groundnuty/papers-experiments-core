"""O68: the fleet's plan gives each shard of one campaign a federation of one platform fingerprint, its step lands on
that federation's own job and node, and a shard moves only to a fresh federation of the fleet's fingerprint whose first
reset is full (deploy/hpc-federation/fleetplan.py).

Every federation runs over a fake cluster of its own (`hpc_fleet_support.Bench`), its platform.toml the one its own
up rendered; the step's srun is stood in by `StepRunner`, which checks each line on its federation's cluster."""

import json

import pytest
from hpc_fed_support import data_stand_in
from hpc_fleet_support import another_ca, fleet_modules, job_ids, make_bench, make_ca
from tls_support import HAS_OPENSSL

pytestmark = pytest.mark.skipif(not HAS_OPENSSL, reason="the zone's CA is made with openssl")


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


def plan(bench, *extra: str) -> tuple[int, str, dict]:
    """`fleet.py plan`: its exit status, what it printed, and each shard's line by its label."""
    code, printed = bench.run("plan", "--manifest", str(bench.settings()), *extra)
    lines = {}
    for text in printed.splitlines():
        if text.startswith("{"):
            entry = json.loads(text)
            lines[entry["shard"]] = entry
    return code, printed, lines


def assigned(bench) -> dict:
    return {s["label"]: s["member"] for s in record(bench)["shards"]}


# ------------------------------------------------------------------------------------------------ plan
def test_o68_plan_gives_each_shard_its_own_federations_job_and_node(bench):
    """O68: shard-k on fed-k: its line an exact step of fed-k's held job's component 1, on the agent's node fed-k's
    state names, labelled and counted as one shard of three; shards.toml written for collect."""
    assert bench.up(3)[0] == 0
    code, printed, lines = plan(bench)
    assert code == 0, printed
    assert assigned(bench) == {"shard-1": "fed-1", "shard-2": "fed-2", "shard-3": "fed-3"}
    for index, label in enumerate(("fed-1", "fed-2", "fed-3"), 1):
        argv = lines[f"shard-{index}"]["argv"]
        assert argv[1:3] == [f"--jobid={job_ids(label)[1]}", "--exact"]  # the agent's own job: no component
        assert not [word for word in argv if word.startswith("--het-group")]
        assert "--chdir=/" in argv[: argv.index("/usr/bin/env")]
        sandbox = json.loads((bench.root(label) / "hpc-federation.json").read_text(encoding="utf-8"))["sandbox"]
        assert argv[argv.index("-w") + 1] == sandbox["node"]
        assert argv[argv.index("--count") + 1] == "3"
        assert argv[argv.index("--label") + 1] == f"shard-{index}"
        assert f"EXPCORE_FED_ROOT={bench.root(label)}" in argv
        bench.fake(label)._step(argv)  # the fake cluster places it on that job's component 1, or fails the test
    manifest = (bench.fleet / "shards.toml").read_text(encoding="utf-8")
    assert manifest.count("[[shards]]") == 3
    reference = record(bench)["reference"]
    assert reference["fingerprint"] == bench.fingerprint("fed-1") == bench.fingerprint("fed-3")


def test_o68_plan_refuses_a_fleet_whose_federations_platform_fingerprints_differ(bench, tmp_path):
    """O68: fed-3's zone is of another image's build (another CA): the federations' platform fingerprints differ in
    their TLS record, so their shards would never be one campaign; nothing is assigned, nothing is written."""
    bench.cas["fed-3"] = another_ca(tmp_path / "other-ca")
    assert bench.up(3)[0] == 0
    code, printed, lines = plan(bench)
    assert code == 2
    assert "the federations' platform fingerprints differ" in printed
    assert "fed-1, fed-2; fed-3 (differing: tls)" in printed
    assert lines == {}
    assert all(member is None for member in assigned(bench).values())
    assert record(bench)["reference"] is None
    assert not (bench.fleet / "shards.toml").exists()


def test_o68_a_failed_federations_shard_is_left_unassigned_and_the_others_planned(bench):
    """O68: fed-2's up failed: shard-1 and shard-3 are planned on their federations, shard-2 is unassigned."""
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    assert bench.up(3)[0] == 1
    code, printed, lines = plan(bench)
    assert code == 1
    assert assigned(bench) == {"shard-1": "fed-1", "shard-2": None, "shard-3": "fed-3"}
    assert sorted(lines) == ["shard-1", "shard-3"]
    assert "shard-2: unassigned: no fresh federation is ready for it" in printed
    assert lines["shard-3"]["argv"][lines["shard-3"]["argv"].index("--count") + 1] == "3"


def test_o68_an_unassigned_shard_goes_to_a_fresh_federation_of_the_fleets_fingerprint(bench):
    """O68: fed-2 failed and was taken down; a fresh fed-4 (its own up, its data checked, the same fingerprint)
    takes shard-2; shard-1 and shard-3 stay where they are."""
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    assert bench.up(3)[0] == 1
    assert plan(bench)[0] == 1
    assert bench.run("down", "--member", "fed-2")[0] == 0
    assert bench.run("add", "1")[0] == 0
    code, printed, lines = plan(bench)
    assert code == 0, printed
    assert assigned(bench) == {"shard-1": "fed-1", "shard-2": "fed-4", "shard-3": "fed-3"}
    assert lines["shard-2"]["argv"][1] == f"--jobid={job_ids('fed-4')[1]}"


def test_o68_a_fresh_federation_of_another_fingerprint_is_never_given_a_shard(bench, tmp_path):
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    bench.cas["fed-4"] = another_ca(tmp_path / "other-ca")
    assert bench.up(3)[0] == 1
    assert plan(bench)[0] == 1
    assert bench.run("down", "--member", "fed-2")[0] == 0
    assert bench.run("add", "1")[0] == 0
    code, printed, _ = plan(bench)
    assert code == 1
    assert "not used: fed-4: its platform fingerprint differs from the fleet's (differing: tls)" in printed
    assert assigned(bench)["shard-2"] is None


def test_o68_plan_refuses_settings_other_than_the_first_plans(bench):
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    other = bench.base / "other-settings.toml"
    other.write_text(bench.settings().read_text(encoding="utf-8").replace("cpus = 2", "cpus = 3"), encoding="utf-8")
    code, printed = bench.run("plan", "--manifest", str(other))
    assert code == 2
    assert "differ from the ones the fleet's first plan read" in printed


# ------------------------------------------------------------------------------------------------ step
def test_o68_step_launches_each_shard_on_its_own_federation_once(bench):
    """O68: each assigned shard's line spawned detached, checked on its own federation's cluster; a second step
    launches nothing while the clients run."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    code, printed = bench.run("step")
    assert code == 0, printed
    assert [(s.label, s.srun.job) for s in bench.spawned] == [("fed-1", job_ids("fed-1")[1]),
                                                              ("fed-2", job_ids("fed-2")[1])]  # fmt: skip
    assert all("--resume" not in s.argv for s in bench.spawned)
    code, printed = bench.run("step")
    assert len(bench.spawned) == 2
    assert "shard-1: its step on fed-1 still runs" in printed


def test_o68_step_checks_the_federation_again_and_refuses_one_no_longer_the_fleets(bench):
    """O68: fed-1's state replaced after the plan: shard-1 is not launched there; shard-2 is, and step exits 1, since
    a shard it was asked to launch was refused."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    path = bench.root("fed-1") / "hpc-federation.json"
    replaced = json.loads(path.read_text(encoding="utf-8"))
    replaced["allocation"]["name"] = "hpcop-another-federation-0000"
    path.write_text(json.dumps(replaced), encoding="utf-8")
    code, printed = bench.run("step")
    assert code == 1
    assert "shard-1: not launched: fed-1: its state file is of another federation" in printed
    assert [s.label for s in bench.spawned] == ["fed-2"]


@pytest.mark.parametrize("now", ["another job", "gone", "pending"])
def test_o68_step_never_launches_into_a_job_slurm_no_longer_holds_for_the_federation(bench, now):
    """O68: everything on file still says fed-1, but Slurm now says otherwise of its agent's job: its id handed to
    another job (Slurm reuses ids), the job gone, or not running. shard-1 is not launched; shard-2 is; step exits 1."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    fake, agent = bench.fake("fed-1"), job_ids("fed-1")[1]
    if now == "another job":
        fake.submitted[agent][0]["--job-name"] = "someone-elses-job"
    elif now == "gone":
        fake.job_states[agent] = "GONE"
    else:
        fake.job_states[agent] = "PENDING"
    code, printed = bench.run("step")
    assert code == 1
    assert "shard-1: not launched: " in printed
    assert [s.label for s in bench.spawned] == ["fed-2"]
    assert all(s.srun.job != agent for s in bench.spawned)


def test_o68_step_launches_nothing_from_a_shard_manifest_the_record_does_not_give(bench):
    """O68: shards.toml edited after the plan to give shard-1 fed-2's root (a linked federation of the fleet's): step
    refuses whole and launches nothing, since a line built from it would land in a job the fleet never checked."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    path = bench.fleet / "shards.toml"
    text = path.read_text(encoding="utf-8")
    assert text.count(f'root = "{bench.root("fed-1")}"') == 1
    path.write_text(
        text.replace(f'root = "{bench.root("fed-1")}"', f'root = "{bench.root("fed-2")}"'), encoding="utf-8"
    )
    code, printed = bench.run("step")
    assert code == 2
    assert "the fleet's shards.toml is not the one its record gives" in printed
    assert bench.spawned == []


def test_o68_a_moved_shard_resumes_on_a_fresh_federation_whose_first_reset_is_full(bench):
    """O68: shard-1 ran on fed-1, which is taken down: it is released, given to the fresh fed-3 (the same
    fingerprint, [placement] reset_shortcut off), and its step there resumes; shard-2 runs on, untouched."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    assert bench.run("step")[0] == 0
    bench.alive.discard(bench.spawned[0].pid)  # fed-1's job is cancelled: its step and client end
    assert bench.run("down", "--member", "fed-1")[0] == 0
    shard = {s["label"]: s for s in record(bench)["shards"]}["shard-1"]
    assert (shard["member"], shard["last"]) == (None, "fed-1")
    assert bench.run("add", "1")[0] == 0
    code, printed, _ = plan(bench)
    assert code == 0, printed
    assert assigned(bench) == {"shard-1": "fed-3", "shard-2": "fed-2"}
    assert {s["label"]: s for s in record(bench)["shards"]}["shard-1"]["moved"] is True
    code, printed = bench.run("step")
    assert code == 0, printed
    moved = bench.spawned[-1]
    assert (moved.label, moved.argv[-1]) == ("fed-3", "--resume")
    assert "moved from fed-1: fed-3's platform.toml leaves [placement] reset_shortcut off" in printed
    assert len(bench.spawned) == 3  # shard-2's step still runs on fed-2: not launched again


def test_o68_a_shard_is_never_moved_to_a_federation_whose_reset_shortcut_is_on(bench):
    """O68: a moved shard's first reset must be full; fed-3's platform.toml turns the shortcut on: refused. The
    shortcut is no part of the platform fingerprint (C35) but the data provider is (C06), so every federation here
    declares the same `[placement]` and fed-3's differs by the shortcut alone: the fingerprints are one, and it is the
    fleet's own check that refuses the move."""
    placement = '\n[placement]\nholder = "aspen"\n'
    assert bench.up(2)[0] == 0
    for label in ("fed-1", "fed-2"):
        platform = bench.root(label) / "platform.toml"
        platform.write_text(platform.read_text(encoding="utf-8") + placement, encoding="utf-8")
    assert plan(bench)[0] == 0
    assert bench.run("down", "--member", "fed-1")[0] == 0
    assert bench.run("add", "1")[0] == 0
    platform = bench.root("fed-3") / "platform.toml"
    platform.write_text(platform.read_text(encoding="utf-8") + placement + "reset_shortcut = true\n", encoding="utf-8")
    code, printed, _ = plan(bench)
    assert code == 1
    assert "shard-1: not moved to fed-3: its platform.toml turns [placement] reset_shortcut on" in printed
    assert assigned(bench)["shard-1"] is None


def test_o68_step_relaunches_an_ended_shard_only_with_resume(bench):
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    assert bench.run("step")[0] == 0
    bench.alive.discard(bench.spawned[0].pid)
    code, printed = bench.run("step", "--shard", "shard-1")
    assert code == 1
    assert "has ended; `fleet.py step --shard shard-1 --resume` relaunches" in printed
    code, printed = bench.run("step", "--shard", "shard-1", "--resume")
    assert code == 0, printed
    assert bench.spawned[-1].argv[-1] == "--resume"
    assert len(bench.spawned) == 3


# ------------------------------------------------------------------------------------------------ status
def test_o68_status_says_each_federation_its_job_and_shard_and_runs_nothing(bench):
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    assert bench.up(3)[0] == 1
    assert plan(bench)[0] == 1
    assert bench.run("step")[0] == 0
    calls = {label: len(fake.calls) for label, fake in bench.fakes.items()}
    code, printed = bench.run("status")
    assert code == 0
    assert f"fed-1: up, phase linked, job(s) {', '.join(job_ids('fed-1'))}, shard shard-1" in printed
    assert "fed-2: failed, phase " in printed
    assert job_ids("fed-2")[0] in printed
    assert "shard-1: on fed-1, its step runs" in printed
    assert "shard-2: unassigned" in printed
    assert {label: len(fake.calls) for label, fake in bench.fakes.items()} == calls
