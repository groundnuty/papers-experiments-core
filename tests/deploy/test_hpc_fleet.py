"""O66: the fleet launcher (deploy/hpc-federation/fleet.py) brings N federations up from one layout, each its own root,
state file and held job, refuses past the operator's maximum and the inode guard before anything is made, keeps the
others up when one fails, and takes down only what its own up made, each verified.

Every federation runs over a fake cluster of its own (`hpc_fleet_support.Bench`); nothing reaches a cluster."""

import json
import os
import shutil
import sys
import threading
from types import SimpleNamespace

import pytest
from hpc_fed_support import LAYOUT, data_stand_in
from hpc_fleet_support import fleet_modules, job_ids, make_bench, make_ca
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


def state(bench, label: str) -> dict:
    return json.loads((bench.root(label) / "hpc-federation.json").read_text(encoding="utf-8"))


def submitted(bench) -> dict:
    return {label: list(fake.submitted) for label, fake in bench.fakes.items()}


# ------------------------------------------------------------------------------------------------ up
def test_o66_up_brings_n_federations_up_each_its_own_root_state_file_and_held_job(bench):
    """O66: three federations from one layout: each root `<fleet>/fed-k/root`, its state linked, one held job of its
    own (its own fake cluster's), a job name with up's random part, all recorded by the fleet."""
    code, printed = bench.up(3)
    assert code == 0, printed
    found = record(bench)
    assert [(m["label"], m["status"]) for m in found["members"]] == [("fed-1", "up"), ("fed-2", "up"), ("fed-3", "up")]
    names = set()
    for member in found["members"]:
        label = member["label"]
        assert member["root"] == str(bench.root(label))
        assert state(bench, label)["phase"] == "linked"
        assert (bench.root(label) / "platform.toml").is_file()
        assert list(bench.fake(label).submitted) == [job_ids(label)[0]]
        assert member["job"] == [job_ids(label)[0]]
        assert member["name"] == state(bench, label)["allocation"]["name"]
        names.add(member["name"])
    assert len(names) == 3
    assert [s["label"] for s in found["shards"]] == ["shard-1", "shard-2", "shard-3"]
    assert all(s["member"] is None for s in found["shards"])


@pytest.mark.parametrize(("maximum", "count"), [("2", 3), ("5", 6)])
def test_o66_up_past_the_maximum_is_refused_before_anything_is_made(bench, maximum, count):
    """O66: N past the operator's maximum: refused, no fleet directory, no sbatch on any cluster."""
    code, printed = bench.up(count, environ={**bench.environ, "HPC_FLEET_MAX": maximum})
    assert code == 2
    assert f"past the maximum of {maximum} (HPC_FLEET_MAX)" in printed
    assert not bench.fleet.exists()
    assert bench.fakes == {}


@pytest.mark.parametrize("value", [None, "", "0", "five", "65"])
def test_o66_up_without_a_maximum_the_operator_set_is_refused(bench, value):
    environ = {k: v for k, v in bench.environ.items() if k != "HPC_FLEET_MAX"}
    if value is not None:
        environ["HPC_FLEET_MAX"] = value
    code, printed = bench.up(2, environ=environ)
    assert code == 2
    assert "HPC_FLEET_MAX" in printed
    assert not bench.fleet.exists()


def test_o66_up_refused_by_the_inode_guard_makes_nothing(bench):
    """O66: the guard is asked once for all N federations, before the fleet's directory or any job."""
    asked = []

    def refuse(count, layout, site, environ):
        asked.append(count)
        raise RuntimeError("the federations' inodes would pass 95% of the account's inode quota on scratch")

    bench.guard = refuse
    code, printed = bench.up(4)
    assert code == 2
    assert asked == [4]
    assert "inode quota" in printed
    assert not bench.fleet.exists()
    assert bench.fakes == {}


def test_o66_up_never_adopts_an_existing_fleet_directory(bench):
    bench.fleet.mkdir()
    code, printed = bench.up(2)
    assert code == 2
    assert "never adopts" in printed
    assert list(bench.fleet.iterdir()) == []
    assert bench.fakes == {}


def test_o66_one_failed_up_leaves_the_others_up_and_says_its_job_may_be_held(bench):
    """O66: fed-2's data is not its manifest's, so its up fails in the core's phase, after its sbatch: fed-1 and fed-3
    stay up, fed-2 is recorded as failed, and its held job is named, never cancelled by the fleet behind its back."""
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    code, printed = bench.up(3)
    assert code == 1
    found = {m["label"]: m for m in record(bench)["members"]}
    assert (found["fed-1"]["status"], found["fed-2"]["status"], found["fed-3"]["status"]) == ("up", "failed", "up")
    assert found["fed-2"]["job"] == [job_ids("fed-2")[0]]
    assert f"its job(s) {job_ids('fed-2')[0]} may still be held: `fleet.py down --member fed-2`" in printed
    assert bench.fake("fed-2").cancelled == []
    for label in ("fed-1", "fed-3"):
        assert state(bench, label)["phase"] == "linked"
        assert bench.fake(label).cancelled == []


def test_o66_up_runs_at_most_the_parallel_setting_at_once(bench):
    """O66: HPC_FLEET_PARALLEL=2 over four federations: two ups at once, never a third, every one run."""
    lock, entered, gate, full = threading.Lock(), [], threading.Event(), threading.Event()
    inflight = {"now": 0, "most": 0}

    def up(site):
        with lock:
            inflight["now"] += 1
            inflight["most"] = max(inflight["most"], inflight["now"])
            entered.append(1)
            if inflight["now"] == 2:
                full.set()
        assert gate.wait(30), "the test never opened the gate"
        with lock:
            inflight["now"] -= 1
        return "stub up"

    def driver(label, layout, root, agent):
        return SimpleNamespace(up=up, say=None)

    environ = {**bench.environ, "HPC_FLEET_PARALLEL": "2"}
    result = {}
    runner = threading.Thread(target=lambda: result.update(code=bench.up(4, environ=environ, driver=driver)))
    runner.start()
    assert full.wait(30)
    with lock:
        assert len(entered) == 2  # the third waits for a free worker
    gate.set()
    runner.join(30)
    assert not runner.is_alive()
    assert inflight["most"] == 2
    assert len(entered) == 4
    assert result["code"][0] == 1  # the stubs leave no linked state: each up counts as failed, which is not the test


# ------------------------------------------------------------------------------------------------ add
def test_o66_add_counts_every_federation_that_may_hold_a_job_against_the_maximum(bench):
    """O66: a failed federation may still hold its job, so it counts; one taken down does not."""
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    environ = {**bench.environ, "HPC_FLEET_MAX": "3"}
    assert bench.up(2, environ=environ)[0] == 1
    code, printed = bench.run("add", "2", environ=environ)
    assert code == 2
    assert "2 federation(s) may hold a job and 2 more were asked for, past the maximum of 3" in printed
    assert "fed-3" not in bench.fakes
    assert bench.run("down", "--member", "fed-2", environ=environ)[0] == 0
    code, printed = bench.run("add", "2", environ=environ)
    assert code == 0, printed
    assert [m["status"] for m in record(bench)["members"]] == ["up", "down", "up", "up"]


def test_o66_add_never_adopts_a_slot_someone_else_made(bench):
    """O66: fed-3's slot made by hand before the add: never brought up, never taken down, left as it is."""
    assert bench.up(2)[0] == 0
    (bench.fleet / "fed-3" / "root").mkdir(parents=True)
    (bench.fleet / "fed-3" / "root" / "theirs").write_text("not the fleet's\n", encoding="utf-8")
    code, printed = bench.run("add", "1")
    assert code == 1
    assert "fed-3: its slot exists and is never adopted" in printed
    assert "fed-3" not in bench.fakes
    assert bench.run("down")[0] == 0
    assert (bench.fleet / "fed-3" / "root" / "theirs").is_file()
    assert "fed-3" not in bench.fakes


# ------------------------------------------------------------------------------------------------ down
def test_o66_down_tears_every_federation_down_each_verified(bench):
    """O66: every federation's driver down: each cluster cancels its own job alone, each root is gone, and the fleet
    records each as down."""
    assert bench.up(3)[0] == 0
    code, printed = bench.run("down")
    assert code == 0, printed
    for label in ("fed-1", "fed-2", "fed-3"):
        assert bench.fake(label).cancelled == [job_ids(label)[0]]
        assert not bench.root(label).exists()
    assert [m["status"] for m in record(bench)["members"]] == ["down", "down", "down"]
    assert "3 of 3 federation(s) down" in printed
    assert bench.run("down")[0] == 0  # nothing left: every one is down already


def test_o66_down_touches_no_federation_and_no_job_it_did_not_make(bench):
    """O66: a federation brought up by hand beside the fleet (its own root, its own cluster) and another user's job on
    a fleet federation's cluster: the fleet's down cancels neither and removes nothing of the first."""
    assert bench.up(2)[0] == 0
    other_root = bench.base / "other" / "root"
    other_root.parent.mkdir()
    other = bench.driver("fed-9", LAYOUT, other_root, True)
    other.up(bench.deploy.hpc.settings.site_from_environment(bench.environ))
    calls = len(bench.fake("fed-9").calls)
    bench.fake("fed-1").submitted["7777"] = [{"--job-name": "someone-elses-job", "--nodes": "1"}]
    assert bench.run("down")[0] == 0
    assert len(bench.fake("fed-9").calls) == calls
    assert bench.fake("fed-9").cancelled == []
    assert (other_root / "hpc-federation.json").is_file()
    assert bench.fake("fed-1").cancelled == [job_ids("fed-1")[0]]
    assert "7777" not in bench.fake("fed-1").cancelled


def test_o66_down_refuses_a_root_whose_state_is_another_federations(bench):
    """O66: fed-2's state file replaced by one of another federation (another job name): fed-2 is not touched and is
    reported, fed-1 goes down, and down fails."""
    assert bench.up(2)[0] == 0
    path = bench.root("fed-2") / "hpc-federation.json"
    replaced = json.loads(path.read_text(encoding="utf-8"))
    replaced["allocation"]["name"] = "hpcop-another-federation-0000"
    path.write_text(json.dumps(replaced), encoding="utf-8")
    code, printed = bench.run("down")
    assert code == 1
    assert "fed-2: NOT TOUCHED: fed-2: its state file is of another federation" in printed
    assert bench.fake("fed-2").cancelled == []
    assert bench.root("fed-2").is_dir()
    assert bench.fake("fed-1").cancelled == [job_ids("fed-1")[0]]
    assert [m["status"] for m in record(bench)["members"]] == ["down", "down-failed"]


def test_o66_down_refuses_a_root_that_is_not_the_directory_the_fleet_made(bench):
    """O66: fed-2's root moved away and another directory put in its place, its state copied: not the fleet's mkdir
    (device and inode), so it is not touched."""
    assert bench.up(2)[0] == 0
    root = bench.root("fed-2")
    os.rename(root, root.with_name("moved"))
    root.mkdir()
    shutil.copyfile(root.with_name("moved") / "hpc-federation.json", root / "hpc-federation.json")
    code, printed = bench.run("down")
    assert code == 1
    assert "fed-2: its root is not the directory this fleet made" in printed
    assert bench.fake("fed-2").cancelled == []


def test_o66_a_record_that_names_a_root_the_fleet_does_not_make_is_refused_whole(bench):
    """O66: the record is the only list down acts on, and it accepts no root but `<fleet>/fed-k/root`."""
    assert bench.up(2)[0] == 0
    path = bench.fleet / "fleet.json"
    edited = json.loads(path.read_text(encoding="utf-8"))
    edited["members"][1]["root"] = str(bench.base / "elsewhere" / "root")
    path.write_text(json.dumps(edited), encoding="utf-8")
    code, printed = bench.run("down")
    assert code == 2
    assert "is not a root this fleet makes" in printed
    assert all(fake.cancelled == [] for fake in bench.fakes.values())


def test_o66_down_of_one_member_leaves_the_others_up(bench):
    assert bench.up(3)[0] == 0
    assert bench.run("down", "--member", "fed-2")[0] == 0
    assert bench.fake("fed-2").cancelled == [job_ids("fed-2")[0]]
    assert bench.fake("fed-1").cancelled == bench.fake("fed-3").cancelled == []
    assert [m["status"] for m in record(bench)["members"]] == ["up", "down", "up"]


# ------------------------------------------------------------------------------------------------ the inode guard
def test_o66_the_inode_guard_reads_the_quota_once_for_n_times_one_federations_estimate(deploy, monkeypatch):
    """O66: with the core's inode guard (hpcquota, stood in here), the fleet's check is the account's usage against N
    times one federation's estimate, read once: N ups would each read the same usage."""
    asked = []
    stand_in = SimpleNamespace(
        estimate_inodes=lambda layout, environ: {"alpha-space": 120, "beta-space": 30},
        check_inodes=lambda runner, site, estimate: asked.append(estimate) or f"inode check: estimate {estimate}",
    )
    monkeypatch.setitem(sys.modules, "hpcquota", stand_in)
    site = SimpleNamespace(storage_root="/invented/scratch")
    said = deploy.fleet.inode_guard(object(), 4, object(), site, {})
    assert asked == [600]
    assert said == "inode check: estimate 600"
    assert "HPC_STORAGE_ROOT is not set" in deploy.fleet.inode_guard(object(), 4, object(), SimpleNamespace(), {})


def test_o66_without_the_inode_guard_the_fleet_says_none_was_made(deploy, monkeypatch):
    monkeypatch.setitem(sys.modules, "hpcquota", None)  # an import of it fails, as in a core without it
    said = deploy.fleet.inode_guard(object(), 3, object(), SimpleNamespace(storage_root="/x"), {})
    assert said == "inode check: none: this core has no inode guard (hpcquota)"
